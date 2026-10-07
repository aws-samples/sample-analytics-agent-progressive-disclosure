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

线上现装的是 2026-08-31 全量重灌的那批(scale 427.07 / seed 42 / 业务轴止 2026-01-24,
逐表行数记在 `data/loaded_row_counts.json`)。右列是重灌前 `data/csv/` 那批种子数据,
留着是因为本地跑测试和 L8 注入用的还是它。

| 域 | 表数 | 线上实测行数 | (旧)种子数据 |
|----|------|--------|--------|
| 用户域 | 5 | 1,299,157 | ~4,500 |
| 商品域 | 3 | 14,958 | ~900 |
| 行为域 | 4 | 23,572,581 | ~55,000 |
| 社交域 | 6 | 36,764,749 | ~87,000 |
| 归因域 | 5 | 152,481 | ~1,500 |
| 营销域 | 5 | 13,980,455 | ~33,000 |
| 实验域 | 3 | 790,122 | ~1,900 |
| 交易域 | 4 | 3,369,255 | ~8,000 |
| **合计** | **35** | **79,943,758** | **~190,000** |

放大倍数不是一律 427 倍:`budget.py` 把表分成 `fixed`(维表照原样)、`sub`(亚线性)、
`fact`(随规模线性)三类,所以商品域只有 14,958 行而社交域到了 3,676 万。

治理层(mart)不额外造数,是从上面这些原始表 CTAS 派生出来的 4 张预聚合表(每日/渠道/用户粒度),随库一起构建。

---

## 阶段五:AgentCore-native 重构(已上线)

> 状态:**已部署并端到端实测跑通**(us-west-2,浏览器同款请求验证:登录 → 提问 → SSE 流式返回真实 Aurora 数据)。EC2 两容器形态被整体替换。

把「EC2 + Docker Postgres」重构成 serverless 形态:

- **AgentCore Runtime** 托管 Claude Agent SDK(`analyticsagent/`,BYO Container:python + Node20 + claude CLI 非 root 运行)。冷启动做三件事:读 `analytics-agent/runtime` secret 注入配置 → 从 S3 同步知识树(75 个 md)到本地 → 惰性起暖 `ClaudeSDKClient` 跨调用复用,摊薄 CLI 进程拉起成本。**前两件事任一失败就 `raise`**,让这个 microVM 起不来而不是带着空配置进流量池——首次部署时正是这里静默降级过,经过见 [analyticsagent/README.md](analyticsagent/README.md)。
- **知识库放 S3**(`knowledge/` 前缀),更新知识不必重建镜像;`db.py` 连接参数惰性读取,消除「import 早于配置注入」的冷启动竞态。
- **Aurora Serverless v2(PG16)**,Runtime 走 **VPC 私有连接**(psycopg 5432),只读单语句安全边界原样保留;35 表 19 万行由一次性 VPC seeder Lambda 灌入。
- 前端 S3 + CloudFront(OAC);认证仍是 app 层 Cognito(SRP 登录拿 idToken)。
- **`/ask` 中继:Fargate + 内网 ALB + CloudFront VPC origin**(`functions/ask-relay/` + `infra/relay.yaml`)。原计划的 Lambda Function URL 方案(代码已移除)被账号的组织 SCP 堵死——公开 URL 和 Cognito 联合角色的 IAM 调用都被拒,而 CloudFront OAC 又签不了带 body 的 POST。改用同账号 graph-cmdb 已验证的模式:CloudFront 经 VPC origin 私网回源到内网 ALB(SG 只放行 CloudFront origin-facing 前缀列表,零公网暴露、零签名),JWT 走 `X-Id-Token` 直达中继校验后 `InvokeAgentRuntime` 透传 SSE(15s 心跳防读超时)。

![架构:AgentCore-native](docs/architecture.svg)

基建三个栈:`infra/foundation.yaml`(VPC/端点/NAT/Aurora/桶)、`infra/relay.yaml`(ECS+ALB)、`infra/edge.yaml`(CloudFront);Runtime 由 `@aws/agentcore` CDK CLI 部署(`analyticsagent/agentcore/`)。

> ⚠️ 括号里那句「VPC/端点/NAT/Aurora/桶」是**当时**的 `foundation.yaml`。它 2026-08-23
> 重写过:Aurora、端点、NAT 都删了(数据层换成 Athena 之后 Runtime 不进 VPC)。
> 现行内容见变更日志的 2026-08-23「浏览器入口补齐」和那个文件的顶部注释。

有意思的是,阶段一最初就跑在 Aurora Serverless v2 + EKS 上,后来为省事退回 EC2;这次是带着 AgentCore 重新走回 serverless。

---

## 阶段六:数据层换代 —— Redshift Serverless + Glue Data Catalog(已被阶段七取代)

> 状态:**已迁移并全链路验收**(对账、一致性、21/21 评测、端点与渲染契约,`bash scripts/test_all.sh`)。Aurora 退役。
>
> ⚠️ **本节是历史记录,不是现行架构**:Redshift 已在阶段七整体退役,下面写的 8000 万行、
> Data API、`analytics_agent_ro` + 动态脱敏那套治理**现在都不生效了**。内容原样保留(改了
> 就是伪造历史),现行形态见下面的阶段七。

阶段五之后 v1 的两个自认短板:35 表 19 万行说服力不足(全 schema 塞 context 也就几千 token),元数据是手写 md、没人验证。这一阶段把两头都换掉,完整设计与踩坑见 [docs/architecture-v2-redshift-glue.md](docs/architecture-v2-redshift-glue.md):

- **数据搬到 Redshift Serverless**(约 8000 万行,`scripts/gen/` 按 v1 数据 427 倍等比放大,业务比例与口径陷阱原样保留)。查询走 **Data API**(HTTPS + IAM):Runtime 不再需要 VPC 连接、连接池和落地密码,workgroup 保持 `publiclyAccessible=false`。
- **元数据拆成三方并对账**:声明态(`schema_manifest.yaml` + DDL,进 git)/ 实际态(Glue Data Catalog,生成的)/ 语义层(`knowledge/` 卡片),`scripts/glue/reconcile.py` 三方两两比,首跑抓出 5 处真实文档漂移。
- **治理下沉到数仓**(`database/redshift/04_governance.sql`):最小权限角色 `analytics_agent_ro`(48 张表授 45 张,私信表不授权)+ `users.email`/`users.phone`/`user_profiles.birth_date` 动态脱敏,均实测生效。
- **UI 读目录**:新增 `GET /api/catalog`(`backend/catalog.py`),前端元数据不再写死在 HTML;线上用部署期快照 `web/catalog.json`(`scripts/deploy/`),带降级闸门。
- **评测与基线**:`eval/` 21 条金标在 Redshift 上全过;金标保持 Postgres 方言、运行时改写(`scripts/gen/pg_to_redshift.py`);一致性基线由生成器直接吐预期值,对账「生成 → COPY」全链路无损。

v1 的本地 Postgres 路径(`docker-compose.cloud.yml`、`db.py`/`run.sh` 的 postgres 分支、顶层 `database/*.sql`)**保留但不再维护**,边界见 [docs/legacy.md](docs/legacy.md)。

---

## 阶段七:湖仓化 —— S3 Tables(Iceberg)+ Athena(现行)

> 状态:**本地全链路跑通并验收**(2026-08-24:`bash scripts/test_all.sh --l8`
> **39 PASS / 0 FAIL**,L0–L6 全绿 + L8 的 **38 个负测 38/38**(`/ask` 流式契约上一轮单独跑过);
> L7 **27/27**(2026-08-24 重跑,工具闸门改成 `PreToolUse` hook 之后),
> 模型 `global.anthropic.claude-opus-4-8`,均耗时 51.9s/题 · 读文档 2.4 次 · SQL 0.8 条,
> 报告 `eval/report.md`、基线归档在
> `eval/baseline/eval.lakehouse-athena.post-funnel-retention-fix.md`/`.json`)。
> 这一轮是**漏斗与留存口径修完后的第一次全量**:`L4-funnel` 的判定理由从
> 「命中 golden[all-time distinct users]」变成「命中 golden[all-time **subset funnel**]」——
> 上一轮那份 26/26 通过率一样,但判的是**错口径**的金标。新增的 `L5-retention-cohort`
> 是唯一一道会因为"结论写错"而判错的题(判分模式 `retention`,先验结论再比数值),
> 实际判定理由是「结论已声明数据限制;命中 golden[weekly matrix pct] 16/16 个数值」。
> 上一份 26/26 基线(`post-anchor-fix.*`)保留为历史,**两轮之间的差别不在通过率**。
> Redshift 整体退役。
> 验收分层与覆盖面缺口写在 [docs/test-plan.md](docs/test-plan.md)。
> **数据层治理(L4)已实现并在云上验过**(2026-08-20,见变更日志同日那条):专属 IAM 角色 +
> Lake Formation 列级授权,`AGENT_ROLE_ARN` 接进后端,三条断言(离线 / agent 角色实测 /
> 后端接线)全绿。**云上副本 `analyticsagent/` 已与 `backend/` 同源**
> (共享代码改成生成物 + `--check`,见变更日志 2026-08-20「云上副本」那条),已于 **2026-08-20 首次部署到
> AgentCore Runtime**,并在 **2026-08-23 漏斗/留存口径修完后重新部署(runtime v3)且五条复验全绿**:
> 退款全量 963,560.92、渠道 GMV 与本地 `compiled_sql` 逐字一致、漏斗 `394→314→258→199`、
> 留存 4 个完整窗 cohort 16 个数值全中且结论声明了数据不可用、CloudTrail 证实生效身份是
> 治理角色 `analytics-agent-ro`。首次部署踩到的坑
> (exec role 晚于容器存在 → 那批容器带着空配置永久降级、`success: true` 却一个数都没有)
> 记在 [analyticsagent/README.md](analyticsagent/README.md);已在 `runtime_config.py` 改成硬失败。

阶段六把数据放进了 Redshift,但为此背上了一整层仓库形态的东西:workgroup、datashare、
RPU 容量与用量上限、跨区(数据在 ap-northeast-1、Web 在 us-west-2)。这一阶段把数据层
换成**表桶本身就是目录**的形态:

