# 核心指标计算

## 活跃指标

### DAU (日活跃用户)
```sql
-- 基于最后活跃时间（users 里没有 last_login_at，只有 last_active_at）
-- 注意这是**当前快照**：每个用户只有一行，只能看出"最后一次活跃在哪天"，
-- 不是真 DAU。真 DAU 用下面那段基于 events 的写法。
SELECT
    DATE(last_active_at) AS date,
    COUNT(DISTINCT user_id) AS dau
FROM users
WHERE last_active_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(last_active_at)
ORDER BY date;
```

```sql
-- 基于事件（更准确）
SELECT
    DATE(event_time) AS date,
    COUNT(DISTINCT user_id) AS dau
FROM events
WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(event_time)
ORDER BY date;
```

### WAU (周活跃用户)
```sql
SELECT
    DATE_TRUNC('week', event_time) AS week,
    COUNT(DISTINCT user_id) AS wau
FROM events
WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '84' day
GROUP BY DATE_TRUNC('week', event_time)
ORDER BY week;
```

### MAU (月活跃用户)
```sql
SELECT
    DATE_TRUNC('month', event_time) AS month,
    COUNT(DISTINCT user_id) AS mau
FROM events
WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '12' month
GROUP BY DATE_TRUNC('month', event_time)
ORDER BY month;
```

### DAU/MAU 比率 (粘性指标)
```sql
WITH daily AS (
    SELECT
        DATE(event_time) AS date,
        COUNT(DISTINCT user_id) AS dau
    FROM events
    WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
    GROUP BY DATE(event_time)
),
monthly AS (
    SELECT COUNT(DISTINCT user_id) AS mau
    FROM events
    WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
)
SELECT
    d.date,
    d.dau,
    m.mau,
    ROUND(CAST(d.dau AS decimal(38,6)) / m.mau * 100, 2) AS stickiness_pct
FROM daily d, monthly m
ORDER BY d.date;
```

## 留存指标

### 新用户留存率
```sql
-- cohort 时间列是 registered_at，不是 created_at（两列都存在且 500/500 行不等，
-- 写错了 EXPLAIN 照样过）。时间锚用 meta_snapshot，不用 CURRENT_DATE。
-- 分子里数的是 c.user_id（cohort 成员），不是 a.user_id——这是硬约束。
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
cohort AS (
    SELECT user_id, CAST(registered_at AS date) AS cohort_date
    FROM users, a
    WHERE CAST(registered_at AS date) > a.d - interval '30' day
),
activity AS (
    SELECT DISTINCT user_id, CAST(event_time AS date) AS activity_date
    FROM events
)
SELECT
    c.cohort_date,
    COUNT(DISTINCT c.user_id) AS cohort_size,
    COUNT(DISTINCT CASE WHEN date_diff('day', c.cohort_date, act.activity_date) = 1 THEN c.user_id END) AS day1_retained,
    COUNT(DISTINCT CASE WHEN date_diff('day', c.cohort_date, act.activity_date) = 7 THEN c.user_id END) AS day7_retained,
    COUNT(DISTINCT CASE WHEN date_diff('day', c.cohort_date, act.activity_date) = 30 THEN c.user_id END) AS day30_retained,
    ROUND(CAST(COUNT(DISTINCT CASE WHEN date_diff('day', c.cohort_date, act.activity_date) = 1
                                   THEN c.user_id END) AS decimal(38,6)) /
          COUNT(DISTINCT c.user_id) * 100, 2) AS day1_retention_pct
FROM cohort c
LEFT JOIN activity act ON act.user_id = c.user_id
GROUP BY c.cohort_date
ORDER BY c.cohort_date
```

⚠️ **按天分 cohort 的样本量取决于湖里装的是哪一批**：种子样本只有 500 个用户，摊到 30 天
每天 2–12 人，实测某天 3/5 = **60% 次日留存**、隔一天又是 0%，全是分母噪声；全量批次是
21 万用户，每天数千人，噪声不再是问题。你**不知道**当前装的是哪一批，所以规矩是一样的：
优先用下面的**按周** cohort，并且**总是把 `cohort_size` 一起给出来**，让分母自己说话。

### 留存矩阵

