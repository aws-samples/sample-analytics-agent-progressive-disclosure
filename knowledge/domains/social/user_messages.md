# user_messages - 用户私信表

> **这张表 agent 查不到，整表。** 私信正文是用户之间的通信内容，不在 agent 角色的
> 授权面里（Lake Formation 表级不授权，见 `scripts/lakehouse/governance.py` 的
> `DENY_TABLES`）：任何引用它的查询都会失败。**实测报的是 `TABLE_NOT_FOUND`**
> （"表不存在"，读起来像表名写错了），也可能是
> `Insufficient Lake Formation permission(s) on user_messages`——两种都是同一件事：
> 这张表不在授权面里。**不要重试、不要以为是表名拼错**。
> 遇到「私信/聊天/消息」类的问题，正确做法是**说清这张表按治理策略不开放**，
> 而不是写一条注定失败的 SQL、也不是拿 `posts` / `post_comments` 之类的公开内容
> 冒充私信去算。下面这份表结构留着是为了让「不开放」这件事可解释——
> 表里有什么、为什么不给看。

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
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| text | 纯文字 | 3,360,384 | 85.04% |
| image | 图片 | 387,323 | 9.80% |
| link | 链接分享（含帖子/商品链接） | 203,972 | 5.16% |

> 只有这 3 个值。**没有** `post_share` / `product_share` / `system`（旧文档写过）——
> 分享类消息统一是 `link`。全表 3,951,679 行。

## 字段说明

- `related_post_id` / `related_product_id`: 设计上给分享类消息带上被分享对象。
  两列都有值：`related_post_id` 非空 592,205 行（14.99%）、`related_product_id`
  非空 473,238 行（11.98%），非空的部分 JOIN 得回 `posts` / `products`。
  > ⚠️ **但这两列跟 `message_type` 不相关**：`text` / `image` / `link` 三类的非空率
  > 都是 ~15% / ~12%（实测 14.99%/11.98%、14.95%/11.93%、14.95%/12.06%），
  > 也就是说纯文字消息同样带着「被分享对象」。所以**不能拿 `related_post_id IS NOT NULL`
  > 当「这是一条分享消息」的判据**——那样算出来的分享量比 `link` 类总数还多三倍。
  > 要数分享消息就用 `message_type = 'link'`；要数「分享了帖子的消息」就两个条件都写上
  > （`message_type = 'link' AND related_post_id IS NOT NULL`，实测 30,503 行）。
  > 这是生成器的已知形态，2026-09-01 重灌后依旧如此。
  >
  > 这一条 2026-09-01 改过：在此之前这两列整列为 NULL，卡片写着「按它们做 JOIN 会得到
  > 空结果」。现在有值了，但要按上面的口径用。
- `is_read`: 默认 FALSE，接收者阅读后更新为 TRUE
- `read_at`: 首次阅读时间

## 索引

- PRIMARY KEY: `message_id`
- INDEX: `sender_id`, `receiver_id`, `message_type`, `sent_at`
- INDEX: (`receiver_id`, `is_read`)（未读消息查询）

## 常用查询

> ⚠️ 下面这几条**在 agent 角色下一条都跑不了**（整表未授权，见页首）。它们记的是
> 这张表本来该怎么查，用管理员凭证才有意义。别把它们当"可以试一下"的模板。

### 私信活跃度分析
```sql
SELECT
    DATE(sent_at) AS msg_date,
    COUNT(*) AS total_messages,
    COUNT(DISTINCT sender_id) AS unique_senders,
    COUNT(DISTINCT receiver_id) AS unique_receivers,
    ROUND(AVG(CASE WHEN is_read THEN 1 ELSE 0 END) * 100, 2) AS read_rate
FROM user_messages
WHERE sent_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
WHERE sent_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY message_type
ORDER BY message_count DESC;
```

### 消息响应时间分析
```sql
SELECT
    CASE
        WHEN date_diff('second', sent_at, read_at) / 60 < 5 THEN '0-5分钟'
        WHEN date_diff('second', sent_at, read_at) / 60 < 30 THEN '5-30分钟'
        WHEN date_diff('second', sent_at, read_at) / 60 < 60 THEN '30-60分钟'
        WHEN date_diff('second', sent_at, read_at) / 3600 < 24 THEN '1-24小时'
        ELSE '>24小时'
    END AS response_time_bucket,
    COUNT(*) AS message_count
FROM user_messages
WHERE is_read = TRUE
    AND sent_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
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
