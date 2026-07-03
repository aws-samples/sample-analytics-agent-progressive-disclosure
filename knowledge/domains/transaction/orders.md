# orders - 订单主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | BIGINT | 主键，订单唯一标识 |
| order_no | VARCHAR(50) | 订单编号（业务唯一标识） |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| status | VARCHAR(20) | 订单状态 |
| total_amount | DECIMAL(12,2) | 商品总金额 |
| discount_amount | DECIMAL(10,2) | 优惠减免金额 |
| shipping_fee | DECIMAL(8,2) | 运费 |
| actual_amount | DECIMAL(12,2) | 实付金额 |
| item_count | INT | 商品件数 |
| coupon_id | INT | 使用的优惠券ID |
| shipping_address | JSONB | 收货地址信息 |
| remark | TEXT | 订单备注 |
| placed_at | TIMESTAMP | 下单时间 |
| paid_at | TIMESTAMP | 支付时间 |
| shipped_at | TIMESTAMP | 发货时间 |
| delivered_at | TIMESTAMP | 签收时间 |
| cancelled_at | TIMESTAMP | 取消时间 |
| cancel_reason | VARCHAR(200) | 取消原因 |
| refunded_at | TIMESTAMP | 退款时间 |
| refund_reason | VARCHAR(200) | 退款原因 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### status 订单状态

状态流转图：
```
pending → paid → shipped → delivered
    ↓        ↓       ↓         ↓
cancelled  refunded  refunded  refunded
```

| 值 | 说明 | 描述 |
|----|------|------|
| pending | 待支付 | 下单未付款 |
| paid | 已支付 | 等待发货 |
| shipped | 已发货 | 运输中 |
| delivered | 已签收 | 交易完成 |
| cancelled | 已取消 | 未支付时取消 |
| refunded | 已退款 | 支付后退款 |

### shipping_address 地址结构

```json
{
  "receiver_name": "张三",
  "phone": "13800138000",
  "province": "广东省",
  "city": "深圳市",
  "district": "南山区",
  "address": "科技园路1号",
  "postal_code": "518000"
}
```

### 金额计算

```
actual_amount = total_amount - discount_amount + shipping_fee
```

## 索引

- PRIMARY KEY: `order_id`
- UNIQUE: `order_no`
- INDEX: `user_id`, `status`, `placed_at`, `paid_at`

## 常用查询

### 每日 GMV 和订单量
```sql
SELECT
    DATE(placed_at) AS order_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN status NOT IN ('cancelled') THEN 1 END) AS valid_orders,
    SUM(total_amount) AS gmv,
    SUM(CASE WHEN status IN ('paid', 'shipped', 'delivered')
        THEN actual_amount ELSE 0 END) AS revenue,
    SUM(discount_amount) AS total_discount,
    ROUND(AVG(CASE WHEN status NOT IN ('cancelled')
        THEN actual_amount END), 2) AS avg_order_value
FROM orders
WHERE placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(placed_at)
ORDER BY order_date DESC;
```

### 订单状态分布
```sql
SELECT
    status,
    COUNT(*) AS order_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct,
    SUM(actual_amount) AS total_amount
FROM orders
WHERE placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY status
ORDER BY order_count DESC;
```

### 订单取消/退款分析
```sql
SELECT
    DATE(placed_at) AS order_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN status = 'cancelled' THEN 1 END) AS cancelled,
    COUNT(CASE WHEN status = 'refunded' THEN 1 END) AS refunded,
    ROUND(COUNT(CASE WHEN status = 'cancelled' THEN 1 END) * 100.0 / COUNT(*), 2) AS cancel_rate,
    ROUND(COUNT(CASE WHEN status = 'refunded' THEN 1 END) * 100.0 / COUNT(*), 2) AS refund_rate
FROM orders
WHERE placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(placed_at)
ORDER BY order_date DESC;
```