> ⚠️ **cohort 时间列是 `registered_at`，不是 `created_at`。** `users` 两列都有，
> 所以写错了 EXPLAIN 照样通过、SQL 照样出结果——但**这份数据里 500/500 行两列都不相等**，
> 用 `created_at` 分出来的是另一批 cohort。旧版本这段就是错的。
>
> ⚠️ **算完先看曲线是不是衰减的，再决定结论怎么写。** 「这份数据的留存结论不可用」
> 是**某一批**数据的性质，不是永远成立的结论：v1 种子样本的活跃度与注册生命周期独立抽样，
> 观测窗完整的 cohort 第 0→4 周是 `45.0% / 43.0% / 42.1% / 41.1% / 43.0%`，近乎水平且末周
> 回升——那时**平坦不等于"留得住"**，数字可以给但必须同时说明这份数据算不出留存；
> 全量重灌批次给用户配了活跃半衰期，同口径实测 `71.6 → 51.9 → 42.3 → 37.2`，是正常衰减，
> 这时照常报结论、别再照抄那句警告。判据：**W4/W1 ≥ 0.8 视为不衰减**。完整口径与两种
> 落笔方式见 `analysis/retention_curve.md`——**答留存题请连它一起读**，哪怕只是取数。

```sql
-- 分子必须限定在 cohort 成员内（LEFT JOIN 上 cohort 名单，再数 c.user_id）。
-- 只按时间筛活跃、不 JOIN cohort，留存率会超过 100%（实测过 509%）。
-- 时间锚在 meta_snapshot.as_of_date，不用 CURRENT_DATE（静态快照，会查空）。
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
cohort AS (
    SELECT user_id, DATE_TRUNC('week', CAST(registered_at AS date)) AS cohort_week
    FROM users, a
    WHERE CAST(registered_at AS date) > a.d - interval '56' day
),
activity AS (
    SELECT DISTINCT user_id, DATE_TRUNC('week', CAST(event_time AS date)) AS activity_week
    FROM events
)
SELECT
    c.cohort_week,
    COUNT(DISTINCT c.user_id) AS cohort_size,
    COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week) = 1
                        THEN c.user_id END) AS week1,
    COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week) = 2
                        THEN c.user_id END) AS week2,
    COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week) = 3
                        THEN c.user_id END) AS week3,
    COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week) = 4
                        THEN c.user_id END) AS week4
FROM cohort c
LEFT JOIN activity act ON act.user_id = c.user_id
GROUP BY c.cohort_week
ORDER BY c.cohort_week
```

最近几个 cohort 的 week3/week4 会是 0——那是**右删失**（观测窗未到），不是真实下跌。
但右删失只解释边缘的 0，**不解释中间那片为什么是平的**，两条都要说。

## 转化指标

### 注册转化漏斗

> ⚠️ **这份数据里做不出注册漏斗**：埋点只有一个 `register` 事件，没有
> `register_page_view` / `register_submit` / `register_success` 这三级
> （旧文档写过，按它们筛全是 0）。能算的只是「打开 APP 的人里有多少触发了注册」：

```sql
WITH funnel AS (
    SELECT
        COUNT(DISTINCT CASE WHEN event_name = 'app_open' THEN user_id END) AS app_opens,
        COUNT(DISTINCT CASE WHEN event_name = 'register' THEN user_id END) AS registers
    FROM events
    WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
)
SELECT
    app_opens,
    registers,
    ROUND(CAST(registers AS decimal(38,6)) / NULLIF(app_opens, 0) * 100, 2) AS register_rate
FROM funnel;
```

### 购买转化漏斗

> ⚠️ 事件名是 `view_product` / `begin_checkout`（不是 `product_view` / `checkout_start`），
> 成交事件是 `purchase`（不是 `payment_success`）——全 25 种见
> `domains/behavior/events.md`。
>
> ⚠️ **口径硬约束**：第 i+1 步只保留「也做过第 i 步」的用户（`AND user_id IN (上一步)`）。
> 每步各 `count(DISTINCT user_id)` 一遍**不是漏斗**——那样会得到
> `394/385/396/373`（结算 > 浏览），然后被误读成「这份种子数据的转化率不衰减」。
> 事件量近似均匀（736–866）说的是**每种事件各自的行数**，跟漏斗形态是两件事：
> 同一份数据按子集口径算是 `394 → 314 → 258 → 199`，逐层流失 20%/18%/23%，
> 单调递减。完整方法见 `analysis/funnel_analysis.md`。
>
> ⚠️ **绝对值不具参考性**（口径对了也一样）：本数据集的转化率显著高于真实业务——
> 全量 `199/394 = 50.5%`、近 30 天 `32/197 = 16.2%`，真实电商同口径通常在
> 个位数百分比。分母缺的是"看过就走"那一侧：浏览过商品的 394 人里只有 80 人
> （20.3%）一次都没加购。**形态可以用**（定位瓶颈环节、比较人群差异），
> **绝对值不要写成业务结论**。但这条只针对绝对值——单调性不成立依然只能是
> 上面那条约束没做到，不许拿它去解释。详见 `analysis/funnel_analysis.md`
> 的「口径声明」。

