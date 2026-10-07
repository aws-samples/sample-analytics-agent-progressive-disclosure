#!/usr/bin/env python3
"""L6 维度分辨率：维度表规模有没有跟事实表脱节（D-02 的判据）。

## 这一组和 L7 的分工

L7 判**单行对不对**（这个品牌卖不卖这个品类、这个价格在不在带里），逐行判、通过线 0。
L6 判**整体够不够分**：一个类目里只有 1 个商品时，L7 每一行都合法，可是「类目 A 的
客单价高于类目 B」这句结论实际上是「商品 a 比商品 b 贵」。这种缺陷没有任何一行是错的，
所以逐行判据永远抓不到它，必须看分布。

## 通过线是怎么定的

任务书把 L6 的通过线留空，说「依赖 D-02 决定的目标规模」，并建议参照两个量：每类目
SKU 数的中位数下限、每 SKU 被下单次数的上限。规模现在定了（`budget.py` 的 SUB 类，
scale=427 → 4133 个 SKU），所以这里把线填上。**先按分析需要推导，再看实测值**，不是
反过来——阈值贴着当前数据定，就只会在有人改生成器时响，不会在数据退化时响：

  1. `每类目 SKU 数 min ≥ 4`
     类目内要能算中位数和四分位，至少要 4 个点；换个说法：剔掉该类目最大的那个单品，
     还得剩 3 个可比对象，否则「类目对比」就是「单品对比」。

  2. `每类目 SKU 数 中位数 ≥ 10`
     长尾分析要在类目内分出头部 / 腰部 / 尾部三段，每段至少 3 个 SKU 才谈得上分布，
     即 ≥ 9，取整到 10。任务书 D-02 记录的值是 1.2。

  3. `每品牌 SKU 数 min ≥ 4`
     与第 1 条同构：品牌 GMV 排名、品牌客单价和类目分析是同一种聚合，下限同为 4。

  4. `每 SKU 平均被下单次数 ≤ 1000`
     这一条的定位和 `semantics.py` 的 MAX_BAND_RATIO 一样，是**量级护栏，不是精确的
     真实性判据**。三个月窗口里一个 SKU 被下单上千次，说明它在数据里承担的是一整个
     品类的角色而不是一个商品——爆款识别和长尾分析的对象都退化了。1000 比真实电商单
     SKU 的季度订单量常见上限宽一个量级，只拦「SKU 退化成品类」这一形态。任务书 D-02
     记录的值是 9,021——**那是导师那套 v2 Redshift 库的数，不是本库的**，见下一节。
     **判的是均值不是最大值**，虽然任务书的措辞是「上限」：下单次数天生长尾（生成器
     用 `fk_skewed` 造热度差异，真实电商也是这样），最大值由最热的那一个 SKU 决定，
     换个种子就能差出几倍，做闸门只会随机误伤。均值等价于「order_items 行数 ÷ SKU 数」
     ——正是 D-02 里 9,021 那个数的定义，直接对上任务书要修的那件事。最大值和 p99
     仍然打印出来供人复核。

  5. `空类目数 = 0`
     空类目在任何聚合结果里都是**不出现**，不报错、看不出来。这是 D-02 里最隐蔽的一面。

  6. `类目内实际价格跨度 ≤ 声明带宽`
     任务书要求输出「各类目内价格区间的跨度倍数」。跨度本身没有绝对好坏（耳机 61× 是
     真实的），有意义的是它有没有冲出 `semantics.yaml` 声明的带。所以这一条判的是
     实际 max/min 与声明 hi/lo 的关系，判据仍然只有那一份配置。

## 预期结论：对现行库跑必然是红的

云上商品是 v1 的 200 行、项目已决定不重灌，所以 L6 对云上跑会红。同 `verify_literals.py`
和 `verify_semantics.py`，本文件**不接进 `scripts/test_all.sh`**：常驻的红灯只会把人
训练成无视红灯。绿灯在生成侧看（`--from-csv`），跑的是同一批 judge 函数。

## 一处与任务书不符：v3 的库整体都是 v1 规模

任务书 D-02 的说法是「事实表按 427 倍放大了，维度表还是 v1 原样」。实测本库不是这样：

    users 500 · orders 2,000 · order_items 4,225 · events 20,000 · products 200

事实表和维度表**一起**停在 v1 规模——427 倍放大只发生在导师看到的那套 v2 Redshift 库
（v3 的源码他没拿到）。这带来一个具体的读数陷阱，`run()` 里因此加了一条提示：

  · 「每 SKU 平均被下单次数」是个**比值**。分子分母一起小 427 倍，比值照样漂亮，在本库
    上只有 21 单，远低于通过线 1000，于是这条判据在云上是**绿**的。这个绿不代表分辨率
    健康，只代表比例没失衡——把它当健康证明，就正好错过了 D-02 要修的那件事。
  · 反过来，D-02 在 v3 里是**潜伏**的缺陷：病灶在生成器（`budget.py` 声明 products 走
    SUB 缩放，可 `tables.py` 整类没有 builder），谁灌一次全量数据它就当场显形。所以修
    在生成器层是对的层次，而不是「云上数看着还行就不用修」。

用法：

    python3 scripts/lakehouse/verify_resolution.py                 # 查云上现行库
    python3 scripts/lakehouse/verify_resolution.py --from-csv /tmp/gen1
    python3 scripts/lakehouse/verify_resolution.py --selftest      # 判据自测（无云依赖）
"""
from __future__ import annotations

