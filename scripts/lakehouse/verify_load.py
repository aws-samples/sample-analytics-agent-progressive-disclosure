#!/usr/bin/env python3
"""装载完整性：`data/csv/*.csv` 真源 ⟷ Athena 现查的 Iceberg 表，逐列比。

## 为什么不用 scripts/consistency/snapshot.py

那个脚本的思路是「迁移前存一份基线 JSON，迁移后比」。在这条链路上它有两个问题：

1. **基线会过期，而且过期时是绿的**。`eval/baseline/consistency.generator-expected.json`
   描述的是 v2 那批数据（users 213,520 行，scale≈427）；本仓库 `data/csv/` 里是
   500 行。L3 原来拿两份**互相一致但都不描述当前部署**的 JSON 对比，稳定通过，
   什么也没验证。写死的数字总会变成这样。
2. 它的两个 backend（本地 Postgres、Redshift Data API）在这套架构里都退役了。

所以这里改成**两边都现算**：CSV 侧用 Python，Iceberg 侧用 Athena 聚合，当场比。
没有中间文件，也就没有会过期的东西。数据重灌、重新生成，这个检查照样成立。
（同一个道理见 `verify_mart_parity.py` 为什么把 GMV 写成恒等式而不是字面量。）

## 比什么

对 35 张基表逐表比：

| 指标 | 覆盖的列 | 抓什么 |
|---|---|---|
| `count` | 全表 | 行数少了/多了（COPY 半途失败、重复装载） |
| `sum:<列>` | 整数 / decimal | 数值精度丢失（decimal 标度被截、整数溢出） |
| `min:<列>` / `max:<列>` | date / timestamp | 时区偏移、日期解析错位 |
| `true:<列>` | boolean | `t`/`f` 解析反了 |

varchar / array 列不取指标：跨方言表示不稳、代价高，而上面四项已经能暴露装载偏移。
**这一点是刻意的取舍，不是遗漏**——字符串内容的正确性由 eval 的金标查询覆盖。

## 外加一条：文档里写的行数必须等于 CSV 实测和

`--selftest` 里还有 `check_doc_totals()`，不连云，比的是 `knowledge/connection.md`
第一段那几个数 ⟷ `data/csv/` 现数。要它是因为**那几个数是 agent 读的**：它跑在治理
角色下，`user_messages` 数不到，所以它没法自己验证全库到底多少行——只能照抄卡片。
卡片写错时的表现是它自信地报一个错的规模，没有任何东西会红。

顺带钉住那份文档里的算术：全库 = 原始 + 派生、治理视角 = 全量 − `user_messages`。
那个减法就是「界面显示 18 万而文档写 19 万」那次的成因，写下来是为了下次不用重新推。

## 两个精度陷阱

- **求和一律走 `decimal.Decimal`，不用 float**。19 万行 float 累加会攒出尾数误差，
  对账时全表假阳性。Athena 侧同理，先 `CAST(col AS DECIMAL(38,4))` 再 `SUM`。
- **时间只比到秒**。CSV 里是 `2026-01-07 14:11:10.53532`，Athena 的
  `format_datetime(..., 'HH:mm:ss')` 截断到秒。两边都截断（不是四舍五入），
  所以比得上；要是一边截断一边进位，`.999999` 那种值会假失败。

用法：

    python3 scripts/lakehouse/verify_load.py                 # 全部 35 张表
    python3 scripts/lakehouse/verify_load.py -t users -t orders
    python3 scripts/lakehouse/verify_load.py --selftest      # 分类/归一自测（无云依赖）
"""
from __future__ import annotations

import argparse
import csv
import decimal
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

CSV_DIR = ROOT / "data" / "csv"
SCALE = decimal.Decimal("0.0001")          # 与 Athena 的 DECIMAL(38,4) 对齐

# CSV 里的 NULL 就是空字段。生成器不写 \N，也不写 'NULL' 字面量。
NULLS = {""}

# CSV 的布尔写法（Postgres COPY 风格）
TRUE_TOKENS = {"t", "true", "1"}
FALSE_TOKENS = {"f", "false", "0"}


# ---------------------------------------------------------------- 列分类

