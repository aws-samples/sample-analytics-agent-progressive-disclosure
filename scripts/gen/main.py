#!/usr/bin/env python3
"""数据生成编排器：分块生成 24 张表 → Parquet/CSV 分片 + 预期值清单。

`budget.BASE` 的 35 张表里，24 张走这里的 builder（`tables.LOAD_ORDER`），
另外 11 张是直通维表（`dims_to_parquet.DIMS`），由 `load_dim_ids()` 读现有 CSV。
这个划分由 `check_table_coverage()` 断言。

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

**没有非空值的列给 `null`，不给 `"0.0000"`**：SQL 的 `SUM` 在这种列上返回 NULL。
理由和键集合按 DDL（而不是按观测值）生成的理由是同一条，写在 `Expected.render` 里。
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
import dims_to_parquet                     # noqa: E402  只为拿 DIMS 清单做交叉断言
import tables as T                         # noqa: E402

ROOT = HERE.parent.parent
CSV_DIR = ROOT / "data" / "csv"

SCALE_4 = 10 ** 4

# 与 snapshot.py 的类型分类保持一致（那边读 information_schema，这边读 DDL）
NUMERIC = {"INT", "DECIMAL"}
TEMPORAL = {"TIMESTAMP", "DATE"}
BOOLEAN = {"BOOL"}

# ---------------------------------------------------------------- 行数对账
#
# `prepare_globals()` 会**故意**改写 ctx.rows 里这几张表的行数：它们的行数由跨表
# 约束推出来（唯一对去重后剩多少、多少订单可支付、归因覆盖多少用户），不是
# budget 能直接算的。所以 budget 的值只是量级参考，不是硬目标。
#
# 但「故意回写」和「builder 少给了行」在代码里长得一模一样，都是
# `实际 != budget`。所以这里必须把前者显式列出来并给出容差上限，剩下的
# 一律按后者处理、直接失败。容差是量出来的，不是拍的：
#   scale=1 / scale=20 实测偏差 product_tags -3.3% / +0.6%，payments -6.8% / -7.5%，
#   user_attributions 两个 scale 都是 0。上限留到实测的三四倍。
ROWS_WRITEBACK = {
    "product_tags":      0.35,   # 与 tables._prep_product_tags 里那条断言同口径
    "payments":          0.20,
    "user_attributions": 0.10,
}


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

    # user_coupons 需要每张券的「每人限领」额度：一个 (user, coupon) 上的持有张数
    # 不得超过它（P1-11）。v1 的 scripts/generators/marketing_domain.py 是逐行拒绝采样
    # 实现这条的，v2 向量化重写时丢了——和 device_brand/model 配对丢失是同一类回归。
    # 150 张券的额度取值 {1,2,3,5}，合计 412，这个和就是「一个用户最多能持有多少张券」。
    lim: dict[int, int] = {}
    with (CSV_DIR / "coupons.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            v = int(r["per_user_limit"] or 1)
            if v < 1:
                raise ValueError(f"coupon {r['coupon_id']} 的 per_user_limit={v} 不是正数")
            lim[int(r["coupon_id"])] = v
    out["_coupon_limits"] = lim

    # ab_test_assignments 需要「每个 test 有哪些 variant」，否则 variant 会跨 test 乱指
    vb: dict[int, list[int]] = {}
    with (CSV_DIR / "ab_test_variants.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            vb.setdefault(int(r["test_id"]), []).append(int(r["variant_id"]))
    missing = [t for t in out["ab_tests"].tolist() if t not in vb]
    if missing:
        raise ValueError(f"这些 ab_test 没有 variant，无法分配：{missing}")
    out["_variants_by_test"] = vb

    # channel_daily_costs 的生成需要两件维表事实：
    #  1. 哪些渠道有投放花费——只有 paid / kol 有。organic / referral / direct
    #     不该出现在成本表里（它们的 CAC 就该是 NULL，卡片里讨论的正是这个口径）。
    #     现有 CSV 恰好是 9 个渠道，就是 paid+kol 的那 9 个，这一点是对的。
    #  2. 每个渠道有哪些 ad_campaign / creative——成本行要挂到本渠道的活动上，
    #     跨渠道乱指会让「按活动看花费」和「按渠道看花费」两条口径互相矛盾。
    # 顺带读出每个渠道的 platform——`user_attributions.tracking_params` 里的 utm_source 取它。
    #     原来那一列是 F.const(n, {"utm_source": "douyin"})：14 个渠道的行全写 douyin，
    #     包括 App Store / 直接访问。它不是 NULL 所以没有任何判据会拦（JSONB 列不在
    #     verify_literals 的普查面里，见 docs/test-plan.md），按它统计渠道会得到
    #     「100% 来自抖音」，而同一行的 channel_id 就在旁边摆着。
    ch_type: dict[int, str] = {}
    ch_platform: dict[int, str] = {}
    with (CSV_DIR / "channels.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ch_type[int(r["channel_id"])] = r["channel_type"]
            ch_platform[int(r["channel_id"])] = r["platform"]
    out["_channel_types"] = ch_type
    out["_channel_platforms"] = ch_platform
    PAID = ("paid", "kol")
    out["_paid_channels"] = np.array(
        sorted(c for c, t in ch_type.items() if t in PAID), dtype=np.int64)

    #  3. 每个 ad_campaign 的投放起止**相对 DATA_START 的天偏移**——成本行只挂在
    #     活动档期内的日子上。不这么做的话「活动 X 结束后还在花钱」大量存在，而
    #     ad_campaigns.start_date / end_date 就在同一个库里摆着，一 join 就露。
    #     注意这 50 行的档期本身铺到 2026-10-01（v1 就这样，透传表重灌不会改），
    #     所以 2799 个渠道-日格子里只有约 46% 能挂到活动，其余 ad_campaign_id 为 NULL
    #     ——语义是「渠道级投放，没归到具体活动」。
    day0 = np.datetime64(budget.DATA_START, "D")
    camps: dict[int, list[int]] = {}
    spans: dict[int, tuple[int, int]] = {}
    with (CSV_DIR / "ad_campaigns.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            acid, cid = int(r["ad_campaign_id"]), int(r["channel_id"])
            camps.setdefault(cid, []).append(acid)
            spans[acid] = tuple(
                int((np.datetime64(r[k], "D") - day0) / np.timedelta64(1, "D"))
                for k in ("start_date", "end_date"))
            if spans[acid][0] > spans[acid][1]:
                raise ValueError(f"ad_campaign {acid} 的 start_date 晚于 end_date")
    nocamp = [c for c in out["_paid_channels"].tolist() if c not in camps]
    if nocamp:
        raise ValueError(f"这些投放渠道没有 ad_campaign，成本行无处可挂：{nocamp}")
    out["_campaigns_by_channel"] = camps
    out["_campaign_spans"] = spans

    crea: dict[int, list[int]] = {}
    with (CSV_DIR / "ad_creatives.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            crea.setdefault(int(r["ad_campaign_id"]), []).append(int(r["creative_id"]))
    out["_creatives_by_campaign"] = crea
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
                s, k = a["sum"].get(name, (0, 0))
                a["sum"][name] = (s + _isum(arr), k + _nonnull(arr))
            elif ct in TEMPORAL:
                lo, hi = _minmax(arr)
                if lo is not None:
                    a["min"][name] = lo if name not in a["min"] else min(a["min"][name], lo)
                    a["max"][name] = hi if name not in a["max"] else max(a["max"][name], hi)
            elif ct in BOOLEAN:
                a["true"][name] = a["true"].get(name, 0) + int(_bsum(arr))

    def render(self, backend: str) -> dict:
        """键集合按 **DDL** 生成，不按"这批数据里恰好有值的列"。

        因为对面（`scripts/consistency/snapshot.py`）的 SQL 是按 information_schema
        逐列渲染的：一列全是 NULL 时它照样有 `sum:` / `min:` / `max:` 键，值是
        `null`（SQL 的 `SUM` 在没有非空值时返回 NULL，不是 0）。这边若按观测值出键，
        `channel_daily_costs.creative_id` 那种整列 NULL 的列会变成「清单写 "0.0000"、
        库里是 null」和「清单没这个键、库里有」两种假失败——同一件事换两个方向各错一次。
        """
        out = {}
        for table, a in self.acc.items():
            m: dict = {"count": a["count"]}
            for name, ct in self.coltypes[table]:
                if ct in NUMERIC or ct == "DECIMAL":
                    s, k = a["sum"].get(name, (0, 0))
                    m[f"sum:{name}"] = None if k == 0 else f"{s / SCALE_4:.4f}"
                elif ct in TEMPORAL:
                    m[f"min:{name}"] = a["min"].get(name)
                    m[f"max:{name}"] = a["max"].get(name)
                elif ct in BOOLEAN:
                    # 布尔列刻意不给 null：`SUM(CASE WHEN c THEN 1 ELSE 0 END)` 在
                    # 非空表上恒有值（NULL 走 ELSE 记 0），整列 NULL 时 SQL 也返回 0。
                    m[f"true:{name}"] = a["true"].get(name, 0)
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


def _nonnull(arr) -> int:
    """非空值个数。分支要和 `_isum` 里"哪些值参与求和"逐条对上。"""
    a = np.asarray(arr)
    if a.dtype == object:
        return sum(1 for x in a.tolist() if x is not None)
    if np.issubdtype(a.dtype, np.floating):
        return int(np.isfinite(a).sum())          # 对上 _isum 的 a[np.isfinite(a)]
    return int(a.size)


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


# ---------------------------------------------------------------- 对账

def check_table_coverage() -> None:
    """断言 budget.BASE 的 35 张表 = 24 张 builder 表 + 11 张直通维表，无重无漏。

    这条比任何行数断言都靠前：少一张表只是 `_manifest.json` 里少一个键，
    生成、装载、行数对账全都不会报错，最后表现为湖里某张表是空的。
    三个文件各自声明了一份清单（budget.BASE / tables.LOAD_ORDER /
    dims_to_parquet.DIMS），让它们互相咬住，改任何一处漏改另一处就会在这里停。
    """
    base, built = set(budget.BASE), set(T.LOAD_ORDER)
    dims = set(dims_to_parquet.DIMS)
    if built & dims:
        raise ValueError(f"表既在 LOAD_ORDER 又在 DIMS：{sorted(built & dims)}"
                         "——同一张表不能有两个来源")
    if built | dims != base:
        missing, extra = base - (built | dims), (built | dims) - base
        raise ValueError(
            f"表清单和 budget.BASE 不一致：budget 有而两边都没有 {sorted(missing)}；"
            f"两边有而 budget 没有 {sorted(extra)}")
    if set(T.BUILDERS) != built:
        raise ValueError(f"BUILDERS 与 LOAD_ORDER 不一致："
                         f"{sorted(set(T.BUILDERS) ^ built)}")
    # 透传表**必须**声明 FIXED。声明 SUB 却没有 builder 的表会得到一个谁都不兑现的
    # 目标行数：budget 说 4133 行、库里 200 行，而缩放层、生成层、装载层没有任何一处
    # 会因此报错（D-02 就是这么漏过去的，直到有人问"为什么 180 万条明细只摊在
    # 200 个 SKU 上"）。要让某张透传表真的随规模长，正确做法是给它写 builder
    # 并移出 DIMS，而不是留一句 SUB 的声明。
    notfixed = sorted(t for t in dims if budget.BASE[t][1] != budget.FIXED)
    if notfixed:
        raise ValueError(
            f"这些透传表声明了非 FIXED 缩放，但没有任何代码兑现它：{notfixed}"
            f"——要么改成 FIXED，要么给它写 builder 并从 dims_to_parquet.DIMS 移出")


def reconcile_budget(ctx, scale: float) -> None:
    """比对 `prepare_globals()` 之后的 ctx.rows 与 budget 的声明值。

    在生成**之前**跑：这一步不对，后面几十分钟的生成就是白跑。

    这里**重新算一遍** budget.table_rows()，不能复用 main() 里那份：`Ctx` 直接
    持有调用方传进来的 dict，`prepare_globals()` 的 `ctx.rows[t] = ...` 会把
    main() 手里那份一起改掉，拿它当基准比对永远相等——这条断言会静默变成空转。
    """
    rows = budget.table_rows(scale)
    bad = []
    for t in T.LOAD_ORDER:
        actual, want = ctx.n(t), rows[t]
        if actual == want:
            continue
        if t not in ROWS_WRITEBACK:
            bad.append(f"  {t:<22} ctx.rows={actual:,} 但 budget={want:,}"
                       f"——该表不在 ROWS_WRITEBACK 里，不该有偏差")
            continue
        dev = abs(actual - want) / want if want else 1.0
        tol = ROWS_WRITEBACK[t]
        flag = "超容差" if dev > tol else "在容差内"
        line = (f"  {t:<22} {actual:>12,} 行（budget {want:,}，"
                f"偏差 {(actual-want)/want*100:+.2f}%，上限 ±{tol*100:.0f}%）{flag}")
        if dev > tol:
            bad.append(line)
        else:
            print(line, flush=True)
    if bad:
        raise ValueError("行数对账失败：\n" + "\n".join(bad))


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
    check_table_coverage()
    print("预生成全局状态（唯一对主键 / orders 状态与金额 / 归因覆盖）…", flush=True)
    T.prepare_globals(ctx)
    print(f"  完成 {time.time()-t0:.1f}s", flush=True)
    print("行数对账（ctx.rows vs budget）…", flush=True)
    reconcile_budget(ctx, scale)
    print("  通过", flush=True)

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
            if got != n:
                # 原先这里是 `n = got` 静默钳位。ctx.rows 的三处故意回写已经在
                # reconcile_budget() 里对过账，走到这一步 n_total 就是**真值**，
                # builder 再少给行只可能是 bug。静默钳位的后果是湖里少几行、
                # 而 _manifest.json 里记的是钳位后的数，任何下游对账都对得上。
                raise ValueError(
                    f"{table} 第 {len(shards)} 片要 {n:,} 行，builder 只给了 {got:,} 行"
                    f"（已生成 {done:,}/{n_total:,}）——builder 与 ctx.n() 不一致，"
                    f"要么修 builder，要么在 prepare_globals 里回写 ctx.rows 并登记进 "
                    f"main.ROWS_WRITEBACK")
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
        if done != n_total:
            raise ValueError(f"{table} 写出 {done:,} 行，但 ctx.n() 声明 {n_total:,} 行")
        manifest[table] = {"rows": done, "shards": shards}
        print(f"  {table:<24} {done:>12,} 行  {time.time()-t1:>6.1f}s", flush=True)

    if not a.only and set(manifest) != set(T.LOAD_ORDER):
        raise ValueError(f"清单没盖住全部 builder 表，缺 "
                         f"{sorted(set(T.LOAD_ORDER) - set(manifest))}")

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
