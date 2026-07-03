# 社交域 (Social Domain)

## 域概述

社交域管理APP内的社交功能数据，包括用户发布的内容（帖子/种草笔记）、社交关系（关注/粉丝）、互动行为（点赞/评论/分享）和私信消息。支撑内容运营、KOL/KOC识别、社交裂变分析等关键业务场景。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| posts | 内容帖子表 | `posts.md` |
| user_follows | 用户关注关系表 | `user_follows.md` |
| post_likes | 帖子点赞表 | `post_likes.md` |
| post_comments | 帖子评论表 | `post_comments.md` |
| post_shares | 帖子分享表 | `post_shares.md` |
| user_messages | 用户私信表 | `user_messages.md` |

## 表间关系

```
posts (内容主表)
  ├── post_likes (1:N) ── users
  ├── post_comments (1:N) ── users
  └── post_shares (1:N) ── users

users (用户)
  ├── posts (1:N)
  ├── user_follows (1:N, 双向关系)
  └── user_messages (1:N, 收发双方)
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 帖子、内容、种草、笔记、发布、曝光、浏览量 | `posts.md` |
| 关注、粉丝、KOL、KOC、大V | `user_follows.md` |
| 点赞、喜欢、收藏 | `post_likes.md` |
| 评论、回复、互动 | `post_comments.md` |
| 分享、转发、传播、裂变 | `post_shares.md` |
| 私信、消息、聊天、会话 | `user_messages.md` |

## 常见分析场景

1. **内容运营分析**: 加载 `posts.md`
2. **KOL/KOC识别**: 加载 `posts.md` + `user_follows.md`
3. **互动效果分析**: 加载 `posts.md` + `post_likes.md` + `post_comments.md`
4. **社交裂变分析**: 加载 `post_shares.md`
5. **私信运营分析**: 加载 `user_messages.md`

## 核心指标公式

- **互动率** = (like_count + comment_count + share_count) / view_count
- **点赞率** = like_count / view_count
- **评论率** = comment_count / view_count
- **分享率** = share_count / view_count