- **S3 Tables(Apache Iceberg)+ Amazon Athena**,全部收到 us-west-2。表桶联邦进 Glue 之后
  namespace 直接映射成一个 Glue database,整层 datashare 消失;查询是 Athena API 调用
  (HTTPS + IAM),按扫描字节计费——没有"空转"的容量成本,也没有需要保持温热的东西。
- **工具链 `scripts/lakehouse/`**:`setup.py`(建表桶 / namespace / workgroup / 联邦进 Glue)
  → `gen_ddl.py`(从 `database/0[1-8]_*.sql` **生成** `database/iceberg/01_tables.sql`,
  `--check` 断言仓库里那份与现生成的逐字相同)→ `load.py`(灌 `data/csv/`)→ `reconcile.py`
  (声明态 ⟷ Glue 实际态 ⟷ 知识库三方对账)→ `verify_load.py` / `verify_enums.py` /
  `verify_mart_parity.py` / `verify_doc_sql.py`。
  规模:`data/csv/` 种子灌完是 Glue 目录 48 张表、约 22 万行(其中 35 张原始表约 19 万行)。
  **这是仓库交付的那一份,不等于某个账号的湖此刻装了什么**——本仓库开发账号 2026-09-17
  实测已经是 8000 万行那一批(`orders` 854,140 而不是 2,000,由 `feat/data-reload-80m` 的
  `load_parquet.py` 灌的,那个脚本不在本分支上)。逐表对照、核查命令、以及「在已有数据的湖上
  跑 `load.py` 会覆盖掉它」这条告警,都写在 `docs/deployment.md` 的「数据说明」。
- **方言换成 Trino**,这是搬迁的主要代价:`::` 强转、`date + 7`、`DISTINCT ON`、
  `interval '30 days'` 全都不合法。金标 SQL 仍是口径单一真源(Postgres 方言),运行时由
  `scripts/gen/pg_to_trino.py` 改写;系统提示、指标 SQL、知识库示例则直接改成
  `interval '30' day` + `CAST`。
- **指标即函数调用**:`backend/metrics_def.py` + `metric_layer.py` 把治理口径编译成 SQL,
  经 `call_metric` 工具调用,`metrics/governed_metrics.md` 由注册表生成。GMV / CAC / ROI /
  退款只有一个权威数,不再每道题现推一遍。
- **统一业务日历锚点**:所有"今天"锚到 `(SELECT max(as_of_date) FROM meta_snapshot)`
  = 2026-01-24,不再用每张表自己的 `max(dt)`——因为各表时间轴末端并不齐
  (`fin_daily_revenue` 到 2026-02-02,`channel_daily_costs` 到 2026-09-01)。这个 bug 实测
  过一次:「哪个渠道 CAC 最低」因此答成了一个在业务日历内根本没花钱的渠道。是否 clamp
  按指标区分(cac/roi clamp,`refund_amount` 不 clamp)。

这一阶段修掉的都是**同一类缺陷:不报错、数看着合理、结论是反的**。除上面的锚点问题,还有两个:

- **判分器不认数量级单位**:KPI 卡片按中文习惯写 `{"value": 144.99, "unit": "万"}`,金标 SQL
  出的是 1449872.13,于是「各渠道投放一共花了多少钱」被判 fail——而 agent 的 SQL、数据源、
  明细、总数全对。`eval/run_eval.py` 的 `agent_numbers()` 改成按 **agent 自己声明的 unit**
  同时收原值和换算值。这类漏判比漏抓错数更坏:它会逼着以后写用例的人去放宽容差。
- **`AWS_REGION` 依赖 import 顺序**:`backend/agent.py` 在 import 时把 `AWS_REGION` 写进进程
  环境,而数据层(`scripts/lakehouse/athena.py`、`backend/catalog.py`)读的是同一个变量。
  它原来兜底成 `us-east-1`,于是谁先 import 谁说话:先 import agent 的入口会让 Athena 客户端
  跑去 us-east-1,报 `WorkGroup is not found`——错误指向工作组,成因却是 region。已对齐成
  `us-west-2`。
- **一致性基线改成实时对账**:`scripts/lakehouse/verify_load.py` 不再读那份生成器时代的
  `consistency.generator-expected.json`(它记的是 21 万行时代的绝对值,与现在的 `data/csv/`
  已经不是同一批数据),改成 CSV ⟷ Athena 实时逐表比对。原话写在脚本头上:
  **基线会过期,而且过期时是绿的**。
---

## 变更日志

按日期倒序。每条只写四件事:**症状 → 成因 → 改法 → 现在谁看着它**。
排查过程本身不在这里——结论都落在了它该住的地方(`docs/test-plan.md` 的分层与
「没有自动化覆盖」清单、各脚本的 docstring、`knowledge/` 卡片顶部的 ⚠️、
`analyticsagent/README.md` 的部署复验清单),每条给出入口。
计数(负测用例数、断言数、同步节点数)记的是**当时**的数,后来都涨过,现值以脚本自己
打印的为准。

### 2026-09-16 · 评审整改

外部评审提的问题按「先修完评审项、再谈别的」的顺序过了一遍,值得记进日志的是这几处:

- **AgentCore exec role 的授权是靠 CDK 自己接的**,不再依赖手工补策略;`cdk.test.ts` 用
  `Fn::Join` 展平后按 `Sid` 找,不再用匹配不上的 `Match.objectLike`。
- **`athena.py::split_statements` 自包含**:原来跨模块借了一份实现,借来的那份改了它会
  静默跟着变。
- **治理角色的信任策略改成合并而不是覆盖**——覆盖会把别人加的信任关系抹掉。
- **提示词里补了「数据治理边界」一节**:被治理挡住的列/表**重试 0 次**,直说边界并给替代
  口径(人群用 `user_level`/`is_vip`/`registration_source`,年龄用 `age`),**列级排除不是
  脱敏**(库里是明文,不许在输出里写"已脱敏"),治理拒绝照常出 `present_result`;
  拼错列名/写错方言仍走原来的「回去 read_doc 重写、最多 2 次」。
- **`bytes_scanned` 有了消费者**:`run_sql` 的返回与工具描述都点明它是成本读数;硬闸仍然
  只有 workgroup 的 `BytesScannedCutoffPerQuery`,这条软/硬的分界写在 `docs/test-plan.md`。
- **`eval/cases_traps.json` 有了入口**(`run_eval.py --cases`,报告分文件落盘)+ 离线结构
  断言;三条 `value_hint` 改成只描述机制——原来钉的是 v1 本地的数字,云上早已不是那个量级。
- **治理探针加了 Iceberg 元数据表两条**(`users$files` / `users$snapshots`):agent 角色读它们
  报 `TABLE_NOT_FOUND`,admin 读得到,所以这两条量的是**权限**而不是"S3 Tables 不暴露元数据"。
  同时 `sts:AssumeRole` 被拒不再被当成"探针红了"——那会把一次基建故障读成四条绿灯。
- **同步器开始比 `ClaudeAgentOptions` 的 kwargs**:节点清单只保证 `DENIED_BUILTINS` 的
  *定义*同源,不保证它*被传进去*,两侧各写一次而无人比对时,一侧收紧边界另一侧不会红。
- **负测的 6 条 expect 正则收紧**:原来 `谓词|不对齐|缺|❌`、`channel|表头|列`、`漂移`
  这类接近空匹配,换个原因红了也算过——而负测要证的恰恰是"红对了原因"。
- **接上了离线 CI**(`.github/workflows/offline.yml`)。为此给 `test_all.sh` 加了 `--l0`:
  在这之前"只跑离线那几十条"唯一的办法是整套跑下去、在 AWS 身份那道闸上吃一个 `exit 1`,
  于是全绿的 L0 被包在非零退出码里,任何按退出码判成败的东西都读成失败。`--l0` 与
  `--full/--l8/--ask` 互斥并显式报错——静默忽略的话 `--l0 --l8` 会打印一份 L0 全绿、
  退出 0、而负测一条没跑的报告。CI 的覆盖边界写在 workflow 顶部,见「明确没做的部分」。

### 2026-09-17 · 文档说的规模 ⟷ 湖里实际的规模,差 427 倍且没有一盏灯

实测发现五处文档都在说「当前部署约 22 万行,数据源是仓库里的 `data/csv/`」,而湖里早已是
**8000 万行**那一批(`orders` 854,140 ⟷ 种子 2,000;`page_views` 1,289 万 ⟷ 3.0 万;
`products` 4,133 ⟷ 200)。**这五句写的时候都是对的,是湖在它们底下被换掉了**——重灌由
`feat/data-reload-80m` 的 `load_parquet.py` 完成,那个脚本不在本分支上,所以在本分支的代码
里找不到能产出这个规模的路径。

**为什么一层都没红**:`reconcile.py` 比表和列、`verify_enums.py` 比枚举取值集合(不比它旁边
那列「实测行数」)、`verify_load.py` 比 `data/csv/` ⟷ Athena。**没有一个比文档散文里的数字**,
所以这种过期是绿的——正是本库反复警告的那个失效模式,这次栽在自己文档上。

**代价不是"数字不好看",是一条可执行的破坏指令**:`deployment.md` 原文写着「改完 `data/csv/`
后跑 `load.py`」,而 `load.py` 的幂等是逐表 `DELETE FROM` + `INSERT`,幂等的前提是两次灌的是
同一份源。照着做就是拿 2,000 单订单覆盖掉 854,140 单,命令正常退出,`verify_load.py` 随后
**全绿**(两边一致正是覆盖成功的结果)。

这次改的:把「仓库交付的那一份」和「某个账号的湖此刻装的那一份」在五处拆开写(`deployment.md`
数据说明 / `data-audit.md` 横幅 / `data-walkthrough.md` 重跑说明与铁律三 /
`database/00_schema_overview.md` / 本文件),给 `deployment.md` 和 `load.py` 的灌数指令各加一条
"先核查再灌"的告警(判据就一句 `SELECT count(*) FROM orders`),并在 `test-plan.md` 的
「没有自动化覆盖」里记成一栏。**没改数据、没改测试、没重跑 L7**;L0 复跑 33/0。
`test-plan.md` 里那节「v3 的库整体都是 v1 规模」刻意留着不删,它记的是同一个潜伏缺陷显形的
全过程——当时预言的"谁灌一次全量它就当场显形"已经兑现,而维度侧仍未跟上
(970 万条核销只指向 150 张券)。

