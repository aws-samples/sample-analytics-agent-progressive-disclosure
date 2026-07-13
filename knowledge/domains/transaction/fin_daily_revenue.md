# fin_daily_revenue - 财务日收入

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：ADS 应用层 · **口径归属**：finance
**粒度**：一行 = 一天（按支付成功时间）

财务日收入：确认收入口径 —— 按 paid_at 记账、扣退款、不含运费

## 何时用这张表

- ✅ 财务/对账/确认收入问题（"财务口径""确认收入""净收入"）
- ❌ 大盘 GMV/成交额（用 mart_daily_revenue 或 growth_daily_gmv，口径是下单额）

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | DATE | 日期（按支付/退款发生日） |
| gross_revenue | NUMERIC(14 | 毛收入 = 有效订单实付 - 运费，按 paid_at 记账 |
| refund_amount | NUMERIC(14 | 当日发生的退款额 |
| net_revenue | NUMERIC(14 | 净收入 = 毛收入 - 退款 |

## 注意（口径与坑）

- 与 mart_daily_revenue/growth_daily_gmv 数字对不上是【设计使然】——那边按 placed_at 计下单额、含运费、不扣退款
- 回答收入问题前先判断问的是哪个部门的口径；不确定时在结论里注明口径

## 构建口径（本表如何从基表算出）

```sql
WITH paid AS (
  SELECT paid_at::date AS dt,
         sum(actual_amount - shipping_fee) AS gross_revenue
  FROM orders
  WHERE status IN ('paid','shipped','delivered') AND paid_at IS NOT NULL
  GROUP BY 1
),
refunds AS (
  SELECT refunded_at::date AS dt, sum(actual_amount) AS refund_amount
  FROM orders WHERE status = 'refunded' AND refunded_at IS NOT NULL
  GROUP BY 1
)
SELECT COALESCE(p.dt, r.dt) AS dt,
       COALESCE(p.gross_revenue, 0)::numeric(14,2) AS gross_revenue,
       COALESCE(r.refund_amount, 0)::numeric(14,2) AS refund_amount,
       (COALESCE(p.gross_revenue, 0) - COALESCE(r.refund_amount, 0))::numeric(14,2) AS net_revenue
FROM paid p FULL OUTER JOIN refunds r USING (dt)
```
