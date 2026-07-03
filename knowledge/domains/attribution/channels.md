# channels - 渠道表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| channel_id | INT | 主键，渠道唯一标识，自增 |
| channel_name | VARCHAR(100) | 渠道名称 |
| channel_type | VARCHAR(30) | 渠道类型 |
| platform | VARCHAR(50) | 投放平台 |
| description | TEXT | 渠道描述 |
| is_active | BOOLEAN | 是否启用 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### channel_type 渠道类型
| 值 | 说明 |
|----|------|
| paid | 付费广告渠道（Google Ads, Facebook Ads, TikTok Ads） |
| organic | 自然流量（ASO、SEO） |
| social | 社交媒体（自然社交传播） |
| referral | 用户推荐（邀请好友） |

### platform 投放平台
| 值 | 说明 |
|----|------|
| google | Google Ads（搜索、展示、YouTube） |
| facebook | Facebook/Instagram Ads |
| tiktok | TikTok/抖音广告 |
| apple | Apple Search Ads |
| organic | 自然流量（无广告平台） |

## 索引

- PRIMARY KEY: `channel_id`
- INDEX: `channel_type`, `platform`, `is_active`

## 常用查询

### 获取所有活跃付费渠道
```sql
SELECT
    channel_id,
    channel_name,
    platform
FROM channels
WHERE channel_type = 'paid'
  AND is_active = TRUE
ORDER BY channel_name;
```

### 各渠道类型分布
```sql
SELECT
    channel_type,
    COUNT(*) AS channel_count,
    SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_count
FROM channels
GROUP BY channel_type
ORDER BY channel_count DESC;
```

### 按平台统计渠道数量
```sql
SELECT
    platform,
    COUNT(*) AS channel_count
FROM channels
WHERE is_active = TRUE
GROUP BY platform
ORDER BY channel_count DESC;
```