**判据还是缺的**:「文档声明的规模 ⟷ 湖的实际规模」没有检查。补它要先决定声明写在哪(候选
是一份 `docs/scale.json`,文档和检查都从它读),属于下一批。

### 2026-09-17 · 四盏红灯:三盏是「比错了东西」,一盏是缺陷自己消失了

`bash scripts/test_all.sh` 在本仓库开发账号上是 **通过 50 · 失败 4**。四条一开始被我读成
"只能解释、不能修"——**错的**。逐条量下来,四条都有明确的机制和明确的修法,而且修法已经
落在栈里的下一个 PR(`feat/data-reload-80m`,重灌那一支)上;我在 `/tmp/wt15` 开了一个临时
worktree,拿那支的检查器对**同一个湖**跑了一遍,四条全绿。四条的共同点是:**它们比的两端
不是同一批数据**——本分支交付 `data/csv/` 种子(189,707 行),湖里是 8000 万行那批。

| 红灯 | 机制(实测) | 修在哪 | 验证输出 |
|---|---|---|---|
| L2 枚举一致 | 只有一列:`sessions.utm_campaign`。卡片写的是 v1 的 6 个占位值(`summer_sale`…)并带「实测行数 745/732/…」,湖里是生成器 `tables.py::CAMPAIGNS` 的 **39 个**真活动名(各 ~4.6–4.7 万行)+ 315,925 NULL | #15 重生卡片(39 个活动名,**去掉**实测行数列——那一列本身就是会过期的绿灯) | `枚举一致 ✅  4 列取值与卡片相符` |
| L2 退化列登记 | **38** 条清单过期(不是日志上那 18 条,见下)。13 条 `RELOAD_PENDING` 已被重灌兑现(`users.created_at` 等)、24 条 `ALL_NULL_PINNED` 现在有值(`page_views.page_url` 1,289 万个非空)、1 条 `DIMS_PASSTHROUGH` 不再是常量(`channel_daily_costs.created_at`——它其实**是**生成表,2,799 行)。这是设计意图:清单条目的清除条件一旦兑现也判 FAIL | #15 把 `RELOAD_PENDING` 清空、`DIMS_PASSTHROUGH` 收到 15 列 / 11 张表、新增第四本 `BY_DESIGN_CONST`,并把数组列纳入普查 | `退化列全部登记在册 ✅  427 个标量列基数正常 + 6 个数组列有真元素,0 待重灌 + 15 透传遗留 + 2 刻意常量 + 24 整列 NULL 已登记` |
| L3 装载完整 | 比的是 `data/csv/` ⟷ Athena,而湖装的是 `~/analytics-agent-data/genFULL_csv`(scale 427.07 / seed 42)。差 427 倍 | #15 的 `verify_load.py` 加 `_resolve_csv_dir()`:从装载快照 `data/loaded_row_counts.json` 的 `source` 反推该跟谁对账,源目录不在就**大声失败**而不是退回 `data/csv` | `装载完整 ✅  3 张表的行数、数值求和、时间边界、布尔计数全等` |
| L5 退款 clamp | 这条钉的是**数据前提**:「退款全量 ≠ 轴内」要成立,得有越过 `as_of_date` 的退款。重灌后**轴外 0 条**(全量 9,897,495.82 == 轴内 9,897,495.82),所以钉子红了——**缺陷自己消失了**,不是 clamp 坏了 | #15 把断言从"两个数不等"改成钉**编译出来的 SQL 里有没有 clamp 谓词**(机制层,与数据无关),数值比对降级成一条 ⚠️「这批数据里没有越过 as_of_date 的退款,数值比对无区分力」 | `✅ 编译 SQL 里 clamp 谓词不在  refund_amount` + 那条 ⚠️;整层 `14 条恒等式 + 2 条缺陷锚点 + 5 条指标层语义钉子全部成立 ✅` |

**为什么第二条我第一次数成了 18:** `test_all.sh` 的 `grep_run()` 失败时只印 `tail -20`,而
那份清单是 38 行——**被截断和"就这么多"在报告上长得一模一样**。于是我按 18 条估了问题规模,
把 13 条 `RELOAD_PENDING` + 1 条 `DIMS_PASSTHROUGH` 整个漏掉,还差点据此把
`channel_daily_costs` 从 `DIMS` 里删掉(它是生成表,删错了方向)。这是本批唯一改在**本分支**
的代码:截断时多印一行「上面还截掉了 N 行」并给出复跑命令。L0 复跑 **33/0**。

**顺带在 #15 上发现两处,记着,不在本分支修:** ① `_resolve_csv_dir()` 在装载快照**缺失**时
返回 `None` 然后静默退回 `data/csv`——快照是 gitignored 的,所以新克隆的仓库会拿 8000 万行的
湖去比 189,707 行的种子,印出的正是这个函数写来防的那条红灯(第一次在 worktree 里跑就撞上了);
② `selftest_closures.py` 的 `ENUM_SUPERSET_OK["sessions.utm_campaign"]` 豁免在卡片重生后已经
**打不中**(自测原话:`刻意超集豁免 1 列:未命中`),它的清除条件("全量重灌 + 重生卡片")已经
兑现,按本库自己的规矩该删——留着它就是给这一列留了一道会吞掉第 40 个活动名的静默豁免。

### 2026-08-24 · 工具白名单:一道从来没触发过的闸

`_make_gate()` 做成 `can_use_tool` 回调,看起来是第三道边界(前两道:`db.py` 只读闸、
L4 列级授权),实际**一次都没被调用过**:`permission_mode="bypassPermissions"` 在咨询回调
之前就自动批准了每个工具调用,SDK 打的 `CanUseToolShadowedWarning` 没人看 stderr。
实测证据:闸门改成拒绝一切,`read_doc` 照样跑;让它走 `Bash` 执行 `echo GATE_PROBE_OK`,
真的执行了;CLI `init` 消息里可达工具 **25 个**,含 `Bash`/`Read`/`Write`/`Edit`/`Task`。
代价不是"多几个工具":模型可以**绕开 `run_sql`**,那道只读闸压根不在 `Bash` 这条路上。
更糟的是把闸门整个删掉当时不会让任何东西变红。

改法两层,缺一不可:`hooks={"PreToolUse": [...]}` 是真闸(不被 `bypassPermissions` 绕过,
**拒绝的形状写错就是静默放行**);`disallowed_tools`(`DENIED_BUILTINS`)砍暴露面,它是
黑名单、天生补不全,所以不能是唯一一层。差一点踩进去的坑:白名单只写那 5 个 MCP 工具会
把 agent 废掉——MCP 工具延迟加载,模型得先调 `ToolSearch` 才拿到 `read_doc` 的 schema。
实测可达工具 25 → 6,诱导读 `.env.local`、直接要求执行 shell 都被拒。

现在谁看着它:L0 的 `backend/agent.py --selftest`(AST 核**两侧**选项都把闸装在 `hooks` 上
且没有 `can_use_tool`,再直接调 hook 核放行/拒绝的返回形状)+ L8 的 `tool-gate-shadowed` /
`tool-gate-toolsearch-locked`。那条 AST 检查**不能按函数名匹配 `ClaudeAgentOptions`**:
云上是 `kwargs = dict(...)` 再 `(**kwargs)`,按名字找会静默跳过云侧,判据是"任何带
`permission_mode` 关键字的调用"。没覆盖的两块(CLI 收到拒绝形状后是否真拦、黑名单完备性)
记在 `docs/test-plan.md`。

同日云上 runtime v3 → v4,复验「退款总额」= 963,560.92,与本地一致;部署前先
`agentcore deploy --diff --yes` 确认 `SourceHash` 真的变了——那是"静默空部署"的判据。
L7 第一轮 26/27、重跑 27/27:唯一失败的 `L3-channel-cost-total` 长得很像本次改动的回归
(五份历史基线约 110 次执行里 `has_result=false` 从没出现过),实测排除了闸门(包日志连跑
三轮,闸门只见到那 6 个白名单工具、拒绝 0 次),真因是模型跑完 SQL 没调 `present_result`。
**判据没有为此放松**:`run_eval.py` 只在"一条 SQL 都没发"时重试,少了 `present_result`
不重试——那同样可能是真回归,自动重试掉等于把这一类问题变成看不见的。

### 2026-08-23 · 浏览器入口补齐:CloudFront + Cognito + Fargate 中继

在这之前云上只有最里面一段(Runtime),能用的调用方式只有 `agentcore invoke`,**没有面向
浏览器的入口**。同日补齐三个栈:`foundation`(最小 VPC + Cognito 池/客户端 + 中继 ECR +
站点桶)→ 中继镜像(ARM64,tag = 源码 hash)→ `relay`(内网 ALB + Fargate)→
`edge`(CloudFront:S3 OAC 服务静态页 + VPC origin 回源 ALB)→ `deploy_web.sh` 发前端。
顺序与取舍写在 `docs/deployment.md` 的「路径 E」。

`foundation.yaml` 重写过,删掉的比留下的多(Aurora 集群/实例/子网组、两个口令 secret、
灌数 Lambda 与 Runtime 进私网的 SG、5 个接口端点 + S3 网关端点、NAT):数据层换成
Athena/Glue/S3 Tables 之后 Runtime 是 `PUBLIC`、**根本不进这个 VPC**;NAT 换成"任务带公网 IP
放在有 IGW 路由的子网",**入站安全性与 NAT 方案相同**(任务 SG 只放行来自 ALB SG 的 8000),
成本差约 9 倍(IPv4 地址 2024-02 起单独计费,**不是免费**——那个文件里曾把它写成 $0)。
要改回"任务零公网 IP"必须三处一起改,清单在文件顶部注释。

**端到端实测(不是"栈建成了"就算)**:用站点自己那份 `amazon-cognito-identity.min.js` 在
Node 里走同码路的 SRP 登录(含首登改密分支)→ 经 CloudFront `POST /ask` → 200
`text/event-stream`,38.7s 收全事件,答案 963,560.92 与参考值一致。无 token / 伪造 token 都回
401 `{"error":"unauthorized"}`——**那个 body 是中继自己写的,能看到它就证明请求走完了整条链**。

