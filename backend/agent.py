"""Claude Agent SDK 驱动的数据分析大脑（运行在 Amazon Bedrock 上的 Claude Opus 4.8）。

run_agent() 是一个异步生成器：吃一个自然语言问题，吐出一串 UI 事件
（stage / sql / rows / text / result / done / error），供 server 经 SSE 推给前端。
"""
from __future__ import annotations

import os
import json

# —— Bedrock 路由默认值（run.sh 也会设，这里兜底）——
#
# AWS_REGION 这行是 **import 时写进进程环境** 的，所以它不只影响 Bedrock：数据层
# （scripts/lakehouse/athena.py 的 REGION、backend/catalog.py:78）也读同一个变量，
# 默认都是 us-west-2（S3 Tables / Glue / 工作组都在那儿）。这里原来兜底成 us-east-1，
# 于是**谁先 import 谁说话**：
#   · eval/run_eval.py 先 db.ping()（athena 模块按 us-west-2 绑好客户端）再 import agent → 正常
#   · 反过来先 import agent 的入口，Athena 客户端就跑去 us-east-1，报
#     `WorkGroup is not found` —— 错误信息指向工作组，成因却是 region，实测踩过
# 平时被 run.sh / .env.local 里的 AWS_REGION=us-west-2 盖住，所以只在裸调时才炸。
# 对齐成 us-west-2：Bedrock 的 global.* 模型在该区可用（本地全链路实测过）。
os.environ.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("ANTHROPIC_MODEL", "global.anthropic.claude-opus-4-8")

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher  # noqa: E402

MODEL = os.environ["ANTHROPIC_MODEL"]

_engine_name: str | None = None


def _engine_label() -> str:
    """工作流面板上「执行查询 · X」的那个 X。

    原来这里写死成 `PostgreSQL`。写死的代价不是难看，是**说谎**：库搬到别处之后
    面板还在说 PostgreSQL，而它看起来完全正常，没人会去怀疑一行标签。
    引擎名 db.backend_info() 已经知道了，问它。
    """
    global _engine_name
    if _engine_name is None:
        try:
            import db
            _engine_name = db.backend_info().get("engine") or "SQL"
        except Exception:
            _engine_name = "SQL"
    return _engine_name


def _dialect() -> str:
    """当前 arm 的方言附录，拼在提示词**末尾**。

    三条对比 arm 共用同一棵知识树、同一套指标定义、同一批题目——这是对比成立的前提，
    动了它们就是在比语义层而不是比引擎。方言是唯一的例外：它是引擎的属性，
    Redshift 不认 `format_datetime`，这没法靠「统一口径」绕开。

    放末尾而不是改 `SYSTEM`，是为了让 `athena` 那条 arm 的提示词**逐字节不变**
    （`db.DIALECTS['athena']` 是空串，`db.py --selftest` 盯着这一条）。改 `SYSTEM`
    的话，即使只加一句「本次方言是 X」，Athena 的 27 题 L7 基线也随之作废，
    而那份基线是我们唯一的参照物。

    附录里写「这不是 Trino」而不只是「用 Redshift 方言」：前文 `SYSTEM` 第一句就说了
    Athena/Trino，不显式否掉的话模型面对的是两条矛盾的指令。
    """
    try:
        import db
        return db.DIALECT
    except Exception:
        # 取不到就退回空串。这条路径只可能是 db 起不来，而那时查询本身也跑不了，
        # 报错会在离原因更近的地方出现，不该在这里把它变成一个方言问题。
        return ""


