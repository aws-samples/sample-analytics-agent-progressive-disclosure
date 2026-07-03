# banners - Banner 广告位表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| banner_id | INT | Banner ID，主键，自增 |
| position | VARCHAR(50) | 展示位置 |
| banner_name | VARCHAR(200) | Banner 名称 |
| image_url | VARCHAR(500) | 图片URL |
| target_url | VARCHAR(500) | 点击跳转URL |
| target_type | VARCHAR(30) | 跳转类型 |
| target_id | VARCHAR(50) | 跳转目标ID |
| sort_order | INT | 排序权重（数字越小越靠前） |
| start_date | TIMESTAMP | 展示开始时间 |
| end_date | TIMESTAMP | 展示结束时间 |
| is_active | BOOLEAN | 是否启用 |
| click_count | BIGINT | 点击次数 |
| impression_count | BIGINT | 曝光次数 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### position 展示位置
| 值 | 说明 |
|----|------|
| home_top | 首页顶部轮播 |
| home_middle | 首页中部推荐位 |
| category_top | 分类页顶部 |
| detail_bottom | 商品详情页底部 |

### target_type 跳转类型
| 值 | 说明 |
|----|------|
| product | 跳转到商品详情页 |
| category | 跳转到分类页 |
| campaign | 跳转到活动页 |
| external | 跳转到外部链接 |
| deeplink | 深度链接（APP内跳转） |

## 索引

- PRIMARY KEY: `banner_id`
- INDEX: `position`, `is_active`, `start_date`, `end_date`
- INDEX: `sort_order`

## 常用查询

### 各位置 Banner CTR 分析
```sql
SELECT
    position,
    banner_name,
    impression_count,
    click_count,
    ROUND(click_count * 100.0 / NULLIF(impression_count, 0), 2) AS ctr_percent
FROM banners
WHERE is_active = true
  AND start_date <= CURRENT_TIMESTAMP
  AND end_date >= CURRENT_TIMESTAMP
ORDER BY position, ctr_percent DESC;
```

### Banner 效果排行榜
```sql
SELECT
    banner_id,
    banner_name,
    position,
    target_type,
    impression_count,
    click_count,
    ROUND(click_count * 100.0 / NULLIF(impression_count, 0), 2) AS ctr_percent
FROM banners
WHERE impression_count >= 1000
ORDER BY ctr_percent DESC
LIMIT 20;
```

### 各位置曝光量统计
```sql
SELECT
    position,
    COUNT(*) AS banner_count,
    SUM(impression_count) AS total_impressions,
    SUM(click_count) AS total_clicks,
    ROUND(SUM(click_count) * 100.0 / NULLIF(SUM(impression_count), 0), 2) AS avg_ctr
FROM banners
WHERE start_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY position
ORDER BY total_impressions DESC;
```
