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

| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 生效中 | 36 |
| expired | 已过期（到期未续费） | 14 |

> 全表 50 行，只有这两个值；`cancelled` 在业务上存在，但种子数据里没有。

### plan_name 订阅计划

| 值 | 说明 | 实测行数 |
|----|------|------|
| 月度会员 | 按月订阅 | 29 |
| 季度会员 | 按季订阅 | 12 |
| 年度会员 | 按年订阅 | 9 |

> **是中文值**，不是 `monthly_basic` 那套英文枚举（旧文档写过）。价格看 `plan_price` 列。

### cancel_reason 取消原因

> ⚠️ **这一列在种子数据里整列为 NULL**（50 行全空，因为没有 `cancelled` 状态的订阅）。
> 做流失原因分析在这份数据上无素材。

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
WHERE start_date >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '12' month
GROUP BY DATE_TRUNC('month', start_date)
ORDER BY start_month DESC;
```

### 取消原因分析
```sql
-- ⚠️ 在种子数据上这条查询返回空集：没有 cancelled 状态的订阅，cancel_reason 整列 NULL。
-- 保留它是为了说明写法，别把空结果解读成「没人取消订阅」。
SELECT
    plan_name,
    cancel_reason,
    COUNT(*) AS cancel_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(PARTITION BY plan_name), 2) AS pct
FROM subscriptions
WHERE status = 'cancelled'
  AND cancelled_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '90' day
GROUP BY plan_name, cancel_reason
ORDER BY plan_name, cancel_count DESC;
```
