#!/usr/bin/env python3
"""查询级正确性：JOIN、窗口、NULL 语义、时间、除零，三条 arm 逐位比。

## 这一层补的是什么

`correctness.py` 比的是**单表聚合**——行数、求和、时间边界、布尔计数。那一层全绿只
证明「三条 arm 装的是同一批数据」，不证明「三条 arm 对同一个问题给同一个答案」。
agent 写的 SQL 里真正容易分叉的东西一条都不在那一层里：JOIN 的 NULL 键、窗口函数的
默认帧、`SUM` 在空集上是 NULL 还是 0、整数相除、除零、CJK 排序、`date_trunc` 的
渲染。这个文件比的就是这些。

## 判据：三方投票 + 不变量，两者都要

- **三方投票**定位「哪一条 arm 不一样」。三条里有一条不同，那一条就是嫌疑人。
- **不变量**抓投票抓不到的那一类：**三条一致地错**。投票对这种情况永远是绿的。
  所以每个用例都可以带一个 `invariant`，它是按 SQL 语义**独立**算出来的期望
  （行数取自装载快照，不硬编码——数据会重灌）。

两条缺一不可。只有投票，三条 arm 一起错的时候报绿；只有不变量，就退化成
「每条 arm 各自跑通」而不是在做对比。

## 两类用例，不能混在一起判

- `must_match=True`——**闸门**。按 SQL 标准三条 arm 就该一样，不一样是缺陷。
- `must_match=False`——**探针**。已知方言层面可能不同（整数相除、除零、
  `ROUND` 的进位方向、CJK 排序）。这里的分叉是**发现**，不是失败：它要被记下来
  写进对比结论，而不是让闸门永远红着。把探针也当闸门，结果是这个脚本从第一天
  起就是红的，红久了就没人看了。

## 报错也是一种结果

除零这类用例里，「抛异常」和「返回 NULL」是两条 arm 的真实差异。所以执行失败不
中断整轮，而是把错误类型记成这一格的值参与比较——真差异要看得见，不能被
traceback 吃掉。

用法：

    python3 scripts/bench/query_correctness.py                 # 全部用例
    python3 scripts/bench/query_correctness.py --gate-only     # 只跑闸门用例
    python3 scripts/bench/query_correctness.py -k join -k null # 按 key 子串筛
    python3 scripts/bench/query_correctness.py --arm athena --arm duckdb
    python3 scripts/bench/query_correctness.py --values        # 扫全部文本列取值，arm 互比
    python3 scripts/bench/query_correctness.py --values -t orders
    python3 scripts/bench/query_correctness.py --selftest      # 判定逻辑自测，不连引擎

`--values` 是独立的一件事，不在上面那些用例里：`correctness.py` 那层比行数、数值求和、
时间边界、布尔计数，**文本列一项都不占**。所以「行数与金额都一样，但某个文本列在其中
两条 arm 上是同一个假常量」在那层永远报绿。这一轮实测就是这么发现的。

受治理的三列走单独一条通路：Redshift 的动态脱敏在聚合前生效，`users.email` /
`users.phone` / `user_profiles.birth_date` 在那条 arm 上**本该**不同，所以它们比的是
「对得上脱敏策略自身的变换」，通过时出治理说明、不计入差异（登记表是
`correctness.MASKED`，两个闸门共用一份）。豁免不是跳过：明文 arm 之间照旧互比，脱敏值
对不上策略照旧报差异。这一档也写 trace（`data/bench/query-values-<时间戳>.jsonl`）——
它要跑十几分钟，没有 trace 的话「没跑过」和「跑过且干净」在事后是分不出来的。

环境变量：`AWS_REGION`、`REDSHIFT_SECRET_ARN`、`BENCH_TRACE_S3`（云上跑要设）。
"""
from __future__ import annotations

import argparse
import decimal
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import arms as A                      # noqa: E402
import correctness as C               # noqa: E402  （借它的 trace 落地和上传）


# ---------------------------------------------------------------- 装载快照

def snapshot() -> dict[str, int]:
    """行数真源。不变量里所有「应该是多少行」都从这里取。

    **不硬编码**：数据会全量重灌，硬编码的数字会在重灌那天变成假失败，而假失败
    比没有检查更坏——它会让人去查引擎，而问题在这一行常量里。
    """
    p = ROOT / "data" / "loaded_row_counts.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    t = d.get("tables", d)
    return {k: int(v) for k, v in t.items() if isinstance(v, (int, str))}


# ---------------------------------------------------------------- 值归一

