# sessions - 会话表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| session_id | BIGINT | 主键，会话ID |
| user_id | BIGINT | 用户ID（未登录为NULL） |
| device_id | VARCHAR(64) | 设备ID |
| start_time | TIMESTAMP | 会话开始时间 |
| end_time | TIMESTAMP | 会话结束时间 |
| duration_seconds | INT | 会话时长（秒） |
| event_count | INT | 会话内事件数 |
| page_view_count | INT | 会话内页面浏览数 |
| is_bounce | BOOLEAN | 是否跳出（仅1个页面） |
| entry_page | VARCHAR(100) | 入口页面 |
| exit_page | VARCHAR(100) | 退出页面 |
| traffic_source | VARCHAR(50) | 流量来源 |
| utm_source | VARCHAR(50) | UTM来源 |
| utm_medium | VARCHAR(50) | UTM媒介 |
| utm_campaign | VARCHAR(100) | UTM活动 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### traffic_source 流量来源
| 值 | 说明 | 实测行数 |
|----|------|------|
| referral | 外部引荐 | 868 |
| social | 社交媒体 | 846 |
| paid | 付费流量 | 826 |
| organic | 自然流量 | 825 |
| direct | 直接访问 | 824 |
| email | 邮件营销 | 811 |

> 全表 5,000 行，6 个值，**分布均匀**（811–868）。旧文档写的 `organic_search` /
> `paid_search` / `push` 都不存在——自然量是 `organic`、付费是 `paid`，没有细分搜索。
> 注意这套值跟 `channels.channel_type`（paid / kol / organic / referral / direct）
> 只是**部分重叠**：这里有 `social`/`email`，那边有 `kol`，两张表不能直接按值 JOIN。

### is_bounce 跳出判定规则
| 条件 | 值 |
|------|-----|
| page_view_count = 1 | TRUE |
| page_view_count > 1 | FALSE |

### utm_source 流量来源平台
| 值 | 说明 | 实测行数 |
|----|------|------|
| weixin | 微信 | 861 |
| douyin | 抖音 | 849 |
| baidu | 百度 | 835 |
| xiaohongshu | 小红书 | 832 |
| organic | 自然流量 | 816 |
| direct | 直接访问 | 807 |

> 只有这 6 个，全是国内平台。旧文档写的 `google` / `weibo` / `taobao` / `jd` /
> `facebook` / `instagram` **都不存在**，这一列也没有 NULL，也没有字符串 `'none'`。

### utm_medium 投放形式
| 值 | 说明 | 实测行数 |
|----|------|------|
| organic | 自然 | 1008 |
| push | 推送 | 1007 |
| paid | 付费 | 1007 |
| referral | 引荐 | 1004 |
| banner | 横幅 | 974 |

> 只有这 5 个。旧文档写的 `cpc` / `cpm` / `social` / `affiliate` / `display` / `video`
> **都不存在**；`email` 也不在这里（它是 `traffic_source` 的值）。

### utm_campaign 活动标记
| 值 | 说明 | 实测行数 |
|----|------|------|
| 618 | 618 大促 | 745 |
| new_user | 新客 | 732 |
| brand_day | 品牌日 | 708 |
| spring_sale | 春季促销 | 702 |
| double11 | 双 11 | 695 |
| recall | 召回 | 679 |

> 另有 739 行为 NULL——这是本表 UTM 三列里唯一有空值的一列，做归因时记得
> `COALESCE(utm_campaign, '(未标记)')`，否则这 15% 的会话会在 GROUP BY 里悄悄变成一个空行。

> 上面三列 2026-08-28 从围栏代码块改写成表格。取值和实测行数一个没动——改的只是**形态**：
> `scripts/lakehouse/verify_enums.py` 的 `parse_card()` 只认「`### 列名`＋`| 值 | 说明 |`」，
> 围栏块里的声明它一律不收（那条行为它自己的自测还专门断言过）。于是这三列虽然写在卡片上，
> 却从未被任何一层比对过，`utm_medium` 的生成器一直在产这张表明文否认的 `cpc` / `social` /
> `email`。声明写成看得见的形态，是这条被发现的前提。

## 索引

- PRIMARY KEY: `session_id`
- INDEX: `user_id`, `device_id`, `start_time`
- INDEX: `traffic_source`, `utm_source`

## 常用查询

### 日活用户及平均会话时长
```sql
SELECT
    DATE(start_time) AS date,
    COUNT(DISTINCT user_id) AS dau,
    COUNT(*) AS total_sessions,
    AVG(duration_seconds) AS avg_session_duration,
    AVG(page_view_count) AS avg_pages_per_session
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
    AND user_id IS NOT NULL
GROUP BY DATE(start_time)
ORDER BY date DESC;
```

### 流量来源分析（带UTM）
```sql
SELECT
    COALESCE(utm_source, traffic_source, 'unknown') AS source,
    utm_medium,
    utm_campaign,
    COUNT(*) AS sessions,
    COUNT(DISTINCT user_id) AS unique_users,
    AVG(duration_seconds) AS avg_duration,
    SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS bounce_rate
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
GROUP BY utm_source, traffic_source, utm_medium, utm_campaign
ORDER BY sessions DESC
LIMIT 20;
```

### 跳出率趋势分析
```sql
SELECT
    DATE(start_time) AS date,
    COUNT(*) AS total_sessions,
    SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) AS bounce_sessions,
    ROUND(SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS bounce_rate
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(start_time)
ORDER BY date DESC;
```