import argparse
import collections
import csv
import statistics as st
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT / "scripts" / "gen"))

import semantics as SEM                          # noqa: E402  判据真源
from verify_semantics import load_products       # noqa: E402  两组共用一份取数

MARK = {"PASS": "\033[32m  ok  \033[0m", "FAIL": "\033[31m FAIL \033[0m"}
CSV_DIR = ROOT / "data" / "csv"

# ---- 通过线。每条的推导写在模块 docstring 里，改这里必须同改那里。
MIN_SKU_PER_CATEGORY = 4        # 类目内要能算四分位
MED_SKU_PER_CATEGORY = 10       # 头部/腰部/尾部各 ≥3
MIN_SKU_PER_BRAND = 4           # 与类目同构
MAX_ORDERS_PER_SKU = 1000       # 量级护栏：SKU 不该退化成品类

# 任务书 D-02 表里的数字，用来在报告里给出「该修成什么样」的对照。
# 写成常量而不是散在字符串里，是为了 --selftest 能拿它做反向用例：这些数字必须判红。
#
# **这是导师那套 v2 Redshift 库的观测值，不是本 v3 库的现状**——括号里印它是为了对照
# 任务书，不是断言本库如此。v3 的 Athena 库从来没灌过全量数据，整库都是 v1 规模
# （见下面 run() 里 order_items 那条提示），所以 order_items / orders_per_sku 这两项在
# v3 上对不上号是正常的，维度侧的 200 / 166 / 59 才两边一致。
BASELINE = {"products": 200, "categories": 166, "brands": 59,
            "order_items": 1_804_240, "orders_per_sku": 9021.2,
            "sku_per_category": 1.2}

# 报告里要列出的维度表。`products` / `product_tags` 现在由 scripts/gen 生成（D-02），
# 其余仍是 data/csv 直灌的 FIXED 表——两类都列，因为 L6 判的正是它们之间的比例关系。
DIM_TABLES = ["categories", "products", "product_tags", "channels", "coupons",
              "campaigns", "banners", "user_segments", "event_definitions",
              "ab_tests", "ab_test_variants", "ad_campaigns", "ad_creatives",
              "channel_daily_costs"]


# ---------------------------------------------------------------- 取数

def load_counts(src: Path | None, client=None) -> tuple[dict[str, int], dict[int, int]]:
    """(各维度表行数, product_id → 被下单次数)。

    CSV 模式下 products/product_tags 读生成目录、其余维度表读 `data/csv`——这正是灌库
    时的真实来源组合，不是图方便。混淆这两个来源会让 L6 报出一个现实中不存在的组合。
    """
    if src is not None:
        rows: dict[str, int] = {}
        for t in DIM_TABLES:
            p = src / f"{t}.csv"
            if not p.exists():
                p = CSV_DIR / f"{t}.csv"
            if not p.exists():
                continue
            with p.open(encoding="utf-8") as fh:
                rows[t] = sum(1 for _ in fh) - 1
        oi = src / "order_items.csv"
        if not oi.exists():
            raise FileNotFoundError(
                f"没有 {oi}。L6 要每 SKU 的下单次数，只生成 products 不够——"
                f"跑一次不带 --only 的 scripts/gen/main.py（scale=1 就够看分布）")
        with oi.open(encoding="utf-8") as fh:
            cnt = collections.Counter(int(r["product_id"]) for r in csv.DictReader(fh))
        rows["order_items"] = sum(cnt.values())
        return rows, dict(cnt)

    rows = {}
    for t in DIM_TABLES:
        try:
            rows[t] = int(client.execute(f"SELECT count(*) FROM {t}")["rows"][0][0])
        except Exception as exc:                       # noqa: BLE001
            print(f"  （{t} 取行数失败：{str(exc)[:80]}）")
    res = client.execute("SELECT product_id, count(*) FROM order_items GROUP BY 1")
    cnt = {int(r[0]): int(r[1]) for r in res["rows"]}
    rows["order_items"] = sum(cnt.values())
    return rows, cnt


