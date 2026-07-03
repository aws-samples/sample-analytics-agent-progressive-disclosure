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
# 数据字典文档根（顶层 knowledge/，与 backend/ 同级；打包时 COPY knowledge/ 进 /app/knowledge）。
DOCS_ROOT = os.path.normpath(os.path.join(HERE, "..", "knowledge"))


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
    "对 app_analytics 库执行一条只读 SQL（仅 SELECT/WITH）。返回 JSON：columns/rows/rowcount/truncated。"
    "执行前必须已通过 read_doc 读过相关表的文档。",
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
    {
        "interpreted": str,   # 一句话复述你如何理解了用户的问题
        "kpis": list,         # 2-4 个 {label, value, unit?, delta?, dir?('up'|'down'), note?, baseline?(对比基准,如"较上周均值"), baseline_value?}
        "chart": dict,        # 单图: 见 agent SYSTEM 的 chart spec 约定;可含 markPoints/markLines 标记异常点/均值线
        "charts": list,       # 可选,多图: [chart, chart, ...] 每个同 chart 约定。给了 charts 就渲染多图(深度分析推荐)
        "insight": str,       # 1-3 句大白话洞察，可用 **加粗** 标重点
        "findings": list,     # 可选,深度分析要点: [{kind:'driver'|'anomaly'|'risk', text, evidence?}] 驱动项/异常项/风险项
        "source": dict,       # 可选,治理来源标注: {metric, owner, version, 口径声明} —— 来自 call_metric 时务必带上
        "method": dict,       # 可选,深度分析【统计方法说明】,前端在结果最上方展示"我们用了什么方法、哪几步、什么公式":
                              #   {name:'异常检测 · Z-score/3σ', steps:['取序列','算μ和σ','算z分数','|z|>2标异常'],
                              #    formula:[{label:'Z 分数', expr:'z = (x − μ) / σ'}, {label:'判定', expr:'|z| > 2 → 异常'}],
                              #    stats:[{label:'μ 均值', value:285.6, unit:'万'}, {label:'σ 标准差', value:42.1, unit:'万'}]}
        "followups": list,    # 3 个建议追问（短句字符串）
    },
)
async def present_result(args):
    # 载荷由 server 从 ToolUseBlock.input 读取，这里仅回执
    return {"content": [{"type": "text", "text": "已呈现给前端。"}]}


def build_server():
    return create_sdk_mcp_server(
        name="analytics",
        version="2.2.0",
        tools=[read_doc, run_sql, call_metric, compute_stats, present_result],
    )
