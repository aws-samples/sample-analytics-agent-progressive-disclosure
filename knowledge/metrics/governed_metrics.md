# 治理层官方指标字典 (Governed Metrics)

> 这是治理层（`mart_` 表）的**官方口径**。在治理层回答问题时，指标定义一律以本文件为准，**不要自己重新拼口径**。每个指标标注定义、对应 mart 表/列、owner、版本——这正是「治理之后」和原始数据的区别：口径被冻结、有人负责，而不是每次现推。

> **口径即 function call（重要）**：下列指标已升级成**可调用的 metric**，**优先用 `call_metric` 工具**直接拿权威数，而不是手写 SQL——口径写死在代码里，返回值和看板/董事会材料一致、模型无法猜错。可调用：`gmv`、`gmv_by_channel`、`refund_amount`、`new_users`、`dau`、`cac`、`roi`、`repurchase_rate_30d`、`new_subscriptions`。例：`call_metric(metric="gmv", time_window="last_quarter")`、`call_metric(metric="cac", group_by=["channel"])`。只有 call_metric 返回 `not_covered`（指标/维度没覆盖）时，才退回手写 SQL。本文件是这些指标的**口径定义说明**（人读），结构化定义在 `backend/metrics_def.py`（机器读）。

## GMV（成交额）
- **定义**：有效订单的实付金额之和。
- **口径**：`sum(actual_amount) WHERE status IN ('paid','shipped','delivered')`。排除退款、取消、未支付。
- **取数**：`mart_daily_kpi.gmv` / `mart_daily_revenue.gmv`
- owner：交易数据团队 · 版本：v1（2026-06）

## 退款金额（Refund Amount）
- **定义**：已退款订单的实付金额之和。
- **口径**：`sum(actual_amount) WHERE status='refunded' AND refunded_at IS NOT NULL`，**按退款发生日（`refunded_at`）计**。与 GMV 的下单日口径不同，**不要直接拿 GMV 减退款算净额**（要净额用 `fin_daily_revenue.net_revenue`）。
- **取数**：`fin_daily_revenue.refund_amount`（该表按 `refunded_at` 建轴，合计是**全量**）

```sql
SELECT sum(refund_amount) AS refund_amount FROM fin_daily_revenue
```

- ⚠️ **不要用 `mart_daily_kpi.refund_amt` 回答「退款总额 / 累计退款 / 一共退了多少」。** 那张表的日期轴由 GMV 侧（`placed_at`）决定、不含 `refunded_at`，轴末之后才发生的退款在 LEFT JOIN 时被丢掉，所以跨日求和**偏低约 4% 且不报错**。它只能回答「**某一天**退了多少」。两表差值就是轴外退款。
- 本表的轴（退款发生日）比订单轴长 9 天：最后一批订单下在 01-24，退款一路发生到 02-02，这 9 天共 38,089.59 元。相对时间窗已统一到业务日历锚点 **2026-01-24** 并带上界，所以 `last_7d` = 01/18~01/24，**与 GMV 同段、可直接对比**；那 9 天的尾巴只出现在 `time_window='all'` 的总量里（本来就该含）。
- owner：交易数据团队 · 版本：v2（取数源从 mart_daily_kpi 改为 fin_daily_revenue；业务定义未变）

## 新客（New User）
- **定义**：按 `registered_at` 当天注册的用户。
- **新客订单 / 新客 GMV**：用户的**首笔有效订单**计为新客贡献（`mart_daily_revenue.is_new_user=true`）。
- **取数**：`mart_daily_kpi.new_users`

## 渠道归因（Attribution）
- **定义**：last_touch，每用户取最近一条 last_touch 记录归到一个渠道。
- **覆盖**：仅覆盖有 **last_touch** 归因记录的用户；其余成交计为「未归因」（`channel_name='未归因'`，当前样本实测占 GMV **64.4%**，随数据重造会变，报结论前请现算）。**报渠道结论时必须披露未归因占比**，否则会高估渠道贡献。
- ⚠️ **「未归因」不等于「来路不明」，披露时要说清成因。** 471 个下单用户里只有 175 人有
  `attribution_type='last_touch'` 记录；另有 175 人**只有 `first_touch` 记录**，被本口径的
  `WHERE attribution_type='last_touch'` 滤掉，一并落进了未归因桶。两批用户在
  `user_attributions` 里是**互斥**的（没有任何用户同时有两种记录）。所以那 64.4% 里
  大约一半其实是有渠道信息的，只是不符合 last_touch 口径。说成"六成用户来路不明"是错的。
