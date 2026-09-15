# 数据质量审计：由浅入深五层

> **2026-08-06 附记：本文列出的缺陷已全部修复并重灌。** 生成器按「先算事实、再从
> 事实回填声明列」重写（`scripts/gen/tables.py` 的 `_prep_*` 系列 +
> `selftest_closures.py` 40 项断言），8000 万行重灌后复审：L4 全部归零、漏斗
> max/min 从 1.006 变为 197.6、留存 D0=13,705 → D14=2,564 单调衰减。复审数字见
> 文末「修复后复审」。**正文保留的是修复前的实测**，作为「生成数据长什么样的病」
> 的标本；重跑 `scripts/audit/run.py all` 得到的是修复后的结果。

这份文档是给**人**看的，用来自己动手核这 48 张表数据到底能不能用。
分五层，每层的 SQL 都能直接跑，下面贴的输出全部抄自实跑结果（2026-08-06，
Redshift Serverless `analytics-agent-wg` / `app_analytics`，ap-northeast-1）。

层的顺序是有意的：越靠前越浅、越快，也越容易通过。前三层查「有没有矛盾」，
第四层查「数字之间讲不讲得通」，第五层查「像不像真的」。
**这份数据前三层几乎全绿，第四层塌得很彻底**，所以别看到 L1-L3 全 PASS 就收工。

## 怎么跑

```bash
cd ~/Desktop/Claude/sample-analytics-agent-progressive-disclosure
SEC=$(aws secretsmanager list-secrets --region ap-northeast-1 \
  --query "SecretList[?contains(Name,'analytics-agent-ns')].ARN" --output text)

backend/.venv/bin/python scripts/audit/run.py L1 --secret "$SEC"   # 跑一层
backend/.venv/bin/python scripts/audit/run.py all --secret "$SEC"  # 全跑，约 30 秒
```

SQL 在 `scripts/audit/L1_inventory.sql` … `L5_realism.sql`，想改阈值或加检查直接编辑。
全部是只读 `SELECT`，不建表不改数据。层与层无依赖，可单独跑。

不能用 `rsql.py --file` 跑这些文件：那条路径是给 DDL 装载用的，`fetch=False`，
只打「ok + 耗时」不打结果集。`run.py` 复用了它的语句切分器，把 fetch 打开。

## 一句话结论

**格式和引用是干净的，业务语义是脏的。**

生成器逐表独立抽样，事后把 ID 引用接对了，但从没做过跨表数值闭环。
结果是：每张表自己无懈可击（主键、枚举、空值、表内公式、时序全对，28 个外键零悬空），
两张表之间只要涉及数值就对不上（订单头与明细 99.99% 不符，冗余计数器虚高 2 到 6 倍）。
行为序列层面更彻底：25 个事件类型的量几乎完全相等，留存曲线不衰减，
**漏斗和留存这两类分析在这份数据上算不出有意义的结果**。

## 缺陷清单

