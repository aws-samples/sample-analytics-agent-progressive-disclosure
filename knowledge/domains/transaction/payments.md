# payments - 支付记录表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| payment_id | BIGINT | 主键，支付记录唯一标识 |
| payment_no | VARCHAR(50) | 支付流水号 |
| order_id | BIGINT | 关联订单ID |
| user_id | BIGINT | 用户ID |
| amount | DECIMAL(12,2) | 支付金额 |
| payment_method | VARCHAR(20) | 支付方式 |
| payment_channel | VARCHAR(50) | 支付渠道 |
| status | VARCHAR(20) | 支付状态 |
| transaction_id | VARCHAR(100) | 第三方交易流水号 |
| paid_at | TIMESTAMP | 支付成功时间 |
| failure_reason | VARCHAR(200) | 支付失败原因 |
| refund_amount | DECIMAL(12,2) | 退款金额 |
| refunded_at | TIMESTAMP | 退款时间 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### payment_method 支付方式

| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| wechat | 微信支付 | 311,037 | 45.12% |
| alipay | 支付宝 | 275,288 | 39.93% |
| credit_card | 银行卡/信用卡 | 68,720 | 9.97% |
| balance | 账户余额 | 34,345 | 4.98% |

> 银行卡的值是 `credit_card`，**不是** `card`。分布不均匀：两家钱包合计 85%，
> 余额支付只占 5%，所以按支付方式切片时小样本桶要留意置信度。

### payment_channel 支付渠道

| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| app | APP 内支付 | 399,339 | 57.93% |
| h5 | H5 页面支付 | 151,925 | 22.04% |
| mini_program | 小程序支付 | 138,126 | 20.04% |

> 只有这 3 个值，无 NULL。这一列跟 `payment_method` 是**两个正交的维度**
> （渠道 = 在哪个端付，方式 = 用什么付），可以交叉分组。
>
> 这一栏 2026-09-01 改过：在此之前这一列整列为 NULL，卡片写着「按它切片只会得到一个
> NULL 桶，要分渠道就用 `payment_method`」。旧结论现在是错的，这一列可以直接用。

### failure_reason 失败原因

> ⚠️ **这一列仍然整列为 NULL**（689,390 行全空），这跟下面 `status` 只有两个值是同一件事：
> 没有失败记录，自然没有失败原因。不是漏灌，重灌也不会变。

### status 支付状态

| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| success | 支付成功 | 647,395 | 93.91% |
| refunded | 已退款 | 41,995 | 6.09% |

> 一单一支付的设计下**没有** `pending` / `failed` 记录，所以**算不了支付成功率**。
> success 数 = 有效订单数（647,395，可以跟 `orders` 对表），refunded 数 = 退款订单数。

## 索引

- PRIMARY KEY: `payment_id`
- UNIQUE: `payment_no`
- INDEX: `order_id`, `user_id`, `status`, `paid_at`, `payment_method`

## 常用查询

### 支付方式分布和成功率
```sql
SELECT
    payment_method,
    COUNT(*) AS total_attempts,
    COUNT(CASE WHEN status = 'success' THEN 1 END) AS success_count,
    COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed_count,
    ROUND(COUNT(CASE WHEN status = 'success' THEN 1 END) * 100.0 / COUNT(*), 2) AS success_rate,
    SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END) AS total_amount
FROM payments
WHERE paid_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
   OR status = 'failed'
GROUP BY payment_method
ORDER BY total_amount DESC;
```

### 支付渠道分析
```sql
SELECT
    payment_method,
    payment_channel,
    COUNT(*) AS payment_count,
    SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END) AS total_amount,
    ROUND(COUNT(CASE WHEN status = 'success' THEN 1 END) * 100.0 / COUNT(*), 2) AS success_rate
FROM payments
WHERE paid_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY payment_method, payment_channel
ORDER BY total_amount DESC;
```

### 支付失败原因分析
```sql
SELECT
    payment_method,
    failure_reason,
    COUNT(*) AS fail_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(PARTITION BY payment_method), 2) AS pct
FROM payments
WHERE status = 'failed'
  AND paid_at IS NULL
  AND payment_no IS NOT NULL
GROUP BY payment_method, failure_reason
ORDER BY payment_method, fail_count DESC;
```
