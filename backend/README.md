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
   └─ present_result   交付 KPI / 图表 spec / 洞察 / 追问
   │
   ▼
PostgreSQL 16(本地 backend/.pgdata:5433,或云上 db 容器)· 库名 app_analytics
```

- `agent.py` —— 系统提示(强制"先读文档再写 SQL"的工作流 + chart 约定 + 时间/业务口径)、`ClaudeAgentOptions`、把 SDK 事件流解析成给前端的 UI 事件(`stage`/`sql`/`rows`/`doc_detail`/`result`/`done`)。
- `tools.py` —— 三个进程内 MCP 工具。
- `db.py` —— 只读连接、SQL 安全校验(仅 SELECT/WITH、单条语句、只读连接、15s 超时、≤1000 行)。
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

> 历史:早期工具版本用 `get_table_schema` 查 `information_schema` 拿结构(`db.py` 里还留着 `get_schema` 这个未用函数)。现已重构为上面的文档路由版——读 Skill 文档树而非系统表,过程才可演示。

## 本地启动

前置:
- 本机 Postgres@16(brew);`run.sh` 会在 `backend/.pgdata` 建集群(端口 5433)并灌入 35 表 / 全部 CSV。
- venv:`backend/.venv`,依赖见 `requirements.txt`(`claude-agent-sdk`、`fastapi`、`uvicorn`、`psycopg`、`PyJWT[crypto]`)。
- Bedrock:确保当前 AWS 账号已在目标区域开通所用模型的访问权限(本项目默认 `global.anthropic.claude-opus-4-8`,跨区推理 profile)。凭证走标准链(`~/.aws` / 环境变量 / 实例角色)。

```bash
cd backend
./run.sh                      # 自动拉起本地 Postgres(5433) + uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

环境变量(`run.sh` 已设默认值,可覆盖):

| 变量 | 默认 | 说明 |
|------|------|------|
| `CLAUDE_CODE_USE_BEDROCK` | `1` | 走 Bedrock |
| `AWS_REGION` | `us-east-1` | 有 Opus 4.8 的区 |
| `ANTHROPIC_MODEL` | `global.anthropic.claude-opus-4-8` | 全局跨区推理 profile(禁裸 ID / `us.` / `eu.` 前缀) |
| `PGPORT` | `5433` | 本地库端口(`PGHOST`/`PGDATABASE`/`PGUSER` 同见 run.sh / db.py) |
| `PORT` | `8000` | 服务端口 |
| `AUTH_ENABLED` | 不设=关 | 设 `1` 开 app 层 Cognito 校验(本地默认关) |

AWS 凭证走标准链(`~/.aws` / 环境变量 / 实例角色)。

## 认证(app 层 Cognito)

为了让 CloudFront 能正常缓存静态资源,认证不放在边缘,而放在应用层:

- 前端 `amazon-cognito-identity-js` 做 SRP 登录,拿到 idToken 存 localStorage,调 `/ask` 时带 `Authorization: Bearer <idToken>`。
- 后端 `server.py` 用 `PyJWKClient` 拉 JWKS 校验 ID token(`AUTH_ENABLED=1` + `COGNITO_REGION`/`COGNITO_USER_POOL_ID`/`COGNITO_CLIENT_ID` 经 compose env 注入)。
- `/ask` 需鉴权;`/health`、`/api/config`、静态资源公开。`/api/config` 只回公开值(pool id / client id),前端据此初始化登录浮层。
- 本地开发不设 `AUTH_ENABLED` 即关闭认证,直接用。

## 路由一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | DB 与配置自检(公开) |
| GET | `/api/config` | 前端初始化 Cognito 登录用(公开,只含公开值) |
| POST | `/ask` | body `{question, session_id?}`,返回 `text/event-stream`;开认证时需 Bearer ID token |
| GET | `/` | 重定向到 `/app/index.html` |
| | `/app/*` | 托管 `../web` 静态资源 |

`/app/vendor/*`(echarts/字体/cognito-sdk 等)发长缓存头,其余 `no-store`。

## 自测

```bash
# 大脑最小全链路(不开服务)
cd backend && .venv/bin/python test_agent.py "各商品品类的销量排行"

# 健康检查
curl -s http://127.0.0.1:8000/health
```

## 上云部署

云上用 `../docker-compose.cloud.yml`(两容器:app + db),跑在 EC2 上、前面挂 CloudFront。改后端代码因镜像无挂载、uvicorn 无 reload,走 S3 → host → `docker cp` 进容器 → restart 的方式。完整步骤、资源 ID、踩坑、拆除见 [../docs/deployment.md](../docs/deployment.md)。

## 已知事项

- 前端在后端不可达时**自动回退**离线演示(模拟数据),`web/index.html` 单独双击也能看 UI。
- 多轮上下文:当前每次提问是独立会话(已捕获 `session_id`,如需续接可在 `/ask` 传回)。
- SQL 安全边界全在 `db.py`:仅 `SELECT`/`WITH`、单条语句、只读连接、15s 超时、最多 1000 行。
- 生产化可迁到 AgentCore Runtime 托管。
