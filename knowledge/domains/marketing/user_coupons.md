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
| claim | 用户主动领取（领券中心） | 5,830,129 |
| gift | 赠送/客服发放 | 1,480,559 |
| reward | 任务或活动奖励 | 1,420,241 |
| system | 系统自动发放 | 978,507 |

> 全表 9,709,436 行。旧文档写的 `campaign` / `manual` / `referral` / `purchase`
> **一个都不存在**——主动领取是 `claim`（占 60%），赠送是 `gift`。

### order_id 与 orders 的关联

> ✅ **`status = 'used'` 的行与用券订单一一对应**：324,482 张已使用的券，`order_id` 逐个命中
> `orders`，且 `(user_id, coupon_id)` 与那一单相同、`used_at` = 该单 `placed_at`。所以
> 「用券订单的客单价比不用券高多少」可以走 `user_coupons JOIN orders`，也可以直接按
> `orders.coupon_id IS NOT NULL` 分组，两者一致。用券单里含未支付 / 已取消的单
> （有效单 246,005 张），算 GMV 时照常加有效订单状态过滤。
>
> 非核销行（`unused` / `expired`）的 `order_id` 为 NULL。领券时刻落在
> `[max(注册, 券 start_date), min(券 end_date, 锚点)]` 内，`expire_at = received_at + coupons.valid_days 天`；
> `expired` ⟺ `expire_at` 早于快照时刻（锚点日末），否则 `unused`。
>
> （2026-11 重灌前这一段写的是「接不上 orders」，那是 v1 数据的形态，已作废。）

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
    COUNT(CASE WHEN uc.status = 'used' THEN 1 END) * 100.0 /
          NULLIF(COUNT(uc.id), 0) AS usage_rate
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
    COUNT(CASE WHEN status = 'used' THEN 1 END) * 100.0 /
          NULLIF(COUNT(*), 0) AS usage_rate
FROM user_coupons
WHERE received_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day AND received_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
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
