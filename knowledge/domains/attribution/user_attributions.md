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
| tracking_params | `string` | 追踪参数（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。只有 `utm_source` 一个键，= 本行渠道的 `channels.platform`，见下 |

## 字段枚举值

### attribution_type 归因模型
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| first_touch | 首次触点归因：将转化归因于用户首次接触的渠道 | 74,737 | 50.00% |
| last_touch | 末次触点归因：将转化归因于用户最后接触的渠道（最常用） | 74,737 | 50.00% |

> ⚠️ **只有这两种，没有 `linear`**（旧文档写过，按它筛是空集）。
>
> 全表 149,474 行 / 74,737 个用户——**被归因的用户每人恰好两行：一条 first_touch + 一条
> last_touch**，是同一个用户的两种归因视角。所以：
> - 按某一种归因模型看渠道，**必须**加 `WHERE attribution_type = 'last_touch'`（或 first_touch），
>   每人一行、不漏人；不加条件直接 GROUP BY 会把每个人数两遍，人数翻倍、两种模型混在一起。
> - 33,413 人（44.7%）只有一次触点：两行的渠道、活动、点击时刻完全相同；其余用户的
>   first_touch 早于 last_touch 1 小时 ~ 14 天，渠道可能不同。
>
> **时序**：`click_time ≤ install_time ≤ users.registered_at = attributed_at`（先点广告、再装 App、
> 再注册；归因在注册那一刻落定）。所以 `click_time` 早于注册是正常的；按"归因日"分桶用
> `attributed_at`，它就是注册时刻。`days_to_install` = ⌊(install − click) / 1 天⌋，逐行成立。
>
> **广告活动**：`ad_campaign_id` 只挂本渠道、点击当天在档期内的活动；投放渠道
> （paid / kol）点击时没有在档活动的为 NULL（paid 6.0%、kol 14.2%），自然量渠道
> （organic / referral / direct）恒为 NULL。
>
> 覆盖率也要留意：有过成交的买家 126,010 人，其中 44,096 人在这张表里有归因记录，
> 另 81,914 人一条归因都没有。分渠道 GMV 里那个占 **65.3%** 的 `(未归因)` 桶就是这么来的——
> **别把它当成一个真实渠道**，也别说成"六成买家来路不明"：准确说法是"这部分成交没有可追踪的投放触点"。

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

> 归因在**注册（转化）那一刻**落定：`attributed_at` 逐行等于该用户的 `users.registered_at`，
> 同一用户的 first_touch / last_touch 两行相同。所以「按归因日看新客」与「按注册日看新客」
> 是同一个口径（`mart_channel_daily` 按注册日、`dws_channel_weekly` 按 `attributed_at` 所在周，
> 两者对得上）。看投放发生在哪天用 `click_time`，它早于注册（见上面「时序」）。
>
> （2026-11 重灌前这一列逐行等于 `click_time`、点击时刻与注册无关，51% 的点击晚于注册。旧说明已作废。）
>
> `days_to_install` 按行算：当日装 68,447 行（45.79%）、1~3 天 42,923 行（28.72%）、
> 3 天以上 38,104 行（25.49%）。last_touch 只有 0 / 1 / 2 / 5 四个值（当日 61.72%）；
> first_touch 多触点那批点击更早，最长 19 天。所以"点击到安装的周期"要分归因模型看。

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
WHERE ua.attribution_type = 'last_touch'      -- 每人两行，按一种归因模型取
  AND ua.attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day AND ua.attributed_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
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
    COUNT(CASE WHEN ua.days_to_install = 0 THEN 1 END) * 100.0 / COUNT(*) AS same_day_rate
FROM user_attributions ua
JOIN channels ch ON ua.channel_id = ch.channel_id
WHERE ua.attribution_type = 'last_touch'      -- first_touch 的点击更早、天数更大，两种模型别混在一起
  AND ua.attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day AND ua.attributed_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
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
WHERE attributed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day AND attributed_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
GROUP BY attribution_type
ORDER BY attribution_count DESC;
```
