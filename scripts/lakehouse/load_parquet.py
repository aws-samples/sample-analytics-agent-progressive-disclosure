#!/usr/bin/env python3
"""Parquet → Iceberg 补灌：把 `s3://<raw>/parquet/<表>/` 直灌进 S3 Tables 的表。

## 为什么会需要这么一个脚本

2026-09-02 那次全量重灌产出了新的 `parquet/`，Redshift 从它 `COPY`，所以 Redshift
侧是新数据；但 S3 Tables 那份 Iceberg **没跟着重灌**，`csv/` 前缀还是 09-01 的。
于是 Athena 和 DuckDB（这两条读同一批 Iceberg 文件）落后一次重灌。

差异的范围是**实测出来的，不是估的**：

- `correctness.py --arms-only` 把 35 张表的行数、数值求和、时间边界、布尔计数在三条
  arm 上逐项比过，全等。所以差的不是行、不是金额、不是时间。
- `query_correctness.py --values` 扫每张表每个文本列的 MIN/MAX，差异只落在 5 列 3 张表：
  `orders.cancel_reason` / `orders.refund_reason` / `orders.shipping_address`、
  `push_notifications.failure_reason`、`user_attributions.tracking_params`。

同一个 seed 下重新生成，只有那 5 列的取值规则改了，别的都是确定性的——这正好解释了
为什么行数和求和一样。所以**只动这 3 张，不碰另外 32 张**。

## 与 load.py 的关系：为什么是新脚本而不是加个开关

`load.py` 的主体是把 CSV 的全 string 列**还原成类型**（`cast_expr` 那一大段），加上
本地 CSV 的前置校验、gzip、行数基准。Parquet 已经是有类型的，这些全都不需要——真塞进
`load.py` 会变成一条处处 `if fmt == "parquet"` 的路径，而那条路径上大半代码不执行。

共用的只有两件事，这里也**照抄了同样的形状**，不是重新发明：

1. 源表写全限定名 `awsdatacatalog.<db>.<t>_pq`。目标 catalog 名带斜杠，不能出现在
   SQL 里（走 `QueryExecutionContext`）；源 catalog 名没有斜杠，可以直接写在 FROM 里。
2. 分区表要切批：Athena 的 Iceberg INSERT 一次最多开 100 个分区写入器
   （`ICEBERG_TOO_MANY_OPEN_PARTITIONS`）。这里**先查真实的分区基数再决定切不切**，
   而不是照抄「一律按月切」——`orders` 只跨 91 天，一条 INSERT 就够，切了反而多扫几遍。

## 时间戳精度这一条必须先验再灌

Parquet 里是 `timestamp[us]`，Iceberg 目标列是 `timestamp(6)`，中间隔着 Glue 外部表
声明的 Hive `timestamp`。Hive 的 `timestamp` 在 Athena 上是**毫秒**语义，真截断的话
`.535320` 会变成 `.535`，而且**不报错**——这正是 CSV 那条路上最贵的一个坑（`load.py`
的 `cast_expr` 里 `timestamp(6)` 那个 `(6)` 就是为它写的）。

所以 `--probe` 是 `--apply` 的前置，它做两件事：

1. **在 Athena 之外**数一遍源 Parquet 里有多少个亚秒时间戳（`subsecond_census`，用本地
   DuckDB 直读 `read_parquet`）。这一步不能走 Athena：要验的正是「Athena 这一层会不会
   截断」，用被验的那一层去验它，截断了也会数出 0。实测这三张表**一个亚秒值都没有**
   （8541400 + 4270700 + 149474 行全是整秒），所以毫秒语义的外部表在这里不丢东西。
   哪张表真有亚秒值，这一步会拒绝往下走。
2. 拿外部表和**现有 Iceberg 表**逐项比，时间戳按微秒比（`CAST(... AS VARCHAR)`，不是
   `format_datetime` 那种到秒的格式）。时间戳不受这次数据变更影响，两边本该相等。

第 2 步比的是**时刻**不是**渲染**：外部表是 `timestamp(3)`、Iceberg 是 `timestamp(6)`，
同一个整秒时刻分别渲染成 `22:44:25.000` 和 `22:44:25.000000`。所以比之前把小数尾部的
零去掉（`_norm_ts`）。这不会放过真截断——`.535` 和 `.53532` 去零之后照样不等。

用法：

    python3 scripts/lakehouse/load_parquet.py --probe          # 建外部表 + 逐项对比，不写
    python3 scripts/lakehouse/load_parquet.py --print-sql orders
    python3 scripts/lakehouse/load_parquet.py --apply          # DELETE + INSERT + 复核
    python3 scripts/lakehouse/load_parquet.py --selftest       # 拼装逻辑自测，不连云

环境变量：`AWS_REGION`、`RAW_GLUE_DB`（默认 `analytics_agent_raw`）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import gen_ddl  # noqa: E402

RAW_DB = os.environ.get("RAW_GLUE_DB", "analytics_agent_raw")
PQ_PREFIX = "parquet"

# 与 athena.py 同名同默认值。这里另写一遍而不是 import athena，因为 `subsecond_census`
# 要在模块层拿到它们，而 athena 会拉起 boto3——`--selftest` 承诺不连云也不依赖 SDK。
# 两处默认值分岔的后果是本地 DuckDB 和 Athena 读的不是同一个桶，而那时 count(*) 会差
# 几个数量级，是刷屏的红不是静默的绿，所以不再加一道相互校验。
REGION = os.environ.get("AWS_REGION", "us-west-2")
RAW_BUCKET = os.environ.get("RAW_BUCKET", "analytics-agent-raw")

#: 这一轮要补灌的表。写死而不是默认全表，理由是这个脚本的用途就是**补一次已知的差**：
#: 默认全表意味着一次误跑会 DELETE 掉 35 张表再重灌，而其中 32 张本来是对的。
#: 想灌别的表得显式 `--only`，那时是调用者自己判断。
STALE = ("orders", "push_notifications", "user_attributions")

#: 差异所在的列。`--probe` / `--apply` 会单独印这几列的 MIN/MAX，因为**指标层看不见
#: 文本列**：`verify_load.athena_probe` 只算行数、数值求和、时间边界、布尔计数，一列
#: 文本指标都不占。也就是说这次要修的东西，恰好是常规闸门结构上看不到的那一类。
WATCH = {
    "orders": ("cancel_reason", "refund_reason", "shipping_address"),
    "push_notifications": ("failure_reason",),
    "user_attributions": ("tracking_params",),
}

DML_TIMEOUT = 1740          # 与 load.py 同口径，理由见那边的注释
DDL_TIMEOUT = 600           # DDL 的服务端上限，不可调
MAX_OPEN_PARTITIONS = 100
#: 切批阈值留 10 个的余量，不贴着 100：分区基数会随数据窗口变化，贴着写下次涨一点就炸。
PARTITION_SAFE = MAX_OPEN_PARTITIONS - 10


# ---------------------------------------------------------------- 类型

#: Iceberg（DDL 写法）→ Hive（Glue 外部表写法）。绝大多数同名，列出来是为了**不认识的
#: 类型要报错而不是原样透传**：透传的后果是 Glue 建表成功、查的时候才报，而那时外部表
#: 已经留在 catalog 里了。
_HIVE = {"string": "string", "int": "int", "bigint": "bigint",
         "boolean": "boolean", "date": "date", "float": "float",
         "double": "double", "timestamp": "timestamp"}


def hive_type(ice: str) -> str:
    """Iceberg 类型 → Glue 外部表的 Hive 类型。

    `decimal(p,s)` 和 `array<...>` 原样成立（两边写法相同），其余走白名单。
    """
    if ice.startswith("decimal(") or ice.startswith("array<"):
        return ice
    if ice in _HIVE:
        return _HIVE[ice]
    raise ValueError(f"不认识的 Iceberg 类型 {ice!r}，先决定它的 Hive 写法再灌")


def external_ddl(table: str, cols: list, bucket: str) -> str:
    """指向 `parquet/<表>/` 的 Glue 外部表 DDL。

    列**按名字**对上 Parquet 里的字段（`parquet.column.index.access=false`）而不是按
    下标。按下标的话，生成器将来在中间插一列，读出来的就是整体错位的数据——每一列的
    类型多半还都对得上，于是不报错。名字对不上是报错，错位是静默出错，选会报错的那个。
    """
    body = ",\n".join(f"  `{c}` {hive_type(gen_ddl.map_type(pg))}"
                      for c, pg, _, _ in cols)
    return (
        f"CREATE EXTERNAL TABLE `{table}_pq` (\n{body}\n)\n"
        f"STORED AS PARQUET\n"
        f"LOCATION 's3://{bucket}/{PQ_PREFIX}/{table}/'\n"
        f"TBLPROPERTIES ('parquet.column.index.access'='false')")


def insert_sql(table: str, cols: list, where: str = "") -> str:
    """INSERT INTO <iceberg 表> SELECT <列> FROM <awsdatacatalog 的 parquet 外部表>。

    列一个不 CAST：Parquet 侧的类型就是目标类型（`decimal128(12,2)` ⟷
    `decimal(12,2)`、`timestamp[us]` ⟷ `timestamp(6)`）。多加一层 CAST 只会把
    「外部表读出来是什么」这个问题藏起来，而那个问题要靠 `--probe` 正面回答。

    列名显式列出而不是 `SELECT *`：外部表和 Iceberg 表的列序**目前**一致，但靠列序
    对齐的 INSERT 在将来某次改列时会静默灌错位的数据。
    """
    names = ",\n  ".join(f'"{c}"' for c, _, _, _ in cols)
    tail = f"\nWHERE {where}" if where else ""
    return (f"INSERT INTO {table} (\n  {names}\n)\nSELECT\n  {names}\n"
            f"FROM awsdatacatalog.{RAW_DB}.{table}_pq{tail}")


def month_batches(months: list[str], col: str) -> list[tuple[str, str]]:
    """[(批名, WHERE 子句)]，按月切，最后挂一批捞分区列为空的行。

    过滤写在 `CAST(col AS VARCHAR)` 的前缀上而不是 `col >= timestamp '...'`：后者能
    走 row-group 剪枝、更省扫描，但要拼月末边界（跨年、月长不同），拼错的表现是**丢行**。
    这里单表最大 186MB，剪枝省下的钱远不值这个风险。行数复核会兜住，但判据不该依赖
    另一条判据才成立。

    空值那一批不能省：`substr(...) = '2026-06'` 天然排除 NULL。
    """
    out = [(m, f"substr(CAST(\"{col}\" AS VARCHAR), 1, 7) = '{m}'") for m in months]
    out.append(("(空值)", f'"{col}" IS NULL'))
    return out


# ---------------------------------------------------------------- 对比

def probe_sql(table: str, cols: list) -> tuple[str, list[str]]:
    """拼一条聚合 SQL，用来把外部表和 Iceberg 表逐项比。返回 (SQL, 标签)。

    与 `verify_load.athena_probe` **不是同一把尺**，两点故意不同：

    1. 时间戳走 `CAST(... AS VARCHAR)`，到微秒。`athena_probe` 那边格式化到秒
       （`HH:mm:ss`），而这里要验的恰好是秒以下那几位有没有在外部表这一层被截掉。
    2. 多带 `WATCH` 里那几列的 MIN/MAX。指标层没有文本指标，而这次要补的差全在文本列——
       不带上的话，「灌完了」和「灌了一份一样的」在输出里长得一模一样。
    """
    sel, labels = ["COUNT(*)"], ["count"]
    for c, pg, _, _ in cols:
        ice = gen_ddl.map_type(pg)
        if ice.startswith("decimal(") or ice in ("int", "bigint", "float", "double"):
            sel.append(f'CAST(SUM(CAST("{c}" AS DECIMAL(38,4))) AS VARCHAR)')
            labels.append(f"sum:{c}")
        elif ice == "timestamp":
            for agg in ("MIN", "MAX"):
                # 到微秒。Trino 把 timestamp(6) 渲染成 2026-06-01 12:34:56.535320
                sel.append(f'CAST({agg}("{c}") AS VARCHAR)')
                labels.append(f"{agg.lower()}:{c}")
        elif ice == "boolean":
            sel.append(f'SUM(CASE WHEN "{c}" THEN 1 ELSE 0 END)')
            labels.append(f"true:{c}")
    for c in WATCH.get(table, ()):
        for agg in ("MIN", "MAX"):
            sel.append(f'{agg}("{c}")')
            labels.append(f"{agg.lower()}:{c}")
        sel.append(f'COUNT(DISTINCT "{c}")')
        labels.append(f"nd:{c}")
    return f'SELECT {", ".join(sel)} FROM "{table}"', labels


def metrics(client, table: str, cols: list, src: str = "") -> dict[str, str]:
    """跑 `probe_sql` 并 zip 成 {标签: 字符串}。`src` 非空时替换 FROM 的表名。"""
    sql, labels = probe_sql(table, cols)
    if src:
        sql = sql.replace(f'FROM "{table}"', f"FROM {src}")
    row = client.execute(sql, timeout=DML_TIMEOUT)["rows"][0]
    return {k: ("<NULL>" if v is None else str(v)) for k, v in zip(labels, row)}


def _norm_ts(v: str) -> str:
    """去掉时间戳小数部分尾部的零，好比**时刻**而不是**渲染**。

    外部表声明的 Hive `timestamp` 在 Athena 上是 `timestamp(3)`，Iceberg 目标列是
    `timestamp(6)`，同一个整秒时刻分别渲染成 `22:44:25.000` 和 `22:44:25.000000`。
    不去零的话这三张表的每一个时间戳列都报差异，而 32 条噪声会把真差异埋掉。

    **这不等于放宽到秒**：`.535` 去零还是 `.535`，`.53532` 还是 `.53532`，真截断照样不等。
    只**在时间戳列上**用——文本列（`WATCH` 里那几列也走 MIN/MAX）去零会改掉真值，
    所以判据是列名在 `ts_cols` 里，不是标签以 `min:` 开头。
    """
    head, dot, frac = v.partition(".")
    if not dot:
        return v
    frac = frac.rstrip("0")
    return head + ("." + frac if frac else "")


def compare(table: str, ice: dict[str, str], pq: dict[str, str],
            ts_cols: frozenset[str] = frozenset()) -> list[str]:
    """返回硬差异清单，并把预期差异打印出来。

    `WATCH` 里那几列**应该**不同——那就是要修的东西。其余任何一项不同都是硬差异：
    行数、金额、时间戳都不受这次数据变更影响，不等只能是外部表这一层读错了。
    """
    watch = set(WATCH.get(table, ()))
    hard, expected = [], []
    for k in sorted(set(ice) | set(pq)):
        a, b = ice.get(k, "<缺>"), pq.get(k, "<缺>")
        col = k.split(":", 1)[1] if ":" in k else ""
        if col in ts_cols:
            a, b = _norm_ts(a), _norm_ts(b)
        if a == b:
            continue
        (expected if col in watch else hard).append(
            f"{table}.{k}：iceberg={a[:80]}  parquet={b[:80]}")
    for e in expected:
        print(f"      要修的差异  {e}")
    return hard


def ts_columns(cols: list) -> frozenset[str]:
    """表里的时间戳列名。只用于决定哪些值该走 `_norm_ts`。"""
    return frozenset(c for c, pg, _, _ in cols
                     if gen_ddl.map_type(pg) == "timestamp")


def subsecond_census(tables: list[str], declared: dict) -> dict[str, int]:
    """数源 Parquet 里的亚秒时间戳行数 → {表: 行数}。**不经过 Athena。**

    要验的是「Athena 的毫秒语义外部表会不会截断」，所以这一步必须绕开 Athena：用它自己
    去数，截断了也只会数出 0。本地 DuckDB 直读 `read_parquet`，微秒原样。

    一条 `COUNT(*) ... WHERE a OR b OR ...` 而不是每列一个 `SUM(CASE ...)`：后者在
    `push_notifications`（186MB / 5 个时间戳列）上要跑几分钟，前者一遍扫完。代价是
    只知道「有没有」不知道「在哪列」——真有的时候再单独查，而按实测这里恒为 0。
    """
    import duckdb

    c = duckdb.connect()
    # 进度条会往 stderr 刷几 MB 的转义序列，把真正的输出埋掉
    c.execute("SET enable_progress_bar=false")
    c.execute("INSTALL httpfs; LOAD httpfs")
    c.execute("CREATE OR REPLACE SECRET s3_default "
              f"(TYPE s3, PROVIDER credential_chain, REGION '{REGION}')")
    out = {}
    for t in tables:
        ts = sorted(ts_columns(declared[t]))
        if not ts:
            out[t] = 0
            continue
        pred = " OR ".join(f'(epoch_us("{c0}") % 1000000 <> 0)' for c0 in ts)
        out[t] = c.execute(
            f"SELECT COUNT(*) FROM read_parquet("
            f"'s3://{RAW_BUCKET}/{PQ_PREFIX}/{t}/*.parquet') WHERE {pred}"
        ).fetchone()[0]
    c.close()
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="Parquet → Iceberg 补灌")
    ap.add_argument("--only", nargs="*", help=f"只处理这些表（默认 {list(STALE)}）")
    ap.add_argument("--probe", action="store_true",
                    help="建外部表并逐项对比，**不写任何数据**")
    ap.add_argument("--apply", action="store_true",
                    help="DELETE FROM + INSERT + 复核。会改 Iceberg 表")
    ap.add_argument("--print-sql", metavar="TABLE", help="打印该表的 SQL 就退出")
    ap.add_argument("--selftest", action="store_true", help="拼装逻辑自测，不连云")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    declared = {t: cols for _src, t, cols in gen_ddl.parse_source()}
    tables = a.only or list(STALE)
    if bad := [t for t in tables if t not in declared]:
        print(f"这些表不在 DDL 真源里：{bad}", file=sys.stderr)
        return 1

    if a.print_sql:
        t = a.print_sql
        if t not in declared:
            print(f"没有表 {t}", file=sys.stderr)
            return 1
        print(external_ddl(t, declared[t], "<桶>") + ";\n")
        print(f"DELETE FROM {t};\n")
        print(insert_sql(t, declared[t]) + ";")
        if spec := gen_ddl.PARTITION_SPEC.get(t):
            print(f"\n-- 分区 {spec}；基数超过 {PARTITION_SAFE} 时按月切，例如：")
            print(insert_sql(t, declared[t], month_batches(["2026-06"], spec[1])[0][1]))
        return 0

    if not (a.probe or a.apply):
        ap.error("给 --probe 或 --apply")

    import athena
    ice = athena.Client()                                  # S3 Tables catalog
    raw = athena.Client(catalog="awsdatacatalog", database=RAW_DB)
    bucket = athena.RAW_BUCKET

    print(f"外部表：s3://{bucket}/{PQ_PREFIX}/<表>/ → {RAW_DB}.<表>_pq")
    print(f"目标：  Iceberg {len(tables)} 张 —— {'、'.join(tables)}\n")

    # 精度前置：源里有亚秒时间戳的话，毫秒语义的外部表会静默截断，不能灌。
    # 走本地 DuckDB 而不是 Athena，理由见 subsecond_census 的 docstring。
    print("亚秒时间戳普查（本地 DuckDB 直读 Parquet，绕开 Athena）")
    sub = subsecond_census(tables, declared)
    for t, n in sub.items():
        print(f"  {'✅' if n == 0 else '❌'} {t:<22} {n:,} 行带亚秒值")
    if any(sub.values()):
        print("\n源里有亚秒时间戳 ❌  Glue 外部表的 Hive `timestamp` 是毫秒语义，会静默"
              "截断。\n要灌得先换一种读法（比如把那几列声明成 bigint 再 "
              "from_unixtime_nanos），别直接灌。")
        return 1
    print()

    hard: list[str] = []
    for t in tables:
        cols = declared[t]
        # DROP + CREATE：列改过也不会留下旧定义。DDL 超时是服务端的 600s。
        raw.execute(f"DROP TABLE IF EXISTS `{t}_pq`", fetch=False, timeout=DDL_TIMEOUT)
        raw.execute(external_ddl(t, cols, bucket), fetch=False, timeout=DDL_TIMEOUT)

        m_pq = metrics(ice, t, cols, src=f"awsdatacatalog.{RAW_DB}.{t}_pq")
        m_ice = metrics(ice, t, cols)
        bad = compare(t, m_ice, m_pq, ts_columns(cols))
        n_ice, n_pq = m_ice["count"], m_pq["count"]
        if bad:
            hard += bad
            print(f"  ❌ {t:<22} iceberg {n_ice} 行 / parquet {n_pq} 行，"
                  f"{len(bad)} 项**非预期**差异")
        else:
            print(f"  ✅ {t:<22} iceberg {n_ice} 行 / parquet {n_pq} 行，"
                  f"除 WATCH 列外逐项相等（含微秒级时间戳）")

    if hard:
        print(f"\n非预期差异 {len(hard)} 项 ❌  **不要灌**：\n")
        for h in hard[:40]:
            print(f"  - {h}")
        print("\n行数、金额、时间戳都不该受这次数据变更影响。这些项不等，指向的是外部表"
              "这一层——Hive `timestamp` 是毫秒语义，读 Parquet 的 timestamp[us] 会静默"
              "截断，见模块 docstring。")
        return 1

    if a.probe:
        print("\n对比通过 ✅  外部表读得住 Parquet，差异只在 WATCH 的那几列上。")
        print("  这一步没写任何数据。要真灌：--apply")
        return 0

    # ---------------- 真灌
    print("\n开始灌。每张表 DELETE FROM 再 INSERT——Iceberg 支持行级 DELETE，所以是幂等的。")
    t0, failed = time.time(), []
    for t in tables:
        cols, t1 = declared[t], time.time()
        want = int(metrics(ice, t, cols, src=f"awsdatacatalog.{RAW_DB}.{t}_pq")["count"])
        try:
            ice.execute(f"DELETE FROM {t}", fetch=False, timeout=DML_TIMEOUT)
            spec = gen_ddl.PARTITION_SPEC.get(t)
            batches = [("(整表)", "")]
            if spec:
                col = spec[1]
                # 先问真实基数再决定切不切。一律按月切会让每批各扫一遍源文件，
                # 而基数本来就在限额里的表（orders 只跨 91 天）根本不需要切。
                keys = raw.execute(
                    f'SELECT COUNT(DISTINCT substr(CAST("{col}" AS VARCHAR), 1, 10)),'
                    f' array_agg(DISTINCT substr(CAST("{col}" AS VARCHAR), 1, 7))'
                    f" FROM {t}_pq")["rows"][0]
                nday = int(keys[0])
                print(f"    {t} 分区列 {col} 基数 {nday} 天"
                      f"（上限 {MAX_OPEN_PARTITIONS} 个写入器）")
                if nday > PARTITION_SAFE:
                    months = sorted(str(keys[1]).strip("[]").replace(" ", "").split(","))
                    batches = month_batches(months, col)
                    print(f"    {t} 切 {len(batches)} 批")
            for i, (label, where) in enumerate(batches, 1):
                ice.execute(insert_sql(t, cols, where),
                            fetch=False, timeout=DML_TIMEOUT)
                if len(batches) > 1:
                    print(f"    {t} 批 {i}/{len(batches)} {label}", flush=True)
            after = metrics(ice, t, cols)
        except Exception as e:                                   # noqa: BLE001
            print(f"  {t:<22} FAIL  {str(e)[:400]}")
            failed.append(t)
            continue
        n = int(after["count"])
        ok = n == want
        # 复核不只看行数：WATCH 那几列必须真的变成多值了。行数相等而列还是假常量，
        # 恰好是「灌了一份一样的数据」，而那是这个脚本唯一要修的东西。
        flat = [c for c in WATCH.get(t, ()) if after.get(f"nd:{c}", "0") in ("0", "1")]
        if flat:
            ok = False
            print(f"    {t} 这几列灌完还是单值：{flat}——源 Parquet 本身就是常量？")
        print(f"  {'✅' if ok else '❌'} {t:<22} {n:>9,} 行  "
              f"{time.time()-t1:>6.1f}s" + ("" if n == want else f"  期望 {want:,}"))
        if not ok:
            failed.append(t)

    print(f"\n耗时 {time.time()-t0:.1f}s")
    if failed:
        print(f"失败 {len(failed)} 张：{failed} ❌")
        return 1
    print("全部灌完 ✅  接着要跑的两件事：")
    print("  python3 scripts/bench/query_correctness.py --values   # 确认 5 列收敛")
    print("  python3 scripts/bench/correctness.py --arms-only -t "
          + " -t ".join(tables))
    print("  然后把这 5 列从 verify_constants.py 的 RELOAD_PENDING 里划掉。")
    print("\nDELETE + INSERT 没动表定义，所以 Lake Formation 授权还在"
          "（那是 --recreate 才会清掉的东西，见 load.py）。")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """不连云能验的：类型映射、DDL/INSERT 拼装、切批、差异分类。"""
    bad = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal bad
        if not cond:
            bad += 1
            print(f"  FAIL {name}{'：' + detail if detail else ''}")

    # 1. 类型映射：decimal / array 原样，白名单外报错
    check("decimal 原样", hive_type("decimal(12,2)") == "decimal(12,2)")
    check("array 原样", hive_type("array<string>") == "array<string>")
    check("int 不变成 integer", hive_type("int") == "int")
    try:
        hive_type("hyperloglog")
        check("未知类型该报错", False)
    except ValueError:
        pass

    declared = {t: cols for _src, t, cols in gen_ddl.parse_source()}

    # 2. 三张表的每一列都能映射。映射不了必须现在就知道，而不是建表时才知道——
    #    那时 DROP 已经执行过了。
    for t in STALE:
        for c, pg, _, _ in declared[t]:
            try:
                hive_type(gen_ddl.map_type(pg))
            except ValueError as e:
                check(f"{t}.{c} 类型映射", False, str(e))

    # 3. DDL：STORED AS PARQUET、按名字对列、LOCATION 指向 parquet 前缀
    ddl = external_ddl("orders", declared["orders"], "b")
    check("STORED AS PARQUET", "STORED AS PARQUET" in ddl)
    check("按名字对列", "'parquet.column.index.access'='false'" in ddl)
    check("LOCATION", "s3://b/parquet/orders/" in ddl)
    check("不带 OpenCSVSerde", "OpenCSVSerde" not in ddl)
    check("decimal 进了 DDL", "`total_amount` decimal(12,2)" in ddl)

    # 4. INSERT：列名显式、跨 catalog 写全限定名、一个 CAST 都没有
    ins = insert_sql("orders", declared["orders"])
    check("跨 catalog 源表", f"FROM awsdatacatalog.{RAW_DB}.orders_pq" in ins)
    check("不用 SELECT *", "SELECT *" not in ins)
    check("没有 CAST", "CAST(" not in ins, ins[:200])
    check("列数对上", ins.count('"order_id"') == 2)     # 目标列表 + SELECT 各一次

    # 5. 切批：每月一条 + 空值一条，且空值那条不能被月份谓词覆盖。
    #    少了空值这一批的后果是静默丢行，所以单独断言。
    b = month_batches(["2026-06", "2026-07"], "placed_at")
    check("批数 = 月数 + 1", len(b) == 3, str(b))
    check("末批捞空值", "IS NULL" in b[-1][1])
    check("月份谓词用字符串前缀", "substr(CAST(\"placed_at\" AS VARCHAR), 1, 7)" in b[0][1])

    # 6. probe 的时间戳到微秒，不是到秒。这一条是 --apply 的前置判据本身，
    #    退化成 format_datetime 那种到秒的格式就等于这道闸门失效了。
    sql, labels = probe_sql("orders", declared["orders"])
    check("时间戳 CAST 成 VARCHAR", 'CAST(MIN("placed_at") AS VARCHAR)' in sql)
    check("不用 format_datetime", "format_datetime" not in sql)
    check("WATCH 列进了标签", "nd:cancel_reason" in labels)
    check("非 WATCH 的文本列不进标签", "min:remark" not in labels)

    # 7. 差异分类：WATCH 列的差异是「要修的」，别的是硬差异
    ots = ts_columns(declared["orders"])
    check("placed_at 认成时间戳列", "placed_at" in ots)
    check("cancel_reason 不是时间戳列", "cancel_reason" not in ots)
    hard = compare("orders",
                   {"count": "9", "min:cancel_reason": "用户取消", "max:placed_at": "x"},
                   {"count": "9", "min:cancel_reason": "已取消", "max:placed_at": "x"},
                   ots)
    check("WATCH 差异不算硬", hard == [], str(hard))
    hard = compare("orders", {"count": "9"}, {"count": "8"}, ots)
    check("行数差算硬差异", len(hard) == 1, str(hard))

    # 8. 时间戳去零：比时刻不比渲染，但真截断必须还是不等。
    #    这两条是一体的——只保前一条会把 32 条真差异也放过去，只保后一条会被 32 条
    #    噪声刷屏。所以正反都断言。
    check("整秒两种渲染视作相等", _norm_ts("22:44:25.000") == _norm_ts("22:44:25.000000"))
    check("去零不改真值", _norm_ts("22:44:25.53532") == "22:44:25.53532")
    check("无小数点原样", _norm_ts("22:44:25") == "22:44:25")
    ts_same = compare("orders", {"max:placed_at": "2026-01-24 22:44:25.000000"},
                      {"max:placed_at": "2026-01-24 22:44:25.000"}, ots)
    check("整秒不报差异", ts_same == [], str(ts_same))
    ts_cut = compare("orders", {"max:placed_at": "2026-01-24 22:44:25.53532"},
                     {"max:placed_at": "2026-01-24 22:44:25.535"}, ots)
    check("真截断仍算硬差异", len(ts_cut) == 1, str(ts_cut))
    # 去零必须**按列**开，不是对所有值开：同一对值在列不在 ts_cols 里时要照报。
    # 不这么钉的话，将来把去零挪到 `_norm_ts` 之外统一处理，文本列的真差异会被抹掉。
    gated = compare("orders", {"sum:item_count": "1.000000"},
                    {"sum:item_count": "1.000"}, frozenset())
    check("去零只对时间戳列生效", len(gated) == 1, str(gated))

    # 9. 默认表集合就是那 3 张，不是全表。误跑一次的代价差 32 张表。
    check("默认只 3 张", set(STALE) == {"orders", "push_notifications",
                                        "user_attributions"})
    check("WATCH 覆盖 STALE", set(WATCH) == set(STALE))

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  类型映射 4 项、列可映射 48 列、DDL 5 项、INSERT 4 项、切批 3 项、"
          "probe 4 项、差异分类 4 项、时间戳去零 6 项、默认集合 2 项")
    print("拼装逻辑自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
