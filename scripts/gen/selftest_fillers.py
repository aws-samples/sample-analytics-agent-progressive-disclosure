"""fillers.py 自测 —— 验确定性、分布信号、引用完整性。

跑法（仓库根）：
    backend/.venv/bin/python scripts/gen/selftest_fillers.py
退出码 0 = 全过。
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import fillers as F  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  ok   {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- 确定性

def test_determinism():
    print("确定性")
    a = F.enum(F.rng_for(42, "orders", "status"), 1000, ["a", "b", "c"], [5, 3, 2])
    b = F.enum(F.rng_for(42, "orders", "status"), 1000, ["a", "b", "c"], [5, 3, 2])
    check("同 (seed,表,列) → 逐元素相同", bool((a == b).all()))

    c = F.enum(F.rng_for(42, "orders", "payment_method"), 1000, ["a", "b", "c"], [5, 3, 2])
    check("不同列名 → 不同数据", not bool((a == c).all()))

    d = F.enum(F.rng_for(43, "orders", "status"), 1000, ["a", "b", "c"], [5, 3, 2])
    check("不同 seed → 不同数据", not bool((a == d).all()))

    # 列间独立：改一列的抽样量不影响另一列（共用 Generator 时会错位）
    _ = F.int_uniform(F.rng_for(42, "orders", "item_count"), 999_999, 1, 5)
    e = F.enum(F.rng_for(42, "orders", "status"), 1000, ["a", "b", "c"], [5, 3, 2])
    check("列间互不干扰", bool((a == e).all()))


# ---------------------------------------------------------------- 分布信号

def test_pareto():
    """集中度必须落在真实业务的区间内。

    这里刻意用**双边**断言。第一版写的是单边「> 50%」，结果 alpha=1.3 造出
    top20%=95.8% 的退化分布照样通过——排第一的父实体独占 25%，尾部空掉，
    复购/分群/cohort 全部失去意义。分布形状的断言必须卡区间，不能只卡下界。
    """
    print("帕累托集中度（fk_skewed）")
    n, pn = 200_000, 5_000
    fk = F.fk_skewed(F.rng_for(42, "t", "c"), n, 1, pn)
    check("外键落在父区间内", bool(fk.min() >= 1 and fk.max() <= pn))
    counts = np.bincount(fk - 1, minlength=pn)
    desc = np.sort(counts)[::-1]

    # 只断言**与父实体数无关**的量。对数正态权重下这几个都有闭式解：
    #   top-p 份额 = Φ(Φ⁻¹(p) + σ)      Gini = 2Φ(σ/√2) − 1
    # 「单个父实体占比」刻意不断言：它是 exp(σ·Φ⁻¹(1−1/N))/N 量级，随 N 漂移，
    # 拿它当不变量会在换规模时假失败（第一版就踩了这个坑）。
    share20 = desc[: pn // 5].sum() / n
    check("前 20% 占比落在 55%-70%（σ=1.15 理论值 62%）",
          0.55 < share20 < 0.70, f"实测 {share20:.1%}")

    bottom50 = desc[pn // 2:].sum() / n
    check("后 50% 仍有 >10% 份额（尾部非空）", bottom50 > 0.10, f"实测 {bottom50:.1%}")

    srt = np.sort(counts)
    cum = np.cumsum(srt) / srt.sum()
    gini = 1 - 2 * cum.mean() + 1 / pn
    check("Gini 落在 0.50-0.68（理论值 0.58）", 0.50 < gini < 0.68, f"实测 {gini:.3f}")

    check("绝大多数父实体都有子行", (counts > 0).sum() > pn * 0.9,
          f"有子行 {(counts>0).sum()}/{pn}")


def test_children():
    print("父展子（children_per_parent + expand_ids）")
    pn, total = 10_000, 45_000
    cnt = F.children_per_parent(F.rng_for(42, "order_items", "order_id"), pn, total,
                                min_count=1)
    check("每父至少 min_count", bool(cnt.min() >= 1))
    check("子数之和恰为 total", int(cnt.sum()) == total, f"{cnt.sum()} vs {total}")
    ids = F.expand_ids(F.pk(pn, 1), cnt)
    check("展开长度 == total", len(ids) == total)
    check("展开后父 id 单调不减", bool((np.diff(ids) >= 0).all()))


def test_unique_pairs():
    print("唯一对（unique_pairs）")
    n = 50_000
    a, b = F.unique_pairs(F.rng_for(42, "user_follows", "pair"), n, 1, 20_000, 1, 20_000)
    check("数量达标", len(a) == n, f"{len(a)}")
    check("无自环", bool((a != b).all()))
    comp = a.astype(np.int64) * 10 ** 9 + b
    check("无重复边", len(np.unique(comp)) == len(comp))


def test_ts_window():
    print("时间信号（ts_window）")
    start = np.datetime64("2025-10-26", "s")
    days = 91
    ts = F.ts_window(F.rng_for(42, "events", "event_time"), 300_000, start, days, trend=0.35)
    check("落在窗口内",
          bool(ts.min() >= start and ts.max() < start + np.timedelta64(days * 86400, "s")),
          f"{ts.min()} → {ts.max()}")

    d = ts.astype("datetime64[D]")
    dow = ((d.astype(int) + 3) % 7)            # 0=周一
    per = np.array([(dow == k).sum() for k in range(7)])
    check("周末(5,6) 高于工作日均值", per[5:].mean() > per[:5].mean(),
          f"工作日均 {per[:5].mean():.0f} / 周末均 {per[5:].mean():.0f}")

    day_idx = (d - start.astype("datetime64[D]")).astype(int)
    first, last = (day_idx < 30).sum(), (day_idx >= days - 30).sum()
    check("末期量高于初期（趋势）", last > first * 1.1, f"前30天 {first} / 后30天 {last}")

    hours = ts.astype("datetime64[h]").astype(int) % 24
    hcnt = np.bincount(hours, minlength=24)
    check("小时分布非均匀", hcnt.max() > hcnt.min() * 3,
          f"峰 {hcnt.argmax()} 时={hcnt.max()} / 谷 {hcnt.argmin()} 时={hcnt.min()}")


def test_amounts():
    print("金额分布（decimal_lognorm）")
    v = F.decimal_lognorm(F.rng_for(42, "orders", "total_amount"), 200_000, median=180.0,
                          sigma=0.8)
    med, mean = float(np.median(v)), float(v.mean())
    check("中位数接近设定", abs(med - 180) / 180 < 0.05, f"实测中位数 {med:.1f}")
    check("均值 > 中位数（右偏）", mean > med, f"均值 {mean:.1f} > 中位数 {med:.1f}")
    check("两位小数", bool((np.round(v, 2) == v).all()))
    check("非负", bool((v > 0).all()))


def test_text():
    print("文本")
    ids = F.pk(5, 1)
    s = F.serial_text("NO", ids, width=10)
    check("单号格式", s[0] == "NO0000000001", str(s[:2]))
    pool = np.array(["x", "y", "z"], dtype=object)
    got = F.from_pool(F.rng_for(42, "t", "c"), 100, pool)
    check("从池取值合法", set(np.unique(got)).issubset({"x", "y", "z"}))


def test_unique_text():
    """唯一字符串列（combine_unique / unique_digits）。

    这两个函数的**全部价值**是"组合空间比行数小的时候仍然唯一"，所以这里刻意把
    池子选到远小于 n：4×4×4 = 64 种组合放 5,000 行。够不着这个条件的用例（池子
    比行数大）验不出任何东西——旧的纯 combine 在那种规模上本来也基本不撞。
    """
    print("唯一字符串（combine_unique / unique_digits）")
    n = 5_000
    pools = [np.array(list("甲乙丙丁"), dtype=object),
             np.array(list("鱼虾蟹贝"), dtype=object),
             np.array(["", "88", "酱", "_x"], dtype=object)]
    plain = F.combine(F.rng_for(42, "users", "username"), n, *pools)
    check("对照：纯 combine 在 64 种组合上大量重复", len(np.unique(plain)) < n / 50,
          f"{len(np.unique(plain))} 个不同值 / {n} 行")

    out = F.combine_unique(F.rng_for(42, "users", "username"), n, *pools)
    check("combine_unique 全列唯一", len(np.unique(out)) == n,
          f"{len(np.unique(out))} / {n}")
    check("同 (seed,表,列) → 逐元素相同",
          bool((out == F.combine_unique(F.rng_for(42, "users", "username"),
                                        n, *pools)).all()))
    # 「先到先得」：每种组合恰好有一行拿到干净的名字（= 该组合本身）
    combos = set(np.unique(plain).tolist())
    clean = [x for x in out.tolist() if x in combos]
    check("每种组合恰有一行不带后缀（先到先得）",
          len(clean) == len(combos) == len(set(clean)),
          f"干净名 {len(clean)} 行 / 组合 {len(combos)} 种")

    # 空间不够时必须抛错，而不是悄悄产出重复列
    try:
        F.combine_unique(F.rng_for(42, "t", "c"), 20_000,
                         np.array(["a", "b"], dtype=object))
        check("组内序次超出后缀空间应报错", False, "没有抛异常")
    except ValueError as e:
        check("组内序次超出后缀空间时报错", "唯一性无法保证" in str(e), str(e)[:60])

    ph = F.unique_digits(F.rng_for(42, "users", "phone"), 200_000,
                         np.array(["138", "139", "150"], dtype=object), 6)
    check("unique_digits 全列唯一", len(np.unique(ph)) == 200_000,
          f"{len(np.unique(ph))} / 200000")
    check("unique_digits 定长且前缀合法",
          bool((np.char.str_len(ph) == 9).all())
          and set(x[:3] for x in ph.tolist()) <= {"138", "139", "150"})
    try:
        F.unique_digits(F.rng_for(42, "t", "c"), 1_001,
                        np.array(["1"], dtype=object), 3)
        check("行数超过空间应报错", False, "没有抛异常")
    except ValueError as e:
        check("行数超过空间时报错", "放不进" in str(e), str(e)[:60])


def test_by_type_guard():
    print("兜底护栏")
    try:
        F.by_type(F.rng_for(42, "t", "c"), 10, "TIMESTAMP", "placed_at")
        check("时间列兜底应报错", False, "没有抛异常")
    except ValueError:
        check("时间列兜底报错（不允许静默生成无意义时间）", True)


if __name__ == "__main__":
    for fn in (test_determinism, test_pareto, test_children, test_unique_pairs,
               test_ts_window, test_amounts, test_text, test_unique_text,
               test_by_type_guard):
        fn()
    print()
    if FAILED:
        print(f"{FAILED} 项失败")
        sys.exit(1)
    print("全部通过")
