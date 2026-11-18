# 部署指南

现行数据层是 **Athena + S3 Tables (Iceberg) + Glue Data Catalog**(v3 湖仓)。
DDL 在 `database/iceberg/`,建站/装载/对账在 `scripts/lakehouse/`,验收跑
`bash scripts/test_all.sh`(L0–L6;加 `--l8` 追加 21 个缺陷注入负测)。
分层含义与**覆盖面缺口**见 [test-plan.md](test-plan.md)。

> **治理层(L4)要单独跑一步才生效**:建完湖仓默认是 Lake Formation admin 全量授权,
> 也就是**没有列级边界**。跑 `scripts/lakehouse/governance.py --apply` 建 agent 专用角色
> 并发列级授权,再把 `AGENT_ROLE_ARN` 配给后端——`/health` 的 `identity` 字段是从外面
> 看它接上没有的唯一处。**在那之前别把真实数据灌进来。**
>
> 另外注意这不是"带值级掩码的参考实现":Lake Formation 没有值级掩码原语,PII 列是
> **不授权**(不可见),不是**掩码**(可见但被替换)。取舍见 `governance.py` 的 docstring。

历史形态:v1 是本地 Docker Postgres,v2 是 Redshift Serverless。**两者都已退役**——
`database/redshift/`、`scripts/redshift/`、`scripts/glue/` 是死路径,别照着跑。
v2 的设计与退役原因见 [architecture-v2-redshift-glue.md](architecture-v2-redshift-glue.md),
其余旧路径见 [legacy.md](legacy.md)。

本项目的几种跑法,按需要选:

| 场景 | 跑什么 | 怎么跑 |
|------|--------|--------|
| **B. 本地 Web App(现行)** | Agent SDK + FastAPI + 前端跑在本机,数据连 **Athena + S3 Tables** | `backend/run.sh` |
| **A. 本地数据库(v1,legacy)** | 只起容器版 Postgres(35 表、约 19 万行) | `docker compose up -d` |
| **C. 云上部署(EC2,v1,legacy)** | 部署到你自己的 EC2 + CloudFront | 见下文「C. 云上部署」 |
| **D. 云上部署(AgentCore)** | Runtime 跑在 AgentCore,数据仍走 Athena + S3 Tables | `cd analyticsagent && agentcore deploy` |
| **E. 云上前端(AgentCore 版)** | 给 D 配一个能用浏览器登录的入口:CloudFront + Cognito + Fargate 中继 | 见下文「云上前端产生的资源(路径 E)」 |

> **D 的状态**:`analyticsagent/app/analytics/` 原来是 `backend/` 的手工副本(`db.py` 纯 `psycopg`、
> 退款口径偏低约 4%、提示词还在教错的时间锚点)。现在共享代码是生成物,由
> `scripts/deploy/sync_agent_code.py` 从 `backend/` 逐字拷来,L0 跑 `--check` 盯着别再分叉。
> **2026-08-20 首次真部署**,资源清单见下面「AgentCore 部署产生的资源」。
> 部署前置条件(secret 里的 `AGENT_ROLE_ARN`、知识树上传、`networkMode` 保持 PUBLIC)与
> 部署后要单独复验的三个数,写在 [analyticsagent/README.md](../analyticsagent/README.md)。

> ⚠️ **`agentcore/cdk/package.json` 里的版本是钉死的,别顺手升**:
> `aws-cdk-lib` `~2.261.0`、`aws-cdk` `~2.1126.0`。CLI 0.27.1 在 `--yes`(非交互)模式下
> 遇到高于它测过的版本会**硬报错退出**,而它给的建议是「装最新版 CLI」——那是条死路,
> 0.27.1 本身就是最新。升 CDK 之前先确认手上的 CLI 版本容忍它。

### 建湖仓数据层(路径 B 的前置,只做一次)

用 `backend/.venv/bin/python`(或任何装了 boto3 的解释器)按顺序跑:

