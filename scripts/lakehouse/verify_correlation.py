#!/usr/bin/env python3
"""L8 画像 ⟷ 行为相关性：画像列是不是与消费独立抽样的（D-01 的判据）。

## 这一组和 L6 / L7 的分工

L7 判**单行对不对**，L6 判**整体够不够分**，这一组判**两列之间有没有关系**。三者抓的
缺陷互不重叠：D-01 的每一行都合法（一个 30 岁的高收入设计师花了 1200 元，无可指摘）、
每个维度自身的分布也够分（12 个职业各上万人），坏的是**列与列之间没有边**。只有把
两列交叉起来看分组均值才看得见，所以它必须是独立的一组。

## 判据从哪来

**唯一真源是 `scripts/gen/profiles.yaml`**，本文件通过 `scripts/gen/profiles.py` 读它，
且只接 `load()` 返回的 `(Dimensions, Judge)` 两件——**拿不到 `Generate`**。这不是自觉，
是作用域：生成侧的倍率不在本文件任何一个变量里。理由见 profiles.yaml 文件头：用生成器
的效应量去推检查器的通过线，就是拿函数的输出验函数，两端不独立，绿灯不携带信息。

两侧唯一共用的**公式**是分档函数 `profiles.age_decade()`。生成器按它索引年龄倍率、
本文件按它分组，必须是同一个：生成器按 `age//10*10`、检查器按 `(age-18)//10` 的话，
量的是另一个划分，红绿都读不出意思。这与 `semantics.price_range()` 必须两侧共用是
同一个理由（Batch 2 的教训：共用配置还不够，公式也得共用）。

## 口径

照任务书 L8 给的查询骨架，一个字没改：

    人均 GMV = 该档**下过有效单的用户**的 SUM(actual_amount) 均值
    有效     = status IN ('paid','shipped','delivered')
    极差     = (max(人均GMV) - min(人均GMV)) / min(人均GMV)

两处要说清，因为它们会改变读数：

  · `JOIN` 而不是 `LEFT JOIN`：**没下过单的用户不进分母**。这是任务书骨架的口径。
    含义是「已经在消费的人里，画像能不能区分消费额」，不含「画像能不能预测会不会消费」。
    后者是另一个问题（转化率而非客单价），本文件不判，也不该假装判了。
  · 「用户数」这一列因此是**该档有单用户数**，与 `judge.shape` 那条职业人数判据的分母
    **不是同一个**——后者查全部 `user_profiles`、不关联订单（任务书：这类检查更省事、
    应该排在前面）。两个数在报告里都出现，所以标题必须写清是哪个分母。

## 一个已知的、刻意留下的缺口：人均订单数仍然与画像无关

报告里「人均单数」那一列修复后仍然各档几乎相等（scale 500 实测 4.96 ~ 5.45）。这是因为
D-01 的修复把全部效应放在**每单金额**上，没有动下单频次。真实电商里高收入用户不只客单价
高、下单也更频繁，所以这一列的平坦是一处**未修的失真**，只是它不在 D-01 的判据范围内。

没顺手一起修，理由是波及面：下单频次由 `tables.py::_pick_registered_user` 的活跃度权重
决定，而那份权重是 `ctx.cache["_users_by_reg"]` 一份、**被 sessions / events / page_views
等行为表共用**。把画像接到它上面，会同时移动留存曲线（`ENGAGEMENT_HALFLIFE` 混出的
D1/D7/D14）和漏斗各级的单调性——这两样各自都是审计抓出来的 P0，各自有判据盯着。要做就得
连它们一起重新推导，那是独立的一批活，不该塞在 D-01 里顺手改掉。

所以本文件的判据**只放在人均 GMV 上**，人均单数只报数、不判。这里不设判据不是因为它不
重要，是因为现在给它设一条线，等于要求一次没规划的连带改动——把判据当成施压工具，
而不是描述期望。

## 四种判定，为什么不是两种

    PASS      极差达到通过线，且方向约束成立
    FAIL      极差不达标 / 方向错。改生成器
    WEAK      **这批数据分辨不出通过线那么大的差异**。加数据量，别动生成器
    无输入    该档位在数据里不存在（如 v3 不产 gender='unknown'）。既不是通过也不是失败

WEAK 是有代价才加的。FAIL 和 WEAK 的修法完全相反，混成一个红灯，小样本上的每次运行都
会把人推向「去调生成器」，而那时生成器可能是对的。判法是**先用通过线算功效**：若
`min_range × 底档人均 < n_sigma × SE_diff`，说明哪怕真实效应恰好等于通过线也测不出来。
用通过线而不是实测极差算，所以不会出现「实测极差恰好很小 → 自动判成看不清」的事后开脱。

## 一件意料之外的发现：职业池有两份，而且已经漂了

`data/csv`（也就是云上现行库）里 `occupation` 有 **15 个取值**，`scripts/gen/tables.py`
只有 12 个，交集 9 个。原因是本仓库有**两套生成器**：

  · `scripts/generators/user_domain.py:66`（旧，`scripts/generate_data.py` →
    `export_to_csv.py` → `data/csv` → 云上）：15 个职业，`random.choice` 等概率；
  · `scripts/gen/tables.py`（新，任务书要修的这套）：12 个职业，`F.from_pool` 等概率。

D-01 的机制在两套里各自独立存在，所以修了新的那套并不会改变云上读数——这一点必须写下来，
否则「改完了云上还是红」会被读成修复失败。DDL 和 knowledge 卡片都没有声明这一列的枚举
（`grep -rn occupation knowledge database`），所以 `verify_enums.py` 抓不到这次漂移，
在本文件之前它没有任何自动化覆盖。报告里那些「← 值域外」的行就是这件事，不是检查器的 bug。

## 预期结论：对现行库跑必然是红的

云上是 v1 那批独立抽样的画像（500 用户），项目已决定不重灌数据，所以本文件对云上跑会
红。同 `verify_literals.py` / `verify_semantics.py` / `verify_resolution.py`，**不接进
`scripts/test_all.sh`**：常驻的红灯只会把人训练成无视红灯。绿灯在生成侧看
（`--from-csv`），跑的是同一批 judge 函数。

云上那 500 行还有一件事值得先说：多数维度会判 **WEAK 而不是 FAIL**。500 个用户分 12 个
职业档，每档 ~40 人，而 per-user 消费额是长尾的——这个样本量本来就分辨不出 60% 的极差。
这不是判据太软，是 v1 数据量太小。审计报告里那四个百分比是在 v2 那套 21 万用户的库上量的，
在那个规模上它们是 FAIL 而不是 WEAK。

修生成器之前实测的红线基线（`--only users user_profiles orders`，seed 默认）：

    scale  用户数    判定                    说明
      1    500     2 FAIL · 4 WEAK       = data/csv / 云上。样本太小，极差判不动
     50    25,000  5 FAIL · 2 WEAK       income / occupation 的极差开始判得动
    200    100,000 7 FAIL · 1 WEAK       只剩 age 的极差还分辨不出
    500    250,000 **8 FAIL · 0 WEAK**   全红。这是「改生成器之前」的基线（约 51s）

极差随规模**变小**（occupation 18.1% → 12.4% → 5.2%），正是独立抽样的预测：观测到的
差异全是噪声，样本越大越趋于 0。250k 用户上量到的 3.7% / 3.4% / 5.2% / 0.6% 与审计在
v2 那 21 万用户上量到的 1.4% / 3.2% / 7.0% / 4.9% 同量级——两套生成器、两套库，
同一个机制。所以**绿灯证明也要在 scale 500 上跑**，小样本上的绿只是分辨不出。

用法：

    python3 scripts/lakehouse/verify_correlation.py                  # 查云上现行库
    python3 scripts/lakehouse/verify_correlation.py --from-csv /tmp/gen1
    python3 scripts/lakehouse/verify_correlation.py --selftest       # 判据自测（无云依赖）
"""
from __future__ import annotations

