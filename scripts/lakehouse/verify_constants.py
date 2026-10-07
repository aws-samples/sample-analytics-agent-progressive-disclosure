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
2026-08-28 实测 `user_profiles.country` 线上库是 `'中国'`，而
`knowledge/domains/user/user_profiles.md:13` 写「国家，默认 `'China'`」（生成器
`tables.py` 里也是 `F.const(n, "China")`），于是 `WHERE country = 'China'` 返回 0 行
且不报错。这条 `verify_enums.py` 抓不到——它的声明只认「`### <列名>` + 枚举表格」，
而这个字面量写在**表结构描述**那一栏里，是上一轮记下的「隐形声明」的第三种形态。
（2026-09-01 全量重灌后这一条已经对上：库里现在是 `'China'`。列仍然是常量，
所以它没有被删掉，而是移进了 `BY_DESIGN_CONST`。）

## 为什么是「逐列登记」而不是「一律判红」

这份清单是 2026-08-28 在 v1 数据（`data/csv`）上建起来的，那时线上库必然有一串退化列。
2026-09-01 全量重灌之后大部分条目按契约被清掉了，但清单机制照旧——透传维表、刻意常量、
以及重灌本身引入的新退化列都还需要逐条登记。
`verify_literals.py` 面对同样的处境选择了**不接进 test_all.sh**（理由写在它的文件头：
默认流程里挂一盏永远红的灯，等于把所有人训练成无视红灯）。这里选了另一条路，因为这里的
红集是**有界且可逐条点名**的（17 + 24 列，不是无界的文本质量），所以能做成一份**清单**：

    清单里的列   → 打印 + 说明它属于哪一类、清除条件是什么，不判红
    清单外的列   → FAIL。这是这个检查的**全部价值**：新出现一个退化列会被抓住
    清单里但已经不退化了 → **也 FAIL**，要求删掉那条

最后那条是刻意的，抄 `selftest_closures.py::ENUM_SUPERSET_OK` 的形态：豁免必须带失效
条件，否则它会变成永久免疫，而"某列被修好了"和"某列还没修"在报告上长得一模一样。

## 四类的边界

1. **`RELOAD_PENDING`（当前 0 列）**——生成器已经产非常量值，清除条件就是"三条 arm 都
   读到重灌后的数据"。每条点名生成器里的产出点，这样"到底是谁该负责"不用重新查一遍。

   清除条件的措辞是 2026-09-02 改的，因为原来写成"重灌"不够。那天的重灌只产出了新的
   `s3://analytics-agent-raw/parquet/`（Redshift 从它 COPY），S3 Tables 那份 Iceberg
   没跟着走，于是同一列在 Redshift 上已是非常量、在 Athena / DuckDB 上还是假常量。
   这个脚本只查 Athena，所以它当时仍然看到 5 条待办——而那不是清单过期，是数据真的
   只修了三分之一。补灌走 `load_parquet.py --apply`，之后 5 条按契约全部删掉。
   照出这类差异的唯一判据是 `scripts/bench/query_correctness.py --values`：行数与金额
   在三条 arm 上完全相等，文本列没有任何指标覆盖。
2. **`DIMS_PASSTHROUGH`（15 列）**——落在 `dims_to_parquet.DIMS` 那 11 张透传表上，
   逐字节来自 v1 CSV，**重灌不会修**。它们不是豁免，是"已知、且当前没有任何代码负责"。
   表名从 `dims_to_parquet.DIMS` **现读**，不在这里复制一份——那份清单变了这里要跟着红。
3. **`BY_DESIGN_CONST`（2 列）**——生成器**刻意**产常量，重灌之后仍是常量，而它是对的。
   这一类是 2026-08-31 加的，因为前三类都装不下 `channel_daily_costs.currency`：
   本库只有人民币一种计价，那一列恒为 'CNY' 是口径而不是缺陷。归进 RELOAD_PENDING
   会在重灌后变成一条永远清不掉的待办；归进 DIMS_PASSTHROUGH 是假话——那张表已经有
   builder、不再透传（P0-5）。失效条件照样带着：它哪天不是常量了仍然红。
