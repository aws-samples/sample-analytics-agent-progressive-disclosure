# ⚠️ 本文件是**生成物**，不要手改。
# 由 scripts/deploy/sync_agent_code.py 从 backend/tools.py 逐字拷来（除本横幅）。
# 要改请改源文件，再跑 `python3 scripts/deploy/sync_agent_code.py --apply`。
# L0 会跑 `--check`：两侧不一致就红。
"""Agent 的进程内 MCP 工具：read_doc / run_sql / present_result。

- read_doc：progressive disclosure 的核心——按需读取「数据字典」md 文档树
  （domains/_index.md → 域 index → 表 doc），agent 顺着路由一层层加载，
  读到哪张表就拿哪张表的结构，再写 SQL。这就是 skill 路由能力的工具化身。
- run_sql：只读执行，安全校验在 db.validate 里。
- present_result：agent 把最终的 KPI / 图表 spec / 洞察吐出来，前端据此渲染。
  本工具本身只回执；真正的载荷由 server 端从工具调用入参里读取并推给前端。
"""
from __future__ import annotations

import os
import json

from claude_agent_sdk import tool, create_sdk_mcp_server

import db

HERE = os.path.dirname(os.path.abspath(__file__))
# 数据字典文档根。两种布局，一个变量搞定：
#   · 本仓库 / backend 镜像：顶层 knowledge/，与 backend/ 同级（Dockerfile COPY 进 /app/knowledge）。
#   · AgentCore Runtime：知识树**不烤进镜像**，冷启动时 knowledge_store.sync_down() 从 S3
#     拉到 KNOWLEDGE_DIR（默认 /app/knowledge），read_doc 读这份本地缓存。
# 之前这两种写法是**两份 tools.py 各写一行**，于是这个文件成了"只差一行但必须手工同步"的
# 副本，其余 170 行的改动全靠人记得拷过去。改成读 env 之后两侧逐字相同，
# scripts/deploy/sync_agent_code.py 才管得住它。
DOCS_ROOT = os.path.normpath(
    os.environ.get("KNOWLEDGE_DIR") or os.path.join(HERE, "..", "knowledge"))


@tool(
    "read_doc",
    "读取数据字典文档（数据库结构的渐进式披露）。**写任何 SQL 前必须先读文档拿到表结构，禁止凭空猜字段。**"
    "按路由逐层加载：先读 'domains/_index.md' 选域 → 读 'domains/<域>/_index.md' 选表 → 读 'domains/<域>/<表>.md' 拿字段。"
    "指标公式读 'metrics/core_metrics.md' 或 'metrics/business_kpis.md'，跨表 JOIN 读 'relationships.md'。"
    "path 是相对 docs 根的路径，形如 'domains/marketing/coupons.md'。",
    {"path": str},
)
async def read_doc(args):
    rel = (args.get("path") or "").strip().lstrip("/")
    if not rel.endswith(".md"):
        return {"content": [{"type": "text", "text": f"只能读 .md 文档：{rel}"}], "is_error": True}
    full = os.path.normpath(os.path.join(DOCS_ROOT, rel))
    # 路径逃逸防护：必须落在 DOCS_ROOT 之内
    if full != DOCS_ROOT and not full.startswith(DOCS_ROOT + os.sep):
        return {"content": [{"type": "text", "text": f"非法文档路径：{rel}"}], "is_error": True}
    if not os.path.isfile(full):
        # 文档不存在时，回一个同目录可读清单，帮 agent 自我纠偏
        parent = os.path.dirname(full)
        hint = ""
        if os.path.isdir(parent):
            sibs = sorted(f for f in os.listdir(parent) if f.endswith(".md"))
            rels = os.path.relpath(parent, DOCS_ROOT)
            prefix = "" if rels == "." else rels + "/"
            hint = "\n该目录下可读文档：" + ", ".join(prefix + s for s in sibs)
        return {"content": [{"type": "text", "text": f"文档不存在：{rel}{hint}"}], "is_error": True}
    with open(full, encoding="utf-8") as f:
        text = f.read()
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "run_sql",
    "对 app_analytics 库执行一条只读 SQL（仅 SELECT/WITH）。"
    "返回 JSON：columns/rows/rowcount/truncated/exec_ms/bytes_scanned"
    "（bytes_scanned = 这条查询扫描的字节数，Athena 按它计费，是成本读数）。"
    "执行前必须已通过 read_doc 读过相关表的文档。"
    "注意：查询走的是最小权限只读角色，有一小块数据不在授权面里——"
    "`users.email` / `users.phone` / `user_profiles.birth_date` 被 Lake Formation "
    "**列级排除**（点名查报 `COLUMN_NOT_FOUND`，`SELECT *` 里也没有），"
    "`user_messages` 整表未授权（报表不存在）。这类拒绝**不要重试、不要换写法绕**，"
    "改写 SQL 也拿不到；直接告诉用户这条边界并给可用的替代维度。"
    "这是列级排除而非脱敏：库里是明文，别在输出里说「已脱敏」。",
    {"sql": str},
    annotations={"readOnlyHint": True},
)
async def run_sql(args):
    sql = args.get("sql", "")
    try:
        result = await db.run_query(sql)
    except db.SqlError as e:
        return {"content": [{"type": "text", "text": f"SQL 被拒绝：{e}"}], "is_error": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"查询出错：{type(e).__name__}: {e}"}], "is_error": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


