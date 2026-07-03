# APP Analytics Demo - Database Schema

## 概览

这是一个模拟 APP 运营分析场景的数据仓库，包含 **35 张表**，分为 **8 个业务域**。

## 业务域结构

```
app-analytics-demo/database/
├── 01_user_domain.sql        # 用户域 (5 tables)
├── 02_behavior_domain.sql    # 行为域 (4 tables)
├── 03_attribution_domain.sql # 渠道归因域 (5 tables)
├── 04_social_domain.sql      # 社交互动域 (6 tables)
├── 05_marketing_domain.sql   # 运营域 (5 tables)
├── 06_experiment_domain.sql  # 实验域 (3 tables)
├── 07_transaction_domain.sql # 交易域 (4 tables)
└── 08_product_domain.sql     # 商品域 (3 tables)
```

## 表清单

### 1. 用户域 (User Domain) - 5 tables
| 表名 | 说明 |
|------|------|
| `users` | 用户主表 |
| `user_profiles` | 用户画像属性 |
| `user_devices` | 设备信息 |
| `user_segments` | 用户分群定义 |
| `user_segment_members` | 分群成员关系 |

### 2. 行为域 (Behavior Domain) - 4 tables
| 表名 | 说明 |
|------|------|
| `event_definitions` | 事件元数据/数据字典 |
| `events` | 行为事件流（核心大表） |
| `sessions` | 会话聚合 |
| `page_views` | 页面浏览 |

### 3. 渠道归因域 (Attribution Domain) - 5 tables
| 表名 | 说明 |
|------|------|
| `channels` | 渠道定义 |
| `ad_campaigns` | 广告投放活动 |
| `ad_creatives` | 广告素材 |
| `user_attributions` | 用户归因记录 |
| `channel_daily_costs` | 渠道每日成本 |

### 4. 社交互动域 (Social Domain) - 6 tables
| 表名 | 说明 |
|------|------|
| `user_follows` | 关注关系 |
| `posts` | 用户发布内容(UGC) |
| `post_likes` | 点赞 |
| `post_comments` | 评论 |
| `post_shares` | 分享 |
| `user_messages` | 私信 |

### 5. 运营域 (Marketing Domain) - 5 tables
| 表名 | 说明 |
|------|------|
| `campaigns` | 运营活动 |
| `push_notifications` | 推送记录 |
| `coupons` | 优惠券定义 |
| `user_coupons` | 用户领券记录 |
| `banners` | Banner/资源位配置 |

### 6. 实验域 (Experiment Domain) - 3 tables
| 表名 | 说明 |
|------|------|
| `ab_tests` | A/B 测试配置 |
| `ab_test_variants` | 实验分支 |
| `ab_test_assignments` | 用户分组 |

### 7. 交易域 (Transaction Domain) - 4 tables
| 表名 | 说明 |
|------|------|
| `orders` | 订单主表 |
| `order_items` | 订单明细 |
| `payments` | 支付记录 |
| `subscriptions` | 会员订阅 |

### 8. 商品域 (Product Domain) - 3 tables
| 表名 | 说明 |
|------|------|
| `categories` | 分类（支持多级） |
| `products` | 商品/内容 |
| `product_tags` | 商品标签 |

## 核心表关系

```
users ──┬── user_profiles
        ├── user_devices
        ├── user_attributions ── channels ── ad_campaigns ── ad_creatives
        │                                 └── channel_daily_costs
        ├── events ── event_definitions
        ├── sessions
        ├── page_views
        ├── user_follows (self-referencing)
        ├── posts ──┬── post_likes
        │           ├── post_comments
        │           └── post_shares
        ├── user_messages
        ├── orders ──┬── order_items ── products ── categories
        │            └── payments           └── product_tags
        ├── subscriptions
        ├── user_coupons ── coupons
        ├── push_notifications ── campaigns
        ├── ab_test_assignments ── ab_test_variants ── ab_tests
        └── user_segment_members ── user_segments
```

## 典型分析场景

| 场景 | 涉及的表 |
|------|---------|
| DAU/MAU/留存 | users, sessions |
| 用户漏斗分析 | events, event_definitions |
| 渠道 ROI | user_attributions, channels, channel_daily_costs, orders |
| A/B 测试分析 | ab_tests, ab_test_variants, ab_test_assignments + 业务指标表 |
| 内容互动分析 | posts, post_likes, post_comments, post_shares |
| KOL 带货效果 | user_follows, posts, orders, order_items |
| 优惠券核销 | coupons, user_coupons, orders |
| 推送效果 | campaigns, push_notifications, events |
| 用户 LTV | users, orders, payments |
| 商品销售分析 | products, categories, order_items, orders |