4. **单行表**——`meta_snapshot` 只有 1 行，"整列同一个值"是同义反复。按规则跳过，
   不进清单：这一类判据在 1 行上没有定义，登记它等于登记一个恒真命题。

## 整列 NULL 那 24 列只钉集合，不逐条裁定

「可空业务列在当前数据下无人填」和「这列本该有值却丢了」需要逐列定口径，24 列的口径
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

数组列（8 列，`array(varchar)` / `array(integer)` / `array(bigint)`）走另一套判据。
上面那套对它们没意义：Trino 的 min/max 对数组是逐元素比较，"常量"在数组上要先定义清楚。
所以这里只判**一件**能定义清楚、且恰好是数组列真实退化形态的事——「这一列一个元素都没有」：

    count(*) = 0                 → 空表，跳过
    count(c) = 0                 → 整列 NULL（和标量列同一类，共用 ALL_NULL_PINNED）
    sum(cardinality(c)) = 0      → 整列空数组：行不是 NULL，但每行都是 `[]`

第三条是 P1-10 的形态：`posts.media_urls` / `tags` / `product_ids` 在新生成器里一度是
`F.const(n, [])` ×3，而 `knowledge/relationships.md:61` 声明了 products ↔ posts 的
N:N、`domains/social/posts.md` 还有一整段 `CROSS JOIN UNNEST(product_ids)` 的示例查询，
在那份数据上恒返回 0 行。这一整轮里**没有任何一层会红**：`verify_load.py` 比的是
CSV ⟷ Athena（源头本来就是空数组，灌得完全忠实）、`verify_literals.py` 只看字符串列、
`verify_enums.py` 只看声明了枚举的列、L1–L5 全是数量关系。而这个脚本此前**跳过所有
数组列、只把列名打印出来**——报告上「没被查过」和「查过没问题」长得一样，缺口就等于
不存在（这正是 `verify_literals.py` 那次记下的教训，当时只学到一半）。

刻意**不**判「长度恒为 k」或元素级的常量：那要先定义数组上的"常量"，而真实缺陷是
「整列空」，不是「每行都两个元素」。判据宽一点、但说得清，比判据看着严、语义含混好。

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
# **2026-09-01 全量重灌把原来那 15 条全部清掉了。** 14 条在重灌那一刻变成非常量
# （这正是这一桶的契约：清除条件达成就要求删掉那条），第 15 条 `user_profiles.country`
# 重灌后仍是常量但值已经对上了（库里现在是 'China'，与生成器和卡片一致），所以它移进了
# BY_DESIGN_CONST——"只造国内用户"是口径，不是待办。
#
# **2026-09-02 又清掉 5 条，这一桶现在是空的。** 那 5 条是重灌之后才第一次被这个检查
# 看见的（重灌前它们整列 NULL、钉在 ALL_NULL_PINNED 里，重灌后变成一个非 NULL 的常量
# ——空列会让人停下来查，假常量不会）：`orders.cancel_reason` / `.refund_reason` /
# `.shipping_address`、`push_notifications.failure_reason`、
# `user_attributions.tracking_params`。基数现在分别是 8 / 8 / 10 / 6 / 11。
#
# 清除它们的那次装载**不是**又一次全量重灌，而是 `load_parquet.py --apply`：09-02 的
# 重灌只产出了新的 `parquet/`（Redshift 从它 COPY），S3 Tables 那份 Iceberg 落后一次，
# 于是同一列在 Redshift 上已是非常量、在 Athena / DuckDB 上还是假常量。**三条 arm 不同步
# 这件事，行数和金额那一层看不见**——`correctness.py --arms-only` 把 35 张表的行数、
# 数值求和、时间边界、布尔计数全比过，逐位相等，因为同一个 seed 下只有这 5 列的取值
# 规则变了。唯一照出来的判据是 `query_correctness.py --values`（文本列的 MIN/MAX）。
# 教训记在这儿而不只是 commit message 里：这一桶的清除条件写「重灌」是不够的，得写
# 「三条 arm 都读到重灌后的数据」。
#
# 空字典是这一桶的正常状态，不是"还没写"。加回条目的门槛没变：每条要说得出谁该修它、
# 什么时候能删。
RELOAD_PENDING: dict[str, str] = {}

