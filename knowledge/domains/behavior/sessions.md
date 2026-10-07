# sessions - 会话表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| session_id | BIGINT | 主键，会话ID |
| user_id | BIGINT | 用户ID（未登录为NULL） |
| device_id | VARCHAR(64) | 设备ID |
| start_time | TIMESTAMP | 会话开始时间 |
| end_time | TIMESTAMP | 会话结束时间 |
| duration_seconds | INT | 会话时长（秒） |
| event_count | INT | 会话内事件数 |
| page_view_count | INT | 会话内页面浏览数 |
| is_bounce | BOOLEAN | 是否跳出（仅1个页面） |
| entry_page | VARCHAR(100) | 入口页面 |
| exit_page | VARCHAR(100) | 退出页面 |
| traffic_source | VARCHAR(50) | 流量来源 |
| utm_source | VARCHAR(50) | UTM来源 |
| utm_medium | VARCHAR(50) | UTM媒介 |
| utm_campaign | VARCHAR(100) | UTM活动 |
| created_at | TIMESTAMP | 记录创建时间 |

## 字段枚举值

### traffic_source 流量来源
| 值 | 说明 | 实测行数 |
|----|------|------|
| referral | 外部引荐 | 868 |
| social | 社交媒体 | 846 |
| paid | 付费流量 | 826 |
| organic | 自然流量 | 825 |
| direct | 直接访问 | 824 |
| email | 邮件营销 | 811 |

> 全表 5,000 行，6 个值，**分布均匀**（811–868）。旧文档写的 `organic_search` /
> `paid_search` / `push` 都不存在——自然量是 `organic`、付费是 `paid`，没有细分搜索。
> 注意这套值跟 `channels.channel_type`（paid / kol / organic / referral / direct）
> 只是**部分重叠**：这里有 `social`/`email`，那边有 `kol`，两张表不能直接按值 JOIN。

### is_bounce 跳出判定规则
| 条件 | 值 |
|------|-----|
| page_view_count = 1 | TRUE |
| page_view_count > 1 | FALSE |

### utm_source 流量来源平台
| 值 | 说明 | 实测行数 |
|----|------|------|
| weixin | 微信 | 861 |
| douyin | 抖音 | 849 |
| baidu | 百度 | 835 |
| xiaohongshu | 小红书 | 832 |
| organic | 自然流量 | 816 |
| direct | 直接访问 | 807 |

> 只有这 6 个，全是国内平台。旧文档写的 `google` / `weibo` / `taobao` / `jd` /
> `facebook` / `instagram` **都不存在**，这一列也没有 NULL，也没有字符串 `'none'`。

### utm_medium 投放形式
| 值 | 说明 | 实测行数 |
|----|------|------|
| organic | 自然 | 1008 |
| push | 推送 | 1007 |
| paid | 付费 | 1007 |
| referral | 引荐 | 1004 |
| banner | 横幅 | 974 |

> 只有这 5 个。旧文档写的 `cpc` / `cpm` / `social` / `affiliate` / `display` / `video`
> **都不存在**；`email` 也不在这里（它是 `traffic_source` 的值）。

### utm_campaign 活动标记
| 值 | 说明 |
|----|------|
| 618_main_app | 618 大促主会场（App） |
| 618_yushou_kol | 618 预售 KOL 种草 |
| 618_flashsale_h5 | 618 限时秒杀（H5） |
| d11_presale | 双 11 预售 |
| d11_zhubo_live | 双 11 主播直播 |
| d11_hongbao | 双 11 红包 |
| d12_qingcang | 双 12 清仓 |
| newuser_lijin | 新客礼金 |
| newuser_0yuan_gou | 新客 0 元购 |
| winback_30d | 流失 30 天召回 |
| winback_90d_coupon | 流失 90 天召回（发券） |
| retarget_cart_7d | 7 天弃购重定向 |
| retarget_view_3d | 3 天浏览未购重定向 |
| brand_kol_xhs | 品牌小红书 KOL |
| brand_kol_dy | 品牌抖音 KOL |
| brand_zhihu_qa | 品牌知乎问答 |
| member_day_0918 | 9 月 18 日会员日 |
| member_day_1018 | 10 月 18 日会员日 |
| chunjie_nianhuo | 春节年货节 |
| wuyi_travel | 五一出行 |
| kaixue_season | 开学季 |
| mid_autumn_gift | 中秋礼盒 |
| app_download_dy | 抖音应用下载投放 |
| app_download_ks | 快手应用下载投放 |
| search_brand_baidu | 百度品牌词搜索 |
| search_generic_baidu | 百度通用词搜索 |
| wechat_moments_ad | 微信朋友圈广告 |
| wechat_mini_share | 微信小程序分享 |
| sms_recall_v3 | 短信召回（第 3 版文案） |
| push_daily_deal | 每日特价推送 |
| email_weekly_edm | 每周邮件 EDM |
| offline_qr_store | 线下门店扫码 |
| kol_livestream_0801 | 8 月 1 日 KOL 直播专场 |
| seed_user_invite | 种子用户邀请 |
| fenxiao_share | 分销分享 |
| group_buy_3ren | 三人拼团 |
| lottery_1yuan | 1 元抽奖 |
| points_mall | 积分商城 |
| vip_upgrade | VIP 升级 |

