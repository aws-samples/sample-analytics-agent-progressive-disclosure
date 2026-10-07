# AgentCore Project

> ## 这个目录不再是手工副本了 —— 共享代码由脚本生成
>
> 这里曾经挂着一条「⛔️ 还没迁移到湖仓架构，不要部署」的横幅：`app/analytics/` 下是
> `backend/` 的旧副本，停留在 v1（Postgres / Redshift）形态，两边分叉到
> `db.py` 518 ⟷ 165 行、`agent.py` 的 SYSTEM 提示词 8696 ⟷ 6203 字符。
>
> **横幅摘掉了，但摘掉的方式不是"手工再拷一遍"** —— 那只会把同一个坑重挖一次。
> 现在共享代码是**生成物**，由 `scripts/deploy/sync_agent_code.py` 从 `backend/` 拷来，
> 文件顶上有「不要手改」横幅，`scripts/test_all.sh` 的 L0 跑 `--check`：
> 源文件改了没同步过来就红。L8 有一条 `cloud-copy-drift` 负测盯着这个检查器本身。
>
> | 文件 | 来源 | 同步方式 |
> |---|---|---|
> | `app/analytics/db.py` | `backend/db.py` | 整份逐字 |
> | `app/analytics/tools.py` | `backend/tools.py` | 整份逐字（`DOCS_ROOT` 读 `KNOWLEDGE_DIR`，两边同字节） |
> | `app/analytics/metric_layer.py` | `backend/metric_layer.py` | 整份逐字 |
> | `app/analytics/metrics_def.py` | `backend/metrics_def.py` | 整份逐字 |
> | `app/analytics/stats.py` | `backend/stats.py` | 整份逐字 |
> | `app/analytics/athena.py` | `scripts/lakehouse/athena.py` | 整份逐字（**平铺**在这里：构建上下文是 `app/analytics/`，COPY 不到上一级） |
> | `app/analytics/agent.py` | `backend/agent.py` | **只同步提示词与共享辅助**（17 个顶层节点） |
>
> `agent.py` 只同步一部分是刻意的：本地是 `run_agent()`（FastAPI 每问一个
> `ClaudeSDKClient`），云上是 `build_options()` + `stream_events()`（`main.py` 持一个暖客户端
> 跨调用复用，省掉每次 8-10s 的 CLI 拉起）。**驱动不同、提示词必须相同** ——
> 锚点 bug 就住在提示词里：本地改成全局 `meta_snapshot` 锚点之后，云上那份还在教
> 「用该表自身的 `max(dt)`」，同一个「近 30 天各渠道 GMV」本地答 100.8 万、云上答 **0**。
>
> **这个检查器不覆盖**（名字别比覆盖面大）：`knowledge/` 那棵文档树云上不烤进镜像、
> 冷启动从 S3 同步，改了文档要重传 S3，脚本管不到那一步；`backend/server.py` /
> `catalog.py` / `web/` 云上没有对应物（HTTP 契约是 AgentCore 的 `/invocations`）。