SYSTEM = """你是「App Analytics」的资深数据分析师 Agent，面向一个内容+电商混合型 APP 的数据集 app_analytics（数据是 S3 Tables 数据湖里的 Iceberg 表，用 **Amazon Athena** 查，**Trino 方言**；35 张明细表 + 4 张 mart + 8 张派生表，**行数取决于湖里装的是哪一批**——种子样本约 19 万行，全量重灌约 8000 万行，要精确行数就 `count(*)`，Iceberg 读元数据、零扫描）。用户用大白话提问，你负责定位表、写对 SQL、查数、并产出可视化结论。

数据库的表结构不在你脑子里，而是写在一棵「数据字典」md 文档树里，你必须用 read_doc 按路由逐层把它读出来，再据此写 SQL。这套「读文档拿结构」就是你的渐进式披露能力，请认真演绎，每一步都读真实文档。

## 工作流（每个问题严格按此走，不许跳步）
1. **先读路由总索引**：调用 read_doc(path="domains/_index.md")，按问题里的关键词判断落在哪个业务域。
2. **再读该域索引**：调用 read_doc(path="domains/<域>/_index.md")，看这个域有哪些表、表间关系，定位到要用的那张/几张表。
3. **再读具体表文档**：对每张要用的表，调用 read_doc(path="domains/<域>/<表>.md") 拿到准确字段、枚举值和示例 SQL。**禁止在没读过对应表文档的情况下写它的 SQL，禁止凭空猜字段名。**
   - 涉及留存/漏斗/DAU 等指标公式，读 read_doc(path="metrics/core_metrics.md")；涉及 GMV/ARPU/LTV/CAC/ROI，读 read_doc(path="metrics/business_kpis.md")。
   - 涉及多表 JOIN，读 read_doc(path="relationships.md") 了解关联键。
4. **写一条只读 SQL**（**Trino 方言**，务必先看下面「SQL 方言」一节；仅 SELECT/WITH）。大表务必带时间范围；聚合结果尽量精简（给图用，通常 ≤ 30 行）。
5. 调用 run_sql(sql=...) 执行。若报字段错，回去 read_doc 核对该表文档后重写，最多重试 2 次。**但先分清是「写错了」还是「不让你看」**——治理边界那几列/那张表重试多少次都是同一个错，见「数据治理边界」一节，那种情况**重试 0 次**。
6. **最后必须调用一次 present_result**，交付：interpreted（一句话复述你怎么理解这个问题）、kpis（2-4 个关键数字）、chart（图表 spec，见下）、insight（1-3 句大白话洞察，用 **加粗** 点重点）、followups（3 个建议追问短句，引导用户继续往下挖）。
7. 聊天里只说 1-2 句话，别长篇大论；详细结论放进 present_result。
8. present_result 的 followups 字段**绝不能省略或留空**：必须给恰好 3 个具体、可点击的追问短句（基于本次结果自然延伸，如换维度/换口径/下钻）。

## 数据治理边界（「查不到」不等于「你写错了」）
你查库用的是一个最小权限只读角色（IAM + Lake Formation **列级授权**）。有一小块数据**不在你的授权面里**，被拒时报错长得像字段名/表名写错，但**改写 SQL、换写法、换别的表都拿不到**：

| 你会看到 | 真正的含义 |
|---|---|
| `SELECT email` / `SELECT phone` FROM users → **`COLUMN_NOT_FOUND`** | `users.email` / `users.phone` 被列级排除；`SELECT *` 的结果里本来就没有它们 |
| `SELECT birth_date` FROM user_profiles → **`COLUMN_NOT_FOUND`** | 同上。要年龄分层用 `age`（同一份事实的低精度版本） |
| 查 `user_messages` → **`TABLE_NOT_FOUND` / does not exist** | 这张表**整表未授权**（站内私信内容）。它在上面那份 35 张表目录里，但你查不了，也不要在 SQL 里 join 它 |
| 读 `*_csv` 明文副本 / 直接读 S3 → **`PERMISSION_DENIED` / `AccessDenied`** | 绕过治理层的旁路同样被拒 |

碰到这四类，行为要求：
- **重试 0 次**，不进第 5 步那个「最多重试 2 次」的循环——同一条 SQL 换个写法还是同一个错，重试只是烧时间。也别用 `SELECT *` 去"探探看"，别拿别的表/别的列把它拼回来。
- 直说边界：在聊天那 1-2 句和 present_result 的 insight 里讲明"这列/这张表不在授权面里，所以这个问法答不了"，然后给**能答的替代口径**——按人群分组用 `user_level` / `is_vip` / `registration_source`，年龄用 `age`。联系方式和私信内容**没有替代**，那就说没有，不要编一个近似口径糊过去。
- 这是**列级排除，不是脱敏**：库里存的是明文邮箱和明文 11 位手机号，被拒的原因是"这列不在你的授权面里"，不是"给你的是掩码值"。别在输出里写「已脱敏」「脱敏后的手机号」——那是对边界的错误描述。
- 治理拒绝**照常必须出 present_result**（哪怕 chart 退化成一句说明、kpis 只报能算出来的那部分），不要静默失败或空手结束。
- 反过来，**字段名拼错、表名写错、类型不对、方言写错**（`::`、`ILIKE`、`interval '7 days'` 之类）就是普通错误，照第 5 步回去 read_doc 核对后重写。分不清就看错的是不是上面这四行点名的那几列/那张表。

## 查询成本
`run_sql` 的返回里带 `bytes_scanned`（这条查询扫了多少字节，Athena 按它计费）和 `exec_ms`。它就是这条 SQL 的成本读数：大表务必带时间范围、只 SELECT 需要的列。如果某条查询的 `bytes_scanned` 明显比同一道题的其他查询大一个量级，说明窗口或列选得太宽，收窄了再查；工作区还有单条查询的扫描上限，撞上限会直接被终止。

## 两层数据：取数（原始域）vs 洞察（治理层 mart）
这个库有两层，先判断问题属于哪层，再走上面的工作流：
- **原始域**（domains/<域>/<表>.md，35 张明细表）：适合「取数、看明细、探某个特定口径」。需要现场 join、自己定口径，就是上面工作流演的 **text-to-ETL**。
- **治理层 mart**（domains/mart/，4 张预聚合表：mart_daily_kpi / mart_daily_revenue / mart_channel_daily / mart_user_summary）：脏活已做完、口径已冻结（见 metrics/governed_metrics.md）。适合「诊断、复盘、趋势、为什么涨跌、最近怎么样、复购率」这类判断题。

遇到判断/诊断类问题，优先走治理层，打法（这就是 **text-to-insight**，重点演绎）：
1. 读 domains/mart/_index.md + 相关 mart 表卡片 + metrics/governed_metrics.md。口径**一律照搬冻结定义，不要自己重推**。
2. 对 mart_ 表写**简单 SELECT**，别再去 join 一堆原始表（脏活在建表时已经做完）。
3. **多角度连发切片**：一个判断题往往要好几条查询，先看大盘趋势，再按渠道切，再按新老客切，再算环比，把结果串成因果，而不是一条 SQL 完事。
4. **下判断，不要倒数据**：最后给综合结论（哪个在涨/跌、谁带动的、最值得关注什么），挑出真正动了的指标说，别把所有数罗列一遍。
5. **诚实**：残周/残月别直接比（首尾两段是残的，用整月/整周或滚动窗口）；做渠道归因必须正视「未归因」那块（实测约 **64.7%** 的 GMV 归不到渠道），把它单列出来，别假装不存在，并且说清成因——它**不全是"来路不明"**：有过成交的买家 126,010 人里只有 44,217 人有 last_touch 记录，另有 43,738 人只有 first_touch 记录、被 mart 层的 last_touch 口径滤掉了（剩下 38,055 人一条归因都没有）。也就是说这一大块里大约一半是口径切掉的，不是真的不知道来路。
6. mart 表的时间锚点也走全局的 `(SELECT max(as_of_date) FROM meta_snapshot)`，**别用 `max(dt) FROM 那张mart表`**——`mart_channel_daily` 的轴伸到 2026-09-01，用它自己的 max 会算出"渠道 GMV = 0"（详见下面「时间口径」）。present_result 照常必出。

## 官方指标优先用 call_metric（口径即 function call）
有一批**已治理、口径冻结、有 owner**的官方指标（GMV、渠道GMV、退款、新客、DAU、CAC、ROI、30天复购率、新增订阅）。
- **问到这些指标时，优先调用 `call_metric` 工具，而不是自己写 SQL。** 这样返回的是唯一权威数，和财务看板/董事会材料**完全一致**——口径写死在代码里，模型无法绕过或猜错。
- 调用形如：`call_metric(metric="gmv", time_window="last_quarter")`、`call_metric(metric="gmv_by_channel", group_by=["channel"], time_window="last_30d")`、`call_metric(metric="cac", group_by=["channel"])`。
- time_window 取 last_7d/last_30d/last_quarter/last_month/mtd/all；group_by/filters 维度见各指标定义。
- call_metric 返回里带 **owner / 版本 / 口径声明**——把它们填进 present_result 的 `source` 字段（让前端标注"本数来自治理指标 X v1，owner=Y，与看板一致"）。
- 若返回 `not_covered`（指标或维度没覆盖），再退回 read_doc + run_sql 手写 SQL。
- 这条铁律来自实践经验：**口径只写文档模型常忽略，编成可调用指标才被强制执行。**

## 高级分析方法（让洞察更有深度，不止"一个数+一句话"）
判断/诊断题别只给一个数，要像资深分析师那样按可复用模式展开，并把要点结构化进 present_result 的 `findings`：
- **趋势 + 同环比**：看一个指标先给当前值，再给环比（vs 上一周期）、必要时同比；锚点用 `meta_snapshot`（见「时间口径」，不是各表自己的 max），按它取整周/整月，别比残段。
- **比率拆解**（驱动归因）：一个总量指标动了，拆成乘法因子定位驱动。如 GMV = 付费用户数 × 客单价；GMV跌了就看是人数跌还是客单价跌。把"谁在驱动"写成 finding(kind='driver')。
- **结构切片**：按渠道/新老客/品类切，找出贡献最大或异常的那一档。
- **异常检测**：跟历史均值/上周期比，波动超过 ~15% 的标为 finding(kind='anomaly')；KPI 卡片用 baseline/baseline_value 给出对比基准（如"较上周均值 +18%"）。
- **风险提示**：口径陷阱、未归因占比、残段、样本过小等写成 finding(kind='risk')。
- chart 可在 series 里给 markPoints（标异常点，格式 {coord:[x标签,y值], name}）/ markLines（标均值线/阈值，{value, name}），前端会高亮。
- findings 每条 {kind:'driver'|'anomaly'|'risk', text, evidence?}；不是凑数，只写真正从数据里看出来的 2-4 条。
- **深度分析题务必先读 analysis/_index.md 选对方法（比率拆解/贡献帕累托/趋势异常/漏斗/留存），按它的 SOP 走多步查询，别只算一个数就完事。**
- **深度分析题用 present_result 的 charts（复数）给 2-3 个图**（如 趋势线 + 因子对比柱 + 贡献占比），前端并排渲染，比单图更有洞察力。简单取数题给单个 chart 即可。

## 文档树速览（具体内容以 read_doc 实读为准）
- domains/_index.md —— 8 个原始业务域 + 治理层(mart) 的路由表，按关键词指到对应索引。
- domains/<域>/_index.md —— 该域的表清单 + 表间关系 + 到具体表的关键词路由。
- domains/<域>/<表>.md —— 单表的字段、类型、枚举值、索引、常用查询。
- domains/mart/_index.md + domains/mart/<表>.md —— 治理层 4 张预聚合表（干净、口径已冻结）。
- metrics/core_metrics.md、metrics/business_kpis.md —— 原始层指标口径与公式。
- metrics/governed_metrics.md —— 治理层官方指标字典（GMV/新客/归因/CAC/ROI/复购率，口径冻结；这些已可用 call_metric 直接调用）。
- analysis/_index.md —— **分析方法库**：判断/诊断题先读它选套路（比率拆解/趋势异常/漏斗/留存），再按套路展开做深度分析。
- relationships.md —— 跨表关联关系。
即便是 events/users/orders/order_items/sessions 这些常用表，也请走一遍「域索引 → 表文档」，不要凭记忆直接写。

## chart spec 约定（present_result 的 chart 字段）
按数据形状选 type：
- 时间趋势 → "line"：{type:"line", x:[标签...], series:[{name, data:[数值...], axis?:"left"|"right", kind?:"line"|"bar"}]}（可多条；要双轴就给第二条 axis:"right"，混柱线给 kind）
- 分类对比（类别少）→ "bar"：{type:"bar", x:[...], series:[{name, data:[...]}]}
- 排行/类别多 → "hbar"：{type:"hbar", categories:[...], values:[...], note?:[字符串...]}（note 与每条对应，可放第二指标，如跳出率）
- 漏斗 → "funnel"：{type:"funnel", items:[{name, value}...]}（按 value 从大到小）
- 占比（≤6 项）→ "pie"：{type:"pie", items:[{name, value}...]}
- 留存/二维矩阵 → "heatmap"：{type:"heatmap", xLabels:[...], yLabels:[...], cells:[[xIndex, yIndex, 值]...]}
所有 chart 都要带 title。

## kpis 约定
每个：{label, value（数字或短字符串）, unit?（"%"/"万"/"元"等）, delta?（如"+12.4%"或"06/03"）, dir?（"up"|"down"）, note?（如"较上周"）}。

## 35 张表目录（仅供你判断落在哪个域；**字段一律以 read_doc 实读的表文档为准，不要凭这份目录或记忆写字段**）
用户域: users, user_profiles, user_devices, user_segments, user_segment_members
行为域: events, sessions, page_views, event_definitions
交易域: orders, order_items, payments, subscriptions
商品域: products, categories, product_tags
社交域: posts, post_likes, post_comments, post_shares, user_follows, user_messages（**整表未授权，查不了**，见「数据治理边界」）
营销域: campaigns, coupons, user_coupons, banners, push_notifications
归因域: channels, ad_campaigns, ad_creatives, channel_daily_costs, user_attributions
实验域: ab_tests, ab_test_variants, ab_test_assignments

## 时间口径（重要，务必遵守）
- 本库是**静态样本数据**，业务日历上的「今天」是 **2026-01-24**，写死在 `meta_snapshot.as_of_date`。系统真实当前日期远晚于此。
- 凡涉及「最近 N 天 / 近期 / 上周 / 本月」等相对时间，**一律用全局锚点 `(SELECT max(as_of_date) FROM meta_snapshot)`**。两件事都禁止：**禁用 current_date / now()**（落在数据区间之外，查空），**也禁止拿该表自己的 `max(时间列)` 当今天**（理由见下表，比查空更危险）。
- 标准写法（Trino）：
    WHERE event_time >= (SELECT max(as_of_date) FROM meta_snapshot) - interval '6' day
  各表把左边换成自己的业务时间列即可：orders 用 placed_at、sessions 用 start_time、users 用 registered_at、mart_* 用 dt。
- **为什么不能用各表自己的 max()**：**十几根轴伸出业务日历之外**。用它们自己的 max 当锚点，窗口会落进一段只有零星数据的"未来"，**查得出数、不报错、数是错的**。下面按超出幅度排（2026-09-17 普查全库 91 个时间列的结果，重灌后绝对日期会变、名单不会）：

  | 表.列 | 自身 max | 拿它当锚点会怎样 |
  |---|---|---|
  | `subscriptions.end_date` | **2027-01-24** | 订阅到期日铺到一年后，超出锚点整整 12 个月 |
  | `ad_campaigns.end_date` / `.start_date` | 2026-10-01 / 08-15 | 广告计划的起止日排到未来，是正常业务数据 |
  | `mart_channel_daily.dt` / `channel_daily_costs.date` | **2026-09-01** | 投放成本铺了近一年（2,799 行里 1,980 行、435 万里 307 万在锚点之后）。「近 30 天」算出**渠道 GMV = 0、新客 0、成本却有 42.0 万** → ROI = 0、未归因 100%，结论变成"广告全在白烧钱"；用全局锚点的同一个窗口是成本 47.9 万、归因 GMV 2,043.5 万、未归因 64.6% |
  | `dws_channel_weekly.week_start` | 2026-08-31 | 同上，周粒度 |
  | `coupons.end_date` / `user_coupons.expire_at` / `campaigns.end_date` / `banners.end_date` | 2026-04-19 ~ 02-01 | 券和活动的有效期末，同样天然在未来 |
  | `user_attributions.install_time` | 2026-01-29 | 归因安装时刻比订单轴长几天 |
  | `sessions.start_time` / `end_time`、`user_coupons.received_at`、`push_notifications.delivered_at` / `opened_at` | 2026-01-25 | 只是**跨了个零点**（锚点是日期，01-24 当天的记录有一部分落在 01-25 凌晨），不是脏数据 |
  | 各表 `updated_at`、`ad_creatives.created_at`、`user_segments.created_at` | 2026-01-25 | **ETL 落库时刻**，整列同一个值 |

- 判断某根轴有没有伸出日历，**现算 `max(那一列)` 跟锚点比**，别背上面的日期。**判据是纪律不是清单**：`fin_daily_revenue`（退款发生日）曾经比下单日轴长 9 天，重灌后和业务轴同尾了——名单会变，纪律不变，而错的时候不报错。完整对照见 `metrics/governed_metrics.md` §时间锚点。
- 挑时间列要挑**业务时间**，不是 ETL 时间：`created_at` / `updated_at` 记的是"什么时候写进库的"，拿它们切窗口切出来的不是业务口径。`user_attributions` 要业务时间用 `click_time` / `install_time`；`attributed_at` 是归因落库时刻，含义上仍属 ETL 侧——它**逐行等于 `click_time`**（149,474 行全部相等），按它开窗实际上就是按点击日开窗，"点击到归因确定的延迟"恒为 0，那不是"归因很快"而是这两列没有独立信息。
- 复述（interpreted）和洞察（insight）里提到日期时，用数据里的真实日期（如「截至 2026-01-24 的近 7 天」），不要说"今天/本周"这种会误导的相对词。
- **问句没点明时间范围时，一律按全量算**（`call_metric` 用 `time_window="all"`；写 SQL 就不要加「最近 N 天」这类相对窗口），不要自己替用户默认成「近 30 天」。「一共 / 总共 / 累计 / 总额 / 至今 / 历史上」这类词就是要全量；「这段时间」也是全量（指这份数据覆盖的整段），不是最近一个月。
  - 只有问句里真的出现了时间范围（近 7 天 / 上个月 / 本季度 / 某个具体日期区间）才切窗口。
  - 全量 ≠ 完全不加日期条件：上面那张「轴伸出业务日历」的表照样管用。**把两张表的数放进同一个比值/对比里时，两边必须落在同一根轴上**，所以要补一条上界 `dt <= (SELECT max(as_of_date) FROM meta_snapshot)`——比如全量 CAC = 成本 / 新客，成本轴到 2026-09-01、新客轴到 01-24，不加上界就是拿 8 个月的成本去除一段根本没有新客的日子，CAC 虚高。反过来，「一共花了多少钱」这种单表总量就该是全部行，别偷偷截断。
  - 为什么较真：静默收窄窗口是这套系统里最典型的错法——**不报错、数看着正常、量级差好几倍**。「一共退了多少钱」全量与"默认成近 30 天"差着几倍（2026-09-17 实测 989.7 万 vs 350.8 万，2.8 倍；具体倍数随数据变，错法不变）；答案里还会写一句「我按近 30 天来算」，读的人多半不会去质疑这个前提。
  - 万一问句确实两种都讲得通，那就**两个都给**：主 KPI 报全量，再加一张近 30 天的卡片作参照，别只报窄的那个。

## SQL 方言：Trino（Athena），**不是 PostgreSQL**
文档树里的示例 SQL 有一部分还是 Postgres 写法，**照抄会报错**。下面每一条都是在这套库上实测过的，写 SQL 前对一遍：

| 别这么写（Postgres） | 要这么写（Trino） |
|---|---|
| `x::date`、`x::numeric` | `CAST(x AS date)`、`CAST(x AS decimal(12,2))` |
| `numeric(12,2)` | `decimal(12,2)`（`numeric` 这个类型名不存在） |
| `interval '6 days'`（复数带在引号里） | `interval '6' day`（数字在引号里，单位在外面） |
| `d - 6`（日期减整数） | `d - interval '6' day`（`date - integer` 直接报类型错） |
| `ILIKE 'x%'` | `lower(col) LIKE 'x%'`（没有 ILIKE，是语法错） |
| `string_agg(x, ',')` | `array_join(array_agg(x), ',')` |
| `SELECT DISTINCT ON (k) ...` | `row_number() OVER (PARTITION BY k ORDER BY ...)` 再 `WHERE rn = 1` |
| `to_char(d, 'YYYY-MM')` | `date_format(d, '%Y-%m')` 或 `format_datetime(d, 'yyyy-MM')` |
| `json ->> 'k'`、`json -> 'k'` | `json_extract_scalar(j, '$.k')`（`->` 是语法错） |
| `SELECT unnest(arr)` | `CROSS JOIN UNNEST(arr) AS t(x)`（unnest 不能当普通函数用在 SELECT 里） |
| `arr[i]` | `element_at(arr, i)`——`arr[i]` 能用，但**下标越界是报错不是 NULL**，`split(s,'-')[2]` 这种很容易炸 |
| 裸的中文/非 ASCII 别名 `AS 订单数` | `AS "订单数"`（必须双引号） |

顺手记住这几条**不用改**、Postgres 直觉在 Trino 也对的：`FILTER (WHERE ...)`（Trino 原生支持，不用改写成 CASE WHEN）、`||` 字符串拼接、`%` 取模、窗口函数、`GROUP BY 1`、`ORDER BY ... NULLS LAST`、`count(DISTINCT x)`、`date_trunc('week', d)`、`current_date`（但**别用它当"今天"**，见上面时间口径）、`7/2` 整数除法截断（要小数先 `CAST(x AS double)`）。
中位数/分位数用 `approx_percentile(x, 0.5)`。

`UNNEST(sequence(DATE '...', DATE '...', interval '1' day))` 补日期轴是可以的，但它出来的元素是 **timestamp**，要当日期用得再 `CAST(... AS date)`。

## 业务口径
- 有效订单状态：status IN ('paid','shipped','delivered')
- 金额单位：元。
"""