| 级别 | 缺陷 | 实测 | 影响 |
|---|---|---|---|
| P0 | 事件流均匀分布 | 25 个事件类型 max/min = **1.006** | 漏斗转化率恒为约 100%，漏斗分析无效 |
| P0 | 留存曲线不衰减 | D0 4247 人 → D14 **4376 人**（反而上升） | 留存、流失、生命周期分析全部无效 |
| P0 | `user_level` 与消费额无关 | 五个等级「消费≥1000 占比」= 21.3/21.4/22.0/21.2/21.3% | 用户分层、高价值人群、LTV 分层无效 |
| P1 | 订单头与明细金额对不上 | **854,019 / 854,078**（99.99%）不符，均值 241.40 vs 340.98 | GMV 取订单头还是明细差 41%，两套数字都能被质疑 |
| P1 | `posts.like_count` 虚高 | 99.1% 不符，声称均值 117 vs 真实 **35** | 内容热度排行错 3.3 倍 |
| P1 | `sessions.event_count` 虚高 | 93% 不符，声称 23 vs 真实 **4** | 会话深度、粘性指标错 5.75 倍 |
| P1 | `orders.item_count` 与明细行数不符 | 594,860 / 854,078（70%）不符 | 件单价、客单件数不可用 |
| P1 | 优惠券核销数远超订单数 | 485 万张 used 券挂在 85 万单上，**平均 5.7 张/单** | 券成本、券 ROI 不可用 |
| P2 | 有效订单缺支付记录 | **64,214** 单（有效单的 10%）无 success 支付 | 支付成功率、对账类分析偏低 10% |
| P2 | 生命周期时间戳缺失 | refunded 缺 `refunded_at` 5,052；cancelled 缺 `cancelled_at` 769；paid 缺 `paid_at` 955 | 退款周期、履约时长这类时间差指标少算 |
| P2 | 事件数与事实表量级反了 | `purchase` 事件 341,295 < 有效订单 640,800 | 用事件算交易会少一半 |
| P3 | 文档行数少算 21% | 文档写 7992 万，实际 **9649 万**（7992 万只等于 base 35 张表） | `knowledge/connection.md`、`database/00_schema_overview.md` 需更正 |
| P3 | 派生「清洗层」是整表复制 | `dwd_events_app` 与 `events` 同为 8,540,780 行，过滤条件命中 0 行 | agent 选哪张表无差别，多一层无收益 |
| 观察 | 消费重尾偏薄 | top 10% 用户占 GMV **41.9%**，真实电商通常 50-70% | 帕累托、二八分析结论会比现实平缓 |

## 哪些分析能做，哪些不能

这是最实用的一张表。前面的缺陷换算成「你能问 agent 什么」：

**可信**

- 规模计数：用户数、订单数、日活量级
- 时间趋势：日、周、小时维度的趋势和峰谷（形状是真的，见 L5.3 / L5.4）
- 枚举占比：注册来源、订单状态、支付方式、内容状态的分布
- GMV 与收入：**前提是统一走订单头口径**。mart 层已经冻结了这个口径且逐日对账 100% 通过
- 渠道成本与 CAC：`channel_daily_costs` 独立生成，引用完整
- 任何跨表 JOIN 的行数：28 个外键关系零悬空，JOIN 不会静默丢行

**不可信**

- 漏斗转化率（事件均匀分布）
- 留存、流失、复购周期（活跃与生命周期无关）
- 用户分层、高价值人群、LTV 分层（`user_level` 与消费独立）
- 商品维度 GMV（明细口径比订单头高 41%）
- 内容热度排行（`like_count` 虚高 3.3 倍）
- 会话深度、人均事件数（`event_count` 虚高 5.75 倍）
- 优惠券 ROI（券核销 5.7 张/单）
- 支付成功率（10% 有效单查不到支付）

---

# L1 体检：库里有什么，覆盖到哪一天

只看规模和边界。这层挂了说明装载没跑完，后面不用跑。

```bash
backend/.venv/bin/python scripts/audit/run.py L1 --secret "$SEC"
```

**L1.1 按层统计**

```
tier       tbls  rows
1_base     35    79924448
2_derived  8     16348624
3_mart     4     218129
4_meta     1     1
```

这里就有一个文档缺陷。`knowledge/connection.md` 写「当前样本约 8000 万行（35 张原始表
+ 4 张 mart + 8 张派生表）」，读起来像是 8000 万覆盖全部 47 张表。实际 **79,924,448
恰好只是 base 那 35 张**，加上派生层和 mart，全库是 **96,491,202 行**，少算 21%。
`database/00_schema_overview.md` 里的「48 张表、7992 万行」同一个错。

**L1.2 表清单**（节选）

```
post_likes       15231627
page_views       12894870
user_coupons      9708732
dwd_events_app    8540780
events            8540780     ← 与 dwd_events_app 行数完全相同
user_follows      8184202
```

行数完全相同的两张表值得停一下。见 L5.9。

**L1.3 / L1.4 / L1.5 边界**

```
empty_tables  verdict          as_of_date  data_start
0             PASS             2026-01-24  2025-10-26

tbl       d0          d1          n
events    2025-10-26  2026-01-24  8540780
orders    2025-10-26  2026-01-24  854078
sessions  2025-10-26  2026-01-24  2135195
users     2025-10-26  2026-01-24  213520
```

