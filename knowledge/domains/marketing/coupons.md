# coupons - 优惠券模板表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| coupon_id | INT | 优惠券ID，主键，自增 |
| coupon_code | VARCHAR(50) | 优惠券码 |
| coupon_name | VARCHAR(200) | 优惠券名称 |
| coupon_type | VARCHAR(20) | 优惠券类型 |
| discount_value | DECIMAL(10,2) | 优惠值：满减券 = 减免金额；折扣券 = **减百分之几**（20 = 打 8 折）；包邮券 = 0 |
| min_purchase | DECIMAL(10,2) | 最低消费门槛 |
| max_discount | DECIMAL(10,2) | 最高优惠金额（针对百分比类型） |
| valid_days | INT | 有效天数（从领取时计算）：`user_coupons.expire_at = received_at + valid_days 天`，取值 7 / 15 / 30 |
| start_date | TIMESTAMP | 券的生效开始时间：领券、用券都不早于它 |
| end_date | TIMESTAMP | 券的生效结束时间：用券下单都不晚于它 |
| total_quota | INT | 总发放数量限制；每张券在 `user_coupons` 里的实发行数 ≤ 它，`depleted` 的券恰好发满 |
| per_user_limit | INT | 每用户领取限制 |
| applicable_products | `string` | 适用商品范围配置（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。**当前数据里整列为 NULL**（150 行全空） |
| status | VARCHAR(20) | 优惠券状态 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### coupon_type 优惠券类型
| 值 | 说明 | 示例 | 实测行数 |
|----|------|------|------|
| fixed_amount | 固定金额减免 | 满100减20，discount_value=20 | 78 |
| percentage | 百分比折扣 | 满100享8折，discount_value=20（减 20%） | 54 |
| free_shipping | 免运费券 | 免运费，discount_value=0 | 18 |

> 全表 150 行。满减券的值是 `fixed_amount`（**不是** `fixed`）、免邮券是
> `free_shipping`（**不是** `shipping`）——旧文档两个都写错了，按它筛是空集。

### status 优惠券状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 可领取、可使用 | 41 |
| expired | 已过期 | 96 |
| depleted | 已领完 | 7 |
| inactive | 已下架，不可领取 | 6 |

> 「已领完」的值是 `depleted`，**不是** `exhausted`。
>
> 状态由档期和发放量决定（快照时刻 = 锚点日末）：`end_date` 已过 → `expired`（占 64%，最多的一档）；
> 未过期的里，下架的是 `inactive`、发满 `total_quota` 的是 `depleted`、其余 `active`。
>
> **券怎么作用到订单上**（`orders.coupon_id` 非空的单，规则逐单成立）：
> - 下单时刻在 `[start_date, end_date]` 内，且订单 `total_amount ≥ min_purchase`；
> - `orders.discount_amount` 就是这张券的优惠：满减券 = `discount_value`；折扣券 =
>   `total_amount × discount_value / 100`，超过 `max_discount` 时封顶；包邮券 = 该单 `shipping_fee`；
> - 同一用户在同一张券上的用券次数 ≤ `per_user_limit`。
> 所以「用券订单的平均优惠」「各券种的让利」可以直接按 `orders` JOIN `coupons` 算。

### applicable_products 的 JSON 结构示例（列类型是 `string`）
> ⚠️ 这是**设计意图**，不是数据：这一列在当前数据里 **150 行全为 NULL**（登记在
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
    CAST(discount_value AS DECIMAL(38,6)) / NULLIF(min_purchase, 0) * 100 AS discount_rate_pct
FROM coupons
WHERE status = 'active'
  AND min_purchase >= 100
ORDER BY min_purchase DESC;
```
