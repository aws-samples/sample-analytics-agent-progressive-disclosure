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
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| short_video | 短视频（<60秒） | 136,435 | 31.95% |
| image | 图片帖（1-9张图片为主） | 119,823 | 28.06% |
| article | 图文文章（长内容，多段文字+图片） | 85,430 | 20.00% |
| review | 商品评测/种草笔记 | 85,382 | 19.99% |

### status 内容状态
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| published | 已发布 | 361,578 | 84.66% |
| under_review | 审核中 | 23,759 | 5.56% |
| deleted | 已删除 | 21,038 | 4.93% |
| draft | 草稿 | 20,695 | 4.85% |

> 审核态的值是 `under_review`，**不是** `pending`；也没有 `hidden`。
> 只统计公开内容时用 `status = 'published'`（占 84.66%，全表 427,070 行）。

## 三个数组列的口径

| 列 | 口径 |
|---|---|
| `media_urls` | 每帖至少 1 个。路径形如 `https://cdn.example.com/posts/{post_id}/{n}.{ext}`，**挂的是本帖自己的 post_id**。`short_video` 恒为 1 个 `.mp4`，其余三种类型是 `.jpg`（`image` 1~9 张、`article` 1~5、`review` 1~6）。所以「带视频的帖子」既可以按 `content_type = 'short_video'` 也可以按后缀判，两者结果相同 |
| `tags` | 每帖至少 2 个，**第一个恒为本帖的商品品类词**（叶子类目名，如 `吹风机` / `连衣裙`），后面接 1~3 个运营话题（`好物推荐` / `平价替代` / `双十一囤货` …）。要统计"哪个品类被种草最多"，`tags[1]`（Trino 数组下标从 1 开始）比 `UNNEST` 全部标签准——后者会把运营话题一起算进去 |
| `product_ids` | 只包含**本帖品类下**的真实 SKU，与 `tags[1]` 和标题说的是同一件东西。`review`（评测/种草笔记）恒关联 1 件；`article` 0~3、`image` 0~2、`short_video` 0~1，因此约三成帖子这一列是**空数组**（实测 129,359 行 = 30.29%，不是 NULL；按类型看 `short_video` 49.9% 空、`image` 33.4%、`article` 24.8%、`review` 0%）。空数组不参与 `CROSS JOIN UNNEST`，那些帖子会被下面「商品种草效果分析」这类查询自动排除——**所以那类查询的分母是 297,711 帖而不是 427,070 帖**，短视频被排掉的比例最高，按内容类型比较种草效果时要留意这个口径差 |

> 空数组和 NULL 不是一回事：`cardinality(product_ids) = 0` 才是"没关联商品"，
> `product_ids IS NULL` 在这张表上选不出行。

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
    AND published_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
    AND published_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
CROSS JOIN UNNEST(po.product_ids) AS p(product_id)
JOIN products pr ON p.product_id = pr.product_id
WHERE po.status = 'published'
    AND po.published_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY p.product_id, pr.product_name
HAVING COUNT(DISTINCT po.post_id) >= 5
ORDER BY total_exposure DESC
LIMIT 20;
```
