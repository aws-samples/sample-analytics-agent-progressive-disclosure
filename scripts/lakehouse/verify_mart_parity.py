#!/usr/bin/env python3
r"""守住手写的集市层：谓词对账（本地）+ 口径恒等式（云上）。

## 为什么需要这个脚本

`database/iceberg/01_tables.sql` 是生成物，`gen_ddl.py --check` 一跑就知道有没有漂。
`database/iceberg/02_mart.sql` **是手写的**（结构性改写没法机械生成，理由见那个文件的
「为什么不生成」），所以它需要一道**别的**守门。

这不是假想的风险。同一个仓库里已经出过一次：

    v1  database/09_mart.sql:67           WHERE status = 'refunded' AND refunded_at IS NOT NULL
    v2  database/redshift/02_mart.sql:67  WHERE refunded_at IS NOT NULL

手写搬运时状态过滤丢了。这批数据上两边算出来一样（refunded_at 非空的行恰好只有
refunded），所以**没有任何测试会红**。而 v2 的 `03_derived.sql` 是 pg_to_redshift.py
从同一份真源生成的，同样的逻辑没漂。手写那半边漂了，生成那半边没漂。

跑 `--against database/redshift/02_mart.sql` 可以看到本脚本把这条真漂移抓出来。

## 两种检查，管的是两类不同的错

**1. `--lint`（默认，无云依赖）—— 谓词有没有在搬运中丢掉**

从 v1 真源里抽出每张表的过滤谓词（`status IN (...)`、`x IS NOT NULL`、
`attribution_type = '...'` 这类），逐条比到目标文件对应的语句上。
v1 有而目标没有 → 报错。目标多出来的 → 只提示（合法的移植会引入
`WHERE rn = 1` 这种改写产物）。

这是**词法**检查，不是语义证明。它管的是"整条过滤条件被漏掉"这一类错 ——
恰好就是真实发生过的那一类。它管不了"谓词写反了"，那由第 2 项兜。

顺带查列数：02_mart.sql 里每张表的列清单写了两遍（CREATE 一遍、INSERT 一遍），
这是不用 CTAS 的代价，两遍不一致就红。

**2. `--numbers`（要云凭证）—— 口径在多条路径上是否自洽**

`test_all.sh` 的 L5 是这个思路，但它把 GMV 写成了字面量 `149685621.44`。
那个值绑死在 v2 那批数据上：重新生成一次数据，测试就红，而代码没问题。

所以这里**一个数据相关的数字都不写**，全部表达成 SQL 恒等式：

    sum(mart_daily_kpi.gmv) == sum(orders.actual_amount) WHERE status IN (...)

数据换了，两边一起变，恒等式照旧成立。真出问题时它才红。

## v1 自带的两处缺陷也被钉在这里

`02_mart.sql` 照搬了 v1 的两个缺陷（不偷偷修，见那个文件的说明）。它们同样写成
恒等式，而不是写成数字：

    mart_daily_kpi 的退款额  ==  只算日期轴之内的退款额        （轴外退款被丢）
    dws_channel_weekly 的花费 ==  按归因扇出重复计数后的花费    （join 扇出虚高）

将来谁修了口径，这两条会红 —— 那是**应该**红：口径变了就得有人确认。
把缺陷钉成期望值，比让它悄悄漂着好。

用法：

    python3 scripts/lakehouse/verify_mart_parity.py                  # 谓词 + 列数（无云）
    python3 scripts/lakehouse/verify_mart_parity.py --numbers        # 加上云上恒等式
    python3 scripts/lakehouse/verify_mart_parity.py --against database/redshift/02_mart.sql
    python3 scripts/lakehouse/verify_mart_parity.py --selftest       # 自测（无云）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

V1_SOURCES = ["database/09_mart.sql", "database/10_derived.sql"]
DEFAULT_TARGET = "database/iceberg/02_mart.sql"

# 13 张衍生表。少一张就是漏搬，多一张就是凭空多出来，两种都该红。
EXPECTED_TABLES = [
    "mart_daily_kpi", "mart_daily_revenue", "mart_channel_daily",
    "mart_user_summary",
    "dwd_orders_valid", "dwd_events_app", "dws_user_daily",
    "dws_channel_weekly", "fin_daily_revenue", "growth_daily_gmv",
    "orders_backup_20251201", "tmp_campaign_roi_analysis",
    # meta_snapshot 不在 v1 真源里（v2 引入的"今天"锚点），谓词对账跳过它，
    # 但它必须存在 —— 静态样本铁律全靠它。
    "meta_snapshot",
]
NOT_IN_V1 = {"meta_snapshot"}

# 抽哪些谓词。只抽"整条过滤条件"级别的东西 —— 这是丢东西最容易发生的粒度。
_PREDICATES = [
    # status IN ('paid','shipped','delivered')
    re.compile(r"\b(\w+)\s+in\s*\(([^)]*)\)", re.IGNORECASE),
    # status = 'refunded' / attribution_type = 'last_touch'
    re.compile(r"\b(\w+)\s*=\s*('(?:[^']|'')*')", re.IGNORECASE),
    # refunded_at IS NOT NULL / user_id IS NULL
    re.compile(r"\b(\w+)\s+is\s+(not\s+null|null)", re.IGNORECASE),
    # placed_at < DATE '2025-12-01'
    re.compile(r"\b(\w+)\s*([<>]=?)\s*date\s+('(?:[^']|'')*')", re.IGNORECASE),
]


def strip_comments(sql: str) -> str:
    """抹掉 `--` 行注释和 `/* */` 块注释，保留字符串字面量。

    必须保留字面量：谓词里的 `'refunded'` 就在字面量里，抹掉就什么都比不了了。
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif two == "/*":
            i += 2
            while i < n and sql[i:i + 2] != "*/":
                i += 1
            i += 2
        elif sql[i] == "'":
            out.append(sql[i])
            i += 1
            while i < n:
                if sql[i] == "'" and sql[i:i + 2] != "''":
                    break
                if sql[i:i + 2] in ("''", "\\'"):
                    out.append(sql[i:i + 2])
                    i += 2
                    continue
                out.append(sql[i])
                i += 1
            if i < n:
                out.append("'")
                i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _norm_pred(m: re.Match) -> str:
    """把一条谓词归一成可比较的字符串。列前缀（o./ua.）和空白都不算差异。"""
    parts = [re.sub(r"^\w+\.", "", g.strip()) for g in m.groups()]
    s = " ".join(" ".join(p.split()) for p in parts).lower()
    # IN 清单里的元素排序无关：('a','b') 与 ('b','a') 是同一个过滤
    s = re.sub(r"\s*,\s*", ",", s)
    if "," in s:
        head, _, rest = s.partition(" ")
        if "," in rest:
            s = head + " " + ",".join(sorted(rest.split(",")))
    return s


