# mart_channel_daily - 渠道效果表

## 用途

按 **天 × 渠道** 预聚合的投放成本与效果。用于 CAC、ROI、投放效果对比。

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | date | 日期 |
| channel_id | int | 渠道 ID |
| channel_name | text | 渠道名 |
| channel_type | text | organic / paid / kol / referral / direct |
| cost | numeric | 当日投放成本（来自 channel_daily_costs） |
| impressions | bigint | 曝光 |
| clicks | bigint | 点击 |
| installs | int | 安装 |
| new_users_attributed | int | 归因到该渠道的新客数（按注册日） |
| gmv_attributed | numeric | 归因到该渠道的 GMV（按下单日） |

## 口径

- 归因 = last_touch。
- **CAC** = `sum(cost) / nullif(sum(new_users_attributed),0)`，按渠道。
- **ROI** = `sum(gmv_attributed) / nullif(sum(cost),0)`。
- 只有 paid 类渠道有成本；organic/kol/direct 多数 cost=0，算出的 CAC/ROI 会是 NULL，属正常。

## 常用查询

### 各渠道 CAC（某月）
```sql
SELECT channel_name,
       sum(cost)::int AS cost,
       sum(new_users_attributed) AS new_users,
       round(sum(cost)/nullif(sum(new_users_attributed),0),1) AS cac
FROM mart_channel_daily
WHERE to_char(dt,'YYYY-MM')='2025-12'
GROUP BY 1 ORDER BY cac DESC NULLS LAST;
```

### 各渠道 ROI（某月）
```sql
SELECT channel_name,
       sum(cost)::int AS cost,
       sum(gmv_attributed)::int AS gmv,
       round(sum(gmv_attributed)/nullif(sum(cost),0),2) AS roi
FROM mart_channel_daily
WHERE to_char(dt,'YYYY-MM')='2025-12'
GROUP BY 1 ORDER BY roi DESC NULLS LAST;
```

### 投放成本趋势
```sql
SELECT dt, sum(cost)::int AS cost FROM mart_channel_daily
WHERE channel_type='paid' GROUP BY 1 ORDER BY 1;
```
