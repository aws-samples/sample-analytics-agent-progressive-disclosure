#!/usr/bin/env python3
"""arm4（Aurora/pg_duckdb）自管实例的 provisioning —— 可复现、可拆除。

## 这条 arm 为什么是「自管 PG」而不是托管 Aurora

托管 Aurora PostgreSQL 装不了 pg_duckdb：它是 C 扩展（共享库），不在 Aurora 的
扩展白名单里，pg_tle 也只能装 SQL/PL 扩展、装不了 C 库（都已核实）。而 mentor 要的
「数据后台统一走 Aurora 入口 + 直查 Iceberg」里，**直查 Iceberg 必须靠 pg_duckdb**。
两者不能同时满足于托管 Aurora，于是取「统一的 Postgres 线协议入口」这一面，用一台
自管 PG + pg_duckdb 顶上——对一个只读对比台，运维成本可以忽略。

## 它在对比里的位置

三条已有 arm 跑在同一个 Fargate 任务里（见 scripts/cloud_arms.sh）。arm4 的 backend
进程仍在那个任务里，只是经 PG 线协议连到这台 EC2 上的 pg_duckdb——所以它像 athena/
redshift（backend 在任务里、隔一跳网络连一个引擎），**不像** in-process 的 duckdb arm。
它读的是**同一批** S3 Tables Iceberg（ATTACH 的 ARN/catalog/namespace 复用 duckdb arm
的 conn 模块），所以残差仍只落在「引擎 + 入口」上。

## 用法（都要 Isengard dev 凭证，us-west-2）

    python3 scripts/aurora/provision.py setup     # 建 secret + 实例角色 + SG（不起实例）
    python3 scripts/aurora/provision.py up        # 起 EC2，跑 user-data，打印 AURORA_PGHOST
    python3 scripts/aurora/provision.py status
    python3 scripts/aurora/provision.py rotate    # 换 secret 口令（旧口令泄露后补救，需随后 down+up）
    python3 scripts/aurora/provision.py down      # 终止实例（secret/角色/SG 留着）
    python3 scripts/aurora/provision.py destroy   # 连 secret/角色/SG 一起删

起好后把 up 打印的 host 填进 Fargate 任务的环境变量：
    DB_BACKEND=aurora
    AURORA_PGHOST=<实例私网 IP>  AURORA_PGPORT=5432
    AURORA_PGDATABASE=app_analytics  AURORA_PGUSER=analytics_agent_ro
    AURORA_PGPASSWORD -> 从本脚本建的 secret 注入（任务角色需 GetSecretValue）

本脚本只在显式带子命令调用时才动云资源；import 或不带子命令都不碰任何东西。
"""
from __future__ import annotations

import json
import os
import secrets
import string
import sys
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_BUCKET = os.environ.get("S3_TABLE_BUCKET", "analytics-agent-tables")

# 复用 ask-relay / cloud_arms.sh 的网络：这些子网没挂 NAT，实例要公网 IP 才能出网
# （装 extension、读 S3 Tables）。SG_SOURCE 是 Fargate 任务 SG——5432 只对它开。
SUBNET = os.environ.get("AURORA_SUBNET", "subnet-0bac5e670766638dd")
SG_SOURCE = os.environ.get("AURORA_SG_SOURCE", "sg-0123456789abcdef0")