def num(v: object) -> decimal.Decimal | None:
    """能当数看就返回 Decimal，否则 None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return decimal.Decimal(str(v))
    except (decimal.InvalidOperation, ValueError, TypeError):
        return None


def cell(v: object, sig: int = 0) -> str:
    """把一格化成可比字符串。

    数值走 `normalize()`：`3.00` 和 `3` 是同一个数，必须比成相等；而 `3` 和 `3.5`
    **必须**比成不等——整数相除那个用例全靠这一条才看得见差异。所以不能用固定小数位
    格式化（那会把 3 和 3.5 都变成 3.5000 之外的东西，或者把差异磨平）。

    `sig > 0` 时数值只比前 `sig` 位有效数字。**只给浮点结果用**，理由是实测出来的：
    同一个 `SUM(CAST(x AS DOUBLE)/y)`，DuckDB 回 140681100.35400835、Redshift 回
    140681100.3540105——两边都没错，浮点求和的累加顺序由引擎的并行计划决定，末几位
    本来就不该相等。对浮点要求逐位相等等于要求两个引擎用同一个执行计划，那不是正确性。
    整数和 DECIMAL 结果**不要**开这个，它们本来就该逐位相等，开了会把真差异磨掉。
    """
    if v is None:
        return "<NULL>"
    if isinstance(v, bool):
        return "true" if v else "false"
    n = num(v)
    if n is not None:
        if sig > 0:
            n = decimal.Context(prec=sig).create_decimal(n)
        # 指数形式（1E+3）要展开成 1000，否则同一个数两种写法比成不等
        return format(n.normalize(), "f")
    return str(v)


def rows_key(rows: list, ordered: bool, sig: int = 0) -> list[tuple[str, ...]]:
    """整个结果集的可比形态。

    `ordered=False` 时按行内容排序——大多数用例只关心集合相等，而引擎返回顺序在没有
    `ORDER BY` 时是未定义的，不排的话会量到一堆假差异。`ordered=True` 留给**就是在比
    顺序**的用例（NULL 排在哪一头），那时顺序本身是被测对象，排序会把它擦掉。
    """
    out = [tuple(cell(c, sig) for c in r) for r in rows]
    return out if ordered else sorted(out)


ERR = "<报错>"


def run_one(arm: A.Arm, sql: str) -> tuple[list, str, float]:
    """跑一条，返回 (rows, 错误摘要, 耗时ms)。错误不抛出——它是一种结果。"""
    t0 = time.time()
    try:
        r = arm.client.execute(sql)
        return r.get("rows") or [], "", float(r.get("elapsed_ms") or
                                              (time.time() - t0) * 1000)
    except Exception as e:                                   # noqa: BLE001
        # 只留第一行：Athena 会把整条 SQL 拼进报错，全留会把 trace 撑爆
        msg = str(e).strip().splitlines()[0][:200]
        return [], f"{type(e).__name__}: {msg}", (time.time() - t0) * 1000


# ---------------------------------------------------------------- 用例

def _t(snap: dict[str, int], name: str) -> int:
    return snap.get(name, -1)


#: 每个用例：
#:   key         短名，`-k` 按子串筛
#:   why         这一条在测什么危险（写清楚，否则半年后没人知道为什么有这一条）
#:   sql         三条 arm 共用的 SQL
#:   by_arm      只在方言逼着不同时覆盖。**能共用就共用**：手写三份等价 SQL 是这套
#:               harness 最大的自伤来源——写歪一份，报出来的「引擎差异」其实是我的
#:               笔误，而它和真差异长得一模一样。
#:   must_match  True=闸门，False=探针（分叉是发现）
#:   ordered     结果集是否按引擎返回顺序比
#:   invariant   fn(rows) -> 问题描述 or None，按 SQL 语义独立算期望
CASES: list[dict] = [
    {
        "key": "join-null-key",
        "why": "channel_daily_costs.creative_id 整列 NULL。NULL 键在 INNER JOIN 里"
               "一行都匹配不上（NULL = NULL 不为真），在 LEFT JOIN 里全部保留。"
               "这是「JOIN 完静默变成 0 行」这类事故的原型，而它不报错。",
        "sql": """
            SELECT
              (SELECT count(*) FROM channel_daily_costs c
                 JOIN ad_creatives a ON c.creative_id = a.creative_id) AS inner_rows,
              (SELECT count(*) FROM channel_daily_costs c
                 LEFT JOIN ad_creatives a ON c.creative_id = a.creative_id) AS left_rows
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[0][0]) == "0"
            and cell(rows[0][1]) == str(_t(snap, "channel_daily_costs"))
            else f"期望 INNER=0、LEFT={_t(snap, 'channel_daily_costs')}，"
                 f"实得 {[cell(x) for x in (rows[0] if rows else [])]}"),
    },
    {
        "key": "join-fanout",
        "why": "一对多 JOIN 之后再 SUM 左表的金额＝按右表行数重复计数。"
               "这是 agent 最常犯的一类错，而结果看起来完全合理（就是偏大）。"
               "这里把三个数一起取出来，让「放大了」这件事在数字上成立。",
        "sql": """
            SELECT count(*) AS joined_rows,
                   count(DISTINCT o.order_id) AS distinct_orders,
                   SUM(o.actual_amount) AS inflated_sum,
                   SUM(i.quantity) AS item_qty
            FROM orders o JOIN order_items i ON o.order_id = i.order_id
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[0][0]) == str(_t(snap, "order_items"))
            and (num(rows[0][1]) or 0) <= _t(snap, "orders")
            else f"期望 joined_rows={_t(snap, 'order_items')}（每个 item 一行）"
                 f"且 distinct_orders≤{_t(snap, 'orders')}，"
                 f"实得 {[cell(x) for x in (rows[0] if rows else [])][:2]}"),
    },
    {
        "key": "null-empty-agg",
        "why": "空集上的聚合：SUM 是 NULL 不是 0，COUNT 是 0，AVG 是 NULL。"
               "把 SUM 当 0 用的 SQL 在有数据时一直是对的，只在筛空的那天错，"
               "而那天通常是季度末对不上账的那天。",
        "sql": """
            SELECT SUM(CASE WHEN status = '__不存在的状态__' THEN total_amount END) AS sum_empty,
                   COUNT(CASE WHEN status = '__不存在的状态__' THEN total_amount END) AS cnt_empty,
                   AVG(CASE WHEN status = '__不存在的状态__' THEN total_amount END) AS avg_empty,
                   COUNT(*) AS all_rows
            FROM orders
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[0][0]) == "<NULL>" and cell(rows[0][1]) == "0"
            and cell(rows[0][2]) == "<NULL>"
            else f"期望 SUM=NULL、COUNT=0、AVG=NULL，"
                 f"实得 {[cell(x) for x in (rows[0] if rows else [])][:3]}"),
    },
    {
        "key": "null-compare",
        "why": "`= NULL` 永远不为真（要用 IS NULL），而 `<> '值'` 会把 NULL 行一起"
               "丢掉。两条都不报错，都只是少算。"
               "2026-09-02 之前这一条是**退化的**：那时 `cancel_reason` 只有一个值，"
               "`<> '用户取消'` 恒得 0，于是「把 NULL 丢掉了」和「没有别的值可匹配」"
               "在结果里长得一样。现在这一列有 8 个值，`neq_val` 是 54,417——比全部"
               "非空行少的那些正是 NULL，判据这才真的判得出东西。",
        "sql": """
            SELECT COUNT(CASE WHEN cancel_reason = NULL THEN 1 END) AS eq_null,
                   COUNT(CASE WHEN cancel_reason IS NULL THEN 1 END) AS is_null,
                   COUNT(CASE WHEN cancel_reason <> '用户取消' THEN 1 END) AS neq_val,
                   COUNT(cancel_reason) AS non_null,
                   COUNT(*) AS all_rows
            FROM orders
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[0][0]) == "0"
            and (num(rows[0][1]) or 0) + (num(rows[0][3]) or 0) == _t(snap, "orders")
            else f"期望 `=NULL` 计数为 0 且 IS NULL + 非空 = "
                 f"{_t(snap, 'orders')}，实得 "
                 f"{[cell(x) for x in (rows[0] if rows else [])][:4]}"),
    },
    {
        "key": "null-group-order",
        "why": "GROUP BY 把 NULL 当成一个独立分组，而 ORDER BY 时 NULL 排在哪一头"
               "由引擎默认决定。这一条**按返回顺序比**，顺序本身就是被测对象。",
        "sql": """
            SELECT cancel_reason, count(*) AS n
            FROM orders GROUP BY cancel_reason ORDER BY cancel_reason
        """,
        "must_match": False,       # NULL 排序方向是方言属性，分叉是发现
        "ordered": True,
        "invariant": lambda rows, snap: (
            None if sum(int(num(r[1]) or 0) for r in rows) == _t(snap, "orders")
            else f"各分组行数之和 {sum(int(num(r[1]) or 0) for r in rows)} "
                 f"≠ 全表 {_t(snap, 'orders')}（NULL 分组丢了？）"),
    },
    {
        "key": "window-rows-frame",
        "why": "窗口函数里**三条 arm 都能表达**的那个子集：ROWS 帧写全，加 RANK / "
               "DENSE_RANK / ROW_NUMBER 在并列上的三种行为。并列是造出来的（`% 2`）——"
               "没有并列的数据上这三个函数结果相同，测不出东西来。"
               "这一条是闸门，也是给 agent 的可移植窗口写法基线。",
        "sql": """
            SELECT status, n, grp,
                   RANK()       OVER (ORDER BY grp) AS rnk,
                   DENSE_RANK() OVER (ORDER BY grp) AS dense,
                   ROW_NUMBER() OVER (ORDER BY grp, status) AS rn,
                   SUM(n) OVER (ORDER BY grp, status
                                ROWS BETWEEN UNBOUNDED PRECEDING
                                         AND CURRENT ROW) AS running_rows
            FROM (SELECT status, count(*) AS n, count(*) % 2 AS grp
                    FROM orders GROUP BY status) t
            ORDER BY grp, status
        """,
        "must_match": True,
        "ordered": True,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[-1][6]) == str(_t(snap, "orders"))
            else f"ROWS 累计到最后一行应等于全表 {_t(snap, 'orders')}，"
                 f"实得 {cell(rows[-1][6]) if rows else '无行'}"),
    },
    {
        "key": "window-range-frame",
        "why": "把上一条的 ROWS 换成 RANGE（含并列同伴）。Trino 和 DuckDB 支持，"
               "**Redshift 直接说没实现**：`RANGE clause of window functions not yet "
               "implemented`。含义比一句报错要重：RANGE 到当前行**就是 SQL 标准的默认"
               "帧**，所以在 Redshift 上标准默认帧是不可表达的——键上有并列时的累计值"
               "在 Redshift 里只能是 ROWS 语义，没有可移植写法。",
        "sql": """
            SELECT status, SUM(n) OVER (ORDER BY grp
                                        RANGE BETWEEN UNBOUNDED PRECEDING
                                                  AND CURRENT ROW) AS running_range
            FROM (SELECT status, count(*) AS n, count(*) % 2 AS grp
                    FROM orders GROUP BY status) t
            ORDER BY status
        """,
        "must_match": False,
        "ordered": True,
    },
    {
        "key": "window-implicit-frame",
        "why": "同一个窗口**不写帧**。Trino 和 DuckDB 按标准补上默认帧，Redshift 拒绝"
               "执行：`Aggregate window functions with an ORDER BY clause require a "
               "frame clause`。配合上一条看：Redshift 既不接受省略帧，又不实现标准"
               "默认帧本身，两条一起决定了「累计求和」这类写法必须按 arm 分开写。"
               "报的是语法错不是结果错，所以它算「失败恢复成本」不算正确性。",
        "sql": """
            SELECT status, SUM(n) OVER (ORDER BY grp) AS running
            FROM (SELECT status, count(*) AS n, count(*) % 2 AS grp
                    FROM orders GROUP BY status) t
            ORDER BY status
        """,
        "must_match": False,
        "ordered": True,
    },
    {
        "key": "time-trunc-month",
        "why": "按月分桶是最常见的一类问题，而 `date_trunc` 的**渲染**三边写法不同。"
               "这一条同时验分桶结果一致和三种渲染写法等价。",
        "by_arm": {
            "athena": """
                SELECT format_datetime(date_trunc('month', placed_at), 'yyyy-MM') AS m,
                       count(*) AS n
                FROM orders GROUP BY 1 ORDER BY 1
            """,
            "duckdb": """
                SELECT strftime(date_trunc('month', placed_at), '%Y-%m') AS m,
                       count(*) AS n
                FROM orders GROUP BY 1 ORDER BY 1
            """,
            "redshift": """
                SELECT TO_CHAR(date_trunc('month', placed_at), 'YYYY-MM') AS m,
                       count(*) AS n
                FROM orders GROUP BY 1 ORDER BY 1
            """,
        },
        "must_match": True,
        "ordered": True,
        "invariant": lambda rows, snap: (
            None if sum(int(num(r[1]) or 0) for r in rows) == _t(snap, "orders")
            else f"各月行数之和 {sum(int(num(r[1]) or 0) for r in rows)} "
                 f"≠ 全表 {_t(snap, 'orders')}"),
    },
    {
        "key": "distinct-exact",
        "why": "精确去重计数。approx_* 三边函数名不同且本就是近似值，不放进闸门；"
               "精确去重必须一致。",
        "sql": """
            SELECT count(DISTINCT user_id) AS du,
                   count(DISTINCT status) AS ds
            FROM orders
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and 0 < (num(rows[0][0]) or 0) <= _t(snap, "users")
            else f"去重用户数应在 (0, {_t(snap, 'users')}] 内，"
                 f"实得 {cell(rows[0][0]) if rows else '无行'}"),
    },
    {
        "key": "int-division",
        "why": "整数相除：Trino 和 Redshift 走整数除法（截断），DuckDB 的 `/` 是"
               "浮点除法。**这一条是探针**——它是方言属性，不是缺陷，但它会让"
               "同一句 SQL 在三条 arm 上给出不同的数，且不报错。",
        "sql": "SELECT 7/2 AS int_div, 7.0/2 AS dec_div, CAST(7 AS DOUBLE)/2 AS dbl_div",
        "by_arm": {
            # Redshift 的 DOUBLE 要写全名 DOUBLE PRECISION
            "redshift": "SELECT 7/2 AS int_div, 7.0/2 AS dec_div, "
                        "CAST(7 AS DOUBLE PRECISION)/2 AS dbl_div",
        },
        "must_match": False,
    },
    {
        "key": "div-by-zero",
        "why": "除零：有的引擎抛错，有的回 NULL。抛错的那条 arm 上 agent 会看到"
               "一次失败并可能重试，回 NULL 的那条会安静地把 NULL 带进报表。"
               "第二列是**可移植的写法**——三条 arm 都该给 NULL。",
        "sql": "SELECT 1/0 AS raw_div",
        "must_match": False,
    },
    {
        "key": "ratio-decimal-raw",
        "why": "DECIMAL ÷ INTEGER，不加任何 CAST——也就是 agent 最自然会写出来的那种"
               "客单价。三条 arm 给的结果类型不同：Trino 按 DECIMAL 规则定结果标度并"
               "逐行进位，DuckDB 和 Redshift 走浮点。实测 Athena 140681679、"
               "DuckDB 140681100.354，差了 578——比例类指标不写 CAST 就不是可移植的。"
               "**探针**：三边都不算错，是类型推导规则不同。",
        "sql": """
            SELECT SUM(CASE WHEN item_count = 0 THEN NULL
                            ELSE actual_amount / item_count END) AS raw_ratio
            FROM orders
        """,
        "must_match": False,
    },
    {
        "key": "ratio-portable",
        "why": "上一条的可移植写法：显式 CAST 成双精度再除。这一条是闸门——连规避手段"
               "都不通用的话，比例类指标在三条 arm 上就没有共同定义。"
               "`sig=12` 而不是逐位相等：浮点求和的累加顺序由引擎的并行计划决定，"
               "实测 DuckDB 与 Redshift 在第 16 位上不同，要求逐位相等等于要求两个"
               "引擎用同一个执行计划。12 位有效数字远严于任何业务口径。",
        "sql": """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN item_count = 0 THEN NULL
                            ELSE CAST(actual_amount AS DOUBLE)
                                 / CAST(item_count AS DOUBLE) END) AS safe_ratio,
                   COUNT(CASE WHEN item_count = 0 THEN 1 END) AS zero_rows
            FROM orders
        """,
        "by_arm": {
            "redshift": """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN item_count = 0 THEN NULL
                                ELSE CAST(actual_amount AS DOUBLE PRECISION)
                                     / CAST(item_count AS DOUBLE PRECISION) END) AS safe_ratio,
                       COUNT(CASE WHEN item_count = 0 THEN 1 END) AS zero_rows
                FROM orders
            """,
        },
        "must_match": True,
        "sig": 12,
        "invariant": lambda rows, snap: (
            None if rows and cell(rows[0][0]) == str(_t(snap, "orders"))
            else f"分母为 0 的行被排除后总行数仍应是 {_t(snap, 'orders')}，"
                 f"实得 {cell(rows[0][0]) if rows else '无行'}"),
    },
    {
        "key": "round-half",
        "why": "ROUND 的进位方向（四舍五入 vs 银行家舍入）和负数方向。金额报表上"
               "这一条会造成分位差异，而分位差异会被当成数据问题去查。",
        "sql": """
            SELECT ROUND(CAST(2.5 AS DECIMAL(10,1))) AS r25,
                   ROUND(CAST(3.5 AS DECIMAL(10,1))) AS r35,
                   ROUND(CAST(-2.5 AS DECIMAL(10,1))) AS rneg,
                   ROUND(CAST(1.005 AS DECIMAL(10,3)), 2) AS r1005
        """,
        "must_match": False,
    },
    {
        "key": "cjk-order",
        "why": "中文取值的 MIN/MAX：按 UTF-8 字节序还是按某种 collation。"
               "`plan_name` 是「月度会员 / 季度会员 / 年度会员」，"
               "三种排序规则会给出不同的 MIN。",
        "sql": """
            SELECT MIN(plan_name) AS mn, MAX(plan_name) AS mx,
                   count(DISTINCT plan_name) AS d
            FROM subscriptions
        """,
        "must_match": False,
    },
    {
        "key": "having-subquery",
        "why": "HAVING + 相关子查询 + CTE 三样一起用。这是 L7 里深度题的常见形状，"
               "而它在三条 arm 上都该是标准 SQL。",
        "sql": """
            WITH per_user AS (
                SELECT user_id, count(*) AS orders_n, SUM(actual_amount) AS amt
                FROM orders GROUP BY user_id
            )
            SELECT count(*) AS heavy_users,
                   SUM(orders_n) AS their_orders,
                   MIN(orders_n) AS min_n
            FROM per_user
            WHERE orders_n >= 5
        """,
        "must_match": True,
        "invariant": lambda rows, snap: (
            None if rows and (num(rows[0][2]) or 0) >= 5
            else f"WHERE orders_n>=5 之后 MIN 不该小于 5，"
                 f"实得 {cell(rows[0][2]) if rows else '无行'}"),
    },
]