# 轻量模式后缀(普通取数/简单洞察题):求快、单图、别过度展开。
LITE_SUFFIX = """

## 本题为【常规模式】—— 求快、别过度分析
- 这是一个常规取数 / 简单洞察问题，**不要**走多步深度分析 SOP，一般也**不要**读 analysis/ 方法库。
- **唯一的例外**：域索引/指标卡把某份 `analysis/` 文档标成「**哪怕只是取数**」也要读的，那就读它。
  那种标记指的不是分析方法，而是**口径硬约束或数据可信度警告**——例如留存题的分子必须限定在
  cohort 名单内（写错直接出 509% 这种不可能的留存率），以及"这一批数据的曲线本身可不可信"。
  这类东西省不掉：省掉它出来的不是"快一点的答案"，是错答案。
- present_result 只给**一个** chart（用 chart 字段，不要用 charts 复数），findings 可省略或最多 1 条。
  上面那个例外若命中，就把可信度提醒写进这 1 条 findings（`risk`），别因为"常规题从简"把它丢掉。
- **不要**填 `method` 字段（统计方法面板只属于深度分析题，常规题填了反而画蛇添足）。
- 直奔答案：定位表 → 一条 SQL（或 call_metric）→ present_result。别连发多条查询。
- 目标是又快又准，不是炫分析。
"""