`deploy_web.sh` 这轮堵掉两个静默失效面:`config.js` 从"桶里手工维护"改成每次部署从栈输出
现算(旧做法的失效是池换了而桶里是旧 ID:页面正常、登录框正常,点了才报
`ResourceNotFoundException`,而部署脚本全绿),部署后验证**比具体 ID**,生成物落 `$TMPDIR`
故意不落 `web/config.js`(本地靠"这份文件不存在"回退 `/api/config`);`vendor/` 首次会补传,
存在性探针挑的是**懒加载**的 `amazon-cognito-identity.min.js` 而不是 echarts——漏传它的表现
是首屏完全正常、点登录才 404。顺带修掉过期文案(「Redshift Serverless」、钉死的"约 7992 万行")
和 `web/config.example.js` 里那对"看着像能用、用了必挂"的真格式旧 ID。

### 2026-08-23 · 云上重新部署与口径复验

漏斗与留存两处口径是在**本地**修的,而 Runtime 是另一条执行路径(提示词烤进镜像、知识树
冷启动从 S3 同步),所以两件事必须一起做:知识树重传 S3 + **重建镜像发新版本**。第二步不是
可选的——知识树只在冷启动同步,暖着的 microVM 永远不会再去 S3 拉。镜像 tag 是**内容寻址**的
(tag = 源码 hash),所以 `containerUri` 跟着变 → runtime v2 → v3、旧容器被换掉;要是 tag 固定成
`:latest`,版本不 bump、暖容器不换,S3 改了也白改,而部署命令**照样退出 0**。

复验五条全部实测:退款全量 963,560.92(走 `refund_amount(time_window=all)`)、30 天渠道 GMV 的
`compiled_sql` 与本地**逐字节相同**、漏斗 `394 → 314 → 258 → 199`、留存 4 个满窗 cohort 16 个
数值全中且结论声明了数据不可用、CloudTrail 5 次 `AssumeRole` 证实生效身份是 `analytics-agent-ro`。
两条值得单独记:知识树生效拿到的是**功能级**证据(答案引用了只存在于修完那版里的
`394/314/258/199` 与「本项目真发生过」那段,文件哈希只能证明"传上去了");治理边界的反证靠
exec role 只有三项授权、`athena/glue/s3tables/lakeformation` 动作零命中——所以查数成功
**只可能**经 AssumeRole 走治理角色,这比"问它要邮箱它说查不了"硬得多,后者只证明 agent
**尊重**边界,不证明边界**存在**。

留了一处没修:云上那句概括写「W1→W4 都稳在 40%~48%」,实测区间是 37.8%~48.6%。口径对、
16 个数对,只有这句 band 说窄了,而判分器比的是数值和结论声明,不比概括句的紧致度。

### 2026-08-21 · 留存题:数字全对,结论仍然是错的

agent 答「首周约 44.7%,到第 2/4 周基本不再衰减,**曲线平稳、留得住**;右下角那几个 0 是
观测窗未到」。逐项核过:数值全对(满窗 cohort 45.0/43.0/42.1/41.1/43.0)、分子正确限定在
cohort 内、右删失那句完全正确。错的只有五个字——「曲线平稳、留得住」:这份数据的活跃度与
注册生命周期是**独立抽样**的,曲线不衰减是项目自己审计出来的 P0(`docs/data-audit.md` 把
留存/流失/复购整类列为不可信),agent 把这个缺陷报成了正面业务发现。而那句右删失说明让整段
**听起来更严谨**——**caveat 写得越像样,错误结论越难被发现**。

根因与漏斗那次同源、方向相反:`grep -rn "留存.*不衰减|不可用" knowledge/` 返回**空**。
这条事实只住在 `docs/`,而 agent 只读 `knowledge/`——**沉默和错话一样能传播缺陷**。
顺手挖出两个不报错的真 bug:cohort 时间列写成 `created_at`(应为 `registered_at`,`users`
两列都存在,所以 EXPLAIN 通过、结果正常,只是分出来的是另一批人,实测 500/500 行两列不等);
时间锚是 `CURRENT_DATE`,而本库是静态快照,直接跑查空。

改法:`analysis/retention_curve.md` 顶部加「这份数据算不出留存」(带实测曲线),要求
**给数字 + 明说结论不可用**,列出「曲线平稳/粘性好/忠实用户占比高」这类禁止写法;加一条
**反向**约束——留存非单调是这份数据的真实性质,不是 SQL 错了(漏斗的单调性由 SQL 的子集约束
保证,**留存的单调性由数据保证**,而这份数据不提供它,所以两边的默认怀疑方向相反);
分子必须 `JOIN` 回 cohort 名单(实测漏掉时第 1 周 219/43 = **509%**);`core_metrics.md` 两段
SQL 重写并实跑核过;路由上补留存关键词指向方法卡。另记一条实跑才看见的:**按天分 cohort 在
500 用户上每组只有 2–12 人**,3/5 = 60% 次日留存全是分母噪声,卡片里写明用按周口径。

现在谁看着它:`run_eval.py --selftest` 的第 8/9 组、六条 `kb-retention-*` / `retention-*` /
`lite-mode-*` 负测、L7 金标 `L5-retention-cohort`(判分模式 `retention`,**先验结论再比数值**;数值不命中时
理由必须指向数值,否则修的人会去改文案)。**2026-09-17 这条判据本身改过一次**:结论闸不再写死
「必须说这份数据算不出留存」,而是现从金标的 `pct` 那一份量曲线形状(`W4/W1 ≥ 0.8` 才算不衰减、
才开闸)。原因是新生成器给用户配了活跃半衰期,全量批次实测 `71.6 → 51.9 → 42.3 → 37.2`
**是正常衰减的**——写死的闸在数据换掉之后开始要求 agent 说一句假话。同一轮还修了另一半:
`LITE_SUFFIX` 里「不要读 analysis/ 方法库」与域索引里「哪怕只是取数也要读」直接对冲,
系统提示赢,于是常规模式下那份方法卡从来没被打开过(deep 模式只有 4 个预设按钮进得去)。
两条都修完当天重跑了整套 L7:**27/27**,该题读文档 **3 次**、理由「曲线在衰减,结论闸不适用
(金标实测 5 个 cohort 的末周/首周 ≈ 0.50)」——读文档 3 次就是路由那一半通了的证据。
写自测时它自己出了三个问题,都值得记:
① 断言把卡片注释里那句「不用 `CURRENT_DATE`」当成违规文本(修法是扫描前先剥 `--` 注释;
**禁止型断言被警告文字触发是误报、会自曝,要求型断言被注释里的示例满足则是永远绿的灯**);
② 「cohort 必须按 `registered_at`」第一版只要求该列出现过,注入时把 SELECT 的键换成
`created_at`、`WHERE` 里留着 `registered_at` 照样全绿,改成同时禁掉错列才见红——
**没被注入验证过的断言不算装了闸**;③ 结论闸的白名单里放了「别当」,而那份缺陷答案的最后
一句正是「别当真实下跌」,于是**本该不算数的 caveat 成了放行凭证**,改成每项只指向机制或
定论。白名单本身是刻意选的:正确答案里就带着「别当成留存好」,任何按"留存好"拦的黑名单都会
打到正确答案身上。

### 2026-08-21 · 漏斗口径:一个错误住在七个地方

人工抽验时 agent 给出「浏览 394 → 加购 385 → 结算 396 → 支付 373,几乎不衰减,不是真实业务
漏斗形态」。四个数排在一起就说明问题:**396 > 394**,而漏斗每一步都是上一步的子集,人数必然
单调不增。它的 SQL 是四个互不相干的 `COUNT(DISTINCT CASE WHEN event_name=…)`——算的是四个
集合各自多大,不是一个漏斗。正确口径(每步约束成上一步子集)全期是 `394 → 314 → 258 → 199`,
逐层流失 20%/18%/23%,整体 50.5%:**数据其实衰减得很正常,结论整个反了**。同一份数据换一个
口径,末步人数差 50 倍以上——口径不是细节,它就是答案本身。

这个缺陷住在**七个地方,而处处互相印证**。前四处在测试面:SOP(`analysis/funnel_analysis.md`)
只给转化率公式、从没说每步必须是上一步的子集;`eval/cases.json` 的 `L4-funnel` 金标**自己就是
错口径**;`judge_funnel` 只比数值不看形态,数值恰好命中错金标于是判 PASS 并进了归档基线;
`stats.funnel()` 喂进非单调序列照样算出 `conv_from_prev=102.9%`、瓶颈指向无关位置。
**改完这四处、三道闸全绿,浏览器上照旧一字未变**——因为 agent 不读金标、不进判分器,
它真正读的是另外三处:`domains/behavior/events.md` 的参考 SQL(就是独立计数)、同卡片和
`metrics/core_metrics.md` 里那两句「这份数据看不出衰减,别当业务结论」的**错误归因**。
**agent 没有推理错误,它是照抄的,连那句错误归因一起抄了。**
第二半是**路由**:`analysis/_index.md` 把方法卡的适用面写成「判断/诊断/深度分析题」,于是一道
朴素取数题只加载行为域卡片,SOP 里的约束一条都没生效。**内容对不对是一回事,送不送得到是
另一回事**,而后者当时没有任何检查器看着。

改法七处一起:SOP 加两条硬约束(逐层收窄 + 显式时间窗)并明写**算完自检单调性、不成立就是
自己 SQL 错了**,特别点名不许解释成"数据质量问题";金标改成子集口径两条(全期 + 30 天锚定);
`judge_funnel` 加形态闸(只看**交付的**图,不看中间 rowset——中间出现非单调数列很正常);
`stats.funnel` 报 `monotonic` + `violations` 并写进 `_stats_summary`(**那是模型唯一看得见的
东西**),措辞直接给出该做什么,且**刻意不抛 `StatsError`**(这不是"参数非法",是"口径可疑");
两张卡片的参考 SQL 换成子集口径、删掉那两句错误归因;路由补上指向方法卡。

