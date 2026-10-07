# Eval Report

- 运行时间: 2026-09-03 01:43:58  · 模型: global.anthropic.claude-opus-4-8  · arm: **redshift**
- 通过率: **27/27** (100%)
- 平均耗时: 47.9s/题 · 平均读文档 2.5 次 · 平均 SQL 0.8 条

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
| L1-users-count | 1 | ✅ | 61.5s | 3 | 1 | 命中 golden[all users]≈213535.0 |
| L1-order-count | 1 | ✅ | 36.2s | 3 | 1 | 命中 golden[all orders]≈854140.0 |
| L1-post-count | 1 | ✅ | 35.9s | 3 | 1 | 命中 golden[all posts]≈427070.0 |
| L1-campaign-count | 1 | ✅ | 29.7s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 45.8s | 0 | 0 | 命中 golden[events last day distinct users]≈30159.0 |
| L1-ab-running | 1 | ✅ | 50.4s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 55.5s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 32.4s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 36.0s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 37.2s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 40.9s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 43.7s | 3 | 1 | 命中 golden[join post_likes] 5/5 |
| L2-coupon-usage-rate | 2 | ✅ | 50.2s | 3 | 1 | 命中 golden[used/total]≈3.343345586705551 |
| L3-gmv-30d | 3 | ✅ | 27.1s | 0 | 0 | 命中 golden[actual_amount valid status]≈57673919.78 |
| L3-top-products-gmv | 3 | ✅ | 36.9s | 3 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 49.1s | 5 | 1 | 命中 golden[no session in 30d]≈93536.0 |
| L3-coupon-aov-compare | 3 | ✅ | 42.7s | 3 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 50.1s | 4 | 1 | 命中 golden[all-time subset funnel] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 52.4s | 4 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-retention-cohort | 5 | ✅ | 101.4s | 4 | 2 | 结论已声明数据限制；命中 golden[weekly matrix pct (full-window cohorts)] 18/20 个数值 |
| L5-repurchase-rate | 5 | ✅ | 63.5s | 2 | 1 | 命中 golden[governed mart definition]≈63.1 |
| L5-wow-gmv | 5 | ✅ | 48.6s | 3 | 1 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |
| L3-refund-total | 3 | ✅ | 44.1s | 0 | 0 | 命中 golden[全量退款 (fin_daily_revenue, 按 refunded_at 建轴)]≈9897495.82 |
| L3-channel-cost-total | 3 | ✅ | 49.3s | 4 | 1 | 命中 golden[真实花费 (channel_daily_costs 明细)]≈4351988.47 |
| L4-cac-lowest-channel | 4 | ✅ | 91.1s | 0 | 0 | 命中 golden[CAC 升序 (成本限制在业务日历轴内)] 2/2 |
| L4-roi-cac-by-channel | 4 | ✅ | 38.9s | 0 | 0 | 命中 golden[抖音搜索 近30天的 ROI 与 CAC (同锚点、带上界)] 两值均匹配 |
| L3-cac-overall | 3 | ✅ | 43.1s | 0 | 0 | 命中 golden[全量 CAC (成本限制在业务日历轴内)]≈17.07 |