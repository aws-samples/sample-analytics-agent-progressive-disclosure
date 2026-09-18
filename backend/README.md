# App Analytics Agent — 后端

把 CLI 里的数据分析 Skill 产品化成一个**独立 Web 应用**:大脑用 **Claude Agent SDK** 自建 Agent,跑在 **Amazon Bedrock 的 Claude Opus 4.8** 上,脱离 Claude Code CLI;前端是 `../web` 那套渐进式披露 UI。

## 架构

```
浏览器 (../web/index.html)
   │  SSE
   ▼
FastAPI (server.py)
   │
   ▼
Claude Agent SDK  ←——  Amazon Bedrock (global.anthropic.claude-opus-4-8)
   │  进程内 MCP 工具(tools.py)
   ├─ read_doc         渐进式披露的核心:按路由逐层读数据字典 md 文档树
   ├─ run_sql          只读单条 SELECT(db.validate 强制安全)
   ├─ call_metric      治理层官方口径(metrics_def.py,指标即代码)
   ├─ compute_stats    统计计算(stats.py)
   └─ present_result   交付 KPI / 图表 spec / 洞察 / 追问
   │
   ▼
DB_BACKEND=athena(默认):S3 Tables(Iceberg)· Athena API(HTTPS + IAM,无 host/port)
DB_BACKEND=postgres(v1 legacy):本地 backend/.pgdata:5433 或云上 db 容器 · psycopg
```

- `agent.py` —— 系统提示(强制"先读文档再写 SQL"的工作流 + chart 约定 + 时间/业务口径)、`ClaudeAgentOptions`、把 SDK 事件流解析成给前端的 UI 事件(`stage`/`sql`/`rows`/`doc_detail`/`result`/`done`)。
- `tools.py` —— 五个进程内 MCP 工具。
- `db.py` —— SQL 安全校验(仅 SELECT/WITH、单条语句、15s 超时、≤1000 行)+ 双后端分派:`athena`(默认,Athena API,复用 `scripts/lakehouse/athena.py`)/ `postgres`(v1 legacy,psycopg 只读连接)。只读闸门两个后端共用。
- `catalog.py` —— `/api/catalog` 的装配逻辑:Glue Data Catalog + `information_schema` + `knowledge/domains/` + `schema_manifest.yaml` 聚成 UI 直接渲染的元数据(表清单/行数/分层/治理现状),Glue 读不到就只用 `information_schema` 并在 `source` 字段里说明。
- `server.py` —— FastAPI + SSE,托管 `../web` 静态页,处理 app 层 Cognito 鉴权。
- 数据字典 md 文档树在顶层 `../knowledge/`(与 `backend/` 同级,是 agent 的单一知识库),`read_doc` 就读这棵树;打镜像时 `COPY knowledge/` 进 `/app/knowledge`。见 [../knowledge/README.md](../knowledge/README.md)。

### read_doc 怎么体现渐进式披露

Agent 脑子里**没有**表结构,系统提示强制它每个问题都走一遍:

```
read_doc("domains/_index.md")          # L1:按关键词判断落在哪个业务域
   → read_doc("domains/<域>/_index.md") # L2:看这个域有哪些表、定位到要用的表
   → read_doc("domains/<域>/<表>.md")   # L3:拿准确字段、枚举值、示例 SQL
   (指标公式读 metrics/*.md,多表 JOIN 读 relationships.md)
→ run_sql(...)                          # 读够了再写 SQL
→ present_result(...)                   # 交付结论
```

`read_doc` 做了路径逃逸防护(只能读 `knowledge/` 内的 `.md`),文档不存在时回一份同目录可读清单帮 Agent 自我纠偏。前端把每次 `read_doc` 渲染成"⟳ 正在读取 <path>"步骤 + 文件查看器,这就是看得见的渐进式披露。

> 历史:早期工具版本用 `get_table_schema` 查 `information_schema` 拿结构。现已重构为上面的文档路由版——读 Skill 文档树而非系统表,过程才可演示。那个旧实现(`get_schema` / `_get_schema_athena` / `_describe_comments`)在 `db.py` 里当了很久的**未用函数**,2026-08-28 删掉了:留着会让人以为 Glue 列注释进得了 agent 上下文,而 agent 看到的表结构只有 `knowledge/` 卡片一个来源。删之前把那段代码里两件实测出来的 Athena 怪癖留在了 `db.py` 的模块 docstring 里。