现在谁看着它:新增 `run_eval.py --selftest`(进 L0),钉住形态闸、`stats.funnel` 的实时闸、
金标 SQL 里**逐步**的子集约束(第一版写成"某处有 `IN (SELECT user_id …)` 就算过",注入当场
证明它是假的:删掉其中一步照样全绿,改成枚举 `s1/s2/…` 的 CTE 逐个核)+ 自测第 7 组扫
`knowledge/**` 的 ```sql 段(判别式第一版把 `WHERE event_name IN (…)` 的趋势查询也拦了,
改成"必须真的分步各算一个人数")+ 四条 `funnel-*` / `kb-funnel-*` 负测
(`kb-funnel-route-gone` 要打两个 patch:指针在 `_index.md` 里有两处,删一处剩一处照样绿)。
`verify_doc_sql.py` 一直覆盖这两个文件、一直全绿,**因为 EXPLAIN 只管语法**:一条口径全错的
SQL 照样 EXPLAIN 通过。这是「名字比覆盖面大」的第三种形态——检查器没写错、也不是零覆盖,
而是它验的那一维恰好不是会出错的那一维。修复的证据不是测试绿,是把浏览器上那句原话直接喂给
agent 后 trace 里路由生效、SQL 是子集口径、输出 `394 → 314 → 258 → 199`、那句种子数据的托辞没了。

同类隐患留了一处已知未修的形状(`retention_curve.md` 分子未显式限定在 cohort 内),当天下一条
就是它。`eval/` 是唯一覆盖 `backend/agent.py` 的东西,而它自己此前没有一条断言——教训比
"漏斗要子集"更一般:**判分器和被判的对象一样会错,而判分器错了没人会红**。其余判分模式
(`numbers` / `contains` / `judge_llm`)至今仍是这个状态,记在 `docs/test-plan.md`。

### 2026-08-21 · 同一份 shell,两套挂载布局:本地图表一直是空白的

uvicorn 日志里三条 404(`/vendor/fonts/fonts.css`、`/config.js`、`/vendor/echarts.min.js`),
文件都在,是**布局**问题:同一份 `web/index.html` 线上在 S3 站点根、本地被 FastAPI 挂在
`/app` 下,绝对路径 `/vendor/...` 在本地落到根上没人接。表现又是最典型的那一类:`echarts`
未定义,`renderChart` 抛 ReferenceError,但那句在 `setTimeout(…,400)` 里,所以解读、KPI 卡、
洞察、SQL、数据表**全都照常渲染**,只有图表框空白、字体退回系统默认,界面上没有一句红字。
它瘸了一段时间没人知道,因为**整套验收里没有一条断言碰过"页面引用的资源取不取得到"**。

改法:本地资源一律相对路径。唯一例外是 `/config.js`——它的语义是"站点根上那份部署期产物",
不该跟着挂载点走,由 `server.py` 回一个空脚本兜住(`window.APP_CONFIG` 依然缺席 → 回退
`/api/config`,行为一字不变,只是日志里不再有一条会误导排查的 404)。顺带堵掉一条**同貌不同
因**的路:`file://` 双击打开时探针必然连不上(`Origin: null` 不在 CORS 名单里),而此前底部
照旧提示"启动后端"——后端很可能正跑着,这句提示把人指向错误方向。

现在谁看着它:`scripts/ui/asset_check.py` 双模——静态那份进 L0(路径是相对的 + 文件在 `web/`
下 + 那个例外确实有路由接),**真取一遍**那份进 L6(逐个 HTTP 200,因此还覆盖挂载点本身和
那条兜底路由,静态检查看不见路由);L8 的 `shell-asset-absolute-path` /
`shell-config-js-route-gone`;`boot_test.mjs` 场景⑩钉住 `file://` 时的文案。

### 2026-08-21 · 降级是单向的:"重启也刷新了还是一模一样"

三态 `/health` + 重试上线、全绿之后,用户报的现象**一个字没变**:打开前端仍是离线演示模式。
排除掉的:进程确实是新代码、`/health` 确实回 `ok:true`、页面确实带新常量、`no-store` 在位、
没有 service worker。真因在 `boot()` 的收尾:`break` 出重试循环后 `MODE='baked'` 就结束了,
**这一页在它整个生命周期里再也不看一眼后端**;而失败额度只有 3 发 × 1.5s ≈ 3s,比 uvicorn
打开端口还短——「重启后端 → 立刻刷新」这个最自然的动作三发全落在 connection refused 上,页面
永久锁死。**从用户角度看"重启 + 刷新"这个万能操作失效了,而归因指向"你改的东西没生效"。**
三次翻车,三次表现完全一样,这是这类缺陷最贵的地方。

改法不是再调数字,是让降级**可逆**:降级后退避重探(3s → ×1.5 → 上限 15s,总窗口 2 分钟;
有上限是刻意的,探通的 `/health` 会真查一次 Athena,无限轮询等于给一个没人看的页面持续记费)、
标签页重新可见时探一发、**提问前探一发**(这条兜住最贵的失败:后端活着而用户拿到烘焙答案)。
方向只有 baked → live 一个:半途换掉一个已经给过答案的页面的身份标签更容易被误读。同轮还修掉
`/health` 一个 2 秒的白等(TTL 过期时先 `ev.wait(2s)`,让 `ok:true` 也要 2.011s;改成手上有旧
结果就立刻回,实测 0.003s)——它让"刷新赶不上"更容易发生,算这次事故的帮凶。

现在谁看着它:`boot_test.mjs` 场景⑧⑨(**先看它们红**——5 项断言失败、⑧ 全程只探了 3 发——
再修)+ L8 的 `probe-degrade-is-permanent` / `probe-baked-answer-while-backend-alive`;
另外拿**真 Chrome**(headless)打桩服务验过:前 3 发断连、之后回 live,渲染从离线翻成
`claude-opus-4-8 · Amazon Bedrock`,桩日志显示第 4 发才是 live。

### 2026-08-20 · 前端存活探针:一个照本地 Postgres 定的超时,把整页变成离线演示

后端好好跑着、`curl /health` 回 `ok:true`,页面右上角却标「离线演示模式」,问「一共退了多少
钱」答的是**DAU 走势**。成因是 `web/index.html` 的启动探针:一发 `/health`、超时 `2500ms`、
超时即**永久**降级到烘焙数据。那个 2500 是本地 Postgres 时代照 `SELECT 1` 的几十毫秒定的;
换成 Athena 后新进程第一发 `/health` **实测 5.0s**(建客户端 + AssumeRole + 一次真 Athena
查询),之后才落到 1.2–1.9s——也就是说**"起完后端第一次打开页面"不是概率性失败,是必然
失败**;刷新一次反而好了,于是它看起来像"偶发",归因指向后端,而后端没问题。坏的形态还是
那一类:不报错、给答案、答的是另一个问题(烘焙答案走关键词粗路由,"退款"落到默认那条)。

**第一次改法是错的,而且是新加的那条断言当场抓住的**:把预算改成 `12000ms`,L6 那条
「预算 ≥ 2× 冷启动实测」立刻量到 12.5s(同一台机器另一次是 5.0s)。任何写死的毫秒数都是在
猜一个云上的分位数。**那条断言的价值不在于证明改法对,在于证明它不对。**

正解是换判据——让后端说自己**在不在**,别让 Athena 的延迟裁决这件事:`/health` 三态
(`ok/warming/error`,"连不上"是第四种,前端自己看得见;ping 改后台线程 + 事件等待,最多等 2s
就照实回 `warming`);前端 `warming` ⟹ 继续等(最多 ≈45s),`error`/连不上 ⟹ 攒够 3 发才降级,
等待期间停在「连接中…」而不是先渲染成「离线演示模式」再翻回来(那个标签会被读成结论),
一直 `warming` 也要**有限**放弃;`ask()` 改成 `await BOOT`;启动预热只为省时间、**不是正确性的
依赖**(预热失败一律不抛,在启动路径上抛会把数据层问题伪装成"服务起不来");ping 结果短缓存
10s 放在 `server.py` 而不是 `db.py`(后者逐字同步到云上,且 ping 作为**探针原语**应当每次真探,
`/health?fresh=1` 强制)。兜底:真降级时烘焙答案正文开头带声明。

顺着这条线量出两处**本来就在、只是没人看**的浪费:`/health` 冷启动 4.06s 里 1.93s 是
`db.backend_info()`(要 AssumeRole 才填得出 `identity`,却**在 async 函数里同步调**,既阻塞
事件循环又和 ping 串行相加,现在 `asyncio.gather` 并发);`_athena()` 的惰性初始化**没有锁**,
两个并发调用者各做一遍 AssumeRole 还互抢 GIL,加双检锁后冷启动 `/health` 4.06s → **2.007s**。

**为什么此前零覆盖,这是重点**:`render_test.mjs` 把 `fetch` 打成必抛,并且调的是一个**假的**
`boot()`,真 `boot()` 一行没跑过;L6 那几条 curl 不带超时,而且跑之前已经把后端等热了。
**没有跨过真实阈值的测试,对那个阈值零覆盖。** 补的:L0 `scripts/ui/boot_test.mjs`(假 fetch
跑**真** `boot()`,十个场景,时间整体按同一系数缩放所以四秒内跑完,且**不写死任何毫秒阈值**)
+ L6 三条(常量都从 `web/index.html` **grep 出来**,不在测试里抄第二份)+ L8 的
`probe-budget-too-tight` / `probe-warming-treated-as-dead`。两个 node 用例缺依赖时打
`⊘ 跳过`,`Case` 因此多了 `requires` 字段——缺依赖既不假红也不静默少跑。

### 2026-08-20 · 云上副本:从手工副本改成生成物

`analyticsagent/app/analytics/` 一直是 `backend/` 的**手工副本**,阶段七只改了 `backend/`,
于是分叉到 `db.py` 518 ⟷ 165 行、`SYSTEM` 提示词 8696 ⟷ 6203 字符、`metrics_def.py` 324 ⟷ 177 行
(`stats.py` 恰好逐字相同——**正是这种"碰巧一样"的文件让分叉的那几个看不出来**)。云上那份还在
教「拿该表自己的 `max(dt)` 当今天」:同一个「近 30 天各渠道 GMV」,本地答 100.8 万,云上答 **0**,
而整套测试一条都不碰它。

