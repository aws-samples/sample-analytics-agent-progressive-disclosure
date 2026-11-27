# 用户域 (User Domain)

## 域概述

用户域是整个分析系统的核心，包含用户基础信息、画像属性、设备绑定和用户分群等数据。支撑用户增长分析、用户画像构建、精准营销等关键业务场景。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| users | 用户主表，基础信息 | `users.md` |
| user_profiles | 用户画像，人口属性 | `user_profiles.md` |
| user_devices | 用户设备绑定 | `user_devices.md` |
| user_segments | 用户分群定义 | `user_segments.md` |
| user_segment_members | 分群成员关系 | `user_segment_members.md` |

## 表间关系

```
users (主表)
  ├── user_profiles (1:1)
  ├── user_devices (1:N)
  └── user_segment_members (1:N) ── user_segments
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 注册、登录、状态、等级、VIP | `users.md` |
| 年龄、性别、城市、兴趣、画像 | `user_profiles.md` |
| 设备、手机、iOS、Android、APP版本 | `user_devices.md` |
| 分群、人群包、圈选 | `user_segments.md` + `user_segment_members.md` |
| 获客渠道、投放渠道、归因渠道（按渠道看用户数、人均消费、CAC） | 不在本域：`domains/attribution/user_attributions.md` + `domains/attribution/channels.md`，或 `domains/mart/mart_user_summary.md` 的 `register_channel`（last_touch 归因渠道）。`users.registration_source` 是注册入口（应用商店、小程序等），**不是**获客渠道 |
| 实验分组、分进实验、对照组 / 实验组的人数 | 不在本域：`domains/experiment/ab_test_assignments.md`。`user_segment_members` 是按规则圈的人群分群，和 A/B 实验分组不是一回事 |
| 注册后 N 天内完成首单（首单率）、首单日期、复购 | 不在本域：`domains/mart/mart_user_summary.md`（`first_paid_date` 是首笔有效订单的日期，按日期算「注册当天起 N 天」；口径已冻结，不要自己 join orders 推） |
| 分组、获分组、分到哪个组 | 本项目里「分组」指 A/B 实验的分流，在 `domains/experiment/ab_test_assignments.md`；按规则圈人叫「分群」，才是本域的 `user_segments` / `user_segment_members` |

## 常见分析场景

1. **新用户分析**: 加载 `users.md`
2. **用户画像分布**: 加载 `users.md` + `user_profiles.md`
3. **设备渗透率**: 加载 `user_devices.md`
4. **分群效果分析**: 加载 `user_segments.md` + `user_segment_members.md`

## 派生层附录

本域还有派生表（清洗层/汇总层/口径表/历史遗留）。**遇到同名近义表拿不准选哪张时，读 `_index.derived.md`**。
