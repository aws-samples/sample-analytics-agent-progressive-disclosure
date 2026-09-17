# ab_tests - A/B测试主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| test_id | INT | 主键，测试唯一标识，自增 |
| test_name | VARCHAR(200) | 测试名称 |
| test_key | VARCHAR(100) | 测试唯一标识符（用于代码引用） |
| hypothesis | TEXT | 实验假设 |
| description | TEXT | 测试描述 |
| primary_metric | VARCHAR(100) | 主要评估指标 |
| secondary_metrics | VARCHAR[] | 次要评估指标数组 |
| target_segment_ids | INT[] | 目标用户分群ID数组 |
| traffic_percentage | DECIMAL(5,2) | 参与实验的流量百分比 |
| min_sample_size | INT | 最小样本量要求 |
| start_date | TIMESTAMP | 实验开始时间 |
| end_date | TIMESTAMP | 实验结束时间 |
| status | VARCHAR(20) | 测试状态 |
| conclusion | TEXT | 实验结论 |
| winner_variant_id | INT | 获胜变体ID，关联 ab_test_variants.variant_id |
| owner | VARCHAR(100) | 实验负责人 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### status 测试状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| running | 运行中，正在收集数据 | 4 |
| completed | 已完成，实验结束并有结论 | 4 |
| draft | 草稿，实验配置中 | 4 |

> 全表 12 行，只有这 3 个值。**没有** `paused`（旧文档写过），按它筛是空集。

### primary_metric 主要指标

> 这一列**不是枚举**：它装的是指标名，12 个实验里有 11 种取值，基本一实验一指标。
> 别照着固定列表筛，要看有哪些就 `SELECT DISTINCT primary_metric FROM ab_tests`。
> 实测取值：`add_to_cart_rate`(2)、`average_order_value`、`coupon_claim_rate`、
> `push_open_rate`、`membership_conversion_rate`、`next_7day_repurchase_rate`、
> `homepage_ctr`、`day1_retention`、`purchase_conversion_rate`、`search_result_ctr`、
> `checkout_completion_rate`（各 1）。旧文档写的 `conversion_rate` / `retention_d7` /
> `revenue_per_user` / `session_duration` / `click_through_rate` 一个都不存在。

## 索引

- PRIMARY KEY: `test_id`
- UNIQUE: `test_key`
- INDEX: `status`, `start_date`, `end_date`, `owner`

## 常用查询

### 查看运行中的实验
```sql
SELECT
    test_id,
    test_name,
    primary_metric,
    traffic_percentage,
    start_date,
    owner
FROM ab_tests
WHERE status = 'running'
ORDER BY start_date DESC;
```

### 实验运行时长统计
```sql
SELECT
    test_name,
    status,
    start_date,
    end_date,
    EXTRACT(DAY FROM COALESCE(end_date, CURRENT_TIMESTAMP) - start_date) AS days_running
FROM ab_tests
WHERE start_date IS NOT NULL
ORDER BY days_running DESC;
```

### 按负责人统计实验数量
```sql
SELECT
    owner,
    COUNT(*) AS total_tests,
    COUNT(*) FILTER (WHERE status = 'running') AS running,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed
FROM ab_tests
GROUP BY owner
ORDER BY total_tests DESC;
```
