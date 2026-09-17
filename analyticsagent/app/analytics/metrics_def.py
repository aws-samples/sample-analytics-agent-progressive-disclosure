# ⚠️ 本文件是**生成物**，不要手改。
# 由 scripts/deploy/sync_agent_code.py 从 backend/metrics_def.py 逐字拷来（除本横幅）。
# 要改请改源文件，再跑 `python3 scripts/deploy/sync_agent_code.py --apply`。
# L0 会跑 `--check`：两侧不一致就红。
"""治理层「指标即 function call」定义 (Metric Registry)。

把 metrics/governed_metrics.md 里的口径从「文档」升级成「可调用、有 owner、口径冻结」的
结构化定义——参考 Agentic Analytics Stack 的做法：agent 问"上季度 GMV"→ 调用
call_metric("gmv", filters={...}) → 返回唯一一个权威数，和看板 / 董事会材料完全一致。

设计要点（对齐 blog 示例 + 治理可追溯）：
- 每个 metric 带 label / owner / 口径声明 / version —— 口径有人负责、被冻结。
- sql 是聚合表达式（作用在某张已治理的 mart 表上，脏活已在建表时做完）。
- dimensions 声明可切片维度；filters 声明默认的口径过滤（如有效订单状态）。
- business_line 区分业务线（电商 / 订阅 …），同一套 call_metric 协议覆盖多业务线。

为什么用 Python dict 而非 YAML：backend 现有依赖里没有 PyYAML，不引入新运行时依赖更稳；
结构与 blog 的 YAML 示例一一对应（见每个 metric 的注释）。
"""
from __future__ import annotations


# 维度名 → 该维度在 mart 表里的真实列 / 表达式。编译器据此生成 GROUP BY / WHERE。
# 维度是跨 metric 复用的，所以集中定义。
DIMENSIONS = {
    "date":          {"label": "日期",   "sql": "dt"},
    "channel":       {"label": "渠道",   "sql": "channel_name"},
    "channel_type":  {"label": "渠道类型", "sql": "channel_type"},
    "is_new_user":   {"label": "新老客", "sql": "is_new_user"},
    "register_channel": {"label": "注册渠道", "sql": "register_channel"},
}

# 业务日历锚点：整个指标层的「今天」。**所有指标共用这一个**。
#
# 原来锚点是「该指标自己那张表的 max(dt)」，看着合理，实测是错的：各表轴末根本不齐。
#   orders / mart_daily_kpi      2026-01-24   ← 业务上真正的"今天"
#   fin_daily_revenue            2026-02-02   （退款轴，尾巴比订单长 9 天）
#   channel_daily_costs / mart_channel_daily
#                                2026-09-01   （投放成本表铺了近一年，
#                                              910 行里 597 行、145 万里 94.6 万
#                                              落在订单轴之后）
#   dws_channel_weekly           2026-08-31
# 后果不是报错，是**静默给错数**：cac 的 last_30d 落在 2026-08-02~09-01，那段有成本、
# 没有任何归因新客，于是 CAC 返回 NULL、ROI 返回 0.0 —— agent 会照着报"最近 30 天
# ROI 为 0"。同一句"最近 30 天"在不同指标上是不同的 30 天，跨指标对比全部失真。
#
# 正解本来就在库里：meta_snapshot.as_of_date = 2026-01-24，是这份静态样本的"今天"，
# 知识库和 agent 手写 SQL 时用的就是它。指标层统一改用它之后：
#   · 所有指标的相对窗口落在同一段日期，可以互相对比
#   · 退款的 last_7d 回到 01/18~01/24，和 GMV 对齐（原先错开 9 天）
#   · cac/roi 的 last_30d 回到 12/25~01/24，出真值（CAC 2880.14 / ROI 6.06）
#   · time_window='all' 是 1=1，总量口径完全不受影响
ANCHOR_SQL = "(SELECT max(as_of_date) FROM meta_snapshot)"