def classify(pg_type: str) -> str:
    """Postgres 类型 → 本脚本的指标类别：num / ts / date / bool / skip。

    分类依据是**真源 DDL 的 Postgres 类型**，不是 Athena 的
    `information_schema`——后者把 `VARCHAR(50)` 和 `TEXT` 都显示成 `varchar`，
    信息更少，而且多绕一次云调用。

    数组类型必须在标量之前判掉：`INT[]` 的前缀是 `INT`，不先剥 `[]` 会被
    当成整数列去求和，然后在 Athena 上报类型错——错得还很难看懂。
    """
    t = " ".join(pg_type.split()).upper()
    if t.endswith("[]"):
        return "skip"
    if re.match(r"^(DECIMAL|NUMERIC)\s*\(", t) or t in (
            "INT", "INTEGER", "INT4", "INT8", "BIGINT", "SMALLINT",
            "SERIAL", "BIGSERIAL", "REAL", "DOUBLE PRECISION"):
        return "num"
    if t in ("TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITHOUT TIME ZONE",
             "TIMESTAMP WITH TIME ZONE"):
        return "ts"
    if t == "DATE":
        return "date"
    if t in ("BOOLEAN", "BOOL"):
        return "bool"
    return "skip"                          # varchar / text / json / uuid …


def norm_ts(s: str) -> str:
    """`2026-01-07 14:11:10.53532` → `2026-01-07T14:11:10`（截断到秒，不进位）。

    截断而非四舍五入是为了和 Athena 的 `format_datetime(..., 'HH:mm:ss')` 一致。
    """
    s = s.strip().replace(" ", "T")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
    return m.group(1) if m else s


def norm_date(s: str) -> str:
    return s.strip()[:10]


def fmt_sum(total: decimal.Decimal) -> str:
    """统一成 4 位小数字符串。Athena 侧的 `CAST(... AS VARCHAR)` 出来就是这个形状。"""
    return f"{total.quantize(SCALE):f}"


# ---------------------------------------------------------------- CSV 侧

def csv_metrics(table: str, cols: list[tuple[str, str]]) -> dict[str, object]:
    """扫一遍 CSV，算出与 Athena 侧同名的指标。

    `cols` 是 [(列名, Postgres 类型)]，顺序取自 DDL。CSV 表头必须与之一致——
    这一点由 `load.py --preflight` 单独守着，这里只按表头取值，不再重复校验。
    """
    path = CSV_DIR / f"{table}.csv"
    kinds = {c: classify(t) for c, t in cols}

    n = 0
    sums: dict[str, decimal.Decimal] = {}
    lo: dict[str, str] = {}
    hi: dict[str, str] = {}
    trues: dict[str, int] = {}

    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            for col, kind in kinds.items():
                if kind == "skip":
                    continue
                raw = row.get(col)
                if raw is None or raw in NULLS:
                    continue               # SUM/MIN/MAX 都忽略 NULL，与 SQL 一致
                if kind == "num":
                    sums[col] = sums.get(col, decimal.Decimal(0)) + decimal.Decimal(raw)
                elif kind in ("ts", "date"):
                    v = norm_ts(raw) if kind == "ts" else norm_date(raw)
                    if col not in lo or v < lo[col]:
                        lo[col] = v
                    if col not in hi or v > hi[col]:
                        hi[col] = v
                elif kind == "bool":
                    tok = raw.strip().lower()
                    if tok in TRUE_TOKENS:
                        trues[col] = trues.get(col, 0) + 1
                    elif tok not in FALSE_TOKENS:
                        raise ValueError(f"{table}.{col} 不是布尔值：{raw!r}")

    out: dict[str, object] = {"count": n}
    # 只报 CSV 里真出现过非空值的列。全 NULL 的列两边都是 NULL，比它没有信息量，
    # 而且 SQL 的 SUM(全 NULL) 返回 NULL、Python 这边返回 0，会造出一处假差异。
    for col, v in sums.items():
        out[f"sum:{col}"] = fmt_sum(v)
    for col in lo:
        out[f"min:{col}"] = lo[col]
        out[f"max:{col}"] = hi[col]
    for col, v in trues.items():
        out[f"true:{col}"] = v
    return out


