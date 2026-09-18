#!/usr/bin/env python3
"""装载完整性（DuckDB arm）：CSV 真源 ⟷ DuckDB 现查的同一批 Iceberg 表。

## 为什么不自己定义指标

指标口径**整份复用** `scripts/lakehouse/verify_load.py`：`classify()` 决定哪些列
参与对账、`csv_metrics()` 算 CSV 侧那一组 `count` / `sum:` / `min:` / `max:` /
`true:`。这里只加 DuckDB 侧的探针 SQL。

这不是省事，是判据的前提。三个 arm 要比的是「同一份逻辑数据在三个引擎上是否一致」，
两边各写一套指标定义的话，比出来的差异里会混进「两套定义本来就不同」这一类——
而这一类**看起来和真差异一模一样**。同一个理由见 lakehouse/verify_load.py 里
「两条聚合路径必须算出同一个 dict」那段。

## 方言映射（三处，每处都对齐了截断/进位方向）

| 指标 | Athena（Trino） | DuckDB |
|---|---|---|
| `sum:` | `SUM(CAST(c AS DECIMAL(38,4)))` | 同 |
| `min:`/`max:` | `format_datetime(..., 'yyyy-MM-dd''T''HH:mm:ss')` | `strftime(..., '%Y-%m-%dT%H:%M:%S')` |
| `true:` | `SUM(CASE WHEN c THEN 1 ELSE 0 END)` | 同 |

时间那一行是唯一有风险的：两边都必须**截断**到秒而不是四舍五入，否则
`14:11:10.999999` 在一边变成 `:10`、另一边变成 `:11`，而这种假失败只在某些行上出现，
排查起来很像数据问题。`strftime` 和 `format_datetime` 都是截断，实测对齐。

## 列序也要比

`--columns` 比的是**表内列序**，不是列名集合。列序错位在所有聚合指标上都看不出来
（每列各自求和，跟顺序无关），但 `SELECT *` 的结果会串列——而 agent 和 eval 金标里
都有 `SELECT *`。三个 arm 的列序必须一致，这一条只有逐位比才抓得到。

用法：

    python3 scripts/duckdb/verify_load.py                 # 35 张表全比
    python3 scripts/duckdb/verify_load.py -t users -t orders
    python3 scripts/duckdb/verify_load.py --columns       # 只比列序（快，不扫数据）
    python3 scripts/duckdb/verify_load.py --rows-only     # 只比行数（快）
    python3 scripts/duckdb/verify_load.py --selftest      # 方言映射自测（无云依赖）

`CSV_DIR` 与 lakehouse 那条共用同一个变量，指向**装载时读的那一份**产出。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
_LAKE_DIR = ROOT / "scripts" / "lakehouse"
# conn 在本目录，gen_ddl 在 lakehouse
sys.path.insert(0, str(_LAKE_DIR))
sys.path.insert(0, str(_HERE))


def _load_lake():
    """按**文件路径**加载 lakehouse 那个 verify_load。

    不能写 `import verify_load`：这个文件自己就叫 verify_load，而 sys.path 上
    本目录在前，`import verify_load` 会拿到自己。那样不报 ImportError，只在用到
    `lake.classify` 时报 AttributeError，报错点离原因很远。
    """
    spec = importlib.util.spec_from_file_location(
        "lakehouse_verify_load", _LAKE_DIR / "verify_load.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


lake = _load_lake()          # CSV 侧指标的唯一定义


# ---------------------------------------------------------------- DuckDB 侧

def duckdb_probe(table: str, cols: list[tuple[str, str]],
                 want: set[str]) -> tuple[str, list[str]]:
    """拼一条聚合 SQL，只取 `want` 里的指标（= CSV 侧真算出来的那些）。

    与 `lake.athena_probe` 一一对应：同样的列分类、同样的标签、同样的顺序。
    只取 CSV 侧有的指标，两边指标集合就天然对齐，剩下的差异全是真差异。
    """
    kinds = {c: lake.classify(t) for c, t in cols}
    sel, labels = ["COUNT(*)"], ["count"]
    for col, kind in kinds.items():
        if kind == "num" and f"sum:{col}" in want:
            # 先定点化再求和，和 Athena 侧同一个 DECIMAL(38,4)：
            # double 求和的结果依赖分片顺序，跨引擎比会出假差异
            sel.append(f'CAST(SUM(CAST("{col}" AS DECIMAL(38,4))) AS VARCHAR)')
            labels.append(f"sum:{col}")
        elif kind in ("ts", "date") and f"min:{col}" in want:
            # strftime 是截断，与 Trino 的 format_datetime 同向。见模块 docstring。
            fmt = "%Y-%m-%d" if kind == "date" else "%Y-%m-%dT%H:%M:%S"
            for agg, tag in (("MIN", "min"), ("MAX", "max")):
                sel.append(f"strftime(CAST({agg}(\"{col}\") AS TIMESTAMP), '{fmt}')")
                labels.append(f"{tag}:{col}")
        elif kind == "bool" and f"true:{col}" in want:
            sel.append(f'SUM(CASE WHEN "{col}" THEN 1 ELSE 0 END)')
            labels.append(f"true:{col}")
    return f'SELECT {", ".join(sel)} FROM "{table}"', labels


def duckdb_metrics(client, table: str, cols: list[tuple[str, str]],
                   want: set[str]) -> dict[str, object]:
    sql, labels = duckdb_probe(table, cols, want)
    row = client.execute(sql)["rows"][0]
    out: dict[str, object] = {}
    for label, val in zip(labels, row):
        if label == "count" or label.startswith("true:"):
            out[label] = int(val) if val is not None else 0
        else:
            out[label] = val
    return out


def _norm_decimal(v: object) -> str:
    """DuckDB 的 DECIMAL→VARCHAR 与 Trino 的差一处：整数部分为 0 时前导零。

    Trino 给 `-1.7500`、`0.0000`；DuckDB 也给同样的形状，但 Python 端
    `fetchall()` 拿到的可能已经是 `Decimal` 对象（当 CAST AS VARCHAR 被优化掉时）。
    统一走 `str()` 之后按 CSV 侧的 4 位小数格式重排，这样比的是数值不是字面。
    """
    import decimal
    s = str(v)
    try:
        return lake.fmt_sum(decimal.Decimal(s))
    except (decimal.InvalidOperation, ValueError):
        return s


# ---------------------------------------------------------------- 列序

def compare_columns(client, table: str,
                    declared: list[tuple[str, str]]) -> list[str]:
    """DDL 声明的列序 ⟷ DuckDB 里的列序，逐位比。返回问题清单。"""
    want = [c for c, _ in declared]
    got = [c for c, _ in client.columns(table)]
    if got == want:
        return []
    if sorted(got) == sorted(want):
        # 集合相同只是顺序不同：所有聚合指标都查不出这一种，只有 SELECT * 会串列
        first = next(i for i, (a, b) in enumerate(zip(got, want)) if a != b)
        return [f"{table} 列序不同（列名集合相同）：第 {first + 1} 列 "
                f"DuckDB={got[first]!r} DDL={want[first]!r}"]
    missing = [c for c in want if c not in got]
    extra = [c for c in got if c not in want]
    out = []
    if missing:
        out.append(f"{table} 少了这些列：{missing}")
    if extra:
        out.append(f"{table} 多了这些列：{extra}")
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="CSV 真源 ⟷ DuckDB 装载完整性对账")
    ap.add_argument("-t", "--table", action="append", default=[],
                    help="只查这些表（可重复）；默认全部")
    ap.add_argument("--columns", action="store_true", help="只比列序，不扫数据")
    ap.add_argument("--rows-only", action="store_true", help="只比行数，不比聚合指标")
    ap.add_argument("--selftest", action="store_true", help="方言映射自测（无云依赖）")
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

    import conn
    client = conn.Client()

    # 列序只比 DDL ⟷ 引擎，不需要 CSV，所以放在解析 CSV_DIR 之前
    if a.columns:
        bad: list[str] = []
        for t in tables:
            bad += compare_columns(client, t, declared[t])
        print(f"列序对账：{len(tables)} 张表")
        if bad:
            print(f"\n发现 {len(bad)} 处问题 ❌")
            for d in bad:
                print(f"  - {d}")
            return 1
        print("  DDL 声明的列序与 DuckDB 里的列序逐位相同 ✅")
        return 0

    # --rows-only 拿装载快照做基准，不重扫 CSV。快照记着自己是哪一次装载
    # （source / scale / seed / 轴止），所以「跟谁比」是写在输出里的，不用猜。
    # 全量 CSV 是 7.3G 且在临时目录里，行数这一项没必要为它等几分钟。
    snap: dict[str, int] = {}
    if a.rows_only:
        import json
        s = json.loads(lake.LOADED_SNAPSHOT.read_text(encoding="utf-8"))
        snap = {k: int(v) for k, v in s["tables"].items()}
        print(f"基准：{lake.LOADED_SNAPSHOT.relative_to(ROOT)}"
              f"（{s['source']}，scale {s['scale']} / seed {s['seed']} / 轴止 {s['as_of']}）")
    else:
        if (rc := lake._resolve_csv_dir()) is not None:
            return rc
        print(f"CSV 侧聚合引擎：{lake._engine()[1]}    目录：{lake.CSV_DIR}")
    print(f"DuckDB 侧：{conn.ALIAS}.{conn.NAMESPACE}"
          f"（内存额度 {conn.MEMORY_LIMIT}，线程 {conn.THREADS or '按核数'}）\n")

    src = "快照" if a.rows_only else "CSV"
    diffs: list[str] = []
    checked = 0
    total_rows = 0
    for t in tables:
        cols = declared[t]

        if a.rows_only:
            if t not in snap:
                print(f"  ·  {t:<28} 跳过（快照里没有这张表）")
                continue
            exp = {"count": snap[t]}
            got = {"count": client.execute(f'SELECT COUNT(*) FROM "{t}"')["rows"][0][0]}
        else:
            if not (lake.CSV_DIR / f"{t}.csv").exists():
                print(f"  ·  {t:<28} 跳过（没有 CSV）")
                continue
            exp = lake.csv_metrics(t, cols)
            got = duckdb_metrics(client, t, cols, set(exp))

        bad = []
        for k in sorted(exp):
            e, g = exp[k], got.get(k, "<无>")
            if k.startswith("sum:"):
                e, g = _norm_decimal(e), _norm_decimal(g)
            if str(e) != str(g):
                bad.append(f"{t}.{k}：{src}={e}  DuckDB={g}")
        checked += 1
        total_rows += int(exp["count"])
        if bad:
            diffs += bad
            print(f"  ❌ {t:<28} {len(exp)} 项指标，{len(bad)} 项不符")
        else:
            print(f"  ✅ {t:<28} {len(exp)} 项指标全等  count={exp['count']:,}")

    print()
    if diffs:
        print(f"发现 {len(diffs)} 处差异 ❌（{src}是基准，DuckDB 是读 Iceberg 的结果）\n")
        for d in diffs[:40]:
            print(f"  - {d}")
        if len(diffs) > 40:
            print(f"  …… 另有 {len(diffs) - 40} 处")
        return 1
    what = "行数" if a.rows_only else "行数、数值求和、时间边界、布尔计数"
    print(f"装载完整 ✅  {checked} 张表的{what}全等，合计 {total_rows:,} 行")

    if not a.table:
        rc_extra = report_extras(client, set(declared))
        if rc_extra:
            return rc_extra
    return 0


def derived_tables() -> set[str]:
    """派生层的表名，从 `database/iceberg/02_mart.sql` 现解析。

    不写死名单：派生层加一张表时，写死的名单会把它算成「来路不明」而误报红灯，
    而那种红灯只会教人去把名单加长，不会让人真去看一眼。
    """
    import re
    p = ROOT / "database" / "iceberg" / "02_mart.sql"
    if not p.is_file():
        return set()
    return set(re.findall(r"(?im)^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                          p.read_text(encoding="utf-8")))


def report_extras(client, declared: set[str]) -> int:
    """命名空间里除 35 张原始表之外还有什么，明说，不静默略过。

    对账只覆盖 35 张有 CSV 真源的原始表，派生层是 CTAS 出来的、没有可比的真源。
    但「只比了 35 张」和「一共就 35 张」是两件事，输出里不写清楚的话，
    下次有人往命名空间里多灌一张表，这条对账照样全绿。
    """
    present = set(client.tables())
    extras = present - declared
    if not extras:
        return 0
    derived = derived_tables()
    known = sorted(extras & derived)
    unknown = sorted(extras - derived)
    print(f"\n命名空间里另有 {len(extras)} 张表没参与对账"
          f"（它们是 CTAS 产物，没有 CSV 真源）：")
    if known:
        print(f"  派生层，database/iceberg/02_mart.sql 里声明的 {len(known)} 张："
              f"{'、'.join(known)}")
    if unknown:
        print(f"  ⚠️  来路不明的 {len(unknown)} 张：{'、'.join(unknown)}"
              f"\n      既不在 DDL 真源里，也不在派生层声明里。要么补声明，要么删掉——"
              f"\n      留着的话，三个 arm 各自读到什么就没有依据说得清了。")
        return 1
    print(f"  合计 {len(declared)} + {len(known)} = {len(present)} 张，都有出处 ✅")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """方言映射的自测：探针 SQL 长对，且与 Athena 侧标签逐位相同。"""
    bad = 0
    cols = [("id", "INT"), ("amt", "DECIMAL(12,2)"), ("created_at", "TIMESTAMP"),
            ("d", "DATE"), ("flag", "BOOLEAN"), ("name", "VARCHAR(50)")]
    want = {"sum:id", "sum:amt", "min:created_at", "min:d", "true:flag"}

    sql, labels = duckdb_probe("t", cols, want)
    a_sql, a_labels = lake.athena_probe("t", cols, want)

    # 1. 标签必须逐位相同 —— 这是「两个 arm 比同一组指标」的全部保证
    if labels != a_labels:
        bad += 1
        print(f"  FAIL 标签与 Athena 侧不同：\n       DuckDB={labels}\n       Athena={a_labels}")

    # 2. DuckDB 方言的三处关键写法
    for must in ['SUM(CAST("amt" AS DECIMAL(38,4)))',
                 "strftime(CAST(MIN(\"created_at\") AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S')",
                 "strftime(CAST(MIN(\"d\") AS TIMESTAMP), '%Y-%m-%d')",
                 'CASE WHEN "flag" THEN 1 ELSE 0 END']:
        if must not in sql:
            bad += 1
            print(f"  FAIL 探针 SQL 里缺 {must!r}\n       {sql}")

    # 3. Trino 专有写法不能漏进来。format_datetime 在 DuckDB 里不存在，
    #    真漏了会在云上报 Binder Error，而那时已经花了一次全表扫描的时间。
    for forbidden in ("format_datetime", "''T''"):
        if forbidden in sql:
            bad += 1
            print(f"  FAIL DuckDB 探针里出现 Trino 专有写法 {forbidden!r}：{sql}")

    # 4. varchar 列不该进探针（两边同样的取舍）
    if "name" in sql:
        bad += 1
        print(f"  FAIL varchar 列不该进探针：{sql}")

    # 5. 真跑一遍：拿构造数据验证探针算出来的就是 CSV 侧那一组数。
    #    这一条把「方言映射对不对」从「SQL 字面看着像」升级成「结果相等」。
    try:
        import duckdb
    except ImportError:
        print("  跳过引擎侧自测（没装 duckdb）")
    else:
        import csv
        import os
        import tempfile
        n_fixture = 0
        keep = lake.CSV_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                lake.CSV_DIR = Path(tmp)
                p = lake.CSV_DIR / "fx.csv"
                with p.open("w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    w.writerow([c for c, _ in lake._FIXTURE_COLS])
                    w.writerows(lake._FIXTURE_ROWS)
                exp = lake.csv_metrics("fx", lake._FIXTURE_COLS)

                c = duckdb.connect()
                # 按真源 DDL 的类型建表，空字段当 NULL —— 与云上装载同一个口径
                c.execute(f"""
                    CREATE TABLE fx AS
                    SELECT * FROM read_csv('{p}', header=true, nullstr='',
                        columns={{
                            'id': 'INTEGER', 'amt': 'DECIMAL(12,2)', 'zilch': 'BIGINT',
                            'created_at': 'TIMESTAMP', 'd': 'DATE',
                            'flag': 'BOOLEAN', 'nope': 'BOOLEAN',
                            'note': 'VARCHAR', 'tags': 'VARCHAR'
                        }})
                """)

                class _C:
                    def execute(self, sql):
                        cur = c.execute(sql)
                        return {"rows": cur.fetchall()}

                got = duckdb_metrics(_C(), "fx", lake._FIXTURE_COLS, set(exp))
                n_fixture = len(exp)
                for k in sorted(exp):
                    e, g = exp[k], got.get(k, "<无>")
                    if k.startswith("sum:"):
                        e, g = _norm_decimal(e), _norm_decimal(g)
                    if str(e) != str(g):
                        bad += 1
                        print(f"  FAIL 构造数据 {k}：CSV={e!r}  DuckDB={g!r}")
                c.close()
        finally:
            lake.CSV_DIR = keep
            os.environ.pop("CSV_ENGINE", None)

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  探针标签与 Athena 侧逐位相同（{len(labels)} 项）")
    print("  DuckDB 方言 4 处写法、Trino 专有写法 2 处不得出现、varchar 不进探针")
    if n_fixture:
        print(f"  构造数据上 CSV 侧与 DuckDB 侧算出同一组 {n_fixture} 项指标")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
