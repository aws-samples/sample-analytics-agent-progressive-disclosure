"""知识库(数据字典 md 文档树)的 S3 冷启动加载。

AgentCore-native 形态下,知识库**不打进镜像**,而是放在 S3,容器冷启动时同步到本地
KNOWLEDGE_DIR,read_doc 读本地缓存。好处:更新知识(改 md、加表卡片)只需重传 S3,
不必重建镜像/重新部署 Runtime。

S3 布局(bucket = $KNOWLEDGE_BUCKET,私有):
    knowledge/domains/_index.md
    knowledge/domains/<域>/<表>.md
    knowledge/metrics/*.md
    knowledge/analysis/*.md
    knowledge/relationships.md
    ...

冷启动路径:sync_down() 在 runtime_config 里(main 之前)调用一次,幂等——每个新
microVM 冷启时把整棵树拉到 KNOWLEDGE_DIR。md 文件小(几十 KB 量级),同步是秒级。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

KNOWLEDGE_BUCKET = os.environ.get("KNOWLEDGE_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_PREFIX = os.environ.get("KNOWLEDGE_S3_PREFIX", "knowledge/")
KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/app/knowledge")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3


def sync_down() -> int:
    """把 S3 上 S3_PREFIX 下的整棵知识树拉到 KNOWLEDGE_DIR。返回写入文件数。
    KNOWLEDGE_BUCKET 未设时返回 0(本地开发直接用仓库里的 knowledge/,不走 S3)。"""
    if not KNOWLEDGE_BUCKET:
        logger.info("KNOWLEDGE_BUCKET 未设置;跳过 S3 知识同步(用本地 knowledge/)")
        return 0
    written = 0
    try:
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=KNOWLEDGE_BUCKET, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(S3_PREFIX):]           # e.g. "domains/marketing/coupons.md"
                if not rel:
                    continue
                dest = os.path.join(KNOWLEDGE_DIR, rel)
                # 路径逃逸防护:恶意/畸形 key 不得写到 KNOWLEDGE_DIR 之外
                if not os.path.abspath(dest).startswith(os.path.abspath(KNOWLEDGE_DIR) + os.sep):
                    logger.warning("跳过不安全的知识 key: %s", key)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _client().download_file(KNOWLEDGE_BUCKET, key, dest)
                written += 1
        logger.info("知识同步完成:%d 个文件 → %s", written, KNOWLEDGE_DIR)
    except Exception:
        logger.exception("知识 S3 同步失败(read_doc 将无文档可读,需排查)")
    return written
