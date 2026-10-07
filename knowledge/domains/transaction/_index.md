# 交易域 (Transaction Domain)

## 域概述

交易域是电商业务的核心，管理所有交易相关数据，包括订单、订单明细、支付和订阅。支撑收入分析、订单追踪、支付对账、用户消费行为分析和会员订阅管理等关键业务场景。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| orders | 订单主表，订单信息和状态 | `orders.md` |
| order_items | 订单明细，商品级别信息 | `order_items.md` |
| payments | 支付记录，支付流水和状态 | `payments.md` |
| subscriptions | 订阅管理，会员订阅服务 | `subscriptions.md` |

## 表间关系

```
orders (订单主表)
  ├── order_items (1:N) ── 一个订单包含多个商品
  └── payments (1:N) ── 一个订单可能有多次支付尝试

users
  ├── orders (1:N) ── 一个用户有多个订单
  ├── payments (1:N) ── 一个用户有多条支付记录
  └── subscriptions (1:N) ── 一个用户可有多个订阅

payments
  └── subscriptions (1:1) ── 订阅关联支付记录
```

## 金额计算关系

```
订单实付金额 = 商品总金额 - 优惠金额 + 运费
actual_amount = total_amount - discount_amount + shipping_fee
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 订单、GMV、销售额、下单、发货、签收、取消 | `orders.md` |
| 退款率、退款订单、哪些单退了 | `orders.md` |
| **退款总额、累计退款、一共退了多少、净收入、财务口径收入** | `fin_daily_revenue.md`（按 `refunded_at` 建轴，是退款的**唯一全量口径**；⚠️ 别用 `mart_daily_kpi.refund_amt` 跨日求和，那张表的轴不含 `refunded_at`，合计偏低约 4% 且不报错） |
| 商品销量、销售排行、SKU、购买数量、商品明细 | `order_items.md` |
| 支付、支付宝、微信、银行卡、支付成功率、支付失败 | `payments.md` |
| 订阅、会员、续费、自动扣款、订阅取消 | `subscriptions.md` |

## 常见分析场景

1. **销售收入分析**: 加载 `orders.md`
2. **商品销售排行**: 加载 `orders.md` + `order_items.md`
3. **支付转化分析**: 加载 `payments.md`
4. **支付方式分布**: 加载 `payments.md`
5. **订阅留存分析**: 加载 `subscriptions.md`
6. **用户消费分析**: 加载 `orders.md`（需关联 users 域）

## 派生层附录

本域还有派生表（清洗层/汇总层/口径表/历史遗留）。下列任一情况**必须先读 `_index.derived.md`**：
- 遇到同名近义表拿不准选哪张时；
- 问的是**金额合计**（退款总额、确认收入、净收入、财务口径）——同一个金额在本域有多套口径的表，上面的表清单只列了基表，选错口径不会报错、只会给出一个偏低/偏高的数。
