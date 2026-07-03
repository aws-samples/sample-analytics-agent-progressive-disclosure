# user_attributions - 用户归因表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| attribution_id | BIGINT | 主键，归因记录唯一标识，自增 |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| channel_id | INT | 归因渠道ID，关联 channels.channel_id |
| ad_campaign_id | INT | 归因广告活动ID，关联 ad_campaigns.ad_campaign_id |
| creative_id | INT | 归因素材ID，关联 ad_creatives.creative_id |
| attribution_type | VARCHAR(20) | 归因模型类型 |
| click_time | TIMESTAMP | 广告点击时间 |
| install_time | TIMESTAMP | APP 安装时间 |
| attributed_at | TIMESTAMP | 归因确定时间 |
| days_to_install | INT | 点击到安装天数 |
| tracking_params | JSONB | 追踪参数 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### attribution_type 归因模型
| 值 | 说明 |
|----|------|
| first_touch | 首次触点归因：将转化归因于用户首次接触的渠道 |
| last_touch | 末次触点归因：将转化归因于用户最后接触的渠道（最常用） |
| linear | 线性归因：将转化平均分配给所有接触渠道 |

### tracking_params 追踪参数
典型结构：
```json
{
  "utm_source": "google",
  "utm_medium": "cpc",
  "utm_campaign": "summer_sale",
  "utm_content": "banner_a",
  "click_id": "gclid_xxx",
  "sub_id": "ad_group_1",
  "device_id": "idfa_xxx"
}
```

## 索引

- PRIMARY KEY: `attribution_id`
- INDEX: `user_id`, `channel_id`, `ad_campaign_id`, `attributed_at`
- INDEX: `click_time`, `install_time`

## 常用查询

### 各渠道归因用户数
```sql
SELECT
    ch.channel_name,
    ch.channel_type,
    COUNT(DISTINCT ua.user_id) AS attributed_users
FROM user_attributions ua
JOIN channels ch ON ua.channel_id = ch.channel_id
WHERE ua.attributed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY ch.channel_id, ch.channel_name, ch.channel_type
ORDER BY attributed_users DESC;
```

### 用户获取漏斗分析（点击到安装时间分布）
```sql
SELECT
    ch.channel_name,
    COUNT(*) AS total_attributions,
    AVG(ua.days_to_install) AS avg_days_to_install,
    COUNT(CASE WHEN ua.days_to_install = 0 THEN 1 END) AS same_day_installs,
    COUNT(CASE WHEN ua.days_to_install BETWEEN 1 AND 3 THEN 1 END) AS installs_1_3_days,
    COUNT(CASE WHEN ua.days_to_install > 3 THEN 1 END) AS installs_over_3_days,
    ROUND(COUNT(CASE WHEN ua.days_to_install = 0 THEN 1 END) * 100.0 / COUNT(*), 2) AS same_day_rate
FROM user_attributions ua
JOIN channels ch ON ua.channel_id = ch.channel_id
WHERE ua.attributed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY ch.channel_id, ch.channel_name
ORDER BY total_attributions DESC;
```

### 归因模型分布
```sql
SELECT
    attribution_type,
    COUNT(*) AS attribution_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM user_attributions
WHERE attributed_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY attribution_type
ORDER BY attribution_count DESC;
```
