#!/usr/bin/env python3
"""Athena 客户端 —— 查询 S3 Tables（Iceberg）的统一入口。

对齐 `scripts/redshift/rsql.py` 的接口（`Client.execute(sql) -> {columns, rows,
rowcount, elapsed_ms}`），这样 `backend/db.py` 换后端时只改分派、不改上层契约。

## 为什么是「提交 → 轮询 → 取结果」

跟 Redshift Data API 一样，Athena 也是异步 API：`StartQueryExecution` 拿
QueryExecutionId → `GetQueryExecution` 轮询状态 → `GetQueryResults` 取结果。
本模块把三步包成同步调用。

好处跟 Data API 一致：HTTPS + IAM，**没有 VPC、没有连接池、没有落地密码**，
AgentCore Runtime 直接调。比 Redshift Serverless 还少一样东西——没有 workgroup
容量下限，Athena 按扫描字节计费，空闲时真的零成本。

## 五个实测踩出来的坑（每一个都花过时间）

**1. catalog 名带斜杠时，不能写进 SQL。** S3 Tables 联邦进 Glue 之后 catalog 名是
`s3tablescatalog/<表桶名>`，里面那个 `/` 会让 Trino 解析器直接拒绝：

    SHOW TABLES IN "s3tablescatalog/analytics-agent-tables".app_analytics
    → MALFORMED_QUERY: mismatched input '/'

即使加双引号也不行。正确做法是**把 catalog/database 放进 QueryExecutionContext**，
SQL 里只写裸表名。这个约束反过来是好事：21 条金标 SQL 里的表名一个字都不用改。

**2. 非 ASCII 列别名必须双引号。** `SELECT count(*) AS 活跃用户` 在 Postgres/Redshift
里合法，Trino 直接 `MALFORMED_QUERY`。要么写 `AS "活跃用户"`，要么用 ASCII 别名。
知识库示例 SQL 里有中文别名，改方言时一并处理。

**3. CSV 转 timestamp 会静默丢精度。** `CAST(s AS timestamp)` 只到**毫秒**：
`2026-01-07 14:11:10.53532` 进去，出来是 `.535`——不报错、不告警，就是少了两位。
必须写 `CAST(s AS timestamp(6))` 才拿到 `.535320`。数据生成器的时间戳带 6 位小数，
灌数时用错这一个类型，一致性对账会在「明明 COPY 成功」的前提下莫名对不上。

**4. 联邦 catalog 不认 `IAM_ALLOWED_PRINCIPALS` 向后兼容模式。** 就算你是 Lake
Formation data lake admin，第一次 `CREATE TABLE` 也会被拒：

    Iceberg cannot access the requested resource: Forbidden:
    Insufficient Lake Formation permission(s): Required Create Table on app_analytics

普通 Glue database 靠 `IAM_ALLOWED_PRINCIPALS` 默认放行，联邦 catalog 不吃这一套，
必须显式 `lakeformation grant-permissions`。setup.py 的第 6 步就是干这个的。

**5. SELECT 结果的第一行是表头，要跳掉。** `GetQueryResults` 对 SELECT 会把列名当作
`Rows[0]` 返回（**只有第一页**如此，后续页没有）。不跳就会在数据里凭空多出一行
全是列名的字符串——数值列变成 `'user_id'` 这种，报错位置离真因很远。
DDL / INSERT 没有结果集，不受影响。

**6. Iceberg DDL 会吃掉换行，`--` 行注释会吞掉下一行代码。** 最小复现：

    -- 头
                                    ← 空行
    CREATE TABLE t (a bigint)
    → FAILED: line 1:35: no viable alternative at input '<EOF>'

`'-- 头' + 'CREATE TABLE t (a bigint)'` 拼起来正好 34 字符——整条语句被当成注释了。
连续换行被折叠，`--` 后面的东西全进注释。报错位置离真因极远（曾报「第 13 行的
`)`」，而第 13 行是空行）。DDL 脚本里改用 `/* */` 块注释（有显式结束符，不依赖
换行）和列级 `COMMENT '...'` 子句。`--file` 会在提交前把这种写法检出来并明说，
但**不会替你改写 SQL**——静默改写用户的 SQL 比报错更坏。

## 类型还原

Athena 把所有值都装在 `VarCharValue` 里，必须按 `ResultSetMetadata` 的列类型还原成
Python 值。这跟 rsql.py 里那段 parity 修复是同一个问题、同一个理由：不还原的话
DECIMAL 回来是字符串，eval 的数值提取直接漏掉，表现为「SQL 完全正确但判定失败」。

用法：

    python3 scripts/lakehouse/athena.py "SELECT count(*) FROM users"
    python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql
    python3 scripts/lakehouse/athena.py --json "SELECT * FROM users LIMIT 3"
    python3 scripts/lakehouse/athena.py --raw "SHOW TABLES"     # 不走 Iceberg catalog
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")

# **两个 workgroup，不是一个。** 管理侧（建表、灌数、对账、L0–L6 的各类核查）走
# `WORKGROUP`；agent 那个最小权限角色走 `AGENT_WORKGROUP`。
#
# 分开的理由不是配额也不是计费口径，是**结果集**：workgroup 的 OutputLocation 决定
# 查询结果写到哪个 S3 前缀，而结果集是**明文行数据的 CSV**。共用一个 workgroup 时
# 两侧的结果落在同一个前缀下，而 agent 角色对那个前缀有 GetObject / PutObject ——
# 于是 LF 在目录层排除掉的 `users.email`，agent 可以从**管理侧的结果文件**里读回来
# （治理探针自己就会跑 `SELECT email FROM users LIMIT 1`，那一行明文就落在那儿），
# 顺带还能覆盖管理侧的结果对象。列级排除挡的是"查得到吗"，挡不了"结果放哪儿"。
#
# 拆成两个 workgroup + 两个子前缀之后，agent 的 S3 授权面只到
# `AGENT_STAGING_PREFIX`，管理侧那半边它既读不到也写不到。
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "analytics-agent-wg")
AGENT_WORKGROUP = os.environ.get("ATHENA_AGENT_WORKGROUP", "analytics-agent-ro-wg")

# S3 表桶名。catalog 名由它派生，两处形式不同，见下面两个常量。
TABLE_BUCKET = os.environ.get("S3_TABLE_BUCKET", "analytics-agent-tables")
NAMESPACE = os.environ.get("ICEBERG_NAMESPACE", "app_analytics")

# 同一个 catalog 的两种写法，**别混用**：
#   ATHENA_CATALOG —— 给 QueryExecutionContext.Catalog，不带账号前缀
#   GLUE_CATALOG_ID —— 给 boto3 glue 的 CatalogId 参数，必须带 `<账号>:` 前缀
# 混用的报错是 EntityNotFoundException，看不出是前缀问题。
ATHENA_CATALOG = os.environ.get("ATHENA_CATALOG",
                                f"s3tablescatalog/{TABLE_BUCKET}")

# 原始 CSV 落地桶 + Athena 结果暂存前缀。CSV 外部表建在这里，灌完可删。
RAW_BUCKET = os.environ.get("RAW_BUCKET", "analytics-agent-raw")

# 结果暂存前缀，**只在这里定义一次**。setup.py（建 workgroup 时的 OutputLocation）
# 和 governance.py（写进 IAM 策略 Resource 的那一串）原来各写一份同样的字面量，
# 两边漂了的表现是"策略文本上看着给了、查询照样报拿不到结果桶"——报错指向桶，
# 不指向两个常量不一致。agent 那半边是它的**子前缀**，于是一条 s3:GetObject 的
# Resource 就能把两侧隔开，不用再开第二个桶。
STAGING_PREFIX = "athena-staging/"
AGENT_STAGING_PREFIX = STAGING_PREFIX + "agent/"

# 轮询节奏。日常用宽一点（少打 API），**计时基准要调窄**：这个间隔会整体
# 加到客户端墙上时钟上，而三条 arm 的间隔不同（DuckDB 是进程内，根本不轮询），
# 于是「谁快」里会掺进「谁的轮询设得松」。实测：一条引擎 690ms 的查询，
# 默认节奏下墙上时钟 2230ms。见 scripts/bench/timing.py。
POLL_INITIAL = float(os.environ.get("ATHENA_POLL_INITIAL", "0.15"))
POLL_MAX = float(os.environ.get("ATHENA_POLL_MAX", "2.0"))


class AthenaError(RuntimeError):
    pass


def glue_catalog_id(account: str | None = None) -> str:
    """给 boto3 glue 用的 CatalogId：`<账号>:s3tablescatalog/<表桶>`。"""
    if os.environ.get("GLUE_CATALOG_ID"):
        return os.environ["GLUE_CATALOG_ID"]
    acct = account or boto3.client("sts", region_name=REGION) \
        .get_caller_identity()["Account"]
    return f"{acct}:s3tablescatalog/{TABLE_BUCKET}"


# ---------------------------------------------------------------- 以什么身份查

def assume_role_session(role_arn: str, region: str = REGION,
                        session_name: str = "analytics-agent"):
    """返回一个用 AssumeRole 凭证的 boto3 Session，**会自动续期**。

    L4 治理层（`governance.py`）建了一个最小权限角色，后端和探针都要以它的身份查数，
    而不是以开发者的 admin 身份。

    为什么不是一句 `sts.assume_role()`：那样拿到的是一组**固定**的临时凭证，默认 1
    小时后过期。后端进程活得比这久，于是表现成"跑了一上午突然 ExpiredTokenException"
    ——而且大概率是在 agent 答题的中途，报错位置离原因很远。botocore 的
    `AssumeRoleCredentialFetcher` + `DeferredRefreshableCredentials` 会在快过期时自己
    重新 assume；这也是 `~/.aws/config` 里配 `role_arn` 时 CLI/SDK 走的同一条路。

    凭证是**延迟获取**的：这个函数不发 STS 调用，第一次真的用到才发。所以
    "角色不存在 / 信任策略里没有你"这类错误会在第一次查询时才炸——调用方要么
    自己先 `get_caller_identity()` 探一次（`db.py` 就是这么做的，为了让 /health
    在配错时立刻说实话），要么接受报错点靠后。
    """
    import botocore.session
    from botocore.credentials import (AssumeRoleCredentialFetcher,
                                      DeferredRefreshableCredentials)

    src = boto3.Session(region_name=region)
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=src._session.create_client,
        source_credentials=src.get_credentials(),
        role_arn=role_arn,
        extra_args={"RoleSessionName": session_name},
    )
    bs = botocore.session.get_session()
    bs._credentials = DeferredRefreshableCredentials(
        method="assume-role", refresh_using=fetcher.fetch_credentials)
    bs.set_config_variable("region", region)
    return boto3.Session(botocore_session=bs)


class Client:
    """同步化的 Athena 客户端。

    `catalog` / `database` 走 QueryExecutionContext 而不是 SQL —— 见模块 docstring
    第 1 点。传 `catalog=None` 则完全不带 context，用于查 `awsdatacatalog`
    （原始 CSV 外部表所在的普通 Glue 目录）。

    `session` 给治理层用：传一个 `assume_role_session()` 出来的 Session，这个客户端
    就以最小权限角色的身份查。不传就走默认凭证链（本地开发 = 你自己的身份）。
    """

    def __init__(self, catalog: str | None = ATHENA_CATALOG,
                 database: str = NAMESPACE, workgroup: str = WORKGROUP,
                 region: str = REGION, session=None):
        self.catalog, self.database, self.workgroup = catalog, database, workgroup
        self._c = (session or boto3.Session()).client("athena", region_name=region)

    def _ctx(self, catalog: str | None, database: str | None) -> dict:
        cat = self.catalog if catalog is None else catalog
        db = self.database if database is None else database
        if not cat:
            return {}
        ctx = {"Catalog": cat}
        if db:
            ctx["Database"] = db
        return {"QueryExecutionContext": ctx}

    def execute(self, sql: str, timeout: float = 900.0, fetch: bool = True,
                catalog: str | None = None, database: str | None = None) -> dict:
        """执行一条 SQL，同步等到结束。有结果集且 fetch=True 时返回 columns/rows。"""
        qid = self._c.start_query_execution(
            QueryString=sql, WorkGroup=self.workgroup,
            **self._ctx(catalog, database))["QueryExecutionId"]
        return self._wait(qid, sql, timeout, fetch)

    def _wait(self, qid: str, sql: str, timeout: float, fetch: bool) -> dict:
        t0, delay = time.time(), POLL_INITIAL
        while True:
            q = self._c.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            st = q["Status"]["State"]
            if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() - t0 > timeout:
                # 超时要主动取消，否则查询继续跑继续扫字节继续计费。
                try:
                    self._c.stop_query_execution(QueryExecutionId=qid)
                except ClientError:
                    pass
                raise AthenaError(f"超时 {timeout}s（已取消）：{sql[:120]}")
            time.sleep(delay)
            delay = min(delay * 1.5, POLL_MAX)

        stats = q.get("Statistics", {})
        if st != "SUCCEEDED":
            reason = q["Status"].get("StateChangeReason", "(无错误信息)")
            raise AthenaError(f"{st}: {reason}\nSQL: {sql[:400]}")

        out = {
            "query_id": qid,
            "elapsed_ms": stats.get("EngineExecutionTimeInMillis", 0),
            "total_ms": stats.get("TotalExecutionTimeInMillis", 0),
            # 成本可观测性：Athena 按扫描字节计费，把它一路传到 UI 上，
            # 「这个问题花了多少钱」就不再是黑盒。Iceberg 的 count(*) 走元数据，
            # 这里会是 0——那不是 bug，是列式 + 清单文件该有的样子。
            "bytes_scanned": stats.get("DataScannedInBytes", 0),
            "rows_affected": stats.get("DataManifestLocation") and -1 or -1,
        }
        if fetch:
            out.update(self._results(qid))
        return out

    def _results(self, qid: str) -> dict:
        """取结果并按列类型还原 Python 值。第一页首行是表头，跳掉（见 docstring 第 5 点）。"""
        cols: list[str] = []
        types: list[str] = []
        rows: list[list] = []
        tok = None
        first_page = True
        while True:
            kw = {"QueryExecutionId": qid, "MaxResults": 1000}
            if tok:
                kw["NextToken"] = tok
            try:
                r = self._c.get_query_results(**kw)
            except ClientError as e:
                # DDL（CREATE/INSERT）没有结果集，这里会报 InvalidRequestException。
                # 不是错误，是「本来就没东西可取」。
                if e.response["Error"]["Code"] in ("InvalidRequestException",):
                    return {"columns": [], "rows": [], "rowcount": 0}
                raise
            meta = r["ResultSet"].get("ResultSetMetadata", {}).get("ColumnInfo", [])
            if not cols and meta:
                cols = [c["Name"] for c in meta]
                types = [str(c.get("Type", "")).lower() for c in meta]
            page = r["ResultSet"].get("Rows", [])
            if first_page and page and cols:
                # 只有第一页的首行是表头。判一下确实等于列名再跳，避免误删数据行：
                # 万一将来 API 改了行为，宁可多一行也别静默少一行。
                head = [f.get("VarCharValue") for f in page[0].get("Data", [])]
                if head == cols:
                    page = page[1:]
            first_page = False
            for rec in page:
                data = rec.get("Data", [])
                rows.append([_scalar(data[i] if i < len(data) else {},
                                     types[i] if i < len(types) else "")
                             for i in range(len(cols))])
            tok = r.get("NextToken")
            if not tok:
                break
        return {"columns": cols, "rows": rows, "rowcount": len(rows),
                "column_types": types}


# ---------------------------------------------------------------- 类型还原

_INT_TYPES = {"tinyint", "smallint", "integer", "int", "bigint"}
_FLOAT_TYPES = {"double", "float", "real", "decimal"}
_TS_TYPES = {"timestamp", "timestamp with time zone",
             "timestamp without time zone"}


def _scalar(field: dict, type_name: str = ""):
    """把 Athena 的 `VarCharValue` 还原成与 psycopg 路径**同类型**的 Python 值。

    跟 rsql.py 的 `_scalar` 同一个理由：不还原的话 DECIMAL 回来是字符串，
    eval 的数值提取直接漏掉，表现成「SQL 正确但判定失败」。
    Athena 比 Data API 更极端——**所有**类型都只有 VarCharValue，连 bigint 也是。
    """
    if "VarCharValue" not in field:
        return None                                   # 字段缺失即 NULL
    s = field["VarCharValue"]
    if s is None:
        return None
    if type_name in _INT_TYPES:
        try:
            return int(s)
        except (TypeError, ValueError):
            return s
    if type_name in _FLOAT_TYPES:
        try:
            return float(s)
        except (TypeError, ValueError):
            return s
    if type_name == "boolean":
        return s.lower() == "true"
    if type_name in _TS_TYPES and isinstance(s, str):
        # Athena 回 '2026-01-07 14:11:10.535320'（空格分隔）；psycopg 路径回 datetime
        # 后被 db.py 格式成 isoformat 的 T 形式。这里对齐成 T，两个后端契约一致。
        t = s.replace(" ", "T", 1)
        return t[:-2] if t.endswith(".0") else t
    return s


# ---------------------------------------------------------------- SQL 拆分

def split_statements(sql: str) -> list[str]:
    """按分号拆分 SQL，**尊重字符串字面量、标识符引号和注释**。

    朴素的 `sql.split(";")` 会在这类语句上炸掉：

        COMMENT ON TABLE t IS '粒度=dt;归因=last_touch';

    注释文本里的 ASCII 分号被当成语句结束符，字符串被截断，报
    "Unterminated string literal"。这里做一遍字符级扫描：单引号内（含 '' 转义）、
    双引号标识符内、`--` 行注释和 `/* */` 块注释内的分号都不算分隔符。

    这段实现原来是 `sys.path.insert(.../redshift)` + `import rsql.split_statements`。
    那条路径有两个问题，第二个是硬故障：(1) `scripts/redshift/` 是 Redshift 退役后的
    死路径（见 AGENTS.md），活代码不该反过来依赖它；(2) 本文件会被
    `scripts/deploy/sync_agent_code.py` 逐字节拷进 `analyticsagent/app/analytics/`，
    容器里**没有** `/app/redshift`，所以那个 import 在云上必然 ImportError。它一直没炸
    只是因为拆语句只走 `--file` 那条仓库侧 CLI 路径。行为与 rsql 那份一致；那份留在
    死路径里不动，作为 v2 的历史记录。
    """
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    in_s = in_d = in_line = in_block = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line:
            cur.append(ch)
            if ch == "\n":
                in_line = False
        elif in_block:
            cur.append(ch)
            if ch == "*" and nxt == "/":
                cur.append(nxt)
                i += 1
                in_block = False
        elif in_s:
            cur.append(ch)
            if ch == "'":
                if nxt == "'":          # '' 是转义的单引号，不结束字面量
                    cur.append(nxt)
                    i += 1
                else:
                    in_s = False
        elif in_d:
            cur.append(ch)
            if ch == '"':
                in_d = False
        elif ch == "-" and nxt == "-":
            cur.append(ch)
            in_line = True
        elif ch == "/" and nxt == "*":
            cur.append(ch)
            in_block = True
        elif ch == "'":
            cur.append(ch)
            in_s = True
        elif ch == '"':
            cur.append(ch)
            in_d = True
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))

    # 丢掉纯注释/空白的片段
    res = []
    for s in out:
        body = re.sub(r"--[^\n]*", "", s)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL).strip()
        if body:
            res.append(s.strip())
    return res


def line_comment_lines(sql: str) -> list[int]:
    """返回语句里含 `--` 行注释的行号（1 起）。字面量和块注释里的 `--` 不算。

    这是坑 6 的诊断。Athena 的 Iceberg DDL 会把 `--` 后面的换行吃掉，于是下一行
    代码整行被注释吞掉，而报错指向的位置和真因差十几行。与其让人对着
    `mismatched input ')'` 猜半天，不如提交前直接说清楚。

    抹字面量的扫描器复用 gen_ddl（那个模块只依赖标准库，L0 自测要求无云依赖，
    所以是 gen_ddl 借给这里，不是反过来）。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gen_ddl import _strip_literals
    return [i for i, line in enumerate(_strip_literals(sql).splitlines(), 1)
            if "--" in line]