> ### 部署状态：**2026-08-24 已重新部署（runtime v4）**
>
> 2026-08-20 首次部署；2026-08-23 漏斗与留存口径修完后重新部署（v2 → v3）；
> **2026-08-24 工具白名单闸门改成 `PreToolUse` hook 后重新部署**，runtime **v3 → v4**，
> DEFAULT endpoint `liveVersion=4`，镜像 tag `b50169ba…`（内容寻址，源码 hash 变了才会 bump
> —— 部署前用 `agentcore deploy --diff --yes` 确认过 `SourceHash` 确实变了，这一步就是在
> 防那次"推了新镜像而版本不 bump、暖容器不换、命令照样退出 0"的静默空部署）。
> 部署后复验：`liveVersion=4` + 真调一次「退款总额」得 **96.36 万**（= 963,560.92，与参考值一致），
> CloudWatch 里那个容器的流上「载入 11 个配置键 ✅ + 同步 75 文件 ✅ + `[req]` + `[warm]`」
> 齐全（同一条流，不是分散在不同容器）。知识树本次**没变**（`s3 sync --dryrun` 无 upload），
> 所以只需换版本、不需重传 S3。
>
> ⚠️ 一个待清理的噪声：CLI 在启动时打 `Permission deny rule "MultiEdit" matches no known tool`
> —— `DENIED_BUILTINS` 里 `MultiEdit` 是**已经不存在**的旧工具名（30 条里只有这一条被点名）。
> 对边界**没有影响**（拒一个不存在的工具是空操作，真闸是 `hooks` 那道白名单：不在
> `GATE_ALLOWED` 里一律拒），但这行 "check for typos" 留在生产日志里会训练人忽略日志，
> 下次动这块代码时一并删掉。
>
> 本地全链路（`backend/` + `web/` + 云上 Athena/Glue/S3 Tables）：
> `bash scripts/test_all.sh --l8` **39 PASS / 0 FAIL**（含 L8 负测 38/38，2026-08-24 实测）。
> `eval/` 27 条金标 **27/27**（2026-08-24 重跑，工具闸门改成 `PreToolUse` hook 之后；
> 上一轮 2026-08-23 是漏斗与留存口径修完后的第一次全量）。
> **2026-08-23 同日把外面两段也部了**:CloudFront + Cognito 接入层 + `/ask` 的 Fargate 中继
> (`infra/foundation.yaml` / `relay.yaml` / `edge.yaml` 三个栈 + `scripts/deploy/deploy_web.sh`)。
> 所以现在**有浏览器入口**了,不再只能 `agentcore invoke`。登录 → 提问 → SSE 实测通过
> (与浏览器同码路的 SRP 登录 → CloudFront `POST /ask` → 200 `text/event-stream`,
> 答案 963,560.92 与直接打 Runtime 的参考值一致);无 token / 伪造 token 都回 401。
> 建栈顺序、拆栈顺序、以及"没有 NAT / Runtime 不进 VPC / 任务是 ARM64"这几处刻意取舍见
> `docs/deployment.md` 的「路径 E」。
>
> 云上这条路径的资源清单与拆除步骤见
> `docs/deployment.md`。
>
> **改了提示词或 `knowledge/` 之后，"重传 S3" 单独做是不够的** —— 知识树**只在冷启动同步**
> （module 级代码一个容器只跑一次），已经暖着的 microVM 永远不会重新去 S3 拉。必须靠
> `agentcore deploy` 出一个新版本把旧容器换掉，两步一起做才真正生效。
> 好消息是镜像 tag 是**内容寻址**的（tag = 源码 hash），源码一改 `containerUri` 就变、
> 版本就会 bump；**如果哪天改成 `:latest` 这类固定 tag，推了新镜像而版本不 bump，
> 暖容器不会被换、S3 改了也白改，而部署命令照样退出 0** —— 那会是一次完全静默的空部署。
>
> `agentcore deploy` 在非 TTY 环境要带 `--yes` 才进非交互模式（`--diff` 单独给会照样
> 报 "requires an interactive terminal"；预览用 `agentcore deploy --diff --yes`）。
>
> **首次部署踩到的坑记在这里，因为它是本项目那个失效模式的教科书例子** ——
> exec role 是部署**之后**才存在的，所以先起来的那批 microVM 还没有
> `secretsmanager:GetSecretValue`。当时 `runtime_config` 对载入失败只
> `logger.exception` 一笔就继续服务，于是那批容器带着空配置进了流量池，
> 而且是**永久的**（module 级代码一个容器只跑一次，事后补 IAM 也不会重跑）。
>
> 表现不是报错，是一个很会说话的 agent：`KNOWLEDGE_BUCKET` 为空 → 知识同步走
> 「没设桶就跳过」→ `read_doc` 读空目录，agent 试了 16 个路径全是「文档不存在」；
> `AGENT_ROLE_ARN` 为空 → 直接用 exec role 查数，而它没有任何数据面权限 → 报权限错。
> 调用方看到的是 HTTP 200、`success: true`、73 秒，答案里从容写着
> 「治理指标暂时报权限错，我走原始表手查」，读起来完全像正常的分析过程，**一个数都没有**。
>
> 定位靠的是 CloudWatch 的 **log stream 名字**（一个 microVM 一条流）：
> 「载入 11 个键 ✅ + 同步 75 文件 ✅」和「[req] 进来了」出现在**不同的流**里 ——
> 配置齐全的容器一次请求都没接到。只看 `--format short` 的混合时间线会把这两拨
> 日志读成同一个容器，得出「配置是好的，怎么还查不到数」的错误结论。
>
> 现在 `runtime_config.py` 在这两处**硬失败**：载不到 secret、或设了知识桶却同步到
> 0 个文件，都在 import 期 `raise`，让这个 microVM 起不来、永远不进流量池。
> 全都起不来就是整体 5xx —— 那是**该看见**的故障。

