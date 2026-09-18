"""只读数据库访问层。

所有 NL→SQL 产生的查询都经 run_query 执行，强制：单条语句、只读、超时、行数上限。
这是 agent 的 SQL 安全边界。

## 四种后端：三条对比 arm + 一条 legacy

`DB_BACKEND` 环境变量选择。前三个是**架构对比的三条 arm**，谁都不取代谁：

- `athena`（默认）—— Athena 查 S3 Tables（Iceberg）。**不需要 VPC、不需要连接池、
  不需要在容器里存密码**，是 HTTPS + IAM 的 AWS API 调用。代价是异步（提交→轮询→取结果），
  由 `scripts/lakehouse/athena.py` 封装成同步。
- `duckdb` —— DuckDB 读**同一批** Iceberg 表（`ATTACH ... TYPE iceberg`）。存储与
  `athena` 完全共享，连物理 Parquet 文件都是同一批，所以这两条 arm 的差异只能来自引擎。
  进程内执行，没有网络往返，代价是算力就是这台机器的算力——因此线程数和内存额度必须
  钉住（`scripts/duckdb/conn.py` 已默认钉在 4 线程 / 4GB），否则「笔记本上更快」
  会被误读成「DuckDB 更快」。
- `redshift` —— Redshift Serverless，走 Data API（HTTPS + IAM，同样不进 VPC）。
  三条 arm 里唯一需要**额外物化**一份数据的：`COPY` 读不了 Iceberg，所以它读的是
  `s3://analytics-agent-raw/parquet/` 那一份拷贝。这次拷贝本身是对比结论之一。
  （这一支 2026-08 曾整体删除、判为退役；现在回来的身份不同——它是被比较的一方，
  不是主线数据层。）
- `postgres` —— psycopg 直连。**LEGACY，不是对比 arm**：只剩本地容器 rig
  （docker-compose.cloud.yml，约 19 万行、无 Glue 目录）。代码保留、不随架构演进，
  测试套件也不覆盖它。见 docs/legacy.md。

只读边界（`validate()`）四个后端共用，与后端无关：单条语句、仅 SELECT/WITH、
禁写关键字。切后端不会削弱这道闸。

## 切 arm 会变的和**必须不变**的

变的只有两件：连到哪、SQL 用什么方言。方言差异由 `DIALECT` 暴露给 `agent.py`，
拼成提示词末尾的一小段附录。

不变的是知识树（`knowledge/**`）、指标定义（`metrics_def.py`）、题目和判据。这一条
是对比成立的前提：语义层动了，量到的就是语义层不是引擎。`athena` 那条 arm 的附录
**是空串**，所以它的提示词与加这个机制之前逐字节相同，已有的 L7 基线仍然有效。

## 三条 arm 的列级治理各不相同，`identity` 只反映其中一条

`athena`：`AGENT_ROLE_ARN` + Lake Formation 列级排除。PII 列在结果里**不存在**，
点名查报 `COLUMN_NOT_FOUND`。生效对象是那个最小权限角色。

`redshift`：动态脱敏（DDM）+ 角色级 GRANT，2026-09-03 挂上
（`database/redshift/04_governance.sql`）。PII 列**查得到、值是掩码**：
`users.email` → `***@masked.invalid`，`phone` → `151****0906`，
`user_profiles.birth_date` 只剩出生年。脱敏挂的是 `TO PUBLIC`，不区分谁连进来，
superuser 也一样——实测确认过。
**但两层里只有脱敏这层落到了 agent 身上**：`analytics_agent_ro` 角色的表级授权面
（45 张表，`user_messages` 不在内）已经建好，而这里连库走的是调用者自己的 IAM 身份，
没有切到那个角色，所以 `user_messages` 在这条 arm 上目前仍然查得到。要补的是
「以什么身份连」这一步，跟 athena 侧 `AGENT_ROLE_ARN` 干的是同一件事。

`duckdb`：**引擎内一条治理原语都没有**——没有用户、角色、GRANT、masking policy，
也没有行列级安全。它是嵌入式库，没有权限分离的概念，边界完全由它手里那份凭证决定，
而 IAM 对 S3 Tables 的动作粒度是命名空间和表，没有列这一级。**表级既是下限也是上限**，
要达到列级效果只能靠物理投影（另存一份去掉 PII 列的表），靠策略做不到。

由此有一条结论值得记住：**DuckDB 直读 Iceberg 会绕过 LF 的列级排除。**athena 那条的
列级治理是引擎在查询时执行的，执行点在引擎里；DuckDB 不与 LF 集成，拿到
`s3tables:GetTableData` 之后读的是底层 Parquet，策略在这条路径上没有执行点。所以
「LF 上配了列级排除」不等于「这一列在任何客户端都读不到」。这跟本仓库已有的另一条
同源：挂了 DDM 的表在 Glue 目录里照常出现、列名都在，Glue federated catalog 是
schema 投影不是权限投影。**这一条目前是从架构推的，还没实测**；要坐实就用 athena 的
最小权限角色凭证跑 DuckDB 查 PII 列，看是明文还是被 IAM 拦住。

`backend_info()['identity']` 只在 `athena` 那条 arm 上非空，因为只有它是靠
AssumeRole 换身份实现的。**别把这个字段读成「这条 arm 有没有治理」**：redshift 的
脱敏已经生效而 `identity` 仍是空的。它反映的是「以谁的身份连」，不是「受不受管」。

## 以谁的身份查（AGENT_ROLE_ARN）

`validate()` 管的是"不许写"，管不了"不许看"。**看**的边界在 L4 治理层：
设了 `AGENT_ROLE_ARN` 时，Athena 客户端走 AssumeRole 用那个最小权限角色查数，
`user_messages` 整表查不到、`users.email` / `phone` / `user_profiles.birth_date`
这几列在结果里根本不存在（Lake Formation 列级排除，见
`scripts/lakehouse/governance.py`）。不设＝用进程自己的凭证，本地开发通常是
admin——那时**只有那道只读闸**生效。`backend_info()` 会把生效身份放进
`identity` 字段，这是从外面唯一能看见"治理接上了没有"的地方。

## 换到 Athena 之后，契约上真正变了的三件事

**1. 方言从 Postgres/Redshift 变成 Trino。** 上层不需要改代码，但 agent 写的 SQL 要
改方言（`agent.py` 的提示词、`metrics_def.py` 的指标 SQL、`knowledge/**` 的示例）。
`::` 转型、`date + 7`、`DISTINCT ON`、`numeric(p,s)`、数组下标 `[]` 都不再合法。
反过来 `FILTER (WHERE ...)` 在 Trino 是原生支持的——那是 Redshift 当年必须改写成
`CASE WHEN` 的地方，这次不用。

**2. 多了一个可观测量：扫描字节数。** Athena 按扫描字节计费，所以
`run_query` 的返回里多一个 `bytes_scanned`。它是**成本**的直接度量，
「这个问题花了多少钱」第一次变成可读的数字。Iceberg 的 `count(*)` 走元数据、
这个值会是 0——那不是 bug，是列式 + 清单文件该有的样子。
（UI 目前不显示它；这个字段是加出来的，不改前端也不会坏。）

**3. `NOT NULL` 没了。** Athena 的 Iceberg DDL 不接受列级 `NOT NULL`（实测拒绝），
所以 `information_schema.columns.is_nullable` **恒为 `YES`**，读它等于读一个常量——
任何地方要渲染可空性都得先想清楚这件事，把"引擎不强制"渲染成"这列可以为空"是把
不知道说成了否。原来的非空约束改写进列注释（`NOT NULL（Iceberg 侧不强制）`），
仍然看得见，但它现在是**文档，不是约束**。这是这次迁移里唯一一处真实的能力下降，
写在这里而不是藏起来。

## 两个后端的差异（已知且刻意保留）

- `statement_timeout`：Postgres 侧用 `set_config` 下发到会话；Athena 每条语句
  各自成一次 API 调用，SET 不跨语句生效，所以 Athena 侧的超时靠客户端轮询超时把关
  （并且超时会主动 StopQueryExecution，否则查询继续跑继续扫字节继续计费）。
- 行数上限：两边都在取回后截断并置 `truncated` 标志，契约一致。

## 这里**没有**取表结构的函数，是刻意的

`get_schema()` / `_get_schema_athena()` / `_describe_comments()` 曾经在这个文件里，
2026-08-28 删掉：**全仓库没有一个调用点**，工具层早就重构成 `read_doc` 读
`knowledge/` 卡片了（`backend/README.md` 记着这次重构）。留着的代价不是几十行死代码，
是**误导**——读代码的人会以为 Glue 列注释进得了 agent 上下文，从而误判"改 Glue 元数据
要重跑 L7"。agent 看到的表结构只有一个来源：`knowledge/` 里那些卡片。

删之前把那段代码里两件实测出来的事留在这儿，免得下次有人重新踩：

1. **Athena 的 `information_schema.columns` 只有 10 列、没有 `comment`**（Trino 原生
   有，联邦目录这版没给）。要列注释只能 `DESCRIBE <表>`，而 `DESCRIBE` 的
   ResultSetMetadata 说有 3 列、值却**全塞在第一列里**用制表符分隔，另两列是 NULL，
   尾部还跟着 `# Partition spec:` 段要截断。
2. **类型不要从 `DESCRIBE` 取**：它回的是 Hive 味类型名（`string`、`decimal(12, 2)`
   带空格），而 agent 写的是 Trino DML（`varchar`、`decimal(12,2)`）。给写 Trino 的
   模型看 Hive 类型名正好诱发 `CAST(x AS string)` 这种错。类型走 `information_schema`、
   注释走 `DESCRIBE`，各取其准的那一半。

顺带纠一处旧说法：这些列注释**不是**灌数时从 `schema_manifest.yaml` 落进 Glue 的
（manifest 驱动的是派生层，见 `catalog.py`），它们是**建表时**从
`database/iceberg/01_tables.sql` 的 `COMMENT` 进去的，而那份 DDL 又是 `gen_ddl.py`
从 `database/0[1-8]_*.sql` 的行内注释生成的。
"""
from __future__ import annotations

