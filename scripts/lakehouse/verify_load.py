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

两个环境变量：`CSV_DIR` 指向装载时读的那一份 CSV（全量 6.8GB 不落在仓库里）；
`CSV_ENGINE=stdlib` 把 CSV 侧聚合退回标准库那条——口径相同，1872 万行慢 12 倍。
"""
from __future__ import annotations

import argparse
import csv
import decimal
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

# 环境变量 CSV_DIR 覆盖，和 load.py 认同一个变量（理由写在那边）：全量 6.8GB
# 不落在仓库里，而这个脚本必须读**装载时读的那一份**，否则对的就不是同一批数据。
CSV_DIR = Path(os.environ.get("CSV_DIR") or ROOT / "data" / "csv")
SCALE = decimal.Decimal("0.0001")          # 与 Athena 的 DECIMAL(38,4) 对齐


def _rel(p: Path) -> Path:
    """能写成仓库相对路径就写，不能就原样。全量产出在仓库外（`~/analytics-agent-data/`），
    `relative_to` 对它会抛——而这些路径只出现在**报错信息**里，不该由排版把它挤成崩溃。
    """
    return p.relative_to(ROOT) if p.is_relative_to(ROOT) else p


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
#
# 8000 万行下这一层是整条对账链路里最慢的一段：逐行 `DictReader` 之后，470 个非 skip 列
# 各做一次 `Decimal` 加法 / 字符串比较 / 布尔取词。实测 1872 万行 55s，全量外推 4 分钟。
# 有 pyarrow 时改成按批读、逐列一次 compute 调用。
#
# **两条路径必须算出同一个 dict**，所以标准库那条留着（`CSV_ENGINE=stdlib` 强制走它），
# 而不是删掉：它是这个 dict 的定义，向量化那条只是它的快路。`--selftest` 里两条路径跑
# 同一份构造数据逐键比；2026-08-31 另外拿 data/csv（35 表 18.9 万行）和一份 1872 万行的
# 全量样本比过，都是逐键相同，耗时 0.6s→0.1s、55.8s→4.6s。
#
# 已知的**唯一**分歧：小数位超过 4 位的值。向量化那条 `cast` 直接报错，标准库那条会
# 按 ROUND_HALF_EVEN 静默进位，而 Athena 的 `CAST(... AS DECIMAL(38,4))` 按 half-up
# 进位（实测 `'1.23455'` → 1.2346）。三边各一套，所以这种列不能存在——本仓库的数值列
# 最大标度是 2，真冒出一列时该做的是先定口径，不是挑一条路径。报错那条因此是对的行为。


def _true_count(kinds: dict[str, str]) -> dict[str, int]:
    """布尔列的初值，**每个布尔列都给一个 0**（而不是等第一个 true 才建键）。

    因为 Athena 侧是 `SUM(CASE WHEN c THEN 1 ELSE 0 END)`：NULL 走 ELSE 记 0，所以
    整列全 false 或全 NULL 时它返回的是 0，不是 NULL。这里跟着给 0，那些列才进得了
    对账；原来的写法在「CSV 全 false」时不产生键，于是这一列**完全不比**——
    Athena 侧真的全 true 也照样绿。数值列和时间列不能这么办，它们的 SQL 聚合在没有
    非空值时返回 NULL。
    """
    return {c: 0 for c, k in kinds.items() if k == "bool"}


def _agg_stdlib(table: str, path: Path, kinds: dict[str, str]):
    """逐行聚合 → (行数, sums, lo, hi, trues)。这是指标口径的定义式。"""
    n = 0
    sums: dict[str, decimal.Decimal] = {}
    lo: dict[str, str] = {}
    hi: dict[str, str] = {}
    trues = _true_count(kinds)

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
                        trues[col] += 1
                    elif tok not in FALSE_TOKENS:
                        raise ValueError(f"{table}.{col} 不是布尔值：{raw!r}")
    return n, sums, lo, hi, trues


def _agg_arrow(table: str, path: Path, kinds: dict[str, str]):
    """向量化聚合 → 与 `_agg_stdlib` 逐键相同的五元组。

    三处口径是刻意对齐的，不是"差不多"：

    1. **全列按 string 读，只把空字段当 NULL**（`null_values=[""]`）。pyarrow 默认还把
       `NA` / `null` / `N/A` 这些词当 NULL，那会让一个内容恰好是 `NULL` 的文本列凭空
       多出空值——虽然这些列都是 skip，但口径不能靠"恰好没影响"成立。
    2. **求和先 `cast(decimal128(38,4))` 再 `sum`**，和 Athena 侧的
       `SUM(CAST(... AS DECIMAL(38,4)))` 是同一件事，也和标准库那条的
       `Decimal` 精确加法同值（本仓库的数值列最大标度是 2，转换无损）。
    3. **时间列在原始字符串上取 min/max，再截断到秒**。顺序不变：这批时间戳到秒之前
       是定宽的，所以 `min(截断(x)) == 截断(min(x))`——先聚合后归一，比逐行归一少
       几千万次正则。
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    from pyarrow import csv as pacsv

    n = 0
    sums: dict[str, decimal.Decimal] = {}
    lo: dict[str, str] = {}
    hi: dict[str, str] = {}
    trues = _true_count(kinds)
    dec = pa.decimal128(38, 4)
    want = [c for c, k in kinds.items() if k != "skip"]
    # 取值集合在这里建，不放模块级：pyarrow 是可选依赖，模块级会让没装它的环境
    # 连 import 都过不去。
    true_set = pa.array(sorted(TRUE_TOKENS))
    bool_set = pa.array(sorted(TRUE_TOKENS | FALSE_TOKENS))

    with pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(block_size=1 << 26),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(
            column_types={c: pa.string() for c in kinds},
            null_values=[""], strings_can_be_null=True),
    ) as rd:
        idx = {c: rd.schema.names.index(c) for c in want}
        for batch in rd:
            n += batch.num_rows
            for col in want:
                arr = batch.column(idx[col])
                kind = kinds[col]

                if kind == "num":
                    try:
                        d = arr.cast(dec)
                    except pa.ArrowInvalid as e:
                        # Athena 侧是同一个 DECIMAL(38,4)，所以这不是引擎差异，
                        # 是这一列的口径本身要重定。换 stdlib 只会把问题推到云上。
                        raise ValueError(
                            f"{table}.{col} 有 DECIMAL(38,4) 装不下的值：{e}") from None
                    if len(d) > d.null_count:
                        sums[col] = sums.get(col, decimal.Decimal(0)) + pc.sum(d).as_py()

                elif kind in ("ts", "date"):
                    if len(arr) == arr.null_count:
                        continue
                    mm = pc.min_max(arr).as_py()
                    norm = norm_ts if kind == "ts" else norm_date
                    a, b = norm(mm["min"]), norm(mm["max"])
                    if col not in lo or a < lo[col]:
                        lo[col] = a
                    if col not in hi or b > hi[col]:
                        hi[col] = b

                elif kind == "bool":
                    # 先 drop_null：`is_in` 对 NULL 输入返回 NULL 还是 false 取决于
                    # 选项，取值集合里没有 NULL 时不该让它参与判断。
                    tok = pc.utf8_lower(pc.utf8_trim_whitespace(pc.drop_null(arr)))
                    trues[col] += pc.sum(pc.cast(
                        pc.is_in(tok, true_set), pa.int64())).as_py() or 0
                    odd = pc.filter(tok, pc.invert(pc.is_in(tok, bool_set)))
                    if len(odd):
                        raise ValueError(f"{table}.{col} 不是布尔值：{odd[0].as_py()!r}")
    return n, sums, lo, hi, trues


def _engine():
    """→ (聚合函数, 引擎名)。`CSV_ENGINE=stdlib` 强制走标准库那条。

    也认 `load.py` 的 `PREFLIGHT_ENGINE`，这样"整条装载链路都退回标准库"是一个
    环境变量的事，不用记两个名字。
    """
    forced = os.environ.get("CSV_ENGINE") or os.environ.get("PREFLIGHT_ENGINE")
    if forced == "stdlib":
        return _agg_stdlib, "stdlib"
    try:
        import pyarrow.csv  # noqa: F401
    except ImportError:
        return _agg_stdlib, "stdlib（没装 pyarrow）"
    return _agg_arrow, "pyarrow"


def csv_metrics(table: str, cols: list[tuple[str, str]]) -> dict[str, object]:
    """扫一遍 CSV，算出与 Athena 侧同名的指标。

    `cols` 是 [(列名, Postgres 类型)]，顺序取自 DDL。CSV 表头必须与之一致——
    这一点由 `load.py --preflight` 单独守着，这里只按表头取值。但要取的列**一个都不能
    少**：缺一列的后果是那一列的指标整个消失，两边指标集合又是按 CSV 侧对齐的，
    于是"少比了一列"和"比过了且相等"在输出里长得一模一样。所以这里显式拦一道。
    """
    path = CSV_DIR / f"{table}.csv"
    kinds = {c: classify(t) for c, t in cols}

    with path.open(encoding="utf-8", newline="") as f:
        head = next(csv.reader(f), [])
    missing = [c for c, k in kinds.items() if k != "skip" and c not in head]
    if missing:
        raise ValueError(f"{path.name} 的表头缺这些要对账的列：{missing}"
                         f"（先跑 load.py --preflight）")

    agg, _ = _engine()
    n, sums, lo, hi, trues = agg(table, path, kinds)

    out: dict[str, object] = {"count": n}
    # 数值列和时间列只报 CSV 里真出现过非空值的那些：SQL 的 SUM/MIN/MAX 在全 NULL 的列上
    # 返回 NULL、Python 这边返回 0 或无值，比它只会造出一处假差异。布尔列不同，见
    # `_true_count`。
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
    """`CSV_DIR/*.csv` 每张表的数据行数（不含表头）。默认 `data/csv`，`CSV_DIR` 可改。"""
    out: dict[str, int] = {}
    for p in sorted(CSV_DIR.glob("*.csv")):
        with p.open(encoding="utf-8", newline="") as f:
            out[p.stem] = max(sum(1 for _ in csv.reader(f)) - 1, 0)
    return out


