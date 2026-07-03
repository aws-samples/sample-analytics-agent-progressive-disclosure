# 治理层 / 数据集市 (Mart Layer)

## 这层是什么

治理层是「治理之后」的数据。原始 35 张表里的脏活（多表 join、口径判断、去重）已经在 ETL 阶段一次性做完，固化成几张干净的预聚合表（`mart_` 前缀）。指标口径（GMV、新客、归因、复购等）在这层是**冻结的官方定义**，见 `metrics/governed_metrics.md`。

## 什么时候用这层（重要）

- **诊断 / 复盘 / 趋势 / 综合判断** 类问题 → 用治理层。例如「最近业务怎么样」「GMV 环比怎么变、谁带动的」「复购率多少」。在这层只需对干净表写**简单 SELECT**，把力气花在多角度切片、归因、下结论上。
- **取数 / 看明细 / 探某个特定原始口径** → 回原始域（`domains/<域>/<表>.md`），那边是明细表，需要现场 join。

## 表清单

| 表名 | 粒度 | 说明 | 详情文件 |
|------|------|------|----------|
| mart_daily_kpi | 天 | 每日业务大盘（DAU/新客/订单/GMV/订阅） | `mart_daily_kpi.md` |
| mart_daily_revenue | 天 × 渠道 × 新老客 | 收入事实表，做 GMV 拆解与归因下钻 | `mart_daily_revenue.md` |
| mart_channel_daily | 天 × 渠道 | 渠道成本/效果，算 CAC/ROI | `mart_channel_daily.md` |
| mart_user_summary | 用户 | 用户级汇总，复购/LTV/cohort | `mart_user_summary.md` |

## 关键词路由

| 关键词 | 加载文件 |
|--------|----------|
| 业务大盘、整体、概况、周报、最近怎么样、关键指标、复盘 | `mart_daily_kpi.md` |
| GMV 拆解、收入归因、为什么涨跌、按渠道/新老客看收入 | `mart_daily_revenue.md` |
| 渠道成本、CAC、ROI、获客成本、投放效果 | `mart_channel_daily.md` |
| 复购、复购率、LTV、用户价值、cohort | `mart_user_summary.md` |

## 通用提醒

- 口径冻结在 `metrics/governed_metrics.md`，直接引用，**不要在这层重新推导口径**。
- 时间锚点同样以表自身 `dt` 的 `max()` 为「今天」，禁用 `current_date`/`now()`。
- **残周/残月别直接比**：数据首尾两段（2025-10 月头、2026-01 月尾、最后一个自然周）是残的，整月/整周才可比。
- **归因覆盖**：渠道归因走 last_touch，只覆盖有归因记录的用户；相当一部分 GMV 落在 `channel_name='未归因'`（约六成）。做渠道归因时必须把这块单列、正视它，不能假装不存在——这本身就是个值得报出来的治理发现。