import os
import re
import sys
import time
import threading
import asyncio
import datetime as _dt
import decimal
import uuid
from pathlib import Path
from typing import Any

PG = {
    "host": os.getenv("PGHOST", "127.0.0.1"),
    "port": int(os.getenv("PGPORT", "5433")),
    "dbname": os.getenv("PGDATABASE", "app_analytics"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}
MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "1000"))
STMT_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "15000"))
# 默认 athena：数据层在 S3 Tables（Iceberg）上。默认指向一个不再维护的本地 Postgres
# 会让人第一步就撞上 `ModuleNotFoundError: No module named 'psycopg'`——而真正的库
# 在云上好着。还需要 v1 本地路径的地方显式设 DB_BACKEND=postgres
# （docker-compose.cloud.yml 已钉）。
BACKEND = os.getenv("DB_BACKEND", "athena").lower()

BACKENDS = ("athena", "duckdb", "redshift", "postgres")
if BACKEND not in BACKENDS:
    # 早炸而不是走到默认分支。拼错 `DB_BACKEND=duckdb ` 之类的值原来会静默落到
    # Athena 分支上，于是「我在测 DuckDB」和「实际在测 Athena」这两件事从输出里
    # 分不出来——对比场景下这是最坏的一种错。
    raise RuntimeError(
        f"DB_BACKEND={BACKEND!r} 不认识，只能是 {'/'.join(BACKENDS)}。"
        f"前三个是对比 arm，postgres 是 legacy 本地路径。")

