#!/usr/bin/env python3
"""透传维表的自洽化：把 data/csv 下手写维表的日期、状态、名称改成与数据窗口一致。

## 为什么需要

这 11 张维表是 v1 时期的 CSV，生成器原样透传。它们的日期是在另一个时间轴上造的：
150 张券里 100 张在数据窗口开始前就过期了、运营活动大多在窗口之前、广告活动大多
在窗口之后（排到 2026-10）、状态和日期互相矛盾（已结束半年的券还是 active）。
生成器的事实表一旦要求「推送落在活动档期内」「用券在券有效期内」「归因时广告活动
已上线」，这些维表就必须先自洽。2026-11 数据普查
把这一批一起查了出来。

## 做什么（逐表）

- coupons：名称按类型和面值重写（percentage 的 discount_value 是「减百分之几」，
  20 → 8 折；旧名「满50享2.0折」把折扣读反了）；满减券的门槛不低于面值的 2 倍；
  valid_days 补成 7/15/30；有效期平移到与数据窗口重叠；券码补上。
  status 与 total_quota **不在这里定**——它们取决于实际发放量，由生成器回填
  （见 tables.build_coupons）。
- campaigns：档期平移到窗口附近，status 由档期推出（completed / active / scheduled），
  人为状态（draft / cancelled / paused）保留；描述换成按类型的模板文案；
  召回 / 拉新活动补上目标分群。
- ad_campaigns：档期平移到窗口附近，status 由档期推出（ended / active），paused 保留。
- ad_creatives / user_segments / ab_test_variants：created_at 落在所属对象生效之前。
- ab_tests：已结束的实验补上结论与胜出方案（胜出 = 对照组，结论写明未达显著）。
- banners：名称换成按位置的模板；is_active 由档期推出。

**不变的东西**：主键、行数、外键、各枚举列的取值集合（卡片逐值列了这些取值）。

## 用法

    python3 scripts/gen/dims_coherence.py --check    # 只检查，不改文件；不自洽则退出 1
    python3 scripts/gen/dims_coherence.py --apply    # 改写 data/csv 下的维表（幂等）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import budget  # noqa: E402

CSV = HERE.parent.parent / "data" / "csv"
START = dt.datetime.fromisoformat(budget.DATA_START)
ANCHOR = dt.datetime.fromisoformat(budget.AS_OF)
ANCHOR_END = ANCHOR + dt.timedelta(days=1) - dt.timedelta(seconds=1)
TS = "%Y-%m-%d %H:%M:%S.%f"
ETL = "2026-01-25 02:16:15.000000"          # 维表落库时刻，晚于锚点一天，与其它维表一致


def rd(path):
    with open(CSV / path, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def wr(path, fields, rows):
    with open(CSV / path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def ts(d):
    return d.strftime(TS)


def day(d):
    return d.strftime("%Y-%m-%d")


def window_start(rng, lead_days, tail_days):
    """档期起点：落在 [窗口起点 − lead, 锚点 − tail]，按秒均匀。"""
    lo = START - dt.timedelta(days=lead_days)
    hi = ANCHOR - dt.timedelta(days=tail_days)
    return lo + dt.timedelta(seconds=rng.randint(0, int((hi - lo).total_seconds())))


def fix_coupons():
    f, rows = rd("coupons.csv")
    rng = random.Random(4101)
    for r in rows:
        cid = int(r["coupon_id"])
        typ, v = r["coupon_type"], float(r["discount_value"])
        mn = float(r["min_purchase"] or 0)
        if typ == "fixed_amount":
            mn = max(mn, 2 * v)                      # 满减门槛至少是面值的两倍
            name = f"满{mn:g}减{v:g}"
        elif typ == "percentage":
            name = (f"满{mn:g}享{(100 - v) / 10:g}折" if mn > 0 else f"全场{(100 - v) / 10:g}折")
        else:
            name = f"满{mn:g}包邮" if mn > 0 else "无门槛包邮"
        r["coupon_name"] = name
        r["min_purchase"] = f"{mn:.2f}"
        r["coupon_code"] = f"CP{cid:06d}"
        r["valid_days"] = str([7, 15, 30][cid % 3])
        s = window_start(rng, 25, 5)                 # 起点最早在窗口前 25 天，最短有效期 30 天 ⇒ 必与窗口重叠
        e = s + dt.timedelta(days=rng.choice([30, 45, 60, 90]))
        r["start_date"], r["end_date"] = ts(s), ts(e)
        r["created_at"] = ts(s - dt.timedelta(days=rng.randint(2, 10)))
        # total_quota / status 由生成器按实际发放回填；这里只保证模板值是正数
        r["total_quota"] = str(abs(int(r["total_quota"] or 0)) or 1000)
    wr("coupons.csv", f, rows)


CAMP_DESC = {
    "festival": "节日主题大促，全站满减叠加限时折扣",
    "recall": "面向沉睡用户的回流召回，发放回归专享券",
    "new_user": "新注册用户首单礼包，引导完成首次下单",
    "promotion": "品类专题促销，精选商品限时特价",
}
CAMP_SEG = {"recall": "{3}", "new_user": "{2}"}     # 沉睡用户 / 新注册用户
MANUAL_CAMP = {"draft", "cancelled", "paused"}


def fix_campaigns():
    f, rows = rd("campaigns.csv")
    rng = random.Random(4102)
    for r in rows:
        st = r["status"]
        if st == "draft":
            s = ANCHOR + dt.timedelta(days=rng.randint(7, 40))       # 草稿：尚未开始
        elif st == "scheduled":
            s = ANCHOR + dt.timedelta(days=rng.randint(3, 30))
        else:
            s = window_start(rng, 30, 2)
        e = s + dt.timedelta(days=rng.choice([14, 21, 30, 45, 60]))
        if st not in MANUAL_CAMP and st != "scheduled":
            st = "completed" if e < ANCHOR else "active"
            r["status"] = st
        r["start_date"], r["end_date"] = ts(s), ts(e)
        r["created_at"] = ts(s - dt.timedelta(days=rng.randint(5, 14)))
        r["updated_at"] = ETL
        r["description"] = CAMP_DESC.get(r["campaign_type"], "运营活动")
        r["target_segment_ids"] = CAMP_SEG.get(r["campaign_type"], "")
    wr("campaigns.csv", f, rows)
    return {int(r["campaign_id"]): (r["start_date"], r["end_date"]) for r in rows}


def fix_ad_campaigns():
    f, rows = rd("ad_campaigns.csv")
    rng = random.Random(4103)
    spans = {}
    for r in rows:
        s = window_start(rng, 45, 15).replace(hour=0, minute=0, second=0, microsecond=0)
        e = s + dt.timedelta(days=rng.choice([30, 45, 60, 90, 120]))
        if r["status"] != "paused":
            r["status"] = "ended" if e < ANCHOR else "active"
        r["start_date"], r["end_date"] = day(s), day(e)
        r["created_at"] = ts(s - dt.timedelta(days=rng.randint(1, 5)))
        r["updated_at"] = ETL
        spans[int(r["ad_campaign_id"])] = s
    wr("ad_campaigns.csv", f, rows)
    return spans


def fix_ad_creatives(spans):
    f, rows = rd("ad_creatives.csv")
    rng = random.Random(4104)
    for r in rows:
        r["created_at"] = ts(spans[int(r["ad_campaign_id"])] - dt.timedelta(
            hours=rng.randint(12, 72)))
    wr("ad_creatives.csv", f, rows)


def fix_user_segments():
    f, rows = rd("user_segments.csv")
    for r in rows:
        r["created_at"] = r["updated_at"] = ts(START - dt.timedelta(days=30))
    wr("user_segments.csv", f, rows)


def fix_ab_tests():
    f, rows = rd("ab_tests.csv")
    vf, vrows = rd("ab_test_variants.csv")
    control = {int(v["test_id"]): v["variant_id"] for v in vrows if v["is_control"] == "t"}
    created = {}
    for r in rows:
        if r["status"] == "completed":
            r["winner_variant_id"] = control[int(r["test_id"])]
            r["conclusion"] = "实验组未达到统计显著，保留对照组方案"
        if r["start_date"]:
            created[r["test_id"]] = r["created_at"]
    wr("ab_tests.csv", f, rows)
    for v in vrows:
        if v["test_id"] in created:
            v["created_at"] = created[v["test_id"]]
    wr("ab_test_variants.csv", vf, vrows)


BANNER_POS = {"home_top": "首页焦点图", "home_middle": "首页腰封", "category_top": "分类页顶部",
              "search_top": "搜索页顶部", "cart_bottom": "购物车底部", "detail_bottom": "商品详情页底部",
              "splash": "开屏广告"}


def fix_banners():
    f, rows = rd("banners.csv")
    rng = random.Random(4105)
    for r in rows:
        r["banner_name"] = f"{BANNER_POS.get(r['position'], '运营位')}-{r['banner_id']}"
        s = window_start(rng, 60, 3)
        e = s + dt.timedelta(days=rng.choice([7, 14, 30, 60]))
        r["start_date"], r["end_date"] = ts(s), ts(e)
        r["is_active"] = "t" if s <= ANCHOR_END and e >= ANCHOR else "f"
        r["created_at"] = ts(s - dt.timedelta(days=rng.randint(1, 7)))
        r["updated_at"] = ETL
    wr("banners.csv", f, rows)


def check() -> list[str]:
    """不自洽之处（空列表 = 自洽）。--apply 之后应当为空。"""
    bad = []
    p = lambda s: dt.datetime.fromisoformat(s) if s else None
    _, cps = rd("coupons.csv")
    for r in cps:
        s, e = p(r["start_date"]), p(r["end_date"])
        if not r["valid_days"] or int(r["valid_days"]) <= 0:
            bad.append(f"coupon {r['coupon_id']} valid_days 缺失")
        if e < START:
            bad.append(f"coupon {r['coupon_id']} 在窗口开始前已结束")
        if r["coupon_type"] == "fixed_amount" and float(r["min_purchase"]) < float(r["discount_value"]):
            bad.append(f"coupon {r['coupon_id']} 满减门槛低于面值")
        if r["coupon_type"] == "percentage" and "折" in r["coupon_name"]:
            v = float(r["discount_value"])
            if f"{(100 - v) / 10:g}折" not in r["coupon_name"]:
                bad.append(f"coupon {r['coupon_id']} 名称折扣与面值不符：{r['coupon_name']}")
    _, cs = rd("campaigns.csv")
    for r in cs:
        s, e = p(r["start_date"]), p(r["end_date"])
        if r["status"] == "completed" and e >= ANCHOR:
            bad.append(f"campaign {r['campaign_id']} completed 但未结束")
        if r["status"] == "active" and not (s <= ANCHOR_END and e >= ANCHOR):
            bad.append(f"campaign {r['campaign_id']} active 但不在档期")
    _, acs = rd("ad_campaigns.csv")
    for r in acs:
        s = dt.datetime.fromisoformat(r["start_date"])
        if s > ANCHOR:
            bad.append(f"ad_campaign {r['ad_campaign_id']} 开始于锚点之后")
    _, bs = rd("banners.csv")
    for r in bs:
        s, e = p(r["start_date"]), p(r["end_date"])
        if (r["is_active"] == "t") != (s <= ANCHOR_END and e >= ANCHOR):
            bad.append(f"banner {r['banner_id']} is_active 与档期不符")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.apply:
        fix_coupons()
        fix_campaigns()
        spans = fix_ad_campaigns()
        fix_ad_creatives(spans)
        fix_user_segments()
        fix_ab_tests()
        fix_banners()
    bad = check()
    for b in bad[:20]:
        print("✗", b)
    print(f"维表自洽检查：{'通过 ✅' if not bad else f'{len(bad)} 处不自洽'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