# 列注释子句。**抽谓词之前必须先删掉它**，否则注释里的散文会被当成谓词。
# 这不只是噪音，是会漏检的：02_mart.sql 的列注释里写着
# 「只算 status IN (paid, shipped, delivered)」，正好长得像一条谓词。
# 万一真正的 WHERE 把它丢了，多重集会被注释配平，漏检的谓词就看不见了。
# 只删 COMMENT 子句，不动别的字面量 —— 谓词的值（'refunded'）就在字面量里。
_COMMENT_CLAUSE = re.compile(r"\bcomment\s+'(?:[^'\\]|\\.|'')*'", re.IGNORECASE)


def predicates(sql: str) -> Counter:
    c: Counter = Counter()
    body = _COMMENT_CLAUSE.sub(" ", strip_comments(sql))
    for rx in _PREDICATES:
        for m in rx.finditer(body):
            c[_norm_pred(m)] += 1
    return c


def statements(sql: str) -> list[str]:
    """按分号拆语句（注释已抹，字面量里的分号要躲开）。

    字符串字面量里的转义**两种都认**：SQL 标准的 `''`，以及反斜杠的 `\\'`。
    后者是本项目 DDL 的实际写法——Athena 的 Iceberg DDL 用 Hive 味解析器，`''`
    在那里报 `no viable alternative at input`，所以 `gen_ddl.sql_str()` 只能用
    反斜杠（见那个函数）。只认 `''` 的话，注释里一旦出现撇号，`\\'` 会被当成
    字面量结束，后面的 `;` 就被吞进"字符串"里，**整条语句静默消失**——
    然后对账脚本报告"这张表没声明"，指向一个根本不存在的原因。
    """
    body = strip_comments(sql)
    out, cur, i, n = [], [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            cur.append(ch)
            i += 1
            while i < n:
                if body[i] == "\\" and i + 1 < n:       # \' 和 \\ 都整对跳过
                    cur.append(body[i])
                    cur.append(body[i + 1])
                    i += 2
                    continue
                if body[i] == "'":
                    if body[i:i + 2] == "''":           # 标准转义：整对跳过
                        cur.append("''")
                        i += 2
                        continue
                    cur.append("'")                     # 真正的收尾引号
                    i += 1
                    break
                cur.append(body[i])
                i += 1
            continue
        if ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur))
    return [s for s in (x.strip() for x in out) if s]


