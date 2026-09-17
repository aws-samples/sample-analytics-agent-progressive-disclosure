#!/usr/bin/env python3
"""数据生成编排器：分块生成 21 张事实表 → Parquet/CSV 分片 + 预期值清单。

## 用法

    # 本地小样（scale=1，约 19 万行，几 MB），出 CSV 方便灌本地 Postgres 验证
    python3 scripts/gen/main.py --scale 1 --format csv --out /tmp/gen1

    # 云上正式版（8000 万行），出 Parquet 分片
    python3 scripts/gen/main.py --target-rows 80000000 --format parquet --out /data/gen

    # 直接上传 S3（EC2 上跑，走实例角色）
    python3 scripts/gen/main.py --target-rows 80000000 --format parquet \\
        --out /data/gen --s3 s3://analytics-agent-data-.../raw/

## 预期值清单（`_expected.json`）

生成时顺手算出每表的 count / 数值列 sum / 时间列 min-max / 布尔列真值数，
格式与 `scripts/consistency/snapshot.py` **完全一致**，落库后可直接：

    python3 scripts/consistency/snapshot.py --compare _expected.json <实际快照>.json

这替代了「迁移前查 Postgres 取基线」——8000 万行本机装不下，而且这样验的是
「生成 → 序列化 → 传输 → COPY」全链路无损，比两个库对比更强。

## 求和必须整数化

snapshot.py 的 SQL 是 `SUM(CAST(col AS DECIMAL(38,4)))`，Postgres/Redshift 的 DECIMAL
求和是精确的。这里若用 float64 累加，1500 万行会攒出尾数误差，对账时全是假阳性。
所以数值列统一放大 10^4 取整后用 int64 累加（上限 9.2e18，本库量级安全），最后再
还原成 4 位小数字符串。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))       # 让 genlib 可导入

import arrow_types                         # noqa: E402
import budget                              # noqa: E402
import ddl                                 # noqa: E402
import tables as T                         # noqa: E402

ROOT = HERE.parent.parent
CSV_DIR = ROOT / "data" / "csv"

SCALE_4 = 10 ** 4

# 与 snapshot.py 的类型分类保持一致（那边读 information_schema，这边读 DDL）
NUMERIC = {"INT", "DECIMAL"}
TEMPORAL = {"TIMESTAMP", "DATE"}
BOOLEAN = {"BOOL"}


# ---------------------------------------------------------------- 维度表 id

def load_dim_ids() -> dict[str, np.ndarray]:
    """从现有 CSV 读维度表的真实 id（它们可能不是 1..N 连续）。

    这里的维度表**不重新生成**：量级只有 2.4 万行（全库 8000 万里可忽略），
    现有 CSV 已经正确且被 eval 金标依赖。事实表引用它们的真实 id 即可。

    **products / product_tags 例外，已移出这个清单**：它们由 tables.py 的
    `_prep_products` / `_prep_product_tags` 生成，`ctx.dim_ids["products"]` 和
    `["_product_names"]` 在 `prepare_globals()` 的第一步被回写。原先从这里读 200 行
    v1 CSV，而 budget.py 声明它该有 4133 行（SUB 缩放），差 20 倍——D-02。
    同一张表不能有两个来源，所以这里必须删干净，不能"两边都留着以防万一"。
    """
    want = {
        "channels": "channel_id", "coupons": "coupon_id",
        "campaigns": "campaign_id", "ab_tests": "test_id",
        "ad_campaigns": "ad_campaign_id", "ad_creatives": "creative_id",
        "user_segments": "segment_id", "event_definitions": "event_name",
    }
    out: dict[str, np.ndarray] = {}
    for table, key in want.items():
        p = CSV_DIR / f"{table}.csv"
        if not p.exists():
            raise FileNotFoundError(f"缺维度表 CSV：{p}")
        with p.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        vals = [r[key] for r in rows]
        if key == "event_name":
            out[table] = np.array(vals, dtype=object)
        else:
            out[table] = np.array([int(v) for v in vals], dtype=np.int64)

    # order_items 的反范式列要带**真实商品名**：真实订单明细表在下单时把商品名冗余下来，
    # agent 会很自然地直接用 oi.product_name 而不去 join products。填占位符会让
    # 「GMV Top10 商品」这类题的答案与 join products 的金标对不上（eval 踩过这一刀）。
    # 这两项现在由 _prep_products 回写，见上面的 docstring。这里放两个空占位是为了让
    # 「键存在但为空」在 build_order_items 里直接 IndexError，而不是安静取到旧数据。
    out["products"] = np.array([], dtype=np.int64)
    out["_product_names"] = np.array([], dtype=object)

    # ab_test_assignments 需要「每个 test 有哪些 variant」，否则 variant 会跨 test 乱指
    vb: dict[int, list[int]] = {}
    with (CSV_DIR / "ab_test_variants.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            vb.setdefault(int(r["test_id"]), []).append(int(r["variant_id"]))
    missing = [t for t in out["ab_tests"].tolist() if t not in vb]
    if missing:
        raise ValueError(f"这些 ab_test 没有 variant，无法分配：{missing}")
    out["_variants_by_test"] = vb
    return out


# ---------------------------------------------------------------- 预期值累加

class Expected:
    """按表累加 count / sum / min / max / true，产出与 snapshot.py 同格式的清单。"""

    def __init__(self, coltypes: dict[str, list[tuple[str, str]]]):
        self.coltypes = coltypes
        self.acc: dict[str, dict] = {}

    def add(self, table: str, cols: dict[str, np.ndarray], n: int) -> None:
        a = self.acc.setdefault(table, {"count": 0, "sum": {}, "min": {}, "max": {},
                                        "true": {}})
        a["count"] += n
        types = dict(self.coltypes[table])
        for name, arr in cols.items():
            ct = types.get(name)
            if ct in NUMERIC or ct == "DECIMAL":
                a["sum"][name] = a["sum"].get(name, 0) + _isum(arr)
            elif ct in TEMPORAL:
                lo, hi = _minmax(arr)
                if lo is not None:
                    a["min"][name] = lo if name not in a["min"] else min(a["min"][name], lo)
                    a["max"][name] = hi if name not in a["max"] else max(a["max"][name], hi)
            elif ct in BOOLEAN:
                a["true"][name] = a["true"].get(name, 0) + int(_bsum(arr))

    def render(self, backend: str) -> dict:
        out = {}
        for table, a in self.acc.items():
            m: dict = {"count": a["count"]}
            for name, s in a["sum"].items():
                m[f"sum:{name}"] = f"{s / SCALE_4:.4f}"
            for name, v in a["min"].items():
                m[f"min:{name}"] = v
                m[f"max:{name}"] = a["max"][name]
            for name, v in a["true"].items():
                m[f"true:{name}"] = v
            out[table] = m
        return {"schema": "public", "backend": backend,
                "table_count": len(out), "tables": out}


def _isum(arr) -> int:
    """整数化求和：放大 10^4 取整后 int64 累加（见模块 docstring）。"""
    a = np.asarray(arr)
    if a.dtype == object:
        vals = [x for x in a.tolist() if x is not None]
        if not vals:
            return 0
        return int(sum(int(round(float(v) * SCALE_4)) for v in vals))
    if np.issubdtype(a.dtype, np.integer):
        return int(a.astype(np.int64).sum()) * SCALE_4
    if np.issubdtype(a.dtype, np.bool_):
        return int(a.sum()) * SCALE_4
    fin = a[np.isfinite(a)]
    return int(np.round(fin.astype(np.float64) * SCALE_4).astype(np.int64).sum())


def _minmax(arr):
    a = np.asarray(arr)
    if a.dtype == object:
        vals = [x for x in a.tolist() if x is not None]
        if not vals:
            return None, None
        iso = sorted(_iso(v) for v in vals)
        return iso[0], iso[-1]
    if a.size == 0:
        return None, None
    return _iso(a.min()), _iso(a.max())


def _iso(v) -> str:
    """与 Postgres/Redshift 的 isoformat 输出严格对齐。

    DATE 列必须给 '2025-10-26'（date.isoformat()），不能给 '2025-10-26T00:00:00'；
    TIMESTAMP 列必须给到秒。第一版这里的三元表达式恒为 "s"，会让所有 DATE 列对账失败。
    """
    if isinstance(v, np.datetime64):
        unit = "D" if np.datetime_data(v.dtype)[0] == "D" else "s"
        return np.datetime_as_string(v, unit=unit)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _bsum(arr) -> int:
    a = np.asarray(arr)
    if a.dtype == object:
        return sum(1 for x in a.tolist() if x is True)
    return int(a.sum())


# ---------------------------------------------------------------- 写出

def align_to_ddl(table: str, cols: dict[str, np.ndarray],
                 coltypes: dict[str, list[tuple[str, str]]]) -> dict[str, np.ndarray]:
    """把 builder 产出的列按 DDL 顺序重排，并校验列集合完全一致。

    **这不是洁癖，是正确性要求**：Redshift `COPY ... FORMAT AS PARQUET` 是
    「按数据文件里列出现的顺序插入目标表的列」（官方文档原文），**按位置映射、不按名字**，
    且列数必须相等。列序错了不会报错，会把数据静默灌进相邻的列——比崩掉难查得多。
    CSV 路径带 header 所以不受影响，但两条路径统一对齐更省心。
    """
    want = [c for c, _ in coltypes[table]]
    got = set(cols)
    missing, extra = [c for c in want if c not in got], [c for c in cols if c not in want]
    if missing or extra:
        raise ValueError(
            f"{table} 的列与 DDL 不符：缺 {missing}，多 {extra}。"
            f"（DDL 是表结构真源，builder 必须与之逐列对应）")
    return {c: cols[c] for c in want}


def write_parquet(path: Path, cols: dict[str, np.ndarray], schema) -> None:
    """写 Parquet 分片，**列类型严格按目标 schema**。

    两条硬约束（都踩过）：
    - Parquet 物理类型必须与 Redshift 列类型兼容，否则 COPY 报
      `incompatible Parquet schema for column ...`。int64 灌不进 INTEGER，
      double 灌不进 DECIMAL(12,2)。所以这里按 `arrow_types.schema_for()` 逐列 cast。
    - JSONB / TEXT[] 列序列化成 JSON 字符串（Redshift 侧是 SUPER）。CSV 路径相反，
      走 pgcsv 的 Postgres 数组字面量 `{a,b}`。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrays = []
    for field in schema:
        v = cols[field.name]
        a = np.asarray(v)
        if a.dtype.kind == "M":
            arr = pa.array(a.astype("datetime64[us]"))
        elif a.dtype == object:
            lst = a.tolist()
            probe = next((x for x in lst if x is not None), None)
            if isinstance(probe, (list, dict, tuple)):
                lst = [None if x is None else json.dumps(x, ensure_ascii=False)
                       for x in lst]
            arr = pa.array(lst)
        else:
            arr = pa.array(a)
        if arr.type != field.type:
            # safe=False：允许 double→decimal 的定标舍入、int64→int32 的窄化
            arr = arr.cast(field.type, safe=False)
        arrays.append(arr)
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), path,
                   compression="snappy")


