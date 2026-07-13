# dws_channel_weekly - 渠道周汇总

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：DWS 汇总层
**粒度**：一行 = 一个渠道 × 一个 ISO 周

渠道周汇总：每渠道每周的花费、新客数、周 CAC

## 何时用这张表

- ✅ 渠道效果的周级趋势、周报；周粒度环比
- ❌ 日粒度分析（用 mart_channel_daily）；单日异常定位

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| channel_id | INT | 渠道ID |
| channel_name | VARCHAR(100) | 渠道名 |
| week_start | DATE | ISO 周一 |
| cost | NUMERIC(14 | 当周投放花费 |
| installs | BIGINT | 当周安装数（渠道上报） |
| new_users | BIGINT | 当周 last_touch 归因新客 |
| weekly_cac | NUMERIC(12 | 周 CAC = cost / new_users，无新客时为空 |

## 注意（口径与坑）

- 归因口径固定 last_touch，与 mart_channel_daily 一致
- 首尾是残周；周环比要掐头去尾，或用完整 ISO 周

## 构建口径（本表如何从基表算出）

```sql
SELECT c.channel_id, c.channel_name,
       date_trunc('week', d.date)::date AS week_start,
       sum(d.cost)::numeric(14,2)  AS cost,
       sum(d.installs)             AS installs,
       count(DISTINCT ua.user_id)  AS new_users,
       (sum(d.cost) / NULLIF(count(DISTINCT ua.user_id), 0))::numeric(12,2) AS weekly_cac
FROM channel_daily_costs d
JOIN channels c USING (channel_id)
LEFT JOIN user_attributions ua
  ON ua.channel_id = d.channel_id
 AND ua.attribution_type = 'last_touch'
 AND ua.attributed_at::date >= date_trunc('week', d.date)::date
 AND ua.attributed_at::date <  date_trunc('week', d.date)::date + 7
GROUP BY 1, 2, 3
```
