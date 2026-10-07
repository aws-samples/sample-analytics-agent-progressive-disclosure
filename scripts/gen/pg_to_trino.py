#!/usr/bin/env python3
"""Postgres SQL → Trino（Athena）方言的定向改写（只做已知必需的几处）。

## 定位

跟 `pg_to_redshift.py` 一样，**不是通用转译器**：只处理本项目 SQL 里实际出现、
且 Trino 不接受的构造。每加一条规则都要在 `_selftest()` 里配一个用例。
不认识的写法原样保留，让 Athena 自己报错——静默"猜着改"比报错危险得多。

Redshift 那版只需要改一处（`FILTER`），Trino 这版需要改五处，因为 Redshift 是
Postgres 的分叉、而 Trino 是另一个血统。

## 已实现的规则

| Postgres | Trino | 出现处 |
|---|---|---|
| `x::type` | `CAST(x AS type')` | knowledge + eval 约 60 处 |
| `numeric(p,s)` / 裸 `numeric` | `decimal(p,s)` / `decimal(38,6)` | 同上 |
| `interval '7 days'` | `interval '7' day` | 约 31 处 |
| `to_char(d,'YYYY-MM')` | `date_format(d,'%Y-%m')` | 4 处 |
| `j->>'k'` | `json_extract_scalar(j,'$.k')` | 6 处 |
| `<某>_date + 7` | `<某>_date + interval '7' day` | 3 处（留存口径） |
| `dt >= '2025-12-01'` | `dt >= date '2025-12-01'` | 5 处 |
| `EXTRACT(EPOCH FROM (a-b))` | `date_diff('second', b, a)` | 2 处 |
| `PERCENTILE_CONT(p) WITHIN GROUP` | `approx_percentile(x, p)`（**近似**） | 1 处 |

### 类型名的坑：裸 `numeric` 不能直接变成裸 `decimal`

Postgres 的 `numeric` 是任意精度，`sum(x)::numeric` 保留小数。
Trino 的裸 `decimal` 等于 `decimal(38,0)`——**标度是 0**，实测
`CAST(1.55 AS decimal)` 得到 `2.0`。金额直接被四舍五入成整数，
而查询照样成功、结果照样像个数。所以裸 `numeric` 一律映射成 `decimal(38,6)`。

`TEXT` 在 Trino 里不存在（`Unknown type: TEXT`），映射成 `varchar`。

## 刻意**不**实现的（数量少、结构性改写，机械改容易改错，手工处理）

- `unnest(a)` 放在 FROM 里 → `CROSS JOIN UNNEST(a) AS t(x)`：要动 FROM 子句结构，
  还要给列别名定元数。全仓 3 处，手工改。
- `DISTINCT ON (k)` → `row_number() OVER (PARTITION BY k ...) = 1`：要把一条
  SELECT 拆成子查询。全仓只在 connection.md 的方言对照表里作为**文字**出现。
- `generate_series` → `UNNEST(sequence(...))`：同上，只在对照表里。

## 已确认**不需要**改写的（Trino 原生支持，实测过）

`FILTER (WHERE ...)`（这正是 Redshift 那版唯一要改的东西，Trino 反而原生支持）、
`||` 拼接、`%` 取模、窗口函数、`GROUP BY 1`、`ORDER BY ... NULLS LAST`、
`count(DISTINCT x)`、`date_trunc('week', d)`、`NULLIF`、`COALESCE`、
`current_date`（语法没问题；但本项目是静态样本，用它当"今天"会查空——那是口径
问题不是方言问题，见 knowledge/connection.md）。

## 用法

    python3 scripts/gen/pg_to_trino.py <file.sql>            # 打到 stdout
    python3 scripts/gen/pg_to_trino.py <file.sql> --out out.sql
    python3 scripts/gen/pg_to_trino.py --text "SELECT x::int"  # 改一小段
    python3 scripts/gen/pg_to_trino.py --selftest
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Postgres 类型名 → Trino 类型名。只列本项目出现过的 + 几个高频近邻。
# 没列到的原样透传：`CAST(x AS 某未知类型)` 会被 Athena 明确拒掉，比猜一个好。
TYPE_MAP = {
    "numeric": "decimal(38,6)",     # 裸 decimal 是 (38,0)，会把金额抹成整数
    "decimal": "decimal(38,6)",
    "text": "varchar",              # Trino 没有 TEXT
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "float8": "double",
    "float": "double",
    "bool": "boolean",
    "timestamptz": "timestamp(6)",
}

# 日期加减用的列名特征。只有列名长得像日期才把 `+ 7` 当成天数——
# `amount + 7` 绝不能被改成加 7 天。宁可漏改（Athena 会报类型错）也不能错改。
_DATEISH = re.compile(r"(?i)(^|[._])(dt|date|day)$|_(date|dt|day|time|at)$")

_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$")

# `(` 前面的标识符不一定是函数名。分三类处理，因为它决定了操作数从哪开始：
#
# _CHAINED：`sum(x) FILTER (WHERE ...)::int` —— 操作数是**整个** `sum(x) FILTER (...)`，
#   所以吃掉 FILTER 之后还要继续往左找。不这么做的话会切出
#   `CAST(FILTER (WHERE ...) AS integer)`，一段语法上根本不成立的 SQL。
#   而这种坏改写在 md 文档里是**看不出来**的：文档不会被执行。
# _STOPWORDS：`WHERE (a+b)::int` —— 操作数只是 `(a+b)`，不能把 WHERE 吃进去。
_CHAINED = {"FILTER", "OVER", "WITHIN"}
_STOPWORDS = {
    "WHERE", "AND", "OR", "NOT", "IN", "ON", "THEN", "ELSE", "WHEN", "CASE",
    "SELECT", "FROM", "BY", "GROUP", "ORDER", "HAVING", "VALUES", "AS", "IS",
    "LIKE", "BETWEEN", "UNION", "ALL", "DISTINCT", "USING", "JOIN", "SET",
    "RETURNING", "LIMIT", "OFFSET", "EXISTS", "ANY", "SOME", "ELSEIF",
}


def _match_paren_back(s: str, close_idx: int) -> int:
    """给定 s[close_idx] == ')'，返回配对左括号的下标。跳过字符串字面量。"""
    depth, i = 0, close_idx
    while i >= 0:
        ch = s[i]
        if ch == "'":
            # 反向跳过字面量（'' 转义）
            i -= 1
            while i >= 0:
                if s[i] == "'":
                    if i - 1 >= 0 and s[i - 1] == "'":
                        i -= 2
                        continue
                    break
                i -= 1
        elif ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                return i
        i -= 1
    raise ValueError(f"括号不配对，右括号在 {close_idx}")


def _operand_start(s: str, end: int) -> int | None:
    """向左找出以 s[end] 结尾的那个操作数的起点下标。找不到返回 None。

    认这几种形态：`)`（函数调用或括号表达式，含前面的函数名）、`'字面量'`、
    标识符/数字（含 `a.b` 限定名和 `"带引号的名"`）。
    """
    i = end
    while i >= 0 and s[i].isspace():
        i -= 1
    if i < 0:
        return None
    if s[i] == ")":
        try:
            j = _match_paren_back(s, i)
        except ValueError:
            return None
        # 括号前面可能是函数名（可含限定前缀），一并吃进来
        k = j - 1
        while k >= 0 and s[k].isspace():
            k -= 1
        if k >= 0 and s[k] in _IDENT_CHARS:
            while k >= 0 and s[k] in _IDENT_CHARS:
                k -= 1
            word = s[k + 1:j].strip().upper()
            if word in _CHAINED:            # FILTER / OVER：继续往左收
                return _operand_start(s, k)
            if word in _STOPWORDS:          # 关键字：操作数只到括号
                return j
            return k + 1
        return j
    if s[i] == "'":
        j = i - 1
        while j >= 0:
            if s[j] == "'":
                if j - 1 >= 0 and s[j - 1] == "'":
                    j -= 2
                    continue
                return j
            j -= 1
        return None
    if s[i] == '"':
        j = s.rfind('"', 0, i)
        return j if j >= 0 else None
    if s[i] in _IDENT_CHARS:
        j = i
        while j >= 0 and s[j] in _IDENT_CHARS:
            j -= 1
        return j + 1
    return None


def map_type(t: str) -> str:
    """`numeric(14,2)` → `decimal(14,2)`；裸 `numeric` → `decimal(38,6)`。"""
    m = re.match(r"(?i)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\(([^)]*)\))?\s*$", t)
    if not m:
        return t
    base, args = m.group(1), m.group(3)
    if args is not None:                       # 带精度：只换类型名，精度照抄
        low = base.lower()
        newbase = "decimal" if low in ("numeric", "decimal") else \
            TYPE_MAP.get(low, base).split("(")[0]
        return f"{newbase}({args.replace(' ', '')})"
    return TYPE_MAP.get(base.lower(), base)


def _rewrite_postfix(sql: str, pattern: str, build) -> tuple[str, int, int]:
    """通用后缀算子改写：`<操作数><算子>` → `build(操作数, match)`。

    **从左往右**做。方向不是随便选的：`x::int::text` 从右往左会先看见 `::text`，
    而它左边的"操作数"扫出来是 `int`（扫到 `:` 停），切出 `x::CAST(int AS varchar)`。
    从左往右则先把 `x::int` 变成 `CAST(x AS integer)`，再看见 `::text` 时左边是个
    完整的 `)`，收得对。

    找不到左操作数时**跳过这一处继续往后走**，并计数返回。原来这里是 `break`，
    于是 core_metrics.md 里一句散文（`` `::numeric` ``，反引号后面没有操作数）
    让**同一个文件里另外 11 处真 SQL 全部没被改写**，而统计显示的是 `::→CAST: 0`
    —— 一个"什么都没做"和"没什么可做"长得一模一样的失败。
    """
    n = skipped = 0
    out, pos = sql, 0
    rx = re.compile(pattern)
    while True:
        m = rx.search(out, pos)
        if not m:
            break
        start = _operand_start(out, m.start() - 1)
        if start is None:
            skipped += 1
            pos = m.start() + 1            # 跳过这一处，别放弃整个文件
            continue
        out = out[:start] + build(out[start:m.start()], m) + out[m.end():]
        n += 1
        pos = start                        # 从操作数起点重扫，接得住链式转型
    return out, n, skipped


def rewrite_cast(sql: str) -> tuple[str, int, int]:
    """`x::type` → `CAST(x AS type)`。"""
    # 精度括号前的空白写在**可选组内部**：写在外面会把 `x::date AS d` 里
    # `date` 后的那个空格一起吃掉，输出 `CAST(x AS date)AS d`。
    return _rewrite_postfix(
        sql,
        r"::\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*(\([0-9,\s]*\)))?",
        lambda operand, m: f"CAST({operand} AS {map_type(m.group(1) + (m.group(2) or ''))})",
    )


# Trino 的区间字面量**只认这几个单位**：year / month / day / hour / minute / second。
# **没有 week，也没有 quarter**——写了会报 `TYPE_NOT_FOUND: Unknown resolvedType:
# interval`，跟 Postgres 那种 `interval '7 day'` 的报错**一字不差**，所以光看报错
# 会以为是引号位置写错了，怎么调引号都不好。只能换算成合法单位。
_UNIT_FACTOR = {
    "year": ("year", 1), "month": ("month", 1), "day": ("day", 1),
    "hour": ("hour", 1), "minute": ("minute", 1), "second": ("second", 1),
    "week": ("day", 7),          # Trino 没有 week
    "quarter": ("month", 3),     # Trino 没有 quarter
}


def rewrite_interval(sql: str) -> tuple[str, int]:
    """`interval '7 days'` → `interval '7' day`（数字进引号，单位出引号且单数）。

    顺带把 Trino 不认的单位换算掉：`interval '12 weeks'` → `interval '84' day`。
    """
    def sub(m: re.Match) -> str:
        num, unit = int(m.group(1)), m.group(2).lower().rstrip("s")
        base, factor = _UNIT_FACTOR[unit]
        return f"interval '{num * factor}' {base}"
    out, n = re.subn(
        r"(?i)\binterval\s*'\s*(\d+)\s*(years?|months?|weeks?|quarters?|"
        r"days?|hours?|minutes?|seconds?)\s*'",
        sub, sql)
    return out, n


def rewrite_bad_interval_unit(sql: str) -> tuple[str, int]:
    """把**已经是 Trino 语法**但单位非法的区间修掉：`interval '2' week` → `interval '14' day`。

    需要单独一条规则，因为上面那条只匹配 Postgres 的 `'2 weeks'` 形态。手工改过一轮
    的文件、或者别人照着"数字进引号"的口诀自己改出来的，都会落在这个形态上。
    """
    def sub(m: re.Match) -> str:
        num, unit = int(m.group(1)), m.group(2).lower().rstrip("s")
        base, factor = _UNIT_FACTOR[unit]
        return f"interval '{num * factor}' {base}"
    out, n = re.subn(
        r"(?i)\binterval\s*'\s*(\d+)\s*'\s*(weeks?|quarters?)\b", sub, sql)
    return out, n


# to_char 的格式串 → date_format 的格式串。只认这几个 token，遇到不认识的就不改。
_FMT_TOKENS = [("YYYY", "%Y"), ("MM", "%m"), ("DD", "%d"),
               ("HH24", "%H"), ("MI", "%i"), ("SS", "%s")]


def _conv_fmt(fmt: str) -> str | None:
    out, i = [], 0
    while i < len(fmt):
        for pg, tr in _FMT_TOKENS:
            if fmt.startswith(pg, i):
                out.append(tr)
                i += len(pg)
                break
        else:
            if fmt[i] in "-/:. ":
                out.append(fmt[i])
                i += 1
            else:
                return None            # 不认识的 token：整段不改
    return "".join(out)


def rewrite_to_char(sql: str) -> tuple[str, int]:
    """`to_char(d,'YYYY-MM')` → `date_format(d,'%Y-%m')`。"""
    n = 0
    out = sql
    for m in reversed(list(re.finditer(r"(?i)\bto_char\s*\(", out))):
        close = None
        try:
            open_idx = out.index("(", m.start())
            depth = 0
            i = open_idx
            while i < len(out):
                if out[i] == "'":
                    i += 1
                    while i < len(out) and out[i] != "'":
                        i += 1
                elif out[i] == "(":
                    depth += 1
                elif out[i] == ")":
                    depth -= 1
                    if depth == 0:
                        close = i
                        break
                i += 1
        except ValueError:
            continue
        if close is None:
            continue
        inner = out[open_idx + 1:close]
        fm = re.match(r"(?s)^(.*),\s*'([^']*)'\s*$", inner)
        if not fm:
            continue
        new_fmt = _conv_fmt(fm.group(2))
        if new_fmt is None:
            continue
        out = (out[:m.start()]
               + f"date_format({fm.group(1).strip()}, '{new_fmt}')"
               + out[close + 1:])
        n += 1
    return out, n


def rewrite_json_arrow(sql: str) -> tuple[str, int, int]:
    """`j->>'k'` → `json_extract_scalar(j, '$.k')`。"""
    return _rewrite_postfix(
        sql,
        r"->>\s*'([^']*)'",
        lambda operand, m: f"json_extract_scalar({operand}, '$.{m.group(1)}')",
    )


def rewrite_extract_epoch(sql: str) -> tuple[str, int]:
    """`EXTRACT(EPOCH FROM (a - b))` → `date_diff('second', b, a)`。

    Trino 的 `EXTRACT` **没有 EPOCH 字段**（报 `Invalid EXTRACT field: EPOCH`），
    而且两个 timestamp 相减在 Trino 里得到的是 interval，不能直接当秒数用。
    注意参数顺序反过来了：`date_diff` 是 (单位, 早, 晚)。
    """
    def sub(m: re.Match) -> str:
        return f"date_diff('second', {m.group(2).strip()}, {m.group(1).strip()})"
    out, n = re.subn(
        r"(?i)\bEXTRACT\s*\(\s*EPOCH\s+FROM\s*\(\s*([^()]+?)\s*-\s*([^()]+?)\s*\)\s*\)",
        sub, sql)
    return out, n


def rewrite_percentile(sql: str) -> tuple[str, int]:
    """`PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)` → `approx_percentile(x, p)`。

    Trino 没有 Postgres 那种有序集聚合。**语义不完全等价**：`approx_percentile`
    是近似分位数，不是精确插值。对"中位数大概多少"这类问题够用，写进对外口径前
    要知道这一点——所以这条规则改完，改写统计里会记一笔。
    """
    def sub(m: re.Match) -> str:
        return f"approx_percentile({m.group(2).strip()}, {m.group(1).strip()})"
    out, n = re.subn(
        r"(?i)\bPERCENTILE_(?:CONT|DISC)\s*\(\s*([^()]+?)\s*\)\s*"
        r"WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+(.+?)\s*\)(?=\s*(?:AS\b|,|$))",
        sub, sql, flags=re.S)
    return out, n


def rewrite_date_arith(sql: str) -> tuple[str, int]:
    """`cohort_date + 7` → `cohort_date + interval '7' day`。

    只在左侧列名**长得像日期**时才动手（见 `_DATEISH`）。`amount + 7` 不能碰：
    改错比漏改危险得多——漏改 Athena 会报 `date - integer` 类型错，改错则是
    一个安静的错数。
    """
    def sub(m: re.Match) -> str:
        col, op, num = m.group(1), m.group(2), m.group(3)
        if not _DATEISH.search(col.split(".")[-1]):
            return m.group(0)
        return f"{col} {op} interval '{num}' day"
    out, _ = re.subn(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
                     r"\s*([-+])\s*(\d+)\b(?!\s*\.)", sub, sql)
    # subn 的计数含未改的，重新数一遍真正改掉的
    n = out.count("interval '") - sql.count("interval '")
    return out, n


# 形如 2025-12-01 / 2025-12-01 10:30:00 的字符串字面量
_DATE_LIT = r"\d{4}-\d{2}-\d{2}"
_TS_LIT = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"


def _lit_prefix(lit: str) -> str:
    return "timestamp" if re.fullmatch(_TS_LIT, lit) else "date"


def rewrite_date_literal(sql: str) -> tuple[str, int]:
    """`dt >= '2025-12-01'` → `dt >= date '2025-12-01'`。

    Trino **不把字符串隐式转成时间**：`ts_col >= '2024-01-01'` 报
    `TYPE_MISMATCH: timestamp(6) <= varchar(10)`。Postgres 会自己转，所以这种写法
    在原库里一直好着，迁过来才炸。

    **只在左边的列名长得像日期时才动手**（复用 `_DATEISH`）。不加这个限制的话，
    `order_no = '2025-12-01'` 这种正常的字符串比较会被改成 date，反而从能跑变成报错。
    加了这个限制，误改需要一个叫 `*_date` / `*_at` / `dt` 却存着字符串的列——
    真有那种列，报错也是它该修。

    `BETWEEN a AND b` 两个边界一起加前缀；已经写了 `date '...'` 的不重复加。
    """
    n = 0
    # BETWEEN 先做：否则下面的比较符规则会把 BETWEEN 的第一个边界单独处理掉
    def sub_between(m: re.Match) -> str:
        nonlocal n
        if not _DATEISH.search(m.group(1).split(".")[-1]):
            return m.group(0)
        n += 1
        return (f"{m.group(1)} BETWEEN {_lit_prefix(m.group(2))} '{m.group(2)}' "
                f"AND {_lit_prefix(m.group(3))} '{m.group(3)}'")

    out = re.sub(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s+BETWEEN\s+"
        rf"'({_TS_LIT}|{_DATE_LIT})'\s+AND\s+'({_TS_LIT}|{_DATE_LIT})'",
        sub_between, sql)

    def sub_cmp(m: re.Match) -> str:
        nonlocal n
        if not _DATEISH.search(m.group(1).split(".")[-1]):
            return m.group(0)
        n += 1
        return f"{m.group(1)} {m.group(2)} {_lit_prefix(m.group(3))} '{m.group(3)}'"

    out = re.sub(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*"
        r"(>=|<=|<>|!=|=|<|>)\s*"
        rf"'({_TS_LIT}|{_DATE_LIT})'",
        sub_cmp, out)
    return out, n


def convert(sql: str) -> tuple[str, dict]:
    """返回 (改写后的 SQL, 统计)。

    统计里的 `skipped:*` 项是**找到了但改不动**的地方——必须人工看一眼。
    统计只报成功数的话，"跳过了 3 处"和"本来就没有"在输出上完全一样。
    """
    stats: dict[str, int] = {}
    for label, fn in (("::→CAST", rewrite_cast),
                      ("interval", rewrite_interval),
                      ("interval 非法单位", rewrite_bad_interval_unit),
                      ("to_char→date_format", rewrite_to_char),
                      ("->>→json_extract_scalar", rewrite_json_arrow),
                      ("EXTRACT(EPOCH)→date_diff", rewrite_extract_epoch),
                      ("percentile→approx_percentile(近似!)", rewrite_percentile),
                      ("date±N→interval", rewrite_date_arith),
                      ("日期字面量加 date/timestamp 前缀", rewrite_date_literal)):
        res = fn(sql)
        sql, k = res[0], res[1]
        if k:
            stats[label] = k
        if len(res) > 2 and res[2]:
            stats[f"skipped:{label}"] = res[2]
    return sql, stats


def _selftest() -> int:
    cases = [
        # —— :: 转型 ——
        ("SELECT x::date", "SELECT CAST(x AS date)"),
        ("SELECT registered_at::date AS d", "SELECT CAST(registered_at AS date) AS d"),
        ("u.registered_at::date", "CAST(u.registered_at AS date)"),
        ("sum(actual_amount)::numeric(14,2)",
         "CAST(sum(actual_amount) AS decimal(14,2))"),
        # 裸 numeric 必须带上标度，否则金额被抹成整数
        ("sum(x)::numeric", "CAST(sum(x) AS decimal(38,6))"),
        ("v::TEXT", "CAST(v AS varchar)"),
        ("cnt::INT", "CAST(cnt AS integer)"),
        ("(a + b)::int", "CAST((a + b) AS integer)"),
        ("'2026-01-01'::date", "CAST('2026-01-01' AS date)"),
        # 一行两处 + 嵌套
        ("a::int / b::numeric",
         "CAST(a AS integer) / CAST(b AS decimal(38,6))"),
        ("round(x::numeric, 2)", "round(CAST(x AS decimal(38,6)), 2)"),
        # —— interval ——
        ("d - interval '6 days'", "d - interval '6' day"),
        ("d - interval '1 month'", "d - interval '1' month"),
        ("d + INTERVAL '30 DAYS'", "d + interval '30' day"),
        ("d - interval '7' day", "d - interval '7' day"),        # 已是 Trino，别动
        # Trino 没有 week / quarter 单位，必须换算
        ("d - interval '12 weeks'", "d - interval '84' day"),
        ("d - interval '1 week'", "d - interval '7' day"),
        ("d - interval '2 quarters'", "d - interval '6' month"),
        # 已经是 Trino 语法但单位非法的，也要修
        ("c.cohort_week + interval '1' week", "c.cohort_week + interval '7' day"),
        ("d - interval '12' week", "d - interval '84' day"),
        # —— to_char ——
        ("to_char(dt,'YYYY-MM')", "date_format(dt, '%Y-%m')"),
        ("to_char(dt, 'YYYY-MM-DD')", "date_format(dt, '%Y-%m-%d')"),
        # 不认识的格式 token：整段保留，别猜
        ("to_char(dt,'Q')", "to_char(dt,'Q')"),
        # —— ->> ——
        ("properties->>'keyword'", "json_extract_scalar(properties, '$.keyword')"),
        ("v.config_json->>'button_color'",
         "json_extract_scalar(v.config_json, '$.button_color')"),
        # 组合：先 ->> 再 ::
        ("(properties->>'result_count')::INT",
         "CAST((json_extract_scalar(properties, '$.result_count')) AS integer)"),
        # —— EXTRACT(EPOCH) ——（注意 date_diff 的参数顺序是 早, 晚）
        ("EXTRACT(EPOCH FROM (read_at - sent_at))",
         "date_diff('second', sent_at, read_at)"),
        ("EXTRACT(EPOCH FROM (a.x - a.y)) / 60",
         "date_diff('second', a.y, a.x) / 60"),
        # 合法的 EXTRACT 字段不许动
        ("EXTRACT(YEAR FROM dt)", "EXTRACT(YEAR FROM dt)"),
        # —— 有序集聚合 ——
        ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_seconds) AS median",
         "approx_percentile(duration_seconds, 0.5) AS median"),
        # —— 日期加减 ——
        ("c.cohort_date + 1", "c.cohort_date + interval '1' day"),
        ("activity_date = cohort_date + 7",
         "activity_date = cohort_date + interval '7' day"),
        # 关键负例：金额不能被当成日期
        ("amount + 7", "amount + 7"),
        ("total_gmv - 100", "total_gmv - 100"),
        ("LIMIT 30", "LIMIT 30"),
        # —— 日期字面量（Trino 不做 varchar→时间的隐式转换）——
        ("WHERE start_date >= '2024-01-01'", "WHERE start_date >= date '2024-01-01'"),
        ("WHERE c.end_date < '2024-12-31'", "WHERE c.end_date < date '2024-12-31'"),
        ("dt BETWEEN '2025-12-01' AND '2025-12-31'",
         "dt BETWEEN date '2025-12-01' AND date '2025-12-31'"),
        ("created_at >= '2025-12-01 10:30:00'",
         "created_at >= timestamp '2025-12-01 10:30:00'"),
        # 已经写了前缀的不许重复加
        ("dt >= date '2025-12-01'", "dt >= date '2025-12-01'"),
        ("dt >= DATE '2025-12-01'", "dt >= DATE '2025-12-01'"),
        # 关键负例：列名不像日期就别碰，否则把正常的字符串比较改坏
        ("order_no = '2025-12-01'", "order_no = '2025-12-01'"),
        ("version = '2025-12-01'", "version = '2025-12-01'"),
        # —— 不该动的 ——
        ("sum(x) FILTER (WHERE s = 'paid')", "sum(x) FILTER (WHERE s = 'paid')"),
        ("date_trunc('week', dt)", "date_trunc('week', dt)"),
        ("a || b", "a || b"),
        # —— `(` 前的标识符不都是函数名 ——
        # FILTER：操作数是整个 sum(...) FILTER (...)，不能只切括号
        ("sum(gmv) FILTER (WHERE mon='2025-12')::int",
         "CAST(sum(gmv) FILTER (WHERE mon='2025-12') AS integer)"),
        ("sum(x) OVER (PARTITION BY k)::numeric",
         "CAST(sum(x) OVER (PARTITION BY k) AS decimal(38,6))"),
        # 关键字：操作数只到括号，别把 WHERE 吃进 CAST
        ("WHERE (a + b)::int > 0", "WHERE CAST((a + b) AS integer) > 0"),
        # 链式转型（要求从左往右扫）
        ("x::int::text", "CAST(CAST(x AS integer) AS varchar)"),
        # 左边没有操作数：跳过它，但**同一段里其他地方照改**
        ("注意 `::numeric` 转换；SELECT a::int",
         "注意 `::numeric` 转换；SELECT CAST(a AS integer)"),
    ]
    bad = 0
    for src, want in cases:
        got, _ = convert(src)
        ok = " ".join(got.split()) == " ".join(want.split())
        if not ok:
            bad += 1
            print(f"  FAIL {src}")
            print(f"       得到: {got}")
            print(f"       期望: {want}")

    # map_type 单测
    for src, want in [("numeric", "decimal(38,6)"), ("numeric(14,2)", "decimal(14,2)"),
                      ("numeric(8, 4)", "decimal(8,4)"), ("TEXT", "varchar"),
                      ("date", "date"), ("varchar(20)", "varchar(20)")]:
        got = map_type(src)
        if got != want:
            bad += 1
            print(f"  FAIL map_type({src!r}) → {got!r}，期望 {want!r}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  改写用例 {len(cases)} 项、类型映射 6 项")
    print("全部通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Postgres SQL → Trino（Athena）定向改写")
    ap.add_argument("src", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--text", help="直接改一小段 SQL（调试用）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.text:
        sql, stats = convert(a.text)
        print(sql)
        print(f"-- 改写统计：{stats}", file=sys.stderr)
        return 0
    if not a.src:
        ap.error("需要 src / --text / --selftest")
    sql, stats = convert(Path(a.src).read_text(encoding="utf-8"))
    header = ("-- ⚙️ 由 scripts/gen/pg_to_trino.py 从 " + a.src + " 转换而来\n"
              "-- DO NOT EDIT —— 改逻辑请改上游后重跑转换。\n"
              f"-- 改写统计：{stats}\n\n")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(header + sql, encoding="utf-8")
        print(f"→ {a.out}   改写统计 {stats}")
    else:
        print(header + sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
