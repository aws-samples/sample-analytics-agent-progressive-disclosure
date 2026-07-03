# page_views - 页面浏览表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| page_view_id | BIGINT | 主键 |
| user_id | BIGINT | 用户ID |
| session_id | BIGINT | 关联 sessions.session_id |
| page_name | VARCHAR(100) | 页面名称 |
| page_url | VARCHAR(500) | 页面URL/路径 |
| referrer | VARCHAR(500) | 来源页面 |
| duration_seconds | INT | 页面停留时长 |
| scroll_depth_pct | INT | 滚动深度百分比（0-100） |
| view_time | TIMESTAMP | 浏览时间 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### page_name 常见页面名称
| 值 | 说明 |
|----|------|
| home | 首页 |
| search | 搜索页 |
| search_result | 搜索结果页 |
| category | 分类页 |
| product_detail | 商品详情页 |
| cart | 购物车页 |
| checkout | 结算页 |
| order_confirm | 订单确认页 |
| payment | 支付页 |
| payment_success | 支付成功页 |
| user_center | 用户中心 |
| order_list | 订单列表页 |

### scroll_depth_pct 滚动深度说明
| 范围 | 说明 |
|------|------|
| 0-25 | 浅度浏览 |
| 26-50 | 中度浏览 |
| 51-75 | 深度浏览 |
| 76-100 | 完整浏览 |

## 索引

- PRIMARY KEY: `page_view_id`
- INDEX: `user_id`, `session_id`, `view_time`
- INDEX: `page_name`

## 常用查询

### 页面浏览量排行 TOP 10
```sql
SELECT
    page_name,
    COUNT(*) AS pv,
    COUNT(DISTINCT user_id) AS uv,
    AVG(duration_seconds) AS avg_duration,
    AVG(scroll_depth_pct) AS avg_scroll_depth
FROM page_views
WHERE view_time >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY page_name
ORDER BY pv DESC
LIMIT 10;
```

### 页面平均停留时长分析
```sql
SELECT
    page_name,
    COUNT(*) AS views,
    AVG(duration_seconds) AS avg_duration,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_seconds) AS median_duration
FROM page_views
WHERE view_time >= CURRENT_DATE - INTERVAL '7 days'
    AND duration_seconds > 0
GROUP BY page_name
ORDER BY avg_duration DESC;
```

### 用户浏览路径分析（单会话）
```sql
SELECT
    session_id,
    ARRAY_AGG(page_name ORDER BY view_time) AS page_path,
    COUNT(*) AS page_count,
    SUM(duration_seconds) AS total_duration
FROM page_views
WHERE session_id = 12345678  -- 指定会话ID
GROUP BY session_id;
```