```bash
python3 scripts/lakehouse/setup.py --verify    # 先只读看一眼现状
python3 scripts/lakehouse/setup.py             # 建表桶 + namespace + workgroup(幂等)

python3 scripts/lakehouse/gen_ddl.py --check   # 声明态是否跟真源一致
python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql   # 48 张表
python3 scripts/lakehouse/load.py              # data/csv/ → Iceberg,35 张基表灌数
                                               # ⚠️ 只在**空湖**上这么跑。已有数据的湖先看
                                               #    下面「数据说明」的「重新灌数前先确认」
python3 scripts/lakehouse/athena.py --file database/iceberg/02_mart.sql     # 派生/集市层

python3 scripts/lakehouse/verify_load.py       # CSV 真源 ⟷ Athena 现查,逐表比行数/求和
python3 scripts/lakehouse/reconcile.py --strict   # DDL ⟷ Glue ⟷ 知识库卡片(比表和列)
python3 scripts/lakehouse/verify_enums.py      # 卡片写的枚举取值 ⟷ 列里实际的值
python3 scripts/lakehouse/verify_mart_parity.py --numbers   # 集市层口径与基表算的一致

# 治理层(L4)。前两条只读,第三条是这一步唯一会写云的命令。
python3 scripts/lakehouse/governance.py --selftest        # 离线:策略 ⟷ 验收契约 ⟷ IAM 策略窄度
python3 scripts/lakehouse/governance.py --verify          # 只读:现在缺什么(角色不存在时 exit 1)
python3 scripts/lakehouse/governance.py --apply           # 建 IAM 角色 + 发 LF 列级授权(幂等)
python3 scripts/lakehouse/governance.py --verify          # 再验一遍:以 agent 角色实测 16 条探针
export AGENT_ROLE_ARN=arn:aws:iam::<账号>:role/analytics-agent-ro   # 或写进 .env.local
python3 scripts/lakehouse/governance.py --verify-backend  # 后端确实在用这个角色查数
```

`--apply` 只建不删:给角色打 `Project` 标签,**遇到同名但没这个标签的角色直接拒绝动它**;
LF 授权只发不撤(LF 权限可叠加,所以多出来的宽授权由 `--verify` 报成漂移,要撤得显式加
`--revoke-extra`)。跑它的身份必须是 Lake Formation 的 data lake admin,否则发不动授权。

`verify_enums.py` 是上面那条 `reconcile.py` 管不到的那一半:reconcile 比的是表和列,
卡片写 `status='active'` 而数据里是 `'on_sale'` 时它照样全绿——SQL 语法对、目录对账过、
**跑出来是空集**,结论直接反过来。所以这两条要一起跑。

`database/0[1-8]_*.sql` 是 DDL 的**唯一真源**,`database/iceberg/01_tables.sql` 由
`gen_ddl.py` 生成、`--check` 断言逐字节一致——别手改后者。
`reconcile.py` 的 `--catalog` 默认读 `GLUE_CATALOG_ID`,那个写法**要带账号前缀**
(`<账号>:s3tablescatalog/<表桶>`),跟 Athena 用的 `s3tablescatalog/<表桶>` 不是一回事,
见 [knowledge/connection.md](../knowledge/connection.md)。
云资源名默认 `analytics-agent-tables`(表桶)/ `analytics-agent-wg`(管理侧 workgroup)
/ `analytics-agent-ro-wg`(agent 专用 workgroup,结果落 `athena-staging/agent/` 子前缀),
要改名复制 `.env.local.example` 成 `.env.local` 覆盖。**两个 workgroup 不能合成一个**:
查询结果 CSV 是明文行数据,共用结果前缀等于让最小权限角色从管理侧的结果文件里
读回 Lake Formation 已经排除掉的列(`scripts/lakehouse/athena.py` 里写了完整理由)。

### AgentCore 部署产生的资源(路径 D)

