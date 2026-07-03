# user_segments - 用户分群定义表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| segment_id | INT | 主键，分群ID，自增 |
| segment_name | VARCHAR(100) | 分群名称 |
| segment_type | VARCHAR(50) | 分群类型 |
| description | TEXT | 分群描述 |
| rules_json | JSONB | 分群规则定义 |
| owner | VARCHAR(50) | 创建人 |
| status | VARCHAR(20) | 状态 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

## 字段枚举值

### segment_type 分群类型
| 值 | 说明 |
|----|------|
| static | 静态分群，手动圈选 |
| dynamic | 动态分群，规则自动更新 |
| rfm | RFM模型分群 |
| prediction | 预测模型分群 |

### status 状态
| 值 | 说明 |
|----|------|
| active | 生效中 |
| paused | 已暂停 |
| archived | 已归档 |

### rules_json 示例结构
```json
{
  "conditions": [
    {"field": "user_level", "operator": ">=", "value": 3},
    {"field": "last_active_at", "operator": ">=", "value": "-30d"}
  ],
  "logic": "AND"
}
```

## 索引

- PRIMARY KEY: `segment_id`
- INDEX: `status`, `segment_type`

## 常用查询

### 活跃分群列表
```sql
SELECT
    segment_id,
    segment_name,
    segment_type,
    user_count,
    created_at
FROM user_segments
WHERE status = 'active'
ORDER BY user_count DESC;
```

### 各类型分群统计
```sql
SELECT
    segment_type,
    COUNT(*) AS segment_count,
    SUM(user_count) AS total_users
FROM user_segments
WHERE status = 'active'
GROUP BY segment_type;
```
