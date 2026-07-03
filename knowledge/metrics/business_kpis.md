# 业务 KPI 计算

## 收入指标

### GMV (成交总额)
```sql
-- 日 GMV
SELECT
    DATE(created_at) AS date,
    COUNT(DISTINCT order_id) AS orders,
    SUM(total_amount) AS gmv,
    AVG(total_amount) AS aov
FROM orders
WHERE status IN ('paid', 'shipped', 'delivered')
  AND created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(created_at)
ORDER BY date;
```

```sql
-- 月度 GMV 趋势
SELECT
    DATE_TRUNC('month', created_at) AS month,
    SUM(total_amount) AS gmv,
    COUNT(DISTINCT order_id) AS orders,
    COUNT(DISTINCT user_id) AS buyers,
    SUM(total_amount) / COUNT(DISTINCT user_id) AS revenue_per_buyer
FROM orders
WHERE status IN ('paid', 'shipped', 'delivered')
GROUP BY DATE_TRUNC('month', created_at)
ORDER BY month;
```

### ARPU (每用户平均收入)
```sql
WITH period_revenue AS (
    SELECT SUM(total_amount) AS total_revenue
    FROM orders
    WHERE status IN ('paid', 'shipped', 'delivered')
      AND created_at >= CURRENT_DATE - INTERVAL '30 days'
),
period_users AS (
    SELECT COUNT(DISTINCT user_id) AS active_users
    FROM events
    WHERE event_time >= CURRENT_DATE - INTERVAL '30 days'
)
SELECT
    pr.total_revenue,
    pu.active_users,
    ROUND(pr.total_revenue / pu.active_users, 2) AS arpu
FROM period_revenue pr, period_users pu;
```

### ARPPU (每付费用户平均收入)
```sql
SELECT
    DATE_TRUNC('month', created_at) AS month,
    SUM(total_amount) AS revenue,
    COUNT(DISTINCT user_id) AS paying_users,
    ROUND(SUM(total_amount) / COUNT(DISTINCT user_id), 2) AS arppu
FROM orders
WHERE status IN ('paid', 'shipped', 'delivered')
GROUP BY DATE_TRUNC('month', created_at)
ORDER BY month;
```

## 用户价值指标

### LTV (用户生命周期价值) - 简化计算
```sql
WITH user_value AS (
    SELECT
        u.user_id,
        u.created_at AS register_date,
        COALESCE(SUM(o.total_amount), 0) AS total_spend,
        COUNT(DISTINCT o.order_id) AS order_count,
        EXTRACT(DAY FROM NOW() - u.created_at) AS days_since_register
    FROM users u
    LEFT JOIN orders o ON u.user_id = o.user_id
        AND o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY u.user_id, u.created_at
)
SELECT
    CASE
        WHEN days_since_register <= 30 THEN '0-30天'
        WHEN days_since_register <= 90 THEN '31-90天'
        WHEN days_since_register <= 180 THEN '91-180天'
        ELSE '180天+'
    END AS cohort,
    COUNT(*) AS users,
    COUNT(CASE WHEN order_count > 0 THEN 1 END) AS paying_users,
    ROUND(AVG(total_spend), 2) AS avg_ltv,
    ROUND(AVG(CASE WHEN order_count > 0 THEN total_spend END), 2) AS avg_paying_ltv
FROM user_value
GROUP BY 1
ORDER BY 1;
```

### 付费率
```sql
WITH monthly_stats AS (
    SELECT
        DATE_TRUNC('month', u.created_at) AS cohort_month,
        COUNT(DISTINCT u.user_id) AS total_users,
        COUNT(DISTINCT o.user_id) AS paying_users
    FROM users u
    LEFT JOIN orders o ON u.user_id = o.user_id
        AND o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY DATE_TRUNC('month', u.created_at)
)
SELECT
    cohort_month,
    total_users,
    paying_users,
    ROUND(paying_users::numeric / total_users * 100, 2) AS pay_rate_pct
FROM monthly_stats
ORDER BY cohort_month;
```

## 渠道效果指标

### CAC (获客成本) 按渠道
```sql
WITH channel_spend AS (
    -- 假设从 ad_campaigns 获取投放成本
    SELECT
        channel,
        SUM(budget) AS total_spend
    FROM ad_campaigns
    WHERE status = 'completed'
    GROUP BY channel
),
channel_users AS (
    SELECT
        channel,
        COUNT(DISTINCT user_id) AS acquired_users
    FROM user_attributions
    WHERE attribution_type = 'first_touch'
    GROUP BY channel
)
SELECT
    cu.channel,
    cs.total_spend,
    cu.acquired_users,
    ROUND(cs.total_spend / NULLIF(cu.acquired_users, 0), 2) AS cac
FROM channel_users cu
LEFT JOIN channel_spend cs ON cu.channel = cs.channel
ORDER BY cac;
```

