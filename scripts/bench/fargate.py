#!/usr/bin/env python3
"""把三 arm harness 放到 Fargate 上跑 —— 建镜像、建角色、注册任务、跑并收日志。

## 为什么非要上云跑

DuckDB 是进程内引擎，它的耗时就是跑它那台机器的耗时。在笔记本上跑 DuckDB、
同时从笔记本调云上的 Athena 和 Redshift，量到的是三样不同的东西：本机算力、
一段跨区网络往返、以及引擎本身。只有把三条 arm 放进**同一个** Fargate 任务，
区域、网络位置、算力口径才是同一个，剩下的差异才只剩引擎。

正确性不需要上云（同一个引擎读同一批数据，换台机器不会换答案，本地已经全绿），
上云要的是**耗时可比**。所以镜像里没有 CSV 真源那 7.2G——云上跑 `--rows` 和计时，
`--full` 留在本地。

## 复用已有的集群，不新建

集群 `analytics-agent-relay` 已经在了，里面跑着 `ask-relay` 服务。这里只往里加一个
**任务定义**，用 `run-task` 一次性跑完就退出，不建 service、不碰 `ask-relay`。
理由是 ask-relay 背后挂着 CloudFront 域名，那个栈动不得。

## 三个 AWS 资源，都是新建且只服务这一件事

- ECR 仓库 `analytics-agent-bench`
- 任务角色 `analytics-agent-bench-task`——查数用的身份，权限按 arm 逐条给（见 `_policy()`）
- 执行角色 `analytics-agent-bench-exec`——只拉镜像、只写日志（AWS 托管策略）

任务角色**只有读权限**。这不是防御性写法：这套 harness 的全部工作就是读同一批数据
比对，任何写权限都只能用来破坏「三条 arm 读的是同一份数据」这个前提。

## IAM 之外还要一步 Lake Formation 授权

`s3tablescatalog` 这个联邦目录没有 `IAM_ALLOWED_PRINCIPALS` 兜底，所以光有 IAM 权限
查不了——每个主体都要显式的 LF 授权。本机跑得通只是因为开发者角色是 data lake admin。
`--setup` 里的 `ensure_lf_grants()` 补这一步，并且刻意**不**套治理层那套列级排除
（理由见那个函数的 docstring：对账要读全量，否则三条 arm 读的不是同一份数据）。

用法：

    python3 scripts/bench/fargate.py --selftest      # 离线自查，不碰 AWS
    python3 scripts/bench/fargate.py --setup         # 建 ECR / 两个角色 / LF 授权 / 任务定义
    python3 scripts/bench/fargate.py --build         # 构建并推镜像（要 docker）
    python3 scripts/bench/fargate.py --run           # 跑一次，跟日志到结束
    python3 scripts/bench/fargate.py --run -- --rows --arm duckdb --arm athena
    python3 scripts/bench/fargate.py --all           # setup + build + run

环境变量：`AWS_REGION`、`REDSHIFT_SECRET_ARN`（redshift arm 要）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
REGION = os.environ.get("AWS_REGION", "us-west-2")

CLUSTER = os.environ.get("BENCH_CLUSTER", "analytics-agent-relay")
FAMILY = "analytics-agent-bench"
REPO = "analytics-agent-bench"
TASK_ROLE = "analytics-agent-bench-task"
EXEC_ROLE = "analytics-agent-bench-exec"
LOG_GROUP = "/ecs/analytics-agent-bench"

# 4 vCPU / 16GB。DuckDB 的内存额度仍钉在 4GB（见 Dockerfile 的注释）：任务给 16GB
# 是为了留出 Python 侧和 Arrow 缓冲的余量，不是给 DuckDB 放大额度用的。
CPU = os.environ.get("BENCH_CPU", "4096")
MEMORY = os.environ.get("BENCH_MEMORY", "16384")
# 溢写盘。默认 20GB 不够：8000 万行上的大排序会把中间结果写满，
# 而写满的表现是任务被杀而不是一句「盘不够」。
EPHEMERAL_GB = int(os.environ.get("BENCH_EPHEMERAL_GB", "100"))

TABLE_BUCKET = os.environ.get("S3_TABLE_BUCKET", "analytics-agent-tables")
RAW_BUCKET = os.environ.get("RAW_BUCKET", "analytics-agent-raw")
ATHENA_WG = os.environ.get("ATHENA_WORKGROUP", "analytics-agent-wg")
RS_WG = os.environ.get("REDSHIFT_WORKGROUP", "analytics-agent-wg")
SECRET_ARN = os.environ.get("REDSHIFT_SECRET_ARN", "")

#: trace 回传的前缀。刻意与 `athena-staging/` 和 `parquet/` 分开：这个前缀下没有
#: 任何被查的数据，所以给它写权限不动「三条 arm 读的是同一份数据」这个前提。
TRACE_PREFIX = "bench-traces/"


def _acct() -> str:
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def image_uri(tag: str = "latest") -> str:
    return f"{_acct()}.dkr.ecr.{REGION}.amazonaws.com/{REPO}:{tag}"


# ---------------------------------------------------------------- IAM

def _policy(acct: str) -> dict:
    """任务角色的内联策略。逐条对应一条 arm 需要的东西，**全部只读**。

    这里不用任何 `*:*` 或托管的 FullAccess：这套 harness 只做一件事——读同一批数据
    做比对。任何写权限都只能用来破坏「三条 arm 读的是同一份数据」这个前提，
    而那个前提一旦破了，之前所有对账结论一起作废且看不出来。

    唯一的写口子是 Athena 结果暂存目录（`athena-staging/`），那是 Athena 的工作机制
    要求的，且限定在那个前缀下。
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AthenaArmQuery",
                "Effect": "Allow",
                "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                           "athena:GetQueryResults", "athena:StopQueryExecution",
                           "athena:GetWorkGroup", "athena:GetQueryResultsStream",
                           # 联邦目录（S3 Tables）要先被解析出来才能查，这两个是
                           # 解析那一步用的。资源写 datacatalog/*：联邦目录名里带
                           # 斜杠，拼进 ARN 是未定义行为，账号内只有这一个目录。
                           "athena:GetDataCatalog", "athena:ListDataCatalogs"],
                "Resource": [f"arn:aws:athena:{REGION}:{acct}:workgroup/{ATHENA_WG}",
                             f"arn:aws:athena:{REGION}:{acct}:datacatalog/*"],
            },
            {
                # Athena 把结果写到暂存目录再读回来，这是它的工作机制，绕不开。
                # 限定在 athena-staging/ 前缀下，桶里的 parquet/ 那份数据碰不到。
                # ListMultipartUploadParts 是大结果集分片上传要的，缺了它 Athena 在
                # 结果超过一个分片时才失败，小查询看不出来。
                "Sid": "AthenaStagingOnly",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload",
                           "s3:ListMultipartUploadParts"],
                "Resource": f"arn:aws:s3:::{RAW_BUCKET}/athena-staging/*",
            },
            {
                # trace 回传。容器的文件系统随任务一起消失，而 trace 是「不相等时
                # 不重跑也能定位到列」的那份东西——不传出来，云上跑一轮只剩
                # PASS/FAIL，等于把这套 harness 最有用的输出扔了。
                # 只给 PutObject，且限定在 bench-traces/ 前缀：这个前缀下没有任何
                # 被查的数据，写坏了不影响「三条 arm 读同一份数据」这个前提。
                "Sid": "BenchTraceWrite",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": f"arn:aws:s3:::{RAW_BUCKET}/{TRACE_PREFIX}*",
            },
            {
                # 桶级元数据，**不能**带 s3:prefix 条件。踩过一次：把
                # GetBucketLocation 和 ListBucket 放进同一条带 prefix 条件的语句里，
                # 而 GetBucketLocation 根本没有 s3:prefix 这个条件键，条件于是永不成立、
                # 调用被拒。Athena 报的是「Unable to verify/create output bucket
                # analytics-agent-raw」——听起来像桶不存在或要建桶的权限，
                # 跟「条件键用错了」差得很远。
                "Sid": "AthenaBucketMeta",
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
                "Resource": f"arn:aws:s3:::{RAW_BUCKET}",
            },
            {
                # 列桶内容才是能按前缀限定的那一个：ListBucket 支持 s3:prefix。
                "Sid": "AthenaStagingList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{RAW_BUCKET}",
                "Condition": {"StringLike": {"s3:prefix": ["athena-staging/*",
                                                           "athena-staging"]}},
            },
            {
                # Glue 目录：Athena 靠它解析表。只读。
                # GetCatalog/GetCatalogs 是 S3 Tables 这条路必须的：表不在默认目录里，
                # 而在联邦目录 `s3tablescatalog/<桶名>` 下，Athena 要先把这个目录解析
                # 出来。缺了它报的是 `CATALOG_NOT_FOUND: Catalog
                # 's3tablescatalog/analytics-agent-tables' does not exist`——
                # 看起来像目录没建，其实是看不见。
                "Sid": "GlueCatalogRead",
                "Effect": "Allow",
                "Action": ["glue:GetCatalog", "glue:GetCatalogs",
                           "glue:GetDatabase", "glue:GetDatabases", "glue:GetTable",
                           "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"],
                "Resource": [
                    f"arn:aws:glue:{REGION}:{acct}:catalog",
                    f"arn:aws:glue:{REGION}:{acct}:catalog/*",
                    f"arn:aws:glue:{REGION}:{acct}:database/*",
                    f"arn:aws:glue:{REGION}:{acct}:table/*",
                ],
            },
            {
                # S3 Tables：athena 和 duckdb 两条 arm 读的**同一批**表就在这儿。
                # 没有 Put/Delete/Update——READ_ONLY 那个 ATTACH 参数在客户端侧挡写，
                # 这里在 IAM 侧再挡一次，两层都不靠对方。
                "Sid": "S3TablesRead",
                "Effect": "Allow",
                "Action": ["s3tables:GetTableBucket", "s3tables:ListNamespaces",
                           "s3tables:GetNamespace", "s3tables:ListTables",
                           "s3tables:GetTable", "s3tables:GetTableMetadataLocation",
                           "s3tables:GetTableData"],
                "Resource": [
                    f"arn:aws:s3tables:{REGION}:{acct}:bucket/{TABLE_BUCKET}",
                    f"arn:aws:s3tables:{REGION}:{acct}:bucket/{TABLE_BUCKET}/*",
                ],
            },
            {
                "Sid": "RedshiftArmQuery",
                "Effect": "Allow",
                "Action": ["redshift-data:ExecuteStatement",
                           "redshift-data:BatchExecuteStatement",
                           "redshift-data:DescribeStatement",
                           "redshift-data:GetStatementResult",
                           "redshift-data:CancelStatement"],
                # Data API 的资源是 workgroup；DescribeStatement/GetStatementResult
                # 按语句 id 授权（语句是自己提的，Data API 只让本人读自己的结果）。
                "Resource": "*",
            },
            {
                "Sid": "RedshiftArmAuth",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": SECRET_ARN or f"arn:aws:secretsmanager:{REGION}:{acct}:secret:redshift!*",
            },
        ],
    }