# 生成器**刻意**产常量，重灌之后仍是常量——这一类不是待办，是口径（理由见文件头第 3 类）。
# 失效条件不变：哪天不是常量了照样红，要求删掉那条。
BY_DESIGN_CONST = {
    "channel_daily_costs.currency":
        "tables.py::build_channel_daily_costs 里是 F.const(n, 'CNY')。本库只有人民币一种"
        "计价，成本表不做多币种——`knowledge/metrics/governed_metrics.md` 的 CAC / ROI "
        "口径全部按元直接相加，引入第二种币种要先加汇率表，那是另一个决策",
    "user_profiles.country":
        "tables.py 里是 F.const(n, 'China')。本库只造国内用户，这一列恒为一个值是口径。"
        "2026-09-01 从 RELOAD_PENDING 移过来：那条登记的问题是「生成器和卡片都写 'China'，"
        "而线上库是 '中国'，WHERE country = 'China' 返回 0 行」——重灌让三方一致，"
        "问题消失，但列仍然是常量，所以它属于这一类而不是被删掉。失效条件：哪天真造"
        "境外用户，这里会红，届时删掉这条并同步 user_profiles.md:13",
}

# 落在 dims_to_parquet.DIMS 的透传表上：逐字节来自 v1 CSV，**重灌不会修**。
# 不是豁免，是"已知、且当前没有任何代码负责产它"。要修得给这些表写 builder，
# 那和 budget.py 那 4 张声明 SUB 却没人兑现的表是同一批活（docs/test-plan.md 有记）。
DIMS_PASSTHROUGH = {
    "ad_campaigns.updated_at", "ad_creatives.status", "ad_creatives.created_at",
    "banners.updated_at", "campaigns.updated_at",
    "categories.created_at", "categories.is_active", "categories.updated_at",
    # channel_daily_costs.currency / .created_at 已移出：P0-5 之后这张表不再透传
    # （已从 dims_to_parquet.DIMS 移除），留在这里 _check_ledger() 会直接红。
    # currency → BY_DESIGN_CONST（刻意常量）；created_at 曾进 RELOAD_PENDING，
    # 2026-09-01 重灌后它已按行落在当天 23:30、不再是常量，那条按契约删掉了。
    "channels.is_active", "channels.created_at",
    "event_definitions.created_at", "event_definitions.updated_at",
    "user_segments.status", "user_segments.created_at", "user_segments.updated_at",
}

