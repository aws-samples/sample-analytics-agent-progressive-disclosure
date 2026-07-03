# categories - 商品分类表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| category_id | INT | 主键，分类ID |
| parent_id | INT | 父分类ID（顶级分类为NULL） |
| category_name | VARCHAR(100) | 分类名称 |
| level | INT | 分类层级（1-3级） |
| sort_order | INT | 排序权重 |
| icon_url | VARCHAR(500) | 分类图标URL |
| is_active | BOOLEAN | 是否启用 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### level 分类层级
| 值 | 说明 | 示例 |
|----|------|------|
| 1 | 一级分类 | 服装、数码、食品、家居 |
| 2 | 二级分类 | 男装、女装、手机、零食 |
| 3 | 三级分类 | T恤、连衣裙、iPhone、坚果 |

### is_active 启用状态
| 值 | 说明 |
|----|------|
| true | 启用，前端可见 |
| false | 禁用，仅后台可见 |

## 索引

- PRIMARY KEY: `category_id`
- INDEX: `parent_id`, `level`, `is_active`

## 常用查询

### 获取完整分类树
```sql
WITH RECURSIVE category_tree AS (
    -- 顶级分类
    SELECT category_id, category_name, parent_id, level,
           category_name::TEXT AS path
    FROM categories
    WHERE parent_id IS NULL AND is_active = true

    UNION ALL

    -- 递归子分类
    SELECT c.category_id, c.category_name, c.parent_id, c.level,
           ct.path || ' > ' || c.category_name
    FROM categories c
    JOIN category_tree ct ON c.parent_id = ct.category_id
    WHERE c.is_active = true
)
SELECT * FROM category_tree
ORDER BY path;
```

### 各级分类数量统计
```sql
SELECT
    level,
    COUNT(*) AS category_count,
    SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_count
FROM categories
GROUP BY level
ORDER BY level;
```

### 二级分类及其商品数
```sql
SELECT
    c.category_id,
    c.category_name,
    pc.category_name AS parent_name,
    COUNT(p.product_id) AS product_count
FROM categories c
LEFT JOIN categories pc ON c.parent_id = pc.category_id
LEFT JOIN products p ON c.category_id = p.category_id
WHERE c.level = 2 AND c.is_active = true
GROUP BY c.category_id, c.category_name, pc.category_name
ORDER BY product_count DESC;
```
