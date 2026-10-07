# user_segments - 用户分群定义表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| segment_id | INT | 主键，分群ID，自增 |
| segment_name | VARCHAR(100) | 分群名称 |
| segment_type | VARCHAR(50) | 分群类型 |
| description | TEXT | 分群描述 |
| rules_json | `string` | 分群规则定义（JSON 文本；**不是 Postgres 的 JSONB**，取值用 `json_extract_scalar`） |
| owner | VARCHAR(50) | 创建人 |
| status | VARCHAR(20) | 状态 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

## 字段枚举值

### segment_type 分群类型
| 值 | 说明 | 实测行数 |
|----|------|------|
| lifecycle | 生命周期分群（新客/活跃/沉睡…） | 2 |
| membership | 会员身份分群 | 1 |
| behavior | 行为分群 | 1 |
| demographic | 人口属性分群 | 1 |
| value | 价值分群（消费额档位） | 1 |
| device | 设备分群 | 1 |
| geographic | 地域分群 | 1 |
| engagement | 互动活跃度分群 | 1 |
| acquisition | 获客来源分群 | 1 |

> 全表 10 行 9 类。**没有** `static` / `dynamic` / `rfm` / `prediction` 这几个旧文档写过
> 的值，别照它们写 WHERE。

### status 状态
| 值 | 说明 |
|----|------|
| active | 生效中 |

> 种子数据里 10 个分群全是 `active`；`paused` / `archived` 在业务上存在，但数据里没有。

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

> ⚠️ 本表**只有分群定义，没有人数**。分群人数一律从 `user_segment_members` 现算
> （在群 = `exited_at IS NULL`）。别写 `user_segments.user_count`，那一列不存在。

### 活跃分群列表（带当前人数）
```sql
SELECT
    s.segment_id,
    s.segment_name,
    s.segment_type,
    COUNT(m.user_id) AS current_members,
    s.created_at
FROM user_segments s
LEFT JOIN user_segment_members m
    ON s.segment_id = m.segment_id
   AND m.exited_at IS NULL
WHERE s.status = 'active'
GROUP BY s.segment_id, s.segment_name, s.segment_type, s.created_at
ORDER BY current_members DESC;
```

### 各类型分群统计
```sql
SELECT
    s.segment_type,
    COUNT(DISTINCT s.segment_id) AS segment_count,
    COUNT(m.user_id)             AS total_members
FROM user_segments s
LEFT JOIN user_segment_members m
    ON s.segment_id = m.segment_id
   AND m.exited_at IS NULL
WHERE s.status = 'active'
GROUP BY s.segment_type;
```

注意 `total_members` 是**人次不是人数**：一个用户可以同时在多个分群里，跨分群相加会重复。
要"至少属于一个分群的人数"用 `COUNT(DISTINCT m.user_id)` 且不要按 segment_type 分组。