`agentcore deploy` 走 CDK,所以第一次会先要求 **bootstrap**。标准 bootstrap 的副作用要
知道再点头:它建一个 `CDKToolkit` 栈、一个 `cdk-hnb659fds-assets-<账号>-<区域>` 桶
(**桶名里带账号数字,这是 CDK 的固定命名约定,改不掉**——本仓库其他桶都刻意不带,
这一个是例外)、一个 container-assets ECR 仓库、5 个 IAM 角色,其中 cfn-exec 角色挂的是
`AdministratorAccess`。这跟最小权限是冲突的;要收紧得用 `--cloudformation-execution-policies`
指定更窄的策略,代价是每加一类资源就可能要回来补策略。

部署完之后**手工补三样**,少一样就是一个「起得来但答不出数」的 agent:

```bash
# 1. secret:非敏感坐标(桶名/工作组/目录两种写法/治理角色 ARN/知识桶)
#    键清单见 analyticsagent/app/analytics/runtime_config.py 的 docstring
aws secretsmanager create-secret --name analytics-agent/runtime --secret-string '{...}'

# 2. 知识树上传(镜像里没有,冷启动从 S3 拉)
aws s3 sync knowledge/ s3://<知识桶>/knowledge/ --exclude "*" --include "*.md"

# 3. 把 exec role 加进治理角色 analytics-agent-ro 的**信任策略**。
#    这一步没法在 CDK 里做:被信任方(exec role)是这个栈建的,而信任策略长在
#    另一个角色上、由 scripts/lakehouse/governance.py 管——那个脚本先跑。
#    栈的 GovernanceRoleArnOutput 输出的就是要改的那个角色的 ARN。
#    `governance.py --apply` 是 merge 语义:它把 exec role 并进去,不覆盖原有的
#    开发者 principal(本地跑 backend/run.sh 靠的就是那一条)。
```

exec role 的**内联策略**不在这张手工清单里了 —— 它是
[`analyticsagent/agentcore/cdk/lib/cdk-stack.ts`](../analyticsagent/agentcore/cdk/lib/cdk-stack.ts)
的 `wireExecutionRole()`,`agentcore deploy` 一起发。四条 Allow(读 secret、
`ListBucket` 按 `knowledge/` 前缀限、读知识树、`sts:AssumeRole` 治理角色)加**一条显式
Deny**(`athena:*` / `glue:*` / `s3tables:*` / `lakeformation:*`)。原来这三行字就是全部
真源:角色由 `agentcore deploy` 建出来、权限靠人手 `put-role-policy` 补,于是换个干净
账号重来一次,得到的是一个「起得来、答不出数」的 Runtime,而且没有任何一处断言过这份
策略长什么样。那条 Deny 才是重点:「exec role 零数据面权限」只在没人加过权限时成立,
而加一条(一个 managed policy、一次调试留下的授权)是很像样的一步。显式 Deny 压得住
后来的任何 Allow,`AGENT_ROLE_ARN` 因此一直是拿到数据的唯一路径;它**不**限制 agent
查数——assume 出来的会话是另一个 principal,策略另算。断言在
`analyticsagent/agentcore/cdk/test/cdk.test.ts`(`npx jest`)。

⚠️ **exec role 是部署之后才存在的**,所以先起来的那批容器拿不到 secret。补完 IAM
**旧容器不会自愈**(module 级配置一个容器只跑一次),得让它们换代——重新
`agentcore deploy` 出一个新版本,或 `update-agent-runtime` bump 版本。
`agentcore pause` / `resume` 管不到 runtime(只管 online eval 和 A/B test)。
现在 `runtime_config.py` 对这种情况**硬失败**,那批容器会起不来而不是带着空配置服务;
排查这类问题要看 CloudWatch 的 **log stream 名字**(一个 microVM 一条流),混着看会
把不同容器的日志读成同一个。经过见 [analyticsagent/README.md](../analyticsagent/README.md)。

### 云上前端产生的资源(路径 E)

三个 CloudFormation 栈,**必须按序建**,后一个要前一个的输出:

```bash
R=us-west-2
# ① foundation:最小 VPC(只为中继)+ Cognito 用户池/客户端 + 中继 ECR 仓库 + 站点桶
aws cloudformation deploy --region $R --stack-name analytics-agent-foundation \
  --template-file infra/foundation.yaml
aws cloudformation describe-stacks --region $R --stack-name analytics-agent-foundation \
  --query "Stacks[0].Outputs" --output table     # 下面几步的参数都从这里取

# ② 中继镜像:tag = 源码 hash(内容寻址,理由同 AgentCore 镜像),ARM64
cd functions/ask-relay
TAG=$(cat Dockerfile package.json server.mjs | shasum -a 256 | cut -c1-16)
aws ecr get-login-password --region $R | docker login --username AWS --password-stdin <RelayRepositoryUri 的域名部分>
docker build --platform linux/arm64 -t <RelayRepositoryUri>:$TAG . && docker push <RelayRepositoryUri>:$TAG
cd ../..

# ③ relay:内网 ALB + Fargate 服务(参数取 ① 的 VpcId/SubnetA/SubnetB + ② 的镜像 + Runtime ARN + Cognito 两项)
aws cloudformation deploy --region $R --stack-name analytics-agent-relay \
  --template-file infra/relay.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides VpcId=... SubnetA=... SubnetB=... ImageUri=... \
                        AgentRuntimeArn=... CognitoUserPoolId=... CognitoClientId=...

# ④ edge:CloudFront(S3 OAC 服务静态页 + VPC origin 回源到 ③ 的 ALB)
aws cloudformation deploy --region $R --stack-name analytics-agent-edge \
  --template-file infra/edge.yaml \
  --parameter-overrides SiteBucketName=analytics-agent-site RelayAlbArn=... RelayAlbDomain=...

# ⑤ 发前端(自己去 ① 的输出里取桶名和 Cognito,不用手填)
bash scripts/deploy/deploy_web.sh

# ⑥ 建登录账号(池里 AllowAdminCreateUserOnly,没有自助注册)
aws cognito-idp admin-create-user --region $R --user-pool-id <UserPoolId> \
  --username demo --temporary-password '<12 位以上,含大小写和数字>' --message-action SUPPRESS
```

⑥ 之后账号是 `FORCE_CHANGE_PASSWORD`:前端的 `newPasswordRequired` 分支会在首次登录时
要求改密。池里**没配邮件投递**,所以必须 `--message-action SUPPRESS`(不加会因为发不出
邮件而失败),临时口令只能靠这条命令的执行者带出去。

几处刻意的选择,改之前先读注释:**没有 NAT**(Fargate 任务在有 IGW 路由的子网里带公网 IP
出网,入站仍只有 ALB;理由与"要改回去得改哪三处"见 `infra/foundation.yaml` 顶部注释 ②)、
**Runtime 不进这个 VPC**(`networkMode` 是 `PUBLIC`,VPC 纯粹为中继存在,注释 ①)、
**任务是 ARM64**(便宜约两成且本机原生构建;改架构必须连镜像一起重建,不然是
`exec format error` 反复重启)、**中继用 `X-Id-Token` 而不是 `Authorization`**
(CloudFront 的 origin request policy 不许白名单 `Authorization`,见 `infra/edge.yaml`)。

`deploy_web.sh` 每次都从 foundation 栈的输出**现算一份 `config.js`** 再传,不再是
"桶里手工维护、脚本绕开不动"。那个旧做法的失效模式是静默的:池换了而桶里还是旧 ID,
页面照常打开、登录框照常在,只有点了登录才报 `ResourceNotFoundException`,而部署脚本全绿。
生成物落 `$TMPDIR`,**故意不落 `web/config.js`** —— 本地页面靠"这份文件不存在"来
回退 `/api/config`,它一存在本地就开始拿线上池登录了。

