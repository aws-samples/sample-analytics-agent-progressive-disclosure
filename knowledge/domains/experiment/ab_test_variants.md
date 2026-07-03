# ab_test_variants - 测试变体表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| variant_id | INT | 主键，变体唯一标识，自增 |
| test_id | INT | 所属测试ID，关联 ab_tests.test_id |
| variant_name | VARCHAR(100) | 变体名称 |
| variant_key | VARCHAR(50) | 变体标识符（如 control, treatment_a） |
| description | TEXT | 变体描述 |
| traffic_percentage | DECIMAL(5,2) | 该变体分配的流量百分比 |
| config_json | JSONB | 变体配置参数 |
| is_control | BOOLEAN | 是否为对照组 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### variant_key 常见变体标识
| 值 | 说明 |
|----|------|
| control | 对照组，保持原有体验 |
| treatment_a | 实验组A |
| treatment_b | 实验组B |
| treatment_c | 实验组C |

### is_control 对照组标识
| 值 | 说明 |
|----|------|
| true | 对照组，用于基准对比 |
| false | 实验组，应用新方案 |

### config_json 变体配置示例
```json
{
  "button_color": "#FF5722",
  "button_text": "立即购买",
  "show_countdown": true,
  "discount_percentage": 15,
  "layout_version": "v2"
}
```

## 索引

- PRIMARY KEY: `variant_id`
- INDEX: `test_id`
- UNIQUE: `(test_id, variant_key)`

## 常用查询

### 查看实验的所有变体配置
```sql
SELECT
    t.test_name,
    v.variant_name,
    v.variant_key,
    v.is_control,
    v.traffic_percentage,
    v.config_json
FROM ab_test_variants v
JOIN ab_tests t ON v.test_id = t.test_id
WHERE t.test_id = 1  -- 指定测试ID
ORDER BY v.is_control DESC, v.variant_id;
```

### 检查变体流量分配是否100%
```sql
SELECT
    t.test_name,
    SUM(v.traffic_percentage) AS total_traffic,
    CASE
        WHEN SUM(v.traffic_percentage) = 100 THEN 'OK'
        ELSE 'ERROR'
    END AS status
FROM ab_tests t
JOIN ab_test_variants v ON t.test_id = v.test_id
WHERE t.status = 'running'
GROUP BY t.test_id, t.test_name
ORDER BY t.test_name;
```

### 查看特定配置项的变体
```sql
SELECT
    t.test_name,
    v.variant_name,
    v.config_json->>'button_color' AS button_color,
    v.config_json->>'discount_percentage' AS discount
FROM ab_test_variants v
JOIN ab_tests t ON v.test_id = t.test_id
WHERE v.config_json ? 'button_color'
ORDER BY t.test_id, v.variant_id;
```
