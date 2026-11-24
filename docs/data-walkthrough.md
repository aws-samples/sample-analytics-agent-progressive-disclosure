# 数据结构说明书：从一张表走到全库

这份文档按由浅入深的顺序把库过一遍：48 张表、8 个业务域加治理层。每一步都配可以直接跑的
SQL，贴的输出全部抄自实跑结果（2026-08-19，在 S3 Tables + Athena 上重跑，**当时湖里装的是
仓库里那份约 22 万行的种子数据**）。

> **数据批次说明（2026-09-01）**：正文里的每个数字都来自 `data/csv/` 那批种子数据
> （约 22 万行）。**线上现在装的不是这批**——2026-08-31 用新生成器全量重灌，
> 云上是 **79,943,758 行**（scale 427.07 / seed 42 / 业务轴止 2026-01-24，
> 记录在 `data/loaded_row_counts.json`）。**结构性结论仍然成立**（表关系、口径陷阱、
> 反例清单、三条铁律），**具体行数和金额全部作废**，别拿正文的数字去对线上查询结果。
> 当前数字的真源是 `knowledge/domains/**` 的卡片；这份文档留着是因为它讲的是
> 「怎么把一个库读明白」，那套读法换批数据也一样用。另有几处重灌后被推翻的规律
> （地址列从整列 NULL 变成单一假常量、取消/退款原因各只剩一个取值、
> `channel_daily_costs` 的轴伸到业务锚点之后）记在对应卡片和 `docs/test-plan.md` 里。
>
> **重跑说明**：这份文档最初写在 v2 的 8000 万行放大数据集上。v3 湖仓化之后装载的是
> 仓库里提交的 `data/csv/` 种子数据（约 22 万行），**不是同一批数据**——不只是数字变小，
> 原文里好几条写成「恒成立」的规律在种子数据上并不成立（首事件早于注册、计数器回填、
> purchase 事件数 = 订单数……）。这次逐条实测重写，不成立的直接改成反例，并集中列进
> 文末附表。v2 那批数据的审计记录留在 `docs/data-audit.md`（历史记录，不再对应现行数据）。
>
> **⚠️ 2026-09-17 补充：这份文档贴的数字又过期了一次，方向和上面相反。** 本仓库开发账号的湖
> 已经被重灌成 **8000 万行**那一批（新生成器 `scale≈427`，`orders` 854,140 而不是 2,000，
> 详见 [deployment.md](deployment.md#数据说明) 的逐表对照与核查命令）。所以：**下面每一段
> 贴出来的输出，你自己跑一遍很可能都不一样**，而且差 427 倍。文档写的时候是对的，是湖在它
> 底下被人换掉了 —— 对账层比表、比列、比枚举取值，**从不比文档散文里的数字**，所以这种过期
> 是绿的。**结构性的结论（表之间怎么连、口径陷阱在哪、哪一列不能信）不受影响，照读；具体
> 数值一律当历史快照看。** 判断你手上的湖是哪一批：`SELECT count(*) FROM orders`
> —— 2,000 是种子，854,140 是重灌后那批。

跟它配套的两份文档分工不同：`docs/data-audit.md` 记录 v2 数据集的质量审计（五层审计，
修复前后都有记录）；本文回答「现在这批数据长什么样、怎么用」。

## 第 0 步：连库与三条铁律

所有查询走 Athena（S3 Tables / Iceberg），客户端封装在 `scripts/lakehouse/athena.py`：

```bash
cd ~/Documents/sample-analytics-agent-progressive-disclosure
AWS_REGION=us-west-2 backend/.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "scripts/lakehouse")
from athena import Client
r = Client().execute("SELECT count(*) AS users FROM users")
print(r["columns"], r["rows"])
PY
```

catalog / database 由 `Client` 从环境变量拼好放进 `QueryExecutionContext`，**SQL 里只写裸表名**
（S3 Tables 的 catalog 名带斜杠，写进 SQL 会被 Trino 解析器拒绝，详见 `athena.py` 文件头）。

**铁律一：这是静态快照，禁用 `current_date` / `now()`。** 业务日历止于 2026-01-24，
起于 2025-10-26，共 91 天。所有「最近 7 天」「本月」都要以
`(SELECT max(as_of_date) FROM meta_snapshot)` 为今天，用系统时间会查出空集。
**也不要用每张表自己的 `max(dt)`**——各表轴末端不齐，见第 2 步。

**铁律二：这是 Trino（Athena），不是 Postgres，也不再是 Redshift。** 常踩的几条：

| Postgres 写法 | Athena / Trino 写法 |
|---|---|
| `x::date` | `CAST(x AS date)` |
| `interval '30 days'` | `interval '30' day`（数字在引号里） |
| `DATEDIFF(day, a, b)` | `date_diff('day', a, b)` |
| `dt + 7` | `dt + interval '7' day` |
| `DISTINCT ON (k)` | `row_number() OVER (PARTITION BY k …)` |
| `count(*) FILTER (WHERE c)` | `count(CASE WHEN c THEN 1 END)` |
| `svv_table_info` 等系统视图 | `information_schema.tables` / `.columns` |
| `AS 活跃用户` | `AS "活跃用户"`（非 ASCII 别名必须双引号） |

还有一条只在 Trino 上咬人的：**`JOIN … USING (dt)` 之后 `dt` 不能再带表前缀**，
`g.dt` 会直接报 `COLUMN_NOT_FOUND`。多表同名列一律写 `ON g.dt = f.dt`（见第 9 步）。
完整方言清单在 `knowledge/connection.md`，金标 SQL 的运行时改写器是
`scripts/gen/pg_to_trino.py`。

**铁律三：这是 22 万行的种子数据，不是「真实规模」。** 行数摊薄之后，不少统计形状
直接消失了：留存曲线不衰减、订单没有周末效应、`user_level` 与消费额几乎不相关、
漏斗的**次序**不成立（严格时序口径下退化到末步 0——注意这不等于"漏斗不衰减"，
子集口径下它逐层流失约 20%，见 4.1）。
**拿它验证 SQL、口径、链路和 agent 行为可以；拿它讲业务规律不行。**
**灌到 8000 万行也不行**（本仓库开发账号现在就是那一批）：这些形状坏在**抽样方式**上
——活跃度与注册生命周期独立抽样、事件按种类近似均匀抽出——不是坏在行数少。行数放大
427 倍只是把同一个噪声取更大的样本，曲线还是平的。
每一条都在下面对应步骤里写了实测数字。

## 第 1 步：全貌，48 张表分四层

```sql
SELECT CASE
         WHEN table_name IN ('dwd_orders_valid','dwd_events_app','dws_user_daily',
                             'dws_channel_weekly','fin_daily_revenue','growth_daily_gmv',
                             'orders_backup_20251201','tmp_campaign_roi_analysis')
              THEN '2_derived'
         WHEN table_name LIKE 'mart_%'        THEN '3_mart'
         WHEN table_name =    'meta_snapshot' THEN '4_meta'
         ELSE '1_base'
       END AS tier, count(*) AS tbls
FROM information_schema.tables
WHERE table_schema = 'app_analytics'
GROUP BY 1 ORDER BY 1;
```

```
tier       tbls
1_base     35
2_derived  8
3_mart     4
4_meta     1
```

Athena 的 `information_schema` 不带行数（Iceberg 的统计在 Glue 表元数据里），逐表行数由
`scripts/lakehouse/verify_load.py` 数——它同时把每张表和 `data/csv/` **实时比对**，不读基线：

| 层 | 表数 | 行数 |
|---|---|---|
| 1_base | 35 | 189,672 |
| 2_derived | 8 | 27,982 |
| 3_mart | 4 | 2,432 |
| 4_meta | 1 | 1 |
| **合计** | **48** | **220,087** |

- **base（35 张）**：原始明细，按 8 个业务域组织，是本文的主体
- **derived（8 张）**：DWD 清洗 / DWS 汇总 / ADS 应用 / 历史遗留，**有坑**，见第 9 步
- **mart（4 张）**：治理层，口径冻结的预聚合表，写简单 SELECT 就能用，见第 10 步
- **meta（1 张 1 行）**：数据「今天」的锚点

8 个域和各自的表数：用户 5、商品 3、行为 4、社交 6、交易 4、归因 5、营销 5、实验 3。
每张表的字段卡片在 `knowledge/domains/<域>/<表>.md`，本文只讲骨架，字段表不复述。

## 第 2 步：时间锚点（不止一根轴）

```sql
SELECT * FROM meta_snapshot;
```

```
as_of_date  data_start
2026-01-24  2025-10-26
```

任何带时间的查询都以这一行为基准。「最近 30 天」是

```sql
WHERE dt > (SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day
```

不是 `>= current_date - 30`，**也不是这张表自己的 `max(dt)`**：各表轴末端并不齐。

| 表 | 时间列 | 末端 |
|---|---|---|
| `orders` / `events` / `growth_daily_gmv` / `mart_daily_kpi` | `placed_at` / `event_time` / `dt` | 2026-01-24 |
| `fin_daily_revenue` | `dt` | 2026-02-02 |
| `channel_daily_costs` | **`date`**（不叫 `dt`） | 2026-09-01 |
| `mart_channel_daily` | `dt` | 2026-09-01 |

这不是洁癖。v3 实测踩过一次：「哪个渠道 CAC 最低」用成本表自己的末端当今天，
答成了一个在业务日历里根本没花钱的渠道——**不报错、数看着合理、结论是反的**。
治理层指标按指标决定是否 clamp 到锚点（`backend/metrics_def.py`：`cac`/`roi` clamp，
`refund_amount` 不 clamp）。

## 第 3 步：用户域，从 users 开始

### 3.1 先抽三行

```sql
SELECT user_id, username, email, phone, CAST(registered_at AS varchar) AS reg,
       registration_source, status, user_level, is_vip
FROM users WHERE user_id IN (42, 160, 252) ORDER BY user_id;
```

```
user_id  username  email                 phone        reg                  registration_source  status  user_level  is_vip
42       btan      min31@example.com     18173799650  2025-12-27 05:23:31  web                  active  2           False
160      na04      xqiao@example.org     18683982622  2025-11-28 18:19:23  huawei_store         active  4           False
252      yan71     zhangjun@example.org  15754885102  2025-12-04 12:56:03  referral             active  2           False
```

注意两件事：

1. **`user_id` 只到 500**（全表 500 行），别按 v2 时代的量级猜 id。`orders.order_id`
   更要注意，那是 12 位随机数，见第 5 步。
2. **email / phone 在库里是明文，但 agent 读不到这两列**。上面这段是用管理员凭证
   直接查出来的，所以看得见全部字段。agent 走的是治理层：最小权限只读角色
   `analytics-agent-ro` + Lake Formation **列级授权**（`scripts/lakehouse/governance.py`，
   L4 现在是实测探针，不再是「未实现」）——`users.email` / `users.phone` /
   `user_profiles.birth_date` 不在授权面里（点名查报 `COLUMN_NOT_FOUND`，`SELECT *`
   里也没有），`user_messages` **整表未授权**。注意这是**列级排除，不是脱敏**：
   Lake Formation 没有值级掩码原语，所以库里存的仍然是明文，边界在"这几列/这张表
   不在角色的授权面里"这一件事上。这层只在 `AGENT_ROLE_ARN` 设了的时候生效；没设
   就是拿进程自己的（通常是管理员）凭证查，那时没有任何列级边界。

枚举值以 `knowledge/domains/user/users.md` 为准，那批卡片已按种子数据实测校准过
（`registration_source` 实测 8 种：referral / organic / huawei_store / web / ad_campaign /
wechat_mini / app_store / google_play；`status` 实测 4 种：active / inactive / deleted /
suspended）。**别照 `database/01_user_domain.sql` 里的注释猜枚举**——那行注释写的是
`'app', 'web', 'mini_program'`，其中两个值在数据里不存在，按它写 WHERE 会拿到空集。

### 3.2 user_level 是个标签列，不要拿它验消费口径

知识库里的等级规则（优先级 5 > 4 > 3 > 1 > 2）：5 = 累计消费≥10000 或 VIP；4 = ≥1000；
3 = 近 30 天活跃≥10 天；1 = 注册<30 天；2 = 默认。实测：

```sql
WITH spend AS (
  SELECT u.user_id, u.user_level,
         COALESCE(SUM(CASE WHEN o.status IN ('paid','shipped','delivered')
                           THEN o.actual_amount END), 0) AS sp
  FROM users u LEFT JOIN orders o ON o.user_id = u.user_id GROUP BY 1, 2)
SELECT user_level, count(*) AS users, round(avg(sp),0) AS avg_spend,
       round(100.0*sum(CASE WHEN sp>=1000 THEN 1 ELSE 0 END)/count(*),1) AS pct_ge_1k
FROM spend GROUP BY 1 ORDER BY 1;
```

```
user_level  users  avg_spend  pct_ge_1k
1           184    15857.0    82.1
2           170    19324.0    91.2
3           83     17564.0    88.0
4           45     14563.0    86.7
5           18     14213.0    88.9
```

**这条规则在种子数据上不成立**：各等级平均消费都在 1.4-1.9 万之间，`sp>=1000` 的比例
都在 82-91%，等级与消费额几乎不相关（v2 的放大数据集里等级 4 是 100%、等级 1-3 是 0%）。
原因是种子数据里 `user_level` 独立取值，而 1,601 张有效单摊到 471 个买家身上，人人过千。
结论：**`user_level` 当标签列筛人群没问题（它是真实存在的列），但别用它验证「等级=消费」
这条口径**；要按消费分层，自己从 `orders` 算。

### 3.3 其余四张

`user_profiles`（1:1 画像：年龄、性别、城市、兴趣）、`user_devices`（1:N 设备）、
`user_segments` + `user_segment_members`（分群定义与成员）。关联键都是 `user_id`。

## 第 4 步：行为域，会话套事件

结构：`sessions` 是壳，`events` 和 `page_views` 都挂在会话下。挑一个真实用户看生命周期
（就用第 5 步那笔订单的买家）：

```sql
SELECT CAST(u.registered_at AS date) AS reg, u.user_level,
       count(DISTINCT s.session_id) AS sessions, count(e.event_id) AS events,
       CAST(min(e.event_time) AS date) AS first_ev, CAST(max(e.event_time) AS date) AS last_ev
FROM users u
LEFT JOIN sessions s ON s.user_id = u.user_id
LEFT JOIN events   e ON e.user_id = u.user_id
WHERE u.user_id = 252 GROUP BY 1, 2;
```

```
reg         user_level  sessions  events  first_ev    last_ev
2025-12-04  2           7         245     2025-11-19  2026-01-21
```

看第一列和倒数第二列：**首个事件早于注册日**。这不是个例——499 个有事件的用户里
**427 个**的首事件在注册时间之前，因为种子数据里事件时间与注册时间独立生成。
（v2 的放大数据集里「先注册后行为」是成立的，原文那句话已经不适用。）
做留存 / 新客分析时**不要假设** `min(event_time) >= registered_at`，需要就自己过滤。

`sessions.event_count` / `page_view_count` 在这批数据上**也不是**明细行数的回填：

```sql
WITH a AS (SELECT s.session_id, s.event_count, count(e.event_id) AS real_ev
           FROM sessions s LEFT JOIN events e ON e.session_id = s.session_id GROUP BY 1,2)
SELECT count(*) AS sessions, sum(CASE WHEN event_count <> real_ev THEN 1 ELSE 0 END) AS mismatch
FROM a;
```

5,000 个会话里 **4,932 个** `event_count` 与实际事件行数不等。**要精确数就回 `events` 数**，
这两列只当生成器给的标签列用。

### 4.1 事件量近似均匀 ——「每步各数一遍」不是漏斗

先看每个事件各有多少（**这不是漏斗**，只是各事件的规模）：

```sql
SELECT event_name, count(*) AS n, count(DISTINCT user_id) AS users
FROM events
WHERE event_name IN ('view_home','view_product','add_to_cart','begin_checkout','purchase')
GROUP BY 1 ORDER BY 2 DESC;
```

```
event_name      n    users
begin_checkout  866  396
add_to_cart     838  385
view_product    823  394
view_home       807  373
purchase        772  373
```

2 万条事件按 event_name 近似均匀分布，所以这五个数彼此接近，`begin_checkout` 甚至最多。
**这里最容易走错一步**：把右边那列 `users` 排成一列当漏斗看，于是"发起结算 396 > 浏览商品
394"，得出「这批数据的漏斗不衰减、不是真实业务形态」。这个结论是错的——它是**口径的产物，
不是数据的性质**。四个独立的 `count(DISTINCT user_id)` 量的是四个互不相干的集合各自多大，
漏斗要求第 i+1 步的用户是第 i 步的**子集**。（项目里真发生过一次，记在
`PROJECT_STATUS.md`；SOP 现在写在 `knowledge/analysis/funnel_analysis.md` 的约束 A。）

按子集口径重算，形状很正常：

```sql
WITH w AS (SELECT user_id, event_name FROM events
           WHERE event_name IN ('view_home','view_product','add_to_cart','begin_checkout','purchase')),
s1 AS (SELECT DISTINCT user_id FROM w WHERE event_name='view_home'),
s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name='view_product'
       AND user_id IN (SELECT user_id FROM s1)),
s3 AS (SELECT DISTINCT user_id FROM w WHERE event_name='add_to_cart'
       AND user_id IN (SELECT user_id FROM s2)),
s4 AS (SELECT DISTINCT user_id FROM w WHERE event_name='begin_checkout'
       AND user_id IN (SELECT user_id FROM s3)),
s5 AS (SELECT DISTINCT user_id FROM w WHERE event_name='purchase'
       AND user_id IN (SELECT user_id FROM s4))
SELECT (SELECT count(*) FROM s1) AS view_home,
       (SELECT count(*) FROM s2) AS view_product,
       (SELECT count(*) FROM s3) AS add_to_cart,
       (SELECT count(*) FROM s4) AS begin_checkout,
       (SELECT count(*) FROM s5) AS purchase;
```

```
view_home  view_product  add_to_cart  begin_checkout  purchase
373        299           250          208             165
```

逐层流失 19.8% / 16.4% / 16.8% / 20.7%，整体转化 **44.2%**，单调递减。真正需要打折看的
不是"衰减不衰减"，而是**次序**：这批数据的事件时间与用户行为无因果关系，所以按
`min(event_time)` 逐步比较的**严格时序**漏斗会退化（近 30 天末步是 0）。
上面这条是**无序子集**漏斗（只要求"也做过上一步"），本项目默认用它，`method` 里必须写明。

另外一件与漏斗无关但同样要注意的事：`purchase` 事件 772 条与有效订单 **1,601** 单
**不相等**——v2 数据集里这两个数是精确对齐的，种子数据里不是。`use_coupon`（804）
与券核销数（11,297）、`register`（843）与用户数（500）同样不对齐。
**别用 purchase 事件数替代订单数**，订单口径永远走 `orders`。

### 4.2 留存在这批数据上不衰减

```sql
WITH coh AS (SELECT user_id, CAST(registered_at AS date) AS d0 FROM users),
     act AS (SELECT DISTINCT user_id, CAST(event_time AS date) AS d FROM events)
SELECT date_diff('day', c.d0, a.d) AS lag_days, count(DISTINCT a.user_id) AS active
FROM coh c JOIN act a ON a.user_id = c.user_id
WHERE date_diff('day', c.d0, a.d) IN (0,1,3,7,14,30)
GROUP BY 1 ORDER BY 1;
```

```
lag_days  active
0         37
1         38
3         41
7         30
14        37
30        23
```

D1 甚至比 D0 高。原因就是上面那条：事件时间与注册时间无关，500 个用户的 2 万条事件
均匀铺在 91 天上。**留存题在这份数据上能验证 SQL 写法（cohort、date_diff、去重口径），
验证不了曲线形状**，别拿这里的数字讲留存。

### 4.3 时间形状也是平的

订单的周内分布：最高周日 / 周一各 15.1%，最低周四 12.8%，**没有周末效应**；
小时分布同样接近均匀（最高 21 点、10 点各 99 单，最低 22 点 65 单）。
日订单量 91 天均值 22.0 单、标准差 5.23、变异系数 **0.238**。

## 第 5 步：交易域，追一笔订单走全链路

这一步追一笔真实订单，看表与表怎么咬合。**`order_id` 是 12 位随机数，不是自增**
（实测范围 100,279,120,223 ~ 999,941,310,725），所以先随便捞一个，别按小整数猜。
下面用 `105575697563`：

```sql
SELECT order_id, user_id, status, total_amount, discount_amount, shipping_fee,
       actual_amount, item_count, coupon_id, CAST(placed_at AS date) AS placed
FROM orders WHERE order_id = 105575697563;
```

```
order_id      user_id  status     total_amount  discount_amount  shipping_fee  actual_amount  item_count  coupon_id  placed
105575697563  252      delivered  1224.51       183.68           0.0           1040.83        2           77         2025-11-22
```

表内公式：`actual = total − discount + shipping`（1224.51 − 183.68 + 0 = 1040.83）。

**跟到明细**：

```sql
SELECT item_id, product_name, quantity, unit_price, discount_amount, actual_amount
FROM order_items WHERE order_id = 105575697563 ORDER BY item_id;
```

```
item_id  product_name     quantity  unit_price  discount_amount  actual_amount
4056     优质太平鸟 裤子   1         1181.43     0.0              1181.43
4057     优质德尔玛 拖把   2         21.54       0.0              43.08
```

1181.43 + 43.08 = 1224.51，**明细之和精确等于订单头 `total_amount`**，行数等于
`item_count`。注意订单头的 `discount_amount`（183.68）挂在头上、不摊到明细上，所以
明细之和对的是 `total_amount` 而不是 `actual_amount`。商品维度、品类维度的 GMV
要和订单头口径对齐时，用哪一列想清楚。

**跟到支付**：

```sql
SELECT payment_no, user_id, amount, payment_method, status,
       CAST(paid_at AS varchar) AS paid_at
FROM payments WHERE order_id = 105575697563;
```

```
payment_no       user_id  amount   payment_method  status   paid_at
PAY336792991236  252      1040.83  wechat          success  2025-11-22 01:01:47
```

金额 = 订单 `actual_amount`，用户与订单同人。全库 1,746 条支付：success 1,601（= 有效
单数）、refunded 145（= 退款单数），**没有 failed / pending**，所以算不了支付成功率。
另外 `payments.payment_channel` 这一列在种子数据里**整列为 NULL**（`subscriptions.cancel_reason`
同样），按它切片会得到空结果。

**跟到券——这里断了**：

```sql
SELECT count(*) AS n FROM user_coupons WHERE order_id = 105575697563;   --  0
```

不是这笔订单特殊。`user_coupons.order_id` 里存的是小整数（如 47003），跟
`orders.order_id` 的 12 位随机数**不在同一个 id 空间**：

```sql
SELECT (SELECT count(*) FROM user_coupons WHERE status='used')                       AS used_coupons,   -- 11297
       (SELECT count(*) FROM user_coupons uc JOIN orders o ON o.order_id=uc.order_id) AS matched;        -- 0
```

11,297 张 used 券**一张都 join 不上订单**。DDL 里 `user_coupons.order_id` 只是个带注释的
`BIGINT`、**没有声明外键**，所以外键对账（第 6 步）不会报它。要按券维度算核销，
走 `orders.coupon_id`（593 张有效期内的带券订单，`coupon_id` 全部指向存在的 `coupons` 行）；
要按人算领券，走 `user_coupons.user_id + coupon_id`。
**别写 `user_coupons JOIN orders USING (order_id)`——它返回空集，又是一个不报错的错答案。**
券的三态：used 11,297 / unused 6,854 / expired 4,584（共 22,735 行）。

**订单状态机**：pending → paid → shipped → delivered，旁路 cancelled / refunded。
实测状态分布：delivered 75.5%、cancelled 12.0%、refunded 7.3%、shipped 3.0%、
paid 1.6%、pending 0.7%——绝大部分单子已经走到终态，中间态很少（v2 数据集里各态更均匀）。
有效单（valid）= paid + shipped + delivered = **1,601 单**（80.1%），
GMV（valid、91 天）= **8,571,786.74**。

消费集中度也在这个域：top 1% 用户占 GMV 5.3%，top 10% 占 32.2%（471 个有效单买家），
比真实电商（50-70%）平得多，二八分析的结论会明显温和。

## 第 6 步：商品域，跨域 JOIN 不丢行

商品域三张：`categories`（166 个多级分类）、`products`（200 个 SKU，反范式冗余了销量
评分）、`product_tags`（512 行）。`order_items.product_name` 是下单时冗余的真实商品名，
与 `products.product_name` 一致，不 join 也能直接用。

四表跨域示例（商品 GMV Top3）：

```sql
SELECT p.product_name, c.category_name, CAST(round(sum(i.actual_amount),0) AS bigint) AS gmv
FROM order_items i
JOIN orders o     ON o.order_id = i.order_id
JOIN products p   ON p.product_id = i.product_id
JOIN categories c ON c.category_id = p.category_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 3;
```

```
product_name       category_name  gmv
经典宜家 床上用品   床上用品       322854
时尚H&M 衬衫       衬衫           239604
新款九阳 收纳箱     收纳箱         239171
```

**Iceberg / Athena 不强制外键**（Glue 目录里根本没有约束这个概念），但
`database/0[1-8]_*.sql` 里声明的 **50 个 `REFERENCES` 关系全部零悬空**——逐条
`LEFT JOIN … WHERE 父键 IS NULL` 反查过，`INNER JOIN` 不会静默丢行。
注意「声明了的」这个限定：第 5 步那个 `user_coupons.order_id` 没声明外键，它就是断的。

## 第 7 步：营销域与归因域，两个「合法的缺口」

营销域五张：`campaigns`（50 个运营活动）、`push_notifications`（10,000 条推送，其中
**8,394 条 `campaign_id` 为 NULL**，那是事务型推送，不挂活动，**不是孤儿行**）、
`coupons`（150）/ `user_coupons`（22,735，见第 5 步）、`banners`（119）。

归因域五张：`channels`（14 个渠道）、`ad_campaigns` / `ad_creatives`、
`user_attributions`（350 行，覆盖 350 个用户 = 500 人的 70%；其中 `last_touch` 175 人）、
`channel_daily_costs`（投放成本，算 CAC/ROI 用，时间列叫 `date` 且到 2026-09-01）。

归因有个**刻意留的缺口**：

```sql
SELECT COALESCE(c.channel_name, '(未归因)') AS channel,
       CAST(round(sum(o.actual_amount),0) AS bigint) AS gmv,
       round(100.0*sum(o.actual_amount)/sum(sum(o.actual_amount)) OVER (),1) AS pct
FROM orders o
LEFT JOIN user_attributions ua ON ua.user_id = o.user_id
                              AND ua.attribution_type = 'last_touch'
LEFT JOIN channels c ON c.channel_id = ua.channel_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1 ORDER BY 2 DESC LIMIT 4;
```

```
channel      gmv      pct
(未归因)     5517174  64.4
老用户邀请   382456   4.5
微信公众号   287211   3.4
小红书种草   283791   3.3
```

`last_touch` 只覆盖 175 人，于是 **64.4% 的 GMV 落在未归因**（486 个买过东西的用户里
只有 170 人有 last_touch 记录）。这是治理场景的素材（知识库明确写了），做渠道分析时
必须带上这一行，别当它是 bug 修掉。

`attribution_type` 实测**只有 `first_touch`(175) 和 `last_touch`(175) 两种，没有 `linear`**
（旧文档写过，按它筛是空集）。而且 350 行对应 350 个不同用户——**同一个用户不会同时有两种归因**，
这两个值是把用户切成了两半，不是同一批人的两种视角。所以上面那句 `AND ua.attribution_type =
'last_touch'` 既是去重也是**砍样本**：不加它不会重复计数（每人本来就一行），加了则只剩一半用户。
报数时把用的是哪种归因口径说出来。

## 第 8 步：社交域与实验域

社交域六张：`posts`（1,000 帖）、`post_likes`（35,668，全库最大表）、
`post_comments`（14,000）、`post_shares`（7,000）、`user_follows`（19,165 条关注边）、
`user_messages`（9,253 条私信）。

> `user_messages` 在 v2 是治理层唯一不授权的表（通信内容不是分析素材）。
> **v3 还没恢复这个边界**，agent 现在能读到它，见第 3.1 步的治理缺口说明。

`posts` 上的 `like_count` / `comment_count` / `share_count` 在种子数据上**不是**明细行数的
回填——1,000 篇帖子的 `like_count` **全部**与实际 `post_likes` 行数不等；只有
`view_count >= like_count` 仍然恒成立（0 例外）。要真实互动数就 count 明细。

帖子间的互动分布**接近均匀**（top 10% 帖子只占 13.0% 点赞），帖子维度做不了帕累托；
二八类分析的素材在用户消费侧（也只是「略微」重尾，见第 5 步）。

实验域三张：`ab_tests`（12 个实验）→ `ab_test_variants`（30 个变体）→
`ab_test_assignments`（1,850 条分组记录），分组记录里的 variant 一定属于所分配的 test
（外键零悬空）。这两个域在日常分析里出场少，两句带过；要留意的只有上面帖子互动均匀
那一条：拿这份数据做「爆款内容分析」demo 会得出「没有爆款」的结论，选题前先看附表。

## 第 9 步：派生层，故意造的干扰项

8 张派生表是清洗副本、部门口径表和废弃遗留，名字都与基础表近似，
不查路由直接按名字选表就会选错。三个例子：

**口径打架**。`growth_daily_gmv`（增长部口径：按下单日）和 `fin_daily_revenue`
（财务口径：按支付日、扣运费），两张表口径不同，逐日会有差：

```sql
SELECT CAST(g.dt AS varchar) AS dt, g.gmv AS growth_gmv, f.gross_revenue AS fin_gross,
       round(g.gmv - f.gross_revenue, 2) AS diff
FROM growth_daily_gmv g JOIN fin_daily_revenue f ON g.dt = f.dt
ORDER BY g.dt DESC LIMIT 3;
```

```
dt          growth_gmv  fin_gross  diff
2026-01-24  58367.42    58367.42   0.0
2026-01-23  129697.48   131571.76  -1874.28
2026-01-22  65725.41    66221.23   -495.82
```

问「昨天 GMV 多少」，先问清楚是谁的口径。两个 Trino 陷阱都在这条 SQL 上：
**不能写 `USING (dt)`**（之后 `g.dt` 报 `COLUMN_NOT_FOUND`）；而且 `fin_daily_revenue`
的轴比 `growth_daily_gmv` 长 9 天（到 2026-02-02），`INNER JOIN` 会**静默砍掉**那 9 天。

**假清洗层**。`dwd_events_app` 定义是 `events WHERE user_id IS NOT NULL`，但
`events` 里没有空 user_id，所以它就是 `events` 的整表复制（20,000 行，一行不差）。
`dwd_orders_valid` 确实做了筛选（1,601 = 有效单数）。另外两张汇总表：
`dws_user_daily`（5,285）、`dws_channel_weekly`（137）。

**废表**。`orders_backup_20251201`（765 行，只有 12-01 前的旧备份）和
`tmp_campaign_roi_analysis`（7 行半成品，`roi` 列全 NULL）。遇到名字相似的表，
先读 `knowledge/domains/_derived_overview.md` 和各域的 `_index.derived.md` 再选。

## 第 10 步：mart 层，能抄近路就抄

四张口径冻结的治理表：`mart_daily_kpi`（日核心指标）、`mart_daily_revenue`
（日×渠道×新老客收入）、`mart_channel_daily`（渠道成本与获客）、
`mart_user_summary`（用户一人一行汇总）。

```sql
SELECT CAST(dt AS varchar) AS dt, dau, new_users, orders, paying_users, gmv
FROM mart_daily_kpi ORDER BY dt DESC LIMIT 3;
```

```
dt          dau  new_users  orders  paying_users  gmv
2026-01-24  27   3          10      10            58367.42
2026-01-23  43   4          19      19            129697.48
2026-01-22  39   2          18      18            65725.41
```

mart 的 GMV 口径 = 订单头 `actual_amount`、valid 状态、按下单日，与明细逐日对账
**0 差异**（`mart_daily_kpi` 91 行，其中 90 天能和明细对上、全对；剩 1 天没有 valid 单）。
这条由 `scripts/lakehouse/verify_mart_parity.py` 每次回归时重算，不存基线。
诊断、复盘、趋势、周报类问题直接查 mart，别回明细现算。
全库 GMV 总额（91 天，valid 口径）：**8,571,786.74**。

治理层的官方口径不止 mart 表这一层：GMV / CAC / ROI / 退款这些**指标即函数调用**，
定义在 `backend/metrics_def.py`，agent 走 `call_metric` 工具拿权威数，
`knowledge/metrics/governed_metrics.md` 由注册表生成。别在题目里现推一遍口径。

## 第 11 步：这本说明书和 knowledge/ 的关系

上面这条路线，就是 agent 每次分析走的路线。`knowledge/` 那棵 md 树是同一套
结构的机读版，三层递进：

1. `domains/_index.md`：9 行域路由表 + 关键词规则（本文第 1 步的角色）
2. `domains/<域>/_index.md`：单域的表清单、关系图、场景组合（第 3-8 步每步的开头）
3. `domains/<域>/<表>.md`：字段、枚举、示例 SQL（每步的细节层，本文未收录）

外加 `metrics/`（指标口径）、`analysis/`（分析方法 SOP）、`relationships.md`
（跨表 JOIN 键）、`_derived_overview.md`（第 9 步的选表指南）。人读本文建立地图，
查细节时按同一条路由读对应卡片，和 agent 的 `read_doc` 是一条路。

那棵树的字段与枚举由两个脚本盯着不许漂移：`scripts/lakehouse/reconcile.py`
（声明态 DDL ⟷ Glue 实际态 ⟷ 知识库三方对账，管表和列）和
`scripts/lakehouse/verify_enums.py`（管枚举取值，本次重跑就是它抓出 9 列漂移）。

## 附：已知边界一览

正文各步讲过的边界，集中成一张查询前的对照表。**打 ⚠️ 的是 v2 数据集上成立、
在现在这批种子数据上不成立的**（原文写成了「恒成立」，已改正）：

| 边界 | 实测数字 | 影响 |
|---|---|---|
| 种子数据规模 | 48 表 / 220,087 行（v2 是 8000 万行） | 够验 SQL 与口径，统计形状不可外推 |
| ⚠️ 首事件早于注册 | 499 个有事件用户里 427 个 | 别假设「先注册后行为」 |
| ⚠️ 计数器列不回填 | `sessions` 4,932/5,000、`posts` 1,000/1,000 与明细不等 | 要准数就回明细 count |
| ⚠️ purchase 事件 ≠ 订单 | 772 事件 vs 1,601 有效单 | 订单口径只走 `orders` |
| ⚠️ 券-订单断链 | `user_coupons.order_id` 11,297 行 0 命中（未声明外键） | 核销走 `orders.coupon_id` |
| ⚠️ 留存不衰减 | D0 37 / D1 38 / D7 30 / D14 37 / D30 23 | 曲线形状不可用 |
| ⚠️ 时间形状平 | 周内 12.8-15.1%，日单量 CV 0.238 | 没有周末 / 时段效应 |
| ⚠️ 等级与消费无关 | 各等级 `pct_ge_1k` 82-91% | 能当标签筛人，不能验口径 |
| ⚠️ 漏斗不单调 | begin_checkout 866 > add_to_cart 838 > view_home 807 | 看口径，别看转化率 |
| 消费重尾偏平 | top 10% 用户占 GMV 32.2%（真实 50-70%） | 二八分析结论温和 |
| 帖子互动近均匀 | top 10% 帖子只占 13.0% 点赞 | 帖子维度做不了帕累托 |
| 支付无失败态 | success 1,601 / refunded 145 | 算不了支付成功率 |
| 整列为 NULL | `payments.payment_channel`、`subscriptions.cancel_reason` | 按它切片是空结果 |
| 未归因 GMV | 64.4%（v2 是 51.6%） | 治理场景素材，知识库写明保留 |
| PII 明文入库、靠授权面挡 | 库里 email / phone 是明文；agent 角色读不到这两列，也读不到 `user_messages` | L4 是 IAM + Lake Formation **列级排除**（不是脱敏），见 `governance.py`；只在 `AGENT_ROLE_ARN` 设了时生效 |
| 静态快照 | 业务日历止于 2026-01-24；成本表轴到 2026-09-01 | 一切「最近」以 `meta_snapshot` 为今天 |
| 声明外键零悬空 | 50 / 50 | `INNER JOIN` 不静默丢行 |
