# 营销域 (Marketing Domain)

## 域概述

营销域管理所有营销相关数据，包括营销活动、优惠券、Banner 广告位和推送通知。该域支持营销团队进行用户触达、促销活动管理和效果追踪。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| campaigns | 营销活动管理 | `campaigns.md` |
| coupons | 优惠券模板定义 | `coupons.md` |
| user_coupons | 用户领取的优惠券 | `user_coupons.md` |
| banners | Banner 广告位管理 | `banners.md` |
| push_notifications | 推送通知记录 | `push_notifications.md` |

## 表间关系

```
campaigns (营销活动)
  ├── coupons (N:1) - 活动可关联多个优惠券
  ├── banners (1:N) - 活动可有多个Banner
  └── push_notifications (1:N) - 活动推送记录

coupons (优惠券模板)
  └── user_coupons (1:N) - 用户领取记录

user_coupons
  └── 关联 users.user_id 和 orders.order_id
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 营销活动、促销、campaign、预算 | `campaigns.md` |
| 优惠券、折扣、满减、coupon | `coupons.md` |
| 领券、用券、券使用率、核销 | `user_coupons.md` |
| Banner、广告位、曝光、点击率、CTR | `banners.md` |
| 推送、通知、push、送达率、打开率 | `push_notifications.md` |

## 常见分析场景

1. **活动效果分析**: 加载 `campaigns.md` + `push_notifications.md`
2. **优惠券ROI分析**: 加载 `coupons.md` + `user_coupons.md`
3. **广告位效果**: 加载 `banners.md`
4. **推送效果分析**: 加载 `push_notifications.md`
5. **用户营销触达**: 加载 `user_coupons.md` + `push_notifications.md`

## 派生层附录

本域还有派生表（清洗层/汇总层/口径表/历史遗留）。**遇到同名近义表拿不准选哪张时，读 `_index.derived.md`**。