# ---------------------------------------------------------------- Lake Formation

def _lf_module():
    """借 governance.py 的目录 ID 和表枚举，不自己拼一份。"""
    sys.path.insert(0, str(ROOT / "scripts" / "lakehouse"))
    import importlib
    return importlib.import_module("governance")


def lf_plan(cid: str, namespace: str,
            tables: list[str]) -> list[tuple[dict, list[str]]]:
    """要发的 LF 授权：库级 DESCRIBE + 每表**全列** SELECT。

    单独抽出来是为了能离线钉住「不带列级排除」这条（见 selftest 第 8 项）：
    `ColumnWildcard` 必须是空 dict，一旦有人往里加 `ExcludedColumnNames`，
    Athena 这条 arm 就在读和另两条不同的数据，而对账结果看起来仍然像是成立的。
    """
    out = [({"Database": {"CatalogId": cid, "Name": namespace}}, ["DESCRIBE"])]
    out += [({"TableWithColumns": {"CatalogId": cid, "DatabaseName": namespace,
                                   "Name": t, "ColumnWildcard": {}}}, ["SELECT"])
            for t in tables]
    return out


def ensure_lf_grants(role_arn: str, apply: bool = True) -> int:
    """给 bench 任务角色发 Lake Formation 授权。**IAM 权限替代不了这一步。**

    `s3tablescatalog` 这个联邦目录的 `CreateTableDefaultPermissions` 和
    `CreateDatabaseDefaultPermissions` 都是空的——也就是说没有
    `IAM_ALLOWED_PRINCIPALS` 兜底，每个主体都得有显式的 LF 授权。本机跑得通是因为
    开发者角色是 data lake admin，admin 绕过授权检查；Fargate 上那个任务角色不是，
    于是 Athena 报 `CATALOG_NOT_FOUND: Catalog 's3tablescatalog/analytics-agent-tables'
    does not exist`。这句话指向「目录不存在」，而真实原因是「你看不见它」，
    所以很容易一路去查目录名、区域、IAM 动作，全都查不出问题。

    ## 为什么这里**不套**治理层那套列级排除

    `governance.py` 给 `analytics-agent-ro` 发的授权是拒 `user_messages`、排除
    `users.email` / `users.phone` / `user_profiles.birth_date`。这个角色刻意**不**照抄：

    - 对账的 35 张表里就有 `user_messages`，那三列也在指标范围内。照抄的话 Athena
      这条 arm 会少一张表、少三列，而 DuckDB / Redshift 两条 arm 没有列级治理层这种
      东西，照样全读。三条 arm 于是在读**不同的数据**，之后所有「引擎差异」的结论
      都是假的。
    - 更坏的是它不会报错：少掉的表在对账里表现为一条失败，看起来像装载不一致。

    治理能力本身是另一个轴上的对比项（用户明确说先放后面），要比的时候用
    `analytics-agent-ro` 那个角色去比，不是把它混进耗时/正确性这一轮。
    """
    g = _lf_module()
    acct = _acct()
    cid = g.catalog_id(acct)
    lf = boto3.client("lakeformation", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)
    tables = g.namespace_tables(glue, cid)
    if not tables:
        print(f"  ❌ 目录 {cid} 的 namespace {g.NAMESPACE} 里一张表都没读到")
        return 1

    principal = {"DataLakePrincipalIdentifier": role_arn}
    todo = lf_plan(cid, g.NAMESPACE, tables)

    if not apply:
        print(f"  LF 待发：库级 DESCRIBE + {len(tables)} 张表全列 SELECT（未执行）")
        return 0
    for res, perms in todo:
        # grant_permissions 是幂等的，重发已有的授权不报错。
        lf.grant_permissions(Principal=principal, Resource=res, Permissions=perms)

    # 回读核对。发完就当成了是这一步第一版最容易犯的错：授权面写错时 grant 照样
    # 返回 200，而查询照旧 CATALOG_NOT_FOUND。
    miss = []
    for t in tables:
        got: set[str] = set()
        for p in lf.list_permissions(
                Principal=principal,
                Resource={"Table": {"CatalogId": cid,
                                    "DatabaseName": g.NAMESPACE, "Name": t}}
                ).get("PrincipalResourcePermissions", []):
            if p["Principal"]["DataLakePrincipalIdentifier"] == role_arn:
                got |= set(p.get("Permissions", []))
        if "SELECT" not in got:
            miss.append(t)
    if miss:
        print(f"  ❌ LF 授权发完回读不到 SELECT：{miss[:5]}"
              f"{f'… 共 {len(miss)} 张' if len(miss) > 5 else ''}")
        return 1
    print(f"  LF 授权：库级 DESCRIBE + {len(tables)} 张表全列 SELECT（回读确认）"
          f"，含 {', '.join(g.DENY_TABLES)}——对比 arm 要读全量，"
          f"治理层的列级排除刻意不套用")
    return 0