# 引擎名的**唯一**出处。`/health` 在还没拿到完整 backend_info() 时也要报得出引擎
# （那一步冷启动要 AssumeRole，见 backend_info 的注释），所以它不能只活在那个函数里面。
ENGINE_LABEL = {
    "athena": "Athena + S3 Tables (Iceberg)",
    "duckdb": "DuckDB + S3 Tables (Iceberg)",
    "redshift": "Redshift Serverless",
    "postgres": "PostgreSQL",
}[BACKEND]

# 只有 Athena 按扫描字节计费，所以只有它给得出「这个问题花了多少钱」的直接度量。
# 另两条 arm 这个字段恒为 0：DuckDB 的成本是机器时间，Redshift 是 RPU 秒。
# 用 0 而不是省略，是为了让返回契约与 arm 无关——上层不该为了换 arm 改代码。
HAS_BYTES_SCANNED = BACKEND == "athena"

# 方言附录：拼在 agent 提示词末尾。**athena 是空串**，所以那条 arm 的提示词与引入
# 这个机制之前逐字节相同，已有的 L7 基线不因此作废。见模块 docstring。
DIALECTS = {
    "athena": "",
    "postgres": "",
    "duckdb": (
        "\n\n## 本次的 SQL 方言：DuckDB（覆盖前文说的 Athena / Trino）\n"
        "查询引擎是 DuckDB，不是 Athena。表和数据完全一样，只是方言换了。\n"
        "语法与 Trino 大体一致，下面几处不同，按 DuckDB 的写：\n"
        "- 日期加减写 `d + INTERVAL 7 DAY`，不写 `date_add('day', 7, d)`。\n"
        "- 取子串偏好 `substr`；`format_datetime` 不存在，用 `strftime(ts, '%Y-%m-%d')`。\n"
        "- `approx_distinct` 不存在，用 `approx_count_distinct`。\n"
        "- 数组下标从 1 开始，与 Trino 相同。\n"
    ),
    "redshift": (
        "\n\n## 本次的 SQL 方言：Redshift（覆盖前文说的 Athena / Trino）\n"
        "查询引擎是 Redshift Serverless，不是 Athena，方言是 PostgreSQL 系而**不是 Trino**。\n"
        "下面几处必须按 Redshift 写：\n"
        "- 转型用 `CAST(x AS type)` 或 `x::type` 都可以。\n"
        "- 日期加减写 `d + 7` 或 `dateadd(day, 7, d)`；没有 `date_add('day', ...)`。\n"
        "- 格式化时间用 `TO_CHAR(ts, 'YYYY-MM-DD')`，没有 `format_datetime`。\n"
        "- 没有 `FILTER (WHERE ...)`，改写成 `SUM(CASE WHEN ... THEN 1 ELSE 0 END)`。\n"
        "- 字符串拼接用 `||`，`concat` 只接两个参数。\n"
        "- `approx_distinct` 不存在，用 `APPROXIMATE COUNT(DISTINCT x)`。\n"
    ),
}
DIALECT = DIALECTS[BACKEND]

