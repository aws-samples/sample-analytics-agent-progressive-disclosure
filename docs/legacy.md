# 哪些代码不再维护

v2 把数据层从 Aurora Postgres 搬到了 Redshift Serverless，元数据进 Glue Data Catalog，
治理下沉到数仓（见 `architecture-v2-redshift-glue.md`）。搬完之后，仓库里有一批东西
只服务于旧形态，或者只服务于"没有真云后端也能演示"这个目标。这批东西**保留但不再维护**。

这份文档记两件事：清单，以及边界——哪些看着像 legacy 其实不是。

## 正式路径只有一条

演示走线上：CloudFront + Cognito 登录 → `/ask` 经 VPC origin 到内网 ALB → Fargate
中继 → AgentCore Runtime → Redshift Serverless（Data API）。前端的元数据来自同域的
`catalog.json`，由 `scripts/deploy/build_catalog_json.py` 在部署期用 `backend/catalog.py`
查真实 Glue 生成。

部署：`bash scripts/deploy/deploy_web.sh`（带部署后边缘实测）。

## 不再维护的清单

| 位置 | 是什么 | 为什么留着 |
|---|---|---|
| `web/index.html` 的 `BAKED` 块 | 离线兜底的写死问答/图表/KPI/SQL，冻结在 v1（DAU 1418、7 日 GMV 124 万、日期 05/29–06/04、Postgres 方言） | 它是 `MODE==='baked'` 的唯一落点；线上配了 `askUrl`，`boot()` 第一行就短路进 live，永远走不到这里。删它要连着改失败路径和一批渲染分支，留着成本为零 |
| `docker-compose.cloud.yml` | v1 自带库形态：容器版 Postgres（35 表、约 19 万行）+ FastAPI | 让"数据在本地容器里"这条旧路径还能跑起来 |
| `backend/db.py` 的 `postgres` 分支 | psycopg 直连。Aurora 已退役，只剩本地容器 rig | 上面那套 compose 依赖它 |
| `backend/run.sh` 的 `postgres` 分支 | 起本地 Postgres 再拉后端 | 同上 |
| `database/*.sql`（顶层 13 个文件）+ `00_schema_overview.md` | v1 的 PostgreSQL DDL，35 张表 | 记录 v1 事实；v2 的 DDL 在 `database/redshift/` |

"不再维护"的具体含义：不随 v2 演进，测试套件（`scripts/test_all.sh`）不覆盖，
里面的数字和方言按 v1 事实原样保留，不修。

## 明确不算 legacy 的东西

这一条比清单重要，因为最容易被顺手砍掉。

**`backend/server.py` + `backend/run.sh` 的 `redshift` 路径，以及 `/api/catalog` 接口，
都要继续维护。** 它们不是"本地演示能力"，是测试设施：

- `scripts/test_all.sh` 的 L6 会自己起一个 uvicorn（端口 8917），打 `/health` 和
  `/api/catalog`，再跑 `scripts/ui/render_test.mjs` 验渲染契约。
- `eval/run_eval.py` 靠这条路径跑 21 条金标。
- `scripts/deploy/build_catalog_json.py` 直接 import `backend/catalog.py`，
  线上快照就是它生成的。砍掉这条路径，线上元数据就没来源了。

换句话说：本地那个 FastAPI 不是给人看的 demo，是给测试和构建用的。

## 为什么线上元数据是快照而不是实时接口

前端取元数据的顺序是 `/api/catalog` 优先、`./catalog.json` 兜底。本地开发命中前者
（实时读 Glue），线上命中后者。

原因是 CloudFront 的路由：默认行为回源 S3，只有 `/ask` 走 VPC origin 到 Fargate 中继，
而中继（`functions/ask-relay/server.mjs`）只实现了 `/health` 和 `/ask`。所以线上
`GET /api/catalog` 落到 S3，拿 403。

要让线上也实时，得在中继里用 JS 重写一遍 `catalog.py`（还要把 `knowledge/domains/` 和
`schema_manifest.yaml` 打进镜像）、给 task role 加 Glue 和 Redshift Data API 权限、
再走 CodeBuild → ECR → ECS 换版本。代价不只是工作量：组装逻辑会变成 Python 和 JS 两份，
而没有任何测试盯着这两份别跑偏——元数据静默失真正是 v2 迁移里反复出现的失效模式。

所以选了部署期快照：数据同样真从 Glue 查，只是时点固定；`catalog.json` 里带
`snapshot: true`，界面会在摘要行显示「快照时间」，不冒充实时。顶栏的「实时链路」
只指后端是真实 AgentCore 链路，不指元数据。

部署默认会重新生成快照；只有显式 `deploy_web.sh --reuse-snapshot` 才复用，并仍会与 post-data-fix baseline 对账。真需要实时，补中继路由即可，
前端已经是实时优先。

## 相关的闸

- `build_catalog_json.py` 默认**拒绝写出降级快照**：来源不是 `glue`、表数低于 40、
  base 层行数低于 5000 万，任一条命中就退出非零。把降级元数据固化上线等于给所有访客
  看错数字，而且界面不会报警。要发降级版本得显式加 `--allow-degraded`。
- `deploy_web.sh` 只显式 `cp` 两个文件，**不用 `s3 sync`**：桶里的 `config.js` 是线上
  真实 Cognito 配置，本地只有 `config.example.js`，sync 会覆盖或删掉它，登录立刻挂。
- `scripts/ui/render_test_prod.mjs` 测线上取数路径（`/api/catalog` 403 → `catalog.json`
  兜底），不依赖后端和 AWS 凭证。这条路人工验要浏览器加登录，而它失败时页面**照常渲染**，
  只是「背后的数据」停在 v1 静态原文，不报任何错。