四张核心事实表的时间边界严格对齐快照，无越界、无空表。这一层除文档行数外全绿。

# L2 单表完整性：每张表自己站不站得住

`database/redshift/01_tables.sql` 里**一条约束都没有**，没有 PK、FK、NOT NULL。
Redshift 本身也不强制。所以这些性质完全靠生成器自觉，必须真查。

```bash
backend/.venv/bin/python scripts/audit/run.py L2 --secret "$SEC"
```

**结果：27 项检查全部通过。**

```
L2.1 主键唯一性          7 张表全 PASS（users / orders / order_items /
                        payments / events / posts / user_profiles）
L2.2 枚举越界            7 个字段全 0（取值表来自 knowledge/ 卡片）
L2.3 关键列空值          7 个列全 0
L2.4 表内金额公式        actual = total - discount + shipping：854078 行，违反 0，负数金额 0
L2.5 行内时序            6 条规则全 0（paid<placed、shipped<paid、delivered<shipped、
                        end<start、used<received、last_active<registered）
L2.6 session 时长        duration_seconds 与起止时间差不符：0
```

这一层的表现是真的好。特别是 L2.4 和 L2.6：`orders` 的金额公式和 `sessions` 的时长
都是**表内推导出来的**，所以完全自洽。记住这个区别，L4 塌的全是**跨表**推导。

# L3 引用完整性：外键指向的行存不存在

孤儿行的危害是静默的，`INNER JOIN` 会直接少算，不报错。

```bash
backend/.venv/bin/python scripts/audit/run.py L3 --secret "$SEC"
```

**结果：28 个外键关系，真正悬空全部为 0。**

输出分三列，不要合并：

```
fk                                rows      null_fk  dangling
orders.coupon_id -> coupons        854078    529648   0
user_coupons.order_id -> orders   9708732   4854555   0
push_notifications.campaign_id    4270390    768330   0
subscriptions.payment_id          21352        3207   0
（其余 24 个关系 null_fk 与 dangling 均为 0）
```

`null_fk` 全部是合理的可空外键：62% 订单没用券；未使用的券没有 `order_id`
（非空数 4,854,177 与 `status='used'` 的数量完全相等，这一点反而很干净）；
事务型推送不挂运营活动。

**这个三列结构是被一次假阳性逼出来的。** 本文件初版把 `LEFT JOIN` 后
`维表主键 IS NULL` 直接当孤儿，于是 `push_notifications` 报出 768,330 行孤儿，
占 18%，看着像个大缺陷。加 `IS NOT NULL` 守卫重查后真正悬空是 0，那 76 万全是
`campaign_id` 本来就为空。`docs/test-plan-v2.md` 里点过这个毛病，检查器的假阳性
和数据缺陷一样要防。

# L4 业务语义一致性：数字之间讲不讲得通

L2 和 L3 过了只说明格式对、ID 对。这一层问两张表说的是不是同一件事，
以及 `knowledge/` 卡片里写的业务规则在数据里成不成立。

**这是重灾区。** 跨表闭环要求生成时就联动，逐表独立抽样天然做不到。

```bash
backend/.venv/bin/python scripts/audit/run.py L4 --secret "$SEC"
```

**L4.1 订单头 vs 订单明细**

```
ord     amt_ne  avg_head  avg_items  avg_diff  min_diff    max_diff  head_gt_items
854078  854019  241.4     340.98     -99.57    -26457.16   12922.78  394836
```

854,078 单里 **854,019 单**（99.99%）的 `total_amount` 不等于明细 `actual_amount` 之和。
只有 59 单碰巧对上。

差值形状说明了根因。如果只是漏加运费，差值会**单向且大致固定**。这里差值范围
从 −26457 到 +12923，46%（394,836 单）是订单头大于明细，另一半反过来。
**两边是各自独立抽的，从没对账。**