# 时间窗口关键字 → 相对区间。严禁 current_date——本库是静态样本，用 current_date 会查空。
# <ANCHOR> 编译时替换成 ANCHOR_SQL；<COL> 替换成该指标的 time_col。
#
# 区间字面量是 **Trino 写法**：`interval '7' day`——数字在引号里，单位在引号外。
# Postgres 的 `interval '7 day'` 在这里是**运行时错误**（Unknown resolvedType:
# interval），不是解析错，所以只有真跑一次才看得见。改这几行时别顺手改回去。
#
# `<COL>` 是时间列占位符，编译时替换成该 metric 的 time_col（缺省 dt）。
# 原来这里直接写死 `dt`，于是作用在没有 dt 列的表上时（mart_user_summary 按用户建行）
# 生成的 SQL 在 Athena 上抛 COLUMN_NOT_FOUND —— 编译期无感、对账器也扫不到。
#
# 每个有界窗口都**必须带上界 `<COL> <= <ANCHOR>`**。原来只有下界，因为那时锚点是
# 表自身的 max(dt)，上界天然等于表末、写不写一样。换成固定业务日历后不写就漏：
# cac 的 last_30d 会变成 "2025-12-26 之后的全部"，把成本表里 2026-09-01 的行也算进来，
# CAC 从真值 2880 涨到 15137 —— 又是一个不报错的错数。加上界后 12/26~01/24 才闭合。
#
# 'all' 故意**不加上界**：它的语义是"全量"。退款总额 963560.92 里就包含 01/24 之后
# 那 9 天的退款尾巴，clamp 掉会退回 925471.33 —— 正是本项目刚修掉的那个 bug。
TIME_WINDOWS = {
    "last_7d":      "<COL> > (<ANCHOR> - interval '7' day)  AND <COL> <= <ANCHOR>",
    "last_30d":     "<COL> > (<ANCHOR> - interval '30' day) AND <COL> <= <ANCHOR>",
    "last_quarter": "<COL> > (<ANCHOR> - interval '90' day) AND <COL> <= <ANCHOR>",
    "last_month":   "<COL> > (<ANCHOR> - interval '30' day) AND <COL> <= <ANCHOR>",
    "mtd":          "<COL> >= date_trunc('month', <ANCHOR>) AND <COL> <= <ANCHOR>",
    "all":          "1=1",
}

# 有界窗口的判定依据：模板里带 "<COL> <=" 就说明它自己已经闭合了上界。
# metric_layer 用它决定要不要给 clamp_to_anchor 的指标补一条上界，
# 避免靠"字符串里有没有出现过这段"这种脆弱判断。
def is_bounded_above(time_window: str | None) -> bool:
    return "<COL> <=" in TIME_WINDOWS.get(time_window or "", "")