@tool(
    "call_metric",
    "调用治理层已冻结口径的【官方指标】，返回唯一权威数值（与看板/董事会材料一致）。"
    "**问到 GMV/新客/DAU/CAC/ROI/复购率/退款/订阅 这类已治理指标时，优先用本工具而非手写 SQL** —— "
    "口径由 owner 冻结、模型无法绕过或猜错。参数：metric(指标名，见 metrics/governed_metrics.md)、"
    "group_by(可选切片维度如 [channel])、filters(可选，如 {channel:'Google Ads'})、"
    "time_window(可选: last_7d/last_30d/last_quarter/last_month/mtd/all)。"
    "返回 columns/rows + owner + 口径声明 + version（请把这些写进 present_result 的来源标注）。"
    "若指标未覆盖会返回 not_covered，此时再退回 read_doc + run_sql 手写。",
    {"metric": str, "group_by": list, "filters": dict, "time_window": str},
)
async def call_metric(args):
    import metric_layer
    metric = (args.get("metric") or "").strip()
    group_by = args.get("group_by") or []
    filters = args.get("filters") or {}
    time_window = (args.get("time_window") or "").strip() or None
    try:
        spec = metric_layer.compile_metric(metric, group_by=group_by,
                                           filters=filters, time_window=time_window)
    except metric_layer.MetricNotCovered as e:
        return {"content": [{"type": "text",
                "text": json.dumps({"not_covered": str(e)}, ensure_ascii=False)}]}
    try:
        result = await db.run_query(spec["sql"])
    except db.SqlError as e:
        return {"content": [{"type": "text", "text": f"指标 SQL 被拒绝：{e}"}], "is_error": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"指标查询出错：{type(e).__name__}: {e}"}], "is_error": True}
    payload = {
        "metric": spec["metric"], "label": spec["label"], "owner": spec["owner"],
        "unit": spec["unit"], "version": spec["version"],
        "口径声明": spec["口径声明"], "compiled_sql": spec["sql"],
        "result": result,
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


@tool(
    "compute_stats",
    "【统计计算器】用确定性代码（非模型心算）精确计算统计量。深度分析题算 μ/σ/Z-score/"
    "比率分解/帕累托/漏斗/留存时**必须调用本工具**，把结果填进 present_result.method.stats。"
    "method 取值与参数："
    "  'zscore'  —— 异常检测。values=数值序列, labels=可选标签, params={threshold:2.0}。"
    "             返回 mean/std/cv/upper/lower/n_anomalies/anomalies/points(每点带 z 和 is_anomaly)。"
    "  'describe'—— 描述统计。values=序列 → mean/std/cv/min/max/median/sum。"
    "  'ratio_decompose' —— 两因子中点分解(GMV=U×P)。params={u0,u1,p0,p1}。"
    "             返回 d_total/contrib_u/contrib_p/各贡献占比/residual(应≈0)。"
    "  'pareto'  —— 贡献度+累计占比。values=各子项值, labels=子项名 → cr3/hhi/n_for_80pct/items(带cum_share)。"
    "  'funnel'  —— 漏斗转化。values=各步人数(递减), labels=步骤名 → overall_conv/bottleneck/steps(带conv_from_prev)。"
    "  'retention' —— 留存。values=各期活跃数, labels=期标签, params={n0:初始人数} → points(带retention%)/cliff_at。"
    "values 传数字数组；params 传方法特定参数（如 ratio_decompose 的 u0/u1/p0/p1）。"
    "先用 run_sql/call_metric 从库里取到原始数（如日 GMV 序列、两期的付费用户数/客单价），再喂给本工具算。",
    {"method": str, "values": list, "labels": list, "params": dict},
)
async def compute_stats(args):
    import stats
    method = (args.get("method") or "").strip()
    try:
        result = stats.compute(method,
                               values=args.get("values") or [],
                               labels=args.get("labels") or [],
                               params=args.get("params") or {})
    except stats.StatsError as e:
        return {"content": [{"type": "text",
                "text": json.dumps({"error": str(e)}, ensure_ascii=False)}], "is_error": True}
    payload = {"method": method, "result": result}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


@tool(
    "present_result",
    "把分析结论交付给前端展示。完成查询后必须调用一次。chart.type 取 line/bar/hbar/funnel/pie/heatmap。"
    "深度分析题建议用 charts(复数)给 2-3 个图(如 趋势+拆解+占比),前端会并排渲染,比单图更有洞察。",
    {"type": "object", "properties": {
            "interpreted": {"type": "string"},   # 一句话复述你如何理解了用户的问题
            "kpis": {"type": "array"},         # 2-4 个 {label, ref?:{query_index,row,col}(数值 KPI 必填,后端按它取原值), format?(amount|count|percent|fraction|ratio), decimals?, unit?, value?(只给文字类,如渠道名), delta?, dir?('up'|'down'), note?, baseline?, baseline_value?, is_answer?}
            "chart": {"type": "object"},        # 单图: {type, title, data_ref:{query_index, x, y:[...]}}（数据由后端按 data_ref 从查询结果取，不要手写数）
            "charts": {"type": "array"},       # 可选,多图: [chart, chart, ...] 每个同 chart 约定。给了 charts 就渲染多图(深度分析推荐)
            "table": {"type": "object"},       # 可选,明细表: {query_index, title?, columns?:[列名 或 {col,label,format,decimals}], limit?}，后端取原值、前端渲染表格
            "insight": {"type": "string"},       # 1-3 句大白话洞察，可用 **加粗** 标重点；数字写成占位符 {{kpi1}} / {{q2.r0.列名|format}}
            "findings": {"type": "array"},     # 可选,深度分析要点: [{kind:'driver'|'anomaly'|'risk', text, evidence?}] 驱动项/异常项/风险项
            "source": {"type": "object"},       # 可选,治理来源标注: {metric, owner, version, 口径声明} —— 来自 call_metric 时务必带上
            "method": {"type": "object"},       # 可选,深度分析【统计方法说明】,前端在结果最上方展示"我们用了什么方法、哪几步、什么公式":
                                  #   {name:'异常检测 · Z-score/3σ', steps:['取序列','算μ和σ','算z分数','|z|>2标异常'],
                                  #    formula:[{label:'Z 分数', expr:'z = (x − μ) / σ'}, {label:'判定', expr:'|z| > 2 → 异常'}],
                                  #    stats:[{label:'μ 均值', value:285.6, unit:'万'}, {label:'σ 标准差', value:42.1, unit:'万'}]}
            "followups": {"type": "array"},    # 3 个建议追问（短句字符串）
            "answer": {"type": "array"},       # 本题答案来自哪几次查询：[{query_index, part?}]。只查过一次可省略（自动取那一次）。不在界面显示，只给评测用
    },
     # 必填只放每题都必须有的字段。原来用简写 {name: type}，SDK 会把所有字段（连标了「可选」的 charts / findings /
     # source / method）都当必填，普通题只给 chart 不给 charts 就被打回、白跑一轮（2026-11-18 实测：同一题 5 次 5 次被拦）。
     # chart / charts 二选一（普通题单图、深度题多图），findings / source / method / answer 按需给。
     "required": ["interpreted", "kpis", "insight", "followups"]},
)
async def present_result(args):
    # 载荷由 server 从 ToolUseBlock.input 读取，这里仅回执
    return {"content": [{"type": "text", "text": "已呈现给前端。"}]}


class QueryLog:
    """一次 agent 运行内的查询记录（每次 build_server 一份，互不串）。

    run_sql / call_metric 每成功执行一次记一条，并把序号 query_index 写回给模型看；
    present_result 按模型在 answer 里给的序号，从这里原样取出结果 —— 答案数据来自引擎的真实返回，
    不经过模型转写。查过多次却没指明答案来源、或序号不存在时，present_result 返回错误让模型补上。"""

    def __init__(self, deep: bool = False):
        self.deep = deep                         # 本题是否深度分析模式（深度题必须给统计方法面板 method）
        self.queries: list[dict] = []
        self.answer: list[dict] | None = None
        self.rejects = 0                         # is_answer 校验打回次数
        self.unbacked: list = []                 # 最终放行时仍对不上的 is_answer 数
        self.marked_labels: set = set()          # 曾经标过 is_answer 的 KPI label
        self.dropped_mark = False                # 被打回后是否试图去掉标记
        self.prev_refs: list = []                # 上一次 present_result 引用的查询（打回后保留）
        self.format_bad: set = set()            # 被判为「输出取整 / 截位」打回过的查询序号（不再自动保留）
        self.formatted: list = []                # 放行时 answer 里仍带取整的查询序号
        self.kpis: list | None = None            # 放行时按出处取值、格式化后的 KPI（前端展示用这一份）
        self.stats: list[dict] = []              # compute_stats 的标量结果（KPI 可用 ref.stats_index 引用）
        self.page: dict | None = None            # 放行时按出处取值的图表 / 表格 / 替换过占位符的文字（前端展示用这一份）
        self.page_unbacked: list = []            # 放行时页面上仍没出处的数（打回 2 次后仍未改好的，评测统计用）
        self.stats_nums: list = []               # compute_stats 结果里所有的数（含嵌套），文字核对用
        self.reject_log: list = []               # 每次打回的原因（评测统计用，看模型最常卡在哪类写法上）

    def reset(self, deep: bool = False) -> None:
        """每个新问题开始时清空（云端暖客户端在同一会话里连续答多个问题，工具服务器只建一次）。"""
        self.__init__(deep)

    def add(self, kind: str, text: str, columns, rows, truncated=False, types=None) -> int:
        self.queries.append(dict(index=len(self.queries) + 1, kind=kind, text=text,
                                 columns=list(columns or []), rows=list(rows or []), truncated=bool(truncated),
                                 types=list(types or [])))      # 引擎返回的列类型，评测按它判数值表示精度
        return len(self.queries)

    def resolve(self, refs) -> tuple[list[dict] | None, str | None]:
        n = len(self.queries)
        if not refs:
            if n == 1:
                refs = [{"query_index": 1}]
            elif n == 0:
                return [], None                  # 没查过数（如治理拒绝），没有答案表
            else:
                listing = "；".join(f"#{q['index']} {q['kind']}: {q['text'][:80]}" for q in self.queries)
                return None, (f"本次共执行了 {n} 次查询（{listing}）。请在 present_result 的 answer 里指明"
                              f"回答本题用到的是哪几次，如 answer=[{{\"query_index\": {n}}}]；"
                              f"多部分答案可给多项并加 part 标签。其余字段保持不变，重新调用一次 present_result。")
        out = []
        for r in refs:
            try:
                k = int(r.get("query_index") if isinstance(r, dict) else r)
            except (TypeError, ValueError):
                return None, f"answer 里的 query_index 必须是整数（1～{n}）：{r}"
            if not 1 <= k <= n:
                return None, f"answer 里的 query_index={k} 不存在：本次只执行了 {n} 次成功的查询（1～{n}）。"
            q = self.queries[k - 1]
            out.append(dict(part=(r.get("part") if isinstance(r, dict) else None) or "", **q))
        return out, None


_UNIT = {"万": 1e4, "万元": 1e4, "万人": 1e4, "万笔": 1e4, "万单": 1e4, "亿": 1e8, "亿元": 1e8, "千": 1e3}


def _formatted_output_cols(sql: str) -> list:
    """answer 引用的 SQL 最外层输出列里的「格式化」：round / floor / ceil / truncate，CAST 成整数（里面有 sum / avg / 除法时），
    或 CAST 成小数位 < 6 的 decimal（里面有除法或 avg 时——金额合计 CAST 成 decimal(14,2) 不丢精度，不算）。
    - 只看输出值本身那一层算术（外面只有 + - * / 括号 / CAST 包着的）；聚合、CASE、窗口里面的中间计算不管；
    - 输出列是分组键（出现在 GROUP BY 里，如 ROUND(x/100)*100 分档）不算；
    - 输出列直接取自 CTE / 子查询的列时，顺着列名追到定义它的那一层再看。返回 [列名, ...]。"""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="trino")
    except Exception:  # noqa: BLE001 —— 解析不了就不拦
        return []
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if sel is None:
        return []
    passthru = (exp.Paren, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.Cast, exp.TryCast, exp.Alias)
    rounding = tuple(t for t in (getattr(exp, n, None) for n in ("Round", "Floor", "Ceil", "Truncate")) if t)
    # 所有内层 SELECT 的输出列定义：alias → 表达式（用于顺着列名追溯）
    defs = {}
    for inner in tree.find_all(exp.Select):
        if inner is sel:
            continue
        for e in inner.expressions:
            if e.alias_or_name:
                defs.setdefault(e.alias_or_name.lower(), e)

    def _has_frac(e, depth=0):
        """表达式里有除法或 avg（会产生小数）；引用了 CTE / 子查询的列时，顺着列名追到定义处再看。"""
        if e.find(exp.Div) or e.find(exp.Avg):
            return True
        if depth < 5:
            for col in e.find_all(exp.Column):
                d = defs.get(col.name.lower())
                if d is not None and d is not e and _has_frac(d, depth + 1):
                    return True
        return False

    def lossy(expr, depth=0):
        stack = [expr]
        while stack:
            node = stack.pop()
            if isinstance(node, rounding):
                return True
            if isinstance(node, (exp.Cast, exp.TryCast)):
                to = node.args.get("to")
                t = (to.this.name if to is not None and hasattr(to.this, "name") else str(to)).lower()
                inner_e = node.this
                frac = _has_frac(inner_e)
                if t in ("int", "integer", "bigint", "smallint", "tinyint") and (frac or inner_e.find(exp.Sum)):
                    return True
                if t == "decimal":
                    ps = list(to.expressions) if to is not None else []
                    sc = int(ps[1].name) if len(ps) >= 2 and ps[1].name.isdigit() else None
                    if sc is not None and sc < 6 and frac:
                        return True
            if isinstance(node, exp.Column) and depth < 5 and node.name.lower() in defs:
                d = defs[node.name.lower()]
                if d is not expr and lossy(d, depth + 1):
                    return True
                continue
            if isinstance(node, exp.Case):                 # CASE：只看各分支的取值，不看条件
                stack.extend(i.args.get("true") for i in node.args.get("ifs") or [] if i.args.get("true") is not None)
                if node.args.get("default") is not None:
                    stack.append(node.args["default"])
                continue
            if isinstance(node, (exp.Coalesce, exp.Nullif)):
                stack.extend(node.iter_expressions())
                continue
            if isinstance(node, passthru) or node is expr:
                stack.extend(node.iter_expressions())
        return False

    group = sel.args.get("group")
    gexprs = list(group.expressions) if group is not None else []
    bad = []
    for pos, item in enumerate(sel.expressions, 1):
        body = item.this if isinstance(item, exp.Alias) else item
        is_key = any(g == body or (isinstance(g, exp.Literal) and g.name == str(pos))
                     or (isinstance(g, exp.Column) and g.name == item.alias_or_name) for g in gexprs)
        if not is_key and lossy(item):
            bad.append(item.alias_or_name)
    return bad


