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
| is_primary | BOOLEAN | 是否主设备：**每个用户恰好一台**为 true |
| first_seen_at | TIMESTAMP | 首次出现时间：= 这台设备上最早一次会话的开始；从没开过会话的设备在注册之后 |
| last_seen_at | TIMESTAMP | 最后活跃时间：= 这台设备上最晚一次会话的结束 |
| created_at | TIMESTAMP | 记录创建时间 |

> **归属关系（2026-11 重灌后）**：每个用户至少一台设备（213,535 人 / 317,740 台）、恰好一台主设备；
> `sessions.device_id` / `events.device_id` 都能 JOIN 回这张表，且设备的 `user_id` 等于会话 / 事件的用户。
> 注册那次会话在主设备上，95.4% 的会话在主设备上。所以「按设备类型 / 品牌看活跃、看转化」
> 可以 `sessions JOIN user_devices USING(device_id)`；按「用户的主设备」归一个人到一种设备
> 用 `WHERE is_primary`。（重灌前 `device_id` 是按 user_id 编码的、设备表的 user_id 另行随机，
> 两边 JOIN 出来的人对不上，旧结论作废。）

## 字段枚举值

### device_type 设备类型
| 值 | 说明 |
|----|------|
| ios | iPhone/iPad |
| android | Android手机/平板 |
| web | 网页浏览器 |
| mini_program | 小程序 |

### device_brand 设备品牌
| 值 | 说明 | 实测行数 |
|----|------|------|
| Apple | 苹果，**唯一**的 ios 品牌 | 104,796 |
| Browser | 浏览器，**唯一**的 web 品牌（不是厂商名） | 35,651 |
| 华为 |  | 19,897 |
| OPPO |  | 19,963 |
| 荣耀 |  | 20,056 |
| 小米 |  | 19,908 |
| realme | 注意**小写** | 19,690 |
| 一加 |  | 19,761 |
| WeChat | 微信，**唯一**的 mini_program 品牌（不是厂商名） | 18,752 |
| 三星 |  | 19,681 |
| vivo |  | 19,585 |

> **牌名以中文入库**（`华为` / `小米` / `三星` / `一加` / `荣耀`），只有 `Apple`、`OPPO`、
> `vivo`、`realme` 是拉丁字母，且 `realme` 小写。写 `WHERE device_brand = 'Huawei'`
> 会**返回 0 行且不报错**——这一列以前在卡片里被译成了英文（`Samsung, Huawei, Xiaomi,
> OnePlus, Realme`），11 个取值里只有 3 个对得上，2026-08-28 按库里实测改回来。
>
> `Browser` / `WeChat` 是**设备类型的占位品牌**，不是手机厂商：web 端只有浏览器、
> 小程序跑在宿主 APP 里，本来就没有机型。要按厂商分析必须
> `WHERE device_type = 'android'`，否则 Apple(104,796) + Browser(35,651) + WeChat(18,752) = 159,199 行
> （占全表 50.1%）会混进"品牌分布"里。
>
> 品牌与 `device_type` / `device_model` **同源**，不会出现
> `device_brand = 'Apple'` 配 `device_model = 'Redmi K70'` 这种组合。

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
    100.0 * COUNT(DISTINCT user_id) / SUM(COUNT(DISTINCT user_id)) OVER() AS percentage
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