- **取数**：`mart_daily_revenue` / `mart_channel_daily` 的 channel_* 列

## CAC（获客成本）
- **定义**：渠道投放成本 / 该渠道归因到的新客数。
- **口径**：`nullif(sum(cost),0)/nullif(sum(new_users_attributed),0)`，按渠道。
- **取数**：`mart_channel_daily`
- ⚠️ **窗口内没有投放成本记录的渠道返回 NULL，不是 0。** 别把 NULL 读成「获客免费」，**更不要用它回答「哪个渠道获客成本最低」**——NULL 混了两类完全不同的情况：
  - 天然无投放的自然量渠道（`channel_type` 是 `organic`/`direct`/`referral`：App Store、微信公众号、应用宝、直接访问、老用户邀请，`channel_daily_costs` 里一行都没有）。它们的 CAC 是**不适用**而非 0——你没法靠加预算买到更多老用户邀请，把它们和付费渠道摆进同一个 CAC 排行榜本身就是错的。
  - 付费渠道但成本行落在窗口外（实测近 30 天：快手信息流全量花了 25.2 万、小红书种草 14.6 万、抖音信息流 7.6 万、微信朋友圈广告 4.8 万，这段窗口内一条成本行都没有）。
  要判断得先看该渠道的**全量**成本与 `channels.channel_type`。
- 这条以前是个静默错数：分子是裸的 `sum(cost)`，14 个渠道里 10 个返回「CAC = 0 元/人」，其中包括花了 25 万的快手信息流。同一批行上 ROI 正确弃权（NULL）、CAC 却给 0，是同一张表两套语义；现在两边一致。
- ⚠️ **CAC / ROI 只统计业务日历轴内（`dt <= 2026-01-24`）的成本行，`time_window='all'` 也一样。** 成本表铺到 2026-09-01 而归因数据止于 2026-01-24，轴外 94.6 万成本**没有任何归因可配**，算进来不是「更完整」而是拿两段不同时期相除。所以 CAC 的分子**不等于**成本表的总花费——问「总投放花费」请直接查 `channel_daily_costs` / `mart_channel_daily` 并说明覆盖到 2026-09-01，别拿 CAC 的分子代答。
- 不 clamp 的后果是实测抓到过的：问「哪个渠道获客成本最低」答成「微博KOL 938.77 元最低，只有次低的五分之一」。而微博KOL 的成本行全在 2026-05-01~05-09（**轴内成本 = 0**）、归因新客全在轴内，它便宜只是因为成本数据只覆盖 9 天；次低的微信朋友圈广告同样轴内 0 成本（成本全在 2026-06~08）。**榜首两名恰好是轴内一分钱没花的两个渠道。** 口径对齐后真实排名：

  | 渠道 | CAC（元/人） |
  |---|---|
  | 小红书种草 | 1,373.55 |
  | 抖音搜索 | 1,424.04 |
  | 小红书搜索 | 4,972.31 |
  | 百度搜索 | 6,315.72 |
  | 快手信息流 | 9,358.31 |
  | 抖音信息流 | 9,474.24 |
  | B站UP主 | 12,162.38 |

  全量口径下 CAC = **2,880.14**、ROI = **6.06**。
- 注意这个 clamp 是**按指标**声明的，不是全局规则：`refund_amount` 轴外那 9 天是轴内订单真实产生的退款，clamp 掉总额就从 963,560.92 掉回 925,471.33。同一个「全量」在退款上意味着「别丢尾巴」，在 CAC 上意味着「别配错时期」。

## ROI（投放回报）
- **定义**：渠道归因 GMV / 渠道成本。
- **口径**：`sum(gmv_attributed)/nullif(sum(cost),0)`，只算轴内成本（同 CAC，见上）。
- **取数**：`mart_channel_daily`
- 成本为 0 的渠道返回 **NULL**（不虚增）。CAC 与此一致，见上。
- 轴外 94.6 万成本若算进分母，ROI 会从真值 **6.06** 被压到 **2.11** —— 方向和 CAC 那个偏差相反，但同一个成因。

