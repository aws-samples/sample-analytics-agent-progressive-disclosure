# user_devices - 用户设备表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| device_id | VARCHAR(100) | 主键，设备唯一标识 |
| user_id | BIGINT | 关联 users.user_id |
| device_type | VARCHAR(20) | 设备类型 |
| os_version | VARCHAR(20) | 操作系统版本 |
| device_model | VARCHAR(50) | 设备型号 |
| device_brand | VARCHAR(50) | 设备品牌 |
| app_version | VARCHAR(20) | APP版本号 |
| push_token | VARCHAR(200) | 推送令牌 |
| is_primary | BOOLEAN | 是否主设备，默认 false |
| first_seen_at | TIMESTAMP | 首次出现时间 |
| last_seen_at | TIMESTAMP | 最后活跃时间 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### device_type 设备类型
| 值 | 说明 |
|----|------|
| ios | iPhone/iPad |
| android | Android手机/平板 |
| web | 网页浏览器 |
| mini_program | 小程序 |

### 常见 device_brand
```
Apple, Samsung, Huawei, Xiaomi, OPPO, vivo, OnePlus, Realme
```

## 索引

- PRIMARY KEY: `device_id`
- INDEX: `user_id`

## 常用查询

### 设备类型分布
```sql
SELECT
    device_type,
    COUNT(DISTINCT user_id) AS user_count,
    COUNT(*) AS device_count,
    ROUND(100.0 * COUNT(DISTINCT user_id) / SUM(COUNT(DISTINCT user_id)) OVER(), 2) AS percentage
FROM user_devices
GROUP BY device_type
ORDER BY user_count DESC;
```

### 操作系统版本分布
```sql
SELECT
    device_type,
    os_version,
    COUNT(DISTINCT user_id) AS user_count
FROM user_devices
WHERE device_type IN ('ios', 'android')
GROUP BY device_type, os_version
ORDER BY device_type, user_count DESC
LIMIT 20;
```

### APP版本分布
```sql
SELECT
    app_version,
    COUNT(DISTINCT user_id) AS user_count
FROM user_devices
WHERE device_type IN ('ios', 'android')
GROUP BY app_version
ORDER BY app_version DESC;
```

### 多设备用户统计
```sql
SELECT
    device_count,
    COUNT(*) AS user_count
FROM (
    SELECT user_id, COUNT(DISTINCT device_id) AS device_count
    FROM user_devices
    GROUP BY user_id
) t
GROUP BY device_count
ORDER BY device_count;
```

### 主设备分布
```sql
SELECT
    device_type,
    COUNT(*) AS primary_device_count
FROM user_devices
WHERE is_primary = true
GROUP BY device_type
ORDER BY primary_device_count DESC;
```