**这条路径按小时花钱**(和只有 D 的时候不同,D 是按调用):ALB 常驻 + Fargate 一个
0.25vCPU/0.5GB 任务常驻 + 任务那个公网 IPv4 地址 + CloudFront 流量。
量级参考(us-west-2,以官方定价页为准):ALB 约 $0.0225/小时 ≈ $16/月 + LCU;
Fargate ARM 约 $0.0099/小时 ≈ $7/月;公网 IP 约 $0.0050/小时 ≈ $3.6/月;
CloudFront 这种用量基本在永久免费额度内。

**不想计费不必拆栈**,三档:

| 档 | 做什么 | 省掉 | 恢复代价 |
|---|---|---|---|
| A | `aws ecs update-service --cluster analytics-agent-relay --service ask-relay --desired-count 0` | Fargate + 公网 IP | 改回 `1`,约 1 分钟(拉镜像 + 健康检查)。域名/密码/前端全不变 |
| B | 删 `analytics-agent-edge` 与 `analytics-agent-relay` 两个栈,**保留 foundation** | 上面全部 + ALB + CloudFront | 两条 `cloudformation deploy`,十几分钟(大头是 CloudFront 铺开)。⚠️ **CloudFront 域名会变** |
| C | 连 foundation 一起删 | 剩下的几分钱 | 要重建 Cognito 池 —— **用户和密码一起没了**,站点桶也要重传 |

**B 档下次起服务不用重装任何东西**:镜像还在 ECR、Cognito 池和用户还在、站点桶里的
`index.html` / `config.js` / `vendor/` 都还在。所以 B 是长期闲置的推荐档,C 只在真的要
彻底清账号时用 —— 它比 B 多省的钱可以忽略,多付的代价是一整套账号重建。

### 日常开关(档 A,URL 不变)

档 A 的做法是把 `ask-relay` 的 `desiredCount` 降到 0,ALB 和 CloudFront 都留着,
所以 CloudFront 分发域名**不会变**,下次开机 URL 照旧。

```bash
R=us-west-2
# 开(约 1 分钟:拉镜像 + ALB 健康检查转 healthy)
aws ecs update-service --cluster analytics-agent-relay --service ask-relay \
  --desired-count 1 --region $R --query "service.desiredCount"
# 等到 running=1 再去点页面
aws ecs wait services-stable --cluster analytics-agent-relay --services ask-relay --region $R

# 关
aws ecs update-service --cluster analytics-agent-relay --service ask-relay \
  --desired-count 0 --region $R --query "service.desiredCount"
```

**关着的样子要认得**:站点 `index.html` 照旧 **200**(CloudFront 直接从 S3 出静态页),
只有 `POST /ask` 变 **503**(ALB 后面没有健康目标)。所以"页面能打开"**不等于**服务开着 ——
在浏览器上表现为登录进去、一提问就报错。

Runtime(AgentCore)这一侧**不用开关**:它按调用计费,闲置不花钱,`agentcore pause/resume`
也管不到它(只管 online eval 和 A/B test)。数据层(S3 Tables / Glue / Athena)只有存储和
按查询的钱,量级是分币。所以"关云上"实际只有上面这一条命令要跑。

### 拆除云资源

**云上前端那条(路径 E)**:按建栈的**反序**删 —— 删 `analytics-agent-edge` 栈
(CloudFront 分发要先 disable 再 delete,CFN 会自己做,但**耗时十几分钟**)
→ 删 `analytics-agent-relay` 栈 → 清空并删站点桶 `analytics-agent-site`
→ 删 ECR 仓库 `analytics-agent-ask-relay` 里的镜像 → 删 `analytics-agent-foundation` 栈。
⚠️ 顺序反了会卡:edge 还在的时候 relay 的 ALB 被 VPC origin 引用着删不掉;
站点桶非空时 foundation 栈删不动(`BucketNotEmpty`)。
Cognito 用户池随 foundation 栈一起删,**里面的用户一并没了**,这是刻意的(demo 账号)。

