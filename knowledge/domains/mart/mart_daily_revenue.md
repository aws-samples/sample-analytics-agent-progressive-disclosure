# mart_daily_revenue - 收入事实表

## 用途

按 **天 × 渠道 × 新老客** 预聚合的 GMV。用于 GMV 拆解、收入归因、复盘（「环比怎么变、主要谁带动的」）。做归因下钻时在这一张表上换维度切片即可，不用 join 原始表。

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| dt | date | 下单日（按 placed_at） |
| channel_id | int | 渠道 ID，未归因 = 0 |
| channel_name | text | 渠道名，未归因 = '未归因' |
| channel_type | text | organic / paid / kol / referral / direct / unknown（未归因） |
| is_new_user | bool | 该订单是否为用户的首笔有效订单（= 新客订单） |
| order_cnt | int | 订单数 |
| paying_user_cnt | int | 付费用户数 |
| gmv | numeric | 实付 GMV |

## 口径

- 渠道 = **last_touch 归因**。**注意：约六成 GMV 归不到渠道（channel_type='unknown' / channel_name='未归因'）。做渠道归因时必须把这块单列出来，不能当它不存在。**
- `is_new_user=true` = 该用户的首笔有效订单。
- 全表 gmv 合计 = `mart_daily_kpi` 的 gmv 合计（同口径，可交叉校验）。

## 常用查询

### 按月看 GMV（整月才可比，残月别直接比）
```sql
SELECT to_char(dt,'YYYY-MM') AS mon, sum(gmv)::int AS gmv, sum(order_cnt) AS orders
FROM mart_daily_revenue GROUP BY 1 ORDER BY 1;
```

### 某月按渠道类型拆解 + 占比
```sql
SELECT channel_type, sum(gmv)::int AS gmv,
       round(100.0*sum(gmv)/sum(sum(gmv)) OVER (),1) AS pct
FROM mart_daily_revenue
WHERE to_char(dt,'YYYY-MM')='2025-12'
GROUP BY 1 ORDER BY 2 DESC;
```

### 新老客拆解
```sql
SELECT is_new_user, sum(gmv)::int AS gmv, sum(order_cnt) AS orders
FROM mart_daily_revenue
WHERE to_char(dt,'YYYY-MM')='2025-12'
GROUP BY 1;
```

### 归因覆盖率（有渠道 vs 未归因）—— 做渠道结论前先看这个
```sql
SELECT CASE WHEN channel_type='unknown' THEN '未归因' ELSE '有渠道归因' END AS seg,
       sum(gmv)::int AS gmv,
       round(100.0*sum(gmv)/sum(sum(gmv)) OVER (),1) AS pct
FROM mart_daily_revenue GROUP BY 1;
```

### 月环比（上月 vs 上上月，按渠道类型看谁带动）
```sql
WITH m AS (
  SELECT to_char(dt,'YYYY-MM') mon, channel_type, sum(gmv) gmv
  FROM mart_daily_revenue GROUP BY 1,2
)
SELECT channel_type,
       sum(gmv) FILTER (WHERE mon='2025-11')::int AS nov,
       sum(gmv) FILTER (WHERE mon='2025-12')::int AS dec
FROM m GROUP BY 1 ORDER BY 3 DESC NULLS LAST;
```