def by_table(sql: str) -> dict[str, str]:
    """把 SQL 归到目标表名下。

    两种形态都认：
        v1 / v2   CREATE TABLE t AS SELECT ...        建表即写入，一条语句
        iceberg   CREATE TABLE t (...); INSERT INTO t ...   拆成两条

    同一张表的多条语句拼在一起 —— 谓词在哪条里不重要，丢了才重要。
    """
    out: dict[str, list[str]] = {}
    for st in statements(sql):
        m = (re.match(r"\s*create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)",
                      st, re.IGNORECASE)
             or re.match(r"\s*insert\s+into\s+(\w+)", st, re.IGNORECASE))
        if not m:
            continue
        out.setdefault(m.group(1).lower(), []).append(st)
    return {k: "\n".join(v) for k, v in out.items()}


def column_counts(sql: str) -> dict[str, tuple[int, int]]:
    """每张表的 (CREATE 声明列数, INSERT 列清单列数)。没有 INSERT 的记 -1。"""
    out: dict[str, tuple[int, int]] = {}
    for st in statements(sql):
        m = re.match(r"\s*create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)\s*\((.*)\)\s*$",
                     st, re.IGNORECASE | re.DOTALL)
        if m:
            # 列定义按行数；每列一行是本文件的格式约定
            ncol = len([l for l in m.group(2).splitlines() if l.strip()])
            t = m.group(1).lower()
            out[t] = (ncol, out.get(t, (0, -1))[1])
        m = re.match(r"\s*insert\s+into\s+(\w+)\s*\((.*?)\)",
                     st, re.IGNORECASE | re.DOTALL)
        if m:
            t = m.group(1).lower()
            nins = len([x for x in m.group(2).split(",") if x.strip()])
            out[t] = (out.get(t, (-1, 0))[0], nins)
    return out


# ---------------------------------------------------------------- 谓词对账

def lint(target_rel: str) -> int:
    src = {}
    for rel in V1_SOURCES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print(f"❌ 缺真源 {rel}")
            return 1
        src.update(by_table(open(path, encoding="utf-8").read()))

    tgt_path = os.path.join(ROOT, target_rel)
    if not os.path.isfile(tgt_path):
        print(f"❌ 缺目标文件 {target_rel}")
        return 1
    tgt = by_table(open(tgt_path, encoding="utf-8").read())

    bad = 0
    print(f"谓词对账：{' + '.join(V1_SOURCES)}  →  {target_rel}")

    # 表集合
    missing = [t for t in EXPECTED_TABLES if t not in tgt]
    if missing:
        print(f"  ❌ 目标文件缺表：{missing}")
        bad += 1
    extra = sorted(set(tgt) - set(EXPECTED_TABLES))
    if extra:
        print(f"  ⚠️  目标文件多出表（不在 13 张清单里）：{extra}")

    for t in EXPECTED_TABLES:
        if t not in tgt:
            continue
        if t in NOT_IN_V1:
            print(f"  ·  {t:<26} 跳过（v1 真源里没有这张表）")
            continue
        if t not in src:
            print(f"  ❌ {t:<26} v1 真源里找不到，EXPECTED_TABLES 是不是写错了？")
            bad += 1
            continue
        want, got = predicates(src[t]), predicates(tgt[t])
        lost = want - got
        gained = got - want
        if lost:
            print(f"  ❌ {t:<26} 丢了 {len(lost)} 条谓词：")
            for p, k in sorted(lost.items()):
                print(f"        少 {k} 次： {p}")
            bad += 1
        elif gained:
            print(f"  ✅ {t:<26} {sum(want.values())} 条谓词齐全"
                  f"（另有 {sum(gained.values())} 条是移植新增："
                  f"{', '.join(sorted(gained))}）")
        else:
            print(f"  ✅ {t:<26} {sum(want.values())} 条谓词逐条对齐")

    # 列数：CREATE 与 INSERT 的列清单必须一致
    for t, (ncol, nins) in sorted(column_counts(
            open(tgt_path, encoding="utf-8").read()).items()):
        if nins == -1:
            continue
        if ncol != nins:
            print(f"  ❌ {t:<26} CREATE 声明 {ncol} 列，INSERT 列清单 {nins} 列")
            bad += 1

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("\n谓词与列数全部对齐 ✅")
    return 0


