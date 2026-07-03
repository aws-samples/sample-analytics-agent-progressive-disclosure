# 演进记录

这个项目不是一次成型的,它从一个 CLI Skill 长成了一个上了云的网页问数应用。这份文档记录它怎么走到现在,方便接手的人理解每个目录为什么存在。

核心假设从头到尾没变:**把数据库 metadata 拆成一棵按需翻阅的 md 文档树,让 Agent 顺路由逐层读出来再写 SQL,比把全库 schema 塞进上下文、或每次从头探索数据库更准更省。** 三个阶段都是这同一个想法在不同形态下的验证。

---

## 阶段一:CLI Agent Skill(起点)

**目标**:在 Claude Code CLI 里做一个数据分析 Skill,用 Progressive Disclosure 按需加载 schema,对比当时流行的多 subagent 方案(每次从头探索、慢、token 贵)。

- 设计了内容+电商混合型 APP 的数据模型:**35 张表 / 8 个业务域**,足够复杂才能体现按需加载的价值。
- 全部 35 张表的 DDL(`database/01-08_*_domain.sql`)+ schema 总览(`database/00_schema_overview.md`)。
- 模块化 Python 数据生成器(`scripts/generators/`),`generate_data.py` 生成约 **19 万行**模拟数据,导出成 35 个 CSV。
- 数据字典本体:一棵 md 文档树(`domains/` 三层 + `metrics/` + `relationships.md`),按路由渐进式披露表结构。
- 早期曾把它放在 `.claude/skills/` 下当 Claude Code CLI skill,还分化出「快捷模板版」和「纯路由版」两套变体做对比;产品化后 CLI skill 那套已弃用,文档树统一收敛为顶层 `knowledge/`(见阶段二/重构说明)。

> 这一阶段最初的数据库跑在 AWS Aurora Serverless v2(ap-northeast-1)+ EKS 里的 pgweb 管理界面上。**该套基建已废弃**,现在本地用 Docker、云上用 EC2 自带的 Postgres 容器。老的 Aurora/EKS 部署方式不再维护。

## 阶段二:产品化为独立 Web App

**目标**:把 Skill 的能力从 CLI 里拿出来,做成一个谁都能打开的网页问数工具,大脑脱离 Claude Code CLI。

- 用 **Claude Agent SDK** 自建 Agent,跑在 **Amazon Bedrock 的 Claude Opus 4.8**(`global.` 跨区推理 profile)上。
- 自建 3 个进程内 MCP 工具(`backend/tools.py`):
  - `read_doc` —— 渐进式披露的核心,按路由逐层读数据字典 md(`domains/_index.md` → 域 index → 表 doc)。
  - `run_sql` —— 只读单条 SELECT,安全边界在 `db.py`。
  - `present_result` —— 交付 KPI / 图表 spec / 洞察 / 追问。
- 早期工具版本用 `get_table_schema` 查 `information_schema` 拿结构;**后来重构成上面的文档路由版**——让 Agent 真正去读那棵 Skill 文档树,而不是查系统表,这样渐进式披露的过程才看得见、可演示。
- 前端 `web/index.html`:把每一步"正在读哪份文档"实时铺开,配合工作流时间线、逐步计时、SQL/结果/图表展示,做成多层渐进式披露的 Demo。后端不可达时自动回退离线模拟数据。
- 数据字典文档树在顶层 `knowledge/`(单一真源),`read_doc` 读它、镜像 `COPY knowledge/` 打进去。

## 阶段三:上云部署

**目标**:部署成一个可长期访问的在线 Demo。

- **EC2 + CloudFront**,两容器:`analytics-app`(FastAPI+Agent SDK)+ `analytics-db`(postgres:16,首启灌 35 表 19 万行)。
- 认证:**app 层 Cognito**(前端 SRP 登录拿 idToken,后端 JWKS 校验),CloudFront / 边缘不碰认证,静态资源可正常缓存,登录后刷新很快。
- 完整部署步骤、认证配置、拆除步骤见 [docs/deployment.md](docs/deployment.md)。

## 阶段四:叠加治理层,覆盖 text-to-insight

**目标**:前三阶段都跑在 35 张**原始表**上,AI 干的本质是 **text-to-ETL**(现场 join、定口径、写复杂 SQL)。但真实生产里业务方面对的往往是**治理后的数据集**。这一阶段在同一套底层数据上叠一层"治理后"的集市表,让一个 demo 同时讲两种范式。

- 新增 `database/09_mart.sql`:4 张 `mart_` 预聚合表(`mart_daily_kpi` / `mart_daily_revenue` / `mart_channel_daily` / `mart_user_summary`),用 CTAS 从原始表跑出来。这段 ETL SQL 本身就是"text-to-ETL 的成品答案",GMV / 新客 / 归因 / 复购等口径在这层被**冻结**。
- 新增知识库文档:`knowledge/domains/mart/` 表卡片 + `knowledge/metrics/governed_metrics.md`(官方指标字典,与 `backend/metrics_def.py` 的 `call_metric` 口径一一对应)。
- `backend/agent.py` 系统提示教 Agent **分层**:取数/看明细走原始域(text-to-ETL);诊断/复盘/综合判断走治理层(text-to-insight),对干净表写简单 SELECT、多角度切片、下判断。
- 前端预设拆成两组:**原始数据·问数** vs **治理层·洞察**,两种范式一眼可见。
- `scripts/docker-init.sh` 在原始表灌完后追加一步构建 mart。现阶段不做 IaC,治理层就是 SQL,随数据库一起重建。

---

## 当前状态

四个阶段都已跑通并验证:本地 CLI Skill 能用、本地 Web App 能跑(含治理层)、可部署到云上(EC2 + CloudFront)。可用于:

1. 演示 Agent Skill 渐进式披露 / 文档路由对 text-to-SQL 准确性的提升。
2. 演示同一套数据上的两种范式:原始表上的 **text-to-ETL**(取数)与治理层上的 **text-to-insight**(诊断/归因/判断)。
3. 作为数据分析类 Agent 的参考实现。
4. 部署一份分享给团队体验。

> 注:治理层(阶段四)已在本地与云上部署验证——Agent 能正确分层、自动归因并主动披露"未归因占比 / 残月口径";`/health` 与前端交互均确认生效。

## 数据规模

| 域 | 表数 | 约行数 |
|----|------|--------|
| 用户域 | 5 | ~4,500 |
| 商品域 | 3 | ~900 |
| 行为域 | 4 | ~55,000 |
| 社交域 | 6 | ~87,000 |
| 归因域 | 5 | ~1,500 |
| 营销域 | 5 | ~33,000 |
| 实验域 | 3 | ~1,900 |
| 交易域 | 4 | ~8,000 |
| **合计** | **35** | **~190,000** |

治理层(mart)不额外造数,是从上面这些原始表 CTAS 派生出来的 4 张预聚合表(每日/渠道/用户粒度),随库一起构建。