**AgentCore 那条(路径 D,只在部署过才有)**:删 `AgentCore-analyticsagent-default` 栈
→ 删 `analytics-agent/runtime` secret → 清空并删知识桶 `analytics-agent-knowledge`
(**开了版本控制,要删所有版本才删得掉桶**)→ 删 exec role 上的 `lakehouse-runtime-access`
内联策略 → 把 `analytics-agent-ro` 信任策略里的 exec role 摘掉(**别整份覆盖,开发者
principal 还在里面**)→ 删 ECR 里的镜像。
CDK bootstrap 的那套(`CDKToolkit` 栈 + assets 桶 + container-assets ECR + 5 个角色 +
SSM 版本参数)**是账号级共享的**,这个账号还有别的 CDK 项目就别删。

**湖仓那条**:撤治理层授权(`aws lakeformation revoke-permissions`,principal 是
`arn:aws:iam::<账号>:role/analytics-agent-ro`;`governance.py --verify` 会列出当前有哪些)
→ 删那个 IAM 角色(先 `delete-role-policy --policy-name lakehouse-read` 再 `delete-role`)
→ 删 S3 Tables 表桶 `analytics-agent-tables`(先删表再删 namespace 再删桶)→ 删**两个**
Athena workgroup(`analytics-agent-wg` 和 `analytics-agent-ro-wg`;只删一个的话另一个
会连着它的结果前缀一直留在账号里)→ 清空并删 Athena 查询结果暂存桶(`athena-staging/`
连它下面的 `agent/` 子前缀一起)→ 撤掉 Lake Formation 里给本账号加的授权。**没有 Redshift workgroup 要拆**(v2 遗留说法,已不适用)。

删角色前先把后端的 `AGENT_ROLE_ARN` 清掉,否则它启动时 AssumeRole 失败会直接报错
(那是刻意的:配了却 assume 不到,静默退回 admin 凭证比报错危险得多)。

---

## A. 本地数据库(Docker,v1 legacy)

### 前置
- Docker / Docker Compose,约 500MB 磁盘。

### 步骤
```bash
# 1. 启动 PostgreSQL(首启自动建表 + 灌数据,约 30 秒)
docker compose up -d
docker compose logs -f db        # 看到灌数完成即可

# 2. 验证
docker compose exec db psql -U postgres -d app_analytics -c "SELECT count(*) FROM users;"
```

`docker-compose.yml` 把 `database/`(DDL)、`data/csv/`(数据)、`scripts/docker-init.sh`(显式建表+灌数+重置序列)挂进容器首启脚本里。

| 连接项 | 值 |
|--------|-----|
| Host / Port | localhost / **5432** |
| Database | app_analytics |
| User / Password | postgres / postgres |

手动探数用 `scripts/dbquery.sh`(`docker exec` 进本地库):
```bash
./scripts/dbquery.sh "SELECT count(*) FROM events;"
```

---

## B. 本地 Web App(现行)

跑那套网页问数(Agent SDK + Bedrock + FastAPI + 前端)。后端默认 `DB_BACKEND=athena`,
经 Athena 查 S3 Tables 上的 Iceberg 表(需要可用的 AWS 凭证;`run.sh` 启动时会
`aws sts get-caller-identity` 自检)。资源名不走默认命名时,复制 `.env.local.example`
成 `.env.local` 覆盖。

