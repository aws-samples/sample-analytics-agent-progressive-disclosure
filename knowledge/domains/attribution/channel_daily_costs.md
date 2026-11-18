# channel_daily_costs - 渠道日成本表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键，自增 |
| channel_id | INT | 渠道ID，关联 channels.channel_id |
| ad_campaign_id | INT | 广告活动ID，关联 ad_campaigns.ad_campaign_id |
| creative_id | INT | 素材ID，关联 ad_creatives.creative_id。**当前数据整列为 NULL**：成本按活动汇总、不拆到素材 |
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
| 值 | 说明 | 实测行数 |
|----|------|------|
| CNY | 人民币 | 2,799 |

> 2,799 行**全是 CNY**，没有 `USD`（旧文档写过）。所以 `cost` 列可以直接跨渠道相加，
> 不需要先换算汇率——但也别因此把这个当成不变量，多币种一旦进来这一列才有意义。

## 索引

- PRIMARY KEY: `id`
- UNIQUE INDEX: `(channel_id, ad_campaign_id, creative_id, date)`
- INDEX: `date`, `channel_id`, `ad_campaign_id`

## 常用查询

### 渠道获客 ROI 分析（近 30 天）
> 官方口径优先用 `call_metric(metric="roi", group_by=["channel"], time_window="last_30d")`。
> 下面是同口径的手写版：ROI = 归因 GMV ÷ 花费（不是 (收入 − 花费) ÷ 花费）。三条要点：
> ① 花费**两端都截**到锚点日——成本轴延伸到 2026-09-01，只写下限会把锚点之后的花费算进来；
> ② 归因只取 `attribution_type = 'last_touch'`（被归因用户每人两行，不过滤会把订单算两遍）；
> ③ GMV 用有效订单（`paid / shipped / delivered`）的 `actual_amount`，按下单时间落在同一窗口。
```sql
WITH channel_costs AS (
    SELECT
        channel_id,
        SUM(cost) AS total_cost,
        SUM(installs) AS total_installs
    FROM channel_daily_costs
    WHERE date > (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
      AND date <= (SELECT max(as_of_date) FROM meta_snapshot)
    GROUP BY channel_id
),
channel_revenue AS (
    SELECT
        ua.channel_id,
        COUNT(DISTINCT o.user_id) AS paying_users,
        SUM(o.actual_amount) AS gmv_attributed
    FROM user_attributions ua
    JOIN orders o ON ua.user_id = o.user_id
    WHERE ua.attribution_type = 'last_touch'
      AND o.status IN ('paid', 'shipped', 'delivered')
      AND o.placed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day
      AND o.placed_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
    GROUP BY ua.channel_id
)
SELECT
    ch.channel_name,
    ch.channel_type,
    cc.total_cost,
    cc.total_installs,
    cc.total_cost / NULLIF(cc.total_installs, 0) AS cpi,
    cr.gmv_attributed,
    cr.gmv_attributed / NULLIF(cc.total_cost, 0) AS roi
FROM channels ch
JOIN channel_costs cc ON ch.channel_id = cc.channel_id
LEFT JOIN channel_revenue cr ON ch.channel_id = cr.channel_id
ORDER BY roi DESC NULLS LAST;
```

### 广告素材效果对比（当前数据做不了）
> ⚠️ **成本表不拆到素材**：`channel_daily_costs.creative_id` 整列为 NULL（成本按活动汇总），
> 所以「哪个素材 CTR / CPI 最好」在这份数据上**算不出来**——按 `creative_id` JOIN 只会得到空集。
> 能做的最细粒度是**广告活动**：
```sql
SELECT
    ac.campaign_name,
    ch.channel_name,
    SUM(cdc.impressions) AS total_impressions,
    SUM(cdc.clicks) AS total_clicks,
    SUM(cdc.installs) AS total_installs,
    SUM(cdc.clicks) * 100.0 / NULLIF(SUM(cdc.impressions), 0) AS ctr_percent,
    SUM(cdc.installs) * 100.0 / NULLIF(SUM(cdc.clicks), 0) AS cvr_percent,
    CAST(SUM(cdc.cost) AS DECIMAL(38,6)) / NULLIF(SUM(cdc.installs), 0) AS cpi
FROM channel_daily_costs cdc
JOIN ad_campaigns ac ON ac.ad_campaign_id = cdc.ad_campaign_id
JOIN channels ch ON ch.channel_id = cdc.channel_id
WHERE cdc.date > (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
  AND cdc.date <= (SELECT max(as_of_date) FROM meta_snapshot)
GROUP BY ac.campaign_name, ch.channel_name
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
    CAST(SUM(cost) AS DECIMAL(38,6)) / NULLIF(SUM(installs), 0) AS daily_cpi
FROM channel_daily_costs
WHERE date >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY date
ORDER BY date DESC;
```
