# 跨域关系说明

## 核心关联图

```
┌─────────────┐
│   users     │◄─────────────────────────────────────────┐
└──────┬──────┘                                          │
       │ user_id                                         │
       ▼                                                 │
┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│user_profiles │    │user_devices  │    │sessions      │ │
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

> 本节的表名与字段名都对着 `database/iceberg/01_tables.sql` 核过一遍。**这一节曾经列过
> 10 张本库不存在的表**（`user_tags` / `user_levels` / `app_events` / `product_categories` /
> `product_skus` / `channel_tracking` / `campaign_users` / `experiment_users` /
> `experiments` / `experiment_results`），照着写的 JOIN 会直接 `TABLE_NOT_FOUND`。
> 每一小节末尾都留了「不存在的表 → 真正该用什么」，因为改对了看不出改过，
> 而下一个人还会去猜同样的名字。

### 用户域 → 其他域

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| users | user_profiles | user_id | 1:1 用户详细信息 |
| users | user_devices | user_id | 1:N 用户设备 |
| users | sessions | user_id | 1:N 会话 |
| users | user_segment_members | user_id | 1:N 分群成员（分群定义在 `user_segments`） |
| users | orders | user_id | 1:N 用户订单 |
| users | subscriptions | user_id | 1:N 订阅 |
| users | posts | user_id | 1:N 用户发布的内容 |
| users | page_views | user_id | 1:N 用户页面浏览 |
| users | events | user_id | 1:N 用户事件 |

- ❌ `user_tags`：不存在。用户侧的分群走 `user_segments` / `user_segment_members`；
  `product_tags` 是**商品**标签，别误用。
- ❌ `user_levels`：不存在。等级是 `users.user_level` 这一**列**，不是表。
- ❌ `app_events`：不存在，表名就叫 `events`（派生层另有 `dwd_events_app`）。

### 商品域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| products | categories | category_id | N:1 商品分类 |
| products | product_tags | product_id | 1:N 商品标签 |
| products | order_items | product_id | 1:N 订单商品 |
| posts | products | product_ids[] | N:N 内容关联商品（数组，见注意事项 1） |

- ❌ `product_categories`：不存在，分类表叫 `categories`（自关联 `parent_id`）。
- ❌ `product_skus`：不存在。SKU 只以 `order_items.sku_id` / `order_items.sku_name`
  的形态存在，**没有 SKU 维表**；而且 `sku_name` 当前整列 NULL，拿它做展示会全空。

### 交易域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| orders | order_items | order_id | 1:N 订单明细 |
| orders | payments | order_id | 1:N 支付记录 |
| orders | coupons | coupon_id | N:1 订单用掉的券 |
| order_items | products | product_id | N:1 商品信息 |
| subscriptions | payments | payment_id | N:1 订阅支付（该列整列 NULL，join 不出东西） |

### 社交域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| posts | post_likes | post_id | 1:N 点赞 |
| posts | post_comments | post_id | 1:N 评论 |
| posts | post_shares | post_id | 1:N 分享 |
| post_comments | post_comments | parent_comment_id | 自关联，楼中楼 |
| user_follows | users | follower_id, following_id | N:N 关注关系 |
| user_messages | users | sender_id, receiver_id | N:N 私信 |

> ⚠️ `user_messages` 整表**不在 agent 角色的授权面里**（Lake Formation 未授予），
> 以治理身份查它会被拒。这条关系记在这里是为了完整，不是为了让你去查它。

### 渠道归因域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| user_attributions | users | user_id | N:1 归因用户 |
| user_attributions | channels | channel_id | N:1 渠道（渠道名要 join 才有） |
| user_attributions | ad_campaigns | ad_campaign_id | N:1 广告计划（该列整列 NULL） |
| ad_campaigns | channels | channel_id | N:1 计划所属渠道 |
| ad_campaigns | ad_creatives | ad_campaign_id | 1:N 广告素材 |
| channel_daily_costs | channels | channel_id | N:1 渠道日成本 |

- ❌ `channel_tracking`：不存在。渠道归因就是 `user_attributions`
  （一个用户有 `first_touch` / `last_touch` 各一条，不过滤会算重）。
- ⚠️ 字段名是 `ad_campaign_id`，**不是 `campaign_id`**——后者属于运营活动
  `campaigns`，两个域完全不同的东西，写错了 join 不上还不报错（都是 BIGINT）。

### 运营域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| campaigns | user_segments | target_segment_ids[] | N:N 活动受众（数组） |
| user_coupons | users | user_id | N:1 领券用户 |
| user_coupons | coupons | coupon_id | N:1 券定义 |
| user_coupons | orders | order_id | N:1 用券的订单 |
| push_notifications | users | user_id | N:1 推送用户 |
| push_notifications | campaigns | campaign_id | N:1 所属活动 |
| banners | （无外键） | target_id | 目标类型由 `target_type` 决定，无约束 |

- ❌ `campaign_users`：不存在。活动受众是 `campaigns.target_segment_ids` 这个**数组列**，
  指向 `user_segments`；要落到人头得再经 `user_segment_members`。
- ❌ `coupons.user_id`：`coupons` 表**没有这一列**（它是券的定义表）。
  「谁领了券 / 谁用了券」在 `user_coupons`。

### 实验域关联

| 源表 | 目标表 | 关联字段 | 说明 |
|------|--------|----------|------|
| ab_test_assignments | users | user_id | N:1 参与用户 |
| ab_test_assignments | ab_tests | test_id | N:1 所属实验 |
| ab_test_assignments | ab_test_variants | variant_id | N:1 所在分组 |
| ab_test_variants | ab_tests | test_id | N:1 实验的分组定义 |
| ab_tests | user_segments | target_segment_ids[] | N:N 实验受众（数组） |

- ❌ `experiment_users` → 用 `ab_test_assignments`；❌ `experiments` → 用 `ab_tests`；
  ❌ `experiment_results` → **不存在这张表**，实验结论在 `ab_tests.conclusion` 与
  `ab_tests.winner_variant_id`，而这两列当前整列 NULL——「实验结果如何」这类问题
  在本库答不出来，要如实说没有数据，不要拿分组指标硬凑一个结论。

## 常用 JOIN 模式

### 用户完整画像
```sql
-- 等级在 users.user_level 列上（没有 user_levels 表）。
-- email / phone / birth_date 三列在 agent 角色的授权面之外，显式写它们会
-- COLUMN_NOT_FOUND，所以这里只列可读的字段——顺带也符合「只 SELECT 用到的列」。
SELECT u.user_id, u.username, u.user_level, u.is_vip, u.status,
       u.registered_at, u.last_active_at,
       up.age, up.gender, up.city, up.province, up.country,
       up.occupation, up.income_level
