# ad_creatives - 广告素材表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| creative_id | INT | 主键，素材唯一标识，自增 |
| ad_campaign_id | INT | 所属广告活动ID，关联 ad_campaigns.ad_campaign_id |
| creative_name | VARCHAR(200) | 素材名称 |
| creative_type | VARCHAR(30) | 素材类型 |
| creative_format | VARCHAR(30) | 素材格式 |
| content_url | VARCHAR(500) | 素材内容URL |
| headline | VARCHAR(200) | 标题文案 |
| description | TEXT | 描述文案 |
| call_to_action | VARCHAR(50) | 行动号召按钮 |
| status | VARCHAR(20) | 素材状态 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

## 字段枚举值

### creative_type 素材类型
| 值 | 说明 |
|----|------|
| image | 静态图片 |
| video | 视频素材 |
| carousel | 轮播图/多图 |
| playable | 可试玩广告 |

### creative_format 素材格式
| 值 | 说明 |
|----|------|
| banner | 横幅广告（常见尺寸 320x50, 728x90） |
| interstitial | 插屏广告（全屏展示） |
| native | 原生广告（融入内容流） |
| rewarded | 激励视频（看完获得奖励） |

### call_to_action 行动号召
| 值 | 说明 |
|----|------|
| download | 立即下载 |
| learn_more | 了解更多 |
| shop_now | 立即购买 |
| sign_up | 立即注册 |
| play_now | 立即体验 |

### status 素材状态
| 值 | 说明 |
|----|------|
| draft | 草稿 |
| active | 投放中 |
| paused | 已暂停 |
| archived | 已归档 |

## 索引

- PRIMARY KEY: `creative_id`
- INDEX: `ad_campaign_id`, `creative_type`, `status`

## 常用查询

### 各活动下的素材列表
```sql
SELECT
    ac.campaign_name,
    cr.creative_name,
    cr.creative_type,
    cr.creative_format,
    cr.headline,
    cr.status
FROM ad_creatives cr
JOIN ad_campaigns ac ON cr.ad_campaign_id = ac.ad_campaign_id
WHERE ac.status = 'active'
ORDER BY ac.campaign_name, cr.creative_name;
```

### 素材类型分布
```sql
SELECT
    creative_type,
    creative_format,
    COUNT(*) AS creative_count
FROM ad_creatives
WHERE status = 'active'
GROUP BY creative_type, creative_format
ORDER BY creative_count DESC;
```

### 各行动号召使用频率
```sql
SELECT
    call_to_action,
    COUNT(*) AS usage_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM ad_creatives
WHERE status IN ('active', 'paused')
GROUP BY call_to_action
ORDER BY usage_count DESC;
```
