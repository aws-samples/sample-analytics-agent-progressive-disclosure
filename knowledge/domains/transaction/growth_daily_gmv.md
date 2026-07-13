# growth_daily_gmv - 增长日 GMV

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：ADS 应用层 · **口径归属**：growth
**粒度**：一行 = 一天（按下单时间）

增长日 GMV：下单口径 —— 按 placed_at、含运费、不扣退款，另含下单用户数

## 何时用这张表

- ✅ 增长/大盘/转化问题（"GMV""成交额""下单用户"）；与投放数据按下单日对齐
- ❌ 财务确认收入（用 fin_daily_revenue）

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | DATE | 下单日期 |
| gmv | NUMERIC(14 | 有效订单实付金额合计（含运费、不扣退款） |
| paid_orders | BIGINT | 有效订单数 |
| paying_users | BIGINT | 有效下单去重用户数 |
| all_orders | BIGINT | 全部订单数（含取消/退款/待支付） |

## 注意（口径与坑）

- gmv 与 mart_daily_revenue.gmv 同口径（可互相校验）；与 fin_daily_revenue.net_revenue 对不上是设计使然

## 构建口径（本表如何从基表算出）

```sql
SELECT placed_at::date AS dt,
       sum(actual_amount) FILTER (WHERE status IN ('paid','shipped','delivered'))::numeric(14,2) AS gmv,
       count(*) FILTER (WHERE status IN ('paid','shipped','delivered'))          AS paid_orders,
       count(DISTINCT user_id) FILTER (WHERE status IN ('paid','shipped','delivered')) AS paying_users,
       count(*) AS all_orders
FROM orders
GROUP BY 1
```
