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

- ⚠️ **不要用 `mart_daily_kpi.refund_amt` 回答「退款总额 / 累计退款 / 一共退了多少」。** 那张表的日期轴由 GMV 侧（`placed_at`）决定、不含 `refunded_at`，轴末之后才发生的退款在 LEFT JOIN 时被丢掉，所以跨日求和**偏低且不报错**。它只能回答「**某一天**退了多少」。两表差值就是轴外退款。
- **当前这份数据上两边恰好相等**（都是 9,897,495.82）：生成器把订单生命周期的时间戳全截在 `as_of_date` 以内，退款轴末和订单轴末同为 2026-01-24，没有退款落在轴外、没有东西可丢。**这不等于缺陷被修了**——少的是"能暴露差额的数据"，取数源照旧要走 `fin_daily_revenue`。旧数据集里退款轴比订单轴长 9 天（到 2026-02-02，那 9 天共 38,089.59 元），`mart_daily_kpi` 侧给的是 925,471.33 而真值 963,560.92，偏低约 4%。
- 相对时间窗已统一到业务日历锚点 **2026-01-24** 并带上界，两条轴同尾，所以 `last_7d` = 01/18~01/24，**与 GMV 同段、可直接对比**。
- owner：交易数据团队 · 版本：v2（取数源从 mart_daily_kpi 改为 fin_daily_revenue；业务定义未变）

## 新客（New User）
- **定义**：按 `registered_at` 当天注册的用户。
- **新客订单 / 新客 GMV**：用户的**首笔有效订单**计为新客贡献（`mart_daily_revenue.is_new_user=true`）。
- **取数**：`mart_daily_kpi.new_users`

## 渠道归因（Attribution）
- **定义**：last_touch，每用户取最近一条 last_touch 记录归到一个渠道。
- **覆盖**：仅覆盖有 **last_touch** 归因记录的用户；其余成交计为「未归因」（`channel_name='未归因'`，当前样本实测占 GMV **64.7%**，随数据重造会变，报结论前请现算）。**报渠道结论时必须披露未归因占比**，否则会高估渠道贡献。
- ⚠️ **「未归因」不等于「来路不明」，披露时要说清成因。** 126,010 个下单用户里只有 44,217 人有
  `attribution_type='last_touch'` 记录；另有 43,738 人**只有 `first_touch` 记录**，被本口径的
  `WHERE attribution_type='last_touch'` 滤掉，一并落进了未归因桶；真正一条归因都没有的是
  38,055 人。两种类型在 `user_attributions` 里是**互斥**的（149,474 行 = 149,474 个用户，
  没有任何用户同时有两种记录）。所以那 64.7% 里大约一半（43,738 / 81,793 = 53.5%）
  其实是有渠道信息的，只是不符合 last_touch 口径。说成"六成用户来路不明"是错的。
- **取数**：`mart_daily_revenue` / `mart_channel_daily` 的 channel_* 列

## CAC（获客成本）
- **定义**：渠道投放成本 / 该渠道归因到的新客数。
- **口径**：`nullif(sum(cost),0)/nullif(sum(new_users_attributed),0)`，按渠道。
- **取数**：`mart_channel_daily`
- ⚠️ **窗口内没有投放成本记录的渠道返回 NULL，不是 0。** 别把 NULL 读成「获客免费」，**更不要用它回答「哪个渠道获客成本最低」**。14 个渠道里有 5 个恒为 NULL：App Store、微信公众号、应用宝、直接访问、老用户邀请——它们的 `channel_type` 是 `organic`/`direct`/`referral`，`channel_daily_costs` 里一行都没有。它们的 CAC 是**不适用**而非 0：你没法靠加预算买到更多老用户邀请，把它们和付费渠道摆进同一个 CAC 排行榜本身就是错的。要判断得先看该渠道的**全量**成本与 `channels.channel_type`。
  > 另一类 NULL——「付费渠道，但成本行恰好全落在窗口外」——**这份数据里不出现**：9 个有投放的渠道（`paid` / `kol`）在业务轴内都有成本行，全量窗口和近 30 天窗口都算得出 CAC。机制仍然成立（换一份成本覆盖更稀的数据就会遇到），只是不能再拿本数据集举这个例子。
