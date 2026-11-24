# user_segment_members - 分群成员关系表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 联合主键，用户ID |
| segment_id | INT | 联合主键，分群ID |
| entered_at | TIMESTAMP | 进入分群时间 |
| exited_at | TIMESTAMP | 退出分群时间（NULL=仍在分群） |

## 成员是怎么来的（按 `user_segments.rules_json` 从事实推导）

每个 (用户, 分群) 至多一行。`entered_at` = 规则开始成立的时刻，`exited_at` = 规则不再成立的时刻，
**当前成员 = `exited_at IS NULL`**。规则逐条可以用别的表复核（重灌后实测，全量 755,163 行）：

| segment_id | 分群 | 成员判定 | entered_at / exited_at | 历史行数 | 当前成员 |
|---|---|---|---|---|---|
| 1 | 高价值用户 | 累计有效订单 `actual_amount` ≥ 5000 | 越过 5000 那一单的 `paid_at`；不退出 | 5,446 | 5,446 |
| 2 | 新注册用户 | 注册 ≤ 30 天 | 注册时刻；注册 + 30 天退出 | 213,535 | 81,456 |
| 3 | 沉睡用户 | `users.last_active_at` 距锚点 ≥ 30 天 | 最后活跃 + 30 天；不退出 | 42,256 | 42,256 |
| 4 | VIP会员 | `users.is_vip` | 注册时刻 | 25,577 | 25,577 |
| 5 | 年轻用户 | `user_profiles.age` 18–25 | 注册时刻 | 39,722 | 39,722 |
| 6 | 一线城市用户 | `user_profiles.city` ∈ 北京/上海/广州/深圳 | 注册时刻 | 91,426 | 91,426 |
| 7 | 高活跃用户 | 某个 7 天桶（从数据窗起点每 7 天一桶）会话 ≥ 5 次 | 首个达标桶的第 5 次会话；最后达标桶结束时退出 | 93,253 | 17,910 |
| 8 | 购物车用户 | 最后一次 `add_to_cart` 之前 30 天内无有效支付 | 那次加购；其后第一笔有效支付时退出 | 129,069 | 70,895 |
| 9 | iOS用户 | 拥有 `device_type='ios'` 的设备 | 最早那台 iOS 设备的 `first_seen_at` | 88,910 | 88,910 |
| 10 | 推广注册用户 | `users.registration_source = 'ad_campaign'` | 注册时刻 | 25,969 | 25,969 |

> 所以「高价值用户的客单价」「iOS 用户的转化」这类问题，按分群取人和按规则现算会得到同一批人。
> 不加 `exited_at IS NULL` 数的是**历史上进过**这个分群的人次（分群 2 / 7 / 8 差别很大）。
> （2026-11 重灌前成员是随机抽的、与规则无关，旧数字作废。）

## 索引

- PRIMARY KEY: `(user_id, segment_id)`
- INDEX: `segment_id`, `entered_at`

## 关联关系

- `user_id` → `users.user_id`
- `segment_id` → `user_segments.segment_id`

## 常用查询

### 分群人数统计
```sql
SELECT
    s.segment_id,
    s.segment_name,
    COUNT(m.user_id) AS current_members
FROM user_segments s
LEFT JOIN user_segment_members m
    ON s.segment_id = m.segment_id
    AND m.exited_at IS NULL
WHERE s.status = 'active'
GROUP BY s.segment_id, s.segment_name
ORDER BY current_members DESC;
```

### 用户所属分群
```sql
SELECT
    s.segment_name,
    m.entered_at
FROM user_segment_members m
JOIN user_segments s ON m.segment_id = s.segment_id
WHERE m.user_id = ?
  AND m.exited_at IS NULL
  AND s.status = 'active';
```

### 分群成员变化趋势
```sql
SELECT
    DATE(entered_at) AS date,
    COUNT(CASE WHEN exited_at IS NULL THEN 1 END) AS entered,
    COUNT(CASE WHEN exited_at IS NOT NULL THEN 1 END) AS exited
FROM user_segment_members
WHERE segment_id = ?
  AND entered_at >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '29' day AND entered_at < (SELECT max(as_of_date) FROM meta_snapshot) + interval '1' day
GROUP BY DATE(entered_at)
ORDER BY date;
```

### 分群交叉分析
```sql
SELECT
    s1.segment_name AS segment_a,
    s2.segment_name AS segment_b,
    COUNT(DISTINCT m1.user_id) AS overlap_count
FROM user_segment_members m1
JOIN user_segment_members m2
    ON m1.user_id = m2.user_id
    AND m1.segment_id < m2.segment_id
JOIN user_segments s1 ON m1.segment_id = s1.segment_id
JOIN user_segments s2 ON m2.segment_id = s2.segment_id
WHERE m1.exited_at IS NULL
  AND m2.exited_at IS NULL
GROUP BY s1.segment_name, s2.segment_name
ORDER BY overlap_count DESC
LIMIT 10;
```