# ---------------------------------------------------------------- 口径恒等式

# 一个数据相关的数字都不写：两边都是 SQL，数据换了一起变。
GMV_BASE = ("SELECT CAST(sum(actual_amount) AS decimal(20,2)) FROM orders "
            "WHERE status IN ('paid','shipped','delivered')")
REFUND_BASE = ("SELECT CAST(sum(actual_amount) AS decimal(20,2)) FROM orders "
               "WHERE status = 'refunded' AND refunded_at IS NOT NULL")

IDENTITIES = [
    ("GMV：mart_daily_kpi ≡ orders 基表",
     "SELECT CAST(sum(gmv) AS decimal(20,2)) FROM mart_daily_kpi", GMV_BASE),
    ("GMV：mart_daily_revenue ≡ orders 基表",
     "SELECT CAST(sum(gmv) AS decimal(20,2)) FROM mart_daily_revenue", GMV_BASE),
    ("GMV：growth_daily_gmv ≡ orders 基表",
     "SELECT CAST(sum(gmv) AS decimal(20,2)) FROM growth_daily_gmv", GMV_BASE),
    ("GMV：mart_user_summary ≡ orders 基表",
     "SELECT CAST(sum(total_gmv) AS decimal(20,2)) FROM mart_user_summary", GMV_BASE),
    ("GMV：dwd_orders_valid ≡ orders 基表",
     "SELECT CAST(sum(actual_amount) AS decimal(20,2)) FROM dwd_orders_valid", GMV_BASE),
    ("GMV：dws_user_daily ≡ orders 基表",
     "SELECT CAST(sum(paid_amount) AS decimal(20,2)) FROM dws_user_daily", GMV_BASE),
    ("退款：fin_daily_revenue ≡ orders 基表",
     "SELECT CAST(sum(refund_amount) AS decimal(20,2)) FROM fin_daily_revenue",
     REFUND_BASE),
    ("行数：mart_user_summary ≡ users 全量",
     "SELECT count(*) FROM mart_user_summary", "SELECT count(*) FROM users"),
    ("行数：dwd_orders_valid ≡ 有效订单数",
     "SELECT count(*) FROM dwd_orders_valid",
     "SELECT count(*) FROM orders WHERE status IN ('paid','shipped','delivered')"),
    ("行数：dwd_events_app ≡ 有 user_id 的事件数",
     "SELECT count(*) FROM dwd_events_app",
     "SELECT count(*) FROM events WHERE user_id IS NOT NULL"),
    ("行数：orders_backup_20251201 ≡ 2025-12-01 前的订单",
     "SELECT count(*) FROM orders_backup_20251201",
     "SELECT count(*) FROM orders WHERE placed_at < DATE '2025-12-01'"),
    ("锚点：meta_snapshot.as_of_date ≡ max(mart_daily_kpi.dt)",
     "SELECT as_of_date FROM meta_snapshot",
     "SELECT max(dt) FROM mart_daily_kpi"),
    ("锚点：meta_snapshot.data_start ≡ min(mart_daily_kpi.dt)",
     "SELECT data_start FROM meta_snapshot",
     "SELECT min(dt) FROM mart_daily_kpi"),
    ("噪音表：tmp_campaign_roi_analysis.roi 恒为 NULL",
     "SELECT count(roi) FROM tmp_campaign_roi_analysis", "SELECT 0"),
]