# 云上那份数据装载时的逐表行数快照。2026-09-01 加的，起因是重灌把这条检查悬空了：
# `connection.md` 描述的是**云上**那 7,994 万行，而它的 CSV 真源是全量产出
# （7.3G、在 /tmp 里、随时会被清掉），默认的 `data/csv` 是 v1 的 19 万行——它现在
# 谁都不描述。于是不带 `CSV_DIR=` 跑 L0 时这条检查必红，而"必红的灯"和"没有灯"
# 一样没用。快照让 doc ⟷ 真源的比对在没有那 7.3G 时也成立，代价是多一份要维护的
# 中间产物，所以它记着自己是哪一次装载、scale / seed / 轴是什么，对不上就能看出来。
LOADED_SNAPSHOT = ROOT / "data" / "loaded_row_counts.json"


def loaded_row_counts() -> tuple[dict[str, int], str, list[str]]:
    """返回 (逐表行数, 这些数的出处, 问题清单)。

    优先用 `CSV_DIR` 现数——它是真源，快照只是抄的那一份。只有在 `CSV_DIR` 就是
    快照记录的那个目录时才拿两者对账（不等 = 快照过期或产出被动过，判红）；
    指向别的目录时不判红、只在出处里说清"这一份没参与比对"，否则本地拿 scale=1
    的产出跑一次 L0 就会红一片，而那批数字压根不该和云上的文档相等。
    """
    if not LOADED_SNAPSHOT.is_file():
        return {}, str(LOADED_SNAPSHOT), [
            f"找不到装载快照 {_rel(LOADED_SNAPSHOT)}——"
            "它是 connection.md 那几个数的比对基准，缺了这条检查就悬空了"]
    snap = json.loads(LOADED_SNAPSHOT.read_text(encoding="utf-8"))
    tables = {k: int(v) for k, v in snap["tables"].items()}
    tag = (f"{_rel(LOADED_SNAPSHOT)}（{snap['source']} 那次装载，"
           f"scale {snap['scale']} / seed {snap['seed']} / 轴止 {snap['as_of']}）")

    csv_counts = csv_row_counts()
    if str(CSV_DIR) != snap["source"]:
        where = CSV_DIR.relative_to(ROOT) if CSV_DIR.is_relative_to(ROOT) else CSV_DIR
        return tables, f"{tag}；CSV_DIR={where} 是另一份数据，没参与这条比对", []

    bad: list[str] = []
    for t in sorted(set(tables) | set(csv_counts)):
        a, b = tables.get(t), csv_counts.get(t)
        if a != b:
            bad.append(f"装载快照与产出不一致：{t} 快照 {a}、{CSV_DIR}/{t}.csv {b}"
                       "——重新灌过就重写快照，没灌过就是产出被动了")
    return csv_counts, f"{CSV_DIR}/ 现数（与装载快照逐表相符）", bad


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

    counts, where, bad0 = loaded_row_counts()
    csv_rows, csv_files = sum(counts.values()), len(counts)
    doc_all_rows, doc_all_tables = got["all_rows"]
    doc_base_tables, doc_base_rows = got["base"]
    doc_der_tables, doc_der_rows = got["derived"]
    doc_gov_all, doc_gov_base = got["governed"]
    (doc_ungranted,) = got["ungranted"]

    bad: list[str] = list(bad0)

    def eq(label: str, doc: int, real: int, how: str) -> None:
        if doc != real:
            bad.append(f"{label}：文档写 {doc:,}，{how} = {real:,}")

    # 一、文档 ⟷ 装载真源。文档是抄的那一份。
    # 出处由 loaded_row_counts() 现给，不写死 `data/csv/`：这几行是**不相等时**唯一
    # 说明"跟谁比"的地方，写死目录名会把人引回那份没在比的 data/csv 里去找差异。
    eq("原始表行数", doc_base_rows, csv_rows, f"{where} 合计")
    eq("原始表张数", doc_base_tables, csv_files, f"{where} 的表数")
    eq(f"{UNGRANTED_TABLE} 行数", doc_ungranted, counts.get(UNGRANTED_TABLE, -1),
       f"{where} 里的 {UNGRANTED_TABLE}")

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