> 共 39 个，命名习惯是「活动_玩法_渠道」，非空行里**基本均匀**（每个约占 2.6%），
> 所以这一列适合按前缀切分（`618_` / `d11_` / `newuser_` / `winback_` / `retarget_` /
> `brand_` / `app_download_` / `search_`）来做活动族分析，逐个值看意义不大。
> 另有约 **14.8%** 的行是 NULL——这是本表 UTM 三列里唯一有空值的一列，做归因时记得
> `COALESCE(utm_campaign, '(未标记)')`，否则这些会话会在 GROUP BY 里悄悄变成一个空行。
> 空值是**真 NULL**，不是字符串 `'none'`，`WHERE utm_campaign IS NULL` 拦得住。

> ⚠️ **这一列是自由文本标记，不是外键。** 它跟 `campaigns.campaign_name`（中文名，
> 例如「618年中大促_帮助」）和 `ad_campaigns.campaign_name`（例如「抖音搜索-活动1」）
> 是三套互不相同的命名，按名字 JOIN **一行都匹配不到**、且不报错。要把会话和投放活动
> 关联起来，走 `user_attributions`（那张表有真的 `ad_campaign_id`）。

> 上面三列 2026-08-28 从围栏代码块改写成表格：`scripts/lakehouse/verify_enums.py` 的
> `parse_card()` 只认「`### 列名`＋`| 值 | 说明 |`」，围栏块里的声明它一律不收（那条行为
> 它自己的自测还专门断言过）。于是这三列虽然写在卡片上，却从未被任何一层比对过，
> `utm_medium` 的生成器一直在产这张表明文否认的 `cpc` / `social` / `email`。
> 声明写成看得见的形态，是这条被发现的前提。
> `utm_campaign` 那张表 2026-09-01 换过一次：原来写的是 v1 的 6 个短码
> （`618` / `new_user` / `brand_day` / `spring_sale` / `double11` / `recall`），
> 而生成器早已按 D-04（占位符与字面值）换成上面这 39 个带渠道玩法的名字，
> 两边完全不相交。这一列也是**只有卡片声明、DDL 注释里没有**的那类，所以
> `verify_ddl_comments.py` 看不见它，唯一的闸是 `verify_enums.py`。
> 这一栏不再钉逐值行数：39 个值各自的行数只是均匀抽样的回声，重灌一次就全变，
> 而「均匀」这件事本身写在上面的散文里。

## 索引

- PRIMARY KEY: `session_id`
- INDEX: `user_id`, `device_id`, `start_time`
- INDEX: `traffic_source`, `utm_source`

## 常用查询

### 日活用户及平均会话时长
```sql
SELECT
    DATE(start_time) AS date,
    COUNT(DISTINCT user_id) AS dau,
    COUNT(*) AS total_sessions,
    AVG(duration_seconds) AS avg_session_duration,
    AVG(page_view_count) AS avg_pages_per_session
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
    AND user_id IS NOT NULL
GROUP BY DATE(start_time)
ORDER BY date DESC;
```

### 流量来源分析（带UTM）
```sql
SELECT
    COALESCE(utm_source, traffic_source, 'unknown') AS source,
    utm_medium,
    utm_campaign,
    COUNT(*) AS sessions,
    COUNT(DISTINCT user_id) AS unique_users,
    AVG(duration_seconds) AS avg_duration,
    SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS bounce_rate
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day
GROUP BY utm_source, traffic_source, utm_medium, utm_campaign
ORDER BY sessions DESC
LIMIT 20;
```

### 跳出率趋势分析
```sql
SELECT
    DATE(start_time) AS date,
    COUNT(*) AS total_sessions,
    SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) AS bounce_sessions,
    ROUND(SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS bounce_rate
FROM sessions
WHERE start_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
GROUP BY DATE(start_time)
ORDER BY date DESC;
```
