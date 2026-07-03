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
| 值 | 说明 |
|----|------|
| direct | 直接访问 |
| organic_search | 自然搜索 |
| paid_search | 付费搜索 |
| social | 社交媒体 |
| referral | 外部引荐 |
| email | 邮件营销 |
| push | 推送通知 |

### is_bounce 跳出判定规则
| 条件 | 值 |
|------|-----|
| page_view_count = 1 | TRUE |
| page_view_count > 1 | FALSE |

### 常见 utm_source 值
```
google, baidu, weixin, weibo, douyin,
xiaohongshu, taobao, jd, facebook, instagram
```

### 常见 utm_medium 值
```
cpc, cpm, banner, email, social, organic,
affiliate, referral, display, video
```

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
WHERE start_time >= CURRENT_DATE - INTERVAL '7 days'
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
WHERE start_time >= CURRENT_DATE - INTERVAL '7 days'
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
WHERE start_time >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(start_time)
ORDER BY date DESC;
```
