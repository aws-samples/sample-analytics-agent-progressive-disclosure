# channels - 渠道表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| channel_id | INT | 主键，渠道唯一标识，自增 |
| channel_name | VARCHAR(100) | 渠道名称 |
| channel_type | VARCHAR(30) | 渠道类型 |
| platform | VARCHAR(50) | 投放平台 |
| description | TEXT | 渠道描述 |
| is_active | BOOLEAN | 是否启用 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### channel_type 渠道类型
| 值 | 说明 | 实测行数 |
|----|------|------|
| paid | 付费广告渠道 | 6 |
| kol | 达人/KOL 合作 | 3 |
| organic | 自然流量（ASO、SEO） | 3 |
| referral | 用户推荐（邀请好友） | 1 |
| direct | 直接访问 | 1 |

> 全表 14 行。旧文档写的 `social` 不存在——社交类渠道被归到 `kol`（小红书种草、
> B站UP主之类）；另外多了 `direct`。**只有 `paid` 和 `kol` 的渠道有投放成本**
> （`channel_daily_costs`），所以 CAC / ROI 只在这些渠道上算得出来。

### platform 投放平台

> 14 行 11 种取值，这一列区分度接近主键，**当维度用而不是当枚举筛**：
> `xiaohongshu`(2)、`douyin`(2)、`weixin`(2)，以及 `baidu` / `tencent` / `bilibili` /
> `kuaishou` / `apple` / `weibo` / `app` / `direct`（各 1）。
> **是国内平台**，不是 `google` / `facebook` / `tiktok` 那套（旧文档写过）。
> 分渠道分析用 `channel_id` 或 `channel_name`。

## 索引

- PRIMARY KEY: `channel_id`
- INDEX: `channel_type`, `platform`, `is_active`

## 常用查询

### 获取所有活跃付费渠道
```sql
SELECT
    channel_id,
    channel_name,
    platform
FROM channels
WHERE channel_type = 'paid'
  AND is_active = TRUE
ORDER BY channel_name;
```

### 各渠道类型分布
```sql
SELECT
    channel_type,
    COUNT(*) AS channel_count,
    SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_count
FROM channels
GROUP BY channel_type
ORDER BY channel_count DESC;
```

### 按平台统计渠道数量
```sql
SELECT
    platform,
    COUNT(*) AS channel_count
FROM channels
WHERE is_active = TRUE
GROUP BY platform
ORDER BY channel_count DESC;
```
