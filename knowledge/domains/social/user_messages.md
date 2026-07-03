# user_messages - 用户私信表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| message_id | BIGINT | 主键，消息ID |
| sender_id | BIGINT | 发送者ID，关联 users.user_id |
| receiver_id | BIGINT | 接收者ID，关联 users.user_id |
| content | TEXT | 消息内容 |
| message_type | VARCHAR(20) | 消息类型 |
| related_post_id | BIGINT | 关联帖子ID（分享帖子时） |
| related_product_id | BIGINT | 关联商品ID（分享商品时） |
| is_read | BOOLEAN | 是否已读 |
| sent_at | TIMESTAMP | 发送时间 |
| read_at | TIMESTAMP | 阅读时间 |

## 字段枚举值

### message_type 消息类型
| 值 | 说明 |
|----|------|
| text | 纯文字 |
| image | 图片 |
| post_share | 帖子分享 |
| product_share | 商品分享 |
| system | 系统消息 |

## 字段说明

- `related_post_id`: 当 message_type = 'post_share' 时有值
- `related_product_id`: 当 message_type = 'product_share' 时有值
- `is_read`: 默认 FALSE，接收者阅读后更新为 TRUE
- `read_at`: 首次阅读时间

## 索引

- PRIMARY KEY: `message_id`
- INDEX: `sender_id`, `receiver_id`, `message_type`, `sent_at`
- INDEX: (`receiver_id`, `is_read`)（未读消息查询）

## 常用查询

### 私信活跃度分析
```sql
SELECT
    DATE(sent_at) AS msg_date,
    COUNT(*) AS total_messages,
    COUNT(DISTINCT sender_id) AS unique_senders,
    COUNT(DISTINCT receiver_id) AS unique_receivers,
    ROUND(AVG(CASE WHEN is_read THEN 1 ELSE 0 END) * 100, 2) AS read_rate
FROM user_messages
WHERE sent_at >= CURRENT_DATE - INTERVAL '30 days'
    AND message_type != 'system'
GROUP BY DATE(sent_at)
ORDER BY msg_date;
```

### 消息类型分布
```sql
SELECT
    message_type,
    COUNT(*) AS message_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct,
    ROUND(AVG(CASE WHEN is_read THEN 1 ELSE 0 END) * 100, 2) AS read_rate
FROM user_messages
WHERE sent_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY message_type
ORDER BY message_count DESC;
```

### 消息响应时间分析
```sql
SELECT
    CASE
        WHEN EXTRACT(EPOCH FROM (read_at - sent_at)) / 60 < 5 THEN '0-5分钟'
        WHEN EXTRACT(EPOCH FROM (read_at - sent_at)) / 60 < 30 THEN '5-30分钟'
        WHEN EXTRACT(EPOCH FROM (read_at - sent_at)) / 60 < 60 THEN '30-60分钟'
        WHEN EXTRACT(EPOCH FROM (read_at - sent_at)) / 3600 < 24 THEN '1-24小时'
        ELSE '>24小时'
    END AS response_time_bucket,
    COUNT(*) AS message_count
FROM user_messages
WHERE is_read = TRUE
    AND sent_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1
ORDER BY
    CASE response_time_bucket
        WHEN '0-5分钟' THEN 1
        WHEN '5-30分钟' THEN 2
        WHEN '30-60分钟' THEN 3
        WHEN '1-24小时' THEN 4
        ELSE 5
    END;
```