def _ensure_role(iam, name: str, policy_doc: dict | None,
                 managed: list[str]) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  角色 {name} 已存在")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
            # 只能是 ASCII/Latin-1：IAM 的 description 有字符集约束（可打印
            # ASCII 加 Latin-1 补充区），中文过不去。报出来的是一条正则不匹配，
            # 看不出原因是「描述里有中文」，所以这里写英文。
            Description="analytics-agent three-arm benchmark harness "
                        "(read-only)")["Role"]["Arn"]
        print(f"  建了角色 {name}")
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    if policy_doc:
        # put_role_policy 是幂等的覆盖写，所以改了 _policy() 重跑 --setup 就生效。
        iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline",
                            PolicyDocument=json.dumps(policy_doc))
        print(f"  写入 {name} 的内联策略（{len(policy_doc['Statement'])} 条语句，全部只读）")
    return arn


# ---------------------------------------------------------------- setup

def setup() -> int:
    acct = _acct()
    ecr = boto3.client("ecr", region_name=REGION)
    iam = boto3.client("iam")
    logs = boto3.client("logs", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)

    try:
        ecr.create_repository(repositoryName=REPO,
                             imageScanningConfiguration={"scanOnPush": True})
        print(f"  建了 ECR 仓库 {REPO}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        print(f"  ECR 仓库 {REPO} 已存在")

    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        print(f"  建了日志组 {LOG_GROUP}（保留 30 天）")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"  日志组 {LOG_GROUP} 已存在")

    task_arn = _ensure_role(iam, TASK_ROLE, _policy(acct), [])
    exec_arn = _ensure_role(
        iam, EXEC_ROLE, None,
        ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"])
    print("  等 IAM 传播 10s")
    time.sleep(10)

    if (rc := ensure_lf_grants(task_arn)):
        return rc

    env = [{"name": "AWS_REGION", "value": REGION},
           # 不设的话 trace 只写在容器里，任务一退就没了（见 correctness.TRACE_S3）。
           {"name": "BENCH_TRACE_S3",
            "value": f"s3://{RAW_BUCKET}/{TRACE_PREFIX.rstrip('/')}"}]
    if SECRET_ARN:
        # 这是 secret 的 **ARN**，不是密码本身，放环境变量里没问题：真正取值要
        # secretsmanager:GetSecretValue，那个权限在任务角色上。
        env.append({"name": "REDSHIFT_SECRET_ARN", "value": SECRET_ARN})

    d = ecs.register_task_definition(
        family=FAMILY, networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"], cpu=CPU, memory=MEMORY,
        taskRoleArn=task_arn, executionRoleArn=exec_arn,
        ephemeralStorage={"sizeInGiB": EPHEMERAL_GB},
        runtimePlatform={"cpuArchitecture": "X86_64",
                         "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": "bench",
            "image": image_uri(),
            "essential": True,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": LOG_GROUP,
                            "awslogs-region": REGION,
                            "awslogs-stream-prefix": "bench"},
            },
            "environment": env,
        }])
    rev = d["taskDefinition"]["revision"]
    print(f"  注册任务定义 {FAMILY}:{rev}  "
          f"（{CPU} CPU 单位 / {MEMORY} MB / 溢写盘 {EPHEMERAL_GB} GB）")
    print(f"\n就绪。下一步 --build 推镜像，再 --run 跑。")
    return 0


