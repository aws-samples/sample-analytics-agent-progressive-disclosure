# orders_backup_20251201 - 某次订单表结构变更前的手工备份快照，数据止于 2025-12-01（已废弃）

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

> ⛔ **已废弃，禁止用于新分析**。请改用 `orders`。
> 下文仅供理解这张表为什么存在、以及读到旧查询时如何解读。

**层级**：历史遗留
**粒度**：一行 = 一笔订单（截至 2025-12-01 的快照）

某次订单表结构变更前的手工备份快照，数据止于 2025-12-01

## 何时用这张表

- ✅ 仅用于追溯 2025-12-01 结构变更前的历史数据核对
- ❌ 一切常规分析 —— 数据不全（缺 12 月之后），字段可能过时

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | BIGINT | 订单ID |
| order_no | VARCHAR(50) | 订单编号 |
| user_id | BIGINT | 用户ID |
| status | VARCHAR(30) | 快照时点的订单状态（可能已过时） |
| total_amount | DECIMAL(12 | 商品总金额 |
| actual_amount | DECIMAL(12 | 实付金额 |
| placed_at | TIMESTAMP | 下单时间（全部 < 2025-12-01） |

## 注意（口径与坑）

- 数据止于 2025-12-01，用它算"最近 30 天"会得到空结果或旧数据
- 快照时点后发生的状态流转（发货/退款）不在本表

## 构建口径（本表如何从基表算出）

```sql
SELECT order_id, order_no, user_id, status,
       total_amount, actual_amount, placed_at
FROM orders
WHERE placed_at < DATE '2025-12-01'
```
