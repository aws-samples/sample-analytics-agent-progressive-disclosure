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
| applicable_products | `string` | 适用商品范围配置（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。**种子数据里整列为 NULL** |
| status | VARCHAR(20) | 优惠券状态 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### coupon_type 优惠券类型
| 值 | 说明 | 示例 | 实测行数 |
|----|------|------|------|
| fixed_amount | 固定金额减免 | 满100减20，discount_value=20 | 78 |
| percentage | 百分比折扣 | 8折优惠，discount_value=0.8 | 54 |
| free_shipping | 免运费券 | 免运费，discount_value=0 | 18 |

> 全表 150 行。满减券的值是 `fixed_amount`（**不是** `fixed`）、免邮券是
> `free_shipping`（**不是** `shipping`）——旧文档两个都写错了，按它筛是空集。

### status 优惠券状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 可领取、可使用 | 58 |
| expired | 已过期 | 58 |
| depleted | 已领完 | 17 |
| inactive | 已下架，不可领取 | 17 |

> 「已领完」的值是 `depleted`，**不是** `exhausted`；旧文档还漏了 `expired`
> （占比 39%，是并列最多的一档）。

### applicable_products 的 JSON 结构示例（列类型是 `string`）
> ⚠️ 这是**设计意图**，不是数据：这一列在种子数据里 **150 行全为 NULL**（登记在
> `scripts/lakehouse/verify_constants.py` 的清单里）。所以"哪些券能用在哪个品类"
> 这类问题在当前数据上答不出来，别按下面的结构去 `json_extract_scalar` 然后把
> 一列 NULL 当成"没有限制"。
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
WHERE start_date >= date '2024-01-01'
GROUP BY coupon_type
ORDER BY coupon_count DESC;
```

### 即将过期的优惠券
```sql
-- 「即将」相对静态样本的今天算（meta_snapshot.as_of_date），不是 CURRENT_DATE：
-- 真实今天早已超过样本窗口，用 CURRENT_DATE 这条查询永远返回空集且不报错。
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot)
SELECT
    coupon_id,
    coupon_name,
    coupon_type,
    discount_value,
    end_date,
    total_quota
FROM coupons CROSS JOIN a
WHERE status = 'active'
  AND end_date BETWEEN a.d AND a.d + interval '7' day
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
