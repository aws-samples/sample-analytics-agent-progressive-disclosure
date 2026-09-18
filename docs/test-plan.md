# 验收流程（现行：S3 Tables + Glue + Athena）

这份文档描述**当前**架构的验收分层。v2（Redshift）那套流程在
[test-plan-v2.md](test-plan-v2.md)，作为历史保留——里面的 `scripts/redshift/*`、
`scripts/glue/*` 已是死路径，预期值绑在那批 8000 万行数据上。

分九层，**从不依赖云资源的秒级自测往上走**，越靠后越慢越贵。早层挂了就没必要跑后面：
L0 红的时候 L2 的"绿"通常只是没跑到。

L2 后面另挂一组**不编号、不是闸门**的数据真实性报告器（「L2+」），它们对现行库必然报红，
所以刻意不进 `test_all.sh`——理由和用法写在那一节。

```bash
bash scripts/test_all.sh              # L0–L6，约 3 分钟（L3 抽查 3 张表）
bash scripts/test_all.sh --l0         # 只跑 L0，不连云、不要凭证，exit 0 可判（CI 跑的是这一档）
bash scripts/test_all.sh --full       # L3 换成全量 35 张表（多约 1 分钟）
bash scripts/test_all.sh --l8         # 追加 L8 负测（49 个用例，约 4 分钟，会临时改文件再还原）
```

L7（端到端 agent）不在里面：它烧 Bedrock token 并覆盖 `eval/report.md`，该有人看着跑。

### CI 覆盖到哪一层（`.github/workflows/offline.yml`）

CI **只跑离线那一档**：`test_all.sh --l0`（34 条）+ `negative_tests.py --offline`（32 个用例）
+ CDK 那 9 条执行角色策略断言（`Template.fromStack`，纯合成）。三步都不连云。

L1–L7 不在 CI 里，而这是个**刻意的缺口**：那几层要能连这个账号的凭证（S3 Tables / Glue /
Athena / Lake Formation / Bedrock），而公开示例仓库里不该放长期 key。所以 offline 绿了只说明
离线断言全绿，**不说明云上那条路是通的**——本文档「没有自动化覆盖」一节列的缺口一条都没被 CI 补上。

`--l0` 这个档是为它加的：在这之前"只跑离线那几十条"只能整套跑下去、在 AWS 身份那道闸上吃一个
`exit 1`，于是全绿的 L0 被包在非零退出码里，按退出码判成败的东西一律读成失败。
`--l0` 与 `--full` / `--l8` / `--ask` 互斥且显式报错（那三个都在 L1 之后）——静默忽略的话
`--l0 --l8` 会给出一份「L0 全绿、exit 0、而负测一条没跑」的报告。

## 前置条件