def write_csv(path: Path, cols: dict[str, np.ndarray], header: bool) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from genlib import pgcsv

    if header:
        pgcsv.write_table(str(path), cols)
        return
    tmp = path.with_suffix(".part")
    pgcsv.write_table(str(tmp), cols)
    with tmp.open(encoding="utf-8") as src, path.open("a", encoding="utf-8") as dst:
        next(src)                      # 跳过 header
        for line in src:
            dst.write(line)
    tmp.unlink()


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="向量化造数")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--scale", type=float)
    g.add_argument("--target-rows", type=int)
    ap.add_argument("--out", required=True)
    ap.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    ap.add_argument("--shard-rows", type=int, default=budget.SHARD_ROWS)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--only", nargs="*", help="只生成这些表（调试用）")
    ap.add_argument("--s3", help="生成后同步到该 S3 前缀")
    a = ap.parse_args()

    scale = a.scale if a.scale else budget.solve_scale(
        a.target_rows or budget.DEFAULT_TARGET_ROWS)
    rows = budget.table_rows(scale)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    coltypes = ddl.parse_all()
    raw_types = ddl.parse_all_raw()
    start = np.datetime64(budget.DATA_START, "s")
    as_of_end = (np.datetime64(budget.AS_OF, "D").astype("datetime64[s]")
                 + np.timedelta64(86399, "s"))

    ctx = T.Ctx(seed=a.seed, rows=rows, start=start, days=budget.WINDOW_DAYS,
                as_of_end=as_of_end, dim_ids=load_dim_ids())

    print(f"scale={scale}  事实表目标 {sum(rows[t] for t in T.LOAD_ORDER):,} 行  "
          f"格式={a.format}  分片={a.shard_rows:,} 行/片")
    t0 = time.time()
    print("预生成全局状态（唯一对主键 / orders 状态与金额 / 归因覆盖）…", flush=True)
    T.prepare_globals(ctx)
    print(f"  完成 {time.time()-t0:.1f}s", flush=True)

    exp = Expected(coltypes)
    todo = a.only if a.only else T.LOAD_ORDER
    manifest = {}

    for table in todo:
        if table not in T.BUILDERS:
            print(f"  跳过 {table}（无 builder）")
            continue
        n_total = ctx.n(table)
        build = T.BUILDERS[table]
        tdir = out / table
        tdir.mkdir(exist_ok=True)
        pq_schema = (arrow_types.schema_for(raw_types[table])
                     if a.format == "parquet" else None)
        shards, done, t1 = [], 0, time.time()
        csv_path = out / f"{table}.csv"
        if a.format == "csv" and csv_path.exists():
            csv_path.unlink()

        while done < n_total:
            n = min(a.shard_rows, n_total - done)
            cols = align_to_ddl(table, build(ctx, done, n), coltypes)
            got = len(next(iter(cols.values())))
            if got != n:                    # builder 实际给的行数（唯一对表可能不足）
                n = got
            exp.add(table, cols, n)
            if a.format == "parquet":
                p = tdir / f"{table}-{len(shards):04d}.parquet"
                write_parquet(p, cols, pq_schema)
                shards.append(p.name)
            else:
                write_csv(csv_path, cols, header=(done == 0))
            done += n
            if n == 0:
                break
        manifest[table] = {"rows": done, "shards": shards}
        print(f"  {table:<24} {done:>12,} 行  {time.time()-t1:>6.1f}s", flush=True)

    (out / "_manifest.json").write_text(
        json.dumps({"scale": scale, "seed": a.seed, "as_of": budget.AS_OF,
                    "data_start": budget.DATA_START, "format": a.format,
                    "tables": manifest}, ensure_ascii=False, indent=2) + "\n")
    (out / "_expected.json").write_text(
        json.dumps(exp.render(f"generator(scale={scale})"), ensure_ascii=False,
                   indent=2, sort_keys=True) + "\n")

    total = sum(v["rows"] for v in manifest.values())
    print(f"\n合计 {total:,} 行，耗时 {time.time()-t0:.1f}s → {out}")
    print(f"预期值清单：{out/'_expected.json'}")

    if a.s3:
        print(f"\n同步到 {a.s3} …", flush=True)
        subprocess.run(["aws", "s3", "sync", str(out), a.s3.rstrip("/") + "/",
                        "--only-show-errors"], check=True)
        print("同步完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