This project was created with the [AgentCore CLI](https://github.com/aws/agentcore-cli).

## Project Structure

```
my-project/
├── AGENTS.md               # AI coding assistant context
├── agentcore/
│   ├── agentcore.json      # Project config (agents, memories, credentials, gateways, evaluators)
│   ├── aws-targets.json    # Deployment targets (account + region)
│   ├── .env.local          # Secrets — API keys (gitignored)
│   ├── .llm-context/       # TypeScript type definitions for AI assistants
│   │   ├── agentcore.ts    # AgentCoreProjectSpec types
│   │   ├── aws-targets.ts  # Deployment target types
│   │   └── mcp.ts          # Gateway and MCP tool types
│   └── cdk/                # CDK infrastructure (@aws/agentcore-cdk)
├── app/                    # Agent application code
└── evaluators/             # Custom evaluator code (if any)
```

## Getting Started

### Prerequisites

- **Node.js** 20.x or later
- **Python 3.10+** and **uv** for Python agents ([install uv](https://docs.astral.sh/uv/getting-started/installation/))
- **AWS credentials** configured (`aws configure` or environment variables)
- **Docker** (only for Container build agents)

### Development

Run your agent locally:

```bash
agentcore dev
```

### Deployment

Deploy to AWS:

```bash
agentcore deploy
```

**部署前的前置条件**（少一条就会部出一个"起得来但答错数"的 agent）：

1. **先同步代码**：`python3 scripts/deploy/sync_agent_code.py --check` 必须 exit 0。
2. **建 Secrets Manager secret** `analytics-agent/runtime`，键见
   `app/analytics/runtime_config.py` 的 docstring。数据层换成 Athena 之后**这里没有
   任何口令**，全是会随环境变的坐标（桶名 / 工作组 / 目录 ID / 治理角色 ARN）。
   - ⚠️ `AGENT_ROLE_ARN` 不设 = **没有列级边界**：`user_messages` 整表、
     `users.email` / `phone` / `user_profiles.birth_date` 就都读得到了。
     那个最小权限角色由 `python3 scripts/lakehouse/governance.py --apply` 建。
     Runtime exec role 还要能 `sts:AssumeRole` 它，且它的信任策略里要有 exec role。
3. **把 `knowledge/` 传到 `KNOWLEDGE_BUCKET`**（前缀 `knowledge/`）。镜像里没有文档树，
   传漏了 `read_doc` 就一片空，agent 会退化成凭记忆猜字段——**不报错**。
4. **`networkMode` 保持 `PUBLIC`。** 这里原来是 `VPC` 并钉着具体 subnet / SG / VPC ID，
   那是 Aurora / Redshift 时代的要求（要进私网连库）。Athena / Glue / S3 Tables 都是
   HTTPS + IAM 的公共 API，Runtime 不需要进 VPC；留着 VPC 配置只会多一层
   "接口端点没配好 → 超时"的故障面，还把真实资源 ID 写进了仓库文件。
5. **部署后单独复验口径**（Runtime 与本地是两条独立执行路径）。
   下面五条 **2026-08-23 重新部署（v3）后全部实测过**（前三条 2026-08-20 首次部署后也测过）：

   - **退款总额（全量）应为 963,560.92**。两种错法都不报错：旧口径（轴建在下单日而非
     `refunded_at`）给 925,471.33，偏低约 4%；把没点明的时间范围默认成「近 30 天」给
     281,104.52，差 3.4 倍——后者还会在答案里大方写着「我按近 30 天来算」，读起来很专业。
     实测 ✅ 精确命中，且走的是治理层 `refund_amount` / `time_window=all`。
   - **「近 30 天各渠道 GMV」：别对期望值写死一个数，比「本地 ⟷ 云上是否同一个数」。**
     这条原来写着「应为 1,007,862」，那是当初诊断锚点 bug 时的一次观测值，数据重灌之后
     就不成立了——而它过期的时候，README 看着完全正常。现在的比法是两边现算并比：

     ```bash
     # 本地：同一套指标定义
     ./backend/.venv/bin/python -c "import sys;sys.path.insert(0,'backend');\
     import metric_layer,db;print(db.system_query(\
     metric_layer.compile_metric('gmv_by_channel',time_window='last_30d',group_by=['channel'])['sql']))"
     ```

     云上问「近 30 天各渠道 GMV 分别是多少」，两边的 `compiled_sql` 与每个渠道的数都该
     **逐字一致**（2026-08-20 实测一致：15 个渠道、可归因合计 1,023,317.84、
     含「未归因」2,841,462.13）。要看的是 SQL 里的锚点是全局
     `(SELECT max(as_of_date) FROM meta_snapshot)`，**不是** mart 表自身的 `max(dt)`。
   - **`db.backend_info()['identity']` 应是治理角色的 ARN，不是 exec role 自己。**
     从外面查最硬的证据是 CloudTrail——`AssumeRole` 事件里调用方是 exec role、
     `roleArn` 是 `analytics-agent-ro`（实测 ✅）。
     顺带一个反证：exec role **零数据面权限**（没有 `athena:*` / `glue:*` / `s3tables:*`），
     所以云上那两次查数只要成功，就只可能是经 AssumeRole 走的治理角色。
     ⚠️ 别拿「问它要邮箱，它说查不了」当证据——agent 从知识卡片就知道那两列被排除，
     它根本不会去点名查，那只证明它**尊重**边界，不证明边界**存在**。
   - **漏斗口径：问「浏览→加购→结算→支付各步独立用户数」应得 `394 → 314 → 258 → 199`**
     （未点明时间范围 ⟹ 全量；点明「近 30 天」是 `197 → 101 → 58 → 32`，两者都合法但必须
     在 method 里说清是哪一种）。**要看的不只是这四个数，还要看 SQL 里有没有
     `AND user_id IN (上一步的用户集)`** —— 少了它就是四个互不相干的集合各数一遍，
     这份数据上会得到 `394/385/396/373`，据此断言的「数据不衰减」是把自己的口径错误
     归因给了数据。2026-08-23 实测 ✅ 四个数精确命中、SQL 带子集约束、答完还自查了单调性。
   - **留存口径：数值对**不够**，结论也必须对**。4 个观测窗完整的 cohort（`registered_at`
     分周、锚点取 `meta_snapshot`、分子限定 cohort 成员）应为
     counts `[41,18,19,19,17] [37,18,16,18,14] [41,18,16,18,19] [49,20,23,23,23]`、
     pct `[43.9,46.3,46.3,41.5] [48.6,43.2,48.6,37.8] [43.9,39.0,43.9,46.3] [40.8,46.9,46.9,46.9]`。
     **同时要求答案声明这份数据算不出真实留存**（活跃度与注册生命周期独立抽样），
     ⚠️ 而且这个声明**不能拿右删失那句顶替** —— 「右下角的 0 是观测窗未到、不是真跌」是对的，
     但它只解释了边缘的 0，没解释中间那片为什么是平的。这题的历史教训正是
     **16 个数全对、右删失 caveat 也写了，结论却报成「曲线平稳、留得住」**，
     把项目自己的 P0 数据缺陷讲成了正面业务发现。2026-08-23 实测 ✅ 16 个数全中，
     且答案把中段的平（独立抽样）与边缘的 0（右删失）**分成两个成因**分别解释。

## Commands

| Command | Description |
| --- | --- |
| `agentcore create` | Create a new AgentCore project |
| `agentcore add` | Add resources (agent, memory, credential, gateway, evaluator, policy) |
| `agentcore remove` | Remove resources |
| `agentcore dev` | Run agent locally with hot-reload |
| `agentcore deploy` | Deploy to AWS via CDK |
| `agentcore status` | Show deployment status |
| `agentcore invoke` | Invoke agent (local or deployed) |
| `agentcore logs` | View agent logs |
| `agentcore traces` | View agent traces |
| `agentcore eval` | Run evaluations |
| `agentcore package` | Package agent artifacts |
| `agentcore validate` | Validate configuration |
| `agentcore pause` | Pause a deployed agent |
| `agentcore resume` | Resume a paused agent |
| `agentcore fetch` | Fetch remote resource definitions |
| `agentcore import` | Import existing resources |
| `agentcore update` | Check for CLI updates |

## Configuration

Edit the JSON files in `agentcore/` to configure your project. See `agentcore/.llm-context/` for type definitions and validation constraints.

The project uses a **flat resource model** — agents, memories, credentials, gateways, evaluators, and policies are top-level arrays in `agentcore.json`. Resources are independent; agents discover memories and credentials at runtime via environment variables or SDK calls.

## Resources

| Resource | Purpose |
| --- | --- |
| Agent (runtime) | HTTP, MCP, or A2A agent deployed to AgentCore Runtime |
| Memory | Persistent context storage with configurable strategies |
| Credential | API key or OAuth credential providers |
| Gateway | MCP gateway that routes tool calls to targets |
| Gateway Target | Tool implementation (Lambda, MCP server, OpenAPI, Smithy, API Gateway) |
| Evaluator | Custom LLM-as-a-Judge or code-based evaluation |
| Online Eval Config | Continuous evaluation pipeline for deployed agents |
| Policy | Cedar authorization policies for gateway tools |

### Agent Types

- **Template agents**: Created from framework templates (Strands, LangChain/LangGraph, GoogleADK, OpenAI Agents, Autogen)
- **BYO agents**: Bring your own code with `agentcore add agent --type byo`
- **Import agents**: Import existing Bedrock agents with `agentcore import`

### Build Types

- **CodeZip**: Python source packaged as a zip and deployed directly to AgentCore Runtime
- **Container**: Docker image built via CodeBuild (ARM64), pushed to ECR, and deployed to AgentCore Runtime

## Documentation

- [AgentCore CLI](https://github.com/aws/agentcore-cli)
- [AgentCore CDK Constructs](https://github.com/aws/agentcore-l3-cdk-constructs)
- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