# v1 自带的两处缺陷。**照搬**是刻意的，所以把缺陷本身写成恒等式钉住。
# 见 database/iceberg/02_mart.sql 的「v1 自带的两处缺陷」。
QUIRKS = [
    ("v1 缺陷 1：mart_daily_kpi 的退款只含日期轴之内的部分",
     "SELECT CAST(sum(refund_amt) AS decimal(20,2)) FROM mart_daily_kpi",
     """SELECT CAST(sum(o.actual_amount) AS decimal(20,2)) FROM orders o
        WHERE o.status = 'refunded' AND o.refunded_at IS NOT NULL
          AND CAST(o.refunded_at AS date)
              BETWEEN (SELECT min(dt) FROM mart_daily_kpi)
                  AND (SELECT max(dt) FROM mart_daily_kpi)""",
     "轴外退款被 LEFT JOIN spine 丢掉；要全量退款查 fin_daily_revenue"),
    ("v1 缺陷 2：dws_channel_weekly 的花费被 join 扇出重复计数",
     "SELECT CAST(sum(cost) AS decimal(20,2)) FROM dws_channel_weekly",
     # 复刻扇出：成本行 × 匹配到的归因条数（LEFT JOIN，无匹配仍保 1 行）
     """SELECT CAST(sum(d.cost) AS decimal(20,2))
        FROM channel_daily_costs d
        JOIN channels c ON c.channel_id = d.channel_id
        LEFT JOIN user_attributions ua
          ON ua.channel_id = d.channel_id
         AND ua.attribution_type = 'last_touch'
         AND CAST(ua.attributed_at AS date) >= CAST(date_trunc('week', d.date) AS date)
         AND CAST(ua.attributed_at AS date) <  CAST(date_trunc('week', d.date) AS date)
                                               + interval '7' day""",
     "归因表在聚合前 join，一笔钱被数多遍；要准确花费查 mart_channel_daily"),
]


