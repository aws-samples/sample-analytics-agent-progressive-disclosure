#!/usr/bin/env python3
"""把 arm4（Aurora/pg_duckdb）接进云上 arms 任务 —— 注册新一版 task-def + 补执行角色权限。

cloud_arms.sh 跑的是 task-def family `analytics-agent-arms`（四条 arm 一个任务）。arm1-3
的那版只有 8000/8001/8002 三个端口和 redshift 的 secret。本脚本在它之上叠出 arm4 需要的：

  1. 容器端口加 8003（镜像的新 CMD 把 aurora uvicorn 起在 8003）。
  2. 环境变量加 AURORA_PGHOST/PGPORT/PGDATABASE/PGUSER —— host 现查正在跑的 arm4 实例私网
     IP（按 tag Name=analytics-agent-aurora），实例每次 up 都换 IP，所以不写死。
  3. secret 加 AURORA_PGPASSWORD <- provision.py 建的那个 secret 的 password 键。
     ECS 的 `secrets` 注入由**执行角色**在拉起容器时取，所以同时给执行角色
     （analytics-agent-bench-exec）补一条 GetSecretValue（限定到这一个 secret ARN）。

它只改 task-def 和执行角色，不碰镜像、不碰数据。注册新版后 `cloud_arms.sh down && up`
让新版生效。前置：arms 镜像已重建成带 8003 的新 CMD 并推上 ECR（见 README「重建」一节）。

    python3 scripts/aurora/wire_arms.py          # 预演：打印将写入的 env/secret/port，不动云
    python3 scripts/aurora/wire_arms.py --apply  # 真注册新版 task-def + 补执行角色权限

用法都要 Isengard dev 凭证、us-west-2。
"""
from __future__ import annotations

import json
import os
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
FAMILY = "analytics-agent-arms"
CONTAINER = "arms"
INSTANCE_TAG = "analytics-agent-aurora"          # 与 provision.py NAME 一致
SECRET_NAME = "analytics-agent-aurora-pg-credentials"
EXEC_ROLE = "analytics-agent-bench-exec"         # arms task-def 的执行角色
EXEC_POLICY = "aurora-pg-secret-read"

PG_PORT = "5432"
PG_DB = "app_analytics"
# superuser 而非最小权限的 analytics_agent_ro:pg_duckdb v1.1.1 对**非超级用户**硬禁
# LocalFileSystem(current_setting('duckdb.disabled_filesystems') 即便为空也挡),而 iceberg
# 扩展要从本地扩展目录 LOAD —— 于是 ro 角色能跑 duckdb.query('SELECT 1') 却读不了外部
# Iceberg(实测:ro 报 "File system LocalFileSystem has been disabled",同一条 SQL 换 postgres
# 连即回 events=8541400,与 athena/duckdb 两条逐字一致)。「pg_duckdb 无最小权限外部读路径」
# 本身是本次架构对比的一条治理结论:不同于 Athena+LakeFormation 的列排除、Redshift 的掩码,
# 它的外部读必须 superuser。db.py 仍有 validate() 只读闸 + conn.read_only=True 两层防写,
# superuser 只用于加载扩展与读 S3。口令两角色共用(user-data.sh 第 36 行),secret 不用改。
PG_USER = "postgres"
ARM_PORT = 8003

# register_task_definition 接受的键（describe 回来的只读字段要剔掉，否则 API 报错）。
_REGISTER_KEYS = {
    "family", "taskRoleArn", "executionRoleArn", "networkMode",
    "containerDefinitions", "volumes", "placementConstraints",
    "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode",
    "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage",
    "runtimePlatform",
}


def _instance_ip(ec2) -> str:
    r = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [INSTANCE_TAG]},
                 {"Name": "instance-state-name", "Values": ["running"]}]
    )["Reservations"]
    if not r:
        sys.exit(f"没有在跑的 arm4 实例（tag Name={INSTANCE_TAG}）。先 provision.py up。")
    return r[0]["Instances"][0]["PrivateIpAddress"]


def _secret_arn(sm) -> str:
    return sm.describe_secret(SecretId=SECRET_NAME)["ARN"]


def _mutate_container(cdef: dict, host: str, secret_arn: str) -> dict:
    """幂等地往容器定义里补 8003 端口 / AURORA_* env / AURORA_PGPASSWORD secret。"""
    ports = cdef.setdefault("portMappings", [])
    if not any(p.get("containerPort") == ARM_PORT for p in ports):
        ports.append({"containerPort": ARM_PORT, "hostPort": ARM_PORT, "protocol": "tcp"})

    want_env = {"AURORA_PGHOST": host, "AURORA_PGPORT": PG_PORT,
                "AURORA_PGDATABASE": PG_DB, "AURORA_PGUSER": PG_USER}
    env = cdef.setdefault("environment", [])
    env[:] = [e for e in env if e["name"] not in want_env]      # 覆盖旧值（IP 会变）
    env.extend({"name": k, "value": v} for k, v in want_env.items())

    secrets = cdef.setdefault("secrets", [])
    secrets[:] = [s for s in secrets if s["name"] != "AURORA_PGPASSWORD"]
    secrets.append({"name": "AURORA_PGPASSWORD",
                    "valueFrom": f"{secret_arn}:password::"})
    return cdef


def _grant_exec_secret_read(iam, secret_arn: str) -> None:
    iam.put_role_policy(
        RoleName=EXEC_ROLE, PolicyName=EXEC_POLICY,
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "AuroraPgSecretRead",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": secret_arn,
            }],
        }))
    print(f"  给执行角色 {EXEC_ROLE} 补了 {EXEC_POLICY}（仅 GetSecretValue，限定该 secret）")


def main(apply: bool) -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)
    iam = boto3.client("iam")

    host = _instance_ip(ec2)
    secret_arn = _secret_arn(sm)
    print(f"arm4 实例私网 IP = {host}")
    print(f"secret ARN       = {secret_arn}")

    td = ecs.describe_task_definition(taskDefinition=FAMILY)["taskDefinition"]
    reg = {k: v for k, v in td.items() if k in _REGISTER_KEYS}
    cdefs = reg["containerDefinitions"]
    cdef = next((c for c in cdefs if c["name"] == CONTAINER), cdefs[0])
    _mutate_container(cdef, host, secret_arn)

    print("\n将写入 arms 容器定义：")
    print(f"  端口     : {[p['containerPort'] for p in cdef['portMappings']]}")
    print(f"  AURORA_* : host={host} port={PG_PORT} db={PG_DB} user={PG_USER}")
    print(f"  secret   : AURORA_PGPASSWORD <- {secret_arn}:password::")

    if not apply:
        print("\n预演（没动云）。真跑加 --apply。")
        return 0

    _grant_exec_secret_read(iam, secret_arn)
    out = ecs.register_task_definition(**reg)["taskDefinition"]
    rev = f"{out['family']}:{out['revision']}"
    print(f"\n已注册新版 task-def：{rev}")
    print("接着让它生效：bash scripts/cloud_arms.sh down && bash scripts/cloud_arms.sh up")
    print("验证：bash scripts/cloud_arms.sh tunnel aurora  然后开 http://127.0.0.1:8000/")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] not in ("--apply",):
        sys.exit(f"用法：{sys.argv[0]} [--apply]")
    sys.exit(main(apply=bool(args)))
