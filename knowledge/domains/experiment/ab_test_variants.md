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
| config_json | `string` | 变体配置参数（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`）。**种子数据里整列为 NULL** |
| is_control | BOOLEAN | 是否为对照组 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### variant_key 变体标识

> 这一列**不是枚举**，是「实验名 + 组名」拼出来的键：30 行 30 个不同取值，形如
> `<test_key>_control` / `<test_key>_treatment_a` / `<test_key>_treatment_b`
> （例如 `homepage_banner_v2_control`）。**不要**照着 `control` / `treatment_a` 筛，
> 那样是空集；要认组别就用 `is_control`，或者 `variant_key LIKE '%_control'`。
> 实测没有 `treatment_c`（旧文档写过）——每个实验最多 3 组。

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
> ⚠️ **这段查询在当前种子数据上返回 0 行**：`config_json` 整列为 NULL（登记在
> `scripts/lakehouse/verify_constants.py` 的清单里）。留着是给"以后填上了怎么查"当模板，
> **别拿它去回答实际问题**——真实分流效果看 `ab_test_assignments`。
> 另外 Postgres 的 `json ? 'k'`（判断键存在）在 Trino 里**是语法错**，
> 要写成 `json_extract_scalar(...) IS NOT NULL`。

```sql
SELECT
    t.test_name,
    v.variant_name,
    json_extract_scalar(v.config_json, '$.button_color') AS button_color,
    json_extract_scalar(v.config_json, '$.discount_percentage') AS discount
FROM ab_test_variants v
JOIN ab_tests t ON v.test_id = t.test_id
WHERE json_extract_scalar(v.config_json, '$.button_color') IS NOT NULL
ORDER BY t.test_id, v.variant_id;
```