def check_metric_layer(c) -> int:
    """治理指标层的语义钉子：**编译出来的 SQL 真跑一遍**，验它没有静默给出假答案。

    上面的 IDENTITIES 验的是「表里的数对不对」，这一节验的是「口径层报出来的数
    会不会被读错」。两者会分别漏掉对方：mart_channel_daily 的成本行分毫不差，
    而 cac 曾经在 14 个渠道里给 10 个报「0 元/人」——包括全量花了 25.2 万的
    快手信息流（它的成本行落在窗口外）。表是对的，答案是错的。

    钉四条，都用 SQL 现算、不写死任何数据相关的数字：
      1. 没有任何渠道的 CAC 等于 0。「0 元获客」是个看着像答案的空值，
         agent 会拿它回答"哪个渠道最便宜"。退回裸 SUM(cost) 时这条红。
      2. 有值的渠道数 ≡ 窗口内同时有成本和归因新客的渠道数。防的是过度弃权：
         第 1 条单独存在时，把整个指标改成恒返回 NULL 也能通过。
      3. cac/roi 的**全量**值 ≡ 只算业务日历轴内的值（clamp_to_anchor 生效）。
         成本表铺到 2026-09-01、归因止于 2026-01-24，不 clamp 就是拿轴外的成本
         除轴内的人：全量成本 435.2 万 / 轴内 307.7 万，CAC 被从 17.07 抬到 58.25
         （3.41 倍）。**这份数据上排名不变**——成本在轴内外的分布对九个有投放的渠道
         几乎同比例——错的是量级，而量级正是拿去和客单价 118 元比的那个东西。
         旧数据集里连排名都是反的（「哪个渠道获客成本最低」被答成"微博KOL 938 元/人
         最低"，而它轴内一分钱没花，成本行全在 2026-05-01~05-09），那是这条当初的由来。
      4. refund_amount 取自 fin_daily_revenue 而不是 mart_daily_kpi —— 反向钉住
         clamp 不是全局规则。**2026-09-01 重灌后两个源恰好相等（都是 9,897,495.82）**：
         生成器把订单生命周期时间戳全截在 as_of_date 内，退款轴和订单轴同止于
         2026-01-24，没有退款落在 mart_daily_kpi 的轴外。所以这条现在只能校验
         **取数源**、校验不了数值差额（旧数据集上退款轴比订单轴长 9 天，
         那 9 天值 38,089.59，换源就退回 925471.33 那个 bug）。少的是能暴露差额的
         数据，不是缺陷本身——所以这条判据留着，别因为两边相等就把源换回 mart。
    """
    sys.path.append(os.path.join(ROOT, "backend"))
    from metric_layer import compile_metric

    bad = 0
    print("\n治理指标层语义（编译真 SQL 跑，验的是「答案会不会被读错」）")
    cac_sql = compile_metric("cac", group_by=["channel"],
                             time_window="last_30d")["sql"]
    try:
        rows = c.execute(cac_sql, timeout=600)["rows"]
    except Exception as e:
        print(f"  ❌ cac 按渠道编译执行失败 {' '.join(str(e).split())[:110]}")
        return 1

    zero = [str(r[0]) for r in rows if r[1] is not None and float(r[1]) == 0.0]
    if zero:
        print(f"  ❌ CAC 报出「0 元/人」的渠道 {zero}"
              f"\n        分子应为 NULLIF(SUM(cost),0)：无成本记录是「不适用」，不是「免费」")
        bad += 1
    else:
        print(f"  ✅ {'CAC 无「0 元/人」假答案（无成本记录→NULL）':<48} "
              f"{len(rows)} 个渠道")

    have = sum(1 for r in rows if r[1] is not None)
    expect = c.execute(
        "SELECT count(*) FROM (SELECT channel_name FROM mart_channel_daily "
        "WHERE dt > ((SELECT max(as_of_date) FROM meta_snapshot) - interval '30' day) "
        "AND dt <= (SELECT max(as_of_date) FROM meta_snapshot) "
        "GROUP BY channel_name "
        "HAVING sum(cost) <> 0 AND sum(new_users_attributed) <> 0)",
        timeout=600)["rows"][0][0]
    if str(have) == str(expect):
        print(f"  ✅ {'CAC 有值的渠道 ≡ 有成本且有归因新客的渠道（未过度弃权）':<48} "
              f"{have}")
    else:
        print(f"  ❌ CAC 有值 {have} ≠ 应有值 {expect}（弃权过度或不足）")
        bad += 1

    # 3 + 4：clamp_to_anchor 是按指标声明的，两个方向都要钉。
    # 手写的右侧 SQL 刻意**不复用编译器**：两边都走编译器的话，把 clamp 整体删掉
    # 也会一起变、测试照样绿。这里右侧是独立表达的"轴内"定义。
    IN_AXIS = "dt <= (SELECT max(as_of_date) FROM meta_snapshot)"
    clamp_cases = [
        ("cac 全量 ≡ 仅轴内成本（clamp 生效）", "cac", True,
         f"SELECT CAST(NULLIF(sum(cost),0) AS REAL)/NULLIF(sum(new_users_attributed),0) "
         f"FROM mart_channel_daily WHERE {IN_AXIS}"),
        ("roi 全量 ≡ 仅轴内成本（clamp 生效）", "roi", True,
         f"SELECT CAST(sum(gmv_attributed) AS REAL)/NULLIF(sum(cost),0) "
         f"FROM mart_channel_daily WHERE {IN_AXIS}"),
        ("退款全量 ≠ 轴内（clamp 未被提成全局开关）", "refund_amount", False,
         f"SELECT CAST(sum(refund_amount) AS decimal(20,2)) "
         f"FROM fin_daily_revenue WHERE {IN_AXIS}"),
    ]
    # 先钉**编译产物**里 clamp 谓词的有无，再比数值。两步分开是 2026-09-01 的教训：
    # 只比数值时，「轴外没有数据」和「clamp 把轴外砍了」给出同一个结果，而这一条判据
    # 会把前者报成后者。新生成器的 `refunded_at` 不越过 as_of_date（`as_of_date` =
    # max(mart_daily_kpi.dt)，两边同一天收尾），于是退款那条的数值比对当场失去区分力，
    # 报出来的却是「已修掉的那个偏低 4% 的 bug 回来了」——结论和事实相反。
    # 谓词那一步不依赖数据形状，是这三条真正想问的东西：clamp 是按指标声明的，不是全局开关。
    for name, metric, want_equal, rhs in clamp_cases:
        lhs = compile_metric(metric, time_window="all")["sql"]
        has_clamp = IN_AXIS.lower() in " ".join(lhs.lower().split())
        if has_clamp != want_equal:
            what = "该带 clamp 却没带" if want_equal else "不该带 clamp 却带上了"
            print(f"  ❌ {metric} 的编译 SQL {what}\n        {lhs}")
            bad += 1
            continue
        print(f"  ✅ {('编译 SQL 里 clamp 谓词' + ('在' if want_equal else '不在')):<48} {metric}")
        try:
            a = c.execute(lhs, timeout=600)["rows"][0][0]
            b = c.execute(rhs, timeout=600)["rows"][0][0]
        except Exception as e:
            print(f"  ❌ {name:<48} 查询失败 {' '.join(str(e).split())[:90]}")
            bad += 1
            continue
        equal = str(a) == str(b)
        if equal == want_equal:
            print(f"  ✅ {name:<48} {a}"
                  + ("" if want_equal else f"  vs 轴内 {b}"))
        elif want_equal:
            print(f"  ❌ {name:<48} 全量 {a} ≠ 轴内 {b}"
                  f"\n        clamp_to_anchor 没生效：轴外成本没有归因可配，会静默给错数")
            bad += 1
        else:
            # 走到这里只剩一种可能：上面那步已经确认编译 SQL 里没有 clamp，所以两值相等
            # 只能是数据里根本没有轴外的量。判据在这批数据上无区分力，说清楚，别判红也
            # 别假装绿——恢复区分力的条件是数据里出现越过 as_of_date 的退款。
            print(f"  ⚠️  {name:<48} 全量 {a} == 轴内 {b}")
            print(f"        这批数据里没有越过 as_of_date 的 {metric}，数值比对无区分力。")
            print(f"        clamp 的有无已由上一条（编译 SQL 里没有 {IN_AXIS}）钉住。")
    return bad


