# APP Analytics Skill 测试问题集

按难度分级(Level 1~5)的 CLI Skill 测试清单,用来人工验证 Skill 的路由准确性与 SQL 生成质量。

> **Web App 的示例题在哪**:网页问数(`web/index.html` / live demo)左侧那 6 个由易到难的预设按钮(30天GMV → 订阅套餐 → 转化漏斗 → 优惠券核销 → 渠道CAC → A/B实验)是另一套、专门挑了能体现"读对文档才查得对"的口径陷阱题,定义在 `web/index.html` 的 `PRESETS` 里,跟本文件互补。

> **⚠️ 时间口径**:本库是**静态样本**,数据落在 2025-10-27 ~ 2026-01-24。下面用了"今天/最近7天/过去30天"这类相对时间的题,**测试时要把"今天"理解成数据里的最新日期**,SQL 用表自身时间列的 `max()` 作锚点(如 `WHERE event_time >= (SELECT max(event_time)::date FROM events) - interval '6 days'`),**不要用 `current_date`/`now()`**——否则会落在数据区间外查出空结果。这本身就是一道隐含的口径考点。

## 治理层 / 洞察场景(text-to-insight)

下面这组题走的是**治理层(`mart_` 表)**,跟下面 Level 1-5 的原始表取数题是两种范式:这里 AI 不再现场 join 35 张原始表,而是对算好的干净表写简单 SELECT,把力气花在多角度切片、归因、下判断上。也就是 Web App 左侧「治理层·洞察」那组预设。

| 题目 | 预期 Agent 行为 |
|------|----------------|
| 复盘最近一个完整自然月的 GMV:环比上月涨跌、主要哪些渠道和新老客带动、有什么值得注意的 | 路由到 `domains/mart/`,查 `mart_daily_revenue` / `mart_daily_kpi`;连发多角度切片(整月环比 → 按渠道类型 → 按新老客);用整月口径避开残月;**主动披露约六成 GMV「未归因」**;给综合判断而非罗列。 |
| 最近 7 天业务整体怎么样,跟前 7 天比关键指标有什么变化,最值得关注的是什么 | 查 `mart_daily_kpi`,以 `max(dt)` 为锚算近 7 天 vs 前 7 天;DAU 用 avg、流量指标用 sum;挑出真正动了的指标说,别把所有指标倒出来。 |
| 最近的用户 30 天复购率是多少,不同注册渠道的复购率有什么差异 | 查 `mart_user_summary`;用官方口径(首单后 30 天、分母只含有首单用户,见 `metrics/governed_metrics.md`),不自己拼;按 `register_channel` 对比。 |

> **验证要点**:这些题**不应**出现对原始明细表(orders/events/…)的复杂 join,应只对 `mart_` 表写简单 SELECT;反过来,下面 Level 1-5 的取数题**不应**误用 mart 表。本地端到端实测已确认两层各走各的路。

---

## 测试说明

- 在**新 session**（无上下文）中逐个测试
- 观察 Claude 是否正确加载 skill、路由到正确的域/表
- 验证 SQL 生成的正确性和查询结果(注意上面的时间口径)
- 记录每个问题的通过/失败状态

---

## Level 1: 单域简单查询（热身）

### 1.1 用户域
```
查一下目前有多少注册用户
```
**预期**: 加载 user 域 → users 表 → COUNT 查询

### 1.2 商品域
```
商品分类有哪些？
```
**预期**: 加载 product 域 → categories 表 → 列出分类

### 1.3 行为域
```
今天的 DAU 是多少
```
**预期**: 加载 behavior 域 → events 表 → COUNT DISTINCT user_id

### 1.4 社交域
```
平台上有多少帖子
```
**预期**: 加载 social 域 → posts 表 → COUNT 查询

### 1.5 交易域
```
查一下总订单数
```
**预期**: 加载 transaction 域 → orders 表 → COUNT 查询

### 1.6 归因域
```
有哪些获客渠道
```
**预期**: 加载 attribution 域 → channels 表 → 列出渠道

### 1.7 营销域
```
目前有多少营销活动
```
**预期**: 加载 marketing 域 → campaigns 表 → COUNT 查询

### 1.8 实验域
```
正在进行的 A/B 测试有哪些
```
**预期**: 加载 experiment 域 → ab_tests 表 → 筛选 status='running'

---

## Level 2: 单域中等查询（字段筛选 + 聚合）

### 2.1 用户域 - 用户画像
```
用户的性别分布是怎样的？男女比例多少？
```
**预期**: user_profiles 表 → GROUP BY gender

### 2.2 用户域 - 设备分布
```
用户使用的设备类型分布，iOS 和 Android 各占多少
```
**预期**: user_devices 表 → GROUP BY device_type

### 2.3 商品域 - 商品统计
```
每个分类下有多少商品，按数量排序
```
**预期**: products + categories → JOIN + GROUP BY + ORDER BY

### 2.4 行为域 - 页面访问
```
最近7天访问量最高的页面 Top 10
```
**预期**: page_views 表 → 时间筛选 + GROUP BY + ORDER BY LIMIT