后果很具体：`avg_items / avg_head = 1.41`。同一个 GMV，用 `orders.actual_amount`
算和用 `order_items` 算差 41%。问「总 GMV」和问「哪个品类卖得好」会拿到两套
加不起来的数。

**L4.2 声明的件数 vs 明细行数**

```
ord     cnt_ne  avg_declared  avg_real
854078  594860  2.0           2.0
```

70% 的单 `item_count` 与真实明细行数不符，但两边**均值都是 2.0**。
这是个好例子：只看均值会以为没问题，逐行比才现形。

**L4.3 / L4.4 冗余计数器**

```
posts   like_ne  declared_likes  real_likes  cmt_ne   declared_cmt  real_cmt
427039  423369   117.0           35.0        415643   34.0          14.0

sess     ev_cnt_ne  declared_ev  real_ev
2135195  1986446    23.0         4.0
```

`posts.like_count` 声称均值 117，真实点赞明细只有 35，虚高 3.3 倍，99.1% 的帖子不符。
`comment_count` 虚高 2.4 倍。`sessions.event_count` 声称 23，真实 4，虚高 5.75 倍。

冗余计数器是最常见的静默错误源：报表读计数器，明细算另一个数，两边永远对不上，
而且都不报错。对比 L2.6 的 `duration_seconds`（表内推导，100% 吻合），
差别就在于是不是跨表。

**L4.5 `user_level` 的业务定义是否成立**

`knowledge/domains/user/users.md` 写着等级 4 = 累计消费 ≥ 1000 元，
等级 5 = 累计消费 ≥ 10000 元或 VIP。查：

```
user_level  users  vips   avg_spend  max_spend  n_ge_1k  n_ge_10k  pct_ge_1k
1           89701  10747  695.0      74652.0    19113    210       21.3
2           55597  6739   711.0      80132.0    11874    143       21.4
3           38444  4610   710.0      32882.0    8445     108       22.0
4           19122  2249   687.0      23881.0    4061     34        21.2
5           10656  1266   693.0      22056.0    2268     22        21.3
```

看最后一列。五个等级的「消费 ≥ 1000 占比」是 21.3 / 21.4 / 22.0 / 21.2 / 21.3%，
几乎完全相同。**只要等级和消费有一点关系，这个比例就必须随等级上升。**
它是平的，等级就是独立抽的。

其他佐证：等级 4 只有 21.2% 的人真的消费 ≥ 1000（文档说这是入门条件）；
等级 5 只有 22/10656 = 0.2% 真的 ≥ 10000；`avg_spend` 五档全在 687 到 711 之间；
最高消费额出现在等级 1 和 2（74652、80132），不在等级 5。

这条比数值对不上更严重：**卡片里写了一个数据里不存在的规则**。agent 读到它，
会照着写出「筛 `user_level=4` 来找高价值用户」这类 SQL，返回的人群跟消费无关，
而且不会有任何报错。

**L4.6 支付覆盖与生命周期**

```
rule                                bad
已付订单缺 success 支付记录             64214
status=refunded 但 refunded_at 为空    5052
status=paid 之后 但 paid_at 为空         955
status=cancelled 但 cancelled_at 为空    769
status=pending 但已有 success 支付         0
success 支付金额 <> 订单 actual_amount     0
```

64,214 单状态是 paid/shipped/delivered，却查不到 success 支付记录，占 640,800
有效单的 10%。反过来 `payments.amount` 与订单 `actual_amount` **完全一致**，
说明支付是照订单头造的，只是覆盖不全。

**L4.7 优惠券核销**

```
used_coupons  distinct_orders_cited  total_orders  orders_declaring_coupon  coupons_per_order
4854177       851207                 854078        324430                   5.7
```

三个数字互相打架。`orders.coupon_id` 说 32.4 万单用了券；`user_coupons` 说 485 万张券
被用掉；这 485 万张挂在 85 万个不同订单上，**平均每单 5.7 张已用券**，
而且被引用的订单占全部订单的 99.7%（含 pending 和 cancelled 的）。

**L4.8 事件数 vs 事实表**

