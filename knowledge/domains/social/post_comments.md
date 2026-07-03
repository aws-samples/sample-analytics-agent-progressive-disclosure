# post_comments - 帖子评论表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| comment_id | BIGINT | 主键，评论ID |
| post_id | BIGINT | 关联帖子ID，关联 posts.post_id |
| user_id | BIGINT | 评论用户ID，关联 users.user_id |
| parent_comment_id | BIGINT | 父评论ID（回复场景），NULL表示一级评论 |
| content | TEXT | 评论内容 |
| like_count | INT | 评论点赞数 |
| status | VARCHAR(20) | 评论状态 |
| created_at | TIMESTAMP | 评论时间 |

## 字段枚举值

### status 评论状态
| 值 | 说明 |
|----|------|
| visible | 正常显示 |
| hidden | 已隐藏（违规） |
| deleted | 已删除（用户主动删除） |

## 字段说明

- `parent_comment_id = NULL`: 一级评论（直接评论帖子）
- `parent_comment_id != NULL`: 二级评论（回复其他评论）
- 支持评论被点赞，`like_count` 记录评论的点赞数

## 索引

- PRIMARY KEY: `comment_id`
- INDEX: `post_id`, `user_id`, `parent_comment_id`, `status`, `created_at`

## 常用查询

### 评论趋势分析
```sql
SELECT
    DATE(created_at) AS comment_date,
    COUNT(*) AS comment_count,
    COUNT(DISTINCT user_id) AS commenter_count
FROM post_comments
WHERE status = 'visible'
    AND created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(created_at)
ORDER BY comment_date;
```

### 热门评论（高点赞评论）
```sql
SELECT
    c.comment_id,
    c.content,
    c.like_count,
    u.username AS commenter,
    p.title AS post_title
FROM post_comments c
JOIN users u ON c.user_id = u.user_id
JOIN posts p ON c.post_id = p.post_id
WHERE c.status = 'visible'
    AND c.created_at >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY c.like_count DESC
LIMIT 50;
```

### 评论互动分析（回复率）
```sql
WITH comment_stats AS (
    SELECT
        post_id,
        COUNT(*) AS total_comments,
        COUNT(CASE WHEN parent_comment_id IS NULL THEN 1 END) AS top_level_comments,
        COUNT(CASE WHEN parent_comment_id IS NOT NULL THEN 1 END) AS replies
    FROM post_comments
    WHERE status = 'visible'
    GROUP BY post_id
)
SELECT
    p.post_id,
    p.title,
    cs.total_comments,
    cs.top_level_comments,
    cs.replies,
    ROUND(cs.replies * 100.0 / NULLIF(cs.top_level_comments, 0), 2) AS reply_rate
FROM comment_stats cs
JOIN posts p ON cs.post_id = p.post_id
WHERE p.status = 'published'
ORDER BY cs.total_comments DESC
LIMIT 100;
```
