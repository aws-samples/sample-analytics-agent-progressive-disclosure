# order_items - 订单明细表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| item_id | BIGINT | 主键，明细唯一标识 |
| order_id | BIGINT | 关联订单ID |
| product_id | BIGINT | 商品ID |
| product_name | VARCHAR(200) | 商品名称（下单时快照） |
| sku_id | BIGINT | SKU ID |
| sku_name | VARCHAR(200) | SKU 名称（如颜色/尺码） |
| quantity | INT | 购买数量 |
| unit_price | DECIMAL(10,2) | 商品单价 |
| discount_amount | DECIMAL(10,2) | 该商品优惠金额 |
| actual_amount | DECIMAL(10,2) | 实付金额 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段说明

### product_name 和 sku_name

- **product_name**: 下单时的商品名称快照，不随商品后续修改变化
- **sku_name**: SKU 属性描述，如 "黑色/XL"、"128GB/深空灰"

### 金额计算

```
单商品实付 = 单价 × 数量 - 优惠金额
actual_amount = unit_price × quantity - discount_amount
```

## 索引

- PRIMARY KEY: `item_id`
- INDEX: `order_id`, `product_id`, `sku_id`

## 常用查询

### 商品销量 TOP 20
```sql
SELECT
    oi.product_id,
    oi.product_name,
    SUM(oi.quantity) AS total_quantity,
    SUM(oi.actual_amount) AS total_revenue,
    COUNT(DISTINCT o.order_id) AS order_count,
    COUNT(DISTINCT o.user_id) AS buyer_count
FROM order_items oi
JOIN orders o ON oi.order_id = o.order_id
WHERE o.status IN ('paid', 'shipped', 'delivered')
  AND o.placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY oi.product_id, oi.product_name
ORDER BY total_revenue DESC
LIMIT 20;
```

### SKU 销售分析
```sql
SELECT
    oi.product_id,
    oi.product_name,
    oi.sku_id,
    oi.sku_name,
    SUM(oi.quantity) AS total_quantity,
    SUM(oi.actual_amount) AS total_revenue
FROM order_items oi
JOIN orders o ON oi.order_id = o.order_id
WHERE o.status IN ('paid', 'shipped', 'delivered')
  AND o.placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY oi.product_id, oi.product_name, oi.sku_id, oi.sku_name
ORDER BY total_revenue DESC
LIMIT 50;
```

### 客单价分布（按商品件数）
```sql
SELECT
    CASE
        WHEN item_count = 1 THEN '1件'
        WHEN item_count = 2 THEN '2件'
        WHEN item_count <= 5 THEN '3-5件'
        ELSE '5件以上'
    END AS item_count_group,
    COUNT(*) AS order_count,
    ROUND(AVG(actual_amount), 2) AS avg_order_value
FROM orders
WHERE status IN ('paid', 'shipped', 'delivered')
  AND placed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1;
```
