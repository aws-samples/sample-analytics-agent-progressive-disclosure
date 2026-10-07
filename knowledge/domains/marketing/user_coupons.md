# user_coupons - 用户优惠券表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键，自增 |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| coupon_id | INT | 关联优惠券模板ID |
| coupon_code | VARCHAR(50) | 用户持有的券码 |
| received_at | TIMESTAMP | 领取时间 |
| expire_at | TIMESTAMP | 过期时间 |
| used_at | TIMESTAMP | 使用时间 |
| order_id | BIGINT | 使用时关联的订单ID |
| status | VARCHAR(20) | 优惠券状态 |
| source | VARCHAR(50) | 领取来源渠道 |

## 字段枚举值

### status 优惠券状态
| 值 | 说明 |
|----|------|
| unused | 未使用 |
| used | 已使用 |
| expired | 已过期 |

### source 来源渠道
| 值 | 说明 | 实测行数 |
|----|------|------|
| claim | 用户主动领取（领券中心） | 13,649 |
| gift | 赠送/客服发放 | 3,466 |
| reward | 任务或活动奖励 | 3,327 |
| system | 系统自动发放 | 2,293 |

> 全表 22,735 行。旧文档写的 `campaign` / `manual` / `referral` / `purchase`
> **一个都不存在**——主动领取是 `claim`（占 60%），赠送是 `gift`。

### order_id 与 orders 的关联

> ⚠️ **这一列接不上 `orders.order_id`**：11,297 张已使用的券里有 `order_id`，
> 拿去 JOIN `orders` **匹配到 0 行**。两张表的 ID 是各自独立生成的，DDL 里也没有声明
> 这条外键，所以 JOIN 不报错、只是静默返回空集。
>
> 后果很典型：问「用券订单的客单价比不用券高多少」，SQL 跑通、返回空、
> 顺着往下写就会得出「没有用券订单」这种反向结论。
> **这份数据上券和订单无法关联**——只能各自分开统计（券的核销率看
> `user_coupons.status`，订单的优惠金额看 `orders.discount_amount`）。

## 索引

- PRIMARY KEY: `id`
- INDEX: `user_id`, `coupon_id`, `status`, `received_at`, `expire_at`
- INDEX: `order_id` (用于订单关联查询)

## 常用查询

### 优惠券使用率分析
```sql
SELECT
    c.coupon_id,
    c.coupon_name,
    c.coupon_type,
    c.discount_value,
    COUNT(uc.id) AS received_count,
    COUNT(CASE WHEN uc.status = 'used' THEN 1 END) AS used_count,
    ROUND(COUNT(CASE WHEN uc.status = 'used' THEN 1 END) * 100.0 /
          NULLIF(COUNT(uc.id), 0), 2) AS usage_rate
FROM coupons c
LEFT JOIN user_coupons uc ON c.coupon_id = uc.coupon_id
WHERE c.start_date >= date '2024-01-01'
GROUP BY c.coupon_id, c.coupon_name, c.coupon_type, c.discount_value
ORDER BY received_count DESC;
```

### 各渠道领券分布
```sql
SELECT
    source,
    COUNT(*) AS coupon_count,
    COUNT(DISTINCT user_id) AS user_count,
    COUNT(CASE WHEN status = 'used' THEN 1 END) AS used_count,
    ROUND(COUNT(CASE WHEN status = 'used' THEN 1 END) * 100.0 /
          NULLIF(COUNT(*), 0), 2) AS usage_rate
FROM user_coupons
WHERE received_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY source
ORDER BY coupon_count DESC;
```

### 用户优惠券持有情况
```sql
SELECT
    user_id,
    COUNT(*) AS total_coupons,
    COUNT(CASE WHEN status = 'unused' THEN 1 END) AS unused_count,
    COUNT(CASE WHEN status = 'used' THEN 1 END) AS used_count,
    COUNT(CASE WHEN status = 'expired' THEN 1 END) AS expired_count
FROM user_coupons
GROUP BY user_id
HAVING COUNT(*) >= 5
ORDER BY total_coupons DESC
LIMIT 100;
```