# ---------------------------------------------------------------- 取值扫描

#: 算「文本列」的 DDL 类型关键字。数组列排除掉：三条 arm 的数组渲染写法不同
#: （Trino `[a, b]`、DuckDB `[a, b]`、Redshift 走 SUPER），比的是渲染不是数据。
TEXT_TYPES = ("text", "varchar", "char", "jsonb", "json")


def unquote_json(s: str) -> str:
    """Redshift 的 Data API 把 SUPER/JSON 列回成**加引号且转义过的 JSON 字符串**，
    另两条 arm 回的是原文。实测：`events.properties` 在 Athena/DuckDB 上是
    `{"amount": 10.0}`，在 Redshift 上是 `"{\\"amount\\": 10.0}"`。

    这是**渲染差异，不是数据差异**——同一批行的 `count(DISTINCT)` 三边都是 664048。
    不剥这一层的话，每一个 JSON 列都会报成不一致，把真差异淹在噪音里。

    只剥「整体是一个 JSON 字符串字面量、且里面装的是对象或数组」这一种。判据不用
    「含反斜杠」——第一版这么写，结果 `"{}"` 里没有转义，一路漏过去报成了不一致。
    也不放宽到任意 JSON 标量：那样 `"123"` 这种普通文本会被剥成 `123`，凭空造出一个
    数值差异。限定成 `{`/`[` 开头就把两头都挡住了。
    """
    if len(s) < 2 or not (s[0] == '"' and s[-1] == '"'):
        return s
    try:
        inner = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s
    if not isinstance(inner, str) or not inner.startswith(("{", "[")):
        return s
    try:
        json.loads(inner)          # 剥出来的确实是 JSON 对象/数组才算
    except (json.JSONDecodeError, ValueError):
        return s
    return inner


