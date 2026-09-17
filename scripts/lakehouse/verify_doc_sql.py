#!/usr/bin/env python3
"""把 knowledge/**/*.md 里的 SQL 示例拿去 Athena 过一遍，看它们是不是真的还能跑。

## 为什么需要这个

progressive disclosure 的整个前提是：agent 读 `knowledge/` 里的文档来决定怎么写
SQL。那么文档里的 SQL 示例**就是代码**——它错了，agent 就照着错的写。但文档不会
被执行，所以它错了没有任何东西会响。这个脚本就是那个"响"。

迁移到 Trino 时这一点尤其要紧：`interval '2' week` 这种写法在 Postgres 下是对的、
读起来也像对的，只有真交给 Athena 才知道 Trino 没有 week 这个单位。

## 用 EXPLAIN 而不是真执行

`EXPLAIN <query>` 走完整的语法 + 目录 + 列名 + 类型解析，但**扫描 0 字节**。
它能抓到方言错、列名错、类型不匹配——也就是文档腐烂的绝大多数形态。

它抓不到的是**结果对不对**（口径、JOIN 基数、时间锚点），那不是本脚本的职责：
口径由 `backend/metrics_def.py` + `scripts/lakehouse/verify_mart_parity.py` 管。

## 故意跳过的

- 非 SELECT/WITH 的块（建表 DDL、bash、输出示例）
- 含参数占位符的（`?` 和 `:name`）——文档里故意留的坑位，不是完整语句

## 为什么还要一道**离线**的表名检查

上面那条"跳过含占位符的"曾经是一个真实的漏洞：`knowledge/relationships.md` 的两个
可复制 JOIN 示例都以 `WHERE u.user_id = ?` 结尾，于是 `PLACEHOLDER` 把整条语句跳过，
EXPLAIN **从没跑过它们**——而它们 JOIN 的 `user_levels` / `product_skus` 在本库根本
不存在。同一个文件的外键表里一共列了 10 张幻表。跳过是对的（那不是完整语句），
但"跳过"当时等于"没有任何检查"，而幻表恰恰是这类示例最容易犯的错。

所以多了一道 `table_refs()`：从 `database/iceberg/*.sql` 的 `CREATE TABLE` 取真表名，
把**所有** SELECT/WITH 块（含被 EXPLAIN 跳过的那些）里 `FROM` / `JOIN` 后面的标识符
比一遍。它**不连云**，所以能进 L0 静态自测；代价是只查表名存在性，列名和方言仍然
只有 EXPLAIN 能管。两道各有各的盲区，别用一道替另一道。

用法：
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py            # 离线表名 + EXPLAIN
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py --offline  # 只做离线表名检查
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py --selftest # 提取器自测
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py --only mart
退出码非 0 表示有文档里的 SQL 已经跑不动了。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge"
FENCE = re.compile(r"```sql\n(.*?)```", re.S)
# 参数占位符：`WHERE user_id = ?` / `= :target_user_id`。文档里的示范写法。
PLACEHOLDER = re.compile(r"(?<![:\w]):[A-Za-z_]\w*|\?")
WORKERS = int(os.getenv("DOC_SQL_WORKERS", "12"))

# ------------------------------------------------------------------ 离线表名检查

DDL_DIR = ROOT / "database" / "iceberg"
_CREATE = re.compile(r'(?is)\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?"?([a-z_]\w*)"?')

# `FROM` / `JOIN` 之后那一段关系列表。只取每个逗号分段的**第一个**标识符——
# 后面跟的是别名，而别名位置误吞一个关键字（`FROM orders WHERE` → 别名 "WHERE"）
# 不影响结果，正是靠"只取第一个"把它消化掉的。
#
# 第一个标识符后面的 `(?!\s*\()` 是为了避开 `FROM` 的**另一种语法角色**：
# `EXTRACT(DAY FROM coalesce(a, b))`、`substring(s FROM 1 FOR 2)`、
# `trim(BOTH ' ' FROM s)` 里的 FROM 不引入关系，后面跟的是函数调用。
# 实测漏过一次（`ab_tests.md` 的 `coalesce` 被报成幻表）。`\b` 不能省：少了它
# 正则会回退一个字符去满足否定预查，`coalesce(` 变成 `coalesc` 照样过。
_REL = re.compile(
    r'(?is)\b(?:from|join)\s+'
    r'("?[a-z_]\w*\b"?(?!\s*\()(?:\s+(?:as\s+)?[a-z_]\w*)?'
    r'(?:\s*,\s*"?[a-z_]\w*\b"?(?!\s*\()(?:\s+(?:as\s+)?[a-z_]\w*)?)*)')
# CTE 名：`WITH a AS (` / `, cohort AS (` / `WITH RECURSIVE t (c1, c2) AS (`。
# 它们不是表，不该被当成幻表报出来。带列清单的那种形态实测漏过一次
# （`categories.md` 的递归 CTE `category_tree`）。
_CTE = re.compile(
    r'(?is)(?:\bwith\s+(?:recursive\s+)?|,\s*)([a-z_]\w*)\s*(?:\([^()]*\))?\s+as\s*\(')
# 不是表的 FROM/JOIN 目标。`unnest` 是 Trino 的表函数；`lateral` 后面跟子查询。
_NOT_A_TABLE = {"unnest", "lateral", "table", "values"}


def _strip_comments(sql: str) -> str:
    """去掉 `--` 行注释与 `/* */` 块注释。

    两边都需要：DDL 侧 `02_mart.sql` 的块注释里写着
    `CREATE TABLE x AS SELECT ...`（在讲为什么不用 CTAS），不剥注释会把 `x`
    收进真表名集合——那是**放松**检查，比误报更难发现。
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def known_tables() -> set[str]:
    """真表名取自建表 DDL（`database/iceberg/*.sql`）——声明侧的真源，不连云。

    刻意**不**从 `knowledge/` 自己的表清单取：那样就是拿文档校验文档，
    幻表在两边同时存在时会一起通过。
    """
    out: set[str] = set()
    for f in sorted(DDL_DIR.glob("*.sql")):
        out |= set(_CREATE.findall(_strip_comments(f.read_text(encoding="utf-8"))))
    return out


