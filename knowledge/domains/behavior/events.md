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

> ⚠️ **`session_id` 只对 22 种"泛化"事件成立；三种保底事件的 `session_id` 是错的。**
> `purchase`（647,395）/ `use_coupon`（324,620）/ `register`（213,535）这 118.6 万行
> 挂的是该用户的**首个会话**，而 `event_time` 取的是订单时刻或注册时刻，
> 两者可以差到 **90 天**。实测（2026-09-01，全表 854.1 万行）：
> **118.4 万行（13.9%）的 `event_time` 落在自己 `session_id` 的
> `[start_time, start_time+duration]` 窗口外**，108.6 万行（12.7%）甚至不在同一天。
>
> 由此产生两条纪律：
> - **不要按 `session_id` 归因这三种事件**（"哪个渠道的会话促成了下单"这类问题，
>   走 `orders` / `user_attributions`，别走 `events.session_id`）。会话级的
>   `event_count` 本身是从这份归属回填的，所以计数对得上、时间对不上。
> - **按天做行为分析时，`events` 的活跃天数比 `sessions` 多**：窗首那批用户人均
>   事件天数 14.5 天 vs 会话天数 5.1 天，因为订单铺满了整段在库时长。留存题受这条
>   影响最大，见 `analysis/retention_curve.md` 顶部那张两口径对照表。
>
> `page_views` 没有这个问题（全部挂在自己会话的窗口内，L5 `pv_in_session` 判据
> 通过线是 0 行）。同一条性质在 `events` 侧**当前没有判据**——这是已登记的缺口，
> 清除条件是下一次全量重灌。

## 字段枚举值

### event_name 事件名称（全 25 种，实测）
| 值 | 说明 | 所属分类 | 实测行数 | 占比 |
|----|------|----------|------|------|
| view_home | 浏览首页 | engagement | 1,393,869 | 16.32% |
| view_product | 商品浏览 | engagement | 1,140,840 | 13.36% |
| add_to_cart | 加入购物车 | conversion | 928,350 | 10.87% |
| app_open | APP 打开 | retention | 857,348 | 10.04% |
| begin_checkout | 发起结算 | conversion | 750,188 | 8.78% |
| purchase | 完成购买 | conversion | 647,395 | 7.58% |
| app_close | APP 关闭 | retention | 608,208 | 7.12% |
| use_coupon | 使用优惠券 | conversion | 324,620 | 3.80% |
| view_post | 查看帖子 | engagement | 285,779 | 3.35% |
| login | 用户登录 | retention | 250,211 | 2.93% |
| register | 用户注册 | acquisition | 213,535 | 2.50% |
| search | 搜索 | engagement | 213,505 | 2.50% |
| view_category | 浏览分类页 | engagement | 185,040 | 2.17% |
| logout | 用户登出 | retention | 157,428 | 1.84% |
| like_post | 点赞帖子 | social | 142,833 | 1.67% |
| receive_push | 收到推送 | engagement | 113,872 | 1.33% |
| add_favorite | 加收藏 | engagement | 71,004 | 0.83% |
| click_banner | 点击 banner | engagement | 57,327 | 0.67% |
| remove_from_cart | 移出购物车 | engagement | 49,700 | 0.58% |
| comment_post | 评论帖子 | social | 42,929 | 0.50% |
| view_profile | 查看个人主页 | engagement | 35,742 | 0.42% |
| share | 分享 | retention | 28,765 | 0.34% |
| follow_user | 关注用户 | social | 21,610 | 0.25% |
| click_push | 点击推送 | engagement | 14,248 | 0.17% |
| edit_profile | 编辑资料 | engagement | 7,054 | 0.08% |