## 复购率（30 天）
- **定义**：有首单的用户里，首笔有效订单后 30 天内再次产生有效订单的比例。
- **口径**：见 `mart_user_summary.is_repurchaser_30d`；**分母只含有首单用户**，不要拿全体用户当分母。
- **取数**：`mart_user_summary`

## 时间锚点（通用）
- 静态样本，数据落在 **2025-10-27 ~ 2026-01-24**。
- 「最近 / 上周 / 本月」一律以 **`meta_snapshot.as_of_date` = 2026-01-24** 为今天，**禁用 `current_date`/`now()`**。`call_metric` 的时间窗已经统一用这个锚点，所以各指标的「最近 7 天」是同一段日期，可以互相对比。
- ⚠️ **手写 SQL 时不要用「表自身时间列的 max()」当今天** —— 各表轴末根本不齐，用它会静默给出错数。下面是**全库 91 个日期/时间列的普查**（2026-09-17 实测），只列超出锚点的那些；没列进来的列轴末都 ≤ 2026-01-24：
  | 表.列 | 轴末 | 超出多少 | 说明 |
  |---|---|---|---|
  | `orders.placed_at` / `mart_daily_kpi.dt` / `fin_daily_revenue.dt` | 2026-01-24 | — | 业务上真正的「今天」，即锚点本身 |
  | `subscriptions.end_date` | **2027-01-24** | +1 年 | 订阅到期日，年费订阅铺到一年后。21354 行、242 个不同取值 |
  | `ad_campaigns.end_date` | **2026-10-01** | +8 月 | 广告计划结束日 |
  | `channel_daily_costs.date` / `.created_at` / `mart_channel_daily.dt` | **2026-09-01** | +7 月 | 投放成本铺了近一年：2799 行里 **1980 行**、435 万里 **307 万**落在锚点之后 |
  | `dws_channel_weekly.week_start` | 2026-08-31 | +7 月 | 同上（周粒度） |
  | `ad_campaigns.start_date` / `.created_at` | 2026-08-15 | +7 月 | 计划开始日也能排到未来 |
  | `coupons.end_date` | 2026-04-19 | +3 月 | 券的有效期末 |
  | `user_coupons.expire_at` | 2026-02-24 | +1 月 | 领券记录的过期时刻 |
  | `campaigns.end_date` | 2026-02-11 | +18 天 | 营销活动结束日 |
  | `banners.end_date` | 2026-02-01 | +8 天 | Banner 下线日 |
  | `user_attributions.install_time` | 2026-01-29 | +5 天 | 归因安装时刻 |
  | `sessions.start_time`/`end_time`、`user_coupons.received_at`、`push_notifications.delivered_at`/`opened_at` | 2026-01-25 | +1 天 | **只是跨了个零点**：锚点是**日期**，01-24 当天的会话/发券/推送有一部分落在 01-25 凌晨。不是脏数据 |
  | `user_segments`/`event_definitions`/`ad_campaigns`/`banners`/`campaigns` 的 `updated_at`、`ad_creatives.created_at` | 2026-01-25 | +1 天 | **ETL 落库时刻**，整列同一个值（灌数那一瞬）。这类列不是业务时间，不能拿来切窗口 |
- 上表里除了「跨零点」和「ETL 落库」两类，其余都是**业务上正常的未来日期**（计划/订阅/券的结束日天然在未来）。它们不是错误，错的是拿它们当「今天」。
- **绝对日期会随重灌变**（上面这批是 scale 427 那次）。判断一根轴有没有伸出日历，现算 `max(...)` 跟 `(SELECT max(as_of_date) FROM meta_snapshot)` 比，别背日期。
- 所以**开时间窗必须写上界**：只写 `dt > 锚点 - interval '30' day` 会把成本表里九月的行也捞进来（实测 CAC 从真值 2880 变成 15137，不报错）。正确写法是 `dt > 锚点 - interval '30' day AND dt <= 锚点`。
- 残周 / 残月不要直接和整周 / 整月比（首尾两段是残的）。
