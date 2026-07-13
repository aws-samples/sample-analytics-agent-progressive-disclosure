# dws_user_daily - 用户日汇总

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：DWS 汇总层
**粒度**：一行 = 一个用户 × 一天（仅该用户当天有事件/订单时存在）

用户日汇总：每用户每天的事件数、会话数、下单数、消费额

## 何时用这张表

- ✅ 用户粒度的日活跃分析、按用户聚合的时序（如"某用户最近的活跃趋势"）
- ❌ 全站日指标（用 mart_daily_kpi，口径已冻结）；用户终身汇总（用 mart_user_summary）

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 用户ID |
| dt | DATE | 日期 |
| event_cnt | BIGINT | 当天事件数 |
| session_cnt | BIGINT | 当天会话数 |
| order_cnt | BIGINT | 当天下单数（含无效单） |
| paid_amount | NUMERIC(14 | 当天有效订单实付金额 |

## 注意（口径与坑）

- 稀疏表：用户当天无活动则无行；算"平均每天"时分母要用日历天数而非行数
- order_cnt 含无效单，paid_amount 只含有效单——两列口径不同是有意的

## 构建口径（本表如何从基表算出）

```sql
WITH ev AS (
  SELECT user_id, event_time::date AS dt,
         count(*) AS event_cnt, count(DISTINCT session_id) AS session_cnt
  FROM events WHERE user_id IS NOT NULL GROUP BY 1, 2
),
od AS (
  SELECT user_id, placed_at::date AS dt,
         count(*) AS order_cnt,
         sum(actual_amount) FILTER (WHERE status IN ('paid','shipped','delivered')) AS paid_amount
  FROM orders GROUP BY 1, 2
)
SELECT COALESCE(ev.user_id, od.user_id) AS user_id,
       COALESCE(ev.dt, od.dt)           AS dt,
       COALESCE(ev.event_cnt, 0)        AS event_cnt,
       COALESCE(ev.session_cnt, 0)      AS session_cnt,
       COALESCE(od.order_cnt, 0)        AS order_cnt,
       COALESCE(od.paid_amount, 0)::numeric(14,2) AS paid_amount
FROM ev FULL OUTER JOIN od ON ev.user_id = od.user_id AND ev.dt = od.dt
```
