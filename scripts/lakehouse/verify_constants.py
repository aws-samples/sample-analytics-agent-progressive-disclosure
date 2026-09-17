#!/usr/bin/env python3
"""退化列体检：线上库里「整列同一个值」和「整列 NULL」的列，逐列登记。

## 为什么单独有这个检查

已有的几层各自都看不到这件事：

    verify_load.py     CSV 真源 ⟷ Athena，逐列比 count / sum / min / max。
                       CSV 里那一列本来就全是 0，灌得**完全忠实**——它只证明装载没歪，
                       不证明源头有值。
    verify_enums.py     只看「卡片声明了枚举的列」。没声明的列不在它的视野里，
                       而退化列恰恰最容易是没声明的那种。
    verify_literals.py  只看文本列长什么样（占位符 / 词沙拉），不问基数。
    reconcile.py        比表和列的**存在性与类型**，不看列里装的值。
    L1–L5               全是数量关系。一列恒等于 0 满足任何求和恒等式。

于是一个库可以每层全绿，而 `posts.like_count` 1000 行全是 0。实测（2026-08-28）就是这样：
`comment_count` / `share_count` 同样整列 0，`view_count` 正常（136 ~ 99,782），
`post_likes` 有 35,668 行真数据。代价是 `knowledge/metrics/core_metrics.md:242-246`
的 `like_rate` / `comment_rate` 在线上库**恒等于 0.00 且不报错**——正是本项目反复点名的
那类「不报错、数看着合理、结论是反的」。

顺带捞出来的第二条，是这个检查独有的能力：**常量列的那个值可以拿去和卡片对**。
`user_profiles.country` 线上库是 `'中国'`，而 `knowledge/domains/user/user_profiles.md:13`
写「国家，默认 `'China'`」（生成器 `tables.py` 里也是 `F.const(n, "China")`）。
`WHERE country = 'China'` 返回 0 行且不报错。这条 `verify_enums.py` 抓不到——它的
声明只认「`### <列名>` + 枚举表格」，而这个字面量写在**表结构描述**那一栏里，
是上一轮记下的「隐形声明」的第三种形态。

## 为什么是「逐列登记」而不是「一律判红」

线上库装的是 `data/csv`（v1 产的），项目已决定不重灌，所以现在跑必然有一串退化列。
`verify_literals.py` 面对同样的处境选择了**不接进 test_all.sh**（理由写在它的文件头：
默认流程里挂一盏永远红的灯，等于把所有人训练成无视红灯）。这里选了另一条路，因为这里的
红集是**有界且可逐条点名**的（33 + 46 列，不是无界的文本质量），所以能做成一份**清单**：

    清单里的列   → 打印 + 说明它属于哪一类、清除条件是什么，不判红
    清单外的列   → FAIL。这是这个检查的**全部价值**：新出现一个退化列会被抓住
    清单里但已经不退化了 → **也 FAIL**，要求删掉那条

最后那条是刻意的，抄 `selftest_closures.py::ENUM_SUPERSET_OK` 的形态：豁免必须带失效
条件，否则它会变成永久免疫，而"某列被修好了"和"某列还没修"在报告上长得一模一样。

## 三类的边界

1. **`RELOAD_PENDING`（14 列）**——生成器已经产非常量值，清除条件就是重灌。每条点名
   生成器里的产出点，这样"到底是谁该负责"不用重新查一遍。
2. **`DIMS_PASSTHROUGH`（17 列）**——落在 `dims_to_parquet.DIMS` 那 12 张透传表上，
   逐字节来自 v1 CSV，**重灌不会修**。它们不是豁免，是"已知、且当前没有任何代码负责"。
   表名从 `dims_to_parquet.DIMS` **现读**，不在这里复制一份——那份清单变了这里要跟着红。
3. **单行表**——`meta_snapshot` 只有 1 行，"整列同一个值"是同义反复。按规则跳过，
   不进清单：这一类判据在 1 行上没有定义，登记它等于登记一个恒真命题。

## 整列 NULL 那 46 列只钉集合，不逐条裁定

「可空业务列在当前数据下无人填」和「这列本该有值却丢了」需要逐列定口径，46 列的口径
不在这一轮的范围里。所以这里**只钉住集合**：新增一列 → FAIL，某列被填上了 → FAIL
（要求从集合里删）。这样"没被裁定"和"裁定为无害"在输出里不会长得一样——
`ALL_NULL_PINNED` 的注释里明说了它是"登记在册，未逐条裁定"。

已知其中一条是**刻意设计**、不该被裁定成缺陷的：`tmp_campaign_roi_analysis` 的
`attributed_gmv` / `roi` 整列 NULL 是 eval 的陷阱题（见 `schema_manifest.yaml`
「ROI 题掉进 tmp 表 NULL 列」）。

## 判据本身

对每个标量列取 `min(CAST(c AS VARCHAR))` / `max(...)` / `count(c)` / `count(*)`：

    count(*) = 0        → 空表，跳过
    count(c) = 0        → 整列 NULL
    min = max           → 整列同一个值

用 `min/max` 而不是 `count(DISTINCT c)`：两者对"是不是常量"等价（在任何全序下
min=max ⟺ 常量），但 min/max 便宜得多，而且顺带**把那个常量值带回来**——上面
`country` 那条就是靠这个值才发现的。先 `CAST(... AS VARCHAR)` 是为了让 468 个列的
结果凑成同一个形状，可以 UNION ALL 成一条 SQL。已知的失真只有一处：`double` 的
`-0.0` 与 `0.0` 转成不同字符串，于是这种列会被判成"非常量"——方向是漏报不是误报，
且本库没有 double 列。

数组列（8 列，`array(varchar)` / `array(integer)` / `array(bigint)`）不在普查面里：
Trino 的 min/max 对数组的语义是逐元素比较，"常量"在数组上要先定义清楚。**跳过的列会
逐个打印出来**，理由同 `verify_literals.py` 那次的教训：报告上「没被查过」和
「查过没问题」长得一样，缺口就等于不存在。

用法：

    python3 scripts/lakehouse/verify_constants.py            # 全库普查（连云）
    python3 scripts/lakehouse/verify_constants.py -t posts -t payments
    python3 scripts/lakehouse/verify_constants.py --selftest # 分类器自测（无云依赖）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT / "scripts" / "gen"))

# 透传表清单的**真源**是生成器那一侧，这里现读。复制一份的话，那边加一张透传表、
# 这边的分类会静默把它算成"生成器该负责"，而实际上没有任何代码在产它。
from dims_to_parquet import DIMS  # noqa: E402

MAX_PARTS = 200            # 一条 UNION ALL 里最多塞多少列（Athena 查询串有长度上限）


# ---------------------------------------------------------------- 清单

# 生成器已产非常量值，**清除条件 = 全量重灌**。括号里是生成器的产出点。
RELOAD_PENDING = {
    "posts.like_count":
        "三计数器改成从明细回填（tables.py::_post_counters；旧版 like_count = views×0.06 "
        "虚高 3.3 倍，审计 L4.3）。selftest_closures.py 断言 posts.like_count == post_likes "
        "行数，L8 有 like-counter-drift 负例。线上库的 0 是 v1 遗留",
    "posts.comment_count": "同 posts.like_count，从 post_comments 明细 bincount 回填",
    "posts.share_count": "同 posts.like_count，从 post_shares 明细 bincount 回填",
    "payments.refund_amount":
        "tables.py 里是 np.where(refunded, amt, 0.0)——线上库有 145 行 status='refunded' "
        "却退了 0 元，自相矛盾。注意 fin_daily_revenue 的退款口径走 orders.actual_amount "
        "而不是这一列（database/10_derived.sql），所以「退款总额」那道题当前答得出真数",
    "user_attributions.attributed_at":
        "tables.py 里是真点击时刻 click。线上库 350 行全等于灌数那一瞬，"
        "于是任何按归因时间分桶的问题都只有一个桶",
    "user_profiles.country":
        "生成器是 F.const(n, 'China')，而 data/csv 与线上库都是 '中国'——"
        "WHERE country = 'China' 返回 0 行。卡片（user_profiles.md）已按库改成 '中国' "
        "并标了退化列，所以现在不一致的只剩生成器这一侧；重灌会让三方一致，"
        "在那之前这一列的字面量以库为准",
    # 下面 8 列是同一件事：v1 灌数时把审计时间戳写成了装载那一瞬。生成器全部把它们
    # 挂在真实业务事件上（reg / st / vt / ev_time / first / ts_window），一个 F.const 都没有。
    "users.created_at": "生成器挂在注册时刻 reg 上；线上库是 v1 的装载瞬时值",
    "users.updated_at": "同 users.created_at",
    "user_profiles.created_at": "生成器是 F.ts_window（铺满窗口）；线上库是装载瞬时值",
    "user_profiles.updated_at": "同 user_profiles.created_at",
    "user_devices.created_at": "生成器挂在设备首次出现时刻 first 上",
    "sessions.created_at": "生成器挂在会话开始时刻 st 上",
    "page_views.created_at": "生成器挂在浏览时刻 vt 上",
    "events.created_at": "生成器挂在事件时刻 ev_time 上",
}

# 落在 dims_to_parquet.DIMS 的透传表上：逐字节来自 v1 CSV，**重灌不会修**。
# 不是豁免，是"已知、且当前没有任何代码负责产它"。要修得给这些表写 builder，
# 那和 budget.py 那 4 张声明 SUB 却没人兑现的表是同一批活（docs/test-plan.md 有记）。
DIMS_PASSTHROUGH = {
    "ad_campaigns.updated_at", "ad_creatives.status", "ad_creatives.created_at",
    "banners.updated_at", "campaigns.updated_at",
    "categories.created_at", "categories.is_active", "categories.updated_at",
    "channel_daily_costs.currency", "channel_daily_costs.created_at",
    "channels.is_active", "channels.created_at",
    "event_definitions.created_at", "event_definitions.updated_at",
    "user_segments.status", "user_segments.created_at", "user_segments.updated_at",
}

# 整列 NULL：**登记在册，未逐条裁定**（理由见文件头）。只钉集合——新增一列会红，
# 某列被填上了也会红（要求从这里删）。其中 tmp_campaign_roi_analysis 那两列是
# eval 刻意的陷阱题，不该被裁定成缺陷。
ALL_NULL_PINNED = {
    # DIMS 透传表（17）
    "ab_test_variants.config_json", "ab_tests.description", "ab_tests.conclusion",
    "ab_tests.winner_variant_id", "ad_campaigns.target_audience",
    "ad_creatives.creative_format", "ad_creatives.description",
    "ad_creatives.headline", "ad_creatives.content_url", "banners.target_id",
    "channel_daily_costs.creative_id", "channels.description",
    "coupons.coupon_code", "coupons.valid_days", "coupons.applicable_products",
    "event_definitions.owner", "event_definitions.properties_schema",
    # 生成器侧（29）
    "events.referrer", "events.ip_address", "order_items.sku_name",
    "orders.shipping_address", "orders.refund_reason", "orders.cancel_reason",
    "page_views.page_url", "page_views.referrer",
    "payments.failure_reason", "payments.payment_channel",
    "push_notifications.failure_reason", "push_notifications.scheduled_at",
    "subscriptions.payment_id", "subscriptions.cancelled_at",
    "subscriptions.cancel_reason",
    "tmp_campaign_roi_analysis.attributed_gmv", "tmp_campaign_roi_analysis.roi",
    "user_attributions.days_to_install", "user_attributions.ad_campaign_id",
    "user_attributions.creative_id", "user_attributions.tracking_params",
    "user_coupons.coupon_code", "user_coupons.expire_at",
    "user_devices.last_seen_at", "user_devices.first_seen_at",
    "user_messages.related_product_id", "user_messages.related_post_id",
    "user_messages.read_at", "user_segment_members.exited_at",
}


def _check_ledger() -> list[str]:
    """清单自身的一致性。跑在连云之前——这些错不该等一次全库扫描才发现。"""
    bad = []
    for key in sorted(RELOAD_PENDING) + sorted(DIMS_PASSTHROUGH) + sorted(ALL_NULL_PINNED):
        if key.count(".") != 1 or not all(key.split(".")):
            bad.append(f"清单键 {key!r} 不是 `表.列` 形态")
    for key in sorted(DIMS_PASSTHROUGH):
        t = key.split(".")[0]
        if t not in DIMS:
            bad.append(f"DIMS_PASSTHROUGH 里的 {key} 所在表不在 dims_to_parquet.DIMS 里"
                       f"——它不是透传表，归错类了")
    for key in sorted(RELOAD_PENDING):
        t = key.split(".")[0]
        if t in DIMS:
            bad.append(f"RELOAD_PENDING 里的 {key} 落在透传表 {t} 上——重灌不会修它，"
                       f"归错类了（该进 DIMS_PASSTHROUGH，或者给那张表写 builder）")
    overlap = set(RELOAD_PENDING) & DIMS_PASSTHROUGH
    if overlap:
        bad.append(f"两份常量清单重叠：{sorted(overlap)}")
    both = (set(RELOAD_PENDING) | DIMS_PASSTHROUGH) & ALL_NULL_PINNED
    if both:
        bad.append(f"同一列既登记成常量又登记成整列 NULL：{sorted(both)}"
                   f"——非空常量和整列 NULL 互斥，其中一条必然过期")
    return bad


# ---------------------------------------------------------------- Athena 侧

def scalar_columns(client, tables: list[str] | None
                   ) -> tuple[dict[str, list[str]], list[str]]:
    """→ ({表: [标量列]}, [跳过的 表.列（数组）])。"""
    rows = client.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema ORDER BY table_name, ordinal_position")["rows"]
    cols: dict[str, list[str]] = {}
    skipped: list[str] = []
    for t, c, ty in rows:
        if tables and t not in tables:
            continue
        if ty.startswith("array("):
            skipped.append(f"{t}.{c}  ({ty})")
        else:
            cols.setdefault(t, []).append(c)
    return cols, skipped


def census(client, cols: dict[str, list[str]]) -> dict[str, tuple[str, str, int, int]]:
    """→ {「表.列」: (min, max, 非空行数, 总行数)}。分块 UNION ALL 成尽量少的几条 SQL。"""
    parts = [
        f"SELECT '{t}' AS t, '{c}' AS c, min(CAST(\"{c}\" AS VARCHAR)) AS lo, "
        f'max(CAST("{c}" AS VARCHAR)) AS hi, count("{c}") AS nn, count(*) AS n '
        f'FROM "{t}"'
        for t in sorted(cols) for c in cols[t]
    ]
    out: dict[str, tuple[str, str, int, int]] = {}
    for i in range(0, len(parts), MAX_PARTS):
        chunk = parts[i:i + MAX_PARTS]
        for t, c, lo, hi, nn, n in client.execute("\nUNION ALL\n".join(chunk))["rows"]:
            out[f"{t}.{c}"] = (lo, hi, int(nn), int(n))
    return out


# ---------------------------------------------------------------- 分类

def classify(key: str, lo: str, hi: str, nn: int, n: int,
             single_row: bool) -> tuple[str, str]:
    """→ (裁定, 说明)。裁定 ∈ ok / empty / single_row / reload / dims / null_pinned
    / FAIL-const / FAIL-null / FAIL-stale-const / FAIL-stale-null。

    纯函数、不连云，为的是 `--selftest` 能把每条分支都走一遍——判据写在这里，
    上面 main() 只负责取数和打印。
    """
    pinned_const = key in RELOAD_PENDING or key in DIMS_PASSTHROUGH
    pinned_null = key in ALL_NULL_PINNED

    if n == 0:
        return "empty", "空表"
    if nn == 0:
        if pinned_null:
            return "null_pinned", "整列 NULL（登记在册，未逐条裁定）"
        return "FAIL-null", "整列 NULL，且不在 ALL_NULL_PINNED 里"
    # 到这里非空。先处理清单过期：这两条比"是不是退化"更该先报，
    # 因为清单过期时后面的裁定读起来是对的、结论是错的。
    if pinned_null:
        return "FAIL-stale-null", (f"登记成整列 NULL，但实际有 {nn} 个非空值"
                                   f"——从 ALL_NULL_PINNED 删掉这条")
    if lo != hi:
        if key in RELOAD_PENDING:
            return "FAIL-stale-const", ("登记成待重灌的常量列，但它已经不是常量了"
                                        "——重灌已经发生？从 RELOAD_PENDING 删掉这条")
        if key in DIMS_PASSTHROUGH:
            return "FAIL-stale-const", ("登记成透传遗留的常量列，但它已经不是常量了"
                                        "——从 DIMS_PASSTHROUGH 删掉这条")
        return "ok", ""
    # 常量列。
    if single_row:
        return "single_row", "表只有 1 行，「整列同一个值」是同义反复"
    if key in RELOAD_PENDING:
        return "reload", RELOAD_PENDING[key]
    if key in DIMS_PASSTHROUGH:
        return "dims", "v1 CSV 逐字节透传（dims_to_parquet.DIMS），重灌不会修"
    if pinned_const:                                  # 理论上到不了，留着当哨兵
        return "FAIL-const", "清单分类漏了一条分支"
    return "FAIL-const", f"整列同一个值 {lo!r}（{nn} 行），且不在任何清单里"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="线上库退化列（常量 / 整列 NULL）体检")
    ap.add_argument("-t", "--table", action="append", default=[],
                    help="只查这些表（可重复）；默认全部")
    ap.add_argument("--selftest", action="store_true", help="分类器自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    problems = _check_ledger()
    if problems:
        print("清单自身有问题，先修它（这些错不必等一次全库扫描）：\n")
        for p in problems:
            print(f"  - {p}")
        return 1

    import athena
    client = athena.Client()

    cols, skipped_arrays = scalar_columns(client, a.table or None)
    if not cols:
        print("没找到列（-t 写的表名对吗？）")
        return 2
    data = census(client, cols)
    rows_of = {t: max((data[f"{t}.{c}"][3] for c in cols[t]), default=0) for t in cols}

    buckets: dict[str, list[str]] = {}
    for t in sorted(cols):
        for c in cols[t]:
            key = f"{t}.{c}"
            lo, hi, nn, n = data[key]
            verdict, note = classify(key, lo, hi, nn, n, single_row=rows_of[t] <= 1)
            buckets.setdefault(verdict, []).append(f"{key:<44} {note}")

    n_scanned = sum(len(v) for v in cols.values())
    print(f"普查 {len(cols)} 张表 / {n_scanned} 个标量列"
          f"（另有 {len(skipped_arrays)} 个数组列跳过）\n")

    for verdict, title in [
            ("reload", "待重灌（生成器已产非常量值，清除条件=重灌）"),
            ("dims", "透传遗留（v1 CSV 逐字节透传，重灌不会修）"),
            ("null_pinned", "整列 NULL（登记在册，未逐条裁定）"),
            ("single_row", "单行表（判据在 1 行上无定义）"),
            ("empty", "空表")]:
        if buckets.get(verdict):
            print(f"  ── {title}：{len(buckets[verdict])} 列")
            for line in buckets[verdict]:
                print(f"     · {line}")
            print()

    if skipped_arrays:
        print(f"  ── 不在普查面里的数组列：{len(skipped_arrays)}")
        for line in skipped_arrays:
            print(f"     · {line}")
        print()

    fails = [line for v, lines in buckets.items() if v.startswith("FAIL") for line in lines]
    ok = len(buckets.get("ok", []))
    if fails:
        print(f"发现 {len(fails)} 处未登记的退化列 / 过期清单条目 ❌\n")
        for line in fails:
            print(f"  - {line}")
        print("\n改法：确认它该属于哪一类，写进对应清单并注明清除条件；"
              "如果是清单过期，删掉那条。**不要**为了变绿而往清单里加一行了事——"
              "清单的每条都要能说出'谁该修它、什么时候能删'。")
        return 1
    print(f"退化列全部登记在册 ✅  {ok} 列基数正常，"
          f"{len(buckets.get('reload', []))} 待重灌 + "
          f"{len(buckets.get('dims', []))} 透传遗留 + "
          f"{len(buckets.get('null_pinned', []))} 整列 NULL 已登记")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    bad = 0

    def want(tag: str, got: str, expect: str) -> None:
        nonlocal bad
        if got != expect:
            bad += 1
            print(f"  FAIL {tag}：裁定 {got!r}，期望 {expect!r}")

    # 清单自身
    for p in _check_ledger():
        bad += 1
        print(f"  FAIL 清单：{p}")

    C = classify
    # 正常列：min≠max
    want("正常列", C("posts.title", "a", "z", 1000, 1000, False)[0], "ok")
    # 未登记的常量列必须红 —— 这是这个检查的全部价值
    want("未登记常量列", C("orders.status", "paid", "paid", 2000, 2000, False)[0],
         "FAIL-const")
    want("未登记整列 NULL", C("orders.note", "", "", 0, 2000, False)[0], "FAIL-null")
    # 登记过的三类
    want("待重灌", C("posts.like_count", "0", "0", 1000, 1000, False)[0], "reload")
    want("透传遗留", C("categories.is_active", "true", "true", 166, 166, False)[0], "dims")
    want("整列 NULL 已登记", C("events.referrer", "", "", 0, 20000, False)[0],
         "null_pinned")
    # 单行表：同义反复，且**优先于**清单（否则 1 行的表会被登记成"待重灌"）
    want("单行表", C("meta_snapshot.as_of_date", "x", "x", 1, 1, True)[0], "single_row")
    want("空表", C("whatever.col", "", "", 0, 0, False)[0], "empty")
    # 清单过期的两个方向 —— 没有这两条，清单就是永久免疫
    want("常量条目过期（已被修好）",
         C("posts.like_count", "0", "55", 1000, 1000, False)[0], "FAIL-stale-const")
    want("透传条目过期（已被修好）",
         C("categories.is_active", "false", "true", 166, 166, False)[0],
         "FAIL-stale-const")
    want("NULL 条目过期（已被填上）",
         C("events.referrer", "a", "z", 20000, 20000, False)[0], "FAIL-stale-null")
    # 整列 NULL 的判定不许被 min/max 的取值干扰：nn=0 时 lo/hi 是 NULL，
    # 驱动那边会把它变成空串或 None，两种都得走同一条分支。
    want("整列 NULL（lo/hi 是 None）",
         C("events.referrer", None, None, 0, 20000, False)[0], "null_pinned")

    # 常量清单与整列 NULL 清单不许判成同一类
    if C("posts.like_count", "0", "0", 1000, 1000, False)[0] == \
            C("events.referrer", "", "", 0, 20000, False)[0]:
        bad += 1
        print("  FAIL 非空常量列和整列 NULL 列被判成了同一类")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  清单：{len(RELOAD_PENDING)} 待重灌 + {len(DIMS_PASSTHROUGH)} 透传遗留 "
          f"+ {len(ALL_NULL_PINNED)} 整列 NULL，键形态与归类互斥性一致")
    print("  分类器 13 项：正常列、未登记常量、未登记 NULL、三类登记、单行表、空表、"
          "三种清单过期、None 形态、两类不混淆")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
