# subscriptions - 订阅表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| subscription_id | BIGINT | 主键，订阅唯一标识 |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| plan_name | VARCHAR(100) | 订阅计划名称 |
| plan_price | DECIMAL(10,2) | 订阅价格 |
| start_date | DATE | 订阅开始日期 |
| end_date | DATE | 订阅结束日期 |
| auto_renew | BOOLEAN | 是否自动续费 |
| status | VARCHAR(20) | 订阅状态 |
| payment_id | BIGINT | 关联支付记录ID |
| cancelled_at | TIMESTAMP | 取消时间 |
| cancel_reason | VARCHAR(200) | 取消原因 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### status 订阅状态

| 值 | 说明 |
|----|------|
| active | 生效中 |
| cancelled | 已取消（用户主动取消） |
| expired | 已过期（到期未续费） |

### plan_name 常见订阅计划

| 值 | 说明 | 价格（示例） |
|----|------|--------------|
| monthly_basic | 月度基础会员 | 9.9 |
| monthly_premium | 月度高级会员 | 19.9 |
| yearly_basic | 年度基础会员 | 99 |
| yearly_premium | 年度高级会员 | 199 |

### cancel_reason 常见取消原因

| 值 | 说明 |
|----|------|
| too_expensive | 价格太贵 |
| not_useful | 功能不需要 |
| found_alternative | 找到替代产品 |
| temporary | 临时不需要 |
| other | 其他原因 |

## 索引

- PRIMARY KEY: `subscription_id`
- INDEX: `user_id`, `status`, `start_date`, `end_date`, `plan_name`

## 常用查询

### 订阅计划分布
```sql
SELECT
    plan_name,
    COUNT(*) AS subscription_count,
    COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_count,
    SUM(CASE WHEN status = 'active' THEN plan_price ELSE 0 END) AS monthly_revenue
FROM subscriptions
GROUP BY plan_name
ORDER BY active_count DESC;
```

### 订阅留存分析（按开始月份）
```sql
SELECT
    DATE_TRUNC('month', start_date) AS start_month,
    COUNT(*) AS total_subscriptions,
    COUNT(CASE WHEN status = 'active' THEN 1 END) AS still_active,
    COUNT(CASE WHEN status = 'cancelled' THEN 1 END) AS cancelled,
    COUNT(CASE WHEN status = 'expired' THEN 1 END) AS expired,
    ROUND(COUNT(CASE WHEN status = 'active' THEN 1 END) * 100.0 / COUNT(*), 2) AS retention_rate
FROM subscriptions
WHERE start_date >= CURRENT_DATE - INTERVAL '12 months'
GROUP BY DATE_TRUNC('month', start_date)
ORDER BY start_month DESC;
```

### 取消原因分析
```sql
SELECT
    plan_name,
    cancel_reason,
    COUNT(*) AS cancel_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(PARTITION BY plan_name), 2) AS pct
FROM subscriptions
WHERE status = 'cancelled'
  AND cancelled_at >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY plan_name, cancel_reason
ORDER BY plan_name, cancel_count DESC;
```
