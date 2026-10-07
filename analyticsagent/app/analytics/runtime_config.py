"""冷启动配置装载:从 Secrets Manager 拉运行配置 → 从 S3 同步知识树。

在 main.py 里**最先** import(先于 tools/db 读取配置)。设计原则(借鉴 lark 的验证做法):
  - 镜像里只烤一个非敏感指针 RUNTIME_SECRET_ID;所有会变的配置(桶名、工作组、
    目录名、治理角色 ARN、Bedrock 路由)都放在这个 Secrets Manager secret 里,冷启动时
    取一次 setdefault 进 os.environ(显式 env 优先)。
  - Runtime exec role 需要 secretsmanager:GetSecretValue(该 secret)+ s3:Get/List
    (知识桶)+ bedrock:InvokeModel* + sts:AssumeRole(AGENT_ROLE_ARN)。

数据层换成 Athena 之后,这里**不再有任何口令**:Athena / Glue / S3 Tables 都是
HTTPS + IAM 的 AWS API 调用,没有连接串、没有密码、不需要 VPC(那也是
agentcore.json 从 networkMode=VPC 改回 PUBLIC 的原因)。secret 里剩下的全是
非敏感的坐标——留着 Secrets Manager 这一层是因为这些坐标**会随环境变**,烤进镜像
就得为改一个桶名重建镜像。

secret JSON 期望键(全部非敏感;除 AGENT_ROLE_ARN 外均可为空走默认):
  DB_BACKEND=athena  ATHENA_WORKGROUP  ATHENA_AGENT_WORKGROUP  ATHENA_CATALOG
  GLUE_CATALOG_ID  ICEBERG_NAMESPACE  S3_TABLE_BUCKET  AGENT_ROLE_ARN
  KNOWLEDGE_BUCKET  AWS_REGION  CLAUDE_CODE_USE_BEDROCK  ANTHROPIC_MODEL

ATHENA_WORKGROUP 与 ATHENA_AGENT_WORKGROUP 是**两个** workgroup,别设成同一个:
workgroup 的 OutputLocation 决定查询结果 CSV 落到哪个 S3 前缀,而结果集是明文行数据。
治理角色生效时 db.py 自动走后者(结果落 `athena-staging/agent/` 子前缀),共用一个的话
agent 能从管理侧的结果文件里把 LF 已排除的 `users.email` 读回来。见 athena.py。

⚠️ ATHENA_CATALOG 与 GLUE_CATALOG_ID 是**同一个目录的两种写法**,不能混用:
  ATHENA_CATALOG   = s3tablescatalog/<表桶>            (Athena 用,不带账号前缀)
  GLUE_CATALOG_ID  = <账号>:s3tablescatalog/<表桶>     (boto3 glue 的 CatalogId,带前缀)
写串了报 EntityNotFoundException,而那个报错指不到成因。GLUE_CATALOG_ID 不设时
athena.py 会从 sts 现算,所以它其实可省;显式放进 secret 是为了让这两种写法在同一处
并排出现——省掉它的代价是下一个人得先读懂 athena.py 才知道有两种写法。

⚠️ AGENT_ROLE_ARN 是 L4 治理层那个最小权限角色(scripts/lakehouse/governance.py 建),
**在云上它是必填项**:不设就等于没有列级边界(db.py 会直接用 Runtime exec role 的凭证
查数,user_messages / users.email / phone / user_profiles.birth_date 都看得见),而那是个
不报错的状态。所以 `_require_governance()` 在设了 RUNTIME_SECRET_ID 时会 raise,并且
不止查"配没配":它 assume 一次、把 db.backend_info()['identity'] 读回来核对角色名。
那个字段是从外面唯一能看见"治理接上了没有"的地方,`main.py` 的 `op: "health"` 分支
把它端出去给 /health 用(见 functions/ask-relay/server.mjs)。
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
    except Exception as e:
        # 不吞。见文件末尾「为什么这里要硬失败」。
        raise RuntimeError(f"运行配置载入失败({sid});容器拒绝启动") from e


def _require_governance() -> None:
    """云上(设了 RUNTIME_SECRET_ID)必须有治理角色,而且必须**真的 assume 得到**。

    两步,缺一不可:

    1. `AGENT_ROLE_ARN` 不能为空。上面那个 `setdefault` 循环只保证"secret 里有什么就
       填什么"——secret 里**少了这个键**、或者值是空串,循环一声不响地跳过,然后
       `db.py` 拿 Runtime exec role 的凭证直接查数,`user_messages` / `users.email` /
       `phone` / `user_profiles.birth_date` 全都看得见。这不是"治理失效"这种会报错的
       状态,是"从来没有生效过"这种不会报错的状态。
    2. assume 之后的身份要**读回来核对**。这是 mentor 指出的那个洞:ARN 配着,可能
       指向一个不存在的角色、或者信任策略里没有 exec role——`assume_role_session()`
       的凭证是延迟获取的,所以这两种错都要等 agent 答到一半才炸在一条查询里,而那时
       用户看到的是"这道题失败了",日志里的报错位置离原因很远。

    代价是冷启动多一次 AssumeRole + STS(~1s)。但这不是**多**出来的开销:`db.py` 把
    客户端缓存在 `_ath_client` 里,这一次调用同时把它建好了,第一个真实查询因此少付
    一次。也就是把一笔本来要付的钱提前付掉,换来"配错的容器起不来"。

    本地开发不受影响:不设 `RUNTIME_SECRET_ID` 时整个函数直接返回。
    """
    if not os.environ.get("RUNTIME_SECRET_ID"):
        return
    arn = os.environ.get("AGENT_ROLE_ARN", "").strip()
    if not arn:
        raise RuntimeError(
            "AGENT_ROLE_ARN 为空(secret 里缺这个键或值是空串);容器拒绝启动。"
            "没有它 db.py 会用 Runtime exec role 直接查数,列级边界从来不会生效——"
            "而那是个不报错的状态。角色用 scripts/lakehouse/governance.py --apply 建。"
        )
    import db                                    # noqa: E402 —— 必须在 env 填好之后
    who = db.backend_info().get("identity", "")
    # 判据是角色名,不是子串:`assumed-role/analytics-agent-ro/xxx` 要认,
    # `assumed-role/analytics-agent-ro-staging/xxx` 是另一个角色,不认。
    want = arn.rsplit("/", 1)[-1]
    parts = who.split("/")
    got = parts[1] if len(parts) > 1 else ""
    if got != want:
        raise RuntimeError(
            f"AGENT_ROLE_ARN={arn} assume 之后的身份是 {who!r},角色名对不上"
            f"({got!r} ≠ {want!r});容器拒绝启动。查数的身份不是治理角色,"
            f"就等于没有列级边界。"
        )
    logger.info("治理角色已生效:%s", who)


_load_runtime_secret()
_require_governance()

# 配置就位后,把知识树从 S3 拉到本地(read_doc 读它)。延迟 import 让 knowledge_store
# 的 module 级配置读到已填好的 env(KNOWLEDGE_BUCKET 等)。
import knowledge_store  # noqa: E402 —— 必须在 _load_runtime_secret() 之后

_synced = knowledge_store.sync_down()
if knowledge_store.KNOWLEDGE_BUCKET and _synced == 0:
    # 设了桶却一个文件都没同步下来 = read_doc 读到的是空目录。sync_down() 自己
    # 不会因此报错(它对「没设桶」和「设了桶但一片空」返回同一个 0),所以在这里拦。
    raise RuntimeError(
        f"知识桶已设({knowledge_store.KNOWLEDGE_BUCKET})但同步到 0 个文件;容器拒绝启动"
    )

# ————————————————————— 为什么这里要硬失败 —————————————————————
# 三处硬失败:载入 secret、同步知识树、`_require_governance()`。
# 前两处原来都是 logger.exception(...) 然后继续跑。2026-08-20 首次部署时踩到了:
# Runtime exec role 是部署之后才存在的,所以先起来的那批容器还没拿到
# secretsmanager:GetSecretValue,载入 secret 失败 → 记一笔日志继续服务。
#
# 后果不是"报错",而是**静默降级,而且是永久的**:module 级代码一个容器只跑一次,
# 补了 IAM 也不会重跑。那批容器在流量池里活了 20 分钟,期间:
#   · KNOWLEDGE_BUCKET 为空 → sync_down() 走「没设桶就跳过」返回 0 → read_doc
#     读空目录,agent 试了 16 个路径全是「文档不存在」,只好凭记忆猜字段
#   · AGENT_ROLE_ARN 为空 → db.py 直接用 exec role 查数,而 exec role 没有任何
#     数据面权限 → call_metric 报权限错
# 而调用方看到的是 HTTP 200、success: true、73 秒,答案里还从容地写着
# 「治理指标暂时报权限错,我走原始表手查」——读起来完全像个正常的分析过程。
#
# 第三处(`_require_governance()`)补的是同一次事故里**更坏的**那个分支:那天 exec role
# 恰好没有数据面权限,所以查询会报错;要是它有(比如以后有人图省事给 exec role 加了
# Glue/LF 权限),AGENT_ROLE_ARN 为空的容器会**答得又快又对**——只是答案里带上了
# users.email 和私信内容,而没有任何一处会变红。所以它不只查"配没配",还 assume 一次
# 把身份读回来核对:配了一个 assume 不到的 ARN,和没配,后果是一样的。
#
# 一个连自己配置都没拿到的容器不该回答问题。在 import 期 raise 会让这个 microVM
# 起不来、永远不进流量池,请求转到配置齐全的容器上;全都起不来就是整体 5xx——
# 那是**该看见**的故障,比一个嘴上很利索、手里没有数据的 agent 好得多。
#
# 本地开发不受影响:RUNTIME_SECRET_ID / KNOWLEDGE_BUCKET 都不设时三处都不触发。