改法不是再手工拷一遍(那只会把同一个坑重挖一次),而是套用仓库里已有的**生成物 + `--check`**
范式:新增 `scripts/deploy/sync_agent_code.py`,6 份整份逐字拷 + `agent.py` 按 AST 逐节点比
(当时 15 个:提示词与共享解析辅助)。两侧驱动是**刻意不同**的(本地 `run_agent` 每问一个
`ClaudeSDKClient`,云上 `build_options` + `stream_events` 持暖客户端跨调用复用,省掉每次 8-10s
的 CLI 拉起)——**驱动不同,提示词必须相同**,bug 就住在提示词里。为了让副本能"逐字相同"而不是
"拷完再改两行",顺手改掉两处人工差异:`tools.py` 的 `DOCS_ROOT` 改读 `KNOWLEDGE_DIR`、
`db.py::_athena()` 先裸 `import athena` 再回退仓库布局。反向也捞到一个:云上那份有 `_norm_method()`
防呆而本地没有——模型偶尔把 `method.formula` 写成字符串,前端 `(mt.formula||[]).map` 抛异常,
UI 上显示成**「后端连接失败」**:一次模型笔误被渲染成一次基础设施故障,排查方向从一开始就错。
顺带清掉 `agentcore.json` 里钉着的真实 subnet/SG/VPC ID(`networkMode` 改回 `PUBLIC`)、
`requirements.txt` 里的 psycopg、`PYTHON_3_14` ⟷ `FROM python:3.11-slim` 的错配。

现在谁看着它:两行 L0(`--selftest` 验检查器认得出漂移、`--check` 验副本没漂)+ L8 的
`cloud-copy-drift`(注入的就是那次真实分叉)。**这一节当时没做的是部署**——只做到"代码与本地
同源",没在 AgentCore 上跑过一次;后来分两次补上(2026-08-20 首次、2026-08-23 口径修完后 v3)。
部署前置与部署后要复验的口径写在 `analyticsagent/README.md`,**那里的期望值别写死具体数字**:
初版把渠道 GMV 钉成 100.8 万,数据重灌后就不成立了,而它过期时 README 看着完全正常。

### 2026-08-20 · L7 抓到的一次真回归:没点明范围时的静默收窄

时间锚点修完后重跑,`L3-refund-total`(「这段时间一共退了多少钱?」)从 ✅ 变 ❌:agent 把
**没点明时间范围**的问题默认成「近 30 天」,答 28.11 万,而全量是 96.36 万——差 3.4 倍。
0 条 SQL、0 次读文档(走 `call_metric`),连"SQL 写错了"这种线索都没有;答案里还大方写着
「我按截至 2026-01-24 的近 30 天来算」,听起来很专业。**不报错、数看着正常、口径悄悄换了一个**
——这正是这套评测存在的理由。

改在 SYSTEM 提示词:问句没点明范围一律全量(`time_window="all"`),「一共/累计/总额/这段时间」
都算全量,真出现时间词才切窗口。但**全量 ≠ 不加日期条件**——比值类要补 `dt <= 锚点` 的上界,
否则全量 CAC 会拿铺到 2026-09-01 的成本去除一段根本没有新客的日子。这两条得一起写:只写前半条
会把 `L3-cac-overall` / `L4-cac-lowest-channel` 弄错,而那两题原本是过的。

### 2026-08-20 · L4 治理层:最小权限角色 + 列级排除

阶段六在 Redshift 里的两道查询时控制(最小权限角色 + 动态脱敏)此前没有 v3 等价物,湖仓一直
跑在 data lake admin 全量授权下。新增 `scripts/lakehouse/governance.py` 补上:

- **身份**:专属 IAM 角色 `analytics-agent-ro`(内联策略 `lakehouse-read`)。后端设了
  `AGENT_ROLE_ARN` 就 AssumeRole 用它查数,`backend_info()["identity"]` 回传生效身份——那是从
  外面唯一能看见"治理接上了没有"的地方。凭证走 `DeferredRefreshableCredentials` **会自动续期**:
  一次性 `sts.assume_role()` 约 1 小时就过期,会炸在 agent 答到一半的某条查询上。
- **授权面**:Lake Formation 列级 SELECT。`user_messages` 整表不授权;`users.email`/`phone`、
  `user_profiles.birth_date` 用 `ColumnWildcard.ExcludedColumnNames` 排除。**LF 没有值级掩码
  原语**,所以 v2 那个 `***@masked.invalid` 换成了"这列根本不在授权面里":`SELECT *` 里没有它,
  点名查报 `COLUMN_NOT_FOUND`。前端 `masked` 这个键名为契约保留,文案改成「不授权的列」。
  不走 Glue Catalog View(那条路能做真掩码)的代价与选择明写在 docstring 里。

五个踩出来的点:① LF 授权**可叠加**,一条 `TableWildcard` 就会盖掉列级排除,所以只发列级授权,
多出来的宽授权由 `--verify` 报成漂移;② 改排除清单**必须先 revoke**,加发一条不会收窄授权面;
③ 同名但没打 `Project` 标签的角色一律拒绝动它;④ **明文旁路一**:中转库的 `*_csv` 外部表指着
明文 CSV,**Lake Formation 完全看不见**,所以角色的 S3 读被钉死在 `athena-staging/agent/` 一个
前缀上——少了这道,上面所有列级授权都是装饰,而云上探针一条都不会红;⑤ **明文旁路二**:
Athena 的**查询结果**也是明文 CSV,所以是**两个 workgroup**(管理侧 / agent 侧),结果前缀一个
是另一个的父前缀,否则 agent 能从管理侧的结果文件里读回 LF 已排除的 `users.email`
(治理探针自己就跑 `SELECT email FROM users LIMIT 1`)。**列级排除管的是"查得到吗",管不了
"结果放哪儿"。**

现在谁看着它:L4 三条(原来那节只打印"缺什么")——`--selftest`(离线:策略清单 ⟷ 验收契约互相
覆盖、IAM 策略窄不窄,含 `csv/` 旁路与两个结果前缀是否分开,都配了正对照)、`--verify`(连云:
授权面比对 + 以 agent 角色实测探针 + 结果集隔离探针,管理侧前缀的 list/get/put 三样都得被拒)、
`--verify-backend`(后端确实在用受限凭证,且它读不到 `users.email`)。那两份清单
(`EXCLUDE_COLUMNS`/`DENY_TABLES` 与 `MUST_NOT_READ_*`)**刻意不互相推导**:验收契约要是从策略
现算,"有人把一列从策略里拿掉"这个缺陷注入完自测照样全绿。L8 同时加三条,正好对上三种失败
形态:`gov-policy-loosened`(策略被改松,离线就该红)、`gov-probe-blind`(探针换 admin 身份跑
必须全红,**全绿即零覆盖**)、`gov-backend-not-assuming`(角色建好了、权限发了,而 `db.py` 仍用
admin 凭证查——`/health` 看起来还是对的,这是最像真实回归的那一种)。

### 2026-08-19 · L8 负测:验证检查器本身

那一整套对账/校验脚本有个没被回答的问题:**它们该报的时候真会报吗?** L0–L6 全绿只说明"现在
没问题",不说明检查器还有效;一个永远绿的检查器和没有检查器等价,而且更糟——它让人以为这块
有人看着。新增 `scripts/negative_tests.py`(`--l8` 挂在套件末尾):每个用例往对应检查器守的那个
**真实缺陷**上打一枪——先跑未注入的命令并要求 exit 0(否则"变红"可能与注入无关)→ 注入,要求
exit 非 0 **且输出匹配预期消息**(换个原因红了不算过)→ 还原并用 sha256 核对。注入用唯一子串
替换,锚点必须恰好出现一次,所以锚点漂移是显式 ERROR;**不用 `git checkout` 还原**(工作树里有
大量未提交改动)。

写这套负测的过程本身抓出**两个真缺陷**,都是"检查器没人验证"的产物:
**名字比覆盖面大**——`scripts/manifest/render.py --check` 只校验 manifest 合法、**从不比对
生成物**,而 `test_all.sh` 那一行标的是「派生层知识卡片是最新渲染」,手改一张生成的卡片它照样
`manifest OK` + exit 0(已修:逐字比对全部 15 个产物);**零覆盖**——`backend/db.py` 的只读闸是
L4 之前**唯一**生效的安全边界,而整套测试里没有一条断言碰过它,把 `_FORBIDDEN` 改松不会让任何
东西变红(已修:`python3 backend/db.py` 是它的自测,含 1 项**已知的过度拒绝**钉在那里——
`WHERE action = 'delete'` 会被拦,宁可误拒也不误放,但这行为要看得见)。这两条与之前踩过的两次
**假阳性**(卡片枚举行被当列名、`IS NOT NULL` 里的 `is` 被当列名)是不同形态:假阳性会自己暴露,
这两种不会。

同时补上文档缺口:此前没有面向当前架构的验收文档,`scripts/test_all.sh` 是事实上的唯一真源。
新增 [docs/test-plan.md](docs/test-plan.md)——L0–L8 每层跑什么/挂了说明什么、负测清单、
**「没有自动化覆盖」清单**、改动 → 测试步覆盖矩阵。最后一节最重要:不写出来,"全绿"会被读成
"全都验过了"。

### 2026-08-19 · 文档清理

现行文档按实测事实改写(`docs/deployment.md`、`docs/legacy.md`、`AGENTS.md`、`eval/README.md`、
`eval/baseline/README.md`、`test_questions.md` 的时间口径、`scripts/localpg/README.md`);
**刻意留存的历史文档只加"已被取代"横幅,正文一字不改**(`docs/architecture-v2-redshift-glue.md`、
`docs/test-plan-v2.md`、`docs/data-audit.md`)——改了就是伪造历史。

`docs/data-audit.md` 的横幅值得单独说:它记的是 v2 修复后的**不变量**(计数器一致、漏斗单调、
留存衰减、券一单一张),这些结论在当前种子数据上**多数不成立**(`posts.like_count` 1,000/1,000
不符、purchase 事件 772 ≠ 有效订单 1,601、漏斗 max/min 1.18、留存不单调)。过期的**数字**容易
识别,过期的**不变量断言**不容易——它读起来像"这个项目的数据性质",所以横幅里逐条列了对照。