def sweep_gov(table: str, col: str, per_arm: dict[str, list[str]],
              distinct: bool) -> tuple[str | None, str | None]:
    """受治理列的判据。`per_arm` 每项是 `[MIN, MAX]` 或 `[MIN, MAX, COUNT(DISTINCT)]`。

    → (说明, 差异)；两者最多一个非 None，都 None 表示这一列不在治理面里。登记表和逐值
    判据都取自 `correctness.MASKED` —— 两个闸门共用一份定义，不然「哪些列受治理」会有
    两个答案，而它们分叉的那天没人会发现。

    `COUNT(DISTINCT)` 只能弱判：掩码把不同的值并到一起，**不可能拆开**，所以脱敏侧的
    基数必须不大于明文侧。这条对三条策略都成立，比逐值核弱，但比不查强。
    """
    ent = C.MASKED.get((table, col))
    if not ent or not any(a in ent["arms"] for a in per_arm):
        return None, None
    if any(v and v[0].startswith(ERR) for v in per_arm.values()):
        # 有 arm 报错。错误本身是要报出来的东西，交回普通比较那条路，别拿治理豁免盖掉。
        return None, None
    note = None
    for slot, name in ((0, "MIN"), (1, "MAX")):
        n, err = C.mask_verdict(table, col, {a: v[slot] for a, v in per_arm.items()})
        if err:
            return None, f"{name}：{err}"
        note = note or n
    if distinct:
        clear = [int(v[2]) for a, v in per_arm.items()
                 if a not in ent["arms"] and v[2].isdigit()]
        for a in (x for x in per_arm if x in ent["arms"]):
            if clear and per_arm[a][2].isdigit() and int(per_arm[a][2]) > max(clear):
                return None, (f"COUNT(DISTINCT)：脱敏侧 {a}={per_arm[a][2]} 比明文侧 "
                              f"{max(clear)} 还多 —— 掩码只能合并取值，不能新增")
    return note, None


