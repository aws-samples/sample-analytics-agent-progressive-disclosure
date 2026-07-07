[English](README.md) | **中文**

# Analytics Agent · Progressive Disclosure

一个"问数"Demo:用大白话提问,Agent 自己定位表、写对 SQL、查数、出图给结论。

它真正想证明的是一件事——**把数据库的表结构写成一棵可按需翻阅的"数据字典"md 文档树,让 Agent 顺着路由一层层读出来再写 SQL,会比"一股脑把全库 schema 塞进上下文"或"每次从头探索数据库"更准、更省**。这套机制就是 Agent Skill 的渐进式披露(progressive disclosure)。

数据集故意做得很杂:一个内容+电商混合型 APP(类似小红书/得物),35 张表、8 个业务域、约 19 万行模拟数据。表一多,口径陷阱就多(GMV 算 total 还是实付?优惠券核销在模板表还是领取表?A/B 该不该掐时间窗?),"先读对文档再写 SQL"的价值才显出来。

![数据分析 Agent 架构图(动态)](docs/architecture.svg)

> 这张图在渲染后的 README 里会动(GitHub 把 SVG 当图片嵌入)。蓝色 = 进行中的请求,青色 = 流式返回,绿色 = 冷启动时从 S3 同步知识树,天蓝 = `run_sql` 经 VPC 打到 Aurora。`/ask` 这一跳走 CloudFront VPC origin 到内网 ALB 和 Fargate 中继,由中继校验 Cognito JWT 并透传 AgentCore Runtime 的 SSE 流。

## 它长什么样

打开后左侧有 6 个示例题(由易到难:30 天 GMV → 订阅套餐 → 转化漏斗 → 优惠券核销 → 渠道 CAC → A/B 实验),Agent 会把每一步"正在读哪份文档"实时铺开,看得见渐进式披露的全过程。前端右上角有 **EN / 中** 语言切换。大脑是 **Claude Agent SDK** 自建的 Agent,跑在 **Amazon Bedrock**(默认 `global.anthropic.claude-opus-4-8`,`global.` 跨区推理)上,不依赖 Claude Code CLI。单题端到端约 25~70 秒(Opus 要多读几份 md、多几次工具往返)。注意:UI **外壳**可切中英,但**分析内容**(洞察 / SQL / 结果)仍以中文返回,因为 Agent 读的知识库是中文。

本地跑起来见下面「[本地跑起来](#本地跑起来)」;部署到自己的 AWS 账号见 [docs/deployment.md](docs/deployment.md)。

## 项目的三层结构

这个项目其实是同一个想法在三个层面的落地,看文档前先有个整体认识:

```
┌──────────────────────────────────────────────────────────────┐
│  3) Web App(独立产品化)  backend/ + web/                      │
│     Claude Agent SDK + Bedrock + FastAPI,自建 5 个 MCP 工具:  │
│     read_doc/run_sql/call_metric/compute_stats/present_result │
│     前端 SSE 实时披露 —— 谁都能打开的网页问数工具             │
├──────────────────────────────────────────────────────────────┤
│  2) 知识库(数据字典 + 路由规则)  knowledge/                   │
│     一棵 md 文档树:domains/_index.md(L1 路由)               │
│       → domains/<域>/_index.md(L2 选表)                      │
│       → domains/<域>/<表>.md(L3 拿字段/枚举/示例)            │
│     + metrics/(指标口径) + relationships.md(表间关系)       │
├──────────────────────────────────────────────────────────────┤
│  1) 数据层(可复现的样本库)  database/ + data/ + scripts/      │
│     8 个域的 DDL + Python 数据生成器 + 导出的 35 个 CSV        │
│     一键灌进 PostgreSQL,35 表 ~19 万行                        │
└──────────────────────────────────────────────────────────────┘
```

- **数据层**是地基:DDL 建 35 张表,`scripts/generate_data.py` 生成模拟数据导出成 CSV,Docker 首启自动灌库。
- **知识库** 是核心资产:`knowledge/` 那棵 md 文档树就是"数据字典",由 Web App 的 `read_doc` 工具按路由逐层读取、并打包进镜像,是 Agent 唯一的知识来源。
- **Web App** 把这套能力产品化:做成一个谁都能打开的网页问数工具,并把"读文档→写 SQL→出图"的全过程可视化。

## 知识库(数据字典)

`knowledge/` 是 Agent 的单一知识库,一棵 md 文档树、8 域齐全 + 治理层 mart:

- `domains/`：三层路由(`_index.md` 总索引 → 域索引 → 单表卡片),表结构、字段枚举、示例 SQL 都在这。
- `metrics/`：指标口径,其中 `governed_metrics.md` 是治理层官方口径,与 `backend/metrics_def.py` 的 `call_metric` 工具一一对应(口径以代码为准)。
- `analysis/`：5 个深度分析方法的 SOP + 统计公式。
- `relationships.md`：跨表 JOIN 的关联键。

细节见 [knowledge/README.md](knowledge/README.md)。知识库文档树保持中文。

> 早期这棵树曾放在 `.claude/skills/` 下当 Claude Code CLI skill(还做过「快捷模板」与「纯路由」两套变体对照）。产品化后统一收敛为顶层 `knowledge/`,CLI skill 那套已移除。

## 两种范式:取数(text-to-ETL)与洞察(text-to-insight)

同一套数据做了两层,一个 demo 讲两件事:

- **原始数据 · 问数(text-to-ETL)**:35 张原始明细表。问"30 天 GMV""转化漏斗""渠道 CAC"这类取数题,Agent 现场 join 多张表、自己定口径、写复杂 SQL,难点在把逻辑构造对、别踩口径陷阱。
- **治理层 · 洞察(text-to-insight)**:在原始表之上用 `database/09_mart.sql` 叠了 4 张"治理后"的预聚合表(`mart_*`),GMV/新客/归因/复购等口径**冻结**在表里和 `metrics/governed_metrics.md`。问"复盘月度 GMV、谁带动的""这周业务咋样""复购率"这类判断题,Agent 对干净表写简单 SELECT,把力气花在多角度切片、归因、下结论上。

这对应真实世界的两种场景:治理之前,AI 帮你把脏数据算成对的数;治理之后,脏活管道做完了,AI 帮你分析、归因、下判断。前端左侧预设按这两组分开摆,点一题就看出区别。Agent 怎么在两层间选,写在 `backend/agent.py` 的系统提示里(取数走原始域,诊断/复盘/综合判断走治理层)。

## 前置条件

- **AWS 账号 + 已开通 Amazon Bedrock 模型访问**:在 Bedrock 控制台为你的 Region 申请所用模型(默认 Claude Opus 4.8)的访问权限。本项目通过跨区推理 profile(`global.anthropic.claude-opus-4-8`)调用。
- **AWS 凭证**:走标准链(`~/.aws`、环境变量或 EC2 实例角色)。
- **Docker + Docker Compose**:用于数据库容器(及完整云上栈)。
- **Python 3.11**:后端运行。
- **PostgreSQL 16**:仅本地(非 Docker)Web App 路径(`backend/run.sh`)需要。
- **Node.js 20+ 与 Claude Code CLI**:`npm install -g @anthropic-ai/claude-code`。Claude Agent SDK 会把 `claude` CLI 作为子进程拉起,必须在 `PATH` 上。(Docker 镜像已内置;本地 `run.sh` 路径需你自己装。)

> **成本**:跑它不是免费的。每个问题都会调用 Amazon Bedrock 上的 Claude Opus(数十秒推理 + 多轮工具往返),云上部署还额外跑一台 EC2 和一个 CloudFront 分发——按标准 Bedrock token 与基础设施计费。用完记得拆除云上资源(见 [清理](#清理))。

## 本地跑起来

**1. 起本地数据库**(Docker,首启自动建表 + 灌数据,约 30 秒):

```bash
docker compose up -d
docker compose logs -f db          # 看到灌数完成即可
# 验证:
docker compose exec db psql -U postgres -d app_analytics -c \
  "SELECT (SELECT count(*) FROM users) users, (SELECT count(*) FROM events) events, (SELECT count(*) FROM orders) orders;"
```

需要手动敲 SQL 探数时,用 `scripts/dbquery.sh "SELECT ..."`(`docker exec` 进本地库)。

**2. 跑 Web App**(Agent SDK + FastAPI + 前端):

```bash
# 前置:本机 Postgres@16(brew)、Python venv、可用的 Bedrock 凭证(账号已在目标区域开通模型访问)
cd backend
./run.sh                            # 自动拉起本地 Postgres(5433) + uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

> `backend/run.sh` 假设 macOS(Apple Silicon)的 Homebrew Postgres 在 `/opt/homebrew/opt/postgresql@16/bin`。Intel macOS 或 Linux 上需改脚本里的 `PGBIN`(或直接用第 1 步的 Docker 库,把 `PGHOST`/`PGPORT` 指过去)。

后端架构、环境变量、自测命令见 [backend/README.md](backend/README.md)。
后端不可达时,`web/index.html` 会自动回退到离线演示(内置模拟数据),单独双击也能看 UI。

## 项目结构

```
sample-analytics-agent-progressive-disclosure/
├── README.md                    # 英文门面
├── README.zh-CN.md              # 本文件:中文门面
├── PROJECT_STATUS.md            # 演进记录(CLI Skill → Web App → 上云)
├── docs/deployment.md           # 部署指南(本地 + 云上)
├── docker-compose.yml           # 本地:仅数据库容器
├── docker-compose.cloud.yml     # 云上:数据库 + FastAPI 应用两容器
│
├── database/                    # ① 数据层 · DDL
│   ├── 00_schema_overview.md
│   ├── 01-08_*_domain.sql        # 8 个业务域的建表语句(原始明细)
│   └── 09_mart.sql               # 治理层:4 张预聚合表(CTAS,口径冻结)
├── data/csv/                    # ① 模拟数据(35 个 CSV)
├── scripts/                     # ① 数据生成器 + 灌库/查询脚本
│   ├── generate_data.py · generators/
│   ├── docker-init.sh            # Docker 首启建表+灌数
│   └── dbquery.sh                # 本地探数(docker exec)
│
├── knowledge/                   # ② 知识库 · 数据字典 md 文档树(read_doc 读它,保持中文)
│   ├── README.md                 #   知识库说明 + 单一真源约定
│   ├── domains/_index.md         #   3 层:总索引→域索引→单表卡片
│   ├── domains/<域>/<表>.md      #   原始明细表(8 域)
│   ├── domains/mart/             #   治理层 4 张表卡片(text-to-insight)
│   ├── metrics/                  #   指标口径(含 governed_metrics.md 官方字典)
│   ├── analysis/                 #   5 个深度分析方法 SOP + 统计公式
│   └── relationships.md          #   表间关系
│
├── backend/                     # ③ Web App · 大脑(Agent SDK + Bedrock)
│   ├── agent.py                  #   系统提示 + 事件流解析
│   ├── tools.py                  #   MCP 工具:read_doc/run_sql/call_metric/compute_stats/present_result
│   ├── metrics_def.py · metric_layer.py · stats.py  # 指标即代码 + 统计计算
│   ├── db.py                     #   只读 SQL 安全边界
│   ├── server.py                 #   FastAPI + SSE,托管前端
│   └── run.sh · Dockerfile · requirements.txt
└── web/index.html               # ③ Web App · 前端(渐进式披露 UI,含 EN/中 切换)
```

## 数据概览

| 业务域 | 表数 | 代表表 |
|--------|------|--------|
| 用户域 | 5 | users, user_profiles, user_devices, user_segments, user_segment_members |
| 行为域 | 4 | events, sessions, page_views, event_definitions |
| 交易域 | 4 | orders, order_items, payments, subscriptions |
| 商品域 | 3 | products, categories, product_tags |
| 社交域 | 6 | posts, post_likes, post_comments, post_shares, user_follows, user_messages |
| 营销域 | 5 | campaigns, coupons, user_coupons, banners, push_notifications |
| 归因域 | 5 | channels, ad_campaigns, ad_creatives, channel_daily_costs, user_attributions |
| 实验域 | 3 | ab_tests, ab_test_variants, ab_test_assignments |

合计 **35 张表 / 约 19 万行**。完整 schema 见 [database/00_schema_overview.md](database/00_schema_overview.md)。

在这 35 张原始表之上,还叠了一层**治理后的数据集市**:4 张 `mart_` 预聚合表(`database/09_mart.sql` 用 CTAS 从原始表派生,口径冻结),专门给"洞察"类问题用,见上面「两种范式」。

> **时间口径提醒**:这是一份静态样本,数据落在 2025-10-27 ~ 2026-01-24。问"最近 N 天/近期"时,要以表自身时间列的 `max()` 作为"今天"锚点,**别用 `current_date`/`now()`**(会落在数据区间外查出空结果)。Web App 的系统提示已强制这一点。

## 能问什么

用户分析(DAU/MAU、留存、画像、分群)、交易分析(GMV、客单价、转化漏斗、订阅)、商品分析(销量排行、品类分布)、社交分析(内容互动、KOL、关系链)、营销分析(活动效果、优惠券核销、推送)、渠道分析(归因、CAC、ROI)、实验分析(A/B 变体对比)。

一份按难度分级的问题清单见 [test_questions.md](test_questions.md)。

## 安全

- **只读 SQL 边界**:所有生成的查询都经 `backend/db.py`——强制单条只读 `SELECT`/`WITH`(禁写关键字、只读连接、语句超时、行数上限)。
- **可选 app 层认证**:云上部署前置 Amazon Cognito(SRP 登录 + JWKS 校验);本地默认不开(`AUTH_ENABLED` 不设)。
- **静态扫描抑制**:少量已知误报用 inline 注释抑制(`# nosec` / `# nosemgrep`):数据生成器用 `random` 造演示数据(非加密用途)、指标编译器从可信注册表拼 SQL(filter 值经转义)、前端 `innerHTML` 渲染 app / 模型生成内容(回显用户文本已 HTML 转义)。均为经审阅的误报。

上报安全问题请按 [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) 的指引,**不要**开公开 issue。

## 清理

本地跑法不会在 AWS 留下任何运行中的资源。**云上**部署用完请拆除:terminate EC2、删除安全组 / IAM 实例配置+角色 / Cognito 用户池 / S3 桶,并 disable + delete CloudFront 分发。步骤见 [docs/deployment.md](docs/deployment.md)。

## License

本项目采用 MIT-0(MIT No Attribution)许可,见 [LICENSE](LICENSE)。