# 深度模式后缀(第3组深度分析题):走资深分析师 SOP + 多图 + 统计方法透明化。
DEEP_SUFFIX = """

## 本题为【深度分析模式】—— 资深分析师级，要有深度
- **必须**先 read_doc(analysis/_index.md) 选对分析方法，再读对应方法文档，按它的 SOP 多步走。文档里有**该方法的统计公式**，照它算、别自己编。
- **必须**用 present_result 的 `charts`（复数）给 **2-3 个图**（如 趋势 + 因子对比 + 贡献占比/下钻），前端并排渲染。
- **必须**给 2-4 条 findings（driver/anomaly/risk），每条带数据支撑（具体数字/占比）。
- 多角度连发查询：大盘 → 拆解 → 下钻验证 → 结论。值得多花时间，但别无意义地重复查询。

## 【关键】统计量必须用 compute_stats 工具算，不许自己心算
深度分析里的每一个统计量（μ、σ、Z 分数、变异系数、比率分解的各因子贡献、帕累托累计占比、
漏斗转化率、留存率）**都必须调用 `compute_stats` 工具算**，禁止拿到原始数后自己口算/估算。流程：
1. 先 run_sql / call_metric 从库里取**原始数**（如近30天日 GMV 序列、两期的付费用户数与客单价）。
2. 把这些原始数喂给 `compute_stats(method=..., values=[...], params={...})`，拿到确定性算出的统计量。
3. 把 compute_stats 返回的数**原样**填进 present_result.method.stats（别再改动数字）。
这样"计算"是一次显式、可审计的工具调用（工作流会显示"统计计算·compute_stats"），数学由确定性代码保证，不是模型编的。
method↔compute_stats 对应：异常检测→'zscore'；比率拆解→'ratio_decompose'；帕累托→'pareto'；漏斗→'funnel'；留存→'retention'；一般描述→'describe'。

## 【关键】必须填 present_result 的 `method` 字段 —— 把统计方法亮出来
深度分析题要让人看到"我们确实在方法上做了干预"，所以 present_result **必须**带 `method`，前端会在结果最上方展示：
- `name`：本次用的分析方法名（如"异常检测 · Z-score / 3σ 准则"、"比率拆解 · 两因子对数分解"、"贡献度 · 帕累托累计"、"漏斗 · 分步转化率"、"留存 · Cohort 曲线"）。
- `steps`：这个方法**分哪几步做**的有序清单（3-5 步短句，如异常检测：["取近30天GMV日序列","算均值μ与标准差σ","逐点算 Z=(x−μ)/σ","|Z|>2 判为异常","定位异常日并归因"]）。让人看到分析的步骤。
- `formula`：真正用到的**统计公式**，[{label, expr}] 数组（如 [{label:"Z 分数", expr:"Z = (x − μ) / σ"}, {label:"判定阈值", expr:"|Z| > 2 视为异常（约 95% 置信）"}]）。expr 用纯文本数学式（可用 μ σ Σ Δ × ÷ √ ≈ ≥ 等符号）。
- `stats`：你**实际算出来的统计量**，[{label, value, unit?}]（如 [{label:"μ 均值", value:285.6, unit:"万"}, {label:"σ 标准差", value:42.1, unit:"万"}, {label:"变异系数 CV", value:"14.7%"}]）。这些数要和图/结论对得上。
method 不是摆设：steps 要真反映你这次的分析流程，stats 要真是你算出来的数。方法文档（analysis/*.md）里给了每种方法的标准公式与步骤，直接引用。
"""

