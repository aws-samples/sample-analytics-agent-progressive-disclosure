#!/usr/bin/env python3
"""数据一致性快照 —— 迁移前后自动对账，替代人工数据核查。

## 为什么需要它（与 eval harness 的分工）

`eval/run_eval.py` 驱动真实 agent 答 21 道题，判分带容差、只覆盖被出题的那些表。
它抓的是「口径错、选错表」这类语义问题，抓不住整体性偏移：某张没被出题覆盖的表
`COPY` 时静默少了 3% 的行，或者某个 DECIMAL 列在方言转换中丢了精度，eval 全绿。

本脚本补的正是这一层：对**每张表**取 `count(*)`、数值列 `SUM`、时间列 `MIN/MAX`、
布尔列真值计数，迁移前后各跑一次做精确 diff。一次写好永久复用，不需要人工看数。

## 用法

    # 迁移前基线（本地 Postgres）
    python3 scripts/consistency/snapshot.py --out eval/baseline/consistency.postgres.json

    # 迁移后（Redshift，Phase 2 接入 Data API backend 后）
    SNAPSHOT_BACKEND=redshift-data \\
      python3 scripts/consistency/snapshot.py --out eval/baseline/consistency.redshift.json

    # 对账：不一致则打印明细并 exit 1（可直接挂 CI / 迁移验收门）
    python3 scripts/consistency/snapshot.py --compare \\
      eval/baseline/consistency.postgres.json eval/baseline/consistency.redshift.json

## 可移植性要点（Postgres ⟷ Redshift）

1. 只用 `information_schema`，两边都有；不碰 `pg_catalog` / `svv_*`。
2. **数值列 SUM 前先 CAST 成 DECIMAL(38,4)**。Redshift 是 MPP，`double precision`
   的求和在多 slice 并行下累加顺序不确定，浮点尾数会与单机 Postgres 不一致，
   直接比会出现大量假阳性。定点化后求和结果与累加顺序无关，才能做精确比对。
3. 生成的 SQL 是标准聚合，不含任何一方独有的函数；换后端只换执行器。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
# 数值型：SUM（定点化后）
NUMERIC_TYPES = {
    "smallint", "integer", "bigint",
    "decimal", "numeric", "real", "double precision",
}
# 时间型：MIN / MAX
TEMPORAL_TYPES = {
    "date", "timestamp without time zone", "timestamp with time zone",
    "time without time zone", "time with time zone",
}
# 布尔型：真值计数
BOOLEAN_TYPES = {"boolean"}

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
# 列集合从 data_classification.yaml 派生，避免治理 SQL、reconcile 与一致性工具各存一份名单。
sys.path.insert(0, str(ROOT / "scripts" / "governance"))
from classification import expected_governance, load_inventory  # noqa: E402

MASKED_COLUMNS = set(expected_governance(load_inventory())[0])


# ---------------------------------------------------------------- backends

class PostgresBackend:
    """本地 Postgres / Aurora。迁移前基线走这条。"""

    name = "postgres"

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


BACKENDS = {
    PostgresBackend.name: PostgresBackend,
    RedshiftDataApiBackend.name: RedshiftDataApiBackend,
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


def build_probe_sql(schema: str, table: str,
                    columns: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """为一张表生成一条聚合 SQL，返回 (sql, 指标名列表)。

    指标名形如 'count'、'sum:actual_amount'、'min:paid_at'、'true:is_active'。
    """
    selects = ["COUNT(*)"]
    labels = ["count"]

    for name, dtype in columns:
        if (table, name) in MASKED_COLUMNS:
            continue                      # 见 MASKED_COLUMNS 注释：脱敏列不参与对账
        q = f'"{name}"'
        if dtype in NUMERIC_TYPES:
            # 见模块 docstring 第 2 点：先定点化再求和，规避 MPP 浮点累加顺序差异。
            # 再 CAST 成 VARCHAR：DECIMAL 经不同驱动回来可能是 Decimal 也可能是 str，
            # 由 SQL 统一渲染成保留标度的文本（'123.4500'），客户端就不必猜。
            selects.append(f"CAST(SUM(CAST({q} AS {DECIMAL_CAST})) AS VARCHAR)")
            labels.append(f"sum:{name}")
        elif dtype in TEMPORAL_TYPES:
            # 时间列**必须由 SQL 渲染成规范文本**：psycopg 回 datetime 对象、
            # Redshift Data API 回 '2025-10-26 00:02:22.0' 这种带空格和 .0 的串，
            # 两边直接比会全表假失败。TO_CHAR 的格式串两个引擎都支持。
            fmt = "YYYY-MM-DD" if dtype == "date" else 'YYYY-MM-DD"T"HH24:MI:SS'
            selects.append(f"TO_CHAR(MIN({q}), '{fmt}')")
            labels.append(f"min:{name}")
            selects.append(f"TO_CHAR(MAX({q}), '{fmt}')")
            labels.append(f"max:{name}")
        elif dtype in BOOLEAN_TYPES:
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
        sql, labels = build_probe_sql(schema, t, cols)
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
            _, sep, column = k.partition(":")
            if sep and (t, column) in MASKED_COLUMNS:
                continue              # 一侧可能是旧的原文基线，另一侧按 DDM 设计不采集
            va, vb = ma.get(k, "<无>"), mb.get(k, "<无>")
            if va != vb:
                diffs.append(f"[不一致] {t}.{k}：A={va}  B={vb}")
    return diffs


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="数据一致性快照 / 对账")
    p.add_argument("--schema", default="public", help="目标 schema（默认 public）")
    p.add_argument("--out", help="快照输出路径（.json）")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"),
                   help="对比两份快照，不一致则 exit 1")
    p.add_argument("--subset", action="store_true",
                   help="只比 A 里有的表（B 多出的表不算差异）")
    args = p.parse_args()

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
        print(f"[snapshot] backend={be.name} schema={args.schema}", file=sys.stderr)
        snap = take_snapshot(be, args.schema)
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
