# event_definitions - 事件定义表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| event_name | VARCHAR(50) | 主键，事件名称 |
| event_category | VARCHAR(30) | 事件分类 |
| description | TEXT | 事件描述 |
| properties_schema | `string` | 事件属性 schema 定义。**Iceberg 里是 JSON 文本（string），不是 Postgres 的 JSONB**：取值用 `json_extract_scalar(...)`，`->` / `->>` 是语法错 |
| owner | VARCHAR(50) | 负责人 |
| is_core_event | BOOLEAN | 是否核心事件 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### event_category 事件分类
| 值 | 说明 | 典型事件 | 实测行数 |
|----|------|----------|------|
| commerce | 交易类 | add_favorite, add_to_cart, begin_checkout, purchase, remove_from_cart, use_coupon, view_category, view_product | 8 |
| engagement | 互动类 | app_close, app_open, click_banner, click_push, edit_profile, search, view_home, view_profile | 8 |
| social | 社交类 | comment_post, follow_user, like_post, share, view_post | 5 |
| system | 系统类 | login, logout, receive_push, register | 4 |

> 全表 25 行，只有这 4 类，「典型事件」列就是**全部**归属事件（实测拉取）。
> 旧文档写的 `acquisition` / `conversion` / `retention` / `revenue` **都不存在**——
> 交易类的值是 `commerce`，登录注册归在 `system`，`app_open`/`app_close` 归在
> `engagement` 而不是留存类。注意 `share` 归 `social`、`add_favorite` 归 `commerce`，
> 别按直觉猜。

### is_core_event 核心事件说明
25 个事件里 **11 个是核心事件**（`is_core_event = TRUE`，实测全量）：

| 事件名 | 说明 |
|--------|------|
| app_open | APP 打开 |
| app_close | APP 关闭 |
| login | 用户登录 |
| register | 用户注册 |
| search | 搜索 |
| view_home | 浏览首页 |
| view_product | 商品详情页浏览 |
| view_post | 查看帖子 |
| add_to_cart | 加入购物车 |
| begin_checkout | 发起结算 |
| purchase | 完成购买 |

> 旧文档在这里列了 `page_view` / `product_view` / `checkout` —— 前者不是本表的事件
> （页面浏览在 `page_views` 表），后两个名字写错了。真名见 `events.md` 的枚举表。

### properties_schema 事件属性定义

> ⚠️ **这一列在种子数据里 25 行全为 NULL**，没有属性契约可查。想知道某个事件带哪些
> 属性，只能直接看 `events.properties`（JSON 字符串，用
> `json_extract_scalar(properties, '$.keyword')` 这类写法取值）。
> `description` 列 25 行都有值，是这张表唯一能用的说明来源。

## 索引

- PRIMARY KEY: `event_name`
- INDEX: `event_category`, `is_core_event`

## 常用查询

### 查看所有核心事件
```sql
SELECT
    event_name,
    event_category,
    description
FROM event_definitions
WHERE is_core_event = TRUE
ORDER BY event_category, event_name;
```

### 按分类统计事件数
```sql
SELECT
    event_category,
    COUNT(*) AS event_count,
    SUM(CASE WHEN is_core_event THEN 1 ELSE 0 END) AS core_count
FROM event_definitions
GROUP BY event_category
ORDER BY event_count DESC;
```

### 查看事件说明
```sql
-- properties_schema 整列为 NULL，只有 description 有内容
SELECT
    event_name,
    description
FROM event_definitions
WHERE event_name IN ('view_product', 'add_to_cart', 'purchase');
```