# 整列 NULL：**登记在册，未逐条裁定**（理由见文件头）。只钉集合——新增一列会红，
# 某列被填上了也会红（要求从这里删）。其中 tmp_campaign_roi_analysis 那两列是
# eval 刻意的陷阱题，不该被裁定成缺陷。
ALL_NULL_PINNED = {
    # DIMS 透传表（16）
    "ab_test_variants.config_json", "ab_tests.description", "ab_tests.conclusion",
    "ab_tests.winner_variant_id", "ad_campaigns.target_audience",
    "ad_creatives.creative_format", "ad_creatives.description",
    "ad_creatives.headline", "ad_creatives.content_url", "banners.target_id",
    "channels.description",
    "coupons.coupon_code", "coupons.valid_days", "coupons.applicable_products",
    "event_definitions.owner", "event_definitions.properties_schema",
    # 这两列是**数组列**，2026-08-31 加上数组普查那一刻才第一次进到判据里（此前
    # 所有数组列一律跳过，见文件头）。两张表都在 DIMS 里透传，v1 CSV 那两列本来就空，
    # 重灌不会修。语义是「定向到哪些人群包」，整列 NULL 意味着卡片里"按人群包看实验/
    # 活动"这类问法在库上恒返回空——和 push_notifications 那两列同一种形态。
    "campaigns.target_segment_ids", "ab_tests.target_segment_ids",
    # 生成器侧（6）
    #
    # **原来这里有 30 条，2026-09-01 全量重灌后删掉了 24 条。** 那 24 条一起红正是重灌
    # 成功的判据（这一桶的契约：「某列被填上了也会红，要求从这里删」），2026-08-31 拿
    # scale=20 产物做的预测是"只有 4 条重灌后仍为空"，实测完全对上。删掉的是
    # events.referrer / ip_address、order_items.sku_name、orders.shipping_address /
    # cancel_reason / refund_reason、page_views.page_url / referrer、
    # payments.payment_channel、push_notifications.scheduled_at / failure_reason、
    # subscriptions.payment_id、user_attributions.days_to_install / ad_campaign_id /
    # creative_id / tracking_params、user_coupons.coupon_code / expire_at、
    # user_devices.first_seen_at / last_seen_at、user_messages.related_post_id /
    # related_product_id / read_at、user_segment_members.exited_at。
    # 其中 5 条不是"被正确填上"，是**从空的变成了假的**——tracking_params、
    # orders.shipping_address / cancel_reason / refund_reason、
    # push_notifications.failure_reason 各自成了一个非 NULL 的常量，已全部移进 RELOAD_PENDING。
    # 这一步值得记住：这一桶清空的时候要逐条看它去了哪儿，"不再整列 NULL"不等于"好了"。
    # 连带改掉的卡片见 docs/test-plan.md「重灌后要动的地方」——payment_channel /
    # related_post_id / related_product_id / push failure_reason 这四列的「整列 NULL」
    # 提示会从真话变成假话，而卡片是 agent 唯一的口径来源，它不会自己去库里复核。
    #
    # 剩下这 4 条生成器侧的都是刻意的（后两条是卡片里的教学点，见
    # tables.py::build_subscriptions 的注释），加 tmp_campaign_roi_analysis 那 2 条
    # 派生列（eval 的陷阱题）不受重灌影响。
    #
    # channel_daily_costs.creative_id 从上面那组挪下来：P0-5 之后这张表有 builder 了，
    # 而 builder 里写的是 F.const(n, None)——成本按活动汇总、不拆到素材，是刻意的整列
    # NULL。这一组的语义是"登记在册、未逐条裁定"，所以它待在这里、不进 BY_DESIGN_CONST
    # （那一类只管非空常量；整列 NULL 走不到 min=max 那条分支）。
    "channel_daily_costs.creative_id",
    "payments.failure_reason",
    "subscriptions.cancelled_at", "subscriptions.cancel_reason",
    "tmp_campaign_roi_analysis.attributed_gmv", "tmp_campaign_roi_analysis.roi",
}

# 整列空数组（行非 NULL，但每行都是 `[]`）。**当前一列都没有**，这是刻意的：
# 2026-08-31 加数组普查时实测 8 个数组列里 6 列有真元素、2 列整列 NULL（已进
# ALL_NULL_PINNED），没有一列是空数组。空字典不是占位——它是一句可判真假的话：
# "此刻没有任何数组列该被豁免"。哪天真要往这里加一条，写法同 RELOAD_PENDING：
# 值是理由 + 清除条件，而不是"暂时先放着"。
ARRAY_EMPTY_PINNED: dict[str, str] = {}


