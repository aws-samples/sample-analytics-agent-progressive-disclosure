# events - 事件明细表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| event_id | BIGINT | 主键，事件ID |
| user_id | BIGINT | 用户ID |
| device_id | VARCHAR(64) | 设备ID |
| session_id | BIGINT | 关联 sessions.session_id |
| event_name | VARCHAR(50) | 事件名称 |
| event_time | TIMESTAMP | 事件发生时间 |
| properties | `string` | 事件属性（JSON 文本）。**Iceberg 里是 string，不是 Postgres 的 JSONB**：取值用 `json_extract_scalar(properties, '$.k')`，`->` / `->>` 是语法错 |
| page_name | VARCHAR(100) | 事件发生页面 |
| referrer | VARCHAR(500) | 来源 |
| ip_address | VARCHAR(45) | IP地址 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### event_name 事件名称（全 25 种，实测）
| 值 | 说明 | 所属分类 | 实测行数 |
|----|------|----------|------|
| begin_checkout | 发起结算 | conversion | 866 |
| register | 用户注册 | acquisition | 843 |
| add_to_cart | 加入购物车 | conversion | 838 |
| view_profile | 查看个人主页 | engagement | 825 |
| view_post | 查看帖子 | engagement | 824 |
| view_product | 商品浏览 | engagement | 823 |
| receive_push | 收到推送 | engagement | 822 |
| share | 分享 | retention | 820 |
| view_home | 浏览首页 | engagement | 807 |
| use_coupon | 使用优惠券 | conversion | 804 |
| search | 搜索 | engagement | 804 |
| app_close | APP 关闭 | retention | 804 |
| comment_post | 评论帖子 | social | 803 |
| remove_from_cart | 移出购物车 | engagement | 800 |
| like_post | 点赞帖子 | social | 796 |
| add_favorite | 加收藏 | engagement | 796 |
| edit_profile | 编辑资料 | engagement | 791 |
| click_banner | 点击 banner | engagement | 788 |
| logout | 用户登出 | retention | 784 |
| login | 用户登录 | retention | 784 |
| follow_user | 关注用户 | social | 780 |
| purchase | 完成购买 | conversion | 772 |
| app_open | APP 打开 | retention | 752 |
| view_category | 浏览分类页 | engagement | 738 |
| click_push | 点击推送 | engagement | 736 |

> ⚠️ **旧文档里这几个名字数据里根本不存在**，照着写 WHERE 就是空集：
> `product_view`（真名 `view_product`）、`checkout`（真名 `begin_checkout`）、
> `registration`（真名 `register`）、`app_install`、`first_open`、`button_click`。
>
> 全表 20,000 行、25 种事件，**分布几乎是均匀的**（736–866，最高/最低 = 1.18）。
>
> ⚠️ **上面这张是「每种事件各数一遍」的行数表，它不是漏斗，也不能用来判断漏斗衰不衰减。**
> 漏斗要求第 i+1 步的用户集是第 i 步的**子集**（见下面「购买漏斗转化分析」的写法）。
> 把每步各数一遍排在一起，会得到 `394/385/396/373` 这种「结算人数比浏览人数还多」的
> 东西——那只说明口径错了，**不说明数据没有漏斗形态**。同一份数据按子集口径算是
> `394 → 314 → 258 → 199`（全量去重用户），逐层流失 20%/18%/23%，单调递减、很正常。
> 所以**不要**写「这份种子数据均匀分布，所以转化率不衰减／别当业务结论」——
> 这句话本身就是把自己的口径错误归因给了数据。
>
> `purchase` 事件数(772)**不等于**有效订单数(1,601)，两者是各自独立生成的，
> 跨表核对请用 `dwd_orders_valid`。

### properties 事件属性（实测形状）

⚠️ **25 种事件里只有 4 种带属性，其余 21 种全是 `{}`**（空 JSON，不是 NULL）。
所以「按属性下钻」这条路在这份数据上只有下面 4 个事件走得通：

**view_product 商品浏览事件**（823 行）
```json
{"product_id": 143, "product_name": "经典戴森 洗衣机"}
```

**add_to_cart 加购事件**（838 行）
```json
{"quantity": 1, "product_id": 143}
```

**purchase 购买事件**（772 行）
```json
{"amount": 1820.29, "order_id": 43811}
```

