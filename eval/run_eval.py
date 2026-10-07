"""Eval harness：自动评测 agent 的 text-to-SQL 准确率。

用法（backend venv，本地库已 load）：
    cd eval && ../backend/.venv/bin/python run_eval.py                 # 全量
    ../backend/.venv/bin/python run_eval.py --level 1 2                # 只跑 L1/L2
    ../backend/.venv/bin/python run_eval.py --case L1-users-count      # 只跑单题
    ../backend/.venv/bin/python run_eval.py --dry-run                  # 只验金标 SQL，不调模型
    ../backend/.venv/bin/python run_eval.py --cases cases_traps.json   # 陷阱题（3 题，见下）

原理：
  1. 每题的金标口径写成 1..N 条「可接受的」golden SQL（cases.json），运行时经与
     agent 相同的只读边界（backend/db.py）现算出期望值——数据重新生成后无需改用例。
  2. 驱动 backend/agent.py 的 run_agent() 跑真实 agent（读文档→写 SQL→present_result），
     从事件流里抓 agent 的 SQL 结果（rows 事件）与最终交付（result 事件的 kpis/chart）。
  3. 按 judge.mode 比对：scalar（数值容差）/ set（键值集合）/ toplist（前K命中）/
     pair、funnel（多数值逐个容差）。命中任一 golden 变体即判对。
  4. 产出 report.md（逐题明细 + 汇总）与 report.json（原始数据，供横向对比）。
     `--dry-run` 写的是另一个文件 report.dryrun.json——它跟全量跑的记录不同构
     （没有 ok/elapsed_s，status 是 golden_ok），混在一个文件名下会让「26/26 通过」
     和「26/26 金标可执行」看起来是同一件事。这两句话差得很远。

评分之外还记录：每题耗时、读文档次数、SQL 重试次数——These are the numbers
progressive disclosure 声称要改善的，跑两种配置（有/无文档路由）就能对比。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
sys.path.insert(0, str(BACKEND))

import db  # noqa: E402  (backend/db.py — 与 agent 同一个只读边界)

CASES_PATH = HERE / "cases.json"
# 第二份用例集：**口径陷阱题**（`cases_traps.json`，3 题）。它跟 cases.json 同构，
# 分开放是因为它问的不是"算得对不对"，而是"会不会掉进那张长得很像的表里"——
# 总订单数掉进 dwd_orders_valid（1601 ≠ 2000）、净收入拿 GMV 口径顶（偏高 16%）、
# ROI 掉进 tmp_campaign_roi_analysis（roi 整列 NULL，见 verify_constants.py 的清单）。
# 用 `--cases cases_traps.json` 跑；报告写到 report.traps.* 里，不覆盖主用例集的报告。
TRAPS_PATH = HERE / "cases_traps.json"
REPORT_MD = HERE / "report.md"
REPORT_JSON = HERE / "report.json"
# --dry-run 的产物单独放：它只证明金标 SQL 能在当前后端上跑通，不含 agent 的判定结果。
# 写进同一个 report.json 会让 md 和 json 悄悄脱钩——md 还是上一次全量跑的 26/26，
# json 已经被换成 golden_ok 记录，两个文件都"存在且看着正常"。这坑我们踩过一次。


def report_dryrun_json(tag: str) -> Path:
    """dry-run 产物路径，**也按 arm 分文件**，理由跟 `report_json` 不完全一样。

    全量报告分文件是为了别覆盖基线；这里是为了让三条 arm 的金标结果**能放在一起比**。
    金标 SQL 在某条 arm 上「跑通了」和「算出同一个数」是两件事，后者要靠横向比对，
    而横向比对的前提是三份产物同时存在。原来这个路径是写死的，连着跑两条 arm，
    第二条把第一条覆盖掉——于是只能看到「当前这条 arm 通了」，永远比不成。
    """
    return HERE / (f"report.dryrun.{tag}.json" if tag else "report.dryrun.json")


def report_json(tag: str) -> Path:
    """报告落盘路径。**非 athena 的 arm 自动带后缀**。

    这不是为了整齐。三条 arm 的题目和判分器完全相同，输出路径原来是写死的
    `report.json`，于是跑 duckdb 会把 athena 那份基线**静默覆盖**——而基线是
    L7 唯一的历史参照（提示词改动要靠它判回归）。默认按 arm 分文件之后，
    忘记加 `--tag` 也不会覆盖，这比"记得加"可靠。
    """
    return HERE / (f"report.{tag}.json" if tag else "report.json")


def report_md(tag: str) -> Path:
    return HERE / (f"report.{tag}.md" if tag else "report.md")


# ---------------------------------------------------------------- utilities

def _norm_text(s: str) -> str:
    """比对用文本归一化：全半角、大小写、空白。"""
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    return re.sub(r"\s+", " ", s)


def _numbers_from(v) -> list[float]:
    """从任意值里抽出数字（'675,179.41 元' → 675179.41）。"""
    out = []
    for m in re.finditer(r"-?\d[\d,]*\.?\d*", str(v)):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return out


def _close(a: float, b: float, tol_pct: float) -> bool:
    if a == b:
        return True
    base = max(abs(a), abs(b))
    if base == 0:
        return False
    return abs(a - b) / base * 100 <= tol_pct


def _flat_values(rows: list) -> list:
    return [c for r in rows for c in (r if isinstance(r, (list, tuple)) else [r])]


# ---------------------------------------------------------------- goldens

_CONVERTERS: dict[str, object] = {}

# 后端 → 改写模块名。缺省（postgres）不改写。
_ADAPTERS = {"redshift": "pg_to_redshift", "athena": "pg_to_trino"}


def _adapt_sql(sql: str) -> str:
    """按后端方言改写金标 SQL。

    `cases.json` 里的金标写的是 Postgres 方言，它是**口径的单一真源**，不为了换库
    分叉成两份（分叉必然漂移）。语法差异一律在运行时补：

    - Redshift 不支持聚合的 `FILTER (WHERE ...)`（cases.json 里 15 处）→ `CASE WHEN`
    - Athena（Trino）不认 `x::type` 和 `interval '6 days'`（共 16 处）→ `CAST` / `interval '6' day`

    只换语法，口径一个字不动。所以数据重新生成、或者再换一次引擎，用例都不用动。
    """
    mod_name = _ADAPTERS.get(getattr(db, "BACKEND", "postgres"))
    if not mod_name:
        return sql
    if mod_name not in _CONVERTERS:
        sys.path.insert(0, str(HERE.parent / "scripts" / "gen"))
        _CONVERTERS[mod_name] = __import__(mod_name)
    return _CONVERTERS[mod_name].convert(sql)[0]


async def compute_goldens(case: dict) -> list[dict]:
    """执行该题全部 golden SQL，返回 [{label, columns, rows}]。"""
    out = []
    for g in case["golden"]:
        res = await db.run_query(_adapt_sql(g["sql"]))
        out.append({"label": g["label"], "columns": res["columns"], "rows": res["rows"]})
    return out


# ---------------------------------------------------------------- agent 侧证据收集

async def run_agent_on(question: str) -> dict:
    """跑真实 agent，收集评测所需证据。"""
    from agent import run_agent  # 延迟 import：--dry-run 不需要 SDK

    ev_sql: list[str] = []
    ev_rows: list[dict] = []        # 每次 run_sql 的 {columns, rows}
    result: dict = {}
    errors: list[str] = []
    docs: list[str] = []            # 读过的卡片路径（判红时要知道它到底读了没）
    t0 = time.perf_counter()
    async for ev in run_agent(question):
        t = ev.get("type")
        if t == "sql":
            ev_sql.append(ev.get("sql", ""))
        elif t == "rows":
            ev_rows.append({"columns": ev.get("columns", []), "rows": ev.get("rows", [])})
        elif t == "metric":
            # call_metric 的权威数也算 agent 的查询证据
            ev_rows.append({"columns": ev.get("columns", []), "rows": ev.get("rows", [])})
        elif t == "stage" and ev.get("key") == "doc":
            docs.append(str(ev.get("detail") or ""))
        elif t == "result":
            result = ev
        elif t == "error":
            errors.append(ev.get("message", ""))
    return {
        "sqls": ev_sql,
        "rowsets": ev_rows,
        "result": result,
        "errors": errors,
        "docs": docs,
        "n_docs": len(docs),
        "n_sql": len(ev_sql),
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }


# KPI 卡片声明的数量级单位 → 换算成基本单位的倍数。
# 前端按中文习惯把大数写成「144.99 万」，agent 的 kpi 就带 unit="万"。
_UNIT_SCALE = {"万": 1e4, "万元": 1e4, "万人": 1e4, "亿": 1e8, "亿元": 1e8, "千": 1e3}


def agent_numbers(evidence: dict) -> list[float]:
    """agent 给出的所有数值证据：查询结果 + kpis。判分时在这里找期望值。

    kpi 带数量级单位时**同时收原值和换算值**：本项目的 kpi 卡片按中文习惯写
    `{"value": 144.99, "unit": "万"}`，而金标 SQL 出的是 1449872.13。只收原值的话，
    「各渠道投放一共花了多少钱」会判 fail —— 但 agent 的 SQL、数据源、明细、总数全对，
    错的是判分器不认单位。这类漏判比漏抓错数更坏：它会逼着以后写用例的人去放宽容差。

    换算依据是 **agent 自己声明的 unit**，不是猜的，所以不会把两个无关的数凑到一起；
    容差 0.5% 也足够吸收 144.99 这种两位小数的舍入（差 0.0019%）。
    judge 里 unit=="pct" 同时接受 want 与 want/100 是同一个思路。
    """
    nums: list[float] = []
    for rs in evidence["rowsets"]:
        for v in _flat_values(rs["rows"]):
            if isinstance(v, (int, float)):
                nums.append(float(v))
            else:
                nums.extend(_numbers_from(v))
    for k in (evidence["result"].get("kpis") or []):
        raw = _numbers_from(k.get("value", ""))
        nums.extend(raw)
        scale = _UNIT_SCALE.get(str(k.get("unit", "")).strip())
        if scale:
            nums.extend(n * scale for n in raw)
    return nums


def agent_texts(evidence: dict) -> list[str]:
    """agent 给出的所有文本证据：查询结果字符串列 + chart 标签。"""
    texts: list[str] = []
    for rs in evidence["rowsets"]:
        texts.extend(str(v) for v in _flat_values(rs["rows"]))
    res = evidence["result"]
    for ch in ([res.get("chart")] if res.get("chart") else []) + (res.get("charts") or []):
        if not isinstance(ch, dict):
            continue
        texts.extend(str(x) for x in ch.get("x", []) or [])
        texts.extend(str(x) for x in ch.get("categories", []) or [])
        texts.extend(str(it.get("name", "")) for it in ch.get("items", []) or [])
    return texts


# ---------------------------------------------------------------- judges

def judge_scalar(case, goldens, evidence, defaults) -> tuple[bool, str]:
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    nums = agent_numbers(evidence)
    for g in goldens:
        vals = [v for v in _flat_values(g["rows"]) if isinstance(v, (int, float))]
        if not vals:
            continue
        want = float(vals[0])
        # 百分比题同时接受 62.4 与 0.624 两种表达
        cands = {want, want / 100} if case["judge"].get("unit") == "pct" else {want}
        for w in cands:
            if any(_close(n, w, tol) for n in nums):
                return True, f"命中 golden[{g['label']}]≈{want}"
    return False, f"期望 {[_flat_values(g['rows'])[:1] for g in goldens]}，agent 数值中未找到"


def judge_set(case, goldens, evidence, defaults) -> tuple[bool, str]:
    key_col = case["judge"].get("key_col", 0)
    val_col = case["judge"].get("val_col")
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    texts = {_norm_text(t) for t in agent_texts(evidence)}
    nums = agent_numbers(evidence)
    for g in goldens:
        keys = [_norm_text(r[key_col]) for r in g["rows"] if isinstance(r, (list, tuple)) and len(r) > key_col]
        if not keys:
            continue
        hit_keys = sum(1 for k in keys if any(k in t or t in k for t in texts if t))
        ok_keys = hit_keys >= max(1, round(len(keys) * 0.8))
        ok_vals = True
        if val_col is not None:
            gvals = [float(r[val_col]) for r in g["rows"]
                     if isinstance(r, (list, tuple)) and isinstance(r[val_col], (int, float))]
            hit_vals = sum(1 for v in gvals if any(_close(n, v, tol) for n in nums))
            ok_vals = hit_vals >= max(1, round(len(gvals) * 0.8))
        if ok_keys and ok_vals:
            return True, f"命中 golden[{g['label']}] 键{hit_keys}/{len(keys)}"
    return False, "键集合或对应数值未达到 80% 命中"


def judge_toplist(case, goldens, evidence, defaults) -> tuple[bool, str]:
    k = case["judge"].get("k", defaults["toplist_k"])
    min_hit = case["judge"].get("min_hit", defaults["toplist_min_hit"])
    texts = [t for t in (_norm_text(x) for x in agent_texts(evidence)) if t]
    for g in goldens:
        want = [_norm_text(r[0] if isinstance(r, (list, tuple)) else r) for r in g["rows"][:k]]
        if not want:
            continue
        hit = sum(1 for w in want if any(w in t or t in w for t in texts))
        if hit / len(want) >= min_hit:
            return True, f"命中 golden[{g['label']}] {hit}/{len(want)}"
    return False, f"前{k}名命中率未达 {min_hit:.0%}"


def judge_pair(case, goldens, evidence, defaults) -> tuple[bool, str]:
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    nums = agent_numbers(evidence)
    for g in goldens:
        vals = [float(v) for v in _flat_values(g["rows"]) if isinstance(v, (int, float))]
        if len(vals) < 2:
            continue
        if all(any(_close(n, v, tol) for n in nums) for v in vals[:2]):
            return True, f"命中 golden[{g['label']}] 两值均匹配"
    return False, "两个对比值未同时命中"


def _delivered_funnels(evidence: dict) -> list[list[float]]:
    """agent **交付**的漏斗序列（chart.items 的 value，保留步骤顺序）。

    只看交付的图，不看中间 rowset：中间查询里出现非单调的数列完全正常
    （比如按渠道拆的明细），拿它去判"非漏斗"就是一个名字比覆盖面大的检查器。
    """
    out: list[list[float]] = []
    res = evidence.get("result") or {}
    charts = ([res.get("chart")] if res.get("chart") else []) + (res.get("charts") or [])
    for ch in charts:
        if not isinstance(ch, dict) or ch.get("type") != "funnel":
            continue
        seq = [float(it["value"]) for it in (ch.get("items") or [])
               if isinstance(it, dict) and isinstance(it.get("value"), (int, float))]
        if len(seq) >= 3:
            out.append(seq)
    return out


def judge_funnel(case, goldens, evidence, defaults) -> tuple[bool, str]:
    """漏斗题判分：**先验形态、再比数值**。

    只比数值是不够的，而这个 mode 叫 `funnel`，名字担保的比它原来做的多。真实
    代价是：`L4-funnel` 的金标曾经写成每步各 `count(DISTINCT user_id)` 一遍，
    `value_hint` 就是非单调的 `394/385/396/373`，agent 照这个口径答"漏斗几乎不
    衰减、是随机种子数据的问题"，判分 PASS，归档基线 26/26。正确口径下是
    `394/314/258/199`，逐层流失 20%/18%/23%。

    所以这里加一条**不依赖金标对不对**的形态闸：交付的漏斗图必须单调不增。
    金标哪天又被写错，这条还拦得住。
    """
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    for seq in _delivered_funnels(evidence):
        bad = [i for i in range(1, len(seq)) if seq[i] > seq[i - 1]]
        if bad:
            i = bad[0]
            return False, (f"非漏斗形态：第{i + 1}步 {seq[i]:g} > 第{i}步 {seq[i - 1]:g}"
                           f"（共 {len(bad)} 处）。每步须约束成上一步的子集，"
                           f"见 knowledge/analysis/funnel_analysis.md 约束 A")
    nums = agent_numbers(evidence)
    for g in goldens:
        vals = [float(v) for v in _flat_values(g["rows"]) if isinstance(v, (int, float))]
        if not vals:
            continue
        hit = sum(1 for v in vals if any(_close(n, v, tol) for n in nums))
        if hit >= max(1, len(vals) - 1):        # 允许漏 1 步
            return True, f"命中 golden[{g['label']}] {hit}/{len(vals)} 步"
    return False, "漏斗步骤数值命中不足"


# 「cohort 之间不可比」的声明方式。**右删失/观测窗不在这里面**——那是另一回事,
# 也正是那次答错时唯一给出的 caveat：它解释了边缘的 0，没解释满窗那几周之间为什么
# 不能排名。
#
# 每一项都必须指向**机制或定论**（为什么不可比、结论因此不能下），不能是泛化的
# 否定词。第一版里放了「别当」和「不能当作」，结果那份缺陷答案**通过了**——
# 它的右删失那句写着「别当真实下跌」，于是"本该不算数的 caveat"恰好成了放行凭证。
# 白名单的每一项都得自己问一遍：它会不会被答案里另一句无关的正确话满足？
#
# **2026-09-01 加了这一条靶子。** 它和下面那条「平坦曲线要声明算不出留存」是
# **两件独立的事**，都归 judge_retention 管，别把它们当成同一条的两个版本：
#   - 形状（纵向）：曲线不衰减时，留存结论整体不可用 → `_RETENTION_CREDIBILITY`；
#   - 可比性（横向）：所有 cohort 出自同一个活跃度模型，满窗几周之间 D1 差 0.9pp、
#     D7 差 2.0pp，"哪批留得最好"没有可读的答案 → 这一条，**与形状无关，总是要求**。
# 全量重灌之后曲线是衰减的（实测 D1 59.4% / D7 22.3% / D14 14.7%，断崖在 D1），
# 于是形状那一路走不到；只留形状那一条闸，"10 月那批粘性最强"这种答案就没人拦了。
# 两头那两周是不完整的周，偏高的是窗口边缘不是用户质量。
#
# 选词上有一处是这次新踩的：**"不可比"这三个字不能单独进白名单**。右删失那句
# 标准写法是「近期 cohort 窗口不完整，不能和老 cohort 比」——它天然在谈可比性，
# 单收"不可比"等于把那句本该不算数的 caveat 又变成放行凭证（和第一版"别当"同一个坑）。
# 所以要么带上范围（`cohort 之间不可比`），要么指向机制（同一个模型 / 生成器），
# 要么指向动作（不要排名）。
_RETENTION_INCOMPARABLE = ("cohort 之间不可比", "cohort之间不可比",
                          "没有差别", "没有差异", "差异不可读", "不可读",
                          "同一个活跃度模型", "同一套活跃度模型",
                          "同一个分布", "同分布", "不是用户质量",
                          "生成器", "合成数据", "不构成业务结论",
                          "不能当业务基准", "别当业务基准", "不是真实产品")

# "别排名"这个动作有太多种说法，逐个往上面那串里塞是个填不满的坑：第一次实测就栽在
# 这上面——真 agent 写的是「不建议排名」，而白名单里躺着"不能/不要/别/不做排名"四个，
# 一个都没命中，闸把一份**完全正确**的答案判红了。所以否定 + 动作这一类改成模式匹配，
# 否定词与动作之间留 14 个字的余地（"没有必要排名""不建议做横向排名"都收得进来）。
# 余地给到 14 是因为中间常常夹一个英文词：「无需对 cohort 做横向排名」，"cohort"
# 一个词就吃掉 6 个字符——按中文语感估 8 个字会正好卡在这种句子上（自测里有这一条）。
# 「没有可读答案」是同一句话的另一种落法，一并收。
# 注意范围仍然要卡住：动作词里**没有**光秃秃的"比"——右删失那句
# 「不能和老 cohort 比」正是这个形状，收它等于让本该不算数的 caveat 放行。
_RETENTION_INCOMPARABLE_RE = re.compile(
    r"(不|别|勿|无需|无法|没有|不必)[^。；\n]{0,14}(排名|排序|横向比较|做比较)"
    r"|没有可读(的)?答案|不存在可读(的)?答案")


def _states_cohort_incomparable(prose: str) -> bool:
    """散文里有没有声明「cohort 之间不可比」。两条通路：固定说法 + 否定动作的模式。"""
    return (any(k in prose for k in _RETENTION_INCOMPARABLE)
            or bool(_RETENTION_INCOMPARABLE_RE.search(prose)))


# 「这一批数据算不出留存」的声明方式——**只在曲线不衰减时**要求（见 judge_retention）。
# 和上面那份是两条独立的判据：上面钉 cohort 之间不能排名，这里钉整条曲线不可信。
# 同一条纪律：每一项都必须指向机制或定论，泛化的否定词会被答案里另一句无关的
# 正确话满足（第一版放了「别当」，被右删失那句「别当真实下跌」放行过）。
_RETENTION_CREDIBILITY = ("算不出", "不可用", "不可信", "独立抽样", "假象",
                          "不是真实留存", "无法反映", "没有建模", "数据缺陷",
                          "不构成业务结论", "样本局限", "与注册生命周期无关")


def _delivered_prose(evidence: dict) -> str:
    """agent 交付的结论散文：interpreted + insight + findings + followups。"""
    res = evidence.get("result") or {}
    parts = [str(res.get("interpreted") or ""), str(res.get("insight") or "")]
    for f in (res.get("findings") or []):
        if isinstance(f, dict):
            parts.extend(str(v) for v in f.values())
        else:
            parts.append(str(f))
    parts.extend(str(x) for x in (res.get("followups") or []))
    return "\n".join(parts)


# 「曲线在衰减」的判据：满窗 cohort 的 W_last/W_first 平均比值，低于这个数算衰减。
# 两批实测把阈值夹得很松：v1 种子样本 W1→W4 一直在 40–48% 之间徘徊，比值 ≈ 0.99
# （就是那条 P0）；2026-09-17 的全量批次 71.6 → 37.2，比值 ≈ 0.52。0.8 落在中间，
# 不是拿某一批的数字调出来的。这个数与 knowledge/analysis/retention_curve.md 和
# knowledge/metrics/core_metrics.md 里写给 agent 的判据是同一个，改这里要一起改。
RETENTION_DECAY_MAX = 0.8


def _retention_shape(goldens) -> tuple[float | None, int]:
    """从**百分比**那份金标里量出曲线形状：各 cohort 末周/首周比值的平均。

    只认 label 里带 `pct` 的那份——人数那份的第一列是 cohort_size，混进来比值全错。
    量不出来时返回 None，判分那边**从严**处理（见 judge_retention）。
    """
    g = next((g for g in goldens if "pct" in str(g.get("label", "")).lower()), None)
    if g is None:
        return None, 0
    ratios = []
    for row in g["rows"]:
        v = [float(x) for x in row if isinstance(x, (int, float))]
        if len(v) >= 2 and v[0]:
            ratios.append(v[-1] / v[0])
    if not ratios:
        return None, 0
    return sum(ratios) / len(ratios), len(ratios)


def judge_retention(case, goldens, evidence, defaults) -> tuple[bool, str]:
    """留存题判分：**先验结论、再比数值**，而"结论该怎么写"由金标现算的曲线形状决定。

    这个 mode 的存在理由和 `funnel` 相反，值得写清楚。漏斗那次是**数值**错了
    （394/385/396/373，结算 > 浏览），所以形态闸盯的是数值。留存那次数值**全对**
    ——44.7 / 41.7 / 42.1 与独立实测分毫不差，分子也正确地限定在了 cohort 内——
    错的只有结论那几个字：「曲线平稳、留得住，10 月那批粘性最强」。纯数值判分会给
    这种答案判 PASS，因为它错的地方一个数字都不涉及。

    ## 两条闸，互相独立，都在这个函数里

    1. **横向：cohort 之间不可比** —— 总是要求。所有 cohort 出自同一个活跃度模型，
       满窗几周之间 D1 差 0.9pp、D7 差 2.0pp，"哪批留得最好"没有可读的答案。
       右删失说明**不算**——那次答错时右删失那句是完全正确的，正是它让整段话听起来
       严谨；它只解释了边缘的 0，不解释满窗那几周之间为什么不能排名。
    2. **纵向：曲线不衰减时，整条留存结论不可用** —— 只在形状判为"不衰减"时要求。

    两条盯的是不同的错：第 1 条拦「把 cohort 之间的噪声报成业务发现」，第 2 条拦
    「把一条压根没建模用户生命周期的平坦曲线报成"留得住"」。全量重灌之后曲线在衰减，
    第 2 条这一路走不到（所以它由 `--selftest` 的固定夹具覆盖，见下），但换一批数据
    它随时回来——两条都留着，不是同一条的两个版本。

    ## 为什么第 2 条不能写死「必须说算不出留存」

    第一版就是写死的，然后数据被换掉了：新生成器给用户配了活跃半衰期
    （`scripts/gen/tables.py` 的 `ENGAGEMENT_HALFLIFE`），全量批次实测
    `71.6 → 51.9 → 42.3 → 37.2`，是一条正常的前陡后平曲线。写死的闸于是开始
    **要求 agent 说一句假话**：这一批数据是算得出留存的。判据钉在数据的性质上、
    而数据可以换，就必然出现这种反转——所以形状改成**现从金标量**：

    - 曲线不衰减（W_last/W_first ≥ `RETENTION_DECAY_MAX`）→ 形状闸生效，散文里必须
      出现"这一批数据算不出留存"这个意思；
    - 曲线正常衰减 → 形状闸不适用，只比数值（此时反过来照抄那句警告也是错的，但这里
      **不做黑名单**，理由见下）。第 1 条闸不受影响，照旧要求。

    量不出形状（金标里没有 pct 那一份）→ 按最严的口径走，即仍然要求声明。判据瞎了
    就该从严，不该静默放行。

    ## 边界，别让名字比覆盖面大

    它是**关键词白名单**，不是"读懂了散文"。一个既说"算不出"又同时下"留得住"结论的
    答案，它拦不住；换个说法表达同一个错误结论（比如"用户黏性稳定"而不提数据缺陷），
    只要一个白名单词都没命中就会被拦下——拦得住是因为白名单在**要求**一句话存在，
    不是在**禁止**某句话存在。禁止型的写法这里刻意没用：正确答案里就写着
    『别当成"留存好"的正面结论』，任何以「留存好」为特征的黑名单都会把它误杀。

    还有一件事值得写下来：在**当前**这批数据上，形状闸这一路是走不到的（曲线在衰减）。
    所以它的两个分支都由 `--selftest` 里的固定夹具覆盖（平坦曲线 → 必须拦；衰减曲线 →
    必须放行），不靠"湖里正好装着哪一批"来体检——否则换一批数据就等于悄悄少了一道闸。
    """
    prose = _delivered_prose(evidence)
    ratio, n_cohorts = _retention_shape(goldens)
    if ratio is None:
        shape = "量不出曲线形状（金标里没有 pct 那一份），按最严口径要求声明"
        decaying = False
    else:
        decaying = ratio < RETENTION_DECAY_MAX
        shape = (f"金标实测 {n_cohorts} 个 cohort 的末周/首周 ≈ {ratio:.2f}"
                 f"（{'衰减' if decaying else '不衰减'}，阈值 {RETENTION_DECAY_MAX}）")

    # 闸一（横向，总是要求）：cohort 之间不可比。
    if not _states_cohort_incomparable(prose):
        return False, ("结论里没有声明「cohort 之间不可比」：所有 cohort 出自同一个"
                       "活跃度模型，满窗那几周 D1 只差 0.9pp、D7 只差 2.0pp，"
                       "答成「某批 cohort 留得最好/最差」就是把噪声报成了业务发现。"
                       "右删失说明不能替代这一条，"
                       "见 knowledge/analysis/retention_curve.md 顶部那节")

    # 闸二（纵向，只在曲线不衰减时要求）：整条留存结论不可用。
    if not decaying and not any(k in prose for k in _RETENTION_CREDIBILITY):
        return False, (f"结论里没有声明「这一批数据算不出留存」：{shape}——曲线平坦是"
                       "活跃度与注册生命周期独立抽样的结果（v1 种子样本的 P0），"
                       "把它答成「留得住/粘性好」就是把数据缺陷报成了业务发现。"
                       "「cohort 之间不可比」是另一条，不能替代这一条，"
                       "见 knowledge/analysis/retention_curve.md 顶部那两节")
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    min_hit = case["judge"].get("min_hit", 6)
    nums = agent_numbers(evidence)
    prefix = ("曲线在衰减，形状闸不适用" if decaying else "结论已声明数据限制")
    for g in goldens:
        vals = [float(v) for v in _flat_values(g["rows"]) if isinstance(v, (int, float))]
        if not vals:
            continue
        hit = sum(1 for v in vals if any(_close(n, v, tol) for n in nums))
        if hit >= min_hit:
            return True, (f"{prefix}（{shape}）；"
                          f"命中 golden[{g['label']}] {hit}/{len(vals)} 个数值")
    return False, f"{prefix}（{shape}），但留存矩阵数值命中不足 {min_hit} 个"


JUDGES = {"scalar": judge_scalar, "set": judge_set, "toplist": judge_toplist,
          "pair": judge_pair, "funnel": judge_funnel, "retention": judge_retention}


# ---------------------------------------------------------------- main

async def run_case(case: dict, defaults: dict, dry_run: bool) -> dict:
    rec = {"id": case["id"], "level": case["level"], "question": case["question"]}
    try:
        goldens = await compute_goldens(case)
        rec["goldens"] = [{"label": g["label"], "rows": g["rows"][:12]} for g in goldens]
    except Exception as e:
        rec.update(status="golden_error", detail=f"{type(e).__name__}: {e}")
        return rec
    if dry_run:
        rec.update(status="golden_ok")
        return rec

    evidence = await run_agent_on(case["question"])
    # 瞬时 infra 失败重试一次：agent 一条 SQL 都没发就返回（限流/流中断的签名，
    # 通常 7~15s 即结束）。语义性答错必然带 SQL，不会触发这条，评测口径不变。
    # 连跑 40+ 次 agent 时每轮随机被砸中 1~2 题，三轮实测 21 题各自都能通过，
    # 重试比"祈祷某轮全绿"便宜得多。
    if evidence["n_sql"] == 0 and not evidence["rowsets"]:
        await asyncio.sleep(20)
        evidence = await run_agent_on(case["question"])
        rec["retried_infra"] = True
    rec.update(elapsed_s=evidence["elapsed_s"], n_docs=evidence["n_docs"],
               agent_docs=evidence["docs"],
               n_sql=evidence["n_sql"], agent_sqls=evidence["sqls"],
               agent_errors=evidence["errors"],
               has_result=bool(evidence["result"]))
    if evidence["errors"] and not evidence["rowsets"] and not evidence["result"]:
        rec.update(status="agent_error", detail="; ".join(evidence["errors"])[:300])
        return rec

    ok, detail = JUDGES[case["judge"]["mode"]](case, goldens, evidence, defaults)
    rec.update(status="pass" if ok else "fail", detail=detail)
    # 结论级的闸（funnel / retention）判的是**散文**，而散文此前一个字都没进报告：
    # 判红时只看得到"没声明某句话"，看不到 agent 究竟说了什么，于是"它真没说"和
    # "白名单太窄"这两种情况在报告上分不开——要区分就得再跑一次真 agent（约 100s
    # 加一次 Bedrock 调用）。判红时把交付散文一起记下来，这个区分就免费了。
    if not ok:
        rec["prose"] = _delivered_prose(evidence)[:2000]
    return rec


def render_report(records: list[dict], meta: dict) -> str:
    done = [r for r in records if r["status"] in ("pass", "fail")]
    npass = sum(1 for r in done if r["status"] == "pass")
    lines = ["# Eval Report", "",
             f"- 运行时间: {meta['ts']}  · 模型: {meta['model']}"
             + f"  · arm: **{meta.get('arm', '(未记录)')}**",
             f"- 通过率: **{npass}/{len(done)}**"
             + (f" ({npass/len(done)*100:.0f}%)" if done else ""),
             f"- 平均耗时: {meta['avg_s']}s/题 · 平均读文档 {meta['avg_docs']} 次 · 平均 SQL {meta['avg_sql']} 条", ""]
    by_level: dict[int, list[dict]] = {}
    for r in done:
        by_level.setdefault(r["level"], []).append(r)
    lines.append("| Level | 通过 | 总数 |")
    lines.append("|---|---|---|")
    for lv in sorted(by_level):
        rs = by_level[lv]
        lines.append(f"| L{lv} | {sum(1 for r in rs if r['status']=='pass')} | {len(rs)} |")
    lines += ["", "## 逐题明细", "",
              "| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        icon = {"pass": "✅", "fail": "❌", "agent_error": "💥",
                "golden_error": "⚠️", "golden_ok": "✔︎(dry)"}.get(r["status"], "?")
        lines.append(
            f"| {r['id']} | {r['level']} | {icon} | {r.get('elapsed_s','-')}s "
            f"| {r.get('n_docs','-')} | {r.get('n_sql','-')} | {str(r.get('detail',''))[:80]} |")
    fails = [r for r in records if r["status"] in ("fail", "agent_error")]
    if fails:
        lines += ["", "## 失败详情", ""]
        for r in fails:
            lines += [f"### {r['id']} — {r['question']}",
                      f"- {r.get('detail','')}"]
            if r.get("agent_docs") is not None:
                lines.append("- agent 读过的卡片: "
                             + ("、".join(r["agent_docs"]) or "(一张都没读)"))
            if r.get("prose"):
                # 结论级的闸判的就是这段话，不贴出来没法判断是 agent 没说还是闸太窄
                lines += ["- agent 交付的结论散文:", "```text", r["prose"], "```"]
            lines += ["- agent SQL:", "```sql",
                      *(r.get("agent_sqls") or ["(无)"]), "```", ""]
    return "\n".join(lines)


def selftest() -> int:
    """离线自测判分器：不连库、不调模型，秒级。进 L0。

    存在的理由很具体：`judge_funnel` 原来只比数值，于是一个非单调的"漏斗"
    （`394/385/396/373`，结算 > 浏览）判 PASS 并进了归档基线。现在加了形态闸，
    而**一条没被验证过的断言随时可能变成永远绿的灯**——这里就是验证它的地方。

    同时钉住 `L4-funnel` 金标的口径本身：它一旦被改回"每步各数一遍"，
    `_looks_like_subset_funnel` 会红。金标是这道题的真源，真源错了下游全错。
    """
    fails: list[str] = []
    ran = 0

    def check(name: str, got, want):
        nonlocal ran
        ran += 1
        if got != want:
            fails.append(f"{name}: 得到 {got!r}，期望 {want!r}")

    defaults = {"tolerance_pct": 5.0}
    case = {"judge": {"mode": "funnel", "tolerance_pct": 10.0}}
    goldens = [{"label": "subset", "rows": [[394, 314, 258, 199]]}]

    def ev(seq, nums=None):
        """造一份最小 evidence：交付一张 funnel 图 + 对应的 rowset 数值。"""
        return {"result": {"chart": {"type": "funnel",
                                     "items": [{"name": f"s{i}", "value": v}
                                               for i, v in enumerate(seq)]},
                           "kpis": []},
                "rowsets": [{"columns": [], "rows": [list(nums or seq)]}]}

    # 1) 正确口径：单调 + 命中金标 → 判对
    ok, why = judge_funnel(case, goldens, ev([394, 314, 258, 199]), defaults)
    check("单调且命中金标应判对", ok, True)

    # 2) 历史缺陷本体：非单调 → 必须判错，且理由指向形态而不是"数值没命中"
    ok, why = judge_funnel(case, goldens, ev([394, 385, 396, 373]), defaults)
    check("非单调应判错", ok, False)
    if "非漏斗形态" not in why:
        fails.append(f"非单调的理由应点明形态，实际: {why}")

    # 3) 形态闸不能越权：单调但数值不对，理由该是数值不命中（不是形态）
    ok, why = judge_funnel(case, goldens, ev([100, 90, 80, 70]), defaults)
    check("单调但数值错应判错", ok, False)
    if "非漏斗形态" in why:
        fails.append(f"单调序列不该被判成非漏斗形态: {why}")

    # 4) 没交付 funnel 图时形态闸不生效，退回数值比对（别把别的图形当漏斗）
    line = {"result": {"chart": {"type": "line", "items": [{"name": "a", "value": 1},
                                                           {"name": "b", "value": 9},
                                                           {"name": "c", "value": 3}]},
                       "kpis": []},
            "rowsets": [{"columns": [], "rows": [[394, 314, 258, 199]]}]}
    ok, why = judge_funnel(case, goldens, line, defaults)
    check("非 funnel 图不应触发形态闸", ok, True)

    # 5) stats.funnel 的实时闸：同一个缺陷序列必须报 monotonic=False
    sys.path.insert(0, str(BACKEND))
    import stats  # noqa: PLC0415
    r_bad = stats.funnel([394, 385, 396, 373], labels=["浏览", "加购", "结算", "支付"])
    check("stats.funnel 应判非单调", r_bad.get("monotonic"), False)
    check("stats.funnel 应报出违规处数", len(r_bad.get("violations") or []), 1)
    r_ok = stats.funnel([394, 314, 258, 199])
    check("stats.funnel 对单调序列应判 True", r_ok.get("monotonic"), True)

    # 6) 金标口径本身：L4-funnel 必须还是子集漏斗（value_hint 单调）
    spec = json.loads(CASES_PATH.read_text())
    fc = next((c for c in spec["cases"] if c["id"] == "L4-funnel"), None)
    if fc is None:
        fails.append("cases.json 里找不到 L4-funnel")
    else:
        for g in fc["golden"]:
            # 逐步核，不是"某处有就算过"：只核存在性的话，删掉其中一步的约束
            # 照样绿——那就又是一个名字比覆盖面大的检查器（injection 实测过）。
            steps = sorted(int(m) for m in re.findall(r"\bs(\d+)\s+AS\s*\(", g["sql"]))
            if len(steps) < 3:
                fails.append(f"金标 {g['label']} 认不出分步 CTE（s1/s2/…），无法核约束 A")
                continue
            for i in steps[1:]:
                want = f"IN (SELECT user_id FROM s{i - 1})"
                if want not in g["sql"]:
                    fails.append(f"金标 {g['label']} 的 s{i} 缺少 `{want}`："
                                 f"这一步没约束成上一步的子集，口径退回独立计数")
        hint = str(fc.get("value_hint", ""))
        seq = [float(x) for x in re.findall(r"\d+", hint.split("(")[0])]
        if len(seq) >= 3 and any(seq[i] > seq[i - 1] for i in range(1, len(seq))):
            fails.append(f"金标 value_hint 非单调: {hint}")

    # 6b) 金标的**时间锚点**：不许用逐表 max() 当"今天"。
    #
    # 这一条和上面那条是同一类问题的两个实例：金标本身写着错口径，于是评测在
    # **奖励**一个知识库里明令禁止的写法。`knowledge/README.md` 的静态样本铁律写的是
    # 「禁用 current_date/now()（会查空），也禁用该表自己的 max(时间列)（不查空，
    # 查出错数）」，而 9 条金标（L1-dau-latest / L2-top-pages-7d / L3-gmv-30d ×3 /
    # L3-churn-30d ×2 / L5-wow-gmv ×2）用的正是后者。
    #
    # 它比 current_date 那种错难发现得多：多数表的时间轴末恰好等于锚点，于是逐表 max
    # 算出来**和正确答案一样**。真正会分叉的是那十几根伸出业务日历的轴——
    # `sessions.start_time` 越到 2026-01-25（跨零点）、`channel_daily_costs` 铺到
    # 2026-09-01。L3-churn-30d 就踩在第一根上：窗口整体推后一天。
    #
    # 判据只拦**当锚点用**的那一种：`(SELECT max(x) …)` 这种子查询形态。分组聚合里的
    # `max(placed_at)`（"每个用户最后一次下单"）是正当用法，不在拦截面里。
    ANCHOR_COL = "as_of_date"
    for c in spec["cases"]:
        for g in c.get("golden", []):
            for col in re.findall(r"\(\s*SELECT\s+max\(\s*([A-Za-z_][\w.]*)\s*\)",
                                  g["sql"], re.I):
                if col.split(".")[-1].lower() != ANCHOR_COL:
                    fails.append(
                        f"{c['id']} 金标 {g['label']}：时间锚点写成了逐表 "
                        f"max({col})。锚点一律用 "
                        f"(SELECT max({ANCHOR_COL}) FROM meta_snapshot)——"
                        f"逐表 max 不查空、只给一个错的数，而且在多数表上恰好"
                        f"等于锚点，所以它是那种「测试通过了、口径是错的」")

    # 7) knowledge/ 里的参考 SQL 自己得是对的口径。
    #
    # 这一组是补一个真实事故的窟窿：前六组全绿、判分器和 stats 都装了闸，
    # 而 agent 在浏览器上照旧答出 `394/385/396/373`——因为它读的是
    # `domains/behavior/events.md` 和 `metrics/core_metrics.md`，那两张卡片里的
    # 「购买漏斗」参考 SQL 本身就是每步各 `COUNT(DISTINCT CASE WHEN ...)` 一遍，
    # 还配了一句「这份种子数据漏斗不衰减，别当业务结论」。它是照抄的，抄得很忠实。
    # `verify_doc_sql.py` 只 EXPLAIN 语法，口径它不看，所以这些文件此前零覆盖。
    FUNNEL_EVENTS = ("view_product", "add_to_cart", "begin_checkout", "purchase")
    KB = HERE.parent / "knowledge"

    def sql_blocks(text: str) -> list[str]:
        """取 ```sql 围栏里的 SQL，并**剥掉 `--` 行注释**。

        剥注释不是洁癖，是这两组闸的正确性前提。这些卡片的注释里就写着
        「不用 CURRENT_DATE」「分子里数的是 c.user_id」这类警告文字，
        不剥的话：禁止型断言会被警告文字自己触发（误报），
        而要求型断言会被注释里的示例字符串满足（**永远绿的灯**，更糟）。
        """
        out = []
        for block in re.findall(r"```sql\n(.*?)```", text, re.S):
            out.append("\n".join(re.sub(r"--.*$", "", ln) for ln in block.splitlines()))
        return out

    for md in sorted(KB.rglob("*.md")):
        rel = md.relative_to(KB.parent)
        for block in sql_blocks(md.read_text()):
            # 只认「分步各算一个人数」的块。`WHERE event_name IN (…)` 的查表/趋势
            # 不是漏斗，不该被这条闸拦下（第一版就是这么误报的）。
            n_indep = len(re.findall(r"(?i)count\s*\(\s*distinct\s+case\s+when\s+event_name",
                                     block))
            # 分步 CTE：按 `名字 AS (` 切段，段里 `event_name = '<漏斗事件>'` 的算一步
            marks = [(m.group(1), m.end()) for m in re.finditer(r"(\w+)\s+AS\s*\(", block)]
            bounds = [m.start() for m in re.finditer(r"(\w+)\s+AS\s*\(", block)] + [len(block)]
            step_ctes = []
            for idx, (name, start) in enumerate(marks):
                body = block[start:bounds[idx + 1]]
                if any(re.search(rf"event_name\s*=\s*'{e}'", body) for e in FUNNEL_EVENTS):
                    step_ctes.append((name, body))
            if n_indep < 3 and len(step_ctes) < 3:
                continue
            # 7a) 每步各数一遍 = 独立计数，四个互不相干的集合，不是漏斗
            if n_indep >= 3:
                fails.append(f"{rel} 的漏斗参考 SQL 用了 {n_indep} 个独立 "
                             f"`count(DISTINCT CASE WHEN event_name…)`：这是独立计数不是漏斗，"
                             f"照它写会得到「结算 > 浏览」")
                continue
            # 7b) 分步 CTE 写法：逐步核子集约束（同约束 A，与金标同一把尺）
            for (name, body), (prev, _) in zip(step_ctes[1:], step_ctes):
                want = f"IN (SELECT user_id FROM {prev})"
                if want not in body:
                    fails.append(f"{rel} 的漏斗参考 SQL 里 {name} 缺少 `{want}`："
                                 f"这一步没约束成上一步的子集，口径退回独立计数")
        ran += 1

    # 7c) 光把卡片改对不够——还得让 agent 走到方法卡。事故的另一半是路由：
    # 一道朴素的「转化漏斗」取数题只加载了行为域卡片，`analysis/funnel_analysis.md`
    # 从头到尾没被读过，于是 SOP 里的约束 A/B 一条都没生效。
    for rel, must in [("knowledge/domains/behavior/_index.md", "analysis/funnel_analysis.md"),
                      ("knowledge/domains/behavior/events.md", "analysis/funnel_analysis.md"),
                      ("knowledge/metrics/core_metrics.md", "analysis/funnel_analysis.md")]:
        ran += 1
        p = HERE.parent / rel
        if not p.exists():
            fails.append(f"{rel} 不存在，无法核漏斗路由")
        elif must not in p.read_text():
            fails.append(f"{rel} 没有指向 `{must}`：漏斗题会只读到表卡片、"
                         f"读不到方法卡，约束 A/B 全部失效")

    # 7d) 口径对了、单调性也对了之后还剩一件事：**绝对值**。这份数据集里漏斗顶端
    # 「看过就走」的那一侧没按真实比例生成——实测浏览过商品的 394 人里，一次都没
    # 加购的只有 80 人（20.3%），而 25 种事件各自的行数被抽得近似均匀
    # （736–866，极差比 1.18），于是顶宽底窄的量级差根本不存在。落到数字上，
    # 端到端转化全量 199/394 = 50.5%、近 30 天 32/197 = 16.2%，量级上不是业务水平。
    # 这一组和第 8d 组同一个病、不同的器官：数值全对、形态全对，**结论**仍可以错
    # ——agent 会把 50.5% 写成「转化表现优异」，也就是把生成器的抽样方式报成了
    # 业务表现。所以钉的是结论层，不是数值层。
    #
    # 注意这条与第 7 组（约束 A）的分工：单调性不成立**永远**是 SQL 的错，不许拿
    # 「这份数据绝对值偏高」去解释，两张卡片里都写明了这一点。
    for rel, musts in [
        ("knowledge/analysis/funnel_analysis.md", ["绝对值不可比", "50.5%"]),
        ("knowledge/metrics/core_metrics.md", ["绝对值不具参考性"]),
    ]:
        p = HERE.parent / rel
        for must in musts:
            ran += 1
            if not p.exists():
                fails.append(f"{rel} 不存在，无法核漏斗绝对值口径")
            elif must not in p.read_text():
                fails.append(f"{rel} 里找不到 `{must}`：漏斗题会读不到"
                             f"「转化率绝对值不具参考性」这条约束，"
                             f"50.5% 会被答成正面业务结论")

    # 8) 留存参考 SQL 的口径。跟第 7 组同一个病、不同的器官：`verify_doc_sql.py`
    # 只 EXPLAIN，而 `users` 上 `created_at` 和 `registered_at` **两列都存在**，
    # cohort 写错列 EXPLAIN 照样通过、结果照样出——只是分出来的是另一批人
    # （实测 500/500 行两列不相等）。另外分子若不 JOIN 回 cohort 名单，
    # 留存率会炸到 509%（见 `analysis/retention_curve.md` 的硬约束）。
    for md in sorted(KB.rglob("*.md")):
        rel = md.relative_to(KB.parent)
        for block in sql_blocks(md.read_text()):
            m_coh = re.search(r"(?i)cohort\s+AS\s*\(", block)
            m_act = re.search(r"(?i)activity\s+AS\s*\(", block)
            if not (m_coh and m_act):
                continue          # 不是 cohort 留存写法，这组不管
            ran += 1
            # 8a) cohort 时间列。cohort 段 = `cohort AS (` 到 `activity AS (` 之间
            coh_body = (block[m_coh.end():m_act.start()]
                        if m_act.start() > m_coh.end() else block)
            # 两条都要：光要求 registered_at "出现过"太松——把 SELECT 里的键换成
            # created_at、WHERE 里留着 registered_at，这条就照样绿（试过）。
            if "registered_at" not in coh_body:
                fails.append(f"{rel} 的留存参考 SQL 里 cohort 不是按 `registered_at` 分的："
                             f"`users` 上 `created_at` 也存在，写错了 EXPLAIN 照样过，"
                             f"分出来是另一批 cohort")
            if "created_at" in coh_body:
                fails.append(f"{rel} 的留存参考 SQL 的 cohort 段里出现了 `created_at`："
                             f"这份数据 500/500 行 `created_at != registered_at`，"
                             f"cohort 键必须整段走 `registered_at`")
            # 8b) 静态快照上 CURRENT_DATE 会查空
            if re.search(r"(?i)current_date", block):
                fails.append(f"{rel} 的留存参考 SQL 用了 `CURRENT_DATE`："
                             f"本库是静态快照，时间锚要用 "
                             f"`(SELECT max(as_of_date) FROM meta_snapshot)`")
            # 8c) 分子必须限定在 cohort 成员内
            m_join = re.search(r"(?i)join\s+activity\s+(\w+)\s+ON\s+([^\n]+)", block)
            m_from = re.search(r"(?i)from\s+cohort\s+(\w+)", block)
            if not (m_join and m_from):
                fails.append(f"{rel} 的留存参考 SQL 没把 activity 按 user_id 关联回 "
                             f"cohort 名单：分子会变成「全站活跃人数」，留存率超 100%")
                continue
            act_alias, coh_alias, on = m_join.group(1), m_from.group(1), m_join.group(2)
            if f"{act_alias}.user_id" not in on or f"{coh_alias}.user_id" not in on:
                fails.append(f"{rel} 的留存参考 SQL 的 JOIN 条件 `{on.strip()}` 没有按 "
                             f"user_id 把 activity 收进 cohort 名单")
            bad = {a for a in re.findall(r"(?i)then\s+(\w+)\.user_id", block)
                   if a != coh_alias}
            bad |= {a for a in re.findall(r"(?i)count\s*\(\s*distinct\s+(\w+)\.user_id", block)
                    if a != coh_alias}
            if bad:
                fails.append(f"{rel} 的留存参考 SQL 分子数的是 {sorted(bad)} 的 user_id，"
                             f"不是 cohort 成员 `{coh_alias}.user_id`："
                             f"实测这么写第 1 周是 219/43 = 509%")

    # 8d) 数字对了、结论仍可以是错的。这一组是 Q3 的窟窿，而它有**两个器官**，
    # 卡片里各写一段、这里各钉一组锚：
    #   - 形状：曲线平坦时（v1 种子样本 45.0/43.0/42.1/41.1/43.0）答「曲线平稳、
    #     留得住」，把项目自己的 P0 数据缺陷报成正面业务发现。卡片写的是**判据**
    #     （W4/W1 ≥ 0.8 算不衰减）而不是结论，因为全量批次的曲线已经衰减了。
    #   - 可比性：矩阵全对，却把 cohort 之间的噪声（满窗几周 D1 差 0.9pp）读成
    #     「10 月那批粘性最强」。这一条与形状无关，任何批次都成立。
    # 两组事实此前都只写在 `docs/`，agent 从不读。
    #
    # 不可比那一组钉的**形态**收窄过一次：原来只要求裸子串「cohort 之间不可比」出现过，
    # 而改写之后这句话在那张卡片里有 5 份副本（顶部小标题、结论段、可照抄的引用块、
    # chart 段、右删失段），于是 `kb-retention-verdict-gone` 那条负测——它按"锚点必须
    # 恰好出现一次"的规矩只改得动一处——注入完 must 依然满足，检查器 exit 0，用例把它
    # 报成**假阴性**。实际是判据把"这句话在不在"定义得太松：5 份副本里少掉 4 份还是绿的。
    # 现在逐个钉**位置不同、各自唯一**的三处：agent 最先读到的小标题、它照抄进
    # risk finding 的引用块、以及判分器那句"盯的就是它"。删掉任意一处都判红。
    for rel, musts in [
        ("knowledge/analysis/retention_curve.md",
         ["算不出留存", "W4/W1", "act.user_id = coh.user_id",
          "## ⚠️ cohort 之间不可比",
          "> cohort 之间不可比、不要排名：",
          '**risk finding 里必须有一句"cohort 之间不可比、不要排名"**']),
        ("knowledge/metrics/core_metrics.md",
         ["留存结论不可用", "W4/W1", "cohort 之间不可比",
          "analysis/retention_curve.md"]),
        ("knowledge/domains/behavior/_index.md", ["analysis/retention_curve.md"]),
    ]:
        p = HERE.parent / rel
        for must in musts:
            ran += 1
            if not p.exists():
                fails.append(f"{rel} 不存在，无法核留存口径/路由")
            elif must not in p.read_text():
                fails.append(f"{rel} 里找不到 `{must}`：留存题会读不到"
                             f"「曲线不衰减时算不出留存」或「cohort 之间不可比」"
                             f"这两条约束之一——平坦曲线会被答成「留得住」，"
                             f"几周之间不到 1pp 的差会被答成「哪批留得最好」")

    # 8e) 上面那一路的**前提**：agent 在常规模式下真的会去读那份 `analysis/` 文档。
    #
    # 这是 L5-retention-cohort 那道红的另一半原因。域索引里写的是「两个都要读，
    # **哪怕只是取数**」，而 `LITE_SUFFIX`（普通题的系统提示后缀）原文写着
    # 「**不要**读 analysis/ 方法库」——两句话直接对冲，而系统提示赢。于是常规模式下
    # 那份文档从来没被打开过：留存题的分子约束（写错出 509%）和曲线可信度判据
    # 一起消失，而 eval 又按"该说的话没说"判它红。deep 模式只有 4 个预设按钮能进，
    # 用户手打的问题一律走 lite，所以这不只是 eval 的保真度问题。
    #
    # 判据用同一句话做锚：知识库标了「哪怕只是取数」，提示词里就必须出现同一句，
    # 并且是围绕 `analysis/` 说的。这样"重新写成一刀切"会红，而正常改写不会。
    LITE_ANCHOR = "哪怕只是取数"
    forced = sorted(md.relative_to(KB.parent) for md in KB.rglob("*.md")
                    if LITE_ANCHOR in md.read_text())
    ran += 1
    ap = HERE.parent / "backend" / "agent.py"
    m = re.search(r"LITE_SUFFIX\s*=\s*\"\"\"(.*?)\"\"\"", ap.read_text(), re.S)
    if m is None:
        fails.append("backend/agent.py 里找不到 LITE_SUFFIX：常规模式的提示后缀"
                     "改名或删了，这条路由检查已经瞎了")
    elif forced:
        lite = m.group(1)
        ran += 1
        if LITE_ANCHOR not in lite:
            fails.append(
                f"LITE_SUFFIX 里没有「{LITE_ANCHOR}」这个例外，而 "
                f"{', '.join(str(f) for f in forced)} 里写着这句话要求"
                f"「哪怕只是取数也要读 analysis/」：两边对冲，系统提示赢，"
                f"常规模式下那份口径硬约束/可信度判据永远读不到")
        elif "analysis/" not in lite:
            fails.append("LITE_SUFFIX 里的例外没有点名 `analysis/`："
                         "写得太泛，agent 不知道例外指的是哪一类文档")

    # 9) 留存判分器的结论闸。这一组和第 1–5 组对称,但盯的维度相反:
    # 漏斗那次错在**数值**(结算 > 浏览),留存这次数值**全对**、分子也对、
    # 右删失说明也对,错的只有结论那几个字「曲线平稳、留得住,10 月那批粘性最强」。
    # 纯数值判分会判 PASS。
    #
    # 夹具是**两条曲线**，不是一条：形状那条闸只在"曲线不衰减"时生效，而当前这批数据
    # 的曲线是衰减的（见 judge_retention 的 docstring）。两个分支都得有固定夹具，
    # 否则换一批数据就等于悄悄少了一道闸、或者反过来开始要求 agent 说假话。
    # 不可比那条闸与形状无关，两条曲线上都必须生效。
    r_case = {"judge": {"mode": "retention", "tolerance_pct": 5.0, "min_hit": 3}}
    # 平坦（v1 种子样本实测量级）：末周/首周 ≈ 0.95，判为不衰减 → 结论闸生效
    r_goldens = [{"label": "matrix counts", "rows": [[41, 18, 19, 19, 17],
                                                    [37, 18, 16, 18, 14]]},
                 {"label": "matrix pct", "rows": [[43.9, 46.3, 46.3, 41.5],
                                                  [48.6, 43.2, 48.6, 37.8]]}]
    # 衰减（全量批次实测量级）：末周/首周 ≈ 0.52 → 结论闸不适用
    d_goldens = [{"label": "matrix counts", "rows": [[41, 18, 19, 19, 17],
                                                    [37, 18, 16, 18, 14]]},
                 {"label": "matrix pct", "rows": [[71.6, 51.9, 42.3, 37.2],
                                                  [70.0, 50.1, 41.9, 32.9]]}]

    def r_ev(prose: str, nums=(41, 18, 19, 19, 17, 37, 18, 16, 18, 14)):
        return {"result": {"interpreted": "按注册周分 cohort", "insight": prose,
                           "kpis": [], "followups": []},
                "rowsets": [{"columns": [], "rows": [list(nums)]}]}

    # 9a) 事故本体（**横向**那条闸）:数字全对,结论把 cohort 之间的噪声报成了业务发现。
    # 故意放在**衰减**曲线上：纵向那条闸此时不参与,所以这一条测到的只可能是横向那条。
    BAD = ("留存矩阵已出:11 月那批 D1 59.5%、12 月末那批 58.6%,**越老的 cohort 粘性越强**,"
           "建议复盘 11 月的拉新渠道。⚠️ 右下角那几个 0 是观测窗未到(右删失),别当真实下跌。")
    ok, why = judge_retention(r_case, d_goldens, r_ev(BAD), defaults)
    check("数字对但把 cohort 差异读成业务结论应判错", ok, False)
    if "cohort 之间不可比" not in why:
        fails.append(f"该失败的理由应指向结论而不是数值，实际: {why}")

    # 9b) 修复后的真实答案 → 必须判对。注意它里面写着『别当成"留存好"的正面结论』,
    # 所以任何以「留存好」为特征的**黑名单**写法都会把正确答案误杀 —— 这就是这两道闸
    # 只用白名单(要求某句话存在)、不用黑名单(禁止某句话出现)的原因。
    GOOD = ("曲线单调衰减、断崖在 D1(D1 59.4% → D7 22.3% → D14 14.7%)。但满窗那几周之间"
            "D1 只差 0.9pp、D7 只差 2.0pp:所有 cohort 出自同一个活跃度模型,"
            "cohort 之间不可比、不要排名,别当成某批用户\"留存好\"的正面结论。"
            "右下三角的 0 是右删失。")
    ok, why = judge_retention(r_case, d_goldens, r_ev(GOOD), defaults)
    check("声明了 cohort 不可比且数值命中应判对", ok, True)

    # 9b2) 真 agent 的说法和白名单里的字**不会逐字相同**。这三条是 2026-09-01 实测
    # 那次的原话与它的近邻：第一条当时被判红了（白名单里躺着"不能/不要/别/不做排名"
    # 四个，agent 写的是「不建议排名」），闸把一份完全正确的答案拦下来了。
    # 现在走否定+动作的模式匹配，这三条都必须通过。
    for phr in ("各 cohort 之间差异很小（满窗 W1 仅差 2.9pp），这份样本里"
                "\"哪批用户留得更好\"没有可读答案，不建议排名。",
                "满窗那几周之间差不到 1pp，无需对 cohort 做横向排名。",
                "cohort 之间没有必要排序，差值在噪声量级。"):
        ok, why = judge_retention(r_case, d_goldens, r_ev(phr), defaults)
        check(f"真 agent 说法应判对: {phr[:14]}…", ok, True)

    # 9c) 只给右删失说明**不算**声明数据限制:那句话本身正确,但它只解释边缘的 0,
    # 不解释满窗那几周之间为什么不能排名 —— 而且正是它让那段错结论听起来很严谨。
    # 这条 fixture 同时是白名单的**选词考题**:它自己就在谈可比性("不能和老 cohort 比"),
    # 所以白名单里放光秃秃的"不可比"会被它满足,闸就空转了。
    ONLY_CENSOR = ("留存矩阵已出。右下角那几个 0 是观测窗未到(右删失),不是真实下跌,"
                   "近期 cohort 的留存窗口不完整,不能和老 cohort 比。"
                   "从满窗那几周看,11 月那批留得最好。")
    ok, why = judge_retention(r_case, d_goldens, r_ev(ONLY_CENSOR), defaults)
    check("只说右删失不应算作声明了数据限制", ok, False)

    # 9d) 结论闸不能越权:结论合格但数值不对时,理由该是数值不命中(同第 3 条的纪律)
    ok, why = judge_retention(r_case, d_goldens, r_ev(GOOD, nums=(1, 2, 3)), defaults)
    check("结论合格但数值错应判错", ok, False)
    if "cohort 之间不可比" in why or "算不出留存" in why:
        fails.append(f"数值不命中不该被报成结论问题: {why}")

    # 9e) **纵向**那条闸:曲线平坦时,横向那句声明齐了也不够 —— 两条闸互相不能替代。
    # 这一对是它的两面:同一段散文,加上"算不出留存"的意思才放行。
    ok, why = judge_retention(r_case, r_goldens, r_ev(GOOD), defaults)
    check("平坦曲线上光有「cohort 不可比」不够", ok, False)
    if "算不出留存" not in why:
        fails.append(f"平坦曲线上该失败的理由应指向曲线可信度，实际: {why}")
    GOOD_FLAT = GOOD + ("另外这一批的曲线整条是平的(W1≈W4),活跃度与注册生命周期"
                        "是独立抽样的,这一批数据算不出留存。")
    ok, why = judge_retention(r_case, r_goldens, r_ev(GOOD_FLAT), defaults)
    check("平坦曲线上两条声明都齐了应判对", ok, True)

    # 9f) 纵向那条闸的另一个分支:曲线**确实在衰减**时它必须让路。写死"必须说算不出留存"
    # 的那一版在数据换成全量批次后开始要求 agent 说假话——这一批是算得出留存的
    # （71.6 → 37.2，前陡后平）。判据钉在数据的性质上而数据会换，就必然有这种反转。
    # 注意这段散文里横向那句仍然写着("不做横向排名")：让路的只有纵向那一条。
    PLAIN = ("满窗的 4 个 cohort，W1 约 71.6% 一路降到 W4 的 37.2%，前陡后平，"
             "是正常的留存衰减形态。几周之间的差在噪声量级，不做横向排名。"
             "末周那几个 0 是右删失。")
    ok, why = judge_retention(r_case, d_goldens, r_ev(PLAIN), defaults)
    check("曲线在衰减时不该再要求声明「算不出留存」", ok, True)
    if "衰减" not in why:
        fails.append(f"判对的理由里应写明是按曲线形状放行的，实际: {why}")
    # 同一段散文放到平坦曲线上必须被拦下——证明放行确实来自形状,不是白名单变松了
    ok, _ = judge_retention(r_case, r_goldens, r_ev(PLAIN), defaults)
    check("同一段散文在平坦曲线上仍应判错", ok, False)
    # 9g) 量不出形状(金标里没有 pct 那份)要**从严**:判据瞎了不能静默放行
    ok, _ = judge_retention(r_case, [d_goldens[0]], r_ev(PLAIN), defaults)
    check("量不出曲线形状时应回到最严口径", ok, False)

    # 9h) 金标自己的口径(同第 6 组对金标做的事):留存金标必须按 registered_at 分 cohort、
    # 且分子 JOIN 回 cohort 名单。真源错了下游全错。
    rc = next((c for c in spec["cases"] if c["id"] == "L5-retention-cohort"), None)
    ran += 1
    if rc is None:
        fails.append("cases.json 里找不到 L5-retention-cohort：留存题端到端零覆盖")
    else:
        if rc["judge"]["mode"] != "retention":
            fails.append(f"L5-retention-cohort 的判分 mode 是 {rc['judge']['mode']}，"
                         f"不是 retention：结论闸不生效，答成「留得住」也会 PASS")
        # 形状闸靠 label 里带 pct 的那份金标量曲线；那份被删掉/改名，闸不会报错，
        # 只会静默退回"最严口径"，于是数据换成衰减的那一批之后开始要求 agent 说假话。
        ran += 1
        if not any("pct" in g["label"].lower() for g in rc["golden"]):
            fails.append("L5-retention-cohort 的金标里没有 label 带 `pct` 的那一份："
                         "judge_retention 量不出曲线形状，结论闸会退回最严口径")
        for g in rc["golden"]:
            ran += 1
            if "registered_at" not in g["sql"] or "created_at" in g["sql"]:
                fails.append(f"留存金标 {g['label']} 的 cohort 列不是 `registered_at`")
            if not re.search(r"(?i)join\s+activity\s+(\w+)\s+ON\s+\1\.user_id\s*=\s*\w+\.user_id",
                             g["sql"]):
                fails.append(f"留存金标 {g['label']} 没把 activity 按 user_id 关联回 "
                             f"cohort 名单：分子会变成全站活跃，留存率超 100%")

    # 10) 第二份用例集 cases_traps.json 的结构与"陷阱"这件事本身。
    #
    # 这一组的由来是：这个文件写完之后**全库没有一个引用**，谁也不会发现它坏了。
    # 现在 `--cases cases_traps.json` 是它的入口，这里是它的离线闸——文件被改坏
    # （judge mode 打错、金标里混进了那张陷阱表）在 L0 就红，不用等一次带模型的跑。
    ran += 1
    if not TRAPS_PATH.is_file():
        fails.append(f"{TRAPS_PATH.name} 不存在：`--cases {TRAPS_PATH.name}` 这条入口是空的")
    else:
        t_spec = json.loads(TRAPS_PATH.read_text())
        ran += 1
        if "defaults" not in t_spec or not t_spec.get("cases"):
            fails.append(f"{TRAPS_PATH.name} 与 cases.json 不同构（缺 defaults / cases）")
        t_ids = [c.get("id") for c in t_spec.get("cases", [])]
        ran += 1
        if len(set(t_ids)) != len(t_ids):
            fails.append(f"{TRAPS_PATH.name} 里有重复的 case id：{t_ids}")
        # 陷阱题的全部价值在于「金标走对的那张表，而错答会走另一张」。所以金标 SQL 里
        # **不许**出现被点名的陷阱表——一旦出现，这题就变成了在考陷阱本身。
        TRAP_TABLES = ("dwd_orders_valid", "tmp_campaign_roi_analysis")
        for c in t_spec.get("cases", []):
            ran += 1
            if c.get("judge", {}).get("mode") not in JUDGES:
                fails.append(f"{c.get('id')} 的判分 mode {c.get('judge', {}).get('mode')!r} "
                             f"不在 JUDGES 里，跑起来会直接 KeyError")
            ran += 1
            if not str(c.get("value_hint", "")).strip():
                fails.append(f"{c.get('id')} 没有 value_hint：读报告的人看不出这题的陷阱是什么")
            for g in c.get("golden", []):
                ran += 1
                bad = [t for t in TRAP_TABLES if t in g.get("sql", "")]
                if bad:
                    fails.append(f"{c.get('id')} 的金标 {g.get('label')} 直接查了陷阱表 "
                                 f"{bad}：这题就不再是陷阱题了")

    for f in fails:
        print("  ✗", f)
    print(f"判分器自测: {ran} 条断言 + 金标口径核对，"
          f"{'全部通过 ✅' if not fails else f'{len(fails)} 项失败 ❌'}")
    return 1 if fails else 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="analytics agent eval harness")
    ap.add_argument("--level", type=int, nargs="*", help="只跑这些 level")
    ap.add_argument("--case", nargs="*", help="只跑这些 case id")
    ap.add_argument("--cases", default=str(CASES_PATH),
                    help=f"用例文件（默认 {CASES_PATH.name}；口径陷阱题用 {TRAPS_PATH.name}）")
    ap.add_argument("--dry-run", action="store_true", help="只验证金标 SQL 可执行")
    ap.add_argument("--tag", default=None,
                    help="报告文件名后缀。默认按 DB_BACKEND 取（athena 不加后缀，"
                         "保持基线路径不变），显式给了就用给的")
    ap.add_argument("--selftest", action="store_true",
                    help="离线自测判分器（不连库不调模型）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    cases_path = Path(args.cases)
    if not cases_path.is_absolute():
        cases_path = (HERE / cases_path) if not cases_path.exists() else cases_path.resolve()
    if not cases_path.is_file():
        print(f"用例文件不存在：{cases_path}"); return 2
    # 非默认用例集的报告单独命名。共用 report.md 的话，跑一次 3 题的陷阱集就会把
    # 27 题的报告盖掉，而两份文件都"存在且看着正常"——同 report_dryrun_json 那条教训。
    case_tag = ("" if cases_path.name == CASES_PATH.name
                else cases_path.stem.replace("cases_", ""))

    spec = json.loads(cases_path.read_text())
    cases = spec["cases"]
    if args.level:
        cases = [c for c in cases if c["level"] in args.level]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        print("没有匹配的用例"); return 2

    # athena 是基线，路径不变；另两条 arm 各自成文件。
    # 两个维度都要进文件名：**arm** 和**用例集**。少了任何一个都会有一对跑法共用
    # 同一个路径而互相覆盖（duckdb 全量 vs. athena 全量；athena 全量 vs. 陷阱集）。
    arm_tag = args.tag if args.tag is not None else (
        "" if db.BACKEND == "athena" else db.BACKEND)
    tag = ".".join(x for x in (arm_tag, case_tag) if x)

    if not db.ping():
        print("数据库不可达（先跑 scripts/localpg/up.sh + load.sh）"); return 2

    records = []
    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c['id']} … ", end="", flush=True)
        rec = await run_case(c, spec["defaults"], args.dry_run)
        print(rec["status"], f"({rec.get('elapsed_s','-')}s)")
        records.append(rec)

    done = [r for r in records if r["status"] in ("pass", "fail")]
    meta = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": os.environ.get("ANTHROPIC_MODEL", "(default)"),
        "cases": cases_path.name,
        # 哪条 arm 是这份报告最重要的一条元信息：三条 arm 的题目、金标、判分器
        # 完全相同，唯一的变量就是它，不记下来这份报告就没法归属。
        "arm": db.BACKEND,
        "avg_s": round(sum(r.get("elapsed_s", 0) for r in done) / len(done), 1) if done else "-",
        "avg_docs": round(sum(r.get("n_docs", 0) for r in done) / len(done), 1) if done else "-",
        "avg_sql": round(sum(r.get("n_sql", 0) for r in done) / len(done), 1) if done else "-",
    }
    out_json = report_dryrun_json(tag) if args.dry_run else report_json(tag)
    out_json.write_text(json.dumps({"meta": meta, "records": records},
                                   ensure_ascii=False, indent=2, default=str))
    if not args.dry_run:
        md = report_md(tag)
        md.write_text(render_report(records, meta))
        print(f"\n通过 {sum(1 for r in done if r['status']=='pass')}/{len(done)}"
              f" · arm={meta['arm']} · 报告: {md.relative_to(HERE.parent)}"
              f" / {out_json.name}")
    else:
        bad = [r for r in records if r["status"] == "golden_error"]
        print(f"\n金标验证: {len(records)-len(bad)}/{len(records)} OK"
              f" · arm={meta['arm']}"
              + (f"；失败: {[r['id'] for r in bad]}" if bad else ""))
        print(f"（只验金标，未调模型；产物 {out_json.name}，未动 report.md/report.json）")
    return 0 if all(r["status"] in ("pass", "golden_ok") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
