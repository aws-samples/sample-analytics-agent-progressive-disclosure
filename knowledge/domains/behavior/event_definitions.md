# event_definitions - 事件定义表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| event_name | VARCHAR(50) | 主键，事件名称 |
| event_category | VARCHAR(30) | 事件分类 |
| description | TEXT | 事件描述 |
| properties_schema | JSONB | 事件属性schema定义 |
| owner | VARCHAR(50) | 负责人 |
| is_core_event | BOOLEAN | 是否核心事件 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### event_category 事件分类
| 值 | 说明 | 典型事件 |
|----|------|----------|
| acquisition | 获客类 | app_install, first_open, registration |
| engagement | 互动类 | page_view, button_click, search |
| conversion | 转化类 | add_to_cart, checkout, purchase |
| retention | 留存类 | app_open, login, share |
| revenue | 营收类 | purchase, subscription, refund |

### is_core_event 核心事件说明
核心事件是业务关键指标的基础，通常包括：
| 事件名 | 说明 |
|--------|------|
| app_open | APP打开 |
| page_view | 页面浏览 |
| search | 搜索 |
| product_view | 商品详情页浏览 |
| add_to_cart | 加入购物车 |
| checkout | 发起结算 |
| purchase | 完成购买 |

### properties_schema 示例
```json
{
    "product_view": {
        "product_id": {"type": "integer", "required": true},
        "product_name": {"type": "string", "required": true},
        "category": {"type": "string", "required": false},
        "price": {"type": "number", "required": true},
        "source": {"type": "string", "required": false}
    }
}
```

## 索引

- PRIMARY KEY: `event_name`
- INDEX: `event_category`, `is_core_event`

## 常用查询

### 查看所有核心事件
```sql
SELECT
    event_name,
    event_category,
    description
FROM event_definitions
WHERE is_core_event = TRUE
ORDER BY event_category, event_name;
```

### 按分类统计事件数
```sql
SELECT
    event_category,
    COUNT(*) AS event_count,
    SUM(CASE WHEN is_core_event THEN 1 ELSE 0 END) AS core_count
FROM event_definitions
GROUP BY event_category
ORDER BY event_count DESC;
```

### 查看事件属性定义
```sql
SELECT
    event_name,
    description,
    properties_schema
FROM event_definitions
WHERE event_name IN ('product_view', 'add_to_cart', 'purchase');
```