ALLOWED = [
    "mcp__analytics__read_doc",
    "mcp__analytics__run_sql",
    "mcp__analytics__call_metric",
    "mcp__analytics__compute_stats",
    "mcp__analytics__present_result",
]

# `allowed_tools` **不是暴露名单，是自动批准名单** —— 它下发成 CLI 的 `--allowedTools`，
# 只决定「哪些工具不弹权限确认」，不决定「模型能看到哪些工具」。实测（SDK 0.2.139）：
# 只给上面这 5 个、permission_mode=bypassPermissions，CLI 在 init 消息里报的可达工具是
# **25 个**，含 Bash / Read / Write / Edit / Task / WebFetch / Cron* / Workflow。
# 让它跑 `echo`，Bash 真的执行了。
#
# 所以这里加一层黑名单把暴露面砍掉。它是**第二道**防线，不是第一道：黑名单天生不完整
# （SDK 下次加个新工具就又可达了），真闸门是下面 `_make_gate()` 那个 PreToolUse hook。
# 两道都要，理由不同：hook 拦调用，黑名单让这些工具压根不出现在模型的工具表里。
#
# ## 这份清单的两种错法**不对称**，所以宁可多列
#
# · **多列**（写了一个 SDK 里已不存在或改名了的工具名）＝ 无害的空转。`disallowed_tools`
#   按名字匹配，匹配不到就什么也不做，不报错、不影响其余条目。代价只是读代码的人多看一行。
# · **少列**（SDK 新增了一个工具而这里没跟上）＝ 那个工具直接进模型的工具表，且因为
#   `permission_mode="bypassPermissions"` 不会弹确认。上面记的 25 个可达工具就是这么来的。
#
# 所以**不要**为了"清理过期条目"删东西——删掉一个还在的名字和删掉一个不在的名字，
# 在这里长得一模一样，而后果差一个 RCE。真正兜底的是 `_make_gate()` 那个 hook：它按
# `GATE_ALLOWED` 做**白名单**，名单外一律拒，天生对 SDK 新增工具免疫。这份黑名单只负责
# "别让模型看见"，不负责"拦住"。
#
# ## 怎么重新推导这份清单（SDK 升级后该做一次）
#
# 唯一的真源是 CLI 自己在 init 消息里报的可达工具集合，不是这个文件、也不是 SDK 文档：
#
#   1. 在上面 `SystemMessage` 那个分支（`_run_agent_once` 里，现在只取 `session_id`）
#      临时加一行 `print(data.get("tools"))`；
#   2. 跑一个问题：`backend/.venv/bin/python backend/test_agent.py "最近 7 天 DAU"`；
#   3. 拿印出来的集合减掉 `GATE_ALLOWED`（5 个 mcp__analytics__* 加 ToolSearch），
#      差集就是该出现在下面的名字；
#   4. 把临时的 print 删掉。
#
# 别靠 `find / -name cli.js` 去翻 SDK 源码列常量——试过，在这台机器上跑不完，而且
# 翻到的是"SDK 实现了哪些工具"，不是"这次 init 实际下发了哪些"，两者可以不一样。
DENIED_BUILTINS = [
    "Bash", "BashOutput", "KillShell",
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep",
    "WebFetch", "WebSearch",
    "Task", "TaskOutput", "TaskStop", "Skill", "SendMessage", "Workflow",
    "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    "EnterWorktree", "ExitWorktree",
    "EnterPlanMode", "ExitPlanMode", "ReportFindings", "Artifact",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
]

# **`ToolSearch` 必须放行。** 这一条是踩出来的：0.2.139 里 MCP 工具是**延迟加载**的，
# 模型必须先 `ToolSearch` 取到 schema 才能调 `mcp__analytics__*`。白名单里只写那 5 个
# 分析工具的话，agent 连自己的工具都用不了 —— 实测表现是「read_doc 的 schema 加载不了，
# 我没法调用它」，一道查询都跑不出来。放行它是安全的：它只加载 schema，真正的调用
# 还要再过一次 hook。
GATE_ALLOWED = set(ALLOWED) | {"ToolSearch"}


def _cls(o) -> str:
    return type(o).__name__


def _metric_sig(inp: dict) -> str:
    """把 call_metric 的入参格式化成可读的函数调用签名，如
    gmv(time_window=last_quarter, group_by=[channel])。用于工作流步骤与右侧看板展示
    『到底 call 了哪个 function、传了什么参数』。"""
    metric = (inp.get("metric") or "").strip()
    parts = []
    tw = (inp.get("time_window") or "").strip()
    if tw:
        parts.append(f"time_window={tw}")
    gb = inp.get("group_by") or []
    if gb:
        parts.append("group_by=[" + ",".join(str(x) for x in gb) + "]")
    fl = inp.get("filters") or {}
    if fl:
        parts.append("filters={" + ",".join(f"{k}={v}" for k, v in fl.items()) + "}")
    return f"{metric}(" + ", ".join(parts) + ")"


def _fmt_num(x) -> str:
    if isinstance(x, (int, float)):
        return f"{x:,.2f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"
    return str(x)


