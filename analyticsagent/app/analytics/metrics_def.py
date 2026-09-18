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
#   fin_daily_revenue            2026-01-24   （退款轴。**现在和订单轴同尾**；旧数据集里
#                                              它到 2026-02-02、比订单长 9 天，那是 v1
#                                              把退款时间写到了快照日之后）
#   channel_daily_costs / mart_channel_daily
#                                2026-09-01   （投放成本表铺了近一年，
#                                              2799 行里 1980 行、435.2 万里 307.7 万
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
#   · cac/roi 的 last_30d 回到 12/25~01/24，出真值（CAC 16.89 / ROI 42.65）
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
# CAC 从真值 16.89 涨到 125.30（7.4 倍）—— 又是一个不报错的错数。加上界后 12/26~01/24 才闭合。
#
# 'all' 故意**不加上界**：它的语义是"全量"，退款按发生日计、哪天发生算哪天。
# **当前这份数据里没有越过 as_of_date 的退款**（生成器把订单生命周期的时间戳全截在
# as_of_date 以内，见 scripts/gen/tables.py::_prep_orders），所以退款总额
# 9897495.82 对 clamp 与不 clamp 是同一个数——这条声明现在没有数值上的区分力，
# 但它写的是**语义**而不是这份数据的形状，别因为"反正一样"就把它并进全局 clamp。
# 旧数据集有 9 天退款尾巴：总额 963560.92，clamp 掉会退回 925471.33，正是本项目
# 修掉的那个 bug。verify_mart_parity.py 会把"这条比对已失去区分力"当成 ⚠️ 打出来。
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
        # SUM(refund_amt) 跨日求和**偏低且不报错**。
        #
        # 这条被实测抓到过：问「一共退了多少钱」，指标层返回 925471.33，
        # 而真值是 963560.92（旧数据集，少 38089.59、约 4%）。agent 还照抄了旧口径
        # 声明说「与 GMV 口径不同」，听起来很专业，但数就是错的——错在取数源，
        # 不在口径表述。
        #
        # **当前这份数据上两边算出来一样：都是 9897495.82。** 生成器把订单生命周期的
        # 时间戳全截在 as_of_date 以内（见 scripts/gen/tables.py::_prep_orders），
        # 退款轴末和订单轴末同为 2026-01-24，于是没有任何退款落在 mart_daily_kpi 的
        # 轴外、没有东西可丢。**别因此把取数源换回 mart_daily_kpi**：这里少的不是
        # 数值差额而是"能暴露差额的数据"，缺陷本身一个字没修——换一份带退款尾巴的
        # 数据（或者把 as_of_date 往前挪）它立刻又偏低，而且照旧不报错。
        #
        # fin_daily_revenue 按 refunded_at 建轴，按日和求和都对，且同样有 dt 列，
        # 所以 metric_layer 的时间锚点 (SELECT max(dt) FROM <table>) 无需改动。
        # mart_daily_kpi.refund_amt 本身不动（那是 v1 照搬的既有缺陷，
        # verify_mart_parity.py 的 QUIRKS 钉着它的数值）。
        #
        # 旧数据集里本表的轴（退款发生日）比订单轴长 9 天：最后一批订单下在 01-24，
        # 它们的退款一路发生到 02-02，这 9 天合计 38089.59 —— 正是那笔轴外差额
        # （数字自洽：963560.92 - 925471.33 = 38089.59）。
        #
        # 曾经这还会让本指标的相对窗口和 GMV 错开 9 天（锚点当时是各表自己的 max(dt)）。
        # 现在锚点统一成 meta_snapshot.as_of_date（见文件顶部 ANCHOR_SQL），
        # 两条轴同尾，last_7d 和 GMV 天然同段。
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
        # 14 个渠道里 5 个是这样：App Store / 微信公众号 / 应用宝 / 直接访问 /
        # 老用户邀请——channel_type 是 organic/direct/referral，channel_daily_costs
        # 里一行都没有。它们被报成「CAC = 0 元/人」，于是问「哪个渠道获客成本最低」时
        # agent 会答"直接访问 0 元最便宜"，把一个买不到的量当成最优投放标的。
        #
        # 还有第二类会撞上同一个 0：**付费渠道，但成本行恰好全落在窗口外**。那种
        # "花了 25 万的渠道被报成 0 元" 比上面更难看出来。**当前这份数据里不出现**——
        # 9 个有投放的渠道（paid / kol）在业务轴内每天都有成本行，全量和近 30 天都算得出
        # CAC。机制照旧成立，只是这份数据举不出这个例子了；NULLIF 同时挡住两类。
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
        # 成本表铺到 2026-09-01，而归因/成交数据止于 2026-01-24：2799 行成本里 1980 行、
        # 435.2 万里 307.7 万落在轴外，且那段**没有任何归因数据可配**。所以 time_window='all'
        # （= 1=1，无上界）会拿"轴外的成本 ÷ 轴内的人"，结果不是"更完整"，是两段不同
        # 时期相除：全量 CAC 从真值 17.07 虚高到 58.25（3.41 倍），ROI 从 41.84 压到
        # 12.26（同一个倍数，方向相反）。
        #
        # 这份数据上 clamp 不改变**渠道之间的排名**——成本在轴内外的分布对九个渠道几乎
        # 同比例，所以不 clamp 的排名和 clamp 后一样（小红书搜索最便宜 → 微博KOL 最贵）。
        # 别据此认为 clamp 可省：错的是**量级**，而量级恰恰是拿去和别的数比的那个东西
        # ——"CAC 58 元 vs 客单价 118 元" 会读成"获客几乎不赚钱"，真值 17.07 才是对的。
        # 旧数据集里连排名都是反的（成本覆盖稀疏的渠道假装最便宜：微博KOL 938.77 元被答成
        # 最低，而它轴内一分钱没花），那是这条 clamp 当初被写下来的原因。
        #
        # 为什么不改成全局 clamp（让 TIME_WINDOWS['all'] 带上界）：对 refund_amount 的
        # 语义是错的。退款是"轴内订单产生的、哪天发生算哪天"，clamp 掉尾巴就是本项目
        # 修掉的那个 bug（旧数据集：963560.92 → 925471.33）。**当前这份数据没有这条
        # 尾巴，两边都是 9897495.82**，所以这个区别现在只在语义上而不在数值上——
        # verify_mart_parity.py 因此改成先钉编译产物里 clamp 谓词的有无、再比数值。
        # 同一个"全量"，在退款上意味着"别丢尾巴"，在 CAC 上意味着"别配错时期"。
        #
        # 代价：问「总投放花费」时 cac 的分子不再等于成本表的 SUM(cost)。这是对的——
        # 那个问题该直接查表/查卡片，不该借 CAC 的分子来问。
        "clamp_to_anchor": True,
        "unit": "元/人",
        "filters": ["渠道=last_touch 归因", "只算业务日历轴内(≤2026-01-24)的成本"],
        "dimensions": ["date", "channel"],
        "口径声明": "CAC = 渠道投放成本 / 该渠道归因到的新客数；按渠道。无归因新客不计入分母。"
                  "⚠️ 窗口内没有投放成本记录的渠道返回 NULL（不是 0）。这份数据里恒为 NULL 的"
                  "是 5 个天然无投放的自然量渠道（App Store / 微信公众号 / 应用宝 / 直接访问 / "
                  "老用户邀请，channel_type 为 organic/direct/referral）；另有一类 NULL 是"
                  "付费渠道的成本行恰好全落在窗口外，本数据集不出现（9 个 paid/kol 渠道轴内每天"
                  "都有成本行）。**别把 NULL 读成「获客免费」、更别拿它做「哪个渠道最便宜」的"
                  "结论**；要判断得先看该渠道全量成本与 channel_type。"
                  "⚠️ 本指标只统计业务日历轴内（≤2026-01-24）的成本，因为成本表铺到 "
                  "2026-09-01 而归因数据止于 2026-01-24（435.2 万里 307.7 万在轴外、"
                  "无归因可配；不 clamp 会把全量 CAC 从 17.07 虚高到 58.25）。"
                  "所以 time_window='all' 时分子**不等于**成本表的总花费——"
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
        # 同 cac：只算轴内。分母是成本，含进轴外那 307.7 万会把 ROI 系统性拉低
        # （全量 ROI 12.26 vs 轴内真值 41.84），方向和 CAC 相反但同一个成因。
        "clamp_to_anchor": True,
        "unit": "x",
        "filters": ["渠道=last_touch 归因", "只算业务日历轴内(≤2026-01-24)的成本"],
        "dimensions": ["date", "channel"],
        "口径声明": "ROI = 渠道归因 GMV / 渠道成本。成本为 0 的渠道返回 NULL（不虚增）——"
                  "cac 现在与此一致（同一批行上不会一个弃权一个给 0）。"
                  "⚠️ 同 cac：只统计业务日历轴内（≤2026-01-24）的成本。成本表铺到 "
                  "2026-09-01、订单止于 2026-01-24，轴外 307.7 万成本没有对应成交，"
                  "算进分母会把 ROI 从真值 41.84 压到 12.26。",
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
        "口径声明": "按订阅开始日计；不含续订/退订事件。当前样本 21354 条，"
                  "轴是 2025-10-26 ~ 2026-01-24，和订单轴同段，所有相对窗口都有值"
                  "（last_7d 1879 / last_30d 7871 / all 21354）。"
                  "⚠️ 旧数据集里这张表只有 50 条、止于 2025-12-21（比锚点早 34 天），"
                  "于是 last_7d / last_30d / mtd 一律返回 0；那种 0 是**数据覆盖到此为止**"
                  "而不是业务归零，别当成「订阅停滞」写进结论。任何窗口返回 0 时先看轴末在哪。",
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