# 指标注册表。每个条目结构对齐 blog 的 YAML 示例：
#   gmv:
#     label / owner / sql / table / filters / dimensions / 口径声明 / version
METRICS = {
    # ---- 业务线: 电商 (ecommerce) ----
    "gmv": {
        "label": "成交总额 (GMV)",
        "business_line": "ecommerce",
        "owner": "交易数据团队",
        "table": "mart_daily_kpi",
        "sql": "SUM(gmv)",                       # mart 已按实付口径冻结
        "unit": "元",
        "filters": ["有效订单(已支付/发货/送达)", "排除退款与取消"],
        "dimensions": ["date"],
        "口径声明": "实付金额口径；mart 建表时已 WHERE status IN ('paid','shipped','delivered')，"
                  "退款单独记 refund_amt，不在 GMV 内。",
        "version": "v1",
    },
    "gmv_by_channel": {
        "label": "渠道 GMV",
        "business_line": "ecommerce",
        "owner": "交易数据团队",
        "table": "mart_daily_revenue",
        "sql": "SUM(gmv)",
        "unit": "元",
        "filters": ["有效订单", "渠道=last_touch 归因"],
        "dimensions": ["date", "channel", "channel_type", "is_new_user"],
        "口径声明": "渠道按 last_touch 归因；约六成 GMV 无归因记录，归入 channel='未归因'，"
                  "报渠道结论时必须披露未归因占比。",
        "version": "v1",
    },
    "refund_amount": {
        "label": "退款金额",
        "business_line": "ecommerce",
        "owner": "交易数据团队",
        # 取数源从 mart_daily_kpi 换到 fin_daily_revenue（v1→v2）。业务定义没变，
        # 变的是**取数源选错了**：mart_daily_kpi 的日期轴由下单日（placed_at）决定、
        # 不含 refunded_at，轴末之后才发生的退款在 LEFT JOIN 时被丢掉。于是
        # SUM(refund_amt) 跨日求和**偏低且不报错**（本数据集少 38089.59，约 4%）。
        #
        # 这条被实测抓到过：问「一共退了多少钱」，指标层返回 925471.33，
        # 而真值是 963560.92。agent 还照抄了旧口径声明说「与 GMV 口径不同」，
        # 听起来很专业，但数就是错的——错在取数源，不在口径表述。
        #
        # fin_daily_revenue 按 refunded_at 建轴，按日和求和都对，且同样有 dt 列，
        # 所以 metric_layer 的时间锚点 (SELECT max(dt) FROM <table>) 无需改动。
        # mart_daily_kpi.refund_amt 本身不动（那是 v1 照搬的既有缺陷，
        # verify_mart_parity.py 的 QUIRKS 钉着它的数值）。
        #
        # 本表的轴（退款发生日）比订单轴长 9 天：最后一批订单下在 01-24，它们的退款
        # 一路发生到 02-02，这 9 天合计 38089.59 —— 正是上面那笔轴外差额
        # （数字自洽：963560.92 - 925471.33 = 38089.59）。
        #
        # 曾经这会让本指标的相对窗口和 GMV 错开 9 天（锚点当时是各表自己的 max(dt)）。
        # 现在锚点统一成 meta_snapshot.as_of_date（见文件顶部 ANCHOR_SQL），
        # last_7d 回到 01/18~01/24，和 GMV 同段；02-02 那 9 天只在 time_window='all'
        # 的总量里出现，而总量本来就该含它。
        "table": "fin_daily_revenue",
        "sql": "SUM(refund_amount)",
        "unit": "元",
        "filters": ["status='refunded' 且 refunded_at 非空"],
        "dimensions": ["date"],
        "口径声明": "按退款发生日 refunded_at 计，与 GMV 的下单日口径不同，不要直接相减"
                    "（要净额用 fin_daily_revenue.net_revenue）。取数走 fin_daily_revenue"
                    "（按 refunded_at 建轴，全量）；不要用 mart_daily_kpi.refund_amt——"
                    "那张表的日期轴不含 refunded_at，轴外退款会被丢掉，跨日求和偏低。"
                    "相对时间窗与其他指标同锚点（业务日历 2026-01-24），可直接与 GMV 对比。",
        "version": "v2",
    },
    "new_users": {
        "label": "新客数",
        "business_line": "ecommerce",
        "owner": "增长数据团队",
        "table": "mart_daily_kpi",
        "sql": "SUM(new_users)",
        "unit": "人",
        "filters": ["按 registered_at 当天注册"],
        "dimensions": ["date"],
        "口径声明": "按注册日计；新客订单/新客GMV另见 mart_daily_revenue.is_new_user。",
        "version": "v1",
    },
    "dau": {
        "label": "日活 (DAU)",
        "business_line": "ecommerce",
        "owner": "增长数据团队",
        "table": "mart_daily_kpi",
        "sql": "SUM(dau)",          # mart 按日预聚合；跨日做趋势时按 date 维度展开
        "unit": "人",
        "filters": ["events 去重 user_id"],
        "dimensions": ["date"],
        "口径声明": "events 表当日去重活跃用户；跨多日时按 date 维度展开看趋势，勿跨日累加成总量。",
        "version": "v1",
    },
    "cac": {
        "label": "获客成本 (CAC)",
        "business_line": "ecommerce",
        "owner": "市场数据团队",
        "table": "mart_channel_daily",
        "type": "ratio",
        # 分子包一层 NULLIF：**窗口内没有投放成本记录时，CAC 是「不适用」，不是「0 元」**。
        #
        # 原来分子是裸的 SUM(cost)，于是 cost=0 的渠道返回 0.0。实测按渠道切近 30 天，
        # 14 个渠道里 10 个是这样。这 10 个混了两类完全不同的情况：
        #   · 天然无投放（App Store / 微信公众号 / 应用宝 / 直接访问 / 老用户邀请）——
        #     channel_type 是 organic/direct/referral，channel_daily_costs 里一行都没有
        #   · 付费渠道但这段窗口没有成本行（快手信息流全量花了 25.2 万、抖音信息流 7.6 万、
        #     小红书种草 14.6 万、微信朋友圈广告 4.8 万、微博KOL 1.4 万，成本行都落在窗口外）
        # 两类都被报成「CAC = 0 元/人」。问「哪个渠道获客成本最低」时，agent 会自信地
        # 答"快手信息流 0 元最便宜" —— 一个花了 25 万的渠道。这跟退款那个 bug 同一类：
        # 不报错、数看着合理、结论反向。
        #
        # 而 roi 的分母天然被 NULLIF 包住（编译器给 ratio 的分母加），成本为 0 时返回
        # NULL、正确弃权。同一批行上 ROI 弃权、CAC 给 0，是**同一张表两套语义**。
        # 现在两边一致：没有成本记录就都返回 NULL，让 agent 只能说"无投放成本记录"。
        #
        # 代价是天然免费的渠道也拿不到"0 元"这个说法。这是刻意的：organic 渠道的 CAC
        # 不是 0 而是不适用（你没法用加预算的方式买到更多老用户邀请），把它和付费渠道
        # 摆在同一个 CAC 排行榜上比较本身就是错的。
        "numerator": "NULLIF(SUM(cost), 0)",
        "denominator": "SUM(new_users_attributed)",
        # 口径域上界：**无论有没有传 time_window，都只算业务日历轴内（≤2026-01-24）的行。**
        #
        # 成本表铺到 2026-09-01，而归因/成交数据止于 2026-01-24：910 行成本里 597 行、
        # 145 万里 94.6 万落在轴外，且那段**没有任何归因数据可配**。所以 time_window='all'
        # （= 1=1，无上界）会拿"轴外的成本 ÷ 轴内的人"，结果不是"更完整"，是两段不同
        # 时期相除。这不是理论担忧，是实测抓到的：
        #
        #   问「哪个渠道获客成本最低」→ 答「微博KOL 938.77 元最低，只有次低的五分之一」
        #   而微博KOL 的成本行全在 2026-05-01~05-09（轴内成本 = 0），归因新客全在轴内；
        #   它便宜只是因为成本数据只覆盖了 9 天。次低的微信朋友圈广告同样是轴内 0 成本
        #   （成本全在 2026-06~08）。榜首两名恰好就是轴内没花过一分钱的两个渠道。
        #   口径对齐后真实排名是小红书种草 1373.55、抖音搜索 1424.04，而这两个渠道
        #   压根不该进榜（轴内无成本 → CAC 不可算）。
        #
        # 为什么不改成全局 clamp（让 TIME_WINDOWS['all'] 带上界）：对 refund_amount 是错的。
        # 它轴外那 9 天（到 02-02）是轴内订单真实产生的退款，clamp 掉会把退款总额从
        # 963560.92 打回 925471.33 —— 正是本项目刚修掉的那个 bug。同一个"全量"，
        # 在退款上意味着"别丢尾巴"，在 CAC 上意味着"别配错时期"。所以按指标声明。
        #
        # 代价：问「总投放花费」时 cac 的分子不再等于成本表的 SUM(cost)。这是对的——
        # 那个问题该直接查表/查卡片，不该借 CAC 的分子来问。
        "clamp_to_anchor": True,
        "unit": "元/人",
        "filters": ["渠道=last_touch 归因", "只算业务日历轴内(≤2026-01-24)的成本"],
        "dimensions": ["date", "channel"],
        "口径声明": "CAC = 渠道投放成本 / 该渠道归因到的新客数；按渠道。无归因新客不计入分母。"
                  "⚠️ 窗口内没有投放成本记录的渠道返回 NULL（不是 0）——"
                  "那既可能是天然无投放的自然量渠道（organic/direct/referral），"
                  "也可能是付费渠道的成本行恰好落在窗口外（如快手信息流全量花了 25.2 万，"
                  "但近 30 天没有成本行）。**别把 NULL 读成「获客免费」、更别拿它做"
                  "「哪个渠道最便宜」的结论**；要判断得先看该渠道全量成本与 channel_type。"
                  "⚠️ 本指标只统计业务日历轴内（≤2026-01-24）的成本，因为成本表铺到 "
                  "2026-09-01 而归因数据止于 2026-01-24（145 万里 94.6 万在轴外、"
                  "无归因可配）。所以 time_window='all' 时分子**不等于**成本表的总花费——"
                  "问「总投放花费」请直接查 channel_daily_costs / mart_channel_daily 并说明"
                  "覆盖到 2026-09-01，别拿 CAC 的分子代答。",
        "version": "v1",
    },
    "roi": {
        "label": "投放回报 (ROI)",
        "business_line": "ecommerce",
        "owner": "市场数据团队",
        "table": "mart_channel_daily",
        "type": "ratio",
        "numerator": "SUM(gmv_attributed)",
        "denominator": "SUM(cost)",
        # 同 cac：只算轴内。分母是成本，含进轴外那 94.6 万会把 ROI 系统性拉低
        # （全量 ROI 2.11 vs 轴内真值 6.06），方向和 CAC 相反但同一个成因。
        "clamp_to_anchor": True,
        "unit": "x",
        "filters": ["渠道=last_touch 归因", "只算业务日历轴内(≤2026-01-24)的成本"],
        "dimensions": ["date", "channel"],
        "口径声明": "ROI = 渠道归因 GMV / 渠道成本。成本为 0 的渠道返回 NULL（不虚增）——"
                  "cac 现在与此一致（同一批行上不会一个弃权一个给 0）。"
                  "⚠️ 同 cac：只统计业务日历轴内（≤2026-01-24）的成本。成本表铺到 "
                  "2026-09-01、订单止于 2026-01-24，轴外 94.6 万成本没有对应成交，"
                  "算进分母会把 ROI 从真值 6.06 压到 2.11。",
        "version": "v1",
    },
    # ---- 业务线: 订阅 / 用户留存 (subscription) ----
    "repurchase_rate_30d": {
        "label": "30天复购率",
        "business_line": "subscription",
        "owner": "用户增长团队",
        "table": "mart_user_summary",
        # mart_user_summary 一行 = 一个用户（终身汇总），**没有 dt 列** —— 它的日期类列
        # 是 first_paid_date / last_active_date / register_date。所以本指标不支持时间窗：
        # time_col=None 让 compile_metric 显式抛 MetricNotCovered。
        #
        # 之前这里没有这个声明，锚点又把 dt 写死，于是 call_metric(repurchase_rate_30d,
        # time_window='last_30d') 生成 `WHERE dt > (SELECT max(dt) FROM mart_user_summary)`，
        # 在 Athena 上抛 COLUMN_NOT_FOUND —— 不带时间窗时（62.42%）完全正常，所以这条
        # 只在 agent 恰好传了时间窗时才炸，报错还是个数据库列错误，指不到成因。
        #
        # 为什么不改成按 first_paid_date 开窗（那样"能跑"）：会造出一个更坏的静默陷阱。
        # 复购率的观察窗本身是 30 天，若把队列限制在"最近 30 天首单的用户"，这批人
        # 根本还没满 30 天可复购，分子系统性偏小、率虚低且不报错。宁可显式拒绝。
        "time_col": None,
        "time_window_hint": "复购率是用户终身队列指标，mart_user_summary 按用户建行、无日期轴。"
                            "要看时间趋势请手写 SQL 按 first_paid_date 划队列，"
                            "并在结论里说明：最近不满 30 天的队列还没走完观察窗，率会偏低。",
        "type": "ratio",
        "numerator": "SUM(CASE WHEN is_repurchaser_30d THEN 1 ELSE 0 END)",
        "denominator": "SUM(CASE WHEN first_paid_date IS NOT NULL THEN 1 ELSE 0 END)",
        "unit": "%",
        "as_pct": True,
        "filters": ["分母仅含有首单用户"],
        "dimensions": ["register_channel"],
        "口径声明": "复购率 = 首单后30天内再次有效下单的用户 / 有首单的用户。分母绝不能用全体用户。",
        "version": "v1",
    },
    "new_subscriptions": {
        "label": "新增订阅数",
        "business_line": "subscription",
        "owner": "订阅业务团队",
        "table": "mart_daily_kpi",
        "sql": "SUM(new_subscriptions)",
        "unit": "个",
        "filters": ["按 subscriptions.start_date 计"],
        "dimensions": ["date"],
        "口径声明": "按订阅开始日计；不含续订/退订事件。"
                  "⚠️ 订阅数据止于 2025-12-21（共 50 条），比业务日历锚点 2026-01-24 早 34 天，"
                  "所以 last_7d / last_30d / last_month / mtd 一律返回 0 —— "
                  "这是**数据覆盖到此为止**，不是业务归零，别当成「订阅停滞」写进结论。"
                  "要看订阅趋势请用 time_window='all' 或 last_quarter。",
        "version": "v1",
    },
}


def list_metrics(business_line: str | None = None) -> dict:
    """返回 metric 名 → label/owner 摘要，供 agent / SYSTEM 提示了解可调用哪些口径。"""
    out = {}
    for name, m in METRICS.items():
        if business_line and m.get("business_line") != business_line:
            continue
        out[name] = {"label": m["label"], "owner": m["owner"],
                     "business_line": m["business_line"],
                     "dimensions": m.get("dimensions", [])}
    return out
