#!/usr/bin/env python3
"""L7 语义配对合理性：品牌、类目、价格三者是不是随机配对（D-03 的判据）。

## 判据从哪来

**唯一真源是 `scripts/gen/semantics.yaml`**，本文件通过 `scripts/gen/semantics.py`
读它，价格区间调的是 `Semantics.price_range()` —— 和生成器调的**同一个函数**。

这一条是任务书点名的硬要求：「这两张表生成器和检查集必须共用同一份，否则修完生成器
再改检查，就变成了自证」。共用配置只是第一层：把区间算错的方式有很多种（把品牌分档
窗口乘反、把上下界当成绝对值而不是比例），所以公式也必须共用。本文件里没有任何一处
自己算价格区间。

## 三类判据与通过线

    白名单     每个 SKU 的 (brand, 叶子类目) 必须在声明的合法对里          通过线 0 违例
    价格       price 必须落在 类目带 ∩ 品牌分档窗口 内，并给出偏离倍数     通过线 0 越界
    商品名     不得能由「修饰词 + 品牌 + 类目」还原；必须是三段式且规格
               来自该二级类目的词池                                        通过线 0 命中

「偏离倍数」= 越界值与最近边界的比（高出上界 3.1 倍 / 低于下界 0.4 倍）。任务书要求
输出它而不只是布尔值：`冰箱 50.12` 和 `冰箱 1200` 都算越界，但前者是量级级错误、后者
是边界毛刺，运维上不是一件事。

## 两类前置违例：不在声明里的品牌 / 不是叶子的类目

活的库里可能出现配置压根没声明的品牌（v1 有 59 个 distinct brand，与本配置的 115 个
只部分重合），也可能出现 `category_id` 指向二级类目而不是叶子。这两种情况**各自单列
一类违例**，不是 SKIP：

  · 判 SKIP 会让「品牌名拼错」这类缺陷静默通过 —— 拼错的品牌查不到白名单，一跳过
    就永远查不出来；
  · 归进「白名单违例」会把「戴森卖电视」和「库里有个配置没听说过的牌子」混成一个数，
    而这两件事的处理动作完全不同（前者改数据，后者要么补配置要么改数据）。

## 预期结论：对现行库跑必然是红的

当前库里的商品是 v1 的 200 行（Faker 随机配对），项目已决定不重灌数据。所以对云上跑
本文件会有一串 FAIL，**那是对现状的正确记录，不是本文件坏了**。因此它和
`verify_literals.py` 一样**不接进 `scripts/test_all.sh`**：默认流程里挂一盏永远红的
灯，只会把所有人训练成无视红灯。

绿灯要在生成侧看：`--from-csv` 指向 `scripts/gen/main.py` 的产出目录，跑的是同一套
judge 函数。两侧都能跑是有意的设计，不是顺手加的开关——一个只会亮红灯的检查器无法
证明它在绿灯那一侧也判得对。生成侧还有一层更快的网在 `selftest_closures.py`
（`check_product_semantics`，L0 会跑）。

用法：

    python3 scripts/lakehouse/verify_semantics.py                 # 查云上现行库
    python3 scripts/lakehouse/verify_semantics.py --from-csv /tmp/genfull
    python3 scripts/lakehouse/verify_semantics.py --limit 30      # 每类多列几行清单
    python3 scripts/lakehouse/verify_semantics.py --selftest      # 判据自测（无云依赖）
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT / "scripts" / "gen"))

import semantics as SEM     # noqa: E402  判据真源

MARK = {"PASS": "\033[32m  ok  \033[0m", "FAIL": "\033[31m FAIL \033[0m"}

# 商品名三段式：`品牌 叶子类目 规格`。只按空格切，不写成含品牌名的正则——
# 品牌里有 `SK-II` `H&M` 这种含正则元字符的名字，逐个转义比切段脆得多。
NAME_PARTS = 3

# v1 的商品名模板：`修饰词 + 品牌 + 空格 + 类目`（`优质戴森 电热水壶`）。修饰词紧贴
# 品牌、中间没有空格，所以第一段会是「优质戴森」这种拼接体。这个正则只用来给出
# **可读的判定理由**，真正的判据是「第一段是否等于本行 brand」——那条不依赖修饰词池
# 的完整性，池子漏了一个词也拦得住。
LEGACY_RE = re.compile(r"^(?P<prefix>{p})(?P<rest>\S+)\s")


def legacy_re(s: SEM.Semantics) -> re.Pattern:
    return re.compile(LEGACY_RE.pattern.format(
        p="|".join(re.escape(w) for w in s.legacy_name_prefixes)))


# ---------------------------------------------------------------- 取数

PRODUCT_COLS = ["product_id", "product_name", "category_id", "brand", "price"]


def load_products(src: Path | None, client=None) -> list[dict]:
    """读商品行。`src=None` 走 Athena 现行库，否则读 `src/products.csv`。

    两条路返回同一形状的 dict 列表，judge 函数因此不需要知道数据从哪来——这是
    「同一套判据两侧都能跑」的落地方式，不是为了省几行代码。
    """
    if src is not None:
        p = src / "products.csv"
        if not p.exists():
            raise FileNotFoundError(f"没有 {p}；先跑 scripts/gen/main.py --format csv --out {src}")
        with p.open(encoding="utf-8") as fh:
            return [{"product_id": int(r["product_id"]),
                     "product_name": r["product_name"],
                     "category_id": int(r["category_id"]),
                     "brand": r["brand"],
                     "price": float(r["price"])} for r in csv.DictReader(fh)]

    res = client.execute(f"SELECT {', '.join(PRODUCT_COLS)} FROM products")
    idx = {c: i for i, c in enumerate(res["columns"])}
    return [{c: r[idx[c]] for c in PRODUCT_COLS} for r in res["rows"]]


# ---------------------------------------------------------------- 判据

def judge_row(row: dict, s: SEM.Semantics, leaf_of: dict[int, str],
              lre: re.Pattern) -> dict:
    """一行商品的三类判定。返回 {类别: (ok, 说明)}，说明用于打清单。

    纯函数、不碰网络，所以 --selftest 能直接喂构造行进来。
    """
    brand, cid, name = row["brand"], row["category_id"], row["product_name"]
    price = row["price"]
    leaf = leaf_of.get(cid)
    out: dict[str, tuple[bool, str]] = {}

    # 前置一：类目必须是叶子。不是叶子就没有价格带也没有规格词池，后两条无从判起。
    if leaf is None:
        out["category"] = (False, f"category_id={cid} 不是声明的叶子类目")
        return out
    out["category"] = (True, "")
    lname = leaf.rsplit(SEM.SEP, 1)[-1]

    # 前置二：品牌必须在声明表里。不在就查不了白名单和分档窗口（理由见模块 docstring）。
    if brand not in s.brand_tier:
        out["brand_known"] = (False, f"品牌 {brand!r} 不在 semantics.yaml 的 brands 里")
        return out
    out["brand_known"] = (True, "")

    # 1. 白名单
    out["whitelist"] = (
        s.allows(brand, leaf),
        "" if s.allows(brand, leaf) else f"{brand} 不经营 {leaf}（该品牌 tier="
                                         f"{s.brand_tier[brand]}）")

    # 2. 价格。区间来自 SEM.price_range —— 与生成器同一个函数。
    lo, hi = s.price_range(brand, leaf)
    if lo <= price <= hi:
        out["price"] = (True, "")
    else:
        # 偏离倍数：越界值 / 最近边界。高出上界报 ×，低于下界报分数形式的同一个比值，
        # 两边都用「离边界多远」这一个尺度，避免上界用倍数下界用差值。
        ratio = price / hi if price > hi else price / lo
        band = s.price_bands[leaf]
        out["price"] = (False, f"{price:.2f} 越界 [{lo:.0f}, {hi:.0f}] "
                               f"（{ratio:.2f}× 边界；类目带 {band[0]:.0f}~{band[1]:.0f}，"
                               f"tier={s.brand_tier[brand]}）")

    # 3. 商品名可还原性
    m = lre.match(name + " ")
    if m:
        out["name"] = (False, f"匹配 v1 模板：修饰词 {m.group('prefix')!r} + "
                              f"{m.group('rest')!r}")
    else:
        parts = name.split(" ")
        if len(parts) != NAME_PARTS:
            out["name"] = (False, f"不是 {NAME_PARTS} 段式（实际 {len(parts)} 段）")
        elif parts[0] != brand:
            out["name"] = (False, f"第一段 {parts[0]!r} ≠ 本行 brand {brand!r}")
        elif parts[1] != lname:
            out["name"] = (False, f"第二段 {parts[1]!r} ≠ 叶子类目名 {lname!r}")
        elif parts[2] not in s.specs_for(leaf):
            out["name"] = (False, f"规格 {parts[2]!r} 不在 "
                                  f"{s.tree.leaf_level2(leaf)} 的词池里")
        else:
            out["name"] = (True, "")
    return out


CLASSES = [
    ("category", "category_id 是叶子类目"),
    ("brand_known", "品牌在 semantics.yaml 声明内"),
    ("whitelist", "品牌 → 类目白名单（通过线 0 违例）"),
    ("price", "价格落在 类目带 ∩ 品牌分档窗口（通过线 0 越界）"),
    ("name", "商品名不可由「修饰词+品牌+类目」还原（通过线 0 命中）"),
]


def run(rows: list[dict], s: SEM.Semantics, limit: int = 10) -> int:
    """跑全量并打报告。返回 FAIL 的类别数。"""
    leaf_of = {cid: leaf for leaf, cid in s.tree.leaf_ids.items()}
    lre = legacy_re(s)
    judged = [(r, judge_row(r, s, leaf_of, lre)) for r in rows]

    print(f"商品 {len(rows):,} 行 · 判据 semantics.yaml"
          f"（{len(s.tree.leaf_paths)} 叶子类目 / {len(s.brands)} 品牌 / "
          f"{sum(len(v) for v in s.brands_for_leaf.values())} 个合法(品牌,叶子)对）\n")

    fails = 0
    for key, title in CLASSES:
        # 只在该行走到这一类时计分：类目不是叶子的行不参与价格判定，把它算进
        # 「价格越界」会让一个缺陷在三个类别里各报一次，数字失去意义。
        scored = [(r, d[key]) for r, d in judged if key in d]
        bad = [(r, note) for r, (ok, note) in scored if not ok]
        verdict = "PASS" if not bad else "FAIL"
        fails += verdict == "FAIL"
        print(f"{MARK[verdict]} {title}")
        print(f"        参与判定 {len(scored):,} 行 · 违例 {len(bad):,}"
              f"（{len(bad) / max(len(scored), 1):.1%}）")
        for r, note in bad[:limit]:
            print(f"          #{r['product_id']:<6} {r['product_name'][:28]:<30} {note}")
        if len(bad) > limit:
            print(f"          …… 另有 {len(bad) - limit:,} 行（--limit 调）")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="L7 语义配对合理性（品牌/类目/价格）")
    ap.add_argument("--from-csv", type=Path, metavar="DIR",
                    help="读生成器产出的 products.csv 而不是查云上库")
    ap.add_argument("--limit", type=int, default=10, help="每类违例最多列几行")
    ap.add_argument("--selftest", action="store_true", help="判据自测（无云依赖）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    s = SEM.load()
    if a.from_csv:
        rows = load_products(a.from_csv)
        where = f"生成器产出 {a.from_csv}"
    else:
        import athena
        rows = load_products(None, athena.Client())
        where = "Athena 现行库"
    print(f"=== L7 语义配对合理性 · {where} ===\n")
    fails = run(rows, s, a.limit)

    if fails:
        print(f"\nL7：{fails} 类 FAIL")
        if not a.from_csv:
            print("  云上商品是 v1 的 200 行（Faker 随机配对），项目已决定不重灌，"
                  "所以这些 FAIL 是对现状的记录。\n"
                  "  生成侧的同一套判据：verify_semantics.py --from-csv <生成目录>，"
                  "更快的一层在 scripts/gen/selftest_closures.py（L0 会跑）。")
    else:
        print("\nL7：全部通过 ✅（白名单违例 0、价格越界 0、模板命中 0）")
    return 0                    # 诊断工具，不是闸门。理由同 verify_literals.py


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """喂构造行给 judge_row，证明每一类判据真的在测那件事。

    这些用例分两组：**任务书给的 D-03 抽样实例**（照抄，一个不改），以及本项目修完
    生成器后的产出形态。前一组必须全红、后一组必须全绿——只验一侧的自测证明不了
    判据有区分力。
    """
    s = SEM.load()
    leaf_of = {cid: leaf for leaf, cid in s.tree.leaf_ids.items()}
    lre = legacy_re(s)
    cid = {leaf: c for leaf, c in s.tree.leaf_ids.items()}
    bad = 0

    def check(name: str, brand: str, leaf: str, price: float,
              expect_bad: set[str], why: str) -> None:
        nonlocal bad
        d = judge_row({"product_id": 0, "product_name": name, "brand": brand,
                       "category_id": cid[leaf], "price": price}, s, leaf_of, lre)
        got = {k for k, (ok, _) in d.items() if not ok}
        if got == expect_bad:
            print(f"  ok  {why}")
        else:
            bad += 1
            print(f"  FAIL {why}\n       期望违例 {sorted(expect_bad)}，实际 {sorted(got)}"
                  f"\n       明细 {d}")

    print("---- 任务书里的 D-03 抽样实例（必须全部判红）----")
    # 逐条照抄任务书 63–77 行。价格保留原值，类目取实例里写的那个。
    check("热卖戴森 电视", "戴森", "家电>大家电>电视", 431.47,
          {"whitelist", "price", "name"}, "热卖戴森 电视 → 该品牌不经营此品类")
    check("优质好孩子 婴儿纸尿裤", "好孩子", "母婴>纸尿裤>婴儿纸尿裤", 6583.85,
          {"whitelist", "price", "name"}, "优质好孩子 婴儿纸尿裤 6583.85 → 价格与品类严重不符")
    check("热卖完美日记 女士香水", "完美日记", "美妆>香水>女士香水", 1430.34,
          {"whitelist", "price", "name"}, "热卖完美日记 女士香水 1430.34 → 平价品牌与价位不符")
    check("优质巴拉巴拉 儿童奶粉", "巴拉巴拉", "母婴>奶粉>儿童奶粉", 757.48,
          {"whitelist", "price", "name"}, "优质巴拉巴拉 儿童奶粉 → 童装品牌不经营奶粉")
    # 任务书这一条只标了「随机配对」没标具体错在哪。三类判据全红，而第三类是**顺带
    # 查出来的**：戴森是 premium，电热水壶带 [49, 699] 的 premium 窗口是 [342, 699]，
    # 156.44 低于下界。也就是说「平价品牌卖高价」的反面——高端品牌挂平价——同一套
    # 分档窗口一起拦住了，不需要另写一条判据。写这条断言时我先按「只有白名单和名字错」
    # 填了期望，是判据把我纠正过来的。
    check("优质戴森 电热水壶", "戴森", "家电>小家电>电热水壶", 156.44,
          {"whitelist", "price", "name"},
          "优质戴森 电热水壶 156.44 → 品类越界 + 名字模板 + 高端品牌挂平价")

    print("\n---- 任务书里的价格脱钩实例（只有价格错）----")
    # 「冰箱最低 50.12」：品牌合法、名字这里给成合规形态，只留价格错。
    check("海尔 冰箱 一级能效", "海尔", "家电>大家电>冰箱", 50.12,
          {"price"}, "海尔 冰箱 50.12 → 只有价格红，白名单和名字不受影响")
    # 「大米 3877.72」
    check("金龙鱼 大米 5kg", "金龙鱼", "食品>粮油>大米", 3877.72,
          {"price"}, "金龙鱼 大米 3877.72 → 只有价格红")
    # 「老人机均价 1998」：老人机带 [99,599]，中兴是 budget → [99, 324]
    check("中兴 老人机 大字版", "中兴", "数码>手机>老人机", 1998.0,
          {"price"}, "中兴 老人机 1998 → 只有价格红")

    print("\n---- 修复后的产出形态（必须全绿）----")
    check("戴森 吹风机 静音款", "戴森", "家电>小家电>吹风机", 2400.0,
          set(), "戴森 吹风机 静音款 2400 → 全绿（premium 窗口 [59+45%, 2999]）")
    check("海尔 冰箱 一级能效", "海尔", "家电>大家电>冰箱", 8999.0,
          set(), "海尔 冰箱 8999 → 全绿")
    check("完美日记 口红 限定色", "完美日记", "美妆>彩妆>口红", 89.0,
          set(), "完美日记 口红 89 → 全绿（budget 窗口 [39, 291]）")

    print("\n---- 边界与前置违例 ----")
    # 边界值必须判通过：区间是闭的，判成开区间会让贴边的合法行被误伤。
    lo, hi = s.price_range("海尔", "家电>大家电>冰箱")
    check("海尔 冰箱 变频", "海尔", "家电>大家电>冰箱", lo, set(),
          f"价格恰在下界 {lo:.2f} → 通过（区间闭）")
    check("海尔 冰箱 变频", "海尔", "家电>大家电>冰箱", hi, set(),
          f"价格恰在上界 {hi:.2f} → 通过（区间闭）")
    # 配置没声明的品牌：单列一类，且不再往下判——否则会把它算进白名单违例。
    check("雀巢 咖啡 500ml", "雀巢", "食品>饮料>咖啡", 59.0,
          {"brand_known"}, "配置没声明的品牌 → 单列 brand_known，不混进白名单违例")
    # 品牌名拼错（尾空格）正是这条要拦的形态：判 SKIP 就永远查不出来。
    # 只报 brand_known 一类：judge_row 在前置违例处**提前返回**，不再往下判白名单和
    # 名字。这是有意的——品牌名不可信时，「白名单违例」和「名字第一段 ≠ brand」都只是
    # 同一个根因的回声，一个缺陷报三行会让三个类别的违例数都失去意义。
    check("海尔 冰箱 变频", "海尔 ", "家电>大家电>冰箱", 8999.0,
          {"brand_known"}, "品牌名带尾空格 → 只报 brand_known（不静默跳过，也不连带报三行）")

    # 类目不是叶子：judge_row 应当只返回 category 一类，不去判价格/名字。
    lv2 = next(iter(sorted(s.tree.level2_paths)))
    d = judge_row({"product_id": 0, "product_name": "小米 手机 全网通", "brand": "小米",
                   "category_id": 999999, "price": 1999.0}, s, leaf_of, lre)
    if set(d) == {"category"} and not d["category"][0]:
        print(f"  ok  category_id 不是叶子 → 只报 category 一类，不连带报价格/名字"
              f"（二级类目如 {lv2} 同理）")
    else:
        bad += 1
        print(f"  FAIL 非叶子类目应只报 category，实际 {d}")

    print("\n---- 名字判据不依赖修饰词池的完整性 ----")
    # 池子里没有「超值」这个词，但第一段 ≠ brand 这条仍然拦得住。
    check("超值戴森 吹风机", "戴森", "家电>小家电>吹风机", 2400.0,
          {"name"}, "修饰词池外的前缀「超值」→ 靠「第一段 ≠ brand」拦住")
    check("戴森 吹风机", "戴森", "家电>小家电>吹风机", 2400.0,
          {"name"}, "缺规格段（2 段）→ 判红")
    check("戴森 吹风机 变频", "戴森", "家电>小家电>吹风机", 2400.0,
          {"name"}, "规格「变频」来自大家电词池，不在小家电池里 → 判红")

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
