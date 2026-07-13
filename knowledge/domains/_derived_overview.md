# 派生层总览（DWD / DWS / ADS / 历史遗留）

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

基表之上有一个派生层。**先判断问题该落在哪一层，再进域索引选表**：

| 层 | 是什么 | 什么时候用 |
|----|--------|-----------|
| DWD 清洗层 | 基表的过滤/标准化副本 | 想省掉常规过滤（有效订单、剔匿名）时 |
| DWS 汇总层 | 按主题预聚合，粒度各异 | 问题粒度与表粒度一致时（日/周） |
| ADS 应用层 | 部门口径表，**口径互不相同** | 问题点名口径（财务/增长）时 |
| 历史遗留 | 备份/临时表，**已废弃** | 永远别用；读到旧查询时用于解读 |

## 全部派生表

| 表 | 域 | 层 | 状态 |
|----|----|----|------|
| `dwd_orders_valid` | transaction | DWD 清洗层 | active |
| `dwd_events_app` | behavior | DWD 清洗层 | active |
| `dws_user_daily` | user | DWS 汇总层 | active |
| `dws_channel_weekly` | attribution | DWS 汇总层 | active |
| `fin_daily_revenue` | transaction | ADS 应用层 | active |
| `growth_daily_gmv` | transaction | ADS 应用层 | active |
| `orders_backup_20251201` | transaction | 历史遗留 | ⛔ 废弃 |
| `tmp_campaign_roi_analysis` | marketing | 历史遗留 | ⛔ 废弃 |

> 口径冲突的裁决顺序：**call_metric（治理指标）> mart/ADS 卡片写明的口径 > 自己现推**。
