# dwd_orders_valid - 订单清洗层

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：DWD 清洗层
**粒度**：一行 = 一笔有效订单（status IN paid/shipped/delivered）

订单清洗层：只含有效订单，剔除 pending/cancelled/refunded

## 何时用这张表

- ✅ 计算 GMV、收入、客单价等只看有效成交的指标（省去每次写 status 过滤）
- ❌ 分析取消率/退款率（被剔除的行恰恰是分子）——用原始 orders

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | BIGINT | 主键，同 orders.order_id |
| order_no | VARCHAR(50) | 订单编号 |
| user_id | BIGINT | 用户ID |
| status | VARCHAR(30) | 只会是 paid/shipped/delivered |
| total_amount | DECIMAL(12 | 商品总金额（应付） |
| discount_amount | DECIMAL(12 | 优惠减免金额 |
| shipping_fee | DECIMAL(10 | 运费 |
| actual_amount | DECIMAL(12 | 实付金额（GMV 官方口径用这列） |
| item_count | INT | 商品件数 |
| coupon_id | INT | 使用的优惠券ID，可空 |
| placed_at | TIMESTAMP | 下单时间 |
| paid_at | TIMESTAMP | 支付时间 |

## 注意（口径与坑）

- 行数少于 orders（≈75%），count(*) 结果与"总订单数"不同——总订单数用 orders

## 构建口径（本表如何从基表算出）

```sql
SELECT order_id, order_no, user_id, status,
       total_amount, discount_amount, shipping_fee, actual_amount,
       item_count, coupon_id, placed_at, paid_at
FROM orders
WHERE status IN ('paid','shipped','delivered')
```
