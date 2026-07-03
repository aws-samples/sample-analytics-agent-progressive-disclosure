# push_notifications - 推送通知表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| push_id | BIGINT | 推送ID，主键，自增 |
| user_id | BIGINT | 目标用户ID，关联 users.user_id |
| campaign_id | INT | 关联营销活动ID |
| push_type | VARCHAR(30) | 推送类型 |
| title | VARCHAR(200) | 推送标题 |
| content | TEXT | 推送内容 |
| deep_link | VARCHAR(500) | 点击跳转深度链接 |
| scheduled_at | TIMESTAMP | 计划发送时间 |
| sent_at | TIMESTAMP | 实际发送时间 |
| delivered_at | TIMESTAMP | 送达时间 |
| opened_at | TIMESTAMP | 用户打开时间 |
| is_delivered | BOOLEAN | 是否送达 |
| is_opened | BOOLEAN | 是否打开 |
| failure_reason | VARCHAR(200) | 发送失败原因 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### push_type 推送类型
| 值 | 说明 |
|----|------|
| marketing | 营销推送（促销、活动通知） |
| transactional | 交易推送（订单状态、物流更新） |
| reminder | 提醒推送（购物车提醒、签到提醒） |

### 状态判断逻辑
| 场景 | 判断条件 |
|------|----------|
| 发送成功 | `sent_at IS NOT NULL AND is_delivered = true` |
| 用户打开 | `is_opened = true` |
| 发送失败 | `failure_reason IS NOT NULL` |
| 待发送 | `scheduled_at IS NOT NULL AND sent_at IS NULL` |

### failure_reason 常见失败原因
| 值 | 说明 |
|----|------|
| device_unregistered | 设备未注册推送 |
| token_expired | 推送token过期 |
| user_opted_out | 用户关闭推送权限 |
| rate_limited | 推送频率限制 |
| network_error | 网络错误 |

## 索引

- PRIMARY KEY: `push_id`
- INDEX: `user_id`, `campaign_id`, `push_type`
- INDEX: `sent_at`, `scheduled_at`
- INDEX: `is_delivered`, `is_opened`

## 常用查询

### 营销活动推送效果分析
```sql
SELECT
    c.campaign_id,
    c.campaign_name,
    COUNT(p.push_id) AS total_sent,
    COUNT(CASE WHEN p.is_delivered THEN 1 END) AS delivered_count,
    COUNT(CASE WHEN p.is_opened THEN 1 END) AS opened_count,
    ROUND(COUNT(CASE WHEN p.is_delivered THEN 1 END) * 100.0 /
          NULLIF(COUNT(p.push_id), 0), 2) AS delivery_rate,
    ROUND(COUNT(CASE WHEN p.is_opened THEN 1 END) * 100.0 /
          NULLIF(COUNT(CASE WHEN p.is_delivered THEN 1 END), 0), 2) AS open_rate
FROM campaigns c
JOIN push_notifications p ON c.campaign_id = p.campaign_id
WHERE c.status = 'completed'
GROUP BY c.campaign_id, c.campaign_name
ORDER BY total_sent DESC;
```

### 推送类型效果对比
```sql
SELECT
    push_type,
    COUNT(*) AS total_sent,
    COUNT(CASE WHEN is_delivered THEN 1 END) AS delivered,
    COUNT(CASE WHEN is_opened THEN 1 END) AS opened,
    ROUND(COUNT(CASE WHEN is_delivered THEN 1 END) * 100.0 /
          NULLIF(COUNT(*), 0), 2) AS delivery_rate,
    ROUND(COUNT(CASE WHEN is_opened THEN 1 END) * 100.0 /
          NULLIF(COUNT(CASE WHEN is_delivered THEN 1 END), 0), 2) AS open_rate
FROM push_notifications
WHERE sent_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY push_type
ORDER BY total_sent DESC;
```

### 推送失败原因分析
```sql
SELECT
    failure_reason,
    COUNT(*) AS failure_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM push_notifications
WHERE failure_reason IS NOT NULL
  AND sent_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY failure_reason
ORDER BY failure_count DESC;
```
