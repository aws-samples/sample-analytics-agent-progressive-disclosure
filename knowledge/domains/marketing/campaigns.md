# campaigns - 营销活动表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| campaign_id | INT | 活动ID，主键，自增 |
| campaign_name | VARCHAR(200) | 活动名称 |
| campaign_type | VARCHAR(50) | 活动类型 |
| description | TEXT | 活动描述 |
| start_date | TIMESTAMP | 活动开始时间 |
| end_date | TIMESTAMP | 活动结束时间 |
| target_segment_ids | INT[] | 目标用户分群ID数组 |
| budget | DECIMAL(12,2) | 活动预算 |
| status | VARCHAR(20) | 活动状态 |
| owner | VARCHAR(100) | 活动负责人 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### campaign_type 活动类型
| 值 | 说明 | 实测行数 |
|----|------|------|
| promotion | 促销活动（如满减、折扣） | 16 |
| recall | 流失召回 | 15 |
| festival | 节日大促 | 11 |
| new_user | 新客专享 | 8 |

> 全表 50 行。旧文档写的 `coupon` / `event` / `content` 都不存在——节日活动的值是
> `festival`。注意这跟 `ad_campaigns.campaign_type`（广告投放表：awareness /
> retargeting / acquisition）是**两套不同的枚举**，别混用。

### status 活动状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| completed | 已结束 | 28 |
| active | 进行中 | 11 |
| scheduled | 已排期，等待开始 | 4 |
| cancelled | 已取消 | 3 |
| draft | 草稿，未发布 | 2 |
| paused | 已暂停 | 2 |

> 旧文档漏了 `cancelled`。「进行中」只有 11 行，而快照日期是固定的
> （见 `meta_snapshot.as_of_date`），所以别用 `CURRENT_DATE` 判活动是否在跑。

## 索引

- PRIMARY KEY: `campaign_id`
- INDEX: `status`, `start_date`, `end_date`, `campaign_type`

## 常用查询

### 活动状态分布统计
```sql
SELECT
    status,
    campaign_type,
    COUNT(*) AS campaign_count,
    SUM(budget) AS total_budget
FROM campaigns
WHERE start_date >= date '2024-01-01'
GROUP BY status, campaign_type
ORDER BY status, campaign_count DESC;
```

### 本月活动预算消耗
```sql
SELECT
    campaign_id,
    campaign_name,
    campaign_type,
    budget,
    start_date,
    end_date,
    status
FROM campaigns
WHERE status IN ('active', 'completed')
  -- 「本月」= 锚点所在的月（2026-01），不是真实当月
  AND start_date >= DATE_TRUNC('month', (SELECT max(as_of_date) FROM meta_snapshot))
ORDER BY budget DESC;
```

### 活动负责人工作量统计
```sql
SELECT
    owner,
    COUNT(*) AS total_campaigns,
    COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_count,
    SUM(budget) AS total_budget_managed
FROM campaigns
WHERE start_date >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '90' day
GROUP BY owner
ORDER BY total_campaigns DESC;
```