# ---------------------------------------------------------------- build

def build(tag: str = "latest") -> int:
    """构建并推镜像。用 buildx 指定 linux/amd64——Mac 上默认出 arm64 的镜像，
    而任务定义写的是 X86_64，架构不匹配时容器起不来，报的是 exec format error，
    离「镜像架构不对」这个原因很远。
    """
    acct = _acct()
    uri = image_uri(tag)
    reg = f"{acct}.dkr.ecr.{REGION}.amazonaws.com"
    steps = [
        f"aws ecr get-login-password --region {REGION} "
        f"| docker login --username AWS --password-stdin {reg}",
        f"docker buildx build --platform linux/amd64 "
        f"-f scripts/bench/Dockerfile -t {uri} --load .",
        f"docker push {uri}",
    ]
    for s in steps:
        print(f"\n$ {s}")
        r = subprocess.run(s, shell=True, cwd=ROOT)
        if r.returncode:
            print(f"\n这一步失败了（退出码 {r.returncode}），后面的没跑。")
            return r.returncode
    print(f"\n推上去了：{uri}")
    return 0


# ---------------------------------------------------------------- run

def _network() -> dict:
    """网络配置沿用 ask-relay 服务那一份。

    沿用而不是自己挑子网，是因为那套配置已经验证过能出网（DuckDB 要连 S3 Tables
    的 endpoint，Athena/Redshift Data API 也都是公网 endpoint）。自己挑一组私有子网
    但没有 NAT 的话，症状是任务卡在拉镜像或者第一次 API 调用超时，
    而超时的报错里不会写「这个子网没有出口」。
    """
    ecs = boto3.client("ecs", region_name=REGION)
    svcs = ecs.describe_services(cluster=CLUSTER, services=["ask-relay"])["services"]
    if not svcs or not svcs[0].get("networkConfiguration"):
        raise RuntimeError(
            f"从 {CLUSTER}/ask-relay 取不到网络配置。手动给："
            f"BENCH_SUBNETS=subnet-a,subnet-b BENCH_SG=sg-x")
    cfg = svcs[0]["networkConfiguration"]["awsvpcConfiguration"]
    if os.environ.get("BENCH_SUBNETS"):
        cfg = dict(cfg, subnets=os.environ["BENCH_SUBNETS"].split(","))
    if os.environ.get("BENCH_SG"):
        cfg = dict(cfg, securityGroups=os.environ["BENCH_SG"].split(","))
    # assignPublicIp 必须 ENABLED：这些子网没挂 NAT，靠公网 IP 出网。
    return {"awsvpcConfiguration": {**cfg, "assignPublicIp": "ENABLED"}}


