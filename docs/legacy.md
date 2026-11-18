# 哪些代码不再维护

数据层搬过两次:v1 Aurora/Postgres → v2 Redshift Serverless → **v3 Athena + S3 Tables
(Iceberg) + Glue Data Catalog**(现行)。每次搬完都留下一批只服务于旧形态、或者只服务于
"没有真云后端也能演示"这个目标的东西。这批东西**保留但不再维护**。

这份文档记两件事：清单，以及边界——哪些看着像 legacy 其实不是。

## 正式路径只有一条

本地跑:`bash backend/run.sh`(默认 `DB_BACKEND=athena`)→ Agent SDK + FastAPI + 前端,
数据经 Athena 查 S3 Tables 上的 Iceberg 表。前端元数据优先打 `/api/catalog`(实时读 Glue),
没有后端时兜底读同域 `catalog.json`(由 `scripts/deploy/build_catalog_json.py`
在部署期用 `backend/catalog.py` 查真实 Glue 生成)。

> **云上那条路径 2026-08-23 补齐了。** 2026-08-20 先部了 **AgentCore Runtime**
> (`analyticsagent/`),2026-08-23 漏斗/留存口径修完后重新部署(runtime v3)并复验通过:
> 退款全量 963,560.92、渠道 GMV 与本地逐字一致、漏斗 `394→314→258→199`、
> 留存 16 个数全中、CloudTrail 证实生效身份是治理角色。共享代码是生成物
> (`scripts/deploy/sync_agent_code.py`,L0 跑 `--check`),与 `backend/` 逐字同源。
> 同日**补上了前面那两段**:CloudFront + Cognito 的接入层、以及把 `/ask` 转成 SSE 的
> Fargate 中继(`infra/foundation.yaml` / `relay.yaml` / `edge.yaml` 三个栈,
> 建栈顺序与拆除顺序见 [deployment.md](deployment.md) 的「路径 E」)——**现在有面向浏览器的
> 入口了**,`agentcore invoke` / `aws bedrock-agentcore invoke-agent-runtime` 仍然可用。
> 前置条件与部署后要复验的五条见
> [../analyticsagent/README.md](../analyticsagent/README.md)。
> `scripts/deploy/deploy_web.sh` 发前端 + 元数据快照 + 部署期现算的 `config.js`
> (首次还会补传 `vendor/`),这部分一直可用。

## 不再维护的清单

| 位置 | 是什么 | 为什么留着 |
|---|---|---|
| `web/index.html` 的 `BAKED` 块 | 离线兜底的写死问答/图表/KPI/SQL，冻结在 v1（DAU 1418、7 日 GMV 124 万、日期 05/29–06/04、Postgres 方言） | 它是 `MODE==='baked'` 的唯一落点；线上配了 `askUrl`，`boot()` 第一行就短路进 live，永远走不到这里。删它要连着改失败路径和一批渲染分支，留着成本为零 |
| `docker-compose.cloud.yml` | v1 自带库形态：容器版 Postgres（35 表、约 19 万行）+ FastAPI | 让"数据在本地容器里"这条旧路径还能跑起来 |
| `backend/db.py` 的 `postgres` 分支 | psycopg 直连。Aurora 已退役，只剩本地容器 rig | 上面那套 compose 依赖它 |
| `backend/run.sh` 的 `postgres` 分支 | 起本机 brew Postgres 再拉后端（需装 `postgresql@16`，没装就只能用 compose） | 同上 |
| **`database/redshift/`、`scripts/redshift/`、`scripts/glue/`** | v2 的 Redshift DDL、COPY 装载、Glue federated catalog 注册与对账 | 记录 v2 事实。**已是死路径**——Redshift 整体退役，这些脚本连不上任何东西。湖仓的对应物在 `database/iceberg/` 与 `scripts/lakehouse/` |
| `scripts/audit/` | v2 的五层数据质量审计（跑 Redshift） | 方法有价值（见 [data-audit.md](data-audit.md)），但连不上现行数据层 |
| `scripts/consistency/` 的基线 JSON | v2 那批数据（21 万用户）的绝对值快照 | 见下文「为什么不再存一致性基线」 |

`infra/foundation.yaml` 里 **v1/v2 时代的数据库基础设施已经删掉了,不是"留着不维护"**:
Aurora Serverless v2 集群/实例/子网组、两个口令 secret、灌数 Lambda 的 SG、
5 个接口端点 + S3 网关端点,以及给 Runtime 进私网用的那套 SG。删的是**声明**,
所以这里不重复清单——**逐条列在那个文件的顶部注释里**(连"为什么不该顺手补回来"一起)。
现在那个栈只为 `/ask` 中继存在(CloudFront → 内网 ALB → Fargate),Runtime 的
`networkMode` 是 `PUBLIC`,根本不进 VPC。