```
ev_purchase  real_paid_orders  ev_use_coupon  real_used_coupons  ev_register  real_users
341295       640800            341857         4854177            341474       213520
```

`purchase` 事件 341,295 次，实际有效订单 640,800 单。**方向反了**：一单至少产生一个
购买事件，事件数不该低于订单数。`use_coupon` 事件与真实用券数差 14 倍。
`register` 事件 341,474 次而用户只有 213,520 人，等于平均每人注册 1.6 次。

# L5 分布真实性 + 治理层对账

前四层查有没有矛盾，这一层查像不像真的。一份内部无矛盾但分布退化的数据，
跑得出漂亮报表，结论全是假的。这层没有 PASS/FAIL，要跟真实业务的经验形状对照读。

```bash
backend/.venv/bin/python scripts/audit/run.py L5 --secret "$SEC"
```

**L5.1 mart 层对账：这是全篇最好的消息**

```
days  missing_in_kpi  kpi_gmv_ne  revenue_gmv_ne  detail_total  kpi_total    revenue_total
91    0               0           0               151238025.32  151238025.32 151238025.32
```

91 天逐日 GMV，`mart_daily_kpi`、`mart_daily_revenue`、明细三者**完全一致**，
总额都是 151,238,025.32，没有一天有偏差。治理层是照定义算对了的。

要注意它的口径：`sum(orders.actual_amount) where status in (paid,shipped,delivered)`，
**采信订单头，不采信 `order_items`**。所以 mart 自身无懈可击，但它继承了 L4.1
那个 41% 的分歧。agent 走 `call_metric` 拿到的数是自洽的，只是别拿它跟商品维度的
明细加总去对。

**L5.2 / L5.3 / L5.4 时间形状：这一块是真的做了**

```
days  min_ord  max_ord  avg_ord  sd      cv
91    6751     13262    9385.0   1617.7  0.172

dow  orders  pct        hr  orders      hr  orders
0    139733  16.36      3   3091  ←谷底  19  68881
1    105686  12.37      4   3169        20  74506  ←主峰
2    104094  12.19      12  54519 ←午峰  21  64657
6    150002  17.56 ←峰  18  58773        23  22156
```

周六 15.0 万单 vs 周二 10.4 万，峰谷比 1.44，**周末效应明显**。小时分布凌晨 3 点
谷底 3,091，晚 20 点主峰 74,506，中间还有个 12 点午间小高峰，是真实的双峰作息曲线。
日订单变异系数 0.172，比真实电商 0.2-0.4 略低但不算假。

这三项是加分项。它们能做对，是因为都是**单表内的一维分布**，抽样时直接按形状加权就行。

**L5.5 消费集中度**

```
top1pct_gmv_share  top10pct_gmv_share  top50pct_gmv_share
10.4               41.93               87.23
```

top 10% 用户占 GMV 41.9%，真实电商通常 50-70%。重尾偏薄，消费额基本是同一个分布抽的。
这跟 L4.5 里五档 `avg_spend` 全在 690 上下是同一件事的两个侧面。

**L5.6 / L5.7 事件漏斗形状：P0**

```
event_name      n       users        event_types  min_n   max_n   max_over_min
view_product    341875  151009       25           340653  342740  1.006
begin_checkout  341745  150973
add_to_cart     341675  151059
purchase        341295  150799
view_home       340653  150626
```

真实漏斗应该逐级掉量级：`view_home` 远大于 `view_product`，再到 `add_to_cart`、
`begin_checkout`、`purchase` 层层收窄。这里五个事件的量**几乎完全相等**，
`purchase`（341,295）甚至比 `view_home`（340,653）还多一点。

全部 25 个事件类型的 max/min = **1.006**。这是均匀抽样的签名。
后果：任何漏斗查询都会算出接近 100% 的转化率。
`database/00_schema_overview.md` 把「用户漏斗分析」列为典型场景，这个场景目前跑不通。

**L5.8 留存曲线：P0**

```
lag_days  active_users        lag_days  active_users
0         4247                7         4233
1         4110                10        4208
2         4104                12        4150
3         4136                14        4376  ← 比 D0 还高
```