# ---------------------------------------------------------------- CLI

def _print_table(res: dict) -> None:
    cols, rows = res.get("columns", []), res.get("rows", [])
    scanned = res.get("bytes_scanned", 0)
    tail = (f"耗时 {res.get('elapsed_ms', 0)}ms  "
            f"扫描 {_human(scanned)}")
    if not cols:
        print(f"(无结果集)  {tail}")
        return
    w = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
         for i, c in enumerate(cols)]
    print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * w[i] for i in range(len(cols))))
    for r in rows[:200]:
        print("  ".join(str(v).ljust(w[i]) for i, v in enumerate(r)))
    print(f"\n{len(rows)} 行  {tail}")


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Athena 客户端（S3 Tables / Iceberg）")
    ap.add_argument("sql", nargs="?", help="要执行的 SQL；省略则从 --file 读")
    ap.add_argument("--file", help="从文件读 SQL（按 ; 拆成多条顺序执行）")
    ap.add_argument("--catalog", default=ATHENA_CATALOG)
    ap.add_argument("--database", default=NAMESPACE)
    ap.add_argument("--workgroup", default=WORKGROUP)
    ap.add_argument("--raw", action="store_true",
                    help="改用 awsdatacatalog（原始 CSV 外部表所在的普通 Glue 目录）。"
                         "仍然带 --database，否则 DESCRIBE / SHOW 这类语句没有库上下文")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--tolerate", help="错误信息匹配该正则时继续执行（用于幂等语句，"
                                       "如 'already exists'）。不匹配的错误仍然中断。")
    a = ap.parse_args()

    # --raw 只换 catalog，database 照旧传：不传 database 的话 DESCRIBE / SHOW TABLES
    # 会落到 Athena 的默认库上，报「表不存在」，而真因是少了库上下文。
    cat = "awsdatacatalog" if a.raw else a.catalog
    c = Client(catalog=cat, database=a.database, workgroup=a.workgroup)

    if a.file:
        stmts = split_statements(open(a.file, encoding="utf-8").read())
        print(f"{len(stmts)} 条语句")
        ok = 0
        # 提交前先把坑 6 检出来。只对 DDL 报：DML 走 Trino 解析器，`--` 正常。
        for i, s in enumerate(stmts, 1):
            if not re.match(r"\s*(/\*.*?\*/\s*)*(CREATE|ALTER|DROP)\b", s,
                            re.IGNORECASE | re.DOTALL):
                continue
            hits = line_comment_lines(s)
            if hits:
                print(f"  [{i}/{len(stmts)}] ⚠️  第 {hits} 行有 -- 行注释。"
                      f"Athena 的 Iceberg DDL 会吃掉换行，`--` 会把下一行代码\n"
                      f"      一起吞进注释，报错位置和真因差十几行"
                      f"（见 athena.py 模块 docstring 坑 6）。\n"
                      f"      改成 /* */ 块注释，列注释改用 COMMENT '...' 子句。")
        for i, s in enumerate(stmts, 1):
            head = " ".join(s.split())[:70]
            try:
                r = c.execute(s, timeout=a.timeout, fetch=False)
                print(f"  [{i}/{len(stmts)}] ok   {head}  "
                      f"({r.get('elapsed_ms', 0)}ms)")
                ok += 1
            except AthenaError as e:
                if a.tolerate and re.search(a.tolerate, str(e), re.IGNORECASE):
                    print(f"  [{i}/{len(stmts)}] skip {head}  （已容忍）")
                    continue
                print(f"  [{i}/{len(stmts)}] FAIL {head}\n      {e}")
                return 1
        print(f"\n{ok}/{len(stmts)} 条成功 ✅")
        return 0

    if not a.sql:
        ap.error("需要 sql 或 --file")
    res = c.execute(a.sql, timeout=a.timeout)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        _print_table(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