if BACKEND == "postgres":
    import psycopg
else:
    psycopg = None                                   # 另三条 arm 都不需要它

# 只读闸门。`unload` / `analyze` 是 Trino/Athena 特有的写动作（UNLOAD 往 S3 写文件、
# ANALYZE 写统计），Postgres/Redshift 时代的清单里没有它们。下面的起始关键字检查
# （必须以 select/with 开头）本来就拦得住，这里再拦一遍是防御性的：万一将来有人
# 放宽了起始检查，写操作不会顺着这个缺口漏出去。
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|comment|merge|call|do|set|begin|commit|"
    r"unload|analyze)\b",
    re.IGNORECASE,
)


class SqlError(Exception):
    pass


def _strip(sql: str) -> str:
    # 去掉行/块注释与首尾空白、尾分号
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";").strip()


def validate(sql: str) -> str:
    s = _strip(sql)
    if not s:
        raise SqlError("空查询")
    if ";" in s:
        raise SqlError("只允许单条语句")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise SqlError("只允许 SELECT / WITH 查询")
    if _FORBIDDEN.search(s):
        raise SqlError("检测到非只读关键字，已拒绝")
    return s


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (_dt.timedelta,)):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, memoryview):
        return v.tobytes().decode("utf-8", "replace")
    return v


# ---------------------------------------------------------------- Athena 后端

_ath_client = None
_identity = ""
# 初始化这一坨要上锁：它做 AssumeRole + STS get_caller_identity + 建 boto3 客户端，
# 冷启动实测 ~2s，而 `if _ath_client is None` 是个不设防的检查。实测代价：/health 的
# 数据层探针和 `backend_info()` 几乎同时进来，两边**各做一遍**同一套初始化，还要互相
# 抢 GIL——两件本该 2s 的事变成 5.0s / 3.2s，把前端探针往超时线上推。
# 惰性初始化是幂等的，所以第二个进来的应该**等**，而不是重做一遍。
_ath_lock = threading.Lock()

# 设了就以这个角色的身份查（L4 治理层的最小权限角色，见
# scripts/lakehouse/governance.py）。不设＝用进程自己的凭证，也就是本地开发时
# 你的 admin 身份——那是**没有列级边界**的状态。
AGENT_ROLE_ARN = os.getenv("AGENT_ROLE_ARN", "")


def _workgroup(athena) -> str:
    """按身份选 workgroup：治理角色走 agent 那个，裸凭证走管理侧那个。

    为什么不是一个 workgroup 加一个 `ATHENA_WORKGROUP` 环境变量了事：workgroup 的
    `OutputLocation` 决定查询结果 CSV 落到哪个 S3 前缀，而结果 CSV 是**明文行数据**。
    两侧共用一个前缀时，agent 角色对该前缀的 GetObject 让它能从管理侧查询的结果文件
    里把 LF 已经排除掉的 `users.email` 读回来。完整说明在 `athena.py` 的
    `AGENT_WORKGROUP` 上方，IAM 侧在 `scripts/lakehouse/governance.py`。

    两个环境变量刻意**分开**（`ATHENA_AGENT_WORKGROUP` / `ATHENA_WORKGROUP`）：
    共用一个的话，`.env.local` 里为了跑管理脚本设的那个值会把治理路径也拽回
    管理侧 workgroup，而这件事从外面看不出来——查询照样成功。
    """
    if AGENT_ROLE_ARN:
        return os.getenv("ATHENA_AGENT_WORKGROUP", athena.AGENT_WORKGROUP)
    return os.getenv("ATHENA_WORKGROUP", athena.WORKGROUP)