def value_sweep(armlist: list, distinct: bool,
                only: list[str] | None) -> tuple[list[str], list[str]]:
    """扫每张表每个文本列的 MIN/MAX（可选 COUNT(DISTINCT)），三条 arm 互比。

    → (差异, 治理说明)。受治理的三列（`users.email` / `users.phone` /
    `user_profiles.birth_date`）在 Redshift 上被动态脱敏，**聚合之前**就生效，所以它们
    在那条 arm 上本该不同；这几列判的是「对得上脱敏策略」而不是「跟另两条相等」，通过
    时进说明不进差异。这个豁免以前不在代码里，于是这个模式在没有缺陷的时候是红的。

    存在的理由是 `correctness.py` 那一层**结构上**看不见这类差异：它比行数、数值求和、
    时间边界、布尔计数——文本列一项都不占。于是「三条 arm 装的行数一样、金额一样，但
    某个文本列在其中两条上是同一个假常量」这种情况在那一层永远是绿的。这一轮实测就是
    这么发现的：`orders.cancel_reason` 在 Athena/DuckDB 上只有 1 个取值，在 Redshift
    上有 8 个。

    MIN/MAX 而不是默认 COUNT(DISTINCT)：MIN/MAX 是一遍扫描的两个聚合，假常量列上
    MIN=MAX，足以把这类差异照出来；COUNT(DISTINCT) 在 8000 万行的表上贵得多，
    所以放在 `--values-distinct` 后面。
    """
    sys.path.insert(0, str(ROOT / "scripts" / "lakehouse"))
    import gen_ddl
    decl = {t: cols for _s, t, cols in gen_ddl.parse_source()}
    tables = sorted(t for t in decl if not only or t in only)
    diffs: list[str] = []
    gov_notes: list[str] = []

    for t in tables:
        cols = [c for c, pg, _nn, _n in decl[t]
                if any(k in pg.lower() for k in TEXT_TYPES) and "[]" not in pg]
        if not cols:
            continue
        parts = []
        for c in cols:
            parts += [f'MIN("{c}")', f'MAX("{c}")']
            if distinct:
                parts.append(f'COUNT(DISTINCT "{c}")')
        stride = 3 if distinct else 2
        sql = f'SELECT {", ".join(parts)} FROM "{t}"'

        got: dict[str, list[str]] = {}
        for x in armlist:
            rows, err, _ = run_one(x, sql)
            got[x.name] = ([f"{ERR} {err}"] * len(parts) if err
                           else [unquote_json(cell(v)) for v in rows[0]])

        off, gov = [], []
        for i, c in enumerate(cols):
            sl = slice(i * stride, (i + 1) * stride)
            per = {a: list(v[sl]) for a, v in got.items()}
            note, err = sweep_gov(t, c, per, distinct)
            if err:
                off.append(c)
                diffs.append(f"{t}.{c} 对不上脱敏策略 —— {err}")
                continue
            if note:
                gov.append(c)
                gov_notes.append(f"{t}.{c}：{note}")
                # 脱敏那条 arm 退出这一列的互比（已按策略核过），**明文 arm 之间照旧比**
                # —— 整列跳过等于在全库最敏感的三列上开盲区。
                per = {a: v for a, v in per.items()
                       if a not in C.MASKED[(t, c)]["arms"]}
                if len(per) < 2:
                    continue
            vals = {a: tuple(v) for a, v in per.items()}
            if len(set(vals.values())) > 1:
                off.append(c)
                diffs.append(f"{t}.{c}：" + "  ".join(
                    f"{a}={'/'.join(x)[:70]}" for a, x in sorted(vals.items())))
        # 受治理列从「三条一致」这句话的计数里拿出来：它们本来就不该三条一致，
        # 算进去会让这句话不成立。
        n_plain = len(cols) - len(gov)
        tail = f"；另 {len(gov)} 个受治理列按脱敏策略核过 {gov}" if gov else ""
        if off:
            print(f"  ❌ {t:<26} {n_plain} 个文本列，{len(off)} 个不一致：{off}{tail}")
        else:
            print(f"  ✅ {t:<26} {n_plain} 个文本列三条一致{tail}")
    return diffs, gov_notes


# ---------------------------------------------------------------- 比较

