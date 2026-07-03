# ab_test_assignments - 用户分组表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键，自增 |
| user_id | BIGINT | 用户ID，关联 users.user_id |
| test_id | INT | 测试ID，关联 ab_tests.test_id |
| variant_id | INT | 分配的变体ID，关联 ab_test_variants.variant_id |
| assigned_at | TIMESTAMP | 分配时间 |
| first_exposure_at | TIMESTAMP | 首次曝光时间 |

## 分流逻辑说明

1. 用户首次触发实验时，根据 ab_tests.traffic_percentage 决定是否参与
2. 参与的用户根据各变体的 traffic_percentage 分配到具体变体
3. 分配结果记录到本表并持久化
4. 同一用户在整个实验期间保持在同一变体（sticky assignment）

## 索引

- PRIMARY KEY: `id`
- UNIQUE: `(user_id, test_id)` - 确保每个用户每个实验只有一条记录
- INDEX: `test_id`, `variant_id`, `assigned_at`, `first_exposure_at`

## 常用查询

### 实验样本量统计
```sql
SELECT
    t.test_name,
    v.variant_name,
    v.is_control,
    COUNT(a.user_id) AS sample_size,
    v.traffic_percentage AS expected_pct,
    ROUND(COUNT(a.user_id) * 100.0 / SUM(COUNT(a.user_id)) OVER (PARTITION BY t.test_id), 2) AS actual_pct
FROM ab_tests t
JOIN ab_test_variants v ON t.test_id = v.test_id
LEFT JOIN ab_test_assignments a ON v.variant_id = a.variant_id
WHERE t.status = 'running'
GROUP BY t.test_id, t.test_name, v.variant_id, v.variant_name, v.is_control, v.traffic_percentage
ORDER BY t.test_id, v.is_control DESC;
```

### 每日新增分配用户
```sql
SELECT
    t.test_name,
    DATE(a.assigned_at) AS assign_date,
    COUNT(*) AS new_assignments
FROM ab_test_assignments a
JOIN ab_tests t ON a.test_id = t.test_id
WHERE a.assigned_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY t.test_id, t.test_name, DATE(a.assigned_at)
ORDER BY t.test_name, assign_date DESC;
```

### 曝光到分配的时间差分析
```sql
SELECT
    t.test_name,
    AVG(EXTRACT(EPOCH FROM (a.first_exposure_at - a.assigned_at))) AS avg_seconds_to_exposure,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (a.first_exposure_at - a.assigned_at))) AS median_seconds,
    COUNT(*) FILTER (WHERE a.first_exposure_at IS NULL) AS no_exposure_count
FROM ab_test_assignments a
JOIN ab_tests t ON a.test_id = t.test_id
WHERE t.status = 'running'
GROUP BY t.test_id, t.test_name;
```
