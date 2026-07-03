# products - 商品主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| product_id | BIGINT | 主键，商品ID |
| product_name | VARCHAR(200) | 商品名称 |
| category_id | INT | 关联 categories.category_id |
| brand | VARCHAR(100) | 品牌名 |
| description | TEXT | 商品描述 |
| price | DECIMAL(10,2) | 当前售价 |
| original_price | DECIMAL(10,2) | 原价/划线价 |
| cost | DECIMAL(10,2) | 成本价 |
| stock | INT | 库存数量 |
| sold_count | INT | 累计销量 |
| view_count | INT | 累计浏览量 |
| favorite_count | INT | 累计收藏数 |
| rating_avg | DECIMAL(2,1) | 平均评分（0-5） |
| rating_count | INT | 评价数量 |
| main_image_url | VARCHAR(500) | 主图URL |
| image_urls | VARCHAR[] | 商品图片数组 |
| status | VARCHAR(20) | 商品状态 |
| is_featured | BOOLEAN | 是否推荐商品 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### status 商品状态
| 值 | 说明 |
|----|------|
| draft | 草稿，编辑中 |
| pending | 待审核 |
| active | 已上架，正常销售 |
| inactive | 已下架，暂停销售 |
| out_of_stock | 售罄 |
| deleted | 已删除（软删除） |

### is_featured 推荐标记
| 值 | 说明 |
|----|------|
| true | 推荐商品，首页/频道页露出 |
| false | 普通商品 |

### 价格相关字段说明
| 字段 | 说明 | 用途 |
|------|------|------|
| price | 当前售价 | 用户实际支付价格 |
| original_price | 原价/划线价 | 展示折扣力度 |
| cost | 采购成本 | 计算毛利润 |

**毛利率计算**: `(price - cost) / price * 100%`

## 索引

- PRIMARY KEY: `product_id`
- INDEX: `category_id`, `status`, `brand`
- INDEX: `sold_count`, `view_count` (排序用)
- INDEX: `is_featured`

## 常用查询

### 各类目商品销售排行
```sql
SELECT
    c.category_name,
    p.product_name,
    p.sold_count,
    p.price,
    p.sold_count * p.price AS gmv
FROM products p
JOIN categories c ON p.category_id = c.category_id
WHERE p.status = 'active'
    AND c.level = 2  -- 二级分类
ORDER BY p.sold_count DESC
LIMIT 20;
```

### 商品转化率分析（浏览->收藏->购买）
```sql
SELECT
    CASE
        WHEN view_count = 0 THEN '0'
        WHEN view_count < 100 THEN '1-99'
        WHEN view_count < 1000 THEN '100-999'
        ELSE '1000+'
    END AS view_tier,
    COUNT(*) AS product_count,
    AVG(favorite_count * 1.0 / NULLIF(view_count, 0)) AS avg_fav_rate,
    AVG(sold_count * 1.0 / NULLIF(view_count, 0)) AS avg_convert_rate
FROM products
WHERE status = 'active'
GROUP BY view_tier
ORDER BY view_tier;
```

### 库存预警商品（低库存+高销量）
```sql
SELECT
    p.product_id,
    p.product_name,
    c.category_name,
    p.stock,
    p.sold_count,
    ROUND(p.stock * 1.0 / NULLIF(p.sold_count / 30.0, 0), 1) AS days_of_stock
FROM products p
JOIN categories c ON p.category_id = c.category_id
WHERE p.status = 'active'
    AND p.stock < 50
    AND p.sold_count > 100
ORDER BY days_of_stock ASC
LIMIT 20;
```
