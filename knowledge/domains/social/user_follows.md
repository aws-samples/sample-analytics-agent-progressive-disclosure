# user_follows - 用户关注关系表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| follower_id | BIGINT | 联合主键，关注者ID，关联 users.user_id |
| following_id | BIGINT | 联合主键，被关注者ID，关联 users.user_id |
| created_at | TIMESTAMP | 关注时间 |

## 字段说明

- `follower_id`: 发起关注的用户（粉丝）
- `following_id`: 被关注的用户（博主/KOL）
- 双向关注 = A关注B 且 B关注A（互相关注）

## 索引

- PRIMARY KEY: (`follower_id`, `following_id`)
- INDEX: `following_id`（查询某人的粉丝列表）
- INDEX: `created_at`（查询关注时间趋势）

## 常用查询

### KOL/KOC 识别（高粉丝+高互动）
```sql
WITH user_stats AS (
    SELECT
        u.user_id,
        u.username,
        COUNT(DISTINCT f.follower_id) AS follower_count,
        COUNT(DISTINCT p.post_id) AS post_count,
        SUM(p.like_count) AS total_likes,
        SUM(p.comment_count) AS total_comments,
        SUM(p.view_count) AS total_views
    FROM users u
    LEFT JOIN user_follows f ON u.user_id = f.following_id
    LEFT JOIN posts p ON u.user_id = p.user_id AND p.status = 'published'
    GROUP BY u.user_id, u.username
)
SELECT
    user_id,
    username,
    follower_count,
    post_count,
    total_likes,
    ROUND((total_likes + total_comments) * 100.0 / NULLIF(total_views, 0), 2) AS engagement_rate
FROM user_stats
WHERE follower_count >= 1000
    AND post_count >= 10
ORDER BY engagement_rate DESC
LIMIT 50;
```

### 粉丝增长趋势
```sql
SELECT
    DATE(created_at) AS follow_date,
    COUNT(*) AS new_follows
FROM user_follows
WHERE following_id = :target_user_id
    AND created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(created_at)
ORDER BY follow_date;
```

### 互关用户统计
```sql
SELECT
    u.user_id,
    u.username,
    COUNT(*) AS mutual_follow_count
FROM users u
JOIN user_follows f1 ON u.user_id = f1.follower_id
JOIN user_follows f2 ON f1.follower_id = f2.following_id
    AND f1.following_id = f2.follower_id
GROUP BY u.user_id, u.username
ORDER BY mutual_follow_count DESC
LIMIT 100;
```
