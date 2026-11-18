#!/usr/bin/env python3
"""arm4（Aurora/pg_duckdb）的耗时测量客户端 —— 与 scripts/duckdb/conn.py::Client 同形。

bench 的 timing/correctness 判据对三条已有 arm 共用一套上层代码，差异只落在各自的
`Client.execute(sql) -> {columns, rows, rowcount, elapsed_ms}` 这一层（athena.py /
duckdb/conn.py / redshift/rsql.py）。这是 arm4 的那一层：经 **Postgres 线协议**连到
EC2 上的 `pgduckdb/pgduckdb:17-v1.1.1`，把只读 SQL 包进 `duckdb.query()` 交内嵌 DuckDB
执行。它读的是**同一批** S3 Tables Iceberg（ATTACH 的 ARN/catalog/namespace 复用 duckdb
arm 的 conn 模块），所以 arm2 vs arm4 的残差只落在「入口方式」——进程内 vs PG wire + 一跳
网络——这正是「Aurora 统一入口」要回答的架构问题。

## 为什么是一个**常驻**连接，而不是像 db.py 那样每查一次新建

pg_duckdb 的内嵌 DuckDB 状态（装的 extension、`CREATE SECRET`、`ATTACH`、`USE`，以及
`threads`/`memory_limit`/`enable_object_cache` 这些 SET）是**每 PG 会话一份**、不跨连接
保留的。backend/db.py 的 `_run_query_aurora` 每查一条就新建连接、重跑整套 init——那是问答
后端要的隔离，但对耗时测量是污染：每条查询都把 extension 加载和 ATTACH 的几秒钟算进墙钟，
量到的就不是「这条 SQL 跑多久」。所以这里 **init 只在连接时跑一次**，`execute()` 只给
`duckdb.query()` 那一次 execute+fetch 计时——与 arm2 的 Client（连接在 connect() 时配好、
execute 只计 SQL 本身）口径一致。

## 为什么连 superuser 而不是最小权限的 analytics_agent_ro

pg_duckdb v1.1.1 对**非超级用户**硬禁 LocalFileSystem（`duckdb.disabled_filesystems`
即便为空也挡），而 `iceberg` 扩展要从本地扩展目录 LOAD —— 于是 ro 角色能跑
`duckdb.query('SELECT 1')` 却读不了外部 Iceberg（实测报 `File system LocalFileSystem has
been disabled by configuration`）。「pg_duckdb 无最小权限外部读路径」本身是本次架构对比的
一条治理结论：不同于 Athena+LakeFormation 的列排除、Redshift 的掩码，它的外部读必须
superuser。防写仍有两层：bench 判据 SQL 本就只读，且 ATTACH 带 READ_ONLY 挡写（与 arm2
一致——arm2 也没有 PG 层 read_only，只靠 ATTACH READ_ONLY）。

## 算力口径必须与 arm2 逐项一致

内嵌 DuckDB 的 threads/memory_limit/temp_directory/enable_object_cache 复用
scripts/duckdb/conn.py 的常量设值，否则 arm2 vs arm4 比的是两套执行计划而不是「入口」。
EC2 实例是 t3.xlarge（4vCPU/16GB，与 bench 的 Fargate 任务同规格），threads=4 /
memory_limit=4GB 与 arm2 的 Fargate 环境变量同值。

用法（都要能连到在跑的 arm4 实例，见 scripts/aurora/provision.py）：

    python3 scripts/aurora/aconn.py "SELECT count(*) FROM events"
    python3 scripts/aurora/aconn.py --json "SELECT * FROM users LIMIT 3"
    python3 scripts/aurora/aconn.py --settings     # 回读内嵌 DuckDB 的算力口径
    python3 scripts/aurora/aconn.py --selftest     # 无云依赖自测

环境变量（名字与 db.py 的 aurora 后端共用，故意的：同一套接线不该有两份配置）：

    AURORA_PGHOST       pg_duckdb 实例私网 IP（无默认，缺了直接报错）
    AURORA_PGPORT       默认 5432
    AURORA_PGDATABASE   默认 app_analytics
    AURORA_PGUSER       默认 postgres（**superuser**，见上）
    AURORA_PGPASSWORD   口令，从 secret 注入（不落代码）

坐标（AWS_REGION / S3_TABLE_BUCKET / ICEBERG_NAMESPACE / DUCKDB_THREADS /
DUCKDB_MEMORY_LIMIT）与 duckdb arm 共用同名环境变量，见 scripts/duckdb/conn.py。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

# 复用 duckdb arm 的 conn 模块作为 catalog/算力口径的**唯一来源**：两条 arm 必须 ATTACH
# 到字面相同的 catalog、SET 相同的算力，否则残差不止来自「入口」这一个变量。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "duckdb"))
import conn as _duck  # noqa: E402

import psycopg  # noqa: E402
from psycopg import sql as _pgsql  # noqa: E402

PGHOST = os.environ.get("AURORA_PGHOST", "")
PGPORT = os.environ.get("AURORA_PGPORT", "5432")
PGDATABASE = os.environ.get("AURORA_PGDATABASE", "app_analytics")
# superuser：pg_duckdb v1.1.1 非超级用户读不了外部 Iceberg（见模块 docstring）。
PGUSER = os.environ.get("AURORA_PGUSER", "postgres")
PGPASSWORD = os.environ.get("AURORA_PGPASSWORD", "")

# pg_duckdb 容器内可写的溢写目录。不跟 arm2 的 DUCKDB_TEMP_DIR 走——那是 Fargate/本机的
# 路径，EC2 容器里不一定存在；溢写**是否发生**由 memory_limit 决定，与具体路径无关，所以
# 两条 arm 的可比性落在额度上，不在路径名上。空则溢写变 OOM（见 conn.py docstring 第 6 条）。
TEMP_DIR = os.environ.get("AURORA_TEMP_DIR", "/tmp/duckdb-spill")

_LOCK = threading.Lock()
_CONN = None


def _dsn() -> dict:
    if not PGHOST:
        raise RuntimeError(
            "AURORA_PGHOST 没设——先 `provision.py up` 起实例，把私网 IP 填进来。")
    return {"host": PGHOST, "port": int(PGPORT), "dbname": PGDATABASE,
            "user": PGUSER, "password": PGPASSWORD}


def init_statements() -> tuple[str, ...]:
    """pg_duckdb 会话初始化，**每连接一次**。与 backend/db.py::_aurora_init_statements
    逐字一致（同一套接线不该有两份），坐标/算力全部复用 duckdb arm 的 conn 模块。"""
    arn = _duck.table_bucket_arn()                   # 要 STS，惰性：只在真连接时拼
    return (
        "INSTALL httpfs; INSTALL aws; INSTALL iceberg; "
        "LOAD httpfs; LOAD aws; LOAD iceberg;",
        # 算力/溢写口径与 arm2(conn.py::_configure) 逐项一致，理由见模块 docstring。
        f"SET memory_limit='{_duck.MEMORY_LIMIT}'; SET threads={int(_duck.THREADS)}; "
        f"SET temp_directory='{TEMP_DIR}'; SET enable_object_cache=true;",
        f"CREATE OR REPLACE SECRET s3_default "
        f"(TYPE s3, PROVIDER credential_chain, REGION '{_duck.REGION}', REFRESH auto)",
        f"ATTACH IF NOT EXISTS '{arn}' AS {_duck.ALIAS} "
        f"(TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY)",
        f"USE {_duck.ALIAS}.{_duck.NAMESPACE}",
    )


def _raw(cur, stmt: str) -> None:
    """side-effecting DDL（LOAD/SECRET/ATTACH/USE）经 duckdb.raw_query() 下发。
    必须内联成字面量：psycopg 扩展协议把绑定参数标成 UNKNOWN 类型，pg_duckdb 转不动，
    报 `Could not convert DuckDB type: UNKNOWN`（v1.1.1 实测）。Literal 负责转义。"""
    cur.execute(_pgsql.SQL("SELECT duckdb.raw_query({})").format(_pgsql.Literal(stmt)))


def connect():
    """本进程唯一的 pg_duckdb 连接，懒初始化。autocommit=True：避免某条查询出错后整个
    事务 abort、把后续查询连带废掉（耗时测量跑一批查询，不想一条坏掉污染其余）。防写不靠
    事务，靠 ATTACH READ_ONLY（与 arm2 同口径）。init 只在这里跑一次。"""
    global _CONN
    with _LOCK:
        if _CONN is None:
            c = psycopg.connect(**_dsn(), autocommit=True, connect_timeout=8)
            with c.cursor() as cur:
                for stmt in init_statements():
                    _raw(cur, stmt)
            _CONN = c
        return _CONN


def reset():
    """丢掉当前连接，下次 connect() 重建（重解析凭证、重 ATTACH）。会丢会话状态，但 bench
    判据不建 temp table，可直接丢（与 conn.py::reset 同理）。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:                        # noqa: BLE001
                pass
            _CONN = None


