#!/usr/bin/env python3
"""把 11 张维度表的现有 CSV 转成 Parquet，走与事实表同一条 Redshift COPY 路径。

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
import re
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
# channel_daily_costs **也已移出**：它由 tables.py 的 _prep_channel_costs /
# build_channel_daily_costs 生成（P0-5）。留在这里的话，v1 那 910 行会覆盖生成器产的
# 2799 行网格——v1 的 installs 是 244,774 而全库只有 500 个用户（490 倍），
# CAC ¥2,900/人，凡是碰 CAC / ROI 的题全错。
DIMS = ["categories", "channels", "event_definitions",
        "user_segments", "campaigns", "coupons", "banners", "ab_tests",
        "ab_test_variants", "ad_campaigns", "ad_creatives"]


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


def _ts(v: str):
    """时间戳文本 → `np.datetime64[us]`，**保留亚秒**。

    2026-09-01 之前这里是 `np.datetime64(v[:19], "us")`。`[:19]` 只留到秒，
    把小数部分整段切掉了：`data/csv` 里这 11 张维表的时间戳带 5–6 位微秒
    （`2025-11-14 05:46:22.474586`），Iceberg arm 从 CSV 装载保留它，Redshift arm
    从这里产出的 Parquet 装载则拿到 `…05:46:22`。**两个 arm 的同一列值不同，
    而且都不报错**——`WHERE created_at = '…22.474586'` 在 Athena 上命中、在 Redshift 上 0 行；
    `ab_tests.start_date` / `end_date` 这种要做 `BETWEEN` 的窗口边界会整体偏移最多 1 秒。
    架构对比测试要求三个 arm 吃同一份数据，这种偏移必须消掉。判据是
    `verify_portability.py --compare` 的 `csv_pq_equal`。

    截断改成**只截超过 6 位的小数**（Redshift 与 Iceberg 都只到微秒，7 位以上无处安放），
    并顺手剥掉时区后缀——`np.datetime64` 遇到 `+08:00` 会抛，而这批 CSV 里没有时区，
    真出现了也说明上游变了形态，宁可在这里显式处理掉。
    """
    s = str(v).strip().replace("T", " ")
    s = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", s)
    m = re.match(r"^(.*\.\d{6})\d+$", s)
    if m:
        s = m.group(1)
    return np.datetime64(s, "us")


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
            fields[name] = pa.array([None if v in ("", None) else _ts(v)
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