# ---------------------------------------------------------------- Athena 侧

def athena_probe(table: str, cols: list[tuple[str, str]],
                 want: set[str]) -> tuple[str, list[str]]:
    """拼一条聚合 SQL，只取 `want` 里的指标（= CSV 侧真算出来的那些）。

    只取 CSV 侧有的指标，两边的指标集合就天然对齐，diff 里剩下的全是真差异。
    """
    kinds = {c: classify(t) for c, t in cols}
    sel, labels = ["COUNT(*)"], ["count"]
    for col, kind in kinds.items():
        if kind == "num" and f"sum:{col}" in want:
            # 先定点化再求和：Trino 的 decimal 求和精确，double 求和依赖分片顺序
            sel.append(f'CAST(SUM(CAST("{col}" AS DECIMAL(38,4))) AS VARCHAR)')
            labels.append(f"sum:{col}")
        elif kind in ("ts", "date") and f"min:{col}" in want:
            # Trino 没有 to_char。format_datetime 用 Joda 模式，字面量 T 要加单引号，
            # 而单引号在 SQL 字符串里要写成两个 —— 于是长成 ''T'' 这样。
            fmt = "yyyy-MM-dd" if kind == "date" else "yyyy-MM-dd''T''HH:mm:ss"
            for agg, tag in (("MIN", "min"), ("MAX", "max")):
                sel.append(f"format_datetime(CAST({agg}(\"{col}\") AS timestamp), '{fmt}')")
                labels.append(f"{tag}:{col}")
        elif kind == "bool" and f"true:{col}" in want:
            sel.append(f'SUM(CASE WHEN "{col}" THEN 1 ELSE 0 END)')
            labels.append(f"true:{col}")
    return f'SELECT {", ".join(sel)} FROM "{table}"', labels


def athena_metrics(client, table: str, cols: list[tuple[str, str]],
                   want: set[str]) -> dict[str, object]:
    sql, labels = athena_probe(table, cols, want)
    row = client.execute(sql)["rows"][0]
    out: dict[str, object] = {}
    for label, val in zip(labels, row):
        if label == "count" or label.startswith("true:"):
            out[label] = int(val) if val is not None else 0
        else:
            out[label] = val
    return out


# ------------------------------------------------- 文档行数 ⟷ CSV 实测（离线）

DOC = ROOT / "knowledge" / "connection.md"

# 治理层不授权、因此 agent 和 /api/catalog 都数不到的那张表。
# 写死这一个名字是刻意的：这条检查要验的正是「文档里解释的那个差额对不对」，
# 而那个解释本身就点了这张表的名。表名变了这里会因为找不到 CSV 而红，那是对的。
UNGRANTED_TABLE = "user_messages"

_DOC_PATTERNS = {
    "all_rows":   r"全库 \*\*([\d,]+) 行 / (\d+) 张表\*\*",
    "base":       r"(\d+) 张原始表 ([\d,]+) 行",
    "derived":    r"(\d+) 张派生表[^\d]*([\d,]+) 行",
    "governed":   r"视角数出来的全库是 \*\*([\d,]+) 行\*\*、原始表 \*\*([\d,]+) 行\*\*",
    "ungranted":  r"`" + UNGRANTED_TABLE + r"`（([\d,]+) 行）",
}


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def csv_row_counts() -> dict[str, int]:
    """`data/csv/*.csv` 每张表的数据行数（不含表头）。"""
    out: dict[str, int] = {}
    for p in sorted(CSV_DIR.glob("*.csv")):
        with p.open(encoding="utf-8", newline="") as f:
            out[p.stem] = max(sum(1 for _ in csv.reader(f)) - 1, 0)
    return out


