# 归因域 (Attribution Domain)

## 域概述

归因域管理用户获取归因相关数据，追踪用户从广告点击到安装的完整路径。该域支撑市场团队分析各渠道的获客效果、ROI 计算和广告投放优化等关键业务场景。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| channels | 渠道定义表 | `channels.md` |
| ad_campaigns | 广告活动表 | `ad_campaigns.md` |
| ad_creatives | 广告素材表 | `ad_creatives.md` |
| user_attributions | 用户归因表 | `user_attributions.md` |
| channel_daily_costs | 渠道日成本表 | `channel_daily_costs.md` |

## 表间关系

```
channels (渠道)
  └── ad_campaigns (1:N) (广告活动)
        └── ad_creatives (1:N) (广告素材)

channels ─┬── user_attributions (1:N) ── users (来自 user 域)
          └── channel_daily_costs (1:N)

ad_campaigns ─┬── user_attributions (1:N)
              └── channel_daily_costs (1:N)

ad_creatives ─┬── user_attributions (1:N)
              └── channel_daily_costs (1:N)
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 渠道、平台、Google、Facebook、TikTok | `channels.md` |
| 广告活动、Campaign、预算、投放目标 | `ad_campaigns.md` |
| 素材、创意、banner、视频、轮播 | `ad_creatives.md` |
| 归因、首次触点、末次触点、点击到安装 | `user_attributions.md` |
| 成本、花费、CPI、曝光、点击、转化 | `channel_daily_costs.md` |
| ROI、获客成本 | `channel_daily_costs.md` + `user_attributions.md` |

## 常见分析场景

1. **渠道效果分析**: 加载 `channels.md` + `channel_daily_costs.md`
2. **广告 ROI 计算**: 加载 `channel_daily_costs.md` + `user_attributions.md`
3. **素材 A/B 测试**: 加载 `ad_creatives.md` + `channel_daily_costs.md`
4. **用户获取路径**: 加载 `user_attributions.md`
5. **预算消耗监控**: 加载 `ad_campaigns.md` + `channel_daily_costs.md`
