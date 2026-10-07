#!/usr/bin/env python3
"""semantics.yaml 的加载器 + 校验器：把语义约束和真实类目树对死。

## 为什么加载要顺带校验，而不是只 yaml.safe_load

`semantics.yaml` 是**声明态**，`data/csv/categories.csv` 是类目树的真源。两者各写
一份类目名，就是本项目已经栽过三次的形态（见 docs/test-plan.md「比的两端不独立」）：
配置里把叶子拼错成「智能手环 」（带尾空格），生成器抽不到这个类目 → 该类目 0 个 SKU
→ 而 L7 用同一份配置去检查，也不知道有这个类目，于是**两边一致地漏掉它**，全绿。

所以校验放在 `load()` 里而不是 `--selftest` 里：任何 import 这份配置的代码（生成器、
L6、L7）都被迫先过一遍校验，没有「跳过检查直接用」的路径。校验是纯本地、毫秒级的。

判据（全部硬 FAIL，因为每一条失败都会导致静默的数据缺陷而不是报错）：

  1. 类目树自身结构成立：一级无父、二级父为一级、叶子父为二级；
  2. `price_bands` 的键集合 == 树里的叶子路径集合（**双向**，多一个少一个都算错）；
  3. 每个价格带 0 < lo < hi，且 hi/lo 不超过 MAX_BAND_RATIO；
  4. 每个品牌的 tier 在 `tier_windows` 里；
  5. 每个品牌声明的类目路径在树里存在（允许二级路径或叶子路径两种粒度）；
  6. **每个叶子类目至少有一个品牌能经营它** —— 漏掉一个就是上面那个静默场景；
  7. `name_specs` 的键集合 == 二级类目路径集合（双向）；
  8. `tier_windows` 的区间 0 ≤ a < b ≤ 1；
  9. `tag_pools` 的标签名跨池不重复（生成器靠这条保证 UNIQUE(product_id, tag_name)）。

第 3 条的定位要说清楚：**它不是真实性保证，是防打字错**（把 `[9, 99]` 敲成
`[9, 99999]`）。价格的真实性靠两件事，都不在这里：品牌分档把带切成子区间
（平价品牌摸不到带顶），以及 L6 把每个类目的实际价格跨度打出来供人复核。
把 MAX_BAND_RATIO 调得很紧只会逼着人去改配置迎合检查，那是自证。

`tag_pools` 的键（tag_type 取值）**故意不在这里校验**：selftest_closures 的
`check_enums_vs_cards` 已经在做「生成器产出 ⊆ knowledge 卡片声明」，product_tags
表一有 builder 就自动被它覆盖。在这里再写一份 tag_type 白名单就是第二份副本。

## 用法

    python3 scripts/gen/semantics.py                # 打印摘要（品牌/类目/价格带统计）
    python3 scripts/gen/semantics.py --selftest     # 跑断言，L0 用这个

无云依赖，无 numpy 依赖（只用 csv + yaml），秒级。
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
YAML_PATH = HERE / "semantics.yaml"
CATEGORIES_CSV = ROOT / "data" / "csv" / "categories.csv"

SEP = ">"

# 价格带 hi/lo 上限。**这是量级打字错的护栏，不是真实性判据**（见模块 docstring）：
# 拦的是 `[9, 99]` 敲成 `[9, 99999]` 这种，不是「这个带看起来太宽」。
#
# 所以定在 100× 这个整数门槛，而**不是**贴着实测最大值 61.2×（数码>配件>耳机）。
# 贴着实测值定有两个坏处：一是把检查变成了对当前配置的复述——它只会在有人改配置时
# 响，而不是在配置错时响；二是下次真要加宽某个带，改的人第一反应是调这个常量，
# 检查就被磨掉了。实测分布中位 12.8×、范围 3.5×~61.2×，跑无参 main 会打出来。
MAX_BAND_RATIO = 100.0


class SemanticsError(Exception):
    """配置与类目树不一致。刻意用异常而不是返回 False：调用方没有忽略它的路径。"""


@dataclass(frozen=True)
class Tree:
    """categories.csv 解析结果。所有路径都是 `一级>二级>叶子` 形式的全路径。

    leaf_paths 按 category_id 升序，保证生成器的抽样顺序可复现（配置是 dict，
    Python 3.7+ 保序，但 CSV 是真源，以它的顺序为准）。
    """
    leaf_paths: tuple[str, ...]
    leaf_ids: dict[str, int]          # 叶子全路径 -> category_id
    level2_paths: frozenset[str]      # `一级>二级`
    level1_names: tuple[str, ...]

    def leaf_level1(self, leaf: str) -> str:
        return leaf.split(SEP, 1)[0]

    def leaf_level2(self, leaf: str) -> str:
        """叶子的二级路径 `一级>二级`。规格词池按这个粒度取，理由见 semantics.yaml。"""
        return leaf.rsplit(SEP, 1)[0]

    def leaves_under(self, path: str) -> list[str]:
        """path 是二级路径时返回其下全部叶子；是叶子路径时返回它本身。"""
        if path in self.leaf_ids:
            return [path]
        pre = path + SEP
        return [p for p in self.leaf_paths if p.startswith(pre)]


def read_tree(csv_path: Path = CATEGORIES_CSV) -> Tree:
    """从 categories.csv 还原类目树。不信任 level 列与 parent 链的一致性，两者都查。"""
    rows: dict[int, dict] = {}
    with csv_path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            cid = int(r["category_id"])
            rows[cid] = {
                "parent": int(r["parent_id"]) if r["parent_id"] else None,
                "name": r["category_name"],
                "level": int(r["level"]),
            }

    def path_of(cid: int) -> str:
        parts, cur, seen = [], cid, set()
        while cur is not None:
            if cur in seen:
                raise SemanticsError(f"类目树有环，category_id={cid}")
            seen.add(cur)
            if cur not in rows:
                raise SemanticsError(f"category_id={cur} 的 parent 指向不存在的行")
            parts.append(rows[cur]["name"])
            cur = rows[cur]["parent"]
        return SEP.join(reversed(parts))

    lv = {1: [], 2: [], 3: []}
    for cid, r in rows.items():
        if r["level"] not in lv:
            raise SemanticsError(f"category_id={cid} 的 level={r['level']} 不在 1/2/3 内")
        lv[r["level"]].append(cid)

    # level 列与 parent 链必须互相印证：只查一个，另一个错了照样过。
    for cid in lv[1]:
        if rows[cid]["parent"] is not None:
            raise SemanticsError(f"一级类目 {rows[cid]['name']} 竟有 parent_id")
    for depth in (2, 3):
        for cid in lv[depth]:
            p = rows[cid]["parent"]
            if p is None:
                raise SemanticsError(f"{depth} 级类目 {rows[cid]['name']} 没有 parent_id")
            if rows[p]["level"] != depth - 1:
                raise SemanticsError(
                    f"{depth} 级类目 {rows[cid]['name']} 的父级 level={rows[p]['level']}")

    leaves = sorted(lv[3])
    return Tree(
        leaf_paths=tuple(path_of(c) for c in leaves),
        leaf_ids={path_of(c): c for c in leaves},
        level2_paths=frozenset(path_of(c) for c in lv[2]),
        level1_names=tuple(rows[c]["name"] for c in sorted(lv[1])),
    )


@dataclass(frozen=True)
class Semantics:
    tree: Tree
    tier_windows: dict[str, tuple[float, float]]
    price_bands: dict[str, tuple[float, float]]
    brand_tier: dict[str, str]
    brands_for_leaf: dict[str, tuple[str, ...]]   # 叶子路径 -> 可经营它的品牌
    leaves_for_brand: dict[str, tuple[str, ...]]  # 品牌 -> 它可经营的叶子
    name_specs: dict[str, tuple[str, ...]]        # 二级类目路径 -> 规格词池
    legacy_name_prefixes: tuple[str, ...]
    tag_pools: dict[str, tuple[str, ...]]

    @property
    def brands(self) -> tuple[str, ...]:
        return tuple(self.brand_tier)

    def price_range(self, brand: str, leaf: str) -> tuple[float, float]:
        """(brand, leaf) 组合的合法价格区间 = 类目价格带 ∩ 该品牌分档窗口。

        生成器按这个区间抽价，L7 按同一个区间判越界——**同一个函数**，不是两份公式。
        这是「生成器和检查集共用一份判据」落到代码层的形态：配置共用还不够,
        把区间算错的方式有很多种，公式也必须共用。
        """
        lo, hi = self.price_bands[leaf]
        a, b = self.tier_windows[self.brand_tier[brand]]
        return lo + (hi - lo) * a, lo + (hi - lo) * b

    def allows(self, brand: str, leaf: str) -> bool:
        return brand in self.brands_for_leaf.get(leaf, ())

    def specs_for(self, leaf: str) -> tuple[str, ...]:
        return self.name_specs[self.tree.leaf_level2(leaf)]

    def capacity(self) -> int:
        """能产出的不重名 product_name 上界 = Σ(该叶子的品牌数 × 该二级的规格词数)。

        生成器分配 SKU 时单个(品牌,叶子)对最多摊到 len(specs) 个，超了必然重名。
        selftest 用它核 budget 的 SKU 目标撑不撑得住。
        """
        return sum(len(v) * len(self.specs_for(leaf))
                   for leaf, v in self.brands_for_leaf.items())


def load(yaml_path: Path = YAML_PATH, csv_path: Path = CATEGORIES_CSV) -> Semantics:
    """读配置 + 全量校验。任何一条不成立都抛 SemanticsError，不返回半成品。"""
    tree = read_tree(csv_path)
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if raw.get("version") != 1:
        raise SemanticsError(f"semantics.yaml version={raw.get('version')}，本加载器只认 1")

    # ---- 4/8. 分档窗口 ----
    windows: dict[str, tuple[float, float]] = {}
    for tier, pair in raw["tier_windows"].items():
        a, b = float(pair[0]), float(pair[1])
        if not (0.0 <= a < b <= 1.0):
            raise SemanticsError(f"tier_windows[{tier}]={pair} 不满足 0 ≤ a < b ≤ 1")
        windows[tier] = (a, b)

    # ---- 2/3. 价格带 ⟷ 叶子路径，双向 ----
    bands: dict[str, tuple[float, float]] = {}
    for leaf, pair in raw["price_bands"].items():
        lo, hi = float(pair[0]), float(pair[1])
        if not (0 < lo < hi):
            raise SemanticsError(f"price_bands[{leaf}]={pair} 不满足 0 < lo < hi")
        if hi / lo > MAX_BAND_RATIO:
            raise SemanticsError(
                f"price_bands[{leaf}]={pair} 带宽 {hi / lo:.1f}× 超过 "
                f"MAX_BAND_RATIO={MAX_BAND_RATIO}（可能是打字错；理由见模块 docstring）")
        bands[leaf] = (lo, hi)

    want, got = set(tree.leaf_paths), set(bands)
    if want != got:
        raise SemanticsError(
            f"price_bands 与 categories.csv 的叶子类目不一致："
            f"配置缺 {sorted(want - got)}；配置多出 {sorted(got - want)}")

    # ---- 5. 品牌类目路径解析 ----
    brand_tier: dict[str, str] = {}
    leaves_for_brand: dict[str, tuple[str, ...]] = {}
    for brand, spec in raw["brands"].items():
        brand = str(brand)
        tier = spec["tier"]
        if tier not in windows:
            raise SemanticsError(f"品牌 {brand} 的 tier={tier!r} 不在 tier_windows 内")
        leaves: list[str] = []
        for path in spec["categories"]:
            if path not in tree.level2_paths and path not in tree.leaf_ids:
                raise SemanticsError(
                    f"品牌 {brand} 声明的类目路径 {path!r} 在 categories.csv 里不存在"
                    f"（只接受二级路径如 '家电>小家电' 或叶子路径如 '家电>小家电>吹风机'）")
            leaves.extend(tree.leaves_under(path))
        if not leaves:
            raise SemanticsError(f"品牌 {brand} 展开后没有任何叶子类目")
        # 去重但保持 leaf_paths 的顺序：二级路径之间可能重叠（写了二级又写了其下叶子）
        seen = set(leaves)
        brand_tier[brand] = tier
        leaves_for_brand[brand] = tuple(p for p in tree.leaf_paths if p in seen)

    # ---- 6. 反向索引 + 每个叶子至少一个品牌 ----
    brands_for_leaf: dict[str, tuple[str, ...]] = {}
    for leaf in tree.leaf_paths:
        bs = tuple(b for b in brand_tier if leaf in leaves_for_brand[b])
        if not bs:
            raise SemanticsError(
                f"叶子类目 {leaf!r} 没有任何品牌能经营它 —— 生成器会抽不到品牌，"
                f"该类目静默 0 个 SKU，而 L7 用同一份配置也看不出来")
        brands_for_leaf[leaf] = bs

    # ---- 7. 规格词池 ⟷ 二级类目路径，双向 ----
    specs = {k: tuple(v) for k, v in raw["name_specs"].items()}
    want2, got2 = set(tree.level2_paths), set(specs)
    if want2 != got2:
        raise SemanticsError(
            f"name_specs 与二级类目不一致：缺 {sorted(want2 - got2)}；"
            f"多出 {sorted(got2 - want2)}（键必须是 '一级>二级' 全路径）")
    for k, v in specs.items():
        if len(set(v)) != len(v):
            raise SemanticsError(f"name_specs[{k}] 有重复词")

    prefixes = tuple(raw["legacy_name_prefixes"])
    if not prefixes:
        raise SemanticsError("legacy_name_prefixes 为空，L7 的商品名模板反查会失去判据")

    tags = {k: tuple(v) for k, v in raw["tag_pools"].items()}
    for k, v in tags.items():
        if len(set(v)) != len(v):
            raise SemanticsError(f"tag_pools[{k}] 有重复标签")
    # 9. 标签名跨池不重复。生成器把 5 个池摊平成一个 28 项列表、无放回抽 k 个，靠的就是
    # 「名字唯一 ⇒ (product_id, tag_name) 唯一」。同名出现在两个池里，无放回也挡不住
    # 同一商品拿到两条同名不同 tag_type 的行，正好违反 08_product_domain.sql 的
    # UNIQUE(product_id, tag_name)——而 Iceberg 不强制约束，会静默留下重复。
    # 「家庭装」这种词横跨 audience/规格语义，很容易被后来的人补进第二个池，所以要有断言。
    owner: dict[str, str] = {}
    for k, v in tags.items():
        for nm in v:
            if nm in owner:
                raise SemanticsError(
                    f"标签 {nm!r} 同时出现在 tag_pools[{owner[nm]}] 和 tag_pools[{k}]："
                    f"生成器按名字无放回抽样，跨池重名会产出同一商品的重复 tag_name，"
                    f"违反 UNIQUE(product_id, tag_name)")
            owner[nm] = k

    return Semantics(
        tree=tree, tier_windows=windows, price_bands=bands, brand_tier=brand_tier,
        brands_for_leaf=brands_for_leaf, leaves_for_brand=leaves_for_brand,
        name_specs=specs, legacy_name_prefixes=prefixes, tag_pools=tags,
    )


# ---------------------------------------------------------------- 摘要 / 自测

def summary(s: Semantics) -> None:
    n_pairs = sum(len(v) for v in s.brands_for_leaf.values())
    per_leaf = sorted(len(v) for v in s.brands_for_leaf.values())
    per_brand = sorted(len(v) for v in s.leaves_for_brand.values())
    ratios = sorted((hi / lo, leaf) for leaf, (lo, hi) in s.price_bands.items())

    def band(v: list[int]) -> str:
        return f"min {v[0]} / 中位 {v[len(v) // 2]} / max {v[-1]}"

    print(f"类目树         一级 {len(s.tree.level1_names)} · 二级 {len(s.tree.level2_paths)}"
          f" · 叶子 {len(s.tree.leaf_paths)}（真源 data/csv/categories.csv）")
    print(f"品牌           {len(s.brands)} 个"
          f"（budget {sum(1 for t in s.brand_tier.values() if t == 'budget')}"
          f" / mid {sum(1 for t in s.brand_tier.values() if t == 'mid')}"
          f" / premium {sum(1 for t in s.brand_tier.values() if t == 'premium')}）")
    print(f"合法(品牌,叶子) {n_pairs} 对")
    print(f"每叶子品牌数   {band(per_leaf)}")
    print(f"每品牌叶子数   {band(per_brand)}")
    print(f"价格带最宽 5   " + "，".join(f"{lf.split(SEP)[-1]} {r:.0f}×"
                                        for r, lf in ratios[-5:][::-1]))
    print(f"价格带最窄 3   " + "，".join(f"{lf.split(SEP)[-1]} {r:.1f}×"
                                        for r, lf in ratios[:3]))
    pool = sorted(len(v) for v in s.name_specs.values())
    print(f"规格词池       {len(s.name_specs)} 个（按二级类目）· 每池 {band(pool)} 词")
    print(f"不重名商品名上界 {s.capacity()} = Σ(叶子品牌数 × 该二级规格词数)"
          f"；SKU 目标须小于它，否则必然重名")


def selftest() -> int:
    """L0 用。断言集中在「配置能支撑生成目标规模」——这是 load() 的校验管不到的一层。"""
    sys.path.insert(0, str(HERE))
    import budget  # noqa: E402  同目录，作为规模目标的真源

    npass = 0

    def ok(cond, msg: str) -> None:
        nonlocal npass
        assert cond, f"FAIL: {msg}"
        npass += 1
        print(f"  ok  {msg}")

    s = load()
    ok(True, f"semantics.yaml 全量校验通过（{len(s.tree.leaf_paths)} 叶子类目 × "
             f"{len(s.brands)} 品牌）")

    # 配置必须撑得住 budget 声明的 SKU 规模，否则商品名会大量重名。
    # 规模的真源是 budget.py，**不在这里复制一个数字**。
    scale = budget.solve_scale(budget.DEFAULT_TARGET_ROWS)
    sku = budget.table_rows(scale)["products"]
    n_pairs = sum(len(v) for v in s.brands_for_leaf.values())
    cap = s.capacity()
    ok(cap >= sku,
       f"不重名商品名上界 {cap} ≥ 目标 SKU {sku}（{n_pairs} 个(品牌,叶子)对，"
       f"scale={scale:.1f}）")

    # 上界够不等于分配得开：还要求平均每对摊到的 SKU 数不超过最小的那个词池，
    # 否则某些叶子（品牌少 + 词池小）会先撑爆。这条是上一条的加严版。
    tight = min((len(v) * len(s.specs_for(leaf)), leaf)
                for leaf, v in s.brands_for_leaf.items())
    ok(sku / n_pairs <= min(len(v) for v in s.name_specs.values()),
       f"平均每(品牌,叶子)对 {sku / n_pairs:.1f} 个 SKU ≤ 最小词池 "
       f"{min(len(v) for v in s.name_specs.values())} 词"
       f"（最紧的叶子是 {tight[1]}，容量 {tight[0]}）")

    # 每个叶子至少能摊到一个 SKU：叶子数不能超过 SKU 数，否则必有空类目。
    # D-02 的病灶正是「200 个 SKU 摊 126 个叶子」，这条断言把它钉住。
    ok(sku >= len(s.tree.leaf_paths) * 4,
       f"目标 SKU {sku} ≥ 叶子数 {len(s.tree.leaf_paths)} × 4"
       f"（每类目至少能摊到 4 个 SKU；旧数据是 {200 / 126:.1f} 个）")

    # price_range 必须落在类目带内，且宽度非零——生成器和 L7 都依赖它。
    bad = []
    for leaf, brands in s.brands_for_leaf.items():
        lo, hi = s.price_bands[leaf]
        for b in brands:
            a, z = s.price_range(b, leaf)
            if not (lo <= a < z <= hi):
                bad.append((b, leaf, a, z))
    ok(not bad, f"每个(品牌,叶子)的价格区间都真包含于类目带且非空"
                + (f"；违例 {bad[:2]}" if bad else ""))

    # 分档要真的把带切开：premium 的下界必须高于 budget 的上界所在的位置，
    # 否则「平价品牌卖高价」这个缺陷根本没被约束住，只是换了个说法。
    a_bud = s.tier_windows["budget"][1]
    a_pre = s.tier_windows["premium"][0]
    ok(a_pre >= a_bud,
       f"premium 下界 {a_pre:.0%} ≥ budget 上界 {a_bud:.0%}"
       f"（两档不重叠，平价品牌摸不到 premium 独占区）")

    # 规格词不能撞上营销修饰词，否则 L7 的模板反查会误判自己产的数据。
    lp = set(s.legacy_name_prefixes)
    clash = {k: sorted(set(v) & lp) for k, v in s.name_specs.items() if set(v) & lp}
    ok(not clash, f"规格词池与 legacy_name_prefixes 无交集"
                  + (f"；冲突 {clash}" if clash else ""))

    # 叶子名在全树内可能重名（T恤/裤子/外套），所以配置必须用全路径。
    # 这条断言存在的意义是：将来有人「简化」成只写叶子名时，它会响。
    from collections import Counter
    dup = [n for n, c in Counter(p.split(SEP)[-1] for p in s.tree.leaf_paths).items() if c > 1]
    ok(all(SEP in k for k in s.price_bands),
       f"price_bands 的键全部是全路径（叶子重名 {len(dup)} 个：{dup}，只写叶子名会撞）")

    print(f"\n全部通过（{npass} 项断言）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="semantics.yaml 加载 / 校验")
    ap.add_argument("--selftest", action="store_true", help="跑断言（L0 用）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    summary(load())
    print("\n校验通过 ✅（判据见模块 docstring；跑 --selftest 追加规模自洽断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
