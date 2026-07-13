# tmp_campaign_roi_analysis - 某分析师做活动复盘时留下的一次性 ROI 中间表，未再维护（已废弃）

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

> ⛔ **已废弃，禁止用于新分析**。请改用 `mart_channel_daily`。
> 下文仅供理解这张表为什么存在、以及读到旧查询时如何解读。

**层级**：历史遗留
**粒度**：一行 = 一个渠道（某分析师 2026-01 的一次性快照）

某分析师做活动复盘时留下的一次性 ROI 中间表，未再维护

## 何时用这张表

- ✅ 不要用。仅保留作历史参照
- ❌ 一切分析。ROI 问题走 mart_channel_daily（官方口径）或 call_metric

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| channel_id | INT | 渠道ID |
| channel_name | VARCHAR(100) | 渠道名 |
| total_cost | NUMERIC(14 | 截至 2026-01-10 的累计花费 |
| attributed_gmv | NUMERIC(14 | 【全为空】当时没算完 |
| roi | NUMERIC(8 | 【全为空】当时没算完 |

## 注意（口径与坑）

- attributed_gmv / roi 两列全为 NULL —— 这张表根本没算完，任何用它的结论都是错的

## 构建口径（本表如何从基表算出）

```sql
SELECT c.channel_id, c.channel_name,
       sum(d.cost)::numeric(14,2) AS total_cost,
       NULL::numeric(14,2)        AS attributed_gmv,
       NULL::numeric(8,4)         AS roi
FROM channel_daily_costs d JOIN channels c USING (channel_id)
WHERE d.date < DATE '2026-01-10'
GROUP BY 1, 2
```
