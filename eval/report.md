# Eval Report

- 运行时间: 2026-09-18 11:22:03  · 模型: global.anthropic.claude-opus-4-8  · arm: **athena**
- 通过率: **27/27** (100%)
- 平均耗时: 50.0s/题 · 平均读文档 2.4 次 · 平均 SQL 0.8 条

| Level | 通过 | 总数 |
|---|---|---|
| L1 | 7 | 7 |
| L2 | 6 | 6 |
| L3 | 7 | 7 |
| L4 | 4 | 4 |
| L5 | 3 | 3 |

## 逐题明细

| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |
|---|---|---|---|---|---|---|
| L1-users-count | 1 | ✅ | 36.2s | 3 | 1 | 命中 golden[all users]≈213535.0 |
| L1-order-count | 1 | ✅ | 32.3s | 3 | 1 | 命中 golden[all orders]≈854140.0 |
| L1-post-count | 1 | ✅ | 33.8s | 3 | 1 | 命中 golden[all posts]≈427070.0 |
| L1-campaign-count | 1 | ✅ | 40.2s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 41.1s | 1 | 1 | 命中 golden[events last day distinct users]≈30159.0 |
| L1-ab-running | 1 | ✅ | 46.6s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 44.8s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 35.8s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 41.4s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 33.4s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 38.2s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 49.5s | 3 | 1 | 命中 golden[join post_likes] 5/5 |
| L2-coupon-usage-rate | 2 | ✅ | 51.7s | 3 | 1 | 命中 golden[used/total]≈3.343345586705551 |
| L3-gmv-30d | 3 | ✅ | 24.9s | 0 | 0 | 命中 golden[actual_amount valid status]≈57673919.78 |
| L3-top-products-gmv | 3 | ✅ | 44.0s | 3 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 91.1s | 5 | 1 | 命中 golden[no session in 30d]≈91291.0 |
| L3-coupon-aov-compare | 3 | ✅ | 50.1s | 3 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 59.2s | 4 | 1 | 命中 golden[all-time subset funnel] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 145.1s | 4 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-retention-cohort | 5 | ✅ | 88.9s | 3 | 1 | 曲线在衰减，形状闸不适用（金标实测 5 个 cohort 的末周/首周 ≈ 0.50（衰减，阈值 0.8））；命中 golden[weekly matrix p |
| L5-repurchase-rate | 5 | ✅ | 55.9s | 0 | 0 | 命中 golden[governed mart definition]≈63.1 |
| L5-wow-gmv | 5 | ✅ | 51.3s | 2 | 1 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |
| L3-refund-total | 3 | ✅ | 28.2s | 0 | 0 | 命中 golden[全量退款 (fin_daily_revenue, 按 refunded_at 建轴)]≈9897495.82 |
| L3-channel-cost-total | 3 | ✅ | 64.8s | 4 | 1 | 命中 golden[真实花费 (channel_daily_costs 明细)]≈4351988.47 |
| L4-cac-lowest-channel | 4 | ✅ | 34.6s | 0 | 0 | 命中 golden[CAC 升序 (成本限制在业务日历轴内)] 2/2 |
| L4-roi-cac-by-channel | 4 | ✅ | 42.5s | 0 | 0 | 命中 golden[抖音搜索 近30天的 ROI 与 CAC (同锚点、带上界)] 两值均匹配 |
| L3-cac-overall | 3 | ✅ | 44.4s | 0 | 0 | 命中 golden[全量 CAC (成本限制在业务日历轴内)]≈17.07 |