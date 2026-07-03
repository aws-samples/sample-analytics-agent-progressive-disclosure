# 核心指标计算

## 活跃指标

### DAU (日活跃用户)
```sql
-- 基于登录时间
SELECT
    DATE(last_login_at) AS date,
    COUNT(DISTINCT user_id) AS dau
FROM users
WHERE last_login_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(last_login_at)
ORDER BY date;
```

```sql
-- 基于事件（更准确）
SELECT
    DATE(event_time) AS date,
    COUNT(DISTINCT user_id) AS dau
FROM events
WHERE event_time >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(event_time)
ORDER BY date;
```

### WAU (周活跃用户)
```sql
SELECT
    DATE_TRUNC('week', event_time) AS week,
    COUNT(DISTINCT user_id) AS wau
FROM events
WHERE event_time >= CURRENT_DATE - INTERVAL '12 weeks'
GROUP BY DATE_TRUNC('week', event_time)
ORDER BY week;
```

### MAU (月活跃用户)
```sql
SELECT
    DATE_TRUNC('month', event_time) AS month,
    COUNT(DISTINCT user_id) AS mau
FROM events
WHERE event_time >= CURRENT_DATE - INTERVAL '12 months'
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
    WHERE event_time >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY DATE(event_time)
),
monthly AS (
    SELECT COUNT(DISTINCT user_id) AS mau
    FROM events
    WHERE event_time >= CURRENT_DATE - INTERVAL '30 days'
)
SELECT
    d.date,
    d.dau,
    m.mau,
    ROUND(d.dau::numeric / m.mau * 100, 2) AS stickiness_pct
FROM daily d, monthly m
ORDER BY d.date;
```

## 留存指标

### 新用户留存率
```sql
WITH cohort AS (
    SELECT
        user_id,
        DATE(created_at) AS cohort_date
    FROM users
    WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
),
activity AS (
    SELECT
        user_id,
        DATE(event_time) AS activity_date
    FROM events
)
SELECT
    c.cohort_date,
    COUNT(DISTINCT c.user_id) AS cohort_size,
    COUNT(DISTINCT CASE WHEN a.activity_date = c.cohort_date + 1 THEN c.user_id END) AS day1_retained,
    COUNT(DISTINCT CASE WHEN a.activity_date = c.cohort_date + 7 THEN c.user_id END) AS day7_retained,
    COUNT(DISTINCT CASE WHEN a.activity_date = c.cohort_date + 30 THEN c.user_id END) AS day30_retained,
    ROUND(COUNT(DISTINCT CASE WHEN a.activity_date = c.cohort_date + 1 THEN c.user_id END)::numeric /
          COUNT(DISTINCT c.user_id) * 100, 2) AS day1_retention_pct
FROM cohort c
LEFT JOIN activity a ON c.user_id = a.user_id
GROUP BY c.cohort_date
ORDER BY c.cohort_date;
```

### 留存矩阵
```sql
WITH cohort AS (
    SELECT
        user_id,
        DATE_TRUNC('week', created_at) AS cohort_week
    FROM users
    WHERE created_at >= CURRENT_DATE - INTERVAL '8 weeks'
),
activity AS (
    SELECT
        user_id,
        DATE_TRUNC('week', event_time) AS activity_week
    FROM events
)
SELECT
    c.cohort_week,
    COUNT(DISTINCT c.user_id) AS cohort_size,
    COUNT(DISTINCT CASE WHEN a.activity_week = c.cohort_week + INTERVAL '1 week' THEN c.user_id END) AS week1,
    COUNT(DISTINCT CASE WHEN a.activity_week = c.cohort_week + INTERVAL '2 weeks' THEN c.user_id END) AS week2,
    COUNT(DISTINCT CASE WHEN a.activity_week = c.cohort_week + INTERVAL '3 weeks' THEN c.user_id END) AS week3,
    COUNT(DISTINCT CASE WHEN a.activity_week = c.cohort_week + INTERVAL '4 weeks' THEN c.user_id END) AS week4
FROM cohort c
LEFT JOIN activity a ON c.user_id = a.user_id
GROUP BY c.cohort_week
ORDER BY c.cohort_week;
```

## 转化指标

### 注册转化漏斗
```sql
WITH funnel AS (
    SELECT
        COUNT(DISTINCT CASE WHEN event_name = 'app_open' THEN user_id END) AS app_opens,
        COUNT(DISTINCT CASE WHEN event_name = 'register_page_view' THEN user_id END) AS register_views,
        COUNT(DISTINCT CASE WHEN event_name = 'register_submit' THEN user_id END) AS register_submits,
        COUNT(DISTINCT CASE WHEN event_name = 'register_success' THEN user_id END) AS register_success
    FROM events
    WHERE event_time >= CURRENT_DATE - INTERVAL '7 days'
)
SELECT
    app_opens,
    register_views,
    ROUND(register_views::numeric / app_opens * 100, 2) AS view_rate,
    register_submits,
    ROUND(register_submits::numeric / register_views * 100, 2) AS submit_rate,
    register_success,
    ROUND(register_success::numeric / register_submits * 100, 2) AS success_rate
FROM funnel;
```

### 购买转化漏斗
```sql
WITH daily_funnel AS (
    SELECT
        DATE(event_time) AS date,
        COUNT(DISTINCT CASE WHEN event_name = 'product_view' THEN user_id END) AS viewers,
        COUNT(DISTINCT CASE WHEN event_name = 'add_to_cart' THEN user_id END) AS cart_adds,
        COUNT(DISTINCT CASE WHEN event_name = 'checkout_start' THEN user_id END) AS checkout_starts,
        COUNT(DISTINCT CASE WHEN event_name = 'payment_success' THEN user_id END) AS purchasers
    FROM events
    WHERE event_time >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY DATE(event_time)
)
SELECT
    date,
    viewers,
    cart_adds,
    ROUND(cart_adds::numeric / NULLIF(viewers, 0) * 100, 2) AS view_to_cart_pct,
    checkout_starts,
    ROUND(checkout_starts::numeric / NULLIF(cart_adds, 0) * 100, 2) AS cart_to_checkout_pct,
    purchasers,
    ROUND(purchasers::numeric / NULLIF(checkout_starts, 0) * 100, 2) AS checkout_to_purchase_pct,
    ROUND(purchasers::numeric / NULLIF(viewers, 0) * 100, 2) AS overall_conversion_pct
FROM daily_funnel
ORDER BY date;
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
    ROUND(AVG(p.like_count)::numeric / NULLIF(AVG(p.view_count), 0) * 100, 2) AS like_rate,
    ROUND(AVG(p.comment_count)::numeric / NULLIF(AVG(p.view_count), 0) * 100, 2) AS comment_rate
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
    ROUND(COUNT(*)::numeric / COUNT(DISTINCT user_id), 2) AS posts_per_creator
FROM posts
WHERE status = 'published'
  AND created_at >= CURRENT_DATE - INTERVAL '12 weeks'
GROUP BY DATE_TRUNC('week', created_at)
ORDER BY week;
```

## 使用说明

1. **时间范围**: 所有查询的时间范围可根据需求调整
2. **性能优化**: 对于大数据量，建议添加适当索引
3. **空值处理**: 使用 `NULLIF` 避免除零错误
4. **数据类型**: 注意 `::numeric` 转换以获得精确的百分比