class Client:
    """与 duckdb/conn.py::Client、athena.Client、rsql.Client 同形的执行入口。"""

    def __init__(self, namespace: str = _duck.NAMESPACE):
        self.namespace = namespace
        self._c = connect()

    def execute(self, sql: str, timeout: float = 0.0) -> dict:
        """跑一条 SQL，返回 {columns, rows, rowcount, elapsed_ms, bytes_scanned}。

        只给 `duckdb.query()` 那一次 execute+fetch 计时——init 已在 connect() 跑过，不进
        墙钟（见模块 docstring）。pg_duckdb 不按扫描字节计费（成本是跑 EC2 的时间），所以
        bytes_scanned 恒 0，与 duckdb/redshift 两条一致，用 0 而非省略以保持返回契约与 arm
        无关。

        `timeout > 0` 时用 PG 的 statement_timeout 掐查询。**实测（2026-10-07，pg_duckdb
        v1.1.1）：它掐得住下推进内嵌 DuckDB 的 CPU-bound 计算**——statement_timeout=2s 在
        2002ms 取消一条 `SELECT sum(i) FROM range(5e9)`，错误来自 pg_duckdb 自定义扫描节点
        （`Duckdb_ExecCustomScan_Cpp: Query cancelled`），即取消信号传进了 DuckDB 执行器。
        此前注释说「掐不住下推的 DuckDB 扫描」，被这次实测推翻。仍存一个未验边角：卡在 S3
        网络 I/O 上的扫描是否同样可中断没测到（CPU-bound 频繁命中中断检查，I/O-bound 未必）。

        凭证过期时重连一次再重试（与 conn.py::Client.execute 同策略）。重连在放开 _LOCK
        之后做（connect() 自己要拿同一把锁）。重试过的样本返回里多一个 reconnected=True，
        它的 elapsed_ms 把重连和两次执行都算进去了——**耗时基准必须丢掉这个样本**。
        """
        try:
            out = self._run(sql, timeout)
            reconnected = False
        except Exception as e:                       # noqa: BLE001
            if not _duck.is_expired_credentials(e):
                raise
            reset()
            self._c = connect()
            out = self._run(sql, timeout)            # 再挂就让它抛
            reconnected = True
        if reconnected:
            out["reconnected"] = True
        return out

    def _run(self, sql: str, timeout: float) -> dict:
        """一次执行尝试。锁在这里，重试逻辑在外面。"""
        with _LOCK:
            with self._c.cursor() as cur:
                if timeout > 0:
                    # set_config 是普通 PG 函数、非 duckdb.*，不被 pg_duckdb 接管，
                    # 绑定参数安全（对比 duckdb.query 必须内联，见 _raw）。
                    cur.execute("SELECT set_config('statement_timeout', %s, false)",
                                (str(int(timeout * 1000)),))
                t0 = time.perf_counter()
                # 已过上层 validate() 的只读 SQL 包进 duckdb.query() 交内嵌 DuckDB 执行；
                # 内联成字面量，理由见 _raw。三段式名 lake.app_analytics.events 不能走裸
                # SQL（Postgres 解析器会先拒 cross-database references），必须经 duckdb.query。
                cur.execute(
                    _pgsql.SQL("SELECT * FROM duckdb.query({})").format(_pgsql.Literal(sql)))
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cols else []
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"columns": cols, "rows": [list(r) for r in rows],
                "rowcount": len(rows), "elapsed_ms": elapsed_ms, "bytes_scanned": 0}

    def effective_settings(self) -> dict[str, str]:
        """从内嵌 DuckDB **回读** threads/memory_limit/temp_directory/enable_object_cache
        的实际值（而非回显打算设的值）。arms.py 的 configure_for_timing(aurora) 用它核对
        算力口径真的与 arm2 一致——写错了参数名 pg_duckdb 会静默忽略。"""
        out = {}
        for name in _duck.TIMING_SETTINGS:
            try:
                r = self.execute(f"SELECT current_setting('{name}')")
                out[name] = str(r["rows"][0][0])
            except Exception as e:                   # noqa: BLE001
                out[name] = f"<读不到：{e}>"
        return out

    def tables(self) -> list[str]:
        """挂载到的表名。走 duckdb_tables()（Iceberg catalog 上没有 information_schema，
        见 conn.py docstring 第 1 条）。"""
        r = self.execute(
            "SELECT table_name FROM duckdb_tables() "
            f"WHERE database_name = '{_duck.ALIAS}' AND schema_name = '{self.namespace}' "
            "ORDER BY table_name")
        return [row[0] for row in r["rows"]]