def _resolve_csv_dir() -> int | None:
    """把 `CSV_DIR` 指到**真正装进云里那份产出**上。返回 None = 继续，返回码 = 就此退出。

    2026-09-01 起必须有这一步：云上是 7,994 万行的全量产出，而 `CSV_DIR` 的默认值
    `data/csv` 是 v1 的 19 万行——拿它跟 Athena 比会得出一屏"差异"，而那屏差异
    **不是装载出了问题**，是在比两份不同的数据。这种红灯比没有灯更坏：它把
    「你比错了东西」印成了「装载不完整」。

    规则：没有显式给 `CSV_DIR=` 时，用装载快照记录的那个目录（`data/loaded_row_counts.json`
    的 `source`）。那个目录不在本机就**判红并说清怎么办**——"没法对账"不等于"对账通过"，
    这一条不允许静默放过。**快照本身缺失也一样判红**：原来这里 `return None` 退回
    `CSV_DIR` 的默认值 `data/csv`，于是"不知道云上装的是哪一份"被降级成了"就当它装的是
    种子那一份"。这条路径有两种落点，都坏：湖里是全量时印出一屏假差异（同上一段），
    湖里恰好就是种子那一份时印出**装载完整 ✅**——而它并没有验证过这件事，它只是没有
    别的目录可比。快照是手工维护的中间产物（见 `LOADED_SNAPSHOT` 上方），"忘了写"是
    它最常见的状态，所以这里必须是硬失败而不是默认值。
    """
    global CSV_DIR
    if os.environ.get("CSV_DIR"):
        return None                       # 显式指定的优先，包括故意指向别的产出
    if not LOADED_SNAPSHOT.is_file():
        print(f"没法做 CSV ⟷ Athena 对账 ❌\n"
              f"  找不到装载快照 {_rel(LOADED_SNAPSHOT)}，"
              f"所以不知道云上这份数据是从哪个目录装进去的。\n"
              f"  两条出路：① 照那个文件的形状补一份（`source` 指向装载用的 CSV 目录，"
              f"外加 `scale` / `seed` / `as_of` / `tables` 逐表行数）——它是手工维护的，"
              f"每次 `load.py` 之后都该更新；② `CSV_DIR=<装载用的产出目录>` 显式指定。\n"
              f"  **不会**默认拿 {_rel(CSV_DIR)} 去比：那只是 `CSV_DIR` 的默认值，"
              f"不是任何一次装载的证据。")
        return 1
    snap = json.loads(LOADED_SNAPSHOT.read_text(encoding="utf-8"))
    src = Path(snap["source"])
    if src == CSV_DIR:
        return None
    if src.is_dir():
        CSV_DIR = src
        print(f"CSV 真源取自装载快照记录的目录：{src}"
              f"（scale {snap['scale']} / seed {snap['seed']} / 轴止 {snap['as_of']}）\n"
              f"  data/csv 是 v1 的 19 万行样本，跟云上这份无关，所以不拿它比。")
        return None
    print(f"没法做 CSV ⟷ Athena 对账 ❌\n"
          f"  云上这份数据的 CSV 真源是 {src}（见 {_rel(LOADED_SNAPSHOT)}），"
          f"它已经不在本机了。\n"
          f"  两条出路：① 重跑生成器把它造回来——"
          f"`python3 scripts/gen/main.py --scale {snap['scale']} --seed {snap['seed']} "
          f"--format csv --out {src}`（同 scale + 同 seed = 同一份数据；轴由 "
          f"budget.AS_OF 定，快照记的是 {snap['as_of']}，对不上说明那个常量被人改过）；"
          f"② `CSV_DIR=<别的产出目录>` 显式指定。\n"
          f"  **不会**退回 data/csv 去比：那是 v1 的 19 万行，比出来的差异全是假的。")
    return 1


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

    if (rc := _resolve_csv_dir()) is not None:
        return rc

    import athena
    client = athena.Client()
    print(f"CSV 侧聚合引擎：{_engine()[1]}    目录：{CSV_DIR}")

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

