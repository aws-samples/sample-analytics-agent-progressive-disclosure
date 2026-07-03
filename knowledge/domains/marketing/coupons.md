# coupons - 优惠券模板表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| coupon_id | INT | 优惠券ID，主键，自增 |
| coupon_code | VARCHAR(50) | 优惠券码 |
| coupon_name | VARCHAR(200) | 优惠券名称 |
| coupon_type | VARCHAR(20) | 优惠券类型 |
| discount_value | DECIMAL(10,2) | 优惠值（金额或折扣率） |
| min_purchase | DECIMAL(10,2) | 最低消费门槛 |
| max_discount | DECIMAL(10,2) | 最高优惠金额（针对百分比类型） |
| valid_days | INT | 有效天数（从领取时计算） |
| start_date | TIMESTAMP | 发放开始时间 |
| end_date | TIMESTAMP | 发放结束时间 |
| total_quota | INT | 总发放数量限制 |
| per_user_limit | INT | 每用户领取限制 |
| applicable_products | JSONB | 适用商品范围配置 |
| status | VARCHAR(20) | 优惠券状态 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### coupon_type 优惠券类型
| 值 | 说明 | 示例 |
|----|------|------|
| fixed | 固定金额减免 | 满100减20，discount_value=20 |
| percentage | 百分比折扣 | 8折优惠，discount_value=0.8 |
| shipping | 免运费券 | 免运费，discount_value=0 |

### status 优惠券状态
| 值 | 说明 |
|----|------|
| active | 可领取、可使用 |
| inactive | 已下架，不可领取 |
| exhausted | 已领完 |

### applicable_products JSONB 结构示例
```json
{
  "type": "category",
  "category_ids": [1, 2, 3],
  "exclude_product_ids": [100, 101]
}
```
或
```json
{
  "type": "all",
  "exclude_category_ids": [5]
}
```

## 索引

- PRIMARY KEY: `coupon_id`
- UNIQUE: `coupon_code`
- INDEX: `status`, `start_date`, `end_date`, `coupon_type`

## 常用查询

### 优惠券发放统计
```sql
SELECT
    coupon_type,
    COUNT(*) AS coupon_count,
    AVG(discount_value) AS avg_discount,
    SUM(total_quota) AS total_issued_quota
FROM coupons
WHERE start_date >= '2024-01-01'
GROUP BY coupon_type
ORDER BY coupon_count DESC;
```

### 即将过期的优惠券
```sql
SELECT
    coupon_id,
    coupon_name,
    coupon_type,
    discount_value,
    end_date,
    total_quota
FROM coupons
WHERE status = 'active'
  AND end_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
ORDER BY end_date;
```

### 高门槛优惠券分析
```sql
SELECT
    coupon_id,
    coupon_name,
    coupon_type,
    discount_value,
    min_purchase,
    ROUND(discount_value / NULLIF(min_purchase, 0) * 100, 2) AS discount_rate_pct
FROM coupons
WHERE status = 'active'
  AND min_purchase >= 100
ORDER BY min_purchase DESC;
```
