# orders - 订单主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | BIGINT | 主键，订单唯一标识 |
| order_no | VARCHAR(50) | 订单编号（业务唯一标识） |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| status | VARCHAR(20) | 订单状态 |
| total_amount | DECIMAL(12,2) | 商品总金额 |
| discount_amount | DECIMAL(10,2) | 优惠减免金额 |
| shipping_fee | DECIMAL(8,2) | 运费 |
| actual_amount | DECIMAL(12,2) | 实付金额 |
| item_count | INT | 商品件数 |
| coupon_id | INT | 使用的优惠券ID |
| shipping_address | `string` | 收货地址（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。**种子数据里整列为 NULL** |
| remark | TEXT | 订单备注 |
| placed_at | TIMESTAMP | 下单时间 |
| paid_at | TIMESTAMP | 支付时间 |
| shipped_at | TIMESTAMP | 发货时间 |
| delivered_at | TIMESTAMP | 签收时间 |
| cancelled_at | TIMESTAMP | 取消时间 |
| cancel_reason | VARCHAR(200) | 取消原因 |
| refunded_at | TIMESTAMP | 退款时间 |
| refund_reason | VARCHAR(200) | 退款原因 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### status 订单状态

状态流转图：
```
pending → paid → shipped → delivered
    ↓        ↓       ↓         ↓
cancelled  refunded  refunded  refunded
```

| 值 | 说明 | 描述 |
|----|------|------|
| pending | 待支付 | 下单未付款 |
| paid | 已支付 | 等待发货 |
| shipped | 已发货 | 运输中 |
| delivered | 已签收 | 交易完成 |
| cancelled | 已取消 | 未支付时取消 |
| refunded | 已退款 | 支付后退款 |

### shipping_address 地址结构

每行三个字段，`count(DISTINCT shipping_address) = 10`：

```json
{"province": "广东省", "city": "深圳市", "district": "南山区"}
```

这一列的 Athena 类型是 `varchar`，装的是 JSON **文本**，不是结构化类型。所以按地域筛要
走字符串抽取，不能当成字段访问：

```sql
-- 按省聚合
SELECT json_extract_scalar(shipping_address, '$.province') AS province,
       sum(actual_amount) AS gmv
FROM orders GROUP BY 1 ORDER BY 2 DESC;

-- 按城市筛。整串相等（`shipping_address = '{"province": …}'`）也可以，
-- 但键序和空格得逐字一致，见下表第一列。
SELECT count(*) FROM orders
WHERE json_extract_scalar(shipping_address, '$.city') = '深圳市';
```

10 个取值的实测分布。**第一列是列里的原文**，整串比较时要照抄——键序固定为
province / city / district，冒号后有一个空格：

| 取值（`shipping_address` 原文） | 省 / 市 / 区 | 行数 |
|------|------|------|
| `{"province": "广东省", "city": "深圳市", "district": "南山区"}` | 广东省 深圳市 南山区 | 136,509 |
| `{"province": "上海市", "city": "上海市", "district": "浦东新区"}` | 上海市 上海市 浦东新区 | 111,371 |
| `{"province": "广东省", "city": "广州市", "district": "天河区"}` | 广东省 广州市 天河区 | 110,722 |
| `{"province": "北京市", "city": "北京市", "district": "朝阳区"}` | 北京市 北京市 朝阳区 | 102,813 |
| `{"province": "浙江省", "city": "杭州市", "district": "西湖区"}` | 浙江省 杭州市 西湖区 | 93,306 |
| `{"province": "江苏省", "city": "南京市", "district": "鼓楼区"}` | 江苏省 南京市 鼓楼区 | 76,835 |
| `{"province": "四川省", "city": "成都市", "district": "武侯区"}` | 四川省 成都市 武侯区 | 68,338 |
| `{"province": "湖北省", "city": "武汉市", "district": "洪山区"}` | 湖北省 武汉市 洪山区 | 59,907 |
| `{"province": "陕西省", "city": "西安市", "district": "雁塔区"}` | 陕西省 西安市 雁塔区 | 51,433 |
| `{"province": "福建省", "city": "厦门市", "district": "思明区"}` | 福建省 厦门市 思明区 | 42,906 |

