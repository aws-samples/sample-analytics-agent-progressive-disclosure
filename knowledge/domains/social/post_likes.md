# post_likes - 帖子点赞表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 联合主键，点赞用户ID，关联 users.user_id |
| post_id | BIGINT | 联合主键，帖子ID，关联 posts.post_id |
| created_at | TIMESTAMP | 点赞时间 |

## 字段说明

- 每个用户对每个帖子只能点赞一次
- 取消点赞后记录删除（物理删除）
- `posts.like_count` 通过触发器或定时任务同步更新

## 索引

- PRIMARY KEY: (`user_id`, `post_id`)
- INDEX: `post_id`（查询帖子的点赞用户列表）
- INDEX: `created_at`（查询点赞时间趋势）

## 常用查询

### 点赞趋势分析
```sql
SELECT
    DATE(created_at) AS like_date,
    COUNT(*) AS like_count
FROM post_likes
WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY DATE(created_at)
ORDER BY like_date;
```

### 帖子点赞用户画像
```sql
SELECT
    up.gender,
    CASE
        WHEN up.age < 18 THEN '0-17'
        WHEN up.age < 25 THEN '18-24'
        WHEN up.age < 35 THEN '25-34'
        WHEN up.age < 45 THEN '35-44'
        ELSE '45+'
    END AS age_group,
    COUNT(*) AS like_count
FROM post_likes pl
JOIN user_profiles up ON pl.user_id = up.user_id
WHERE pl.post_id = :target_post_id
GROUP BY up.gender, age_group
ORDER BY like_count DESC;
```

### 高频点赞用户（活跃粉丝识别）
```sql
SELECT
    pl.user_id,
    u.username,
    COUNT(*) AS total_likes,
    COUNT(DISTINCT DATE(pl.created_at)) AS active_days
FROM post_likes pl
JOIN users u ON pl.user_id = u.user_id
WHERE pl.created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY pl.user_id, u.username
HAVING COUNT(*) >= 50
ORDER BY total_likes DESC
LIMIT 100;
```