def check_numbers() -> int:
    import athena
    c = athena.Client()
    bad = 0

    print("\n口径恒等式（两边都是 SQL，不写死任何数据相关的数字）")
    for name, lhs, rhs in IDENTITIES:
        try:
            a = c.execute(lhs, timeout=600)["rows"][0][0]
            b = c.execute(rhs, timeout=600)["rows"][0][0]
        except Exception as e:
            print(f"  ❌ {name:<48} 查询失败 {' '.join(str(e).split())[:110]}")
            bad += 1
            continue
        if str(a) == str(b):
            print(f"  ✅ {name:<48} {a}")
        else:
            print(f"  ❌ {name:<48} 左 {a} ≠ 右 {b}")
            bad += 1

    print("\nv1 自带缺陷（照搬，钉成期望值；这里红了说明有人改了口径，要有人确认）")
    for name, lhs, rhs, why in QUIRKS:
        try:
            a = c.execute(lhs, timeout=600)["rows"][0][0]
            b = c.execute(rhs, timeout=600)["rows"][0][0]
        except Exception as e:
            print(f"  ❌ {name:<48} 查询失败 {' '.join(str(e).split())[:110]}")
            bad += 1
            continue
        if str(a) == str(b):
            print(f"  ✅ {name}\n        {a}   （{why}）")
        else:
            print(f"  ❌ {name}\n        实际 {a} ≠ 复刻 {b}   （{why}）")
            bad += 1

    bad += check_metric_layer(c)

    if bad:
        print(f"\n{bad} 项不符 ❌")
        return 1
    print(f"\n{len(IDENTITIES)} 条恒等式 + {len(QUIRKS)} 条缺陷锚点 "
          f"+ 5 条指标层语义钉子全部成立 ✅")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    bad = 0

    # strip_comments：字面量要留着，注释要没
    for src, want in [
        ("a -- 注释\nb", "a \nb"),
        ("a /* 块\n注释 */ b", "a  b"),
        ("WHERE s = '--not a comment'", "WHERE s = '--not a comment'"),
        ("WHERE s = '/* nor this */'", "WHERE s = '/* nor this */'"),
    ]:
        got = strip_comments(src)
        if got != want:
            print(f"  ❌ strip_comments({src!r}) → {got!r}，期望 {want!r}")
            bad += 1

    # 谓词归一：列前缀、空白、IN 清单顺序都不算差异
    same = [
        ("WHERE o.status IN ('paid','shipped')",
         "WHERE status IN ('shipped',  'paid')"),
        ("WHERE ua.attribution_type = 'last_touch'",
         "WHERE attribution_type='last_touch'"),
        ("WHERE refunded_at IS NOT NULL", "WHERE o.refunded_at  is  not  null"),
    ]
    for a, b in same:
        if predicates(a) != predicates(b):
            print(f"  ❌ 该归一成同一条却没有：{a!r} vs {b!r}\n"
                  f"     {predicates(a)} vs {predicates(b)}")
            bad += 1

    # 必须能区分的：丢掉状态过滤要被看见（这就是真实发生过的那次漂移）
    v1 = "WHERE status = 'refunded' AND refunded_at IS NOT NULL"
    v2 = "WHERE refunded_at IS NOT NULL"
    lost = predicates(v1) - predicates(v2)
    if "status 'refunded'" not in lost:
        print(f"  ❌ 没抓出丢掉的状态过滤，抓到的是 {dict(lost)}")
        bad += 1

    # IS NOT NULL 与 IS NULL 不能混为一谈
    if predicates("x IS NOT NULL") == predicates("x IS NULL"):
        print("  ❌ IS NOT NULL 和 IS NULL 被归一成同一条了")
        bad += 1

    # 列注释里的散文不能被当成谓词 —— 否则注释会把丢掉的谓词配平，直接漏检
    if predicates("a int COMMENT '只算 status IN (paid, shipped)'"):
        print("  ❌ 列注释里的散文被当成谓词抽出来了："
              f"{dict(predicates(chr(39).join(['a int COMMENT ', '只算 status IN (paid)', ''])))}")
        bad += 1
    # 关键场景：WHERE 丢了状态过滤，但列注释里提到它 —— 必须仍然抓出来
    v1_c = "WHERE status = 'refunded' AND refunded_at IS NOT NULL"
    v2_c = ("x int COMMENT '退款额，口径 status = \\'refunded\\'' "
            "WHERE refunded_at IS NOT NULL")
    if "status 'refunded'" not in (predicates(v1_c) - predicates(v2_c)):
        print("  ❌ 列注释提到该谓词时，丢掉的 WHERE 条件被配平了（会漏检）")
        bad += 1

    # 语句拆分：字面量里的分号不能当分隔符。转义引号两种写法都要认——
    # 认错的后果不是报错而是**整条语句静默消失**，下游会报告一个不存在的原因。
    for label, src in [
        ("裸分号", "SELECT ';' AS a; SELECT 2"),
        ("标准转义 ''", "SELECT 'a''b;c' AS a; SELECT 2"),
        ("反斜杠转义 \\' （本项目 DDL 的写法）", r"SELECT 'don\'t;stop' AS a; SELECT 2"),
        ("字面量末尾的 \\\\", r"SELECT 'trail\\' AS a; SELECT 2"),
    ]:
        st = statements(src)
        if len(st) != 2:
            print(f"  ❌ 语句拆分（{label}）拆成了 {len(st)} 条：{st}")
            bad += 1

    # by_table：两种形态都要认到同一张表
    bt = by_table("CREATE TABLE t (a int); INSERT INTO t (a) SELECT 1;")
    if set(bt) != {"t"}:
        print(f"  ❌ CREATE + INSERT 没归到同一张表：{list(bt)}")
        bad += 1
    bt = by_table("CREATE TABLE t AS SELECT 1 AS a;")
    if set(bt) != {"t"}:
        print(f"  ❌ CTAS 形态没认出来：{list(bt)}")
        bad += 1

    # 列数：CREATE 与 INSERT 对不上要能看出来
    cc = column_counts("CREATE TABLE t (\n a int,\n b int\n);"
                       "\nINSERT INTO t (a) SELECT 1;")
    if cc.get("t") != (2, 1):
        print(f"  ❌ 列数没数对：{cc}")
        bad += 1

    # 真源 + 目标文件必须都能跑通 lint 的解析（不判结果，只判解析）
    for rel in V1_SOURCES + [DEFAULT_TARGET]:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print(f"  ❌ 缺文件 {rel}")
            bad += 1
            continue
        n = len(by_table(open(path, encoding="utf-8").read()))
        if n == 0:
            print(f"  ❌ 从 {rel} 一张表都没解析出来")
            bad += 1

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  注释抹除 4 例、谓词归一 3 例、区分能力 4 项、"
          f"语句拆分 4 项、表归属 2 项、列数 1 项、真源解析 {len(V1_SOURCES) + 1} 项")
    print("全部通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="守住手写的集市层：谓词对账 + 口径恒等式")
    ap.add_argument("--against", default=DEFAULT_TARGET,
                    help=f"要对账的目标文件（默认 {DEFAULT_TARGET}）")
    ap.add_argument("--numbers", action="store_true",
                    help="加做云上的口径恒等式检查（需要 AWS 凭证）")
    ap.add_argument("--selftest", action="store_true", help="自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    rc = lint(a.against)
    if a.numbers:
        rc |= check_numbers()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