def _check_ledger() -> list[str]:
    """清单自身的一致性。跑在连云之前——这些错不该等一次全库扫描才发现。"""
    bad = []
    for key in (sorted(RELOAD_PENDING) + sorted(DIMS_PASSTHROUGH)
                + sorted(BY_DESIGN_CONST) + sorted(ALL_NULL_PINNED)
                + sorted(ARRAY_EMPTY_PINNED)):
        if key.count(".") != 1 or not all(key.split(".")):
            bad.append(f"清单键 {key!r} 不是 `表.列` 形态")
    for key in sorted(DIMS_PASSTHROUGH):
        t = key.split(".")[0]
        if t not in DIMS:
            bad.append(f"DIMS_PASSTHROUGH 里的 {key} 所在表不在 dims_to_parquet.DIMS 里"
                       f"——它不是透传表，归错类了")
    # RELOAD_PENDING 和 BY_DESIGN_CONST 都是"生成器负责"的类别，落在透传表上就是自相矛盾：
    # 透传表没有 builder，谁都不会去产那一列。
    for label, ledger in (("RELOAD_PENDING", RELOAD_PENDING),
                          ("BY_DESIGN_CONST", BY_DESIGN_CONST)):
        for key in sorted(ledger):
            t = key.split(".")[0]
            if t in DIMS:
                bad.append(f"{label} 里的 {key} 落在透传表 {t} 上——那张表没有 builder，"
                           f"生成器不产这一列，归错类了（该进 DIMS_PASSTHROUGH，"
                           f"或者给那张表写 builder 并从 DIMS 移出）")
    consts = [("RELOAD_PENDING", set(RELOAD_PENDING)),
              ("DIMS_PASSTHROUGH", set(DIMS_PASSTHROUGH)),
              ("BY_DESIGN_CONST", set(BY_DESIGN_CONST))]
    for i, (na, a) in enumerate(consts):
        for nb, b in consts[i + 1:]:
            if a & b:
                bad.append(f"{na} 与 {nb} 重叠：{sorted(a & b)}")
    both = (set(RELOAD_PENDING) | set(DIMS_PASSTHROUGH) | set(BY_DESIGN_CONST)) \
        & ALL_NULL_PINNED
    if both:
        bad.append(f"同一列既登记成常量又登记成整列 NULL：{sorted(both)}"
                   f"——非空常量和整列 NULL 互斥，其中一条必然过期")
    # 「整列空数组」的前提是行**不是** NULL，所以它和整列 NULL 同样互斥。
    both_arr = set(ARRAY_EMPTY_PINNED) & ALL_NULL_PINNED
    if both_arr:
        bad.append(f"同一列既登记成整列空数组又登记成整列 NULL：{sorted(both_arr)}"
                   f"——空数组的行是非 NULL 的，两者互斥，其中一条必然过期")
    return bad


# ---------------------------------------------------------------- Athena 侧

def split_columns(client, tables: list[str] | None
                  ) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]]]:
    """→ ({表: [标量列]}, {表: [(数组列, 类型)]})。两套判据、两条取数路径。"""
    rows = client.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema ORDER BY table_name, ordinal_position")["rows"]
    cols: dict[str, list[str]] = {}
    arrays: dict[str, list[tuple[str, str]]] = {}
    for t, c, ty in rows:
        if tables and t not in tables:
            continue
        if ty.startswith("array("):
            arrays.setdefault(t, []).append((c, ty))
        else:
            cols.setdefault(t, []).append(c)
    return cols, arrays


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


def array_census(client, arrays: dict[str, list[tuple[str, str]]]
                 ) -> dict[str, tuple[int, int, int, int]]:
    """→ {「表.列」: (元素总数, 最长, 非空行数, 总行数)}。判据见文件头的数组那一节。

    `sum(cardinality(c))` 而不是 `max(...) = 0`：两者对"一个元素都没有"等价，但和数
    顺带把「这一列到底有多少个元素」带回报告里——`posts.product_ids` 那条 N:N 关系
    兑现没兑现，看的就是这个数。`max` 一起取，因为"最长 1 个元素"和"最长 9 个"在
    媒体列上是两种不同的数据形态，打印出来比只说"非空"有用。
    """
    parts = [
        f"SELECT '{t}' AS t, '{c}' AS c, "
        f'coalesce(sum(cardinality("{c}")), 0) AS tot, '
        f'coalesce(max(cardinality("{c}")), 0) AS mx, '
        f'count("{c}") AS nn, count(*) AS n FROM "{t}"'
        for t in sorted(arrays) for c, _ in arrays[t]
    ]
    out: dict[str, tuple[int, int, int, int]] = {}
    for i in range(0, len(parts), MAX_PARTS):
        chunk = parts[i:i + MAX_PARTS]
        for t, c, tot, mx, nn, n in client.execute("\nUNION ALL\n".join(chunk))["rows"]:
            out[f"{t}.{c}"] = (int(tot), int(mx), int(nn), int(n))
    return out


# ---------------------------------------------------------------- 分类