## 本地启动

前置:
- venv:`backend/.venv`,依赖见 `requirements.txt`(`claude-agent-sdk`、`fastapi`、`uvicorn`、`boto3`、`PyJWT[crypto]`;`psycopg` 仅 v1 legacy 路径用到)。
- AWS 凭证:走标准链(`~/.aws` / 环境变量 / 实例角色)。默认后端调 Athena / Glue 靠 IAM,`run.sh` 启动时会 `aws sts get-caller-identity` 自检。
- 一个灌好数据、已联邦进 Glue 的 S3 表桶 + 一个 Athena workgroup(建法见 `../scripts/lakehouse/setup.py` 与 [../docs/deployment.md](../docs/deployment.md))。
- Bedrock:确保当前 AWS 账号已在目标区域开通所用模型的访问权限(本项目默认 `global.anthropic.claude-opus-4-8`,跨区推理 profile)。
- *(仅 v1 legacy 路径)* 本机 Postgres@16(brew);`DB_BACKEND=postgres` 时 `run.sh` 会在 `backend/.pgdata` 建集群(端口 5433)并灌入 35 表 / 全部 CSV。

```bash
cd backend
./run.sh                      # 凭证自检 → uvicorn(8000);DB_BACKEND=postgres ./run.sh 走 v1 路径
# 打开 http://127.0.0.1:8000/
```

环境变量(`run.sh` 已设默认值,可覆盖;本地覆盖建议写 `.env.local`,见 `.env.local.example`):

| 变量 | 默认 | 说明 |
|------|------|------|
| `DB_BACKEND` | `athena` | `athena`(现行)/ `postgres`(v1 legacy) |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | 走 Bedrock |
| `AWS_REGION` | `us-west-2`(athena)/ `us-east-1`(postgres) | athena 后端与表桶 / Glue / workgroup 同区 |
| `ANTHROPIC_MODEL` | `global.anthropic.claude-opus-4-8` | 全局跨区推理 profile(禁裸 ID / `us.` / `eu.` 前缀) |
| `ATHENA_WORKGROUP` | `analytics-agent-wg` | 管理侧 Athena workgroup(带查询结果位置)。**没设 `AGENT_ROLE_ARN` 时**走这个 |
| `ATHENA_AGENT_WORKGROUP` | `analytics-agent-ro-wg` | 治理角色专用 workgroup,结果落在 `athena-staging/agent/` 子前缀下。**设了 `AGENT_ROLE_ARN` 时**自动走这个:查询结果 CSV 是明文行数据,共用一个 workgroup 等于让 agent 从管理侧的结果文件里读回 LF 已排除的 `users.email`。两个值**不要**设成同一个 |
| `S3_TABLE_BUCKET` | `analytics-agent-tables` | S3 表桶名 |
| `ICEBERG_NAMESPACE` | `app_analytics` | 表桶 namespace = Athena 里的 database 名 |
| `ATHENA_CATALOG` | `s3tablescatalog/<表桶>` | Athena 侧目录名(**不带**账号前缀) |
| `GLUE_CATALOG_ID` | `<账号>:s3tablescatalog/<表桶>` | UI 元数据来源,Glue API 用的 ID(**带**账号前缀);账号启动时从 sts 现算。不配则 `/api/catalog` 只用 `information_schema` |
| `AGENT_ROLE_ARN` | 不设 | 设了就 AssumeRole 用这个最小权限角色查数(L4 治理层):`user_messages` 整表读不到,`users.email` / `phone`、`user_profiles.birth_date` 不在授权面里。**不设＝用进程自己的凭证**(本地常是 admin,那时没有列级边界)。角色用 `scripts/lakehouse/governance.py --apply` 建 |
| `PGPORT` | `5433` | (仅 postgres 后端)本地库端口 |
| `PORT` | `8000` | 服务端口 |
| `AUTH_ENABLED` | 不设=关 | 设 `1` 开 app 层 Cognito 校验(本地默认关) |

## 认证(app 层 Cognito)

为了让 CloudFront 能正常缓存静态资源,认证不放在边缘,而放在应用层:

- 前端 `amazon-cognito-identity-js` 做 SRP 登录,拿到 idToken 存 localStorage,调 `/ask` 时带 `Authorization: Bearer <idToken>`。
- 后端 `server.py` 用 `PyJWKClient` 拉 JWKS 校验 ID token(`AUTH_ENABLED=1` + `COGNITO_REGION`/`COGNITO_USER_POOL_ID`/`COGNITO_CLIENT_ID` 经 compose env 注入)。
- `/ask` 需鉴权;`/health`、`/api/config`、静态资源公开。`/api/config` 只回公开值(pool id / client id),前端据此初始化登录浮层。
- 本地开发不设 `AUTH_ENABLED` 即关闭认证,直接用。

## 路由一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | DB 与配置自检(公开);`db` 字段按后端给出身份信息(engine/workgroup 或 host/port),其中 `identity` 是**实际查数用的身份**——设了 `AGENT_ROLE_ARN` 时是那个受限角色的 ARN,空＝用的是进程自己的凭证。这是从外面看治理接上没有的唯一处 |
| GET | `/api/catalog` | UI 的元数据来源:表清单/字段/行数/分层/治理现状(缓存 5 分钟,`?refresh=1` 跳过) |
| GET | `/api/config` | 前端初始化 Cognito 登录用(公开,只含公开值) |
| POST | `/ask` | body `{question, session_id?}`,返回 `text/event-stream`;开认证时需 Bearer ID token |
| GET | `/` | 重定向到 `/app/index.html` |
| | `/app/*` | 托管 `../web` 静态资源 |

`/app/vendor/*`(echarts/字体/cognito-sdk 等)发长缓存头,其余 `no-store`。

## 自测

```bash
# 只读 SQL 边界自测(不连网、不看后端;10 类拒绝 + 9 条放行 + 1 项已知过度拒绝)
python3 backend/db.py

# 治理层(L4):策略自测(离线)→ 以 agent 角色实测 → 后端确实在用它查
python3 scripts/lakehouse/governance.py --selftest
python3 scripts/lakehouse/governance.py --verify
python3 scripts/lakehouse/governance.py --verify-backend

# 大脑最小全链路(不开服务)
cd backend && .venv/bin/python test_agent.py "各商品品类的销量排行"

# 健康检查
curl -s http://127.0.0.1:8000/health
```

第一条挂进了 `scripts/test_all.sh` 的 L0,它守的是 `validate()`,也就是**「不许写」**这道闸;
后三条是 L4,守的是**「不许看」**。两条边界互不覆盖——授权面里那些表 agent 是有 SELECT 的,
所以只读闸不能省;反过来只读闸也拦不住 `SELECT email`。

在这两个自测之前,整套测试里没有一条断言碰过 `validate()`——把 `_FORBIDDEN` 改松不会让
任何东西变红。反向验证(该报时真会报)在 `scripts/negative_tests.py`:
`readonly-guard-hole` 盯只读闸,三个 `gov-*` 盯治理层(策略被改松 / 探针其实没在探 /
角色建好了但后端仍用 admin 凭证)。见 [../docs/test-plan.md](../docs/test-plan.md)。

## 上云部署

现行形态是 AgentCore Runtime + Fargate 中继 + CloudFront(见 [../PROJECT_STATUS.md](../PROJECT_STATUS.md) 阶段五/六);前端与元数据快照用 `../scripts/deploy/deploy_web.sh` 发布。v1 的 EC2 两容器形态(`../docker-compose.cloud.yml`)留作 legacy。完整步骤见 [../docs/deployment.md](../docs/deployment.md)。

## 已知事项

- 前端在后端不可达时**自动回退**离线演示(模拟数据,冻结在 v1),`web/index.html` 单独双击也能看 UI。
- 多轮上下文:当前每次提问是独立会话(已捕获 `session_id`,如需续接可在 `/ask` 传回)。
- SQL 安全边界全在 `db.py`:仅 `SELECT`/`WITH`、单条语句、15s 超时、最多 1000 行;与后端无关,切 `DB_BACKEND` 不削弱。**能读到什么**不在这里,在 `AGENT_ROLE_ARN` 那个角色的 Lake Formation 授权面上。
- `AGENT_ROLE_ARN` 配了却 assume 不到时,`_athena()` **直接抛错**而不是退回自己的凭证——静默降级成 admin 身份查数比启动失败危险得多。这个错在启动/`/health` 时就炸,不会等 agent 答到一半。
- 本地这个 FastAPI 不只是 demo,还是测试与构建设施(`scripts/test_all.sh` L6、eval、`build_catalog_json.py` 都依赖它),别当 legacy 砍——边界见 [../docs/legacy.md](../docs/legacy.md)。