# ———————————————— KPI 按出处取值 + 确定性格式化 ————————————————
# 卡片上的数不让模型手写：模型给 ref={query_index, row, col}，这里从查询记录里取引擎原值，
# 再按 format 生成展示文本（display）。前端只显示 display，不再自己取整。
KPI_FORMATS = {"amount": 2, "count": 0, "percent": 2, "fraction": 2, "ratio": 2, "number": 2}   # 默认小数位


def _round_half_up(v: float, n: int) -> str:
    from decimal import Decimal, ROUND_HALF_UP
    q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)
    return f"{q:,.{n}f}"


def format_kpi_value(v, fmt: str | None = None, unit: str | None = None, decimals: int | None = None) -> dict:
    """引擎原值 → 展示文本。返回 {display, unit, note_extra}。四舍五入一律 half-up（与引擎 round 一致）。
    - amount 金额：完整金额、千分位、2 位小数，单位默认「元」；≥ 1 万时备注「约 29.4 万」（≥ 1 亿「约 1.51 亿」）
    - count 计数：千分位、0 位（原值不是整数时退到 2 位）
    - percent 原值已是百分数（6.22 → 6.22%）；fraction 原值是比例（0.0622 → 6.22%）
    - ratio / number：千分位、2 位，单位照模型给的（元/人、倍……）
    decimals 显式给出时（如题目要求保留 1 位）覆盖默认值。"""
    if v is None:
        return {"display": "—", "unit": unit or "", "note_extra": ""}
    fmt = fmt if fmt in KPI_FORMATS else "number"
    x = float(v)
    n = decimals if isinstance(decimals, int) and 0 <= decimals <= 6 else KPI_FORMATS[fmt]
    if fmt == "count" and decimals is None and not x.is_integer():
        n = 2
    if fmt == "number" and decimals is None and x.is_integer():
        n = 0
    extra = ""
    if fmt == "fraction":
        x, unit = x * 100, "%"
    elif fmt == "percent":
        unit = "%"
    elif fmt == "amount":
        unit = unit or "元"
        if abs(x) >= 1e8:
            extra = f"约 {_round_half_up(x / 1e8, 2)} 亿"
        elif abs(x) >= 1e4:
            extra = f"约 {_round_half_up(x / 1e4, 1)} 万"
    return {"display": _round_half_up(x, n), "unit": unit or "", "note_extra": extra}


def _is_numeric_text(v) -> bool:
    """KPI 的 value 是不是一个数（含「1.50亿」「31.55%」「293,963.85」这种写法）。"""
    import re
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return bool(re.fullmatch(r"\s*[-+]?[\d,]*\.?\d+\s*(万|亿|千|%|元|万元|亿元|人|笔|单|次|个)?\s*", str(v or "")))


def resolve_kpis(kpis, queries, stats=None, pool=None) -> tuple[list, list]:
    """按 ref 从查询记录取原值并格式化。返回 (resolved, errors)。
    - 有 ref：query_index 1 起、row 0 起、col 为列名或 0 起的列号；取到的格子写进 value，并生成 display
    - 没 ref 且 value 是数：记错（数值 KPI 必须给出处）
    - 没 ref 且 value 是文字（如渠道名）：原样保留"""
    out, errs = [], []
    for i, k in enumerate(kpis or []):
        if not isinstance(k, dict):
            continue
        k = dict(k)
        k["_pos"] = i + 1                                  # 原始序号：文字里的 {{kpiN}} 按它找，不受前面 KPI 被丢掉影响
        ref = k.get("ref")
        label = k.get("label") or f"第 {i + 1} 个"
        if isinstance(ref, dict) and ref.get("stats_index") is not None and stats is not None:
            try:
                si = int(ref.get("stats_index"))
            except (TypeError, ValueError):
                si = 0
            key = ref.get("key") or ref.get("col")
            if not 1 <= si <= len(stats) or key not in stats[si - 1]:
                errs.append(f"「{label}」的 ref 指向的统计量不存在（stats_index 1～{len(stats)}；key 如 mean / std，"
                            "嵌套的用点号路径，如 max_abs_z.z、anomalies.0.value）")
                continue
            cell = stats[si - 1][key]
            k["value"] = cell
            f = format_kpi_value(cell, k.get("format"), k.get("unit"), k.get("decimals"))
            k["display"], k["unit"] = f["display"], f["unit"]
            if f["note_extra"]:
                k["note"] = f"{k['note']} · {f['note_extra']}" if k.get("note") else f["note_extra"]
            out.append(k)
        elif isinstance(ref, dict):
            try:
                qi, row = int(ref.get("query_index")), int(ref.get("row", 0))
            except (TypeError, ValueError):
                errs.append(f"「{label}」的 ref 不完整（要 query_index / row / col）")
                continue
            if not 1 <= qi <= len(queries):
                errs.append(f"「{label}」的 ref.query_index={qi} 不存在（本次查询 1～{len(queries)}）")
                continue
            q = queries[qi - 1]
            cols, rows = q.get("columns") or [], q.get("rows") or []
            col = ref.get("col")
            ci = cols.index(col) if col in cols else (int(col) if isinstance(col, int) or str(col).isdigit() else None)
            if ci is None or not 0 <= ci < len(cols) or not 0 <= row < len(rows):
                errs.append(f"「{label}」的 ref 指向的格子不存在（#{qi} 有 {len(rows)} 行，列 {cols}）")
                continue
            r = rows[row]
            cell = (list(r.values()) if isinstance(r, dict) else list(r))[ci]
            k["value"] = cell
            mis = pct_misuse(q, cols[ci], cell, k.get("format")) or decimal_truncated(q, cols[ci]) \
                or binding_error(q, row, f"{k.get('label') or ''} {k.get('note') or ''}")
            if mis:
                errs.append(f"「{label}」：{mis}")
                continue
            if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                f = format_kpi_value(cell, k.get("format"), k.get("unit"), k.get("decimals"))
                k["display"], k["unit"] = f["display"], f["unit"]
                if f["note_extra"]:
                    k["note"] = f"{k['note']} · {f['note_extra']}" if k.get("note") else f["note_extra"]
            elif cell is None:                  # 引擎返回 NULL（如零新客渠道的 CAC）：显示「—」，不带单位
                k["display"], k["unit"] = "—", ""
            else:
                k["display"] = str(cell)
            out.append(k)
        elif _is_numeric_text(k.get("value")):
            raw = str(k.get("value"))
            if pool is not None and not unbacked_numbers(raw, pool, set()):
                x = _as_num(_re.sub(r"[^\d.\-]", "", raw))   # 结果里找得到（如行数、结果里已有的数）：按 format 显示
                f = format_kpi_value(x, k.get("format") or ("count" if x is not None and float(x).is_integer() else "number"),
                                     k.get("unit"), k.get("decimals"))
                k["value"], k["display"], k["unit"] = x, f["display"], k.get("unit") or f["unit"]
                out.append(k)
            else:
                errs.append(f"「{label}」的数值 {k.get('value')!r} 在查询结果里找不到：用 ref 指向结果里的格子，或先写 SQL 算出来")
        else:
            k["display"] = "" if k.get("value") is None else str(k.get("value"))
            out.append(k)
    return out, errs