```bash
cd backend
./run.sh                          # 凭证自检 → uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

`run.sh` 的 `.env.local` 加载器会**跳过已在环境里的变量**,所以命令行传的优先:
`AWS_REGION=us-east-1 ./run.sh` 真的会走 us-east-1。健康检查 `curl :8000/health`
会回当前 backend、region 和目录名,连错地方一眼能看出来。

要走 v1 本地库(legacy):`DB_BACKEND=postgres ./run.sh`,会拉起本机 brew Postgres
(端口 5433,`backend/.pgdata`)——需要装了 `postgresql@16`,没装就只能用下面 A 的 Docker 方式。

环境变量、Bedrock 配置、自测命令见 [../backend/README.md](../backend/README.md)。本地默认不开认证(`AUTH_ENABLED` 不设)。

---

## C. 云上部署(EC2,v1 legacy)

把 Demo 部署到你自己的 AWS 账号(EC2 + CloudFront + Cognito)。前置:一台能跑 Docker 的 EC2、一个 Cognito 用户池 + app 客户端(公共客户端,用 SRP 登录)、一个 CloudFront 分发指向 EC2。EC2 实例角色需有调用 Bedrock 所用模型的权限。

### 架构
```
浏览器 ─HTTPS→ CloudFront(默认证书,转发 POST/SSE,源读超时 60s)
                  │ HTTP:80
                  ▼
              EC2(docker-compose.cloud.yml)
                ├─ analytics-app   FastAPI + Agent SDK + claude CLI,代码烤进镜像(无挂载,uvicorn 无 reload)
                └─ analytics-db    postgres:16,带 pgdata 持久卷,首启灌 35 表 19 万行