### 阶段七搬迁期 · 枚举取值漂移:对账的第三条路径

前两条对账路径(`gen_ddl.py --check` 比声明态、`reconcile.py` 比表和列)都管不到**列里装的值**。
卡片写 `status='active'` 而数据里是 `'on_sale'` 时:SQL 语法正确、目录对账全绿、EXPLAIN 通过、
跑出来是**空集**——然后"没有在售商品"这个结论就被端出去了。这是本项目那类缺陷最纯的形态。

新增 `scripts/lakehouse/verify_enums.py`(L0 自测 + L2):把卡片里 `### <列名>` 小节的枚举表跟
Athena 实际取值**双向**比,两个方向都算失败(卡片多写了不存在的值,或数据里有卡片没写的值);
非列的小节按 `information_schema.columns` 剔除,布尔列和取值超 50 个的列跳过。第一次跑抓出
**46 处漂移**,样本:`events.event_name` 卡片写的六个事件名**一个都不存在**(漏斗题四个事件名
有两个是错的,照卡片写出来的漏斗**每一级都是 0**)、`user_coupons.source` 四个全错、
`coupons.coupon_type` 写成 `fixed`/`shipping`(实际 `fixed_amount`/`percentage`/`free_shipping`)、
`banners.position` 漏掉的三个值占 **57% 的行**、`user_attributions.attribution_type` 多写了 `linear`。

顺带挖出三类「不报错但结果为空/为 NULL」的东西,都在卡片里加了 ⚠️:**没声明的软外键 JOIN 到
零行**(`user_coupons.order_id` → `orders` 11,297 行零匹配;`purchase` 事件的
`properties.order_id` 是小整数而 `orders.order_id` 是 12 位,772 行零匹配)——问"用券订单的
客单价"会拿到空集,读成"没人用券下单";**25 类事件里 21 类的 `properties` 是 `{}`**,
`event_definitions.properties_schema` 25/25 全 NULL;**几列全 NULL 的枚举**
(`ad_creatives.creative_format`、`push_notifications.failure_reason`、
`user_attributions.tracking_params`)——失败率这类指标在这份数据上**算不出来**,不能把 0 当成
"没有失败"。

### 阶段七搬迁期 · 一个静态快照在替不存在的治理层背书

`web/catalog.json` 是部署期从真 Glue 生成的元数据快照(没有后端时前端的兜底源),当时还是 v2 的
产物:`engine: "Redshift Serverless"`、`region: ap-northeast-1`、`rows: 79,924,448`,以及
`governance.available: true` 带着一份掩码列清单。也就是说**每个访客都会看到"PII 已脱敏"的治理
面板,而湖仓上治理层根本没实现,`users.email`/`phone` 在 Athena 里是明文**。重新生成后是
`Athena + S3 Tables (Iceberg)` / `us-west-2` / 48 张表 / `governance.available: false` + 原因;
治理层建好之后是 `available: true` + 47/48 授权 + 3 个不授权的 PII 列。`web/index.html` 的注释
写清了原则:**治理层没实现时面板要显示"为什么没有",而不是画一个空的"已脱敏"——一个看起来
齐全的治理面板比没有面板更危险。**

行数那两个键在治理层建好之后**变了**,记下来免得下次被当成数据丢了:`rows: 180,419` /
`rows_all_layers: 210,834`。差的 9,253 行是 `user_messages`——`catalog.py` 跑在
`analytics-agent-ro` 下,这张表不在授权面里,`count(*)` 失败即丢键,于是它悄悄不进总数。数据
一行没少(`verify_load.py` 逐表比 CSV 全等),少的是**视角**。所以 `totals` 里另加了
`rows_uncounted_governed` / `rows_uncounted_failed`,前端跟在行数后面显示「另有 1 张表不授权,
未计入」:一个少 5% 又没人解释的数字比不显示更糟。两个键分开是因为成因不同——前者是治理按
设计挡的正常现状,后者是真故障,不该混着看。

---

## 明确没做的部分

写明而不藏着:

- **值级掩码没有 v3 等价物**。阶段六的最小权限角色在湖仓上有对应物(见 L4 那条),但动态脱敏
  没有——Lake Formation 没有值级掩码原语。PII 列落成了"不授权"(不可见),不是"掩码"(可见但被
  替换)。这是一处真实的能力下降,不是换了个说法。
- **治理只约束 agent 这一个角色**。谁还有这个账号的 Lake Formation / S3 权限(包括跑 `--apply`
  的那个 admin),没有任何检查断言。
- **云上那条执行路径没有自动化覆盖**。共享代码是生成物,`--check` + L8 `cloud-copy-drift` 盯着它
  别再分叉;但 2026-08-20 / 08-23 两次部署后按 `analyticsagent/README.md` 复验的五条**全是人工
  跑的**,`scripts/test_all.sh` 和 `eval/` 都只打本地 `backend/`。Runtime 那条路径(暖客户端跨
  调用复用、知识树从 S3 同步、exec role 假借治理角色)下次悄悄坏掉时,本地全绿、eval 全过,
  没有一盏灯会红。浏览器入口那三个栈是同一种情况:`deploy_web.sh` 有 7 项部署后线上验证,但
  「登录 → 提问 → SSE」这条链只人工走过一次。
- **CI 只覆盖离线那一档**。`.github/workflows/offline.yml` 跑 `scripts/test_all.sh --l0`(44 条)
  + `negative_tests.py --offline`(37 个用例) + CDK 那 9 条策略断言,都不连云。L1–L7 仍然只靠人在
  本地跑:那几层要的是能连这个账号的凭证,而公开示例仓库里不该放长期 key——所以这块缺口是
  刻意留着的,不是忘了。绿的 offline 不代表云上那条路是通的。
- ~~**8000 万行的生成器路径未接**~~:**已接上**——`scripts/gen/main.py --scale 427.07` → Parquet
  → `load_parquet.py`,湖里当前就是那 79,943,758 行(见下面「阶段八」和 2026-09-17 那节)。
  `load.py`(`data/csv/` 种子 → Iceberg)保留,它是小规模离线那一档的入口。
- **「文档声明的规模 ⟷ 湖的实际规模」没有检查**。这不是假设,是 2026-09-17 实测栽过的一次:
  五处文档写着 22 万行,湖里是 8000 万行,而对账层比表、比列、比枚举取值,**从不比文档里的
  数字**,所以过期的时候它是绿的。已把文档改对并加了灌数前的核查告警,但**判据仍然缺席**——
  补它要先决定"声明"写在哪(候选:一份 `docs/scale.json`,文档和检查都从它读),属于下一批。
- **`verify_load.py` 的绿灯不保证湖里没被覆盖**。它只证明「`data/csv/` 那一份完整地在湖里」,
  在一个被 `load.py` 从 8000 万行灌回 22 万行的湖上,它照样全绿。
---

## 阶段八:数据换代 —— 全量重灌 7,994 万行(2026-08-31 / 09-01,现行)

起因是架构对比测试:两套架构必须跑同一份数据,而云上装的还是 Redshift 时代那批 v1 种子
数据(189,672 行),不符合新生成器的口径。所以先审生成器、修完再生成一份全量,然后重灌。

### 数据本身

`scripts/gen/main.py --scale 427.07 --seed 42`,业务轴止 2026-01-24(轴来自 `budget.AS_OF`,
**没有 `--as-of` 参数**),出 **79,943,758 行**。24 张表由 builder 产出,11 张透传维表由
`dims_to_parquet.py` 逐字节搬 v1 的值。逐表行数提交在 `data/loaded_row_counts.json` 里——
这份快照是后来 L3 的 CSV 侧真源,理由见下面「一个默认值不再描述任何真实对象」。

装载走 `load.py --preflight` → `--recreate`。**`--recreate` 不是可选的**:`CREATE TABLE
IF NOT EXISTS` 不改已存在的表,不带它那四张分区表灌进去的还是不分区的旧表,而且没有任何
一步会报错。分区表的 `INSERT` 按月切批提交,因为一条整表 `INSERT` 会撞上 Athena 给 Iceberg
的 **100 个并发写入器**硬上限(真撞过)。按月切的两个理由:每批最多 31 个分区,且各批分区键
互不相交,所以不会多出小文件——实测四张表都是 1.00 文件/分区。

**`DROP` 会连带清掉那张表的 Lake Formation 授权**,所以 `governance.py --apply` 是重灌流程的
必跑项。而且它必须排在 `02_mart.sql` **之后**:那份 SQL 里 13 张派生 / 集市表是 DROP + CREATE
重建的,顺序反了会把刚补好的授权又清掉 12 张。表现不是"没权限"而是 **`TABLE_NOT_FOUND`**
(受限角色连目录里都看不到那张表),agent 会答"表不存在",看上去像装载漏了表。

### 忠实性怎么验的

L3 `--full`:35 张基表的行数、数值列求和、时间列边界、布尔列真值数,CSV 真源 ⟷ Athena 现查
全等。默认只比三张表,重灌后不带 `--full` 等于没验。

### 重灌带出来的七件事

**一、五列从"整列 NULL"变成了"单一假常量"。** `user_attributions.tracking_params`、
`orders.shipping_address` / `cancel_reason` / `refund_reason`、
`push_notifications.failure_reason`。**非空的假常量比 NULL 更难发现**:`IS NOT NULL` 过、
聚合出数、值本身看着合理。能看见它的只有 `verify_constants` 的 min=max 那一支——
`verify_literals` 排除 JSONB / 数组列,`verify_enums` 只读声明过枚举的列,而 L3 比的是
"灌得忠不忠实",源头本来就是这样,它一定绿。生成器五处已改成加权抽池,**非空掩码一行没动**,
只让取值多样化。这五条当时登记进了 `RELOAD_PENDING`,现在都已出桶——`RELOAD_PENDING` 是
空的,清除条件也换了口径:不是"跑过一次重灌",而是**三条 arm 都读到重灌后的数据**
(`docs/test-plan.md` 里的现行口径)。改口径的理由是 2026-09-02 那次:Parquet 重生成只更新
了 Redshift 的源,Athena 和 DuckDB 共读的 Iceberg 那份还是上一代,而"重灌跑过了"这个条件当时
是成立的——按旧口径这五条本该出桶,实际上有两条 arm 还是旧数据。