def table_refs(sql: str) -> set[str]:
    """一条语句里 FROM / JOIN 引用到的表名（已剔除 CTE 名、别名、表函数）。"""
    sql = _strip_comments(sql)
    ctes = {c.lower() for c in _CTE.findall(sql)}
    refs: set[str] = set()
    for seg in _REL.findall(sql):
        for part in seg.split(","):
            tok = part.strip().split()[0].strip('"').lower() if part.strip() else ""
            if tok and tok not in ctes and tok not in _NOT_A_TABLE:
                refs.add(tok)
    return refs


def check_table_names(only: str | None = None) -> tuple[int, int, list[str]]:
    """→ (查过的语句数, 引用到的表数, 问题列表)。不连云。"""
    real = known_tables()
    if not real:
        return 0, 0, [f"没从 {DDL_DIR} 里解析出任何 CREATE TABLE——DDL 挪走了？"]
    stmts, seen, bad = 0, set(), []
    for rel, s in _blocks(only):
        stmts += 1
        for t in sorted(table_refs(s)):
            seen.add(t)
            if t not in real:
                head = next((l for l in s.splitlines() if l.strip()), "")[:90]
                bad.append(f"{rel}：引用了不存在的表 `{t}`\n      {head}")
    return stmts, len(seen), bad


# ------------------------------------------------------------------ 语句收集

def _blocks(only: str | None = None):
    """所有 SELECT/WITH 语句，**含**含占位符的那些。表名检查用这个全集。"""
    for p in sorted(KNOWLEDGE.rglob("*.md")):
        rel = str(p.relative_to(ROOT))
        if only and only not in rel:
            continue
        for blk in FENCE.findall(p.read_text(encoding="utf-8")):
            for stmt in blk.split(";"):
                s = "\n".join(l for l in stmt.splitlines()
                              if not l.strip().startswith("--")).strip()
                if s and re.match(r"(?is)^\s*(select|with)\b", s):
                    yield rel, s


