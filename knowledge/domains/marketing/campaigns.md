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
| 值 | 说明 |
|----|------|
| promotion | 促销活动（如满减、折扣） |
| coupon | 优惠券发放活动 |
| event | 事件活动（如新品发布、节日活动） |
| content | 内容营销（如文章推送、视频营销） |

### status 活动状态
| 值 | 说明 |
|----|------|
| draft | 草稿，未发布 |
| scheduled | 已排期，等待开始 |
| active | 进行中 |
| paused | 已暂停 |
| completed | 已结束 |

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
WHERE start_date >= '2024-01-01'
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
  AND start_date >= DATE_TRUNC('month', CURRENT_DATE)
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
WHERE start_date >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY owner
ORDER BY total_campaigns DESC;
```
