#!/usr/bin/env python3
"""L4 治理层 —— 给 agent 一个最小权限角色，并把 PII 列从它眼里拿掉。

在这个脚本之前，湖仓跑在 Lake Formation **data lake admin** 身份下：agent 能读
每一张表的每一列，包括 `user_messages` 的私信正文和 `users.email` 明文。唯一生效的
边界是应用层那道只读闸（`backend/db.py` 的 `validate()`）——它只管"不许写"，
不管"不许看"。

v2 在 Redshift 里有两道**查询时**生效的控制（`database/redshift/04_governance.sql`）：
最小权限角色 `analytics_agent_ro`（私信表连表都不授权）+ 三列动态脱敏。这个脚本是
它们在湖仓上的等价物，落地方式换成 IAM 角色 + Lake Formation 列级授权。

## 一处能力下降，明说：脱敏变成了「看不见」

Redshift 的 DDM 是**值级**的：`SELECT email FROM users` 照常返回，但值是
`***@masked.invalid`。**Lake Formation 没有这个原语**——它的列级控制只有
「授权 / 不授权」两态（`ColumnWildcard.ExcludedColumnNames`）。所以 v3 里
`users.email` 不是"查出来是掩码"，而是**这列根本不存在**：

    SELECT email FROM users   →  COLUMN_NOT_FOUND: Column 'email' cannot be resolved
    SELECT * FROM users       →  结果里没有 email / phone 这两列

对"防止 PII 出现在 agent 上下文里"这个目的，排除比掩码更彻底；代价是 agent 看不到
"这里有一列但你不能看"，只能从知识库卡片知道它存在。要做到值级脱敏得另铺一层
Glue Catalog View（把 agent 指到另一个 database，48 张表全部复制成视图，再多一份
没人对账的元数据面）——那笔复杂度换来的只是掩码字符串的观感，不值。

> **前端字段名沿用 `masked`**（`/api/catalog` 的 `governance.masked`），语义改成
> "被排除的列"。改字段名要动前端两处 i18n + 两个渲染契约测试，而含义在文案里说清
> 更划算；`web/index.html` 的 `gov_note` 已经按"看不见"改写。

## 两份清单，刻意不共用

- **策略输入**：`DENY_TABLES` / `EXCLUDE_COLUMNS` —— `--apply` 照它发权限。
- **验收契约**：`MUST_NOT_READ_*` / `MUST_READ_*` —— `--verify` 照它探活。

它们内容重叠，但**不是同一个常量**，而且 `--selftest` 断言两边互相覆盖。理由是这个
项目栽过的那类问题：检查器的预期值从被检查对象现算，那它永远是绿的。分成两份之后，
"有人悄悄把 email 从 EXCLUDE_COLUMNS 里拿掉"会在**离线**就变红（L8
`gov-policy-loosened` 就是注入这个），而不是等到某天有人翻查询日志才发现。

## 明文旁路一：CSV 中转库

`analytics_agent_raw` 那 35 张 `*_csv` 外部表指着 `s3://<原始桶>/csv/`，里面是**明文
CSV**。LF 的列级排除完全管不到它——那是普通 Glue 库 + 普通 S3 对象。所以角色的 IAM
策略里，S3 读权限**一个字节都不给 `csv/`**；`--selftest` 有一条断言专门盯这个
（连"给它加一条 csv/* 的 GetObject"这种改法都会红），`--verify` 里也有一条真探针
去查 `users_csv.email` 并要求它失败。把这条漏了，前面所有列级授权都是装饰。

## 明文旁路二：Athena 自己的查询结果

同类、更隐蔽，而且**它一度是真的**：Athena 把每条查询的结果写成 CSV 落到 workgroup
的 `OutputLocation`，那份 CSV 是**明文行数据**。原来两侧共用一个 workgroup、共用
`athena-staging/` 前缀，而 agent 角色对该前缀有 `GetObject`/`PutObject`——于是

- LF 在目录层从它眼里拿掉的 `users.email`，它可以从**管理侧查询的结果文件**里读回来。
  而管理侧真的会跑那条查询：这个脚本的 `--verify --as-caller` 每次都执行一遍
  `SELECT email FROM users LIMIT 1`，那一行明文就落在共享前缀下。
- 反方向也通：它能**覆盖**管理侧的结果对象，而对账脚本会把结果读回去比数字。

修法是拆成两个 workgroup、两个前缀（`athena-staging/` 与 `athena-staging/agent/`），
agent 的 S3 授权面只到后者。三处各盯一段，缺一段这个洞就能悄悄回来：

- **声明侧**：`setup.py` 的 `workgroup_plan()` + 它的自测（两个 workgroup 的结果位置
  必须真的分开，且 agent 那个是真子前缀）。
- **策略侧（离线）**：`policy_findings()`——把前缀退回 `athena-staging/`、把
  `s3:prefix` 条件退回共享前缀、或者授权到管理侧 workgroup，三种改法都会红。
- **云上侧**：`probe_staging_isolation()`——以角色凭证真去列 / 读 / 写管理侧前缀，
  三样都必须被拒。它盯的是"角色上实际挂着的策略"，而离线那道只盯本脚本声明的文本。

一句话：LF 的列级排除管的是「查得到吗」，管不了「结果放哪儿」。

## 踩出来的点

1. **LF 权限是累加的。** 已经有一条 `TableWildcard` 的 SELECT 时，再发一条带
   `ExcludedColumnNames` 的授权**不会**收紧任何东西——两条并存，宽的那条胜。所以
   这个脚本对目标角色**只发列级授权、绝不发表通配**，并在 `--verify` 里把
   "存在表通配授权"当成 drift 报出来。
2. **改排除列必须先 revoke。** 从"排除 email"改成"排除 email+phone"不是覆盖，是
   新增一条。`--apply` 只做增量授权；发现冲突的旧授权时它**报告而不自己撤**，
   要撤得显式加 `--revoke-extra`（只会碰发给本脚本这个专属角色的授权）。
3. **角色可能不是我们建的。** 同名角色已存在且**没有本项目的 tag** 时，直接退出并
   报出来——宁可什么都不做，也不去改一个别人的角色的信任策略。
4. **一次 `sts.assume_role()` 不够。** 后端进程活得比 1 小时长，固定凭证会在 agent
   答题中途 ExpiredToken。走 `athena.assume_role_session()`（可自动续期），
   见那边的注释。

用法：

    python3 scripts/lakehouse/governance.py --selftest        # 无云依赖，秒级
    python3 scripts/lakehouse/governance.py                   # = --verify（只读）
    python3 scripts/lakehouse/governance.py --verify
    python3 scripts/lakehouse/governance.py --verify-backend  # 后端真的在用这个角色吗
    python3 scripts/lakehouse/governance.py --apply           # 建角色 + 发权限（幂等）
    python3 scripts/lakehouse/governance.py --apply --revoke-extra   # 顺带撤掉多余授权
    python3 scripts/lakehouse/governance.py --verify --as-caller     # 负测用，见下

`--as-caller` 用当前身份（data lake admin）跑同一批探针。那时"读不到"的断言**必须
全部变红**——这是证明探针真的在探、而不是永远绿的唯一办法。L8 的
`gov-probe-blind` 就是钉这个。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import zlib
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import athena  # noqa: E402  import 时不发任何 AWS 调用，--selftest 保持无云

REGION = athena.REGION
RAW_BUCKET = athena.RAW_BUCKET
TABLE_BUCKET = athena.TABLE_BUCKET
NAMESPACE = athena.NAMESPACE

# **agent 走自己的 workgroup 和自己的结果前缀**，管理侧那一对只在这里作为"不许碰
# 的那一半"出现。理由见 `athena.py` 的 `AGENT_WORKGROUP` 上方；一句话版本：
# workgroup 的 OutputLocation 决定结果 CSV 落在哪个 S3 前缀，而结果 CSV 是明文行
# 数据 —— 共用前缀时 agent 能从管理侧的结果文件里把 LF 已经排除掉的 email 读回来，
# 还能覆盖管理侧的结果对象。列级排除管"查得到吗"，管不了"结果放哪儿"。
ADMIN_WORKGROUP = athena.WORKGROUP
WORKGROUP = athena.AGENT_WORKGROUP
STAGING_PREFIX = athena.STAGING_PREFIX
AGENT_STAGING_PREFIX = athena.AGENT_STAGING_PREFIX
CATALOG_NAME = "s3tablescatalog"
RAW_GLUE_DB = os.environ.get("RAW_GLUE_DB", "analytics_agent_raw")

ROLE_NAME = os.environ.get("AGENT_ROLE_NAME", "analytics-agent-ro")
POLICY_NAME = "lakehouse-read"
ROLE_DESC = ("Analytics agent · read-only lakehouse access "
             "(Lake Formation column-level). Managed by scripts/lakehouse/governance.py")
TAG_KEY = "Project"
TAG_VALUE = "analytics-agent-progressive-disclosure"
SESSION_NAME = "governance-verify"

# ------------------------------------------------------------------ 策略输入
# 这两个常量是 `--apply` 的输入。改它们等于改治理策略。

# 连表都不授权的表。私信正文不是分析素材，是通信内容——比"脱敏"更彻底。
# 与 v2 `database/redshift/04_governance.sql` 的取舍一致。
DENY_TABLES = ("user_messages",)

# 授权到表、但把这些列排除掉。
EXCLUDE_COLUMNS = {
    "users": ("email", "phone"),
    "user_profiles": ("birth_date",),
}

DATABASE_PERMS = ["DESCRIBE"]
TABLE_PERMS = ["SELECT"]

# ------------------------------------------------------------------ 验收契约
# 这些是 `--verify` 的预期。**刻意不从上面那两个常量推导**，理由见 docstring。

MUST_NOT_READ_TABLES = ("user_messages",)
MUST_NOT_READ_COLUMNS = (("users", "email"), ("users", "phone"),
                         ("user_profiles", "birth_date"))
# 明文旁路：CSV 中转库里的同一份数据（普通 Glue 表 + 明文 S3 对象，LF 管不到）
MUST_NOT_READ_RAW = (("users_csv", "email"),)

# Iceberg 的**元数据表**（`<表>$files` / `$snapshots` / `$manifests`）。单独钉一条的
# 理由和 MUST_NOT_READ_RAW 完全同类——它是一条**从 SQL 里读出旁路入口**的路：
# `users$files` 的 `file_path` 列直接给出数据文件的 S3 路径，拿到路径再配上
# `s3tables:GetTableData`（那个权限**必须**给，理由见上面那段长注释）就能绕过
# 查询引擎的列级排除去读明文。`backend/db.py` 的 validate() 不会挡它（只读闸门管的是
# 写操作，`users$files` 是个合法的 SELECT）。
#
# 实测（2026-09-16，两个身份对跑）：agent 角色查 `users$files` / `users$snapshots`
# 报 `TABLE_NOT_FOUND: … or requester is not authorized`；**管理员身份查得到**
# （返回 content / file_path / file_format / record_count / file_size_in_bytes）。
# 两边不一样才说明这道边界是权限给的，不是"S3 Tables 压根不支持元数据表"——
# 后者的话这条探针绿了也什么都不证明，而 `--as-caller` 负测模式正好会把这件事戳破。
MUST_NOT_READ_META = ("users$files", "users$snapshots")

MUST_READ_TABLES = ("users", "user_profiles", "orders", "channels",
                    "mart_daily_kpi", "meta_snapshot", "tmp_campaign_roi_analysis")
MUST_READ_COLUMNS = (("users", "user_id"), ("users", "username"),
                     ("users", "registration_source"), ("user_profiles", "age"))

# 授权面里出现这些就是 drift：宽授权会盖掉列级排除（踩出来的点 1）
FORBIDDEN_GRANT_SHAPES = ("TableWildcard", "AllTables")

# 失败原因白名单。探针"该失败"时还要看它为什么失败——因为一个写错表名的 SQL
# 也会失败，那时"红了"证明不了任何权限约束。
_DENIED = re.compile(
    r"COLUMN_NOT_FOUND|TABLE_NOT_FOUND|cannot be resolved|does not exist|"
    r"Insufficient Lake Formation|Insufficient permission|not authorized|"
    r"AccessDenied|Access Denied|Forbidden|Unauthorized",
    re.IGNORECASE)

# 排除清单，**优先于**上面的白名单。这些失败发生在权限判定之前——查询压根没跑到
# "这张表/这列有没有授权"那一步，所以不能记成"正确拒绝"。
#
# 这不是假想的洞：`CATALOG_NOT_FOUND: Catalog '…' does not exist` 会被白名单里的
# `does not exist` 匹配上。实测时角色一度看不到联邦目录，于是 4 条「读不到」探针
# 全部变绿——**治理层最核心的那几条断言在完全没生效的情况下报了通过**。
# 白名单只问"这个理由像不像拒绝"，答不了"它是不是根本没走到判定"。
_NOT_A_DENIAL = re.compile(
    r"CATALOG_NOT_FOUND|SCHEMA_NOT_FOUND|"          # 目录/库层面就断了
    r"Unable to verify/create output bucket|"        # 结果桶权限，跟数据授权无关
    r"WORKGROUP_NOT_FOUND|workgroup .* not found|"   # 环境配错
    r"ExpiredToken|security token included in the request is expired|"
    r"ThrottlingException|TooManyRequests|SlowDown|"  # 被限流不是被拒绝
    # AssumeRole 本身被拒 —— 这一轮**没能变成 agent 角色**，所以每条探针用的都是
    # 别的身份。它长得最像"权限挡住了"（就是一条 AccessDenied），而它意味着的恰恰
    # 是"什么都没测到"。[7] 那里 assume 完会立刻 get_caller_identity() 把这种情况
    # 挡在探针之前；这一条是第二层，防的是别处新写的调用绕过那道检查。
    r"sts:AssumeRole|not authorized to perform: sts:",
    re.IGNORECASE)


def is_denial(msg: str) -> bool:
    """这条失败信息能不能算作「权限把它挡住了」。

    顺序是刻意的：先排除基础设施级失败，再看像不像拒绝。反过来写就会把
    `CATALOG_NOT_FOUND: … does not exist` 判成拒绝。
    """
    if _NOT_A_DENIAL.search(msg):
        return False
    return bool(_DENIED.search(msg))

# 策略自检用：像写操作的动作名，除白名单外一律报出来
_WRITEISH = re.compile(
    r":(Create|Delete|Update|Put|Grant|Revoke|Register|Deregister|Start|Stop|"
    r"Tag|Untag|Abort|Modify|Set|Attach|Detach|Add|Import|Batch|Write)",
    re.IGNORECASE)
_WRITEISH_OK = {
    "athena:StartQueryExecution",    # 提交查询本身就是这个动作名，没有只读版
    "athena:StopQueryExecution",     # 超时要能取消，否则继续扫字节继续计费
    "s3:PutObject",                  # Athena 把结果集写到 staging 前缀
    "s3:AbortMultipartUpload",       # 大结果集分段上传的清理
}

# S3 Tables 的数据面动作。必须分成两类，因为取舍完全相反。
#
# `GetTableData` **既是必须给的，又是 LF 列级排除的一条真旁路**。两件事都是实测
# 结论，只记住其中一件都会得出错误的设计：
#
#   · 不给会怎样：Glue 对联邦目录的解析（`get_table` / `get_tables`）直接
#     `Access Denied`（不带来源信息），Athena 侧表现为 TABLE_NOT_FOUND。六个
#     s3tables 读动作逐个 bisect 过，就是它。根因是 S3 Tables 的表桶**无法**
#     注册成 LF 的数据湖位置——`RegisterResource` 对表桶 ARN 返回
#     `Unsupported resource path`，对联邦 catalog ARN 返回 `Un-supported
#     resource arn format`。没有注册位置就没有 vended credentials 这条路，
#     Glue 只能拿调用方自己的身份去 S3 Tables 取元数据。
#   · 给了会怎样：持有该角色凭证的进程可以 `get_table_metadata_location`
#     → 直读 metadata JSON → 顺 manifest-list / manifest 走到 Parquet。实测
#     `users` 的 500 行数据文件里 `email` / `phone` 明文可读。
#
# 所以 LF 的列级排除是**查询引擎层**的控制：Agent 看到的目录里根本没有这两列，
# 它既选不到也无法提及（`SELECT email` → COLUMN_NOT_FOUND）。但它不隔离一个
# 已经拿到角色凭证的进程。这与 `csv/` 明文旁路同类，只是更隐蔽——那条是"数据
# 在别处还有一份明文"，这条是"同一份数据的授权面下面有一层没被治理"。
# 这个结论不能只写在注释里，probe_direct_read() 会去真读一次把它钉住。
_S3TABLES_REQUIRED = {
    "s3tables:GetTableData",
}

# 写入侧没有任何"必须"的理由，出现即 drift。
# （`_WRITEISH` 也会抓到它；这里多一层是为了给出"为什么危险"而不只是"像写操作"。）
_S3TABLES_DATA_PLANE = {
    "s3tables:PutTableData",
}

# **桶级**动作：请求里没有对象键，因此 `s3:prefix` 这个条件键根本不存在。
# 把它们和带 `s3:prefix` 条件的语句放在一起，条件恒为假 → 等于没授权，
# 而策略文本上看起来是给了的。第一版就是这么写的，症状是 Athena 报
# "Unable to verify/create output bucket"——报错指向桶，不指向策略条件，
# 所以很难往这边想。这套动作名单存在的意义是让这个洞离线就能被抓住。
_NO_PREFIX_CONDITION = {
    "s3:GetBucketLocation",
    "s3:ListAllMyBuckets",
    "s3:GetBucketVersioning",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def _msg(e: Exception) -> str:
    return " ".join(str(e).split())[:200]


def role_arn(acct: str) -> str:
    return f"arn:aws:iam::{acct}:role/{ROLE_NAME}"


def catalog_id(acct: str) -> str:
    """Glue/LF 侧的目录 ID，**带账号前缀**（Athena 侧那个不带）。"""
    return f"{acct}:{CATALOG_NAME}/{TABLE_BUCKET}"


# ---------------------------------------------------------------- IAM 策略文本

TRUST_SID = "AllowAgentRuntimeAndDeveloper"


def trust_policy(principals: list[str]) -> dict:
    """谁能 assume 这个角色。**只在角色不存在、要新建时用这个。**

    只放调用方自己的 role ARN（本地开发就是你当前的开发者角色）。上云时把
    AgentCore Runtime 的执行角色也加进来——追加不是替换，所以这里收的是一个列表。
    角色已经存在的情况走 `merge_trust_policy()`，别拿这个函数的返回值去
    `update_assume_role_policy`：那会把现有文档整个换掉。
    """
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": TRUST_SID,
        "Effect": "Allow",
        "Principal": {"AWS": sorted(set(principals))},
        "Action": "sts:AssumeRole",
    }]}


class TrustPrincipalError(ValueError):
    """`--trust` 或当前身份给出的 principal 不能进信任策略。"""


def validate_principals(principals: list[str]) -> list[str]:
    """信任策略的入口校验。**只放具体的 IAM role/user ARN。**

    三类要拦（都不是假想的）：

    1. `*` —— 任何账号的任何身份都能 assume 这个角色。`--trust '*'` 原来直接就写进去了，
       而写进去之后这个角色不再是「最小权限」，是「任何人都能拿的最小权限」。
    2. `arn:aws:iam::<账号>:root` —— 那不是一个身份，是整个账号：账号里任何有
       `sts:AssumeRole` 的 principal 都能扮演它。`me` 是从 `get_caller_identity()` 来的,
       用 root 凭证跑一次 `--apply`，root 就这么进了信任策略，而且没有一行日志说这事。
    3. 不是 `arn:aws:iam::` 开头的 —— 服务 principal（`lambda.amazonaws.com`）、
       联合身份、拼错的 ARN。服务 principal 要进的话得显式写一条带 Condition 的语句，
       不该从这个列表悄悄溜进来。注意 `arn:aws:sts::…:assumed-role/…` 也在这里被拦：
       它是一个**会话**而不是一个身份，写进信任策略不会生效（要写它背后那个 role），
       `setup.caller_role_arn()` 就是为了把它换算回 role ARN 的。

    去重并排序返回，让 `--apply` 幂等。
    """
    out: set[str] = set()
    for p in principals:
        p = (p or "").strip()
        if not p:
            continue
        if p == "*" or p.endswith(":root"):
            raise TrustPrincipalError(
                f"拒绝把 {p!r} 放进 {ROLE_NAME} 的信任策略："
                + ("`*` 等于任何账号的任何身份都能 assume 它。"
                   if p == "*" else
                   "`:root` 是整个账号而不是一个身份，账号里任何能 AssumeRole 的"
                   "principal 都能扮演这个角色。用 root 凭证跑的话，改用具体的"
                   "开发者角色：--trust arn:aws:iam::<账号>:role/<角色名>。")
            )
        if not p.startswith("arn:aws:iam::"):
            raise TrustPrincipalError(
                f"拒绝把 {p!r} 放进 {ROLE_NAME} 的信任策略：只接受具体的 "
                f"arn:aws:iam::<账号>:role/… 或 :user/…。"
                + ("`assumed-role` 是会话不是身份，写进去不生效——要写它背后那个 role。"
                   if ":assumed-role/" in p else
                   "服务 principal / 联合身份要单独写一条带 Condition 的语句。")
            )
        out.add(p)
    if not out:
        raise TrustPrincipalError("信任策略的 principal 列表为空——角色会没人能 assume")
    return sorted(out)


def _as_list(v) -> list:
    """IAM 文档里「单值可以不写成数组」，读的时候要两种都接。"""
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def unconditional_principals(doc: dict) -> set[str]:
    """文档里**无条件**就能 `sts:AssumeRole` 的 AWS principal 集合。

    两处刻意从严，都是为了别把"其实 assume 不到"读成"已经齐了"：

    - 带 `Condition` 的语句不算。信任策略里的条件通常正是门槛（要求 MFA、要求
      `sts:ExternalId`），Runtime 的 exec role 满足不了；把它当成"已授权"会让
      `--verify` 绿着而云上 assume 失败。
    - 只认 `Effect: Allow`。`Deny` 语句里出现的 principal 当然不算授权。

    代价是：如果有人手工给我们这条加了 Condition，`--apply` 会另外补一条无条件的
    语句进去（而不是改他那条）。那是**该发生**的——脚本不改别人写的语句。
    """
    out: set[str] = set()
    for st in _as_list(doc.get("Statement")):
        if not isinstance(st, dict) or st.get("Effect") != "Allow" or st.get("Condition"):
            continue
        acts = _as_list(st.get("Action"))
        if not any(a in ("sts:AssumeRole", "sts:*", "*") for a in acts):
            continue
        principal = st.get("Principal")
        if not isinstance(principal, dict):
            continue                      # `"Principal": "*"` —— 不当成具体 ARN
        out |= set(_as_list(principal.get("AWS")))
    return out


def merge_trust_policy(current: dict, principals: list[str]) -> dict:
    """把 principals 并进**现有**信任策略，保留其余一切。

    原来这里是 `trust_policy(sorted(cur_p | set(principals)))`：读出现有文档里所有
    `Principal.AWS`，然后拿声明模板重新拼一份盖回去。少的东西不显眼但都致命——
    `Condition`（MFA / ExternalId 门槛）、`Service` 与 `Federated` principal
    （比如让某个服务扮演它、或 SSO 联合身份）、以及第二条之后的所有语句。盖回去的
    那一刻它们就没了，而 `update_assume_role_policy` 不会有任何抗议：下一次
    `--apply` 会安静地拆掉一条别人加的授权路径。

    所以改成真的合并：现有语句一律原样保留，缺的 principal 优先并进我们自己那条
    （按 `Sid` 认领），认不到就**追加**一条新语句。任何情况下都不改写别人写的语句。
    """
    if not current or not _as_list(current.get("Statement")):
        return trust_policy(principals)
    # 深拷贝：调用方（和 --verify 的对比）拿到的 `current` 不该被改。json 往返
    # 够用——IAM 文档只有 JSON 标量。
    out = json.loads(json.dumps(current))
    out["Statement"] = _as_list(out.get("Statement"))
    want = set(principals) - unconditional_principals(out)
    if not want:
        return out
    for st in out["Statement"]:
        if isinstance(st, dict) and st.get("Sid") == TRUST_SID:
            aws = set(_as_list(st.get("Principal", {}).get("AWS")))
            st.setdefault("Principal", {})["AWS"] = sorted(aws | want)
            return out
    out["Statement"].append(trust_policy(sorted(want))["Statement"][0])
    return out


def policy_document(acct: str, region: str = REGION) -> dict:
    """角色的内联只读策略。

    ARN 集合是**实测迭代出来的**，不是照文档抄的——少一条的表现通常不是
    "AccessDenied"，而是 Athena 报一句指不到成因的 GENERIC_INTERNAL_ERROR。

    两处刻意的通配，都有原因：
    - `datacatalog/*`：联邦目录名里带斜杠（`s3tablescatalog/<表桶>`），拼进 ARN
      是未定义行为。账号内只有这一个目录，通配的实际暴露面等于零。
    - `glue` 的 `database/*` `table/*`：Athena 内部要 GetTable 才能规划查询，而
      **数据面不靠这层挡** —— 靠 LF 列级授权 + S3 前缀。它的后果是角色能看到
      CSV 中转库的表**结构**（不含值），这是可接受的。
    """
    cat = f"arn:aws:glue:{region}:{acct}:catalog"
    return {"Version": "2012-10-17", "Statement": [
        {
            "Sid": "AthenaQuery",
            "Effect": "Allow",
            "Action": ["athena:StartQueryExecution",
                       "athena:StopQueryExecution",
                       "athena:GetQueryExecution",
                       "athena:GetQueryResults",
                       "athena:GetQueryResultsStream",
                       "athena:GetWorkGroup",
                       "athena:GetDataCatalog",
                       "athena:ListDataCatalogs"],
            "Resource": [f"arn:aws:athena:{region}:{acct}:workgroup/{WORKGROUP}",
                         f"arn:aws:athena:{region}:{acct}:datacatalog/*"],
        },
        {
            "Sid": "GlueCatalogRead",
            "Effect": "Allow",
            "Action": ["glue:GetCatalog", "glue:GetCatalogs",
                       "glue:GetDatabase", "glue:GetDatabases",
                       "glue:GetTable", "glue:GetTables",
                       "glue:GetPartition", "glue:GetPartitions"],
            # `glue:GetUnfiltered*` **刻意不在这里**，尽管排查 TABLE_NOT_FOUND 时
            # 一度加过。那一组是给第三方引擎（Spark / Trino 等）走 LF 过滤读的，
            # 本账号 `AllowExternalDataFiltering = false`，直接调用一律 AccessDenied；
            # 而 Athena 是 first-party，不受这个开关约束，走的也不是这条路。
            # 给了它既不解决问题也不产生效果，只会让策略看起来比实际更宽。
            "Resource": [cat, f"{cat}/*",
                         f"arn:aws:glue:{region}:{acct}:database/*",
                         f"arn:aws:glue:{region}:{acct}:table/*"],
        },
        {
            # S3 Tables 的**元数据**读。为什么需要这一条：Glue 的联邦目录不是自己
            # 存元数据，而是把 GetDatabases / GetTables 转发给 S3 Tables 服务，
            # 而那一跳用的是**调用者自己的身份**。少了这条，报错是
            #   AccessDeniedException … From federation source: … s3tables:GetTableBucket
            # 而 Athena 那边只会说 CATALOG_NOT_FOUND——指向"目录不存在"，
            # 不指向"你没权限看它"，所以很容易被当成配置写错了。
            #
            # `s3tables:GetTableData` 是**不得不给**的，而它同时是列级排除的一条
            # 真旁路。这个取舍的完整依据见 `_S3TABLES_REQUIRED` 上方的注释，
            # 以及 probe_direct_read()（会真读一次 Parquet 把这条旁路钉住）。
            # 一句话版本：表桶无法注册成 LF 数据湖位置，没有 vended credentials
            # 这条路，Glue 只能用调用方身份去取元数据；不给它，整个目录解析不了。
            "Sid": "S3TablesRead",
            "Effect": "Allow",
            "Action": ["s3tables:GetTableBucket",
                       "s3tables:ListNamespaces",
                       "s3tables:GetNamespace",
                       "s3tables:ListTables",
                       "s3tables:GetTable",
                       "s3tables:GetTableMetadataLocation",
                       "s3tables:GetTableData"],
            "Resource": [f"arn:aws:s3tables:{region}:{acct}:bucket/{TABLE_BUCKET}",
                         f"arn:aws:s3tables:{region}:{acct}:bucket/{TABLE_BUCKET}/*"],
        },
        # `lakeformation:GetDataAccess` **刻意不给**。凭直觉它是必需的——"LF 管着
        # 数据，当然要允许走 LF 取凭证"——而且它是 AWS 各种示例策略里的标配。
        # 实测（去掉它跑探针）：查询照样通，被排除的列照样拒。原因和上面
        # `_S3TABLES_REQUIRED` 说的是同一件事：表桶注册不进 LF，就没有凭证下发
        # 这条路，Athena 直接用调用方的 s3tables 权限读数据。给了它等于凭空多一条
        # `Resource: "*"` 的授权，还会让人误以为数据面是 LF 在守。
        {
            # Athena 在提交查询前会先问结果桶在哪个区域。这是**桶级**动作，
            # 请求里没有对象键，所以不能挂 `s3:prefix` 条件——挂了条件恒为假，
            # 查询会以 "Unable to verify/create output bucket" 失败。
            # 不带条件是安全的：它只返回一个区域字符串，读不到任何对象。
            "Sid": "AthenaStagingBucketLocation",
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation"],
            "Resource": f"arn:aws:s3:::{RAW_BUCKET}",
        },
        {
            # 枚举对象键才是要挡的：不限前缀就能列出 csv/ 下的明文文件。
            # 见 docstring「明文旁路」。
            #
            # 前缀是 **agent 那个子前缀**，不是共享的 `athena-staging/`：后者下面还有
            # 管理侧查询的结果 CSV。少了这层收窄，`csv/` 那条旁路挡住了，
            # 管理侧结果集这条没挡住 —— 而 `SELECT email FROM users LIMIT 1` 的结果
            # 就落在那里（治理探针自己每次 --verify 都会跑一遍）。
            "Sid": "AthenaStagingList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": f"arn:aws:s3:::{RAW_BUCKET}",
            "Condition": {"StringLike": {"s3:prefix": [f"{AGENT_STAGING_PREFIX}*"]}},
        },
        {
            # 同上：读写都只到 agent 自己那一格。`PutObject` 收窄这一半同样要紧——
            # 共享前缀下它意味着 agent 能**覆盖**管理侧的查询结果对象，
            # 而那些结果是对账脚本读回去比数的。
            "Sid": "AthenaStagingObjects",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
            "Resource": f"arn:aws:s3:::{RAW_BUCKET}/{AGENT_STAGING_PREFIX}*",
        },
    ]}


def policy_findings(doc: dict) -> list[str]:
    """审这份策略够不够窄。返回问题清单（空 = 通过）。

    这是**离线**能跑的那半边治理检查。它挡三类改动：偷偷加个写权限、
    偷偷把 S3 读扩到 `csv/`（后者能一步废掉全部列级排除）、
    以及把桶级动作塞进带 `s3:prefix` 条件的语句里。

    第三条是"太窄"而不是"太宽"，方向和前两条相反，但同样要抓：条件恒假的授权
    **在策略文本上看起来是给了的**，出的错却指向别处（Athena 报结果桶有问题）。
    一个只会朝"太宽"看的审查器，对这种写法永远是绿的。
    """
    bad: list[str] = []
    for st in doc.get("Statement", []):
        sid = st.get("Sid", "?")
        if st.get("Effect") != "Allow":
            bad.append(f"{sid}: 只应有 Allow 语句（Deny 会掩盖策略本身太宽这件事）")
        actions = st.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        res = st.get("Resource", [])
        res = [res] if isinstance(res, str) else res
        for a in actions:
            if a in ("*", "iam:*") or a.endswith(":*"):
                bad.append(f"{sid}: 动作通配 {a}")
                continue
            if _WRITEISH.search(a) and a not in _WRITEISH_OK:
                bad.append(f"{sid}: 疑似写权限 {a}（不在白名单里）")
        # S3 读写只允许落在 **agent 自己那个子前缀**。
        #
        # 判据从 `athena-staging/` 收到 `athena-staging/agent/` 是 B3 的离线那一半：
        # 共享前缀下面有管理侧查询的结果 CSV（明文行数据，含 LF 已经排除掉的 email），
        # 所以"没越出 athena-staging/"这个旧判据对那个洞是绿的。
        # 写操作一起查：`PutObject` 落在共享前缀就是"能覆盖管理侧结果对象"。
        s3_obj = [a for a in actions
                  if a.startswith("s3:") and any(
                      k in a for k in ("Get", "List", "Put", "Delete", "Abort"))]
        if s3_obj:
            ok_exact = f"arn:aws:s3:::{RAW_BUCKET}"
            ok_prefix = f"arn:aws:s3:::{RAW_BUCKET}/{AGENT_STAGING_PREFIX}"
            shared = f"arn:aws:s3:::{RAW_BUCKET}/{STAGING_PREFIX}"
            for r in res:
                if r == ok_exact or r.startswith(ok_prefix):
                    continue
                if r.startswith(shared):
                    bad.append(
                        f"{sid}: S3 授权到共享前缀 {STAGING_PREFIX} → {r}"
                        f"（那里面有管理侧查询的结果 CSV，是明文行数据；"
                        f"必须收到 {AGENT_STAGING_PREFIX}）")
                    continue
                bad.append(f"{sid}: S3 越出 {AGENT_STAGING_PREFIX} → {r}"
                           f"（csv/ 下是明文 PII）")
        # `s3:prefix` 条件同样要收到子前缀：Resource 收窄了但条件还写共享前缀时，
        # ListBucket 仍然能枚举出管理侧的结果对象键（键名里带 QueryExecutionId，
        # 拿到就能直接 GetObject 试）。
        for a in actions:
            if a != "s3:ListBucket":
                continue
            pref = st.get("Condition", {}).get(
                "StringLike", {}).get("s3:prefix", [])
            pref = [pref] if isinstance(pref, str) else pref
            if not pref:
                bad.append(f"{sid}: s3:ListBucket 没挂 s3:prefix 条件 ——"
                           f" 能枚举整个桶，包括 csv/ 下的明文文件名")
            for p in pref:
                if not p.startswith(AGENT_STAGING_PREFIX):
                    bad.append(f"{sid}: s3:prefix 条件 {p!r} 没收到 "
                               f"{AGENT_STAGING_PREFIX}（能列出管理侧的结果对象键）")
        # Athena workgroup 也是边界的一部分：agent 只能用自己那个。给了管理侧那个，
        # 结果就又落回共享前缀了（OutputLocation 挂在 workgroup 上，
        # 且 `EnforceWorkGroupConfiguration=true` 意味着提交方无法另指位置——
        # 也就是说这条给宽了，S3 那两条收窄反而让 agent 取不到自己的结果，
        # 报错还是指向"结果桶有问题"）。
        if any(a.startswith("athena:") for a in actions):
            admin_wg = f":workgroup/{ADMIN_WORKGROUP}"
            for r in res:
                if r.endswith(admin_wg) and ADMIN_WORKGROUP != WORKGROUP:
                    bad.append(f"{sid}: 授权到管理侧 workgroup {ADMIN_WORKGROUP} → {r}"
                               f"（它的结果落在共享前缀，agent 只能用 {WORKGROUP}）")
        # 资源通配一律不允许。曾经这里对 `lakeformation:*` 开了个豁免，理由是
        # "GetDataAccess 不支持资源级授权"——那条豁免现在没有用户了：实测
        # GetDataAccess 根本不需要（见 policy_document 里的说明）。豁免留着不要紧，
        # 但一个没人用的豁免正是下一条宽授权的入口。
        if "*" in res:
            bad.append(f"{sid}: 资源通配 *")
        # s3tables 的写数据面。注意它不会被上面那条 S3 前缀规则覆盖：动作名是
        # `s3tables:` 不是 `s3:`，`startswith("s3:")` 判不到。
        # 读数据面（`GetTableData`）**不在这里**——它是必须给的，理由见
        # `_S3TABLES_REQUIRED`；盯着它的是 probe_direct_read()，不是这个函数。
        for a in actions:
            if a in _S3TABLES_DATA_PLANE:
                bad.append(
                    f"{sid}: {a} 能直接写底层 Iceberg 数据文件，"
                    f"绕开 LF 授权面（只读角色没有任何理由需要它）")
        # 条件恒假 = 静默没授权。只看"太宽"的审查器抓不到这一类。
        cond = json.dumps(st.get("Condition", {}))
        if "s3:prefix" in cond:
            for a in actions:
                if a in _NO_PREFIX_CONDITION:
                    bad.append(
                        f"{sid}: {a} 是桶级动作，请求里没有 s3:prefix，"
                        f"这条条件恒为假＝这个动作其实没授权"
                        f"（Athena 会报 Unable to verify/create output bucket）")
    return bad


# ---------------------------------------------------------------- LF 资源形状

def db_resource(cid: str) -> dict:
    return {"Database": {"CatalogId": cid, "Name": NAMESPACE}}


def table_resource(cid: str, name: str, excluded: tuple[str, ...] = ()) -> dict:
    """列级授权的资源形状。

    `ColumnWildcard: {}` = 所有列；带 `ExcludedColumnNames` = 除这些列以外的所有列。
    **不用 `Table` + `TableWildcard`**：那是表级通配，会盖掉列级排除。
    """
    wildcard: dict = {"ExcludedColumnNames": sorted(excluded)} if excluded else {}
    return {"TableWithColumns": {"CatalogId": cid, "DatabaseName": NAMESPACE,
                                 "Name": name, "ColumnWildcard": wildcard}}


def declared_table_grants(cid: str, tables: list[str]) -> list[tuple[str, dict]]:
    """按策略算出「应该有」的每表授权。DENY_TABLES 不出现在结果里。"""
    out = []
    for t in sorted(tables):
        if t in DENY_TABLES:
            continue
        out.append((t, table_resource(cid, t, EXCLUDE_COLUMNS.get(t, ()))))
    return out


@dataclass
class Grant:
    """从 LF 读回来的、发给某个 principal 的一条授权（按表聚合）。"""
    table: str
    perms: set = field(default_factory=set)
    excluded: set = field(default_factory=set)
    columns: set = field(default_factory=set)     # 显式列清单（我们不发这种）
    shapes: set = field(default_factory=set)      # TableWithColumns / Table / TableWildcard


def actual_grants(lf, cid: str, principal: str, tables: list[str]) -> dict[str, Grant]:
    """读 LF 里这个 principal 在本 namespace 上的实际授权，按表聚合。

    **必须逐表查**，一次 `TableWildcard` 查不出来。`list_permissions` 的
    `Resource` 是精确过滤器而不是范围过滤器：拿 `Table{TableWildcard:{}}` 去问，
    只会返回真的发在"表通配"这个资源上的授权，那 47 条 `TableWithColumns` 一条
    都不返回。第一版就是那么写的，后果不是报错而是 `actual` 恒为空——于是
    `--apply` 每次都显示"0 张已符合，47 张新发"，每次都在重发一遍已经存在的授权。
    幂等看起来是幂等的（LF grant 本身幂等），但"补齐"和"全量重发"分不出来，
    也就永远看不出有没有人在旁边改了授权面。

    代价是 N+1 次 API 调用（48 张表 ≈ 5 秒）。可以接受：这条路只在 --apply /
    --verify 上跑，不在查询热路径上。

    `tables` 要传 **namespace 里的全部表**，不是策略内的那些——`DENY_TABLES`
    上如果有授权，只有查了才知道。
    """
    out: dict[str, Grant] = {}

    def collect(resource: dict) -> None:
        kw = {"Principal": {"DataLakePrincipalIdentifier": principal},
              "Resource": resource}
        tok = None
        while True:
            if tok:
                kw["NextToken"] = tok
            try:
                r = lf.list_permissions(**kw)
            except ClientError as e:
                if _code(e) in ("EntityNotFoundException", "InvalidInputException"):
                    return
                raise
            for p in r.get("PrincipalResourcePermissions", []):
                if p.get("Principal", {}).get("DataLakePrincipalIdentifier") != principal:
                    continue
                res = p.get("Resource", {})
                if "TableWithColumns" in res:
                    t = res["TableWithColumns"]
                    name = t.get("Name") or "*"
                    g = out.setdefault(name, Grant(name))
                    g.shapes.add("TableWithColumns")
                    cw = t.get("ColumnWildcard")
                    if cw is None:
                        g.columns |= set(t.get("ColumnNames", []))
                    else:
                        g.excluded |= set(cw.get("ExcludedColumnNames", []))
                elif "Table" in res:
                    t = res["Table"]
                    wild = "TableWildcard" in t
                    name = "*" if wild else (t.get("Name") or "*")
                    g = out.setdefault(name, Grant(name))
                    g.shapes.add("TableWildcard" if wild else "Table")
                else:
                    continue
                g.perms |= set(p.get("Permissions", []))
            tok = r.get("NextToken")
            if not tok:
                return

    for t in tables:
        collect({"Table": {"CatalogId": cid, "DatabaseName": NAMESPACE, "Name": t}})
    # 表通配授权是"一步废掉全部列级排除"的那种改动，它只在这个形状下查得到。
    collect({"Table": {"CatalogId": cid, "DatabaseName": NAMESPACE,
                       "TableWildcard": {}}})
    return out


def namespace_tables(glue, cid: str) -> list[str]:
    out: list[str] = []
    tok = None
    while True:
        kw = {"CatalogId": cid, "DatabaseName": NAMESPACE, "MaxResults": 100}
        if tok:
            kw["NextToken"] = tok
        r = glue.get_tables(**kw)
        out += [t["Name"] for t in r.get("TableList", [])]
        tok = r.get("NextToken")
        if not tok:
            return sorted(out)


def glue_columns(glue, cid: str, table: str) -> list[str]:
    t = glue.get_table(CatalogId=cid, DatabaseName=NAMESPACE, Name=table)["Table"]
    return [c["Name"] for c in t.get("StorageDescriptor", {}).get("Columns", [])]


# ---------------------------------------------------------------- 建角色

def ensure_role(iam, acct: str, principals: list[str], apply: bool) -> tuple[int, bool]:
    """幂等地建/校验角色与内联策略。返回 (不符项数, 角色是否存在)。

    `principals` 先过 `validate_principals()`：`*` / `:root` / 非 IAM ARN 直接抛
    `TrustPrincipalError`，**--verify 也拦**（不是只在 --apply 时拦）。理由是
    `--verify` 的输出会被当成"这个角色的信任面是对的"来读，而它拿的是同一份列表。
    """
    principals = validate_principals(principals)
    want_trust = trust_policy(principals)
    want_doc = policy_document(acct)
    bad = 0

    try:
        got = iam.get_role(RoleName=ROLE_NAME)["Role"]
        exists = True
    except ClientError as e:
        if _code(e) != "NoSuchEntity":
            raise
        exists = False

    if not exists:
        if not apply:
            log(f"[1] ❌ 角色 {ROLE_NAME} 不存在（--apply 会建）")
            return 1, False
        iam.create_role(
            RoleName=ROLE_NAME, Description=ROLE_DESC,
            AssumeRolePolicyDocument=json.dumps(want_trust),
            MaxSessionDuration=3600,
            Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])
        log(f"[1] 建角色 {ROLE_NAME}（信任 {principals}）")
    else:
        tags = {t["Key"]: t["Value"] for t in
                iam.list_role_tags(RoleName=ROLE_NAME).get("Tags", [])}
        if tags.get(TAG_KEY) != TAG_VALUE:
            log(f"[1] ❌ 角色 {ROLE_NAME} 已存在，但没有 {TAG_KEY}={TAG_VALUE} 标签"
                f"——不是本项目建的，**不动它**。改名重试：AGENT_ROLE_NAME=… "
                f"（现有标签 {tags or '无'}）")
            return 1, True
        cur = got.get("AssumeRolePolicyDocument") or {}
        cur_p = unconditional_principals(cur)
        missing = sorted(set(principals) - cur_p)
        if missing and apply:
            # 合并而非重建，见 merge_trust_policy 的说明。
            merged = merge_trust_policy(cur, principals)
            iam.update_assume_role_policy(
                RoleName=ROLE_NAME, PolicyDocument=json.dumps(merged))
            log(f"[1] 角色 {ROLE_NAME} 已存在，信任列表补入 {missing}")
        elif missing:
            log(f"[1] ❌ 角色 {ROLE_NAME} 的信任列表缺 {missing}"
                f"（当前身份 assume 不了它）")
            bad += 1
        else:
            log(f"[1] 角色 {ROLE_NAME} 已存在，信任列表齐（{sorted(cur_p)}）")

    # 内联策略：逐字比对，不一致就报（--apply 覆盖）
    live = None
    try:
        live = iam.get_role_policy(RoleName=ROLE_NAME,
                                   PolicyName=POLICY_NAME)["PolicyDocument"]
    except ClientError as e:
        if _code(e) != "NoSuchEntity":
            raise
    if live == want_doc:
        log(f"[2] 内联策略 {POLICY_NAME} 与声明一致 ✅")
    elif apply:
        iam.put_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME,
                            PolicyDocument=json.dumps(want_doc))
        log(f"[2] 写入内联策略 {POLICY_NAME}"
            f"（{'新建' if live is None else '与声明不一致，已覆盖'}）")
    else:
        log(f"[2] ❌ 内联策略 {POLICY_NAME} "
            f"{'不存在' if live is None else '与声明不一致（有人在控制台改过？）'}")
        bad += 1

    # 托管策略一条都不该有：那是绕过上面这份逐字比对的口子
    att = iam.list_attached_role_policies(RoleName=ROLE_NAME).get(
        "AttachedPolicies", []) if (exists or apply) else []
    if att:
        log(f"[2] ❌ 角色上挂了托管策略 {[a['PolicyName'] for a in att]}"
            f"——本脚本只管内联策略，挂上来的东西它比不出来")
        bad += 1

    findings = policy_findings(want_doc)
    for f in findings:
        log(f"[2] ❌ 策略自审：{f}")
    return bad + len(findings), True


# ---------------------------------------------------------------- 发 LF 权限

def sync_lf(lf, glue, cid: str, principal: str, apply: bool,
            revoke_extra: bool) -> int:
    tables = namespace_tables(glue, cid)
    if not tables:
        log(f"[3] ❌ namespace {NAMESPACE} 里一张表都没读到"
            f"（catalog id 写错？要带账号前缀）")
        return 1
    declared = dict(declared_table_grants(cid, tables))
    actual = actual_grants(lf, cid, principal, tables)
    bad = 0

    # 3a database DESCRIBE
    res = db_resource(cid)
    got: set[str] = set()
    for p in lf.list_permissions(
            Principal={"DataLakePrincipalIdentifier": principal},
            Resource=res).get("PrincipalResourcePermissions", []):
        if p["Principal"]["DataLakePrincipalIdentifier"] == principal:
            got |= set(p.get("Permissions", []))
    missing = [p for p in DATABASE_PERMS if p not in got]
    if not missing:
        log(f"[3] database {NAMESPACE}: {sorted(got)} ✅")
    elif apply:
        lf.grant_permissions(Principal={"DataLakePrincipalIdentifier": principal},
                             Resource=res, Permissions=DATABASE_PERMS)
        log(f"[3] database {NAMESPACE}: 发 {DATABASE_PERMS}")
    else:
        log(f"[3] ❌ database {NAMESPACE} 缺 {missing}（现有 {sorted(got) or '无'}）")
        bad += 1

    # 3b 每表列级 SELECT
    added, okc, drift = 0, 0, []
    for t, res in declared.items():
        want_excl = set(EXCLUDE_COLUMNS.get(t, ()))
        g = actual.get(t)
        if g and "SELECT" in g.perms and g.excluded == want_excl and not g.columns:
            okc += 1
            continue
        if g and (g.excluded != want_excl or g.columns or g.shapes - {"TableWithColumns"}):
            drift.append(f"{t}: 已有授权与声明不同"
                         f"（排除 {sorted(g.excluded) or '无'}，声明 {sorted(want_excl) or '无'}"
                         f"{'，还有显式列清单' if g.columns else ''}"
                         f"{'，形状 ' + str(sorted(g.shapes)) if g.shapes - {'TableWithColumns'} else ''}）")
            continue
        if apply:
            lf.grant_permissions(
                Principal={"DataLakePrincipalIdentifier": principal},
                Resource=res, Permissions=TABLE_PERMS)
            added += 1
        else:
            drift.append(f"{t}: 缺 SELECT"
                         + (f"（排除 {sorted(want_excl)}）" if want_excl else ""))
    log(f"[4] 表级授权：{okc} 张已符合，{added} 张新发，{len(declared)} 张在策略内"
        f"（{len(tables)} 张总表 − {len(DENY_TABLES)} 张拒绝表）")
    for d in drift[:12]:
        log(f"      ❌ {d}")
    if len(drift) > 12:
        log(f"      … 另有 {len(drift) - 12} 条")
    bad += len(drift)

    if apply and added:
        # 发完立刻回读核对。这一步不是形式主义：读授权面的过滤器写错过一次，
        # 后果是 `actual` 恒为空 → 每次 --apply 都报"0 张已符合，N 张新发"，
        # 而 LF 的 grant 本身幂等，所以既不报错也不出错，只是**永远看不出**
        # 授权面有没有被别人改过。回读对不上就必须红。
        again = actual_grants(lf, cid, principal, tables)
        lost = [t for t in declared
                if not (again.get(t) and "SELECT" in again[t].perms
                        and again[t].excluded == set(EXCLUDE_COLUMNS.get(t, ())))]
        if lost:
            log(f"[4] ❌ 发完回读不到 {len(lost)} 张表的授权：{lost[:6]}"
                f"{' …' if len(lost) > 6 else ''}"
                f"\n      （grant 没生效，或者读授权面的过滤器和写的那条路对不上）")
            bad += len(lost)
        else:
            log(f"[4] 回读核对：{len(declared)} 张表的授权都读得回来 ✅")

    # 3c 多余授权：拒绝表、表通配、策略外的表
    extra = []
    for name, g in sorted(actual.items()):
        why = None
        if name in DENY_TABLES:
            why = "拒绝表却有授权"
        elif g.shapes & set(FORBIDDEN_GRANT_SHAPES):
            why = f"表通配授权（会盖掉列级排除）shapes={sorted(g.shapes)}"
        elif name not in declared and name != "*":
            why = "策略里没有这张表"
        if why:
            extra.append((name, g, why))
    for name, g, why in extra:
        log(f"[5] ❌ 多余授权 {name}：{why}，权限 {sorted(g.perms)}")
    if extra and revoke_extra and apply:
        for name, g, _ in extra:
            if "TableWildcard" in g.shapes or name == "*":
                res = {"Table": {"CatalogId": cid, "DatabaseName": NAMESPACE,
                                 "TableWildcard": {}}}
            elif "Table" in g.shapes:
                res = {"Table": {"CatalogId": cid, "DatabaseName": NAMESPACE,
                                 "Name": name}}
            else:
                res = table_resource(cid, name, tuple(sorted(g.excluded)))
            lf.revoke_permissions(
                Principal={"DataLakePrincipalIdentifier": principal},
                Resource=res, Permissions=sorted(g.perms))
            log(f"[5] 已撤 {name} {sorted(g.perms)}")
        extra = []
    elif extra:
        log("[5] （只报告不撤。要撤：--apply --revoke-extra，"
            "只会碰发给这个专属角色的授权）")
    bad += len(extra)

    # 3d 排除的列在 Glue 里真的存在吗（打错字＝排除了个不存在的列，看起来还是绿的）
    for t, cols in sorted(EXCLUDE_COLUMNS.items()):
        if t not in tables:
            log(f"[6] ❌ EXCLUDE_COLUMNS 写了表 {t}，但 namespace 里没有")
            bad += 1
            continue
        have = set(glue_columns(glue, cid, t))
        gone = [c for c in cols if c not in have]
        if gone:
            log(f"[6] ❌ {t} 声明排除 {gone}，但 Glue 里没有这些列"
                f"——排除了个不存在的列，等于没排除")
            bad += 1
        else:
            log(f"[6] {t} 排除列 {list(cols)} 在 Glue 里都存在 ✅")
    return bad


# ---------------------------------------------------------------- 探针

@dataclass
class Probe:
    label: str
    sql: str
    want_ok: bool
    catalog: str | None = None
    database: str | None = None


def build_probes() -> list[Probe]:
    ps: list[Probe] = []
    for t in MUST_READ_TABLES:
        ps.append(Probe(f"读得到 {t}", f"SELECT count(*) FROM {t}", True))
    for t, c in MUST_READ_COLUMNS:
        ps.append(Probe(f"读得到 {t}.{c}", f"SELECT {c} FROM {t} LIMIT 1", True))
    for t in MUST_NOT_READ_TABLES:
        ps.append(Probe(f"读不到 {t}", f"SELECT count(*) FROM {t}", False))
    for t, c in MUST_NOT_READ_COLUMNS:
        ps.append(Probe(f"读不到 {t}.{c}", f"SELECT {c} FROM {t} LIMIT 1", False))
    for t in MUST_NOT_READ_META:
        # 表名带 `$`，必须双引号；不带的话 Trino 报语法错，那种红不算过
        ps.append(Probe(f"读不到元数据表 {t}", f'SELECT * FROM "{t}" LIMIT 1', False))
    for t, c in MUST_NOT_READ_RAW:
        ps.append(Probe(f"读不到明文副本 {RAW_GLUE_DB}.{t}.{c}",
                        f"SELECT {c} FROM {t} LIMIT 1", False,
                        catalog="awsdatacatalog", database=RAW_GLUE_DB))
    return ps


def run_probes(cl, probes: list[Probe]) -> int:
    bad = 0
    for p in probes:
        try:
            cl.execute(p.sql, timeout=120, catalog=p.catalog, database=p.database)
            ok, err = True, ""
        except Exception as e:            # athena.py 把 FAILED 也翻成异常
            ok, err = False, _msg(e)
        if ok and p.want_ok:
            log(f"      {p.label} ✅")
        elif ok and not p.want_ok:
            log(f"      ❌ {p.label} —— 查询成功了，说明这道边界没生效")
            bad += 1
        elif not ok and p.want_ok:
            log(f"      ❌ {p.label} —— 本该能读却失败：{err}")
            bad += 1
        elif is_denial(err):
            log(f"      {p.label} ✅（{err[:60]}…）")
        else:
            log(f"      ❌ {p.label} —— 失败了，但原因不像权限/列不存在，"
                f"换个原因红了不算过：{err}")
            bad += 1
    return bad


def probe_shape(cl) -> int:
    """两条形状断言：`SELECT *` 与 information_schema 里都不该出现被排除的列。

    单查 `SELECT email` 报错只证明"点名要不给"；`SELECT *` 是 agent 真会写的形态，
    它要是把 email 带出来，前面所有断言都白做。
    """
    bad = 0
    for t, cols in sorted(EXCLUDE_COLUMNS.items()):
        try:
            res = cl.execute(f"SELECT * FROM {t} LIMIT 1", timeout=120)
        except Exception as e:
            log(f"      ❌ SELECT * FROM {t} 失败：{_msg(e)}")
            bad += 1
            continue
        got = [c for c in res.get("columns", []) if c in set(cols)]
        if got:
            log(f"      ❌ SELECT * FROM {t} 里还有 {got}")
            bad += 1
        else:
            log(f"      SELECT * FROM {t} 的 {len(res.get('columns', []))} 列"
                f"里没有 {list(cols)} ✅")

        sql = ("SELECT column_name FROM information_schema.columns "
               f"WHERE table_schema = '{NAMESPACE}' AND table_name = '{t}'")
        try:
            res = cl.execute(sql, timeout=120)
        except Exception as e:
            log(f"      note: information_schema 查不了（{_msg(e)[:80]}），跳过这条")
            continue
        names = {r[0] for r in res.get("rows", [])}
        leaked = sorted(set(cols) & names)
        if leaked:
            log(f"      ❌ information_schema 里 {t} 还列着 {leaked}"
                f"——agent 会照它写 SQL，然后每次都撞权限错")
            bad += 1
        else:
            log(f"      information_schema 里 {t} 不含 {list(cols)} ✅")
    return bad


def _varint(b: bytes, i: int) -> tuple[int, int]:
    """avro 的 zigzag varint。返回 (值, 新下标)。"""
    val = shift = 0
    while True:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return (val >> 1) ^ -(val & 1), i
        shift += 7


def avro_paths(raw: bytes, suffix: str) -> list[str]:
    """从 avro 容器文件里捞出以 `suffix` 结尾的 s3:// 路径。

    刻意**不**按 schema 反序列化，因此不需要 fastavro 这个依赖：avro 的 string
    是"varint 长度 + UTF-8 原文"，块解压之后 `s3://…` 就是明文，正则够用。
    要做的只有把容器拆对——头（magic + metadata map + 16 字节 sync marker）之后
    是若干 (记录数, 字节数, 数据, sync) 块，deflate 编码的块是裸 deflate 流。

    上一版偷懒：直接在整个文件上试 `zlib.decompressobj(-15)` 找起点，结果解出来
    的字节里一个路径都没有，差点得出"走不到数据文件"这个反向结论。少写这 20 行
    的代价是一个方向完全相反的错误结论。
    """
    if not raw.startswith(b"Obj\x01"):
        return []
    i = 4
    codec = b""
    n, i = _varint(raw, i)
    while n:
        if n < 0:                       # 负数：后面跟着块字节数，然后是 |n| 个键值
            _, i = _varint(raw, i)
            n = -n
        for _ in range(n):
            ln, i = _varint(raw, i)
            key = raw[i:i + ln]
            i += ln
            ln, i = _varint(raw, i)
            if key == b"avro.codec":
                codec = raw[i:i + ln]
            i += ln
        n, i = _varint(raw, i)
    i += 16                             # sync marker

    out: list[str] = []
    pat = re.compile(rb"s3://[\x20-\x7e]+?" + re.escape(suffix.encode()))
    while i < len(raw):
        _, i = _varint(raw, i)          # 记录数
        nbytes, i = _varint(raw, i)
        blk = raw[i:i + nbytes]
        i += nbytes + 16                # 块数据 + sync marker
        if codec == b"deflate":
            blk = zlib.decompressobj(-15).decompress(blk)
        elif codec not in (b"", b"null"):
            return []                   # snappy/zstd：不硬解，交给调用方报"测不了"
        out += [m.decode() for m in pat.findall(blk)]
    return sorted(set(out))


def probe_direct_read(sess, acct: str) -> int:
    """**已知旁路的固化断言**：拿角色凭证直读底层 Parquet，PII 值应当可见。

    方向和这个文件里其它探针相反，所以先说清为什么：

    `s3tables:GetTableData` 是查询跑通的必要条件（S3 Tables 的表桶没法注册成 LF
    的数据湖位置，没有 vended credentials 这条路，Glue 只能拿调用方身份去取元数据），
    而它同时让持有凭证的进程可以绕过 Athena 直读数据文件。也就是说 LF 的列级排除
    是**查询引擎层**的控制，不是凭证级隔离。

    这件事写在文档里是不够的——文档不会在环境变化时报错。所以这里反过来断言：
    直读**应该成功**。它变红意味着旁路被堵上了（AWS 支持了表桶注册、或者策略被
    收窄），那时该做的是**更新文档把列级排除升级成真边界**，而不是改这个探针。
    一个"这里其实防不住"的结论，值得和"这里防得住"的结论一样被 CI 盯着。
    """
    t = next(iter(sorted(EXCLUDE_COLUMNS)))
    cols = EXCLUDE_COLUMNS[t]
    barn = f"arn:aws:s3tables:{REGION}:{acct}:bucket/{TABLE_BUCKET}"
    s3t = sess.client("s3tables", region_name=REGION)
    s3 = sess.client("s3", region_name=REGION)

    def denied(step: str, e: Exception) -> int:
        if is_denial(_msg(e)):
            log(f"      ❌ {step} 被拒 —— 已知旁路似乎被堵上了。"
                f"这是好事，但文档里"
                f"「列级排除只在查询引擎层生效」那段话现在是错的，去改文档。")
        else:
            log(f"      ❌ {step} 失败，但原因不像权限：{_msg(e)[:140]}")
        return 1

    try:
        loc = s3t.get_table_metadata_location(
            tableBucketARN=barn, namespace=NAMESPACE, name=t)["metadataLocation"]
    except Exception as e:                              # noqa: BLE001
        return denied(f"直读 {t} 的元数据位置", e)

    bucket = urllib.parse.urlparse(loc).netloc

    def get(uri: str) -> bytes:
        key = urllib.parse.urlparse(uri).path.lstrip("/")
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    try:
        meta = json.loads(get(loc))
    except Exception as e:                              # noqa: BLE001
        return denied(f"直读 {t} 的 metadata JSON", e)

    fields = {f["name"] for s in meta.get("schemas", []) for f in s.get("fields", [])}
    seen = sorted(set(cols) & fields)
    log(f"      直读 {t} 的 metadata JSON：{len(fields)} 个字段，"
        f"含被排除的 {seen or '（无）'} "
        f"{'✅（旁路成立）' if seen else '—— 列名没泄露'}")

    snaps = meta.get("snapshots") or []
    if not snaps:
        log(f"      note: {t} 没有 snapshot，走不到数据文件，这条只测到元数据层")
        return 0
    mans = avro_paths(get(snaps[-1]["manifest-list"]), ".avro")
    pq = [p for m in mans for p in avro_paths(get(m), ".parquet")]
    if not pq:
        log(f"      note: manifest 链没解出数据文件（{len(mans)} 个 manifest），"
            f"这条只测到元数据层")
        return 0

    data = get(pq[0])
    hit = [c for c in cols if c.encode() in data]
    n_mail = len(set(re.findall(rb"[\w.%+-]+@[\w.-]+\.[a-zA-Z]{2,}", data)))
    log(f"      直读数据文件 {len(data)} 字节：footer 里有 {hit}，"
        f"邮箱明文 {n_mail} 个 "
        f"✅（LF 列级排除确实拦不住持凭证直读——这是记录在案的边界，不是回归）")
    return 0


def probe_staging_isolation(sess) -> int:
    """结果集这道边界的**云上**那一半：agent 角色读不到管理侧查询的结果对象。

    为什么需要它，而不是只有 `policy_findings` 那道离线判据：离线那道审的是**本脚本
    声明的策略文本**。角色上真正挂着的那份可能不是它——有人在控制台点过、`--apply`
    没跑、或者另一条 attached policy 补了一条更宽的 GetObject。策略文本对了而实际
    没对，症状是零：查询照样跑、探针照样绿。

    三件事各查一遍，因为它们坏的方式不同：

    1. **列不出**管理侧前缀（ListBucket 的 s3:prefix 条件）。少了这条，对象键名
       （里面带 QueryExecutionId）就能被枚举出来，接下来直接 GetObject 试。
    2. **读不到**管理侧前缀下的对象（GetObject 的 Resource）。这是明文那一跳：
       管理侧跑过的 `SELECT email FROM users LIMIT 1` 的结果 CSV 就在那儿。
    3. **写不进**管理侧前缀（PutObject 的 Resource）。方向反过来，但同样要挡：
       能覆盖管理侧的结果对象，就能让读结果回来比数的对账脚本拿到伪造的数字。
       这一条**刻意不真写**——真写成功就污染了共享前缀。用一个不存在的键发
       PutObject 并要求它被拒；被拒说明没权限，那也就写不进任何键。

    注意 `csv/` 那条旁路由 build_probes() 里的 MUST_NOT_READ_RAW 探针盯着（走 Athena
    查 `*_csv` 外部表），和这里不重叠：那条查的是"能不能用 SQL 读明文副本"，
    这条查的是"能不能绕过 SQL 直接取 S3 对象"。
    """
    s3 = sess.client("s3", region_name=REGION)
    bad = 0

    def want_denied(label: str, fn) -> None:
        nonlocal bad
        try:
            fn()
        except ClientError as e:
            code = _code(e)
            if code in ("AccessDenied", "AccessDeniedException", "403"):
                log(f"      {label} ✅（{code}）")
            elif code in ("NoSuchKey", "NoSuchBucket"):
                # **不算过**：键不存在时 S3 对有权限的调用方就是报 NoSuchKey，
                # 所以这个结果分不出"没权限"和"有权限但键恰好不在"。
                log(f"      ❌ {label} —— 报的是 {code}，不是 AccessDenied。"
                    f"这条分不出「没权限」和「有权限但键不在」，不算过。")
                bad += 1
            else:
                log(f"      ❌ {label} —— 失败了，但原因不像权限：{code} {_msg(e)[:120]}")
                bad += 1
        else:
            log(f"      ❌ {label} —— 成功了，说明这道边界没生效")
            bad += 1

    # 1) 列管理侧前缀。ListBucket 有权限时返回 200（哪怕前缀下一个对象都没有），
    #    所以这条不会出现上面那种"分不出"的情况。
    want_denied(
        f"列不出管理侧结果前缀 {STAGING_PREFIX}",
        lambda: s3.list_objects_v2(Bucket=RAW_BUCKET, Prefix=STAGING_PREFIX,
                                   MaxKeys=1))

    # 2) 读管理侧前缀下的对象。先用当前身份（管理侧）**找一个真实存在的键**，
    #    否则拿不到能区分 AccessDenied / NoSuchKey 的证据。找不到就说明测不了，
    #    如实报 note 而不是记成通过。
    admin_key = None
    try:
        me = boto3.client("s3", region_name=REGION)
        for page in me.get_paginator("list_objects_v2").paginate(
                Bucket=RAW_BUCKET, Prefix=STAGING_PREFIX,
                PaginationConfig={"MaxItems": 200}):
            for o in page.get("Contents", []):
                # 只挑**不在** agent 子前缀下的键：agent 自己那些是它该读的。
                if not o["Key"].startswith(AGENT_STAGING_PREFIX):
                    admin_key = o["Key"]
                    break
            if admin_key:
                break
    except ClientError as e:
        log(f"      note: 当前身份也列不了 {STAGING_PREFIX}（{_code(e)}），"
            f"这条测不了")

    if admin_key:
        want_denied(
            f"读不到管理侧的结果对象 {admin_key.rsplit('/', 1)[-1][:40]}",
            lambda: s3.get_object(Bucket=RAW_BUCKET, Key=admin_key))
    else:
        log(f"      note: {STAGING_PREFIX} 下没有 agent 子前缀之外的对象，"
            f"「读不到管理侧结果」这条**没测到**（跑一条管理侧查询再来）")

    # 3) 写不进管理侧前缀。用一个明显不存在、也不打算存在的键。
    want_denied(
        f"写不进管理侧结果前缀 {STAGING_PREFIX}",
        lambda: s3.put_object(Bucket=RAW_BUCKET,
                              Key=f"{STAGING_PREFIX}.governance-probe-should-fail",
                              Body=b""))
    return bad


# ---------------------------------------------------------------- 后端接线

def verify_backend(acct: str) -> int:
    """后端真的在用这个角色吗。

    这是最可能悄悄坏掉的一环：角色建好了、LF 权限也发了、`--verify` 全绿，
    而 `backend/db.py` 因为一行改动仍旧用 admin 凭证在查 —— 所有治理都在，
    只是没接上。前面那批探针发现不了它，因为它们自己 assume 角色。
    """
    arn = role_arn(acct)
    os.environ["AGENT_ROLE_ARN"] = arn
    os.environ.setdefault("DB_BACKEND", "athena")
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import db                                    # noqa: E402  要在设完环境变量之后

    bad = 0
    info = db.backend_info()
    ident = info.get("identity", "")
    if ROLE_NAME in ident:
        log(f"[B1] backend_info().identity = {ident} ✅")
    else:
        log(f"[B1] ❌ backend_info().identity = {ident!r}，认不出 {ROLE_NAME}"
            f"——后端没在用这个角色（db.py 的 AGENT_ROLE_ARN 分支没走到？）")
        bad += 1

    try:
        r = db._run_query_athena("SELECT count(*) AS n FROM users")
        log(f"[B2] 后端查 users 拿到 {r['rows'][0][0]} 行 ✅")
    except Exception as e:
        log(f"[B2] ❌ 后端连 users 都查不了：{_msg(e)}")
        bad += 1

    for t, c in MUST_NOT_READ_COLUMNS[:1] + tuple(
            (t, "count(*)") for t in MUST_NOT_READ_TABLES):
        sql = f"SELECT {c} FROM {t}" + ("" if c.startswith("count") else " LIMIT 1")
        try:
            db._run_query_athena(sql)
            log(f"[B3] ❌ 后端读到了 {t}.{c} —— 用的不是受限凭证")
            bad += 1
        except Exception as e:
            if is_denial(_msg(e)):
                log(f"[B3] 后端读不到 {t}.{c} ✅")
            else:
                log(f"[B3] ❌ {t}.{c} 失败原因不对：{_msg(e)}")
                bad += 1
    return bad


# ---------------------------------------------------------------- 自测（无云）

def selftest() -> int:
    bad = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal bad
        if not cond:
            log(f"❌ {msg}")
            bad += 1

    # 1) 两份清单互相覆盖 —— 分成两份的意义全在这一段
    declared_cols = {(t, c) for t, cs in EXCLUDE_COLUMNS.items() for c in cs}
    check(declared_cols == set(MUST_NOT_READ_COLUMNS),
          f"策略与契约不一致：EXCLUDE_COLUMNS={sorted(declared_cols)} "
          f"vs MUST_NOT_READ_COLUMNS={sorted(MUST_NOT_READ_COLUMNS)}")
    check(set(DENY_TABLES) == set(MUST_NOT_READ_TABLES),
          f"DENY_TABLES={sorted(DENY_TABLES)} 与 "
          f"MUST_NOT_READ_TABLES={sorted(MUST_NOT_READ_TABLES)} 不一致")
    check(not (set(MUST_READ_TABLES) & set(DENY_TABLES)),
          "同一张表既要求读得到又要求读不到")
    check(not (set(MUST_READ_COLUMNS) & set(MUST_NOT_READ_COLUMNS)),
          "同一列既要求读得到又要求读不到")
    # 列级断言必须有同表的表级正对照，否则"读不到 email"可能只是整张表读不到
    for t, _ in MUST_NOT_READ_COLUMNS:
        check(t in MUST_READ_TABLES,
              f"{t} 有列级排除断言，但没有 count(*) 正对照——"
              f"整张表读不到时那条断言会假绿")
    check(bool(MUST_NOT_READ_RAW), "少了 CSV 明文旁路的断言（见 docstring）")

    # 2) 声明排除的列在 v1 DDL 真源里真的存在（打错字＝排除了个不存在的列）
    try:
        import gen_ddl
        src = {t: [c for c, *_ in cols] for _, t, cols in gen_ddl.parse_source()}
    except Exception as e:                        # noqa: BLE001
        log(f"note: 读不到 v1 DDL 真源（{_msg(e)}），跳过列存在性自测")
        src = {}
    if src:
        for t, cols in sorted(EXCLUDE_COLUMNS.items()):
            check(t in src, f"EXCLUDE_COLUMNS 的表 {t} 不在 v1 DDL 真源里")
            for c in cols:
                check(c in src.get(t, []),
                      f"{t}.{c} 不在 v1 DDL 真源的列里（拼错了？排除不存在的列＝没排除）")
        for t in DENY_TABLES:
            check(t in src, f"DENY_TABLES 的表 {t} 不在 v1 DDL 真源里")
        for t, c in MUST_READ_COLUMNS:
            check(c in src.get(t, []), f"MUST_READ_COLUMNS 的 {t}.{c} 不在真源里")

    # 3) 策略够窄，且这套审查真的能抓东西（自带正对照）
    doc = policy_document("000000000000")
    f = policy_findings(doc)
    check(not f, f"声明的策略自审就不过：{f}")

    loosened = json.loads(json.dumps(doc))
    loosened["Statement"].append({
        "Sid": "Hole", "Effect": "Allow", "Action": ["s3:GetObject"],
        "Resource": f"arn:aws:s3:::{RAW_BUCKET}/csv/*"})
    check(any("csv" in x for x in policy_findings(loosened)),
          "策略审查抓不到「S3 读扩到 csv/」这个洞——那它挡不住最要紧的旁路")

    # s3tables 写数据面：动作名前缀是 `s3tables:` 而不是 `s3:`，上面那条 S3 前缀
    # 规则天然判不到，所以必须单独有正对照。
    for act in sorted(_S3TABLES_DATA_PLANE):
        bypass = json.loads(json.dumps(doc))
        bypass["Statement"].append({
            "Sid": "Bypass", "Effect": "Allow", "Action": [act],
            "Resource": f"arn:aws:s3tables:{REGION}:000000000000:bucket/{TABLE_BUCKET}/*"})
        check(any("绕开 LF" in x for x in policy_findings(bypass)),
              f"策略审查抓不到 {act}——只读角色拿到它就能改数据")

    # 反向正对照：读数据面（`GetTableData`）**不该**被报成 drift。它是查询跑通的
    # 必要条件（见 `_S3TABLES_REQUIRED`），一旦有人把它加回 drift 清单，`--verify`
    # 就永远红，接着就会有人把整个策略审查关掉。同时钉住它必须真在策略里——
    # 少了它的症状是 Athena 报 TABLE_NOT_FOUND，指向"表不存在"，极难往权限想。
    for act in sorted(_S3TABLES_REQUIRED):
        check(act not in _S3TABLES_DATA_PLANE,
              f"{act} 同时在必需集和 drift 集里——策略审查会和现实永久打架")
        check(any(act in json.dumps(st.get("Action", []))
                  for st in doc["Statement"]),
              f"策略里没有 {act}——目录解析会整体失败（表现为 TABLE_NOT_FOUND）")

    # 资源通配：现在一条都不该有，审查器也要抓得到。
    check('"*"' not in json.dumps([st.get("Resource") for st in doc["Statement"]]),
          '策略里出现了 Resource: "*"——没有任何动作还需要它')
    check(bool(policy_findings({"Statement": [
        {"Sid": "Wild", "Effect": "Allow", "Action": ["glue:GetTable"],
         "Resource": "*"}]})), '策略审查抓不到 Resource: "*"')
    # 钉住 `lakeformation:GetDataAccess` 别飘回来：它是各种示例策略里的标配，
    # 直觉上"必需"，实测（去掉它跑探针）完全不需要。
    check("GetDataAccess" not in json.dumps(doc),
          "lakeformation:GetDataAccess 又回来了——实测不需要，"
          "给了只会多一条 Resource:* 并让人误判数据面是 LF 在守")

    # `glue:GetUnfiltered*`：排查时加过，实测无效（账号 AllowExternalDataFiltering
    # 为 false，且 Athena 是 first-party 不走这条路）。钉住它别再飘回来——
    # 它不产生效果，只让策略看起来比实际更宽。
    check("Unfiltered" not in json.dumps(doc),
          "策略里又出现了 glue:GetUnfiltered*——它不解决任何问题，只放宽了授权面")
    for hole in ({"Sid": "W", "Effect": "Allow", "Action": ["glue:DeleteTable"],
                  "Resource": "*"},
                 {"Sid": "W2", "Effect": "Allow", "Action": ["athena:*"],
                  "Resource": "*"}):
        check(bool(policy_findings({"Statement": [hole]})),
              f"策略审查抓不到 {hole['Action']}")

    # 正对照：把桶级动作塞回带 prefix 条件的语句里（这就是第一版的写法，
    # 症状是探针全红而策略自审全绿）。方向是"太窄"，也必须抓到。
    too_narrow = {"Sid": "N", "Effect": "Allow",
                  "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                  "Resource": f"arn:aws:s3:::{RAW_BUCKET}",
                  "Condition": {"StringLike": {
                      "s3:prefix": [f"{STAGING_PREFIX}*"]}}}
    check(any("恒为假" in x for x in policy_findings({"Statement": [too_narrow]})),
          "策略审查抓不到「桶级动作挂了 s3:prefix 条件」——那它对静默没授权是瞎的")

    # 3b) 结果集这道边界（B3）。列级排除挡的是"查得到吗"，挡不了"结果放哪儿"：
    # workgroup 的 OutputLocation 决定结果 CSV 落到哪个 S3 前缀，而结果 CSV 是明文
    # 行数据。共用一个前缀时 agent 能从管理侧的结果文件里把 email 读回来
    # ——`--verify` 自己每次都会跑一遍 `SELECT email FROM users LIMIT 1`，
    # 那一行明文就落在那儿。这一组断言是这道边界的**离线**闸。
    check(WORKGROUP != ADMIN_WORKGROUP,
          f"agent 和管理侧共用 workgroup {WORKGROUP} —— 结果集落在同一个前缀下")
    check(AGENT_STAGING_PREFIX.startswith(STAGING_PREFIX)
          and AGENT_STAGING_PREFIX != STAGING_PREFIX,
          f"{AGENT_STAGING_PREFIX!r} 不是 {STAGING_PREFIX!r} 的真子前缀")
    check(AGENT_STAGING_PREFIX.endswith("/"),
          f"{AGENT_STAGING_PREFIX!r} 没以 / 结尾，前缀条件会匹配到兄弟前缀"
          f"（athena-staging/agentXXX/）")
    check(f"{STAGING_PREFIX}*" not in json.dumps(doc),
          f"策略里还出现了共享前缀 {STAGING_PREFIX}* —— 那是 B3 那个洞")
    check(ADMIN_WORKGROUP not in json.dumps(doc),
          f"策略里出现了管理侧 workgroup {ADMIN_WORKGROUP}")
    # 三个正对照，一条一个退化方向。**都是"改回原来的写法"**，也就是这道边界
    # 真正会被怎么弄坏：有人为了让某条查询跑通，把前缀从 agent/ 退回 athena-staging/。
    widened_obj = json.loads(json.dumps(doc))
    for st in widened_obj["Statement"]:
        if st.get("Sid") == "AthenaStagingObjects":
            st["Resource"] = f"arn:aws:s3:::{RAW_BUCKET}/{STAGING_PREFIX}*"
    check(any("共享前缀" in x for x in policy_findings(widened_obj)),
          "策略审查抓不到「S3 对象授权退回共享 staging 前缀」"
          "——那管理侧查询结果里的明文 agent 就能读")

    widened_list = json.loads(json.dumps(doc))
    for st in widened_list["Statement"]:
        if st.get("Sid") == "AthenaStagingList":
            st["Condition"] = {"StringLike": {"s3:prefix": [f"{STAGING_PREFIX}*"]}}
    check(any("s3:prefix" in x for x in policy_findings(widened_list)),
          "策略审查抓不到「ListBucket 的前缀条件退回共享前缀」"
          "——Resource 收窄了也没用，键名里带 QueryExecutionId，列到就能试着取")

    wrong_wg = json.loads(json.dumps(doc))
    for st in wrong_wg["Statement"]:
        if st.get("Sid") == "AthenaQuery":
            st["Resource"] = [r.replace(f"workgroup/{WORKGROUP}",
                                        f"workgroup/{ADMIN_WORKGROUP}")
                              for r in st["Resource"]]
    check(any("管理侧 workgroup" in x for x in policy_findings(wrong_wg)),
          "策略审查抓不到「授权到管理侧 workgroup」——OutputLocation 挂在 workgroup 上，"
          "给了它结果就又落回共享前缀")

    # 4) LF 资源形状：列级排除不能退化成表通配
    r = table_resource("acct:cat/bucket", "users", ("email", "phone"))
    check("TableWithColumns" in r, "列级授权的资源形状写错了")
    check(r["TableWithColumns"]["ColumnWildcard"]["ExcludedColumnNames"]
          == ["email", "phone"], "ExcludedColumnNames 没排序/没带上")
    check("TableWildcard" not in json.dumps(r),
          "列级授权里出现了 TableWildcard —— 会盖掉排除")
    r2 = table_resource("acct:cat/bucket", "orders")
    check(r2["TableWithColumns"]["ColumnWildcard"] == {},
          "无排除时应是 ColumnWildcard: {}（全列）")
    check("SELECT" in TABLE_PERMS and not (set(TABLE_PERMS) & {
        "INSERT", "DELETE", "ALTER", "DROP"}),
        f"表权限不该带写权限：{TABLE_PERMS}")
    check(not (set(DATABASE_PERMS) & {"CREATE_TABLE", "ALTER", "DROP"}),
          f"database 权限不该带 DDL：{DATABASE_PERMS}")

    # 5) 拒绝表不会出现在应发清单里
    tables = ["users", "user_messages", "orders"]
    got = [t for t, _ in declared_table_grants("acct:cat/bucket", tables)]
    check("user_messages" not in got, f"拒绝表混进了应发清单：{got}")
    check(set(got) == {"users", "orders"}, f"应发清单不对：{got}")

    # 6) 探针集合完整，且"该失败"的原因白名单认得真实报错
    ps = build_probes()
    check(sum(1 for p in ps if not p.want_ok)
          == len(MUST_NOT_READ_TABLES) + len(MUST_NOT_READ_COLUMNS)
          + len(MUST_NOT_READ_META) + len(MUST_NOT_READ_RAW),
          "负向探针数量与契约不符")
    check(any(p.database == RAW_GLUE_DB for p in ps), "少了 CSV 中转库那条探针")
    # 元数据表那几条：表名带 `$`，忘了双引号会红在语法错上——那种红不算过
    for p in ps:
        if any(t in p.label for t in MUST_NOT_READ_META):
            check('"' in p.sql, f"元数据表探针的表名没加双引号，会红在语法错上：{p.sql}")
    for real in ("COLUMN_NOT_FOUND: line 1:8: Column 'email' cannot be resolved",
                 "Insufficient Lake Formation permission(s) on user_messages",
                 "User: arn:… is not authorized to perform: athena:StartQueryExecution",
                 "AccessDeniedException"):
        check(is_denial(real), f"失败原因白名单认不出真实报错：{real}")
    for unrelated in ("SYNTAX_ERROR: line 1:1 mismatched input",
                      "Query exhausted resources at this scale factor"):
        check(not is_denial(unrelated),
              f"失败原因白名单太宽，把无关错误也当成"
              f"「权限挡住了」：{unrelated}")
    # 基础设施级失败：这些**实测出现过**，而且都被白名单误判成过「正确拒绝」。
    # 它们的共同点是查询没走到权限判定那一步，所以「红了」什么也证明不了。
    for infra in (
        "FAILED: CATALOG_NOT_FOUND: line 1:22: Catalog "
        "'s3tablescatalog/analytics-agent-tables' does not exist",
        "InvalidRequestException: Unable to verify/create output bucket "
        "analytics-agent-raw",
        "SCHEMA_NOT_FOUND: line 1:15: Schema 'app_analytics' does not exist",
        "ExpiredToken: The security token included in the request is expired",
        "ThrottlingException: Rate exceeded",
        # AssumeRole 被拒：这一轮根本没变成 agent 角色，探针用的是别的身份。
        # 它长得就是一条 AccessDenied，是这一类里最像"正确拒绝"的一个。
        "AccessDenied: User: arn:aws:iam::1234:user/dev is not authorized to "
        "perform: sts:AssumeRole on resource: arn:aws:iam::1234:role/"
        "analytics-agent-ro",
    ):
        check(not is_denial(infra),
              f"把基础设施级失败当成了「权限挡住了」——治理层没生效也会报绿：{infra[:70]}")

    # 6b) 手写的 avro 容器解析。probe_direct_read 全靠它才能不带依赖地走到数据文件，
    # 而它**已经错过一次**：第一版在整个文件上盲试 deflate，解出来一个路径都没有，
    # 于是差点得出"直读走不到数据"这个方向完全相反的结论。所以离线造一个容器来钉住。
    def _zz(n: int) -> bytes:
        n = (n << 1) ^ (n >> 63) if n < 0 else n << 1
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def _ocf(paths: list[str], codec: bytes) -> bytes:
        body = b"".join(_zz(len(p)) + p.encode() for p in paths)
        blk = zlib.compressobj(9, zlib.DEFLATED, -15) if codec == b"deflate" else None
        blk = (blk.compress(body) + blk.flush()) if blk else body
        head = b"Obj\x01" + _zz(1)
        head += _zz(len(b"avro.codec")) + b"avro.codec" + _zz(len(codec)) + codec
        head += _zz(0) + b"\x00" * 16
        return head + _zz(len(paths)) + _zz(len(blk)) + blk + b"\x00" * 16

    want = ["s3://b/metadata/snap-1.avro", "s3://b/metadata/2-manifest.avro"]
    for codec in (b"deflate", b"null"):
        got = avro_paths(_ocf(want, codec), ".avro")
        check(got == sorted(want), f"avro 容器（codec={codec.decode()}）解析不出路径：{got}")
    check(avro_paths(_ocf(["s3://b/data/x.parquet"], b"deflate"), ".avro") == [],
          "avro 路径提取没按后缀过滤——manifest 和数据文件会混在一起")
    check(avro_paths(b"not an avro file", ".avro") == [],
          "非 avro 输入应当返回空而不是抛异常")
    check(avro_paths(_ocf(want, b"snappy"), ".avro") == [],
          "遇到不支持的 codec 应返回空（让调用方报「测不了」），别当成「没有路径」")

    # 7) 常量拼法：Glue 侧 catalog id 带账号前缀，别跟 Athena 那个混了
    check(catalog_id("123456789012") == f"123456789012:{CATALOG_NAME}/{TABLE_BUCKET}",
          "Glue catalog id 拼错")
    check(":" not in athena.ATHENA_CATALOG, "Athena 侧 catalog 不该带账号前缀")
    check(role_arn("123456789012").endswith(f":role/{ROLE_NAME}"), "role ARN 拼错")

    # 8) 信任策略只放 sts:AssumeRole
    tp = trust_policy(["arn:aws:iam::1:role/a", "arn:aws:iam::1:role/a"])
    check(tp["Statement"][0]["Action"] == "sts:AssumeRole", "信任策略动作不对")
    check(tp["Statement"][0]["Principal"]["AWS"] == ["arn:aws:iam::1:role/a"],
          "信任策略没去重")

    # 8b) 信任策略是**合并**不是重建。这一组是回归测试：原来 --apply 会把现有文档
    #     拿模板重拼一份盖回去，Condition / Service principal / 第二条语句全丢。
    _dev, _exec = "arn:aws:iam::1:role/dev", "arn:aws:iam::1:role/exec"
    _live = {"Version": "2012-10-17", "Statement": [
        {"Sid": TRUST_SID, "Effect": "Allow", "Action": "sts:AssumeRole",
         "Principal": {"AWS": _dev}},
        {"Sid": "SomebodyElse", "Effect": "Allow", "Action": "sts:AssumeRole",
         "Principal": {"Service": "lambda.amazonaws.com"},
         "Condition": {"StringEquals": {"sts:ExternalId": "x"}}},
    ]}
    _m = merge_trust_policy(_live, [_dev, _exec])
    check(len(_m["Statement"]) == 2, "合并信任策略时语句数变了——别人的语句被吃掉了")
    check(_m["Statement"][0]["Principal"]["AWS"] == sorted([_dev, _exec]),
          "exec role 没并进我们自己那条（按 Sid 认领）")
    check(_m["Statement"][1] == _live["Statement"][1],
          "别人写的那条语句被改了：Condition / Service principal 必须原样留着")
    check(_live["Statement"][0]["Principal"]["AWS"] == _dev,
          "merge_trust_policy 改了入参——调用方手里的文档不该被就地修改")
    # 只有别人那条（带 Condition）的时候，要**追加**一条，而不是改他那条
    _m2 = merge_trust_policy({"Version": "2012-10-17",
                              "Statement": [_live["Statement"][1]]}, [_exec])
    check(len(_m2["Statement"]) == 2 and _m2["Statement"][1]["Sid"] == TRUST_SID,
          "认不到自己那条时应当追加一条无条件语句")
    # 带 Condition 的语句不算「已授权」：assume 不到却报绿是最坏的一种绿
    check(unconditional_principals(
        {"Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                        "Principal": {"AWS": _exec},
                        "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"}}}]}
    ) == set(), "带 Condition 的信任语句被当成了无条件授权")
    check(unconditional_principals(
        {"Statement": [{"Effect": "Deny", "Action": "sts:AssumeRole",
                        "Principal": {"AWS": _exec}}]}
    ) == set(), "Deny 语句里的 principal 被当成了授权")
    check(unconditional_principals(_live) == {_dev},
          "无条件 principal 集合算错")

    # 8c) 信任策略的 principal 入口校验：`*` / `:root` / 非 IAM ARN 一律拒
    def _rejects(p, why):
        try:
            validate_principals([_dev, p])
        except TrustPrincipalError:
            return
        check(False, why)

    _rejects("*", "`--trust '*'` 被放进了信任策略——那等于任何身份都能 assume 这个角色")
    _rejects("arn:aws:iam::123456789012:root",
             "`:root` 被放进了信任策略——那是整个账号，不是一个身份")
    _rejects("lambda.amazonaws.com", "服务 principal 溜进了 AWS principal 列表")
    _rejects("arn:aws:sts::123456789012:assumed-role/r/session",
             "assumed-role 会话 ARN 被当成身份——写进信任策略不生效")
    check(validate_principals([_dev, "", _dev]) == [_dev],
          "principal 列表没去重/没滤掉空串")
    try:
        validate_principals([])
        check(False, "空 principal 列表应当被拒（角色会没人能 assume）")
    except TrustPrincipalError:
        pass

    if bad:
        log(f"\n{bad} 项自测不过 ❌")
        return 1
    log(f"治理层自测通过 ✅（{len(EXCLUDE_COLUMNS)} 张表排除 "
        f"{len(declared_cols)} 列，{len(DENY_TABLES)} 张表整表不授权，"
        f"{len(build_probes())} 条探针）")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="L4 治理层：agent 最小权限角色 + Lake Formation 列级授权")
    ap.add_argument("--selftest", action="store_true", help="自测（无云依赖）")
    ap.add_argument("--verify", action="store_true",
                    help="只读核查 + 假扮 agent 角色实测（默认行为）")
    ap.add_argument("--apply", action="store_true",
                    help="建角色 / 补权限（幂等；不撤任何东西，除非加 --revoke-extra）")
    ap.add_argument("--revoke-extra", action="store_true",
                    help="连同撤掉发给这个专属角色的多余授权（只与 --apply 一起生效）")
    ap.add_argument("--as-caller", action="store_true",
                    help="用当前身份跑探针。**负测用**：那时「读不到」必须全红")
    ap.add_argument("--verify-backend", action="store_true",
                    help="后端是否真的在用这个角色查（导入 backend/db.py）")
    ap.add_argument("--no-probes", action="store_true", help="跳过实测探针（只看授权面）")
    ap.add_argument("--trust", action="append", default=[],
                    help="额外可 assume 这个角色的 principal（可重复，例如上云的执行角色）。"
                         "只接受具体的 arn:aws:iam::<账号>:role/… 或 :user/…；"
                         "'*' 与 ':root' 会被拒（见 validate_principals）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    sts = boto3.client("sts", region_name=REGION)
    iam = boto3.client("iam")
    acct = sts.get_caller_identity()["Account"]
    setup = _import_setup()
    me = setup.caller_role_arn(sts, iam) if setup else sts.get_caller_identity()["Arn"]
    arn = role_arn(acct)
    cid = catalog_id(acct)

    if a.verify_backend:
        log(f"账号 {acct}  区域 {REGION}  角色 {arn}\n模式：后端接线核查\n")
        bad = verify_backend(acct)
        log(f"\n{bad} 项不符 ❌" if bad else "\n后端确实在用受限角色查数 ✅")
        return 1 if bad else 0

    apply = a.apply
    mode = "创建/补齐" if apply else "只读核查"
    log(f"账号 {acct}  区域 {REGION}  当前身份 {me}\n"
        f"目标角色 {arn}\n模式：{mode}"
        f"{'（探针用当前身份跑 —— 负测模式）' if a.as_caller else ''}\n")

    try:
        bad, exists = ensure_role(iam, acct, [me] + a.trust, apply)
    except TrustPrincipalError as e:
        # 拒绝的理由要读得懂，不该是一段 traceback：这条路径最常见的触发方式是
        # 有人用 root 凭证跑 --apply，而那时他要的是"换个身份重跑"，不是栈帧。
        log(f"[1] ❌ {e}")
        return 1
    if not exists and not apply:
        log("\n治理层还没建：python3 scripts/lakehouse/governance.py --apply")
        return 1
    if bad and not exists:
        return 1

    lf = boto3.client("lakeformation", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)
    bad += sync_lf(lf, glue, cid, arn, apply, a.revoke_extra)

    if a.no_probes:
        log("\n（跳过探针：--no-probes。授权面齐不等于查得动，见 docstring）")
        return 1 if bad else 0

    log("\n[7] 实测探针"
        + ("（当前身份，负测模式）" if a.as_caller else f"（assume {ROLE_NAME}）"))
    # 这个 try/except 光包 assume_role_session() 是**不够**的：它返回的 Session 里
    # 凭证是**延迟获取**的，AssumeRole 那次调用要等到第一次用凭证才真的发出去。所以
    # 「信任策略里没有我」不会炸在这里，会炸成一条 Athena 查询失败——而 4 条负向探针
    # 恰好期待失败，`is_denial()` 又认得 `AccessDeniedException`，于是它们**假绿**：
    # 报告写着"读不到 user_messages ✅"，真相是这一轮根本没能变成 agent 角色。
    # 所以 assume 完立刻 get_caller_identity() 把凭证取一次，并核对拿到的确实是
    # 这个角色的会话。
    sess = None
    if not a.as_caller:
        try:
            sess = athena.assume_role_session(arn, REGION, SESSION_NAME)
            who = sess.client("sts", region_name=REGION).get_caller_identity()["Arn"]
        except ClientError as e:
            log(f"      ❌ assume 不了 {arn}：{_code(e)} {_msg(e)}"
                f"\n      （角色的信任策略里有 {me} 吗？--apply 会补）")
            return 1
        if ROLE_NAME not in who:
            log(f"      ❌ assume 之后的身份是 {who}，认不出 {ROLE_NAME}"
                f"——下面每一条探针都在用别的身份，绿了也不算数")
            return 1
        log(f"      身份已确认：{who} ✅")
    # 探针必须走 **agent 那个 workgroup**：它的 OutputLocation 是 agent 唯一有
    # GetObject 权限的前缀。用管理侧那个的话查询能提交、取结果时 AccessDenied，
    # 而报错指向结果桶，看不出是 workgroup 选错了。
    # `--as-caller` 下也用它：负测比的是同一批探针换个身份，workgroup 要一样。
    cl = athena.Client(session=sess, workgroup=WORKGROUP)
    bad += run_probes(cl, build_probes())
    bad += probe_shape(cl)
    if sess is None:
        log("      note: --as-caller 下跳过直读与结果集隔离探针（admin 当然读得到，"
            "证明不了任何关于 agent 角色的事）")
    else:
        bad += probe_direct_read(sess, acct)
        log("\n[8] 结果集隔离（agent 读不到管理侧查询的结果对象）")
        bad += probe_staging_isolation(sess)

    if bad:
        log(f"\n{bad} 项不符 ❌"
            + ("（--verify 只报告，不修；--apply 会补齐）" if not apply else ""))
        if a.as_caller:
            log("（--as-caller 下变红是**预期**：admin 身份就该读得到，"
                "说明探针真的在探）")
        return 1
    if a.as_caller:
        log("\n❌ --as-caller 全绿 —— 这些探针没有在探任何东西。"
            "admin 身份本该读到被排除的列和拒绝表。")
        return 1
    log("\n治理层就位 ✅"
        "\n（边界的准确说法：列级排除拦的是 agent 的 SQL 和它看到的目录，"
        "不隔离持有该角色凭证的进程——见 probe_direct_read）\n下一步："
        f"\n  AGENT_ROLE_ARN={arn} ./backend/run.sh    # 后端以受限角色查数"
        "\n  python3 scripts/lakehouse/governance.py --verify-backend")
    return 0


def _import_setup():
    """借 setup.py 的 ARN 规范化（SSO 角色带路径，手拼是错的）。"""
    try:
        import setup
        return setup
    except Exception:                             # noqa: BLE001
        return None


if __name__ == "__main__":
    raise SystemExit(main())