# 构造一张把口径都踩到的表。每一列都是为了钉一件具体的事，见 `_selftest_engines`。
_FIXTURE_COLS = [
    ("id", "INT"), ("amt", "DECIMAL(12,2)"), ("zilch", "BIGINT"),
    ("created_at", "TIMESTAMP"), ("d", "DATE"),
    ("flag", "BOOLEAN"), ("nope", "BOOLEAN"),
    ("note", "TEXT"), ("tags", "TEXT[]"),
]
_FIXTURE_ROWS = [
    # id  amt      zilch created_at                    d            flag nope note   tags
    ("1", "1.50",  "", "2026-01-07 14:11:10.53532", "2026-01-07", "t", "f", "NULL", "{a,b}"),
    ("2", "",      "", "2026-01-07 14:11:10.9",     "",           "f", "f", "NA",   "{}"),
    ("3", "-3.25", "", "2025-11-02 00:00:01",       "2025-11-02", "",  "f", "",     ""),
    ("4", "0.00",  "", "",                          "2026-03-01", "T", "f", "x",    "{c}"),
]


def _selftest_engines() -> int:
    """两条聚合路径跑同一份构造数据，逐键比。

    钉住的六件事：空字段是 NULL 而不是 0（`amt` 少一行、`d` 少一行）；整列 NULL 的数值
    列不产生 `sum:` 键（`zilch`）；整列 false 的布尔列**要**产生 `true:` 键且为 0
    （`nope`）；布尔取词大小写无关（`T`）；时间边界是截断到秒后的值，不是原始字符串的
    max（`.9` 那行不能把 max 带成 `14:11:10.9`）；skip 列一个都不进指标（`note` 里的
    `NULL` / `NA` 是给 pyarrow 的默认空值表设的饵，`tags` 是数组前缀陷阱）。
    """
    import tempfile

    global CSV_DIR
    keep = CSV_DIR
    bad = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            CSV_DIR = Path(tmp)
            with (CSV_DIR / "fx.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow([c for c, _ in _FIXTURE_COLS])
                w.writerows(_FIXTURE_ROWS)

            got = {}
            for eng in ("pyarrow", "stdlib"):
                os.environ["CSV_ENGINE"] = "" if eng == "pyarrow" else "stdlib"
                _fn, name = _engine()
                if name != eng:                # 没装 pyarrow 时只能测一条，说清楚
                    print(f"  跳过 {eng} 路径（当前引擎是 {name}）")
                    continue
                got[eng] = csv_metrics("fx", _FIXTURE_COLS)
            os.environ.pop("CSV_ENGINE", None)

            want = {
                "count": 4,
                "sum:id": "10.0000",           # 1+2+3+4
                "sum:amt": "-1.7500",          # 1.50 − 3.25 + 0.00，空字段不算 0
                "min:created_at": "2025-11-02T00:00:01",
                "max:created_at": "2026-01-07T14:11:10",   # 不是 ...:10.9
                "min:d": "2025-11-02", "max:d": "2026-03-01",
                "true:flag": 2,                # t 和 T，空字段不算
                "true:nope": 0,                # 整列 false 也要有这个键
            }
            for eng, m in got.items():
                if m != want:
                    bad += 1
                    keys = set(m) | set(want)
                    print(f"  FAIL {eng} 路径的指标不对：")
                    for k in sorted(keys):
                        if m.get(k, "<无>") != want.get(k, "<无>"):
                            print(f"       {k}: 得到 {m.get(k, '<无>')!r}，"
                                  f"期望 {want.get(k, '<无>')!r}")
            if len(got) == 2 and got["pyarrow"] != got["stdlib"]:
                bad += 1
                print("  FAIL 两条聚合路径结果不同")
    finally:
        CSV_DIR = keep
        os.environ.pop("CSV_ENGINE", None)
    return bad


def _selftest_resolve_csv_dir() -> int:
    """`_resolve_csv_dir()` 的四条分支，全部用临时目录，不碰真快照。

    钉住的是**"没法对账"必须判红**这一条：缺快照、或快照记的目录不在本机，都得返回
    非零退出码，且不许把 `CSV_DIR` 留在默认值上继续跑下去——那会让 L3 拿 `data/csv`
    去比一个不是从它装出来的湖。显式给了 `CSV_DIR=` 是唯一的例外（人已经说明白比谁）。
    """
    import contextlib
    import io as _io
    import tempfile

    global CSV_DIR, LOADED_SNAPSHOT
    keep_dir, keep_snap, keep_env = CSV_DIR, LOADED_SNAPSHOT, os.environ.get("CSV_DIR")
    bad = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "genFULL_csv"
            src.mkdir()
            snap = tmp / "loaded.json"

            def run(case: str, want_rc, want_dir: Path) -> None:
                nonlocal bad
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = _resolve_csv_dir()
                if rc != want_rc or CSV_DIR != want_dir:
                    bad += 1
                    print(f"  FAIL _resolve_csv_dir {case}：返回 {rc!r}（期望 {want_rc!r}）、"
                          f"CSV_DIR={CSV_DIR}（期望 {want_dir}）")

            def snapshot(source: Path) -> None:
                snap.write_text(json.dumps(
                    {"source": str(source), "scale": 427.07, "seed": 42,
                     "as_of": "2026-01-24", "tables": {}}), encoding="utf-8")

            os.environ.pop("CSV_DIR", None)
            LOADED_SNAPSHOT = snap

            # ① 快照缺失 → 判红，且 CSV_DIR 不动（不许退回默认值去比）
            CSV_DIR = keep_dir
            run("缺快照", 1, keep_dir)

            # ② 快照记的目录不在本机 → 判红
            snapshot(tmp / "gone")
            CSV_DIR = keep_dir
            run("源目录已不在", 1, keep_dir)

            # ③ 快照记的目录在 → 改指到它，继续
            snapshot(src)
            CSV_DIR = keep_dir
            run("源目录在", None, src)

            # ④ 显式 CSV_DIR= 优先于快照，连快照都不必存在
            LOADED_SNAPSHOT = tmp / "nope.json"
            os.environ["CSV_DIR"] = str(src)
            CSV_DIR = keep_dir
            run("显式指定", None, keep_dir)
    finally:
        CSV_DIR, LOADED_SNAPSHOT = keep_dir, keep_snap
        os.environ.pop("CSV_DIR", None)
        if keep_env is not None:
            os.environ["CSV_DIR"] = keep_env
    return bad


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

    bad += _selftest_engines()
    bad += _selftest_resolve_csv_dir()

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
    print(f"  CSV 聚合两条路径（{_engine()[1]} / stdlib）对同一份构造数据算出同一组 9 项指标")
    print("  CSV 真源解析 4 条分支：缺快照判红、源目录不在判红、源目录在改指、显式 CSV_DIR 优先")
    # 出处照 loaded_row_counts() 现说，不写死目录名：默认那条路比的是装载快照而不是
    # data/csv（那份是 v1 的 19 万行），印错出处会让人以为这条检查覆盖了 data/csv。
    print(f"  knowledge/connection.md 的行数声明 ⟷ {loaded_row_counts()[1]}：7 项相等")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