- 这条以前是个静默错数：分子是裸的 `sum(cost)`，14 个渠道里 10 个返回「CAC = 0 元/人」，其中包括真花过钱的渠道。同一批行上 ROI 正确弃权（NULL）、CAC 却给 0，是同一张表两套语义；现在两边一致。
- ⚠️ **CAC / ROI 只统计业务日历轴内（`dt <= 2026-01-24`）的成本行，`time_window='all'` 也一样。** 成本表铺到 2026-09-01 而归因数据止于 2026-01-24，**2,799 行成本里 1,980 行、435.2 万里 307.7 万落在轴外**，那段**没有任何归因可配**，算进来不是「更完整」而是拿两段不同时期相除。所以 CAC 的分子**不等于**成本表的总花费——问「总投放花费」请直接查 `channel_daily_costs` / `mart_channel_daily` 并说明覆盖到 2026-09-01，别拿 CAC 的分子代答。
- 不 clamp 的偏差有多大：全量 CAC 从 **17.07** 虚高到 **58.25**（3.41 倍），ROI 从 **41.84** 被压到 **12.26**（同样 3.41 倍，方向相反）。两个偏差同一个成因——分母/分子里多了 8 个月没有成交的成本。
  > **这份数据上 clamp 不改变渠道之间的排名。** 九个投放渠道的成本在轴内外的分布几乎同比例，所以不 clamp 排出来的顺序和下表一致。别据此认为 clamp 可省：错的是**量级**，而量级正是拿去和别的数比较的那个东西——「CAC 58 元」放在客单价旁边会读成"获客几乎不赚钱"，真值 17.07 才是对的。旧数据集里连排名都是反的（成本覆盖只有 9 天的微博KOL 被答成"938.77 元最便宜"，而它轴内一分钱没花），那是这条 clamp 当初被写下来的原因。
- 口径对齐后的真实排名（全量，元/人）：

  | 渠道 | channel_type | CAC（元/人） | ROI |
  |---|---|---|---|
  | 小红书搜索 | paid | 6.54 | 112.78 |
  | 抖音搜索 | paid | 10.17 | 68.15 |
  | 快手信息流 | paid | 13.27 | 51.63 |
  | 百度搜索 | paid | 17.29 | 45.66 |
  | 抖音信息流 | paid | 21.44 | 32.69 |
  | 微信朋友圈广告 | paid | 27.08 | 27.14 |
  | 小红书种草 | kol | 32.74 | 20.33 |
  | B站UP主 | kol | 42.96 | 16.03 |
  | 微博KOL | kol | 68.27 | 10.29 |

  全量口径下 CAC = **17.07**、ROI = **41.84**。注意 CAC 和 ROI 在这份数据上**排名完全相反**（最便宜的渠道回报也最高），三个 `kol` 渠道占据 CAC 榜尾——报结论时这个规律值得一句说明，但别当成因果。
- 注意这个 clamp 是**按指标**声明的，不是全局规则。`refund_amount` 刻意不 clamp，因为它的语义是「轴内订单产生的退款，哪天发生算哪天」。**这份数据里没有越过 `as_of_date` 的退款**（`fin_daily_revenue` 的轴末就是 2026-01-24），所以对它 clamp 与不 clamp 的数值相同（都是 9,897,495.82）——但声明照旧留在指标上，换一份带退款尾巴的数据时它才是对的。旧数据集有 9 天尾巴，clamp 会让总额从 963,560.92 掉回 925,471.33，那是这条声明当初被写下来的原因。

## ROI（投放回报）
- **定义**：渠道归因 GMV / 渠道成本。
- **口径**：`sum(gmv_attributed)/nullif(sum(cost),0)`，只算轴内成本（同 CAC，见上）。
- **取数**：`mart_channel_daily`
- 成本为 0 的渠道返回 **NULL**（不虚增）。CAC 与此一致，见上。
- 轴外 307.7 万成本若算进分母，ROI 会从真值 **41.84** 被压到 **12.26** —— 方向和 CAC 那个偏差相反，但同一个成因。

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
  | `orders.placed_at` / `mart_daily_kpi.dt` / `fin_daily_revenue.dt` | 2026-01-24 | — | 业务上真正的「今天」，即锚点本身。退款轴**现在和订单轴同尾**（重灌前它比订单轴长 9 天，到 2026-02-02） |
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
- 所以**开时间窗必须写上界**：只写 `dt > 锚点 - interval '30' day` 会把成本表里九月的行也捞进来，不报错。实测过两次，量级都是 5–7 倍：CAC 从真值 **2880** 变成 **15137**，全量批次上是近 30 天 CAC 从 **16.89** 变成 **125.30**（7.4 倍）。**绝对值随批次变，错法不变**。正确写法是 `dt > 锚点 - interval '30' day AND dt <= 锚点`。
- 残周 / 残月不要直接和整周 / 整月比（首尾两段是残的）。
