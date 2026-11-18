#!/usr/bin/env python3
"""三 arm 正确性闸门：同一批指标在 Athena / DuckDB / Redshift 上必须逐位相等。

这是三架构对比的**第一道也是唯一一道闸门**。它之前不出任何性能数字——一条 arm 的
结果要是不对，它快多少都没有意义，而且「快」往往就是「少算了东西」的表现。

## 两个基准，不是一个

- **CSV 真源**（`--full`）：生成器的产出，三条 arm 的共同上游。它是唯一有资格叫
  「对」的那一份。
- **arm 与 arm 之间**：三条都对得上 CSV 时这一项自然成立；但**三条一起偏离 CSV**
  和**arm 之间互不相同**是两种完全不同的故事，前者指向装载或生成，后者指向引擎。
  所以两种比较都报，不合并。

`athena` 与 `duckdb` 读的是同一批物理 Parquet 文件，它们之间的任何差异**只能**是
引擎造成的。`redshift` 隔了一次 `COPY`，与前两者的差异要先排除物化这一步。

## 为什么输出一份 trace 而不只是 PASS/FAIL

每次跑都往 `data/bench/correctness-<时间戳>.jsonl` 写逐条 span：arm、表、SQL、
指标 dict、引擎耗时。理由有两条，都是实际的：

1. **计时那一步要复用同一份产物。** 正确性和耗时是同一次执行的两个侧面，分两套脚本
   各跑一次的话，报出来的耗时对应的不是通过了正确性检查的那一次执行。
2. **不相等时要能回答「哪儿不一样」而不只是「不一样」。** span 里存了原始 SQL 和
   完整指标 dict，差异出现时不用重跑就能定位到列。

耗时字段这一步**不作为性能结论**：正确性跑的时候没关引擎侧缓存
（见 `arms.configure_for_timing`），数字只用于发现异常量级。

## 一个例外：受治理的列

Redshift 的动态脱敏在聚合之前生效，所以 `users.email` / `users.phone` /
`user_profiles.birth_date` 在那条 arm 上**本该**不同。这三列登记在 `MASKED` 里，比的
不是「和另两条 arm 相等」而是「对得上脱敏策略自身的变换」，通过时进治理说明、不进差异
（`query_correctness.py --values` 复用同一份登记）。展开的理由在 `MASKED` 上方。

用法：

    python3 scripts/bench/correctness.py --rows            # 行数，快，拿装载快照做基准
    python3 scripts/bench/correctness.py --full            # 全指标，拿 CSV 做基准（要扫 7.2G）
    python3 scripts/bench/correctness.py --full -t orders -t users
    python3 scripts/bench/correctness.py --rows --arm athena --arm duckdb
    python3 scripts/bench/correctness.py --selftest        # 判定逻辑自测，不连任何引擎

环境变量：`AWS_REGION`、`REDSHIFT_SECRET_ARN`、`CSV_DIR`（`--full` 需要）、
`BENCH_TRACE_S3`（在 Fargate 上跑**必须**设，否则 trace 随容器一起消失）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import arms as A  # noqa: E402

TRACE_DIR = ROOT / "data" / "bench"

#: 设了就把 trace 传到这个 S3 前缀。Fargate 上跑必须设：容器的文件系统随任务一起
#: 消失，而 trace 正是「不相等时不重跑也能定位」的那份东西，丢了等于云上只能拿到
#: 一个 PASS/FAIL。形如 `s3://analytics-agent-raw/bench-traces/`。
TRACE_S3 = os.environ.get("BENCH_TRACE_S3", "").rstrip("/")


def upload_trace(path: Path) -> str:
    """把 trace 传到 `TRACE_S3`，返回落地的 URI（没配就返回空串）。

    上传失败**不改闸门结论**——闸门判的是数据对不对，不是管道通不通——但要吵一声。
    静默丢 trace 恰好是这个函数要修的那个问题，再静默一次就白加了。
    """
    if not TRACE_S3:
        return ""
    bucket, _, prefix = TRACE_S3.removeprefix("s3://").partition("/")
    key = f"{prefix.rstrip('/')}/{path.name}" if prefix else path.name
    try:
        import boto3
        boto3.client("s3", region_name=os.environ.get("AWS_REGION")).upload_file(
            str(path), bucket, key)
        return f"s3://{bucket}/{key}"
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️  trace 传不上 {TRACE_S3}：{e}\n"
              f"      闸门结论不受影响，但这一轮的 span 只在容器里，任务退出就没了。")
        return ""


# ---------------------------------------------------------------- 治理豁免

# Redshift 的动态脱敏(`database/redshift/04_governance.sql`,三条策略全挂 `TO PUBLIC`)
# 在**聚合之前**生效,所以那条 arm 上的 MIN/MAX 是脱敏值域的极值,跟另两条 arm 的原值
# 必然不同。这不是「三条 arm 装的不是同一份数据」——恰恰是治理层在生效的证据。
#
# 在此之前这件事只活在 `项目进度.md` 的散文里,没有一行代码知道,于是这一层和
# `query_correctness.py --values` 两个闸门**在没有缺陷的时候是红的**。这个项目反复
# 栽的是「绿灯掩盖缺陷」,而红得没道理是同一枚硬币的另一面:红久了没人看,等哪天真出
# 跨 arm 数据不一致(2026-09-02 出过一次,形态一模一样也是两三个文本列),真事就埋在
# 假红里了。
#
# **豁免不是跳过。** 跳过等于在治理面这三列上开一个盲区——那三列恰好是全库最敏感的
# 三列,盲区开在这里最糟。所以每条登记项都带一个正向断言:脱敏值必须**对得上策略本身
# 的变换**,对不上照旧报差异。三条策略能核到的强度不一样,这一点必须写明,因为
# 「核过了」和「核到了值」是两件事:
#
#   - `mask_email`     常量掩码       → 能逐位核(MIN=MAX=那个字面量)
#   - `mask_birthdate` DATE_TRUNC 年  → 单调,「极值的脱敏」=「脱敏的极值」,能逐位核
#   - `mask_phone`     拼第 1-3 与第 8-11 位 → **丢掉中间四位所以不单调**,脱敏后的
#                      极值和脱敏前的极值压根不是同一行,只能核形状
#
# 换句话说:`users.phone` 的跨 arm 相等性通过这条连接是**验不了**的,验的是掩码在生效。
# 要验底层数据得换一条不受 `TO PUBLIC` 约束的连接。这是个真窟窿,写在这里而不是留给
# 读代码的人自己发现。


def _mask_const(lit: str):
    def f(masked: str, clear: str | None) -> str | None:
        if masked == lit:
            return None
        return f"常量掩码期望 {lit!r},实得 {masked!r}"
    return f


def _mask_shape(pat: str, desc: str):
    rx = re.compile(pat)

    def f(masked: str, clear: str | None) -> str | None:
        if rx.match(masked):
            return None
        return f"掩码形状应为 {desc},实得 {masked!r}"
    return f


def _mask_year(masked: str, clear: str | None) -> str | None:
    """`DATE_TRUNC('year', birth_date)`:单调,所以能拿原值算出确切期望。"""
    if clear is None:
        # 只跑了 redshift 这一条 arm,拿不到原值。降级成形状,并且**说出来**——
        # 静默降级会让「核到了值」和「只看了个形状」在输出里长得一样。
        if re.match(r"^\d{4}-01-01", masked):
            return None
        return f"没有明文 arm 可比,只核形状 ^\\d{{4}}-01-01,实得 {masked!r}"
    m = re.match(r"^(\d{4})-", clear)
    if not m:
        return f"明文侧不像日期,算不出期望:{clear!r}"
    want = f"{m.group(1)}-01-01"
    if masked.startswith(want):
        return None
    return f"期望 DATE_TRUNC('year', {clear})={want},实得 {masked!r}"


#: (表, 列) → 这一列在哪些 arm 上被脱敏,以及怎么核。
MASKED: dict[tuple[str, str], dict] = {
    ("users", "email"): {
        "policy": "mask_email", "arms": ("redshift",), "strength": "值",
        "expect": _mask_const("***@masked.invalid"),
    },
    ("users", "phone"): {
        "policy": "mask_phone", "arms": ("redshift",), "strength": "形状",
        "expect": _mask_shape(r"^\d{3}\*{4}\d{4}$", r"^\d{3}\*{4}\d{4}$"),
    },
    ("user_profiles", "birth_date"): {
        "policy": "mask_birthdate", "arms": ("redshift",), "strength": "值",
        "expect": _mask_year,
    },
}


def mask_verdict(table: str, col: str,
                 vals: dict[str, str]) -> tuple[str | None, str | None]:
    """→ (说明, 差异)。两者最多一个非 None;都是 None 表示这一列不在治理面里。

    明文 arm 之间先得自己一致:它们没被脱敏,不一致就是真的装载不同步,不能被
    「这列受治理」这个理由盖过去。
    """
    ent = MASKED.get((table, col))
    if not ent:
        return None, None
    masked_arms = [a for a in vals if a in ent["arms"]]
    if not masked_arms:
        return None, None                      # 脱敏的那条 arm 没参加这一轮
    clear = {a: v for a, v in vals.items() if a not in ent["arms"]}
    if len(set(clear.values())) > 1:
        return None, ("明文 arm 之间就不一致 "
                      + "  ".join(f"{a}={v}" for a, v in sorted(clear.items()))
                      + "（是受治理列,但这处差异不在治理面上）")
    ref = next(iter(clear.values())) if clear else None
    for a in masked_arms:
        why = ent["expect"](vals[a], ref)
        if why:
            return None, f"{a} 的值对不上脱敏策略 {ent['policy']} —— {why}"
    return (f"{'/'.join(masked_arms)} 按 {ent['policy']} 脱敏,"
            f"已核到「{ent['strength']}」这一级"), None


# ---------------------------------------------------------------- 判定

def norm(label: str, v: object) -> str:
    """把一个指标值化成可比的字符串。

    `sum:` 走 CSV 侧的 4 位小数格式化，这样比的是**数值**不是字面：Data API 把
    DECIMAL 回成字符串、DuckDB 可能回 `Decimal`、Trino 回字符串，三种表示同一个数。
    """
    if v is None:
        return "<NULL>"
    if label.startswith("sum:"):
        import decimal
        try:
            return A.lake_vl().fmt_sum(decimal.Decimal(str(v)))
        except (decimal.InvalidOperation, ValueError):
            return str(v)
    return str(v)


def compare(table: str, baseline: dict[str, object] | None,
            per_arm: dict[str, dict[str, object]],
            baseline_name: str) -> tuple[list[str], list[str]]:
    """→ (差异清单, 治理说明)。差异为空 = 通过。基准可为 None，那时只做 arm 互比。

    治理说明单独出一条通路而不是并进差异里：受治理的列在脱敏 arm 上**本该**不同，
    把它算成差异会让这个闸门在没有缺陷的时候红着（见 `MASKED` 上方那段）。
    """
    diffs: list[str] = []
    notes: list[str] = []
    labels: list[str] = []
    for m in ([baseline] if baseline else []) + list(per_arm.values()):
        for k in m:
            if k not in labels:
                labels.append(k)

    for k in labels:
        vals = {a: norm(k, m.get(k, "<缺>")) for a, m in per_arm.items()}

        # 治理豁免。指标名形如 `min:birth_date`，冒号后面才是列名。
        col = k.split(":", 1)[1] if ":" in k else k
        note, err = mask_verdict(table, col, vals)
        if err:
            diffs.append(f"{table}.{k}：{err}")
            continue
        if note:
            notes.append(f"{table}.{k}：{note}")
            # 脱敏的 arm 退出这一项的比较：它的值已经按策略核过了，再跟基准或明文
            # arm 比一次只会重复报同一件事。**明文 arm 之间照旧互比**，否则豁免
            # 就成了整列盲区。
            vals = {a: v for a, v in vals.items()
                    if a not in MASKED[(table, col)]["arms"]}
            if not vals:
                continue

        if baseline is not None:
            b = norm(k, baseline.get(k, "<缺>"))
            off = {a: v for a, v in vals.items() if v != b}
            if len(off) == len(vals) and len(vals) > 1:
                # 三条 arm 一致地偏离基准 —— 指向装载或生成，不是引擎
                diffs.append(f"{table}.{k}：{baseline_name}={b}，"
                             f"但**三条 arm 一致地**都是 {sorted(set(vals.values()))}"
                             f"（一致偏离基准 → 查装载/生成，不是引擎）")
                continue
            for a, v in off.items():
                diffs.append(f"{table}.{k}：{baseline_name}={b}  {a}={v}")
        # arm 互比。基准比过之后这一项通常自然成立，但基准缺失时它是唯一的判据。
        uniq = set(vals.values())
        if len(uniq) > 1 and baseline is None:
            diffs.append(f"{table}.{k}：arm 之间不一致 "
                         + "  ".join(f"{a}={v}" for a, v in sorted(vals.items())))
    return diffs, notes


def want_from_cols(cols: list[tuple[str, str]]) -> set[str]:
    """按 DDL 列类型推该算哪些指标。只在 `--arms-only` 用。

    `--full` 的指标集合来自 CSV 侧真算出来的那些，两边天然对齐。没有 CSV 时得自己
    推一份，判据借 `verify_load.classify`——「什么列算什么指标」这条规则只能有一处，
    在这里另写一遍就等于给三条 arm 换了把尺，而换尺之后对不上会被当成引擎差异。
    """
    lake = A.lake_vl()
    want = {"count"}
    for c, pg in cols:
        kind = lake.classify(pg)
        if kind == "num":
            want.add(f"sum:{c}")
        elif kind in ("ts", "date"):
            want |= {f"min:{c}", f"max:{c}"}
        elif kind == "bool":
            want.add(f"true:{c}")
    return want


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="三 arm 正确性闸门")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rows", action="store_true",
                   help="只比行数，基准是 data/loaded_row_counts.json（快）")
    g.add_argument("--full", action="store_true",
                   help="比全部指标，基准是 CSV 真源（要扫 7.2G）")
    g.add_argument("--arms-only", action="store_true",
                   help="比全部指标但**不用基准**，只做 arm 互比（不需要 CSV）")
    ap.add_argument("--arm", action="append", default=[], choices=list(A.ARMS),
                    help="只跑这些 arm（可重复）；默认三条都跑")
    ap.add_argument("-t", "--table", action="append", default=[])
    ap.add_argument("--no-trace", action="store_true", help="不写 trace 文件")
    ap.add_argument("--selftest", action="store_true", help="判定逻辑自测，不连引擎")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not (a.rows or a.full or a.arms_only):
        ap.error("给 --rows、--full 或 --arms-only")

    import gen_ddl
    declared = {t: [(c, pg) for c, pg, _nn, _n in cols]
                for _src, t, cols in gen_ddl.parse_source()}
    tables = a.table or sorted(declared)
    if bad := [t for t in tables if t not in declared]:
        print(f"这些表不在 DDL 真源里：{bad}")
        return 2

    lake = A.lake_vl()
    snap: dict[str, int] = {}
    if a.rows:
        s = json.loads(lake.LOADED_SNAPSHOT.read_text(encoding="utf-8"))
        snap = {k: int(v) for k, v in s["tables"].items()}
        base_name = "快照"
        print(f"基准：{lake.LOADED_SNAPSHOT.relative_to(ROOT)}"
              f"（{s['source']}，scale {s['scale']} / seed {s['seed']} / 轴止 {s['as_of']}）")
    elif a.arms_only:
        # 没有基准。`--rows` 太弱（只比行数，装载不同步时照样绿），`--full` 又要那份
        # 7.2G CSV 在手边。这一档补中间：全部指标都比，但只判 arm 之间是否相等。
        # 「三条一起偏离真源」这一类它抓不到——那要靠 --full——所以两档都保留。
        base_name = "—（无基准）"
        print("基准：无。全部指标做 arm 互比，指标定义仍来自 verify_load.py（同一把尺）")
    else:
        if (rc := lake._resolve_csv_dir()) is not None:
            return rc
        base_name = "CSV"
        print(f"基准：{lake.CSV_DIR}（聚合引擎 {lake._engine()[1]}）")

    armlist = A.all_arms(a.arm or None)
    print(f"arm：{'、'.join(x.name for x in armlist)}    表：{len(tables)} 张\n")

    trace = None
    if not a.no_trace:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tp = TRACE_DIR / f"correctness-{stamp}.jsonl"
        trace = tp.open("w", encoding="utf-8")
        trace.write(json.dumps({
            "kind": "run", "mode": "rows" if a.rows else "full",
            "started_at": stamp, "baseline": base_name,
            "arms": [x.name for x in armlist], "tables": len(tables),
            "region": os.environ.get("AWS_REGION", ""),
            # 计时口径没关缓存，写进来免得有人把这里的 elapsed_ms 当性能结论
            "timing_valid_for_perf": False,
        }, ensure_ascii=False) + "\n")

    diffs: list[str] = []
    gov_notes: list[str] = []
    checked = 0
    total_rows = 0
    per_arm_ms: dict[str, float] = {x.name: 0.0 for x in armlist}
    t0 = time.time()

    for t in tables:
        cols = declared[t]
        baseline: dict[str, object] | None
        if a.rows:
            if t not in snap:
                print(f"  ·  {t:<26} 跳过（快照里没有）")
                continue
            baseline = {"count": snap[t]}
            want = set(baseline)
        elif a.arms_only:
            baseline = None
            # 指标集合按 DDL 列类型推，判据与 verify_load.classify 同一份，
            # 不在这里另写一套「什么列该算什么」。
            want = want_from_cols(cols)
        else:
            if not (lake.CSV_DIR / f"{t}.csv").exists():
                print(f"  ·  {t:<26} 跳过（没有 CSV）")
                continue
            baseline = lake.csv_metrics(t, cols)
            want = set(baseline)
        per_arm: dict[str, dict[str, object]] = {}
        for x in armlist:
            if a.rows:
                r = x.client.execute(f'SELECT COUNT(*) FROM "{t}"')
                per_arm[x.name] = {"count": int(r["rows"][0][0])}
                ms, sql = float(r.get("elapsed_ms") or 0), f'SELECT COUNT(*) FROM "{t}"'
            else:
                per_arm[x.name], ms = x.metrics(t, cols, want)
                sql = x.probe(t, cols, want)[0]
            per_arm_ms[x.name] += ms
            if trace:
                trace.write(json.dumps({
                    "kind": "span", "arm": x.name, "table": t,
                    "sql": sql, "elapsed_ms": ms,
                    "metrics": {k: str(v) for k, v in per_arm[x.name].items()},
                }, ensure_ascii=False) + "\n")

        bad, gov = compare(t, baseline, per_arm, base_name)
        checked += 1
        # 没有基准时行数取第一条 arm 的——它已经和别的 arm 比过了，不等的话上面报了
        n = int((baseline or next(iter(per_arm.values())))["count"])
        total_rows += n
        gov_notes += gov
        # 受治理的列**算比过了**，所以这里跟着报出来。不打的话「豁免了」和
        # 「压根没查」在输出里又一样了。
        tail = f"（另 {len(gov)} 项按脱敏策略核过）" if gov else ""
        if bad:
            diffs += bad
            print(f"  ❌ {t:<26} {len(want)} 项指标，{len(bad)} 项不符{tail}")
        else:
            print(f"  ✅ {t:<26} {len(want)} 项 × {len(armlist)} arm 全等  "
                  f"count={n:,}{tail}")

    elapsed = time.time() - t0
    if trace:
        trace.write(json.dumps({
            "kind": "summary", "tables_checked": checked, "rows": total_rows,
            "diffs": len(diffs), "wall_s": round(elapsed, 1),
            "engine_ms_by_arm": {k: round(v, 1) for k, v in per_arm_ms.items()},
            # 治理豁免进 trace，不只进 stdout：一次「0 差异」的跑，到底是全等还是
            # 有三项被豁免掉了，事后只能从这里看出来。
            "governance_notes": gov_notes,
        }, ensure_ascii=False) + "\n")
        trace.close()
        uploaded = upload_trace(tp)

    print()
    if gov_notes:
        # 放在结论**之前**打。放后面的话，一份「通过 ✅」的输出末尾挂着几行治理说明，
        # 读的人会先记住「全等」再看到「除了这三列」。
        print(f"治理豁免 {len(gov_notes)} 项 —— 这几项在脱敏 arm 上本该不同，"
              f"按策略自身的变换核过，不计入差异：")
        for g in gov_notes:
            print(f"  ·  {g}")
        print()
    if diffs:
        print(f"正确性闸门未通过 ❌  {len(diffs)} 处差异\n")
        for d in diffs[:50]:
            print(f"  - {d}")
        if len(diffs) > 50:
            print(f"  …… 另有 {len(diffs) - 50} 处")
        if trace:
            print(f"\n逐条 span 在 {uploaded or tp.relative_to(ROOT)}，"
                  f"含原始 SQL 和完整指标 dict")
        return 1

    what = "行数" if a.rows else "行数、数值求和、时间边界、布尔计数"
    versus = "彼此逐位相等" if a.arms_only else f"与{base_name}逐位相等"
    # 「逐位相等」这句话在有豁免的时候是不成立的，所以带上例外，别让结论比事实强。
    ex = f"（{len(gov_notes)} 项受治理列除外，已按策略核过）" if gov_notes else ""
    print(f"正确性闸门通过 ✅  {checked} 张表的{what}在 "
          f"{'、'.join(x.name for x in armlist)} 上{versus}{ex}，"
          f"合计 {total_rows:,} 行")
    if a.arms_only:
        print("  ⚠️  这一档没有真源：三条 arm 一起偏离生成器的情况它抓不到，那要 --full。")
    print(f"  墙钟 {elapsed:.1f}s，引擎侧累计 "
          + "、".join(f"{k} {v/1000:.1f}s" for k, v in per_arm_ms.items()))
    print("  ⚠️  这里的耗时不是性能结论：正确性这一步没关引擎侧缓存。")
    if trace:
        print(f"  trace：{tp.relative_to(ROOT)}"
              + (f" → {uploaded}" if uploaded else ""))
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判定逻辑自测：不连任何引擎，只喂构造的指标 dict。

    要盯住的是「三条一起偏离」必须与「arm 之间不同」分开报——这两种情况的排查方向
    完全不同（装载/生成 vs 引擎），合成一条报的话等于把最有用的信息扔了。
    """
    bad = 0

    def cmp_d(*a) -> list[str]:
        """只要差异那一半。治理说明有自己的一组断言，见下面第 8 组。"""
        return compare(*a)[0]

    def check(name, got, want_sub, want_n=None):
        nonlocal bad
        if want_n is not None and len(got) != want_n:
            bad += 1
            print(f"  FAIL {name}：期望 {want_n} 条，实际 {len(got)}\n       {got}")
            return
        if want_sub and not any(want_sub in g for g in got):
            bad += 1
            print(f"  FAIL {name}：没有一条包含 {want_sub!r}\n       {got}")

    # 1. 全等 → 无差异
    check("全等", cmp_d("t", {"count": 5}, {"a": {"count": 5}, "b": {"count": 5}}, "CSV"),
          "", 0)

    # 2. 一条 arm 偏离 → 只报那一条，且不能报成「一致偏离」
    d = cmp_d("t", {"count": 5}, {"a": {"count": 5}, "b": {"count": 4}}, "CSV")
    check("单条偏离", d, "b=4", 1)
    if any("一致地" in x for x in d):
        bad += 1
        print(f"  FAIL 单条偏离被报成了一致偏离：{d}")

    # 3. 三条一起偏离 → 一条，且必须指向装载/生成
    d = cmp_d("t", {"count": 5},
              {"a": {"count": 4}, "b": {"count": 4}, "c": {"count": 4}}, "CSV")
    check("一致偏离", d, "一致地", 1)
    check("一致偏离指路", d, "装载/生成")

    # 4. 没有基准时，arm 互比是唯一判据
    check("无基准互比", cmp_d("t", None, {"a": {"count": 5}, "b": {"count": 4}}, "CSV"),
          "arm 之间不一致", 1)
    check("无基准全等", cmp_d("t", None, {"a": {"count": 5}, "b": {"count": 5}}, "CSV"),
          "", 0)

    # 5. sum: 比数值不比字面 —— 三种表示同一个数，不能报差异
    check("sum 归一化",
          cmp_d("t", {"sum:x": "1.5"},
                {"a": {"sum:x": "1.5000"}, "b": {"sum:x": 1.5}}, "CSV"), "", 0)

    # 6. 缺列必须报出来。指标集合按基准对齐，某条 arm 少给一项时若静默通过，
    #    「少比了一列」和「比过了且相等」在输出里就一模一样。
    check("缺列", cmp_d("t", {"count": 5, "sum:x": "1.0"},
                        {"a": {"count": 5}, "b": {"count": 5, "sum:x": "1.0"}}, "CSV"),
          "<缺>", 1)

    # 7. NULL 与 0 不能混为一谈
    check("NULL≠0", cmp_d("t", {"sum:x": None}, {"a": {"sum:x": 0}}, "CSV"),
          "<NULL>", 1)

    # 8. 治理豁免。这一组要钉住的是**豁免没有变成盲区**：脱敏值对得上策略才放过，
    #    对不上照旧红，而且明文 arm 之间的分歧不能被「这列受治理」盖掉。
    EMAIL = "***@masked.invalid"

    def gov(table, col, vals):
        return mask_verdict(table, col, vals)

    n_ok, n_err = gov("users", "email",
                      {"athena": "a@b.com", "duckdb": "a@b.com", "redshift": EMAIL})
    if n_err or not n_ok:
        bad += 1
        print(f"  FAIL 治理-常量掩码本该放过：说明={n_ok!r} 差异={n_err!r}")

    # 掩码值对不上策略 → 必须报差异。这是「豁免不是跳过」的那一条：真出问题它还红。
    n_ok, n_err = gov("users", "email",
                      {"athena": "a@b.com", "redshift": "a@b.com"})
    if not n_err or n_ok:
        bad += 1
        print(f"  FAIL 治理-掩码失效本该报差异：说明={n_ok!r} 差异={n_err!r}")

    # 明文 arm 自己不一致 → 报差异，并且要说清这处差异不在治理面上，
    # 否则读的人会以为又是脱敏引起的，方向就查反了。
    n_ok, n_err = gov("users", "email",
                      {"athena": "a@b.com", "duckdb": "z@b.com", "redshift": EMAIL})
    if not n_err or "治理面" not in (n_err or ""):
        bad += 1
        print(f"  FAIL 治理-明文侧分歧本该报差异且指明不在治理面：{n_err!r}")

    # DATE_TRUNC('year') 单调，所以拿明文能算出确切期望，核到值一级。
    n_ok, n_err = gov("user_profiles", "birth_date",
                      {"athena": "1978-06-14", "redshift": "1978-01-01"})
    if n_err or "值" not in (n_ok or ""):
        bad += 1
        print(f"  FAIL 治理-birth_date 逐位核本该放过：说明={n_ok!r} 差异={n_err!r}")
    n_ok, n_err = gov("user_profiles", "birth_date",
                      {"athena": "1978-06-14", "redshift": "1979-01-01"})
    if not n_err:
        bad += 1
        print("  FAIL 治理-birth_date 截错年份本该报差异")

    # 只跑 redshift 一条 arm：拿不到明文，降级成形状，而且必须**说出来**。
    n_ok, n_err = gov("user_profiles", "birth_date", {"redshift": "1978-06-14"})
    if not n_err or "只核形状" not in (n_err or ""):
        bad += 1
        print(f"  FAIL 治理-无明文时未降级或未声明：说明={n_ok!r} 差异={n_err!r}")

    # phone 的掩码丢掉中间四位，不单调，只能核形状——这一级要在说明里写明，
    # 不然「核过了」会被读成「核到了值」。
    n_ok, n_err = gov("users", "phone",
                      {"athena": "13000045003", "redshift": "130****0000"})
    if n_err or "形状" not in (n_ok or ""):
        bad += 1
        print(f"  FAIL 治理-phone 形状核本该放过并标明强度：说明={n_ok!r} 差异={n_err!r}")
    n_ok, n_err = gov("users", "phone",
                      {"athena": "13000045003", "redshift": "13000045003"})
    if not n_err:
        bad += 1
        print("  FAIL 治理-phone 未脱敏本该报差异")

    # 不在治理面上的列一律不碰：两个 None 才对，否则豁免会漏到别的列上。
    if gov("orders", "email", {"a": "x", "redshift": EMAIL}) != (None, None):
        bad += 1
        print("  FAIL 治理-非治理面列被误判")

    # 脱敏 arm 没参加这一轮时，也不该产生说明。
    if gov("users", "email", {"athena": "a@b.com", "duckdb": "a@b.com"}) != (None, None):
        bad += 1
        print("  FAIL 治理-脱敏 arm 缺席时不该出说明")

    # 9. 走完整条 compare()：受治理列出说明不出差异，同表的普通列照旧比。
    d, g = compare("users", None,
                   {"athena": {"min:email": "a@b.com", "count": 5},
                    "duckdb": {"min:email": "a@b.com", "count": 5},
                    "redshift": {"min:email": EMAIL, "count": 5}}, "CSV")
    check("compare 治理列不算差异", d, "", 0)
    if len(g) != 1 or "mask_email" not in g[0] or "users.min:email" not in g[0]:
        bad += 1
        print(f"  FAIL compare 治理说明不对：{g}")
    # 明文 arm 之间在受治理列上分歧 → 必须红。豁免整列的写法会在这里放绿灯。
    d, g = compare("users", None,
                   {"athena": {"min:email": "a@b.com"},
                    "duckdb": {"min:email": "z@b.com"},
                    "redshift": {"min:email": EMAIL}}, "CSV")
    check("compare 治理列上的真分歧", d, "治理面", 1)

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  全等 / 单条偏离 / 一致偏离 / 无基准互比 / sum 归一化 / 缺列 / NULL≠0")
    print("  治理豁免：常量掩码 / 掩码失效必红 / 明文侧分歧必红 / DATE_TRUNC 逐位核 /"
          " 截错年份必红 / 无明文降级并声明 / phone 只核形状 / 未脱敏必红 /"
          " 非治理列不碰 / 脱敏 arm 缺席不出说明 / compare 全链路两例")
    print("判定逻辑自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
