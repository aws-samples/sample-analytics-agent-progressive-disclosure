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
| tracking_params | `string` | 追踪参数（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。**整列为 NULL**，见下 |

## 字段枚举值

### attribution_type 归因模型
| 值 | 说明 | 实测行数 |
|----|------|------|
| first_touch | 首次触点归因：将转化归因于用户首次接触的渠道 | 175 |
| last_touch | 末次触点归因：将转化归因于用户最后接触的渠道（最常用） | 175 |

> ⚠️ **只有这两种，没有 `linear`**（旧文档写过，按它筛是空集）。
>
> 全表 350 行 / 350 个用户——**每个用户恰好一行**，也就是说这张表里
> **first_touch 和 last_touch 分给的是不同的用户**，不是同一个用户的两种归因视角。
> 所以：
> - 想「按末次归因看渠道」时若加 `WHERE attribution_type = 'last_touch'`，样本会砍到 175 人；
> - 直接不加条件做 GROUP BY，等于把两种归因模型的结果混在一张表里，口径不纯。
>
> 覆盖率也要留意：有过成交的买家 486 人，其中只有 170 人在这张表里有归因记录。
> 分渠道 GMV 里那个占 64% 的 `(未归因)` 桶就是这么来的——**别把它当成一个真实渠道**。

### tracking_params 追踪参数

> ⚠️ **这一列在种子数据里 350 行全为 NULL**。旧文档给的 `utm_source` / `click_id` /
> `device_id` 那套结构在数据里不存在，取值只会得到 NULL。要看 UTM 参数请查
> `sessions` 表的 `utm_source` / `utm_medium` / `utm_campaign` 三列（那边有实际值）。

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
WHERE ua.attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
WHERE ua.attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
WHERE attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY attribution_type
ORDER BY attribution_count DESC;
```
