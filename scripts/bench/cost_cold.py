#!/usr/bin/env python3
"""冷启动整批成本 —— 一条 arm 从**开机**到答完 8 条查询，各自花多少钱。

## 为什么不能用 `timing.py` 那一栏

`timing.py` 的「整批摊薄」栏有三处算错，方向全都不一样，所以它不是"偏一点"，
而是不能用：

1. **交叉记账。** 那一栏把 `batch_s`（整批墙上时长，实测 129.8s）**整条**同时
   记到 Redshift 和 DuckDB 头上。可那 129.8 秒里三条 arm 是轮着跑的：Athena 在跑
   的时候 Redshift 什么也没干。让 Redshift 为 Athena 的等待付 RPU-秒，等于把
   "我们把三条 arm 放进同一个循环"这个测量安排算进了它的账单。
2. **多算 6 倍。** 那 129.8 秒覆盖 8 条 × 3 arm × 6 次（1 次一致性/热身 + 5 次
   计时）＝ 144 次执行，而同一栏里 Athena 的金额是「每条查询字节中位数之和」，
   正好一遍。两个数不同口径，摆在一行里比。
3. **Athena 的起步价按批收了一次。** Athena 的 10MB 最低计费是**按查询**的，
   8 条就是 8 个下限。那一栏先把字节加总再套一次下限，把 Athena 算便宜了。

三处错里 Athena 只沾第 3 处（它按字节收钱，与时长无关），所以那一栏越是拿来
"证明 Athena 贵"，越是在证明我们自己的循环安排。

## 这个脚本量的是什么

一条 arm，8 条查询，**各跑一遍**。不热身、不取中位数——成本是"跑一遍花多少"的
函数，重复 5 次是为了给延迟去噪，那是 `timing.py` 的事，两件事分两个脚本。

计费口径按各家真实形状，且**只认账单侧的数**，不认我们自己的折算模型：

| arm      | 成本 = | 取自 |
|----------|--------|------|
| athena   | Σ 每条 max(字节, 10MB) × $5/TB | 查询自报的 `bytes_scanned` |
| duckdb   | Fargate(开机 + 建连 + 自己的执行时长) + S3 GET 次数 | ECS 任务生命周期 + 实测请求数 |
| redshift | Σ 窗口内 `charged_seconds` × RPU 单价 | `SYS_SERVERLESS_USAGE` |

**Redshift 那一行是这个脚本最主要的改动。** 原来的模型是 `max(执行秒, 60)`，
而真账单不是这么算的：实测 `sys_serverless_usage` 里每个活动分钟都是
`charged_seconds = 480`（＝60s × 8 RPU），而且**活动之后紧邻的空闲分钟也照收**
（实测有 `compute_seconds = 0` 但 `charged_seconds = 480` 的整分钟）。也就是说
60 秒下限不是"整批只收一次"，而是按活动分钟一段段地收。这一项模型和账单差
多少，只能去读账单，不能推。

**只有 DuckDB 的成本里含容器。** Athena 和 Redshift 从哪台机器发查询都不改它们的
账单，容器对那两条 arm 只是"让延迟可比"的量具；DuckDB 是进程内引擎，承载它的那
台机器**就是**它的算力，所以开机那一段只记到它头上。三条 arm 放在同一个任务里跑
不影响这个归因：DuckDB 单独跑也要这一段。

## 怎么用

容器内（跑一遍，落 trace）：

    python scripts/bench/cost_cold.py

本机（补账单侧真值，出成本表；不重跑查询、不花钱）：

    python3 scripts/bench/cost_cold.py --settle data/bench/cost-cold-....jsonl --task <任务ID>

离线自测（归因与算式，不连引擎）：

    python3 scripts/bench/cost_cold.py --selftest

`--settle` 拆出来是因为它依赖两件**运行结束之后才存在**的事实：ECS 的
`stoppedAt`（任务还活着的时候没有这个字段），以及 `sys_serverless_usage` 的
计费分钟（按分钟聚合，且有几分钟的落地延迟）。在容器里现算这两个数只能算出
"到目前为止"，那正是要避免的那种半个数。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arms as A                                             # noqa: E402
import correctness as C                                      # noqa: E402
import prices as PR                                          # noqa: E402
import query_correctness as QC                               # noqa: E402
import timing as T                                           # noqa: E402

#: Redshift 的计费分钟从哪里读。`charged_seconds` 已经是 RPU-秒（含 60s 下限和
#: 空闲尾巴），所以除以 3600 再乘 RPU-小时单价就是钱——**不要再乘一次 RPU**，
#: 那是把 8 RPU 算两遍。
USAGE_SQL = """
SELECT start_time, end_time, compute_seconds, compute_capacity, charged_seconds
FROM sys_serverless_usage
WHERE end_time >= '{lo}' AND start_time <= '{hi}'
ORDER BY start_time
"""

#: 计费分钟落地要等一会儿。实测一次查询跑完，它所在那一分钟的 `charged_seconds`
#: 还是 0，几分钟后才补上。`--settle` 等不到就明说等不到，不拿 0 当"这段不要钱"。
USAGE_LAG_SECONDS = 240


# ------------------------------------------------------------------ 容器内：跑一遍

def one_pass(armlist: list[A.Arm], qs: list[dict]) -> dict:
    """每条查询在每条 arm 上各跑一遍，按 arm 分别记账。

    返回的 `active_s` 是**每条 arm 自己的**执行秒数之和，不是整批墙钟。这就是
    修掉交叉记账的那一处：三条 arm 轮着跑的时候，别人在跑的时间不进你的账。

    顺序刻意保持"逐条查询、三条 arm 轮询"，而不是"一条 arm 跑完 8 条再换下一条"：
    前者让每条 arm 的**第一条**查询都落在真正的冷态上，这正是"客户开起来就问"
    那个场景；后者会让后面两条 arm 在别人跑完之后才开始，那时 Redshift 已经被
    唤醒、DuckDB 的进程也活了一段时间了。
    """
    setup_s: dict[str, float] = {}
    for x in armlist:
        t0 = time.perf_counter()
        # 建客户端（Athena/Redshift 是第一次 API 调用，Redshift 还要开会话）+
        # 关加速。这一段属于"开机"，必须计进成本，否则 DuckDB 的 ATTACH 和
        # Redshift 的建会话都成了免费的。
        knobs = x.configure_for_timing()
        setup_s[x.name] = time.perf_counter() - t0
        for line in knobs:
            print(f"  {x.name:<9} {line}")
    print()

    active_s = {x.name: 0.0 for x in armlist}
    rows: list[dict] = []
    inconsistent: list[str] = []

    for q in qs:
        print(f"── {q['key']}")
        rec = {"key": q["key"], "arms": {}}
        keys = {}
        for x in armlist:
            r = T.run_query(x, T.sql_for(q, x.name))
            # 报错那一次的时间也是真花掉的，照记：账单不因为查询失败而退钱。
            active_s[x.name] += r["wall_ms"] / 1000
            if r["error"]:
                print(f"   ❌ {x.name} 跑不动：{r['error']}")
                rec["arms"][x.name] = {"error": r["error"],
                                       "wall_ms": round(r["wall_ms"], 1)}
                keys[x.name] = None
                continue
            keys[x.name] = QC.rows_key(r["rows"], ordered=False)
            rec["arms"][x.name] = {
                "wall_ms": round(r["wall_ms"], 1),
                "engine_ms": round(r["engine_ms"], 1),
                "bytes_scanned": r["bytes_scanned"],
                "reconnected": r["reconnected"],
            }
            print(f"   {x.name:<9} {r['wall_ms']:8.1f}ms"
                  f"  扫描 {T.fmt_bytes(r['bytes_scanned'])}")
        # 一致性在这里只**记**不拦。理由：钱已经花掉了，答案不对不会退钱，所以
        # 成本栏照报；但一致性坏了整份对比就没有意义，所以它进退出码。
        got = {k: v for k, v in keys.items() if v is not None}
        if len(set(map(str, got.values()))) > 1:
            inconsistent.append(q["key"])
            print(f"   ⚠️  三条 arm 答案不同 —— 成本照报（钱花了），"
                  f"但这一条的对比不成立")
        rec["inconsistent"] = q["key"] in inconsistent
        rows.append(rec)

    return {"setup_s": setup_s, "active_s": active_s, "rows": rows,
            "inconsistent": inconsistent}


def duckdb_requests(arm: A.Arm, qs: list[dict]) -> dict | None:
    """一条全新连接上顺跑 8 条，数整批发了多少次 S3 请求。

    为什么这么数，而不是复用 `count_http` 的「每条各建一条冷连接」：那种数法把
    每条查询都当成空缓存开局，加总起来比真实的一遍高——真跑一遍时前面查询拉过的
    列块留在对象缓存里，后面的查询就不再拉。这里要的正是"一遍"的请求结构：
    第一条冷、之后带着缓存。

    **不在上面那个计时循环里开日志**：开日志有开销，会让 DuckDB 的执行秒数虚高，
    而那个秒数直接换成 Fargate 的钱。所以这一遍单独跑，时间丢掉、只取请求数。
    代价是多跑一遍 8 条查询（多花的是这一遍的 Fargate 时长，几秒钱）。
    """
    if arm.name != "duckdb":
        return None
    with arm._fresh_conn() as c:                              # noqa: SLF001
        def body(conn):
            for q in qs:
                conn.execute(T.sql_for(q, "duckdb")).fetchall()
        return arm._http_around(c, body)                      # noqa: SLF001


# ------------------------------------------------------------------ 本机：补账单

def parse_lifecycle(spec: str) -> dict:
    """`--lifecycle` 的三个时刻 → 与 `ecs_lifecycle` 同形状的 dict。

    存在的理由是 ECS 只留停止任务约一小时。过了那个窗口 `describe_tasks` 返回空，
    结算就再也做不了——而开机那一段是 DuckDB 成本里最大的一项，缺了它这条 arm 会
    显著偏低。所以留一条口子：把当时读到的三个时刻手工传进来。

    **这是断言，不是测量。** 传进来的数没有任何东西能替你核对，出处得自己交代清楚
    （下面会在输出里标成「手工给定」，别让后来读报告的人以为它是从 ECS 读的）。
    真正的修法是让 `--run` 结束时就把生命周期写进 trace，这样结算不再有时限。
    """
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 3:
        raise SystemExit(
            "--lifecycle 要三个 ISO 时刻，逗号分隔："
            "createdAt,startedAt,stoppedAt（例如 "
            "2026-09-07T13:33:41.924+08:00,...,2026-09-07T13:35:50.543+08:00）")
    try:
        ts = [datetime.fromisoformat(p) for p in parts]
    except ValueError as e:
        raise SystemExit(f"--lifecycle 时刻解析不了：{e}")
    if not ts[0] <= ts[1] <= ts[2]:
        raise SystemExit(
            f"--lifecycle 三个时刻不是递增的：created {parts[0]} / "
            f"started {parts[1]} / stopped {parts[2]}。顺序错了算出来的开机段会是负数。")
    k = ("createdAt", "startedAt", "stoppedAt")
    out = {n: t.astimezone(timezone.utc).isoformat() for n, t in zip(k, ts)}
    return {**out, "stoppingAt": None, "lastStatus": "STOPPED",
            "source": "手工给定（--lifecycle），不是从 ECS 读的"}


def ecs_lifecycle(task: str, cluster: str, region: str) -> dict:
    """任务的生命周期时刻。`createdAt → startedAt` 就是拉镜像 + 起容器那一段。

    这一段算进 DuckDB 的成本是**故意的**：Fargate 从任务开始置备就计费，而客户
    真实体感里"开机等的那一分钟"也确实在等。它单独一行列出来，这样想按"常驻、
    不含开机"读的人自己减得掉。
    """
    import boto3
    ecs = boto3.client("ecs", region_name=region)
    r = ecs.describe_tasks(cluster=cluster, tasks=[task])
    if not r["tasks"]:
        raise RuntimeError(
            f"在 {cluster} 里找不到任务 {task}。ECS 只保留停止任务约 1 小时，"
            f"超过就查不到了——那时只能用 trace 里的进程内时长，"
            f"开机那一段会缺，DuckDB 的成本会偏低。")
    t = r["tasks"][0]
    out = {k: t.get(k) for k in
           ("createdAt", "startedAt", "stoppingAt", "stoppedAt", "lastStatus")}
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
    out["source"] = f"ecs:describe_tasks {cluster}/{task}"
    if t.get("lastStatus") != "STOPPED":
        raise RuntimeError(
            f"任务 {task} 还是 {t.get('lastStatus')}，没有 stoppedAt。"
            f"Fargate 按存活时长计费，任务没停就没有"
            f"「一共开了多久」这个数——现在算出来的只是「到目前为止」。")
    return out


def redshift_charged(lo: str, hi: str, region: str) -> dict:
    """窗口内 Redshift 真实计费的 RPU-秒。取自 `sys_serverless_usage`。

    窗口两头各放宽一分钟：那张表按整分钟聚合，一次落在 05:26:50 的查询记在
    05:26:00–05:27:00 这一行上，按秒级窗口去筛会把它整行漏掉。
    """
    import boto3
    rsd = boto3.client("redshift-data", region_name=region)
    lo_dt = datetime.fromisoformat(lo) - timedelta(minutes=1)
    hi_dt = datetime.fromisoformat(hi) + timedelta(minutes=1)
    sql = USAGE_SQL.format(lo=lo_dt.strftime("%Y-%m-%d %H:%M:%S"),
                           hi=hi_dt.strftime("%Y-%m-%d %H:%M:%S"))
    kw = {"Database": os.environ.get("REDSHIFT_DATABASE", "app_analytics"),
          "WorkgroupName": os.environ.get("REDSHIFT_WORKGROUP",
                                          "analytics-agent-wg"),
          "Sql": sql}
    if os.environ.get("REDSHIFT_SECRET_ARN"):
        kw["SecretArn"] = os.environ["REDSHIFT_SECRET_ARN"]
    sid = rsd.execute_statement(**kw)["Id"]
    for _ in range(60):
        d = rsd.describe_statement(Id=sid)
        if d["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(2)
    if d["Status"] != "FINISHED":
        raise RuntimeError(f"读 sys_serverless_usage 失败：{d.get('Error')}")
    out = []
    for rec in rsd.get_statement_result(Id=sid)["Records"]:
        vals = [list(c.values())[0] for c in rec]
        out.append({"start": str(vals[0]), "end": str(vals[1]),
                    "compute_seconds": float(vals[2] or 0),
                    "capacity": float(vals[3] or 0),
                    "charged_rpu_seconds": int(vals[4] or 0)})
    return {"minutes": out,
            "charged_rpu_seconds": sum(m["charged_rpu_seconds"] for m in out),
            "compute_seconds": sum(m["compute_seconds"] for m in out)}


def settle(trace: Path, task: str | None, cluster: str, region: str,
           skip_usage: bool, lifecycle: str | None = None) -> int:
    """把一遍的测量 + 账单侧事实合成成本表。"""
    lines = [json.loads(l) for l in
             trace.read_text(encoding="utf-8").splitlines() if l.strip()]
    head = next(d for d in lines if d["kind"] == "run")
    end = next(d for d in lines if d["kind"] == "end")
    rows = [d for d in lines if d["kind"] == "query"]
    prices = head["prices"]
    armnames = head["arms"]

    print(f"冷启动整批成本 —— 结算\n")
    print(f"  测量 trace  {trace}")
    print(f"  跑于        {head['started_at']}  主机 {head.get('host')}")
    print(f"  查询        {len(rows)} 条，每条每 arm 各 1 遍（不热身、不取中位数）")
    print(f"  单价        {prices['source']}，取数时刻 {prices['fetched_at']}")
    if end.get("inconsistent"):
        print(f"  ⚠️  答案不一致的查询：{'、'.join(end['inconsistent'])}"
              f" —— 成本数照给，但这几条的 arm 间对比不成立")

    life = None
    startup_s = 0.0
    if lifecycle or task:
        life = parse_lifecycle(lifecycle) if lifecycle \
            else ecs_lifecycle(task, cluster, region)
        c0 = datetime.fromisoformat(life["createdAt"])
        s0 = datetime.fromisoformat(life["startedAt"] or life["createdAt"])
        startup_s = (s0 - c0).total_seconds()
        task_s = (datetime.fromisoformat(life["stoppedAt"]) - c0).total_seconds()
        print(f"\n  ECS 任务    {task or '(未给 --task)'}")
        print(f"    生命周期出处   {life['source']}")
        print(f"    置备到容器就绪 {startup_s:.1f}s（拉镜像 + 起容器）")
        print(f"    任务总存活     {task_s:.1f}s（Fargate 按这个数计费）")
    else:
        print(f"\n  ⚠️  没给 --task：开机那一段拿不到，DuckDB 的成本会**偏低**。"
              f"\n      这不是可以忽略的小项——实测置备就绪要几十秒，"
              f"而它比 DuckDB 自己的执行时长还长。")

    active = end["active_s"]
    setup = end["setup_s"]

    print(f"\n每条 arm 自己的时长（秒）")
    print(f"  {'arm':<10} {'建连/开会话':>12} {'执行 8 条':>10} {'小计':>8}")
    for n in armnames:
        print(f"  {n:<10} {setup.get(n, 0):>12.2f} {active.get(n, 0):>10.2f} "
              f"{setup.get(n, 0) + active.get(n, 0):>8.2f}")
    print(f"  三条加起来 {sum(setup.values()) + sum(active.values()):.1f}s，"
          f"整批墙钟 {end['batch_s']:.1f}s —— 差额是客户端自己的开销。"
          f"\n  **旧的「整批摊薄」栏把 {end['batch_s']:.0f}s 整条同时记给了 "
          f"Redshift 和 DuckDB**，而它们各自只占其中一段。")

    cost: dict[str, tuple[float, str]] = {}

    if "athena" in armnames:
        per = []
        for r in rows:
            b = r["arms"].get("athena", {}).get("bytes_scanned")
            if b is None:
                continue
            per.append(PR.athena_cost(int(b), prices)[0])
        b_tot = sum(int(r["arms"].get("athena", {}).get("bytes_scanned") or 0)
                    for r in rows)
        cost["athena"] = (sum(per),
                          f"{len(per)} 条各自套 10MB 下限（**下限按查询算，"
                          f"不是按批算**），整批实扫 {T.fmt_bytes(b_tot)}")

    if "duckdb" in armnames:
        own = startup_s + setup.get("duckdb", 0.0) + active.get("duckdb", 0.0)
        # 这一档算的是「为跑这一遍而起一个任务」，单位正是一个任务，所以套 60s 起步价。
        # 开机那段通常已经把它顶过去了，但不写上就等于默许「租 0.9 秒任务」这种报价。
        base = PR.fargate_cost(own, prices, floor=True)[0]
        reqs = (end.get("duckdb_requests") or {}).get("total")
        if reqs is None:
            cost["duckdb"] = (base, f"Fargate {own:.1f}s"
                                    f"（开机 {startup_s:.0f}s + 建连 "
                                    f"{setup.get('duckdb', 0):.1f}s + 执行 "
                                    f"{active.get('duckdb', 0):.1f}s）"
                                    f"；**请求费没数到，这个数偏低**")
        else:
            rc = PR.s3_get_cost(reqs, prices)[0]
            cost["duckdb"] = (base + rc,
                              f"Fargate {own:.1f}s（开机 {startup_s:.0f}s + 建连 "
                              f"{setup.get('duckdb', 0):.1f}s + 执行 "
                              f"{active.get('duckdb', 0):.1f}s）"
                              f" + 整批 {reqs} 次 S3 GET")

    if "redshift" in armnames and not skip_usage:
        age = time.time() - _epoch(end["finished_at"])
        if age < USAGE_LAG_SECONDS:
            print(f"\n  ⚠️  这一遍才结束 {age:.0f}s，`sys_serverless_usage` 的"
                  f"计费分钟通常要 {USAGE_LAG_SECONDS}s 才落地。"
                  f"\n      现在读会读到 charged_seconds=0，那不是"
                  f"「这段不要钱」，是「还没记上」。等一会儿再 --settle。")
        u = redshift_charged(head["started_at_iso"], end["finished_at"], region)
        rate = PR.usd(prices, "redshift_per_rpu_hour")
        c = u["charged_rpu_seconds"] / 3600 * rate
        billed_min = sum(1 for m in u["minutes"] if m["charged_rpu_seconds"])
        idle_min = sum(1 for m in u["minutes"]
                       if m["charged_rpu_seconds"] and not m["compute_seconds"])
        cost["redshift"] = (c,
                            f"账单侧 {u['charged_rpu_seconds']} RPU-秒"
                            f"（{billed_min} 个计费分钟，其中 {idle_min} 个"
                            f"compute=0 也照收）；引擎实算 "
                            f"{u['compute_seconds']:.0f}s")
        _usage_table(u, active.get("redshift", 0.0), prices)
    elif "redshift" in armnames:
        c, note = PR.redshift_cost(active.get("redshift", 0.0), prices)
        cost["redshift"] = (c, note + "（**模型折算，不是账单**："
                                      "跳过了 sys_serverless_usage）")

    print(f"\n冷启动整批成本（USD，一遍 8 条，从开机算起）")
    for n in armnames:
        if n not in cost:
            continue
        c, why = cost[n]
        print(f"  {n:<10} {c:>12.6f}   {why}")

    vals = {n: c for n, (c, _) in cost.items()}
    if len(vals) > 1:
        lo = min(vals, key=vals.get)
        print(f"\n  最便宜的是 {lo}（${vals[lo]:.6f}）。倍数：" + "、".join(
            f"{n} {vals[n] / vals[lo]:.1f}×" for n in armnames
            if n in vals and n != lo))

    print(f"\n端到端墙钟（一遍 8 条，含开机；这不是 timing.py 那张热态中位数表）")
    for n in armnames:
        e2e = setup.get(n, 0) + active.get(n, 0) + (
            startup_s if n == "duckdb" else 0.0)
        note = "（含开机 %.0fs）" % startup_s if n == "duckdb" and startup_s else ""
        print(f"  {n:<10} {e2e:>8.1f}s {note}")
    print("  Athena / Redshift 不含开机不是漏算：它们是托管服务，"
          "从哪台机器发查询都不改账单，\n  容器对那两条 arm 只是让延迟可比的量具。"
          "DuckDB 是进程内引擎，那台机器**就是**它的算力。")
    return 0


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _usage_table(u: dict, active_s: float, prices: dict) -> None:
    rate = PR.usd(prices, "redshift_per_rpu_hour")
    print(f"\nRedshift 的计费分钟（`sys_serverless_usage`，账单侧真值）")
    print(f"  {'分钟':<20} {'引擎实算':>8} {'容量':>6} {'计费 RPU-秒':>12} {'钱':>10}")
    for m in u["minutes"]:
        print(f"  {m['start'][:19]:<20} {m['compute_seconds']:>8.1f} "
              f"{m['capacity']:>6.0f} {m['charged_rpu_seconds']:>12} "
              f"{m['charged_rpu_seconds'] / 3600 * rate:>10.6f}")
    model = PR.redshift_cost(active_s, prices)[0]
    real = u["charged_rpu_seconds"] / 3600 * rate
    if model:
        print(f"  模型 max({active_s:.1f}s, 60s) 折算 ${model:.6f}，"
              f"账单侧 ${real:.6f} —— 差 {real / model:.1f}×。"
              f"\n  差在哪：60 秒下限不是"
              f"「整批只收一次」，而是按**活动分钟**一段段收，"
              f"而且活动之后紧邻的空闲分钟也照收。")


# ------------------------------------------------------------------ 自测

def selftest() -> int:
    """归因与算式，不连引擎。每一例都对着上面 docstring 里说的那三处错。"""
    # 自测用固定单价，不连价目 API：这几例验的是**算式和归因**，不是当天的价格。
    # 数值与 prices.py 自测里那一份一致，两处对不上时说明有人只改了一处。
    P = {"source": "自测", "fetched_at": "-",
         "items": {"athena_per_tb_scanned": {"usd": 5.0},
                   "redshift_per_rpu_hour": {"usd": 0.36},
                   "fargate_per_vcpu_hour": {"usd": 0.04048},
                   "fargate_per_gb_hour": {"usd": 0.004445},
                   "s3_per_get_request": {"usd": 4e-7}}}
    bad = 0

    def want(label, got, exp, tol=1e-9):
        nonlocal bad
        if abs(got - exp) > tol:
            bad += 1
            print(f"  FAIL {label}：得到 {got!r}，期望 {exp!r}")

    # 1. Athena 的 10MB 下限**按查询**收，不是按批收。这一条就是第 3 处错：
    #    8 条各 1MB，正确答案是 8 个下限，不是把 8MB 加起来套一个下限。
    per = sum(PR.athena_cost(1 << 20, P)[0] for _ in range(8))
    batch = PR.athena_cost(8 << 20, P)[0]
    want("8 条各 1MB = 8 个下限", per, 8 * PR.athena_cost(1 << 20, P)[0])
    if not per > batch:
        bad += 1
        print(f"  FAIL 按查询套下限（${per:.6f}）应当**贵于**先加总再套下限"
              f"（${batch:.6f}）——这正是旧那一栏把 Athena 算便宜的地方")

    # 2. 交叉记账：三条 arm 各自的时长之和不该等于任何一条 arm 的计费时长。
    #    用一组假数据把旧算法和新算法都算一遍，确认它们不同且方向是"旧的偏贵"。
    active = {"athena": 81.0, "redshift": 26.5, "duckdb": 5.6}
    old_shared = 129.8
    old_rs = PR.redshift_cost(old_shared, P)[0]
    new_rs = PR.redshift_cost(active["redshift"], P)[0]
    if not old_rs > new_rs:
        bad += 1
        print(f"  FAIL 共享窗口记账（${old_rs:.6f}）应当贵于按自己时长记账"
              f"（${new_rs:.6f}）")
    # 26.5s 落在 60s 下限以下，所以新算法应当恰好等于下限价
    want("自己时长不足 60s 时按下限", new_rs, 60 / 3600 * 8 * 0.36)

    # 3. charged_seconds 已经是 RPU-秒，换钱时**不能再乘一次 RPU**。
    #    1920 RPU-秒（＝4 个计费分钟 × 480）应当等于 4 × 60s × 8RPU 的价。
    want("1920 RPU-秒 = 4 分钟 × 8RPU", 1920 / 3600 * 0.36,
         4 * PR.redshift_cost(60, P, rpu=8)[0])

    # 4. DuckDB 的开机段必须计入，且它比执行段更大——**这就是为什么不给 --task
    #    会显著偏低**。用实测量级：置备 60s、执行 5.6s。
    with_boot = PR.fargate_cost(60 + 1.0 + 5.6, P)[0]
    without = PR.fargate_cost(1.0 + 5.6, P)[0]
    if not with_boot > 5 * without:
        bad += 1
        print(f"  FAIL 含开机 ${with_boot:.6f} 与不含 ${without:.6f} 的差距"
              f"应当在一个量级以上——不然 --task 缺不缺就不重要了")

    # 5. 请求费不随时长变化：它是次数的函数。摊薄时长不该让请求费缩水。
    want("1376 次 GET", PR.s3_get_cost(1376, P)[0], 1376 * 4e-7)

    # 6. 窗口两头放宽一分钟：一次落在 05:26:50 的查询必须能被 05:26:00 那一行
    #    覆盖到。这里只验时间算术，不连引擎。
    lo = datetime.fromisoformat("2026-09-07T05:26:50+00:00") - timedelta(minutes=1)
    if lo.strftime("%Y-%m-%d %H:%M:%S") != "2026-09-07 05:25:50":
        bad += 1
        print("  FAIL 窗口下界放宽算错了")

    # 7. --lifecycle 的手工时刻：过了 ECS 那一小时窗口之后唯一还能结算的路。
    #    它是断言不是测量，所以两件事都要钉：算出来的开机段对，以及出处标得出来。
    lc = parse_lifecycle("2026-09-07T13:33:41.924+08:00,"
                         "2026-09-07T13:34:07.952+08:00,"
                         "2026-09-07T13:35:50.543+08:00")
    boot = (datetime.fromisoformat(lc["startedAt"])
            - datetime.fromisoformat(lc["createdAt"])).total_seconds()
    want("手工生命周期的开机段", round(boot, 1), 26.0)
    if "手工" not in lc["source"]:
        bad += 1
        print("  FAIL --lifecycle 没把出处标成手工给定——报告读者会以为它是从 ECS 读的")
    # 顺序颠倒必须直接拒掉：算出来会是负的开机时长，而负数会静静地让成本偏低。
    for spec, why in (
            ("2026-09-07T13:34:07+08:00,2026-09-07T13:33:41+08:00,"
             "2026-09-07T13:35:50+08:00", "started 早于 created"),
            ("2026-09-07T13:33:41+08:00,2026-09-07T13:34:07+08:00", "只给了两个时刻")):
        try:
            parse_lifecycle(spec)
            bad += 1
            print(f"  FAIL --lifecycle 收下了坏输入（{why}）")
        except SystemExit:
            pass

    if bad:
        print(f"\n{bad} 项没过 ❌")
        return 1
    print("自测通过 ✅  12 项：Athena 下限按查询 / 按查询套下限更贵 / 交叉记账方向 / "
          "不足 60s 走下限 / RPU-秒不重复乘 / 开机段量级 / 请求费与时长无关 / 窗口放宽 / "
          "手工生命周期（开机段、出处标注、乱序与缺项各拒一次）")
    return 0


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="冷启动整批成本")
    ap.add_argument("--arm", action="append", default=[], choices=list(A.ARMS))
    ap.add_argument("-k", "--key", action="append", default=[])
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--settle", metavar="TRACE",
                    help="本机结算：读 trace + 账单侧真值，不重跑查询")
    ap.add_argument("--task", help="--settle 用：这一遍跑在哪个 ECS 任务上")
    ap.add_argument("--lifecycle", metavar="CREATED,STARTED,STOPPED",
                    help="--settle 用：手工给任务生命周期的三个 ISO 时刻。"
                         "ECS 只留停止任务约 1 小时，过期后 --task 查不到，"
                         "这条口子让结算还能做——但那是断言不是测量，会在输出里标明")
    ap.add_argument("--cluster", default=os.environ.get("BENCH_CLUSTER",
                                                        "analytics-agent-relay"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--skip-usage", action="store_true",
                    help="--settle 用：不读 sys_serverless_usage，退回模型折算")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.settle:
        if a.settle.startswith("s3://"):
            # `Path("s3://x/y")` 会被规整成 `s3:/x/y`，然后报一个看不出所以然的
            # FileNotFoundError。trace 默认就上传到 S3，所以粘 URI 进来是常态。
            raise SystemExit(
                f"--settle 收的是本机路径。先拉下来再结算：\n"
                f"  aws s3 cp {a.settle} data/bench/ --region {a.region}\n"
                f"  {sys.argv[0]} --settle data/bench/{a.settle.rsplit('/', 1)[-1]}")
        return settle(Path(a.settle), a.task, a.cluster, a.region, a.skip_usage,
                      a.lifecycle)

    qs = T.QUERIES
    if a.key:
        qs = [q for q in qs if any(k in q["key"] for k in a.key)]
    if not qs:
        print("按这些条件一个查询都没选中")
        return 2

    prices = PR.load()
    armlist = A.all_arms(a.arm or None)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    print(f"冷启动整批成本 —— 测量")
    print(f"  arm {'、'.join(x.name for x in armlist)}    查询 {len(qs)} 条"
          f"    每条每 arm **各 1 遍**")
    print(f"  单价取自 {prices['source']}，取数时刻 {prices['fetched_at']}\n")

    # 轮询必须在建客户端之前收紧，理由见 timing.py 里那两个常量。这里同样要收：
    # 客户端 sleep 会进 DuckDB 之外两条 arm 的墙钟，而 Redshift 的墙钟这里只用来
    # 报端到端，不用来算钱（钱走账单侧），但报出来的数还是要是它自己的。
    T.tighten_polling()
    print("关加速（回读确认）：")

    batch_t0 = time.time()
    out = one_pass(armlist, qs)
    batch_s = time.time() - batch_t0

    # 请求数最后数，时间不进上面的窗口。
    dreq = None
    for x in armlist:
        if x.name != "duckdb":
            continue
        try:
            dreq = duckdb_requests(x, qs)
        except Exception as e:                                # noqa: BLE001
            print(f"\n⚠️  DuckDB 整批请求数没数到：{type(e).__name__}: {e}"[:200]
                  + "\n    成本栏会缺请求费这一项，方向是把 DuckDB 算便宜。")
    if dreq:
        print(f"\nDuckDB 整批 S3 请求 {dreq['total']} 次"
              f"（对象 {dreq['object']} / catalog {dreq['catalog']}）"
              f"\n  这是**一遍**的请求结构：第一条冷读、之后带着对象缓存。"
              f"不是「每条各冷读一次」的加总，那样会高估。")

    finished = datetime.now(timezone.utc)
    print(f"\n整批墙钟 {batch_s:.1f}s；三条 arm 各自时长："
          + "、".join(f"{n} {out['setup_s'][n] + out['active_s'][n]:.1f}s"
                      for n in out['active_s']))

    if not a.no_trace:
        C.TRACE_DIR.mkdir(parents=True, exist_ok=True)
        tp = C.TRACE_DIR / f"cost-cold-{stamp}.jsonl"
        with tp.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "kind": "run", "layer": "cost-cold", "started_at": stamp,
                "started_at_iso": started.isoformat(),
                "arms": [x.name for x in armlist], "queries": len(qs),
                "reps": 1, "prices": prices, "host": os.uname().nodename,
                # 这一遍**不是**性能数字：单次采样、含冷启动，与 timing.py 那份
                # 热态中位数不可混用。标出来，免得后来的人当延迟基准引用。
                "timing_valid_for_perf": False,
            }, ensure_ascii=False) + "\n")
            for r in out["rows"]:
                f.write(json.dumps({"kind": "query", **r},
                                   ensure_ascii=False) + "\n")
            f.write(json.dumps({
                "kind": "end", "batch_s": round(batch_s, 1),
                "finished_at": finished.isoformat(),
                "setup_s": {k: round(v, 3) for k, v in out["setup_s"].items()},
                "active_s": {k: round(v, 3) for k, v in out["active_s"].items()},
                "duckdb_requests": dreq,
                "inconsistent": out["inconsistent"],
            }, ensure_ascii=False) + "\n")
        print(f"\ntrace：{tp}")
        uri = C.upload_trace(tp)
        if uri:
            print(f"      {uri}")
        print(f"\n下一步（本机，等 {USAGE_LAG_SECONDS}s 让计费分钟落地）："
              f"\n  python3 scripts/bench/cost_cold.py --settle "
              f"data/bench/{tp.name} --task <任务ID>")

    if out["inconsistent"]:
        print(f"\n{len(out['inconsistent'])} 条查询三条 arm 答案不同 ❌")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