```sql
-- 无序子集漏斗（本项目默认口径）。近 30 天；问句没点明时间范围时按全量算，
-- 把 w 里那两行时间条件整段删掉。
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
w AS (
    SELECT user_id, event_name
    FROM events, a
    WHERE CAST(event_time AS date) > a.d - interval '30' day
      AND CAST(event_time AS date) <= a.d
      AND event_name IN ('view_product', 'add_to_cart', 'begin_checkout', 'purchase')
),
s1 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'view_product'),
s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'add_to_cart'
       AND user_id IN (SELECT user_id FROM s1)),
s3 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'begin_checkout'
       AND user_id IN (SELECT user_id FROM s2)),
s4 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'purchase'
       AND user_id IN (SELECT user_id FROM s3))
SELECT (SELECT count(*) FROM s1) AS viewers,
       (SELECT count(*) FROM s2) AS cart_adds,
       (SELECT count(*) FROM s3) AS checkout_starts,
       (SELECT count(*) FROM s4) AS purchasers,
       ROUND(CAST((SELECT count(*) FROM s2) AS decimal(38,6))
             / NULLIF((SELECT count(*) FROM s1), 0) * 100, 2) AS view_to_cart_pct,
       ROUND(CAST((SELECT count(*) FROM s3) AS decimal(38,6))
             / NULLIF((SELECT count(*) FROM s2), 0) * 100, 2) AS cart_to_checkout_pct,
       ROUND(CAST((SELECT count(*) FROM s4) AS decimal(38,6))
             / NULLIF((SELECT count(*) FROM s3), 0) * 100, 2) AS checkout_to_purchase_pct,
       ROUND(CAST((SELECT count(*) FROM s4) AS decimal(38,6))
             / NULLIF((SELECT count(*) FROM s1), 0) * 100, 2) AS overall_conversion_pct
```

## 内容指标

### 内容互动率
```sql
SELECT
    p.content_type,
    COUNT(*) AS post_count,
    AVG(p.view_count) AS avg_views,
    AVG(p.like_count) AS avg_likes,
    AVG(p.comment_count) AS avg_comments,
    AVG(p.share_count) AS avg_shares,
    ROUND(CAST(AVG(p.like_count) AS decimal(38,6)) / NULLIF(AVG(p.view_count), 0) * 100, 2) AS like_rate,
    ROUND(CAST(AVG(p.comment_count) AS decimal(38,6)) / NULLIF(AVG(p.view_count), 0) * 100, 2) AS comment_rate
FROM posts p
WHERE p.status = 'published'
GROUP BY p.content_type
ORDER BY avg_views DESC;
```

### 创作者活跃度
```sql
SELECT
    DATE_TRUNC('week', created_at) AS week,
    COUNT(DISTINCT user_id) AS active_creators,
    COUNT(*) AS total_posts,
    ROUND(CAST(COUNT(*) AS decimal(38,6)) / COUNT(DISTINCT user_id), 2) AS posts_per_creator
FROM posts
WHERE status = 'published'
  AND created_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '84' day
GROUP BY DATE_TRUNC('week', created_at)
ORDER BY week;
```

## 使用说明

1. **时间范围**: 所有查询的时间范围可根据需求调整。注意本库是**静态样本**，业务日历截止
   2026-01-24，所以问"最近 N 天"时统一用 `(SELECT max(as_of_date) FROM meta_snapshot)`
   作锚点——本文件里的例子已经全部这么写了，`CURRENT_DATE` / `NOW()` 在 Trino 里语法合法，
   写了不报错，只会安静地返回 0 行。
   **别用各表自己的 `max(dt)`**：`fin_daily_revenue` 到 2026-02-02、
   `channel_daily_costs`（时间列叫 `date` 不叫 `dt`）和 `mart_channel_daily` 到 2026-09-01，
   按各自的 max 取"最近 30 天"会得到互相错位的窗口，跨表一对账就是对不上。见 connection.md
2. **性能优化**: 引擎是 Athena（Trino）查 Iceberg 表，**没有索引**这回事。省钱省时间靠
   两点：只 SELECT 用到的列（列式存储按列计费），以及在时间列上加范围条件让 Iceberg
   跳过用不到的文件
3. **空值处理**: 使用 `NULLIF` 避免除零错误
4. **数据类型**: 整数相除会**截断**（`7/2` = 3），算百分比前先
   `CAST(x AS decimal(38,6))`。注意别写成裸的 `CAST(x AS decimal)`——那等于
   `decimal(38,0)`，标度是 0，小数会被抹掉
