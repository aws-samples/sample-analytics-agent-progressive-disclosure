# transaction 域 · 派生层附录

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

本域除基表外还有以下派生表。**同名近义表怎么选，看「选哪张」列**。

| 表名 | 层级 | 一句话 | 选哪张 |
|------|------|--------|--------|
| `dwd_orders_valid` | DWD 清洗层 | 订单清洗层：只含有效订单，剔除 pending/cancelled/refunded | 计算 GMV、收入、客单价等只看有效成交的指标（省去每次写 status 过滤） |
| `fin_daily_revenue` | ADS 应用层 | 财务日收入：确认收入口径 —— 按 paid_at 记账、扣退款、不含运费 | 财务/对账/确认收入问题（"财务口径""确认收入""净收入"） |
| `growth_daily_gmv` | ADS 应用层 | 增长日 GMV：下单口径 —— 按 placed_at、含运费、不扣退款，另含下单用户数 | 增长/大盘/转化问题（"GMV""成交额""下单用户"）；与投放数据按下单日对齐 |
| `orders_backup_20251201`（废弃） | 历史遗留 | 某次订单表结构变更前的手工备份快照，数据止于 2025-12-01 | ⛔ 别用，改用 `orders` |

详情读各表卡片：`dwd_orders_valid.md`、`fin_daily_revenue.md`、`growth_daily_gmv.md`、`orders_backup_20251201.md`
