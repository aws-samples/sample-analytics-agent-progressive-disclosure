#!/usr/bin/env python3
"""数据一致性快照 —— 迁移前后自动对账，替代人工数据核查。

## 为什么需要它（与 eval harness 的分工）

`eval/run_eval.py` 驱动真实 agent 答 21 道题，判分带容差、只覆盖被出题的那些表。
它抓的是「口径错、选错表」这类语义问题，抓不住整体性偏移：某张没被出题覆盖的表
`COPY` 时静默少了 3% 的行，或者某个 DECIMAL 列在方言转换中丢了精度，eval 全绿。

本脚本补的正是这一层：对**每张表**取 `count(*)`、数值列 `SUM`、时间列 `MIN/MAX`、
布尔列真值计数，迁移前后各跑一次做精确 diff。一次写好永久复用，不需要人工看数。

## 和 `scripts/lakehouse/verify_load.py` 的分工

那个脚本把 `data/csv/*.csv` 和 Athena 现查的表当场比，两边都现算，没有会过期的中间
文件——它的 docstring 里说得对：写死的基线数字总会变成「过期时是绿的」。

本脚本在湖仓这条链路上仍然有一件它做不到的事：**8000 万行的 CSV 侧对账，Python 逐行
读不动**。生成器在生成时顺手用 int64 累加出 `_expected.json`（见 `scripts/gen/main.py`
的模块 docstring），代价接近零；拿它对 Athena 快照，验的是「生成 → 序列化 → 传输 →
装载」全链路无损，而且不需要把 6.8GB 再读一遍。

所以判据是：`_expected.json` 是**这一次生成**当场算的，不是仓库里存着的基线。
`eval/baseline/consistency.*.json` 那几份描述的是 v2 那批数据，已经是历史，不要拿来当门。

## 用法

    # 湖仓现状快照（Athena / S3 Tables，当前架构）
    SNAPSHOT_BACKEND=athena \\
      python3 scripts/consistency/snapshot.py --out /tmp/athena.json

    # 与生成器当场算出的预期值对账（--subset：库里 48 张表，生成器只覆盖 24 张）
    python3 scripts/consistency/snapshot.py --subset --compare \\
      data/csv/_expected.json /tmp/athena.json

    # 跨方言渲染自测（不连库）
    python3 scripts/consistency/snapshot.py --selftest

    # 已退役的两条：本地 Postgres 基线 / Redshift Data API
    python3 scripts/consistency/snapshot.py --out eval/baseline/consistency.postgres.json
    SNAPSHOT_BACKEND=redshift-data \\
      python3 scripts/consistency/snapshot.py --out eval/baseline/consistency.redshift.json

## 可移植性要点（Postgres / Redshift / Trino）

1. 只用 `information_schema`，三边都有；不碰 `pg_catalog` / `svv_*` / `$tables`。
2. **数值列 SUM 前先 CAST 成 DECIMAL(38,4)**。Redshift 和 Trino 都是 MPP，
   `double` 的求和在多 slice / 多 worker 并行下累加顺序不确定，浮点尾数会与单机
   Postgres 不一致，直接比会出现大量假阳性。定点化后求和结果与累加顺序无关。
3. 聚合本身是标准的，**类型名和时间渲染不是**——这两处按方言分开（见 `Dialect`）：
   Postgres 报 `timestamp without time zone`，Trino 报 `timestamp(6)`；`TO_CHAR`
   在 Trino 里根本不存在。所以「换后端只换执行器」这句话在加进 Athena 那一刻就不成立了，
   写死一套类型名的后果是**所有数值列和时间列被当成"其余类型"静默跳过**，快照只剩
   `count`，而输出看起来完全正常。`--selftest` 钉的就是这件事。

## 空列的求和是 NULL，不是 0

一列全是 NULL 时，SQL 的 `SUM` 返回 **NULL**，不是 0（`channel_daily_costs.creative_id`
现在就是这个形态）。生成侧一度用 int64 累加吐 `"0.0000"`，两边对账会在这种列上假失败。
判据统一按 SQL 走：没有非空值就是 `null`。生成侧同步改了（`scripts/gen/main.py`
的 `Expected.render`），并且改成**按 DDL 列出的列生成键**，而不是按"这一批数据里恰好
有值的列"——否则库里有键、清单里没键，同一件事换个方向再假失败一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# 定点化宽度。DECIMAL(38,4) 足以容纳本库所有金额/计数列且不溢出；
# 小数位固定为 4 使两端截断行为一致。
DECIMAL_CAST = "DECIMAL(38,4)"

# 挂了动态数据脱敏（DDM）的列，一致性对账要跳过。
#
# 原因：治理层给这些列挂了 masking policy（见 database/redshift/04_governance.sql），
# **查询会读到掩码值**——`birth_date` 被 DATE_TRUNC 到年、`email` 被换成固定串。
# 而生成器吐出的预期值是原始值，两者必然不等。这不是数据损坏，是脱敏按设计生效。
#
# 所以这两个机制在被脱敏的列上是互斥的：要么对账，要么脱敏，不能既读到原文又保证不可读。
# 取舍是脱敏优先（PII 保护比对账覆盖率重要），代价是这几列失去搬迁损耗的自动校验。
# 加新的 masking policy 时必须同步这里，否则对账会报假失败。
#
# **这一组只对 Postgres / Redshift 成立。** 湖仓那侧的 L4 治理是 Lake Formation 的
# 列级排除，不是掩码（AGENTS.md 里专门写了这个区别）：被排除的列在
# `information_schema.columns` 里根本不出现，取数时自然轮不到它；而能看见的列拿到的
# 就是原值，可以正常对账。所以 `TrinoDialect.masked` 是空集——把这一组照搬过去会
# 无声少掉 `user_profiles.birth_date` 的 min/max，而清单里有这两个键，对账会假失败。
MASKED_COLUMNS = {
    ("users", "email"),
    ("users", "phone"),
    ("user_profiles", "birth_date"),
}


# ---------------------------------------------------------------- dialects
#
# 一个方言要回答三件事：某个 `information_schema` 类型名归哪一类、时间列怎么渲染成
# 规范文本、哪些列不参与对账。除此之外的 SQL 两边共用。

class PgDialect:
    """Postgres / Redshift。

    这一支的渲染**一个字节都不能变**：`eval/baseline/consistency.postgres.json` 和
    `.redshift.json` 是按它产出的，而且 `scripts/gen/main.py` 的 `_expected.json`
    也照着它的类型分类走。加 Trino 支的时候把它抽出来而不是改写，就是为了这条。
    """

    name = "postgres"
    default_schema = "public"
    masked = MASKED_COLUMNS

    NUMERIC = {"smallint", "integer", "bigint",
               "decimal", "numeric", "real", "double precision"}
    TEMPORAL = {"date", "timestamp without time zone", "timestamp with time zone",
                "time without time zone", "time with time zone"}

    @classmethod
    def kind(cls, dtype: str) -> str | None:
        if dtype in cls.NUMERIC:
            return "num"
        if dtype in cls.TEMPORAL:
            return "date" if dtype == "date" else "time"
        if dtype == "boolean":
            return "bool"
        return None

    @classmethod
    def temporal(cls, agg: str, q: str, kind: str) -> str:
        # 时间列**必须由 SQL 渲染成规范文本**：psycopg 回 datetime 对象、
        # Redshift Data API 回 '2025-10-26 00:02:22.0' 这种带空格和 .0 的串，
        # 两边直接比会全表假失败。TO_CHAR 的格式串 Postgres / Redshift 都支持。
        fmt = "YYYY-MM-DD" if kind == "date" else 'YYYY-MM-DD"T"HH24:MI:SS'
        return f"TO_CHAR({agg}({q}), '{fmt}')"


class TrinoDialect(PgDialect):
    """Athena / Trino。

    类型名带参数（`decimal(12,2)` / `timestamp(6)`），所以判类只能按前缀，不能查集合。
    时间渲染换成 `date_format`：**Trino 没有 `TO_CHAR`**，照抄那一支会直接报
    `FUNCTION_NOT_FOUND`（这一条算走运——真正危险的是类型名，它不报错，只是静默漏列）。
    """

    name = "trino"
    default_schema = None                  # 由后端给（Iceberg namespace）
    masked: frozenset = frozenset()        # 见 MASKED_COLUMNS 注释

    NUMERIC_EXACT = {"tinyint", "smallint", "integer", "bigint", "real", "double"}

    @classmethod
    def kind(cls, dtype: str) -> str | None:
        if dtype in cls.NUMERIC_EXACT or dtype.startswith("decimal("):
            return "num"
        if dtype == "date":
            return "date"
        if dtype.startswith("timestamp"):
            return "time"
        if dtype == "boolean":
            return "bool"
        if dtype.startswith("time"):
            # 本库没有 time 列。真出现一列时不能猜渲染：Trino 的 time 和 Postgres 的
            # `TO_CHAR(..., 'YYYY-MM-DD"T"HH24:MI:SS')` 对不上（那个格式串套在 time 上
            # 本身就是错的），随便写一个只会让两边在这一列上永远不相等。宁可当场炸。
            raise ValueError(f"time 类型（{dtype}）没有跨方言一致的渲染，先定口径再加")
        return None                        # varchar / char / varbinary / array(...) / row(...)

    @classmethod
    def temporal(cls, agg: str, q: str, kind: str) -> str:
        if kind == "date":
            return f"CAST({agg}({q}) AS VARCHAR)"          # → '2025-11-02'
        return f"date_format({agg}({q}), '%Y-%m-%dT%H:%i:%S')"


# ---------------------------------------------------------------- backends

class PostgresBackend:
    """本地 Postgres / Aurora。迁移前基线走这条。"""

    name = "postgres"
    dialect = PgDialect

    def __init__(self) -> None:
        import psycopg  # 延迟 import：另一后端不需要它

        self._conn = psycopg.connect(
            host=os.getenv("PGHOST", "127.0.0.1"),
            port=int(os.getenv("PGPORT", "5433")),
            dbname=os.getenv("PGDATABASE", "app_analytics"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD", ""),
            connect_timeout=15,
        )
        self._conn.read_only = True

    def query(self, sql: str) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def close(self) -> None:
        self._conn.close()


class RedshiftDataApiBackend:
    """Redshift Serverless via Data API。

    复用 `scripts/redshift/rsql.py` 的客户端（那边已封好异步三步：ExecuteStatement →
    DescribeStatement 轮询 → GetStatementResult）。

    配置走环境变量：REDSHIFT_WORKGROUP / REDSHIFT_DATABASE / REDSHIFT_SECRET_ARN。
    不给 SECRET_ARN 时走 IAM 临时凭证，库用户由调用者的 IAM 身份派生。
    """

    name = "redshift-data"
    dialect = PgDialect

    def __init__(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "redshift"))
        import rsql
        self._rsql = rsql
        self._c = rsql.Client(
            workgroup=os.getenv("REDSHIFT_WORKGROUP", rsql.WORKGROUP),
            database=os.getenv("REDSHIFT_DATABASE", rsql.DATABASE),
            secret_arn=os.getenv("REDSHIFT_SECRET_ARN", rsql.SECRET_ARN),
        )

    def query(self, sql: str) -> list[tuple]:
        res = self._c.execute(sql, timeout=1800)
        return [tuple(r) for r in res.get("rows", [])]

    def close(self) -> None:
        pass                                  # Data API 无连接可关


class AthenaBackend:
    """Athena over S3 Tables / Iceberg。**当前架构走这条。**

    复用 `scripts/lakehouse/athena.py` 的 `Client`：catalog / database 走
    QueryExecutionContext 而不是 SQL 前缀，`_scalar` 已经按 ResultSetMetadata 把整数
    转成 int、把 varchar 原样留成 str——所以 SQL 渲染出来的定点文本（'9535347.6600'）
    和时间文本原封不动到这里，`normalize` 不需要再猜。

    超时给 1740s：8000 万行下 `page_views` / `post_likes` 这几张表的整表聚合会明显
    慢于默认的 900s。这个数取自 `scripts/lakehouse/load.py::DML_TIMEOUT`，理由写在
    那边——比 Athena 的 DML 配额（30 分钟）压低一分钟，好让超时只有一种表现。
    """

    name = "athena"
    dialect = TrinoDialect
    timeout = 1740

    def __init__(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lakehouse"))
        import athena
        self._c = athena.Client()
        self.default_schema = self._c.database

    def query(self, sql: str) -> list[tuple]:
        res = self._c.execute(sql, timeout=self.timeout)
        return [tuple(r) for r in res.get("rows", [])]

    def close(self) -> None:
        pass                                  # Athena 无连接可关


BACKENDS = {
    PostgresBackend.name: PostgresBackend,
    RedshiftDataApiBackend.name: RedshiftDataApiBackend,
    AthenaBackend.name: AthenaBackend,
}


def make_backend():
    kind = os.getenv("SNAPSHOT_BACKEND", PostgresBackend.name)
    if kind not in BACKENDS:
        sys.exit(f"未知 SNAPSHOT_BACKEND='{kind}'，可选：{sorted(BACKENDS)}")
    return BACKENDS[kind]()


# ---------------------------------------------------------------- introspect

def list_tables(be, schema: str) -> list[str]:
    rows = be.query(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    )
    return [r[0] for r in rows]


def list_columns(be, schema: str, table: str) -> list[tuple[str, str]]:
    rows = be.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    )
    return [(r[0], r[1]) for r in rows]


def build_probe_sql(schema: str, table: str, columns: list[tuple[str, str]],
                    dialect=PgDialect) -> tuple[str, list[str]]:
    """为一张表生成一条聚合 SQL，返回 (sql, 指标名列表)。

    指标名形如 'count'、'sum:actual_amount'、'min:paid_at'、'true:is_active'。
    默认方言是 Postgres，所以老调用方（以及那两份基线）的输出一字不变。
    """
    selects = ["COUNT(*)"]
    labels = ["count"]

    for name, dtype in columns:
        if (table, name) in dialect.masked:
            continue                      # 见 MASKED_COLUMNS 注释：脱敏列不参与对账
        q = f'"{name}"'
        k = dialect.kind(dtype)
        if k == "num":
            # 见模块 docstring 第 2 点：先定点化再求和，规避 MPP 浮点累加顺序差异。
            # 再 CAST 成 VARCHAR：DECIMAL 经不同驱动回来可能是 Decimal 也可能是 str，
            # 由 SQL 统一渲染成保留标度的文本（'123.4500'），客户端就不必猜。
            selects.append(f"CAST(SUM(CAST({q} AS {DECIMAL_CAST})) AS VARCHAR)")
            labels.append(f"sum:{name}")
        elif k in ("date", "time"):
            selects.append(dialect.temporal("MIN", q, k))
            labels.append(f"min:{name}")
            selects.append(dialect.temporal("MAX", q, k))
            labels.append(f"max:{name}")
        elif k == "bool":
            selects.append(f"SUM(CASE WHEN {q} THEN 1 ELSE 0 END)")
            labels.append(f"true:{name}")
        # 其余类型（varchar/text/json/uuid/bytea/array…）不取指标：
        # 代价高、跨方言表示不稳，且行数与数值列已足够暴露搬迁偏移。

    sql = f'SELECT {", ".join(selects)} FROM "{schema}"."{table}"'
    return sql, labels


def normalize(v: Any) -> Any:
    """把驱动返回值规约成可稳定 JSON 序列化、且跨后端可比的形式。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    # Decimal / float：统一成定长字符串，避免 JSON float 表示差异
    import decimal
    if isinstance(v, decimal.Decimal):
        return f"{v:.4f}"
    if isinstance(v, float):
        return f"{decimal.Decimal(repr(v)):.4f}"
    # date / datetime / time
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def take_snapshot(be, schema: str) -> dict:
    tables = list_tables(be, schema)
    out: dict[str, dict[str, Any]] = {}
    for t in tables:
        cols = list_columns(be, schema, t)
        sql, labels = build_probe_sql(schema, t, cols, be.dialect)
        row = be.query(sql)[0]
        out[t] = {label: normalize(val) for label, val in zip(labels, row)}
        print(f"  {t:<32} count={out[t]['count']:>8}  ({len(labels) - 1} 项指标)",
              file=sys.stderr)
    return {
        "schema": schema,
        "backend": be.name,
        "table_count": len(tables),
        "tables": out,
    }


