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
- **CAC** = `CAST(sum(cost) AS DECIMAL(38,6)) / nullif(sum(new_users_attributed),0)`，按渠道。
- **ROI** = `CAST(sum(gmv_attributed) AS DECIMAL(38,6)) / nullif(sum(cost),0)`。
- **`gmv_attributed` 只含有 last_touch 归因的成交，不含「未归因」**：全表合计 52,543,305.36，等于 `mart_daily_revenue` 里 `channel_name<>'未归因'` 的 GMV；全站 GMV 是 151,333,127.30，未归因占 65.3%。所以问「渠道 GMV」（合计、按天、按渠道类型）时**用 `call_metric(metric="gmv_by_channel", ...)`**：治理口径含 `channel_name='未归因'` / `channel_type='unknown'` 这一组，不分组的合计等于全站 GMV。本表的 `gmv_attributed` 只用于 ROI 分子和「可归因部分」的渠道对比。
- 只有 paid 和 kol 两类渠道有成本，且 **kol 花费最多**（锚点前 kol 约 77 万、paid 约 51 万；微博KOL 是单渠道第一）；organic/referral/direct 的 cost 恒为 0，算出的 CAC 是 0、ROI 是 NULL，属正常。
- paid/kol 的成本行延伸到 2026-09-01（锚点之后新客/GMV 恒为 0），不带日期上限的花费趋势会混进锚点后的成本。

## 常用查询

### 各渠道 CAC（某月）
```sql
SELECT channel_name,
       CAST(sum(cost) AS integer) AS cost,
       sum(new_users_attributed) AS new_users,
       round(CAST(sum(cost) AS DECIMAL(38,6))/nullif(sum(new_users_attributed),0),1) AS cac
FROM mart_channel_daily
WHERE date_format(dt, '%Y-%m')='2025-12'
GROUP BY 1 ORDER BY cac DESC NULLS LAST;
```

### 各渠道 ROI（某月）
```sql
SELECT channel_name,
       CAST(sum(cost) AS integer) AS cost,
       CAST(sum(gmv_attributed) AS integer) AS gmv,
       round(CAST(sum(gmv_attributed) AS DECIMAL(38,6))/nullif(sum(cost),0),2) AS roi
FROM mart_channel_daily
WHERE date_format(dt, '%Y-%m')='2025-12'
GROUP BY 1 ORDER BY roi DESC NULLS LAST;
```

### 投放成本趋势
```sql
SELECT dt, CAST(sum(cost) AS integer) AS cost FROM mart_channel_daily
WHERE channel_type='paid' GROUP BY 1 ORDER BY 1;
```