def compare_case(case: dict, per_arm: dict[str, tuple[list, str]],
                 snap: dict[str, int]) -> tuple[list[str], list[str], bool]:
    """→ (闸门失败, 记录下来的分叉, 是否一致)。

    分两条判据，顺序有讲究：**先看不变量，再看投票**。三条 arm 一致地错的时候投票
    是绿的，只有不变量能抓到；反过来，投票不同的时候不变量往往只在错的那条上红，
    两条都报出来比只报一条清楚。
    """
    ordered = bool(case.get("ordered"))
    sig = int(case.get("sig", 0))
    shaped = {a: (ERR + " " + err if err else rows_key(rows, ordered, sig))
              for a, (rows, err) in per_arm.items()}
    fails: list[str] = []
    notes: list[str] = []

    # 不变量：逐条 arm 独立算期望。报错的 arm 跳过——它没有结果可以验。
    inv = case.get("invariant")
    if inv:
        for a, (rows, err) in per_arm.items():
            if err:
                continue
            try:
                bad = inv(rows, snap)
            except Exception as e:                            # noqa: BLE001
                bad = f"不变量本身算不出来：{type(e).__name__}: {e}"
            if bad:
                fails.append(f"[{case['key']}] {a} 违反不变量：{bad}")

    # 投票。分组键用 JSON 串（结果集是嵌套 list，不可哈希），但**值留原对象**，
    # 报告时不用再反序列化一次。
    groups: dict[str, tuple[object, list[str]]] = {}
    for a, s in shaped.items():
        k = json.dumps(s, ensure_ascii=False)
        groups.setdefault(k, (s, []))[1].append(a)
    same = len(groups) == 1

    if not same:
        parts = []
        for shape, who in groups.values():
            parts.append("/".join(who) + "→" + _brief(shape))
        line = f"[{case['key']}] 三条 arm 不一致：" + "；".join(parts)
        (fails if case.get("must_match", True) else notes).append(line)

    return fails, notes, same


