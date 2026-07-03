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

| 值 | 说明 |
|----|------|
| alipay | 支付宝 |
| wechat | 微信支付 |
| card | 银行卡/信用卡 |
| balance | 账户余额 |

### payment_channel 支付渠道

| 值 | 说明 |
|----|------|
| alipay_app | 支付宝APP |
| alipay_wap | 支付宝H5 |
| alipay_mini | 支付宝小程序 |
| wechat_app | 微信APP |
| wechat_jsapi | 微信公众号 |
| wechat_mini | 微信小程序 |
| wechat_h5 | 微信H5 |
| unionpay | 银联 |
| visa | VISA卡 |
| mastercard | MasterCard |

### status 支付状态

| 值 | 说明 |
|----|------|
| pending | 待支付 |
| success | 支付成功 |
| failed | 支付失败 |
| refunded | 已退款 |

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
WHERE paid_at >= CURRENT_DATE - INTERVAL '30 days'
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
WHERE paid_at >= CURRENT_DATE - INTERVAL '30 days'
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