def _athena():
    """惰性拿 Athena 客户端（复用 scripts/lakehouse/athena.py，避免两份实现）。

    跟原来复用 `scripts/redshift/rsql.py` 是同一个理由：那个模块被 load.py、
    gen_ddl.py、verify_mart_parity.py 反复跑过，坑（catalog 名带斜杠不能进 SQL、
    结果首行是表头、timestamp 精度）都已经踩平。再抄一份到 backend/ 意味着
    两份实现各自漂移，而漂移的表现是"云上对、本地错"这类最难查的问题。

    代价是 backend 依赖 `athena` 这个模块找得到，而它在两种布局下位置不同：

    - 本仓库：`scripts/lakehouse/athena.py`，相对 `backend/` 是 `../scripts/lakehouse`。
    - AgentCore Runtime 镜像：构建上下文就是 `app/analytics/`（agentcore.json 的
      codeLocation），**拷不到上一级目录**，所以同步脚本把 athena.py 放成 db.py 的
      同级文件，`import athena` 直接命中（`/app` 本来就在 sys.path 上）。

    所以先裸 import 一次再退回仓库布局，而不是无条件插路径：顺序反过来的话，
    容器里那次 `sys.path.insert` 插的是个不存在的目录，能过，但把"为什么能过"
    藏起来了。两条都不中时报错要能照着做——ModuleNotFoundError 本身会报在离原因
    很远的地方。
    """
    global _ath_client, _identity
    if _ath_client is not None:
        return _ath_client                     # 常态：快路径，不进锁
    with _ath_lock:
        if _ath_client is not None:            # 等锁期间别人建好了
            return _ath_client
        lakehouse = Path(__file__).resolve().parent.parent / "scripts" / "lakehouse"
        try:
            import athena                      # 容器布局：athena.py 与 db.py 同级
        except ModuleNotFoundError:
            sys.path.insert(0, str(lakehouse))  # 仓库布局：../scripts/lakehouse
            try:
                import athena
            except ModuleNotFoundError as e:
                raise SqlError(
                    f"找不到 Athena 客户端（既不在 db.py 同级，也不在 {lakehouse}）。"
                    f"要么跑 scripts/deploy/sync_agent_code.py --apply 把它拷到位，"
                    f"要么设 DB_BACKEND=postgres 走本地库。原始错误：{e}") from e
        region = os.getenv("AWS_REGION", athena.REGION)
        session = None
        if AGENT_ROLE_ARN:
            # 治理层角色。**这里就把 STS 打通**（凭证本身是延迟获取的），
            # 目的是让"角色不存在 / 信任策略里没有我"这类配置错误炸在启动和
            # /health 上，而不是等 agent 答到一半才炸在一条查询里——那时报错
            # 位置离原因很远，而且用户看到的是"这道题失败了"。
            session = athena.assume_role_session(
                AGENT_ROLE_ARN, region, "analytics-agent-backend")
            try:
                _identity = session.client(
                    "sts", region_name=region).get_caller_identity()["Arn"]
            except Exception as e:               # noqa: BLE001
                raise SqlError(
                    f"assume 不了 AGENT_ROLE_ARN={AGENT_ROLE_ARN}：{e}。"
                    f"要么把当前身份加进它的信任策略"
                    f"（python3 scripts/lakehouse/governance.py --apply），"
                    f"要么不设这个变量（那就没有列级边界）。") from e
        else:
            _identity = ""
        _ath_client = athena.Client(
            catalog=os.getenv("ATHENA_CATALOG", athena.ATHENA_CATALOG),
            database=os.getenv("ICEBERG_NAMESPACE", athena.NAMESPACE),
            workgroup=_workgroup(athena),
            region=region, session=session,
        )
    return _ath_client


def _run_query_athena(sql: str) -> dict:
    clean = validate(sql)                      # 只读闸门与 Postgres 路径共用
    t0 = time.perf_counter()
    # 超时用客户端轮询把关（Athena 没有会话级 statement_timeout），
    # 且 athena.py 超时后会主动取消查询，避免"没人等了但还在扫字节"。
    res = _athena().execute(clean, timeout=STMT_TIMEOUT_MS / 1000.0 * 4)
    exec_ms = round((time.perf_counter() - t0) * 1000, 1)
    rows = res.get("rows", [])
    truncated = len(rows) > MAX_ROWS
    return {"columns": res.get("columns", []),
            "rows": [[_jsonable(c) for c in r] for r in rows[:MAX_ROWS]],
            "rowcount": min(len(rows), MAX_ROWS),
            "truncated": truncated,
            "exec_ms": res.get("elapsed_ms", exec_ms),
            # 按扫描字节计费，所以这是成本的直接度量。见模块 docstring 第 2 点。
            "bytes_scanned": res.get("bytes_scanned", 0)}


# ---------------------------------------------------------------- DuckDB 后端

_duck_client = None
_duck_lock = threading.Lock()


def _arm_module(mod: str, subdir: str):
    """import 一个 arm 的客户端模块，兼容两种布局。

    与 `_athena()` 同样的两段式：先裸 import（AgentCore 镜像里同步脚本把文件放成
    db.py 的同级），再退回仓库布局 `../scripts/<subdir>`。顺序不能反——反过来那次
    `sys.path.insert` 在容器里插的是不存在的目录，能过，但把「为什么能过」藏起来了。
    """
    try:
        return __import__(mod)
    except ModuleNotFoundError:
        d = Path(__file__).resolve().parent.parent / "scripts" / subdir
        sys.path.insert(0, str(d))
        try:
            return __import__(mod)
        except ModuleNotFoundError as e:
            raise SqlError(
                f"找不到 {mod}（既不在 db.py 同级，也不在 {d}）。"
                f"原始错误：{e}") from e


def _duck():
    """DuckDB 客户端。惰性建：第一次要装 extension + ATTACH，实测数秒。"""
    global _duck_client
    if _duck_client is not None:
        return _duck_client
    with _duck_lock:                       # 与 Athena 同理：初始化幂等，第二个该等
        if _duck_client is None:
            conn = _arm_module("conn", "duckdb")
            _duck_client = conn.Client()
        return _duck_client


