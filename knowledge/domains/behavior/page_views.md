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
| settings | 设置页 | 142,867 |
| checkout | 结算页 | 379,673 |
| messages | 私信页 | 253,528 |
| post_detail | 帖子详情页 | 521,920 |
| coupon_center | 领券中心 | 300,865 |
| favorites | 收藏页 | 221,918 |
| post_feed | 内容流 | 664,532 |
| home | 首页 | 3,164,394 |
| product_detail | 商品详情页 | 1,930,622 |
| order_list | 订单列表页 | 569,548 |
| category | 分类页 | 1,644,214 |
| cart | 购物车页 | 822,943 |
| order_detail | 订单详情页 | 475,856 |
| profile | 个人主页 | 442,713 |
| search | 搜索页 | 1,360,213 |

> ⚠️ 旧文档写的 `search_result` / `order_confirm` / `payment` / `payment_success` /
> `user_center` **都不存在**（用户中心叫 `profile`）；漏掉了社交侧的 `messages` /
> `post_detail` / `post_feed` / `favorites` / `coupon_center` / `settings` /
> `order_detail`。
>
> 12,895,806 行摊在 15 个页面上**是长尾而非均匀**（`settings` 142,867 – `home` 3,164,394，最高/最低约 22 倍），
> 热门页面榜可以排（2026-11 重灌前是 30,000 行、极差 6% 的近似均匀，已作废）。

### scroll_depth_pct 滚动深度说明

> 这是 0–100 的整数百分比，但实测只有 10 / 25 / 50 / 75 / 100 五种取值（离散档位）。
> 要分档就自己 `CASE WHEN`：0-25 浅度 / 26-50 中度 / 51-75 深度 / 76-100 完整。

## 索引

- PRIMARY KEY: `page_view_id`
- INDEX: `user_id`, `session_id`, `view_time`
- INDEX: `page_name`

## 常用查询

### 页面浏览量排行 TOP 10
```sql
-- 当前数据 15 个页面 PV 是长尾（最高/最低约 22 倍），排名可读；
-- 报结论时带上差异幅度。
-- 时间窗锚在业务日历上，不用 CURRENT_DATE（静态快照）
SELECT
    page_name,
    COUNT(*) AS pv,
    COUNT(DISTINCT user_id) AS uv,
    AVG(duration_seconds) AS avg_duration,
    AVG(scroll_depth_pct) AS avg_scroll_depth
FROM page_views
WHERE view_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '6' day AND view_time < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
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
WHERE view_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '6' day AND view_time < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
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