#: 镜像的默认入口。命令覆盖会把 CMD **整条**替换掉，不是追加，所以只给
#: `--rows` 的话它会被当成可执行文件名，报 `exec: "--rows": not found`——
#: 报错里完全看不出是「覆盖语义是替换」。所以下面按第一个参数是不是选项来补齐。
ENTRY = ["python", "scripts/bench/correctness.py"]


def _full_command(args: list[str] | None) -> list[str] | None:
    """把 harness 参数补成完整 argv。

    规则：第一个参数以 `-` 开头＝只给了 harness 的选项，补上默认入口；
    否则当成调用者自己给的完整命令，原样下发（这样也能跑 arms.py 之类的别的脚本）。
    """
    if not args:
        return None
    return ENTRY + args if args[0].startswith("-") else args


def run(command: list[str] | None) -> int:
    ecs = boto3.client("ecs", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)
    command = _full_command(command)
    override = {"name": "bench"}
    if command:
        override["command"] = command
    r = ecs.run_task(
        cluster=CLUSTER, taskDefinition=FAMILY, launchType="FARGATE",
        networkConfiguration=_network(),
        overrides={"containerOverrides": [override]},
        # 起因写进任务，这样在控制台上能看出这个任务是谁为了什么起的
        startedBy="bench-harness")
    if r.get("failures"):
        print(f"起不来：{json.dumps(r['failures'], ensure_ascii=False)}")
        return 1
    task_arn = r["tasks"][0]["taskArn"]
    tid = task_arn.rsplit("/", 1)[-1]
    print(f"任务 {tid} 已提交，命令 {command or '（用镜像默认）'}")
    print(f"日志：{LOG_GROUP}/bench/bench/{tid}\n")

    stream = f"bench/bench/{tid}"
    token, last = None, time.time()
    while True:
        t = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn])["tasks"][0]
        try:
            kw = {"logGroupName": LOG_GROUP, "logStreamName": stream,
                  "startFromHead": True}
            if token:
                kw["nextToken"] = token
            ev = logs.get_log_events(**kw)
            for e in ev["events"]:
                print(e["message"])
            # nextForwardToken 不变表示这一轮没有新事件，别把它当结束条件——
            # 任务可能只是还没输出。结束条件只看 ECS 的 lastStatus。
            token = ev["nextForwardToken"]
        except logs.exceptions.ResourceNotFoundException:
            pass                      # 日志流要等容器真正起来才有
        if t["lastStatus"] == "STOPPED":
            code = t["containers"][0].get("exitCode")
            reason = t.get("stoppedReason", "")
            print(f"\n任务结束，退出码 {code}"
                  + (f"，原因：{reason}" if reason else ""))
            if code is None:
                # 没有退出码说明容器没跑起来（拉镜像失败、角色不对、架构不匹配）。
                print("  没有退出码＝容器没起来，不是 harness 失败。三个常见原因："
                      "命令覆盖是**整条替换**而不是追加（见 _full_command）、"
                      "镜像架构不是 linux/amd64、任务角色拉不到镜像。")
                return 1
            return int(code)
        if time.time() - last > 5:
            last = time.time()
            print(f"  … {t['lastStatus']}", flush=True)
        time.sleep(4)


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    """离线自查：策略是不是真的只读、镜像与本机算力口径是不是同一个。"""
    bad = 0
    pol = _policy("111122223333")

    # 1. 任务角色不许有写动作。这一条是「三条 arm 读的是同一份数据」的 IAM 侧保证。
    #    只有两个例外，各自必须限定在自己的前缀下，且那两个前缀下都没有被查的数据：
    #    Athena 的结果暂存目录，和 trace 回传目录。
    _WRITE_OK = {"AthenaStagingOnly": "athena-staging/",
                 "BenchTraceWrite": TRACE_PREFIX}
    for st in pol["Statement"]:
        for act in st["Action"]:
            verb = act.split(":", 1)[1]
            writes = ("Put", "Delete", "Update", "Create", "Write", "Abort")
            if any(verb.startswith(w) for w in writes):
                need = _WRITE_OK.get(st["Sid"])
                if need is None:
                    bad += 1
                    print(f"  FAIL {st['Sid']} 里有写动作 {act}——任务角色必须只读")
                elif need not in str(st["Resource"]):
                    bad += 1
                    print(f"  FAIL {st['Sid']} 的写权限没限定在 {need} 前缀下")

    # 1b. 写权限一律不能碰到数据前缀。`parquet/` 是 Redshift COPY 的来源，
    #     `csv/` 是明文真源——任何一处可写都能一步废掉之前所有对账结论。
    for st in pol["Statement"]:
        if st["Sid"] not in _WRITE_OK:
            continue
        for danger in ("parquet/", "csv/"):
            if danger in str(st["Resource"]):
                bad += 1
                print(f"  FAIL {st['Sid']} 的写权限覆盖到了数据前缀 {danger}")

    # 2. 不许出现 FullAccess / 通配动作
    for st in pol["Statement"]:
        for act in st["Action"]:
            if act.endswith(":*") or act == "*":
                bad += 1
                print(f"  FAIL {st['Sid']} 用了通配动作 {act}")

    # 3. 镜像里的算力口径必须与 conn.py 的默认值一致。不一致的话「云上更快/更慢」
    #    量的是配置不是引擎，而这种错从输出里看不出来。
    df = (ROOT / "scripts" / "bench" / "Dockerfile").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "scripts" / "duckdb"))
    conn = importlib.import_module("conn")
    for var, want in (("DUCKDB_THREADS", conn.THREADS),
                      ("DUCKDB_MEMORY_LIMIT", conn.MEMORY_LIMIT)):
        if f"{var}={want}" not in df:
            bad += 1
            print(f"  FAIL Dockerfile 里的 {var} 与 conn.py 的默认值 {want!r} 不一致"
                  f"——云上和本机就不是同一把尺了")

    # 4. 镜像必须把三条 arm 的客户端都拷进去。少一条的表现是 ModuleNotFoundError，
    #    而那时任务已经起来了、日志里只有一行 traceback。
    for need in ("scripts/lakehouse/", "scripts/duckdb/", "scripts/redshift/",
                 "scripts/bench/", "database/", "data/loaded_row_counts.json"):
        if f"COPY {need}" not in df and need not in df:
            bad += 1
            print(f"  FAIL Dockerfile 没拷 {need}")

    # 5. 命令补齐。踩过一次：只给 `--rows` 时它被当成可执行文件名，
    #    因为 ECS 的 command 覆盖是整条替换。
    for args, want in ((["--rows"], ENTRY + ["--rows"]),
                       (["--rows", "--arm", "duckdb"],
                        ENTRY + ["--rows", "--arm", "duckdb"]),
                       (["python", "scripts/bench/arms.py"],
                        ["python", "scripts/bench/arms.py"]),
                       (None, None)):
        got = _full_command(args)
        if got != want:
            bad += 1
            print(f"  FAIL _full_command({args!r}) 得到 {got!r}，期望 {want!r}")

    # 6. `s3:prefix` 条件只能套在支持它的动作上。踩过一次：GetBucketLocation 和
    #    ListBucket 放在同一条带 prefix 条件的语句里，前者没有这个条件键，
    #    条件永不成立、调用被拒，而 Athena 报的是「Unable to verify/create output
    #    bucket」——指向桶不存在，不指向条件键。
    _PREFIX_OK = {"s3:ListBucket"}
    for st in pol["Statement"]:
        keys = {k for m in st.get("Condition", {}).values() for k in m}
        if "s3:prefix" not in keys:
            continue
        for act in st["Action"]:
            if act not in _PREFIX_OK:
                bad += 1
                print(f"  FAIL {st['Sid']} 给 {act} 套了 s3:prefix 条件，"
                      f"而这个动作没有该条件键——条件永不成立，调用会被拒")

    # 7. Athena 那条 arm 要的读动作，必须覆盖 governance.py 里那一份。那份 ARN/动作
    #    集合是**实测迭代出来的**（它的 docstring 写着这件事），少一条的表现不是
    #    AccessDenied 而是 CATALOG_NOT_FOUND 之类指不到成因的报错。这一轮就少了
    #    glue:GetCatalog(s) 和 athena:GetDataCatalog，两次都是跑到云上才发现。
    sys.path.insert(0, str(ROOT / "scripts" / "lakehouse"))
    gov = importlib.import_module("governance")
    mine = {a for st in pol["Statement"] for a in st["Action"]}
    theirs = {a for st in gov.policy_document("111122223333")["Statement"]
              for a in ([st["Action"]] if isinstance(st["Action"], str)
                        else st["Action"])
              # 只比读路径这三族；治理角色的 S3 暂存那几条这边形状不同。
              if a.split(":", 1)[0] in ("glue", "s3tables", "athena")}
    if gap := sorted(theirs - mine):
        bad += 1
        print(f"  FAIL 比 governance.py 的实测读动作集少了 {gap}"
              f"——少的表现是 CATALOG_NOT_FOUND 一类，不是 AccessDenied")

    # 8. LF 授权不能套治理层的列级排除，也不能漏掉治理层拒绝的那张表：
    #    对账的 35 张表里就有 user_messages，排掉它 Athena 这条 arm 就在读和另两条
    #    不同的数据，而对账结果看起来仍然像是成立的。
    #    判据看**生成出来的计划**而不是文件文本——第一版扫文本，被自己解释这件事的
    #    docstring 里的 ExcludedColumnNames 绊倒了。
    plan = lf_plan("111122223333:s3tablescatalog/b", "ns",
                   ["orders", *gov.DENY_TABLES])
    for res, _ in plan:
        cw = res.get("TableWithColumns", {}).get("ColumnWildcard")
        if cw is not None and cw != {}:
            bad += 1
            print(f"  FAIL LF 计划里出现了非全列授权 {cw}——对比 arm 必须读全量")
    granted = {res["TableWithColumns"]["Name"] for res, _ in plan
               if "TableWithColumns" in res}
    if missing := [t for t in gov.DENY_TABLES if t not in granted]:
        bad += 1
        print(f"  FAIL LF 计划漏了治理层拒绝的表 {missing}，但对账要读它")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  策略 {len(pol['Statement'])} 条语句只读，两处写权限各限定在自己的前缀下"
          f"（{'、'.join(sorted(_WRITE_OK.values()))}，均不含被查数据）"
          f"· 无通配动作 · s3:prefix 只套在 ListBucket 上")
    print(f"  算力口径与 conn.py 一致：threads={conn.THREADS} / "
          f"memory_limit={conn.MEMORY_LIMIT}")
    print(f"  镜像内容齐全：三条 arm 的客户端 + DDL 真源 + 行数基准")
    print(f"  命令补齐 4 例（选项补默认入口 / 完整命令原样下发）")
    print(f"  读动作集覆盖 governance.py 的实测集 · 无列级排除（三条 arm 读全量）")
    print("离线自查通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="三 arm harness 的 Fargate 部署与运行",
        epilog="传给 harness 的参数写在 -- 之后，例：--run -- --rows --arm duckdb")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--setup", action="store_true", help="建 ECR / 角色 / 任务定义")
    ap.add_argument("--build", action="store_true", help="构建并推镜像")
    ap.add_argument("--run", action="store_true", help="跑一次并跟日志")
    ap.add_argument("--all", action="store_true", help="setup + build + run")
    ap.add_argument("--tag", default="latest")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args()

    cmd = [x for x in a.rest if x != "--"] or None

    if a.selftest:
        return selftest()
    if not (a.setup or a.build or a.run or a.all):
        ap.error("给 --selftest / --setup / --build / --run / --all")
    if a.setup or a.all:
        if (rc := setup()):
            return rc
    if a.build or a.all:
        if (rc := build(a.tag)):
            return rc
    if a.run or a.all:
        return run(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