def check_doc_totals() -> list[str]:
    """比 `knowledge/connection.md` 的行数声明 ⟷ CSV 实测。返回问题清单（空 = 通过）。"""
    if not DOC.is_file():
        return [f"找不到 {DOC.relative_to(ROOT)}"]
    text = DOC.read_text(encoding="utf-8")

    got: dict[str, tuple[int, ...]] = {}
    for key, pat in _DOC_PATTERNS.items():
        ms = re.findall(pat, text)
        # 锚点必须恰好命中一次。命中 0 次说明文档被改写了，命中多次说明同一个数在
        # 两处各写了一份——两种情况都不能"挑第一个用"，那会让这条检查悄悄失去意义。
        if len(ms) != 1:
            return [f"{DOC.relative_to(ROOT)} 里 /{pat}/ 命中 {len(ms)} 次（应为 1）："
                    "文档被改写了，这条断言已经不指向那几个数，先修断言"]
        m = ms[0]
        got[key] = tuple(_num(x) for x in (m if isinstance(m, tuple) else (m,)))

    counts = csv_row_counts()
    csv_rows, csv_files = sum(counts.values()), len(counts)
    doc_all_rows, doc_all_tables = got["all_rows"]
    doc_base_tables, doc_base_rows = got["base"]
    doc_der_tables, doc_der_rows = got["derived"]
    doc_gov_all, doc_gov_base = got["governed"]
    (doc_ungranted,) = got["ungranted"]

    bad: list[str] = []

    def eq(label: str, doc: int, real: int, how: str) -> None:
        if doc != real:
            bad.append(f"{label}：文档写 {doc:,}，{how} = {real:,}")

    # 一、文档 ⟷ 真源。CSV 是真源，文档是抄的那一份。
    eq("原始表行数", doc_base_rows, csv_rows, "data/csv/ 现数")
    eq("原始表张数", doc_base_tables, csv_files, "data/csv/ 的文件数")
    eq(f"{UNGRANTED_TABLE} 行数", doc_ungranted, counts.get(UNGRANTED_TABLE, -1),
       f"data/csv/{UNGRANTED_TABLE}.csv")

    # 二、文档内部的算术。派生层没有 CSV 真源（是 CTAS 出来的），所以只能这样自洽校验。
    eq("全库行数", doc_all_rows, doc_base_rows + doc_der_rows, "原始 + 派生")
    eq("全库张数", doc_all_tables, doc_base_tables + doc_der_tables, "原始 + 派生")

    # 三、治理视角 = 全量 − 那张数不到的表。这就是「界面 18 万 ⟷ 文档 19 万」的成因，
    # 钉住它，免得下次有人把差额改成一个凑出来的数。
    eq("治理视角全库", doc_gov_all, doc_all_rows - doc_ungranted,
       f"全库 − {UNGRANTED_TABLE}")
    eq("治理视角原始表", doc_gov_base, doc_base_rows - doc_ungranted,
       f"原始表 − {UNGRANTED_TABLE}")
    return bad


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="CSV 真源 ⟷ Athena 装载完整性对账")
    ap.add_argument("-t", "--table", action="append", default=[],
                    help="只查这些表（可重复）；默认全部")
    ap.add_argument("--selftest", action="store_true", help="分类/归一自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    import gen_ddl

    declared = {t: [(c, pg) for c, pg, _nn, _n in cols]
                for _src, t, cols in gen_ddl.parse_source()}
    tables = a.table or sorted(declared)

    missing = [t for t in tables if t not in declared]
    if missing:
        print(f"这些表不在 DDL 真源里：{missing}")
        return 2

    import athena
    client = athena.Client()

    diffs: list[str] = []
    checked = 0
    for t in tables:
        cols = declared[t]
        if not (CSV_DIR / f"{t}.csv").exists():
            print(f"  ·  {t:<28} 跳过（没有 CSV）")
            continue
        exp = csv_metrics(t, cols)
        got = athena_metrics(client, t, cols, set(exp))
        bad = [f"{t}.{k}：CSV={exp[k]}  Athena={got.get(k, '<无>')}"
               for k in sorted(exp) if str(exp[k]) != str(got.get(k, "<无>"))]
        checked += 1
        if bad:
            diffs += bad
            print(f"  ❌ {t:<28} {len(exp)} 项指标，{len(bad)} 项不符")
        else:
            print(f"  ✅ {t:<28} {len(exp)} 项指标全等  count={exp['count']}")

    print()
    if diffs:
        print(f"发现 {len(diffs)} 处差异 ❌（CSV 是真源，Athena 是装载结果）\n")
        for d in diffs[:40]:
            print(f"  - {d}")
        if len(diffs) > 40:
            print(f"  …… 另有 {len(diffs) - 40} 处")
        return 1
    print(f"装载完整 ✅  {checked} 张表的行数、数值求和、时间边界、布尔计数全等")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    bad = 0

    for pg, want in [
        ("INT", "num"), ("BIGINT", "num"), ("SERIAL", "num"),
        ("DECIMAL(12,2)", "num"), ("DECIMAL(38,4)", "num"),
        ("DOUBLE PRECISION", "num"),
        ("TIMESTAMP", "ts"), ("timestamp", "ts"),
        ("DATE", "date"),
        ("BOOLEAN", "bool"), ("BOOL", "bool"),
        ("VARCHAR(50)", "skip"), ("TEXT", "skip"), ("JSONB", "skip"),
        # 数组必须先判：INT[] 的前缀是 INT，漏了就会被当成数值列
        ("INT[]", "skip"), ("TEXT[]", "skip"), ("VARCHAR(50)[]", "skip"),
    ]:
        got = classify(pg)
        if got != want:
            bad += 1
            print(f"  FAIL classify({pg!r}) → {got!r}，期望 {want!r}")

    for src, want in [
        ("2026-01-07 14:11:10.53532", "2026-01-07T14:11:10"),
        ("2026-01-07T14:11:10", "2026-01-07T14:11:10"),
        ("2026-01-07 14:11:10", "2026-01-07T14:11:10"),
        # 截断不进位：.999999 不能变成 :11
        ("2026-01-07 14:11:10.999999", "2026-01-07T14:11:10"),
    ]:
        got = norm_ts(src)
        if got != want:
            bad += 1
            print(f"  FAIL norm_ts({src!r}) → {got!r}，期望 {want!r}")

    for src, want in [(decimal.Decimal("1.5"), "1.5000"),
                      (decimal.Decimal("0"), "0.0000"),
                      (decimal.Decimal("8571786.74"), "8571786.7400")]:
        got = fmt_sum(src)
        if got != want:
            bad += 1
            print(f"  FAIL fmt_sum({src}) → {got!r}，期望 {want!r}")

    # decimal 求和必须精确：同样的加法用 float 会掉尾数
    cents = [decimal.Decimal("0.01")] * 300
    if fmt_sum(sum(cents, decimal.Decimal(0))) != "3.0000":
        bad += 1
        print("  FAIL decimal 求和不精确")

    # 探针 SQL：只取 want 里的指标，且时间列的 Joda 字面量转义要对
    sql, labels = athena_probe(
        "t", [("id", "INT"), ("amt", "DECIMAL(12,2)"), ("created_at", "TIMESTAMP"),
              ("d", "DATE"), ("flag", "BOOLEAN"), ("name", "VARCHAR(50)")],
        {"sum:id", "sum:amt", "min:created_at", "min:d", "true:flag"})
    for must in ['SUM(CAST("amt" AS DECIMAL(38,4)))', "''T''", "'yyyy-MM-dd'",
                 'CASE WHEN "flag" THEN 1 ELSE 0 END']:
        if must not in sql:
            bad += 1
            print(f"  FAIL 探针 SQL 里缺 {must!r}\n       {sql}")
    if "name" in sql:
        bad += 1
        print(f"  FAIL varchar 列不该进探针：{sql}")
    if labels != ["count", "sum:id", "sum:amt", "min:created_at", "max:created_at",
                  "min:d", "max:d", "true:flag"]:
        bad += 1
        print(f"  FAIL 标签顺序与 SELECT 不对应：{labels}")

    # 文档声明的行数 ⟷ CSV 实测。放在 --selftest 里是因为它不连云、几百毫秒，
    # 于是每次 `bash scripts/test_all.sh`（L0）都跑得到——文档漂移要在第一层就红。
    doc_bad = check_doc_totals()
    for d in doc_bad:
        bad += 1
        print(f"  FAIL 文档行数声明：{d}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  类型分类 17 例、时间归一 4 例、求和格式 4 项、探针 SQL 6 项")
    print("  knowledge/connection.md 的行数声明 ⟷ data/csv/ 实测：7 项相等")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
