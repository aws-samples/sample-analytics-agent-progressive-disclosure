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
| refund_amt | numeric | 当日退款额（status='refunded'，按 refunded_at）。⚠️ **合计不等于全量退款**，见「口径」 |
| new_subscriptions | int | 当日新增订阅（`start_date`） |

## 口径

- gmv / orders / paying_users 只算**有效订单**：`status IN ('paid','shipped','delivered')`。
- 时间锚点：用全局的 `(SELECT max(as_of_date) FROM meta_snapshot)`，**不要用
  `max(dt) FROM mart_daily_kpi`**。本表这两个值恰好相等（都是 2026-01-24），所以在这里
  写哪个都跑得出同样的数——但那是巧合，而"照抄邻居卡片的写法"是真会发生的：
  同为 mart 层的 `mart_channel_daily` 轴铺到 2026-09-01，同样的写法在那张表上会算出
  渠道 GMV = 0。锚点统一到一处，跨表的「最近 7 天」才是同一段日期。
- dau 是「日」活，跨多天看活跃要用 `avg(dau)`，不要把每天的 dau 相加。
- ⚠️ **`sum(refund_amt)` 不是全量退款，别用它回答「退款总额」。** 本表的日期轴由
  GMV 侧（`placed_at`）决定，不含 `refunded_at`；轴末之后才发生的退款在 LEFT JOIN
  时被丢掉，所以本表退款合计**偏低**。
  - 问「退款总额」「累计退款」「一共退了多少」→ 用 `fin_daily_revenue.refund_amount`，
    那张表按 `refunded_at` 建轴，是全量。
  - 本表的 `refund_amt` 只能回答「**某一天**退了多少」。
  - 这是从 v1 照搬的既有缺陷，不是本表写错了；两张表的差值就是轴外退款。

## 常用查询

### 每日 GMV / DAU 趋势
```sql
SELECT dt, gmv, dau, orders FROM mart_daily_kpi ORDER BY dt;
```

### 近 7 天 vs 前 7 天关键指标对比
```sql
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot)
SELECT CASE WHEN k.dt > a.d - interval '7' day THEN '近7天' ELSE '前7天' END AS period,
       round(avg(k.dau))      AS avg_dau,
       sum(k.new_users)       AS new_users,
       sum(k.orders)          AS orders,
       CAST(sum(k.gmv) AS integer)        AS gmv,
       sum(k.new_subscriptions) AS new_subs
FROM mart_daily_kpi k, a
WHERE k.dt > a.d - interval '14' day
GROUP BY 1 ORDER BY 1 DESC;
```

### 整月对比（残月别直接比）
```sql
SELECT date_format(dt, '%Y-%m') AS mon,
       round(avg(dau)) avg_dau, sum(new_users) new_users,
       sum(orders) orders, CAST(sum(gmv) AS integer) gmv
FROM mart_daily_kpi GROUP BY 1 ORDER BY 1;
```
