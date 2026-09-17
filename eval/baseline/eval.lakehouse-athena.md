# Eval Report

- 运行时间: 2026-08-19 17:52:43  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **26/26** (100%)
- 平均耗时: 52.7s/题 · 平均读文档 2.4 次 · 平均 SQL 1.0 条

| Level | 通过 | 总数 |
|---|---|---|
| L1 | 7 | 7 |
| L2 | 6 | 6 |
| L3 | 7 | 7 |
| L4 | 4 | 4 |
| L5 | 2 | 2 |

## 逐题明细

| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |
|---|---|---|---|---|---|---|
| L1-users-count | 1 | ✅ | 54.4s | 3 | 1 | 命中 golden[all users]≈500.0 |
| L1-order-count | 1 | ✅ | 45.3s | 3 | 1 | 命中 golden[all orders]≈2000.0 |
| L1-post-count | 1 | ✅ | 58.9s | 3 | 2 | 命中 golden[all posts]≈1000.0 |
| L1-campaign-count | 1 | ✅ | 66.4s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 54.3s | 0 | 0 | 命中 golden[events last day distinct users]≈27.0 |
| L1-ab-running | 1 | ✅ | 50.1s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 53.7s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 43.3s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 48.5s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 42.5s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 48.3s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 67.3s | 4 | 3 | 命中 golden[join post_likes] 4/5 |
| L2-coupon-usage-rate | 2 | ✅ | 67.6s | 3 | 1 | 命中 golden[used/total]≈49.68990543215307 |
| L3-gmv-30d | 3 | ✅ | 43.6s | 0 | 0 | 命中 golden[actual_amount valid status]≈2841462.13 |
| L3-top-products-gmv | 3 | ✅ | 65.9s | 5 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 57.9s | 3 | 1 | 命中 golden[no session in 30d]≈18.0 |
| L3-coupon-aov-compare | 3 | ✅ | 61.4s | 4 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 75.4s | 3 | 3 | 命中 golden[all-time distinct users] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 55.1s | 3 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-repurchase-rate | 5 | ✅ | 46.4s | 1 | 0 | 命中 golden[governed mart definition]≈62.4 |
| L5-wow-gmv | 5 | ✅ | 50.2s | 2 | 2 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |
| L3-refund-total | 3 | ✅ | 39.7s | 0 | 0 | 命中 golden[全量退款 (fin_daily_revenue, 按 refunded_at 建轴)]≈963560.92 |
| L3-channel-cost-total | 3 | ✅ | 50.8s | 4 | 1 | 命中 golden[真实花费 (channel_daily_costs 明细)]≈1449872.13 |
| L4-cac-lowest-channel | 4 | ✅ | 49.4s | 0 | 0 | 命中 golden[CAC 升序 (成本限制在业务日历轴内)] 2/2 |
| L4-roi-cac-by-channel | 4 | ✅ | 37.9s | 0 | 0 | 命中 golden[抖音搜索 近30天的 ROI 与 CAC (同锚点、带上界)] 两值均匹配 |
| L3-cac-overall | 3 | ✅ | 34.8s | 0 | 0 | 命中 golden[全量 CAC (成本限制在业务日历轴内)]≈2880.14 |