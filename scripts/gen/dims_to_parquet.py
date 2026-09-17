#!/usr/bin/env python3
"""把 14 张维度表的现有 CSV 转成 Parquet，走与事实表同一条 Redshift COPY 路径。

## 为什么需要转换

维度表本轮不重新生成（量级 2.4 万行，且 eval 金标依赖现有值），但直接 COPY CSV 到
Redshift 有两个障碍：

1. **Postgres 数组字面量不是合法 JSON**。`{数码,美妆}` 这种值在 Redshift 侧对应
   SUPER 列，SUPER 只吃 JSON，`{a,b}` 会被拒。必须转成 `["数码","美妆"]`。
2. **列序**。Redshift 从列式格式 COPY 是按位置映射的，转 Parquet 时统一按 DDL 排序，
   与事实表的守卫逻辑一致。

## 用法

    python3 scripts/gen/dims_to_parquet.py --out <目录> [--s3 s3://.../raw/]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arrow_types  # noqa: E402
import ddl  # noqa: E402

ROOT = HERE.parent.parent
CSV_DIR = ROOT / "data" / "csv"

# products / product_tags **不在这里**：它们已由 scripts/gen 生成（tables.py 的
# _prep_products / _prep_product_tags，见 D-02）。留在这个清单里会让 data/csv 的 200 行
# v1 商品覆盖生成器产出的 4133 行——同一张表两个来源，谁最后写谁赢，还取决于跑的顺序。
DIMS = ["categories", "channels", "event_definitions",
        "user_segments", "campaigns", "coupons", "banners", "ab_tests",
        "ab_test_variants", "ad_campaigns", "ad_creatives", "channel_daily_costs"]


def parse_pg_array(s: str):
    """Postgres 数组字面量 `{a,"b,c",NULL}` → Python list。

    手写而非用库：只需覆盖本项目 CSV 里出现的形态（一维、可含引号转义、可含 NULL）。
    """
    s = s.strip()
    if not s or s in ("{}", "NULL"):
        return []
    if not (s.startswith("{") and s.endswith("}")):
        return [s]
    body, out, cur, in_q, esc = s[1:-1], [], [], False, False
    for ch in body:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [None if v == "NULL" else v for v in out if v != "" or in_q]


def _date(s: str):
    import datetime
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def convert(table: str, cols: list[tuple[str, str, str]], out_dir: Path) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = CSV_DIR / f"{table}.csv"
    with src.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0

    fields = {}
    for name, _raw, ctype in cols:                # 按 DDL 顺序
        raw = [r.get(name, "") for r in rows]
        if ctype == "INT":
            fields[name] = pa.array([None if v in ("", None) else int(float(v))
                                     for v in raw], type=pa.int64())
        elif ctype == "DECIMAL":
            fields[name] = pa.array([None if v in ("", None) else float(v) for v in raw],
                                    type=pa.float64())
        elif ctype == "BOOL":
            fields[name] = pa.array([None if v in ("", None)
                                     else str(v).lower() in ("t", "true", "1", "yes")
                                     for v in raw], type=pa.bool_())
        elif ctype == "TIMESTAMP":
            fields[name] = pa.array([None if v in ("", None) else np.datetime64(v[:19], "us")
                                     for v in raw], type=pa.timestamp("us"))
        elif ctype == "DATE":
            # pa.date32() 不接受 np.datetime64[D]，要给 Python datetime.date
            fields[name] = pa.array([None if v in ("", None) else _date(v[:10])
                                     for v in raw], type=pa.date32())
        elif ctype == "ARRAY":
            # Postgres `{a,b}` → JSON 文本，Redshift SUPER 才吃得下
            fields[name] = pa.array([None if v in ("", None)
                                     else json.dumps(parse_pg_array(v), ensure_ascii=False)
                                     for v in raw], type=pa.string())
        elif ctype == "JSON":
            def _j(v):
                if v in ("", None):
                    return None
                try:
                    return json.dumps(json.loads(v), ensure_ascii=False)
                except Exception:
                    return json.dumps({"raw": v}, ensure_ascii=False)
            fields[name] = pa.array([_j(v) for v in raw], type=pa.string())
        else:
            fields[name] = pa.array([None if v == "" else v for v in raw],
                                    type=pa.string())

    # 按目标 schema 逐列 cast：Redshift COPY 要求 Parquet 物理类型与列类型兼容
    # （int64 灌不进 INTEGER，double 灌不进 DECIMAL(p,s)）。见 arrow_types 模块说明。
    schema = arrow_types.schema_for(cols)
    arrays = []
    for field in schema:
        arr = fields[field.name]
        if arr.type != field.type:
            arr = arr.cast(field.type, safe=False)
        arrays.append(arr)
    tdir = out_dir / table
    tdir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema),
                   tdir / f"{table}-0000.parquet", compression="snappy")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--s3")
    a = ap.parse_args()

    coltypes = ddl.parse_all_raw()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for t in DIMS:
        if t not in coltypes:
            print(f"  跳过 {t}（DDL 里没有）")
            continue
        n = convert(t, coltypes[t], out)
        total += n
        print(f"  {t:<24} {n:>8,} 行")
    print(f"合计 {total:,} 行 → {out}")

    if a.s3:
        import subprocess
        subprocess.run(["aws", "s3", "sync", str(out), a.s3.rstrip("/") + "/",
                        "--only-show-errors"], check=True)
        print(f"已同步 {a.s3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