# ---------------------------------------------------------------- 判据

def dist(v: list[int]) -> dict:
    """分布摘要。任务书要 min/avg/max，这里多给中位数和 p10——均值被长尾拉高时
    只看 min/avg/max 分不出「多数类目都很薄」和「个别类目很薄」。"""
    s = sorted(v)
    return {"n": len(s), "min": s[0], "p10": s[len(s) // 10], "med": st.median(s),
            "avg": sum(s) / len(s), "p99": s[min(len(s) - 1, len(s) * 99 // 100)],
            "max": s[-1]}


def judge(rows: list[dict], counts: dict[int, int], s: SEM.Semantics) -> list[tuple]:
    """返回 [(verdict, 标题, 说明)]。纯函数，--selftest 直接喂构造数据。"""
    leaf_of = {cid: leaf for leaf, cid in s.tree.leaf_ids.items()}
    out: list[tuple] = []

    per_cat = collections.Counter(r["category_id"] for r in rows)
    per_brand = collections.Counter(r["brand"] for r in rows)
    empty = [leaf for leaf, cid in s.tree.leaf_ids.items() if not per_cat.get(cid)]

    # 5. 空类目
    out.append(("FAIL" if empty else "PASS", f"空类目数 = 0（通过线 0）",
                f"{len(s.tree.leaf_paths)} 个叶子类目里 {len(empty)} 个没有 SKU"
                + (f"；例 {empty[:5]}" if empty else "")))

    # 1/2. 每类目 SKU 数。分母用**声明的全部叶子**而不是数据里出现过的类目：
    # 用后者会把空类目从分布里抹掉，min 永远 ≥1，第 1 条判据自动失效。
    cv = [per_cat.get(cid, 0) for cid in s.tree.leaf_ids.values()]
    d = dist(cv)
    out.append((("PASS" if d["min"] >= MIN_SKU_PER_CATEGORY else "FAIL"),
                f"每类目 SKU 数 min ≥ {MIN_SKU_PER_CATEGORY}",
                f"min {d['min']} · p10 {d['p10']} · 中位 {d['med']:.0f} · "
                f"均值 {d['avg']:.1f} · max {d['max']}"))
    out.append((("PASS" if d["med"] >= MED_SKU_PER_CATEGORY else "FAIL"),
                f"每类目 SKU 数 中位数 ≥ {MED_SKU_PER_CATEGORY}",
                f"中位 {d['med']:.0f}（任务书 D-02 记录的 v2 库：均值 "
                f"{BASELINE['sku_per_category']}）"))

    # 3. 每品牌 SKU 数
    bd = dist(list(per_brand.values())) if per_brand else dist([0])
    out.append((("PASS" if bd["min"] >= MIN_SKU_PER_BRAND else "FAIL"),
                f"每品牌 SKU 数 min ≥ {MIN_SKU_PER_BRAND}",
                f"{bd['n']} 个品牌 · min {bd['min']} · p10 {bd['p10']} · "
                f"中位 {bd['med']:.0f} · max {bd['max']}"))

    # 4. 每 SKU 被下单次数
    ov = [counts.get(r["product_id"], 0) for r in rows]
    od = dist(ov) if ov else dist([0])
    out.append((("PASS" if od["avg"] <= MAX_ORDERS_PER_SKU else "FAIL"),
                f"每 SKU 平均被下单次数 ≤ {MAX_ORDERS_PER_SKU}",
                f"均值 {od['avg']:.1f}（= order_items ÷ SKU 数，正是 D-02 里那个数的"
                f"定义；任务书 v2 库 {BASELINE['orders_per_sku']:.0f}）· "
                f"中位 {od['med']:.0f} · "
                f"min {od['min']} · p99 {od['p99']} · max {od['max']}"
                f" —— 长尾是有意的，闸门只看均值，理由见模块 docstring"))

    # 6. 类目内价格跨度 vs 声明带宽
    by_cat: dict[int, list[float]] = collections.defaultdict(list)
    for r in rows:
        by_cat[r["category_id"]].append(r["price"])
    over, spans = [], []
    for cid, ps in by_cat.items():
        leaf = leaf_of.get(cid)
        if leaf is None or min(ps) <= 0 or len(ps) < 2:
            continue
        actual = max(ps) / min(ps)
        lo, hi = s.price_bands[leaf]
        spans.append((actual, leaf))
        if actual > hi / lo + 1e-9:
            over.append((leaf, actual, hi / lo))
    spans.sort()
    # 只有 1 个 SKU 的类目算不出跨度。**要把它们的个数报出来**：`0 个越界` 配
    # `120 个类目算不出跨度` 是一句危险的绿——分辨率不够时这条判据没有输入，
    # 不是「查过了没问题」。它红不红由上面第 1/2 条负责，这里只负责不假装自己查过。
    n_thin = len(by_cat) - len(spans) + len(empty)
    if not spans:
        out.append(("PASS", "类目内实际价格跨度 ≤ 声明带宽（通过线 0 越界）",
                    f"无输入：{len(by_cat)} 个非空类目全部只有 1 个 SKU，跨度算不出来"
                    f"——这条判据此刻不含信息，看上面的 SKU 数两条"))
        return out
    top = "，".join(f"{lf.rsplit(SEM.SEP, 1)[-1]} {v:.0f}×" for v, lf in spans[-3:][::-1])
    out.append((("PASS" if not over else "FAIL"),
                "类目内实际价格跨度 ≤ 声明带宽（通过线 0 越界）",
                f"{len(spans)} 个类目可算跨度（{n_thin} 个不足 2 个 SKU、跳过）· "
                f"中位 {st.median([v for v, _ in spans]):.1f}× · 最宽 {top}"
                + (f"；越界 {len(over)} 个，例 "
                   f"{[(l.rsplit(SEM.SEP, 1)[-1], f'{a:.0f}×>{d:.0f}×') for l, a, d in over[:3]]}"
                   if over else "")))
    return out


def run(rows: list[dict], counts: dict[int, int], dim_rows: dict[str, int],
        s: SEM.Semantics) -> int:
    print("维度表行数（括号里是任务书 D-02 记录的 v2 Redshift 库观测值，供对照，非本库现状）")
    for t in DIM_TABLES + ["order_items"]:
        if t not in dim_rows:
            continue
        was = BASELINE.get(t)
        print(f"  {t:<22} {dim_rows[t]:>12,}" + (f"   （v2 库 {was:,}）" if was else ""))
    nb = len({r["brand"] for r in rows})
    print(f"  {'brand (distinct)':<22} {nb:>12,}   （v2 库 {BASELINE['brands']:,}）")

    # 通过线是按**全量目标规模**推导的（见模块 docstring）。scale=1 的小样本只有 200 个
    # SKU 摊 126 个类目，下面几条必然红——那是样本小，不是数据退化。不说这句话，一份
    # 小样本报告会被读成「修复没生效」。
    # 反过来也不能因此把阈值调软去让小样本变绿：那等于让检查去迎合手头的数据。
    import budget                                       # noqa: PLC0415  真源在这里
    tgt = budget.table_rows(budget.solve_scale(budget.DEFAULT_TARGET_ROWS))
    if len(rows) < tgt["products"] * 0.9:
        print(f"\n  ⚠ 本次样本 {len(rows):,} 个 SKU，全量目标是 {tgt['products']:,}"
              f"（budget.py）。分辨率类判据的通过线按全量推导，小样本会红——"
              f"要看绿灯请对全量产出跑。")

    # 「每 SKU 被下单次数」是个**比值**，事实表和维度表一起缩小时它照样漂亮。所以
    # 必须把分子的规模也报出来，否则那条 ok 会被读成「这个指标没问题」。
    #
    # 这一点在 v3 上尤其要紧，而且和任务书的描述不一样：任务书 D-02 写「事实表按 427 倍
    # 放大了，维度表还是 v1 原样，平均每个 SKU 被下单约 9,021 次」——那是导师看到的
    # **v2 Redshift 库**。v3 的 Athena 库从来没灌过 scripts/gen 的全量数据，整库都还是
    # v1 规模（500 用户 / 2,000 单 / 4,225 明细），所以 9,021 这个数在 v3 上不存在，
    # 每 SKU 只有 21 单。D-02 在 v3 里是**潜伏**的：病灶在生成器（budget.py 声明了 SUB
    # 缩放却整类没有 builder），谁一灌全量数据它就当场显形。修生成器因此是对的层次。
    oi, oi_t = dim_rows.get("order_items", 0), tgt.get("order_items", 0)
    if oi_t and oi < oi_t * 0.9:
        print(f"\n  ⚠ order_items 只有 {oi:,} 行，全量目标 {oi_t:,}（{oi / oi_t:.1%}）。"
              f"「每 SKU 被下单次数」是比值，分子分母一起小的时候它照样通过——"
              f"这条 ok 只说明比例没失衡，不说明规模够用。")
    print()

    fails = 0
    for verdict, title, note in judge(rows, counts, s):
        print(f"{MARK[verdict]} {title}")
        print(f"        {note}")
        fails += verdict == "FAIL"
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="L6 维度分辨率")
    ap.add_argument("--from-csv", type=Path, metavar="DIR",
                    help="读生成器产出而不是查云上库（需要 products.csv + order_items.csv）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（无云依赖）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    s = SEM.load()
    if a.from_csv:
        rows = load_products(a.from_csv)
        dim_rows, counts = load_counts(a.from_csv)
        where = f"生成器产出 {a.from_csv}"
    else:
        import athena
        c = athena.Client()
        rows = load_products(None, c)
        dim_rows, counts = load_counts(None, c)
        where = "Athena 现行库"
    print(f"=== L6 维度分辨率 · {where} ===\n")
    fails = run(rows, counts, dim_rows, s)

    if fails:
        print(f"\nL6：{fails} 项 FAIL")
        if not a.from_csv:
            print("  云上商品是 v1 的 200 行，项目已决定不重灌，所以这些 FAIL 是对现状"
                  "的记录。\n  生成侧：verify_resolution.py --from-csv <生成目录>。")
    else:
        print("\nL6：全部通过 ✅")
    return 0                    # 诊断工具，不是闸门。理由同 verify_literals.py


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """两组构造数据：D-02 记录的 v2 库形态必须全红，修复后的规模必须全绿。

    只验一侧证明不了判据有区分力——这是 Batch 1 学到的：`verify_enums.py` 当年两端
    都是同一份数据的副本，绿灯不含任何信息。
    """
    s = SEM.load()
    leaves = list(s.tree.leaf_ids.items())
    bad = 0

    def show(tag: str, rows, counts, want_fail: set[str]) -> None:
        nonlocal bad
        got = {t for v, t, _ in judge(rows, counts, s) if v == "FAIL"}
        titles = {t for _, t, _ in judge(rows, counts, s)}
        unknown = want_fail - titles
        if unknown:
            bad += 1
            print(f"  FAIL {tag}：用例写了不存在的判据标题 {unknown}")
            return
        if got == want_fail:
            print(f"  ok  {tag}")
        else:
            bad += 1
            print(f"  FAIL {tag}\n       期望红 {sorted(got ^ want_fail)} 处不符"
                  f"\n       实际红 {sorted(got)}")

    T_EMPTY = "空类目数 = 0（通过线 0）"
    T_MIN = f"每类目 SKU 数 min ≥ {MIN_SKU_PER_CATEGORY}"
    T_MED = f"每类目 SKU 数 中位数 ≥ {MED_SKU_PER_CATEGORY}"
    T_BRAND = f"每品牌 SKU 数 min ≥ {MIN_SKU_PER_BRAND}"
    T_ORD = f"每 SKU 平均被下单次数 ≤ {MAX_ORDERS_PER_SKU}"
    T_SPAN = "类目内实际价格跨度 ≤ 声明带宽（通过线 0 越界）"

    def mk(per_cat: int, per_brand_cycle: int, orders: int, spread: float = 1.0):
        """造 per_cat × 126 行商品，品牌按 per_brand_cycle 个轮换，每 SKU orders 单。"""
        rows, counts, pid = [], {}, 0
        for leaf, cid in leaves:
            lo, hi = s.price_bands[leaf]
            for k in range(per_cat):
                pid += 1
                brands = s.brands_for_leaf[leaf]
                b = brands[k % min(per_brand_cycle, len(brands))]
                rows.append({"product_id": pid, "product_name": "x", "brand": b,
                             "category_id": cid,
                             "price": lo * (spread ** k) if k else lo})
                counts[pid] = orders
        return rows, counts

    print("---- 任务书 D-02 记录的 v2 库形态（每类目 1.2 个 SKU、每 SKU 9021 单）----")
    # 每类目 1 个 → min/中位都不达标；每 SKU 9021 单 → 超量级护栏。
    # 品牌只有 1 个轮换 → 每品牌 SKU 数也不达标。价格跨度 1×（只有 1 个 SKU 的类目
    # 被跳过），所以 T_SPAN 不该红——这条正是「分布类判据不该互相牵连」的证明。
    rows, counts = mk(1, 1, 9021)
    show("每类目 1 个 SKU + 9021 单/SKU → min/中位/品牌/下单次数四条红，价格跨度不红",
         rows, counts, {T_MIN, T_MED, T_BRAND, T_ORD})

    print("\n---- 空类目：最隐蔽的一面 ----")
    rows2 = [r for r in rows if r["category_id"] != leaves[0][1]]
    show("删掉一个类目的全部 SKU → 空类目那条红（其余不变）",
         rows2, counts, {T_EMPTY, T_MIN, T_MED, T_BRAND, T_ORD})

    print("\n---- 修复后的规模（每类目 33 个 SKU、436 单/SKU）----")
    # 33 × 126 ≈ 4158，贴近实际的 4133；品牌轮换取 4 个，满足每品牌 ≥4。
    rows3, counts3 = mk(33, 4, 436, spread=1.0)
    show("每类目 33 个 SKU + 436 单/SKU + 品牌轮换 4 → 全绿", rows3, counts3, set())

    print("\n---- 边界：阈值必须是闭的 ----")
    # 这两条的期望里带着 T_BRAND，那是**夹具的性质、不是判据的错**：每类目只放 4 个 SKU
    # 时，配置里只有 1 个合法品牌的叶子（brands_for_leaf 的 min 就是 1）凑不出「每品牌
    # 4 个」。写用例时我先按「只有中位数那条红」填了期望，是判据把我纠正过来的。要让夹具
    # 同时满足两条保底线得按品牌全局轮换，那会让「恰好 min=4」这个被测性质失真——所以
    # 选择把夹具的局限写在这里，而不是调判据去迎合夹具。
    rows4, counts4 = mk(MIN_SKU_PER_CATEGORY, MIN_SKU_PER_BRAND, MAX_ORDERS_PER_SKU)
    show(f"恰好 每类目 min={MIN_SKU_PER_CATEGORY}、均值={MAX_ORDERS_PER_SKU} → 这两条都不红"
         f"（阈值闭区间）；红的是中位数（{MIN_SKU_PER_CATEGORY} < {MED_SKU_PER_CATEGORY}）"
         f"和夹具撑不起的品牌保底",
         rows4, counts4, {T_MED, T_BRAND})
    rows5, counts5 = mk(MIN_SKU_PER_CATEGORY - 1, MIN_SKU_PER_BRAND, MAX_ORDERS_PER_SKU + 1)
    show("每类目 min 差 1、均值超 1 → min 与下单次数跟着红（证明上一条不是恒绿）",
         rows5, counts5, {T_MIN, T_MED, T_ORD, T_BRAND})

    print("\n---- 价格跨度：越界必须单独红，不牵连其它 ----")
    # 每类目 33 个 SKU，价格逐个 ×1.3 → 跨度 1.3^32 ≈ 5700×，远超任何声明带宽。
    rows6, counts6 = mk(33, 4, 436, spread=1.3)
    show("类目内价格逐级 ×1.3（跨度 ~5700×）→ 只有价格跨度那条红",
         rows6, counts6, {T_SPAN})

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
