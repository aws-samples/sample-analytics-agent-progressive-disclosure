# mart_user_summary - 用户汇总表

## 用途

用户级（一人一行）的汇总。用于复购率、LTV、人均消费、按注册渠道/cohort 看用户质量。

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | bigint | 用户 ID |
| register_date | date | 注册日（registered_at） |
| register_channel | text | last_touch 归因渠道名，未归因 = '未归因' |
| first_paid_date | date | 首笔有效订单日期；从没下单则为 NULL |
| paid_order_cnt | int | 有效订单数 |
| total_gmv | numeric | 累计实付 GMV |
| is_repurchaser_30d | bool | 首单后 30 天内是否再次产生有效订单 |
| last_active_date | date | 最后活跃日（events 最大 event_time） |

## 口径

- **复购（is_repurchaser_30d）** = 首笔有效订单后 30 天内产生第二笔有效订单。
- **复购率** = 复购用户数 / **有首单的用户数**（分母不含从没下过单的人，这点很关键，别拿全体当分母）。
- LTV 近似 = 有订单用户的 `avg(total_gmv)`。

## 常用查询

### 复购率（官方口径）
```sql
SELECT round(100.0 * count(*) FILTER (WHERE is_repurchaser_30d)
            / nullif(count(*) FILTER (WHERE first_paid_date IS NOT NULL),0), 1)
       AS repurchase_rate_30d_pct
FROM mart_user_summary;
```

### 各注册渠道的复购率 + 人均消费
```sql
SELECT register_channel,
       count(*) FILTER (WHERE first_paid_date IS NOT NULL) AS buyers,
       round(100.0 * count(*) FILTER (WHERE is_repurchaser_30d)
             / nullif(count(*) FILTER (WHERE first_paid_date IS NOT NULL),0), 1) AS repurchase_pct,
       round(avg(total_gmv) FILTER (WHERE first_paid_date IS NOT NULL), 0) AS arpu_buyer
FROM mart_user_summary
GROUP BY 1 ORDER BY buyers DESC;
```

### 注册月 cohort 的付费转化
```sql
SELECT to_char(register_date,'YYYY-MM') AS cohort,
       count(*) AS users,
       count(*) FILTER (WHERE first_paid_date IS NOT NULL) AS buyers,
       round(100.0*count(*) FILTER (WHERE first_paid_date IS NOT NULL)/count(*),1) AS paid_conv_pct
FROM mart_user_summary GROUP BY 1 ORDER BY 1;
```