NAME = "analytics-agent-aurora"           # 资源统一前缀
INSTANCE_ROLE = f"{NAME}-ec2"
INSTANCE_PROFILE = f"{NAME}-ec2"
SG_NAME = f"{NAME}-pg"
SECRET_NAME = f"{NAME}-pg-credentials"
# t3.xlarge = 4 vCPU / 16 GB,与 bench 的 Fargate 任务（cpu 4096 / mem 16384）同规格。
# 这是耗时对比的前提:arm4(pg_duckdb) 与 arm2(DuckDB 进程内) 共用 DuckDB 1.4.3 引擎,只有
# 机器一致、threads/memory 口径一致,arm2 vs arm4 的残差才干净地落在「入口方式」(进程内 vs
# PG wire + 一跳网络) 上——正是 Aurora 统一入口要回答的架构问题。内嵌 DuckDB 的 threads=4 /
# memory_limit=4GB 由 db.py 的 aurora 路径在连接时 SET(与 arm2 的 Fargate 环境变量同值)。
INSTANCE_TYPE = os.environ.get("AURORA_INSTANCE_TYPE", "t3.xlarge")
# AL2023 最新 AMI 走 SSM 公共参数解析，不写死 AMI id（跨区/随时间都能用）。
AL2023_SSM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
PG_USER = "analytics_agent_ro"
PG_DB = "app_analytics"

USER_DATA_TEMPLATE = Path(__file__).resolve().parent / "user-data.sh"


