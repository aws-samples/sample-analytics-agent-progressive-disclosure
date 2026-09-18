# 业务域索引

## 概述

APP Analytics 数据库包含 **8 个原始业务域（共 35 张明细表）**，外加一层**治理后的数据集市（mart，4 张预聚合表）**。根据分析需求加载对应索引文件。

> **两层数据，先分清再路由**：
> - **原始域**（下表前 8 行）是明细表，适合「取数 / 看明细 / 探特定口径」，需要现场 join。
> - **治理层 mart**（最后一行）是算好的干净表、口径已冻结，适合「诊断 / 复盘 / 趋势 / 综合判断」，只需写简单 SELECT。遇到「最近怎么样、为什么涨跌、环比、复购率」这类问题，优先走 mart。

## 域路由表

| 域 | 表数量 | 索引文件 | 适用场景 |
|----|--------|----------|----------|
| 用户域 | 5 | `user/_index.md` | 用户画像、注册分析、设备分析、用户分群 |
| 商品域 | 3 | `product/_index.md` | 商品分析、类目运营、库存管理 |
| 行为域 | 4 | `behavior/_index.md` | 埋点分析、页面路径、会话分析 |
| 社交域 | 6 | `social/_index.md` | 内容分析、互动分析、社交关系 |
| 交易域 | 4 | `transaction/_index.md` | 订单分析、支付分析、GMV |
| 归因域 | 5 | `attribution/_index.md` | 渠道分析、广告ROI、获客成本 |
| 营销域 | 5 | `marketing/_index.md` | 活动分析、优惠券、推送效果 |
| 实验域 | 3 | `experiment/_index.md` | A/B测试、实验效果 |
| **治理层(mart)** | 4 | `mart/_index.md` | 诊断/复盘/趋势/综合判断、GMV拆解归因、CAC/ROI、复购率 |

## 关键词路由

根据问题中的关键词选择对应域：

- **用户相关**: 用户、注册、画像、设备、会员、等级、分群 → `user/_index.md`
- **商品相关**: 商品、分类、SKU、库存、价格、品牌、类目 → `product/_index.md`
- **行为相关**: 会话、页面、事件、埋点、点击、曝光、路径 → `behavior/_index.md`
- **社交相关**: 帖子、评论、点赞、关注、分享、私信、内容 → `social/_index.md`
- **交易相关**: 订单、支付、退款、GMV、交易、结算 → `transaction/_index.md`
- **归因相关**: 渠道、归因、ROI、投放、广告、获客、CAC → `attribution/_index.md`
- **营销相关**: 活动、优惠券、推送、Banner、促销 → `marketing/_index.md`
- **实验相关**: A/B测试、实验、变体、分桶、对照组 → `experiment/_index.md`
- **诊断/综合判断相关**: 最近怎么样、整体、复盘、周报、为什么涨跌、环比、复购率、CAC/ROI → `mart/_index.md`（治理层，口径已冻结，写简单 SELECT 即可）

## 全局锚点表 meta_snapshot（不属于任何域，写时间条件必用）

这张表**不在上面的路由表里**，因为它不属于任何业务域：每一个带时间条件的查询都要用它。
所以它在这里、在总索引上直接写清，不需要先猜是哪个域再去找。

| 列 | 类型 | 含义 |
|---|---|---|
| `as_of_date` | date | 数据里的「今天」= `max(mart_daily_kpi.dt)`。所有相对日期从这里起算 |
| `data_start` | date | 数据起始日 = `min(mart_daily_kpi.dt)` |

一行，两列。**「最近 / 上周 / 本月」一律写 `(SELECT max(as_of_date) FROM meta_snapshot)`**：

```sql
WHERE dt >  (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
  AND dt <= (SELECT max(as_of_date) FROM meta_snapshot)
```

- **禁用 `current_date` / `now()`**：语法合法、不报错，但落在数据区间之外，安静地返回 0 行。
- **也禁用该表自己的 `max(时间列)`**：这条更隐蔽，它不返回空集而是返回一个错的数——**全库 91 个时间列里有 12 类轴末超出业务日历**（2026-09-17 实测）：`subscriptions.end_date` 到 2027-01-24、`ad_campaigns.end_date` 到 2026-10-01、`channel_daily_costs` / `mart_channel_daily` 到 2026-09-01、`coupons.end_date`、`user_coupons.expire_at`、`campaigns.end_date`、`banners.end_date` 依次递减。拿各自的 max 当今天，两张表的「最近 7 天」指向完全不同的两段日期。
- **开窗必须同时写上下界**，只写下界会把日历外的行捞进来（实测 CAC 从 2880 变成 15137，不报错）。全表轴末对照见 `../metrics/governed_metrics.md` §时间锚点。

## 跨域分析

当分析涉及多个域时，参考 `../relationships.md` 了解表间关联关系。

## 派生层（DWD/DWS/ADS/历史遗留）

基表之上还有一个**派生层**：清洗副本、预聚合、部门口径表和废弃遗留表。**如果问题涉及口径归属（财务/增长）、现成聚合、或碰到名字相似的表拿不准，先读 `_derived_overview.md` 再进域**。
