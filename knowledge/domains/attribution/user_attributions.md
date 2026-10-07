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
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| first_touch | 首次触点归因：将转化归因于用户首次接触的渠道 | 74,759 | 50.01% |
| last_touch | 末次触点归因：将转化归因于用户最后接触的渠道（最常用） | 74,715 | 49.99% |

> ⚠️ **只有这两种，没有 `linear`**（旧文档写过，按它筛是空集）。
>
> 全表 149,474 行 / 149,474 个用户——**每个用户恰好一行**，也就是说这张表里
> **first_touch 和 last_touch 分给的是不同的用户**，不是同一个用户的两种归因视角
> （实测同时有两种记录的用户数 = 0）。所以：
> - 想「按末次归因看渠道」时若加 `WHERE attribution_type = 'last_touch'`，样本会砍掉一半，
>   只剩 74,715 人；
> - 直接不加条件做 GROUP BY，等于把两种归因模型的结果混在一张表里，口径不纯。
>
> 覆盖率也要留意：有过成交的买家 126,010 人，其中 87,955 人在这张表里有归因记录
> （44,217 人是 last_touch、43,738 人只有 first_touch），另 38,055 人一条归因都没有。
> 分渠道 GMV 里那个占 **64.7%** 的 `(未归因)` 桶就是这么来的——**别把它当成一个真实渠道**，
> 也别说成"六成买家来路不明"：那里面 43,738 人是有渠道信息的，只是不符合 last_touch 口径。

### tracking_params 追踪参数

只有一个键 `utm_source`，取值 11 种（就是 `channels.platform` 的 11 个值，见下）：

```json
{"utm_source": "xiaohongshu"}
```

> `utm_source` **逐行等于本行 `channel_id` 对应的 `channels.platform`**（149,474 行
> 全部相等，两侧基数都是 11）。所以它是渠道信息的**冗余副本**，不是独立的追踪参数：
> 按它分组和按 `channel_id` JOIN `channels` 分组的结果只差渠道名——同一个 platform
> 下有多个 channel（14 个渠道映到 11 个 platform），所以按它分组会**把渠道合并**。
> 要渠道粒度请一律走 `channel_id` JOIN `channels`，别用这一列代替。
>
> ⚠️ 旧文档给的 `click_id` / `device_id` 两个键不存在，取它们得到 NULL。要看真正的
> UTM 三件套请查 `sessions` 表的 `utm_source` / `utm_medium` / `utm_campaign`
> （2,135,350 行），那三列与本表无关、也不保证对得上。
>
> 这一栏两次改过，形态值得记一下。2026-09-01 之前它整列 NULL；09-01 到 09-02 之间
> 149,474 行**全是同一个** `{"utm_source": "douyin"}`——14 个渠道的行（含 App Store、
> 直接访问）都写着 douyin，按它统计渠道会得到「100% 来自抖音」。**那时它不是空的，
> 是错的**，而错的比空的危险：`IS NOT NULL` 一路通过、`json_extract_scalar` 也确实返回
> 一个讲得通的值，空值会让人停下来查，假值不会。

### attributed_at 归因确定时间

> ⚠️ **这一列不是"归因确定时间"，它逐行等于 `click_time`**（149,474 行全部相等）。
> 所以「点击到归因确定的延迟」恒为 0，不是"归因很快"，是这两列没有独立信息。
> 更别拿它当 ETL 落库时刻——它有真实的业务时间分布（2025-10-26 ~ 2026-01-24、91 天），
> 用来开时间窗是可以的，只是要知道你实际按的是点击日。
>
> 与 `install_time` 相等的只有 92,529 行，正是 `days_to_install = 0` 的那批；
> 其余 56,945 行里 `attributed_at < install_time`，即"归因先于安装确定"——
> 真实归因管线不会这样，**别拿这两列的先后关系讲转化故事**。
>
> `days_to_install` 取值 0~5：当日装 92,529 行（61.90%）、1~3 天 47,968 行（32.09%）、
> 3 天以上 8,977 行（6.01%）。没有更长的尾巴，所以"长周期归因窗口"在这份数据里无从谈起。

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