def _run_query_duckdb(sql: str) -> dict:
    clean = validate(sql)                  # 只读闸门四条 arm 共用
    # 超时这条 arm 也管得住，只是要自己补：DuckDB 没有会话级 statement_timeout
    # （`duckdb_settings()` 里一条相关的都没有），但有 `interrupt()`，conn.Client
    # 用一个看门狗线程调它。实测能在 2.0s 掐停一条跑不完的查询。
    # 三条 arm 的超时机制各不相同——Athena 是轮询 + StopQueryExecution，Redshift 是
    # Data API 的轮询超时，DuckDB 是客户端看门狗——差的是实现成本，不是能力。
    res = _duck().execute(clean, timeout=STMT_TIMEOUT_MS / 1000.0)
    rows = res.get("rows", [])
    truncated = len(rows) > MAX_ROWS
    return {"columns": res.get("columns", []),
            "rows": [[_jsonable(c) for c in r] for r in rows[:MAX_ROWS]],
            "rowcount": min(len(rows), MAX_ROWS),
            "truncated": truncated,
            "exec_ms": res.get("elapsed_ms", 0),
            "bytes_scanned": 0}            # 见 HAS_BYTES_SCANNED


# ---------------------------------------------------------------- Redshift 后端

_rs_client = None
_rs_lock = threading.Lock()


def _rs():
    global _rs_client
    if _rs_client is not None:
        return _rs_client
    with _rs_lock:
        if _rs_client is None:
            rsql = _arm_module("rsql", "redshift")
            # keepalive=0：agent 路径不需要跨语句保持会话状态，而会话会占住一个
            # Redshift 连接。计时那条路径才开会话（scripts/bench/arms.py）。
            _rs_client = rsql.Client()
        return _rs_client


def _run_query_redshift(sql: str) -> dict:
    clean = validate(sql)
    t0 = time.perf_counter()
    # 与 Athena 同一个理由用客户端轮询超时：Data API 每次调用是独立会话，
    # `SET statement_timeout` 发下去下一条就没了（见 rsql.py 的 docstring）。
    res = _rs().execute(clean, timeout=STMT_TIMEOUT_MS / 1000.0 * 4)
    exec_ms = round((time.perf_counter() - t0) * 1000, 1)
    rows = res.get("rows", [])
    truncated = len(rows) > MAX_ROWS
    return {"columns": res.get("columns", []),
            "rows": [[_jsonable(c) for c in r] for r in rows[:MAX_ROWS]],
            "rowcount": min(len(rows), MAX_ROWS),
            "truncated": truncated,
            "exec_ms": res.get("elapsed_ms", exec_ms),
            "bytes_scanned": 0}


# ---------------------------------------------------------------- Postgres 后端

