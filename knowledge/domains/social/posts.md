# posts - 内容帖子表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| post_id | BIGINT | 主键，帖子ID |
| user_id | BIGINT | 发布者用户ID，关联 users.user_id |
| content_type | VARCHAR(20) | 内容类型 |
| title | VARCHAR(200) | 标题 |
| content | TEXT | 正文内容 |
| media_urls | VARCHAR[] | 媒体文件URL数组 |
| tags | VARCHAR[] | 话题标签数组 |
| location | VARCHAR(100) | 发布位置 |
| product_ids | BIGINT[] | 关联商品ID数组 |
| view_count | INT | 浏览量 |
| like_count | INT | 点赞数 |
| comment_count | INT | 评论数 |
| share_count | INT | 分享数 |
| status | VARCHAR(20) | 内容状态 |
| is_featured | BOOLEAN | 是否精选/推荐 |
| published_at | TIMESTAMP | 发布时间 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### content_type 内容类型
| 值 | 说明 |
|----|------|
| article | 图文文章（长内容，多段文字+图片） |
| image | 图片帖（1-9张图片为主） |
| short_video | 短视频（<60秒） |
| review | 商品评测/种草笔记 |

### status 内容状态
| 值 | 说明 |
|----|------|
| draft | 草稿 |
| pending | 待审核 |
| published | 已发布 |
| hidden | 已隐藏（违规或用户删除） |
| deleted | 已删除 |

## 索引

- PRIMARY KEY: `post_id`
- INDEX: `user_id`, `content_type`, `status`, `published_at`, `is_featured`

## 常用查询

### 内容发布趋势（按类型）
```sql
SELECT
    DATE(published_at) AS pub_date,
    content_type,
    COUNT(*) AS post_count
FROM posts
WHERE status = 'published'
    AND published_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(published_at), content_type
ORDER BY pub_date DESC, post_count DESC;
```

### 内容表现分析（互动率排名）
```sql
SELECT
    content_type,
    COUNT(*) AS post_count,
    AVG(view_count) AS avg_views,
    AVG(like_count) AS avg_likes,
    AVG(comment_count) AS avg_comments,
    AVG(share_count) AS avg_shares,
    ROUND(AVG((like_count + comment_count + share_count) * 100.0 / NULLIF(view_count, 0)), 2) AS avg_engagement_rate
FROM posts
WHERE status = 'published'
    AND published_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY content_type
ORDER BY avg_engagement_rate DESC;
```

### 商品种草效果分析
```sql
SELECT
    p.product_id,
    pr.product_name,
    COUNT(DISTINCT po.post_id) AS mention_count,
    SUM(po.view_count) AS total_exposure,
    SUM(po.like_count) AS total_likes,
    COUNT(DISTINCT po.user_id) AS creator_count
FROM posts po
CROSS JOIN LATERAL unnest(po.product_ids) AS p(product_id)
JOIN products pr ON p.product_id = pr.product_id
WHERE po.status = 'published'
    AND po.published_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY p.product_id, pr.product_name
HAVING COUNT(DISTINCT po.post_id) >= 5
ORDER BY total_exposure DESC
LIMIT 20;
```