### 2.5 社交域 - 内容互动
```
点赞数最多的帖子 Top 5
```
**预期**: posts + post_likes → JOIN + COUNT + ORDER BY LIMIT

### 2.6 交易域 - 订单状态
```
各订单状态的数量分布
```
**预期**: orders 表 → GROUP BY status

### 2.7 归因域 - 渠道效果
```
各渠道带来的新用户数量排名
```
**预期**: user_attributions + channels → JOIN + GROUP BY

### 2.8 营销域 - 优惠券使用
```
优惠券的使用率是多少
```
**预期**: user_coupons 表 → 计算 used/total 比例

---

## Level 3: 跨域关联查询

### 3.1 用户 + 交易
```
高价值用户（消费金额 Top 100）的年龄和性别分布
```
**预期**: users + orders + user_profiles → 多表 JOIN

### 3.2 商品 + 交易
```
GMV 最高的商品 Top 10，显示商品名称和销售额
```
**预期**: order_items + products + orders → JOIN + SUM + 状态筛选

### 3.3 行为 + 用户
```
过去30天未登录的用户有多少（流失用户）
```
**预期**: users + sessions → LEFT JOIN + IS NULL 或 NOT IN

### 3.4 社交 + 交易
```
发过帖子的用户和没发过帖子的用户，平均消费金额对比
```
**预期**: users + posts + orders → 分组对比分析

### 3.5 归因 + 交易
```
各获客渠道的用户 ARPU（人均消费）对比
```
**预期**: user_attributions + users + orders → 渠道维度聚合

### 3.6 营销 + 交易
```
使用了优惠券的订单和未使用优惠券的订单，客单价对比
```
**预期**: orders + user_coupons → 条件分组对比

---

## Level 4: 复杂指标计算

### 4.1 留存分析
```
计算最近一周每天的次日留存率
```
**预期**: 加载 core_metrics → 自连接计算 Day 1 Retention

### 4.2 转化漏斗
```
从浏览商品 → 加入购物车 → 下单 → 支付的转化漏斗
```
**预期**: events 表 → 漏斗各步骤用户数 + 转化率

### 4.3 LTV 计算
```
计算用户的平均生命周期价值（LTV）
```
**预期**: 加载 business_kpis → users + orders 聚合计算

### 4.4 ROI 分析
```
各广告渠道的 ROI，按 ROI 从高到低排序
```
**预期**: ad_campaigns + channel_daily_costs + user_attributions + orders

### 4.5 用户分群价值
```
各用户分群的平均消费金额对比
```
**预期**: user_segments + user_segment_members + orders

---

## Level 5: 综合业务分析（开放式）

### 5.1 综合增长分析
```
分析过去30天的整体业务增长情况，包括用户增长、订单增长、GMV 增长趋势
```
**预期**: 多表时间序列分析，按天聚合多个指标

### 5.2 用户行为路径
```
分析用户从首次访问到首次下单的典型路径和时间
```
**预期**: sessions + events + orders → 用户旅程分析

### 5.3 A/B 测试效果
```
对比某个 A/B 测试的各变体转化率和收入效果
```
**预期**: ab_tests + ab_test_variants + ab_test_assignments + orders

### 5.4 渠道归因效果
```
分析首次触点归因和末次触点归因下，各渠道的贡献对比
```
**预期**: user_attributions → attribution_type 分组分析

### 5.5 营销活动 ROI
```
评估最近一次促销活动的效果，包括参与用户数、订单量、GMV 提升、优惠券核销率
```
**预期**: campaigns + coupons + user_coupons + orders → 综合分析

---

## 测试评分标准

| 等级 | 通过率 | 评价 |
|------|--------|------|
| ⭐⭐⭐⭐⭐ | 90%+ | Skill 非常成熟，可直接使用 |
| ⭐⭐⭐⭐ | 75-89% | 基本可用，部分场景需优化 |
| ⭐⭐⭐ | 60-74% | 勉强可用，需要较多改进 |
| ⭐⭐ | 40-59% | 问题较多，需大幅优化 |
| ⭐ | <40% | 不可用，需要重新设计 |

## 测试记录模板

```markdown
## 测试日期: YYYY-MM-DD

### Level 1 (8题)
- [ ] 1.1 用户数 - ✅/❌ 备注:
- [ ] 1.2 商品分类 - ✅/❌ 备注:
- [ ] 1.3 DAU - ✅/❌ 备注:
- [ ] 1.4 帖子数 - ✅/❌ 备注:
- [ ] 1.5 订单数 - ✅/❌ 备注:
- [ ] 1.6 获客渠道 - ✅/❌ 备注:
- [ ] 1.7 营销活动 - ✅/❌ 备注:
- [ ] 1.8 A/B测试 - ✅/❌ 备注:

### Level 2 (8题)
...

### Level 3 (6题)
...

### Level 4 (5题)
...

### Level 5 (5题)
...

### 总分: __/32
### 评级: ⭐
```
