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

## 字段枚举值

### creative_type 素材类型
| 值 | 说明 | 实测行数 |
|----|------|------|
| image | 静态图片 | 49 |
| video | 视频素材 | 48 |
| carousel | 轮播图/多图 | 47 |

> **没有** `playable`（旧文档写过）。

### creative_format 素材格式

> ⚠️ **这一列在种子数据里整列为 NULL**（144 行全空），按它分组只会得到一个 NULL 桶。
> 旧文档列过 `banner` / `interstitial` / `native` / `rewarded`，数据里一个都没有。
> 要区分素材形态就用 `creative_type`。

### call_to_action 行动号召
| 值 | 说明 | 实测行数 |
|----|------|------|
| 马上抢购 | 促销导向 | 42 |
| 限时优惠 | 促销导向 | 39 |
| 立即下载 | 拉新装机 | 32 |
| 查看详情 | 内容导向 | 31 |

> **是中文文案**，不是 `download` / `shop_now` 那套英文键（旧文档写过）。

### status 素材状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 投放中 | 144 |

> ⚠️ 144 行**全是 `active`**，这一列在这份数据上没有区分度。业务上的
> `draft` / `paused` / `archived` 一条都没有——按它们筛是空集。

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