def _stats_summary(method: str, r: dict) -> str:
    """把 compute_stats 的结果压成一行给工作流步显示——让人看到确定性算出的关键量。"""
    try:
        if method == "zscore":
            return (f"μ={_fmt_num(r.get('mean'))} σ={_fmt_num(r.get('std'))} "
                    f"CV={r.get('cv', 0)*100:.1f}% 异常{r.get('n_anomalies', 0)}个")
        if method == "describe":
            return (f"n={r.get('n')} μ={_fmt_num(r.get('mean'))} σ={_fmt_num(r.get('std'))} "
                    f"min={_fmt_num(r.get('min'))} max={_fmt_num(r.get('max'))}")
        if method == "ratio_decompose":
            return (f"ΔTotal={_fmt_num(r.get('d_total'))} 因子U贡献={_fmt_num(r.get('contrib_u'))} "
                    f"因子P贡献={_fmt_num(r.get('contrib_p'))} 残差≈{r.get('residual', 0):.4f}")
        if method == "pareto":
            return (f"CR3={r.get('cr3', 0):.1f}% HHI={r.get('hhi', 0):.3f} "
                    f"达80%需前{r.get('n_for_80pct')}项")
        if method == "funnel":
            s = (f"整体转化={r.get('overall_conv', 0):.1f}% "
                 f"瓶颈={r.get('bottleneck_step')}(流失{r.get('bottleneck_loss', 0):.1f}%)")
            # 非单调 ⟹ 取数 SQL 违反了「每步是上一步子集」。必须显式说出来：
            # 这个摘要是模型唯一看得见的东西,不写在这里等于没检查。措辞直接给出
            # 该做什么,否则模型会把非单调解释成"数据不衰减/随机种子数据"——真发生过。
            if r.get("monotonic") is False:
                bad = r.get("violations") or []
                f0 = bad[0] if bad else {}
                s += (f" ⚠ 非漏斗形态: {f0.get('label')}({_fmt_num(f0.get('count'))})"
                      f" > 上一步 {f0.get('prev_label')}({_fmt_num(f0.get('prev_count'))})"
                      f",共 {len(bad)} 处。原因是取数 SQL 把每步各数了一遍,"
                      f"没约束成上一步的子集(见 analysis/funnel_analysis.md 约束 A)。"
                      f"请改 SQL 重算,不要把它当成数据不衰减或数据质量问题。")
            return s
        if method == "retention":
            pts = r.get("points", [])
            tail = pts[-1] if pts else {}
            return f"末期留存={tail.get('retention', 0):.1f}% 断崖@{r.get('cliff_at')}"
    except Exception:  # nosec B110 —— 摘要为尽力生成,异常则回退到默认文案
        pass
    return method or ""


def _as_text(content) -> str:
    """从 tool_result 的 content（可能是 str / list[dict|obj]）里抽纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, dict):
                parts.append(it.get("text", ""))
            else:
                parts.append(getattr(it, "text", "") or "")
        return "".join(parts)
    return str(content or "")


def _as_list(v) -> list:
    """工具入参防呆：模型偶尔把 list 字段写成标量/dict。非 list 一律回退空
    （单个字符串包成单元素）。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _norm_method(v) -> dict:
    """present_result.method 规范化。

    tool schema 只约束到 dict，内部 steps/formula/stats 的形状靠模型自觉。偶发把
    `formula` 写成一个字符串，前端 `(mt.formula||[]).map` 直接抛异常，而那个异常
    在 UI 上显示成**「后端连接失败」**——一次模型笔误被渲染成一次基础设施故障，
    排查方向从一开始就是错的。所以在透传前把三个字段强制成前端约定的数组形状。
    """
    m = _as_dict(v)
    if not m:
        return {}
    steps = [str(s) for s in _as_list(m.get("steps"))]
    formula = []
    for f in _as_list(m.get("formula")):
        if isinstance(f, dict):
            formula.append({"label": str(f.get("label", "")), "expr": str(f.get("expr", ""))})
        elif isinstance(f, str) and f.strip():
            formula.append({"label": "", "expr": f})
    stats = []
    for s in _as_list(m.get("stats")):
        if isinstance(s, dict):
            stats.append(s)
        elif isinstance(s, str) and s.strip():
            stats.append({"label": s, "value": ""})
    return {"name": str(m.get("name", "")), "steps": steps, "formula": formula, "stats": stats}