`database/*.sql`（顶层 8 个文件）**不是 legacy**：它们现在是 DDL 的**唯一真源**，
`database/iceberg/01_tables.sql` 由 `scripts/lakehouse/gen_ddl.py` 从它们生成，
`--check` 断言逐字节一致。

"不再维护"的具体含义：不随现行版本演进，测试套件（`scripts/test_all.sh`）不覆盖，
里面的数字和方言按当时事实原样保留，不修。

## 明确不算 legacy 的东西

这一条比清单重要，因为最容易被顺手砍掉。

**`backend/server.py` + `backend/run.sh` 的 `athena` 路径，以及 `/api/catalog` 接口，
都要继续维护。** 它们不是"本地演示能力"，是测试设施：

- `scripts/test_all.sh` 的 L6 会自己起一个 uvicorn（端口 8917），打 `/health` 和
  `/api/catalog`，再跑 `scripts/ui/render_test.mjs` 验渲染契约。
- `eval/run_eval.py` 靠这条路径跑金标（当前 27 条）。
- `scripts/deploy/build_catalog_json.py` 直接 import `backend/catalog.py`，
  线上快照就是它生成的。砍掉这条路径，线上元数据就没来源了。

换句话说：本地那个 FastAPI 不是给人看的 demo，是给测试和构建用的。

## 为什么不再存一致性基线

`scripts/consistency/snapshot.py` 那套做法是：把一批指标的**绝对值**存成 JSON，
下次跑再比。问题在数据集一换，基线里的数字就全成了历史，而**比较仍然"通过"**——
它比的是当前值和一个早已无意义的旧值，差异大就报警、一模一样就沉默,唯独不会说
"我参照的那批数据已经不存在了"。基线会过期，而且过期时是绿的。

现在 L3 换成 `scripts/lakehouse/verify_load.py`：`data/csv/` 真源与 Athena 现查
**两边都当场算**，没有会过期的中间文件。`eval/` 的金标是另一回事——那里的期望值在运行时
从数据现算，所以跨数据集可比，基线继续留着(见 `eval/baseline/README.md`)。

## 为什么线上元数据是快照而不是实时接口

前端取元数据的顺序是 `/api/catalog` 优先、`./catalog.json` 兜底。本地开发命中前者
（实时读 Glue），线上命中后者。

原因是 CloudFront 的路由：默认行为回源 S3，只有 `/ask` 走 VPC origin 到 Fargate 中继，
而中继（`functions/ask-relay/server.mjs`）只实现了 `/health` 和 `/ask`。所以线上
`GET /api/catalog` 落到 S3，拿 403。

要让线上也实时，得在中继里用 JS 重写一遍 `catalog.py`（还要把 `knowledge/domains/` 和
`schema_manifest.yaml` 打进镜像）、给 task role 加 Glue + Athena + Lake Formation 权限、
再走 CodeBuild → ECR → ECS 换版本。代价不只是工作量：组装逻辑会变成 Python 和 JS 两份，
而没有任何测试盯着这两份别跑偏——元数据静默失真正是这个项目反复出现的失效模式。

所以选了部署期快照：数据同样真从 Glue 查，只是时点固定；`catalog.json` 里带
`snapshot: true`，界面会在摘要行显示「快照时间」，不冒充实时。顶栏的「实时链路」
只指后端是真实 AgentCore 链路，不指元数据。

数据集变了就重新发一次（`deploy_web.sh --fresh`）。真需要实时，补中继路由即可，
前端已经是实时优先。

## 相关的闸

- `build_catalog_json.py` 默认**拒绝写出降级快照**：来源不是 `glue`、表数低于 40、
  或**行数覆盖率低于 95%**（有多少张表真的拿到了 `count(*)`），任一条命中就退出非零。
  把降级元数据固化上线等于给所有访客看错数字，而且界面不会报警。要发降级版本得显式加
  `--allow-degraded`。
  > 这道闸原来写的是「base 层行数低于 5000 万」——那个阈值绑死在 v2 那批数据上，
  > 换成 22 万行的种子数据后它会**永远失败**，而失败原因跟数据完整性毫无关系。
  > 覆盖率是跨数据集可比的：48 张表里 48 张都拿到了行数才算查全，这才是那道闸真想查的东西。
- `deploy_web.sh` 只显式 `cp` 两个文件，**不用 `s3 sync`**：桶里的 `config.js` 是线上
  真实 Cognito 配置，本地只有 `config.example.js`，sync 会覆盖或删掉它，登录立刻挂。
- `scripts/ui/render_test_prod.mjs` 测线上取数路径（`/api/catalog` 403 → `catalog.json`
  兜底），不依赖后端和 AWS 凭证。这条路人工验要浏览器加登录，而它失败时页面**照常渲染**，
  只是「背后的数据」停在 v1 静态原文，不报任何错。