**二、只剩一个取值的列"声明不出来"。** `verify_enums.parse_card` 要求表格 ≥ 2 行才算一次
枚举声明,于是上面那三列**退化到极致时反而没有任何一层能声明它们**(生成器侧的
`check_enums_vs_cards` 同理失明)。这一处刻意没有加豁免:豁免会变成一条永远"未命中"的死条目。
缺口写进了 `docs/test-plan.md` 的「没有自动化覆盖」,兜底是 `verify_constants`。

**三、L8 有四个锚点失效,形态分两种。** 前三个是锚点过期:`degenerate-col-unlisted` 注入的
`posts.share_count` 重灌后本来就不是常量、`degenerate-col-stale-entry` 的锚点行被清单裁剪
删掉、`doc-row-total-drift` 钉的 189,672 已经不在 `connection.md` 里。第四个不一样,是
**注入的作用面被移走**:`csv-value-changed` 改的是 `data/csv/channels.csv`,而检查器的 CSV 侧
默认已经改读装载快照记的目录,于是它正常绿、用例把这盏正常的绿灯报成"假阴性"。四个都已重接
(第四个的修法是给 `Case` 加 `env` 字段,这一例显式 `CSV_DIR=data/csv`;`channels` 是直通维表,
两个目录里逐字节相同,所以拿它比是成立的)。教训:**清单一变,钉着清单的负测就可能变成一盏
空转的绿灯,而它不会报错。**

**四、一个默认值不再描述任何真实对象。** 云上装的是全量产出,`CSV_DIR=data/csv` 这个默认值
拿 19 万行去比,会把"比错了对象"打印成"装载不完整"——比没有灯更糟。修法是提交装载快照
`data/loaded_row_counts.json`(source / scale / seed / 轴 / 逐表行数),`verify_load.py` 从它
解析 CSV 侧目录;目录不在了就打印重建命令并退出,**明确拒绝回退到 `data/csv`**。

**五、修复把一条结论级约束变成了假话,而钉着它的六层一致地绿。** 重灌之前
「这份数据算不出留存、曲线不衰减」是实测出来的真话(`45.0 / 43.0 / 42.1 / 41.1 / 43.0`,
第 4 周还回升,成因是旧数据的活跃度与注册生命周期独立抽样),于是它被复制到**六个地方**:
`analysis/retention_curve.md`、`metrics/core_metrics.md`、行为域路由、`eval/cases.json` 的 trap、
`run_eval.py::judge_retention` 的判分闸,以及四个 L8 用例。新生成器把会话落在注册后第 k 天
(截断指数,四组活跃半衰期 1/5/20/60 天,权重 40/30/20/10),曲线变成
`D0 100% / D1 59.4% / D2 47.1% / D3 36.9% / D7 22.3% / D14 14.7%`——单调衰减、断崖在 D1,
那句话当场作废。**这类过期和前面几条不是一个方向**:判分闸从此开始**要求** agent 说出一句
不成立的话,而套件全绿——金标按旧口径写、agent 照旧卡片答、判分器按旧白名单判,三者自洽,
只有拿数据重算才看得出来。六处已重接到那条仍然成立的性质:**cohort 之间不可比、不要排名**
(满窗那几周 W1 只差 2.9pp、日粒度 D1 只差 0.9pp,是噪声;所有 cohort 出自同一个活跃度模型,
"哪批留得最好"没有可读答案)。重接过程里栽了两次,方向正好相反:白名单只认
「不能/不要/别排名」而 agent 写的是「不建议排名」,**一份正确的回答被判红**;改宽时又差点
收下光秃秃的「不可比」——那三个字会被右删失那句「不能和老 cohort 比」满足,而那正是不该
算数的说法。现在是"固定说法清单 + 否定加动作的正则"两条通路,`--selftest` 131 条断言盯着,
其中一组照抄 agent 真写过的三种措辞。收尾还栽了第三次,方向又不一样:改写把这句话在
`retention_curve.md` 里留下了 **5 份副本**,而判据只要求"裸子串出现过",于是钉着它的 L8 用例
(只改得动顶部小标题,锚点规矩是"恰好出现一次")注完检查器照旧 exit 0,报出来是**假阴性**,
根因却在判据太松:5 份副本里少掉 4 份仍然绿。修法是把 must 拆成位置不同、各自唯一的三处
(小标题 / 照抄进 risk finding 的引用块 / 「判分器盯的就是它」那句)。
**一句话在卡片里有多份副本时,"这句话在不在"这种判据会连同钉着它的负测一起失效。**

**六、`events` 的保底事件挂在错误的会话上,13.9% 的事件时刻落在自己会话的窗口外。**
`purchase` / `use_coupon` / `register` 被挂到用户的**首个会话**,时刻却取 `orders.placed_at` /
注册时刻。实测 854.1 万行事件里 **118.4 万行(13.9%)** 的 `event_time` 不在所属会话的
`[start_time, start_time+duration]` 内(41.1 万早于开始、77.2 万晚于结束),108.6 万行(12.7%)
连日期都不同天,最大偏差 **90 天**。后果不止"关联不上":它让事件口径的活跃天数随账龄增长
(窗首那批人均 14.5 天 vs 腹地 8.3 天,而两者的会话口径都是 5 天左右),于是按 `events`
分 cohort 时**窗首 D1 虚高约 21pp**,按 `sessions` 分则看不出左边缘。
`verify_behavior.py` 对 `page_views` 有一条通过线为 0 的同性质硬闸(`judge_pv_in_session`),
**而 `events` 此前一条都没有**——同一条性质在小表上是硬闸、在旁边那张大 90 倍的表上零覆盖,
而报告上两张都是绿的。**覆盖缺口已补**:加了第 28 条 `judge_ev_in_session`,Athena 分组聚合 /
CSV 逐行两条路都实现,`--selftest` 用红绿两侧夹具钉住(83 条断言全绿)。它不进 `test_all.sh`,
这五个报告器一律只挂 `--selftest`。**缺陷本身没修**:改生成器的保底事件归属只有下一次
全量重灌才生效,所以这条判据当前必红,纪律同时写进了 `knowledge/domains/behavior/events.md`。

**七、`users` 的三个时间列逐行字节相同。** `registered_at` / `created_at` / `updated_at`
在 213,535 行上逐行比,时间差恒为 0(`build_users` 直接 `"created_at": reg, "updated_at": reg`)。
三列各自分布正常、基数上万,所以 `verify_constants`(查的是一列内部退化)、`verify_literals`、
`reconcile` 全绿——**"两列之间重复"这个形态没有任何一层看得见**。这次的后果恰好是好的:
L8 有个用例钉着"卡片必须教 agent 用 `registered_at` 定义 cohort",写错列本该拿到另一批人,
现在写错也是同一批人,那个陷阱自己失效了(用例保留,它钉的是"卡片教哪一列")。
**但方向可以反过来**:`updated_at` 本该随资料变更前移,等同于注册时刻会让"最近更新过资料
的用户"恒为空集,而不会有任何一层红。

### 数字刷新

卡片、提示词、判据 docstring 里钉住的数字按实测重测。**被推翻的一律改成反例并注日期,不删**——
"证据消失"最容易被读成"问题修好了"。几处主要的:

- 退款总额全量 **989.75 万**,默认成"近 30 天"给的是 **337.63 万**,差 2.9 倍。
- 未归因 GMV **64.7%**。有过成交的买家 126,010 人里,44,217 人有 last_touch、43,738 人只有
  first_touch、38,055 人一条归因都没有——这一大块里约一半是口径切掉的,不是真的不知道来路。
- CAC 全量成本 435.2 万 / 轴内 307.7 万,17.07 → 58.25(3.41×)。这份数据上**渠道排名没变**,
  错的只剩量级。
- 退款取数源那两条路径现在**恰好相等**(都是 9,897,495.82,两轴同尾)。判据保留:
  少的是能暴露差额的数据,不是缺陷本身。
- `channel_daily_costs` 是目前唯一一张轴伸到业务锚点之后的表(2,799 行里 1,980 行在锚点之后),
  按"近 30 天"开窗会得出"渠道 GMV = 0、成本 42.0 万、广告全在白烧钱"。**判据是纪律不是清单**:
  `fin_daily_revenue` 曾经比下单日轴长 9 天、`user_attributions.attributed_at` 曾经全表同一个值,
  现在两者都和业务轴同尾了,但下次动生成器时该看的还是这件事。
- `new_subscriptions` 的口径声明整段作废:原写"50 行、轴止 2025-12-21、所有相对窗口返回 0",
  现在是 21,354 行、轴与订单同止、每个窗口都有值。教训写进卡片:**任何窗口返回 0 时先看轴末在哪**。

### 重灌不修的部分

- 4 张声明过 SUB 的透传维表(`ad_campaigns` 50 / `ad_creatives` 144 / `campaigns` 50 /
  `coupons` 150)在全量下仍是这个规模。声明 ⟷ 兑现的不一致这次补上了断言
  (`main.check_table_coverage()` 要求 `DIMS` 里的表全声明 FIXED,四张已改),但**维度分辨率的
  缺口要靠给它们写 builder 才补得上**,`verify_literals --from-csv` 的 4 个 FAIL 就在这里。
- 17 个透传表上的退化列,以及新登记的 5 个待重灌列。
- 三张够格上分区的表没加:`user_coupons` 970 万、`user_follows` 820 万、`post_comments` 600 万。
- `docs/data-walkthrough.md` 正文仍是种子数据的数字,只加了批次横幅——结构性结论仍然成立,
  具体行数金额全部作废。改它等于重写六百行叙述,而当前数字的真源是卡片。
- **`events` 保底事件的会话归属**(上面第六件):检查器补了,**生成器没改**——改了也要等下一次
  全量重灌才生效,所以那条判据当前必红。"两列逐行相同"的判据(第七件)没做。
- 数据窗**右端**的 D1 抬升改不掉:注册越晚,同样约 10 次会话被压进越少的剩余天数
  (人均活跃天数从 5.1 天掉到 2.3 天),这是有限观测窗的固有产物,换 SQL 躲不开。
  卡片的处置是只报腹地那几期(约 2025-11-25 … 2025-12-25)。
