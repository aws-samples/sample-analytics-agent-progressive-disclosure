# fin_daily_revenue - 财务日收入

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：ADS 应用层 · **口径归属**：finance
**粒度**：一行 = 一天（按支付成功时间）

财务日收入：确认收入口径 —— 按 paid_at 记账、扣退款、不含运费

## 何时用这张表

- ✅ 财务/对账/确认收入问题（"财务口径""确认收入""净收入"）；**任何"退款总额/累计退款/一共退了多少"** —— 本表是退款的唯一全量口径
- ❌ 大盘 GMV/成交额（用 mart_daily_revenue 或 growth_daily_gmv，口径是下单额）

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | DATE | 日期（按支付/退款发生日） |
| gross_revenue | NUMERIC(14 | 毛收入 = 已付款订单（含之后退款的）实付 - 运费，按 paid_at 记账 |
| refund_amount | NUMERIC(14 | 当日发生的退款额。本表按 refunded_at 建轴，所以 sum 是【全量退款】—— 问退款总额用这一列 |
| net_revenue | NUMERIC(14 | 净收入 = 毛收入 - 当日冲回的收入（退款订单实付 - 运费，按 refunded_at）。refund_amount 含运费，不能直接拿来减 |

## 注意（口径与坑）

- 与 mart_daily_revenue/growth_daily_gmv 数字对不上是【设计使然】——那边按 placed_at 计下单额、含运费、不扣退款
- 回答收入问题前先判断问的是哪个部门的口径；不确定时在结论里注明口径
- 退款问题优先用本表：mart_daily_kpi.refund_amt 的日期轴由 placed_at 决定、不含 refunded_at，轴末之后的退款被 LEFT JOIN 丢掉，合计偏低；差值就是轴外退款
- 本表轴末与业务日历一致（2026-01-24）：生成器把订单生命周期的时间戳都截在 as_of_date 以内，退款不再发生在轴外。但口径上它按 paid_at / refunded_at 记账，跟 GMV 的下单日口径仍不是同一段日期，同时报多个指标时要写明区间
- 净收入不能用「毛收入 − refund_amount」自己算：refund_amount 是退款现金（含运费），毛收入不含运费，两者口径不同；直接用 net_revenue

## 构建口径（本表如何从基表算出）

> 方言为 Trino（Athena）。真源是 `schema_manifest.yaml` 里的 Postgres 写法，
> 由 `scripts/gen/pg_to_trino.py` 转换而来。

```sql
WITH paid AS (
  SELECT CAST(paid_at AS date) AS dt,
         sum(actual_amount - shipping_fee) AS gross_revenue
  FROM orders
  WHERE status IN ('paid','shipped','delivered','refunded') AND paid_at IS NOT NULL
  GROUP BY 1
),
refunds AS (
  SELECT CAST(refunded_at AS date) AS dt, sum(actual_amount) AS refund_amount,
         sum(actual_amount - shipping_fee) AS refund_revenue
  FROM orders WHERE status = 'refunded' AND refunded_at IS NOT NULL
  GROUP BY 1
)
SELECT COALESCE(p.dt, r.dt) AS dt,
       CAST(COALESCE(p.gross_revenue, 0) AS decimal(14,2)) AS gross_revenue,
       CAST(COALESCE(r.refund_amount, 0) AS decimal(14,2)) AS refund_amount,
       CAST((COALESCE(p.gross_revenue, 0) - COALESCE(r.refund_revenue, 0)) AS decimal(14,2)) AS net_revenue
FROM paid p FULL OUTER JOIN refunds r ON p.dt = r.dt
```
