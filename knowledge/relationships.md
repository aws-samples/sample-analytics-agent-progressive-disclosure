# 跨域关系说明

## 核心关联图

```
┌─────────────┐
│   users     │◄─────────────────────────────────────────┐
└──────┬──────┘                                          │
       │ user_id                                         │
       ▼                                                 │
┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│user_profiles │    │user_devices  │    │user_tags     │ │
└──────────────┘    └──────────────┘    └──────────────┘ │
                                                         │
       ┌─────────────────────────────────────────────────┤
       │                                                 │
       ▼                                                 │
┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│   orders     │───►│ order_items  │───►│  products    │ │
└──────────────┘    └──────────────┘    └──────────────┘ │
       │                                       ▲         │
       │                                       │         │
       ▼                                       │         │
┌──────────────┐                               │         │
│  payments    │                               │         │
└──────────────┘                               │         │
                                               │         │
┌──────────────┐    ┌──────────────┐           │         │
│    posts     │───►│ post_likes   │           │         │
│              │───►│ post_comments│           │         │
│              │───►│ post_shares  │           │         │
│   (UGC)      │────┼──────────────┼───────────┘         │
└──────────────┘    │              │                     │
       │            └──────────────┘                     │
       │                                                 │
       └─────────────────────────────────────────────────┘
```

## 主要外键关系

### 用户域 → 其他域

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| users | user_profiles | user_id | 1:1 用户详细信息 |
| users | user_devices | user_id | 1:N 用户设备 |
| users | user_tags | user_id | 1:N 用户标签 |
| users | user_levels | user_id | 1:1 用户等级 |
| users | orders | user_id | 1:N 用户订单 |
| users | posts | user_id | 1:N 用户发布的内容 |
| users | page_views | user_id | 1:N 用户页面浏览 |
| users | app_events | user_id | 1:N 用户事件 |

### 商品域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| products | product_categories | category_id | N:1 商品分类 |
| products | product_skus | product_id | 1:N 商品SKU |
| products | order_items | product_id | 1:N 订单商品 |
| products | posts | product_ids[] | N:N 关联内容 |

### 交易域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| orders | order_items | order_id | 1:N 订单明细 |
| orders | payments | order_id | 1:N 支付记录 |
| order_items | products | product_id | N:1 商品信息 |
| order_items | product_skus | sku_id | N:1 SKU信息 |

### 社交域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| posts | post_likes | post_id | 1:N 点赞 |
| posts | post_comments | post_id | 1:N 评论 |
| posts | post_shares | post_id | 1:N 分享 |
| user_follows | users | follower_id, following_id | N:N 关注关系 |
| user_messages | users | sender_id, receiver_id | N:N 私信 |

### 渠道归因域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| channel_tracking | users | user_id | N:1 渠道用户 |
| user_attributions | users | user_id | N:1 归因用户 |
| ad_campaigns | ad_creatives | campaign_id | 1:N 广告素材 |

### 运营域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| campaign_users | users | user_id | N:1 活动用户 |
| campaign_users | campaigns | campaign_id | N:1 活动 |
| coupons | users | user_id | N:1 优惠券用户 |
| push_notifications | users | user_id | N:1 推送用户 |

### 实验域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| experiment_users | users | user_id | N:1 实验用户 |
| experiment_users | experiments | experiment_id | N:1 实验 |
| experiment_results | experiments | experiment_id | 1:1 实验结果 |

## 常用 JOIN 模式

### 用户完整画像
```sql
SELECT u.*, up.*, ul.*
FROM users u
LEFT JOIN user_profiles up ON u.user_id = up.user_id
LEFT JOIN user_levels ul ON u.user_id = ul.user_id
WHERE u.user_id = ?;
```

### 订单完整信息
```sql
SELECT o.*, oi.*, p.product_name, ps.sku_name
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
LEFT JOIN product_skus ps ON oi.sku_id = ps.sku_id
WHERE o.order_id = ?;
```

### 内容互动统计
```sql
SELECT
    p.post_id,
    p.title,
    COUNT(DISTINCT pl.like_id) AS likes,
    COUNT(DISTINCT pc.comment_id) AS comments,
    COUNT(DISTINCT ps.share_id) AS shares
FROM posts p
LEFT JOIN post_likes pl ON p.post_id = pl.post_id
LEFT JOIN post_comments pc ON p.post_id = pc.post_id
LEFT JOIN post_shares ps ON p.post_id = ps.post_id
GROUP BY p.post_id, p.title;
```

### 用户行为归因
```sql
SELECT
    u.user_id,
    ua.channel,
    ua.first_touch_campaign,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(o.total_amount) AS total_gmv
FROM users u
JOIN user_attributions ua ON u.user_id = ua.user_id
LEFT JOIN orders o ON u.user_id = o.user_id
GROUP BY u.user_id, ua.channel, ua.first_touch_campaign;
```

## 注意事项

1. **数组字段关联**: `posts.product_ids` 是数组类型，需要使用 `ANY()` 或 `unnest()` 进行关联
   ```sql
   SELECT p.*, pr.product_name
   FROM posts p, unnest(p.product_ids) AS pid
   JOIN products pr ON pr.product_id = pid;
   ```

2. **软删除**: 部分表有 `status` 或 `is_deleted` 字段，查询时注意过滤

3. **时间字段**: 所有表都有 `created_at`，可用于时间范围筛选
