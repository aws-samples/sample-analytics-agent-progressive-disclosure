#!/usr/bin/env python3
"""三条 arm 的耗时与成本对比 —— 指标里的第二和第三项。

正确性是**闸门**（错了就不是"部分好"），耗时和成本是**刻度**。这个脚本只跑刻度，
但它把闸门作为前置条件写进流程里，见下面第 1 条。

## 五条规矩，每一条都是为了让数字可比

**1. 一条查询的耗时只有在三条 arm 答案相同时才有意义。** 否则量的是"谁做的活少"。
所以每条查询先在三条 arm 上各跑一次、用 `query_correctness` 那套比法（同一个
`rows_key`，不另造一把尺）对结果集；**不一致的查询不报耗时**，直接标 ❌ 并说明。
这一条不是锦上添花：`SUM(actual_amount / item_count)` 这种查询三条 arm 会给出
不同的数（DECIMAL/INTEGER 的方言差异），拿它比耗时等于比两个不同的问题。

**2. 尺子是客户端墙上时钟，不是引擎自报的时间。** 三家自报的东西口径不同：
Athena 的 `EngineExecutionTimeInMillis` **不含排队**，Redshift Data API 的
`Duration` 是服务端执行时长，DuckDB 是进程内、自报就是墙上时钟。拿引擎时间比
DuckDB 的墙上时钟，等于放掉 Athena 的排队和两家的网络往返——而那是用户真的在等的
时间。所以主指标统一用客户端墙上时钟，引擎自报值**一起记下来**，两者之差本身
就是一个发现（Athena 的排队 + 取结果开销）。

**3. 先热一次，再量 N 次，报中位数。** 第一次跑含冷启动、排队、连接建立、
元数据加载，它是"用户第一次问"的真实体感，所以**单独报**而不是丢掉也不是混进去。
之后 N 次取中位数：中位数抗单点抖动，而平均值会被一次网络抖动带跑。min/max
一起报，这样"这个数稳不稳"是看得见的。

**4. 三家的计费形状不同，所以成本必须报两个数。** Athena 按扫描字节、
Redshift 按 RPU-秒（60 秒起步）、DuckDB 自己免费但要为承载它的 Fargate 任务付时长。
把它们压成一个"每条查询多少钱"会误导：Redshift 那 60 秒起步价意味着**一条 0.9 秒的
查询和一条 59 秒的查询同价**，于是"单条隔离成本"里 Redshift 恒定偏贵，而真实
批量作业里连续查询共享同一个计费时段。所以两栏都给：**单条隔离**（每条各自单独来一次，各付满自己的起步价）
和**整批摊薄**（把这批查询连着跑一遍，起步价只落一次），并注明哪一栏对应哪种用法。

隔离那一栏对 DuckDB 还要多算一项：**每条查询单独来一次就得先起一个任务**，
所以它含 `DUCKDB_BOOT_SECONDS` 那 26 秒拉镜像起容器。这一项 2026-09-07 之前漏了，
把它报低了约 52 倍（$0.000606 对 $0.031344），而漏的形态是：摊薄那栏含开机、
隔离那栏不含，同一张表里两条 arm 口径不一致。托管服务没有开机这一项，进程内引擎有。

摊薄那一栏的时长口径是**各 arm 自己的中位耗时之和**，不是整批墙上时长。这一点踩过坑：
整批墙钟覆盖的是「三条 arm × 每条查询 × (热身 + N 遍)」，拿它去算 Redshift 的 RPU-秒和
Fargate 的任务时长，等于让每条 arm 为另两条跑的时间付钱，而且同一段时间被收了两遍。
Athena 那一格更直接——它按扫描字节收钱，10MB 起步价是**每条查询**的，压根没有可摊薄的
时长，所以它两栏按构造相等，这个相等是结论不是算漏。

**5. 单价从 AWS 价目 API 取，不写在代码里。** 见 `prices.py`。

## 不由这个脚本回答的事

- **本机跑不出 DuckDB 的计算成本**，因为本机不计费。这里给的是"同样的墙上时长
  放到 Fargate 上要多少钱"这个折算值，成本说明里会写出它的构成（开机 + 执行 +
  起步价 + S3 请求），不当账单用。要账单侧真值得走 `cost_cold.py --settle`，
  它读的是 ECS 任务的真实生命周期。
- **DuckDB 直读 S3 的请求费已经计入，是实测值。** 这一项曾经是个已知缺口，理由是
  「请求数在客户端不可观测」——那句话在 DuckDB 1.5 之后不成立了：`enable_logging('HTTP')`
  会把每一次请求落进 `duckdb_logs_parsed('HTTP')`，连 URL 一起。所以现在按查询数
  真实请求数（见 `arms.Arm.count_http`），按 S3 GET 单价折成钱加进 DuckDB 那一栏。
  数请求要开日志，开日志有开销，所以它**单独跑一次、不进计时样本**。
  Athena 的 $5/TB 和 Redshift 的 RPU-秒里已经含了各自的请求费，那两栏这里是空的。
- Redshift 的**按查询** RPU 归因是折算（RPU 数 × 时长），真账单在
  `SYS_SERVERLESS_USAGE` 里按时间窗聚合。两者对不上是正常的，原因就是第 4 条说的
  计费时段共享。

用法：

    python3 scripts/bench/timing.py --selftest        # 统计与成本逻辑自测，不连引擎
    python3 scripts/bench/timing.py --list            # 列出查询集，不跑
    python3 scripts/bench/timing.py --reps 1 -k scan  # 先小跑一条确认管路
    python3 scripts/bench/timing.py                   # 全量（花钱，先看 --list 的预估）
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arms as A                                            # noqa: E402
import correctness as C                                     # noqa: E402
import prices as PR                                         # noqa: E402
import query_correctness as QC                              # noqa: E402

DEFAULT_REPS = 5

#: 计时期间收紧客户端轮询间隔，单位秒。**这不是调参，是修一个量错了的东西**：
#: Athena 和 Redshift 都是「提交 → 轮询 → 取结果」，那个 sleep 整体加在墙上时钟上，
#: 而三条 arm 的间隔各不相同（DuckDB 进程内、不轮询）。实测默认节奏下，
#: 一条引擎自报 109ms 的 Redshift 查询墙上时钟量到 1293ms，七成是我们自己的 sleep——
#: 那时候「Redshift 比 DuckDB 慢 3 倍」这句话说的是我们的客户端，不是引擎。
#: 收紧后残差仍在（提交 + 取结果的往返），但它是这条 arm 真实的接口成本，
#: 所以报告里把 墙上时钟 / 引擎自报 / 差额 三个数一起给，让读者看得见构成。
TIMING_POLL_INITIAL = 0.02
TIMING_POLL_MAX = 0.2

#: 查询集。`shape` 是这条查询**想让引擎差异现形**的地方，不是装饰——挑查询的唯一
#: 理由就是它。`sql` 是三家通写；方言不同的用 `by_arm`，与 `query_correctness`
#: 同一种写法（不另造机制）。
#:
#: **只放三条 arm 都跑得动的 SQL**，这本身是个约束而不是取舍：Redshift 没有 `RANGE`
#: 窗口帧、也拒绝"有 ORDER BY 无帧"的聚合窗口函数，而那正是 SQL 标准的默认帧。
#: 所以基准里的窗口查询只能用 `ROWS` 那种写法——"可移植的写法比标准写法窄"这件事
#: 是对比结论之一，记在 AGENTS.md 里。
QUERIES: list[dict] = [
    {
        "key": "scan-agg",
        "shape": "全表列式扫描 + 数值聚合。最基本的一条：三家读的是同一批 Parquet，"
                 "所以这条的差距接近纯引擎差距。",
        "sql": """
            SELECT count(*) AS n, SUM(actual_amount) AS gmv,
                   SUM(discount_amount) AS disc, MIN(item_count) AS mn,
                   MAX(item_count) AS mx
            FROM orders
        """,
    },
    {
        "key": "prune-1day",
        "shape": "分区剪枝。orders 按 day(placed_at) 分区，这条只碰一天。"
                 "Athena/DuckDB 走 Iceberg 清单剪枝，Redshift 那份是 COPY 进去的"
                 "普通表，靠排序键和区块统计——**这正是那次额外物化的代价现形的地方**。",
        "sql": """
            SELECT count(*) AS n, SUM(actual_amount) AS gmv
            FROM orders
            WHERE placed_at >= TIMESTAMP '2025-11-15 00:00:00'
              AND placed_at <  TIMESTAMP '2025-11-16 00:00:00'
        """,
    },
    {
        "key": "groupby-highcard",
        "shape": "高基数分组（按 user_id，十几万组）+ top-N。哈希聚合的内存压力，"
                 "DuckDB 4GB 额度下可能溢写——溢写与否是它耗时的主要来源。",
        # ORDER BY 里带 user_id 是必须的：只按 gmv 排，并列行的顺序由引擎决定，
        # 会量出一堆假的"结果不一致"。
        "sql": """
            SELECT user_id, count(*) AS orders_n, SUM(actual_amount) AS gmv
            FROM orders
            GROUP BY user_id
            ORDER BY gmv DESC, user_id
            LIMIT 20
        """,
    },
    {
        "key": "join-fanout",
        "shape": "一对多 JOIN（85 万单 ⋈ 180 万明细）后聚合。JOIN 策略的差异。",
        "sql": """
            SELECT count(*) AS joined, SUM(i.quantity) AS qty,
                   SUM(i.unit_price * i.quantity) AS line_total
            FROM orders o JOIN order_items i ON o.order_id = i.order_id
        """,
    },
    {
        "key": "events-daily",
        "shape": "全库最大的表（854 万行）按天分桶。扫描量最大的一条，"
                 "Athena 的成本在这里最高，因为它按字节收钱。",
        "sql": """
            SELECT CAST(event_time AS DATE) AS d, count(*) AS n
            FROM events
            GROUP BY CAST(event_time AS DATE)
            ORDER BY d
        """,
    },
    {
        "key": "text-like",
        "shape": "427 万行上的文本匹配，没有任何剪枝可用。字符串处理的差异，"
                 "而且这一列 09-02 重灌后有 6 个真值，匹配得出东西。",
        "sql": """
            SELECT count(*) AS hits
            FROM push_notifications
            WHERE failure_reason LIKE '%token%'
        """,
    },
    {
        "key": "window-rows",
        "shape": "窗口函数累计求和。只能用 ROWS 帧——Redshift 不实现 RANGE 帧，"
                 "也拒绝无帧的聚合窗口函数，所以标准默认帧在三家里不可移植。",
        "sql": """
            SELECT d, SUM(g) OVER (ORDER BY d
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running
            FROM (SELECT CAST(placed_at AS DATE) AS d, SUM(actual_amount) AS g
                  FROM orders GROUP BY CAST(placed_at AS DATE)) t
            ORDER BY d
        """,
    },
    {
        "key": "join-3way",
        "shape": "三表 JOIN + 去重计数，是这个 demo 的招牌问题（分渠道 GMV）。"
                 "最接近 agent 真的会写出来的那种 SQL。",
        "sql": """
            SELECT ch.channel_name,
                   count(DISTINCT o.user_id) AS buyers,
                   SUM(o.actual_amount) AS gmv
            FROM orders o
            JOIN user_attributions ua ON o.user_id = ua.user_id
            JOIN channels ch ON ua.channel_id = ch.channel_id
            WHERE ua.attribution_type = 'last_touch'
            GROUP BY ch.channel_name
            ORDER BY gmv DESC, ch.channel_name
        """,
    },
]


def sql_for(q: dict, arm: str) -> str:
    return " ".join((q.get("by_arm", {}).get(arm) or q["sql"]).split())


# ------------------------------------------------------------------ 统计

def summarize(samples: list[float]) -> dict:
    """一组样本 → 中位数 / min / max / 相对离散度。

    中位数而不是平均值：一次网络抖动能把平均值带跑，中位数不会。样本数是偶数时
    `statistics.median` 取中间两个的平均，那是标准做法，这里不改。

    `spread` = (max-min)/median，用来回答「这个数稳不稳」。它大到某个程度时，
    两条 arm 的中位数差个百分之几就没有意义了——**报告里必须能看见这件事**，
    否则读者会把噪声当结论。
    """
    if not samples:
        return {"n": 0}
    med = statistics.median(samples)
    return {
        "n": len(samples),
        "median_ms": round(med, 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "spread": round((max(samples) - min(samples)) / med, 3) if med else 0.0,
    }


def cost_of(arm: str, wall_s: float, engine_s: float,
            bytes_scanned: int | None, prices: dict,
            requests: int | None = None) -> tuple[float | None, str]:
    """单条查询**隔离**跑的成本。三家形状不同，说明串一起返回。

    Redshift 用引擎自报时长而不是墙上时钟：计费按服务端执行算，网络往返不收钱。
    Athena 用扫描字节，与时长无关——**它是三家里唯一"跑得慢不额外收钱"的**。
    DuckDB 算的是承载它的那个任务：**这一栏的前提是每条查询单独来一次，所以每条
    都要先起一个全新任务**，成本 = 开机 `DUCKDB_BOOT_SECONDS` + 这条自己的执行时长，
    不足 60 秒按 Fargate 的一分钟起步价收，再加它自己发的 S3 请求费。

    开机那一段 2026-09-07 之前漏了，这一栏因此把 DuckDB 报低了约 52 倍
    （$0.000606 对 $0.031344）。漏的形态值得记一下：右边那一栏（连着跑一遍）
    是含开机的，于是同一张表里两条 arm 的口径不一致——托管服务没有开机这一项，
    进程内引擎有，把它省掉等于让 DuckDB 白拿一次热身。这一栏也因此不该套
    「起步价按任务只收一次」那条：八条各自单独来就是八个任务，收八次才对。

    `requests` 是 `Arm.count_http()` 数出来的请求数。给了就把请求费算进去，
    没给就在说明里写明这一项没算——这个漏算偏向 DuckDB，不能悄悄留着。
    """
    if arm == "athena":
        if bytes_scanned is None:
            return None, "拿不到扫描字节"
        return PR.athena_cost(bytes_scanned, prices)
    if arm == "redshift":
        return PR.redshift_cost(engine_s or wall_s, prices)
    boot = PR.DUCKDB_BOOT_SECONDS
    c, note = PR.fargate_cost(boot + wall_s, prices, floor=True)
    note = f"单独起一个任务：开机 {boot:.1f}s + 执行 {wall_s:.1f}s；" + note
    if requests is None:
        return c, note + "（未计入 S3 GET 请求费）"
    rc, rnote = PR.s3_get_cost(requests, prices)
    return c + rc, f"{note} + {rnote}"


# ------------------------------------------------------------------ 跑

def tighten_polling() -> None:
    """把两条远程 arm 的轮询间隔调到 `TIMING_POLL_*`。理由见那两个常量的注释。"""
    os.environ["ATHENA_POLL_INITIAL"] = str(TIMING_POLL_INITIAL)
    os.environ["ATHENA_POLL_MAX"] = str(TIMING_POLL_MAX)
    os.environ["REDSHIFT_POLL_INITIAL"] = str(TIMING_POLL_INITIAL)
    os.environ["REDSHIFT_POLL_MAX"] = str(TIMING_POLL_MAX)


def poll_readback(arm: str) -> list[str]:
    """回读客户端模块里真实生效的轮询间隔。

    回读而不是相信刚才那次赋值：`os.environ` 设晚了（模块已经导入过）不会有任何
    报错，只会让间隔保持默认——而那正是「墙上时钟里掺了我们自己的 sleep」这个
    问题的原样复现，且从输出上看不出来。
    """
    mod = {"athena": "athena", "redshift": "rsql"}.get(arm)
    if mod is None:
        return ["轮询：进程内执行，不轮询（这一条 arm 没有这项开销）"]
    m = sys.modules.get(mod)
    if m is None:                       # 还没被惰性导入，说明还没连过，回读不了
        return ["轮询：客户端尚未导入，未回读"]
    got = getattr(m, "POLL_INITIAL", None)
    if got != TIMING_POLL_INITIAL:
        raise RuntimeError(
            f"{arm} 的轮询间隔没收紧：回读到 {got}s，要求 {TIMING_POLL_INITIAL}s。"
            f"\n  说明 {mod} 模块在设环境变量之前就被导入了，"
            f"墙上时钟里会掺进 {got}s 的 sleep，而这个数在三条 arm 上不同。")
    return [f"轮询 {got*1000:.0f}ms 起步 / "
            f"{getattr(m, 'POLL_MAX', 0)*1000:.0f}ms 上限（模块回读）"]


def fmt_bytes(n: int | None) -> str:
    """字节数按量级选单位。75,196 字节写成 `0.0MB` 等于把剪枝效果擦掉了。"""
    if n is None:
        return "—"
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n/1024:.1f}KB"
    if n < 1024**3:
        return f"{n/1024**2:.1f}MB"
    return f"{n/1024**3:.2f}GB"


def run_query(arm: A.Arm, sql: str) -> dict:
    """跑一条，返回墙上时钟、引擎自报时长、扫描字节。错误当结果返回，不抛。"""
    t0 = time.perf_counter()
    try:
        r = arm.client.execute(sql)
    except Exception as e:                                  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300],
                "wall_ms": (time.perf_counter() - t0) * 1000}
    wall = (time.perf_counter() - t0) * 1000
    return {
        "wall_ms": wall,
        "engine_ms": float(r.get("elapsed_ms") or 0),
        "bytes_scanned": r.get("bytes_scanned"),
        "rows": r.get("rows") or [],
        # DuckDB 重连过的那一条：耗时含重连和两次执行，不能当样本。见 conn.py 第 7 条。
        "reconnected": bool(r.get("reconnected")),
        "error": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="三条 arm 的耗时与成本对比")
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS,
                    help=f"热身之外的计时次数，默认 {DEFAULT_REPS}")
    ap.add_argument("--arm", action="append", default=[], choices=list(A.ARMS))
    ap.add_argument("-k", "--key", action="append", default=[],
                    help="按 key 子串筛查询")
    ap.add_argument("--list", action="store_true", help="列出查询集，不跑")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="统计与成本逻辑自测，不连引擎")
    ap.add_argument("--render", metavar="TRACE",
                    help="从已有 trace 重出报告，不连引擎、不花钱")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.render:
        # 报告的表述会改，测量结果不会。改一次呈现就重跑一遍云上查询是白花钱，
        # 所以 trace 里存的是**原始测量**，报告是它的函数。
        lines = [json.loads(l) for l in Path(a.render).read_text().splitlines() if l]
        head = next(d for d in lines if d["kind"] == "run")
        end = next((d for d in lines if d["kind"] == "end"), {})
        rows = [d for d in lines if d["kind"] == "query"]
        print(f"从 trace 重出报告：{a.render}")
        print(f"测量时刻 {head['started_at']}  主机 {head.get('host')}  "
              f"reps={head['reps']}")
        for name, ks in (head.get("knobs") or {}).items():
            for line in ks:
                print(f"  {name:<9} {line}")
        report(rows, head["prices"], end.get("batch_seconds", 0.0),
               head["arms"], head.get("host", "(trace 里没记)"),
               end.get("attach_http"))
        return 0

    qs = QUERIES
    if a.key:
        qs = [q for q in qs if any(k in q["key"] for k in a.key)]
    if not qs:
        print("按这些条件一个查询都没选中")
        return 2

    if a.list:
        print(f"查询集 {len(qs)} 条：\n")
        for q in qs:
            print(f"  {q['key']}\n    {q['shape']}")
        n = len(qs) * (a.reps + 1) * len(a.arm or A.ARMS)
        print(f"\n全量一轮 = {len(qs)} 条 × ({a.reps} 次计时 + 1 次热身) × "
              f"{len(a.arm or A.ARMS)} arm = {n} 次执行")
        return 0

    prices = PR.load()
    armlist = A.all_arms(a.arm or None)
    print(f"arm：{'、'.join(x.name for x in armlist)}    查询 {len(qs)} 条    "
          f"每条热身 1 次 + 计时 {a.reps} 次")
    print(f"单价取自 {prices['source']}，取数时刻 {prices['fetched_at']}\n")

    # 收紧轮询必须在任何客户端**被创建之前**：athena / rsql 在模块导入时读这两个
    # 环境变量，而 arms.py 是惰性导入它们的，所以这里设了才来得及。
    tighten_polling()

    # 计时前必须关掉只有某一条 arm 有的加速，并把回读结果记进 trace。
    # 这一步是 `configure_for_timing` 存在的全部理由。
    print("关加速（回读确认）：")
    knobs: dict[str, list[str]] = {}
    for x in armlist:
        knobs[x.name] = x.configure_for_timing()
        knobs[x.name] += poll_readback(x.name)
        for line in knobs[x.name]:
            print(f"  {x.name:<9} {line}")
    print()

    trace = tp = None
    if not a.no_trace:
        C.TRACE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tp = C.TRACE_DIR / f"timing-{stamp}.jsonl"
        trace = tp.open("w", encoding="utf-8")
        trace.write(json.dumps({
            "kind": "run", "layer": "timing", "started_at": stamp,
            "arms": [x.name for x in armlist], "queries": len(qs),
            "reps": a.reps, "knobs": knobs,
            "prices": prices, "host": os.uname().nodename,
            # 这份 trace 的耗时**是**性能数字，与 correctness 那份刻意相反
            "timing_valid_for_perf": True,
        }, ensure_ascii=False) + "\n")

    batch_t0 = time.time()
    #: 花在「测量本身」上的秒数（数 HTTP 请求那几次执行）。装在 list 里是为了让
    #: 下面的循环能就地累加。它从整批时长里扣掉，见累加处的注释。
    aux_s = [0.0]
    results: list[dict] = []
    bad = 0

    for q in qs:
        print(f"── {q['key']}")
        # 第 1 条规矩：先验三条 arm 答案一致，否则不报耗时。
        answers, first = {}, {}
        for x in armlist:
            r = run_query(x, sql_for(q, x.name))
            first[x.name] = r
            if r["error"]:
                answers[x.name] = None
            else:
                answers[x.name] = QC.rows_key(r["rows"], ordered=False)
        errs = {k: v["error"] for k, v in first.items() if v["error"]}
        if errs:
            bad += 1
            for k, v in errs.items():
                print(f"   ❌ {k} 跑不动：{v}")
            results.append({"key": q["key"], "skipped": "有 arm 报错", "errors": errs})
            continue
        # 空结果集不是基准。一条 WHERE 条件写歪、把所有分区都剪掉的查询，
        # 三条 arm 会**一致地**返回 0 行——一致性闸门放它过去，而它量的是
        # 「读元数据要多久」。所以这里单独拦一次。
        empty = [k for k, v in answers.items() if not v]
        if empty:
            bad += 1
            print(f"   ❌ 结果集是空的（{'、'.join(empty)}），**不报耗时**："
                  f"三条 arm 一致地返回 0 行时一致性闸门拦不住，"
                  f"而空查询量的是读元数据的时间")
            results.append({"key": q["key"], "skipped": "结果集为空"})
            continue
        ref_name, ref = next(iter(answers.items()))
        diff = [k for k, v in answers.items() if v != ref]
        if diff:
            bad += 1
            print(f"   ❌ 结果不一致（{ref_name} ⟷ {'、'.join(diff)}），"
                  f"**不报耗时**：答案不同的两条查询比耗时是比两个不同的问题")
            results.append({"key": q["key"], "skipped": "结果不一致",
                            "row_counts": {k: len(v) if v else 0
                                           for k, v in answers.items()}})
            continue

        row = {"key": q["key"], "shape": q["shape"], "arms": {}}
        for x in armlist:
            warm = first[x.name]                    # 热身那一次就是刚跑的这一次
            samples, engine, scanned, dropped = [], [], [], 0
            for _ in range(a.reps):
                r = run_query(x, sql_for(q, x.name))
                if r["error"]:
                    print(f"   ⚠️  {x.name} 第 {len(samples)+1} 次报错：{r['error']}")
                    continue
                if r["reconnected"]:
                    dropped += 1                    # 见 conn.py 第 7 条
                    continue
                samples.append(r["wall_ms"])
                engine.append(r["engine_ms"])
                if r["bytes_scanned"] is not None:
                    scanned.append(int(r["bytes_scanned"]))
            st = summarize(samples)
            med_wall = st.get("median_ms", 0.0)
            med_eng = statistics.median(engine) if engine else 0.0
            med_bytes = int(statistics.median(scanned)) if scanned else None
            # 请求数单独再跑，**不进计时样本**：开 HTTP 日志本身有开销。
            # 冷热两个数都要，理由见 arms.Arm.count_http——它们差一个量级，
            # 而单条隔离成本该按冷态算（每次问的都是新问题）。
            # 数不到就当没有，成本说明里会写明这一项没算，不静默按 0 处理。
            http = http_cold = None
            for label, kw in (("热", {}), ("冷", {"cold": True})):
                aux_t0 = time.time()
                try:
                    got = x.count_http(sql_for(q, x.name), **kw)
                except Exception as e:                          # noqa: BLE001
                    print(f"             ⚠️  {x.name} {label}态请求数没数到："
                          f"{type(e).__name__}: {e}"[:160])
                    continue
                finally:
                    # 数请求的这几次执行**不算进整批时长**：摊薄成本是整批墙钟的
                    # 函数，把测量动作算进去会让 Redshift 的金额随「我们多测了什么」
                    # 上涨——那不是它的成本。实测这一项给整批加了约 36s。
                    aux_s[0] += time.time() - aux_t0
                if kw:
                    http_cold = got
                else:
                    http = got
            cost, cnote = cost_of(
                x.name, med_wall / 1000, med_eng / 1000, med_bytes, prices,
                requests=(http_cold or http or {}).get("total")
                if (http_cold or http) else None)
            row["arms"][x.name] = {
                **st,
                "first_ms": round(warm["wall_ms"], 1),
                "engine_median_ms": round(med_eng, 1),
                "bytes_scanned": med_bytes,
                "http_requests": http,
                "http_requests_cold": http_cold,
                "cost_isolated_usd": cost,
                "cost_note": cnote,
                "dropped_reconnect": dropped,
            }
            tail = f"  扫描 {fmt_bytes(med_bytes)}" if med_bytes is not None else ""
            if http or http_cold:
                tail += ("  请求 冷 %s / 热 %s 次" % (
                    http_cold["total"] if http_cold else "—",
                    http["total"] if http else "—"))
            # 差额 = 提交 + 轮询 + 取结果的往返，是这条 arm 真实的接口开销。
            # 单独印出来，否则读者会把它算进引擎的账上。
            gap = f"  接口开销 {med_wall - med_eng:+.0f}ms" if med_eng else ""
            print(f"   {x.name:<9} 中位 {med_wall:8.1f}ms  "
                  f"（首次 {warm['wall_ms']:.0f}ms  引擎自报 {med_eng:.0f}ms{gap}  "
                  f"离散 {st.get('spread', 0):.0%}）{tail}")
            if dropped:
                print(f"             丢弃 {dropped} 个样本（DuckDB 重连过，耗时不可比）")
        results.append(row)
        if trace:
            trace.write(json.dumps({"kind": "query", **row},
                                   ensure_ascii=False) + "\n")

    wall_total = time.time() - batch_t0
    batch_s = wall_total - aux_s[0]
    if aux_s[0] > 0:
        print(f"\n整批墙上时长 {batch_s:.1f}s"
              f"（实际跑了 {wall_total:.1f}s，扣掉数请求的 {aux_s[0]:.1f}s——"
              f"那是测量动作，不是被测的工作量）")
    else:
        print(f"\n整批墙上时长 {batch_s:.1f}s")

    # 连接 + ATTACH 那一笔请求最后量：它另建一条临时连接，放在计时之前会白白
    # 多打一遍 catalog，放在这里对上面的样本没有任何影响。
    attach_http = None
    for x in armlist:
        try:
            attach_http = x.count_http_attach() or attach_http
        except Exception as e:                                  # noqa: BLE001
            print(f"⚠️  {x.name} 的 ATTACH 请求数没数到："
                  f"{type(e).__name__}: {e}"[:160])

    report(results, prices, batch_s, [x.name for x in armlist],
           os.uname().nodename, attach_http)

    if trace:
        trace.write(json.dumps({"kind": "end", "batch_seconds": round(batch_s, 1),
                                "wall_seconds_total": round(wall_total, 1),
                                "measurement_overhead_seconds": round(aux_s[0], 1),
                                "queries_reported": sum(1 for r in results
                                                        if "arms" in r),
                                "attach_http": attach_http},
                               ensure_ascii=False) + "\n")
        trace.close()
        print(f"\ntrace：{tp}")
        uri = C.upload_trace(tp)
        if uri:
            print(f"      {uri}")

    if bad:
        print(f"\n{bad} 条查询没能进入耗时对比 ❌")
        return 1
    return 0


def amortized(ok: list[dict], prices: dict,
              armnames: list[str]) -> tuple[dict, dict, dict]:
    """整批摊薄那一栏 → (每条 arm 的钱, 每条 arm 自身耗时秒, 每条 arm 的说明)。

    拆成纯函数是为了能被自测钉住。它原来在 `report()` 里内联，三个数全是拿 `batch_s`
    （整批墙钟）算的，而那**同时**是三个错：

    - `batch_s` 覆盖的是「三条 arm × 每条查询 × (热身 + N 遍)」的总时长。拿它算
      Redshift 的 RPU-秒，等于让 Redshift 为 Athena 和 DuckDB 跑的时间付钱；同一个数
      又整份记给了 Fargate，于是两条 arm 各自被收了整批的钱，加起来超过 100%。
    - Athena 那一格是另一种错：它把每条查询的字节**求和之后**只落一次 10MB 起步价。
      Athena 的起步价是**每条查询**收的，与时长无关，所以它压根没有可摊薄的东西——
      这一栏对它按构造等于隔离栏。

    现在的口径是「把这批查询连着跑一遍」：各 arm 用**自己**的中位耗时求和（与隔离栏
    同一个中位数，所以两栏之差只剩起步价落在哪里，那正是这两栏想分开的东西），起步价
    按一遍算一次。由此得到一条可断言的性质，也是自测钉的那条：**任何一条 arm 的金额
    都不随另一条 arm 的耗时变化。**

    三条 arm 的起步价单位各不相同，这一栏之所以要一条条写清楚，就是因为这个：
    Athena 按**查询**（每条 10MB，摊不掉）、Redshift 按**活动分钟**、Fargate 按**任务**
    （60s，一遍就是一个任务，收一次）。当前规模下后两条都没跑满 60 秒，也就是说这两条 arm
    的整批成本**全部由起步价决定**，与查询快慢无关——这是结论的一部分，不是四舍五入。

    Redshift 那一格只能报**下界**：活动分钟数取决于查询落在挂钟的哪一段，跨边界就多收
    一段，活动后紧邻的空闲分钟也照收，这些从「自身耗时之和」推不出来。2026-09-07 与
    `sys_serverless_usage` 对过一次，实收是模型的 3 倍。账单侧真值只有
    `cost_cold.py --settle` 给得出，这一栏负责的是「同一口径下三条 arm 怎么比」。
    """
    own_s = {n: sum(r["arms"].get(n, {}).get("median_ms", 0.0) for r in ok) / 1000
             for n in armnames}
    amort: dict[str, float] = {}
    why: dict[str, str] = {}
    for n in armnames:
        if n == "athena":
            per = [PR.athena_cost(r["arms"][n].get("bytes_scanned") or 0, prices)[0]
                   for r in ok if n in r["arms"]]
            b = sum(r["arms"][n].get("bytes_scanned") or 0 for r in ok
                    if n in r["arms"])
            floored = sum(1 for r in ok if n in r["arms"]
                          and (r["arms"][n].get("bytes_scanned") or 0)
                          < PR.ATHENA_MIN_BYTES)
            amort[n] = sum(per)
            why[n] = (f"{len(per)} 条各收一次 10MB 起步价（其中 {floored} 条整条成本就是"
                      f"起步价），整批扫描 {fmt_bytes(b)}；与隔离栏相等——按字节计费"
                      f"没有时长可摊")
        elif n == "redshift":
            # **这一格是下界，不是账单。** 60s 下限的单位是「活动分钟」而不是「一遍」：
            # 跨了挂钟的分钟边界就多收一段，活动后紧邻的空闲分钟也照收。落到几段上取决
            # 于查询在挂钟上的落点，从自身耗时推不出来，所以这里只能按「一遍一段」报，
            # 并把 2026-09-07 对过的账单倍数一起写出来——不写的话这一栏会被当成账单读。
            amort[n] = PR.redshift_cost(own_s[n], prices)[0]
            why[n] = (f"自身 {own_s[n]:.1f}s × {PR.REDSHIFT_RPU} RPU，"
                      f"60s 起步价按一段算"
                      + ("（这一遍还没跑满 60s，所以整栏就是那一次起步价）"
                         if own_s[n] < PR.REDSHIFT_MIN_SECONDS else "")
                      + f"；**这是下界**——下限按活动分钟收，跨分钟就多一段，"
                        f"2026-09-07 对账实收 "
                        f"{PR.REDSHIFT_BILLED_OVER_MODEL:.1f}× "
                        f"（≈${amort[n] * PR.REDSHIFT_BILLED_OVER_MODEL:.6f}），"
                        f"账单侧真值走 cost_cold.py --settle")
        else:
            # `floor=True`：一遍就是一个任务，而 Fargate 每个任务至少收 1 分钟。
            # 不套的话这一栏会报出「租 0.9 秒任务」的价，而那种任务租不到——同一类
            # 错误 Athena 那一格是漏收起步价，这里是漏收得更狠（时长差了 60 倍）。
            # 起步价的单位是任务，所以套几次取决于那一栏问的是几个任务：这一栏八条
            # 共用一个任务，收一次；隔离栏八条各自单独来、各起一个任务，收八次。
            # 这一栏也因此**只加一次开机**（开机含在下面 setup_s 覆盖的那一段里），
            # 按条加等于把一次拉镜像收八遍。
            base = PR.fargate_cost(own_s[n], prices, floor=True)[0]
            floored_fg = own_s[n] < PR.FARGATE_MIN_SECONDS
            # 请求费不随摊薄变少：查询发了多少次请求就是多少次，与计费时段无关。
            # 一遍的请求数按「第一条冷读、其余热读」算 —— 每条查询在一遍里只执行一次，
            # 原来那样把每条的冷态数和热态数**相加**，等于每条都跑了两遍。这仍是近似：
            # 哪张表在第几条查询上才被缓存上，取决于查询顺序。
            counted = [(r["arms"][n].get("http_requests_cold"),
                        r["arms"][n].get("http_requests"))
                       for r in ok if n in r["arms"]]
            reqs = 0
            for i, (hc, hw) in enumerate(counted):
                pick = hc if i == 0 else (hw or hc)
                reqs += (pick or {}).get("total", 0)
            fg = (f"任务存活 {own_s[n]:.1f}s"
                  + (f"，按 {PR.FARGATE_MIN_SECONDS}s 起步价计费（任务租不到更短的，"
                     f"这一栏的时长成本就是那一次起步价）" if floored_fg else ""))
            if any(hc or hw for hc, hw in counted):
                amort[n] = base + PR.s3_get_cost(reqs, prices)[0]
                why[n] = (f"{fg} + 一遍约 {reqs} 次 S3 请求"
                          f"（首条冷读、其余热读；折算，本机不计费）")
            else:
                amort[n] = base
                why[n] = f"{fg}（折算，本机不计费；请求费没数到）"
    return amort, own_s, why


def report(results: list[dict], prices: dict, batch_s: float,
           armnames: list[str], host: str = "",
           attach_http: dict | None = None) -> None:
    """两栏成本，理由见 docstring 第 4 条。"""
    ok = [r for r in results if "arms" in r]
    if not ok:
        print("没有可报的查询")
        return

    print("\n耗时中位数（客户端墙上时钟，ms）")
    w = max(len(r["key"]) for r in ok)
    print(f"  {'查询':<{w}}  " + "  ".join(f"{n:>12}" for n in armnames))
    for r in ok:
        cells = []
        for n in armnames:
            v = r["arms"].get(n, {})
            cells.append(f"{v.get('median_ms', 0):>12.1f}")
        print(f"  {r['key']:<{w}}  " + "  ".join(cells))

    print("\n单条隔离成本（USD，含各家起步价——对应"
          "「偶尔问一个问题」这种用法）")
    tot = {n: 0.0 for n in armnames}
    for r in ok:
        cells = []
        for n in armnames:
            c = r["arms"].get(n, {}).get("cost_isolated_usd")
            cells.append("           —" if c is None else f"{c:>12.6f}")
            tot[n] += c or 0.0
        print(f"  {r['key']:<{w}}  " + "  ".join(cells))
    print(f"  {'合计':<{w}}  " + "  ".join(f"{tot[n]:>12.6f}" for n in armnames))

    amort, own_s, why_by_arm = amortized(ok, prices, armnames)
    print("\n整批摊薄成本（USD，把这批查询连着跑一遍——对应「跑一个作业」这种用法）")
    print("  口径：各 arm 用自己的中位耗时求和（"
          + "、".join(f"{n} {own_s[n]:.1f}s" for n in armnames)
          + f"），起步价按一遍算一次。整批实测墙钟 {batch_s:.0f}s 是**测量**用掉的时间"
            f"（三条 arm 轮着跑、每条查询还跑了热身加多遍），不摊给任何一条 arm。")
    for n in armnames:
        print(f"  {n:<10} {amort[n]:>12.6f}   {why_by_arm[n]}")

    if "athena" in armnames:
        scans = [r["arms"]["athena"].get("bytes_scanned") for r in ok
                 if "athena" in r["arms"]]
        scans = [s for s in scans if s is not None]
        under = [s for s in scans if s < PR.ATHENA_MIN_BYTES]
        if scans:
            print(f"\nAthena 扫描量：最小 {fmt_bytes(min(scans))}、"
                  f"最大 {fmt_bytes(max(scans))}，其中 {len(under)}/{len(scans)} 条"
                  f"低于 10MB 起步价。")
            if len(under) == len(scans):
                print("  **这个数据量下 Athena 的每条查询成本全部等于起步价**，"
                      "所以那一栏数字彼此相同不是巧合，\n"
                      "  也不代表「几种查询一样贵」——它代表这个规模还没进入"
                      "按量计费的区间。Athena 的成本要到\n"
                      "  单条查询扫描超过 10MB 才开始体现差异，"
                      "而列式 + 分区剪枝把这些查询压到了那条线以下。")

    requests_report(ok, armnames, prices, attach_http)

    print("\n两栏为什么会差这么多：起步价在单条隔离那一栏里被收了"
          f" {len(ok)} 次，而连着跑一遍只收一次。Redshift 是 60 秒 × 8 RPU 收"
          f" {len(ok)} 次；DuckDB 是「起一个新任务」收 {len(ok)} 次，"
          f"每次含 {PR.DUCKDB_BOOT_SECONDS:.1f}s 开机加一分钟起步价。"
          "\nAthena 两栏相等，因为它的起步价单位就是一条查询，连着跑没有可共享的部分。"
          "\n把「单条隔离」当成日常成本会高估 Redshift 与 DuckDB，"
          "把「连着跑一遍」当成偶发查询的成本会低估它们——两种用法本来就不同价。")

    decomposition(ok, armnames)
    ranking_report(ok, armnames)
    venue_note(host)


def requests_report(ok: list[dict], armnames: list[str], prices: dict,
                    attach_http: dict | None) -> None:
    """DuckDB 的 S3 请求数与它折成的钱。

    这一段存在的理由是它以前**不存在**：DuckDB 那一栏的成本只算了折算的 Fargate
    时长，没算它自己发的 S3 请求费，而漏算的方向恰好让 DuckDB 显得更便宜。
    Athena 和 Redshift 这里是空的不是漏测——它们的请求费在 $5/TB 和 RPU-秒里，
    客户端也看不到服务端发的请求。
    """
    rows = [(r["key"], r["arms"]["duckdb"].get("http_requests_cold"),
             r["arms"]["duckdb"].get("http_requests")) for r in ok
            if "duckdb" in r["arms"]
            and (r["arms"]["duckdb"].get("http_requests_cold")
                 or r["arms"]["duckdb"].get("http_requests"))]
    if not rows:
        if "duckdb" in armnames:
            print("\nDuckDB 的 S3 请求数这次没数到，成本栏里缺请求费这一项。")
        return
    rate = PR.usd(prices, "s3_per_get_request")
    print(f"\nDuckDB 的 S3 请求数（实测，按 S3 GET Tier-2 单价 ${rate:.9f}/次折算）"
          f"\n  冷＝空缓存的临时连接，对应「每次问的都是新问题」，agent 就是这种用法；"
          f"\n  热＝上面那条跑计时的连接，对象缓存已经装着列块，对应同一批查询反复跑。"
          f"\n  请求数是数出来的，单价是**借用**普通 S3 的 GET 价："
          f"这些请求打的是 S3 Tables 的数据端点，"
          f"\n  它自己的请求价目本项目没取，所以这一栏的金额按量级读，别当账单。")
    w = max(len(k) for k, _, _ in rows)

    def cell(h):
        return (f"{h['total']:>4} 次（对象 {h['object']:>3} / catalog {h['catalog']:>2}）"
                if h else f"{'—':>4}    {'':<21}")

    cold_t = warm_t = 0
    print(f"  {'查询':<{w}}  {'冷':<34}  {'热':<34}  冷态费用")
    for key, hc, hw in rows:
        cold_t += (hc or {}).get("total", 0)
        warm_t += (hw or {}).get("total", 0)
        print(f"  {key:<{w}}  {cell(hc)}  {cell(hw)}  "
              f"${(hc or {}).get('total', 0) * rate:.8f}")
    print(f"  {'合计':<{w}}  {cold_t:>4} 次{'':<27}  {warm_t:>4} 次{'':<27}  "
          f"${cold_t * rate:.8f}")
    if cold_t and warm_t:
        print(f"  冷热差 {cold_t / max(warm_t, 1):.0f}× ——"
              f"`enable_object_cache` 把重复读的列块留在内存里，所以热态几乎不发对象请求。"
              f"\n  单条隔离那一栏按**冷态**算，整批摊薄那一栏按热态算："
              f"拿热态去算单条成本等于把 DuckDB 的请求费抹成零。")
    if attach_http:
        print(f"  另有连接 + ATTACH {attach_http['total']} 次"
              f"（对象 {attach_http['object']} / catalog {attach_http['catalog']}）"
              f"  ${attach_http['total'] * rate:.8f}"
              f"\n  这一笔每进程一次，不摊到查询上；它属于冷启动那一下的开销。")


def decomposition(ok: list[dict], armnames: list[str]) -> None:
    """把墙上时钟拆成「引擎」和「访问路径」两段。

    这一段是整个对比里最容易被读反的地方，所以它必须自动印出来而不是靠人记住：
    同区实测，Redshift 的引擎自报 48–81ms，**是三条 arm 里最快的**，比 DuckDB 的
    44–229ms 还稳；但它每条查询要付约 0.5s 的 Data API 往返（提交 + 轮询 + 取结果，
    三次 HTTPS），于是端到端反而排第三。DuckDB 这一段恒为 0，因为它是进程内引擎。

    也就是说「谁最快」这句话的答案取决于量的是引擎还是访问路径，而两者在这份数据
    规模上**不同数量级**：引擎差几十毫秒，访问路径差几百毫秒。选型时该问的不是
    「哪个引擎快」，是「这个查询量级下，访问路径的固定开销会不会盖掉引擎的差异」。
    """
    print("\n墙上时钟拆成两段（引擎自报 / 访问路径）")
    for n in armnames:
        rs = [r["arms"][n] for r in ok if n in r["arms"] and r["arms"][n].get("n")]
        if not rs:
            continue
        eng = [v["engine_median_ms"] for v in rs]
        ov = [v["median_ms"] - v["engine_median_ms"] for v in rs]
        if max(eng) == 0:                       # 这条 arm 没自报引擎时间
            print(f"  {n:<10} 引擎自报缺失，拆不开")
            continue
        path = ("进程内，无访问路径开销" if max(ov) < 5
                else f"访问路径 {min(ov):.0f}–{max(ov):.0f}ms")
        print(f"  {n:<10} 引擎 {min(eng):.0f}–{max(eng):.0f}ms   {path}")
    # 名次会不会因为换一段而翻，是由数据算出来的，不是写死的结论。
    by_eng, by_wall = {}, {}
    for n in armnames:
        rs = [r["arms"][n] for r in ok if n in r["arms"] and r["arms"][n].get("n")]
        if rs and max(v["engine_median_ms"] for v in rs) > 0:
            by_eng[n] = statistics.median([v["engine_median_ms"] for v in rs])
            by_wall[n] = statistics.median([v["median_ms"] for v in rs])
    if len(by_eng) >= 2:
        oe = sorted(by_eng, key=by_eng.get)
        ow = sorted(by_wall, key=by_wall.get)
        print(f"  按引擎排：{' < '.join(oe)}")
        print(f"  按墙钟排：{' < '.join(ow)}")
        if oe != ow:
            print("  **两个排法名次不同**——所以「哪条 arm 快」这句话必须说明量的是"
                  "哪一段。\n"
                  "  引擎差异是几十毫秒量级，访问路径差异是几百毫秒量级；"
                  "在这个数据规模下\n"
                  "  后者盖掉了前者，而数据再大十倍时结论可能反过来。")


def venue_note(host: str) -> None:
    """在哪台机器上量的，这件事会系统性地偏向某条 arm，所以必须印在报告里。

    **本机跑的数字不是最终结论**：Athena 和 Redshift 的每一次提交、轮询、取结果
    都要走一趟公网往返（实测接口开销 1.2–1.3s，且它与查询复杂度无关，八条查询上
    几乎是同一个常数），而 DuckDB 的客户端就在本地、没有这一趟。反过来，DuckDB
    从 S3 拉列块也走公网，所以它的**冷**读被同一件事重罚（实测最差 47s）。
    两个方向的偏差不会互相抵消——它们落在不同的格子里。

    在 Fargate 上跑时三条 arm 同区，那趟往返对谁都是同区往返，这才是可比的场地。
    """
    on_fargate = host.startswith("ip-") or bool(os.environ.get("ECS_CONTAINER_METADATA_URI_V4"))
    print(f"\n场地：{host}", end="  ")
    if on_fargate:
        print("（同区，三条 arm 的网络往返口径一致——这是可比的场地）")
        return
    print("（**本机**）")
    print("  本机场地对三条 arm 的偏差方向不同，所以上面的名次只在这个场地成立：")
    print("  · Athena / Redshift 的每次提交-轮询-取结果都走公网往返，"
          "实测接口开销 1.2–1.3s，\n"
          "    而且它在八条查询上近似为常数——也就是说这两条 arm 的耗时里"
          "有一大截量的是网络，不是引擎。")
    print("  · DuckDB 客户端在本地、没有这一趟，但它读 S3 列块同样走公网，"
          "所以冷读被重罚、热读几乎免费。")
    print("  要出可比的数就在同区跑：先 `fargate.py --build`（镜像会带上这个脚本），"
          "再\n    `fargate.py --run -- python scripts/bench/timing.py`。")


def ranking_report(ok: list[dict], armnames: list[str]) -> None:
    """抖动会不会改变名次。

    **不用「离散度超过 50% 就警告」那种判据**，那一版实测在这份数据上把 8 条查询
    里 8 条都标红了，理由是 DuckDB 的中位数只有 250ms：几百毫秒的绝对抖动除以
    250ms 就是百分之一百多，而同一时刻 Athena 是 2300ms——名次根本不受威胁。
    按中位数归一的离散度衡量的是「这条 arm 自己稳不稳」，不是「结论稳不稳」，
    拿它当警告线会把每一条都标红，于是警告等于没有。

    真正该问的是：最快那条 arm 的**最慢一次**，是否仍然快于次快那条的**最快一次**。
    区间不重叠时名次成立（抖动再大也不影响），重叠时不成立（差距再好看也不作数）。
    """
    print("\n名次是否被抖动威胁（比较各 arm 的 [min, max] 区间是否重叠）")
    shaky = 0
    for r in ok:
        got = [(n, r["arms"][n]) for n in armnames
               if n in r["arms"] and r["arms"][n].get("n")]
        if len(got) < 2:
            continue
        got.sort(key=lambda t: t[1]["median_ms"])
        (n1, v1), (n2, v2) = got[0], got[1]
        if v1["max_ms"] < v2["min_ms"]:
            print(f"  {r['key']:<18} ✅ {n1} 最快："
                  f"它最慢的一次 {v1['max_ms']:.0f}ms 仍快于 {n2} 最快的一次 "
                  f"{v2['min_ms']:.0f}ms")
        else:
            shaky += 1
            print(f"  {r['key']:<18} ⚠️  {n1} 与 {n2} 区间重叠"
                  f"（{v1['min_ms']:.0f}–{v1['max_ms']:.0f}ms vs "
                  f"{v2['min_ms']:.0f}–{v2['max_ms']:.0f}ms），第一名不成立")
    if shaky:
        print(f"  {shaky} 条查询的第一名没分出来，样本数不够或抖动太大。")

    print("\n冷启动（首次 vs 稳定态中位数）")
    for n in armnames:
        rs = [r["arms"][n] for r in ok if n in r["arms"] and r["arms"][n].get("n")]
        if not rs:
            continue
        worst = max(rs, key=lambda v: v["first_ms"] / max(v["median_ms"], 1))
        ratio = worst["first_ms"] / max(worst["median_ms"], 1)
        print(f"  {n:<10} 最差一条 {worst['first_ms']:.0f}ms → {worst['median_ms']:.0f}ms"
              f"（{ratio:.0f}×）")
    print("  这一栏决定「用户第一次提问要等多久」，"
          "而它对三条 arm 的成因不同：\n"
          "  Athena 是排队与元数据、Redshift 是 workgroup 恢复、"
          "DuckDB 是从 S3 首次拉取列块。\n"
          "  DuckDB 那个倍数最大，也最容易被误读成「它慢」——"
          "它稳定态是三条里最快的，代价是必须先热起来，\n"
          "  也就是说这条 arm 的体感取决于 Fargate 任务是不是常驻。")


# ---------------------------------------------------------------- 自测（无引擎）

def selftest() -> int:
    bad = 0

    def want(label, got, exp):
        nonlocal bad
        if got != exp:
            bad += 1
            print(f"  FAIL {label}：得到 {got!r}，期望 {exp!r}")

    # 1. 中位数而不是平均值：一个离群点不该把中心带跑
    s = summarize([100.0, 101.0, 102.0, 103.0, 5000.0])
    want("中位数抗离群", s["median_ms"], 102.0)
    if abs(sum([100, 101, 102, 103, 5000]) / 5 - s["median_ms"]) < 100:
        bad += 1
        print("  FAIL 这组样本的平均值和中位数太近，测不出「抗离群」这件事")
    # 1b. 离散度要能把这组标成噪声
    if s["spread"] < 0.5:
        bad += 1
        print(f"  FAIL 含 5000ms 离群点的样本离散度只有 {s['spread']}，报告不会警告")
    # 1c. 稳定样本不该被标成噪声
    if summarize([100.0, 101.0, 102.0])["spread"] > 0.5:
        bad += 1
        print("  FAIL 稳定样本被判成噪声")
    want("空样本", summarize([])["n"], 0)

    # 2. 成本分派：三条 arm 各走各的公式
    P = {"items": {                         # 假价目，自测不连网
        "athena_per_tb_scanned": {"usd": 5.0},
        "redshift_per_rpu_hour": {"usd": 0.36},
        "fargate_per_vcpu_hour": {"usd": 0.04048},
        "fargate_per_gb_hour": {"usd": 0.004445},
    }}
    c_a, _ = cost_of("athena", 10.0, 9.0, 1024**4, P)
    want("athena 按字节", round(c_a, 6), 5.0)
    # 2b. Athena 跑得慢不额外收钱——同字节不同时长同价
    c_a2, _ = cost_of("athena", 999.0, 998.0, 1024**4, P)
    want("athena 与时长无关", round(c_a2, 6), round(c_a, 6))
    # 2c. Redshift 用引擎自报时长（网络往返不收钱）
    c_r1, _ = cost_of("redshift", 300.0, 60.0, None, P)
    c_r2, _ = cost_of("redshift", 60.0, 60.0, None, P)
    want("redshift 只看引擎时长", round(c_r1, 8), round(c_r2, 8))
    # 2d. DuckDB 用墙上时钟，**并且要含单独起一个任务的开机时长**。
    #     这一栏的前提是「这条查询单独来一次」，单独来一次就得先起个任务；
    #     漏掉开机会把它报低约 52 倍，而右边那一栏是含开机的，漏了就两栏不同口径。
    hourly = 4 * 0.04048 + 16 * 0.004445
    c_d, note = cost_of("duckdb", 3600.0, 0.0, None, P)
    want("duckdb 含开机的 1 小时", round(c_d, 6),
         round((PR.DUCKDB_BOOT_SECONDS + 3600.0) / 3600 * hourly, 6))
    for k in ("开机", "S3 GET"):
        if k not in note:
            bad += 1
            print(f"  FAIL duckdb 成本说明里没有 {k!r}——金额的构成就看不出来了")
    # 2d-1. 开机是压倒性的一项，所以一条 0 秒的查询也不可能免费，
    #       而且必须至少是一分钟任务的价（Fargate 起步价，单位是任务）。
    c_d_zero, _ = cost_of("duckdb", 0.0, 0.0, None, P)
    if c_d_zero < PR.fargate_cost(PR.FARGATE_MIN_SECONDS, P)[0] - 1e-12:
        bad += 1
        print("  FAIL duckdb 单条成本低于一分钟任务的价——开机或 60s 起步价漏了")
    # 2d-1b. 八条各自单独来就是八个任务，起步价该收八次，不是一次。
    if round(8 * c_d_zero, 9) <= round(PR.fargate_cost(
            8 * PR.DUCKDB_BOOT_SECONDS, P, floor=True)[0], 9):
        bad += 1
        print("  FAIL 隔离栏把八次开机算成了一个任务——那是连着跑那一栏的口径")
    # 2d-2. 给了请求数就必须把请求费加进去，且说明里不能再写「未计入」
    P2 = {"items": {**P["items"], "s3_per_get_request": {"usd": 4e-7}}}
    c_d0, _ = cost_of("duckdb", 3600.0, 0.0, None, P2, requests=0)
    c_d1, note1 = cost_of("duckdb", 3600.0, 0.0, None, P2, requests=1_000_000)
    want("请求费按次累加", round(c_d1 - c_d0, 6), round(1_000_000 * 4e-7, 6))
    if "未计入" in note1:
        bad += 1
        print("  FAIL 请求数已经数到了，说明里还写着「未计入」")
    if "次 GET" not in note1:
        bad += 1
        print("  FAIL 算了请求费却没在说明里给出请求次数——金额的出处就丢了")
    # 2e. 拿不到扫描字节时返回 None 而不是 0（0 会算出「免费」）
    want("athena 无字节数", cost_of("athena", 1.0, 1.0, None, P)[0], None)

    # 3. 查询集本身
    keys = [q["key"] for q in QUERIES]
    want("key 不重复", len(keys), len(set(keys)))
    for q in QUERIES:
        if not q.get("shape"):
            bad += 1
            print(f"  FAIL {q['key']} 没写 shape——挑这条查询的理由就丢了")
        if "sql" not in q and "by_arm" not in q:
            bad += 1
            print(f"  FAIL {q['key']} 既没有 sql 也没有 by_arm")
    # 3b. 方言互不串味，与 arms.py 同一种检查
    for forbidden, wrong in (("format_datetime", "duckdb"), ("strftime", "athena"),
                             ("strftime", "redshift"), ("TO_CHAR", "athena"),
                             ("TO_CHAR", "duckdb"), ("::", "athena")):
        for q in QUERIES:
            if forbidden in sql_for(q, wrong):
                bad += 1
                print(f"  FAIL {q['key']} 在 {wrong} 上出现了别家写法 {forbidden!r}")
    # 3c. 不能出现 Redshift 不实现的窗口帧写法。**这一条是实测出来的**：
    #     Redshift 既没有 RANGE 帧，也拒绝"有 ORDER BY 无帧"的聚合窗口函数。
    for q in QUERIES:
        s = sql_for(q, "redshift").upper()
        if "RANGE BETWEEN" in s:
            bad += 1
            print(f"  FAIL {q['key']} 用了 RANGE 帧，Redshift 跑不动")
        if "OVER (" in s and "ROWS BETWEEN" not in s:
            bad += 1
            print(f"  FAIL {q['key']} 有窗口函数但没显式 ROWS 帧，Redshift 会拒")
    # 3d. 带 LIMIT 的排序必须有唯一的 tiebreak，否则并列行的顺序由引擎定，
    #     会量出假的"结果不一致"。
    for q in QUERIES:
        s = sql_for(q, "athena").upper()
        if "LIMIT" in s and "ORDER BY" in s:
            tail = s.split("ORDER BY")[-1]
            if tail.count(",") < 1:
                bad += 1
                print(f"  FAIL {q['key']} 有 LIMIT 但 ORDER BY 只有一个键，"
                      f"并列行顺序不确定")

    # 3e. 字节格式化：75KB 不能印成 0.0MB（那等于把剪枝效果擦掉）
    want("75KB", fmt_bytes(75196), "73.4KB")
    want("4MB", fmt_bytes(4102190), "3.9MB")
    want("None", fmt_bytes(None), "—")
    if fmt_bytes(75196).startswith("0."):
        bad += 1
        print("  FAIL 75KB 印成了 0.x——量级信息丢了")

    # 3f. 轮询回读：模块没收紧时必须抛，不能默默用默认间隔
    import types
    fake = types.SimpleNamespace(POLL_INITIAL=0.4, POLL_MAX=3.0)
    sys.modules["rsql"] = fake
    try:
        poll_readback("redshift")
        bad += 1
        print("  FAIL 轮询没收紧竟然没抛——墙上时钟里会掺 400ms 的 sleep")
    except RuntimeError:
        pass
    fake.POLL_INITIAL = TIMING_POLL_INITIAL
    if "回读" not in poll_readback("redshift")[0]:
        bad += 1
        print("  FAIL 收紧后的回读没报告实际值")
    del sys.modules["rsql"]
    if "不轮询" not in poll_readback("duckdb")[0]:
        bad += 1
        print("  FAIL duckdb 该说明自己没有轮询开销")

    # 4. 结果集比法就是 query_correctness 那一把尺（不另造）
    r1 = QC.rows_key([(1, "a"), (2, "b")], ordered=False)
    r2 = QC.rows_key([(2, "b"), (1, "a")], ordered=False)
    want("无序比较相等", r1, r2)
    want("3 与 3.5 不等",
         QC.rows_key([(3,)], False) == QC.rows_key([(3.5,)], False), False)

    # 5. 整批摊薄那一栏。钉的是**串台**：这一栏原来三个数全用整批墙钟算，于是每条 arm
    #    都在为另两条的时间付钱。核心性质只有一条——一条 arm 的金额不能随另一条的耗时动。
    P3 = {"items": {**P["items"], "s3_per_get_request": {"usd": 4e-7}}}
    MB = 1024 * 1024

    def rows(rs_ms, dk_ms, at_bytes):
        return [{"key": f"q{i}", "arms": {
            "athena": {"median_ms": 1000.0, "bytes_scanned": b},
            "duckdb": {"median_ms": dk_ms},
            "redshift": {"median_ms": rs_ms},
        }} for i, b in enumerate(at_bytes)]

    names = ["athena", "duckdb", "redshift"]
    a1, own1, _ = amortized(rows(500.0, 100.0, [1 * MB] * 8), P3, names)
    # 5a. 自身耗时是各算各的，不是共用一个墙钟
    want("自身耗时 redshift", round(own1["redshift"], 3), 4.0)
    want("自身耗时 duckdb", round(own1["duckdb"], 3), 0.8)
    # 5b. 把 Redshift 的耗时放大 20 倍，另两条 arm 的金额必须一个字节都不动。
    #     这正是旧实现做不到的事。
    a2, own2, _ = amortized(rows(10_000.0, 100.0, [1 * MB] * 8), P3, names)
    want("redshift 变慢不影响 athena", a2["athena"], a1["athena"])
    want("redshift 变慢不影响 duckdb", a2["duckdb"], a1["duckdb"])
    if a2["redshift"] <= a1["redshift"]:
        bad += 1
        print("  FAIL redshift 自己慢了 20 倍，它自己的摊薄成本竟然没涨")
    # 5c. 反过来：DuckDB 变慢不能让 Redshift 变贵
    a3, _, _ = amortized(rows(500.0, 30_000.0, [1 * MB] * 8), P3, names)
    want("duckdb 变慢不影响 redshift", a3["redshift"], a1["redshift"])
    # 5d. Athena 的 10MB 起步价是每条查询的：8 条各 1MB 必须收 8 次，不是 1 次。
    one = PR.athena_cost(1 * MB, P3)[0]
    want("athena 起步价按条收", round(a1["athena"], 9), round(8 * one, 9))
    if abs(a1["athena"] - PR.athena_cost(8 * MB, P3)[0]) < 1e-12:
        bad += 1
        print("  FAIL athena 摊薄成本等于「字节求和后收一次起步价」——起步价漏收了 7 次")
    # 5e. Redshift 的 60s 起步价整遍只落一次：4s 的一遍与 40s 的一遍同价
    a4, _, _ = amortized(rows(5_000.0, 100.0, [1 * MB] * 8), P3, names)
    want("60s 起步价整遍一次", round(a4["redshift"], 9), round(a1["redshift"], 9))
    # 5f. 说明里必须写清时长口径，否则读者会以为还是整批墙钟
    _, _, why5 = amortized(rows(500.0, 100.0, [1 * MB] * 8), P3, names)
    if "自身" not in why5["redshift"]:
        bad += 1
        print("  FAIL redshift 的摊薄说明没写「自身」耗时——旧口径正是没说清才错了很久")
    # 5f2. 这一格是下界，说明里必须写明，否则会被当成账单读。对过账的倍数是 3×。
    if "下界" not in why5["redshift"]:
        bad += 1
        print("  FAIL redshift 的摊薄说明没标「下界」——账单按活动分钟收，实测是模型的 3 倍")
    # 5g. Fargate 也有 60s 起步价，单位是**任务**。一遍 0.8s 不能按 0.8s 收——
    #     那种任务租不到。请求数不变时，0.8s 的一遍与 40s 的一遍必须同价。
    a5, _, _ = amortized(rows(500.0, 5_000.0, [1 * MB] * 8), P3, names)
    want("fargate 60s 起步价整遍一次", round(a5["duckdb"], 9), round(a1["duckdb"], 9))
    if a1["duckdb"] < PR.fargate_cost(PR.FARGATE_MIN_SECONDS, P3)[0]:
        bad += 1
        print("  FAIL duckdb 的摊薄成本低于一分钟任务的价——Fargate 起步价漏了")
    # 5h. 起步价默认不套，由调用点按自己那一栏的前提决定套几次：这一栏（八条共用
    #     一个任务）套一次，隔离栏（八条各起一个任务）套八次。默认套死就没法区分。
    if PR.fargate_cost(0.1, P3)[0] >= PR.fargate_cost(PR.FARGATE_MIN_SECONDS, P3)[0]:
        bad += 1
        print("  FAIL fargate_cost 默认套了起步价——该由调用点按任务数决定套几次")
    # 5h-2. 两栏的差必须是开机被收了几次：隔离栏八次、这一栏一次。方向钉住就够了,
    #       具体倍数随查询时长变，钉数值会变成把实现抄一遍。
    if not (8 * cost_of("duckdb", 1.0, 0.0, None, P3)[0] > a1["duckdb"]):
        bad += 1
        print("  FAIL 隔离栏八条合计没有高于连跑一遍——八次开机被算成了一次")
    # 5i. 跑满一分钟之后必须按实际时长走，别把起步价当上限
    if not (PR.fargate_cost(120.0, P3, floor=True)[0]
            > PR.fargate_cost(PR.FARGATE_MIN_SECONDS, P3, floor=True)[0]):
        bad += 1
        print("  FAIL fargate 跑满 60s 之后金额没随时长涨——起步价被当成了定价")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  统计 5 项、成本分派 12 项（含隔离栏必须含开机、八条八个任务）、查询集 {len(QUERIES)} 条"
          f"（shape/方言/窗口帧/tiebreak 四类检查）、字节格式化 4 项、"
          f"轮询回读 3 项、结果集比法 2 项")
    print("  整批摊薄 13 项：自身耗时各算各的 / 一条 arm 变慢不影响另两条（双向）/ "
          "Athena 起步价按条收 / Redshift 按一段算且标明是下界 / 时长口径写进说明 / "
          "Fargate 60s 起步价按任务收（默认不套、由调用点按任务数决定套几次、跑满后按实际时长）")
    print("耗时与成本逻辑自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