# ———————————————— 图表 / 文字 / 表格按出处取值（2026-11-23） ————————————————
# 与 KPI 同一原则：页面上的数不让模型手写。
#   图表   chart.data_ref 指明用哪次查询、哪列做 x、哪几列做 y，后端从查询记录取引擎原值填进图表；
#   文字   insight / findings / KPI 的 delta·note 里的数写成占位符（{{kpi1}} / {{q2.r0.gmv|amount}} / {{s1.mean}}），
#          后端替换成格式化好的文本；没用占位符的裸数字必须能在查询结果 / KPI / 统计量里找到，否则打回；
#   表格   table 指明用哪次查询、哪几列，后端取原值格式化，前端渲染成表格（insight 里不许手写 markdown 表格）。
import re as _re

_PH = _re.compile(r"\{\{\s*(?:kpi\s*(\d+)|q\s*(\d+)\s*\.\s*r\s*(\d+)\s*\.\s*([^|{}\s]+)|s\s*(\d+)\s*\.\s*([^|{}\s]+))"
                  r"\s*(?:\|\s*([a-z]+)\s*(?::\s*(\d))?)?\s*\}\}")
_SCALE = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3}
# 不算「数值结论」的数字：日期、时间、时长窗口、名次、序号、季度 / 周次标记
_EXEMPT = [r"\d{4}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2}\s*日?)?", r"\d{1,2}\s*月\s*\d{1,2}\s*日?", r"\d{1,2}/\d{1,2}",
           r"\d{1,2}:\d{2}", r"(?<![\d.])(?:19|20)\d{2}\s*年?(?![\d.%])", r"[QqDdWwHhMm]\s*\d+(?![\d.])",
           r"(?:Top|TOP|top)\s*\d+", r"(?:前|后|第|近|最近|过去|头部)\s*\d+",
           r"\d+\s*(?:天|日|周|个月|月份|月|季度|年|小时|分钟|秒|岁|号)",
           # 表示范围的说法（超 200 万、不到 5%、10 万以上）只给界限，不是数值结论
           r"(?:超过|超出|超|逾|不足|不到|多于|少于|至少|最多)\s*[\d,.]+\s*(?:万亿|亿|万|千|%|倍)?",
           r"[\d,.]+\s*(?:万亿|亿|万|千|%|倍)?\s*(?:以上|以下|开外|出头)"]
_NUM = _re.compile(r"(?<![\w.])([-+−]?\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|万|千|%|‰|倍)?")


def decimal_truncated(q, col) -> str | None:
    """引擎返回的列类型是小数位 ≤ 4 的 decimal、而 SQL 里这一列是除法 / 平均：Athena 的 decimal 相除会截掉小数
    （2026-11-23 实测：环比 0.1727 被算成 0.17，页面显示 17.00%，和别处的 17.27% 对不上）。"""
    if (q or {}).get("kind") != "sql":
        return None
    cols, types = q.get("columns") or [], q.get("types") or []
    if col not in cols or cols.index(col) >= len(types):
        return None
    m = _re.match(r"decimal\((\d+),\s*(\d+)\)", str(types[cols.index(col)]).lower())
    if not m or int(m.group(2)) > 4:
        return None
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(q.get("text") or "", read="trino")
    except Exception:  # noqa: BLE001
        return None
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    hit = [e for e in (sel.expressions if sel else []) if e.alias_or_name == col]
    defs = {e.alias_or_name: e for inner in tree.find_all(exp.Select) if inner is not sel for e in inner.expressions}

    def frac(e, depth=0):
        if e.find(exp.Div) or e.find(exp.Avg):
            return True
        return depth < 4 and any(c.name in defs and defs[c.name] is not e and frac(defs[c.name], depth + 1)
                                 for c in e.find_all(exp.Column))
    if hit and frac(hit[0]):
        return (f"#{q.get('index')} 的 {col} 是 decimal 相除，引擎只保留了 {m.group(2)} 位小数（被截位）："
                "把分子 CAST(... AS DECIMAL(38,6)) 或 CAST(... AS DOUBLE) 后重跑，再引用新查询")
    return None


def pct_misuse(q, col, v, fmt) -> str | None:
    """format=percent 表示「原值已经是百分数」（6.22 → 6.22%）。原值在 (-1, 1) 之间、而 SQL 里这一列没有乘 100，
    说明它其实是比例（0.1727），应该用 fraction——否则 17.27% 会显示成 0.17%（2026-11-23 实测：环比 17.27% 被写成 +0.2%）。"""
    x = _as_num(v)
    if fmt != "percent" or x is None or x == 0 or abs(x) >= 1 or (q or {}).get("kind") != "sql":
        return None
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(q.get("text") or "", read="trino")
    except Exception:  # noqa: BLE001
        return None
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if sel is None:
        return None
    hit = [e for e in sel.expressions if e.alias_or_name == col]
    if not hit:
        return None
    defs = {e.alias_or_name: e for inner in tree.find_all(exp.Select) if inner is not sel for e in inner.expressions}

    def times100(e, depth=0):
        if any(isinstance(l, exp.Literal) and str(l.name) in ("100", "100.0") for m in e.find_all(exp.Mul) for l in m.iter_expressions()):
            return True
        return depth < 4 and any(c.name in defs and defs[c.name] is not e and times100(defs[c.name], depth + 1)
                                 for c in e.find_all(exp.Column))
    if times100(hit[0]):
        return None
    return f"#{q.get('index')} 的 {col} 是比例（原值 {x:.4g}，SQL 里没有乘 100），format 应该用 fraction（显示成 {x * 100:.2f}%），不是 percent"


def _as_num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _cell(queries, qi, row, col):
    """查询记录里的一个格子：(值, 错误)。qi 从 1 起，row 从 0 起，col 是列名或列号。"""
    if not 1 <= qi <= len(queries):
        return None, f"query_index={qi} 不存在（本次查询 1～{len(queries)}）"
    q = queries[qi - 1]
    cols, rows = q.get("columns") or [], q.get("rows") or []
    ci = cols.index(col) if col in cols else (int(col) if str(col).isdigit() else None)
    if ci is None or not 0 <= ci < len(cols):
        return None, f"#{qi} 没有列 {col!r}（列：{cols}）"
    if not 0 <= row < len(rows):
        return None, f"#{qi} 只有 {len(rows)} 行，row={row} 越界"
    r = rows[row]
    return (list(r.values()) if isinstance(r, dict) else list(r))[ci], None


def _col(queries, qi, col):
    """一整列的值：(值列表, 错误)。"""
    if not 1 <= qi <= len(queries):
        return None, f"query_index={qi} 不存在（本次查询 1～{len(queries)}）"
    q = queries[qi - 1]
    cols = q.get("columns") or []
    ci = cols.index(col) if col in cols else (int(col) if str(col).isdigit() else None)
    if ci is None or not 0 <= ci < len(cols):
        return None, f"#{qi} 没有列 {col!r}（列：{cols}）"
    return [(list(r.values()) if isinstance(r, dict) else list(r))[ci] for r in q.get("rows") or []], None


