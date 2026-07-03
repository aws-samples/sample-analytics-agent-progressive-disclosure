# post_shares - 帖子分享表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| share_id | BIGINT | 主键 |
| user_id | BIGINT | 分享用户ID，关联 users.user_id |
| post_id | BIGINT | 被分享帖子ID，关联 posts.post_id |
| share_channel | VARCHAR(30) | 分享渠道 |
| created_at | TIMESTAMP | 分享时间 |

## 字段枚举值

### share_channel 分享渠道
| 值 | 说明 |
|----|------|
| wechat_friend | 微信好友 |
| wechat_moments | 微信朋友圈 |
| weibo | 微博 |
| qq | QQ |
| copy_link | 复制链接 |
| save_image | 保存图片 |
| in_app | 站内转发 |

## 字段说明

- 同一用户可以多次分享同一帖子到不同渠道
- `posts.share_count` 通过触发器或定时任务同步更新
- 分享是社交裂变的核心指标

## 索引

- PRIMARY KEY: `share_id`
- INDEX: `post_id`, `user_id`, `share_channel`, `created_at`

## 常用查询

### 分享渠道分布
```sql
SELECT
    share_channel,
    COUNT(*) AS share_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM post_shares
WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY share_channel
ORDER BY share_count DESC;
```

### 高传播力内容（分享最多）
```sql
SELECT
    p.post_id,
    p.title,
    p.content_type,
    u.username AS author,
    COUNT(ps.share_id) AS share_count,
    COUNT(DISTINCT ps.user_id) AS sharer_count
FROM posts p
JOIN users u ON p.user_id = u.user_id
JOIN post_shares ps ON p.post_id = ps.post_id
WHERE p.status = 'published'
    AND ps.created_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY p.post_id, p.title, p.content_type, u.username
ORDER BY share_count DESC
LIMIT 50;
```

### 社交裂变分析（分享到微信的转化）
```sql
SELECT
    DATE(created_at) AS share_date,
    share_channel,
    COUNT(*) AS share_count,
    COUNT(DISTINCT user_id) AS unique_sharers,
    COUNT(DISTINCT post_id) AS unique_posts
FROM post_shares
WHERE share_channel IN ('wechat_friend', 'wechat_moments')
    AND created_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(created_at), share_channel
ORDER BY share_date DESC, share_count DESC;
```
