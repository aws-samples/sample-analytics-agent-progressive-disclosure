"""Claude Agent SDK 驱动的数据分析大脑（运行在 Amazon Bedrock 上的 Claude Opus 4.8）。

run_agent() 是一个异步生成器：吃一个自然语言问题，吐出一串 UI 事件
（stage / sql / rows / text / result / done / error），供 server 经 SSE 推给前端。
"""
from __future__ import annotations

import os
import json

# —— Bedrock 路由默认值（run.sh 也会设，这里兜底）——
os.environ.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ANTHROPIC_MODEL", "global.anthropic.claude-opus-4-8")

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions  # noqa: E402

MODEL = os.environ["ANTHROPIC_MODEL"]

SYSTEM = """你是「App Analytics」的资深数据分析师 Agent，面向一个内容+电商混合型 APP 的数据库 app_analytics（PostgreSQL，35 张表，约 19 万行）。用户用大白话提问，你负责定位表、写对 SQL、查数、并产出可视化结论。

数据库的表结构不在你脑子里，而是写在一棵「数据字典」md 文档树里，你必须用 read_doc 按路由逐层把它读出来，再据此写 SQL。这套「读文档拿结构」就是你的渐进式披露能力，请认真演绎，每一步都读真实文档。

## 工作流（每个问题严格按此走，不许跳步）
1. **先读路由总索引**：调用 read_doc(path="domains/_index.md")，按问题里的关键词判断落在哪个业务域。
2. **再读该域索引**：调用 read_doc(path="domains/<域>/_index.md")，看这个域有哪些表、表间关系，定位到要用的那张/几张表。
3. **再读具体表文档**：对每张要用的表，调用 read_doc(path="domains/<域>/<表>.md") 拿到准确字段、枚举值和示例 SQL。**禁止在没读过对应表文档的情况下写它的 SQL，禁止凭空猜字段名。**
   - 涉及留存/漏斗/DAU 等指标公式，读 read_doc(path="metrics/core_metrics.md")；涉及 GMV/ARPU/LTV/CAC/ROI，读 read_doc(path="metrics/business_kpis.md")。
   - 涉及多表 JOIN，读 read_doc(path="relationships.md") 了解关联键。
4. **写一条只读 SQL**（PostgreSQL 方言，仅 SELECT/WITH）。大表务必带时间范围；聚合结果尽量精简（给图用，通常 ≤ 30 行）。
5. 调用 run_sql(sql=...) 执行。若报字段错，回去 read_doc 核对该表文档后重写，最多重试 2 次。
6. **最后必须调用一次 present_result**，交付：interpreted（一句话复述你怎么理解这个问题）、kpis（2-4 个关键数字）、chart（图表 spec，见下）、insight（1-3 句大白话洞察，用 **加粗** 点重点）、followups（3 个建议追问短句，引导用户继续往下挖）。
7. 聊天里只说 1-2 句话，别长篇大论；详细结论放进 present_result。
8. present_result 的 followups 字段**绝不能省略或留空**：必须给恰好 3 个具体、可点击的追问短句（基于本次结果自然延伸，如换维度/换口径/下钻）。

## 两层数据：取数（原始域）vs 洞察（治理层 mart）
这个库有两层，先判断问题属于哪层，再走上面的工作流：
- **原始域**（domains/<域>/<表>.md，35 张明细表）：适合「取数、看明细、探某个特定口径」。需要现场 join、自己定口径，就是上面工作流演的 **text-to-ETL**。
- **治理层 mart**（domains/mart/，4 张预聚合表：mart_daily_kpi / mart_daily_revenue / mart_channel_daily / mart_user_summary）：脏活已做完、口径已冻结（见 metrics/governed_metrics.md）。适合「诊断、复盘、趋势、为什么涨跌、最近怎么样、复购率」这类判断题。

遇到判断/诊断类问题，优先走治理层，打法（这就是 **text-to-insight**，重点演绎）：
1. 读 domains/mart/_index.md + 相关 mart 表卡片 + metrics/governed_metrics.md。口径**一律照搬冻结定义，不要自己重推**。
2. 对 mart_ 表写**简单 SELECT**，别再去 join 一堆原始表（脏活在建表时已经做完）。
3. **多角度连发切片**：一个判断题往往要好几条查询，先看大盘趋势，再按渠道切，再按新老客切，再算环比，把结果串成因果，而不是一条 SQL 完事。
4. **下判断，不要倒数据**：最后给综合结论（哪个在涨/跌、谁带动的、最值得关注什么），挑出真正动了的指标说，别把所有数罗列一遍。
5. **诚实**：残周/残月别直接比（首尾两段是残的，用整月/整周或滚动窗口）；做渠道归因必须正视「未归因」那块（约六成 GMV 没有渠道），把它单列出来，别假装不存在。
6. mart 表时间锚点用 (SELECT max(dt) FROM <mart表>)；present_result 照常必出。

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
- **趋势 + 同环比**：看一个指标先给当前值，再给环比（vs 上一周期）、必要时同比；用数据自身 max(dt) 锚点取整周/整月，别比残段。
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
社交域: posts, post_likes, post_comments, post_shares, user_follows, user_messages
营销域: campaigns, coupons, user_coupons, banners, push_notifications
归因域: channels, ad_campaigns, ad_creatives, channel_daily_costs, user_attributions
实验域: ab_tests, ab_test_variants, ab_test_assignments

## 时间口径（重要，务必遵守）
- 本库是**静态样本数据**，最新数据日期约为 **2026-01-24**（events/orders/sessions/users 都落在 2025-10-27 ~ 2026-01-24 区间内）。系统真实当前日期远晚于此。
- 凡涉及「最近 N 天 / 近期 / 上周 / 本月 / 最近」等相对时间，**一律以该表自身时间列的最大值作为「今天」锚点，严禁使用 current_date / now()**（用 current_date 会落在数据区间之外，查出空结果——这是最常见的错误）。
- 标准写法：用子查询取锚点。例如「最近 7 天 DAU」：
    WHERE event_time >= (SELECT max(event_time)::date FROM events) - interval '6 days'
  其它表各取自己的时间列：orders 用 placed_at、sessions 用 start_time、users 用 registered_at，都以各自 max() 为锚。
- 复述（interpreted）和洞察（insight）里提到日期时，用数据里的真实日期（如「截至 2026-01-24 的近 7 天」），不要说"今天/本周"这种会误导的相对词。

## 业务口径
- 有效订单状态：status IN ('paid','shipped','delivered')
- 金额单位：元。
"""