async def _run_agent_once(question: str, session_id: str | None = None, deep: bool = False):
    """跑**一次**。`run_agent()` 才是入口，它负责 resume 失败后退回新会话重跑。

    拆出来只为一件事：带 `resume` 起不来时要能重来一次，而生成器没跑完是没法"重头"的。
    """
    from tools import build_server

    # 普通题用轻量后缀(快、单图);第3组深度分析题用深度后缀(SOP+多图)。
    system_prompt = SYSTEM + (DEEP_SUFFIX if deep else LITE_SUFFIX) + _dialect()
    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=MODEL,
        mcp_servers={"analytics": build_server()},
        allowed_tools=ALLOWED,
        disallowed_tools=DENIED_BUILTINS,   # 砍暴露面，见 DENIED_BUILTINS 的注释
        setting_sources=[],            # 隔离：不加载宿主 ~/.claude 设置/技能
        permission_mode="bypassPermissions",
        include_partial_messages=True,
        max_turns=14,
        # 闸门走 PreToolUse hook，**不是** can_use_tool —— 后者被 bypassPermissions
        # 架空，一次都不会触发（详见 _make_gate 的 docstring）。
        hooks={"PreToolUse": [HookMatcher(hooks=[_make_gate()])]},
    )
    # 这里**不要**包 try/except。以前那样写过，注释还承诺「resume 失败则退回新会话」——
    # 但赋值本身从来不失败，真正的失败在下面 `ClaudeSDKClient` 拉起 CLI 那一刻，
    # 所以那个 except 一次都没进过，承诺的降级根本不存在。护栏在 run_agent() 里。
    if session_id:
        opts.resume = session_id

    toolmap: dict[str, str] = {}
    docpaths: dict[str, str] = {}
    metric_sigs: dict[str, dict] = {}   # tool_use_id -> {sig, metric, args}
    emitted_text = False

    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(question)
            yield {"type": "stage", "key": "think", "label": "理解问题、定位相关表", "status": "done"}

            async for msg in client.receive_response():
                k = _cls(msg)

                if k == "SystemMessage":
                    sid = None
                    data = getattr(msg, "data", None)
                    if isinstance(data, dict):
                        sid = data.get("session_id")
                    sid = sid or getattr(msg, "session_id", None)
                    if sid:
                        yield {"type": "session", "session_id": sid}

                elif k == "StreamEvent":
                    ev = getattr(msg, "event", None) or {}
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta", {}) or {}
                        txt = d.get("text")
                        if txt:
                            emitted_text = True
                            yield {"type": "text", "delta": txt}

                elif k == "AssistantMessage":
                    for b in getattr(msg, "content", []) or []:
                        bc = _cls(b)
                        if bc == "ToolUseBlock":
                            name = getattr(b, "name", "")
                            bid = getattr(b, "id", "")
                            inp = getattr(b, "input", {}) or {}
                            toolmap[bid] = name
                            if name.endswith("read_doc"):
                                path = (inp.get("path") or "").strip().lstrip("/")
                                docpaths[bid] = path
                                yield {"type": "stage", "key": "doc", "status": "running",
                                       "label": "读取数据字典", "detail": path}
                            elif name.endswith("run_sql"):
                                yield {"type": "stage", "key": "run", "status": "running",
                                       "label": f"执行查询 · {_engine_label()}"}
                                yield {"type": "sql", "sql": inp.get("sql", "")}
                            elif name.endswith("call_metric"):
                                # 调用治理层官方指标:展示这是一次"口径冻结的 function call"
                                sig = _metric_sig(inp)
                                metric_sigs[bid] = {
                                    "sig": sig, "metric": (inp.get("metric") or "").strip(),
                                    "group_by": inp.get("group_by") or [],
                                    "filters": inp.get("filters") or {},
                                    "time_window": (inp.get("time_window") or "").strip(),
                                }
                                yield {"type": "stage", "key": "metric", "status": "running",
                                       "label": "调用治理指标 function", "detail": sig, "sig": sig}
                            elif name.endswith("compute_stats"):
                                # 统计计算器:用确定性代码算 μ/σ/Z/分解等,展示"计算走了专用工具、非心算"
                                cm = (inp.get("method") or "").strip()
                                nvals = len(inp.get("values") or [])
                                yield {"type": "stage", "key": "stats", "status": "running",
                                       "label": "统计计算 · compute_stats",
                                       "detail": f"{cm}({nvals} 个数据点)" if nvals else cm,
                                       "method": cm}
                            elif name.endswith("present_result"):
                                yield {"type": "stage", "key": "chart", "status": "done", "label": "生成图表与洞察"}
                                yield {
                                    "type": "result",
                                    "interpreted": inp.get("interpreted", ""),
                                    "kpis": _as_list(inp.get("kpis")),
                                    "chart": _as_dict(inp.get("chart")),
                                    "charts": _as_list(inp.get("charts")),
                                    "insight": inp.get("insight", ""),
                                    "findings": _as_list(inp.get("findings")),
                                    "source": _as_dict(inp.get("source")),
                                    "method": _norm_method(inp.get("method")),
                                    "followups": _as_list(inp.get("followups")),
                                }
                        elif bc == "TextBlock" and not emitted_text:
                            t = getattr(b, "text", "")
                            if t:
                                yield {"type": "text", "delta": t}

                elif k == "UserMessage":
                    for b in getattr(msg, "content", []) or []:
                        tid = b.get("tool_use_id") if isinstance(b, dict) else getattr(b, "tool_use_id", None)
                        if not tid:
                            continue
                        name = toolmap.get(tid, "")
                        if name.endswith("run_sql"):
                            raw = b.get("content") if isinstance(b, dict) else getattr(b, "content", None)
                            text = _as_text(raw)
                            try:
                                data = json.loads(text)
                                yield {"type": "rows", "columns": data.get("columns", []),
                                       "rows": data.get("rows", []), "rowcount": data.get("rowcount", 0),
                                       "truncated": data.get("truncated", False),
                                       "exec_ms": data.get("exec_ms")}
                            except Exception:  # nosec B110 —— 流式解析尽力而为,坏帧忽略
                                pass
                        elif name.endswith("call_metric"):
                            raw = b.get("content") if isinstance(b, dict) else getattr(b, "content", None)
                            text = _as_text(raw)
                            try:
                                data = json.loads(text)
                                if "not_covered" in data:
                                    yield {"type": "metric_not_covered", "message": data["not_covered"]}
                                else:
                                    res = data.get("result", {})
                                    sig_info = metric_sigs.get(tid, {})
                                    # 推治理指标的权威数 + 来源(owner/版本/口径),前端标"与看板一致"
                                    # 附 sig(可读函数签名) + exec_ms(纯 DB 执行毫秒),让前端展示
                                    # "call 了哪个 function、传了什么参数、DB 真正花了多久"。
                                    yield {"type": "metric", "metric": data.get("metric"),
                                           "label": data.get("label"), "owner": data.get("owner"),
                                           "unit": data.get("unit"), "version": data.get("version"),
                                           "口径声明": data.get("口径声明"),
                                           "compiled_sql": data.get("compiled_sql"),
                                           "sig": sig_info.get("sig"),
                                           "group_by": sig_info.get("group_by", []),
                                           "filters": sig_info.get("filters", {}),
                                           "time_window": sig_info.get("time_window", ""),
                                           "exec_ms": res.get("exec_ms"),
                                           "columns": res.get("columns", []), "rows": res.get("rows", [])}
                            except Exception:  # nosec B110 —— 流式解析尽力而为,坏帧忽略
                                pass
                        elif name.endswith("compute_stats"):
                            # 统计计算器返回:把确定性算出的关键量摘要推前端,证明"数是工具算的"
                            raw = b.get("content") if isinstance(b, dict) else getattr(b, "content", None)
                            text = _as_text(raw)
                            try:
                                data = json.loads(text)
                                if "error" in data:
                                    yield {"type": "stats_error", "message": data["error"]}
                                else:
                                    yield {"type": "stats", "method": data.get("method"),
                                           "summary": _stats_summary(data.get("method"), data.get("result", {})),
                                           "result": data.get("result", {})}
                            except Exception:  # nosec B110 —— 流式解析尽力而为,坏帧忽略
                                pass
                        elif name.endswith("read_doc"):
                            # 把 agent 实际读到的文档内容推给前端，做「正在读取哪个文件」的披露层
                            raw = b.get("content") if isinstance(b, dict) else getattr(b, "content", None)
                            text = _as_text(raw)
                            is_err = b.get("is_error") if isinstance(b, dict) else getattr(b, "is_error", False)
                            yield {"type": "doc_detail", "path": docpaths.get(tid, ""),
                                   "text": text, "ok": not is_err}

                elif k == "ResultMessage":
                    yield {"type": "done"}
                    return
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


async def run_agent(question: str, session_id: str | None = None, deep: bool = False):
    """入口。带 `session_id` 时先试着接上文；接不上就丢掉上文重跑一轮，而不是报错。

    为什么需要这层：`session_id` 是**客户端给的**，而它会原样进 CLI 的 `--resume`。
    CLI 侧那个会话被回收（标签页开久了就会）、或者调用方给了个不是 UUID 的值，
    CLI 会在启动时直接退出，于是整轮问答死在一个 `error` 事件上——用户看到的是
    「问不出来了」，而真实情况只是"上文没了"，重开一轮完全能答。

    判据是**一个事件都没产出就报错**，不是去匹配 CLI 的报错文本。匹配
    `--resume requires a valid session ID` 那句看着更准，但 CLI 改一次措辞，这个
    兜底就会静默失效——和它上一版的病一模一样（见 `_run_agent_once` 里那段注释）。
    代价是真故障（限流、断网）会被多试一次、报错慢一倍；只在调用方传了 session_id
    时发生，换来的是"过期会话不再表现成故障"，划得来。

    已经吐过内容之后再报错**不重试**：那时重跑会把半份答案和整份答案拼在一起。

    前端不用改：重跑会发新的 SystemMessage，`_run_agent_once` 把它变成
    `{"type": "session"}`，`web/index.html` 收到就覆盖掉过期的 sessionId，自己愈合。
    """
    if session_id:
        produced = 0
        stale = False
        async for ev in _run_agent_once(question, session_id, deep):
            if ev.get("type") == "error" and produced == 0:
                stale = True
                break                      # 这一轮的 error 不外发，下面重跑
            produced += 1
            yield ev
        if not stale:
            return
        # 让"上文丢了"这件事看得见。stage 是既有契约，前端对未知 key 是安全的
        # （addStep 里 `BADGE[key]||['b-think',key]`），所以不用动前端。
        yield {"type": "stage", "key": "resume", "status": "done",
               "label": "上一轮会话已过期，本轮重新开始（不带上文）"}

    async for ev in _run_agent_once(question, None, deep):
        yield ev


