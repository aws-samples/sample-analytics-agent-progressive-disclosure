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

### page_name 页面名称（全 15 种，实测）
| 值 | 说明 | 实测行数 |
|----|------|------|
| settings | 设置页 | 2,070 |
| checkout | 结算页 | 2,057 |
| messages | 私信页 | 2,053 |
| post_detail | 帖子详情页 | 2,049 |
| coupon_center | 领券中心 | 2,043 |
| favorites | 收藏页 | 2,036 |
| post_feed | 内容流 | 2,035 |
| home | 首页 | 2,026 |
| product_detail | 商品详情页 | 2,010 |
| order_list | 订单列表页 | 2,006 |
| category | 分类页 | 1,980 |
| cart | 购物车页 | 1,976 |
| order_detail | 订单详情页 | 1,957 |
| profile | 个人主页 | 1,954 |
| search | 搜索页 | 1,944 |

> ⚠️ 旧文档写的 `search_result` / `order_confirm` / `payment` / `payment_success` /
> `user_center` **都不存在**（用户中心叫 `profile`）；漏掉了社交侧的 `messages` /
> `post_detail` / `post_feed` / `favorites` / `coupon_center` / `settings` /
> `order_detail`。
>
> 30,000 行摊在 15 个页面上**几乎完全均匀**（1,944–2,070，极差 6%），
> 所以这份数据上排不出有意义的「热门页面榜」——见下面查询里的提醒。

### scroll_depth_pct 滚动深度说明

> 这是 0–100 的整数百分比（实测 81 种取值），**不是枚举**，别照着固定档位筛。
> 要分档就自己 `CASE WHEN`：0-25 浅度 / 26-50 中度 / 51-75 深度 / 76-100 完整。

## 索引

- PRIMARY KEY: `page_view_id`
- INDEX: `user_id`, `session_id`, `view_time`
- INDEX: `page_name`

## 常用查询

### 页面浏览量排行 TOP 10
```sql
-- ⚠️ 这份种子数据里 15 个页面的 PV 几乎相等（极差 6%），排名主要是噪声。
-- 报结论时要么带上差异幅度，要么直接说「分布均匀，无显著热门页」。
-- 时间窗锚在业务日历上，不用 CURRENT_DATE（静态快照）
SELECT
    page_name,
    COUNT(*) AS pv,
    COUNT(DISTINCT user_id) AS uv,
    AVG(duration_seconds) AS avg_duration,
    AVG(scroll_depth_pct) AS avg_scroll_depth
FROM page_views
WHERE view_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
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
    approx_percentile(duration_seconds, 0.5) AS median_duration
FROM page_views
WHERE view_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
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
