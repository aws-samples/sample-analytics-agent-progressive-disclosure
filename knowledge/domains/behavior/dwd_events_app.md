# dwd_events_app - 事件清洗层

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：DWD 清洗层
**粒度**：一行 = 一条 App 端埋点事件（剔除 user_id 为空的匿名事件）

事件清洗层：只含可归到具体用户的事件，匿名/爬虫流量已剔除

## 何时用这张表

- ✅ 计算 DAU、人均事件数等以"用户"为分母的指标
- ❌ 统计总流量/PV（匿名事件也算流量）——用原始 events

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| event_id | BIGINT | 主键，同 events.event_id |
| user_id | BIGINT | 用户ID（本表内非空） |
| session_id | BIGINT | 会话ID |
| event_name | VARCHAR(100) | 事件名，枚举同 event_definitions |
| event_time | TIMESTAMP | 事件时间 |
| page_name | VARCHAR(100) | 页面名 |

## 注意（口径与坑）

- 与 events 的行数差 = 匿名事件量；两表算 DAU 结果相同（DAU 本就 count distinct user_id）

## 构建口径（本表如何从基表算出）

```sql
SELECT event_id, user_id, session_id, event_name, event_time, page_name
FROM events
WHERE user_id IS NOT NULL
```