> ⚠️ **旧文档里这几个名字数据里根本不存在**，照着写 WHERE 就是空集：
> `product_view`（真名 `view_product`）、`checkout`（真名 `begin_checkout`）、
> `registration`（真名 `register`）、`app_install`、`first_open`、`button_click`。
>
> 全表 8,541,400 行、25 种事件，**分布是漏斗形的**：头部 `view_home` 16.32%，
> 尾部 `edit_profile` 0.08%，最高/最低 = 197.6 倍。上面的行数是 2026-09-01 那一版
> 全量数据的实测值；重灌会等比例变化，**占比那一列才是稳定的**，写结论时优先引占比。
>
> 有三种事件的行数是**按定义等于另一张表的**，可以拿来做跨表自检：
> `register` = `users` 行数（213,535，每个用户恰好一条）、
> `purchase` = 有效订单数（647,395，`orders.status IN ('paid','shipped','delivered')`）、
> `use_coupon` = `user_coupons` 里 `status = 'used'` 的行数（324,620）。
> 这三条对不上就是装载漏了分区，不是业务波动。
>
> ⚠️ **上面这张是「每种事件各数一遍」的行数表，它不是漏斗，也不能用来判断漏斗衰不衰减。**
> 漏斗要求第 i+1 步的用户集是第 i 步的**子集**（见下面「购买漏斗转化分析」的写法）。
> 按「每步各数一遍」的去重人数排出来是 `92,800 / 87,737 / 82,447 / 98,745`（近 30 天），
> 这种「购买人数比浏览人数还多」的东西只说明口径错了，**不说明数据没有漏斗形态**。
> 同一个窗口按子集口径算是 `92,800 → 75,676 → 63,781 → 24,052`，逐层留存
> 81.5%/84.3%/37.7%，单调递减、很正常。所以**不要**写「这份种子数据分布均匀，
> 所以转化率不衰减／别当业务结论」——这句话本身就是把自己的口径错误归因给了数据。

### properties 事件属性（实测形状）

⚠️ **25 种事件里只有 4 种带属性，其余 21 种全是 `{}`**（空 JSON，不是 NULL）。
所以「按属性下钻」这条路在这份数据上只有下面 4 个事件走得通：

**view_product 商品浏览事件**（1,140,840 行）
```json
{"product_id": 1822, "product_name": "富安娜 毛巾 双人款"}
```

`product_id` 接得回 `products`，`product_name` 就是该 `product_id` 在 `products` 里的名字，
所以「按属性里的名字分组」和「JOIN `products` 再分组」给出同一个答案，用哪个都行
（属性里的那个省一次 JOIN）。

**add_to_cart 加购事件**（928,350 行）
```json
{"quantity": 2, "product_id": 3089}
```

`quantity` 只有 1 / 2 / 3 三个值，分布基本均匀。`product_id` 同样接得回 `products`，
但这里**没有** `product_name`——要商品名得自己 JOIN。

**purchase 购买事件**（647,395 行）
```json
{"amount": 1236.58, "order_id": 2}
```

**search 搜索事件**（213,505 行）
```json
{"keyword": "海鲜"}
```

只有 `keyword` 一个键，**没有** `result_count` / `filter_applied`（旧文档写过）——
按它们取值会得到整列 NULL，`AVG()` 出来是 NULL 而不是 0。
词池就是 `categories` 的 120 个叶子类目名（不是自由文本），213,505 次搜索把 120 个词
全都覆盖到了，热度是长尾：头部 `腮红` 3.84%、第十位 `燃气灶` 1.99%、头尾差约 13 倍。
所以「搜索词 TOP N 里哪些类目缺货」这类问题可以直接把 `keyword` 和
`categories.category_name` 对上，不会出现站内不存在的词。

> ✅ **`purchase.properties.order_id` 是接得上 `orders.order_id` 的真外键**：647,395 个
> purchase 事件的单号互不重复，逐个命中 `orders` 里状态为 `paid`/`shipped`/`delivered`
> 的那 647,395 单，一一对应、无落空。并且 `properties.amount` 逐条等于该单的
> `orders.actual_amount`（精确到分），所以「按事件属性求 GMV」和「按 `orders` 求 GMV」
> 结果相同（全量 151,333,127.30）。要把行为和交易关联起来，这个键和 `user_id` 都能走；
> 需要单粒度（订单金额、下单商品）就走 `order_id`。
>
> 这一条 2026-09-01 反过来了：在此之前 `order_id` 是一列独立生成的随机数，JOIN 匹配 0 行，
> 卡片这里原本写着「接不上、请走 user_id」。旧结论现在是错的。

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

实测：全量 `177,644 → 151,924 → 131,677 → 77,681`（整体 43.7%），
近 30 天 `92,800 → 75,676 → 63,781 → 24,052`（整体 25.9%）。
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