FROM users u
LEFT JOIN user_profiles up ON u.user_id = up.user_id
WHERE u.user_id = 1001;
```

### 订单完整信息
```sql
-- 没有 SKU 维表：sku_id / sku_name 就在 order_items 上（sku_name 当前整列 NULL）。
SELECT o.order_id, o.order_no, o.status, o.total_amount, o.actual_amount, o.placed_at,
       oi.item_id, oi.quantity, oi.unit_price, oi.actual_amount AS item_amount,
       oi.sku_id, p.product_name
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
WHERE o.order_id = 1001;
```

### 内容互动统计
```sql
SELECT
    p.post_id,
    p.title,
    -- post_likes 没有代理主键，主键是 (user_id, post_id)，所以按 user_id 去重
    COUNT(DISTINCT pl.user_id) AS likes,
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
-- user_attributions 里只有 channel_id / ad_campaign_id（没有 channel、
-- 也没有 first_touch_campaign）；渠道名要 JOIN channels 取
SELECT
    u.user_id,
    ch.channel_name,
    ua.ad_campaign_id,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(o.total_amount) AS total_gmv
FROM users u
JOIN user_attributions ua ON u.user_id = ua.user_id
JOIN channels ch ON ch.channel_id = ua.channel_id
LEFT JOIN orders o ON u.user_id = o.user_id
-- 一个用户有多条归因记录（first_touch / last_touch 各一条），不过滤会把订单算重
WHERE ua.attribution_type = 'first_touch'
GROUP BY u.user_id, ch.channel_name, ua.ad_campaign_id;
```

## 注意事项

1. **数组字段关联**: `posts.product_ids` 是数组类型，展开用 `CROSS JOIN UNNEST(...)`，
   只判断"包不包含"用 `contains(arr, v)`（Trino 的 `= ANY` 只接子查询，不接数组）
   ```sql
   SELECT p.*, pr.product_name
   FROM posts p
   CROSS JOIN UNNEST(p.product_ids) AS t(pid)
   JOIN products pr ON pr.product_id = t.pid;
   ```

2. **软删除**: 部分表有 `status` 字段，查询时注意过滤。本库**没有** `is_deleted` 列。

3. **时间字段**: `created_at` **不是每张表都有**（48 张里 18 张没有，包括 `orders` /
   `events` / `payments` / `posts` 这些主表以及全部派生层表）。而且即使有，它也多半是
   **灌数那一瞬**，不是业务时间——按时间筛选要用各表的业务时间列：`orders.placed_at`、
   `events.event_time`、`page_views.view_time`、`payments.paid_at`、`posts.published_at`、
   `users.registered_at`、`sessions.start_time`。具体到表看它自己的卡片。

4. **时间锚点**: 「最近 N 天」一律以 `(SELECT max(as_of_date) FROM meta_snapshot)` 为今天，
   禁用 `current_date` / `now()`（在 Trino 里语法合法，只会安静地返回 0 行），也别用
   各表自己的 `max()`。详见 `connection.md` 与 `metrics/governed_metrics.md` §时间锚点。