取 2025-11-03 到 11-09 注册的队列，看注册后第 N 天还活跃的人数。
真实留存应单调递减，D1 约 40%、D7 约 20%。这里 D0 到 D14 一路持平，
第 14 天 4,376 人**比第 0 天的 4,247 人还多**。

根因和 L5.6 是一个：`events` 的 `user_id` 和 `event_time` 独立随机抽，
没建模用户生命周期。留存、流失、复购周期这一类全部不可用。

**L5.9 派生层是否真的做了清洗**

```
events_rows  dwd_rows  events_null_user  orders_valid  dwd_orders_valid_rows
8540780      8540780   0                 640800        640800
```

`dwd_events_app` 的定义是 `SELECT ... FROM events WHERE user_id IS NOT NULL`。
而 `events` 里 `user_id` 为空的行是 **0**，所以这个过滤条件命中 0 行，
这张「清洗层」是整表复制，8,540,780 行一行不少。

`dwd_orders_valid` 同理，640,800 行与直接筛 `orders` 完全一致，它至少是真的做了筛选。

# 根因

把五层的结果并起来，生成器的行为模式很清楚：

| 生成器做对的 | 为什么能做对 |
|---|---|
| 主键唯一、枚举合法、关键列非空 | 单列约束，抽样时限定值域即可 |
| 表内公式（订单金额、session 时长） | 同一行内推导，一次算完 |
| 行内时序递增 | 同一行内排序 |
| 28 个外键零悬空 | 先造维表再造事实表，从维表主键里抽 ID |
| 时间形状（周末效应、作息双峰） | 单表内的一维分布，按形状加权抽样 |
| mart 层逐日对账 | CTAS 从明细算出来的，必然自洽 |

| 生成器做错的 | 为什么会错 |
|---|---|
| 订单头 vs 明细金额、件数 | 两张表分别抽，没有「先造明细再汇总回头」这一步 |
| 冗余计数器虚高 | 计数器当普通数值列抽了，没回头数明细 |
| `user_level` 与消费无关 | 等级按固定概率抽，没读订单 |
| 支付覆盖不全、时间戳缺失 | 状态机没有真的跑一遍状态转移 |
| 券核销数超订单数 | 券表按用户量抽，没受订单数约束 |
| 事件均匀、留存不衰减 | 事件流按类型均匀抽 + 时间独立抽，没建模用户生命周期 |

一句话：**凡是能在一行或一张表内闭合的，全对；凡是要跨表联动的，全没做。**
两个 P0（漏斗、留存）性质更重一层，它们不是对账问题，是根本没建模行为序列。

# 要改的话，改哪里

按修复成本从低到高：

1. **文档层，几分钟**。`knowledge/connection.md` 和 `database/00_schema_overview.md`
   的行数改成 base 7992 万 / 全库 9649 万，说清 7992 万只覆盖 35 张 base 表。
2. **卡片层，几分钟**。`knowledge/domains/user/users.md` 里 `user_level` 那张「条件」表
   要么删掉，要么标注为「样本数据未按此规则生成」。它现在会误导 agent 写出错误 SQL，
   而且不报错，是这批缺陷里唯一会**静默传播到分析结论**的文档问题。
3. **生成器补对账，工作量中等**。`scripts/gen/` 里改成先造 `order_items` 再汇总回填
   `orders.total_amount` 和 `item_count`；`posts.like_count`、`sessions.event_count`
   这类计数器同样从明细回填。这一步能一次消掉全部 P1。
4. **状态机跑一遍，工作量中等**。订单从 pending 走到 delivered/cancelled/refunded 时
   同步写时间戳和支付记录，消掉 P2。
5. **行为序列建模，工作量最大**。事件按漏斗层级设递减概率，活跃按用户注册后的衰减
   曲线抽，才能让 L5.6 和 L5.8 变成有意义的形状。这是两个 P0，也是最贵的一项。

前两项是文档改动，不影响任何已有产物。后三项要重跑生成器和重灌 8000 万行，
改之前先决定这份样本要不要支持漏斗和留存这两类场景。

