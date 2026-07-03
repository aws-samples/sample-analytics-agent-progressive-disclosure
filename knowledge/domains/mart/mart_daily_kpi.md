# mart_daily_kpi - 每日业务大盘

## 用途

每日一行的业务总览。用于「最近业务怎么样」「周报」「关键指标环比」这类综合判断题。一张表拿到所有头部指标，不用再 join。

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | date | 自然日（粒度主键） |
| dau | int | 日活，`events` 去重 user_id |
| new_users | int | 当日注册新用户（`registered_at`） |
| orders | int | 当日有效订单数 |
| paying_users | int | 当日付费用户数（有效订单去重 user） |
| gmv | numeric | 当日 GMV，实付口径 |
| refund_amt | numeric | 当日退款额（status='refunded'，按 refunded_at） |
| new_subscriptions | int | 当日新增订阅（`start_date`） |

## 口径

- gmv / orders / paying_users 只算**有效订单**：`status IN ('paid','shipped','delivered')`。
- 时间锚点：以 `(SELECT max(dt) FROM mart_daily_kpi)` 为今天。
- dau 是「日」活，跨多天看活跃要用 `avg(dau)`，不要把每天的 dau 相加。

## 常用查询

### 每日 GMV / DAU 趋势
```sql
SELECT dt, gmv, dau, orders FROM mart_daily_kpi ORDER BY dt;
```

### 近 7 天 vs 前 7 天关键指标对比
```sql
WITH a AS (SELECT max(dt) AS d FROM mart_daily_kpi)
SELECT CASE WHEN k.dt > a.d - 7 THEN '近7天' ELSE '前7天' END AS period,
       round(avg(k.dau))      AS avg_dau,
       sum(k.new_users)       AS new_users,
       sum(k.orders)          AS orders,
       sum(k.gmv)::int        AS gmv,
       sum(k.new_subscriptions) AS new_subs
FROM mart_daily_kpi k, a
WHERE k.dt > a.d - 14
GROUP BY 1 ORDER BY 1 DESC;
```

### 整月对比（残月别直接比）
```sql
SELECT to_char(dt,'YYYY-MM') AS mon,
       round(avg(dau)) avg_dau, sum(new_users) new_users,
       sum(orders) orders, sum(gmv)::int gmv
FROM mart_daily_kpi GROUP BY 1 ORDER BY 1;
```