def _run_query_sync(sql: str) -> dict:
    if BACKEND == "athena":
        return _run_query_athena(sql)
    if BACKEND == "duckdb":
        return _run_query_duckdb(sql)
    if BACKEND == "redshift":
        return _run_query_redshift(sql)
    clean = validate(sql)
    with psycopg.connect(**PG, connect_timeout=8) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            # statement_timeout 用 set_config 参数化下发（STMT_TIMEOUT_MS 本就是内部 int
            # 常量、非用户输入，但参数化可彻底消除 SQL 字符串拼接的静态告警）。
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (str(int(STMT_TIMEOUT_MS)),))
            # 只计 execute+fetch 的纯 DB 耗时（不含建连/校验），用于前端把"SQL 真正
            # 执行时间"与"模型推理时间"拆开显示——这俩以前被混计在一个 stage 里。
            t0 = time.perf_counter()
            cur.execute(clean)
            cols = [d.name for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(MAX_ROWS + 1)
            exec_ms = round((time.perf_counter() - t0) * 1000, 1)
            truncated = len(fetched) > MAX_ROWS
            rows = [[_jsonable(c) for c in r] for r in fetched[:MAX_ROWS]]
    return {"columns": cols, "rows": rows, "rowcount": len(rows),
            "truncated": truncated, "exec_ms": exec_ms}


async def run_query(sql: str) -> dict:
    return await asyncio.to_thread(_run_query_sync, sql)


def system_query(sql: str) -> list[tuple]:
    """服务端自己的元数据查询，返回原始行。**不给 agent 用。**

    与 `run_query` 的两点区别，都是刻意的：

    1. **不做 MAX_ROWS 截断。** `run_query` 截到 1000 行是为了保护前端和 token 预算，
       但元数据查询要的是完整集合——48 张表 × 平均 12 列已经 576 行，别人加几张表就会
       静默越界，然后 UI 少显示几个字段而不报错。这种"静默少一点"的 bug 最难发现。
    2. **不返回耗时/截断等展示字段**，调用方要的是数据本身。

    仍然过 `validate()`：这些 SQL 都写死在本仓库里、不含用户输入，但过闸门几乎零成本，
    而它保证「服务端也不会对数据库发起写操作」这条不变量没有例外通道。
    """
    clean = validate(sql)
    if BACKEND == "athena":
        return [tuple(r) for r in _athena().execute(clean).get("rows", [])]
    if BACKEND == "duckdb":
        return [tuple(r) for r in _duck().execute(clean).get("rows", [])]
    if BACKEND == "redshift":
        return [tuple(r) for r in _rs().execute(clean).get("rows", [])]
    with psycopg.connect(**PG, connect_timeout=8) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(clean)
            return cur.fetchall()


def backend_info() -> dict:
    """当前后端的身份信息，给 /health 与 UI 顶栏用。

    原来 /health 无条件读 `PG` 这个字典，于是切了后端之后前端顶栏还显示
    `127.0.0.1:5433`——库明明在云上。**坐标错了比不显示更糟**，因为它
    看起来是对的。这里按后端分派，并额外给出 `engine`，让 UI 不必自己拼引擎名
    （拼死的那份就是"PostgreSQL（实时）"一直挂在顶栏的由来）。
    """
    if BACKEND == "duckdb":
        conn = _arm_module("conn", "duckdb")
        return {
            "engine": ENGINE_LABEL,
            "name": conn.NAMESPACE,
            "table_bucket": conn.TABLE_BUCKET,
            "region": conn.REGION,
            "transport": "in-process",                # 没有 host/port，也没有网络往返
            # 算力口径进 backend_info 是刻意的：这条 arm 的耗时**取决于跑它的机器**，
            # 而另两条不。不把 threads/memory_limit 摆在外面能看见的地方，
            # 「DuckDB 更快」这句话就没有前提。这里是从进程外唯一能核对它的入口。
            "compute": conn.effective_settings(),
            # 空串：这条 arm 没有列级治理边界，见模块 docstring 最后一段。
            "identity": "",
        }
    if BACKEND == "redshift":
        rsql = _arm_module("rsql", "redshift")
        return {
            "engine": ENGINE_LABEL,
            "name": rsql.DATABASE,
            "workgroup": rsql.WORKGROUP,
            "region": rsql.REGION,
            "transport": "Redshift Data API",         # 同样是 HTTPS + IAM，不进 VPC
            # 三条 arm 里只有这条读的不是 Iceberg 表，而是 COPY 进来的一份拷贝。
            # 摆在这里免得看着 /health 以为三条 arm 读的是同一份存储。
            "storage": "Redshift 托管存储（COPY 自 s3://analytics-agent-raw/parquet/）",
            "identity": "",
        }
    if BACKEND == "athena":
        _athena()                                    # 确保 athena 已进 sys.path
        import athena
        return {
            "engine": ENGINE_LABEL,
            "name": os.getenv("ICEBERG_NAMESPACE", athena.NAMESPACE),
            # 治理角色生效时这里是 agent 专属 workgroup（结果集落在它自己的 S3
            # 子前缀下，读不到管理侧的结果文件）。见 `_workgroup()`。
            "workgroup": _workgroup(athena),
            "catalog": os.getenv("ATHENA_CATALOG", athena.ATHENA_CATALOG),
            "region": os.getenv("AWS_REGION", athena.REGION),
            "transport": "Athena API",                # 没有 host/port：HTTPS + IAM
            # 以谁的身份查数。空 = 进程自己的凭证（本地开发常是 admin，**没有列级
            # 边界**）。治理层建的最小权限角色生效时这里是它的 ARN——这个字段是
            # "治理接上了没有"唯一可从外面看见的证据，`governance.py
            # --verify-backend` 断言的就是它。
            "identity": _identity,
        }
    return {
        "engine": ENGINE_LABEL,
        "name": PG["dbname"],
        "host": PG["host"],
        "port": PG["port"],
        "transport": "psycopg",
    }


def _selftest() -> int:
    """只读闸自测（无云依赖）：`python3 backend/db.py`。

    这道闸管的是「不许写」，L4 治理层（最小权限角色 + Lake Formation 列级排除）
    管的是「不许看」，两条别互相当替补：授权面里那些表 agent 是有 SELECT 的，
    拦住「顺着写一条 DROP 出去」的第一道就是这里。L4 那个角色的 IAM 策略在 Glue 上
    也是只读的（`governance.py --selftest` 会盯着这一点），所以真要绕过去还得撞一层，
    但只有这里能给出一句能读懂的拒绝、并且不必先付一次 Athena 调用。
    而在这个自测之前，整套
    L0–L6 里没有一条断言碰过它——`validate()` 被谁改松了，没有任何东西会响。
    改宽一个边界不会让任何测试变红，这正是最该有测试的地方。

    只测 `validate()`：它是纯函数，不连网、不看后端。真正执行路径上的另外两道
    （超时、行数上限）要连库才能验，在 L5/L6 覆盖。
    """
    reject = [
        ("DELETE FROM users", "写语句"),
        ("SELECT 1; DROP TABLE users", "分号拼接第二条"),
        ("SELECT 1\nDROP TABLE users", "不带分号的尾随写动作"),
        ("-- SELECT 1\nUPDATE users SET status='x'", "注释伪装成只读开头"),
        ("/* SELECT */ TRUNCATE TABLE orders", "块注释伪装"),
        ("UNLOAD ('SELECT 1') TO 's3://x/'", "Trino 特有的写动作：UNLOAD"),
        ("ANALYZE orders", "Trino 特有的写动作：ANALYZE"),
        ("CREATE TABLE t AS SELECT 1", "CTAS"),
        ("", "空查询"),
        ("   ;  ", "只有分号"),
    ]
    accept = [
        "SELECT count(*) FROM users",
        "select 1",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
        "SELECT * FROM orders WHERE dt > (SELECT max(as_of_date) FROM meta_snapshot) "
        "- interval '30' day",
        "SELECT count(*) FILTER (WHERE status = 'paid') FROM orders",
        "SELECT a FROM t1 UNION ALL SELECT a FROM t2",
        "SELECT count(*) FROM post_comments",          # 含 comment 但不是关键字
        "SELECT created_at, is_active FROM channels",   # 含 create/act 的子串
        "SELECT 1 -- DROP TABLE users",                # 注释里的写动作不算
    ]

    bad = 0
    for sql, why in reject:
        try:
            validate(sql)
            bad += 1
            print(f"  FAIL 该拒的没拒（{why}）：{sql!r}")
        except SqlError:
            pass
    for sql in accept:
        try:
            validate(sql)
        except SqlError as e:
            bad += 1
            print(f"  FAIL 该放的被拒了（{e}）：{sql!r}")

    # 已知的**过度拒绝**，钉在这里而不是当 bug：字面量里出现关键字会被拦
    # （`WHERE action = 'delete'`）。宁可误拒也不误放——但这行为要看得见，
    # 免得下次有人当成"闸坏了"去放宽正则。
    try:
        validate("SELECT * FROM logs WHERE action = 'delete'")
        bad += 1
        print("  FAIL 字面量里的关键字现在放过去了——闸被放宽过？")
    except SqlError:
        pass

    # ---- arm 切换的两条不变量。放在这个自测里是因为它们和只读闸一样：
    #      被改坏了不会让任何别的测试变红。
    if DIALECTS["athena"] != "":
        bad += 1
        print("  FAIL athena 的方言附录不再是空串——那条 arm 的提示词字节变了，"
              "已有的 L7 基线（27 题）随之作废，必须重跑才能继续拿它做参照")
    if set(DIALECTS) != set(BACKENDS):
        bad += 1
        print(f"  FAIL DIALECTS 的键 {sorted(DIALECTS)} 与 BACKENDS "
              f"{sorted(BACKENDS)} 不一致——切到缺的那个 arm 会在 import 时 KeyError")
    # 方言附录之间不能串味。判据是「这一行**叫模型写**别家的写法」，不是「提到了」——
    # 提到恰恰是必要的：Redshift 那段要写「没有 format_datetime」才拦得住模型去用它。
    # 所以按行看，同一行有否定词就算是在禁止，没有就算是在教。
    _NEG = ("没有", "不存在", "不写", "改写成", "不是", "不接受")
    for arm, alien in (("redshift", "format_datetime"),
                       ("redshift", "FILTER (WHERE"),
                       ("redshift", "approx_distinct"),
                       ("duckdb", "TO_CHAR"),
                       ("duckdb", "date_add")):
        for line in DIALECTS[arm].splitlines():
            if alien in line and not any(n in line for n in _NEG):
                bad += 1
                print(f"  FAIL {arm} 的方言附录在教别家的写法 {alien!r}：{line.strip()!r}")

    if bad:
        print(f"\n{bad} 项失败 ❌  只读边界或 arm 切换被削弱了，"
              "先看 validate() / _FORBIDDEN / DIALECTS")
        return 1
    print(f"  拒绝 {len(reject)} 类写/多语句、放行 {len(accept)} 条合法查询、"
          "1 项已知过度拒绝")
    print(f"  arm 切换：{len(BACKENDS)} 个后端各有方言附录，athena 为空串"
          f"（当前 {BACKEND}，附录 {len(DIALECT)} 字符）")
    print("全部通过 ✅")
    return 0


def ping() -> bool:
    if BACKEND != "postgres":
        runner = {"athena": _athena, "duckdb": _duck, "redshift": _rs}[BACKEND]
        try:
            runner().execute("SELECT 1")
            return True
        except Exception:
            return False
    try:
        with psycopg.connect(**PG, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(_selftest())
