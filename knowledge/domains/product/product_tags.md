# product_tags - 商品标签表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT | 主键 |
| product_id | BIGINT | 关联 products.product_id |
| tag_name | VARCHAR(50) | 标签名称 |
| tag_type | VARCHAR(30) | 标签类型 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### tag_type 标签类型
| 值 | 说明 | 常见标签示例 |
|----|------|-------------|
| promotion | 促销标签 | 新品、热销、限时特惠、秒杀、满减 |
| feature | 功能标签 | 包邮、正品保证、七天无理由、急速发货 |
| season | 季节标签 | 春季新款、夏季清凉、秋冬保暖、四季通用 |
| audience | 人群标签 | 学生专享、送礼佳品、白领首选 |
| style | 风格标签 | 简约、复古、潮流、轻奢、国潮 |

### 常见 tag_name 值
实测全表 10,659 行、28 个 tag_name（下面是全集，按它之外的值过滤是空集；各标签行数在 334~416 之间，分布均匀）：
```
-- promotion 促销类
新品, 热销, 限时特惠, 秒杀, 满减, 买一送一, 清仓

-- feature 功能类
包邮, 正品保证, 七天无理由, 急速发货, 进口, 自营

-- season 季节类
春季新款, 夏季清凉, 秋冬保暖, 四季通用

-- audience 人群类
学生专享, 送礼佳品, 白领首选, 家庭装, 情侣款

-- style 风格类
简约, 复古, 潮流, 轻奢, 文艺, 国潮
```

## 索引

- PRIMARY KEY: `id`
- INDEX: `product_id`
- INDEX: `tag_type`, `tag_name`

## 常用查询

### 各类型标签使用统计
```sql
SELECT
    tag_type,
    COUNT(DISTINCT product_id) AS product_count,
    COUNT(*) AS tag_count
FROM product_tags
GROUP BY tag_type
ORDER BY product_count DESC;
```

### 热门标签 TOP 20
```sql
SELECT
    tag_name,
    tag_type,
    COUNT(*) AS usage_count
FROM product_tags
GROUP BY tag_name, tag_type
ORDER BY usage_count DESC
LIMIT 20;
```

### 标签商品销量分析
```sql
SELECT
    pt.tag_name,
    pt.tag_type,
    COUNT(DISTINCT p.product_id) AS product_count,
    SUM(p.sold_count) AS total_sold,
    AVG(p.sold_count) AS avg_sold_per_product
FROM product_tags pt
JOIN products p ON pt.product_id = p.product_id
WHERE p.status = 'on_sale'
GROUP BY pt.tag_name, pt.tag_type
HAVING COUNT(DISTINCT p.product_id) >= 10
ORDER BY total_sold DESC
LIMIT 20;
```