```
Bedrock 经 EC2 实例角色走 IMDS 取凭证(hop limit=2),模型 `global.anthropic.claude-opus-4-8`。

### 认证(app 层 Cognito)
前端 `amazon-cognito-identity-js` SRP 登录拿 idToken → 后端 `server.py` 用 JWKS 校验。CloudFront / 边缘不碰认证,所以静态资源能正常缓存、登录后刷新快。`/app/vendor/*` 走长缓存行为,其余 `no-store`。

> `docker-compose.cloud.yml` 从环境变量注入 `AUTH_ENABLED` 和 `COGNITO_*`。把你自己的 `COGNITO_USER_POOL_ID` / `COGNITO_CLIENT_ID` 写进项目根的 `.env`(见 `.env.example`),compose 会自动读取。本地开发不设 `AUTH_ENABLED` 即关闭认证。

### 部署(在 EC2 上)
把代码拉到 EC2,在项目根:
```bash
# 先把 Cognito 的 pool / client id 填进 .env(见 .env.example)
docker compose -f docker-compose.cloud.yml up -d --build
```
db 容器首启会自动建表灌数;app 容器构建镜像时把 `backend/` + `web/` + `knowledge/` 一起打进去。改代码后重跑 `up -d --build` 即可生效(app 镜像无挂载、uvicorn 无 reload,靠重建;db 有持久卷,不会重灌)。

### 安全组
EC2 入站 80 只对 CloudFront 的托管前缀列表(`com.amazonaws.global.cloudfront.origin-facing`)开放,**不要用 `0.0.0.0/0`**。

### 拆除
terminate EC2 → 删 SG → 删 instance-profile / role → 删 Cognito 用户池(含域名) → 清空并删 S3 桶 → disable & delete CloudFront 分发。

### 踩过的坑
1. `claude` CLI 拒绝以 root 跑 `--dangerously-skip-permissions`(SDK 的 bypassPermissions 会下发该 flag)→ Dockerfile 必须建非 root 用户(appuser uid 10001)跑。
2. CloudFront `DefaultRootObject=app/index.html`,`/` 直接服务 app 页 → `index.html` 里资源引用必须用**绝对** `/app/vendor/...`,相对 `./vendor` 会解析成 `/vendor` 404。
3. 前端 API 地址判断按 `location.protocol`,别用 `location.port`(HTTPS 默认端口下会误判退回本地 8000)。

---

## 数据说明

- **时间范围**:静态样本,业务日历落在 2025-10-26 ~ 2026-01-24。查"最近 N 天"时统一以
  `(SELECT max(as_of_date) FROM meta_snapshot)` 为锚点,**别用 `current_date`/`now()`**,
  也**别用各表自己的 `max(dt)`**(`fin_daily_revenue` 到 2026-02-02,
  `channel_daily_costs`/`mart_channel_daily` 到 2026-09-01,按各自 max 取窗口会互相错位)。
- **规模(仓库交付的那一份)**:`data/csv/` 是 `scripts/gen/main.py --scale 1` 的小样,
  灌完是 Glue 目录 **48 张表、约 22 万行**(35 张基表 189,672 + 8 张派生 27,982 +
  4 张集市 2,432 + 1 张 meta)。照上面「建湖仓数据层」从空湖跑一遍,得到的就是这一份。
- **⚠️ 湖里当前装的是哪一批,和上面这个数字不是一回事**。这两件事必须分开读:前者是
  仓库里有什么,后者是某个 AWS 账号的湖此刻有什么,后者会在文档底下被人改掉。
  **本仓库开发账号 2026-09-17 实测**:湖里是 **8000 万行**那一批(`scale≈427`),不是种子:

  | 表 | 湖里现查 | `data/csv/` 种子 | 倍数 |
  |---|---|---|---|
  | `page_views` | 12,895,806 | 30,196 | 427× |
  | `user_coupons` | 9,709,436 | 22,735 | 427× |
  | `events` | 8,541,400 | 20,000 | 427× |
  | `order_items` | 1,804,371 | 4,225 | 427× |
  | `orders` | 854,140 | 2,000 | 427× |
  | `users` | 213,535 | 500 | 427× |
  | `products` | 4,133 | 200 | 20.7×(按 `budget.py` 声明生成) |
  | `coupons` / `ad_campaigns` | 150 / 50 | 150 / 50 | 1×(透传,**没随规模走**) |

  它是 `scripts/gen/main.py --target-rows 80000000` 出 parquet、再由
  `scripts/lakehouse/load_parquet.py` 灌进去的 —— **不是** `load.py` 从 `data/csv/` 灌的。
  那两个脚本随「数据层重灌至 8000 万行」那一批(分支 `feat/data-reload-80m`)进来,
  **不在本分支上**,所以在本分支的代码里找不到能产出这个规模的路径,这不是矛盾。
- **⚠️ 重新灌数前先确认湖里是哪一批**。`load.py` 的幂等是**逐表 `DELETE FROM` 再
  `INSERT`**,幂等的前提是两次灌的是同一份源。在一个已经装了 8000 万行的湖上跑它,
  等于拿 2,000 单订单**覆盖掉** 854,140 单,而且它会正常退出、`verify_load.py` 随后
  还会全绿(它比的就是 `data/csv/` ⟷ Athena)。判据是一条命令:

  ```bash
  AWS_REGION=us-west-2 backend/.venv/bin/python -c "
  import sys; sys.path.insert(0, 'scripts/lakehouse')
  from athena import Client
  print(Client().execute('SELECT count(*) FROM orders')['rows'])"
  # 2,000 → 种子;854,140 → 8000 万那批,别跑 load.py
  ```

  确认是种子之后,改完 `data/csv/` 再 `python3 scripts/lakehouse/load.py`,并跑
  **全量** `verify_load.py`(35 张表)对账。
- **上面那张表本身没有任何检查盯着**:对账层比的是表、列、枚举取值
  (`reconcile.py` / `verify_enums.py`),**不比文档散文里的行数**。所以这段话过期的时候
  它是绿的 —— 这正是本仓库反复栽的那个失效模式。看到它就顺手用上面那条命令核一遍。
- **这不是真实规模,放大到 8000 万也不是**:25 种事件、15 个页面、6 种流量来源的分布
  几乎完全均匀,漏斗算不出衰减、热门榜排的是噪声。**这些是抽样方式的性质,不随行数变**
  ——灌到 8000 万只会让噪声的样本变大,形状照旧,统计形状一律不可外推。细节见
  [data-walkthrough.md](data-walkthrough.md) 铁律三。

## 常见问题

**Q:Docker 启动失败 / 端口被占**
`lsof -i :5432` 看占用,改 `docker-compose.yml` 端口映射。

**Q:CSV 导入失败**
确保 CSV 列顺序与表结构一致:`head -1 data/csv/<表>.csv` 对比 `psql -c "\d <表>"`。

**Q:Web App 查询超时 / 很慢**
Opus 走文档路由每题要多读几份 md、多几次工具往返,端到端约 25~70 秒,属正常。
