# channel_daily_costs - 渠道日成本表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键，自增 |
| channel_id | INT | 渠道ID，关联 channels.channel_id |
| ad_campaign_id | INT | 广告活动ID，关联 ad_campaigns.ad_campaign_id |
| creative_id | INT | 素材ID，关联 ad_creatives.creative_id |
| date | DATE | 统计日期 |
| impressions | BIGINT | 曝光数 |
| clicks | BIGINT | 点击数 |
| installs | INT | 安装数 |
| cost | DECIMAL(12,4) | 花费金额 |
| currency | VARCHAR(10) | 货币单位 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段说明

### 关键指标计算
| 指标 | 计算公式 | 说明 |
|------|----------|------|
| CTR（点击率） | clicks / impressions * 100 | 广告点击效率 |
| CVR（转化率） | installs / clicks * 100 | 点击到安装转化效率 |
| CPI（单安装成本） | cost / installs | 获取一个安装的成本 |
| CPM（千次曝光成本） | cost / impressions * 1000 | 千次展示成本 |
| CPC（单点击成本） | cost / clicks | 单次点击成本 |

### currency 货币单位
| 值 | 说明 |
|----|------|
| CNY | 人民币 |
| USD | 美元 |

## 索引

- PRIMARY KEY: `id`
- UNIQUE INDEX: `(channel_id, ad_campaign_id, creative_id, date)`
- INDEX: `date`, `channel_id`, `ad_campaign_id`

## 常用查询

### 渠道获客 ROI 分析
```sql
-- 计算各渠道的 CPI（单用户获取成本）和 ROI
WITH channel_costs AS (
    SELECT
        channel_id,
        SUM(cost) AS total_cost,
        SUM(installs) AS total_installs
    FROM channel_daily_costs
    WHERE date >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY channel_id
),
channel_revenue AS (
    SELECT
        ua.channel_id,
        COUNT(DISTINCT ua.user_id) AS attributed_users,
        SUM(o.actual_amount) AS total_revenue
    FROM user_attributions ua
    JOIN orders o ON ua.user_id = o.user_id
    WHERE ua.attributed_at >= CURRENT_DATE - INTERVAL '30 days'
      AND o.status = 'delivered'
    GROUP BY ua.channel_id
)
SELECT
    ch.channel_name,
    ch.channel_type,
    cc.total_cost,
    cc.total_installs,
    ROUND(cc.total_cost / NULLIF(cc.total_installs, 0), 2) AS cpi,
    cr.total_revenue,
    ROUND((cr.total_revenue - cc.total_cost) / NULLIF(cc.total_cost, 0) * 100, 2) AS roi_percent
FROM channels ch
JOIN channel_costs cc ON ch.channel_id = cc.channel_id
LEFT JOIN channel_revenue cr ON ch.channel_id = cr.channel_id
ORDER BY roi_percent DESC NULLS LAST;
```

### 广告素材效果对比
```sql
SELECT
    ac.creative_name,
    ac.creative_type,
    ac.creative_format,
    SUM(cdc.impressions) AS total_impressions,
    SUM(cdc.clicks) AS total_clicks,
    SUM(cdc.installs) AS total_installs,
    ROUND(SUM(cdc.clicks) * 100.0 / NULLIF(SUM(cdc.impressions), 0), 2) AS ctr_percent,
    ROUND(SUM(cdc.installs) * 100.0 / NULLIF(SUM(cdc.clicks), 0), 2) AS cvr_percent,
    ROUND(SUM(cdc.cost) / NULLIF(SUM(cdc.installs), 0), 2) AS cpi
FROM ad_creatives ac
JOIN channel_daily_costs cdc ON ac.creative_id = cdc.creative_id
WHERE cdc.date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY ac.creative_id, ac.creative_name, ac.creative_type, ac.creative_format
HAVING SUM(cdc.impressions) > 1000
ORDER BY cvr_percent DESC;
```

### 每日成本趋势
```sql
SELECT
    date,
    SUM(impressions) AS daily_impressions,
    SUM(clicks) AS daily_clicks,
    SUM(installs) AS daily_installs,
    SUM(cost) AS daily_cost,
    ROUND(SUM(cost) / NULLIF(SUM(installs), 0), 2) AS daily_cpi
FROM channel_daily_costs
WHERE date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY date
ORDER BY date DESC;
```