import argparse
import collections
import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT / "scripts" / "gen"))

import profiles as PROF                                # noqa: E402  判据真源

MARK = {"PASS": "\033[32m  ok  \033[0m", "FAIL": "\033[31m FAIL \033[0m",
        "WEAK": "\033[33m WEAK \033[0m", "NOINPUT": "\033[90m  --  \033[0m"}
CSV_DIR = ROOT / "data" / "csv"

# 有效订单状态。字面量与 `database/iceberg/02_mart.sql` 的 mart 建表、
# `backend/agent.py` 的口径声明、`scripts/gen/tables.py::VALID_STATUS` 一致——
# 这个三元组在本项目里已经有十几处副本（`grep "paid','shipped','delivered'"`），
# 收敛它是另一件事；这里只做一件：selftest 断言本文件这份与生成器那份相等，
# 免得 D-01 的检查悄悄用上了和数据不同的口径。
VALID_STATUS = ("paid", "shipped", "delivered")

# 分组表达式。**`age` 那条是 `PROF.age_decade` 的 SQL 等价物**，不是第二个口径：
# Trino 的整数除法在正整数上与 Python `//` 同义，selftest 逐岁核对这件事。
# 任务书 L8 的骨架写的就是 `age/10*10`。
BUCKET_SQL = {
    "income_level": "p.income_level",
    "gender": "p.gender",
    "occupation": "p.occupation",
    "age": "CAST(p.age / 10 * 10 AS varchar)",
}


