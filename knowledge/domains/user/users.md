# users - 用户主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 主键，用户唯一标识 |
| username | VARCHAR(50) | 用户名 |
| email | VARCHAR(100) | 邮箱地址 |
| phone | VARCHAR(20) | 手机号，**明文 11 位**（500/500 行都是）；不在授权面里，见下 |
| registered_at | TIMESTAMP | 注册时间 |
| registration_source | VARCHAR(50) | 注册来源 |
| status | VARCHAR(20) | 账号状态 |
| user_level | INT | 用户等级（1-5） |
| is_vip | BOOLEAN | 是否VIP会员 |
| last_active_at | TIMESTAMP | 最后活跃时间 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

> **`email` / `phone` 查不到。** 这两列在表里，但不在 agent 角色的授权面里
> （Lake Formation 列级排除，见 `scripts/lakehouse/governance.py`）：`SELECT *`
> 的结果里没有它们，点名 `SELECT email` 直接报 `COLUMN_NOT_FOUND`。要按人群
> 分组就用 `user_level` / `is_vip` / `registration_source`，要联系方式没有替代——
> **这是治理上的硬约束，不是提示词里的建议，绕不过去。**
>
> 注意这**不是脱敏**：库里存的是明文邮箱和明文 11 位手机号，Lake Formation 也没有
> 值级掩码这个原语（见 `governance.py` 的「一处能力下降」）。边界全在"这两列不在
> 授权面里"这一件事上，所以别把 `phone` 当成"反正是脱敏过的"而写进任何输出。

## 字段枚举值

### registration_source 注册来源
| 值 | 说明 | 实测行数 |
|----|------|------|
| referral | 老用户推荐 | 73 |
| organic | 自然流量 | 68 |
| huawei_store | 华为应用市场 | 65 |
| web | PC网站 | 62 |
| ad_campaign | 广告投放 | 61 |
| wechat_mini | 微信小程序 | 59 |
| app_store | App Store（iOS） | 57 |
| google_play | Google Play | 55 |

> 只有这 8 个值。**没有** `app` / `mini_program` / `h5`——`database/01_user_domain.sql`
> 的行内注释写的是旧枚举，照它写 `WHERE registration_source='app'` 会拿到空集
> （不报错的空结果）。取值由 `scripts/lakehouse/verify_enums.py` 对着数据校准。

### status 账号状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 正常 | 376 |
| inactive | 未激活 | 68 |
| deleted | 已注销（软删除） | 30 |
| suspended | 已封禁/冻结 | 26 |

> 封禁态的值是 `suspended`，**不是** `banned`。

### user_level 用户等级
| 值 | 说明 | 条件 |
|----|------|------|
| 1 | 新用户 | 注册<30天 |
| 2 | 普通用户 | 默认 |
| 3 | 活跃用户 | 近30天活跃>=10天 |
| 4 | 高价值用户 | 累计消费>=1000元（有效订单口径） |
| 5 | 超级用户 | 累计消费>=10000元 或 VIP |

条件按 **5 > 4 > 3 > 1 > 2** 的优先级取最高档：一个注册 20 天、已消费 2000 元的
用户是等级 4 不是等级 1。这是**业务上的等级定义**。

> ⚠️ **但在当前种子数据上这条规则不成立**：`user_level` 是独立生成的标签列，
> 实测各等级的平均消费都在 1.4-1.9 万之间、消费≥1000 的比例都在 82-91%，
> 等级与消费额几乎不相关（实测分布：1 → 184 人、2 → 170、3 → 83、4 → 45、5 → 18）。
> **可以用 `user_level` 筛人群，但不要把它当「高消费」的代理，也不要用它验证消费口径**；
> 要按消费分层就从 `orders` 现算。实测细节见 `docs/data-walkthrough.md` 第 3.2 步。

## 索引

- PRIMARY KEY: `user_id`
- INDEX: `registered_at`, `status`, `registration_source`

## 常用查询

### 新用户注册趋势（按来源）
```sql
SELECT
    DATE(registered_at) AS reg_date,
    registration_source,
    COUNT(*) AS new_users
FROM users
WHERE registered_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(registered_at), registration_source
ORDER BY reg_date DESC;
```

### 用户等级分布
```sql
SELECT
    user_level,
    COUNT(*) AS user_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM users
WHERE status = 'active'
GROUP BY user_level
ORDER BY user_level;
```

### 活跃用户数（近7天有登录）
```sql
SELECT COUNT(*) AS active_users
FROM users
WHERE last_active_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
  AND status = 'active';
```
