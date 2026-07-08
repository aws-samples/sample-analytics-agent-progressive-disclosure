# Eval Report

- 运行时间: 2026-07-08 15:43:40  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **21/21** (100%)
- 平均耗时: 42.5s/题 · 平均读文档 2.7 次 · 平均 SQL 1.1 条

| Level | 通过 | 总数 |
|---|---|---|
| L1 | 7 | 7 |
| L2 | 6 | 6 |
| L3 | 4 | 4 |
| L4 | 2 | 2 |
| L5 | 2 | 2 |

## 逐题明细

| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |
|---|---|---|---|---|---|---|
| L1-users-count | 1 | ✅ | 33.0s | 3 | 1 | 命中 golden[all users]≈500.0 |
| L1-order-count | 1 | ✅ | 34.4s | 3 | 1 | 命中 golden[all orders]≈2000.0 |
| L1-post-count | 1 | ✅ | 48.0s | 3 | 1 | 命中 golden[all posts]≈1000.0 |
| L1-campaign-count | 1 | ✅ | 43.7s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 35.5s | 0 | 0 | 命中 golden[events last day distinct users]≈27.0 |
| L1-ab-running | 1 | ✅ | 34.8s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 38.9s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 32.3s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 31.7s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 41.3s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 39.9s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 47.5s | 4 | 2 | 命中 golden[join post_likes] 4/5 |
| L2-coupon-usage-rate | 2 | ✅ | 38.9s | 3 | 1 | 命中 golden[used/total]≈49.7 |
| L3-gmv-30d | 3 | ✅ | 49.9s | 0 | 1 | 命中 golden[actual_amount valid status]≈2841462.13 |
| L3-top-products-gmv | 3 | ✅ | 41.6s | 4 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 46.4s | 5 | 1 | 命中 golden[no session in 30d]≈18.0 |
| L3-coupon-aov-compare | 3 | ✅ | 42.6s | 4 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 58.0s | 3 | 3 | 命中 golden[all-time distinct users] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 56.5s | 3 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-repurchase-rate | 5 | ✅ | 48.6s | 0 | 0 | 命中 golden[governed mart definition]≈62.4 |
| L5-wow-gmv | 5 | ✅ | 48.9s | 1 | 2 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |