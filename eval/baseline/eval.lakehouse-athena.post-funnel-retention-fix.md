# Eval Report

- 运行时间: 2026-08-23 18:08:36  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **27/27** (100%)
- 平均耗时: 48.3s/题 · 平均读文档 2.6 次 · 平均 SQL 0.8 条

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
| L1-users-count | 1 | ✅ | 36.1s | 3 | 1 | 命中 golden[all users]≈500.0 |
| L1-order-count | 1 | ✅ | 36.2s | 3 | 1 | 命中 golden[all orders]≈2000.0 |
| L1-post-count | 1 | ✅ | 36.3s | 3 | 1 | 命中 golden[all posts]≈1000.0 |
| L1-campaign-count | 1 | ✅ | 37.9s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 41.8s | 0 | 0 | 命中 golden[events last day distinct users]≈27.0 |
| L1-ab-running | 1 | ✅ | 37.6s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 41.6s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 39.0s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 36.5s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 35.1s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 61.7s | 3 | 1 | 命中 golden[page_views 7d anchored] 9/10 |
| L2-top-liked-posts | 2 | ✅ | 60.3s | 4 | 2 | 命中 golden[join post_likes] 4/5 |
| L2-coupon-usage-rate | 2 | ✅ | 43.3s | 3 | 1 | 命中 golden[used/total]≈49.68990543215307 |
| L3-gmv-30d | 3 | ✅ | 34.8s | 0 | 0 | 命中 golden[actual_amount valid status]≈2841462.13 |
| L3-top-products-gmv | 3 | ✅ | 61.6s | 4 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 62.1s | 5 | 1 | 命中 golden[no session in 30d]≈18.0 |
| L3-coupon-aov-compare | 3 | ✅ | 57.4s | 5 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 49.3s | 4 | 1 | 命中 golden[all-time subset funnel] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 74.3s | 4 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-retention-cohort | 5 | ✅ | 115.8s | 4 | 1 | 结论已声明数据限制；命中 golden[weekly matrix pct (full-window cohorts)] 16/16 个数值 |
| L5-repurchase-rate | 5 | ✅ | 57.2s | 1 | 0 | 命中 golden[governed mart definition]≈62.4 |
| L5-wow-gmv | 5 | ✅ | 51.7s | 3 | 1 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |
| L3-refund-total | 3 | ✅ | 35.9s | 0 | 0 | 命中 golden[全量退款 (fin_daily_revenue, 按 refunded_at 建轴)]≈963560.92 |
| L3-channel-cost-total | 3 | ✅ | 52.3s | 3 | 1 | 命中 golden[真实花费 (channel_daily_costs 明细)]≈1449872.13 |
| L4-cac-lowest-channel | 4 | ✅ | 33.8s | 0 | 0 | 命中 golden[CAC 升序 (成本限制在业务日历轴内)] 2/2 |
| L4-roi-cac-by-channel | 4 | ✅ | 39.0s | 0 | 0 | 命中 golden[抖音搜索 近30天的 ROI 与 CAC (同锚点、带上界)] 两值均匹配 |
| L3-cac-overall | 3 | ✅ | 36.1s | 0 | 0 | 命中 golden[全量 CAC (成本限制在业务日历轴内)]≈2880.14 |