def collect(only: str | None = None) -> tuple[list[tuple[str, str]], int]:
    """可以交给 EXPLAIN 的语句（含占位符的排除在外）。"""
    jobs: list[tuple[str, str]] = []
    skipped = 0
    for rel, s in _blocks(only):
        if PLACEHOLDER.search(s):
            skipped += 1
            continue
        jobs.append((rel, s))
    return jobs, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 knowledge/ 文档里的 SQL 能否被 Athena 接受")
    ap.add_argument("--only", help="只查路径里含该子串的文档")
    ap.add_argument("--verbose", action="store_true", help="打印每条失败语句的全文")
    ap.add_argument("--offline", action="store_true",
                    help="只做离线表名检查（不连云，可进 L0）")
    ap.add_argument("--selftest", action="store_true", help="表名提取器自测（不连云）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    # 表名检查先跑：它不花钱、覆盖面更大（含被 EXPLAIN 跳过的语句），
    # 而且幻表这类错误 EXPLAIN 的报错信息反而更难读。
    stmts, ntab, bad_names = check_table_names(a.only)
    print(f"离线表名检查：{stmts} 条语句、{ntab} 个表引用")
    for b in bad_names:
        print(f"  ✗ {b}")
    if bad_names:
        print(f"\n{len(bad_names)} 处引用了本库不存在的表 ❌  "
              f"真表名见 database/iceberg/*.sql")
        return 1
    print("  表名全部存在 ✅")
    if a.offline:
        return 0

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from athena import Client  # noqa: PLC0415  延迟导入：--offline / --selftest 不需要 boto3

    jobs, skipped = collect(a.only)
    if not jobs:
        print("没有可校验的语句")
        return 0
    files = len({f for f, _ in jobs})
    print(f"共 {len(jobs)} 条 SELECT/WITH，来自 {files} 个文档"
          f"（跳过 {skipped} 条含参数占位符的）")

    cli = Client()

    def check(job: tuple[str, str]):
        f, s = job
        try:
            cli.execute("EXPLAIN " + s, fetch=False)
            return f, s, None
        except Exception as e:                       # noqa: BLE001
            return f, s, str(e).replace("\n", " ")

    with ThreadPoolExecutor(min(len(jobs), WORKERS)) as ex:
        results = list(ex.map(check, jobs))

    bad = [r for r in results if r[2]]
    print(f"通过 {len(results) - len(bad)} / {len(results)}")
    for f, s, err in bad:
        head = next((l for l in s.splitlines() if l.strip()), "")[:100]
        print(f"\n  ✗ {f}\n    {head}\n    {err[:240]}")
        if a.verbose:
            print("    ---\n" + "\n".join("    " + l for l in s.splitlines()))
    if bad:
        print(f"\n{len(bad)} 条文档 SQL 跑不动 ❌")
        return 1
    print("\n全部通过 ✅")
    return 0


# ------------------------------------------------------------------ 自测

def selftest() -> int:
    """提取器自测。`table_refs` 是纯函数，每条分支都能在这里走一遍。

    最后两条是这个检查存在的**理由**：`relationships.md` 那两个带 `?` 的示例
    在被 EXPLAIN 跳过的同时，表名必须仍然被查到。
    """
    bad = 0

    def want(tag: str, sql: str, expect: set[str]) -> None:
        nonlocal bad
        got = table_refs(sql)
        if got != expect:
            bad += 1
            print(f"  FAIL {tag}\n       得到 {sorted(got)}\n       期望 {sorted(expect)}")

    want("最简", "SELECT 1 FROM orders", {"orders"})
    want("带别名", "SELECT * FROM orders o", {"orders"})
    want("AS 别名", "SELECT * FROM orders AS o", {"orders"})
    want("别名位置是关键字（只取第一个标识符）",
         "SELECT * FROM orders WHERE x = 1", {"orders"})
    want("多个 JOIN",
         "SELECT * FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
         "LEFT JOIN products p ON p.product_id = oi.product_id",
         {"orders", "order_items", "products"})
    want("逗号连接", "SELECT * FROM users u, user_profiles p WHERE u.x = p.x",
         {"users", "user_profiles"})
    # 没有 WITH 定义 `a` 时，`a` **应该**被报出来（那确实是个不存在的关系）
    want("CROSS JOIN（无 CTE 定义时不放过）",
         "SELECT * FROM coupons CROSS JOIN a", {"coupons", "a"})
    want("EXTRACT 里的 FROM 不是关系",
         "SELECT EXTRACT(DAY FROM coalesce(a, b)) FROM orders", {"orders"})
    want("substring 里的 FROM 不是关系",
         "SELECT substring(s FROM 1 FOR 2) FROM users", {"users"})
    want("带列清单的递归 CTE",
         "WITH RECURSIVE tree (id, lvl) AS (SELECT category_id, level FROM categories) "
         "SELECT * FROM tree", {"categories"})
    want("CTE 名不算表",
         "WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot) "
         "SELECT * FROM orders CROSS JOIN a", {"meta_snapshot", "orders"})
    want("多个 CTE",
         "WITH a AS (SELECT 1 FROM meta_snapshot), cohort AS (SELECT 1 FROM users) "
         "SELECT * FROM cohort JOIN a ON true", {"meta_snapshot", "users"})
    want("标量子查询里的表",
         "SELECT * FROM orders WHERE dt > (SELECT max(as_of_date) FROM meta_snapshot)",
         {"orders", "meta_snapshot"})
    want("带引号", 'SELECT * FROM "orders" o', {"orders"})
    want("UNNEST 不是表",
         "SELECT * FROM posts p CROSS JOIN UNNEST(p.product_ids) AS t(pid)", {"posts"})
    want("UNION 两侧",
         "SELECT a FROM t1 UNION ALL SELECT a FROM t2", {"t1", "t2"})
    want("CAST 里的 decimal 不会被当成 CTE",
         "SELECT CAST(count(*) AS decimal(38,6)) FROM orders", {"orders"})
    # 这个检查的全部价值：含占位符、被 EXPLAIN 跳过的语句，表名照样查得到
    ph = ("SELECT u.user_id FROM users u "
          "LEFT JOIN user_levels ul ON u.user_id = ul.user_id WHERE u.user_id = ?")
    want("被 EXPLAIN 跳过的语句里的幻表", ph, {"users", "user_levels"})
    if not PLACEHOLDER.search(ph):
        bad += 1
        print("  FAIL 上面那条本该被 PLACEHOLDER 判成'跳过 EXPLAIN'，现在不是了")

    want("行注释里的 FROM 不算引用",
         "SELECT 1 FROM orders -- 以前是 FROM user_levels", {"orders"})
    want("块注释里的 FROM 不算引用",
         "SELECT 1 /* FROM product_skus */ FROM orders", {"orders"})

    real = known_tables()
    if len(real) < 40:
        bad += 1
        print(f"  FAIL 只从 DDL 解析出 {len(real)} 张表，太少——建表语句的形态变了？")
    # 注释里的建表示例不许进真表名集合：那是**放松**检查
    if "x" in real:
        bad += 1
        print("  FAIL DDL 注释里的 `CREATE TABLE x AS SELECT` 被收进了真表名集合")
    for t in ("orders", "users", "meta_snapshot", "mart_daily_kpi"):
        if t not in real:
            bad += 1
            print(f"  FAIL DDL 里没解析出 {t}")
    for t in ("user_levels", "product_skus", "experiments"):
        if t in real:
            bad += 1
            print(f"  FAIL {t} 居然真存在——那这个自测的反例过期了，改掉它")

    # 全仓实跑一遍：自测绿了但仓库里有幻表，等于这个检查没接上
    stmts, ntab, problems = check_table_names()
    for p in problems:
        bad += 1
        print(f"  FAIL 仓库里有幻表：{p}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  提取器 21 项：别名/AS/关键字位/逗号/CROSS JOIN/CTE/递归 CTE/标量子查询/"
          "引号/UNNEST/UNION/CAST/EXTRACT/substring/行注释/块注释/占位符语句")
    print(f"  DDL 真源 {len(real)} 张表；knowledge/ 里 {stmts} 条语句、"
          f"{ntab} 个表引用全部存在")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