**search 搜索事件**（804 行）
```json
{"keyword": "手机"}
```

只有 `keyword` 一个键，**没有** `result_count` / `filter_applied`（旧文档写过）——
按它们取值会得到整列 NULL，`AVG()` 出来是 NULL 而不是 0。

> ⚠️ **`purchase.properties.order_id` 接不上 `orders.order_id`**：772 个 purchase 事件
> 按这个键去 JOIN `orders`，**匹配到 0 行**。这一列是独立生成的小整数（例如 43811），
> 而 `orders.order_id` 是 12 位数（例如 105575697563）。这不是声明的外键，DDL 里也没约束，
> 所以查询不报错、只是静默返回空——**要把行为和交易关联起来，走 `user_id`，不要走这个 order_id**。

其余 21 种事件（`app_open` / `login` / `click_banner` / `like_post` / `use_coupon` …）
的 `properties` 都是 `{}`：能算触发次数和触发人数，但没有可下钻的维度。

## 索引

- PRIMARY KEY: `event_id`
- INDEX: `user_id`, `session_id`, `event_time`
- INDEX: `event_name`
- ~~GIN INDEX: `properties`~~ —— Postgres 时代的索引。**Iceberg 表没有二级索引**，对
  `properties` 的过滤一律是全扫；上面几条同理，它们记的是 v1 的建表意图，不是 Athena
  上真实存在的结构（Iceberg 靠分区和文件级统计裁剪）

## 常用查询

### 购买漏斗转化分析

**写漏斗前先看这条硬约束**：第 i+1 步只保留「**也做过**第 i 步」的用户
（`AND user_id IN (上一步)`）。少了它，各步就是四个互不相干的集合，
`begin_checkout` 完全可能大于 `view_product`。完整方法、公式、口径声明要求见
`analysis/funnel_analysis.md`——**答漏斗题请连它一起读**。

```sql
-- 事件名用 view_product / begin_checkout，不是 product_view / checkout（见上面枚举表）；
-- 时间窗锚在 meta_snapshot.as_of_date 上，不用 CURRENT_DATE（这是静态快照）。
-- 问句没点明时间范围 → 按全量算：把 w 里那两行时间条件整段删掉即可。
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
w AS (
    SELECT user_id, event_name
    FROM events, a
    WHERE CAST(event_time AS date) > a.d - interval '30' day
      AND CAST(event_time AS date) <= a.d
      AND event_name IN ('view_product', 'add_to_cart', 'begin_checkout', 'purchase')
),
s1 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'view_product'),
s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'add_to_cart'
       AND user_id IN (SELECT user_id FROM s1)),
s3 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'begin_checkout'
       AND user_id IN (SELECT user_id FROM s2)),
s4 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'purchase'
       AND user_id IN (SELECT user_id FROM s3))
SELECT (SELECT count(*) FROM s1) AS step1_view,
       (SELECT count(*) FROM s2) AS step2_cart,
       (SELECT count(*) FROM s3) AS step3_checkout,
       (SELECT count(*) FROM s4) AS step4_purchase
```

实测：全量 `394 → 314 → 258 → 199`（整体 50.5%），近 30 天 `197 → 101 → 58 → 32`。
两个都是合法口径，**必须在 method 里写明算的是哪一个**。
要按天/渠道切分，把维度带进 `w` 并**在每个维度内部**保持子集约束——
不要先 `GROUP BY date` 再各步分别计数，那样又退回独立计数了。

### 事件触发量趋势
```sql
SELECT
    DATE(event_time) AS date,
    event_name,
    COUNT(*) AS event_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM events
WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
    AND event_name IN ('view_product', 'add_to_cart', 'purchase')
GROUP BY DATE(event_time), event_name
ORDER BY date DESC, event_count DESC;
```

### 搜索关键词 TOP 10
```sql
-- properties 里只有 keyword，没有 result_count（取它会得到整列 NULL）
SELECT
    json_extract_scalar(properties, '$.keyword') AS keyword,
    COUNT(*) AS search_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM events
WHERE event_name = 'search'
    AND event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
    AND json_extract_scalar(properties, '$.keyword') IS NOT NULL
GROUP BY json_extract_scalar(properties, '$.keyword')
ORDER BY search_count DESC
LIMIT 10;
```