```bash
aws sts get-caller-identity      # 后面绝大多数步骤是云调用，先确认身份
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

- **区域是 `us-west-2`**，脚本默认值。账号从 `sts` 现算，Glue catalog ID 按命名规律拼，
  所以通常不需要 `.env.local`；多账号环境建议在那里钉 `EXPECT_ACCOUNT`（见
  `.env.local.example`）。
- **跑 agent / 负测要用 `./backend/.venv/bin/python`**：`claude_agent_sdk`、`boto3`、
  `pyyaml` 都在那个 venv 里。`test_all.sh` 和 `negative_tests.py` 会自己优先挑它，
  挑不到才退回系统 `python3`（那时缺依赖的步骤会红）。
- **别在管道后面读 `$?`**：`cmd | tail -3; echo $?` 拿到的是 `tail` 的状态，永远 0，
  会把失败读成成功。bash 用 `${PIPESTATUS[0]}`，zsh 是 `${pipestatus[1]}`，两者不通用。

## L0 静态自测（无云依赖，秒级）

34 项，全部是纯函数级断言（其中 3 项生成器自测要 numpy、1 项 boot() 契约要 node，缺了打 note、不算 FAIL）。改了 `scripts/` 下任何生成器/解析器/改写器，**先跑这一层**（`--l0` 就只跑它）。

注意那两个「缺了打 note」的依赖在 CI 里是**装上的**（`scripts/requirements.txt` + `setup-node`）：
note 不算 FAIL 是给本机开的方便，如果 CI 也让它跳过，那批断言会在一片绿灯里一直不跑。

| 检查 | 挂了说明什么 |
|---|---|
| `pg_to_trino.py --selftest` | 方言改写有回归（`::` / `date + 7` / `interval '30 days'`） |
| `backend/db.py`（只读边界自测） | 安全闸被削弱，见下 |
| `gen_ddl.py --selftest` / `--check` | v1 DDL → Iceberg DDL 的映射变了，或有人手改了生成的 `database/iceberg/01_tables.sql` |
| `verify_ddl_comments.py --selftest` / 无参 | `database/0[1-8]_*.sql` 的**行内注释**里的枚举取值与知识卡片漂开。`knowledge/README.md` 写着「表结构以 `database/*.sql` 为准」，而这道闸挂上之前，那个「为准」的源里可比对的 34 行枚举注释**有 23 行是错的**（`products.status` 四个值全错，照它写 `WHERE status='active'` 得到 0 行、不报错、结论是「一件在售商品都没有」）。这批注释还正是 `scripts/gen/tables.py` 当年抄枚举池的地方——13 个生成器缺陷的成因，且会被 `gen_ddl.py` 搬进 `01_tables.sql` 的 `COMMENT`、再进 Glue 列元数据。判据是**注释里的引号取值集合 == 卡片取值集合**；「业务上合法但本批数据没有」的值要**去掉引号**、挪进后面的散文（沿用卡片自己的体例：表格给 agent 抄，散文给人读），所以这个检查器**不需要豁免表** |
| `manifest/render.py --check` | manifest 不合法，**或**有人手改了 15 个生成物之一（卡片 / `10_derived.sql` / 域索引片段） |
| `deploy/sync_agent_code.py --selftest` / `--check` | 同步器自己坏了（认不出漂移），**或**云上副本 `analyticsagent/app/analytics/` 与 `backend/` 不再是同一份代码 |
| `eval/run_eval.py --selftest` | L7 判分器坏了：`judge_funnel` 的形态闸不再拦非单调"漏斗"、`stats.funnel` 的实时闸不再报 `monotonic=False`，`L4-funnel` 的金标口径被改回"每步各数一遍"，**或 `knowledge/` 里的漏斗参考 SQL 退回独立计数 / 漏斗题不再路由到方法卡**（最后这一项是 agent 真正读的那条路，前三项全绿时它照样能答错）。另一半盯**留存**：cohort 时间列退回 `created_at`（`users` 两列都有，EXPLAIN 照样过）、分子不再限定在 cohort 内（会炸出 509%）、「这份数据算不出留存」这条**结论级**约束被摘掉，以及 `judge_retention` 的结论闸被摘掉——后两条都是「数字全对而结论仍然错」那一类，一条管卡片里那句话还在，一条管判分器真的会因此判错。还有一条管**金标自己的时间锚点**：`(SELECT max(x) …)` 里的 x 必须是 `meta_snapshot.as_of_date`，写成逐表 max 直接判错——这条补的是「评测在奖励知识库明令禁止的写法」，9 条金标原来用的正是逐表 max，而它在多数表上恰好等于锚点、算出来和正确答案一样。见下 |
| `verify_mart_parity.py --selftest` / 无参 | 集市层 CTAS 的谓词或列数在手工搬运中丢了 |
| `reconcile.py` / `verify_load.py` / `verify_enums.py` / `setup.py --selftest` | 对应对账器自己的解析逻辑坏了（这些自测是**假阳性**的防线） |
| `verify_scale.py --selftest` | `docs/scale.json` 与 `data/csv/` 对不上（seed 那一批的每表行数是抄的，CSV 是真源），**或**某处文档里声明规模的原文被改写/被抄成了另一批的数。这条补的是整个对账层的一处结构性盲区：比表、比列、比枚举、比退化列、比装载忠实，**从不比规模**——于是「五处文档写着 19 万行、湖里装着 7994 万行、L0–L6 全绿」真的发生过。湖里到底装的是哪一批要连云，在 L3 |
| `gen/semantics.py --selftest` | `scripts/gen/semantics.yaml` 自相矛盾了。它是商品语义（品牌→类目白名单 / 价格带 / 品牌分档 / 规格词池）的**唯一真源**，生成器和 `verify_semantics.py` 都从它读——所以配置错的时候两侧会**一起**用错的那份，于是一致、全绿、而数据是错的。9 项校验在 `load()` 里，任何调用方都跑得到；`--selftest` 另外查 4 件小样本查不到的事，最要紧的一条是「不重名商品名上界 ≥ 全量目标 SKU 数」（scale=1 只造 200 行永远够用，缩了品牌白名单要到灌全量那一刻才炸）。**这一条不在 numpy 分支里**：只要 pyyaml |
| `verify_literals.py` / `verify_semantics.py` / `verify_resolution.py` / `verify_correlation.py` / `verify_behavior.py` 的 `--selftest` | L2+ 那五个报告器的**判据失去了区分力**。挂的只有 `--selftest`（正向那一支对现行库必然红，理由在 L2+ 那一节），所以这一条是那 157 项断言（23 + 19 + 6 + 28 + 81）的**唯一**闸门：夹具喂给纯函数判据，均匀/合法那一侧必须绿、退化/违例那一侧必须红。前三个是 2026-08-27 补挂的——写前两批时还没把「自测就是唯一闸门」定成模式，那 48 项断言（23 + 19 + 6）在此之前没有任何东西在跑 |
| `load.py --preflight` | CSV 表头/列序/换行/数组格式与 DDL 不一致——灌进去会整列错位且不报错 |
| `ui/boot_test.mjs` | 前端存活探针的行为坏了：慢探针被判死（页面静默落进离线烘焙数据）、**后端说「还在预热」却被当成「不在」**、探不通却假装在线、一直预热不完却永远转圈、探针期间提问不等探针、**降级之后再也不看一眼后端**（先开页面后起后端就永久锁死）、或**降级过的页面在后端已经活着时仍给烘焙答案**。见下 |
| `ui/asset_check.py` | shell 引用的本地资源在**本地那套挂载布局**下取不到：线上它在站点根、本地被挂在 `/app` 下，写死绝对路径就是静默 404——图表框空白、字体退回系统默认，页面其余部分照常渲染，界面上一句红字都没有 |

两点值得单独说：

- **`backend/db.py` 的只读闸管「不许写」，L4 治理层管「不许看」，两条别互相当替补**
  （授权面里那些表 agent 是有 SELECT 的）。而在加这条自测
  之前，整套测试里没有一条断言碰过它：把 `_FORBIDDEN` 改松不会让任何东西变红。
  自测覆盖 10 类写/多语句拒绝、9 条合法查询放行，外加**一项已知的过度拒绝**
  （`WHERE action = 'delete'` 被拦）——那是刻意的，宁可误拒也不误放，但行为要看得见，
  免得下次有人当成"闸坏了"去放宽正则。
- **`render.py --check` 曾经名字比覆盖面大**：它只校验 manifest 合法，从不比对产物，
  而 `test_all.sh` 那一行标的是「派生层知识卡片是最新渲染」。现在它逐字比对全部产物。
- **`sync_agent_code.py --check` 补的是一处零覆盖**：`analyticsagent/`（AgentCore Runtime
  那份）原来是 `backend/` 的**手工副本**，已经分叉到 `db.py` 518⟷165 行、SYSTEM 提示词
  8696⟷6203 字符——云上那份还在教「拿该表自己的 `max(dt)` 当今天」，同一个
  「近 30 天各渠道 GMV」本地答 100.8 万、云上答 **0**，而整套测试一条都不碰它。
  现在共享代码是生成物（6 份整拷 + `agent.py` 的 17 个节点），改了 `backend/` 不同步过去就红。
  `agent.py` 只同步一部分是刻意的：两侧驱动不同（本地 `run_agent` / 云上暖客户端
  `build_options` + `stream_events`），**必须一致的是提示词**。

- **`run_eval.py --selftest` 补的是 L7 判分器的零覆盖，而它的代价是一条判错方向的金标**：
  `L4-funnel` 的口径原来写成每步各 `count(DISTINCT user_id)` 一遍，于是 `value_hint`
  本身就是**非单调**的 `394/385/396/373`（"发起结算"396 > "浏览商品"394）。agent 照这个
  口径答「这份数据几乎不衰减，不是真实业务漏斗形态，是随机种子数据的问题」——判分 PASS，
  还进了归档基线 26/26。正确口径（每步约束成上一步的子集，见
  `knowledge/analysis/funnel_analysis.md` 的约束 A）是 `394/314/258/199`，逐层流失
  20%/18%/23%，健康得很。**结论整个反了，而全链路没有一处报错**：`stats.funnel` 照样
  算得出东西（`conv_from_prev=102.9%`、流失为负、"瓶颈"指向一个与业务无关的位置），
  判分器只比数值不看形态，金标又恰好写着那四个错的数——三处互相印证了同一个错误。
  这就是"名字比覆盖面大"在**判分器**上的形态：`eval/` 是唯一覆盖 `backend/agent.py`
  的东西，而它自己此前没有任何断言。
  现在这条自测（不连库不调模型，秒级）钉三样：`judge_funnel` 的形态闸
  （非单调直接判错，且理由必须点明形态而不是"数值没命中"）、`stats.funnel` 的实时闸
  （`monotonic` + 逐处 `violations`，由 `_stats_summary` 写进模型看得见的摘要）、
  以及金标 SQL 里**逐步**的子集约束。最后这条一开始写成了"某处有 `IN (SELECT user_id
  FROM …)` 就算过"——删掉其中一步的约束照样全绿，又是一个名字比覆盖面大的检查器；
  改成枚举 `s1/s2/…` 的 CTE 并逐个核。

  **然后这三道闸全绿，agent 在浏览器上照旧答出 `394/385/396/373`。** 这是这条记录里
  最该记住的部分：闸装在了 agent 不走的那条路上。它读的不是金标、也不进判分器——
  它读 `domains/behavior/events.md` 和 `metrics/core_metrics.md`，而**那两张卡片里的
  「购买漏斗」参考 SQL 本身就是每步各 `COUNT(DISTINCT CASE WHEN …)` 一遍**，
  底下还配着一句「这份种子数据事件量均匀，漏斗算出来不会逐级衰减，别当业务转化结论」。
  agent 是照抄的，抄得很忠实——连那句错误归因都一起抄了。同一个错误一共住在**七个
  地方**（金标、判分器、`stats.funnel` 的缺口、SOP、`docs/data-walkthrough.md`、
  `docs/data-audit.md`，以及这两张 agent 真正会读的卡片），修前六个都不改变浏览器里
  的输出。`verify_doc_sql.py` 逐条 EXPLAIN 知识文档里的每段 SQL——但 **EXPLAIN 只管
  语法**，一条口径全错的漏斗 SQL 照样 EXPLAIN 通过，所以这两个文件此前是零覆盖。
  另一半是**路由**：`knowledge/analysis/_index.md` 把方法卡的适用面写成
  「判断/诊断/深度分析题」，于是一道朴素的「转化漏斗」取数题只加载行为域表卡片，
  `funnel_analysis.md` 从头到尾没被读过，SOP 里的约束 A/B 一条都没生效。
  内容对不对是一回事，**送不送得到**是另一回事，而后者没有任何检查器看着。
  现在自测第 7 组补上了这一面：扫 `knowledge/**` 里所有 ```sql 段，凡是算分步人数的
  就必须带子集约束（独立计数直接判错），并要求三张卡片都指向方法卡。
  L8 的四个 `funnel-*` / `kb-funnel-*` 用例盯着它不退化。

- **`boot_test.mjs` 补的是另一处零覆盖，而且那个缺陷真的在用户面前发生了**：
  `boot()` 决定整页走真实链路还是离线烘焙数据，它的超时预算写死 2500ms——那是本地
  Postgres 时代照 `SELECT 1` 的几十毫秒定的。换成 Athena 后冷启动那一发 ping 要
  **5.0s ～ 13.5s**（AssumeRole + 建 boto3 客户端 + 一次真查询在 workgroup 里排队），
  于是「起完后端第一次打开页面」**必然**落进离线模式：右上角标「离线演示模式」、
  问"这段时间退了多少钱"答的是 DAU 走势、底部提示"后端未连接，请启动后端"——
  而后端就在那儿好好跑着。
  当时的覆盖面是零：`render_test.mjs` 把 `fetch` 打成必抛并用一个**假的** boot()
  （直接 `MODE='live'`），L6 那几条 curl 不带超时、且跑之前已经把后端等热了。
  **没有跨过真实阈值的测试，对那个阈值零覆盖。**
  第一次的修法是把预算从 2500 改成 12000ms，**下面 L6 那条断言当场就把它顶红了**
  （同一台机器量到 12.5s）——写死的毫秒数只是在猜云上的一个分位数。所以判据换成
  三态：`/health` 现在答得快，并且回 `dataLayer: ok / warming / error`，
  「后端在不在」由一个本地就能答的事实决定（见 `backend/server.py`）。
  还有第三幕，它比前两幕更难归因：三态和重试都上线之后，**用户报告「重启也刷新了，
  还是跟之前一模一样」**。原因不在阈值上——`boot()` 一旦 `break` 出重试循环，这一页
  在它整个生命周期里**再也不看一眼后端**；而失败额度只有 3 发 ≈ 3s，比 uvicorn 打开
  端口还短。于是「重启后端 → 立刻刷新」这个最自然的动作，三发全落在 connection refused
  上，页面永久锁死在离线演示模式，后端随后热起来也没人再问。**看起来就像修改没生效。**
  修法是三条自愈路径（都在 `web/index.html`）：降级后**退避重探**（有上限，因为探通的
  `/health` 会真查一次 Athena，无限轮询等于给一个没人看的空闲页面持续记费）、标签页
  **重新可见**时探一发、以及**提问前**探一发——最后这条兜住最贵的那种失败：后端活着，
  而用户拿到的是烘焙答案。同一轮还修掉了 `/health` 一个 2 秒的白等（TTL 过期时先等在飞
  的那一发、等不到才退回上一次结果，实测让一个 `ok:true` 的响应也要 2.011s，现在
  0.003s）——它让「刷新赶不上」更容易发生。

  `boot_test.mjs` 用假 fetch 跑真 `boot()`，十个场景：慢探针（3000ms > 旧预算）仍应
  上线 / 探不通才降级 / `dataLayer=error` 也算不活 / 前两发失败第三发通仍应上线 /
  探针期间提问要等探针（否则答烘焙数据）/ **连续 warming 之后探通仍应上线**（"还在
  预热"不等于"后端不在"）/ **一直 warming 也要有限放弃**（否则页面永远转圈，那同样
  是不说实话）/ **页面先开、后端后起要自愈回实时**（前 10 发不可达、第 11 发通）/
  **降级过的页面提问时后端已经活了 ⟹ 必须走真实链路** / **`file://` 打开时底部不许提示
  "启动后端"**（那时探针必然连不上：`Origin: null` 不在后端 CORS 白名单里，而后端很可能
  正跑着——两种情形的现象一字不差，底部那句话是唯一能把它们分开的东西）。时间整体按同一
  系数缩放，所以它跑在四秒内，且**不写死任何毫秒阈值**。

- **`asset_check.py` 补的是「同一份文件在两套布局下跑」这件事**：`web/index.html`
  线上放在 S3 站点根（`vendor/x` → `/vendor/x`），本地被 FastAPI 挂在 `/app` 下
  （`vendor/x` → `/app/vendor/x`）。原来那几个引用写的是绝对路径 `/vendor/...`，
  线上对、**本地必然 404**。它的现象是这套验收最偏爱的那一类：**不报错、看着正常、
  其实少了东西**——`echarts.min.js` 拿不到，`renderChart` 里 `echarts` 未定义抛
  ReferenceError，但那句在 `setTimeout(...,400)` 里，所以解读、KPI 卡、洞察、SQL、
  数据表全都照常显示，只有图表框是空白的；字体同时退回系统默认。三条 404 只出现在
  uvicorn 的访问日志里，2026-08-21 汇报前一晚才被人从日志里看出来。
  唯一允许的绝对路径是 `/config.js`（语义是"站点根上那份部署期产物"，不跟着 shell
  的挂载点走），本地由 `server.py` 回一个空脚本兜住 → `window.APP_CONFIG` 缺席 →
  回退 `/api/config`；检查器要求那条路由确实存在，否则这个例外就成了没人管的 404。
  静态那份进 L0（只核对"是相对路径 + 文件在 `web/` 下"），**真取一遍**的那份进 L6
  ——静态检查看不见挂载点和路由。

`scripts/gen/` 的生成器自测（fillers / 跨表闭环正例 / 跨表闭环反例）**需要 numpy**，
本机没装时打印 note 而不是 FAIL——它不在湖仓链路上（装载的是仓库里的 `data/csv/`）。
这三行不能直接删掉：那样"L0 全绿"会被读成"生成器也验过了"。

- **跨表闭环自测的反例一侧**（`selftest_closures.py --negative`，2026-08-28 补的）。
  在此之前那个文件的**断言全是正向的**（当时 105 条），唯一的 helper 是 `ok()`：判据写歪了
  （比错列、把违例算成 0、条件恒真）它照样一片绿，而它跑在最前面、绿得最快，后面每
  一层的"通过"都会被读成"数据是对的"。这正是本文档「比的两端不独立」那一栏命名过的
  假绿灯形态，落在任务书硬约束 #5（写断言 → 跑，确认失败 → 改生成器 → 跑，确认通过）
  的正中央——而那个「确认失败」此前只发生在写代码的当时，没有任何东西把它留下来。
  实现是把 `main()` 拆成 `build_tables()` + `run_all_checks(t, ctx)`，然后往
  `deepcopy` 出来的表里注入 **44 个缺陷**（`ctx.cache` 也深拷一份，走一个只转发
  `n()` / `as_of_end` 的 `_CtxView`，免得反例之间互相污染），要求整套自测变红。
  判据是**红在指定的那条断言上**，不是"有断言红了"：注入的缺陷很可能先撞到一条无关的
  断言，那时反例会"通过"而它本来该盯的判据其实已经坏掉。**第一次跑就抓到一个**：
  `tag-not-unique` 只搬了 `tag_name` 没搬 `tag_type`，而 `tag_pools` 是按 type 分池的，
  于是先破的是分池那条；补上 `tag_type` 才落到 `UNIQUE(product_id, tag_name)` 上。
  最终 44/44 红对，正例侧仍 106/106，两侧合计约 8 秒。
  它盖不到两类，写在 `NEG_CASES` 上方：①注入是**表级**的，病灶在生成器内部的缺陷造不
  出来——典型是 D-01「倍率传导到 GMV」的 `mult[uid-1]` 下标错位，那条判据的红由 2026-08
  实测留档（错位 0.49× vs 阈值 1.48×）；②同一 check 家族里**先判的挡住后判的**，
  商品名的 v1 修饰词模板判据在结构判据之后、`Spearman(累计, 窗内)` 之前有两条更粗的
  判据，所以这 44 个是**每个家族至少一条**，不是每条断言各一个反例。

## L1 云资源状态

```bash
python3 scripts/lakehouse/setup.py --verify        # 只读核查，可随便跑
```

预期 `基建齐备 ✅`：表桶 `analytics-agent-tables`、namespace `app_analytics`、
**两个** Athena workgroup（管理侧 `analytics-agent-wg` 与 agent 专用
`analytics-agent-ro-wg`，各自的结果位置必须落在 `athena-staging/` 与
`athena-staging/agent/`）、以及表桶已联邦进 Glue。

挂了说明什么：联邦掉了（namespace 不再映射成 Glue database）、workgroup 被删、
**agent workgroup 的结果位置被改到管理侧那个前缀上**（那一条报 ❌ 而不是 ⚠️：
共享前缀等于 agent 能读到管理侧查询结果里的明文 PII），
或凭证指向了另一个账号/区域。

## L2 元数据对账（四条独立路径）

```bash
python3 scripts/lakehouse/reconcile.py --catalog "<账号>:s3tablescatalog/analytics-agent-tables" --strict
python3 scripts/lakehouse/verify_doc_sql.py
python3 scripts/lakehouse/verify_enums.py
python3 scripts/lakehouse/verify_constants.py
```

四条**不能互相替代**，因为它们比的是不同层次的东西：

| 脚本 | 比什么 | 独有覆盖 |
|---|---|---|
| `reconcile.py --strict` | 声明态（v1 DDL + 集市 Iceberg DDL）⟷ Glue 实际态 ⟷ 知识库卡片 | 表和**列**：名字、类型、列注释、指标 SQL 引用的标识符 |
| `verify_doc_sql.py` | 卡片里每条示例 SQL ⟷ Athena `EXPLAIN` | **可执行性**：语法 + 目录 + 列 + 类型解析，扫描 0 字节。文档不会被执行，错了没人会响 |
| `verify_enums.py` | 卡片枚举小节 ⟷ 列里的实际取值（双向） | **列里装的值**。上面两条都管不到 |
| `verify_constants.py` | 每个标量列的**基数**（`min`/`max`/`count`）⟷ 一份逐列登记的清单 | **列里有没有值**。前三条全绿时一列仍可能整列恒等于 0 或整列 NULL |

第三条是这个项目那类缺陷最纯的形态：卡片写 `status='active'` 而数据是 `'on_sale'` 时，
SQL 语法正确、对账全绿、EXPLAIN 通过、**跑出来是空集**，然后"没有在售商品"这个结论
就被端出去了。它第一次跑抓出 46 处漂移（`events.event_name` 六个值一个都不存在，
按卡片写的漏斗每一级都是 0）。

`reconcile.py` 的 catalog ID **必须带账号前缀**（`<账号>:s3tablescatalog/<表桶>`）；
Athena 用的那个写法不带前缀。写串了报 `EntityNotFoundException`，而报错指不到成因。
`test_all.sh` 会自己拼，手跑时注意。

### 「隐形声明」：解析器看不见的声明等于没有声明

`verify_enums.py` 的覆盖面不是"卡片里的枚举"，是"**`parse_card()` 认得出的那种写法**"。
两者的差集里每一条都是零覆盖，而报告上和"查过没问题"长得一样。已经撞见**三种形态**，
都是 2026-08-28 这一批：

| 形态 | 实例 | 为什么解析器看不见 | 处置 |
|---|---|---|---|
| ① 枚举写在**围栏代码块**里 | 实测 6 列：`user_profiles.interests`（卡片 16 个英文标签 ⟷ 库里 15 个中文标签，**交集为空**）、`user_devices.device_brand`（8 个英文牌名 ⟷ 库里 11 个、6 个是中文）、`sessions.utm_source` / `utm_medium` / `utm_campaign`，外加 `user_profiles.occupation` 压根没声明。`utm_medium` 那条最狠：卡片正文明写「旧文档写的 `cpc` / `social` 都不存在」，而生成器**一直在产** `cpc` / `social`——两边都在仓库里，六个月没人看见 | `parse_card()` 只认「`### <ascii 列名>` + 表格」，围栏块整段被当正文跳过。这个跳过是**刻意**的（围栏里一行逗号分隔的值不是结构化声明，认它会把 SQL 示例和字段说明表全误收） | 解析器不改，改的是**不许再有隐形声明**：`_no_fenced_enums()` 扫真实卡片树，「`### 列名`」小节里只有围栏块没有表格就判红并给出改法。判据落在**形态**上不落在值上——值由 `verify_enums` 主流程（卡片 ⟷ 线上库）和 `selftest_closures`（卡片 ⟷ 生成器产出）各自去比，前提是声明得先看得见 |
| ② 声明了枚举但列是**数组类型** | `user_profiles.interests`（`array(varchar)`） | 声明认得出，取数那边炸了：`CAST(array(varchar) AS VARCHAR)` 在 Trino 里直接 `TYPE_MISMATCH`。此前这类列是**静默跳过**的 | `actual_values()` 加了 `types` 驱动的 `CROSS JOIN UNNEST` 分支，数组列照样进对账 |
| ③ 字面量写在**表结构描述那一栏** | `user_profiles.md:13`「国家，默认 `'China'`」 | 它根本不在枚举小节里，是表结构表格的一个单元格。`verify_enums` / `reconcile` / `verify_doc_sql` 全都不看 | 被 `verify_constants.py` 顺手抓到（见下节）——常量列的那个值可以拿去和卡片对 |

形态 ② 有两处**口径差**，写在 `actual_values()` 的 docstring 里，读输出时要知道：
`n` 从"行数"变成"元素出现次数"（`interests` 15 个标签 / 500 行 → 合计 1458），
`<NULL>` 从"这行是 NULL"变成"这行的数组是 NULL **或空数组**"。后者那条 SQL 带
`HAVING COUNT(*) > 0` 不是可选的：无 `GROUP BY` 的聚合恒返回一行，n=0 时会在输出里
伪造出一个「+0 NULL」，也会盖掉真有 `'<NULL>'` 这个**元素值**的情形。

反过来，`knowledge/domains/behavior/sessions.md:58` 是这类事该怎么写的**正面样本**：它
明写「`facebook` / `instagram` **都不存在**，这一列也没有 NULL，也没有字符串 `'none'`」。
把"哪些值不存在"也记下来，挡的是 agent 自己发明一个哨兵值去 `WHERE utm_source = 'none'`
然后拿到空集当结论。枚举对账只能保证卡片里**写了的**值存在、数据里**有的**值写了；
"读者可能猜到的那个值不存在"这件事只有散文能承载，而散文正文零覆盖。

### 退化列：前三条全绿时列里可能什么都没有

`verify_constants.py`（2026-08-28 新增，L0 有分类器自测、L2 连云跑普查）。判据是对每个
标量列取 `min(CAST(c AS VARCHAR))` / `max(...)` / `count(c)` / `count(*)`：`min = max`
⟹ 整列同一个值，`count(c) = 0` ⟹ 整列 NULL。

**为什么前面每一层都看不到它**：`verify_load.py` 比的是 CSV 真源 ⟷ Athena，而 CSV 里
那一列本来就全是 0，灌得**完全忠实**；`verify_enums.py` 只看声明过枚举的列，而退化列
恰恰最容易是没声明的那种；`verify_doc_sql.py` 的 `EXPLAIN` 不看基数；L1–L5 全是数量
关系，而**一列恒等于 0 满足任何求和恒等式**。于是每层全绿而
`knowledge/metrics/core_metrics.md:242-246` 的 `like_rate` / `comment_rate` 在线上库
**恒等于 0.00 且不报错**——正是本文档反复点名的「不报错、数看着合理、结论是反的」。

实测（468 个标量列 + 8 个数组列跳过，全库 75s）：**33 个常量列 + 46 个整列 NULL**，
账算得平（389 正常 + 33 + 46 = 468）。33 = 14 生成器侧待重灌 + 17 透传遗留 + 2 单行表。
逐列登记在脚本里的三份清单，每条带**谁该修它、什么时候能删**：

- **`RELOAD_PENDING`（14）**——生成器已产非常量值，清除条件就是重灌。含
  `posts.like_count` / `comment_count` / `share_count`（三计数器现在从明细回填）、
  `user_attributions.attributed_at`（线上库 350 行全等于灌数那一瞬，按归因时间分桶只有
  一个桶）、8 个审计时间戳（v1 把它们写成了装载瞬时值），以及下面单列出来的两条。
- **`DIMS_PASSTHROUGH`（17）**——落在 `dims_to_parquet.DIMS` 那 12 张透传表上，逐字节
  来自 v1 CSV，**重灌不会修**。表名从 `dims_to_parquet.DIMS` **现读**，不在这里复制
  一份：那份清单变了这里要跟着红。它们不是豁免，是"已知、且当前没有任何代码负责产它"，
  和 `budget.py` 那 4 张声明 SUB 却没人兑现的表是同一批活。
- **`ALL_NULL_PINNED`（46）**——**只钉集合，未逐条裁定**。「可空业务列在当前数据下
  无人填」和「这列本该有值却丢了」要逐列定口径，不在这一轮范围里。其中
  `tmp_campaign_roi_analysis.attributed_gmv` / `roi` 是 eval **刻意的**陷阱题，
  不该被裁定成缺陷。

两条值得单独记的实例：

1. **`user_profiles.country` 线上库是 `'中国'`**，而卡片 `user_profiles.md:13` 和生成器
   `tables.py` 都写 `'China'` ——`WHERE country = 'China'` 返回 0 行且不报错。这是上面
   形态 ③，**只有这个检查抓得到**，因为 `min/max` 会把那个常量值带回来。
2. **`payments` 有 145 行 `status='refunded'` 而 `refund_amount` 整列 0.00**，自相矛盾。
   边界是确认过的：`fin_daily_revenue` 的退款口径走 `orders.actual_amount` 而不是这一列
   （`database/10_derived.sql`），所以「退款总额」那道题当前**答得出真数**。

**为什么是逐列登记而不是一律判红。** 隔壁 `verify_literals.py` 面对同样处境选择了
**不接进 `test_all.sh`**（理由见下面「为什么刻意不接进 `test_all.sh`」那一节）。这里走了
另一条路，因为这里的红集是**有界且可逐条点名的**（33 + 46 列，不是无界的文本质量），
所以能做成清单：清单里的列打印+说明，清单外的列 FAIL，**清单里但已经不退化了的也 FAIL**。
最后那条抄 `selftest_closures.py::ENUM_SUPERSET_OK` 的形态——豁免必须带失效条件，
否则它变成永久免疫，而"某列被修好了"和"某列还没修"在报告上长得一模一样。L8 的
`degenerate-col-unlisted` / `degenerate-col-stale-entry` 成对守这两个方向。

`ENUM_SUPERSET_OK` 本身是同一形态的先例，值得对照着看：它豁免的是「生成器产出比卡片
声明**更宽**」这种情形（生成器产 39 个真实活动名，卡片写的是 v1 那 6 个低基数占位符），
失效条件是「全量重灌 + 按实际取值重生卡片」。**这个条件在本分支上兑现了，所以那条豁免
被删掉、清单现在是空的**——不是"留着当历史记录"：留着的话生成器以后多产一个第 40 个值
会被印成「刻意超集 ⊋ 声明」然后照样绿，豁免恰好吞掉它本该抓的那种漂移。机制留着、清单
空着，再加条目要同时给出消除条件、护栏（产出必须真的更宽）和一个证明它失效时会红的反例。
两处的共同点是：**豁免是有到期日的**，没有到期日的豁免就是把检查器关掉；到期了就该删，
删不掉就说明那不是豁免而是一条没写的判据。原来盯着那条豁免的 L8 反例
`enum-superset-exemption-void` 随之改名成 `enum-values-wholesale-swapped`，同样的注入
（整列换成 3 个未声明的值）现在红在无条件那条断言「生成器枚举产出全部在卡片声明内」上。

`min/max` 而不是 `count(DISTINCT c)`：两者对"是不是常量"等价（任何全序下 min=max ⟺
常量），但便宜得多，而且顺带把那个常量值带回来——`country` 那条就是靠这个值发现的。
已知失真一处：`double` 的 `-0.0` 与 `0.0` 转成不同字符串，这种列会被判成"非常量"，
方向是**漏报不是误报**，且本库没有 double 列。8 个数组列不在普查面里（Trino 对数组的
min/max 是逐元素比较，"常量"在数组上要先定义），**跳过的列逐个打印出来**——理由同
形态 ② 的教训：报告上「没被查过」和「查过没问题」长得一样，缺口就等于不存在。

## L2+ 数据真实性诊断（五个报告器，**不是闸门**，`test_all.sh` 不跑）

L2 那三条问的是「**声明和实际对不对得上**」。这一组问的是另一件事：**数据本身像不像
真的**。卡片写 `on_sale`、库里装 `on_sale`，L2 全绿；可这批商品叫「优质戴森 电视」、
戴森不卖电视、价格 431.47、166 个类目摊 200 个 SKU——每一条都通不过一个业务人的眼睛，
而 L0–L6 里没有任何一条会红。

```bash
python3 scripts/lakehouse/verify_literals.py                        # 字面值多样性
python3 scripts/lakehouse/verify_semantics.py                       # 品牌/类目/价格配对
python3 scripts/lakehouse/verify_resolution.py                      # 维度表分辨率
python3 scripts/lakehouse/verify_correlation.py                     # 画像 ⟷ 消费相关性
python3 scripts/lakehouse/verify_behavior.py                        # 四张行为大表的分布形状
# 同一套判据也能对生成侧跑（这才是能看到绿灯的一侧）。$OUT 是 main.py --out 的产出目录；
# **各条需要的 scale 不同**，写在行尾——按小的那个迭代，别一上来就造全量：
python3 scripts/lakehouse/verify_semantics.py   --from-csv $OUT   # 商品域即可（products/product_tags/order_items）
python3 scripts/lakehouse/verify_resolution.py  --from-csv $OUT   # 同上
python3 scripts/lakehouse/verify_correlation.py --from-csv $OUT   # 要 scale ≳ 500，效应量在小样本上量不出来
python3 scripts/lakehouse/verify_behavior.py    --from-csv $OUT   # scale 1 就够迭代
python3 scripts/lakehouse/verify_literals.py    --from-csv $OUT   # 要**全表**产出，它普查所有文本列
python3 scripts/lakehouse/verify_semantics.py   --selftest           # 判据自测，无云依赖
python3 scripts/lakehouse/verify_behavior.py    --selftest --why      # --why 打印每条判据的理由
```

**五个现在两条路都能跑到底**（`verify_literals.py --from-csv` 是 2026-08-28 补的，之前
它只有 Athena 一条路，于是生成器的字面值修复没有任何一条路验得到——当时云上是 v1 规模、
已决定不重灌；**2026-09-17 更正：湖后来还是被重灌成 8000 万行那一批了**，见「一处与
phase-2 任务书不符」那节顶部的补充）。`verify_behavior.py` 是印证做得最实的一个：不带参数走 Athena（18 个
聚合查询），`--from-csv` 走逐行流式，两条路都把结果收进同一个 `Facts`、喂给同一批纯
判据函数，跑出来 27 个结论逐位相同——**这说明两份实现对得上，不说明数据对**，两端本来就
不独立（见本文档「两端不独立」那一栏）。**2026-09-17 起这里还多了一层麻烦**：湖已重灌成
8000 万行那一批，`data/csv` 不再是灌进 Athena 的那一份，所以两条路现在读的是**两份不同规模
的数据**，"27 个结论逐位相同"这个印证在当前湖上不再成立（分布形状的判据大多仍成立，但逐位
相同不该再期待）。要重新拿到那个印证，得让 `--from-csv` 那条路读全量产出目录而不是
`data/csv`。补 `verify_literals`
时按同一条线抽出了三个共用判据（`merge_col_verdicts` / `judge_posts` / `judge_device`，
`verify_literals.py:185` 起），**PASS/FAIL 的合成规则一行都没有复制第二份**：这类规则
（谁盖谁、EMPTY 算不算过）本身是判据的一部分，复制两份就会出现「云上判 FAIL、离线判
PASS」这种只由报告代码引起的分歧。

`verify_behavior.py` 也是唯一一个**scale 1 的小样本就能给出确定结论**的（分布形状的判据
不像分辨率类判据那样依赖全量），所以迭代成本是 1 秒造数 + 1 秒判读。**但小样本迭代有个
代价，2026-08-28 实测栽过一次**：`--from-csv` 那条路有一处二次复杂度，只在千万行量级
现形，scale 1 上根本量不出来（见「没有自动化覆盖」里「报告器自己在目标规模下跑不跑得完」
那一栏）。所以判据用小样本迭代，**至少一次全量**要真跑过——现在两条都跑过了，L5 119s、
L9 157s，见最近一次实测。

| 脚本 | 判据真源 | 通过线 | 独有覆盖 |
|---|---|---|---|
| `verify_literals.py` | 各列自身的分布 | 无硬线，报可疑项 | **一列里只有一个值**这类形态（`orders.remark` 1 个 distinct、`events.referrer` 2 个）。行数对、类型对、枚举对，可是拿它分组只会得到一行 |
| `verify_semantics.py` | `scripts/gen/semantics.yaml` | 0 违例 × 5 类 | **单行的业务合理性**：(品牌, 叶子类目) 在不在白名单、价格在不在「类目带 ∩ 品牌分档窗口」、商品名能不能由「修饰词+品牌+类目」还原 |
| `verify_resolution.py` | 同上 + `scripts/gen/budget.py` | 6 条推导出来的线 | **整体的分辨率**：空类目数、每类目/每品牌 SKU 数、每 SKU 被下单次数、类目内价格跨度 vs 声明带宽 |
| `verify_correlation.py` | `scripts/gen/profiles.yaml` | 4 条极差线 + 4 条方向约束 + 1 条人数分布 | **列与列之间有没有边**：画像四维（收入/职业/年龄/性别）分档后的人均 GMV 极差与方向 |
| `verify_behavior.py` | **样本自算的随机基线** + 两张卡片自己声明的规则 | 27 条：8 条集中度（写成基线的倍数）、11 条定义性（通过线恒为 0）、4 条取值域、4 条比值/区间 | **四张行为大表自身的形状**：度分布重尾、互惠性、漏斗形状、标记 ⟷ 事实一致性、取值域退化。`post_likes` / `page_views` / `user_follows` / `push_notifications` 合计 95,029 行，此前的自动化覆盖**只有计数对账**（`like_count` / `page_view_count` 的行数对得上），形状零覆盖 |

前三条的分工值得写下来，因为很容易以为一条就够：**L7 判单行对不对，L6 判整体够不够
分**。一个类目里只有 1 个商品时，`verify_semantics.py` 每一行都合法，可「类目 A 客单价
高于类目 B」这句结论实际上是「商品 a 比商品 b 贵」——**这种缺陷没有任何一行是错的**，
逐行判据永远抓不到。反过来 L6 看分布，看不出「戴森卖电视」。

`verify_correlation.py` 是第三种，与前两种都不重叠：D-01 的每一行都合法（一个 30 岁的
高收入设计师花了 1200 元，无可指摘）、每个维度自身的分布也够分（12 个职业各上万人），
坏的是**两列之间没有关系**。只有把两列交叉起来看分组均值才看得见。

它多出一种判定：**WEAK**（这批数据分辨不出通过线那么大的差异）。加它是因为 FAIL 和
WEAK 的修法完全相反——FAIL 改生成器，WEAK 加数据量。混成一个红灯，小样本上的每次运行
都会把人推向"去调生成器"，而那时生成器可能是对的。功效用**通过线**而不是实测极差算，
所以不会出现"实测极差恰好很小 → 自动判成看不清"这种事后开脱。报告里 WEAK 不计入通过。
`verify_behavior.py` 沿用同一套四值词汇，功效闸换成三个各管一件事的下限（父行数 < 30 /
平均度数 μ < 4 / 分组格子 < 30），混成一个就分不清「判不动」的原因。

`verify_behavior.py` 是第四种，**它问的是一张表内部的形状**，与上面三种都不重叠。四张
行为大表在它之前的自动化覆盖只有一句「行数对得上」，于是这些全都不会让任何东西变红：
500 个用户的点赞数落在 49~100 之间（真实社交是重尾，多数人 0 次）；19,165 条关注边里
互关 7.74%，而同一张图**完全随机连边**的期望互关率是 7.68%；15 个页面的 PV 极差 6%，
`checkout` 比 `home` 还多；`is_bounce` 与实际页面数完全脱钩（103 个「跳出」会话的页面数
是 2~10，**没有一个是 1**，而卡片写着 `page_view_count = 1 → TRUE`）。主键唯一、外键不
悬空、枚举合法、行数对账全过——**而这四条各自否掉一整类分析**（内容热度榜、KOL 识别、
页面漏斗、流量质量）。

它的判据来源也和前三种不同：**通过线全部写成「样本自算的随机基线的若干倍」**，而不是
绝对数。等概率多项抽样下父行度数近似 Poisson(μ)，于是 top-k 份额的期望 =
`k·(1 + z̄_k/√μ)`（`z̄_k = φ(Φ⁻¹(1−k))/k`）、Gini 的期望 ≈ `1/√(πμ)`。这件事很要紧：
「完全均匀」的 top10% 份额不是 0.10 而是 0.12~0.13——抽样噪声本身就制造一点集中度，
所以把通过线写成 0.15 之类的绝对数会在小样本上误判成「有重尾」。写成倍数则**与规模
无关**，样本越大基线越趋近 k、门槛自动收紧。四个公式值与现行库实测逐位吻合
（0.1294/0.1300、0.1208/0.1210、0.0143/0.0143、0.1714/0.1660），`--selftest` 拿这四个
**实测点**核公式，而不是拿公式核公式。

另外两类判据是 (b) **卡片自己声明的规则**（`sessions.md:41` 的 is_bounce 判定表、
`push_notifications.md` 的「状态判断逻辑」表——两条在现行库上都**恒假**，也就是卡片教给
agent 的判据在这份数据上查不出任何东西）和 (c) **可推导的上界**（均匀分布 U(a,b) 的
top10% 份额上界恰好是 0.19，所以「停留时长 top10% > 0.19」等价于「这一列不是均匀抽的」；
`home PV / checkout PV ≥ 3` 等价于「首页→结算粗转化率 ≤ 33%」，是任何电商都到不了的
上界）。**行业绝对数字一律不用**：跳出率那条判的是「落在 (5%, 90%) 内」即这个指标带不带
信息，不是「等于真实电商的 40–60%」——后者无法复核。三条候选判据因为找不到可复核的
依据被降级成「只报数不判」（自赞率、五类推送量的 max/min、推送量 ⟷ GMV 的方向），
理由逐条写在脚本 docstring 末尾。

集中度全部用 **LEFT JOIN**（零度父行进分母），这与 `verify_correlation.py` 的 `JOIN`
**刻意相反**：那边问「已经在消费的人里画像能不能区分消费额」，没下过单的人不该进分母；
这边问「有多少内容没人看」，零赞帖正是信号本身。同一个仓库里两种口径并存，所以每条
报告都写清分母是什么。所有集中度都从「度数 → 父行数」直方图**精确**算出（不抽样、不用
`approx_percentile`），一个纯函数 `concentration()` 同时服务两条取数路径——两侧各写一份
实现的话，一致时你分不清是都对还是都错。

### 为什么刻意不接进 `test_all.sh`

这几个脚本对云上库跑**必然是红的**，而且短期内会一直红：现行库是 v1 那批 200 行商品、
四张行为大表也是 v1 那批（21/27 红），项目已决定不重灌数据。默认流程里挂一盏永远红的
灯，代价不是"多看一眼"，是把所有人训练成无视红灯——那会顺带废掉旁边那几十盏真闸门。
所以它们的 `main()` **一律 `return 0`**，脚本头上写着「诊断工具，不是闸门」。

绿灯要在**生成侧**看（`--from-csv` 指向 `scripts/gen/main.py` 的产出目录），跑的是同一批
judge 函数。两侧都能跑是有意的设计：**一个只会亮红灯的检查器无法证明它在绿灯那一侧也
判得对**。另外每个脚本都有 `--selftest`，喂构造行给纯函数判据，正例反例各一组；
**五个的 `--selftest` 现在都接进了 L0**（合计 157 项断言，无云依赖、秒级）。

前三个是 2026-08-27 才补挂的，记一下为什么：写头两批时还没把「自测就是这批判据的唯一
闸门」当成模式，等到第三、四批定下来，前两批已经落在测试之外——48 项断言存在、没人跑。
**这类不一致不会以红灯的形式暴露**，它长得就像"这些检查器没有覆盖"，而实际上覆盖是
写好了的、只是没接线。挂进来的代价是六行，不挂的代价是判据的区分力可以静静退化。

顺带说清 L8 为什么对这五个**结构上不适用**，免得被当成待补的缺口：L8 的机制是「注入
缺陷 → 断言变红 → 还原」，而第一步要求**注入前是绿的**——`test_all.sh` 里那句「前面有红
就不跑负测」正是这条前提。这五个的正向那一支在现行库上恒红，所以"从绿变红"这个信号
根本产生不出来（它们还一律 `return 0`，退出码也不承载判定）。红绿双侧夹具承担的就是
L8 那份保障，只是不叫 L8。**真正剩下的缺口是取数那一段**，见「没有自动化覆盖」一节。

### 判据的两条硬约束

- **判据必须可复核，不能是执行者的行业印象。** 「戴森不卖电视」「口红 39~299 元」这类
  判断如果散在检查器的代码里，就成了一个人的主观印象，别人无法复核也无法修正。所以
  全部外置到 `scripts/gen/semantics.yaml`，配套 `semantics.py` 在 `load()` 时做 9 项
  自相矛盾校验（L0 会跑，见上）。
- **生成器和检查集必须共用同一份，否则修完生成器再改检查就是自证。** 共用配置只是
  第一层：把价格区间算错的方式有很多种（分档窗口乘反、上下界当绝对值而不是比例），
  所以**公式也共用**——两侧都调 `SEM.Semantics.price_range()` 这**同一个函数**，
  `verify_semantics.py` 里没有任何一处自己算区间。

第一条约束在**没有配置可以外置**的时候还有第三种解法，`verify_behavior.py` 用的是它：
**让判据自己校准**。「点赞够不够集中」既没有 yaml 可依、也不该由执行者拍一个 0.15，
于是通过线写成「**样本自算的随机基线**的 2 倍」——基线是「完全均匀时这个指标会是多少」的
解析式（`k·(1 + z̄_k/√μ)`），审阅者可以拿现行库的四个实测值当场核它，且门槛与数据规模
无关。同理，`Gini(粉丝) − Gini(关注) ≥ 0.10` 的分母侧期望是 0（两个分布同 μ），
`互关率 / 边密度 ≥ 3` 的分母就是「互关全是巧合」时的取值。**判据的可复核性来自它能被
独立重算，不来自它被写在哪个文件里。** 剩下的判据用第二种可复核来源：卡片自己声明的
规则（通过线恒为 0，没有可调余地）。三条候选判据因为两种来源都够不上而被降级成
「只报数不判」，理由写在脚本 docstring 里——**设一条无法复核的线，比不设更坏**。

### 一处与 phase-2 任务书不符：v3 的库整体都是 v1 规模

> **⚠️ 2026-09-17：这一节已经被现实推翻,但刻意留着,因为它记的是「潜伏缺陷显形」的全过程。**
> 本节下面所有实测数字是 **2026-08 湖里还装着 `data/csv/` 种子**时量的。那之后湖被重灌成
> 8000 万行那一批(新生成器 `scale≈427`,由 `feat/data-reload-80m` 的 `load_parquet.py` 灌,
> 本分支上没有那个脚本)。**逐条对照下面第 2 点的预言**:`products` 现在是 **4,133**(生成器
> 补上 builder 了),`orders` 854,140、`order_items` 1,804,371 —— 也就是「谁灌一次全量数据它
> 就当场显形」这句话已经兑现。**仍然没兑现的是维度侧**:`coupons` 150、`ad_campaigns` 50
> 一动没动,于是 `user_coupons` 970 万条核销只指向 150 张券(见下面「`budget.py` 声明的规模
> ⟷ 生成器实际产出」那一栏)。当前规模与核查命令见
> [deployment.md](deployment.md#数据说明)。

任务书 D-02 写的是「事实表按 427 倍放大了，维度表还是 v1 原样，平均每个 SKU 被下单约
9,021 次」。实测本库**不是这样**：

```
users 500 · orders 2,000 · order_items 4,225 · events 20,000 · products 200
```

事实表和维度表**一起**停在 v1 规模。427 倍放大只发生在那套 v2 Redshift 库上（v3 的
源码没上传过，所以任务书是照 v2 写的）。两个后果要记住：

1. **「每 SKU 被下单次数」在云上是绿的，而这个绿不含信息。** 它是个比值，分子分母
   一起小 427 倍，比值照样漂亮（实测 21.1，通过线 1000）。把它读成「这项没问题」，
   就正好错过 D-02 要修的那件事。`verify_resolution.py` 因此在 `order_items` 行数
   远低于全量目标时打一条提示，就印在那条 ok 的上面。
2. **D-02 在 v3 里是潜伏缺陷，病灶在生成器。** `budget.py` 声明 `products` 走 SUB
   缩放（scale=427 → 4,133 行），而 `tables.py` 里这一类**整个没有 builder**，
   `main.py` / `dims_to_parquet.py` 反而各自从 `data/csv` 读那 200 行 v1 商品。谁灌
   一次全量数据它就当场显形。所以修在生成器层是对的层次，不是"云上数看着还行"。

## L3 装载完整性（CSV 真源 ⟷ Athena 现查）

```bash
python3 scripts/lakehouse/verify_load.py                     # 全量 35 张，约 1 分钟
python3 scripts/lakehouse/verify_load.py -t users -t orders  # 抽查
```

逐表比行数、数值列求和、时间边界、布尔计数。**改过 `data/csv/` 或重灌过表必须跑全量**
（`bash scripts/test_all.sh --full`）。

这里**不用**归档的基线 JSON。原来那两份（`consistency.generator-expected.json` 等）
记的是 v2 那批 21 万行的绝对值，与现在的 `data/csv/` 已经不是同一批数据，比起来
**稳定通过但什么也没验证**。原话写在脚本头上：基线会过期，而且过期时是绿的。

挂了说明什么：装载丢行、类型映射掉精度、时区偏移。

**⚠️ 2026-09-17 起这一层在本仓库开发账号上必然红，原因不在装载**。湖已重灌成 8000 万行那
一批，而这个检查比的是 `data/csv/` 种子 ⟷ Athena，两端根本不是同一份数据（`order_items`
CSV 4,225 ⟷ Athena 1,804,371）。**读到这盏红灯先分清是哪一种**：

| 现象 | 含义 |
|---|---|
| 行数/求和差出 427 倍量级 | 湖里是重灌后那批，不是装载缺陷。要覆盖那一批得用 `feat/data-reload-80m` 的 `verify_load.py`（它对的是全量产出的 `_expected.json`，不是 `data/csv/`） |
| 差一点点（少几行、尾数不同、时间偏几小时） | 才是这一层要抓的：装载丢行、精度、时区 |

修法**已经存在并实测验过**（2026-09-17，在 `feat/data-reload-80m` 的临时 worktree 里对同一个
湖跑出 `装载完整 ✅`）：`_resolve_csv_dir()` 从装载快照 `data/loaded_row_counts.json` 的 `source`
反推该跟哪份 CSV 对账。这一层与另外三盏红灯的逐条归因、修法与验证输出，见本文档末尾
「最近一次实测（2026-09-17）」。

**更该警惕的是它反过来的那一面**：如果湖里被人用 `load.py` 灌回了种子，这个检查会**全绿**
——绿的同时湖已经从 8000 万行退回 22 万行。这盏灯的绿只保证「`data/csv/` 那一份完整地在湖
里」，**不保证湖里没有别的、更大的一份被它覆盖掉了**。

## L4 治理层（最小权限角色 + 列级排除）

```bash
python3 scripts/lakehouse/governance.py --selftest        # 离线，秒级
python3 scripts/lakehouse/governance.py --verify          # 只读核查 + 以 agent 角色实测
python3 scripts/lakehouse/governance.py --verify-backend  # 后端真的在用这个角色查
python3 scripts/lakehouse/governance.py --apply           # 建角色 + 发 LF 授权（唯一会写的一条）
```

落地方式是**一个专属 IAM 角色（`analytics-agent-ro`）+ Lake Formation 列级授权**。
后端设了 `AGENT_ROLE_ARN` 就走 AssumeRole 用它查数，`/health` 的 `identity` 字段回传
生效身份——那是从外面唯一能看见「治理接上了没有」的地方。

**这里有一处真实的能力下降，不是换了个说法：** v2 用 Redshift 的动态脱敏（列在，
值变成 `***@masked.invalid`），而 **Lake Formation 没有值级掩码原语**。等价物只能是
列级排除：`users.email` / `phone`、`user_profiles.birth_date` **不在授权面里**，
`SELECT *` 的结果里没有这几列，点名 `SELECT email` 报 `COLUMN_NOT_FOUND`。
`user_messages` 整表不授权，引用它报 `Insufficient Lake Formation permission(s)`。
取舍与为什么不走 Glue Catalog View 那条路（48 张表要复制一份视图 + 多一个没人对账的
元数据面），写在 `scripts/lakehouse/governance.py` 的 docstring 里。前端 `masked`
这个键名为了契约保留，文案已改成「不授权的列」。

三条检查各自不可替代，少哪条都留下一种「看起来绿了」：

| 检查 | 独有覆盖 | 少了它会怎样 |
|---|---|---|
| `--selftest`（离线） | 策略清单（`EXCLUDE_COLUMNS` / `DENY_TABLES`）⟷ 验收契约（`MUST_NOT_READ_*`）互相覆盖；IAM 策略窄不窄（含两个 workgroup 的结果前缀是否分开，配了三个正对照） | 有人把一列从策略里拿掉，云上照发照过，探针也照样全绿 |
| `--verify`（连云） | 授权面比对 **+ 以 agent 角色实测 18 条探针**：该读到的读到、该读不到的读不到（含 Iceberg 元数据表 `users$files` / `users$snapshots`——它们的 `file_path` 列是直读旁路的入口，而 `db.py` 的只读闸门放行这种 SELECT）；**外加结果集隔离探针**（对管理侧 `athena-staging/` 的 list / get / put 三样都必须被拒） | 授权齐不等于查得动；也发现不了「授权面对但边界不在」，以及「目录层排掉了但结果 CSV 还能读」 |
| `--verify-backend` | 后端确实在用受限凭证：`backend_info()["identity"]` 是那个角色，且它读不到 `users.email` | 角色建好了、权限发了，而 `db.py` 仍用 admin 凭证——治理全在，只是没接上 |

`--selftest` 里那两份清单**刻意不互相推导**：验收契约要是从策略现算，
L8 的 `gov-policy-loosened` 注入完自测照样全绿。同一个理由，它还专门盯
**两条明文旁路**：

1. **`csv/` 中转库**——`analytics_agent_raw` 里是明文 CSV 外部表，Lake Formation
   完全看不见它，所以角色的 S3 读被钉死在 `athena-staging/agent/` 这一个前缀上，
   这条断言配了正反两个对照（挖一个 `csv/*` 的洞必须被抓到）。
2. **Athena 自己的查询结果**——结果集是明文行数据的 CSV，落在 workgroup 的
   `OutputLocation` 下。所以 agent 走**自己的** workgroup（`analytics-agent-ro-wg`）、
   读写面只到 `athena-staging/agent/`；管理侧那半边它既读不到（否则治理探针自己跑的
   `SELECT email FROM users LIMIT 1` 那一行明文就在那儿）也写不到（否则它能覆盖
   对账脚本要读回的结果对象）。列级排除管的是「查得到吗」，管不了「结果放哪儿」。

少了这两条，上面所有列级授权都是装饰，而云上探针一条都不会红。

`--apply` 的安全边界：只建不删。给角色打 `Project` 标签，**遇到同名但没这个标签的
角色直接拒绝动它**；LF 权限只发不撤（LF 授权是可叠加的，所以多出来的宽授权由
`--verify` 报成漂移，要撤得显式加 `--revoke-extra`）。

`backend/catalog.py` 没配 `AGENT_ROLE_ARN` 时 `/api/catalog` 的 `governance` 返回
`{available: false, reason: …}`，前端显示"为什么没有"而不是画一个空的"已脱敏"面板——
**一个看起来齐全的治理面板比没有面板更危险**（v2 的 `web/catalog.json` 快照就干过
这事：治理层根本没实现，每个访客却看到"PII 已脱敏"）。同理，跑服务的身份是 data lake
admin 时不能拿它的授权面充数：LF 对 admin 整体绕过，列出来会是"48/48 全部授权"。

### 第二处诚实的边界：列级排除拦不住持凭证直读

上面那处能力下降（没有值级掩码）是**功能**上的。这一处是**边界范围**上的，比前者更容易
被说过头，所以单独写：

`s3tables:GetTableData` 是**不得不给**的。S3 Tables 的表桶注册不进 Lake Formation
（`RegisterResource` 对表桶 ARN 返回 `Unsupported resource path`，对联邦 catalog ARN
返回 `Un-supported resource arn format`），因此不存在"LF 代发临时凭证、调用方自己没有
数据面权限"那条路——Glue 解析联邦目录时用的是**调用方身份**。不给这个动作，整个目录
解析失败，Athena 侧报 `TABLE_NOT_FOUND`（指向"表不存在"，极难往权限想）。

而给了它，持有该角色凭证的进程可以绕开 Athena：`get_table_metadata_location` → 直读
metadata JSON → 顺 manifest-list / manifest 走到 Parquet。实测 `users` 的数据文件里
`email` / `phone` **明文可读**。

所以准确的说法是：**Lake Formation 的列级排除是查询引擎层的控制。** 它拦住的是
agent 写的 SQL 和 agent 看得见的目录——`SELECT *` 里没有这些列、点名查报
`COLUMN_NOT_FOUND`、`information_schema` 里也查不到，所以 agent 既读不到也无法据此
编出 SQL。它**不是**对持有该角色凭证的进程的隔离。这和 `csv/` 明文旁路同类，
只是更隐蔽：那条是"同一份数据在别处还有一份明文"，这条是"同一份数据的授权面下面
还有一层没被治理"。

这个结论由 `probe_direct_read()` 盯着，**方向和其它所有探针相反**：它断言直读
**应当成功**。变红意味着旁路被堵上了（AWS 支持了表桶注册，或者策略被收窄），
那时要改的是这段文档——把列级排除升级成真边界——而不是改探针。一个"这里其实防不住"
的结论，值得和"这里防得住"的结论一样被断言盯着，否则它会随着环境变化悄悄变成假话。

顺带撤掉两个"直觉上必需、实测不需要"的授权，两个都在 `--selftest` 里钉住别飘回来：

| 撤掉的 | 为什么直觉上像必需 | 实测 |
|---|---|---|
| `lakeformation:GetDataAccess` | AWS 各种示例策略的标配；"LF 管着数据，当然要允许走 LF 取凭证" | 去掉后查询照跑、被排除的列照样拒。没有注册位置就没有凭证代发，这条路上本来什么都没发生。给了只是凭空多一条 `Resource: "*"`，还会让人误判数据面是 LF 在守 |
| `glue:GetUnfiltered*` | 名字读起来正是"LF 按列级授权裁剪后返回"的那组接口，排查 `TABLE_NOT_FOUND` 时加过 | 账号 `AllowExternalDataFiltering = false`，直接调一律 AccessDenied；而 Athena 是 first-party，不受这个开关约束也不走这条路。给了既不解决问题也不产生效果，只让策略看起来比实际更宽 |

### 云上执行踩出来的四个坑

都是"看起来对"的那一类，所以每一个都留下了一条断言，而不只是一句注释：

| 症状 | 根因 | 现在盯着它的 |
|---|---|---|
| Athena 报 `Unable to verify/create output bucket`，18 条探针全红 | `s3:GetBucketLocation` 和 `s3:ListBucket` 写在同一条带 `s3:prefix` 条件的语句里。桶级请求里**没有对象键**，`s3:prefix` 这个条件键根本不存在 → 条件恒为假 → 这个动作其实没授权，而策略文本上看起来给了 | `policy_findings` 的 `_NO_PREFIX_CONDITION` 规则（方向是"太窄"，只朝"太宽"看的审查器对它永远绿）+ selftest 正对照 |
| 治理层完全没生效时，4 条「读不到」探针报绿 | 失败原因白名单里有 `does not exist`，而 `CATALOG_NOT_FOUND: Catalog '…' does not exist` 正好撞上它。查询压根没走到权限判定那一步 | `is_denial()`：先用 `_NOT_A_DENIAL` 排除基础设施级失败，再看像不像拒绝。顺序是刻意的；selftest 里有 5 条真实出现过的基础设施报错做负对照 |
| Athena 报 `TABLE_NOT_FOUND`，而表在 Glue 里查得到 | 联邦 catalog 把 Glue 请求转发给 S3 Tables 服务时用的是**调用方身份**，角色一个 `s3tables:` 动作都没有。Glue 那层的报错是不带来源的 `Access Denied`，到 Athena 只剩 `TABLE_NOT_FOUND` | `S3TablesRead` 语句（七个动作，逐个 bisect 出来的）；selftest 钉住 `GetTableData` 必须在策略里 |
| `--apply` 每次都报「0 张已符合，47 张新发」 | `list_permissions` 的 `Resource` 是**精确**过滤器而不是范围过滤器：拿 `Table{TableWildcard:{}}` 去问，那 47 条 `TableWithColumns` 一条都不返回。LF 的 grant 本身幂等，所以既不报错也不出错，只是**永远看不出**授权面有没有被人改过 | 逐表查 + `--apply` 发完**回读核对**。同一个 bug 在 `backend/catalog.py::_governance` 里还有一份，后果更糟：`available: true` 而 `granted_tables: 0`，UI 会正常地画一个"0 / 48 已授权、48 张表全部未授权"的面板 |

### 云上那半边的治理证据：`{"op": "health"}` 与中继的 `/health`

上面三条检查（`--selftest` / `--verify` / `--verify-backend`）都是**本地**发起的：
`--verify-backend` 读的是本机进程的 `db.backend_info()["identity"]`。云上那半边——
AgentCore Runtime 里那个容器**以谁的身份查数**——原来在外面没有任何证据。它的失效方式
是最坏那种：`AGENT_ROLE_ARN` 掉了的容器用 exec role 查数（没有列级边界），而它答起来和
正常容器**一模一样**。`runtime_config._require_governance()` 让配错的容器起不来，但
「起不来」在外面看是 5xx，看不出是治理还是别的。

所以 Runtime 上有一条**不进模型**的旁路，中继上有一个字段：

```bash
# ① 直接问 Runtime（要 bedrock-agentcore 权限；SSE，取 type=health 那一帧）
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$RUNTIME_ARN" --qualifier DEFAULT \
  --runtime-session-id health-probe-000000000000000000000000000000000 \
  --content-type application/json --accept text/event-stream \
  --payload '{"op":"health"}' /dev/stdout

# ② 经中继看（不带认证，ALB 探活走的就是这条）
curl -s https://<relay-host>/health
# {"ok":true,"runtime":true,
#  "governance":{"wired":true,"role":"analytics-agent-ro","ageMs":41230}}
# ageMs 是这份缓存的年龄：读到 wired 的同时要知道它是多久前探到的。

# ③ 要完整 ARN（带账号 ID）必须带有效 JWT，走同一份缓存、不额外调上游。
#    JWT 也可以放 x-id-token 头。验不过时回 identity:null——明说"没给"，
#    而不是装作没这个字段（后者读起来像"云上没有这个信息"）。
curl -s -H "Authorization: Bearer $ID_TOKEN" 'https://<relay-host>/health?identity=1'
# governance 里多出 identity（完整 assumed-role ARN）、engine、workgroup
```

几处形状是刻意的，改之前先读这几行：

| 约束 | 为什么 | 代价（如果反过来做） |
|---|---|---|
| `{"op":"health"}` **不进模型**（`main.py::agent_invocation` 第一个分支就 return） | 这条路径会被 ALB 探活间接触发 | 每次探活走一次 Bedrock，就是一笔按分钟计的账单 |
| 中继 `/health` **绝不 await 上游**：回缓存 + 后台刷新，TTL 5 分钟 + 单飞去重 | ALB 探活必须永远快、永远 200 | 探活超时 → task 被摘出目标组；或者每次探活打一次 `InvokeAgentRuntime` |
| 探不到时 `wired: null`，不是 `false` | `null` = 不知道，`false` = 确认没接上 | 把「探不到」说成「没接上」，会让人去查一个不存在的治理故障 |
| 刷新失败**不影响** `/health` 的 200 | 那是探活端点，不是诊断端点 | 少一个诊断字段 → 整个 task 被摘掉 |
| 默认只回 `governance.role`（角色名）+ `wired`，不回 ARN | `/health` 不带认证，而完整 ARN 里有账号 ID | 账号 ID 挂在公网未认证端点上 |
| 固定 `runtimeSessionId`（`health-probe-…`） | `main.py::_get_client` 换 session 就 disconnect 重连暖客户端 | 探针把 Runtime 的暖客户端反复重建，每次 8–10s |

**这一节仍然是人工跑的。** `test_all.sh` 的 L4 只打本地进程、L6 只打本地 `uvicorn`，
两条都到不了云上那个容器；上面三条命令要一个已部署的 Runtime + 中继。它属于
「没有自动化覆盖」里「云上副本 `analyticsagent/` 的运行时行为」那一栏——加了这个字段之后
云上治理身份**看得见**了，但仍然没有任何一盏灯会因为它变红。

## L5 查询路径

```bash
python3 scripts/lakehouse/verify_mart_parity.py --numbers
DB_BACKEND=athena python3 eval/run_eval.py --dry-run
```

第一条把口径写成**恒等式**（多条路径的 GMV / 退款 / 行数互等），不写死任何数据相关的
数字——原来这里钉着字面量 `149685621.44`，重新生成一次数据测试就红，而代码没问题。
它同时钉住两处 v1 缺陷仍然存在（那是数据的性质，不是 bug，被"修掉"才要报警）。

第二条把 27 条金标 SQL 全部在 Athena 上跑一遍，**不调模型**：覆盖面等于全部金标，
顺带验证 `pg_to_trino` 的运行时改写。性价比最高的一步。断言正则锚在 `OK$` 上，
有失败时 `run_eval` 会在后面接「；失败: [id…]」，正则自然不匹配。

## L6 服务与前端渲染契约

`test_all.sh` 自己起后端（`uvicorn`，随机高端口）、轮询 `/health` 等就绪、跑完自动关。

| 检查 | 要挡的事 |
|---|---|
| `/health` 的 `engine` | 它曾无条件读 `db.PG`，切了后端之后顶栏还显示 `127.0.0.1:5433`。**坐标错了比不显示更糟**，因为它看起来是对的 |
| `/api/catalog` 的 `source` 是 `glue` | 降级到 `information_schema` 时要看得出来 |
| `/health` 的 `ok: true` | 前端存活探针曾被误改成 `GET /ask`（`/ask` 是 POST → 405 → 永久掉进 catch → 一直显示烘焙数据，而提示写的是"启动后端"，归因指错方向） |
| `render_test.mjs`（中英双路径） | UI 原来把表数/行数/引擎名写死在 HTML 里。它抓到过一个差 10 倍的 bug：行数按 `n/1e4` 算却配英文单位 `K`。**单位错误比数字缺失危险**，因为它看起来是个正常数字 |
| `/health` 答第一句话够快 | 上面那些 curl **不带超时**，而且跑之前已经轮询把后端等热了——所以浏览器那个超时预算此前零覆盖。这条量的是**首次响应耗时**（要求 ≤ 单发预算的一半）。注意它盯的不是"Athena 有多快"：`/health` 现在不等 ping 查完才回，冷启动实测 2.0s（此前 4.06s——`backend_info()` 冷启动 1.93s 竟在事件循环里同步跑，还和 ping 串行相加） |
| 预热耗时仍在前端等待窗口内 | 数据层预热完（`ok:true`）要多久由 Athena 决定，实测 5s ～ 13.5s。这条要求它 ≤ 前端愿意等 `warming` 的窗口的一半（窗口 = `PROBE_WARM_ATTEMPTS × PROBE_GAP_MS`，两个数都从 `web/index.html` grep 出来，不在测试里抄第二份） |
| `/health` 带 `dataLayer` 三态 | 少了这个字段，前端就分不出「还在预热」和「探不通」，于是又退回"拿一个写死的毫秒数裁决后端在不在"——那正是这次事故的成因 |
| shell 的资源逐个真取得到 | L0 那条只静态核对路径写法；这条**真发 HTTP**，因此还覆盖了挂载点本身和 `/config.js` 那条兜底路由。少了它，「本地图表画不出来」这件事在整套验收里零覆盖 |
| `render_test_prod.mjs` | 线上是另一套取数路径：CloudFront 只把 `/ask` 转给 relay，`GET /api/catalog` 落到 S3 拿 403，前端要退到同域快照 `./catalog.json`。这条路没法人工验（要浏览器 + Cognito），而它失败时页面**照常渲染**，只是数据停在旧的静态原文 |

缺 `node` 时后两条打印 note 而不是 FAIL。

### `POST /ask` 流式契约（`--ask`，默认不跑）

```bash
bash scripts/test_all.sh --ask                                    # 挂在 L6 末尾
./backend/.venv/bin/python scripts/ask_probe.py http://127.0.0.1:8000   # 单独打已起的后端
```

上面那张表里的每一条都只覆盖**页面加载**那一段。用户真正在用的 `POST /ask` 此前
**零覆盖**：`test_all.sh` 不碰它，`eval/run_eval.py` 直接 import `run_agent()`，不走
HTTP、不解析 SSE。于是夹在中间的东西——SSE 分帧、事件键名、会话接续——只能靠人开浏览器点。

| 检查 | 要挡的事 |
|---|---|
| 过期 `session_id` 不产生 `error` 事件 | `session_id` 由客户端给、原样进 CLI 的 `--resume`；会话被回收（标签页开久了）时 CLI 启动即退出，整轮死在一个 `error` 上，用户看到「问不出来了」，而真相只是上文没了。`agent.py::run_agent()` 现在丢掉上文重跑一轮 |
| 有 `stage/resume` 事件 | 降级要**看得见**。悄悄重开的话界面上看不出上文已丢，用户会以为它还记得前一轮 |
| 回传新的 UUID 会话 id | 否则前端一直拿着那个接不上的值，每轮都要降级一次 |
| `text` 事件的键是 `delta` | `web/index.html` 读 `ev.delta`。键名改了没有任何东西会报错，页面只是**不再显示答案** |
| 最后一个 `result` 带 KPI | 断言的是**最后那个**——agent 可以多次 `present_result`，前端按"覆盖 payload 再重渲染、最后一个生效"设计。写成「恰好一个」会把正常行为判成失败 |

`agent.py` 上一版**已经写着**「resume 失败则退回新会话」这句承诺，但 `try` 只包住了赋值
（赋值从不失败，真正的失败在 `ClaudeSDKClient` 拉起 CLI 那一刻），那个 `except` 一次都
没进过。所以这条不能靠读代码确认，必须真发一个坏 `session_id` 看结果。

默认关是因为它要烧一次 Opus（约 1 分钟）。不跑时套件末尾会明说「本次没跑」——
一份写着"全绿"的报告最容易被读成"全都验过了"。

## L7 端到端 agent（手动，烧 token）

```bash
./backend/.venv/bin/python eval/run_eval.py --case L1-users-count   # smoke
./backend/.venv/bin/python eval/run_eval.py                          # 全量 27 题，约 22 分钟（2026-08-28 实测 1340s）
```

⚠️ 全量跑会覆盖 `eval/report.md` 和 `report.json`；现行基线归档在
`eval/baseline/eval.lakehouse-athena.post-anchor-fix.md`+`.json`（**新一轮请用新文件名，别覆盖旧的**：
两轮之间的差别只有把两份放在一起才看得见）。

只改了文档、对账器、注释时不必跑全量——agent 路径没动，L5 的 27 条金标已经覆盖 SQL 层。
改了 `knowledge/`、`backend/agent.py`、`metrics_def.py` 才需要。

## L8 负测：证明检查器该报的时候真会报

```bash
python3 scripts/negative_tests.py --list        # 用例清单
python3 scripts/negative_tests.py --offline     # 只跑不连云的 27 个（秒级）
python3 scripts/negative_tests.py               # 全部 49 个
bash scripts/test_all.sh --l8                   # 挂在套件末尾跑
```

L0–L6 全绿只说明「现在没问题」，**不说明检查器还有效**。一个永远绿的检查器和没有
检查器等价，而且更糟——它让人以为这块有人看着。本项目在这上面栽过三次，三种形态：

| 形态 | 实例 | 会不会自己暴露 |
|---|---|---|
| 假阳性 | 卡片枚举行被当列名、`IS NOT NULL` 里的 `is` 被当列名 | 会（测试红了，有人来看） |
| 名字比覆盖面大 | `render.py --check` 只校验 manifest，却被标成「卡片是最新渲染」 | **不会** |
| 零覆盖 | `backend/db.py` 的只读闸没有任何断言 | **不会** |

后两条不是"检查器写错了"，是**没人验证过检查器**。这就是 L8 的职责。

L4 治理层是带着这三种形态一起来的，所以它的三个用例（`gov-*`）一一对应：策略被改松
（离线就该红）、探针其实没在探（`--as-caller` 换成 admin 身份跑，**全绿即零覆盖**）、
治理建好了但后端没接上（授权面全对，查数用的却是 admin 凭证）。

**每个用例做三件事**：先跑未注入的命令并要求 exit 0（否则"变红"可能与注入无关）→
注入缺陷，要求 exit 非 0 **且**输出匹配预期消息（换个原因红了不算过）→ 还原并用
sha256 核对。第一步是关键：少了它，一个本来就红的检查器会让所有负测"通过"，负测自己
变成那种最坏的测试。

安全性：只改仓库内文件，改前整份复制到临时目录，`finally` + 信号处理里还原。
**不用 `git checkout` 还原**——工作树里有大量未提交改动，checkout 会一起冲掉。
注入用唯一子串替换，锚点必须恰好出现一次，否则用例直接 ERROR（锚点漂移显式失败，
而不是静默改了别的地方）。还原一旦失败就停跑剩下的用例：脏工作树上的"绿"不可信。

### 49 个用例

| id | 守的检查器 | 注入的缺陷 |
|---|---|---|
| `readonly-guard-hole` | `db.py` 只读闸 | 从 `_FORBIDDEN` 拿掉 `drop` |
| `tool-gate-shadowed` | `agent.py --selftest`（工具白名单边界） | 把闸门从 `hooks={"PreToolUse": …}` 改回 `can_use_tool=_make_gate()`——**这就是它坏掉时的真实样子**：`bypassPermissions` 在咨询回调前就批准了每一次调用，回调一次都不触发，而代码读起来完全正常 |
| `tool-gate-toolsearch-locked` | `agent.py --selftest`（白名单里的 `ToolSearch`） | 从 `GATE_ALLOWED` 拿掉 `ToolSearch`，即"只放行那 5 个分析工具"这个看着更严的写法。MCP 工具是**延迟加载**的，拒了它模型连自己的工具都调不到——**agent 直接废掉**。这条失效平时只在 23 分钟的 L7 全量里露头，所以要在秒级这一层钉住 |
| `gov-policy-loosened` | `governance.py --selftest` | 把 `users.email` 从治理策略里拿掉（云上照发照过） |
| `gov-probe-blind` | `governance.py --verify` | 探针换成调用方（admin）身份跑：**必须全红**，全绿＝那批断言什么也没验 |
| `gov-backend-not-assuming` | `governance.py --verify-backend` | AssumeRole 照做但 session 没传给 Athena 客户端（`identity` 字段仍然对） |
| `iceberg-ddl-handedit` | `gen_ddl.py --check` | 手改生成的 Iceberg DDL 列名 |
| `ddl-enum-comment-drift` | `verify_ddl_comments.py` | 把 `products.status` 注释里的 `'on_sale'` 改回 `'active'`——**这就是它坏掉时的真实样子**：SQL 语法对、目录解析通过、返回空集，结论直接反过来（「一件在售商品都没有」），而 `verify_doc_sql` 的 EXPLAIN 不看 `WHERE` 里的字面量、`reconcile` 只比表和列 |
| `ddl-enum-prose-requoted` | `verify_ddl_comments.py`（去引号约定） | 给「业务上合法但本批数据没有」的值加回引号（`（业务上还有 draft` → `（业务上还有 'draft'`）。约定的全部意义在于**引号是「可以直接抄进 SQL」的标记**，加回去注释就重新变成一份能抄的假清单。这条是写文档时真踩的：我在 `products.status` 的说明里写了带引号的 `DEFAULT 'active'`，检查器当场判红 |
| `mart-predicate-dropped` | `verify_mart_parity.py` | 集市层 CTAS 少一个状态过滤（GMV 会多算取消单） |
| `manifest-artifact-handedit` | `render.py --check` | 手改生成的派生层卡片 |
| `cloud-copy-drift` | `sync_agent_code.py --check` | 手改云上那份生成的 `agent.py`，把时间锚点改回各表自己的 `max(dt)`（正是当年那次分叉） |
| `csv-header-renamed` | `load.py --preflight` | CSV 表头与 DDL 列名不一致 |
| `table-undocumented` | `reconcile` A 类 | 删掉一张表的卡片（agent 路由到不了它） |
| `card-ghost-column` | `reconcile` B 类 | 卡片写一个 Glue 里没有的列 |
| `mart-ddl-ghost-column` | `reconcile` C 类 | DDL 声明了但 Glue 里没有的列 |
| `metrics-ghost-identifier` | `reconcile` D 类 | 治理指标 SQL 引用不存在的标识符 |
| `bogus-catalog` | `reconcile` F 类 | catalog ID 写错（无表可比，最容易被读成"没问题"） |
| `ddl-type-drift` | `reconcile` G 类 | 真源列类型与 Glue 不一致 |
| `ddl-comment-drift` | `reconcile` H 类 | 真源改了列注释但没重灌 Glue |
| `card-missing-column` | `reconcile` I 类 | Glue 里有的列卡片没写 |
| `enum-value-absent` | `verify_enums` 方向一 | 卡片写了数据里不存在的枚举值 |
| `enum-value-undocumented` | `verify_enums` 方向二 | 数据里有、卡片没写 |
| `degenerate-col-unlisted` | `verify_constants` 方向一 | 从登记清单里摘掉一列。线上库里一列整个恒等于某个值时，`verify_load` 比的是 CSV ⟷ Athena（源头本来就是那个值，灌得完全忠实）、EXPLAIN 不看基数、没声明枚举所以 `verify_enums` 不看它、任何求和恒等式都满足——于是 `core_metrics.md` 的 `like_rate` 恒等于 0.00 且不报错 |
| `degenerate-col-stale-entry` | `verify_constants` 方向二 | 往清单里塞一条**已经不退化**的列。只守方向一的话那份清单会退化成只增不减的白名单：某列被修好之后条目永远留着，"已修"和"未修"在报告上长得一样 |
| `doc-sql-rotten` | `verify_doc_sql` | 卡片示例 SQL 引用不存在的列 |
| `csv-value-changed` | `verify_load` | 改一个 CSV 数值（行数对但求和不对） |
| `doc-row-total-drift` | `verify_load --selftest` | 改 `knowledge/connection.md` 里的全库规模声明（**agent 数不出这个数**，治理角色读不到 `user_messages`，只能照抄卡片）。注入打在**张数**上而不是行数上：行数随重灌变，锚点绑在某一批数据上会让负测自己先炸（重灌到 7994 万行那次就炸了），张数不变而报错路径相同 |
| `scale-prompt-hardcoded` | `verify_scale --selftest` | 把 prompt 里的「行数取决于湖里装的是哪一批」换回写死的「35 张明细表，约 19 万行」。**这就是本库真实发生过的那个状态**：五处文档声明 19 万行、湖里装着 7994 万行、L0–L6 全绿——数量级差 427 倍而没有一盏灯，因为整个对账层比表、比列、比枚举、比退化列、比装载忠实，**从不比规模** |
| `scale-decl-half-edited` | `verify_scale --selftest` | 把 `backend/run.sh` 里两处「约 19 万行」中的一处改掉。判据钉的是**出现次数**而不是「只准出现一处」：真要求去重，判据就变成在管别人的散文；钉次数则一改一漏立刻红（2 → 1），而正当的多处引用不受干扰 |
| `kb-golden-pertable-max` | `eval/run_eval.py --selftest` | 把 `L1-dau-latest` 的金标锚点从 `meta_snapshot` 换回 `max(event_time) FROM events`。**这是修之前 9 条金标的真实样子**：评测在奖励一个知识库明令禁止的写法。它比 `current_date` 那种错难发现得多——多数表的轴末恰好等于锚点，逐表 max 算出来和正确答案一样，只有踩到伸出业务日历的轴才分叉（`sessions.start_time` 越到 2026-01-25，`L3-churn-30d` 的窗口整体推后一天） |
| `scale-lake-unregistered-batch` | `verify_scale`（连云） | 改 `docs/scale.json` 里已登记批次的一张表行数，模拟"湖里换了一批数据而没人来登记"。`verify_load` 答不了这件事——它比的是「湖 ⟷ `data/csv`」并默认两侧本该相等，而这个账号本来就装着另一批 |
| `probe-warming-treated-as-dead` | `ui/boot_test.mjs`（场景⑥） | 把 `dataLayer=warming` 算进「失败」的额度——后端明说"我在，只是还在预热"，前端却当它不在。这是同一个缺陷的第二种走法：第一次是超时预算猜小了，这次是把"还在预热"读成了"后端不在" |
| `probe-degrade-is-permanent` | `ui/boot_test.mjs`（场景⑧） | 拿掉降级后的自愈重探——失败额度只有 3 发 ≈ 3s，比 uvicorn 打开端口还短，于是「重启后端 → 立刻刷新」把页面永久锁在离线演示模式。**同一个缺陷的第三种走法，也是最难归因的一种：它看起来像「你的修改没生效」** |
| `probe-baked-answer-while-backend-alive` | `ui/boot_test.mjs`（场景⑨） | 拿掉提问前的重探——降级过的页面在后端已经活着时照旧给烘焙答案（问退款给 DAU 走势），且不报错 |
| `shell-asset-absolute-path` | `ui/asset_check.py` | 把 shell 的资源引用改回绝对路径 `/vendor/...`——线上照旧对，本地静默 404：图表框空白、字体退回系统默认，其余照常渲染，**没有任何红字** |
| `funnel-shape-gate-gone` | `eval/run_eval.py --selftest` | 摘掉 `judge_funnel` 的形态闸——判分退回"只比数值"，于是 `394/385/396/373` 这种"结算 > 浏览"的非漏斗照旧判 PASS。这就是那次真实事故的本体：agent 据此答「数据几乎不衰减，是随机种子数据的问题」，正确口径其实是 `394/314/258/199`（逐层流失 20%/18%/23%），**结论整个反了而没有一处报错** |
| `funnel-golden-subset-gone` | `eval/run_eval.py --selftest` | 删掉 30 天那条金标里 s2 的 `IN (SELECT user_id FROM s1)`——口径当场退回独立计数，而 SQL 照样跑得通、照样出四个数。金标是这道题的真源，真源错了下游全错。这条同时钉住检查器必须**逐步**核：第一版只核"某处有就算过"，删掉一步照样全绿 |
| `kb-funnel-subset-gone` | `eval/run_eval.py --selftest` | 删掉 `knowledge/domains/behavior/events.md` 里漏斗参考 SQL 的一步子集约束。**这条守的是前两条守不住的那半边**：形态闸和金标口径全绿之后，agent 在浏览器上照旧答出 `394/385/396/373`——它读的是行为域卡片和 `core_metrics.md`，那两张卡片里的漏斗 SQL 本身就是每步各数一遍，还配着一句「这份种子数据漏斗不衰减」。agent 是照抄的。`verify_doc_sql.py` 只 EXPLAIN 语法不看口径，这两个文件此前零覆盖 |
| `kb-funnel-route-gone` | `eval/run_eval.py --selftest` | 摘掉 `domains/behavior/_index.md` 里指向 `analysis/funnel_analysis.md` 的两处指针（删一处剩一处照样绿，所以两处都摘）。事故的另一半是**路由**：方法卡的适用面写成「判断/诊断/深度分析题」，于是朴素取数题只加载表卡片，SOP 里的约束 A/B 一条都没生效。内容对不对是一回事，**送不送得到**是另一回事 |
| `kb-funnel-absolute-gone` | `eval/run_eval.py --selftest` | 摘掉 `analysis/funnel_analysis.md` 里「绝对值不可比」那条口径声明。守的是口径修对之后剩下的那一半：**数值全对、形态也全对**（394/314/258/199，逐层流失 20%/18%/23%，单调递减），结论仍然可以错——端到端转化 50.5%（近 30 天 16.2%）被答成「转化表现优异」，也就是把生成器的抽样方式报成了业务表现。根因在分母：漏斗顶端「看过就走」这一侧没按真实比例生成，浏览过商品的 394 人里一次都没加购的只有 80 人（20.3%），而 25 种事件各自的行数被抽得近似均匀（736–866，极差比 1.18）。跟 `kb-retention-verdict-gone` 同一个病、不同的器官：钉**结论层**，数值闸对这种错一律判 PASS |
| `kb-retention-cohort-column` | `eval/run_eval.py --selftest` | 把留存 cohort 的时间列从 `registered_at` 换成 `created_at`。**`users` 上两列都存在**，所以 EXPLAIN 通过、SQL 出结果、行数也正常，只是分出来的是另一批人（实测 500/500 行两列不相等）。这是「检查器验的是另一个维度」的第三种形态：`verify_doc_sql.py` 查语法，语法完全对，错的是语义。旧版本这段写的就是 `created_at`。断言必须**同时**禁掉错列、要求对列——只要求 `registered_at` 出现过太松：把 SELECT 的键换掉、`WHERE` 里留着，那条照样绿（写的时候真踩了） |
| `kb-retention-numerator-unbounded` | `eval/run_eval.py --selftest` | 把留存分子从 cohort 成员（`c.user_id`）换成全站活跃（`act.user_id`）。Rₜ 的分子是「**这个 cohort 里**第 t 期还活跃的人数」，换掉之后实测第 1 周 219 人 / cohort 43 人 = **509%**。这个缺陷至少会自己炸出来（>100% 一眼可见），比漏斗那次"悄悄给一个像样的数"友好，但只有装了闸才会在提交前炸 |
| `kb-retention-verdict-gone` | `eval/run_eval.py --selftest` | 摘掉 `analysis/retention_curve.md` 顶部那条结论判据（三处一起摘——那句话在这份文档里出现三次，只改一处剩下两处照样把它教会）。**这条钉的是结论层，不是数值层**：数字全对、右删失说明也完全正确，结论仍然是错的——曲线在 v1 种子样本上平坦（45.0/43.0/42.1/41.1/43.0），agent 答成「曲线平稳、留得住」，把项目自己的 P0 数据缺陷报成了正面业务发现，而那句右删失说明让整段话听起来很严谨。根因是这条事实此前只写在 `docs/data-audit.md`，`knowledge/` 里一个字都没有，而 agent 只读 `knowledge/`。文档现在写的是**判据**（W4/W1 ≥ 0.8 算不衰减）而不是结论，因为全量批次的曲线已经衰减了 |
| `retention-verdict-gate-gone` | `eval/run_eval.py --selftest` | 摘掉 `judge_retention` 里的结论闸（那句必须出现「算不出/不可信/独立抽样/假象」的白名单校验）。上一条钉的是知识卡片里那句话还在，**这条钉的是判分器会不会因此判错**——少了它，留存金标退回「数值对就 PASS」，而那份把 P0 数据缺陷答成「留得住」的答案数值恰恰全对，会安安静静进归档基线。闸必须是白名单而不是黑名单：**正确答案里就带着「别当成"留存好"的正面结论」**，任何按「留存好」拦的写法都会打到正确答案身上。第一版白名单里放了「别当」，结果被那份缺陷答案的右删失句「别当真实下跌」满足了——本该不算数的 caveat 成了放行凭证 |
| `retention-gate-hardcoded` | `eval/run_eval.py --selftest` | 把 `judge_retention` 的结论闸从「按曲线形状开合」改回**写死**（`decaying = False`）。上一条守的是"闸不许被摘掉"，这一条守的是相反的方向：**闸也不许硬编码一句关于数据的结论**。写死那一版在 v1 种子样本上是对的（曲线平坦，W4/W1≈0.99），数据换成全量重灌批次之后（2026-09-17 实测 71.6→37.2，W4/W1≈0.52）就开始**要求 agent 说一句假话**——这一批是算得出留存的。判据钉在数据的性质上而数据会换，就必然有这种反转；正确做法是现从金标 `pct` 那一份量比值，`≥ 0.8` 才开闸。`--selftest` 里两个分支各有固定夹具，所以不靠"湖里正好装着哪一批"来体检 |
| `lite-mode-analysis-banned` | `eval/run_eval.py --selftest` | 把 `LITE_SUFFIX`（常规题的系统提示后缀）里那句例外换个说法，让它不再与知识库对上。这是 `L5-retention-cohort` 那道红的另一半：域索引写的是「两个都要读，**哪怕只是取数**」，而提示后缀原文写着「**不要**读 analysis/ 方法库」——两句话直接对冲，**系统提示赢**，于是常规模式下那份文档从来没被打开过，留存题的分子约束（写错出 509%）和曲线可信度判据一起消失，而 eval 又按"该说的话没说"判它红。deep 模式只有 4 个预设按钮进得去，用户手打的问题一律走 lite，所以这不是评测保真度问题、是产品缺陷。判据拿同一句话当锚：知识库标了「哪怕只是取数」，提示词里就必须出现同一句、并且围绕 `analysis/` 说 |
| `shell-config-js-route-gone` | `ui/asset_check.py` | 摘掉 `server.py` 里 `/config.js` 那条兜底路由——唯一被允许的绝对路径例外变成没人管的 404，行为不变（照旧回退 `/api/config`），只剩会误导排查的日志噪音 |
| `probe-budget-too-tight` | `ui/boot_test.mjs`（boot() 行为契约） | 把前端存活探针的超时预算改回本地 Postgres 时代的 2500ms——湖仓冷启动第一发 `/health` 实测 5.0s，于是「起完后端第一次打开页面」**必然**落进离线演示模式（问退款答 DAU 走势，底部还提示"请启动后端"）。要 node，没装则**明说跳过** |

`reconcile.py` 的 A–I **八类 finding 全部有对应负测**（E 类是 Redshift 的 DDM 检查，
随 Redshift 一起退役了）。`card-ghost-column` 额外钉住假阳性回归：注入一处就只该报
一处，同一张卡片里的枚举行不该被当成列名。

reconcile 家族 8 个用例共用同一条基线命令，基线结果按命令缓存——否则那次约 40 秒的
云调用要白跑 7 遍。缓存成立的前提是每个用例退出时工作树已还原，所以还原失败即停跑。

---

## 没有自动化覆盖

这一节是这份文档最该被读的部分。上面每个 PASS 都有明确边界，下面这些**没有任何
检查器盯着**——不写出来，"全绿"会被读成"全都验过了"。

| 缺口 | 为什么没覆盖 / 怎么补 |
|---|---|
| **文档里写的规模 ⟷ 湖里实际的规模** | **2026-09-17 实测到的实例,不是假设**：`docs/deployment.md`、`docs/data-audit.md`、`docs/data-walkthrough.md`、`database/00_schema_overview.md`、`PROJECT_STATUS.md` 五处都写着「当前部署约 22 万行、数据源是仓库里的 `data/csv/`」——而湖早就被重灌成 **8000 万行**那一批（`orders` 854,140 而不是 2,000，差 427 倍）。这五句**写的时候都是对的，是湖在它们底下被换掉了**。没有任何一层会红：`reconcile.py` 比表和列、`verify_enums.py` 比枚举取值、`verify_load.py` 比 `data/csv/` ⟷ Athena，**没有一个比文档散文里的数字**——而 `verify_load.py` 更糟，它在一个装了 8000 万行的湖上跑仍然会因为「CSV 那 2,000 行都在」而全绿。**代价是可操作的**：照 `deployment.md` 原来那句「改完 `data/csv/` 后跑 `load.py`」执行，就是拿 2,000 单订单 `DELETE`+`INSERT` 覆盖掉 854,140 单，命令正常退出、`verify_load.py` 随后全绿。已改成把「仓库交付的那一份」和「某个账号的湖此刻装的那一份」分开写，并在两处灌数指令上加了先核查再灌的告警。**2026-09-17 补上了**：声明落在 `docs/scale.json`（每一批数据的每表行数 + 三组缩放类 + 8 条文档声明的原文与出现次数），判据是 `scripts/lakehouse/verify_scale.py`——`--selftest` 进 L0（seed 声明逐表等于 `data/csv` 现数、声明原文还在且次数没变、数值对得上它自称的批次），连云那半进 L3（现查 35 张表 `count(*)`，必须逐表命中**某一批**已登记规模；命中不了就印出最像哪一批、差在哪些表）。L8 三条证明它会红：`scale-prompt-hardcoded` / `scale-decl-half-edited` / `scale-lake-unregistered-batch`。**剩下的缺口**：① 登记的是 8 处原文，`data-audit.md` / `data-walkthrough.md` 里其余描述规模的散文（尤其 220,087 这种含派生层的合计）没进对账面；② 判据只答「湖里装的是已登记的哪一批」，不答「这一批该不该装在这个账号上」——那是部署侧的事；③ `fixed` 那 11 张维度表不随 scale 放大**是数据缺口不是判据缺口**，见下面 `budget.py` 那两栏 |
| **深度分析模式（`DEEP_SUFFIX`）在评测里零覆盖** | `eval/run_eval.py` 调的是 `run_agent(question)`，`deep` 用默认值 `False`，所以 **27 题全部走 `LITE_SUFFIX`**——提示词里那半篇「必须读 `analysis/` 选方法、必须用 `compute_stats` 算统计量、必须填 `method` 面板、必须给 2–4 条 findings」从来没被执行过一次。它只走浏览器上那 4 个「深度分析 · 资深分析师」预设按钮（`web/index.html` 的 `deep:true`），而那条路唯一的验收是人眼。2026-09-17 这一轮**没有**顺手给用例加 `deep` 字段：那会改掉 27 题里若干题实际收到的提示词，等于把归档基线的可比性一次性作废，而且要多烧一轮 19 分钟的 Bedrock 才知道新基线长什么样——该单独做，不该夹在一次修红里做。**当前的真实状态是：`LITE_SUFFIX` 有 27 题覆盖，`DEEP_SUFFIX` 有 0 题覆盖，`compute_stats` 的调用契约在端到端一侧完全没验过**（它自己的 `stats.py --selftest` 在 L0，但"模型会不会照规矩调它"没人查） |
| **治理的「值级掩码」** | LF 没这个原语，落成了列级排除（列不可见），不是"测试缺失"而是**能力下降**，写在 L4 那一节。想要真掩码得上 Glue Catalog View，那条路的代价也写在那儿 |
| **除 agent 外的其他访问者** | L4 只约束 `analytics-agent-ro` 这一个角色。谁还有这个账号的 LF/S3 权限（包括跑 `--apply` 的那个 admin），没有任何检查断言 |
| **CLI 是否真的执行 `PreToolUse` 的拒绝** | `backend/agent.py --selftest`（L0）验的是两件**静态**的事：两侧 options 确实把闸门装在 `hooks` 上（AST 核对），以及直接调那个 hook 函数时放行/拒绝的**返回形状**对。它证明不了 CLI 收到那个形状后真的会拦——那一层只在 2026-08-24 用活探针验过一次：可达工具从 25 个降到 6 个、直接要求执行 shell 被拒、诱导读 `.env.local` 的注入被拒。**SDK 换一次 hook 语义或拒绝形状的键名，自测照样全绿而闸门已经死了**——和 `can_use_tool` 被 `bypassPermissions` 架空是同一种失效。要补就得像 `--ask` 那样烧一次真调用，断言 init 消息里的可达工具集合 |
| **`DENIED_BUILTINS` 的完备性** | 它是**黑名单**，天生补不全：SDK 下一版加一个内置工具，在有人把它写进这张表之前就是可达的。真正的闸是 `hooks` 那道白名单（不在 `GATE_ALLOWED` 里一律拒），黑名单只负责把暴露面缩小、让模型看不见。所以这两层缺一不可——但"黑名单是否已覆盖当前 SDK 的全部内置工具"没有任何检查断言 |
| **云上副本 `analyticsagent/` 的运行时行为** | 共享代码已经改成生成物（L0 `sync_agent_code.py --check` + L8 `cloud-copy-drift`），所以「云上那份代码和 backend/ 不是同一份」这个缺口补上了。**没补的是「这份代码在 AgentCore 上真跑得对」这件事的自动化**：2026-08-20 已首次部署并按 `analyticsagent/README.md` 人工复验通过三条（退款全量 963,560.92、渠道 GMV 与本地逐字一致、CloudTrail 证实生效身份是治理角色）。但 Runtime 是另一条执行路径（暖客户端跨调用复用、知识树从 S3 同步、身份是 exec role 假借治理角色），**这三条至今只能人工跑**——`test_all.sh` 与 `eval/` 都只打本地 `backend/`。首次部署就撞上了一个只有云上才有的失效模式：exec role 晚于容器存在，那批容器带着空配置**永久**降级（module 级配置一个容器只跑一次），而调用方看到的是 `success: true`、73 秒、一个数都没有。已在 `runtime_config.py` 改成硬失败，但**那个硬失败本身也没有负测盯着**。`render_test_prod.mjs` 只验前端兜底渲染，不验云上真实链路 |
| **DDL 里 `DEFAULT` 的字面量** | `verify_ddl_comments.py` 只比行内注释里的枚举清单，**不看同一行的 `DEFAULT`**。刻意不加：正确判据是「`DEFAULT` ∈ 业务上合法的值」，而这一层能拿到的只有「∈ 本批数据里出现过的值」，两者不是一回事——`ad_campaigns.status DEFAULT 'draft'` 完全合法（默认值命名的是**初始态**，它在被观测到之前就已经流转掉了）。退一步的规则「∈ 卡片 ∪ 注释散文提到的值」也不成立：散文里为了讲清缺陷本来就会提到错值（我自己那句「旧注释的 draft/active/inactive/deleted 一个都不存在」就带着 `active` 这个词）。**判据推导不出来的检查不该写**——写了就是一盏语义不明的灯。已知的唯一一处实例是 `products.status DEFAULT 'active'`（那批错值的残留），已就地改成 `'on_sale'`；`gen_ddl.py` 生成 Iceberg DDL 时会把 `DEFAULT` 整个丢掉，只有 `setup_local.sh` 的 v1 Postgres 路径会执行它，而那条路是 CSV `COPY` 灌数、这一列总是被显式提供，**所以运行时零影响**。下次再出现只能靠人读 |
| **5 行不可比对的枚举注释** | `channels.platform`、`ad_creatives.creative_format`、`ab_tests.primary_metric`、`ab_test_variants.variant_key`、`payments.payment_channel` 的注释里有取值清单，但知识卡片里**没有对应的枚举表**，`verify_ddl_comments.py` 对它们只能报 SKIP。基准不存在时"一致"没有意义，所以不硬凑；要补就得先给卡片加枚举小节（那样 `verify_enums.py` 会顺带把它们纳入云上比对） |
| **"比的两端不独立"这类假绿灯** | 不是某一个检查器的缺口，是一种**形态**，本项目已发现**五处**：①`verify_enums.py`（卡片 ⟷ 线上库——库里装的是照卡片生成的数据）；②`reconcile.py` H 类（源注释 ⟷ Glue 注释——Glue 的注释就是建表时从这份源写进去的）；③此前压根不存在的「生成器 ⟷ 卡片」；④**新检查器天生就在这个陷阱里**：`verify_semantics.py` 判的商品是 `scripts/gen` 照 `semantics.yaml` 造的，判据也是 `semantics.yaml`——所以它对生成侧的那声"全绿"本身不含信息，真正携带信息的是它对**云上 v1 数据**跑出来的红（白名单 123/200、价格 154/200、名字模板 200/200）和 `--selftest` 里照抄任务书的反例。凡是"生成器 ⟷ 检查器"共用真源的检查，都必须有一组**不由这个生成器产出**的数据来证明它有区分力；⑤`product_tags` 的 `tag_type` 一列：生成器从 `tag_pools` 摊平时**顺带**记住每个名字来自哪个池，检查侧若也从同一份 `tag_pools` 反查，就是拿函数的输出去验函数——所以真正加了保护的是 `semantics.py` 的第 9 条校验（**名字跨池不重复**），它管的是配置层，不依赖任何一侧的实现；⑥`profiles.yaml` 的效应量 ⟷ 通过线：用生成器的倍率去推检查器的通过线就是同一个形态，所以这份配置把 `judge:` 和 `generate:` 分成两段、`load()` 返回三个独立数据类，**检查侧结构上拿不到 `Generate`**。两段的数字刻意不相等（gender 通过线 15% ⟷ 倍率 35%）：前者是"多小就不值得据此分层"，后者是"真实综合电商里常见的幅度"，不该是同一个数。共同点是**两端不是独立来源，而是同一份东西的两个副本**，于是"一致"不携带任何信息，比没有检查更坏——它会发出一盏假绿灯。这份文档里每加一条新检查，都该先问一句「这两端各自的真源是谁」 |
| **同一个枚举有三份副本，且没有任何一份被声明** | 查 D-01 时顺带发现：`user_profiles.occupation` 在 `scripts/generators/user_domain.py`（旧生成器，产出 `data/csv` → 云上）里是 **15 个值**，在 `scripts/gen/tables.py`（新生成器）里是 **12 个**，**交集只有 9 个**。DDL 只写 `VARCHAR(50)`、knowledge 卡片只写「职业」，两处都没有声明枚举取值，所以 `verify_enums.py`（卡片 ⟷ 库）和 L2 的枚举对账**都抓不到**——它们只能核对"被声明过"的列。这一列在本批之前是零覆盖。已把真源收敛到 `profiles.yaml` 的 `dimensions.occupation`，`tables.py` 从那里读、`verify_correlation.py` 也按同一份分档并把值域外的取值单独标出来。**教训是「没有声明的枚举列，对账层看不见」**：本库还有多少这样的列没数过 |
| **卡片教给 agent 的判定规则可以在数据上恒假，而没有任何一层会红** | 查行为大表分布时发现三处。`knowledge/domains/behavior/sessions.md:41` 的「is_bounce 跳出判定规则」表写着 `page_view_count = 1 → TRUE`，而现行库 103 个 `is_bounce=true` 的会话页面数是 2~10、**单页会话一个都没有**；`knowledge/domains/marketing/push_notifications.md` 的「状态判断逻辑」表写着「发送失败 = `failure_reason IS NOT NULL`」和「待发送 = `scheduled_at IS NOT NULL AND sent_at IS NULL`」，而这两列在现行库里**整列为空**——照卡片查，失败数恒为 0（实际 470 条）、待发送恒为空集。这类缺陷躲过了每一层：`verify_doc_sql.py` 只 EXPLAIN（语法/目录/列/类型全对）、`verify_enums.py` 只比枚举取值集合、`reconcile.py` 只比列名与类型，**没有任何一条检查「卡片声明的这条规则在真实数据上还选得出行」**。现在 `verify_behavior.py` 有三条判据钉住它，但那是逐条手写的，不是通用机制：卡片里还有多少条这样的规则没数过。通用解法要能从散文表格里提取可执行谓词，成本远高于本批。**2026-08-28 补了一半**：`verify_constants.py` 把「这一列整列 NULL」这个**前提**钉住了（`push_notifications.failure_reason` / `scheduled_at` 都在 `ALL_NULL_PINNED` 里，那两列被填上会红），于是"规则恒假"的成因至少不会再悄悄出现或悄悄消失。但**没被覆盖的仍是主体**：钉的是列的基数，不是「规则还选得出行」；`sessions.md:41` 那条 `page_view_count = 1` 恒假靠的是取值分布而不是整列 NULL，这一层完全够不着 |
| **五个报告器的「取数那一段」** | 它们的 `--selftest`（157 项）验的是**判据逻辑**：给定一份事实，判得对不对。验不到的是事实**怎么来的**——Trino 聚合写错、列名拼错、`LEFT JOIN` 手滑写成 `JOIN`、CSV 解析器读错一列、布尔 `t`/`true` 两种写法漏认一种。这一段目前唯一的印证是 `verify_behavior.py` 两条路径（Athena / CSV）27 条判据逐位相同——而**这正好是本表里「比的两端不独立」那个形态**：两个实现一致，不证明任何一个对（两边都把 `LEFT` 写成 `JOIN` 就会一致地错）。另外四个报告器连这层互印都没有：`verify_semantics` / `verify_resolution` / `verify_correlation` / `verify_literals` 的两条路共用的判据函数覆盖不到各自的取数码（`verify_literals` 的 `--from-csv` 是 2026-08-28 补的，**共用的是判据、不是取数**，所以补完仍在这一栏里；两条路还有一处已知的口径差：Trino 的 `regexp_like` 走 RE2J 认 `\p{Han}`，离线那条用 CJK 基本区 + 扩展 A + 兼容表意文字近似，覆盖面略窄，差异写在 `HAN_DOT_RE` 上方）。要补得有一份**小的、可控的、带已知缺陷的固定数据集**，两条路径都去读、都必须报出同一批红——那是一批独立的活，本阶段没做。L8 补不了这个：见 L2+ 那一节讲的结构性原因 |
| **报告器自己在目标规模下跑不跑得完** | 没有任何一条断言碰过五个报告器的**复杂度**。它们的 `--selftest` 用几十到几千行的夹具，量的是判得对不对，量不出「同样的代码喂 4,000 万行会怎样」。2026-08-28 栽过一次，值得逐字记下来：`verify_behavior.py --from-csv` 的取值域累加器写成 `col_acc[c] = (cnt + 1, seen | {val})`，而 `seen | {val}` **不是插入、是重建一个集合并把已攒下的元素全拷一遍**，单行 O(\|seen\|)、整循环 O(n·k)。四个被查的列里三个（`page_views.referrer` / `page_url`、`push_notifications.deep_link`）只有 9–26 个不同取值，**scale 1 几秒跑完、`test_all.sh` 一路绿**；现形的是 `push_notifications.scheduled_at`——它是时间戳，全量 427 万行里 302 万个不同值，累计元素拷贝 4.6×10¹² 次。实测增长指数 **2.25–2.56**（scale 4/12/40 → 7.03s / 83.22s / 1819.29s），全量外推 **≈57 小时 CPU**；真跑过的那次烧到 147 分钟 CPU 才被杀，进度约 4%。改成原地 `add` 后指数回到 0.94–1.04、scale 40 快 **201×**、**输出与旧实现逐字节相同**（85 行，拿被杀那轮之前存下的 scale-40 输出当 oracle 比过）。**这类缺陷按构造测不到**：小样本是判据迭代的正确工具，但它对复杂度天生盲。补法只有一条——判据定稿后**至少跑一次全量**，并把耗时记进本文档，下次谁改了取数码、耗时从两分钟变成两小时就会被看见。顺带一个诊断细节：作业当时输出 0 字节，那是 `print` 全在结尾 + 重定向下的块缓冲，**不是卡死**；判活要看 CPU 时间在不在涨，不要看有没有输出 |
| **数组 / JSONB 列的字面值形态** | `verify_literals.py` 的形态判据（占位符、词沙拉）只对标量文本有定义，8 个数组 / JSONB 列因此不在普查面里：`events.properties`、`orders.shipping_address`、`posts.media_urls`、`posts.product_ids`、`posts.tags`、`products.image_urls`、`user_attributions.tracking_params`、`user_profiles.interests`。**原来这个缺口是不可见的**：Athena 那条路从 `information_schema` 只捞 `varchar`，数组/JSONB 压根不出现在结果里，于是报告上「没被查过」和「查过没问题」长得一模一样。2026-08-28 把离线那条路的类型真源换成 DDL（`scripts/gen/ddl.py::parse_all_raw`）并**加了一行「普查之外」把这 8 列连同 5 个 `TYPE_EXEMPT` 列逐个点名**——缺口还在，但从此印在报告上，不再靠读代码才知道。真要补，得先定义数组列的形态判据（元素基数？每行元素数分布？JSON 键集合的稳定性？），那是另一批活 |
| **白名单只到「品牌 × 二级类目」的粒度** | `semantics.yaml` 的品牌经营范围声明在**二级类目**上（`家电>大家电`），叶子由它展开。所以「海尔卖冰箱」和「海尔卖电视」在这份配置里是同一件事，`verify_semantics.py` 也就分不出叶子级的不合理配对。收紧到叶子要手写 632 → 数千条声明，且大部分声明没有可复核的依据（「海尔到底做不做电视」这种事上，写细一格就从"可复核"退回"执行者的行业印象"，正好违反那条硬约束）。**当前粒度是刻意的**：拦得住任务书里那五类实例（跨大类的随机配对），拦不住同一大类内的细分错配 |
| **`get_schema` 这条死通道** | `backend/db.py:255 _describe_comments()` → `:294 _get_schema_athena()` → `:446 get_schema()` 会从 `DESCRIBE` 里抽 Glue 列注释拼进 schema 文本，带着很讲究的 docstring（解释为什么类型走 `information_schema` 而注释走 `DESCRIBE`），**但全仓库没有一个调用点**——工具层早已重构成 `read_doc` 读 `knowledge/`（`backend/README.md:49` 记着这件事）。它不是覆盖缺口，是**会误导判断的死代码**：读代码的人（包括本次审查）会以为 Glue 注释进得了 agent 上下文，从而误判「改 Glue 元数据要重跑 L7」。同一份死代码在 `analyticsagent/app/analytics/db.py:259` 还有一份同步副本。另外 `db.py:62` 那句「注释是灌数时从 `schema_manifest.yaml` 落进 Glue 的」也不准：manifest 里没有这些枚举文本，它们是建表时从 `database/iceberg/01_tables.sql` 的 `COMMENT` 进去的 |
| **`web/catalog.json` 的新鲜度** | 它是部署期快照，没有任何检查断言它与当前 Glue 一致。v2 时代它就在替不存在的治理层背书。改了数据/表结构后手动重跑 `scripts/deploy/build_catalog_json.py` |
| **Cognito 认证路径** | 要浏览器 + SRP 登录，无法自动化。只能人工过一遍 |
| **知识卡片的散文正文** | `verify_doc_sql` 只验 SQL 能跑、`verify_enums` 只验枚举值。`use_when` / `avoid_when` / `caveats` 里一句写错的口径说明，几乎没有检查器能抓——而 agent 就是照它选表的。**唯一的例外是被事故钉过的那几句**：selftest 第 7/8/9 组按关键字核了漏斗的方法卡路由、漏斗的「绝对值不可比」这条结论约束、留存的「这份数据算不出留存」这条结论约束，以及判分器认不认后者（`kb-funnel-route-gone` / `kb-funnel-absolute-gone` / `kb-retention-verdict-gone` / `retention-verdict-gate-gone`）。那是**逐字符串**的白名单，不是"能读懂散文"——换个说法写同一句错话，它照样绿 |
| **数据本身的业务自洽性** | `verify_load` 只证明 CSV → Athena 无损，不证明 CSV 里的数据在业务上讲得通。`docs/data-audit.md` 那些不变量（漏斗单调、留存衰减、计数器一致）在当前种子数据上**多数不成立**，那份文档带横幅。**部分补上了**：上面「L2+ 数据真实性诊断」那五个报告器覆盖了字面值多样性、商品域的单行语义、维度分辨率、画像 ⟷ 消费的相关性、四张行为大表的分布形状——但它们**一律不是闸门**（对云上必红，理由写在那一节）。剩下的真缺口是**跨表的数值口径自洽**：`order_items.unit_price` ⟷ `products.price`（同一件商品在明细里的单价与商品表的标价无关——**这一条还没修**。2026-08-28 在**新生成器的全量产出**上复测过一次，缺陷仍在且性质没变：`/tmp/genFULL_csv`，180.4 万条明细逐行 join 4,133 个 SKU，`unit_price` 均值 **92.10** ⟷ 同 SKU 的 `products.price` 均值 **1216.76**（13.2×），**逐行相关 −0.0010**，两者相等（差 < 1 分）的行只占 **0.0009%**。注意这不是"整体贵了 13 倍"这种可以一个系数改掉的偏移：相关系数是 0，明细单价与它自己那件商品的标价**互相独立**，所以「按标价折扣率」「客单价 ⟷ 商品定价档」这类问题在这份数据上答出来的都是噪声。修它要让 `build_order_items` 从 `products.price` 起价再乘折扣，而那会同时移动 `orders` 的四个金额列和 `products` 五个计数器的回填口径——属于下一批）。**已修**：`products` 的五个计数器 ⟷ `order_items` 的实际售出，原来 `sold_count` 是独立抽样的，目标规模下 Σ 比窗内真实件数高 26.9×、154 个 SKU（3.7%）连方向都相反（最糟的一行 `sold_count=1` 而窗内真卖了 163 件）、两条「爆款榜」的秩相关只有 +0.02（Top20 重合 0/20）；现在从明细回填（`_prep_product_counters`），实测比值 1.61×、硬矛盾 0、秩相关 +0.97、Top20 重合 17/20，`selftest_closures.check_product_counters` 的 10 条断言盯着它。`posts.like_count` / `sessions.page_view_count` 这类声明计数列与明细表的对账在生成侧有 `selftest_closures.py` 盯着，**云上那份没有**。`verify_behavior.py` 刻意不碰这一类，理由是它属于另一层（跨表数值闭环），造第二份实现只会两处各自漂。**刻意不修的一条**：漏斗转化率的绝对值。事件按种类近似均匀独立抽出（25 种各 736–866 行，极差比 1.18），漏斗顶端「看过就走」那一侧没按真实比例生成——浏览过商品的 394 人里一次都没加购的只有 80 人（20.3%），于是端到端转化全量 50.5%、近 30 天 16.2%，量级上不是业务水平。要修就得重造事件流并抬高行数预算；本项目的用途是演示 progressive disclosure 与 agent 写 SQL 的准确性，**形态**（394/314/258/199，逐层流失 20%/18%/23%，单调递减）已经够用，所以改成在语义卡片里加注：`analysis/funnel_analysis.md` 的「口径声明」+ `metrics/core_metrics.md` 的购买转化漏斗块，由 `run_eval.py --selftest` 第 7d 组逐字盯着、L8 `kb-funnel-absolute-gone` 证明那盏灯会红。注意这条只针对绝对值——**单调性不成立永远是 SQL 的错**，两张卡片都写明了不许拿它甩锅 |
| **「`budget.py` 声明的规模 ⟷ 生成器实际产出」没有任何断言** | `budget.BASE` 是唯一声明每张表规模与缩放类的地方，但没有一条检查把它和 `main.py` 真正写出来的行数对起来。这不是假设，是已经栽过一次又剩了四次的实例：`products` 声明 SUB（4,133）而整类没有 builder、被 `data/csv` 的 200 行盖掉，差 20 倍全绿；修完之后 `ad_campaigns` / `ad_creatives` / `campaigns` / `coupons` 四张仍是同一状态（声明 1,033 / 2,976 / 1,033 / 3,100，实际透传 v1 的 50 / 144 / 50 / 150）。**这一栏的危险性全在重灌那一刻**：现在库里是 v1 规模，声明与产出的差距看不出后果；一灌全量，转化侧 ×427 而广告/券/活动侧不动，`user_coupons` 会变成 970 万行核销只指向 150 张券（每张券发出约 6.5 万次，v1 是 151 次）、`user_attributions` 14.9 万条归因只指向 50 个活动。判据是现成的、也很便宜——`main.py` 写完每张表后拿 `budget.table_rows(scale)` 对一遍行数，声明与产出不等就炸；透传那 12 张要么改成 FIXED（承认它们不缩放），要么给它们写 builder。**刻意先记下来不动**：改 `budget.BASE` 的类别会移动全量产出的每一个数，属于下一批 |
| **计数器与「本该解释它」的那一列零相关（`verify_correlation.py` 只盖了画像 ⟷ 消费这一对）** | D-01 补的 `verify_correlation.py` 建立了「两列之间该有效应量」这类判据，但它的判据面只有**画像属性 ⟷ 消费金额**。同一形态在别处仍然零覆盖，2026-08-28 在全量产出（`/tmp/genFULL_csv`）上量到两处，都记在这里而**没修**：①**帖子的传播量与它的曝光时长无关**——36.16 万条已发布帖子，曝光天数 0–91 天，`corr(曝光天数, like_count) = +0.0035`，`view_count +0.0034`、`share_count +0.0016`、`comment_count −0.0009`；按曝光时长四分档看平均赞数是 41.4 / 42.0 / 42.6 / 42.5，**一根平线**。真实社区里累计计数是随暴露时间单调累积的，所以"昨天发的"和"三个月前发的"在这份数据里点赞数同分布；②**`product_tags` 的标签与商品的销售表现无关**——4,133 个 SKU、按 `tag_name` 分组看「该组平均 `sold_count` / 全体平均」，全部落在 **0.88×–1.11×**（每组 n≈390，这个跨度就是噪声）；反方向也一样，销量前 10% 的 SKU 平均带 **0.650** 个 `promotion` 标签、后 10% 带 **0.645**（1.01×），`corr(sold_count, promotion 标签数) = −0.0080`、`corr(sold_count, 标签总数) = −0.0117`。也就是说「限时特惠/满减 到底带不带得动销量」这个问题在这份数据上恒为"没区别"。两处的共同点和 `unit_price` 那条一样：**每一列单看都合法、每一张表单看都自洽，坏的是两列之间该有的那个方向**，而现有的每一层——闭环自测（查的是恒等式）、`verify_behavior`（查的是单列分布形状）、`verify_correlation`（只有画像 ⟷ 消费）——都不查它。要补得把「A 与 B 之间该有 ρ ≥ x」抽成一张可声明的表（像 `profiles.yaml` 的 `judge:` 那样），而不是再逐条手写判据 |
| **agent 的判断质量** | L7 的 27 题只覆盖有金标的数值题。洞察/归因的措辞、分层选择（何时该走治理层）靠人读 `report.md` |
| **扫描字节 / 成本** | `run_query` 返回 `bytes_scanned`，**唯一的消费者是模型自己**：`run_sql` 的工具描述说明了这个字段是成本读数，系统提示词的「查询成本」一节要求它据此收窄窗口和列。那是软约束，不是断言——没有任何阈值检查，UI 也不显示它，所以一道题从 0 字节变成扫全表不会有人发现。真正的硬护栏只有工作组的 `BytesScannedCutoffPerQuery`（`scripts/lakehouse/setup.py`），它拦的是单条查询的上限，不是"这道题比上周贵了 10 倍" |
| **前端探针预算的真实上界** | L6 那条断言量的是**本机**冷启动延迟（≥ 2× 才算过），它证明不了别人机器/别的网络上不会更慢。`boot_test.mjs` 也只验行为（慢探针要能上线、探不通才降级），不验"多慢算慢"。真正的兜底不再是那个数字，而是**降级可逆**：重试 3 发 + 降级后退避重探 + 标签页可见时重探 + 提问前重探，外加降级时页面明确带上声明。所以「这台机器/这条网络就是更慢」最坏只是**晚几秒**上线，而不是这一页永久离线。剩下的真缺口是「网络烂到 `/health` 长期不通」——那时页面只能、也应该显示离线演示模式 |
| **`web/index.html` 里展示用的 SQL 字符串** | 那是给观众看的文本，不进数据库，方言错了没有任何东西会红。人工核对 |
| **v1 本地 Postgres 分支** | `db.py` 的 postgres 路径、`docker-compose.cloud.yml`、顶层 `database/*.sql` **刻意不覆盖**，边界见 [legacy.md](legacy.md) |
| **`scripts/gen/` 生成器** | 缺 numpy 时 L0 自动跳过；且 8000 万行 → Parquet → COPY 那条路没接到 Iceberg，装载的是仓库里的 CSV 种子数据 |
| **L8 自己的覆盖面** | 49 个用例覆盖 **18 个检查器脚本 / 22 个可执行入口**（同一脚本的不同 flag 各算一个入口，例如 `governance.py` 的 `--selftest` / `--verify` / `--verify-backend`；这两个数是从 `guards=` 现数的，此前这一栏写的「27 个检查器」对不上任何一种数法），绝大多数是**一种**缺陷形态（`asset_check.py` 有两个，`backend/agent.py --selftest` 有两个（闸门退回 `can_use_tool`、白名单里丢掉 `ToolSearch`），`verify_constants.py` 有两个（清单外新增退化列、清单条目已过期——**必须成对**，只守前者的话那份清单会退化成只增不减的白名单），`verify_scale.py --selftest` 有两个（prompt 写死某一批的行数、同一个数抄两处只改了一处），`boot_test.mjs` 有四个：超时预算被改小、`warming` 被当成"后端不在"、降级不可逆、后端活着却给烘焙答案——同一处代码栽过三次；`run_eval.py --selftest` 有**十二个**，是全场最密的：判分器的形态闸、金标的口径、`knowledge/` 参考 SQL 的口径、漏斗题到方法卡的路由、漏斗「绝对值不可比」这条结论约束、金标的时间锚点不许写成逐表 `max()`，加上留存的五条——cohort 时间列、分子的 cohort 限定、「曲线不衰减时算不出留存」这条**结论级**判据写在卡片里、判分器真的会因为它判错、以及**结论闸得跟着曲线形状开合**（写死那一版在数据换成会衰减的批次之后开始要求 agent 说假话），再加**常规模式的文档路由**（`LITE_SUFFIX` 一刀切禁读 `analysis/` ⟷ 域索引写着"哪怕只是取数也要读"，两句话对冲、系统提示赢，那份口径硬约束就永远读不到）。它密不是因为它重要，是因为同一个错误在这里住过七个地方，而前两个闸全绿时 agent 在浏览器上照旧答错：它读的是知识卡片，不是金标）。它证明"该报时会报"，不证明检查器想得全。想得不全的部分就在这张表里 |

## 改动 → 测试步覆盖矩阵

| 改动 | 被哪一步覆盖 |
|---|---|
| `scripts/gen/pg_to_trino.py` | L0 自测 + L5 金标 dry-run |
| `scripts/gen/fillers.py` / `tables.py` / `main.py` | L0 三个生成器自测（**要装 numpy，否则静默跳过**）：fillers、跨表闭环正例（106 条）、跨表闭环反例（44 个注入）。商品域（`_prep_products` / `_prep_product_tags`）另有 L2+ 的 `verify_semantics.py --from-csv` 和 `verify_resolution.py --from-csv`，但那两条**要手跑**、且要跑一次**全量**产出（分辨率类判据的通过线按全量推导，scale=1 的小样本必红） |
| `scripts/gen/selftest_closures.py` **本身**（加断言 / 改判据 / 改 `NEG_CASES`） | **两侧都得跑**：`selftest_closures.py`（正例 106 条）和 `selftest_closures.py --negative`（44 个注入）。加了新断言就该同时加一个反例——不加不会有任何东西变红，而"106 → 107 条全绿"读起来和"多验了一件事"一模一样。改已有断言的消息文本会让对应反例的**期望片段**失配（报 `WRONG` 并打出实际消息），那是刻意的：片段就是把「这条判据在测哪件事」钉住的锚。注入函数只许读写 `t` 和深拷的 `cache`——`run_all_checks` 的入参边界是这一侧能存在的前提，往里塞对真 `ctx` 的写会污染后面每一个反例 |
| `scripts/gen/semantics.yaml` | L0 `semantics.py --selftest`（配置自相矛盾）+ L2+ `verify_semantics.py --selftest`（判据有没有区分力）。**改价格带或白名单必须两条都跑**：它同时是生成器和检查器的真源，改错了两侧会一起用错的那份、一起变绿 |
| `scripts/gen/profiles.yaml`（画像 ⟷ 消费的效应量与判据） | L0 `profiles.py --selftest`（21 项，13 项是反例）+ L0 `verify_correlation.py --selftest`（30 项，含审计实测那四个数必须判 FAIL）+ L0 跨表闭环里的 D-01 五条断言。**这份配置刻意分成三段**：`dimensions` 两侧共用、`judge` 只有检查侧读、`generate` 只有生成侧读，`load()` 返回三个独立数据类让这条边界结构化而不是靠自觉。唯一的例外是加载时断言「倍率跨度 ≥ 通过线 × 1.6」——它拦的是"生成出来的数据先天过不了自己的检查"，不是用倍率去推通过线。注意这条断言**对"有人把通过线调松"无能为力**，那件事只能靠 code review 和每条判据的 `why` 字段 |
| `scripts/gen/tables.py` 的四个行为大表 builder（`build_post_likes` / `build_page_views` / `build_user_follows` / `build_push_notifications`）及它们依赖的 `fillers.py::children_per_parent` / `unique_pairs` / `from_pool` | L0 跨表闭环自测只查**计数对账**（`like_count` / `page_view_count` ⟷ 明细行数）。**形状**要手跑 L2+ `verify_behavior.py --from-csv <产出目录>`，**迭代 scale 1 就够**（1s 造数 + 1s 判读，分布形状不像分辨率类判据那样依赖全量）。改这四个 builder 必须跑：`--selftest` 只验判据有没有区分力，不会因为生成器变差而红。已知实测基线：scale 1 → **14 FAIL / 27**，逐项写在 `verify_behavior.py` 的 docstring 里，改完对着比；全量（scale 427，四表 40,581,089 行）→ **27 / 27 全过**，耗时 **119.39s**。**改到取数那一段（`_rows` 循环、累加器）就不能只跑 scale 1**：那里有过一处只在千万行量级现形的二次复杂度，见「没有自动化覆盖」那一栏。耗时记在本文档就是这条的兜底 |
| `scripts/gen/budget.py`（表规模与缩放类） | L0 跨表闭环自测（scale=1）+ L2+ `verify_resolution.py`。**这里有过一处零覆盖的实例**：`products` 被声明成 SUB 缩放（scale=427 → 4,133 行）而 `tables.py` 里整类没有 builder，`main.py` / `dims_to_parquet.py` 反而各自去读 `data/csv` 那 200 行 v1 商品——「声明的规模 ⟷ 实际产出」当时没有任何断言，差 20 倍且全绿。**2026-08-28 数了一遍，同一处缺陷还剩 4 张表没修**：`data/csv` 的 35 张表里有 12 张不由生成器产出（`dims_to_parquet.py` 的 `DIMS`，逐字节透传 v1 值），其中 8 张在 `budget.py` 里声明为 FIXED——透传与声明一致，没问题；另外 **4 张声明为 SUB 却没有任何代码兑现**：`ad_campaigns` 50 → 声明 1,033、`ad_creatives` 144 → 2,976、`campaigns` 50 → 1,033、`coupons` 150 → 3,100（scale 427.039 下）。实测印证：全量产出目录 `/tmp/genFULL_csv` 只有 **23 个 CSV**，这 12 张一个都不在里面。和 `products` 那次的区别只是**它当时被撞见了**——判据仍然缺席，所以这 4 张的 20.7× 差距至今没有任何一盏灯 |
| `scripts/lakehouse/verify_semantics.py` / `verify_resolution.py` / `verify_literals.py` / `verify_correlation.py` / `verify_behavior.py` | 各自的 `--selftest`（正反两组构造数据，合计 157 项）。**五个现在都接进了 L0**（无 numpy / 无云依赖，秒级；前三个是 2026-08-27 补挂的，理由写在 L2+ 那一节）。**都没有 L8 负测，而且不该有**：L8 要求「注入前是绿的」，这五个的正向那一支对现行库恒红，"从绿变红"这个信号产生不出来；它们还一律 `return 0`，退出码也不承载判定。红绿双侧夹具承担的就是 L8 那份保障。真正没覆盖的是**取数那一段**和**它们自己的复杂度**，各单列在上面「没有自动化覆盖」里。`verify_literals.py` 另有三个两条路共用的判据（`merge_col_verdicts` / `judge_posts` / `judge_device`）：**改它们等于同时改云上和离线两条路的结论**，这正是抽出来的目的——但也意味着改一处要两条路都跑过才算验完 |
| `scripts/lakehouse/verify_behavior.py` 的 27 条判据本身 | L0 `verify_behavior.py --selftest`（79 项）。它是这批判据的**唯一**闸门，因为正向那一支对现行库必然 21/27 红，接进 `test_all.sh` 就是一盏永远红的灯。自测分六组，其中三组是别处没有的：①**每条判据 × 红绿两侧**（27×2 项）——同一条判据，全均匀夹具必须判 FAIL、强重尾夹具必须判 PASS，只验一侧证明不了判据有方向；②**集中度统计量 ⟷ 朴素实现逐位相等**（6 个随机直方图 × 9 个统计量）——直方图口径的 Gini/top-k 是整数算术推的，写错了报告照样打印得很像样；③**随机基线公式 ⟷ 四个审计实测点**，公式若错则 8 条集中度判据的通过线一起错。另外三组是 WEAK/FAIL/NOINPUT 三者分得开、通过线是闭的（`≥` 算过，且边界两侧的 μ 都在功效闸以上）、以及声明一致性（`CHECKS` ⟷ `TH` ⟷ `DEGENERATE_COLS` 三者互相不许有孤儿项） |
| `scripts/gen/tables.py` 里 D-01 那条边（`_prep_user_profiles` → `_prep_orders` 的顺序、`mult[uid-1]` 的对齐、归一） | L0 跨表闭环的 D-01 五条断言（**scale 1 就能红**）。这里有一处刻意的分工：`verify_correlation.py` 在**产出**上量极差，看得见效应大小，看不见效应是怎么接上的。实测把对齐改成 `mult[uid]`（每个用户拿隔壁的倍率）：闭环断言在 scale 1 上报 0.49×（阈值 1.48×，差 3 倍）；而 L8 在 scale 1 上，**修好的和错位的生成器四条极差判据都是 WEAK**，只有方向约束偶然抓到——L8 要给出确定结论得跑到 scale 500（造数 51s）。所以闭环那条断言不是重复 L8，是把错位变成**迭代规模上**的确定性红灯 |
| `scripts/lakehouse/gen_ddl.py` | L0 `--selftest` + `--check` + L8 `iceberg-ddl-handedit` |
| `database/0[1-8]_*.sql`（v1 真源 DDL） | L2 reconcile（声明态从这里来）+ L8 `ddl-type-drift` / `ddl-comment-drift`；**行内注释里的枚举清单**另有 L0 `verify_ddl_comments.py` + L8 `ddl-enum-comment-drift` / `ddl-enum-prose-requoted`。同一行的 `DEFAULT` **无覆盖**，见上一节 |
| `scripts/lakehouse/verify_ddl_comments.py` | L0 `--selftest`（14 条断言喂真项目取值，含一条「基准不能是空集」的防线）+ L8 两个 `ddl-enum-*` |
| `database/iceberg/01_tables.sql` | 它是生成物：L0 `gen_ddl --check` |
| `database/iceberg/02_mart.sql` | L0 谓词对账 + L5 恒等式 + L8 `mart-predicate-dropped` / `mart-ddl-ghost-column` |
| `schema_manifest.yaml` | L0 `render.py --check`（改完要重跑无参 `render.py` 生成 15 个产物） |
| `knowledge/domains/**` 手写卡片 | L2 四条路径 + L8 四个卡片类用例；**散文正文无覆盖**——而"散文正文"比听起来大：卡片里写在表结构描述那一栏的字面量（`user_profiles.md:13` 的默认值 `'China'`）就住在这里，见「隐形声明」那一节 |
| `knowledge/**` 的生成卡片 | L0 `render.py --check`（别手改） |
| `data/csv/**` | L0 `--preflight` + L3 **全量**（`--full`）+ L8 `csv-header-renamed` / `csv-value-changed` |
| `scripts/lakehouse/load.py` | L3 |
| `scripts/lakehouse/setup.py` | L0 `--selftest` + L1 `--verify` |
| `scripts/lakehouse/reconcile.py` | L0 自测 + L2 + **L8 八类负测** |
| `scripts/lakehouse/verify_enums.py` / `verify_doc_sql.py` | L0 自测 + L2 + L8 三个用例。`verify_enums.py` 的 `actual_values()` **改了取数那一段就要在真库上跑一次**：数组列走 `UNNEST` 那条分支的两处口径差（`n` 的含义、`<NULL>` 的含义）在构造夹具上看不出来，`--selftest` 只验解析器 |
| `scripts/lakehouse/verify_constants.py`（判据 / 三份登记清单） | L0 `--selftest`（13 项分类器断言，含**两个方向的清单过期**）+ L2 连云普查 + L8 `degenerate-col-unlisted` / `degenerate-col-stale-entry`。**改清单必须两个方向都想过**：往里加一条很容易（变绿），而漏了"条目过期也要红"那一侧，清单就退化成只增不减的白名单。`DIMS_PASSTHROUGH` 的表名从 `dims_to_parquet.DIMS` **现读**——那边加一张透传表，这边会跟着红，这是刻意的 |
| `backend/db.py`（`validate` / `_FORBIDDEN`） | L0 只读边界自测 + L8 `readonly-guard-hole` |
| `backend/db.py`（`backend_info` / `system_query`） | L6 `/health` 断言 + L5 dry-run |
| `backend/db.py`（`AGENT_ROLE_ARN` / AssumeRole 那段） | L4 `--verify-backend` + L8 `gov-backend-not-assuming` |
| `scripts/lakehouse/governance.py`（策略/契约常量） | L4 `--selftest` + `--verify` + L8 三个 `gov-*` |
| `scripts/lakehouse/athena.py`（`assume_role_session`） | L4 `--verify` / `--verify-backend`（其余路径 L2/L3/L5 全程在用） |
| `backend/metrics_def.py` / `metric_layer.py` | L2 reconcile D 类 + L8 `metrics-ghost-identifier` + **L7（口径对不对要看答案）** |
| `backend/catalog.py` | L6 `/api/catalog` 断言 |
| `backend/server.py`（启动预热 / `/health` 三态与 ping 缓存） | L6 三条 `/health` 断言 + 首发延迟测量（预热把 5.0s 那一发挪到启动、缓存让探针重试共用一发、TTL 过期时立刻回上一发结果而不白等 2s）。**缓存 TTL 与预热的存在本身没有独立断言**：删掉预热只会让 L6 那两个数字变大，不会指名道姓 |
| `backend/agent.py`（系统提示、分层策略） | **只有 L7**。改了它必须跑全量 27 题 |
| `backend/agent.py`（`ALLOWED` / `GATE_ALLOWED` / `DENIED_BUILTINS` / `_make_gate` 的接法） | L0 `agent.py --selftest` + L8 `tool-gate-shadowed` / `tool-gate-toolsearch-locked`。**这一面此前零覆盖**，代价是闸门死了一整段时间没人知道：`can_use_tool` 被 `permission_mode="bypassPermissions"` 架空、一次都没触发过，实测可达工具 25 个（含 Bash/Read/Write），让它 `echo` 一句 Bash 真的执行了；把闸门整个删掉当时也不会让任何东西变红。自测查的是**两侧**的选项构造（云上是 `dict(...)` 再 `**kwargs`，所以按「带 `permission_mode` 的调用」找，不按函数名找） |
| `backend/stats.py`（`funnel` 的单调性闸）+ `agent.py` 的 `_stats_summary` | L0 `run_eval.py --selftest`（`monotonic` + `violations`）。`_stats_summary` **那段措辞本身无断言**：摘要是模型唯一看得见的东西，写没写清"回去改 SQL"只有 L7 才看得出 |
| `backend/stats.py` 的其余方法（`describe` / `zscore` / `ratio_decompose` / `pareto` / `retention`） | `python3 backend/stats.py` 的 `_selftest()` 只**打印**结果、不断言，**且没进 L0**。改了它们没有任何东西会红 |
| `knowledge/analysis/**`（方法 SOP） | L2 `verify_doc_sql.py`（```sql 围栏逐条 EXPLAIN）验**能不能跑**，不验**口径对不对**；`funnel_analysis.md` 的口径另有 L0 + L8 两个 `funnel-*` 盯着。其余方法卡片（含 `retention_curve.md` 分子未显式限定在 cohort 内这类同类隐患）**只有人工评审** |
| `web/index.html`（动态元数据渲染） | L6 `render_test.mjs` 中英双路径 |
| `web/index.html`（`boot()` / 存活探针与降级自愈） | L0 `ui/boot_test.mjs` 十个场景（慢探针要上线、`warming` 不算死、探不通才降级、有限放弃、提问等探针、**降级后要自愈**、**后端活着不许给烘焙答案**、`file://` 下的提示要指对方向）+ L6「首发响应 ≤ ½ 单发预算」「预热耗时 ≤ ½ warming 窗口」「`dataLayer` 三态在位」+ L8 四个 `probe-*` 用例 |
| `web/index.html`（静态资源引用） | L0 `ui/asset_check.py`（相对路径 + 文件在位）+ L6 同一脚本的 HTTP 模式（逐个真取，含 `/config.js` 兜底路由）+ L8 两个 `shell-*` 用例 |
| `web/index.html`（展示用 SQL 字符串） | **无覆盖**，人工核对 |
| `web/catalog.json` | L6 `render_test_prod.mjs` 验渲染，**不验新鲜度** |
| `eval/run_eval.py`（`_adapt_sql`、金标 SQL） | L5 dry-run |
| `eval/run_eval.py`（`judge_funnel` 的形态闸）+ `eval/cases.json` 的 `L4-funnel` 金标口径 | L0 `--selftest` + L8 `funnel-shape-gate-gone` / `funnel-golden-subset-gone` |
| `knowledge/**` 里漏斗参考 SQL 的**口径**（不只是语法）+ 漏斗题到 `analysis/funnel_analysis.md` 的路由 + 「转化率绝对值不具参考性」这条结论约束 | L0 `--selftest` 第 7 组 + L8 `kb-funnel-subset-gone` / `kb-funnel-route-gone` / `kb-funnel-absolute-gone`。**这一面此前零覆盖**，是那次事故真正的漏点：`verify_doc_sql.py` 逐条 EXPLAIN 知识文档里的 SQL，但 EXPLAIN 只管语法——一条口径全错的漏斗 SQL 照样 EXPLAIN 通过 |
| `eval/run_eval.py`（`judge_retention` 的结论闸）+ `eval/cases.json` 的 `L5-retention-cohort` 金标口径 + `knowledge/**` 里留存参考 SQL 的口径与结论 + `backend/agent.py` 的 `LITE_SUFFIX` 文档路由 | L0 `--selftest` 第 8/9 组（含 8e 的路由对冲检查）+ L8 `kb-retention-cohort-column` / `kb-retention-numerator-unbounded` / `kb-retention-verdict-gone` / `retention-verdict-gate-gone` / `retention-gate-hardcoded` / `lite-mode-analysis-banned`。这一面盯的是**数值全对而结论仍然错**的那一类：曲线不衰减时，闸要求答案里出现「算不出/不可信/独立抽样/假象」这类声明，而**右删失说明不算**——那正是那次答错时唯一给出的 caveat；曲线衰减时闸让路，否则就是要求 agent 说假话（形状现从金标量，两个分支各有固定夹具） |
| `eval/run_eval.py` 的其余判分模式（`numbers` / `contains` / `judge_llm`） | **无覆盖**：形态闸那次事故说明判分器和被判的对象一样会错，而这几个模式至今没有断言碰过 |
| `analyticsagent/app/analytics/` 的六份整拷 + `agent.py` 的 17 个节点 | L0 `sync_agent_code.py --selftest` + `--check`（逐字比对）+ L8 `cloud-copy-drift`；**改这些要改 `backend/` 那份再跑 `--apply`** |
| `analyticsagent/` 的其余部分（`main.py` 暖客户端 / `knowledge_store.py` / `runtime_config.py` 的硬失败 / `Dockerfile` / `agentcore.json`） | **无自动化覆盖**。2026-08-20 已部署并人工复验过 `README.md` 里那三条，但每次改动都得重新手跑一遍 |
| `docs/**` | **无覆盖**，人工评审 |

## 最近一次实测（2026-09-17，把「4 盏红灯」逐条归因到机制并验证修法）

这一轮**没动数据、没动云上资源、没动 prompt**，做的是一件纯核对的事：把
`bash scripts/test_all.sh` 在本仓库开发账号上那个 **通过 50 · 失败 4** 拆开，逐条量出机制，
并证明修法存在、已实现、真能变绿。起因是我先前把这四条写成了「只能给出原因、没办法修复」
——那个判断是错的。

四条的共同点：**比的两端不是同一批数据**。本分支交付 `data/csv/` 种子（189,707 行），湖里是
8000 万行那批（scale 427.07 / seed 42，2026-08-31 从 `~/analytics-agent-data/genFULL_csv` 灌入）。
所以修法的落点不在本分支，而在栈里做重灌的那一支 `feat/data-reload-80m`（PR #15，`9be4a39`）。
验法：`git worktree add /tmp/wt15`，拿那一支的检查器对**同一个湖**跑，四条全绿。

| 红灯 | 机制（实测） | 修在哪 | 该支上的实测输出 |
|---|---|---|---|
| L2 枚举一致 | 只有一列：`sessions.utm_campaign`。卡片（`knowledge/domains/behavior/sessions.md`）写的是 v1 的 6 个占位值并带「实测行数 745/732/708/702/695/679」，湖里是生成器 `tables.py::CAMPAIGNS` 的 **39 个**真活动名（各 ~4.6–4.7 万行）+ 315,925 NULL | #15 重生卡片：39 个活动名，并**删掉实测行数那一列**——那一列本身就是一个会过期而且过期时是绿的声明 | `枚举一致 ✅  4 列取值与卡片相符`；`sessions.utm_campaign 39 种取值 +315925 NULL` |
| L2 退化列登记 | **38** 条清单过期：13 条 `RELOAD_PENDING` 已被重灌兑现（`users.created_at` / `posts.like_count` / `payments.refund_amount` …）、24 条 `ALL_NULL_PINNED` 现在有值（`page_views.page_url` 12,895,806 个非空、`user_coupons.coupon_code` 9,709,436、`events.ip_address` 8,541,400）、1 条 `DIMS_PASSTHROUGH` 不再是常量（`channel_daily_costs.created_at`）。**这是设计意图**：清除条件一旦兑现也判 FAIL，否则清单会变成"写上就永远绿" | #15：`RELOAD_PENDING` 清空、`DIMS_PASSTHROUGH` 收到 15 列 / 11 张表（`channel_daily_costs` 移出——它其实是生成表，2,799 行）、新增第四本 `BY_DESIGN_CONST`、数组列纳入普查 | `退化列全部登记在册 ✅  427 个标量列基数正常 + 6 个数组列有真元素，0 待重灌 + 15 透传遗留 + 2 刻意常量 + 24 整列 NULL + 0 整列空数组 已登记` |
| L3 装载完整 | 比 `data/csv/` ⟷ Athena，两端差 427 倍（`order_items` 4,225 ⟷ 1,804,371） | #15 的 `verify_load.py`：`CSV_DIR` 环境覆盖 + `_resolve_csv_dir()` 从装载快照 `data/loaded_row_counts.json` 的 `source` 反推该跟谁对账，源目录不在就**大声失败**、明说"不会退回 `data/csv` 去比" | `装载完整 ✅  3 张表的行数、数值求和、时间边界、布尔计数全等`（`-t users -t orders -t order_items`） |
| L5 退款 clamp | 这条钉的是**数据前提**而不是代码：「退款全量 ≠ 轴内」要有区分力，前提是存在越过 `as_of_date` 的退款。重灌后**轴外 0 条**（全量 9,897,495.82 == 轴内 9,897,495.82），钉子随之红——**是缺陷自己消失了**，clamp 没坏（`clamp_to_anchor` 本来就是 per-metric 声明的，`refund_amount` 上刻意为 False） | #15 把断言换到机制层：钉**编译出来的 SQL 里有没有 clamp 谓词**（与数据规模无关），数值比对降级成一条 ⚠️「这批数据里没有越过 as_of_date 的 refund_amount，数值比对无区分力」 | `✅ 编译 SQL 里 clamp 谓词不在  refund_amount` + 那条 ⚠️；整层 `14 条恒等式 + 2 条缺陷锚点 + 5 条指标层语义钉子全部成立 ✅` |

**本批唯一改在本分支的代码，也是这轮真正的教训**：`test_all.sh` 的 `grep_run()` 失败时只印
`tail -20`，而 L2 那份失败清单是 **38 行**——于是日志上只露出后 18 条，**被截断和"就这么多"
在报告上长得一模一样**。我按 18 条估了问题规模，把 13 条 `RELOAD_PENDING` + 1 条
`DIMS_PASSTHROUGH` 整个漏掉，还差点据此把 `channel_daily_costs` 从 `dims_to_parquet.DIMS` 里
删掉（方向正好反了：它是生成表）。改法是截断时多印一行「上面还截掉了 N 行」并给出复跑命令。
**这条缺陷与本套件反复点名的那一类同形**：不是给出错的答案，是让人读不出自己看的是残片。
L0 复跑 **33 / 0 ✅**。

**在 #15 上顺带发现两处，记在这里，不在本分支修：**

1. `_resolve_csv_dir()` 在装载快照**缺失**时返回 `None`，然后静默退回 `data/csv`。快照是
   gitignored 的，所以一个新克隆的仓库会拿 8000 万行的湖去比 189,707 行的种子，印出的正是
   这个函数写来防的那条红灯（第一次在 `/tmp/wt15` 里跑就撞上了，把快照拷进去才对得上）。
   快照缺失应当是**硬失败**，与"源目录不在"同一档。
2. `scripts/gen/selftest_closures.py` 的 `ENUM_SUPERSET_OK["sessions.utm_campaign"]` 豁免在
   #15 重生卡片之后已经**打不中**（自测原话：`刻意超集豁免 1 列：未命中`）。它自己写的清除
   条件是"全量重灌 + 重生卡片"，两件都已发生，按本库的规矩该删。留着它等于给这一列留了一道
   会吞掉第 40 个活动名的静默豁免——`enum-superset-exemption-void` 那个负测守的是"豁免被滥用
   到别的列"，守不到"豁免已经失效"。

## 上一批实测（2026-08-28 下半天，退化列体检 + 隐形声明那一批）

这一轮**没动数据、没动云上资源、没动 prompt**，加的是一层新判据和两处已有判据的补洞。
起因是上一轮末尾欠着的两条尾巴，查下去发现它们不是两个孤立缺陷，而是**同一个类**的
两种形态：「声明存在、但没有任何一层看得见它」。第三种形态在做这一层的过程中被顺手撞出来
（`user_profiles.country`），三种都记在「隐形声明」那一节。

| 层 | 结果 |
|---|---|
| L0–L6 + L8 | **通过 53 · 失败 0 ✅**（`bash scripts/test_all.sh --l8`，exit 0）。其中 L0–L6 是 **52**，L8 整体那一项是第 53 条。相对上一批 51 → 53 的两条都是本轮新挂的：L0 的「退化列分类器 + 登记清单自测」和 L2 的「线上库退化列全部登记在册」 |
| L8 负测 | 上一轮整套连跑 **43 / 43 全过 ✅**（**27 个离线 + 16 个连云**）；41 → 43 是那一轮新增的 `degenerate-col-unlisted` / `degenerate-col-stale-entry`，成对守新清单的两个方向。还原按 sha256 核对通过。**2026-09-17 本轮 43 → 49**：新增三个规模用例、一个金标锚点用例、一个留存结论闸的**反方向**用例（`retention-gate-hardcoded`：闸不许硬编码一句关于数据的结论）、一个常规模式的文档路由用例（`lite-mode-analysis-banned`），**离线 32 个整档连跑全过 ✅**，连云那个 `scale-lake-unregistered-batch` 单跑 PASS；其余 16 个连云用例本轮**没有重跑**（沿用上一轮结果）。顺带修了两个**锚点漂移**：`retention-verdict-gate-gone` 和 `kb-retention-verdict-gone` 的注入锚点被这一轮的改动挪走了，负测按设计显式 ERROR（"锚点出现 0 次"）而不是静默改别处——这正是那条"唯一子串替换"规则要的效果 |
| L7 端到端（2026-09-17 重跑） | **27 / 27 ✅**（20:29 起，24.5 分钟，27 次 Bedrock 调用，模型 `global.anthropic.claude-opus-4-8`，均 55.0s/题、均读文档 2.4 次、均 SQL 0.7 条）。这一轮**必须跑**：改了 prompt（规模措辞、轴末表、`LITE_SUFFIX` 的例外）、两张语义卡片和判分器，而 L7 是唯一覆盖 `backend/agent.py` 的一层。上一轮那道红 `L5-retention-cohort` 本轮 **88.2s / 读文档 3 次 / 1 条 SQL**，理由「曲线在衰减，结论闸不适用（金标实测 5 个 cohort 的末周/首周 ≈ 0.50，阈值 0.8）」——读文档 3 次说明 lite 模式的例外确实生效了（修之前那份 `analysis/` 文档在常规模式下打不开）。报告覆盖在 `eval/report.md` / `.json` |
| L0 跨表闭环 | 正例 **106 / 106**、反例 **44 / 44 按预期变红**（约 8s） |
| L2 `verify_constants.py` 连云普查（新增） | **退化列全部登记在册 ✅**：48 张表 / **468 个标量列**，389 列基数正常 + **14 待重灌 + 17 透传遗留 + 46 整列 NULL** 已登记 + 2 单行表，另有 **8 个数组列跳过并逐个打印**。耗时 **75s**。账算得平：389 + 33 + 46 = 468 |
| 两个方向都**在真库上注入验过** | 不只是离线 `--selftest`：从清单里摘掉 `posts.share_count` → 真库跑出「不在任何清单里」；往清单里塞一条 `posts.title` → 跑出「已经不是常量了」。验完按 sha256 还原，字节相同。然后把这两次注入固化成上面那两个 L8 用例——**否则这盏新灯本身是没验过的** |
| 修掉的两处套件红灯 | ① `profiles.py --selftest` 抛 `KeyError: '管理者'`：职业池对齐线上库时（12 → 15 个取值）删掉了 `管理者`，而自测里有 3 处硬写这个名字。**它是崩溃不是断言失败**，所以按 `/FAIL/` 抓的那层看不见它，按 `/全部通过/` 抓才看得见——这条教训写进了 `profiles.py` 的注释。修完 **21 项断言全过**。② `verify_enums.py` 报 `TYPE_MISMATCH: Cannot cast array(varchar) to varchar`：给 `interests` 补上声明之后，取数那边对数组列做 `CAST(... AS VARCHAR)` 直接炸。改成 `types` 驱动的 `UNNEST` 分支（见「隐形声明」形态 ②） |
| `eval/cases.json` 的 `trap` 文字 | 改了 `L2-top-liked-posts` 那条——原文写「1000 条里 7 条重名」，实测是 **38 个重名标题覆盖 77 行**，而且真正的问题不是重名：`posts.like_count` 整列 0，那道题的第二个金标变体**已经死了**。**改 `trap` 不动判分**（全仓库没有任何 Python 读这个字段，先核过才改），所以不需要 L7。想把 `p.title` 换成 `post_id` 或删掉死变体则**要动金标 → 一次完整 L7** |
| `backend/agent.py` 的 `DENIED_BUILTINS` 注释 | 补了「这份清单的两种错法**不对称**」和「SDK 升级后怎么重新推导这份清单」两段。多列 = 无害空转（按名字匹配，匹配不到静默跳过）；少列 = 那个工具直接进模型工具表且因 `bypassPermissions` 不弹确认。所以**不要**为了清理过期条目删东西。`--selftest`（白名单 6 · 黑名单 30）与 `sync_agent_code.py --check` 都绿 |
| L7 端到端 | **本轮没跑，刻意的**。本轮零 prompt 改动，`AGENTS.md` 的触发条件不成立；`knowledge/` 卡片这一批改过 4 处（枚举表格化），要不要为它跑一次 27 例（≈22 分钟 + token）、以及卡片是靠 `COPY knowledge/` 进镜像所以云上要**重新部署**（`sync_agent_code.py` **不覆盖** `knowledge/`），留给人定 |
| 全量重灌 | **仍未做**。本轮又攒了几条重灌前要定的口径，逐条见下 |

本轮新增的重灌输入（和上一批的那些并列，不是替代）：

- 14 个 `RELOAD_PENDING` 列在重灌后应当自动变成非常量——**它们同时是重灌成功的判据**，
  因为 `verify_constants.py` 对"清单条目过期"也判红，重灌后不删清单套件就是红的。
- `user_profiles.country` 会从 `'中国'` 变成 `'China'`，届时卡片、生成器、库三方一致；
  **在那之前这一列的字面量以库为准**。
- `L2-top-liked-posts` 的第二个金标变体会复活（`like_count` 不再恒 0）。
- `users.phone` 的唯一性口径**先不动**，它是重灌前要定的口径之一（见 L9 那一栏，注意
  别把全量的 3 个重复读成线上库的）。
- 4 张 SUB 表（`ad_campaigns` / `ad_creatives` / `campaigns` / `coupons`）仍未对齐，
  它们落在 `DIMS_PASSTHROUGH` 上，**重灌不会修**。

## 上一批实测（2026-08-28 上半天，两个报告器跑到全量那一批）

这一轮**没动生成器、没动云上任何东西**，做的是把 L2+ 两个报告器从「小样本上判得对」
推到「全量产出上真跑得完、真跑过」，末尾补上了欠着的一次 L7 全量。之所以单独记一轮：
过程中抓到的两个缺陷都**只在目标规模下存在**，按小样本构造永远碰不到（详见「没有自动化
覆盖」新增的两栏）。

跑 L7 时踩到一条操作陷阱，写在这里免得下一个人丢产物：**`run_eval.py --case <id>` 会
直接覆写 `eval/report.md` / `report.json`**（`--dry-run` 不会，它只写 `report.dryrun.json`）。
所以「跑完全量、再单跑一题复现」这个很自然的顺序会把 27 例的产物换成 1 例的。
单跑前先备份，本轮的全量产物是从备份还原回去的。

| 层 | 结果 |
|---|---|
| L0–L6 | **50 PASS / 0 FAIL**（`bash scripts/test_all.sh`，exit 0；49 → 50 是本轮新挂的闭环反例一侧那一行）。加 `--l8` 或 `--ask` 各再多一行 → **51 / 0**，两个档本轮都跑过。这是硬约束「改完跑全链路，不只跑改动的那部分」那一条；两个改动文件在 `test_all.sh` 里只被 `--selftest` 盯着（第 153–167 行），所以这条不是形式 |
| L8 负测 | **41 / 41 全过 ✅**（`bash scripts/test_all.sh --l8`，exit 0，**含连云那 14 个**）。「每个检查器都在对应缺陷面前变红并给出了正确的消息」。它临时改仓库内文件（含 `data/csv/`）再按 sha256 还原，本轮还原核对通过 |
| L0 跨表闭环**反例一侧**（本轮新增） | **42 / 42 按预期变红**（`selftest_closures.py --negative`，约 7s），正例侧同时仍 **105 / 105**。判据是"红在指定的那条断言上"而不是"有断言红了"，第一次跑就抓到一个红在别处的注入（`tag-not-unique` 漏搬 `tag_type`，先破了 tag_pools 分池那条）。详见 L0 那一节 |
| L6+ `/ask` 流式契约 | **PASS**（`bash scripts/test_all.sh --ask`，exit 0，全场 51/0）。SSE 分帧 / `delta` 键名 / 过期 `session_id` 降级三条，一次真问答烧一次 Opus。这三样此前只在 2026-08 挂上时跑过，本轮是补跑——它们坏掉的样子是"界面上一句红字"，静态检查看不出来 |
| L2+ `verify_behavior.py` **全量**（L5） | **27 项全部通过 ✅**。规模：7 张 CSV 共 **43,356,843** 行，其中四张行为大表 **40,581,089** 行（赞 15,231,627 · 关注 8,184,202 · 浏览 12,894,870 · 推送 4,270,390）。耗时 **119.39s real / 116.88s user**。这是这批判据**第一次**在目标规模上给出结论——此前所有绿灯都来自 scale 1 |
| L2+ `verify_literals.py` **全量**（L9，新增 `--from-csv`） | **PASS 63 · FAIL 0 · 豁免 5 · 空列 2**，一致性五条全 ok，「字面值体检：全部通过 ✅」。覆盖 **19 张表 / 70 个文本列**，普查之外 8 个数组/JSONB 列 + 5 个 `TYPE_EXEMPT` 列**逐个点名**。耗时 **156.65s real / 154.48s user**，峰值 RSS **478,412,800 字节（456 MB）**——累加器按表释放，所以峰值跟着最大单表走，不是跟着 7.0 GB 的整份产出走 |
| L5 全量的关键实测点 | 取值域四条：`referrer` 非空 9,286,734/12,894,870 = 72.0% · 9 个取值；`page_url` 100% · 15 个；`scheduled_at` 100% · **3,024,602** 个；`deep_link` 100% · 26 个。集中度：每帖被赞 top10% 份额 0.6617 = 随机基线的 **5.11×**；每用户点赞 3.69×；粉丝数 top1% **7.02×**；每用户推送数 0.4497 = 基线 0.1392 的 **3.23×**。形状：互关率 30.0184% vs 随机边密度 0.0180%（**1672×**）、Gini(粉丝) − Gini(关注) = +0.210、home PV / checkout PV = 8.334×、corr(停留时长, 滚动深度) = **+0.5338**（n=12,894,870）、打开率 max/min **3.795×**（order 41.93% … promotion 11.05%）。定性 11 条通过线恒为 0 的判据在全量上**全部为 0 行**（草稿帖被赞、赞早于发帖、自关注、`view_time` 越窗、推送时间戳倒序、事务型带 campaign_id 各 0）。诊断：自赞 69 行 = 0.000%、五类推送量 max/min 1.067、每会话页面数 max=582 p50=4 |
| L9 全量的关键实测点 | `users.email` 域名基数 10；`users.phone` 号段基数 40；push 标题⟷正文按类型一一对应（order 6 / promotion 9 / reminder 5 / social 5 / system 4 种模板）；`posts` **427,039 行正文首句全部与标题同源**；`user_devices` 品牌/机型/系统三列自洽。空列两个（`payments.failure_reason`、`subscriptions.cancel_reason`），与「明确不要修」一致。**一个新观察，本轮没动**：`users.phone` n=213,520 而基数 **213,517**，即有 3 个号码重复——它走的是 `ex`（纯数字列，真实性由号段正则负责），所以现有判据不会红；重复手机号在真实系统里通常是唯一约束，要不要当缺陷需要先定口径。**这两个数是全量产出（`/tmp/genFULL_csv`）上的，不是线上库的**——两个规模差 427 倍，读串了会得出相反结论：2026-08-28 在线上库（500 行）上现查的是 `phone` **0 个重复**，真有碰撞的是 `username`（11 个重复值）和 `email`（2 个）。所以这条口径的落点是「全量下会变成上千个重复」，而不是「现在库里就有 3 个」 |
| 修掉的缺陷 | `verify_behavior.py --from-csv` 的二次复杂度（`seen \| {val}` 逐行重建集合，4 处改动，现在 `scripts/lakehouse/verify_behavior.py:1115` 起）。指数从 2.25–2.56 掉到 0.94–1.04，scale 40 从 1819.29s 到 9.05s（**201×**），全量从外推 ≈57 小时变成 119s。**验法是拿旧实现的输出当 oracle**：同一份 scale-40 夹具，新旧输出 85 行**逐字节相同**——只有 `len()` 被消费，原地插入与重建结果必然一致，但「必然」得有一次实测撑着 |
| 补掉的缺口 | `verify_literals.py` 是五个里最后一个只有 Athena 一条路的，于是生成器的字面值修复**没有任何一条路验得到**（云上是 v1 数据、已决定不重灌）。补 `--from-csv` 时抽出三个共用判据（`merge_col_verdicts` / `judge_posts` / `judge_device`），**PASS/FAIL 合成规则零复制**；类型真源用 DDL 而非 `information_schema`，顺带把数组/JSONB 那个**不可见**的覆盖缺口变成了报告上印出来的一行 |
| L7 端到端 | **27 / 27 全过**（2026-08-28 15:47，`eval/run_eval.py` 全量 27 例，模型 `global.anthropic.claude-opus-4-8`）。均 **49.6s/题**、均读文档 2.6 次、均 SQL 0.9 条，27 题合计 1340.3s（≈22 分钟）；`agent_errors` 全空，最慢三题 `L3-churn-30d` 71.8s · `L5-repurchase-rate` 71.2s · `L5-retention-cohort` 70.9s。**跑之前先跑了一次 `--dry-run`**（只验金标、不调模型，27/27 金标 SQL 可执行），避免把金标语法错烧成 20 分钟的 token。触发这一轮的是 D-05 改过的两张语义卡片（`knowledge/analysis/funnel_analysis.md`、`knowledge/metrics/core_metrics.md`），`AGENTS.md` 的触发条件成立。逐题与基线的对比见下一栏 |
| L7 与基线逐题对比 | 基线 `eval/baseline/eval.lakehouse-athena.post-funnel-retention-fix.json`（2026-08-23）。**题集相同、状态零变化（27 全 pass → 27 全 pass）**；`avg_s` 48.3 → 49.6、`avg_sql` 0.8 → 0.9。判定理由（`detail`）**只有 1 题变了**：`L5-retention-cohort` 从「命中 golden[weekly matrix **pct**] 16/16」变成「命中 golden[weekly matrix **counts**] 20/20」，结论闸「结论已声明数据限制」两轮都过。**这不是判分放松，是答案更全了**，而且可验证：`judge_retention` 按声明顺序先试 counts 再试 pct，我拿两组金标值实测过交叉命中——纯百分比答案在 5% 容差下只能蹭到 counts 的 4/20（`41≈41.5`、`37≈37.8`、`49`），**低于 `min_hit=6`**，所以「命中 counts 20/20」只能来自答案里真的印出了那张整数矩阵；反向则会蹭（纯计数答案能命中 pct 9/16），所以**pct 那条 label 才是不可靠的那个**。单跑该题复现了一次，两次都是 counts。两轮金标行本身逐字相同（数据没动）。另有 7 题 `n_sql`/`n_docs` 有运行间抖动（`L1-dau-latest` 0→1 条 SQL、`L5-wow-gmv` 1→2、`L3-top-products-gmv` 读文档 4→6 等），**不是判定依据**；真正该守的那条不变量守住了：指标层五题（`L3-gmv-30d`/`L3-refund-total`/`L3-cac-overall`/`L4-cac-lowest-channel`/`L4-roi-cac-by-channel`）仍然 `n_sql=0`，没有绕过 `metric_layer` 去手写 SQL |
| L7 在全量批次上的那一道红（2026-09-17） | 湖里换成 8000 万行那一批之后，`eval/report.md` 记的是 **26 / 27**，红的是 `L5-retention-cohort`，理由「结论里没有声明「这份数据算不出留存」」。查下来是**两个**独立成因，都不在判分器的数值一侧：①**路由**——`LITE_SUFFIX` 写着「不要读 analysis/ 方法库」，而 `knowledge/domains/behavior/_index.md` 写着「两个都要读，哪怕只是取数」，系统提示赢，于是那份文档在常规模式下从来没被打开过（deep 模式只有 4 个预设按钮进得去，手打的问题一律走 lite，所以这是产品缺陷不是评测保真度问题）；②**判据自己过期了**——新生成器给用户配了活跃半衰期（`scripts/gen/tables.py` 的 `ENGAGEMENT_HALFLIFE`），这一批的曲线实测 71.6→51.9→42.3→37.2（W4/W1≈0.52）是**正常衰减**的，写死的结论闸于是在要求 agent 说一句假话。两条都已修：提示后缀加了一条点名 `analysis/` 的例外、两份卡片从"下结论"改成"给判据（W4/W1 ≥ 0.8 算不衰减）"、`judge_retention` 改成现从金标 `pct` 那一份量形状再决定开不开闸，新增 L8 `lite-mode-analysis-banned` / `retention-gate-hardcoded` 两条反向用例。**修完当天重跑了整套 L7：27/27 ✅**（2026-09-17 20:29，24.5 分钟，均 55.0s/题），该题耗时 88.2s、读文档 **3** 次（路由那一半确实通了），判定理由是「曲线在衰减，结论闸不适用（金标实测 5 个 cohort 的末周/首周 ≈ 0.50，阈值 0.8）」。离线一侧同时全绿：`run_eval.py --selftest` 122 条断言（含两个分支的固定夹具）、离线负测 32/32、L0 34/0 |
| 全量重灌 | **未做，刻意的**。任务书硬约束 #1「全量重灌放在最后一步」；重灌的危害面已单独盘过，最尖的一条是**成本/维度表冻在 ×1 而转化类表 ×427，CAC 会塌 427 倍、ROI 涨 427 倍**，而 CAC/ROI 是 `governed_metrics.md` 的招牌指标、卡片里钉着具体数字、没有任何闸门盯着。等口径决定 |

上表几行都是**文档与 docstring 也改完之后重跑的**，不是只覆盖代码改动的那一次：改完注释
先跑了一轮全绿，随后补写本文档和两个脚本的 docstring，再整套重跑一遍，四条依次是
`verify_behavior --selftest` 全部通过 · `verify_literals --selftest` 全部通过 ·
`negative_tests.py --offline` 27/27 · `test_all.sh` **通过 49 · 失败 0**。写这一句是因为
中间隔了一次环境故障，容易被读成"注释改完没重跑"。补完闭环反例一侧之后又跑了两遍完整
链路（`--l8` 与 `--ask`），两次都是 **51 / 0、exit 0**（基线档 50 / 0，`--l8` 与 `--ask` 各多一行），这才是当前的口径。

**本轮末尾把三条只在全量上量到、此前没记在任何地方的发现补进了「没有自动化覆盖」**，
连同复测数字与产地（`/tmp/genFULL_csv`）：`order_items.unit_price` ⟷ `products.price`
逐行相关 −0.0010（旧条目的 16.5× 换成了新生成器上的实测 13.2×），以及新增一栏「计数器
与本该解释它的那一列零相关」下的两处——帖子传播量 ⟷ 曝光时长 `+0.0035`、`product_tags`
标签 ⟷ 销量 0.88×–1.11×。三条都**没修**，写下来的理由是：它们不写进文档就只存在于一次
对话里，而下一个人看到的是「L2+ 五个报告器全绿」。

### 仓库位置：2026-08-28 从 `~/Desktop` 移到 `~/Documents`

这不是洁癖，是**跑不了测试**：macOS 对 `~/Desktop` 的保护会拒掉终端子进程的**目录枚举**
（`ls` / `du` / Python 的 `os.listdir` 一律 `Operation not permitted`，而读单个文件、
甚至新建文件都正常），于是 Python 连 `import` 都做不了——`import` 要 readdir，
`sys.path` 上的每个目录都被拒，报出来的却是 `No module named ...`，很容易被误诊成代码问题。
这个限制的形状不是单纯缺一个「完全磁盘访问权限」开关能解释的（FDA 是整片放行或整片拒绝，
而这里是「读文件可以、列目录不行、建文件可以、删文件不行」，且个别子目录如
`backend/.venv` 反而可列），本机是 MDM 托管、有四个安全 agent 持全盘授权，最可能是它们
在桌面上的策略。`~/Documents` / `~/Downloads` 均不受影响，所以搬走是成本最低的解法。

搬迁对本仓库是安全的，**已核对**：`test_all.sh` / `run.sh` / `.env.local` / 部署文档里
写死项目绝对路径的地方是 **0** 处（全走相对路径或 `Path(__file__)`）。只有 venv 内部两处
提旧路径：`pyvenv.cfg` 的 `command =` 是纯记录（起作用的 `home` / `executable` 指向
`/opt/homebrew`，与项目位置无关），以及 **`backend/.venv/bin/pip` 的 shebang 仍指向旧路径、
现在是坏的**——本套测试一次都不调 `pip`，所以没影响；真要用就走
`backend/.venv/bin/python -m pip`，或重建 venv。**venv 没有重建**，这一点记在这里，
免得下次有人看到 `pip` 报错去查别处。

## 上一批实测（2026-08-26，商品域重做那一批）

| 层 | 结果 |
|---|---|
| L0–L6 | **43 PASS / 0 FAIL**（`bash scripts/test_all.sh`）。比上一轮多的那 1 项是新加的 L0 闸 `semantics.py --selftest`（8 条断言） |
| L0 生成器闭环 | `selftest_closures.py` **66 条断言全过**。这一批里它先是**红**的：断言按顺序先写、先跑、确认失败，再改生成器——没有那一步就证明不了断言真在测那件事 |
| L2+ `verify_semantics.py` | `--selftest` 全过；对生成侧全量产出（4,133 SKU）**5 类判据 0 违例**；对云上 v1 数据 **3 类 FAIL**（白名单 123/200 违例、价格 154/200、名字模板 200/200）。后一组是它有区分力的证据 |
| L2+ `verify_resolution.py` | `--selftest` 全过；对全量产出**6 条全绿**（空类目 0；每类目 SKU min 7 / 中位 29；每品牌 min 6；每 SKU 均值 436.5 单；126 个类目可算价格跨度、中位 4.8×、0 越界）；对云上 **5 项 FAIL**（25 个空类目、每类目中位 1、27 个类目价格跨度越界） |
| L2+ `verify_correlation.py`（D-01） | `--selftest` 30 项全过（含审计实测那四个极差 1.4/3.2/4.9/7.0% 必须判 FAIL）。**改生成器之前**在 `scripts/gen` 产出上逐级量过：scale 1 → 2 FAIL·4 WEAK，scale 50 → 5 FAIL·2 WEAK，scale 200 → 7 FAIL·1 WEAK，**scale 500 → 8 FAIL·0 WEAK**（全红基线）。极差随规模**变小**（occupation 18.1% → 12.4% → 5.2%），正是独立抽样的预测。**改完之后** scale 500 → **8 条全绿**（income 373.2% / occupation 232.9% / age 105.6% / gender 31.8%，各自贴着配置声明的倍率跨度 373/236/115/35%），全库 GMV 只漂了 +0.19%，卡片钉住的两条边际分布分毫未动。对云上跑仍然红（v1 数据，已决定不重灌） |
| L2+ `verify_behavior.py`（四张行为大表的分布） | `--selftest` **79 项全过**。**两条取数路径的 27 个结论逐位相同**（Athena 18 个聚合查询 ⟷ CSV 逐行流式），这验的是两份实现对得上，不是数据对。**现行库 21 FAIL / 27**；`scripts/gen` scale 1 **14 FAIL / 27**。关键是**各自过的不是同一批**：4 条现行库过而新生成器不过（赞的时序 0 ⟷ 17,802 行、推送时序 0 ⟷ 13 行、campaign 归属 0 ⟷ 6,539 行、`deep_link` 4,275 ⟷ 1 个取值），11 条反过来（两个人均集中度、会话深度、跳出率两条、停留时长、`view_time` 窗口、`failure_reason`、推送量集中度、`page_url`、`scheduled_at`）。这一组因此**不含恒红的判据**（那种只在描述一个已知缺陷，不携带信息）**也不含恒绿的**（那种是死掉的灯）——本批只加检查不动生成器，这是能给出的最强区分力证据。只有 4 条在两份数据上同为绿，**要留着**：它们是唯一「现有数据这件事做对了」的正向结论 |
| L7 / L8 | **本轮没跑**。L7 没跑是因为这一批没动提示词、金标或 `knowledge/`（`AGENTS.md` 的触发条件不成立）。**L8 没跑是个真缺口**：`scripts/test_all.sh` 加了一行、`scripts/gen/` 大幅改动，负测能证明「这些新检查该报时真会报」，而这一点目前只有各脚本自己的 `--selftest` 在担保 |

商品域修完前后的对照（`scripts/gen/main.py` 全量产出，非云上库）：

| 指标 | 修之前 | 修之后 |
|---|---|---|
| SKU 行数 | 200（`data/csv` 的 v1 数据，`budget.py` 声明该有 4,133） | 4,133 |
| `product_tags` 行数 | 512 | 10,659 |
| 有 SKU 的叶子类目 | 101 / 126 | **126 / 126** |
| 每类目 SKU 数 | min 0 · 中位 1 | min 7 · 中位 29 |
| 每品牌 SKU 数 | min 1 · 中位 3 | min 6 · 中位 29 |
| 每 SKU 平均被下单次数 | — | 436.5 |

## 上一次全量实测（2026-08-24）

| 层 | 结果 |
|---|---|
| L0–L6 | **38 PASS / 0 FAIL**（`bash scripts/test_all.sh --l8`，us-west-2；含 L8 整体那一项共 **39 PASS / 0 FAIL**）。带 `--ask` 的那一轮见下面 L6+ |
| L0 | 18 项；生成器那 2 项因本机没装 numpy 记 note（不算 PASS 也不算 FAIL）。比上一轮多的那一项是工具白名单边界自测（`backend/agent.py --selftest`） |
| L6 | 探针相关断言实测：`/health` 首次响应 **2012ms**（单发预算 8000ms；这个数现在由本地常量 `_PING_WAIT_S`/`_INFO_WAIT_S` 定上限，不再由 Athena 决定——见下面那次回归）、数据层预热 **5s**（前端等 `warming` 的窗口 ≈45s；这个数由 Athena 决定，同机器实测 5s～13.5s 都出现过）、`dataLayer` 三态在位 |
| L3 | 抽查 3 张表；`--full` 35 张亦通过 |
| L4 | 三条断言**全部在云上跑通**：`--selftest` 离线过、`--verify` 以 agent 角色实测 18 条探针（assume 完先 `get_caller_identity()` 确认身份，否则 AssumeRole 被拒时负向探针会假绿）、`--verify-backend` 确认后端用的是受限凭证（`/health` 的 `identity` 就是治理角色） |
| L7 | **27/27**（2026-08-24，工具闸门改成 `PreToolUse` hook 之后重跑），模型 `global.anthropic.claude-opus-4-8`，均 51.9s/题、均读文档 2.4 次、均 SQL 0.8 条（`eval/report.md`；基线归档在 `eval/baseline/eval.lakehouse-athena.post-funnel-retention-fix.md`+`.json`，那是 2026-08-23 那轮）。**同日前一轮是 26/27**，唯一失败的 `L3-channel-cost-total` 是模型跑完 SQL 后**没调 `present_result`**（`has_result=false`，`agent_errors` 为空），不是口径或 SQL 错。闸门被排除在成因之外靠的是实测而不是推理：给 hook 加上日志连跑三轮，闸门只见到那 6 个白名单工具、**拒绝 0 次**；该题此后连过 5 次。判据留着不放松——`run_eval.py` 只在"一条 SQL 都没发"时重试（瞬时 infra 的签名），少了 `present_result` 不重试，因为那也可能是真回归 |**这是漏斗与留存口径修完后的第一次全量**：`L4-funnel` 的判定理由从「命中 golden[all-time distinct users]」变成「命中 golden[all-time **subset funnel**]」——上一轮的 26/26（`post-anchor-fix.*`，保留为历史）通过率一样，但判的是错口径的金标，**两轮之间的差别不在通过率**。新增 `L5-retention-cohort`（判分模式 `retention`）实测理由是「结论已声明数据限制；命中 golden[weekly matrix pct] 16/16 个数值」。其余 25 题状态与理由未变；读文档次数有几处运行间抖动（`L1-dau-latest` 3 → 0 等），**不是判定依据** |
| L6+ | `/ask` 流式契约通过（`--ask`）：过期 `session_id` 不再打死整轮，收到 `stage/resume` + 新 UUID + 带 KPI 的 `result` |
| L8 | **38/38**（24 个离线 + 14 个连云，含两个工具白名单用例 `tool-gate-shadowed` / `tool-gate-toolsearch-locked`，三个 `gov-*`、`cloud-copy-drift`、`doc-row-total-drift`，四个探针用例 `probe-budget-too-tight` / `probe-warming-treated-as-dead` / `probe-degrade-is-permanent` / `probe-baked-answer-while-backend-alive`，两个 `shell-*` 静态资源用例，四个漏斗用例 `funnel-shape-gate-gone` / `funnel-golden-subset-gone` / `kb-funnel-subset-gone` / `kb-funnel-route-gone`，以及四个留存用例 `kb-retention-cohort-column` / `kb-retention-numerator-unbounded` / `kb-retention-verdict-gone` / `retention-verdict-gate-gone`）。其中 `gov-backend-not-assuming` 在整套连跑时红过一次，原因是本机 AWS 凭证跑到一半过期（报的是凭证获取失败，不是那个缺陷的消息）——**负测要求消息匹配，所以它没被算成通过**，刷新凭证后单跑该用例 PASS |

这一轮里 L7 抓到一次真回归，值得记下来，因为它正是这套评测存在的理由：
`L3-refund-total`（「这段时间一共退了多少钱」）从 ✅ 变 ❌ —— agent 把没点明时间范围的
问题**默认成了「近 30 天」**，答 28.11 万，而全量是 96.36 万，差 3.4 倍。它不报错、
数看着正常，答案里还大方写着「我按近 30 天来算」。0 条 SQL、0 次读文档就答了（走
`call_metric`），所以连"SQL 写错了"这种线索都没有。修在 `backend/agent.py` 的 SYSTEM
提示词里（没点明范围 → 全量；但比值类要补 `dt <= 锚点` 的上界，否则全量 CAC 会拿
8 个月成本去除一段没有新客的日子），改完 26 题全过。

规模：Glue 目录 48 张表、约 220,087 行（其中 35 张原始表 189,672 行）。
`/api/catalog` 与 `web/catalog.json` 报的是 **180,419 行 / 行数覆盖 47/48** —— 差额是
`user_messages`：L4 之后它整表不在授权面里，后端拿不到它的行数，UI 明说覆盖 47/48。
两个数都对，只是**一个是数据的规模，一个是被治理后看得见的规模**。