@dataclass(frozen=True)
class Stat:
    """一个档位的汇总。judge 函数只吃这个，所以 Athena / CSV / 构造数据同权。"""
    bucket: object
    users: int              # 该档**有有效单**的用户数
    mean_gmv: float
    sd_gmv: float           # per-user GMV 的样本标准差
    mean_orders: float

    @property
    def se(self) -> float:
        """人均 GMV 的标准误。users < 2 时标准差无定义，返回 inf → 必然判 WEAK。"""
        if self.users < 2 or not math.isfinite(self.sd_gmv):
            return math.inf
        return self.sd_gmv / math.sqrt(self.users)


# ---------------------------------------------------------------- 取数

def _coerce(dim: str, raw, dims: PROF.Dimensions):
    """把取到的档位值转成配置里那个类型（age 是 int，其余是 str）。"""
    if dim == "age":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return raw
    return None if raw is None else str(raw)


def load_athena(client, dims: PROF.Dimensions
                ) -> tuple[dict[str, list[Stat]], dict[str, int]]:
    """五个查询：四个维度各一个 + 职业人数（不关联订单）。"""
    status = ", ".join(f"'{s}'" for s in VALID_STATUS)
    spend = (f"SELECT user_id, CAST(SUM(actual_amount) AS double) AS amt, "
             f"COUNT(*) AS cnt FROM orders WHERE status IN ({status}) GROUP BY 1")
    out: dict[str, list[Stat]] = {}
    for dim, expr in BUCKET_SQL.items():
        sql = (f"WITH s AS ({spend})\n"
               f"SELECT {expr} AS bucket, count(*) AS users, avg(s.amt) AS mean_gmv,\n"
               f"       stddev_samp(s.amt) AS sd_gmv, avg(CAST(s.cnt AS double)) AS mean_ord\n"
               f"FROM user_profiles p JOIN s ON p.user_id = s.user_id\n"
               f"GROUP BY {expr}")
        res = client.execute(sql)
        out[dim] = [Stat(bucket=_coerce(dim, r[0], dims), users=int(r[1]),
                         mean_gmv=float(r[2] or 0.0),
                         sd_gmv=float(r[3]) if r[3] is not None else math.nan,
                         mean_orders=float(r[4] or 0.0))
                    for r in res["rows"]]
    res = client.execute("SELECT occupation, count(*) FROM user_profiles GROUP BY 1")
    counts = {str(r[0]): int(r[1]) for r in res["rows"]}
    return out, counts


