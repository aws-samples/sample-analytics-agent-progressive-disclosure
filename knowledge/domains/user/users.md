# users - 用户主表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 主键，用户唯一标识 |
| username | VARCHAR(50) | 用户名 |
| email | VARCHAR(100) | 邮箱地址 |
| phone | VARCHAR(20) | 手机号（脱敏存储） |
| registered_at | TIMESTAMP | 注册时间 |
| registration_source | VARCHAR(50) | 注册来源 |
| status | VARCHAR(20) | 账号状态 |
| user_level | INT | 用户等级（1-5） |
| is_vip | BOOLEAN | 是否VIP会员 |
| last_active_at | TIMESTAMP | 最后活跃时间 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### registration_source 注册来源
| 值 | 说明 |
|----|------|
| app | iOS/Android 原生APP |
| web | PC网站 |
| mini_program | 微信/支付宝小程序 |
| h5 | 移动端H5页面 |

### status 账号状态
| 值 | 说明 |
|----|------|
| active | 正常 |
| inactive | 未激活 |
| banned | 封禁 |

### user_level 用户等级
| 值 | 说明 | 条件 |
|----|------|------|
| 1 | 新用户 | 注册<30天 |
| 2 | 普通用户 | 默认 |
| 3 | 活跃用户 | 月活跃>=10天 |
| 4 | 高价值用户 | 累计消费>=1000元 |
| 5 | 超级用户 | 累计消费>=10000元 或 VIP |

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
WHERE registered_at >= CURRENT_DATE - INTERVAL '30 days'
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
WHERE last_active_at >= CURRENT_DATE - INTERVAL '7 days'
  AND status = 'active';
```