def classify(key: str, lo: str, hi: str, nn: int, n: int,
             single_row: bool) -> tuple[str, str]:
    """→ (裁定, 说明)。裁定 ∈ ok / empty / single_row / reload / dims / by_design
    / null_pinned / FAIL-const / FAIL-null / FAIL-stale-const / FAIL-stale-null。

    纯函数、不连云，为的是 `--selftest` 能把每条分支都走一遍——判据写在这里，
    上面 main() 只负责取数和打印。
    """
    pinned_const = (key in RELOAD_PENDING or key in DIMS_PASSTHROUGH
                    or key in BY_DESIGN_CONST)
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
        if key in BY_DESIGN_CONST:
            return "FAIL-stale-const", ("登记成刻意的常量列，但它已经不是常量了"
                                        "——口径变了？从 BY_DESIGN_CONST 删掉这条")
        return "ok", ""
    # 常量列。
    if single_row:
        return "single_row", "表只有 1 行，「整列同一个值」是同义反复"
    if key in RELOAD_PENDING:
        return "reload", RELOAD_PENDING[key]
    if key in DIMS_PASSTHROUGH:
        return "dims", "v1 CSV 逐字节透传（dims_to_parquet.DIMS），重灌不会修"
    if key in BY_DESIGN_CONST:
        return "by_design", BY_DESIGN_CONST[key]
    if pinned_const:                                  # 理论上到不了，留着当哨兵
        return "FAIL-const", "清单分类漏了一条分支"
    return "FAIL-const", f"整列同一个值 {lo!r}（{nn} 行），且不在任何清单里"