def _brief(s) -> str:
    """结果集的短表示。整段贴进报告会淹掉结论，所以只留前两行。"""
    if isinstance(s, str):
        return s[:120]
    if not s:
        return "<空结果集>"
    head = " | ".join(",".join(r) for r in s[:2])
    return head[:160] + (f" …共 {len(s)} 行" if len(s) > 2 else "")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(
        description="查询级正确性：JOIN / 窗口 / NULL / 时间 / 除零，三条 arm 逐位比")
    ap.add_argument("--arm", action="append", choices=list(A.ARMS))
    ap.add_argument("-k", "--key", action="append", help="按 key 子串筛用例")
    ap.add_argument("--gate-only", action="store_true", help="只跑 must_match 的用例")
    ap.add_argument("--values", action="store_true",
                    help="扫全部文本列的 MIN/MAX 做 arm 互比（correctness.py 那层看不见"
                         "文本列，装载不同步只能在这里露出来）")
    ap.add_argument("--values-distinct", action="store_true",
                    help="--values 时连 COUNT(DISTINCT) 一起比，更灵敏但在大表上贵")
    ap.add_argument("-t", "--table", action="append", help="--values 时只扫这些表")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.values or a.values_distinct:
        armlist = A.all_arms(a.arm or None)
        mode = "MIN/MAX + COUNT(DISTINCT)" if a.values_distinct else "MIN/MAX"
        print(f"arm：{'、'.join(x.name for x in armlist)}    取值扫描（{mode}）\n")
        t0 = time.time()
        diffs, gov = value_sweep(armlist, a.values_distinct, a.table)
        wall = time.time() - t0
        print()
        if gov:
            print(f"治理豁免 {len(gov)} 项 —— 这几列在 Redshift 上被动态脱敏（聚合前生效），"
                  f"比的是「对得上策略」不是「跟另两条相等」：")
            for g in gov:
                print(f"  ·  {g}")
            print()
        # 这一档以前不写 trace：函数在写 trace 那段之前就 return 了，于是「没跑过」和
        # 「跑过而且干净」在 data/bench/ 里长得一模一样。这一档要跑十几分钟，重跑一次
        # 只为确认它跑过，代价不小。
        if not a.no_trace:
            C.TRACE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            vp = C.TRACE_DIR / f"query-values-{stamp}.jsonl"
            with vp.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "run", "layer": "values", "mode": mode,
                    "started_at": stamp, "arms": [x.name for x in armlist],
                    "tables": a.table or "all", "timing_valid_for_perf": False,
                }, ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "kind": "summary", "diffs": len(diffs), "wall_s": round(wall, 1),
                    "differing": diffs, "governance_notes": gov,
                }, ensure_ascii=False) + "\n")
            up = C.upload_trace(vp)
            print(f"trace：{vp.relative_to(ROOT)}" + (f" → {up}" if up else "") + "\n")
        if diffs:
            # 原文写的是「三条 arm 装的不是同一份数据」。受治理列进了上面的说明通路之后
            # 这句话才站得住：剩下的差异确实只能是装载不同步或生成侧的问题。
            print(f"文本列取值在 arm 之间不一致 ❌  {len(diffs)} 处 —— 已排除受治理列，"
                  f"剩下的差异指向装载不同步（2026-09-02 出过一次，形态一样）\n")
            for d in diffs:
                print(f"  - {d}")
            return 1
        print("文本列取值三条 arm 一致 ✅"
              + (f"（{len(gov)} 个受治理列除外，已按策略核过）" if gov else ""))
        return 0

    cases = CASES
    if a.gate_only:
        cases = [c for c in cases if c.get("must_match", True)]
    if a.key:
        cases = [c for c in cases if any(k in c["key"] for k in a.key)]
    if not cases:
        print("按这些条件一个用例都没选中")
        return 2

    snap = snapshot()
    armlist = A.all_arms(a.arm or None)
    gate_n = sum(1 for c in cases if c.get("must_match", True))
    print(f"arm：{'、'.join(x.name for x in armlist)}    "
          f"用例：{len(cases)} 个（闸门 {gate_n} / 探针 {len(cases) - gate_n}）")
    print(f"行数真源：data/loaded_row_counts.json（orders={snap.get('orders'):,}）\n")

    trace = tp = None
    if not a.no_trace:
        C.TRACE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tp = C.TRACE_DIR / f"query-correctness-{stamp}.jsonl"
        trace = tp.open("w", encoding="utf-8")
        trace.write(json.dumps({
            "kind": "run", "layer": "query", "started_at": stamp,
            "arms": [x.name for x in armlist], "cases": len(cases),
            "timing_valid_for_perf": False,
        }, ensure_ascii=False) + "\n")

    fails: list[str] = []
    notes: list[str] = []
    t0 = time.time()

    for case in cases:
        per_arm: dict[str, tuple[list, str]] = {}
        for x in armlist:
            sql = case.get("by_arm", {}).get(x.name) or case["sql"]
            sql = " ".join(sql.split())
            rows, err, ms = run_one(x, sql)
            per_arm[x.name] = (rows, err)
            if trace:
                trace.write(json.dumps({
                    "kind": "span", "case": case["key"], "arm": x.name,
                    "sql": sql, "error": err, "elapsed_ms": round(ms, 1),
                    "rows": [[cell(c) for c in r] for r in rows[:50]],
                    "row_count": len(rows),
                }, ensure_ascii=False) + "\n")

        f, n, same = compare_case(case, per_arm, snap)
        fails += f
        notes += n
        gate = case.get("must_match", True)
        if f:
            mark = "❌"
        elif same:
            mark = "✅"
        else:
            mark = "🔶"                 # 探针分叉：是发现，不是失败
        kind = "闸门" if gate else "探针"
        print(f"  {mark} {case['key']:<20} {kind}  " +
              ("三条一致" if same else "有分叉") +
              (f"  ⚠️ {len(f)} 处不合" if f else ""))
        for a2, (_, err) in per_arm.items():
            if err:
                print(f"       {a2} 报错：{err[:110]}")

    if trace:
        trace.write(json.dumps({
            "kind": "summary", "cases": len(cases), "gate_failures": len(fails),
            "probe_divergences": len(notes), "wall_s": round(time.time() - t0, 1),
        }, ensure_ascii=False) + "\n")
        trace.close()
        up = C.upload_trace(tp)
        print(f"\ntrace：{tp.relative_to(ROOT)}" + (f" → {up}" if up else ""))

    if notes:
        print(f"\n方言分叉 {len(notes)} 处——**这些是对比结论，不是缺陷**：")
        for n in notes:
            print(f"  · {n}")

    print()
    if fails:
        print(f"查询级闸门未通过 ❌  {len(fails)} 处\n")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"查询级闸门通过 ✅  {gate_n} 个闸门用例在 "
          f"{'、'.join(x.name for x in armlist)} 上一致且满足不变量"
          f"，另有 {len(notes)} 处已记录的方言分叉")
    print(f"  墙钟 {time.time() - t0:.1f}s")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判定逻辑自测：不连引擎，只喂构造的结果集。"""
    bad = 0

    def check(cond: bool, msg: str):
        nonlocal bad
        if not cond:
            bad += 1
            print(f"  FAIL {msg}")

    snap = {"orders": 100, "order_items": 200, "channel_daily_costs": 7, "users": 50}

    # 1. 数值归一：3.00 == 3，但 3 != 3.5（整数相除那条用例全靠这一点）
    check(cell(decimal.Decimal("3.00")) == cell(3), "3.00 与 3 应当比成相等")
    check(cell(decimal.Decimal("3")) != cell(decimal.Decimal("3.5")),
          "3 与 3.5 必须比成不等，否则整数相除的差异看不见")
    check(cell(decimal.Decimal("1E+3")) == "1000", "指数形式应展开")
    check(cell(None) == "<NULL>" and cell(0) == "0",
          "NULL 与 0 必须区分——空集聚合那条用例判的就是这个")

    # 1b. sig 只磨末几位，不磨真差异。左边这两个数是 DuckDB 和 Redshift 实测回的值。
    d1, d2 = "140681100.35400835", "140681100.3540105"
    check(cell(d1, 12) == cell(d2, 12),
          f"sig=12 应当把 {d1} 与 {d2} 视为同一个数（浮点累加顺序差异）")
    check(cell(d1) != cell(d2), "不开 sig 时这两个值必须仍然比成不等")
    check(cell("140681679", 12) != cell(d1, 12),
          "sig=12 不能把 Athena 的 140681679 与 140681100.354 磨成相等——"
          "那是真差异，磨掉了这个用例就白设了")
    check(cell(1, 12) == "1" and cell(None, 12) == "<NULL>",
          "sig 不该影响整数与 NULL 的表示")

    # 2. 无序比较忽略行序，有序比较不忽略
    r1, r2 = [(1, "a"), (2, "b")], [(2, "b"), (1, "a")]
    check(rows_key(r1, False) == rows_key(r2, False), "无序比较应当忽略行序")
    check(rows_key(r1, True) != rows_key(r2, True), "有序比较不能忽略行序")

    # 3. 三条一致 → 通过
    case = {"key": "t", "must_match": True}
    f, n, same = compare_case(case, {a: ([(1,)], "") for a in A.ARMS}, snap)
    check(same and not f and not n, "三条一致时不该有任何输出")

    # 4. 一条 arm 不同 → 闸门用例算失败，探针用例只记录
    off = {"athena": ([(1,)], ""), "duckdb": ([(2,)], ""), "redshift": ([(1,)], "")}
    f, n, same = compare_case({"key": "t", "must_match": True}, off, snap)
    check(bool(f) and not same, "闸门用例上的分叉必须算失败")
    f, n, same = compare_case({"key": "t", "must_match": False}, off, snap)
    check(not f and bool(n), "探针用例上的分叉只能记录，不能算失败")

    # 5. 三条一致地错 → 投票是绿的，只有不变量抓得到。这一条是这个文件存在的理由。
    inv = lambda rows, s: None if cell(rows[0][0]) == "0" else "应当为 0"  # noqa: E731
    allwrong = {a: ([(9,)], "") for a in A.ARMS}
    f, n, same = compare_case({"key": "t", "must_match": True, "invariant": inv},
                              allwrong, snap)
    check(same, "三条一致地错时，投票本身确实是一致的")
    check(len(f) == len(A.ARMS),
          "三条一致地错必须被不变量抓到（每条 arm 各报一次），"
          f"实得 {len(f)} 条")

    # 6. 报错是一种结果，不是崩溃：一条抛错、两条回值 → 记成分叉
    mixed = {"athena": ([], "DivByZero: x"), "duckdb": ([(None,)], ""),
             "redshift": ([], "DivByZero: x")}
    f, n, same = compare_case({"key": "t", "must_match": False}, mixed, snap)
    check(not same and bool(n), "抛错与回 NULL 必须比成不同并记录下来")

    # 7. 不变量自己抛异常时要报出来，不能吞掉
    boom = lambda rows, s: 1 / 0                              # noqa: E731
    f, _, _ = compare_case({"key": "t", "must_match": True, "invariant": boom},
                           {"athena": ([(1,)], "")}, snap)
    check(bool(f) and "不变量本身算不出来" in f[0],
          "不变量抛异常必须报出来，否则等于这条检查静默消失了")

    # 8. 用例表本身：key 唯一、每条都有 why、by_arm 的键必须是真 arm 名、
    #    共用 SQL 与 by_arm 至少有一个
    keys = [c["key"] for c in CASES]
    check(len(keys) == len(set(keys)), f"用例 key 有重复：{keys}")
    for c in CASES:
        check(bool(c.get("why", "").strip()), f"{c['key']} 没写 why")
        check("sql" in c or "by_arm" in c, f"{c['key']} 既没有 sql 也没有 by_arm")
        for k in c.get("by_arm", {}):
            check(k in A.ARMS, f"{c['key']} 的 by_arm 里有不认识的 arm {k!r}")
        if "by_arm" in c and "sql" not in c:
            check(set(c["by_arm"]) == set(A.ARMS),
                  f"{c['key']} 没有共用 SQL，by_arm 就必须覆盖全部 arm，"
                  f"现在只有 {sorted(c['by_arm'])}")

    # 9. 方言不能串味：共用 SQL 里不许出现任何一家的专有写法
    aliens = ("format_datetime", "strftime", "TO_CHAR", "approx_distinct",
              "APPROXIMATE COUNT", "::")
    for c in CASES:
        s = c.get("sql", "")
        for al in aliens:
            check(al not in s,
                  f"{c['key']} 的**共用** SQL 里有方言专有写法 {al!r}，"
                  f"这样它在另两条 arm 上必然失败")

    # 10. Redshift 的 SUPER 剥壳：只剥「整体是 JSON 字符串字面量」这一种，别的原样。
    check(unquote_json(r'"{\"a\": 1}"') == '{"a": 1}',
          "Redshift 回的转义 JSON 应当被剥成原文，否则每个 JSON 列都会假报不一致")
    check(unquote_json('{"a": 1}') == '{"a": 1}', "已经是原文的不该再动")
    check(unquote_json('"{}"') == "{}",
          '`"{}"` 里没有反斜杠也要剥——第一版按「含反斜杠」判，这个值漏过去了')
    check(unquote_json('"就是一句带引号的中文"') == '"就是一句带引号的中文"',
          "剥出来不是 JSON 的普通文本必须原样退回")
    check(unquote_json('"123"') == '"123"',
          '`"123"` 不能剥成 123——那会凭空造出一个数值差异')
    check(unquote_json("<NULL>") == "<NULL>" and unquote_json('"') == '"',
          "NULL 标记和单个引号不该被剥坏")

    # 11. sig 只准出现在浮点用例上。整数/DECIMAL 用例开了它就是在掩盖真差异。
    for c in CASES:
        if c.get("sig"):
            check("DOUBLE" in c.get("sql", "") or
                  any("DOUBLE" in s for s in c.get("by_arm", {}).values()),
                  f"{c['key']} 开了 sig 但 SQL 里没有 DOUBLE——"
                  f"sig 只给浮点结果用，整数和 DECIMAL 本该逐位相等")

    # 12. 治理豁免（`--values` 那一档）。登记表在 correctness.py，这里验的是本文件
    #     把它接对了：MIN 和 MAX 都要过、任一格对不上就红、明文侧分歧不被盖掉。
    EMAIL = "***@masked.invalid"
    n, e = sweep_gov("users", "email",
                     {"athena": ["a@b.com", "z@b.com"], "duckdb": ["a@b.com", "z@b.com"],
                      "redshift": [EMAIL, EMAIL]}, False)
    check(bool(n) and not e, f"治理列本该只出说明：说明={n!r} 差异={e!r}")
    n, e = sweep_gov("users", "email",
                     {"athena": ["a@b.com", "z@b.com"], "redshift": [EMAIL, "z@b.com"]},
                     False)
    check(bool(e) and "MAX" in (e or ""),
          f"只有 MIN 被脱敏时必须报差异并指出是 MAX 那一格：{e!r}")
    n, e = sweep_gov("users", "email",
                     {"athena": ["a@b.com", "z@b.com"], "duckdb": ["a@b.com", "q@b.com"],
                      "redshift": [EMAIL, EMAIL]}, False)
    check(bool(e) and "治理面" in (e or ""),
          f"明文 arm 之间的分歧不能被治理豁免盖掉：{e!r}")
    check(sweep_gov("orders", "status", {"athena": ["a", "b"]}, False) == (None, None),
          "不在治理面上的列不该被这条通路碰")
    check(sweep_gov("users", "email",
                    {"athena": [f"{ERR} boom", f"{ERR} boom"],
                     "redshift": [EMAIL, EMAIL]}, False) == (None, None),
          "有 arm 报错时应当交回普通比较，报错本身要看得见")
    # COUNT(DISTINCT)：掩码只能合并取值。脱敏侧基数反而更大 → 一定有问题。
    n, e = sweep_gov("users", "email",
                     {"athena": ["a@b.com", "z@b.com", "500"],
                      "redshift": [EMAIL, EMAIL, "1"]}, True)
    check(bool(n) and not e, f"脱敏侧基数更小是正常的：说明={n!r} 差异={e!r}")
    n, e = sweep_gov("users", "email",
                     {"athena": ["a@b.com", "z@b.com", "500"],
                      "redshift": [EMAIL, EMAIL, "900"]}, True)
    check(bool(e) and "COUNT(DISTINCT)" in (e or ""),
          f"脱敏侧基数更大必须报差异：{e!r}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  数值归一 / sig 容差 / SUPER 剥壳 / 有序与无序比较 / 投票 / 「三条一致地错」由不变量兜住 / "
          f"报错当结果 / 不变量异常不吞 / 用例表 {len(CASES)} 条结构与方言检查")
    print("  治理豁免：MIN/MAX 双格核 / 单格失效必红 / 明文侧分歧必红 / 非治理列不碰 / "
          "报错不被豁免盖掉 / COUNT(DISTINCT) 基数只减不增")
    print("判定逻辑自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