def format_text_value(v, fmt=None, decimals=None, unit=None) -> str:
    """把一个原值格式化成文字里出现的样子（与 KPI 卡片同一套规则）：金额带「元」、百分比带「%」。"""
    if v is None:
        return "—"
    if _as_num(v) is None:
        return str(v)
    f = format_kpi_value(_as_num(v), fmt, unit, decimals)
    u = f["unit"]
    return f"{f['display']}%" if u == "%" else (f"{f['display']} {u}" if u else f["display"])


def fill_placeholders(text, kpis, queries, stats) -> tuple[str, list]:
    """把文字里的 {{kpi1}} / {{q2.r0.gmv|amount}} / {{s1.mean|number:3}} 换成格式化好的值。返回 (新文字, 错误)。"""
    if not isinstance(text, str) or "{{" not in text:
        return text, []
    errs = []

    def rep(m):
        ki, qi, row, col, si, key, fmt, dec = m.groups()
        if qi is not None and 1 <= int(qi) <= len(queries or []):   # 占位符前面 24 个字点名的行要和引用的行一致
            ctx = text[max(0, m.start() - 24):m.start()]
            ctx = ctx.rsplit("。", 1)[-1].rsplit("；", 1)[-1]
            be = binding_error(queries[int(qi) - 1], int(row), _PH.sub(" ", ctx))
            if be:
                errs.append(f"{m.group(0)}：{be}")
            dt = decimal_truncated(queries[int(qi) - 1], col)
            if dt:
                errs.append(f"{m.group(0)}：{dt}")
        dec = int(dec) if dec is not None else None
        if ki is not None:
            k = int(ki)
            hit = [x for x in kpis or [] if x.get("_pos") == k] or \
                  ([kpis[k - 1]] if 1 <= k <= len(kpis or []) and "_pos" not in kpis[k - 1] else [])
            if not hit:
                errs.append(f"{m.group(0)}：第 {k} 个 KPI 不存在或没通过校验")
                return m.group(0)
            kp = hit[0]
            disp, u = kp.get("display", ""), kp.get("unit", "")
            return f"{disp}%" if u == "%" else (f"{disp} {u}" if u else str(disp))
        if qi is not None:
            v, e = _cell(queries, int(qi), int(row), col)
            if e:
                errs.append(f"{m.group(0)}：{e}")
                return m.group(0)
            mis = pct_misuse(queries[int(qi) - 1], col, v, fmt)
            if mis:
                errs.append(f"{m.group(0)}：{mis}")
            return format_text_value(v, fmt, dec)
        s = int(si)
        if not 1 <= s <= len(stats or []) or key not in stats[s - 1]:
            errs.append(f"{m.group(0)}：统计量不存在（stats_index 1～{len(stats or [])}）")
            return m.group(0)
        return format_text_value(stats[s - 1][key], fmt or "number", dec)

    return _PH.sub(rep, text), errs