def load_csv(src: Path, dims: PROF.Dimensions
             ) -> tuple[dict[str, list[Stat]], dict[str, int]]:
    """从生成器产出算同样的汇总。

    `user_profiles.csv` / `orders.csv` 优先读生成目录，缺了退回 `data/csv`——这正是
    灌库时的真实来源组合（同 `verify_resolution.load_counts`）。两张表都缺就报错而不是
    静默算出一份空汇总：空汇总会让每条判据都判「无输入」，读起来像「查过了」。
    """
    def pick(name: str) -> Path:
        p = src / name
        if p.exists():
            return p
        p2 = CSV_DIR / name
        if p2.exists():
            return p2
        raise FileNotFoundError(f"{src} 和 {CSV_DIR} 里都没有 {name}")

    op = pick("orders.csv")
    if op.parent != src:
        raise FileNotFoundError(
            f"{src} 里没有 orders.csv。L8 判的是画像与**消费**的关系，只生成 "
            f"user_profiles 不够——跑一次不带 --only 的 scripts/gen/main.py。"
            f"（不退回 data/csv/orders.csv：那是 v1 的 2,000 单，配生成的画像就是"
            f"两批不相干的数据拼在一起，算出来的极差没有意义。）")

    # user_id -> [GMV, 单数]。全量 orders 也只是每用户一条，内存无压力。
    valid = set(VALID_STATUS)
    spend: dict[int, list[float]] = {}
    with op.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["status"] not in valid:
                continue
            e = spend.setdefault(int(r["user_id"]), [0.0, 0.0])
            e[0] += float(r["actual_amount"])
            e[1] += 1

    # 每档累加 n / Σx / Σx² / Σ单数，最后一次算均值和样本标准差。
    acc: dict[str, dict] = {d: collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
                            for d in BUCKET_SQL}
    occ_all: collections.Counter = collections.Counter()
    with pick("user_profiles.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            occ_all[r["occupation"]] += 1
            e = spend.get(int(r["user_id"]))
            if e is None:
                continue                       # 没下过单 → 不进分母（同任务书骨架的 JOIN）
            gmv, nord = e
            keys = {"income_level": r["income_level"], "gender": r["gender"],
                    "occupation": r["occupation"],
                    "age": PROF.age_decade(int(r["age"]))}
            for d, k in keys.items():
                a = acc[d][k]
                a[0] += 1
                a[1] += gmv
                a[2] += gmv * gmv
                a[3] += nord

    out: dict[str, list[Stat]] = {}
    for d, buckets in acc.items():
        rows = []
        for k, (n, sx, sxx, so) in buckets.items():
            mean = sx / n
            var = (sxx - n * mean * mean) / (n - 1) if n > 1 else math.nan
            rows.append(Stat(bucket=k, users=n, mean_gmv=mean,
                             sd_gmv=math.sqrt(max(var, 0.0)) if n > 1 else math.nan,
                             mean_orders=so / n))
        out[d] = rows
    return out, dict(occ_all)


# ---------------------------------------------------------------- 判据

def judge_range(dim: str, stats: list[Stat], judge: PROF.Judge
                ) -> tuple[str, str, str]:
    """极差判据 + 功效前置检查。纯函数。"""
    line = judge.min_range[dim]
    title = f"{dim} 档间人均 GMV 极差 ≥ {line:.0%}"
    usable = [s for s in stats if s.users > 0]
    if len(usable) < 2:
        return ("NOINPUT", title,
                f"只有 {len(usable)} 个非空档位，极差算不出来——这条判据此刻不含信息")

    lo = min(usable, key=lambda s: s.mean_gmv)
    hi = max(usable, key=lambda s: s.mean_gmv)
    if lo.mean_gmv <= 0:
        return ("NOINPUT", title, f"底档 {lo.bucket!r} 人均 GMV 为 0，极差是比值，算不出来")
    rng = (hi.mean_gmv - lo.mean_gmv) / lo.mean_gmv

    # 功效：这批数据能分辨出的最小极差。用**通过线**判资格，不用实测极差。
    se_diff = math.hypot(lo.se, hi.se)
    detectable = judge.n_sigma * se_diff / lo.mean_gmv
    shown = (f"极差 {rng:.1%}（{lo.bucket!r} {lo.mean_gmv:,.1f} → "
             f"{hi.bucket!r} {hi.mean_gmv:,.1f}，{len(usable)} 档）")
    if not math.isfinite(detectable) or detectable > line:
        # 需要多少样本：SE ∝ 1/√n，所以 n 要放大 (detectable/line)²。
        # 指出**卡在哪个档**：噪声下限由 SE 最大的那一档主导，而它往往是个结构性瘦档
        # （如 age 的 10 档只装 18~19 两岁、60 档只装 60 岁整）。不写出来，读者会以为
        # 「要 41 倍样本」是效应太弱，而实际是分档口径把两端切薄了。
        bind = max((lo, hi), key=lambda s: s.se)
        need = ("样本量不足以估标准差" if not math.isfinite(detectable)
                else f"约需当前样本的 {(detectable / line) ** 2:.0f} 倍")
        return ("WEAK", title,
                f"{shown}；但这批数据能分辨的最小极差是 {detectable:.1%} > 通过线 "
                f"{line:.0%}（{judge.n_sigma:g}σ，极端两档 n={lo.users:,}/{hi.users:,}）"
                f"——**分辨不出，不是不达标**。瓶颈在 {bind.bucket!r} 档"
                f"（n={bind.users:,}，标准误 {bind.se:,.1f}）；{need}。别据此改生成器。")
    verdict = "PASS" if rng >= line else "FAIL"
    return (verdict, title,
            f"{shown}；此样本可分辨到 {detectable:.1%}，判定有效")


def judge_order(rule: PROF.OrderRule, stats: list[Stat]) -> tuple[str, str, str]:
    """方向约束。极差只管差多少，管不了差在哪个方向。"""
    m = {s.bucket: s for s in stats if s.users > 0}
    title = f"{rule.dim} 方向：{rule.kind} {list(rule.buckets)}"

    if rule.kind == "monotone":
        present = [b for b in rule.buckets if b in m]
        if len(present) < 2:
            return ("NOINPUT", title, f"数据里只有 {present} 这些档位，单调性无从判断")
        seq = [m[b].mean_gmv for b in present]
        bad = [(present[i], present[i + 1]) for i in range(len(seq) - 1)
               if seq[i] > seq[i + 1]]
        note = " < ".join(f"{b}={m[b].mean_gmv:,.0f}" for b in present)
        if bad:
            return ("FAIL", title,
                    f"应按 {present} 递增，实际 {note}；逆序处 {bad}")
        return ("PASS", title, f"{note}（按声明顺序递增）")

    if rule.kind == "peak_within":
        if not m:
            return ("NOINPUT", title, "该维度没有任何非空档位")
        peak = max(m.values(), key=lambda s: s.mean_gmv).bucket
        note = "，".join(f"{s.bucket}={s.mean_gmv:,.0f}"
                        for s in sorted(m.values(), key=lambda s: str(s.bucket)))
        if peak in rule.buckets:
            return ("PASS", title, f"峰值在 {peak} 档，落在 {list(rule.buckets)} 内；{note}")
        return ("FAIL", title,
                f"峰值在 {peak} 档，不在 {list(rule.buckets)} 内；{note}")

    if rule.kind == "not_top":
        hit = [b for b in rule.buckets if b in m]
        if not hit:
            return ("NOINPUT", title,
                    f"{list(rule.buckets)} 在数据里不存在——这条判据此刻不含信息，"
                    f"**不是通过**")
        top = max(m.values(), key=lambda s: s.mean_gmv).bucket
        if top in rule.buckets:
            return ("FAIL", title, f"排名第一的是 {top}，而它不该居首")
        return ("PASS", title, f"排名第一的是 {top}，{hit} 未居首")

    # outranks
    hi, lo = rule.buckets
    if hi not in m or lo not in m:
        return ("NOINPUT", title,
                f"{hi!r} / {lo!r} 至少一个在数据里没有有单用户，比不了")
    if m[hi].mean_gmv > m[lo].mean_gmv:
        return ("PASS", title,
                f"{hi} {m[hi].mean_gmv:,.0f} > {lo} {m[lo].mean_gmv:,.0f}")
    return ("FAIL", title,
            f"{hi} {m[hi].mean_gmv:,.0f} ≤ {lo} {m[lo].mean_gmv:,.0f}——方向反了")


def judge_shape(counts: dict[str, int], judge: PROF.Judge) -> tuple[str, str, str]:
    """职业**人数**分布是否平坦。不关联行为表，所以排在最前面（任务书要求）。"""
    line = judge.occupation_count_min_range
    title = f"occupation 人数极差 ≥ {line:.0%}（全部用户，不关联订单）"
    v = [n for n in counts.values() if n > 0]
    if len(v) < 2:
        return ("NOINPUT", title, f"只有 {len(v)} 个非空职业档，极差算不出来")
    rng = (max(v) - min(v)) / min(v)
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    note = (f"极差 {rng:.1%}（{top[0][0]} {top[0][1]:,} → {top[-1][0]} {top[-1][1]:,}，"
            f"{len(v)} 档）")
    if rng >= line:
        return ("PASS", title, note)
    return ("FAIL", title,
            f"{note}——这种平坦度指向等概率抽样（审计实测 3.3%）")


# ---------------------------------------------------------------- 报告

def _table(dim: str, stats: list[Stat], dims: PROF.Dimensions) -> None:
    """任务书要求的四列：档位 / 用户数 / 人均消费 / 人均订单数。"""
    order = list(dims.buckets(dim))
    seen = {s.bucket: s for s in stats}
    extra = [b for b in seen if b not in order]
    print(f"  {dim}（用户数 = 该档**有有效单**的用户）")
    print(f"    {'档位':<12}{'用户数':>10}{'人均GMV':>12}{'人均单数':>10}{'  标准误':>10}")
    for b in order + extra:
        s = seen.get(b)
        if s is None:
            tag = "（生成器不产）" if b in dims.not_generated.get(dim, ()) else "（无有单用户）"
            print(f"    {str(b):<12}{'-':>10}{'-':>12}{'-':>10}   {tag}")
            continue
        se = f"{s.se:,.1f}" if math.isfinite(s.se) else "n/a"
        flag = "  ← 值域外" if b in extra else ""
        print(f"    {str(b):<12}{s.users:>10,}{s.mean_gmv:>12,.1f}"
              f"{s.mean_orders:>10.2f}{se:>10}{flag}")


def run(stats: dict[str, list[Stat]], occ_counts: dict[str, int],
        dims: PROF.Dimensions, judge: PROF.Judge) -> tuple[int, int]:
    """打印全部报告，返回 (FAIL 数, WEAK 数)。"""
    results: list[tuple[str, str, str]] = []

    # 1. 维度自身的分布。不关联行为表，任务书要求排在前面。
    print("=== 一、维度自身分布（不关联订单） ===\n")
    occ_sorted = sorted(occ_counts.items(), key=lambda kv: -kv[1])
    print(f"  occupation 人数（全部 {sum(occ_counts.values()):,} 个 user_profiles）")
    for k, n in occ_sorted:
        print(f"    {k:<12}{n:>10,}")
    print()
    results.append(judge_shape(occ_counts, judge))
    for verdict, title, note in results:
        print(f"{MARK[verdict]} {title}\n        {note}")

    # 2. 分档汇总表
    print("\n=== 二、各维度分档汇总 ===\n")
    for dim in PROF._ALL_DIMS:
        _table(dim, stats.get(dim, []), dims)
        print()

    # 3. 极差 + 方向
    print("=== 三、判据 ===\n")
    corr: list[tuple[str, str, str]] = []
    for dim in PROF._ALL_DIMS:
        corr.append(judge_range(dim, stats.get(dim, []), judge))
        for rule in judge.rules_for(dim):
            corr.append(judge_order(rule, stats.get(dim, [])))
    for verdict, title, note in corr:
        print(f"{MARK[verdict]} {title}\n        {note}")

    all_r = results + corr
    return (sum(v == "FAIL" for v, _, _ in all_r),
            sum(v == "WEAK" for v, _, _ in all_r))


def main() -> int:
    ap = argparse.ArgumentParser(description="L8 画像 ⟷ 行为相关性")
    ap.add_argument("--from-csv", type=Path, metavar="DIR",
                    help="读生成器产出而不是查云上库（需要 user_profiles.csv + orders.csv）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（无云依赖）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    dims, judge, _gen = PROF.load()          # `_gen` 拿到就扔：检查侧不许读生成参数
    del _gen
    if a.from_csv:
        stats, occ = load_csv(a.from_csv, dims)
        where = f"生成器产出 {a.from_csv}"
    else:
        import athena
        stats, occ = load_athena(athena.Client(), dims)
        where = "Athena 现行库"
    print(f"=== L8 画像 ⟷ 行为相关性 · {where} ===")
    print(f"    口径：人均 GMV = 该档有效单用户的 SUM(actual_amount) 均值，"
          f"有效 = status IN {VALID_STATUS}\n")
    fails, weaks = run(stats, occ, dims, judge)

    print()
    if fails:
        print(f"L8：{fails} 项 FAIL" + (f" · {weaks} 项 WEAK（分辨不出）" if weaks else ""))
        if not a.from_csv:
            print("  云上是 v1 那批独立抽样的画像，项目已决定不重灌，所以这些 FAIL 是对"
                  "现状的记录。\n  生成侧：verify_correlation.py --from-csv <生成目录>。")
    elif weaks:
        print(f"L8：0 项 FAIL，但 {weaks} 项 WEAK —— **这不是通过**。"
              f"\n  WEAK 的意思是这批数据分辨不出通过线那么大的差异，要加样本量再判；"
              f"\n  把它读成绿灯，正是 D-01 这类缺陷当初能活下来的方式。")
    else:
        print("L8：全部通过 ✅")
    return 0                    # 诊断工具，不是闸门。理由同 verify_literals.py


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """三组构造数据：审计实测形态必须红、修复后形态必须绿、样本不足必须判 WEAK。

    只验一侧证明不了判据有区分力（Batch 1 的教训）。这里还额外验一件事：**WEAK 不是
    万能挡箭牌**——同一组均值，样本大判 FAIL、样本小判 WEAK，两者必须分得开。
    """
    dims, judge, gen = PROF.load()
    bad = 0

    def expect(tag: str, got: str, want: str) -> None:
        nonlocal bad
        if got == want:
            print(f"  ok  {tag}")
        else:
            bad += 1
            print(f"  FAIL {tag}\n       期望 {want}，实际 {got}")

    def mk(buckets: dict, users: int = 20_000, sd: float = 900.0,
           orders: float = 3.0) -> list[Stat]:
        """把 {档位: 人均GMV} 造成 Stat 列表。默认样本足够大，功效不构成瓶颈。"""
        return [Stat(bucket=b, users=users, mean_gmv=v, sd_gmv=sd, mean_orders=orders)
                for b, v in buckets.items()]

    # ---- 口径一致性：本文件的有效状态必须和生成器那份相等 ----
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "gen"))
        import tables as T                              # noqa: PLC0415  要 numpy
        expect("有效订单状态与 tables.py::VALID_STATUS 相等（口径不许两份）",
               str(sorted(VALID_STATUS)), str(sorted(T.VALID_STATUS)))
    except ImportError as exc:
        print(f"  --  跳过口径一致性核对（import tables 失败：{exc}）。"
              f"装了 numpy 会自动跑。")

    # ---- 分档函数的 SQL 等价物 ----
    mism = [a for a in range(dims.age_min, dims.age_max + 1)
            if PROF.age_decade(a) != (a // 10) * 10]
    expect(f"BUCKET_SQL['age'] 的 age/10*10 与 PROF.age_decade 在 "
           f"{dims.age_min}~{dims.age_max} 上逐岁一致", str(mism), "[]")

    # ---- 一、审计实测形态：必须 FAIL，且不是 WEAK ----
    print("\n---- 审计实测形态（任务书 D-01 那张表的数）----")
    # 极差 1.4%，且 medium 最低——两条判据各自要红。
    audit_income = mk({"low": 1201.0, "medium": 1192.9, "high": 1205.0,
                       "very_high": 1209.3})
    expect("income 极差 1.4% → FAIL（不是 WEAK：档内 2 万人，功效够）",
           judge_range("income_level", audit_income, judge)[0], "FAIL")
    expect("income medium 垫底 → monotone FAIL",
           judge_order(judge.rules_for("income_level")[0], audit_income)[0], "FAIL")

    # 极差 3.2%，峰值在 60 档。
    audit_age = mk({10: 1200.0, 20: 1195.0, 30: 1192.5, 40: 1210.0,
                    50: 1205.0, 60: 1230.1})
    expect("age 极差 3.2% → FAIL", judge_range("age", audit_age, judge)[0], "FAIL")
    expect("age 峰值在 60 档 → peak_within FAIL",
           judge_order(judge.rules_for("age")[0], audit_age)[0], "FAIL")

    # 极差 4.9%，unknown 最高。
    audit_gender = mk({"female": 1200.0, "male": 1186.7, "unknown": 1245.3})
    expect("gender 极差 4.9% → FAIL", judge_range("gender", audit_gender, judge)[0], "FAIL")
    expect("gender unknown 居首 → not_top FAIL",
           judge_order(judge.rules_for("gender")[0], audit_gender)[0], "FAIL")

    # 极差 7.0%，高消费职业垫底、学生反而更高。审计原文说的是「管理者垫底、学生高于
    # 管理者」，`管理者` 这个取值 2026-08-28 随职业池对齐线上库时删掉了（见 profiles.yaml），
    # 这里换成同样属于高可支配支出、且现在是倍率之首的 `企业主`——被复现的**形态**没变。
    audit_occ = mk({o: 1180.0 for o in dims.occupation})
    audit_occ = [Stat(**{**s.__dict__,
                         "mean_gmv": {"企业主": 1146.7, "学生": 1227.3}.get(s.bucket, 1180.0)})
                 for s in audit_occ]
    expect("occupation 极差 7.0% → FAIL",
           judge_range("occupation", audit_occ, judge)[0], "FAIL")
    expect("学生高于企业主 → outranks FAIL",
           judge_order(judge.rules_for("occupation")[0], audit_occ)[0], "FAIL")

    # 各职业人数 10,288 起、每档 +30，极差 3.3%（档数随 dimensions.occupation 变）。
    flat = {o: 10_288 + i * 30 for i, o in enumerate(dims.occupation)}
    expect("职业人数极差 3.3% → shape FAIL", judge_shape(flat, judge)[0], "FAIL")

    # ---- 二、修复后形态：必须全绿 ----
    # 人均 GMV 按**配置里的倍率**成比例（1200 × 倍率）。这里读 `gen` 是自测的特权：
    # 它在造夹具，不是在判定。判定函数 judge_* 一个都没碰过 gen。
    print("\n---- 修复后形态（人均 GMV ∝ 配置倍率）----")
    base = 1200.0

    def shaped(dim: str) -> list[Stat]:
        m = gen.table(dim)
        return mk({b: base * m[b] for b in dims.reachable(dim)})

    for dim in PROF._ALL_DIMS:
        expect(f"{dim} 按倍率成比例 → 极差 PASS",
               judge_range(dim, shaped(dim), judge)[0], "PASS")
        for r in judge.rules_for(dim):
            want = "NOINPUT" if all(b in dims.not_generated.get(dim, ())
                                    for b in r.buckets) else "PASS"
            expect(f"{dim} {r.kind} → {want}",
                   judge_order(r, shaped(dim))[0], want)
    shaped_counts = {o: int(w * 100) for o, w in gen.occupation_weights.items()}
    expect("职业人数按配置权重 → shape PASS",
           judge_shape(shaped_counts, judge)[0], "PASS")

    # ---- 三、WEAK 必须与 FAIL 分得开 ----
    print("\n---- WEAK ⟷ FAIL：同一组均值，只改样本量 ----")
    small_ok = mk({b: base * gen.table("gender")[b] for b in dims.reachable("gender")},
                  users=60, sd=1800.0)
    expect("35% 的真实极差 + 每档 60 人 → WEAK（分辨不出，不能判 FAIL）",
           judge_range("gender", small_ok, judge)[0], "WEAK")
    expect("同样的 35% 极差 + 每档 2 万人 → PASS（证明上一条不是恒 WEAK）",
           judge_range("gender", shaped("gender"), judge)[0], "PASS")
    # 关键：小样本 + 零效应也判 WEAK。这**不是漏放**——它是在说「这批数据说明不了任何
    # 事」，而报告里 WEAK 不计入通过。功效用通过线算，所以零效应也拿不到 PASS。
    small_flat = mk({"female": 1200.0, "male": 1200.0}, users=60, sd=1800.0)
    expect("零效应 + 每档 60 人 → WEAK 而不是 PASS（WEAK 不计入通过）",
           judge_range("gender", small_flat, judge)[0], "WEAK")
    expect("零效应 + 每档 2 万人 → FAIL（样本够了就必须给出结论）",
           judge_range("gender", mk({"female": 1200.0, "male": 1200.0}), judge)[0], "FAIL")

    # ---- 四、无输入不许当通过 ----
    print("\n---- 「无输入」既不是通过也不是失败 ----")
    expect("gender 数据里没有 unknown → not_top 判 NOINPUT",
           judge_order(judge.rules_for("gender")[0],
                       mk({"female": 1380.0, "male": 1020.0}))[0], "NOINPUT")
    expect("某维度只有一个非空档 → 极差判 NOINPUT",
           judge_range("occupation", mk({"学生": 1200.0}), judge)[0], "NOINPUT")

    # ---- 五、阈值必须是闭的 ----
    print("\n---- 边界：阈值闭区间 ----")
    line = judge.min_range["gender"]
    expect(f"极差恰好等于通过线 {line:.0%} → PASS",
           judge_range("gender", mk({"female": 1200.0 * (1 + line), "male": 1200.0}),
                       judge)[0], "PASS")
    expect("极差差一点点 → FAIL",
           judge_range("gender", mk({"female": 1200.0 * (1 + line) - 1.0, "male": 1200.0}),
                       judge)[0], "FAIL")

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
