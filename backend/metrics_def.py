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

# 时间窗口关键字 → 相对锚点（锚点用「表自身 dt 的 max()」，严禁 current_date——
# 本库是静态样本，最新数据约 2026-01-24，用 current_date 会查空。编译器把这些翻译成
# WHERE dt ... 。<ANCHOR> 占位符在编译时替换成 (SELECT max(dt) FROM <table>)。
TIME_WINDOWS = {
    "last_7d":      "dt > (<ANCHOR> - interval '7 day')",
    "last_30d":     "dt > (<ANCHOR> - interval '30 day')",
    "last_quarter": "dt > (<ANCHOR> - interval '90 day')",
    "last_month":   "dt > (<ANCHOR> - interval '30 day')",
    "mtd":          "dt >= date_trunc('month', <ANCHOR>)",
    "all":          "1=1",
}


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
        "table": "mart_daily_kpi",
        "sql": "SUM(refund_amt)",
        "unit": "元",
        "filters": ["status='refunded' 且 refunded_at 非空"],
        "dimensions": ["date"],
        "口径声明": "按退款发生日 refunded_at 计，与 GMV 的下单日口径不同，不要直接相减。",
        "version": "v1",
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
        "numerator": "SUM(cost)",
        "denominator": "SUM(new_users_attributed)",
        "unit": "元/人",
        "filters": ["渠道=last_touch 归因"],
        "dimensions": ["date", "channel"],
        "口径声明": "CAC = 渠道投放成本 / 该渠道归因到的新客数；按渠道。无归因新客不计入分母。",
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
        "unit": "x",
        "filters": ["渠道=last_touch 归因"],
        "dimensions": ["date", "channel"],
        "口径声明": "ROI = 渠道归因 GMV / 渠道成本。成本为 0 的渠道返回 NULL（不虚增）。",
        "version": "v1",
    },
    # ---- 业务线: 订阅 / 用户留存 (subscription) ----
    "repurchase_rate_30d": {
        "label": "30天复购率",
        "business_line": "subscription",
        "owner": "用户增长团队",
        "table": "mart_user_summary",
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
        "口径声明": "按订阅开始日计；不含续订/退订事件。",
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