def _acct() -> str:
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def _s3tables_read(acct: str) -> dict:
    """arm4 实例角色唯一需要的数据面权限：S3 Tables 只读。

    与 scripts/bench/fargate.py 的 `S3TablesRead` 语句**逐字一致**——pg_duckdb 走的是
    直连 S3 Tables Iceberg REST catalog 这条路（`ENDPOINT_TYPE s3_tables`），和 duckdb
    arm 完全相同，不经 Glue 联邦目录、也**不经 Lake Formation**（见 governance.py：
    `s3tables:GetTableData` 本身就是 LF 列级排除的一条旁路）。所以这里不需要 glue:* /
    LF 授权，只要这 7 个 s3tables 读动作。没有任何 Put/Delete/Update——READ_ONLY 那个
    ATTACH 参数在客户端挡写，IAM 这里再挡一层。
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
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
        }],
    }


def _ensure_secret(sm) -> str:
    """PG 口令存 Secrets Manager，格式 {"username","password"}。已存在就复用，不覆盖
    （覆盖会让已起实例上的口令和 secret 对不上）。返回 ARN。"""
    try:
        arn = sm.describe_secret(SecretId=SECRET_NAME)["ARN"]
        print(f"  secret {SECRET_NAME} 已存在，复用")
        return arn
    except sm.exceptions.ResourceNotFoundException:
        pw = "".join(secrets.choice(string.ascii_letters + string.digits)
                     for _ in range(32))
        arn = sm.create_secret(
            Name=SECRET_NAME,
            Description="analytics-agent arm4 pg_duckdb credentials (read-only arm)",
            SecretString=json.dumps({"username": PG_USER, "password": pw}),
        )["ARN"]
        print(f"  建了 secret {SECRET_NAME}")
        return arn


def _ensure_instance_role(iam, acct: str, secret_arn: str) -> None:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }]}
    try:
        iam.get_role(RoleName=INSTANCE_ROLE)
        print(f"  角色 {INSTANCE_ROLE} 已存在")
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(
            RoleName=INSTANCE_ROLE,
            AssumeRolePolicyDocument=json.dumps(trust),
            # IAM description 只接受 ASCII/Latin-1，中文过不去，故写英文。
            Description="analytics-agent arm4 pg_duckdb EC2 instance (read-only)")
        print(f"  建了角色 {INSTANCE_ROLE}")

    policy = _s3tables_read(acct)
    # 实例角色还要能读 PG 口令那个 secret（user-data 启动时取），限定到这一个 ARN。
    policy["Statement"].append({
        "Sid": "PgCredentialRead",
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue"],
        "Resource": secret_arn,
    })
    iam.put_role_policy(RoleName=INSTANCE_ROLE, PolicyName=f"{INSTANCE_ROLE}-inline",
                        PolicyDocument=json.dumps(policy))
    print(f"  写入 {INSTANCE_ROLE} 内联策略（{len(policy['Statement'])} 条，全只读）")

    try:
        iam.create_instance_profile(InstanceProfileName=INSTANCE_PROFILE)
        iam.add_role_to_instance_profile(InstanceProfileName=INSTANCE_PROFILE,
                                         RoleName=INSTANCE_ROLE)
        print(f"  建了实例配置文件 {INSTANCE_PROFILE}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"  实例配置文件 {INSTANCE_PROFILE} 已存在")


def _ensure_sg(ec2) -> str:
    """建 SG：入站只开 5432，且只对 Fargate 任务 SG（SG_SOURCE）开，不对 0.0.0.0/0。"""
    vpc = ec2.describe_subnets(SubnetIds=[SUBNET])["Subnets"][0]["VpcId"]
    try:
        gid = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [SG_NAME]},
                     {"Name": "vpc-id", "Values": [vpc]}]
        )["SecurityGroups"][0]["GroupId"]
        print(f"  SG {SG_NAME} 已存在：{gid}")
        return gid
    except (IndexError, KeyError):
        pass
    gid = ec2.create_security_group(
        GroupName=SG_NAME, VpcId=vpc,
        Description="analytics-agent arm4 pg_duckdb: 5432 from Fargate arms SG only",
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=gid,
        IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
            "UserIdGroupPairs": [{"GroupId": SG_SOURCE,
                                  "Description": "Fargate arms task"}],
        }],
    )
    print(f"  建了 SG {SG_NAME}：{gid}（5432 ← {SG_SOURCE}）")
    return gid


def _instance_id(ec2) -> str | None:
    r = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [NAME]},
                 {"Name": "instance-state-name",
                  "Values": ["pending", "running", "stopping", "stopped"]}]
    )["Reservations"]
    return r[0]["Instances"][0]["InstanceId"] if r else None


def setup() -> int:
    acct = _acct()
    iam = boto3.client("iam")
    sm = boto3.client("secretsmanager", region_name=REGION)
    ec2 = boto3.client("ec2", region_name=REGION)
    secret_arn = _ensure_secret(sm)
    _ensure_instance_role(iam, acct, secret_arn)
    _ensure_sg(ec2)
    print(f"\nsetup 完成。secret ARN:\n  {secret_arn}")
    print("把它配进 Fargate 任务角色的 GetSecretValue + 任务环境变量 AURORA_PGPASSWORD。")
    print("接着 `provision.py up` 起实例。")
    return 0


def up() -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)

    if _instance_id(ec2):
        print(f"实例已在跑：{_instance_id(ec2)}。先 down 再 up，或直接用它。")
        return 0

    secret_arn = sm.describe_secret(SecretId=SECRET_NAME)["ARN"]
    sg = _ensure_sg(ec2)
    ami = ssm.get_parameter(Name=AL2023_SSM)["Parameter"]["Value"]
    user_data = (USER_DATA_TEMPLATE.read_text()
                 .replace("__REGION__", REGION)
                 .replace("__PW_SECRET_ARN__", secret_arn))

    inst = ec2.run_instances(
        ImageId=ami, InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1,
        IamInstanceProfile={"Name": INSTANCE_PROFILE},
        UserData=user_data,
        # IMDSv2 强制 + hop limit 2：host 网络下 hop=1 本就够，这里再放一档做双保险，
        # 免得将来有人改回 bridge 网络时 credential_chain 静默取不到实例角色。
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
        NetworkInterfaces=[{
            "DeviceIndex": 0, "SubnetId": SUBNET, "Groups": [sg],
            "AssociatePublicIpAddress": True,   # 子网无 NAT，出网装 extension / 读 S3 要公网 IP
        }],
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "Name", "Value": NAME}]}],
    )["Instances"][0]
    iid = inst["InstanceId"]
    print(f"起了实例 {iid}（{INSTANCE_TYPE}），等私网 IP…")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    desc = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    print(f"\n实例在跑：{iid}")
    print(f"  AURORA_PGHOST = {desc['PrivateIpAddress']}   （同 VPC 内的 Fargate 任务可达）")
    print(f"  AURORA_PGPORT=5432  AURORA_PGDATABASE={PG_DB}  AURORA_PGUSER={PG_USER}")
    print("user-data 还在装 docker/拉镜像/预热 extension，约 2-3 分钟后才可连。")
    print("可 SSM 上去看：`docker logs pgduck` / `cat /var/log/cloud-init-output.log`。")
    return 0


def status() -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    iid = _instance_id(ec2)
    if not iid:
        print("没有在跑的 arm4 实例。")
        return 0
    d = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    print(f"实例 {iid}：{d['State']['Name']}  "
          f"私网 {d.get('PrivateIpAddress','?')}  公网 {d.get('PublicIpAddress','?')}")
    return 0


def down() -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    iid = _instance_id(ec2)
    if not iid:
        print("没有在跑的实例，无需 down。")
        return 0
    ec2.terminate_instances(InstanceIds=[iid])
    print(f"已请求终止 {iid}。secret/角色/SG 保留（destroy 才删）。")
    return 0


def rotate() -> int:
    """给 secret 换一串新口令（旧版本降为 AWSPREVIOUS）。用在旧口令泄露后补救——比如
    首版 user-data 的 set -x 把口令回显进过串口控制台。换完必须 down + up：正在跑的实例
    把旧口令烘进了容器环境和 ro 角色，只有重新 bootstrap 才会采用新口令。"""
    sm = boto3.client("secretsmanager", region_name=REGION)
    cur = json.loads(sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"])
    cur["password"] = "".join(secrets.choice(string.ascii_letters + string.digits)
                              for _ in range(32))
    sm.put_secret_value(SecretId=SECRET_NAME, SecretString=json.dumps(cur))
    print("已轮换 secret 的 password（旧版本降为 AWSPREVIOUS，没打印新值）。")
    print("当前实例仍握着旧口令，必须重新 bootstrap 才会采用新口令：")
    print("  python3 scripts/aurora/provision.py down")
    print("  python3 scripts/aurora/provision.py up")
    print("up 后实例私网 IP 会变，记得重跑 `wire_arms.py --apply` 刷新 task-def 的 AURORA_PGHOST。")
    return 0


def destroy() -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam")
    sm = boto3.client("secretsmanager", region_name=REGION)
    if _instance_id(ec2):
        print("还有实例在跑，先 `down` 再 `destroy`。")
        return 1
    # SG
    try:
        vpc = ec2.describe_subnets(SubnetIds=[SUBNET])["Subnets"][0]["VpcId"]
        sgs = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [SG_NAME]},
                     {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
        for g in sgs:
            ec2.delete_security_group(GroupId=g["GroupId"])
            print(f"  删了 SG {g['GroupId']}")
    except Exception as e:                      # noqa: BLE001  删除是尽力而为
        print(f"  SG 删除跳过：{e}")
    # 实例配置文件 + 角色
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=INSTANCE_PROFILE,
                                              RoleName=INSTANCE_ROLE)
        iam.delete_instance_profile(InstanceProfileName=INSTANCE_PROFILE)
        print(f"  删了实例配置文件 {INSTANCE_PROFILE}")
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        iam.delete_role_policy(RoleName=INSTANCE_ROLE, PolicyName=f"{INSTANCE_ROLE}-inline")
        iam.delete_role(RoleName=INSTANCE_ROLE)
        print(f"  删了角色 {INSTANCE_ROLE}")
    except iam.exceptions.NoSuchEntityException:
        pass
    # secret：带 7 天恢复窗，不立刻硬删（口令删错了还能救）。
    try:
        sm.delete_secret(SecretId=SECRET_NAME, RecoveryWindowInDays=7)
        print(f"  安排删除 secret {SECRET_NAME}（7 天恢复窗）")
    except sm.exceptions.ResourceNotFoundException:
        pass
    return 0


CMDS = {"setup": setup, "up": up, "status": status, "rotate": rotate,
        "down": down, "destroy": destroy}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CMDS:
        print(f"用法：{sys.argv[0]} {{{'|'.join(CMDS)}}}")
        sys.exit(2)
    sys.exit(CMDS[sys.argv[1]]())