def classify_array(key: str, tot: int, mx: int, nn: int, n: int) -> tuple[str, str]:
    """数组列的裁定。→ (裁定, 说明)，裁定 ∈ array_ok / empty / null_pinned / array_empty
    / FAIL-null / FAIL-stale-null / FAIL-array-empty / FAIL-stale-array-empty。

    正常的数组列走 `array_ok` 而不是和标量列共用 `ok`：`ok` 那一桶不打印，而数组列**要**
    逐列打印出来（元素总数带在说明里）。这一条是上一轮的直接教训——数组列此前是"跳过并
    打印列名"，缺口反而是唯一被看见的部分；现在换成"查过并打印结论"，报告的行数不变、
    每行的含义变了。

    和 `classify` 一样是纯函数（`--selftest` 要走遍分支），判据只有文件头那三条。
    刻意**没有** single_row 分支：这里唯一的常量类判据是"一个元素都没有"，它在 1 行上
    也是一句真话（那一行确实什么都没有），不像 min=max 在 1 行上是同义反复。
    """
    pinned_null = key in ALL_NULL_PINNED
    pinned_empty = key in ARRAY_EMPTY_PINNED

    if n == 0:
        return "empty", "空表"
    if nn == 0:
        if pinned_null:
            return "null_pinned", "整列 NULL（登记在册，未逐条裁定）"
        return "FAIL-null", "整列 NULL，且不在 ALL_NULL_PINNED 里"
    # 清单过期先报，理由同 classify：过期时后面那句裁定读起来是对的、结论是错的。
    if pinned_null:
        return "FAIL-stale-null", (f"登记成整列 NULL，但实际有 {nn} 个非空数组"
                                   f"——从 ALL_NULL_PINNED 删掉这条")
    if tot == 0:
        if pinned_empty:
            return "array_empty", ARRAY_EMPTY_PINNED[key]
        return "FAIL-array-empty", (
            f"整列空数组（{nn} 行非 NULL，元素总数 0），且不在 ARRAY_EMPTY_PINNED 里。"
            f"UNNEST 这一列的查询全部恒返回 0 行，而装载、EXPLAIN、枚举、求和恒等式"
            f"没有一层会红")
    if pinned_empty:
        return "FAIL-stale-array-empty", (f"登记成整列空数组，但实际有 {tot} 个元素"
                                          f"——从 ARRAY_EMPTY_PINNED 删掉这条")
    return "array_ok", f"{tot} 个元素 / {nn} 行，最长 {mx}"


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

    cols, arrays = split_columns(client, a.table or None)
    if not cols and not arrays:
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

    adata = array_census(client, arrays)
    for t in sorted(arrays):
        for c, ty in arrays[t]:
            key = f"{t}.{c}"
            verdict, note = classify_array(key, *adata[key])
            buckets.setdefault(verdict, []).append(f"{key:<44} {ty:<15} {note}")

    n_scanned = sum(len(v) for v in cols.values())
    n_arr = sum(len(v) for v in arrays.values())
    print(f"普查 {len(set(cols) | set(arrays))} 张表 / {n_scanned} 个标量列 + "
          f"{n_arr} 个数组列\n")

    for verdict, title in [
            ("reload", "待重灌（生成器已产非常量值，清除条件=重灌）"),
            ("dims", "透传遗留（v1 CSV 逐字节透传，重灌不会修）"),
            ("by_design", "刻意的常量（口径，不是待办；失效条件仍在）"),
            ("null_pinned", "整列 NULL（登记在册，未逐条裁定）"),
            ("array_empty", "整列空数组（登记在册）"),
            ("array_ok", "数组列（元素总数 / 非空行 / 最长）"),
            ("single_row", "单行表（判据在 1 行上无定义）"),
            ("empty", "空表")]:
        if buckets.get(verdict):
            print(f"  ── {title}：{len(buckets[verdict])} 列")
            for line in buckets[verdict]:
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
    print(f"退化列全部登记在册 ✅  {ok} 个标量列基数正常 + "
          f"{len(buckets.get('array_ok', []))} 个数组列有真元素，"
          f"{len(buckets.get('reload', []))} 待重灌 + "
          f"{len(buckets.get('dims', []))} 透传遗留 + "
          f"{len(buckets.get('by_design', []))} 刻意常量 + "
          f"{len(buckets.get('null_pinned', []))} 整列 NULL + "
          f"{len(buckets.get('array_empty', []))} 整列空数组 已登记")
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
    # 登记过的三类。
    # `RELOAD_PENDING` 这一桶 2026-09-02 起是空的（5 条按契约全删了），所以这两条
    # `reload` / `FAIL-stale-const` 用**临时插进去的夹具键**测，跑完还原。
    #
    # 别的桶都拿清单里真实存在的键当夹具（2026-09-01 重灌后 posts.like_count /
    # events.referrer 已删，拿它们会让自测测到空处），这里不能照办——桶是空的。
    # 而空桶恰恰是这两条分支最容易烂掉又没人发现的时候：没有夹具就没有断言，
    # 下次真往桶里加一条时，分类器认不认它已经没人验过了。
    FIXTURE = "orders.__selftest_reload__"
    RELOAD_PENDING[FIXTURE] = "分类器自测夹具，不是真待办"
    try:
        want("待重灌", C(FIXTURE, "同一个值", "同一个值", 100, 100, False)[0], "reload")
        want("待重灌条目过期（已被修好）",
             C(FIXTURE, "甲", "乙", 100, 100, False)[0], "FAIL-stale-const")
    finally:
        del RELOAD_PENDING[FIXTURE]
    # 还原了才算完：夹具留在字典里会让全库普查把一个不存在的列报成待办。
    want("夹具已还原", "reload" if FIXTURE in RELOAD_PENDING else "gone", "gone")
    want("透传遗留", C("categories.is_active", "true", "true", 166, 166, False)[0], "dims")
    want("刻意常量", C("channel_daily_costs.currency", "CNY", "CNY", 2799, 2799, False)[0],
         "by_design")
    want("整列 NULL 已登记", C("payments.failure_reason", "", "", 0, 689390, False)[0],
         "null_pinned")
    # 单行表：同义反复，且**优先于**清单（否则 1 行的表会被登记成"待重灌"）
    want("单行表", C("meta_snapshot.as_of_date", "x", "x", 1, 1, True)[0], "single_row")
    want("空表", C("whatever.col", "", "", 0, 0, False)[0], "empty")
    # 清单过期的两个方向 —— 没有这两条，清单就是永久免疫
    # （`RELOAD_PENDING` 那个方向在上面用夹具键测过了，那一桶现在是空的。）
    want("透传条目过期（已被修好）",
         C("categories.is_active", "false", "true", 166, 166, False)[0],
         "FAIL-stale-const")
    # 刻意常量这一类同样不许永久免疫：口径变了（真出现第二种币种）必须红。
    want("刻意常量条目过期（口径变了）",
         C("channel_daily_costs.currency", "CNY", "USD", 2799, 2799, False)[0],
         "FAIL-stale-const")
    want("NULL 条目过期（已被填上）",
         C("payments.failure_reason", "a", "z", 689390, 689390, False)[0],
         "FAIL-stale-null")
    # 整列 NULL 的判定不许被 min/max 的取值干扰：nn=0 时 lo/hi 是 NULL，
    # 驱动那边会把它变成空串或 None，两种都得走同一条分支。
    want("整列 NULL（lo/hi 是 None）",
         C("payments.failure_reason", None, None, 0, 689390, False)[0], "null_pinned")

    # 常量清单与整列 NULL 清单不许判成同一类。两边都用**已登记**的键，否则比的是
    # 「未登记」和「已登记」的差别，而那不是这一条要盯的东西。
    if C("channel_daily_costs.currency", "CNY", "CNY", 2799, 2799, False)[0] == \
            C("payments.failure_reason", "", "", 0, 689390, False)[0]:
        bad += 1
        print("  FAIL 非空常量列和整列 NULL 列被判成了同一类")

    # ---- 数组列（2026-08-31 新增，判据见文件头） ----
    A = classify_array
    want("数组列有真元素", A("posts.tags", 2461, 4, 1000, 1000)[0], "array_ok")
    # 未登记的整列空数组必须红 —— 这一条就是 P1-10 的形态，加这套判据的全部理由
    want("未登记整列空数组", A("posts.media_urls", 0, 0, 1000, 1000)[0],
         "FAIL-array-empty")
    want("数组列整列 NULL 已登记",
         A("campaigns.target_segment_ids", 0, 0, 0, 50)[0], "null_pinned")
    want("数组列整列 NULL 未登记", A("posts.tags", 0, 0, 0, 1000)[0], "FAIL-null")
    want("数组列 NULL 条目过期（已被填上）",
         A("campaigns.target_segment_ids", 30, 2, 50, 50)[0], "FAIL-stale-null")
    want("数组列空表", A("whatever.col", 0, 0, 0, 0)[0], "empty")
    # 「行非 NULL 但每行都是 []」和「整列 NULL」必须分开判：两者的成因和修法不同，
    # 前者是 builder 产了 `[]`，后者是这一列压根没人填。
    if A("posts.media_urls", 0, 0, 1000, 1000)[0] == A("posts.tags", 0, 0, 0, 1000)[0]:
        bad += 1
        print("  FAIL 整列空数组和整列 NULL 被判成了同一类")
    # ARRAY_EMPTY_PINNED 当前是空的，两条豁免分支走不到真实键，用临时键把它们走一遍：
    # 空清单不等于那两条分支不用测——哪天真加一条，分支已经验过了。
    ARRAY_EMPTY_PINNED["_selftest.col"] = "自测用的临时条目"
    try:
        want("整列空数组已登记", A("_selftest.col", 0, 0, 100, 100)[0], "array_empty")
        want("空数组条目过期（已被填上）",
             A("_selftest.col", 7, 2, 100, 100)[0], "FAIL-stale-array-empty")
    finally:
        del ARRAY_EMPTY_PINNED["_selftest.col"]

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  清单：{len(RELOAD_PENDING)} 待重灌 + {len(DIMS_PASSTHROUGH)} 透传遗留 "
          f"+ {len(BY_DESIGN_CONST)} 刻意常量 + {len(ALL_NULL_PINNED)} 整列 NULL "
          f"+ {len(ARRAY_EMPTY_PINNED)} 整列空数组，键形态与归类互斥性一致")
    print("  标量分类器 15 项：正常列、未登记常量、未登记 NULL、四类登记、单行表、空表、"
          "四种清单过期、None 形态、两类不混淆")
    print("  数组分类器 9 项：有真元素、未登记整列空数组、整列 NULL（登记 / 未登记）、"
          "空表、两种清单过期、已登记空数组、空数组与整列 NULL 不混淆")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