> ⚠️ **只有 10 个地址，不是连续的地理分布。** 「各省 GMV 分布」按它算得到 8 个省级桶
> （上海、北京两个直辖市的 province 与 city 同名），这是真结论；但**别把它当成全国
> 覆盖**——一个省只有一个城市、一个城市只有一个区，「城市下钻」到区一级就只剩一行。
> 用户侧的地域维度另有一套：`user_profiles.province` / `city` 是逐行不同的，
> 两者**不保证一致**（收货地和常住地本来就可以不同），别混着 JOIN 当同一个维度用。
>
> 省名带「省」「市」后缀（`广东省` 而不是 `广东`），
> `WHERE json_extract_scalar(shipping_address, '$.province') = '广东'` 返回 0 行。
>
> 这一列**没有** `receiver_name` / `phone` / `address` / `postal_code`，旧文档列过这几个
> 字段，取它们得到 NULL。这也是一条设计边界，将来也不会放收件人姓名和手机号：L4 治理
> 把 `users.phone` 排除在授权之外，而这张表的地址列是授权可读的，把同一个手机号抄进来
> 等于绕开列级排除。
>
> 这一栏 2026-09-02 改过：在此之前 854,140 行全是同一个 `{"province": "广东", "city":
> "深圳"}`（两个字段、无 `district`），那时「各省 GMV 分布」恒得一行。

### cancel_reason 取消原因

| 值 | 实测行数 | 占取消单 |
|----|------|------|
| 用户取消 | 21,054 | 27.90% |
| 超时未支付 | 15,103 | 20.01% |
| 不想要了 | 10,727 | 14.21% |
| 地址填错了 | 7,552 | 10.01% |
| 拍错了重新下单 | 6,771 | 8.97% |
| 价格比别处贵 | 6,037 | 8.00% |
| 缺货商家取消 | 4,535 | 6.01% |
| 支付失败 | 3,692 | 4.89% |

### refund_reason 退款原因

| 值 | 实测行数 | 占退款单 |
|----|------|------|
| 商品问题 | 10,015 | 23.85% |
| 尺码不合适 | 7,570 | 18.03% |
| 与描述不符 | 6,350 | 15.12% |
| 签收时已破损 | 4,995 | 11.89% |
| 物流太慢不想要了 | 4,706 | 11.21% |
| 买重复了 | 3,353 | 7.98% |
| 商家发错货 | 2,953 | 7.03% |
| 无理由退货 | 2,053 | 4.89% |

> 两列各 8 个值，`NULL` 的行是没取消 / 没退款的单。取消率 75,471 / 854,140 = 8.84%，
> 退款率 41,995 / 854,140 = 4.92%——**原因分布的分母是取消单 / 退款单，不是全部订单**，
> 上面两张表的占比按前者算。
>
> ⚠️ 两列的非空条件严格等于对应的 `status`（`cancelled` / `refunded`），所以
> 「有取消原因的单」和「已取消的单」是同一批，别当两个判据各算一遍。
>
> 这两栏 2026-09-02 改过：在此之前 `cancel_reason` 75,471 行全是「用户取消」、
> `refund_reason` 41,995 行全是「商品问题」，「原因 TOP5」只有一个桶。

### 金额计算

```
actual_amount = total_amount - discount_amount + shipping_fee
```

## 索引

- PRIMARY KEY: `order_id`
- UNIQUE: `order_no`
- INDEX: `user_id`, `status`, `placed_at`, `paid_at`

## 常用查询

### 每日 GMV 和订单量
```sql
SELECT
    DATE(placed_at) AS order_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN status NOT IN ('cancelled') THEN 1 END) AS valid_orders,
    SUM(total_amount) AS gmv,
    SUM(CASE WHEN status IN ('paid', 'shipped', 'delivered')
        THEN actual_amount ELSE 0 END) AS revenue,
    SUM(discount_amount) AS total_discount,
    ROUND(AVG(CASE WHEN status NOT IN ('cancelled')
        THEN actual_amount END), 2) AS avg_order_value
FROM orders
WHERE placed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(placed_at)
ORDER BY order_date DESC;
```

### 订单状态分布
```sql
SELECT
    status,
    COUNT(*) AS order_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct,
    SUM(actual_amount) AS total_amount
FROM orders
WHERE placed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY status
ORDER BY order_count DESC;
```

### 订单取消/退款分析
```sql
SELECT
    DATE(placed_at) AS order_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN status = 'cancelled' THEN 1 END) AS cancelled,
    COUNT(CASE WHEN status = 'refunded' THEN 1 END) AS refunded,
    ROUND(COUNT(CASE WHEN status = 'cancelled' THEN 1 END) * 100.0 / COUNT(*), 2) AS cancel_rate,
    ROUND(COUNT(CASE WHEN status = 'refunded' THEN 1 END) * 100.0 / COUNT(*), 2) AS refund_rate
FROM orders
WHERE placed_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(placed_at)
ORDER BY order_date DESC;
```
