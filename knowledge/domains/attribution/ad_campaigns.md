# ad_campaigns - 广告活动表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| ad_campaign_id | INT | 主键，广告活动唯一标识，自增 |
| channel_id | INT | 所属渠道ID，关联 channels.channel_id |
| campaign_name | VARCHAR(200) | 活动名称 |
| campaign_type | VARCHAR(50) | 活动类型 |
| objective | VARCHAR(100) | 投放目标 |
| budget_total | DECIMAL(12,2) | 总预算（元） |
| budget_daily | DECIMAL(10,2) | 日预算（元） |
| start_date | TIMESTAMP | 开始日期 |
| end_date | TIMESTAMP | 结束日期 |
| target_audience | JSONB | 目标受众配置 |
| status | VARCHAR(20) | 活动状态 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### campaign_type 活动类型
| 值 | 说明 |
|----|------|
| awareness | 品牌认知（提升曝光度） |
| consideration | 兴趣考虑（增加互动） |
| conversion | 转化获客（促进安装/注册） |
| retargeting | 再营销（召回流失用户） |

### objective 投放目标
| 值 | 说明 |
|----|------|
| installs | APP 安装量 |
| registrations | 用户注册量 |
| purchases | 购买转化 |

### status 活动状态
| 值 | 说明 |
|----|------|
| draft | 草稿（未发布） |
| active | 投放中 |
| paused | 已暂停 |
| completed | 已结束 |

### target_audience 目标受众配置
典型结构：
```json
{
  "age_range": {"min": 18, "max": 45},
  "gender": ["male", "female"],
  "interests": ["gaming", "electronics"],
  "locations": ["北京", "上海", "深圳"],
  "devices": ["ios", "android"],
  "lookalike_source": "high_value_users"
}
```

## 索引

- PRIMARY KEY: `ad_campaign_id`
- INDEX: `channel_id`, `status`, `start_date`, `end_date`

## 常用查询

### 当前投放中的活动列表
```sql
SELECT
    ac.ad_campaign_id,
    ac.campaign_name,
    ch.channel_name,
    ac.campaign_type,
    ac.objective,
    ac.budget_daily,
    ac.start_date
FROM ad_campaigns ac
JOIN channels ch ON ac.channel_id = ch.channel_id
WHERE ac.status = 'active'
ORDER BY ac.start_date DESC;
```

### 各渠道预算分配
```sql
SELECT
    ch.channel_name,
    COUNT(ac.ad_campaign_id) AS campaign_count,
    SUM(ac.budget_total) AS total_budget,
    SUM(ac.budget_daily) AS daily_budget
FROM ad_campaigns ac
JOIN channels ch ON ac.channel_id = ch.channel_id
WHERE ac.status IN ('active', 'paused')
GROUP BY ch.channel_id, ch.channel_name
ORDER BY total_budget DESC;
```

### 活动类型分布
```sql
SELECT
    campaign_type,
    objective,
    COUNT(*) AS campaign_count,
    SUM(budget_total) AS total_budget
FROM ad_campaigns
WHERE status = 'active'
GROUP BY campaign_type, objective
ORDER BY campaign_count DESC;
```