# ---------------------------------------------------------------- compare

def compare(a: dict, b: dict, subset: bool = False) -> list[str]:
    """返回差异描述列表。空列表 = 完全一致。

    subset=True 时只比 A 里有的表，B 多出来的表不算差异。用于「生成器预期值（只覆盖
    21 张事实表）vs 库里实际快照（48 张，含维度表和 mart）」这种场景。
    """
    diffs: list[str] = []
    ta, tb = a.get("tables", {}), b.get("tables", {})

    only_a = sorted(set(ta) - set(tb))
    only_b = sorted(set(tb) - set(ta))
    for t in only_a:
        diffs.append(f"[缺表] {t}：仅存在于 A（{a.get('backend')}）")
    if not subset:
        for t in only_b:
            diffs.append(f"[多表] {t}：仅存在于 B（{b.get('backend')}）")

    for t in sorted(set(ta) & set(tb)):
        ma, mb = ta[t], tb[t]
        for k in sorted(set(ma) | set(mb)):
            va, vb = ma.get(k, "<无>"), mb.get(k, "<无>")
            if va != vb:
                diffs.append(f"[不一致] {t}.{k}：A={va}  B={vb}")
    return diffs


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    """跨方言渲染自测（不连库）。

    钉两件事：Postgres 那一支的 SQL 与加 Trino 之前**逐字节相同**（下面的期望串是从
    改动前的输出抄来的），以及 Trino 那一支认得带参数的类型名。第二条是这次真正的缺陷
    面——类型名对不上时 `kind()` 返回 None，列被当成 varchar 跳过，快照只剩 `count`，
    而脚本正常退出、输出格式完全正常，没有任何一层会红。
    """
    bad = 0

    def want(label: str, got, exp) -> None:
        nonlocal bad
        if got != exp:
            bad += 1
            print(f"  FAIL {label}\n       期望 {exp!r}\n       实际 {got!r}")

    # --- 类型分类 ---------------------------------------------------------
    for dtype, exp in [("bigint", "num"), ("integer", "num"), ("smallint", "num"),
                       ("numeric", "num"), ("double precision", "num"),
                       ("date", "date"), ("timestamp without time zone", "time"),
                       ("boolean", "bool"), ("character varying", None),
                       ("text", None), ("jsonb", None)]:
        want(f"pg kind({dtype})", PgDialect.kind(dtype), exp)

    # 这一组全是 Athena 的 information_schema 现报的字面值（2026-08-31 实测 48 张表，
    # 类型名只有这些）。`decimal(12,2)` / `timestamp(6)` 不在 Postgres 那组集合里，
    # 照抄一套类型名就是在这里静默漏掉全部金额列和全部时间列。
    for dtype, exp in [("bigint", "num"), ("integer", "num"), ("double", "num"),
                       ("decimal(10,2)", "num"), ("decimal(12,2)", "num"),
                       ("decimal(38,4)", "num"), ("decimal(2,1)", "num"),
                       ("date", "date"), ("timestamp(6)", "time"),
                       ("timestamp(3) with time zone", "time"),
                       ("boolean", "bool"), ("varchar", None), ("char(3)", None),
                       ("array(varchar)", None), ("array(bigint)", None),
                       ("row(a bigint)", None), ("varbinary", None)]:
        want(f"trino kind({dtype})", TrinoDialect.kind(dtype), exp)

    try:
        TrinoDialect.kind("time(6)")
        bad += 1
        print("  FAIL trino kind(time(6)) 应当抛错而不是猜一个渲染")
    except ValueError:
        pass

    # --- 整条 SQL ---------------------------------------------------------
    cols = [("user_id", "bigint"), ("amount", "numeric"), ("d", "date"),
            ("paid_at", "timestamp without time zone"), ("ok", "boolean"),
            ("note", "text")]
    sql, labels = build_probe_sql("public", "payments", cols)
    want("pg 指标名", labels,
         ["count", "sum:user_id", "sum:amount", "min:d", "max:d",
          "min:paid_at", "max:paid_at", "true:ok"])
    want("pg SQL 逐字未变", sql,
         'SELECT COUNT(*), CAST(SUM(CAST("user_id" AS DECIMAL(38,4))) AS VARCHAR), '
         'CAST(SUM(CAST("amount" AS DECIMAL(38,4))) AS VARCHAR), '
         'TO_CHAR(MIN("d"), \'YYYY-MM-DD\'), TO_CHAR(MAX("d"), \'YYYY-MM-DD\'), '
         'TO_CHAR(MIN("paid_at"), \'YYYY-MM-DD"T"HH24:MI:SS\'), '
         'TO_CHAR(MAX("paid_at"), \'YYYY-MM-DD"T"HH24:MI:SS\'), '
         'SUM(CASE WHEN "ok" THEN 1 ELSE 0 END) FROM "public"."payments"')

    tcols = [("user_id", "bigint"), ("amount", "decimal(12,2)"), ("d", "date"),
             ("paid_at", "timestamp(6)"), ("ok", "boolean"),
             ("note", "varchar"), ("tags", "array(varchar)")]
    tsql, tlabels = build_probe_sql("app_analytics", "payments", tcols, TrinoDialect)
    want("trino 指标名与 pg 一致", tlabels, labels)
    want("trino SQL", tsql,
         'SELECT COUNT(*), CAST(SUM(CAST("user_id" AS DECIMAL(38,4))) AS VARCHAR), '
         'CAST(SUM(CAST("amount" AS DECIMAL(38,4))) AS VARCHAR), '
         'CAST(MIN("d") AS VARCHAR), CAST(MAX("d") AS VARCHAR), '
         'date_format(MIN("paid_at"), \'%Y-%m-%dT%H:%i:%S\'), '
         'date_format(MAX("paid_at"), \'%Y-%m-%dT%H:%i:%S\'), '
         'SUM(CASE WHEN "ok" THEN 1 ELSE 0 END) FROM "app_analytics"."payments"')
    if "TO_CHAR" in tsql:
        bad += 1
        print("  FAIL trino SQL 里出现了 TO_CHAR（Trino 没有这个函数）")

    # --- 脱敏列 -----------------------------------------------------------
    # 两支**刻意不同**：pg 侧 DDM 会读到掩码值所以跳过，Trino 侧是列级排除、
    # 能查到就是原值所以照常对账。见 MASKED_COLUMNS 注释。
    upcols = [("user_id", "bigint"), ("birth_date", "date")]
    want("pg 跳过脱敏列", build_probe_sql("public", "user_profiles", upcols)[1],
         ["count", "sum:user_id"])
    tup = [("user_id", "bigint"), ("birth_date", "date")]
    want("trino 不跳过脱敏列",
         build_probe_sql("app_analytics", "user_profiles", tup, TrinoDialect)[1],
         ["count", "sum:user_id", "min:birth_date", "max:birth_date"])

    # --- 归一 / 对账 ------------------------------------------------------
    import decimal as _d
    want("normalize Decimal", normalize(_d.Decimal("1.5")), "1.5000")
    want("normalize None", normalize(None), None)
    want("normalize bool", normalize(True), True)
    want("normalize str 原样", normalize("2025-11-02"), "2025-11-02")

    one = {"tables": {"t": {"count": 1, "sum:a": None}}, "backend": "A"}
    two = {"tables": {"t": {"count": 1, "sum:a": "0.0000"}, "u": {"count": 9}},
           "backend": "B"}
    want("null ≠ 0.0000（空列求和的判据）", len(compare(one, two, subset=True)), 1)
    want("subset 忽略 B 多出的表", len(compare({"tables": {}}, two, subset=True)), 0)
    want("非 subset 报 B 多出的表", len(compare({"tables": {}}, two)), 2)

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("全部通过 ✅  方言分类 28 项 + 整条 SQL 4 项 + 脱敏 2 项 + 归一对账 7 项")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="数据一致性快照 / 对账")
    p.add_argument("--schema", help="目标 schema（不给则用后端默认：pg 是 public，"
                                    "Athena 是 Iceberg namespace）")
    p.add_argument("--out", help="快照输出路径（.json）")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"),
                   help="对比两份快照，不一致则 exit 1")
    p.add_argument("--subset", action="store_true",
                   help="只比 A 里有的表（B 多出的表不算差异）")
    p.add_argument("--selftest", action="store_true",
                   help="跨方言渲染自测（不连库）")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text())
        b = json.loads(Path(args.compare[1]).read_text())
        diffs = compare(a, b, subset=args.subset)
        if not diffs:
            print(f"一致 ✅  {a.get('table_count')} 张表，逐表 count/sum/min-max 全等")
            return 0
        print(f"发现 {len(diffs)} 处差异 ❌\n")
        for d in diffs:
            print("  " + d)
        return 1

    if not args.out:
        p.error("需要 --out 或 --compare")

    be = make_backend()
    try:
        schema = (args.schema or getattr(be, "default_schema", None)
                  or be.dialect.default_schema)
        if not schema:
            p.error(f"backend={be.name} 没有默认 schema，请用 --schema 指定")
        print(f"[snapshot] backend={be.name} schema={schema}", file=sys.stderr)
        snap = take_snapshot(be, schema)
    finally:
        be.close()

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    total = sum(len(v) for v in snap["tables"].values())
    print(f"[snapshot] 写出 {dest}：{snap['table_count']} 张表，{total} 项指标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