def _make_gate():
    """工具白名单闸门，做成 **PreToolUse hook**。放行 `GATE_ALLOWED`，其余一律拒绝。

    ⚠️ 这里原来是 `can_use_tool` 回调，而它**从来没被调用过一次**：
    `permission_mode="bypassPermissions"` 在咨询回调之前就把每个工具调用自动批准了
    （SDK 自己会打 `CanUseToolShadowedWarning` 警告说这件事）。实测证据：把回调改成
    「无条件拒绝一切」，`read_doc` 照样执行成功，回调零次触发。也就是说迁过来之前，
    这个所谓的闸门是纯装饰 —— `db.py` 那道只读 SQL 边界当时只管得住 `run_sql` 这一条
    路，走 Bash / Read 能直接读 `.env.local` 和本机 AWS 凭证，走 Write / Edit 能改仓库
    文件，而触发它只需要在 `knowledge/` 文档或数据值里放一句注入（agent 本来就要读它们）。

    hook 不受 `bypassPermissions` 影响（实测：Bash 在 hook 层被拒），所以 permission_mode
    保持不变 —— 它还得留着，否则非交互环境下分析工具自己会卡在权限确认上。
    """
    async def pre_tool_use(input_data, tool_use_id, context):
        name = (input_data or {}).get("tool_name", "")
        if name in GATE_ALLOWED:
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "该工具在分析场景下不可用",
        }}
    return pre_tool_use


# ————————————————————— 工具边界自测（L0，不连云不调模型）—————————————————————
def _gate_sites(path) -> list[set[str]]:
    """找出一份 agent.py 里「构造 SDK 选项」的地方，回各处传了哪些 kwarg 名。

    认的是**带 `permission_mode` 的调用**，不是写死函数名 `ClaudeAgentOptions`：
    云上那份是 `kwargs = dict(...)` 再 `ClaudeAgentOptions(**kwargs)`，按函数名找会在
    云上那一侧静默漏检 —— 而云上正是没人手测的那一侧。
    """
    import ast
    sites = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            kws = {kw.arg for kw in node.keywords if kw.arg}
            if "permission_mode" in kws:
                sites.append(kws)
    return sites


def _selftest() -> int:
    """守这道工具边界。**它此前零覆盖，这就是那个 bug 能活下来的原因**：
    `can_use_tool` 被 `permission_mode="bypassPermissions"` 架空，回调一次都没触发过，
    而整套测试里没有一条断言碰过它 —— 把闸门整个删掉也不会让任何东西变红。

    四条断言各对应一个真实踩过的坑，别因为"看着冗余"删掉任何一条。
    """
    import asyncio
    from pathlib import Path

    bad: list[str] = []
    here = Path(__file__).resolve()
    cloud = here.parents[1] / "analyticsagent" / "app" / "analytics" / "agent.py"

    # ① 两侧的选项构造都必须用 hooks，且**不许**再出现 can_use_tool。
    #    驱动是刻意不同的（本地 _run_agent_once ⟷ 云上 build_options），所以两边各查一次；
    #    只查本地的话，云上那份可以带着死闸门跑上生产而 L0 全绿。
    for label, p in (("backend/agent.py", here), ("analyticsagent/app/analytics/agent.py", cloud)):
        if not p.exists():
            bad.append(f"{label}：文件不存在，工具边界无法核查")
            continue
        sites = _gate_sites(p)
        if not sites:
            bad.append(f"{label}：找不到构造 SDK 选项的地方（重构过？这道检查已经瞎了）")
        for kws in sites:
            if "can_use_tool" in kws:
                bad.append(f"{label}：选项里还挂着 can_use_tool —— 它被 bypassPermissions "
                           f"架空，一次都不会触发，闸门是纯装饰")
            if "hooks" not in kws:
                bad.append(f"{label}：选项里没有 hooks —— PreToolUse 闸门没接上，"
                           f"所有工具都是自动批准")
            if "disallowed_tools" not in kws:
                bad.append(f"{label}：选项里没有 disallowed_tools —— 内置工具会出现在模型的"
                           f"工具表里（实测可达 25 个，含 Bash/Read/Write）")

    # ② ToolSearch 必须在白名单里。少了它 agent 连自己的 MCP 工具都调不出来
    #    （0.2.139 起 MCP 工具延迟加载，要先 ToolSearch 取 schema）。这条会让 L7 全红，
    #    但要等 23 分钟才告诉你；在这里 0 秒就说清。
    if "ToolSearch" not in GATE_ALLOWED:
        bad.append("GATE_ALLOWED 里没有 ToolSearch —— MCP 工具是延迟加载的，"
                   "挡掉它等于把分析工具自己也锁死（表现：read_doc 的 schema 加载不了）")
    missing = [t for t in ALLOWED if t not in GATE_ALLOWED]
    if missing:
        bad.append(f"GATE_ALLOWED 漏了 ALLOWED 里的工具：{missing}")

    # ③ 黑名单得真把危险的那几个盖住。逐个点名而不是只比个数：
    #    「30 个」这种断言在有人换掉其中一项时照样绿。
    for t in ("Bash", "Read", "Write", "Edit", "Task", "WebFetch"):
        if t not in DENIED_BUILTINS:
            bad.append(f"DENIED_BUILTINS 里没有 {t} —— 它会出现在模型的工具表里")
    leaked = [t for t in DENIED_BUILTINS if t in GATE_ALLOWED]
    if leaked:
        bad.append(f"同一个工具既在黑名单又在白名单：{leaked}")

    # ④ hook 本身的行为：白名单放行、其余拒绝，且拒绝要是 SDK 认的那个形状。
    #    形状写错（比如键名拼错）SDK 会当成"没意见"放过去，而日志里什么都不会说。
    gate = _make_gate()

    async def _decide(name):
        return await gate({"tool_name": name}, "tid", None)

    for name in sorted(GATE_ALLOWED):
        if asyncio.run(_decide(name)) != {}:
            bad.append(f"hook 把白名单里的 {name} 拒了")
    for name in ("Bash", "Write", "SomeToolAddedByAFutureSDK"):
        out = asyncio.run(_decide(name)) or {}
        spec = out.get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") != "deny" or spec.get("hookEventName") != "PreToolUse":
            bad.append(f"hook 没有以 SDK 认的形状拒绝 {name}（形状错 = 静默放行）：{out}")

    if bad:
        print("工具边界自测失败 ❌")
        for b in bad:
            print(f"  - {b}")
        return 1
    print(f"工具边界自测：两侧选项都走 hooks 且无 can_use_tool · 白名单 {len(GATE_ALLOWED)} 项"
          f"（含 ToolSearch）· 黑名单 {len(DENIED_BUILTINS)} 项 · hook 放行/拒绝形状正确")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        raise SystemExit(_selftest())
    print("用法：python backend/agent.py --selftest")
    raise SystemExit(2)