### ROI (投资回报率) 按广告活动
```sql
WITH campaign_revenue AS (
    SELECT
        ua.first_touch_campaign AS campaign_id,
        SUM(o.total_amount) AS revenue
    FROM user_attributions ua
    JOIN orders o ON ua.user_id = o.user_id
    WHERE o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY ua.first_touch_campaign
)
SELECT
    ac.campaign_id,
    ac.campaign_name,
    ac.budget AS spend,
    COALESCE(cr.revenue, 0) AS revenue,
    ROUND((COALESCE(cr.revenue, 0) - ac.budget) / NULLIF(ac.budget, 0) * 100, 2) AS roi_pct
FROM ad_campaigns ac
LEFT JOIN campaign_revenue cr ON ac.campaign_id = cr.campaign_id
WHERE ac.status = 'completed'
ORDER BY roi_pct DESC;
```

## 运营活动效果

### 活动目标用户覆盖
```sql
-- 基于 target_segment_ids 计算活动覆盖的目标用户数
SELECT
    c.campaign_id,
    c.campaign_name,
    c.campaign_type,
    c.status,
    COUNT(DISTINCT usm.user_id) AS target_users
FROM campaigns c
LEFT JOIN user_segment_members usm ON usm.segment_id = ANY(c.target_segment_ids)
    AND usm.exited_at IS NULL
WHERE c.status IN ('active', 'completed')
GROUP BY c.campaign_id, c.campaign_name, c.campaign_type, c.status
ORDER BY target_users DESC;
```

### 优惠券核销率
```sql
SELECT
    coupon_type,
    COUNT(*) AS total_issued,
    COUNT(CASE WHEN status = 'used' THEN 1 END) AS used,
    COUNT(CASE WHEN status = 'expired' THEN 1 END) AS expired,
    ROUND(COUNT(CASE WHEN status = 'used' THEN 1 END)::numeric / COUNT(*) * 100, 2) AS redemption_rate
FROM coupons
GROUP BY coupon_type
ORDER BY redemption_rate DESC;
```

## A/B 实验效果

### 实验关键指标对比
```sql
WITH experiment_metrics AS (
    SELECT
        ata.test_id,
        ata.variant_id,
        COUNT(DISTINCT ata.user_id) AS users,
        COUNT(DISTINCT o.order_id) AS orders,
        COALESCE(SUM(o.total_amount), 0) AS revenue
    FROM ab_test_assignments ata
    LEFT JOIN orders o ON ata.user_id = o.user_id
        AND o.created_at >= ata.assigned_at
    WHERE ata.test_id = 1  -- 指定实验ID
    GROUP BY ata.test_id, ata.variant_id
)
SELECT
    variant_id,
    users,
    orders,
    ROUND(orders::numeric / users * 100, 2) AS conversion_rate,
    revenue,
    ROUND(revenue / NULLIF(users, 0), 2) AS revenue_per_user
FROM experiment_metrics
ORDER BY variant_id;
```

## 商品分析

### 商品销售排行
```sql
SELECT
    p.product_id,
    p.product_name,
    pc.category_name,
    SUM(oi.quantity) AS total_sold,
    SUM(oi.subtotal) AS total_revenue,
    COUNT(DISTINCT oi.order_id) AS order_count
FROM order_items oi
JOIN products p ON oi.product_id = p.product_id
JOIN categories pc ON p.category_id = pc.category_id
JOIN orders o ON oi.order_id = o.order_id
WHERE o.status IN ('paid', 'shipped', 'delivered')
  AND o.created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY p.product_id, p.product_name, pc.category_name
ORDER BY total_revenue DESC
LIMIT 20;
```

### 品类销售占比
```sql
WITH category_sales AS (
    SELECT
        pc.category_name,
        SUM(oi.subtotal) AS revenue
    FROM order_items oi
    JOIN products p ON oi.product_id = p.product_id
    JOIN categories pc ON p.category_id = pc.category_id
    JOIN orders o ON oi.order_id = o.order_id
    WHERE o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY pc.category_name
)
SELECT
    category_name,
    revenue,
    ROUND(revenue / SUM(revenue) OVER() * 100, 2) AS revenue_pct
FROM category_sales
ORDER BY revenue DESC;
```

## 使用说明

1. **指标口径**: 订单状态筛选 `('paid', 'shipped', 'delivered')` 代表有效订单
2. **时间维度**: 可灵活调整为日/周/月
3. **归因模型**: 示例使用 first_touch，可改为 last_touch 或其他模型
4. **数据精度**: 金额计算保留2位小数