---

# 修复后复审（2026-08-06）

上面五项建议全部做完：生成器重写（`scripts/gen/tables.py`，「先算事实、再回填声明列」，
金额走整数分）、新增 `scripts/gen/selftest_closures.py`（40 项跨表断言，挂进
`test_all.sh` L0）、8000 万行重灌（S3 前缀 `raw-v3/`，TRUNCATE+COPY 483 秒）、
mart/derived/governance 三层 CTAS 重建、`snapshot.py` 与生成侧 `_expected.json`
对账 471 项指标仅 2 处差异（`birth_date` 被脱敏策略掩码，属「脱敏与对账互斥」的
已文档化取舍，非搬迁丢数据）。

`scripts/audit/run.py all` 复审关键数字，与正文逐条对照：

| 项 | 修复前 | 修复后 |
|---|---|---|
| 订单头 vs 明细金额不符 | 854,019 / 854,078 | **0**（avg_head = avg_items = 241.40） |
| `item_count` 不符 | 594,860 | **0** |
| `posts.like_count` 不符 | 423,369（虚高 3.3 倍） | **0** |
| `sessions.event_count` 不符 | 1,986,446（虚高 5.75 倍） | **0** |
| `user_level` 与消费 | 五档「≥1000 占比」全为 21% | 等级 4 内 **100%** ≥1000，等级 1-3 内 **0%** |
| 有效订单缺 success 支付 | 64,214 | **0** |
| refunded/cancelled/paid 缺时间戳 | 5,052 / 769 / 955 | **0 / 0 / 0** |
| 券核销 | 485 万张挂 85 万单（5.7 张/单） | 324,596 张 = 用券订单数（**1.0 张/单**，三元对齐） |
| purchase 事件 vs 有效订单 | 341,295 < 640,800（方向反） | **647,265 = 647,265**（精确相等） |
| 漏斗 | 25 类事件 max/min = 1.006 | 139.4万 > 114.1万 > 92.8万 > 75.0万 > 64.7万，max/min = **197.6** |
| 留存 | D14 比 D0 高 | D0=13,705 → D1=8,803 → D7=3,922 → D14=2,564 **单调衰减** |
| mart 对账 | 91 天 0 差异（旧口径） | 91 天 0 差异，GMV = **151,238,025**（旧 149,685,621，+1% 来自窗末 refunded 按剩余窗口下调为 paid） |

全库行数变为 **91,294,056**（base 79,921,903 + derived 11,154,023 + mart 218,129 + meta 1）。
派生层缩小是生命周期建模的正常结果：活跃集中到注册后早期，`dws_user_daily` 的
(user, day) 组合数下降。`payments` 从 691,800 变为 689,255（每个付过钱的订单恰好
一条，行数由状态分布决定）。

**修复后仍保留的已知限制**（有意为之或成本不划算）：

- 漏斗只保证单调，转化率仍偏高（行预算 85 万订单配 850 万事件，purchase 占比约
  7.6%，真实 APP 是 0.5%~2%）
- `payments` 不再有 failed / pending 花色（一单一支付的代价）
- `user_coupons` 的 used 占比约 3.3%（核销数被订单数锁死，973 万张券本来就发得多）
- top 10% 用户 GMV 份额约 42%，重尾仍偏薄（观察级，未动）
- `dwd_events_app` 仍与 `events` 等行（`user_id` 无空值，清洗条件命中 0 行）——
  这是派生层作为「治理素材」的一部分，刻意保留
- `user_attributions` 只覆盖约 70% 用户 →「约六成 GMV 未归因」，这是知识库写明的
  治理发现素材，**不是缺陷，别修**

**eval 重基线（同日完成）**：全量 21/21 通过，存档
`eval/baseline/eval.post-datafix-redshift.{md,json}`。顺带修了 eval 自身两处问题
（金标自带舍入在数值变小后与容差打架、连跑 40+ 次 agent 的瞬时限流无重试），
细节见 `eval/baseline/README.md` 的 2026-08-06 小节。
