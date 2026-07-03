# user_profiles - 用户画像表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 主键，关联 users.user_id |
| age | INT | 年龄 |
| gender | VARCHAR(10) | 性别 |
| birth_date | DATE | 出生日期 |
| city | VARCHAR(50) | 城市 |
| province | VARCHAR(50) | 省份 |
| country | VARCHAR(50) | 国家，默认 'China' |
| interests | TEXT[] | 兴趣标签数组 |
| occupation | VARCHAR(50) | 职业 |
| income_level | VARCHAR(20) | 收入水平 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### gender 性别
| 值 | 说明 |
|----|------|
| male | 男 |
| female | 女 |
| unknown | 未知 |

### income_level 收入水平
| 值 | 说明 |
|----|------|
| low | 低收入 |
| medium | 中等收入 |
| high | 高收入 |
| very_high | 超高收入 |

### interests 常见兴趣标签
```
fashion, electronics, sports, beauty, food, travel, gaming, parenting,
music, movies, reading, fitness, photography, cooking, pets, cars
```

## 索引

- PRIMARY KEY: `user_id`
- INDEX: `gender`, `city`, `province`

## 常用查询

### 用户年龄分布
```sql
SELECT
    CASE
        WHEN age < 18 THEN '0-17'
        WHEN age < 25 THEN '18-24'
        WHEN age < 35 THEN '25-34'
        WHEN age < 45 THEN '35-44'
        ELSE '45+'
    END AS age_group,
    COUNT(*) AS user_count
FROM user_profiles
GROUP BY 1
ORDER BY 1;
```

### 性别分布
```sql
SELECT
    gender,
    COUNT(*) AS user_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM user_profiles
GROUP BY gender;
```

### 城市 TOP 10
```sql
SELECT city, COUNT(*) AS user_count
FROM user_profiles
WHERE city IS NOT NULL
GROUP BY city
ORDER BY user_count DESC
LIMIT 10;
```

### 兴趣标签分布
```sql
SELECT
    unnest(interests) AS interest,
    COUNT(*) AS user_count
FROM user_profiles
WHERE interests IS NOT NULL
GROUP BY 1
ORDER BY user_count DESC;
```