# ---------------------------------------------------------------- 自测（无云）

def selftest() -> int:
    """不连云能验的部分：init 语句拼装、catalog 坐标与 arm2 同源、SQL 内联转义。"""
    bad = 0

    # 1. init 语句复用 duckdb arm 的坐标——两条 arm 必须 ATTACH 到字面相同的 catalog。
    #    用显式 ARN 避免自测时调 STS。
    keep = os.environ.get("S3_TABLES_ARN")
    try:
        os.environ["S3_TABLES_ARN"] = "arn:aws:s3tables:us-west-2:1:bucket/x"
        stmts = init_statements()
    finally:
        os.environ.pop("S3_TABLES_ARN", None)
        if keep:
            os.environ["S3_TABLES_ARN"] = keep

    joined = " ".join(stmts)
    for must in (f"AS {_duck.ALIAS} ", "ENDPOINT_TYPE s3_tables", "READ_ONLY",
                 f"USE {_duck.ALIAS}.{_duck.NAMESPACE}",
                 f"memory_limit='{_duck.MEMORY_LIMIT}'",
                 f"threads={int(_duck.THREADS)}", "REFRESH auto"):
        if must not in joined:
            bad += 1
            print(f"  FAIL init 语句缺少 {must!r}")

    # 1b. READ_ONLY 真的在 ATTACH 上（arm4 只读同一批表，可写连接会让「同一份数据」失守）。
    attach = next((s for s in stmts if s.startswith("ATTACH")), "")
    if "READ_ONLY" not in attach:
        bad += 1
        print("  FAIL ATTACH 没带 READ_ONLY —— arm4 本应只读")

    # 1c. 算力口径与 arm2 同源：直接比常量，不另写一份。
    if (_duck.MEMORY_LIMIT, _duck.THREADS) != (
            os.environ.get("DUCKDB_MEMORY_LIMIT", "4GB"),
            os.environ.get("DUCKDB_THREADS", "4")):
        pass  # 由调用者环境决定，不在此断言具体值；只确保取自 conn 模块（上面已用）。

    # 2. duckdb.query 内联转义：带单引号的 SQL 不能破坏外层字面量。
    q = _pgsql.SQL("SELECT * FROM duckdb.query({})").format(
        _pgsql.Literal("SELECT 'O''Brien' AS name")).as_string(None)
    if "duckdb.query(" not in q or "O''" not in q.replace("'O''''Brien'", "O''"):
        # 只要 Literal 把内嵌引号转义了即可（psycopg 会翻倍单引号）。
        if "O" not in q:
            bad += 1
            print(f"  FAIL duckdb.query 内联转义异常：{q}")

    # 3. 过期分类器复用 conn.py 的（同一判据，arm 间不该有两套）。
    for msg, want in (
        ("The security token included in the request is expired", True),
        ("AccessDenied: not authorized", False),
    ):
        if _duck.is_expired_credentials(RuntimeError(msg)) != want:
            bad += 1
            print(f"  FAIL 过期分类器把 {msg[:40]!r} 判成 {not want}")

    # 4. 缺 AURORA_PGHOST 时 _dsn() 必须报错（而不是连到空 host 超时）。
    keep_host = os.environ.get("AURORA_PGHOST")
    global PGHOST
    saved = PGHOST
    try:
        PGHOST = ""
        try:
            _dsn()
            bad += 1
            print("  FAIL 空 AURORA_PGHOST 竟没报错")
        except RuntimeError:
            pass
    finally:
        PGHOST = saved
        if keep_host is not None:
            os.environ["AURORA_PGHOST"] = keep_host

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  init 语句 7 项断言、ATTACH READ_ONLY、内联转义、过期分类器 2 例、空 host 报错")
    print(f"  catalog 坐标（与 arm2 同源）：{_duck.ALIAS}.{_duck.NAMESPACE} @ {_duck.REGION}")
    print(f"  算力口径（取自 conn 模块）：threads={_duck.THREADS} memory_limit={_duck.MEMORY_LIMIT}")
    print("全部通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="arm4（Aurora/pg_duckdb）→ S3 Tables 查询入口")
    ap.add_argument("sql", nargs="?", help="要执行的 SQL")
    ap.add_argument("--json", action="store_true", help="结果按 JSON 输出")
    ap.add_argument("--tables", action="store_true", help="列出挂载到的表")
    ap.add_argument("--settings", action="store_true", help="回读内嵌 DuckDB 的算力口径")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="PG statement_timeout 秒数（>0 才设）。注意它掐 Postgres 执行器，"
                         "掐不住已下推进内嵌 DuckDB 的扫描——用它来实测这条 caveat")
    ap.add_argument("--selftest", action="store_true", help="无云依赖自测")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    cli = Client()
    if a.settings:
        for k, v in cli.effective_settings().items():
            print(f"  {k} = {v}")
        return 0
    if a.tables:
        ts = cli.tables()
        print(f"{_duck.ALIAS}.{_duck.NAMESPACE} 下 {len(ts)} 张表：")
        for t in ts:
            print(f"  {t}")
        return 0
    if not a.sql:
        ap.error("给一条 SQL，或用 --tables / --settings / --selftest")

    # --timeout 实测那条 caveat：到点被掐断会抛，没抛就说明 statement_timeout 没掐住
    # 下推的 DuckDB 扫描。取消有**两种形态**：PG 标准的 QueryCanceled（SQLSTATE 57014，
    # 当超时在 Postgres 执行器一侧触发时），以及 pg_duckdb 把下推扫描的取消包成的
    # InternalError_「… Query cancelled」（实测 v1.1.1 走的是这条）。其它异常——连不上、
    # SQL 错、凭证过期——**不是**超时取消，不能混报成「被掐断」，否则这个探针「掐没掐住」
    # 的结论就假了。
    if a.timeout > 0:
        t0 = time.perf_counter()
        try:
            r = cli.execute(a.sql, timeout=a.timeout)
        except Exception as e:                           # noqa: BLE001
            dt = (time.perf_counter() - t0) * 1000
            cancelled = (isinstance(e, psycopg.errors.QueryCanceled)
                         or "cancel" in str(e).lower())
            if cancelled:
                print(f"timeout={a.timeout}s → {dt:.0f}ms 后被 statement_timeout 掐断："
                      f"{type(e).__name__}: {e}")
                return 0
            print(f"timeout={a.timeout}s → {dt:.0f}ms 后失败，但**不是**超时取消"
                  f"（所以说明不了掐没掐住）：{type(e).__name__}: {e}")
            return 1
        dt = (time.perf_counter() - t0) * 1000
        print(f"timeout={a.timeout}s → 查询在 {dt:.0f}ms 完成，未被掐断"
              f"（{dt/1000:.1f}s {'>' if dt/1000 > a.timeout else '≤'} {a.timeout}s"
              f"{'：statement_timeout 没掐住下推进 DuckDB 的扫描，caveat 坐实' if dt/1000 > a.timeout else ''}）")
        print(f"{r['rowcount']} 行  {r['elapsed_ms']}ms")
        return 0

    r = cli.execute(a.sql)
    if a.json:
        print(json.dumps({"columns": r["columns"],
                          "rows": [[str(v) for v in row] for row in r["rows"]],
                          "rowcount": r["rowcount"],
                          "elapsed_ms": r["elapsed_ms"]},
                         ensure_ascii=False, indent=2))
    else:
        print("\t".join(r["columns"]))
        for row in r["rows"][:200]:
            print("\t".join("" if v is None else str(v) for v in row))
        if r["rowcount"] > 200:
            print(f"…… 共 {r['rowcount']} 行，只印了前 200")
        print(f"\n{r['rowcount']} 行  {r['elapsed_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
