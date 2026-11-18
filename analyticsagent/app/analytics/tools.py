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
            "kpis": {"type": "array"},         # 2-4 个 {label, value, unit?, delta?, dir?('up'|'down'), note?, baseline?(对比基准,如"较上周均值"), baseline_value?, is_answer?(true=直接回答问题的数,须能在 answer 引用的查询结果里找到)}
            "chart": {"type": "object"},        # 单图: 见 agent SYSTEM 的 chart spec 约定;可含 markPoints/markLines 标记异常点/均值线
            "charts": {"type": "array"},       # 可选,多图: [chart, chart, ...] 每个同 chart 约定。给了 charts 就渲染多图(深度分析推荐)
            "insight": {"type": "string"},       # 1-3 句大白话洞察，可用 **加粗** 标重点
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

    def __init__(self):
        self.queries: list[dict] = []
        self.answer: list[dict] | None = None
        self.rejects = 0                         # is_answer 校验打回次数
        self.unbacked: list = []                 # 最终放行时仍对不上的 is_answer 数
        self.marked_labels: set = set()          # 曾经标过 is_answer 的 KPI label
        self.dropped_mark = False                # 被打回后是否试图去掉标记
        self.prev_refs: list = []                # 上一次 present_result 引用的查询（打回后保留）
        self.format_bad: set = set()            # 被判为「输出取整 / 截位」打回过的查询序号（不再自动保留）
        self.formatted: list = []                # 放行时 answer 里仍带取整的查询序号

    def reset(self) -> None:
        """每个新问题开始时清空（云端暖客户端在同一会话里连续答多个问题，工具服务器只建一次）。"""
        self.__init__()

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
            return await _present_result_checked(args)

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
            missing = _unbacked_answer_kpis(kpis, ans)
            qlog.unbacked = missing
            qlog.dropped_mark = qlog.dropped_mark or dropped
            if (missing or dropped) and qlog.rejects < 2:   # 最多打回 2 次，避免死循环；第 3 次放行但记下 unbacked
                qlog.rejects += 1
                if dropped and not missing:
                    return {"content": [{"type": "text", "text":
                            "上一次标了 is_answer 的答案数这次去掉了标记。不要去掉标记：请补一条直接算出该答案数的 SQL，"
                            "把它加进 answer，保留 is_answer 后重新调用 present_result。"}], "is_error": True}
                refs = "、".join(f"#{a['index']}" for a in ans) or "（无）"
                return {"content": [{"type": "text", "text":
                        f"kpis 里标了 is_answer 的数 {missing} 不在 answer 引用的查询结果（{refs}）里。"
                        "答案数值必须由查询直接算出：请补一条直接算出它的 SQL（如 SELECT count(*) / sum(...)），"
                        "把这次查询**追加**进 answer（原来引用的查询保留），再重新调用 present_result。"}], "is_error": True}
            qlog.answer = ans
            return await present_result.handler(args)

        tools = [read_doc, _with_log(run_sql, run_sql_logged), _with_log(call_metric, call_metric_logged),
                 compute_stats, _with_log(present_result, present_result_logged)]
    return create_sdk_mcp_server(name="analytics", version="2.2.0", tools=tools)
