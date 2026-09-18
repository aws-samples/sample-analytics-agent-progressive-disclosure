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
| 值 | 说明 | 实测行数 | 占比 |
|----|------|------|------|
| reminder | 提醒推送（购物车提醒、签到提醒） | 881,014 | 20.63% |
| social | 社交推送（关注、评论、私信提醒） | 869,293 | 20.35% |
| promotion | 营销推送（促销、活动通知） | 854,365 | 20.01% |
| order | 交易推送（订单状态、物流更新） | 840,090 | 19.67% |
| system | 系统通知 | 825,938 | 19.34% |

> 全表 4,270,700 行、5 类，**分布均匀**（19.34%–20.63%）。营销推送的值是 `promotion`
> （**不是** `marketing`）、交易推送是 `order`（**不是** `transactional`）——旧文档两个都写错了，
> 还漏了 `social` 和 `system`。

### 状态判断逻辑
| 场景 | 判断条件 | 实测命中 |
|------|----------|------|
| 发送成功 | `sent_at IS NOT NULL AND is_delivered = true` | 3,757,726（87.99%） |
| 用户打开 | `is_opened = true` | 902,420（21.13%） |
| 发送失败 | `failure_reason IS NOT NULL` | 512,974（12.01%） |
| 待发送 | `scheduled_at IS NOT NULL AND sent_at IS NULL` | **0 行**，见下 |

> ⚠️ **「待发送」这一行在这份数据上恒为空集。** `scheduled_at` 和 `sent_at` 两列都是
> 100% 非空，而且**逐行相等**（4,270,700 行全部相等）——每条推送都是「计划即发出」。
> 后果有两条：一是没有待发送的推送，二是「计划到实发的延迟」
> （`date_diff('second', scheduled_at, sent_at)`）**恒为 0**，那不是「投递很快」，
> 是这两列没有独立信息。被问到发送时效时要说明这一点，别把 0 秒当成一个业务结论。
> 这是生成器的已知形态，2026-09-01 重灌后依旧如此。

### failure_reason 失败原因

| 值 | 说明 | 实测行数 | 占失败 |
|----|------|------|------|
| token 失效 | 设备推送令牌失效（中文，注意中间有个空格） | 215,687 | 42.05% |
| 设备已卸载应用 | 应用已被卸载 | 102,543 | 19.99% |
| 用户关闭了通知权限 | 系统级通知权限被关 | 76,939 | 15.00% |
| 通道限流 | 厂商通道限流 | 51,282 | 9.99% |
| 网络超时 | 下发超时 | 40,968 | 7.99% |
| 厂商通道返回未知错误 | 通道返回未识别的错误码 | 25,555 | 4.98% |

> 6 个值，合计 512,974 行，其余 87.99% 的行是 NULL。**占比的分母是失败推送，不是全部
> 推送**：上面那一栏按前者算，失败率（512,974 / 4,270,700 = 12.01%）按后者算，两个数
> 别混。旧文档列过 `device_unregistered` / `token_expired` / `user_opted_out` /
> `rate_limited` / `network_error` 这五个英文值，数据里一个都没有——按它们过滤是空集。
>
> ⚠️ 这一列跟 `is_delivered` 是**互补的同一件事**：`failure_reason IS NOT NULL` 的
> 512,974 行正是 `is_delivered = false` 的那 512,974 行，两个判据等价，别当两个指标各算一遍。
>
> 这一栏两次改过。2026-09-01 之前整列 NULL，卡片写着「按 `failure_reason IS NOT NULL`
> 判失败恒为 0 行、算不出推送失败率」；09-01 到 09-02 之间它是**单一假值**——512,974 行
> 全是「token 失效」，失败率算得出来但分布只有一个桶。现在是 6 个值，失败率没变。

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
WHERE sent_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
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
  AND sent_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
GROUP BY failure_reason
ORDER BY failure_count DESC;
```
