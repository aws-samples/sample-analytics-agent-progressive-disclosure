"""Eval harness：自动评测 agent 的 text-to-SQL 准确率。

用法（backend venv，本地库已 load）：
    cd eval && ../backend/.venv/bin/python run_eval.py                 # 全量
    ../backend/.venv/bin/python run_eval.py --level 1 2                # 只跑 L1/L2
    ../backend/.venv/bin/python run_eval.py --case L1-users-count      # 只跑单题
    ../backend/.venv/bin/python run_eval.py --dry-run                  # 只验金标 SQL，不调模型

原理：
  1. 每题的金标口径写成 1..N 条「可接受的」golden SQL（cases.json），运行时经与
     agent 相同的只读边界（backend/db.py）现算出期望值——数据重新生成后无需改用例。
  2. 驱动 backend/agent.py 的 run_agent() 跑真实 agent（读文档→写 SQL→present_result），
     从事件流里抓 agent 的 SQL 结果（rows 事件）与最终交付（result 事件的 kpis/chart）。
  3. 按 judge.mode 比对：scalar（数值容差）/ set（键值集合）/ toplist（前K命中）/
     pair、funnel（多数值逐个容差）。命中任一 golden 变体即判对。
  4. 产出 report.md（逐题明细 + 汇总）与 report.json（原始数据，供横向对比）。

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
REPORT_MD = HERE / "report.md"
REPORT_JSON = HERE / "report.json"


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

async def compute_goldens(case: dict) -> list[dict]:
    """执行该题全部 golden SQL，返回 [{label, columns, rows}]。"""
    out = []
    for g in case["golden"]:
        res = await db.run_query(g["sql"])
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
    n_docs = 0
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
            n_docs += 1
        elif t == "result":
            result = ev
        elif t == "error":
            errors.append(ev.get("message", ""))
    return {
        "sqls": ev_sql,
        "rowsets": ev_rows,
        "result": result,
        "errors": errors,
        "n_docs": n_docs,
        "n_sql": len(ev_sql),
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }


def agent_numbers(evidence: dict) -> list[float]:
    """agent 给出的所有数值证据：查询结果 + kpis。判分时在这里找期望值。"""
    nums: list[float] = []
    for rs in evidence["rowsets"]:
        for v in _flat_values(rs["rows"]):
            if isinstance(v, (int, float)):
                nums.append(float(v))
            else:
                nums.extend(_numbers_from(v))
    for k in (evidence["result"].get("kpis") or []):
        nums.extend(_numbers_from(k.get("value", "")))
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


def judge_funnel(case, goldens, evidence, defaults) -> tuple[bool, str]:
    tol = case["judge"].get("tolerance_pct", defaults["tolerance_pct"])
    nums = agent_numbers(evidence)
    for g in goldens:
        vals = [float(v) for v in _flat_values(g["rows"]) if isinstance(v, (int, float))]
        if not vals:
            continue
        hit = sum(1 for v in vals if any(_close(n, v, tol) for n in nums))
        if hit >= max(1, len(vals) - 1):        # 允许漏 1 步
            return True, f"命中 golden[{g['label']}] {hit}/{len(vals)} 步"
    return False, "漏斗步骤数值命中不足"


JUDGES = {"scalar": judge_scalar, "set": judge_set, "toplist": judge_toplist,
          "pair": judge_pair, "funnel": judge_funnel}


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
    rec.update(elapsed_s=evidence["elapsed_s"], n_docs=evidence["n_docs"],
               n_sql=evidence["n_sql"], agent_sqls=evidence["sqls"],
               agent_errors=evidence["errors"],
               has_result=bool(evidence["result"]))
    if evidence["errors"] and not evidence["rowsets"] and not evidence["result"]:
        rec.update(status="agent_error", detail="; ".join(evidence["errors"])[:300])
        return rec

    ok, detail = JUDGES[case["judge"]["mode"]](case, goldens, evidence, defaults)
    rec.update(status="pass" if ok else "fail", detail=detail)
    return rec


def render_report(records: list[dict], meta: dict) -> str:
    done = [r for r in records if r["status"] in ("pass", "fail")]
    npass = sum(1 for r in done if r["status"] == "pass")
    lines = ["# Eval Report", "",
             f"- 运行时间: {meta['ts']}  · 模型: {meta['model']}",
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
                      f"- {r.get('detail','')}",
                      "- agent SQL:", "```sql",
                      *(r.get("agent_sqls") or ["(无)"]), "```", ""]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description="analytics agent eval harness")
    ap.add_argument("--level", type=int, nargs="*", help="只跑这些 level")
    ap.add_argument("--case", nargs="*", help="只跑这些 case id")
    ap.add_argument("--dry-run", action="store_true", help="只验证金标 SQL 可执行")
    args = ap.parse_args()

    spec = json.loads(CASES_PATH.read_text())
    cases = spec["cases"]
    if args.level:
        cases = [c for c in cases if c["level"] in args.level]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        print("没有匹配的用例"); return 2

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
        "avg_s": round(sum(r.get("elapsed_s", 0) for r in done) / len(done), 1) if done else "-",
        "avg_docs": round(sum(r.get("n_docs", 0) for r in done) / len(done), 1) if done else "-",
        "avg_sql": round(sum(r.get("n_sql", 0) for r in done) / len(done), 1) if done else "-",
    }
    REPORT_JSON.write_text(json.dumps({"meta": meta, "records": records},
                                      ensure_ascii=False, indent=2, default=str))
    if not args.dry_run:
        REPORT_MD.write_text(render_report(records, meta))
        print(f"\n通过 {sum(1 for r in done if r['status']=='pass')}/{len(done)}"
              f" · 报告: {REPORT_MD.relative_to(HERE.parent)} / report.json")
    else:
        bad = [r for r in records if r["status"] == "golden_error"]
        print(f"\n金标验证: {len(records)-len(bad)}/{len(records)} OK"
              + (f"；失败: {[r['id'] for r in bad]}" if bad else ""))
    return 0 if all(r["status"] in ("pass", "golden_ok") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
