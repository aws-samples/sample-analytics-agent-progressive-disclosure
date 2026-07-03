# 部署指南

本项目有三种跑法,按需要选:

| 场景 | 跑什么 | 怎么跑 |
|------|--------|--------|
| **A. 本地数据库** | 只起 Postgres(给 CLI Skill / 看数据用) | `docker compose up -d` |
| **B. 本地 Web App** | Agent SDK + FastAPI + 前端,跑在本机 | `backend/run.sh` |
| **C. 云上部署** | 部署到你自己的 EC2 + CloudFront | 见下文「云上部署」 |

> 数据库统一用 Docker 容器版 Postgres:本地是 `docker-compose.yml` 的 db 容器,云上是 `docker-compose.cloud.yml` 的 db 容器(带持久卷)。

---

## A. 本地数据库(Docker)

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

## B. 本地 Web App

跑那套网页问数(Agent SDK + Bedrock + FastAPI + 前端)。后端默认连**本机 brew Postgres(端口 5433,`backend/.pgdata`)**,不是上面那个 Docker 库(5432)。

```bash
cd backend
./run.sh                          # 自动拉起本地 Postgres(5433) + uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

环境变量、Bedrock 配置、自测命令见 [../backend/README.md](../backend/README.md)。本地默认不开认证(`AUTH_ENABLED` 不设)。

---

## C. 云上部署

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

- **时间范围**:静态样本,数据落在 2025-10-27 ~ 2026-01-24。查"最近 N 天"时以表自身时间列的 `max()` 为锚点,**别用 `current_date`/`now()`**。
- **规模**:35 张表,约 19 万行(各域行数见 [../PROJECT_STATUS.md](../PROJECT_STATUS.md))。
- **重新生成数据**:`cd scripts && python generate_data.py`,输出到 `data/csv/`。

## 常见问题

**Q:Docker 启动失败 / 端口被占**
`lsof -i :5432` 看占用,改 `docker-compose.yml` 端口映射。

**Q:CSV 导入失败**
确保 CSV 列顺序与表结构一致:`head -1 data/csv/<表>.csv` 对比 `psql -c "\d <表>"`。

**Q:Web App 查询超时 / 很慢**
Opus 走文档路由每题要多读几份 md、多几次工具往返,端到端约 25~70 秒,属正常。
