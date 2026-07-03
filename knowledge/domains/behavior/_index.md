# 行为域 (Behavior Domain)

## 域概述

Behavior 域记录用户在APP内的所有行为轨迹，包括会话、页面浏览和自定义事件。这是分析系统最核心的数据域，支撑用户行为分析、漏斗转化、留存分析、归因分析等关键场景。数据量通常最大，需要合理的分区策略。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| event_definitions | 事件元数据定义 | `event_definitions.md` |
| sessions | 用户会话记录 | `sessions.md` |
| page_views | 页面浏览明细 | `page_views.md` |
| events | 事件明细表 | `events.md` |

## 表间关系

```
event_definitions (元数据)
      │
      ↓ (event_name 关联)
   events ──────────────────┐
      │                     │
      │ (session_id)        │ (user_id)
      ↓                     ↓
  sessions ←── page_views   users (用户域)
      │            │
      └────────────┴──→ user_id
```

**关系说明：**
- `sessions` 1:N `events`: 一个会话包含多个事件
- `sessions` 1:N `page_views`: 一个会话包含多个页面浏览
- `events.event_name` → `event_definitions.event_name`: 事件定义引用

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 事件定义、事件分类、核心事件、事件schema | `event_definitions.md` |
| 会话、session、访问时长、跳出率、入口页、退出页 | `sessions.md` |
| 页面浏览、PV、停留时长、滚动深度 | `page_views.md` |
| 事件、行为、点击、转化、漏斗、埋点 | `events.md` |
| 流量来源、UTM、渠道归因 | `sessions.md` |

## 常见分析场景

1. **事件埋点管理**: 加载 `event_definitions.md`
2. **会话分析/流量分析**: 加载 `sessions.md`
3. **页面浏览分析**: 加载 `page_views.md`
4. **漏斗转化分析**: 加载 `events.md`
5. **用户行为路径**: 加载 `sessions.md` + `page_views.md` + `events.md`
6. **渠道归因分析**: 加载 `sessions.md` + `events.md`
