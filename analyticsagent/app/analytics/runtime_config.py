"""冷启动配置装载:从 Secrets Manager 拉运行配置 → 从 S3 同步知识树。

在 main.py 里**最先** import(先于 tools/db 读取配置)。设计原则(借鉴 lark 的验证做法):
  - 镜像里只烤一个非敏感指针 RUNTIME_SECRET_ID;所有动态/敏感配置(Aurora 端点、
    DB 口令、知识桶名、Bedrock 路由)都放在这个 Secrets Manager secret 里,冷启动时
    取一次 setdefault 进 os.environ(显式 env 优先)。口令绝不进镜像。
  - Runtime exec role 需要 secretsmanager:GetSecretValue(该 secret)+ s3:Get/List
    (知识桶)+ bedrock:InvokeModel*。VPC 内经接口/网关端点访问,无需公网。

secret JSON 期望键:
  PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD KNOWLEDGE_BUCKET
  AWS_REGION CLAUDE_CODE_USE_BEDROCK ANTHROPIC_MODEL
"""
from __future__ import annotations

import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _load_runtime_secret() -> None:
    sid = os.environ.get("RUNTIME_SECRET_ID", "")
    if not sid:
        logger.info("RUNTIME_SECRET_ID 未设置;沿用现有 env(本地开发)")
        return
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        data = json.loads(client.get_secret_value(SecretId=sid)["SecretString"])
        for k, v in data.items():
            os.environ.setdefault(k, str(v))
        # 只记键数,值(含口令)绝不写日志
        logger.info("已从 Secrets Manager 载入 %d 个运行配置键", len(data))
    except Exception:
        logger.exception("载入运行配置失败;沿用现有 env")


_load_runtime_secret()

# 配置就位后,把知识树从 S3 拉到本地(read_doc 读它)。延迟 import 让 knowledge_store
# 的 module 级配置读到已填好的 env(KNOWLEDGE_BUCKET 等)。
try:
    import knowledge_store
    knowledge_store.sync_down()
except Exception:
    logger.exception("冷启动知识同步失败(read_doc 可能无文档,需排查)")
