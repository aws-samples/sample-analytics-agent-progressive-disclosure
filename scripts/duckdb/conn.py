#!/usr/bin/env python3
"""DuckDB 客户端 —— 第三条 arm，读的是 Athena arm 那**同一批** Iceberg 表。

对齐 `scripts/lakehouse/athena.py` 和 `scripts/redshift/rsql.py` 的接口
（`Client.execute(sql) -> {columns, rows, rowcount, elapsed_ms}`），三个 arm
的上层判据因此可以共用一套代码，差异只在这一层里。

## 为什么这个 arm 不需要自己的存储

DuckDB 通过 `ATTACH ... (TYPE iceberg, ENDPOINT_TYPE s3_tables)` 直接读
S3 Tables 的 Iceberg 表——和 Athena 读的是同一份物理表、同一批 Parquet 数据文件。
所以三个 arm 里只有 Redshift 需要额外物化一份 Parquet（它 `COPY` 不了 Iceberg）。
「Redshift 多一次物化」本身是个对比结论，不是我们偷懒，见 `架构图.md`。
那次物化的代价在 `timing.py` 的 `prune-1day` 上量得到：Athena 与 DuckDB 走 Iceberg
清单剪枝，Redshift 那份是 `COPY` 进去的普通表，只能靠排序键和区块统计。

## 七个实测踩出来的坑

**1. `information_schema` 在挂载的 catalog 上不存在。** 挂进来的是 Iceberg REST
catalog，不是 DuckDB 原生库，`lake.information_schema.tables` 直接报
`CatalogException: schema "information_schema" does not exist`。列名和表名要走
`duckdb_tables()` / `duckdb_schemas()` 这两个表函数，或者 `DESCRIBE`。

**2. `count(*)` 走 Iceberg metadata，扫 0 字节。** 拿它量剪枝效率会得到「零字节」
这种无意义的数——行数写在 manifest 里，引擎根本不读数据文件。要量必须用真列聚合
（`count(distinct <列>)` 或 `sum(<列>)`）。这一条 Athena 侧同样成立。

**3. 一个 connection 只有一个 active result set。** 多线程各自 `execute` + `fetch`
会互相偷结果集，症状是 `count(*)` 静默返回 0 行——不报错。所以 `execute()` 全程
持 `_LOCK`。**不能改成每线程一个 `cursor()`**：那样每个线程会拿到自己的 temp table
命名空间，跨调用的会话状态就没了。

**4. 容器里可能没有可解析的 HOME，extension 装不上。** `SET home_directory` 必须
显式给，不然 `INSTALL httpfs` 在 Fargate / SSM 上报找不到 extension 目录。

**5. secret 的 REGION 决定 S3 流量往哪签名。** S3 Tables 的托管存储在表桶所在区，
secret 的 REGION 写错会拿到 403 而不是「区域不对」这种能看懂的错。

**6. 溢写目录要显式给。** 8000 万行上的大排序和 hash join 会超内存额度，没有
`temp_directory` 时 DuckDB 直接 OOM 而不是溢写。Fargate 上还得把
`ephemeralStorage` 开大，默认 20GB 不保险。

**7. 凭证默认只解析一次，跑得比凭证寿命长就会中途挂掉。** `CREATE SECRET` 那一刻
解析 credential chain，之后不再刷新，于是长跑到某一步报
`The security token included in the request is expired`。栽过一次：35 张表的
`correctness.py --arms-only` 挂在第 33 张，**前 32 张已经全绿**——它不是数据分歧，
是这条 arm 的凭证到期了。另两条 arm 走 boto3，自动续期，所以这是**arm 之间的差异**
而不是缺陷，该记进「失败恢复成本」那一栏。

这里两层都做了，因为它们挡的不是同一件事：

- `REFRESH auto` 让 secret 到期时重跑 credential chain。加了这个参数就够覆盖
  catalog 和数据面两侧——实测 `ATTACH ... (SECRET <名>)` 是真选项（给个不存在的名字
  报 `No secret by the name of`，而不认识的选项报 `Unhandled options found`），
  说明 S3 Tables 的挂载走的是同一个 secret 管理器。**但这个参数的值不校验**，
  所以必须回读，见 `_assert_refresh()`。
- `Client.execute` 认出凭证过期后重连一次再重试。这一层不依赖某个 DuckDB 版本的
  refresh 行为，而且覆盖 `ATTACH` 本身已经持有过期句柄的情形。「refresh 到期那一刻
  是否真的触发」这件事要等一个比凭证寿命更长的时间才能证，所以不单押它。

用法：

    python3 scripts/duckdb/conn.py "SELECT count(*) FROM orders"
    python3 scripts/duckdb/conn.py --json "SELECT * FROM users LIMIT 3"
    python3 scripts/duckdb/conn.py --tables            # 列出挂载到的表
    python3 scripts/duckdb/conn.py --selftest          # 无云依赖自测

环境变量（名字与 Athena arm 共用，故意的：同一批表不该有两套配置）：

    AWS_REGION            默认 us-west-2
    S3_TABLE_BUCKET       默认 analytics-agent-tables
    ICEBERG_NAMESPACE     默认 app_analytics
    S3_TABLES_ARN         直接给全 ARN，给了就不再拼
    DUCKDB_MEMORY_LIMIT   默认 4GB（本机与 Fargate 必须同值，见下）
    DUCKDB_TEMP_DIR       默认 <系统临时目录>/duckdb-spill
    DUCKDB_THREADS        默认 4，**已钉住**（见下）

## 算力为什么钉在 4 线程 / 4GB，而不是「按机器来」

DuckDB 默认按核数定线程，于是同一份代码在笔记本和 Fargate 上不是同一个口径，
而输出里看不出来——这正是最坏的一类不可比：数字有了，前提没了。所以默认值就钉死，
不再依赖调用者记得设。

选 4 / 4GB 的理由是**两边都成立**：Fargate 任务是 4 vCPU / 16GB，本机核数更多但
内存额度更容易踩顶。取两边都满足的那一组，本机和云上才是同一把尺。任务定义里
（`scripts/duckdb/fargate.py`）把这两个值显式写进环境变量，跟这里的默认值一致，
所以改了一处不会悄悄只改半边。

内存额度**不跟着 Fargate 的 16GB 放大**：额度决定的是「什么时候溢写」，而溢写与否
是耗时的主要来源之一。两边额度不同，等于在比两种执行计划。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from pathlib import Path

REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_BUCKET = os.environ.get("S3_TABLE_BUCKET", "analytics-agent-tables")
NAMESPACE = os.environ.get("ICEBERG_NAMESPACE", "app_analytics")
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "4GB")
TEMP_DIR = os.environ.get("DUCKDB_TEMP_DIR") or str(
    Path(tempfile.gettempdir()) / "duckdb-spill")
# 默认就钉住，不留「按核数」这个选项：理由见 docstring「算力为什么钉在 4 线程」。
# 想跑满本机核数得显式设 DUCKDB_THREADS，那时对比结论作废，是调用者自己的选择。
THREADS = os.environ.get("DUCKDB_THREADS", "4")

# 挂载别名。写死是刻意的：判据 SQL 里出现的库名必须只有一个可能，
# 环境变量能改的话，「表找不到」和「挂到了别的 catalog」就分不出来了。
ALIAS = "lake"

# 见模块 docstring 第 3 条。模块级而非连接级：这个进程只应该有一个连接。
_LOCK = threading.Lock()
_CONN = None


def table_bucket_arn() -> str:
    """表桶 ARN。给了 `S3_TABLES_ARN` 就用它，否则用当前身份的账号拼。

    拼而不是硬编码账号，是为了让这份代码在别的账号里也能跑；但 ARN 一旦拼错，
    `ATTACH` 报的是 `Forbidden` 而不是「桶不存在」，所以出错时把拼出来的 ARN
    印出来（见 `attach()`）。
    """
    if os.environ.get("S3_TABLES_ARN"):
        return os.environ["S3_TABLES_ARN"]
    import boto3

    acct = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return f"arn:aws:s3tables:{REGION}:{acct}:bucket/{TABLE_BUCKET}"


def _configure(c) -> None:
    """连接级设置。**在 ATTACH 之前**执行——extension 要先装上。"""
    # 见 docstring 第 4 条：容器里没有可解析的 HOME 时 INSTALL 会失败
    c.execute(f"SET home_directory='{TEMP_DIR}'")
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    c.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    # 见 docstring 第 6 条：不给溢写目录时超额度是 OOM 而不是溢写
    c.execute(f"SET temp_directory='{TEMP_DIR}'")
    if THREADS:
        # 基准测试要求「每次跑算力一样」。不钉住的话 DuckDB 按机器核数定，
        # 笔记本和 Fargate 上就不是同一个口径，而输出里看不出来。
        # 默认值已经是 4，走到 else 只可能是有人显式设了空串。
        c.execute(f"SET threads={int(THREADS)}")
    c.execute("SET enable_object_cache=true")
    c.execute("INSTALL httpfs; LOAD httpfs;")
    c.execute("INSTALL aws;    LOAD aws;")
    c.execute("INSTALL iceberg; LOAD iceberg;")
    # credential_chain：本地读 profile / EC2 读实例角色 / Fargate 读任务角色，
    # 三种环境同一份代码。REGION 见 docstring 第 5 条。REFRESH 见第 7 条。
    c.execute(
        "CREATE OR REPLACE SECRET s3_default "
        f"(TYPE s3, PROVIDER credential_chain, REGION '{REGION}', REFRESH auto)"
    )
    _assert_refresh(c)


#: `REFRESH auto` 生效后会在 secret 里留下的那一段。见 docstring 第 7 条。
REFRESH_MARK = "'refresh': auto"


def _assert_refresh(c) -> None:
    """回读 secret，确认 `REFRESH auto` 真的登记上了。

    这一步不是防御性代码，是这个参数**必须**回读的直接后果：值写错时
    DuckDB 不报错。实测 1.5.5，`REFRESH banana` 绑定阶段一声不吭
    （对比：参数**名**写错会报 `Binder Error: Unknown parameter`），
    而 `secret_string` 里连 `refresh_info` 都不出现——凭证照旧固定，
    只是从此没人知道。所以判据是「`refresh_info` 在不在」，不是「建 secret 有没有报错」。
    """
    row = c.execute(
        "SELECT secret_string FROM duckdb_secrets() WHERE name = 's3_default'"
    ).fetchone()
    if not row:
        raise RuntimeError("secret s3_default 没建出来")
    if REFRESH_MARK not in row[0]:
        raise RuntimeError(
            "secret 建出来了但 REFRESH 没登记上——凭证会固定在此刻，长跑到中途会以 "
            "ExpiredToken 挂掉。回读到的是：\n  "
            + row[0].replace(";", ";\n  ")
        )


#: 影响耗时、因此必须记进 trace 的设置。`threads` 和 `memory_limit` 决定并行度和
#: 是否溢写，这两项不同的两次跑不可比；`temp_directory` 空则溢写不了，会变成 OOM。
TIMING_SETTINGS = ("threads", "memory_limit", "temp_directory", "enable_object_cache")


def effective_settings(c=None) -> dict[str, str]:
    """从引擎**回读**这几项的实际值，而不是回显我们打算设的值。

    这两者不等价，而且不等价时不报错：`SET` 一个拼错的参数名在某些版本上被静默忽略，
    `memory_limit` 会被规整成别的写法（`4GB` → `3.7 GiB`）。基准报告里该出现的是
    引擎认的那个数——写「我设了 4GB」而实际是别的值，等于给出一个查不出来的错前提。
    """
    c = c or connect()          # connect() 自己进出锁，所以这一步之后锁是空闲的
    out = {}
    with _LOCK:                 # 回读也是 execute+fetch，同样受第 3 条坑约束
        for name in TIMING_SETTINGS:
            try:
                out[name] = str(c.execute(
                    f"SELECT current_setting('{name}')").fetchone()[0])
            except Exception as e:                  # noqa: BLE001
                out[name] = f"<读不到：{e}>"
    return out


def attach(c, arn: str | None = None) -> str:
    """把 S3 Tables catalog 以 READ_ONLY 挂到 `ALIAS` 下。返回用到的 ARN。

    READ_ONLY 不是防御性写法而是判据的一部分：这个 arm 只负责**读**同一批表。
    能写的连接会让「三个 arm 读的是同一份数据」这句话失去保证——某次跑偏的
    基准脚本可以改掉数据，而下一个 arm 完全看不出来。
    """
    arn = arn or table_bucket_arn()
    try:
        c.execute(f"ATTACH '{arn}' AS {ALIAS} (TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY)")
    except Exception as e:
        raise RuntimeError(
            f"挂载 S3 Tables 失败。用的 ARN 是 {arn}\n"
            f"  区域 {REGION} / 表桶 {TABLE_BUCKET} / 命名空间 {NAMESPACE}\n"
            f"  Forbidden 通常是 ARN 里的账号或区域不对，不是权限不够；\n"
            f"  真的权限不够时报的是 AccessDenied 并且会带上 principal。\n"
            f"  原始错误：{e}"
        ) from e
    return arn


def connect():
    """本进程唯一的 DuckDB 连接，懒初始化。内存库，不落地任何东西。"""
    global _CONN
    with _LOCK:
        if _CONN is None:
            import duckdb

            c = duckdb.connect()          # 内存库
            _configure(c)
            attach(c)
            c.execute(f"USE {ALIAS}.{NAMESPACE}")
            _CONN = c
        return _CONN


def reset():
    """丢掉当前连接，下次 `connect()` 重建（重新解析凭证、重新 ATTACH）。

    **会丢掉会话状态**（temp table、`USE` 之外的设置）。本仓库的判据脚本都不用
    temp table，所以这里可以直接丢；哪天有人在这条 arm 上建了 temp table，
    重连之后它就不在了，而报错会是「表不存在」这种指不到根因的形状。
    """
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:                       # noqa: BLE001
                pass                                # 已经坏掉的连接关不上也无所谓
            _CONN = None


#: 见 docstring 第 7 条。**刻意窄**：只认凭证过期这一类，不认 AccessDenied
#: （那是权限配错，重连一万次也一样）也不认 Forbidden（ARN 拼错）。放宽会把
#: 「配置错了」变成「重试到超时」，根因从此看不见。
_EXPIRED_MARKS = (
    "security token included in the request is expired",
    "expiredtoken",
    "token has expired",
    "the provided token has expired",
)


def is_expired_credentials(err: BaseException) -> bool:
    """这个异常是不是「凭证过期」。"""
    return any(m in str(err).lower() for m in _EXPIRED_MARKS)


class Client:
    """与 `athena.Client` / `rsql.Client` 同形的执行入口。"""

    def __init__(self, namespace: str = NAMESPACE):
        self.namespace = namespace
        self._c = connect()

    def execute(self, sql: str, timeout: float = 0.0) -> dict:
        """跑一条 SQL，返回 {columns, rows, rowcount, elapsed_ms}。

        全程持 `_LOCK`，理由见 docstring 第 3 条：execute 和 fetch 之间被别的线程
        插进来的话，拿到的是对方的结果集，而且不报错。

        `timeout > 0` 时用看门狗线程做语句超时（秒）。DuckDB **没有**会话级
        `statement_timeout`——`duckdb_settings()` 里一条相关的都没有——但它有
        `interrupt()`，从另一个线程调就能把正在跑的查询掐掉。实测 1.5.5 上一条
        跑不完的查询 2.0s 被掐停，抛 `InterruptException: INTERRUPT Error:
        Interrupted!`。所以这不是「这条 arm 做不到超时」，而是「要自己在客户端补一个
        看门狗」：Athena 是 `StopQueryExecution`、Redshift 是 Data API 的轮询超时，
        三条 arm 都有，实现成本不同而已。

        `interrupt()` 不需要拿 `_LOCK`——它作用在连接上，不碰结果集。

        凭证过期时**重连一次再重试**（见 docstring 第 7 条）。重连必须在放开
        `_LOCK` 之后做：`connect()` 自己要拿同一把锁，在锁里重连是死锁。
        重试过的那一条会在返回值里多一个 `reconnected: True`——这不是装饰，
        它的 `elapsed_ms` 把重连和两次执行都算进去了，**耗时基准必须丢掉这个样本**。
        """
        t0 = time.perf_counter()
        try:
            cols, rows = self._run(sql, timeout)
            reconnected = False
        except Exception as e:                       # noqa: BLE001
            if not is_expired_credentials(e):
                raise
            reset()
            self._c = connect()
            cols, rows = self._run(sql, timeout)     # 再挂就让它抛
            reconnected = True
        out = {
            "columns": cols,
            "rows": rows,
            "rowcount": len(rows),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        if reconnected:
            out["reconnected"] = True
        return out

    def _run(self, sql: str, timeout: float) -> tuple[list, list]:
        """一次执行尝试。锁和看门狗都在这里，重试逻辑在外面。"""
        import duckdb                        # 与 connect() 同口径：惰性导入

        with _LOCK:
            timer = None
            if timeout > 0:
                timer = threading.Timer(timeout, self._c.interrupt)
                timer.daemon = True          # 别让它拖住进程退出
                timer.start()
            try:
                cur = self._c.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cols else []
            except duckdb.InterruptException as e:
                # 换成 TimeoutError：调用方要区分「查询太慢」和「SQL 写错了」，
                # InterruptException 这个名字在上层看不出是超时。
                raise TimeoutError(
                    f"DuckDB 查询超过 {timeout:.1f}s，已由看门狗 interrupt() 掐停") from e
            finally:
                if timer is not None:
                    timer.cancel()           # 正常跑完就别再去掐下一条了
        return cols, rows

    def tables(self) -> list[str]:
        """挂载到的表名。走 `duckdb_tables()` —— 见 docstring 第 1 条。"""
        r = self.execute(
            "SELECT table_name FROM duckdb_tables() "
            f"WHERE database_name = '{ALIAS}' AND schema_name = '{self.namespace}' "
            "ORDER BY table_name"
        )
        return [row[0] for row in r["rows"]]

    def columns(self, table: str) -> list[tuple[str, str]]:
        """[(列名, DuckDB 类型)]，**按表内列序**。列序要参与对账，所以不排序。"""
        r = self.execute(f'DESCRIBE SELECT * FROM "{table}" LIMIT 0')
        i_name = r["columns"].index("column_name")
        i_type = r["columns"].index("column_type")
        return [(row[i_name], row[i_type]) for row in r["rows"]]


# ---------------------------------------------------------------- 自测（无云）

def selftest() -> int:
    """不连云能验的部分：配置项拼装、ARN 拼装、READ_ONLY 真的只读。"""
    bad = 0

    # 1. ARN 拼装：给了 S3_TABLES_ARN 就原样用
    keep = os.environ.get("S3_TABLES_ARN")
    try:
        os.environ["S3_TABLES_ARN"] = "arn:aws:s3tables:eu-west-1:1:bucket/x"
        got = table_bucket_arn()
        if got != "arn:aws:s3tables:eu-west-1:1:bucket/x":
            bad += 1
            print(f"  FAIL 显式 ARN 没被原样使用：{got}")
    finally:
        os.environ.pop("S3_TABLES_ARN", None)
        if keep:
            os.environ["S3_TABLES_ARN"] = keep

    try:
        import duckdb
    except ImportError:
        print("  跳过引擎侧自测（没装 duckdb）")
        print("全部通过 ✅")
        return 0

    # 2. 配置项真的生效。逐项回读，而不是「执行没报错就算过」——
    #    SET 一个不存在的参数在某些版本上是静默忽略的。
    c = duckdb.connect()
    _configure(c)
    for name, want in (("memory_limit", None), ("temp_directory", TEMP_DIR)):
        got = c.execute(f"SELECT current_setting('{name}')").fetchone()[0]
        if want is not None and str(got) != want:
            bad += 1
            print(f"  FAIL {name} 回读 {got!r}，期望 {want!r}")
        if not str(got):
            bad += 1
            print(f"  FAIL {name} 回读为空")

    # 2b. 线程数**真的钉住了**。这一条单独断言，因为它是耗时可比性的前提：
    #     没钉住时 DuckDB 按核数定，笔记本 10 线程、Fargate 4 线程，两个数放一张表里
    #     看起来是「DuckDB 在云上变慢了」，其实是算力不一样。
    eff = effective_settings(c)
    if THREADS and eff["threads"] != str(int(THREADS)):
        bad += 1
        print(f"  FAIL threads 回读 {eff['threads']!r}，期望 {int(THREADS)}")
    elif not THREADS:
        bad += 1
        print("  FAIL DUCKDB_THREADS 被设成了空串，算力没钉住——耗时对比不成立")

    # 3. 三个 extension 都真的 LOAD 了
    loaded = {r[0] for r in c.execute(
        "SELECT extension_name FROM duckdb_extensions() WHERE loaded").fetchall()}
    for ext in ("httpfs", "aws", "iceberg"):
        if ext not in loaded:
            bad += 1
            print(f"  FAIL extension {ext} 没 LOAD（已加载：{sorted(loaded)}）")

    # 4. secret 建出来了，且 REGION 是我们给的那个
    secs = c.execute("SELECT name FROM duckdb_secrets()").fetchall()
    if not any(s[0] == "s3_default" for s in secs):
        bad += 1
        print(f"  FAIL secret s3_default 不存在（有：{secs}）")

    # 4b. REFRESH 真的登记上了（凭证不会固定在建 secret 那一刻）。
    #     `_configure` 里已经 assert 过一次，这里再断言一遍是因为**这一条要能单独失败**：
    #     哪天有人把 `_assert_refresh` 的调用删掉，自测得替他红。
    try:
        _assert_refresh(c)
    except RuntimeError as e:
        bad += 1
        print(f"  FAIL {e}")

    # 4c. 回读判据本身有区分力。`REFRESH banana` 是实测过的静默失效形态：
    #     绑定阶段不报错，`refresh_info` 不出现。判据必须对它红，否则它只是
    #     一句永远为真的话。
    c.execute("CREATE OR REPLACE SECRET selftest_norefresh "
              f"(TYPE s3, PROVIDER credential_chain, REGION '{REGION}', REFRESH banana)")
    s = c.execute("SELECT secret_string FROM duckdb_secrets() "
                  "WHERE name = 'selftest_norefresh'").fetchone()[0]
    if REFRESH_MARK in s:
        bad += 1
        print("  FAIL 拼错的 REFRESH 值竟然也登记上了——回读判据认不出静默失效")
    c.execute("DROP SECRET selftest_norefresh")

    # 4d. 过期分类器**两个方向**都要对。只认凭证过期，不认权限/ARN 配错——
    #     放宽了的话「配置错了」会变成「重试到超时」，根因从此看不见。
    for msg, want in (
        ('{"message":"The security token included in the request is expired"}', True),
        ("ExpiredToken: token is no good", True),
        ("The provided token has expired.", True),
        ("AccessDenied: not authorized to perform s3:GetObject", False),
        ("Forbidden", False),
        ("IO Error: Connection reset by peer", False),
        ("Binder Error: Referenced column \"expired\" not found", False),
    ):
        if is_expired_credentials(RuntimeError(msg)) != want:
            bad += 1
            print(f"  FAIL 过期分类器把 {msg[:50]!r} 判成 {not want}")

    # 4e. 重试的控制流。用桩替掉连接和 `connect()`——要验的是「重试几次、什么时候
    #     不重试」，跟云无关。三条都必须成立，少一条这层就是坏的：过期要重试一次、
    #     连续过期要抛（不能无限重试）、非凭证错误一次都不能重试。
    EXPIRED = '{"message":"The security token included in the request is expired"}'

    class _Stub:
        description = [("a",)]

        def __init__(self, fail_times, err=EXPIRED):
            self.fail_times, self.err, self.calls = fail_times, err, 0

        def execute(self, sql):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError(self.err)
            return self

        def fetchall(self):
            return [(1,)]

        def interrupt(self):
            pass

    # `execute` 里的 `reset` / `connect` 是调用时从模块全局查的，所以替 globals 就够
    mod = globals()
    keep = (mod["connect"], mod["reset"])
    cli = Client.__new__(Client)           # 绕开 __init__，它会去连云
    cli.namespace = NAMESPACE
    try:
        mod["reset"] = lambda: None
        for label, stub, want_calls, want_raise in (
            ("过期一次→重连重试", _Stub(1), 2, False),
            ("连续过期→抛，不无限重试", _Stub(9), 2, True),
            ("AccessDenied→一次都不重试", _Stub(9, "AccessDenied: no"), 1, True),
        ):
            cli._c = stub
            mod["connect"] = lambda s=stub: s
            raised = False
            try:
                r = cli.execute("SELECT 1")
            except Exception:                        # noqa: BLE001
                raised = True
            else:
                if want_calls == 2 and not r.get("reconnected"):
                    bad += 1
                    print("  FAIL 重连过了，但返回值没标 reconnected——"
                          "耗时基准会把这个样本当成正常样本")
            if raised != want_raise:
                bad += 1
                print(f"  FAIL {label}：抛={raised}，期望抛={want_raise}")
            if stub.calls != want_calls:
                bad += 1
                print(f"  FAIL {label}：执行了 {stub.calls} 次，期望 {want_calls}")
    finally:
        mod["connect"], mod["reset"] = keep

    # 5. READ_ONLY 语义：挂一个本地库验证写会被拒。用本地文件而不是云上表桶，
    #    这样这条断言在没有云凭证时照样跑得到——它要证的是 ATTACH 的 READ_ONLY
    #    参数真的会挡写，与挂的是什么后端无关。
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ro.db"
        w = duckdb.connect(str(p))
        w.execute("CREATE TABLE t(a int); INSERT INTO t VALUES (1)")
        w.close()
        c.execute(f"ATTACH '{p}' AS ro (READ_ONLY)")
        n = c.execute("SELECT count(*) FROM ro.t").fetchone()[0]
        if n != 1:
            bad += 1
            print(f"  FAIL READ_ONLY 挂载读不到数据：count={n}")
        try:
            c.execute("INSERT INTO ro.t VALUES (2)")
            bad += 1
            print("  FAIL READ_ONLY 挂载竟然允许写入")
        except Exception:
            pass
        c.execute("DETACH ro")

    c.close()

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  ARN 拼装 1 例、配置回读 3 项（含线程钉住）、extension 3 个、"
          f"secret 1 个 + REFRESH 回读 2 项、过期分类器 7 例、重试控制流 3 例、"
          f"READ_ONLY 读通写拒 2 项")
    print("  引擎回读的实际算力口径："
          + " · ".join(f"{k}={eff[k]}" for k in TIMING_SETTINGS))
    print("全部通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DuckDB → S3 Tables 查询入口")
    ap.add_argument("sql", nargs="?", help="要执行的 SQL")
    ap.add_argument("--json", action="store_true", help="结果按 JSON 输出")
    ap.add_argument("--tables", action="store_true", help="列出挂载到的表")
    ap.add_argument("--selftest", action="store_true", help="无云依赖自测")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    cli = Client()
    if a.tables:
        ts = cli.tables()
        print(f"{ALIAS}.{NAMESPACE} 下 {len(ts)} 张表：")
        for t in ts:
            print(f"  {t}")
        return 0
    if not a.sql:
        ap.error("给一条 SQL，或用 --tables / --selftest")

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