def _flat_nums(v, out):
    if isinstance(v, dict):
        for x in v.values():
            _flat_nums(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _flat_nums(x, out)
    elif _as_num(v) is not None and not isinstance(v, str):
        out.append(float(v))
    return out


def _number_pool(queries, kpis, stats, extra=None) -> tuple[list, set]:
    """页面上允许出现的数：所有查询结果的数值格子、行数、SQL 里的数字字面量（阈值等），KPI 原值，统计量。"""
    pool, lits = [], set()
    for q in queries or []:
        pool.append(float(len(q.get("rows") or [])))
        for r in q.get("rows") or []:
            for c in (r.values() if isinstance(r, dict) else r):
                x = _as_num(c)
                if x is not None:
                    pool.append(x)
        lits |= {float(x) for x in _re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", q.get("text") or "")}
    for k in kpis or []:
        x = _as_num(k.get("value")) if isinstance(k, dict) else None
        if x is not None:
            pool.append(x)
    for s in stats or []:
        pool += [float(v) for v in s.values() if _as_num(v) is not None]
    pool += list(extra or [])                              # compute_stats 结果里嵌套的数（各点的 Z 分数、贡献占比等）
    return pool, lits


def derived_pool(queries, kpis) -> dict:
    """能直接从结果里算出来的简单推算数，按类型分开（避免「占比 3.3%」被拿去对「3.3 倍」）：
      ratio 倍数 a/b；pct 比例 (a−b)/b、a/b、占列合计（都是 0～1 的比例，文字里的 % 按 ×0.01 去对）；diff 差值 a−b。
    为免碰巧对上，只在这些组合里算：KPI 两两之间；同一行的数值列之间；同一列里第 0 行与其他行、相邻两行之间。"""
    out = {"ratio": [], "pct": [], "diff": []}

    def add(a, b):
        if b:
            out["ratio"].append(a / b)
            out["pct"] += [a / b, (a - b) / b]
        out["diff"].append(a - b)

    def both(a, b):
        add(a, b)
        add(b, a)
    nums_k = [x for x in (_as_num(k.get("value")) for k in kpis or [] if isinstance(k, dict)) if x is not None]
    for i, a in enumerate(nums_k):
        for b in nums_k[i + 1:]:
            both(a, b)
    for q in queries or []:
        rows = [list(r.values()) if isinstance(r, dict) else list(r) for r in (q.get("rows") or [])[:60]]
        if not rows:
            continue
        for r in rows:                                     # 同一行
            v = [x for x in (_as_num(c) for c in r) if x is not None]
            for i, a in enumerate(v):
                for b in v[i + 1:]:
                    both(a, b)
        for ci in range(len(rows[0])):                     # 同一列
            col = [_as_num(r[ci]) if ci < len(r) else None for r in rows]
            if any(c is None for c in col) or len(col) < 2:
                continue
            tot = sum(col)
            if tot:
                out["pct"] += [c / tot for c in col]
            for j in range(1, len(col)):
                both(col[0], col[j])
                if j > 1:
                    both(col[j - 1], col[j])
    return out


def unbacked_numbers(text, pool, lits, derived=None) -> list:
    """文字里（占位符之外）手写的数字，在查询结果 / KPI / 统计量里都找不到的，返回它们。
    比较按显示位数四舍五入：写「29.4 万」对得上 293,963.85，写「12%」对得上 0.1234 / 12.34；正负号不计。"""
    if not isinstance(text, str) or not text:
        return []
    t = _PH.sub(" ", text)
    for p in _EXEMPT:
        t = _re.sub(p, " ", t)
    bad = []
    for m in _NUM.finditer(t):
        s, unit = m.group(1).replace("−", "-").replace(",", ""), m.group(2) or ""
        try:
            v = abs(float(s))
        except ValueError:
            continue
        d = len(s.split(".")[1]) if "." in s else 0
        # 「约 5,300」「近 1,600 人」：前面有约数词的整数，末尾的 0 是取整位，容差按取整位算（5,338 → 约 5,300）
        approx = bool(_re.search(r"(?:约|大约|近|接近|将近|大概|左右的?)\s*$", t[max(0, m.start() - 4):m.start()]))
        if d == 0 and v >= 100 and approx:
            z = len(s) - len(s.rstrip("0"))
            if z:
                d = -z
        if not unit and d == 0 and v <= 10:            # 「3 个渠道」「2 条」这类小整数不当数值结论
            continue
        if not unit and v in lits:                     # SQL 里写的阈值 / 条件（如满 50、≥ 3 单）
            continue
        k = 2.0 if approx else 1.0                         # 有约数词：容差放宽到末位一整个单位
        cands = []
        if unit in _SCALE:
            cands.append((v * _SCALE[unit], k * 0.5 * 10 ** -d * _SCALE[unit]))
        elif unit in ("%", "‰"):
            sc = 0.01 if unit == "%" else 0.001
            cands.append((v * sc, k * 0.5 * 10 ** -d * sc))
            if v >= 1:                                     # 「17.27%」也可能对应百分数原值 17.27；但「0.2%」不能拿 0.1727 这种比例去对
                cands.append((v, k * 0.5 * 10 ** -d))
        elif unit == "倍":
            cands.append((v, k * 0.5 * 10 ** -d))
        else:
            cands.append((v, k * 0.5 * 10 ** -d))
        hit = lambda P, cs: any(abs(abs(x) - c) <= tol * 1.0001 + 1e-9 or (c and abs(abs(x) - c) <= abs(c) * 1e-4)
                                for x in P for c, tol in cs)
        ok = hit(pool, cands)
        if not ok and derived:                             # 推算数按单位对应类型去对：倍→比值，%→比例，其余→差值
            if unit == "倍":
                ok = hit(derived["ratio"], [(v, 0.5 * 10 ** -d)])
            elif unit in ("%", "‰"):
                sc = 0.01 if unit == "%" else 0.001
                ok = hit(derived["pct"], [(v * sc, 0.5 * 10 ** -d * sc)])
            else:
                ok = hit(derived["diff"], cands) or (not unit and hit(derived["ratio"], [(v, 0.5 * 10 ** -d)]))
        if not ok:
            bad.append(m.group(0).strip())
    return bad


def _ycols(dr) -> list:
    out = []
    for y in (dr.get("y") if isinstance(dr.get("y"), list) else [dr.get("y")] if dr.get("y") else []):
        out.append(dict(y) if isinstance(y, dict) else {"col": y})
    return out


def _fmt_of(y) -> dict:
    return {k: y.get(k) for k in ("format", "decimals", "unit") if y.get(k) is not None}


def _chart_has_numbers(spec) -> bool:
    if not isinstance(spec, dict):
        return False
    vals = [v for s in spec.get("series") or [] if isinstance(s, dict) for v in (s.get("data") or [])]
    vals += list(spec.get("values") or [])
    vals += [it.get("value") for it in spec.get("items") or [] if isinstance(it, dict)]
    vals += [c[2] for c in spec.get("cells") or [] if isinstance(c, (list, tuple)) and len(c) > 2]
    return any(_as_num(v) is not None for v in vals)


def resolve_chart(spec, queries, stats, pool=None, lits=None) -> tuple[dict, list]:
    """chart.data_ref → 从查询记录取原值填出 x / series / items / cells。返回 (新 spec, 错误)。
    data_ref：{query_index, x: 列名, y: [列名 或 {col, name?, format?, decimals?, unit?, axis?, kind?, markPoints?, markLines?}],
              sort?: {col, desc?}, limit?}；heatmap 用 {query_index, x, y, value}。"""
    if not isinstance(spec, dict) or not spec.get("type"):
        return spec, []
    title = spec.get("title") or spec.get("type")
    dr = spec.get("data_ref")
    if not isinstance(dr, dict):
        return spec, ([f"图表「{title}」的数据是手写的：请用 data_ref 指明查询和列，不要在 chart 里写数"]
                      if _chart_has_numbers(spec) else [])
    try:
        qi = int(dr.get("query_index"))
    except (TypeError, ValueError):
        return spec, [f"图表「{title}」的 data_ref.query_index 必须是整数"]
    if not 1 <= qi <= len(queries):
        return spec, [f"图表「{title}」的 data_ref.query_index={qi} 不存在（本次查询 1～{len(queries)}）"]
    q = queries[qi - 1]
    cols = q.get("columns") or []
    rows = [list(r.values()) if isinstance(r, dict) else list(r) for r in q.get("rows") or []]
    idx = lambda c: cols.index(c) if c in cols else (int(c) if str(c).isdigit() and int(c) < len(cols) else None)
    srt = dr.get("sort")
    if isinstance(srt, dict) and idx(srt.get("col")) is not None:
        si = idx(srt.get("col"))
        rows = sorted(rows, key=lambda r: (_as_num(r[si]) is None, _as_num(r[si]) if _as_num(r[si]) is not None else str(r[si])),
                      reverse=bool(srt.get("desc")))
    if isinstance(dr.get("limit"), int) and dr["limit"] > 0:
        rows = rows[:dr["limit"]]
    t = spec.get("type")
    out = {k: v for k, v in spec.items() if k not in ("data_ref", "x", "series", "categories", "values", "note",
                                                      "items", "xLabels", "yLabels", "cells")}
    errs = []
    if t == "heatmap":
        cx, cy, cv = idx(dr.get("x")), idx(dr.get("y") if not isinstance(dr.get("y"), list) else None), idx(dr.get("value"))
        if None in (cx, cy, cv):
            return spec, [f"热力图「{title}」的 data_ref 要给 x / y / value 三个列名（列：{cols}）"]
        lab = dr.get("labels") if isinstance(dr.get("labels"), dict) else {}
        xs, ys = [], []
        for r in rows:
            if str(r[cx]) not in xs:
                xs.append(str(r[cx]))
            if str(r[cy]) not in ys:
                ys.append(str(r[cy]))
        out.update(xLabels=[str(lab.get(x, x)) for x in xs], yLabels=[str(lab.get(y, y)) for y in ys],
                   cells=[[xs.index(str(r[cx])), ys.index(str(r[cy])), _as_num(r[cv])] for r in rows], fmt=_fmt_of(dr))
        return out, []
    cx = idx(dr.get("x"))
    ys = _ycols(dr)
    if cx is None and dr.get("x") is None and ys and len(rows) == 1 and t in ("bar", "hbar", "pie", "funnel"):
        bad = [y.get("col") for y in ys if idx(y.get("col")) is None]
        if bad:
            return spec, [f"图表「{title}」的 data_ref.y 里的列 {bad} 不在 #{qi} 的结果里（列：{cols}）"]
        names = [y.get("name") or str(y["col"]) for y in ys]   # 只有一行：每列一根柱 / 一块扇区
        vals = [_as_num(rows[0][idx(y["col"])]) for y in ys]
        f0 = _fmt_of(ys[0])
        if t == "bar":
            out.update(x=names, series=[{"name": title, "data": vals, "fmt": f0}])
        elif t == "hbar":
            out.update(categories=names, values=vals, fmt=f0)
        else:
            out.update(items=[{"name": n, "value": v} for n, v in zip(names, vals)], fmt=f0)
        return out, []
    if cx is None or not ys:
        return spec, [f"图表「{title}」的 data_ref 要给 x（列名）和 y（至少一列）；本次查询的列：{cols}"]
    bad = [y.get("col") for y in ys if idx(y.get("col")) is None]
    if bad:
        return spec, [f"图表「{title}」的 data_ref.y 里的列 {bad} 不在 #{qi} 的结果里（列：{cols}）"]
    lab = dr.get("labels") if isinstance(dr.get("labels"), dict) else {}
    xv = ["—" if r[cx] is None else str(lab.get(str(r[cx]), r[cx])) for r in rows]
    yv = [[_as_num(r[idx(y["col"])]) for r in rows] for y in ys]
    for y, data in zip(ys, yv):
        mis = pct_misuse(q, cols[idx(y["col"])], next((v for v in data if v), None), y.get("format"))
        if mis:
            errs.append(f"图表「{title}」：{mis}")
    if t in ("line", "bar"):
        series = []
        for y, data in zip(ys, yv):
            s = {"name": y.get("name") or str(y["col"]), "data": data, "fmt": _fmt_of(y)}
            for k in ("axis", "kind"):
                if y.get(k):
                    s[k] = y[k]
            pts = []
            for p in y.get("markPoints") or []:            # 异常点：按 x 标签定位，y 值取数据里的原值
                if not isinstance(p, dict):
                    continue
                xl = p.get("x") if p.get("x") is not None else (p.get("coord") or [None])[0]
                if xl is not None and str(xl) in xv:
                    nm = str(p.get("name") or p.get("label") or "")
                    if pool is not None and unbacked_numbers(nm, pool, lits or set()):
                        errs.append(f"图表「{title}」的 markPoints 名称 {nm!r} 里有找不到出处的数字：name 只写文字（如「异常」）")
                    pts.append({"coord": [str(xl), data[xv.index(str(xl))]], "name": nm})
                else:
                    errs.append(f"图表「{title}」的 markPoints 标在 x={xl!r}，但数据里没有这个 x")
            lines = []
            for ml in y.get("markLines") or []:            # 均值线 / 阈值线：引用统计量或查询格子；手写的值要能在结果里找到
                if not isinstance(ml, dict):
                    continue
                r_ = ml.get("ref") or {}
                if r_.get("stats_index") is not None:
                    try:
                        val = (stats or [])[int(r_["stats_index"]) - 1][r_.get("key")]
                    except (IndexError, KeyError, TypeError, ValueError):
                        errs.append(f"图表「{title}」的 markLines 引用的统计量不存在")
                        continue
                elif r_.get("query_index") is not None:
                    val, e = _cell(queries, int(r_["query_index"]), int(r_.get("row", 0)), r_.get("col"))
                    if e:
                        errs.append(f"图表「{title}」的 markLines：{e}")
                        continue
                else:
                    val = _as_num(ml.get("value") if ml.get("value") is not None else ml.get("yAxis"))
                    if val is None or (pool is not None and unbacked_numbers(repr(val), pool, lits or set())):
                        errs.append(f"图表「{title}」的 markLines 值 {ml.get('value')!r} 没有出处：用 ref 引用 compute_stats 的统计量")
                        continue
                lines.append({"value": _as_num(val), "name": ml.get("name") or ml.get("label") or ""})
            if pts:
                s["markPoints"] = pts
            if lines:
                s["markLines"] = lines
            series.append(s)
        out.update(x=xv, series=series)
    elif t == "hbar":
        out.update(categories=xv, values=yv[0], fmt=_fmt_of(ys[0]))
        if len(ys) > 1:                                    # 第二列作每条的附注（如跳出率），按它的 format 格式化
            out["note"] = [f"{ys[1].get('name') or ys[1]['col']} {format_text_value(v, ys[1].get('format'), ys[1].get('decimals'))}"
                           for v in yv[1]]
    elif t in ("pie", "funnel"):
        out.update(items=[{"name": n, "value": v} for n, v in zip(xv, yv[0])], fmt=_fmt_of(ys[0]))
    else:
        return spec, [f"图表「{title}」的 type={t!r} 不支持"]
    return out, errs


def resolve_table(tb, queries) -> tuple[dict | None, list]:
    """table：{query_index, title?, columns?: [列名 或 {col, label?, format?, decimals?}], limit?（默认 20，最多 50）}
    → {title, columns: [表头], rows: [[显示文本]], num: [是否数值列], total}。"""
    if not isinstance(tb, dict) or tb.get("query_index") is None:
        return None, []
    try:
        qi = int(tb.get("query_index"))
    except (TypeError, ValueError):
        return None, ["table.query_index 必须是整数"]
    if not 1 <= qi <= len(queries):
        return None, [f"table.query_index={qi} 不存在（本次查询 1～{len(queries)}）"]
    q = queries[qi - 1]
    cols = q.get("columns") or []
    rows = [list(r.values()) if isinstance(r, dict) else list(r) for r in q.get("rows") or []]
    spec = [c if isinstance(c, dict) else {"col": c} for c in (tb.get("columns") or cols)]
    idxs = [cols.index(c["col"]) if c.get("col") in cols else None for c in spec]
    if None in idxs:
        return None, [f"table.columns 里的列 {[c.get('col') for c, i in zip(spec, idxs) if i is None]} 不在 #{qi} 的结果里（列：{cols}）"]
    errs = []
    for c, i in zip(spec, idxs):
        mis = pct_misuse(q, cols[i], next((r[i] for r in rows if _as_num(r[i])), None), c.get("format"))
        if mis:
            errs.append(f"table：{mis}")
    lim = tb.get("limit") if isinstance(tb.get("limit"), int) and tb["limit"] > 0 else 20
    lim = min(lim, 50)
    def unit_of(c):                                        # 一列的单位：显式给的 unit 优先；金额默认「元」；百分比的 % 留在格子里
        if c.get("format") in ("percent", "fraction"):
            return ""
        return str(c.get("unit") or ("元" if c.get("format") == "amount" else "")).strip()

    def cell(v, c):                                        # 有单位的列：格子里只放数，单位统一放到表头
        fmt = c.get("format") or ("number" if _as_num(v) is not None else None)
        if _as_num(v) is None:
            lab = c.get("labels") if isinstance(c.get("labels"), dict) else {}
            return format_text_value(lab.get(str(v), v) if v is not None else v, fmt, c.get("decimals"))
        if fmt in ("percent", "fraction"):
            return format_text_value(v, fmt, c.get("decimals"))
        return format_kpi_value(_as_num(v), fmt, None, c.get("decimals"))["display"]

    def head(c):
        lab, u = str(c.get("label") or c["col"]), unit_of(c)
        return f"{lab}（{u}）" if u and u not in lab else lab
    body = [[cell(r[i], c) for c, i in zip(spec, idxs)] for r in rows[:lim]]
    num = [bool(rows) and all(_as_num(r[i]) is not None for r in rows if r[i] is not None) for i in idxs]
    return {"title": tb.get("title") or "", "columns": [head(c) for c in spec], "rows": body,
            "num": num, "total": len(rows)}, errs


_MD_TABLE = _re.compile(r"^\s*\|.*\|\s*$", _re.M)


def _row_keys(q, row):
    """一行里的文字键（渠道名、日期等非数值格子），日期再加一个 MM-DD 的简写。"""
    r = (q.get("rows") or [])[row]
    r = list(r.values()) if isinstance(r, dict) else list(r)
    keys = set()
    for c in r:
        if c is None or _as_num(c) is not None:
            continue
        t = str(c).strip()
        if len(t) >= 2:
            keys.add(t)
            m = _re.match(r"\d{4}-(\d{2}-\d{2})", t)
            if m:
                keys.add(m.group(1))
    return keys


def binding_error(q, row, context) -> str | None:
    """context（KPI 的标签 / 备注，或占位符前面那一小段文字）点名了结果里某一行的键，却不是被引用的这一行 → 报错。"""
    if not context or not (q.get("rows") or []) or not 0 <= row < len(q["rows"]):
        return None
    mine = _row_keys(q, row)
    if any(k in context for k in mine):
        return None
    for i in range(len(q["rows"])):
        if i == row:
            continue
        other = [k for k in _row_keys(q, i) if k in context and not any(k in m or m in k for m in mine)]
        if other:
            return (f"文字点名的是「{other[0]}」，引用的却是 #{q.get('index')} 第 {row} 行"
                    f"（{'、'.join(sorted(mine)[:2]) or '另一行'}）：row 要指向「{other[0]}」那一行")
    return None


def resolve_page(args, kpis, queries, stats, extra_nums=None) -> tuple[dict, list]:
    """present_result 除 KPI 外的页面内容：图表、表格按出处取值；文字里的占位符替换、裸数字核对。返回 (page, 错误)。"""
    pool, lits = _number_pool(queries, kpis, stats, extra_nums)
    derived = derived_pool(queries, kpis)
    errs, page = [], {}
    for key in ("chart",):
        if isinstance(args.get(key), dict) and args[key]:
            page[key], e = resolve_chart(args[key], queries, stats, pool, lits)
            errs += e
    if isinstance(args.get("charts"), list):
        page["charts"] = []
        for c in args["charts"]:
            r, e = resolve_chart(c, queries, stats, pool, lits)
            page["charts"].append(r)
            errs += e
    page["table"], e = resolve_table(args.get("table"), queries)
    errs += e

    def text(field, s):
        if not isinstance(s, str):
            return s
        s = s.replace("\\n", "\n")                       # 模型偶尔把换行写成字面的 \n
        if field == "insight" and _MD_TABLE.search(s):
            errs.append("insight 里手写了 markdown 表格：表格请用 table 字段（指明查询和列），insight 只写 1-3 句结论")
        filled, e = fill_placeholders(s, kpis, queries, stats)
        errs.extend(e)
        bad = unbacked_numbers(s, pool, lits, derived)
        if bad:
            errs.append(f"{field} 里的 {bad} 在查询结果里找不到，也不是结果里两个数的比 / 差 / 增长率")
        return filled

    page["insight"] = text("insight", args.get("insight", ""))
    page["followups"] = [fill_placeholders(f, kpis, queries, stats)[0] if isinstance(f, str) else f
                         for f in args.get("followups") or []]
    for c in ([page.get("chart")] if page.get("chart") else []) + (page.get("charts") or []):
        if isinstance(c, dict) and isinstance(c.get("title"), str):
            c["title"] = fill_placeholders(c["title"], kpis, queries, stats)[0]
    page["interpreted"], _ = fill_placeholders(args.get("interpreted", ""), kpis, queries, stats)
    finds = []
    for f in args.get("findings") or []:
        if isinstance(f, dict):
            f = dict(f)
            f["text"] = text("findings", f.get("text", ""))
            if f.get("evidence"):
                f["evidence"] = text("findings.evidence", f["evidence"])
        finds.append(f)
    page["findings"] = finds
    for k in kpis or []:                                   # KPI 的 delta / note / baseline 也是文字，同样处理
        for fld in ("delta", "note", "baseline"):
            if isinstance(k.get(fld), str) and k[fld]:
                k[fld] = text(f"KPI「{k.get('label')}」的 {fld}", k[fld])
        if k.get("baseline_value") is not None and not isinstance(k.get("baseline_value"), str):
            k["baseline_value"] = str(k["baseline_value"])
        if isinstance(k.get("baseline_value"), str):
            k["baseline_value"] = text(f"KPI「{k.get('label')}」的 baseline_value", k["baseline_value"])
    m = args.get("method")
    if isinstance(m, dict) and m.get("stats"):              # 统计方法面板里的统计量：与 KPI 同样按 ref 取值
        st = [s for s in m.get("stats") if isinstance(s, dict)]
        resolved, e = resolve_kpis(st, queries, stats)
        errs += [x.replace("的数值", "（统计量）的数值") for x in e]
        page["method"] = dict(m, stats=resolved)
    return page, errs


def _unbacked_answer_kpis(kpis, ans) -> list:
    """kpis 里 is_answer=true 的数，若在被引用查询结果的任何单元格里都找不到（容差 0.5%，按 KPI 单位换算），返回它们。"""
    import re
    cells = []
    for a in ans or []:
        for r in a.get("rows") or []:
            vals = r.values() if isinstance(r, dict) else (r if isinstance(r, (list, tuple)) else [r])
            for c in vals:
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    cells.append(float(c))
                else:
                    try:
                        cells.append(float(str(c).replace(",", "")))
                    except (TypeError, ValueError):
                        pass
    out = []
    for k in kpis or []:
        if not (isinstance(k, dict) and k.get("is_answer")):
            continue
        m = re.search(r"-?\d[\d,]*\.?\d*", str(k.get("value", "")))
        if not m:
            continue
        v = float(m.group().replace(",", ""))
        unit = str(k.get("unit", "")).strip()
        cands = [v, v * _UNIT[unit]] if unit in _UNIT else [v]
        if unit == "%" or "%" in str(k.get("value", "")):     # 「31.55%」把百分号写在值里
            cands += [v / 100]
        if not any(abs(c - x) <= max(abs(x) * 0.005, 0.005) for c in cells for x in cands):
            out.append(k.get("value"))
    return out


def _with_log(t, wrapper):
    import dataclasses
    return dataclasses.replace(t, handler=wrapper)


def build_server(qlog: "QueryLog | None" = None):
    tools = [read_doc, run_sql, call_metric, compute_stats, present_result]
    if qlog is not None:
        async def run_sql_logged(args):
            res = await run_sql.handler(args)
            if not res.get("is_error"):
                data = json.loads(res["content"][0]["text"])
                types = data.pop("column_types", None)        # 只进查询记录，不改模型看到的工具输出
                k = qlog.add("sql", args.get("sql", ""), data.get("columns"), data.get("rows"), data.get("truncated"), types)
                data["query_index"] = k
                res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            return res

        async def call_metric_logged(args):
            res = await call_metric.handler(args)
            if not res.get("is_error"):
                data = json.loads(res["content"][0]["text"])
                if "result" in data:
                    r = data["result"]
                    types = r.pop("column_types", None)
                    sig = f"{data.get('metric')}(group_by={args.get('group_by') or []}, filters={args.get('filters') or {}}, time_window={args.get('time_window') or ''})"
                    data["query_index"] = qlog.add("metric", sig, r.get("columns"), r.get("rows"), r.get("truncated"), types)
                    res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            return res

        async def present_result_logged(args):
            res = await _present_result_checked(args)
            if res.get("is_error"):
                qlog.reject_log.append(res["content"][0]["text"][:600])
            return res

        async def _present_result_checked(args):
            ans, err = qlog.resolve(args.get("answer"))
            if err:
                return {"content": [{"type": "text", "text": err}], "is_error": True}
            # 被打回后补查询是「追加」：之前提交过的引用自动保留（防止只引用新补的总数、丢掉原来那张分组表）
            have = {a["index"] for a in ans}
            ans = ans + [dict(q) for q in qlog.prev_refs if q["index"] not in have and q["index"] not in qlog.format_bad]
            qlog.prev_refs = [dict(a) for a in ans]
            # SQL 只算数：answer 引用的查询最外层输出列不许取整 / 截位（打回，让 agent 补一条保留原值的查询）
            fmt = {a["index"]: _formatted_output_cols(a["text"]) for a in ans if a.get("kind") == "sql"}
            fmt = {k: v for k, v in fmt.items() if v}
            if fmt and qlog.rejects < 2:
                qlog.rejects += 1
                qlog.format_bad |= set(fmt)
                where = "；".join(f"#{k} 的 {', '.join(v)}" for k, v in fmt.items())
                return {"content": [{"type": "text", "text":
                        f"answer 引用的查询对输出做了取整 / 截位（{where}）。SQL 只算数、不做格式化：请把这几列的 round / "
                        "CAST 截位去掉重跑一次（比率用 CAST(... AS DECIMAL(38,6))），用新查询替换 answer 里的旧查询，"
                        "四舍五入只在卡片和文字里做；再重新调用 present_result。"}], "is_error": True}
            qlog.formatted = sorted(fmt)
            kpis = [dict(k) for k in (args.get("kpis") or []) if isinstance(k, dict)]
            marked_now = [k for k in kpis if k.get("is_answer")]
            # 被打回后不许靠去掉标记过关：之前标过的 label 仍按答案数校验；整个去掉标记也打回
            for k in kpis:
                if k.get("label") in qlog.marked_labels:
                    k["is_answer"] = True
            dropped = bool(qlog.marked_labels) and not marked_now
            qlog.marked_labels |= {k.get("label") for k in marked_now}
            # KPI 数值按出处取引擎原值（数值 KPI 必须给 ref）
            resolved, kerr = resolve_kpis(kpis, qlog.queries, qlog.stats, _number_pool(qlog.queries, [], qlog.stats, qlog.stats_nums)[0])
            ans_idx = {a["index"] for a in ans}
            missing = [k.get("label") for k in resolved if k.get("is_answer") and isinstance(k.get("ref"), dict)
                       and k["ref"].get("query_index") is not None and int(k["ref"]["query_index"]) not in ans_idx]
            qlog.unbacked = kerr + [f"「{m}」不在 answer 引用的查询里" for m in missing]
            qlog.dropped_mark = qlog.dropped_mark or dropped
            # 图表 / 表格 / 文字：按出处取值、替换占位符、核对手写数字
            page, perr = resolve_page(args, resolved, qlog.queries, qlog.stats, qlog.stats_nums)
            if qlog.deep:                        # 深度题必须给统计方法面板：方法名、步骤、公式、统计量
                m = args.get("method") if isinstance(args.get("method"), dict) else {}
                lack = [k for k in ("name", "steps", "formula", "stats") if not m.get(k)]
                if lack:
                    perr = perr + [f"深度分析题要填 method（统计方法面板），缺 {lack}：name 方法名、steps 步骤、"
                                   "formula 公式、stats 统计量（用 ref 引用 compute_stats 的结果）"]
            qlog.page_unbacked = perr
            if (kerr or missing or dropped or perr) and qlog.rejects < 2:   # 最多打回 2 次，避免死循环；第 3 次放行
                qlog.rejects += 1
                if perr and not (kerr or missing or dropped):
                    return {"content": [{"type": "text", "text":
                            f"页面校验没过：{'；'.join(perr)}。只改上面这几处（数字换成占位符 {{{{kpi1}}}} / {{{{q2.r0.列名|format}}}}，"
                            "或改写成结果里有的数；图表用 data_ref），其余字段原样保留，重新调用 present_result。"}],
                            "is_error": True}
                if dropped and not (kerr or missing):
                    return {"content": [{"type": "text", "text":
                            "上一次标了 is_answer 的答案数这次去掉了标记。不要去掉标记：请补一条直接算出该答案数的 SQL，"
                            "把它加进 answer，保留 is_answer 后重新调用 present_result。"}], "is_error": True}
                msg = "；".join(qlog.unbacked + perr)
                return {"content": [{"type": "text", "text":
                        f"kpis 校验没过：{msg}。KPI 的数不要手写：用 ref={{query_index, row, col}} 指向查询结果里的那个格子"
                        "（row 从 0 起，col 写列名），并给 format（amount / count / percent / fraction / ratio）；"
                        "日均、占比、环比这类推算出来的数，先写一条 SQL 把它算出来再引用；标了 is_answer 的数，"
                        "ref 要指向 answer 里引用的查询（新补的查询追加进 answer）。改好后重新调用 present_result。"}],
                        "is_error": True}
            qlog.answer = ans
            qlog.kpis = resolved                # 放行时只保留有出处的数值 KPI（打回 2 次后仍没出处的不展示）
            qlog.page = page
            args = dict(args, kpis=resolved)
            return await present_result.handler(args)

        async def compute_stats_logged(args):
            res = await compute_stats.handler(args)
            if not res.get("is_error"):
                data = json.loads(res["content"][0]["text"])
                r = data.get("result") or {}
                # 嵌套结果也能引用：按点号路径展开（max_abs_z.z、anomalies.0.value、points.16.z）
                scalars = {}

                def _flat(v, path):
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            _flat(vv, f"{path}.{kk}" if path else str(kk))
                    elif isinstance(v, list):
                        for i, vv in enumerate(v[:200]):
                            _flat(vv, f"{path}.{i}")
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        scalars[path] = v
                _flat(r, "")
                qlog.stats.append(scalars)
                qlog.stats_nums += _flat_nums(r, [])
                data["stats_index"] = len(qlog.stats)
                res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            return res

        tools = [read_doc, _with_log(run_sql, run_sql_logged), _with_log(call_metric, call_metric_logged),
                 _with_log(compute_stats, compute_stats_logged), _with_log(present_result, present_result_logged)]
    return create_sdk_mcp_server(name="analytics", version="2.2.0", tools=tools)