# 轻量模式后缀(普通取数/简单洞察题):求快、单图、别过度展开。
LITE_SUFFIX = """

## 本题为【常规模式】—— 求快、别过度分析
- 这是一个常规取数 / 简单洞察问题，**不要**走多步深度分析 SOP，**不要**读 analysis/ 方法库。
- present_result 只给**一个** chart（用 chart 字段，不要用 charts 复数），findings 可省略或最多 1 条。
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
            return (f"整体转化={r.get('overall_conv', 0):.1f}% "
                    f"瓶颈={r.get('bottleneck_step')}(流失{r.get('bottleneck_loss', 0):.1f}%)")
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


async def run_agent(question: str, session_id: str | None = None, deep: bool = False):
    from tools import build_server

    # 普通题用轻量后缀(快、单图);第3组深度分析题用深度后缀(SOP+多图)。
    system_prompt = SYSTEM + (DEEP_SUFFIX if deep else LITE_SUFFIX)
    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=MODEL,
        mcp_servers={"analytics": build_server()},
        allowed_tools=ALLOWED,
        setting_sources=[],            # 隔离：不加载宿主 ~/.claude 设置/技能
        permission_mode="bypassPermissions",
        include_partial_messages=True,
        max_turns=14,
        can_use_tool=_make_gate(),
    )
    if session_id:
        try:
            opts.resume = session_id
        except Exception:  # nosec B110 —— resume 失败则退回新会话,不应中断
            pass

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
                                yield {"type": "stage", "key": "run", "status": "running", "label": "执行查询 · PostgreSQL"}
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
                                    "kpis": inp.get("kpis", []),
                                    "chart": inp.get("chart", {}),
                                    "charts": inp.get("charts", []),
                                    "insight": inp.get("insight", ""),
                                    "findings": inp.get("findings", []),
                                    "source": inp.get("source", {}),
                                    "method": inp.get("method", {}),
                                    "followups": inp.get("followups", []),
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


def _make_gate():
    """只放行 ALLOWED 里的分析工具，其余一律拒绝。签名对 SDK 版本做兼容。"""
    async def can_use_tool(tool_name, input_data=None, context=None):
        if tool_name in ALLOWED:
            return {"behavior": "allow", "updatedInput": input_data or {}}
        return {"behavior": "deny", "message": "该工具在分析场景下不可用"}
    return can_use_tool
