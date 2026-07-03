# 治理层官方指标字典 (Governed Metrics)

> 这是治理层（`mart_` 表）的**官方口径**。在治理层回答问题时，指标定义一律以本文件为准，**不要自己重新拼口径**。每个指标标注定义、对应 mart 表/列、owner、版本——这正是「治理之后」和原始数据的区别：口径被冻结、有人负责，而不是每次现推。

> **口径即 function call（重要）**：下列指标已升级成**可调用的 metric**，**优先用 `call_metric` 工具**直接拿权威数，而不是手写 SQL——口径写死在代码里，返回值和看板/董事会材料一致、模型无法猜错。可调用：`gmv`、`gmv_by_channel`、`refund_amount`、`new_users`、`dau`、`cac`、`roi`、`repurchase_rate_30d`、`new_subscriptions`。例：`call_metric(metric="gmv", time_window="last_quarter")`、`call_metric(metric="cac", group_by=["channel"])`。只有 call_metric 返回 `not_covered`（指标/维度没覆盖）时，才退回手写 SQL。本文件是这些指标的**口径定义说明**（人读），结构化定义在 `backend/metrics_def.py`（机器读）。

## GMV（成交额）
- **定义**：有效订单的实付金额之和。
- **口径**：`sum(actual_amount) WHERE status IN ('paid','shipped','delivered')`。排除退款、取消、未支付。
- **取数**：`mart_daily_kpi.gmv` / `mart_daily_revenue.gmv`
- owner：交易数据团队 · 版本：v1（2026-06）

## 新客（New User）
- **定义**：按 `registered_at` 当天注册的用户。
- **新客订单 / 新客 GMV**：用户的**首笔有效订单**计为新客贡献（`mart_daily_revenue.is_new_user=true`）。
- **取数**：`mart_daily_kpi.new_users`

## 渠道归因（Attribution）
- **定义**：last_touch，每用户取最近一条 last_touch 记录归到一个渠道。
- **覆盖**：仅覆盖有归因记录的用户；无记录的成交计为「未归因」（`channel_name='未归因'`，约占 GMV 六成）。**报渠道结论时必须披露未归因占比**，否则会高估渠道贡献。
- **取数**：`mart_daily_revenue` / `mart_channel_daily` 的 channel_* 列

## CAC（获客成本）
- **定义**：渠道投放成本 / 该渠道归因到的新客数。
- **口径**：`sum(cost)/nullif(sum(new_users_attributed),0)`，按渠道。
- **取数**：`mart_channel_daily`

## ROI（投放回报）
- **定义**：渠道归因 GMV / 渠道成本。
- **口径**：`sum(gmv_attributed)/nullif(sum(cost),0)`。
- **取数**：`mart_channel_daily`

## 复购率（30 天）
- **定义**：有首单的用户里，首笔有效订单后 30 天内再次产生有效订单的比例。
- **口径**：见 `mart_user_summary.is_repurchaser_30d`；**分母只含有首单用户**，不要拿全体用户当分母。
- **取数**：`mart_user_summary`

## 时间锚点（通用）
- 静态样本，数据落在 **2025-10-27 ~ 2026-01-24**。
- 「最近 / 上周 / 本月」一律以表自身 `dt` 的 `max()` 为今天，**禁用 `current_date`/`now()`**。
- 残周 / 残月不要直接和整周 / 整月比（首尾两段是残的）。
