#!/usr/bin/env python3
"""横向比 27 道题的金标**取值**，三条 arm 之间。不连引擎，只读 dry-run 产物。

## 这一层补的是什么

`run_eval.py --dry-run` 回答的是「金标 SQL 在这条 arm 上跑得通吗」。跑得通不等于
算得对：同一条 SQL 在三种方言上都能执行、而其中一条少算一截，dry-run 是全绿的。
实测就出过这个：Redshift 那条 arm 上 11 道题报 `relation does not exist`，缺的是
整个集市层（`mart_*` / `fin_*` / `meta_snapshot` 共 13 张表，DDL 写好了但从没执行过）。
补完之后 27/27 通过——而「通过」这两个字里不含任何关于数值的信息。

所以这里比的是取值本身。

## 三档判定，不是一档

第一版只有「相等 / 不相等」，在真实数据上把 5 处差异全标成失败，其中 4 处根本不是
缺陷。混在一起报的后果是这个脚本从第一天起就是红的，红久了就没人看了。所以分三档：

- **`value`（闸门，会让退出码非 0）**——超出该题判分容差的取值差异。这是缺陷。
- **`dialect`（发现，不失败）**——差异存在但落在该题的 `tolerance_pct` 之内。实测
  例子：`sum(gmv_attributed)/sum(cost)` 在两条 decimal(14,2) 上，Trino 给 68.31，
  Redshift 给 68.3051——Trino 把 decimal 除法的标度截到固定位数，Redshift 保留更多。
  相对差 0.0075%，判分容差 0.5%，两边都判对。这是要写进对比结论的方言差异，
  不是要修的 bug。
- **`order`（发现，不失败）**——只有行序不同，多重集完全一致。金标 SQL 没写
  `ORDER BY` 时行序**不由查询定义**，任何顺序都对，这三题的 `judge.mode` 也确实是
  `set`。所以按排序后的多重集比，行序差异单独记一笔。

## 三件容易把红的判成绿的事

1. **同样报错不算一致。** 两条 arm 都失败、错误文本恰好相同时，朴素的相等比较会
   打印「一致 ✅」。写这个脚本的前身时踩过：`db.run_query` 是 async，两条 arm 都
   拿到 `'coroutine' object is not subscriptable`，字符串相等，报了 7/7 全绿——那个
   绿色什么都没量。这里凡有一条 arm 缺记录或状态非 `golden_ok`，直接算失败。

2. **类型还原的末位差要吸收，口径差异不能吸收。** `FLOAT_RTOL` 取 1e-6：够吸收
   Data API 把 DECIMAL 经字符串还原成 float 的末位差，不够吸收任何真实口径差异
   （口径差异的量级是百分之几，不是百万分之一）。落在 1e-6 与判分容差之间的，
   进 `dialect` 档而不是被静默放过——差异要看得见。

3. **只有一条 arm 的产物在场时，不能报成功。** 少一条 arm 的横向比较不是横向比较。
   默认要求至少两份产物，`--arms` 显式点名时按点名的那几条要求。

用法：

    python3 eval/compare_goldens.py                       # 自动发现所有 report.dryrun.*.json
    python3 eval/compare_goldens.py --arms athena redshift
    python3 eval/compare_goldens.py --strict              # dialect/order 也算失败
    python3 eval/compare_goldens.py --selftest            # 判定逻辑自测，不读产物
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 浮点相对容差，吸收类型还原的末位差。见 docstring 第 2 点。
FLOAT_RTOL = 1e-6

#: 题目没给 tolerance_pct 时的兜底，与 cases.json 的 defaults 一致。
#: 真值从 cases.json 读，这里只是读不到时的下限。
FALLBACK_TOL_PCT = 0.5

#: 会让退出码非 0 的档。另两档是「发现」，要报出来但不算失败。
GATE_KINDS = {"unavailable", "shape", "value"}


def dryrun_path(arm: str) -> Path:
    """athena 那条 arm 的产物历史上不带后缀，两种写法都认。"""
    tagged = HERE / f"report.dryrun.{arm}.json"
    if tagged.exists():
        return tagged
    return HERE / "report.dryrun.json"


def discover() -> list[str]:
    """自动发现产物。带后缀的按后缀取 arm；不带后缀的那份要读 meta.arm 才知道
    是谁——文件名里没有这个信息，猜成 athena 会把一份 duckdb 产物归错。"""
    found: list[str] = []
    for p in sorted(HERE.glob("report.dryrun.*.json")):
        found.append(p.name.split(".")[2])
    plain = HERE / "report.dryrun.json"
    if plain.exists():
        arm = json.loads(plain.read_text(encoding="utf-8")).get("meta", {}).get("arm")
        if arm and arm not in found:
            found.append(arm)
    return found


def load(arm: str) -> tuple[dict, dict]:
    """返回 (meta, {case_id: record})。"""
    p = dryrun_path(arm)
    if not p.exists():
        raise FileNotFoundError(f"{arm} 没有 dry-run 产物：{p.name}\n"
                                f"  先跑 DB_BACKEND={arm} python3 eval/run_eval.py --dry-run")
    d = json.loads(p.read_text(encoding="utf-8"))
    # meta.arm 是这份产物的归属声明。文件名对不上 meta 时以 meta 为准并明说，
    # 否则一份被手工改过名的产物会被归到错的 arm 上，而结论看不出问题。
    got = d.get("meta", {}).get("arm")
    if got and got != arm:
        print(f"⚠️  {p.name} 的 meta.arm 是 {got!r}，不是文件名暗示的 {arm!r}。"
              f"按 meta 归属。")
    return d.get("meta", {}), {r["id"]: r for r in d.get("records", [])}


def load_cases() -> tuple[dict, float]:
    """返回 ({case_id: case}, 默认容差)。判定要用题目自己的容差和金标 SQL。"""
    p = HERE / "cases.json"
    if not p.exists():
        return {}, FALLBACK_TOL_PCT
    d = json.loads(p.read_text(encoding="utf-8"))
    cases = d["cases"] if isinstance(d, dict) else d
    dflt = (d.get("defaults", {}) if isinstance(d, dict) else {}).get(
        "tolerance_pct", FALLBACK_TOL_PCT)
    return {c["id"]: c for c in cases}, dflt


def tol_pct_of(case: dict | None, dflt: float) -> float:
    if not case:
        return dflt
    return case.get("judge", {}).get("tolerance_pct", dflt)


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def sql_defines_order(sql: str | None) -> bool:
    """这条金标 SQL 是否定义了行序。

    宽松地判：只要出现 `ORDER BY` 就算定义了。窗口函数里的 `ORDER BY` 会让它误判成
    「有序」，而误判方向是**更严格**（该题按行序比），宁可多报一处行序差异让人看，
    也不要把一处真实的行序缺陷按多重集比掉。
    """
    return bool(sql) and bool(_ORDER_BY.search(sql))


def eq(a, b, rtol: float = FLOAT_RTOL) -> bool:
    """逐位相等，浮点按相对容差。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        scale = max(abs(a), abs(b))
        return scale > 0 and abs(a - b) <= rtol * scale
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(eq(x, y, rtol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(eq(a[k], b[k], rtol) for k in a)
    return a == b


def sort_key(row):
    """按类型分组再排，避免 int 与 str 混列时 sorted() 抛 TypeError。
    NULL 单独一组：`None` 跟任何东西都不可比。"""
    if isinstance(row, list):
        return tuple(sort_key(v) for v in row)
    if row is None:
        return (0, "")
    if isinstance(row, bool):
        return (1, int(row))
    if isinstance(row, (int, float)):
        return (2, float(row))
    return (3, str(row))


def as_multiset(rows):
    return sorted(rows, key=sort_key) if isinstance(rows, list) else rows


def goldens_of(rec: dict) -> list:
    """把一条记录压成 [(label, rows), ...]，只留参与比较的部分。"""
    return [(g.get("label", ""), g.get("rows")) for g in rec.get("goldens", [])]


def golden_sql(case: dict | None, i: int, label: str) -> str | None:
    """取第 i 条金标的 SQL。先按 label 找，找不到再按下标——label 是稳定的标识，
    下标在题目被编辑过之后会指错条目。"""
    if not case:
        return None
    gs = case.get("golden", [])
    for g in gs:
        if g.get("label") == label:
            return g.get("sql")
    return gs[i].get("sql") if i < len(gs) else None


def classify(vals: dict, arms: list[str], ordered: bool, tol_pct: float) -> str | None:
    """返回 None（一致）或档位名。三档的含义见模块 docstring。"""
    base = arms[0]
    if all(eq(vals[a], vals[base]) for a in arms):
        return None
    # 行序：SQL 没定义顺序时，多重集一致就是一致，行序差异单独记一笔
    if not ordered and all(eq(as_multiset(vals[a]), as_multiset(vals[base]))
                           for a in arms):
        return "order"
    # 落在判分容差内的数值差异 = 方言差异，是发现不是缺陷。
    # 行序也要一并放开：一条 arm 既换了序又差了末位时，按序比会永远判成 value。
    rtol = tol_pct / 100.0
    cmp_vals = ({a: vals[a] for a in arms} if ordered
                else {a: as_multiset(vals[a]) for a in arms})
    if all(eq(cmp_vals[a], cmp_vals[base], rtol) for a in arms):
        return "dialect"
    return "value"


def compare(arms: list[str], data: dict, cases: dict | None = None,
            dflt_tol: float = FALLBACK_TOL_PCT) -> list[dict]:
    """返回差异项列表（含三档）。空列表即完全一致。"""
    cases = cases or {}
    ids: list[str] = []
    for a in arms:                       # 保持第一条 arm 的题目顺序
        for cid in data[a]:
            if cid not in ids:
                ids.append(cid)
    out = []
    base = arms[0]
    for cid in ids:
        case = cases.get(cid)
        tol = tol_pct_of(case, dflt_tol)
        # 缺记录 / 状态不对 —— 直接失败，不参与「一致」判定（docstring 第 1 点）
        missing = [a for a in arms if cid not in data[a]]
        errored = [a for a in arms
                   if cid in data[a] and data[a][cid].get("status") != "golden_ok"]
        if missing or errored:
            out.append({"id": cid, "kind": "unavailable",
                        "missing": missing, "errored": errored,
                        "detail": {a: (data[a][cid].get("detail")
                                       or data[a][cid].get("status"))
                                   for a in errored}})
            continue
        gs = {a: goldens_of(data[a][cid]) for a in arms}
        if any([l for l, _ in gs[a]] != [l for l, _ in gs[base]] for a in arms):
            out.append({"id": cid, "kind": "shape",
                        "detail": {a: [l for l, _ in gs[a]] for a in arms}})
            continue
        for i, (label, _) in enumerate(gs[base]):
            vals = {a: gs[a][i][1] for a in arms}
            ordered = sql_defines_order(golden_sql(case, i, label))
            kind = classify(vals, arms, ordered, tol)
            if kind:
                out.append({"id": cid, "kind": kind, "label": label,
                            "tol_pct": tol, "ordered": ordered,
                            "detail": {a: vals[a] for a in arms}})
    return out


_HEADING = {
    "unavailable": "取不到金标",
    "shape":       "金标条目不同构",
    "value":       "取值差异超出判分容差",
    "dialect":     "方言差异（判分容差内，不影响判分）",
    "order":       "仅行序不同（金标 SQL 未定义顺序，多重集一致）",
}


def render(arms: list[str], data: dict, diffs: list[dict], strict: bool) -> None:
    n = len({cid for a in arms for cid in data[a]})
    print(f"金标取值横向对账：{' / '.join(arms)}  ({n} 道题)\n")
    gates = GATE_KINDS | ({"dialect", "order"} if strict else set())
    for kind in ("unavailable", "shape", "value", "dialect", "order"):
        items = [d for d in diffs if d["kind"] == kind]
        if not items:
            continue
        mark = "❌" if kind in gates else "·"
        print(f"{mark} {_HEADING[kind]}（{len(items)} 处）")
        for b in items:
            if kind == "unavailable":
                who = ", ".join(b["missing"] + b["errored"])
                print(f"    {b['id']:26} 缺/错：{who}")
                for a, d in b["detail"].items():
                    print(f"      {a}: {str(d)[:160]}")
            elif kind == "shape":
                print(f"    {b['id']:26}")
                for a, ls in b["detail"].items():
                    print(f"      {a:9}: {ls}")
            else:
                extra = ("" if kind == "order"
                         else f"  容差 {b['tol_pct']}%")
                print(f"    {b['id']:26} [{b['label'][:44]}]{extra}")
                for a, v in b["detail"].items():
                    s = json.dumps(v, ensure_ascii=False, default=str)
                    print(f"      {a:9}: {s[:190]}{'…' if len(s) > 190 else ''}")
        print()
    failed = {d["id"] for d in diffs if d["kind"] in gates}
    noted = {d["id"] for d in diffs if d["kind"] not in gates} - failed
    print(f"{n - len(failed)}/{n} 道题通过"
          + (f"（其中 {len(noted)} 道有方言/行序差异，已记录）" if noted else "")
          + (" ✅" if not failed else f"；{len(failed)} 道失败 ❌"))


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # eq：末位差吸收，口径差异不吸收
    ok("末位差算相等", eq(151333127.3, 151333127.30000001))
    ok("百分之一的差不算相等", not eq(100.0, 101.0))
    ok("嵌套 rows 逐位比", eq([[1, 2.0]], [[1, 2.0]]))
    ok("行数不同不算相等", not eq([[1]], [[1], [2]]))
    ok("bool 不跟 int 混", not eq(True, 1))
    ok("0 与 1e-9 不算相等（scale 为 0 时不放过）", not eq(0.0, 1e-9))

    # sql_defines_order
    ok("有 ORDER BY 判为有序", sql_defines_order("SELECT a FROM t ORDER BY a"))
    ok("无 ORDER BY 判为无序", not sql_defines_order("SELECT a, count(*) FROM t GROUP BY a"))
    ok("SQL 缺失时判为无序", not sql_defines_order(None))

    # sort_key：混类型不抛异常，NULL 单独一组
    try:
        as_multiset([[1, "a"], [None, "b"], ["x", 2]])
        ok("混类型 + NULL 排序不抛异常", True)
    except TypeError:
        ok("混类型 + NULL 排序不抛异常", False)

    # classify：三档
    A = [["male", 103524], ["female", 110011]]
    B = [["female", 110011], ["male", 103524]]
    ok("行序不同 + 无 ORDER BY → order",
       classify({"a": A, "b": B}, ["a", "b"], False, 0.5) == "order")
    ok("行序不同 + 有 ORDER BY → value",
       classify({"a": A, "b": B}, ["a", "b"], True, 0.5) == "value")
    ok("容差内的数值差 → dialect",
       classify({"a": [[68.31, 10.41]], "b": [[68.3051, 10.4077]]},
                ["a", "b"], True, 0.5) == "dialect")
    ok("超容差的数值差 → value",
       classify({"a": [[68.31]], "b": [[75.0]]}, ["a", "b"], True, 0.5) == "value")
    ok("完全一致 → None",
       classify({"a": A, "b": list(A)}, ["a", "b"], True, 0.5) is None)
    ok("既换序又差末位（无 ORDER BY）→ dialect 而非 value",
       classify({"a": [[1, 68.31], [2, 5.0]], "b": [[2, 5.0], [1, 68.3051]]},
                ["a", "b"], False, 0.5) == "dialect")

    # compare：同样报错不算一致（docstring 第 1 点）
    err = {"id": "X", "status": "golden_error", "detail": "boom", "goldens": []}
    diffs = compare(["a", "b"], {"a": {"X": err}, "b": {"X": dict(err)}})
    ok("两条 arm 同样报错 → unavailable 而非一致",
       len(diffs) == 1 and diffs[0]["kind"] == "unavailable")

    good = {"id": "X", "status": "golden_ok",
            "goldens": [{"label": "l", "rows": [[1]]}]}
    diffs = compare(["a", "b"], {"a": {"X": good}, "b": {}})
    ok("一条 arm 缺记录 → unavailable",
       len(diffs) == 1 and diffs[0]["missing"] == ["b"])

    g2 = {"id": "X", "status": "golden_ok",
          "goldens": [{"label": "l", "rows": [[2]]}]}
    diffs = compare(["a", "b"], {"a": {"X": good}, "b": {"X": g2}})
    ok("取值不同 → value", len(diffs) == 1 and diffs[0]["kind"] == "value")

    diffs = compare(["a", "b"], {"a": {"X": good}, "b": {"X": dict(good)}})
    ok("全一致 → 无差异项", diffs == [])

    g3 = {"id": "X", "status": "golden_ok", "goldens": []}
    diffs = compare(["a", "b"], {"a": {"X": good}, "b": {"X": g3}})
    ok("金标条目数不同 → shape", len(diffs) == 1 and diffs[0]["kind"] == "shape")

    # compare 用题目自己的容差：同一处差异在 15% 容差下是 dialect，在 0.5% 下是 value
    c = {"X": {"id": "X", "judge": {"tolerance_pct": 15.0},
               "golden": [{"label": "l", "sql": "SELECT 1 ORDER BY 1"}]}}
    a_rec = {"id": "X", "status": "golden_ok",
             "goldens": [{"label": "l", "rows": [[100.0]]}]}
    b_rec = {"id": "X", "status": "golden_ok",
             "goldens": [{"label": "l", "rows": [[105.0]]}]}
    diffs = compare(["a", "b"], {"a": {"X": a_rec}, "b": {"X": b_rec}}, c)
    ok("题目容差 15% 下 5% 的差 → dialect",
       len(diffs) == 1 and diffs[0]["kind"] == "dialect")
    c["X"]["judge"]["tolerance_pct"] = 0.5
    diffs = compare(["a", "b"], {"a": {"X": a_rec}, "b": {"X": b_rec}}, c)
    ok("题目容差 0.5% 下同一处差 → value",
       len(diffs) == 1 and diffs[0]["kind"] == "value")

    # golden_sql 按 label 取而不是按下标
    case = {"golden": [{"label": "p", "sql": "A ORDER BY 1"},
                       {"label": "q", "sql": "B"}]}
    ok("golden_sql 按 label 命中", golden_sql(case, 0, "q") == "B")

    for name, good_ in checks:
        print(f"  {'✅' if good_ else '❌'} {name}")
    n_bad = sum(1 for _, g in checks if not g)
    print(f"\n{len(checks)-n_bad}/{len(checks)} 项通过"
          + (" ✅" if not n_bad else " ❌"))
    return 1 if n_bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="横向比三条 arm 的金标取值")
    ap.add_argument("--arms", nargs="+", default=None,
                    help="要比的 arm；默认自动发现 report.dryrun.*.json")
    ap.add_argument("--strict", action="store_true",
                    help="方言差异和行序差异也算失败（默认只记录）")
    ap.add_argument("--selftest", action="store_true",
                    help="判定逻辑自测，不读产物")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    arms = a.arms or discover()
    if len(arms) < 2:
        print(f"❌ 只找到 {len(arms)} 条 arm 的产物（{arms}）。"
              f"少一条 arm 的横向比较不是横向比较——"
              f"先把另外的 arm 跑出 dry-run 产物再来。")
        return 1
    cases, dflt = load_cases()
    data = {}
    for arm in arms:
        _, data[arm] = load(arm)
    diffs = compare(arms, data, cases, dflt)
    render(arms, data, diffs, a.strict)
    gates = GATE_KINDS | ({"dialect", "order"} if a.strict else set())
    return 1 if any(d["kind"] in gates for d in diffs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
