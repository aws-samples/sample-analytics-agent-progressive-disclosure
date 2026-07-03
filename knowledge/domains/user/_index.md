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

## 常见分析场景

1. **新用户分析**: 加载 `users.md`
2. **用户画像分布**: 加载 `users.md` + `user_profiles.md`
3. **设备渗透率**: 加载 `user_devices.md`
4. **分群效果分析**: 加载 `user_segments.md` + `user_segment_members.md`
