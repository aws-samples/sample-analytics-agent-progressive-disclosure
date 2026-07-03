# user_segment_members - 分群成员关系表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 联合主键，用户ID |
| segment_id | INT | 联合主键，分群ID |
| entered_at | TIMESTAMP | 进入分群时间 |
| exited_at | TIMESTAMP | 退出分群时间（NULL=仍在分群） |

## 索引

- PRIMARY KEY: `(user_id, segment_id)`
- INDEX: `segment_id`, `entered_at`

## 关联关系

- `user_id` → `users.user_id`
- `segment_id` → `user_segments.segment_id`

## 常用查询

### 分群人数统计
```sql
SELECT
    s.segment_id,
    s.segment_name,
    COUNT(m.user_id) AS current_members
FROM user_segments s
LEFT JOIN user_segment_members m
    ON s.segment_id = m.segment_id
    AND m.exited_at IS NULL
WHERE s.status = 'active'
GROUP BY s.segment_id, s.segment_name
ORDER BY current_members DESC;
```

### 用户所属分群
```sql
SELECT
    s.segment_name,
    m.entered_at
FROM user_segment_members m
JOIN user_segments s ON m.segment_id = s.segment_id
WHERE m.user_id = ?
  AND m.exited_at IS NULL
  AND s.status = 'active';
```

### 分群成员变化趋势
```sql
SELECT
    DATE(entered_at) AS date,
    COUNT(CASE WHEN exited_at IS NULL THEN 1 END) AS entered,
    COUNT(CASE WHEN exited_at IS NOT NULL THEN 1 END) AS exited
FROM user_segment_members
WHERE segment_id = ?
  AND entered_at >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(entered_at)
ORDER BY date;
```

### 分群交叉分析
```sql
SELECT
    s1.segment_name AS segment_a,
    s2.segment_name AS segment_b,
    COUNT(DISTINCT m1.user_id) AS overlap_count
FROM user_segment_members m1
JOIN user_segment_members m2
    ON m1.user_id = m2.user_id
    AND m1.segment_id < m2.segment_id
JOIN user_segments s1 ON m1.segment_id = s1.segment_id
JOIN user_segments s2 ON m2.segment_id = s2.segment_id
WHERE m1.exited_at IS NULL
  AND m2.exited_at IS NULL
GROUP BY s1.segment_name, s2.segment_name
ORDER BY overlap_count DESC
LIMIT 10;
```
