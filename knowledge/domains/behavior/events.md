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
| properties | JSONB | 事件属性（JSON格式） |
| page_name | VARCHAR(100) | 事件发生页面 |
| referrer | VARCHAR(500) | 来源 |
| ip_address | VARCHAR(45) | IP地址 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### event_name 常见事件名称
| 值 | 说明 | 所属分类 |
|----|------|----------|
| app_open | APP打开 | retention |
| app_install | APP安装 | acquisition |
| first_open | 首次打开 | acquisition |
| registration | 用户注册 | acquisition |
| login | 用户登录 | retention |
| search | 搜索 | engagement |
| product_view | 商品浏览 | engagement |
| add_to_cart | 加入购物车 | conversion |
| remove_from_cart | 移出购物车 | engagement |
| checkout | 发起结算 | conversion |
| purchase | 完成购买 | conversion/revenue |
| share | 分享 | retention |
| button_click | 按钮点击 | engagement |

### properties 事件属性示例

**product_view 商品浏览事件**
```json
{
    "product_id": 12345,
    "product_name": "iPhone 15 Pro",
    "category": "手机",
    "price": 7999,
    "source": "search_result"
}
```

**add_to_cart 加购事件**
```json
{
    "product_id": 12345,
    "quantity": 1,
    "price": 7999,
    "cart_total": 15998
}
```

**purchase 购买事件**
```json
{
    "order_id": "ORD20240115001",
    "amount": 7999,
    "items_count": 1,
    "payment_method": "alipay"
}
```

**search 搜索事件**
```json
{
    "keyword": "iPhone",
    "result_count": 156,
    "filter_applied": true
}
```

## 索引

- PRIMARY KEY: `event_id`
- INDEX: `user_id`, `session_id`, `event_time`
- INDEX: `event_name`
- GIN INDEX: `properties` (支持JSONB查询)

## 常用查询

### 购买漏斗转化分析
```sql
WITH funnel AS (
    SELECT
        DATE(event_time) AS date,
        COUNT(DISTINCT CASE WHEN event_name = 'product_view' THEN user_id END) AS viewed,
        COUNT(DISTINCT CASE WHEN event_name = 'add_to_cart' THEN user_id END) AS added_cart,
        COUNT(DISTINCT CASE WHEN event_name = 'checkout' THEN user_id END) AS checkout,
        COUNT(DISTINCT CASE WHEN event_name = 'purchase' THEN user_id END) AS purchased
    FROM events
    WHERE event_time >= CURRENT_DATE - INTERVAL '7 days'
        AND event_name IN ('product_view', 'add_to_cart', 'checkout', 'purchase')
    GROUP BY DATE(event_time)
)
SELECT
    date,
    viewed,
    added_cart,
    ROUND(added_cart * 100.0 / NULLIF(viewed, 0), 2) AS view_to_cart_pct,
    checkout,
    ROUND(checkout * 100.0 / NULLIF(added_cart, 0), 2) AS cart_to_checkout_pct,
    purchased,
    ROUND(purchased * 100.0 / NULLIF(checkout, 0), 2) AS checkout_to_purchase_pct
FROM funnel
ORDER BY date DESC;
```

### 事件触发量趋势
```sql
SELECT
    DATE(event_time) AS date,
    event_name,
    COUNT(*) AS event_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM events
WHERE event_time >= CURRENT_DATE - INTERVAL '7 days'
    AND event_name IN ('product_view', 'add_to_cart', 'purchase')
GROUP BY DATE(event_time), event_name
ORDER BY date DESC, event_count DESC;
```

### 搜索关键词 TOP 10
```sql
SELECT
    properties->>'keyword' AS keyword,
    COUNT(*) AS search_count,
    COUNT(DISTINCT user_id) AS unique_users,
    AVG((properties->>'result_count')::INT) AS avg_results
FROM events
WHERE event_name = 'search'
    AND event_time >= CURRENT_DATE - INTERVAL '7 days'
    AND properties->>'keyword' IS NOT NULL
GROUP BY properties->>'keyword'
ORDER BY search_count DESC
LIMIT 10;
```
