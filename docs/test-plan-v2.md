# v2 迁移测试流程

覆盖 Redshift + Glue 这次全部改动的验收流程。分九层，**从不依赖云资源的秒级自测往上走**，
越靠后越慢越贵，早层挂了就没必要跑后面。

L0–L6 可以一条命令跑完（`scripts/test_all.sh`）。L7 要花 LLM token，L8 会临时改文件，
两者手动执行。

> 这份流程里的每一条命令、每一个预期值**都实际执行过**，包括 L8 的六个缺陷注入
> （逐个确认对应检查会触发，然后撤销）。写测试流程本身也会犯「看着对但没跑过」的毛病，
> 所以预期输出都是抄实测结果，不是照理推的。

**为什么 L8 不能省**：本项目在检查器上踩过两次假阳性（卡片的枚举行被当列名、
`IS NOT NULL` 里的 `is` 被当列名），假阴性同样要防。一个永远绿的检查器和没有检查器
等价，甚至更糟——它会让人以为有覆盖。`test_all.sh` 自己也做了这个验证：
给错的 catalog / 错的账号期望时确认它真的 exit 1。

## 前置条件

```bash
aws sts get-caller-identity            # 确认是你部署这套资源的账号
# 多账号环境建议在 .env.local 钉 EXPECT_ACCOUNT=<账号>，脚本会自动核对（见 .env.local.example）
export AWS_REGION=ap-northeast-1
```

两个容易踩的环境问题：

- **`DB_BACKEND` 默认已是 `redshift`**，一般不用管。曾经默认 `postgres`，不设就去
  `import psycopg` 报 `ModuleNotFoundError`——而真正的库在云上好着，属于典型的
  「默认值指向已退役资源」。要走 v1 本地 Postgres 就显式设 `DB_BACKEND=postgres`
  （`docker-compose.cloud.yml` 已经钉住了这个值）。
- **跑 agent 要用 `./backend/.venv/bin/python`**。`claude_agent_sdk` 装在那个 venv 里，
  系统 python 没有。只跑 SQL 层的步骤用系统 `python3` 就行。

还有一个判定退出码时的坑，写下来因为我们自己踩了：**别在管道后面读 `$?`**。
`cmd | tail -3; echo $?` 拿到的是 `tail` 的状态，永远是 0，会把失败读成成功。
bash 里用 `${PIPESTATUS[0]}`，zsh 里是 `${pipestatus[1]}`，两者不通用。
最省心的写法是不接管道：

```bash
bash scripts/test_all.sh > /dev/null 2>&1; echo "exit=$?"
```

---

## L0 静态自测（无云依赖，秒级）

```bash
python3 scripts/gen/selftest_fillers.py            # 27 项执行断言（28 个 check 调用点，其中 1 对互斥）
python3 scripts/gen/selftest_closures.py           # 40 项跨表闭环断言（头=明细和、计数器回填、等级规则、券闭环、漏斗单调、留存衰减）
python3 scripts/gen/pg_to_redshift.py --selftest   # 6 个方言改写 case
python3 scripts/manifest/render.py --check         # manifest 必填字段/layer/owner/replaced_by/domain
python3 scripts/governance/selftest_classification.py  # 48 表 476 列分类清单 + 治理快照
```

预期前三条和分类自测输出 `全部通过`，manifest 输出 `manifest OK：8 张派生表`。

挂了说明什么：分布形状退化（幂律参数那个坑）、或 `FILTER (WHERE)` → `CASE WHEN` 改写有回归。
这一层不碰网络，改生成器或方言转换后**先跑这个**。

## L1 云资源状态

```bash
python3 scripts/glue/register_catalog.py --verify
```

预期五步全部 `跳过 ✅`，末尾 `合计 48 张表`，并列出两个子 catalog（`/dev` 空库 +
`/app_analytics` 48 张）。

挂了说明什么：datashare 关联掉了（`ACTIVE` 变回 `AUTHORIZED`）、LF 注册被撤、
或 catalog 被删。`--verify` 只读不写，可以随便跑。

## L2 元数据对账（两条独立路径）

```bash
python3 scripts/gen/verify_ddl_vs_redshift.py                    # DDL ⟷ 数据库
python3 scripts/glue/reconcile.py \
  --catalog 123456789012:analytics_agent_rs --governance-state --strict
  # DDL ⟷ Glue ⟷ 卡片 ⟷ data_classification ⟷ 实际 DDM/GRANT
```

预期分别是 `一致 ✅  列名与顺序全等`（35 张表）和 `对账通过 ✅`，两条都 exit 0。

两条不能互相替代：

| 脚本 | 实际态来源 | 独有覆盖 |
|---|---|---|
| `verify_ddl_vs_redshift.py` | Redshift `information_schema` | **列顺序**。Parquet COPY 按位置映射，顺序错了会静默灌进相邻列 |
| `reconcile.py` | Glue Catalog + Redshift 治理视图 | 语义层、分类清单完整性，以及实际 DDM/GRANT 是否符合声明 |

`reconcile.py` 会额外打印一段「目录可见面实测」，那是**边界说明不是缺陷**：DDM 表在
`GetTables` 里照常出现（连 PII 列名都在），只有 `GetTable` 被挡。别把它当失败。
第七类 G 检查以 `database/redshift/data_classification.yaml` 为声明态：任何实际新增但未进入
`reviewed_columns` 的列都会失败；`--governance-state` 再核对 DDM policy 与 45/48 授权面。

## L3 数据一致性（生成器预期值 ⟷ 库里实际）

回归检查（比两份归档，秒级）：

```bash
python3 scripts/consistency/snapshot.py --compare \
  eval/baseline/consistency.generator-expected.postdatafix.json \
  eval/baseline/consistency.redshift.postdatafix.json --subset
```

真实检查（重新查库，48 张表，约 3–5 分钟）：

```bash
SNAPSHOT_BACKEND=redshift-data python3 scripts/consistency/snapshot.py --out /tmp/rs.json
python3 scripts/consistency/snapshot.py --compare \
  eval/baseline/consistency.generator-expected.postdatafix.json /tmp/rs.json --subset
```

预期 `一致 ✅  21 张表，逐表 count/sum/min-max 全等`。

**改了数据或重灌过库，必须跑真实检查**，回归检查只能证明归档没被人手改过。
挂了说明什么：COPY 丢行、类型映射掉精度、时区偏移。

`--subset` 不能省：生成器只覆盖 21 张事实表，库里有 48 张（含维度表和 mart），
不加这个参数会把多出来的表当差异报。

## L4 治理层实测

```bash
# 脱敏是否真生效（注意：这是以管理员身份查，仍应看到掩码值）
python3 scripts/redshift/rsql.py "SELECT email, phone FROM users LIMIT 2"

# 策略附着的权威证据
python3 scripts/redshift/rsql.py "SELECT policy_name, table_name, input_columns, \
  grantee, is_masking_datashare_on FROM svv_attached_masking_policy ORDER BY table_name"

# 授权面的形状
python3 scripts/redshift/rsql.py "SELECT count(*) FROM svv_relation_privileges \
  WHERE identity_name='analytics_agent_ro' AND privilege_type='SELECT'"

# 私信正文必须缺席（预期 0 行）
python3 scripts/redshift/rsql.py "SELECT relation_name FROM svv_relation_privileges \
  WHERE identity_name='analytics_agent_ro' AND relation_name='user_messages'"
```

预期值：

| 检查 | 预期 |
|---|---|
| `users.email` | `***@masked.invalid` |
| `users.phone` | `103****xxxx`（前 3 位 + 掩码 + 后 4 位） |
| 策略条数 | 3 条，`grantee=public`，`is_masking_datashare_on=t` |
| 角色可 SELECT 表数 | **45**（目录里 48，差 `user_messages` 与两张陷阱表） |
| `user_messages` 授权 | **0 行** |

以管理员身份也应该看到掩码值——这就是 `TO PUBLIC` 的意义。**看到明文就是治理失效**，
说明 `04_governance.sql` 没跑或策略被 DETACH 了。

> `svv_attached_masking_policy` 与 `svv_relation_privileges` 会按当前身份过滤可见行。只读审计
> 角色既不是 policy owner 也不是 grantee 时会得到 **0 行而非报错**，所以 L2 的
> `--governance-state` 及上述“3 条策略/45 张表”必须用 admin/owner 凭证。只读角色应改做
> 负测：确认 3 个字段返回掩码，并确认 `user_messages` 与两张陷阱表均 `permission denied`。

## L5 查询路径

```bash
# 四条独立路径只比较彼此是否相等，不把某次重载的 GMV 常量写进门禁
python3 scripts/redshift/rsql.py "
WITH paths AS (
  SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) v FROM mart_daily_kpi
  UNION ALL SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) FROM mart_daily_revenue
  UNION ALL SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) FROM growth_daily_gmv
  UNION ALL SELECT CAST(SUM(actual_amount) AS DECIMAL(20,2)) FROM orders
    WHERE status IN ('paid','shipped','delivered')
)
SELECT CASE WHEN COUNT(*)=4 AND COUNT(v)=4 AND MIN(v)=MAX(v)
            THEN 'GMV_PATHS_AGREE' ELSE 'GMV_PATHS_DIFFER' END verdict,
       MAX(v) gmv FROM paths"

# 21 条主金标 + 3 条陷阱金标 SQL 是否都能执行（不烧 LLM）
python3 eval/run_eval.py --dry-run
python3 eval/run_eval.py --cases eval/cases_traps.json --dry-run
```

预期 verdict 为 `GMV_PATHS_AGREE`，并输出 `金标验证: 21/21 OK` 与 `3/3 OK`。
GMV 数值仍打印供人阅读，但不参与 PASS/FAIL；`COUNT(v)=4` 同时防止 NULL 被忽略后假通过。

四条路径分别走 mart 层、另一张 mart、派生层、基础表原始状态过滤，任意一条对不上说明
mart 重算或派生层口径出了问题。`--dry-run` 顺带验证了 `pg_to_redshift` 的运行时改写和
`rsql._scalar()` 的类型还原——它覆盖全部 21 条金标却不需要模型，性价比最高的一步。

## L6 服务与前端渲染契约

```bash
# 起后端（默认 redshift + Glue）
bash backend/run.sh &

# 后端自报的坐标必须是真的
curl -s http://127.0.0.1:8000/health | grep '"engine"'
# 期望 "engine": "Redshift Serverless"，且**没有** host/port（Data API 不走 TCP 坐标）

# UI 的元数据来源
curl -s http://127.0.0.1:8000/api/catalog | grep '"source"'
# 期望 "source": "glue"。若为 "information_schema" 说明 Glue 没读到（降级了但没静默）

# 前端渲染契约：本地动态 API + 线上 403 后静态快照，两条路径分开测
node scripts/ui/render_test.mjs http://127.0.0.1:8000
python3 scripts/deploy/check_catalog_freshness.py
node scripts/ui/render_test_prod.mjs
```

预期两条渲染测试输出 `全部通过 ✅`，新鲜度检查确认 48 张表行数与提交的
`consistency.redshift.postdatafix.json` 完全一致。它防止来源仍为 Glue、结构也正确，但内容是
重载前旧快照的静默漂移。`deploy_web.sh` 现在默认重建快照；显式 `--reuse-snapshot` 仍会跑此检查。

这一层要挡的是**「UI 显示的是不是真的」**。UI 原来把表数、行数、引擎名全写死在 HTML 与
i18n 字典里，数据从 19 万涨到 8000 万之后全错，而且**不会报警**——静态文本不会因为库变了
就失效。改成从 `/api/catalog` 动态读之后风险换了形态：接口字段改名、语言分支写错、
重渲染钩子漏挂，界面照样静默显示错的东西。

`render_test.mjs` 实际抓到过一个差 10 倍的 bug：行数缩写按 `n/1e4` 算却配英文单位 `K`，
7992 万渲染成 `7,992K`（≈799 万）。**单位错误比数字缺失危险**，因为它看起来是个正常数字。
所以它中英两条路径都验。

另外两个曾经出过事的点也在这一层：

- **存活探针的端点**。它曾被误改成 `GET /ask`，而 `/ask` 是 POST，GET 必然 405，
  `h.ok` 是 undefined → 每次都掉进 `catch` → 本地 demo 一直在放烘焙数据。界面标了
  "离线演示模式"没错，但提示是"后端未连接，启动后端"——后端明明起着，归因指错了方向。
- **`/health` 的后端坐标**。它曾无条件读 `db.PG`，于是 Redshift 模式下顶栏显示
  `127.0.0.1:5433`。坐标错了比不显示更糟，因为它看起来是对的。

`scripts/test_all.sh` 的 L6 会自己起后端、跑完自动关，不需要你先手动起服务。

## L7 端到端 agent

```bash
# smoke：一道最简 + 一道最难，约 75 秒
./backend/.venv/bin/python eval/run_eval.py \
  --case L1-users-count L5-repurchase-rate

# 全量 21 题，约 16 分钟
./backend/.venv/bin/python eval/run_eval.py

# 三条 level-6 陷阱题：验证不会选错派生/财务/临时 ROI 表
./backend/.venv/bin/python eval/run_eval.py --cases eval/cases_traps.json
```

预期 `通过 2/2` / `通过 21/21` / `通过 3/3`。陷阱报告写入
`eval/report.cases_traps.{md,json}`，不会覆盖 21 题的 `eval/report.{md,json}`。

⚠️ **全量跑会覆盖 `eval/report.md` 和 `report.json`**。基线归档在
`eval/baseline/eval.post-migration-redshift.*`，跑完 smoke 后想恢复完整报告：

```bash
cp eval/baseline/eval.post-migration-redshift.md  eval/report.md
cp eval/baseline/eval.post-migration-redshift.json eval/report.json
```

只改了文档、reconcile、或治理注释时不必跑全量——agent 路径没动，L5 的 21 条金标已经
覆盖了 SQL 层。改了 `knowledge/`、`backend/agent.py`、`metrics_def.py` 才需要全量。

## L8 负测：证明检查器该报的时候会报

这一层最容易被跳过，但**跑绿的检查器不等于有用的检查器**。本项目踩过两次假阳性
（卡片枚举行被当列名、`IS NOT NULL` 的 `is` 被当列名），反方向的假阴性同样要验。

下面七个注入用于覆盖 A–G，逐个做完再撤销。**先备份，别用 `git checkout` 恢复**——
工作树里有大量未提交改动，checkout 会一起冲掉：

```bash
B=/tmp/negtest && rm -rf $B && mkdir -p $B
for f in knowledge/domains/attribution/channels.md knowledge/connection.md \
         backend/metrics_def.py scripts/glue/reconcile.py \
         database/03_attribution_domain.sql database/redshift/data_classification.yaml; do
  mkdir -p "$B/$(dirname $f)" && cp "$f" "$B/$f"
done
```

| 类 | 注入方式 | 预期 finding |
|---|---|---|
| A | 把 `knowledge/connection.md` 里的 `meta_snapshot` 改个名 | `meta_snapshot：声明在 knowledge/connection.md 成文（CARD_EXEMPT），但该文件里找不到它——豁免已失效` |
| B | 在 `channels.md` 的**表结构**小节加一行 `\| ghost_col_b \| INT \| … \|` | `channels：卡片写了但 Glue 里没有的列 ['ghost_col_b']` |
| C | 在 `03_attribution_domain.sql` 的 `CREATE TABLE channels` 里加一列 `ghost_col_c INT,` | `channels：DDL 有但 Glue 没有的列 ['ghost_col_c']` |
| D | 把 `metrics_def.py` 里的 `is_repurchaser_30d` 改成 `ghost_col_d` | `mart_user_summary：指标 SQL 引用了表里没有的标识符 ['ghost_col_d']` |
| E | 在分类清单中把 `orders.order_no` 临时声明为 `mask` + `policy: ghost_policy` | `orders：挂了 DDM 的表却能被 GetTable 读到` |
| F | `--catalog 123456789012:no_such_catalog` | `Glue Catalog 里读不到任何表：尚未注册，或注册失败/无权限` |
| G | 从 `users.reviewed_columns` 删除 `email` | `users：新增列未审阅 ['email']` |

A–D 与 G 可以一次注入、一次跑完。同时要确认**只报 5 处**——
`channels.md` 的「字段枚举值」小节里那些 `paid` / `organic` / `google` 行
**不应该**被当成列名，这是假阳性回归。

F 不需要改文件，只换命令行参数。撤销：

```bash
for f in $(cd $B && find . -type f | sed 's|^\./||'); do cp "$B/$f" "$f"; done
python3 scripts/glue/reconcile.py --catalog 123456789012:analytics_agent_rs --governance-state --strict
git status --short          # 确认没有负测残留
```

---

## L9 数据质量人工审计（不进自动门禁）

```bash
backend/.venv/bin/python scripts/audit/run.py all --secret "$SEC"
```

它对真实仓库运行 32 条语句，从存量、闭环到分布现实性逐层展开。L5 的“漏斗像不像真实业务”、
“留存衰减是否合理”不能可靠压成机器 PASS/FAIL，因此 L9 明确要求人工阅读，不放进
`scripts/test_all.sh`。数据重载或生成器分布变化后必须运行；只完成 L0–L8 不代表已审阅真实
91.29M 行的数据形状。完整判读方法见 `docs/data-audit.md`。

---

## 改动 → 测试步覆盖矩阵

| 改动 | 被哪一步覆盖 |
|---|---|
| `scripts/gen/fillers.py`（分布生成） | L0 自测（含尺度不变性、Gini 区间） |
| `scripts/gen/ddl.py`（DDL 解析） | L2 `verify_ddl_vs_redshift` |
| `scripts/gen/pg_to_redshift.py` | L0 自测 + L5 金标 dry-run |
| `scripts/gen/arrow_types.py`（类型映射） | L3（COPY 成功且数值全等即反证映射正确） |
| `scripts/gen/tables.py`、`budget.py`、`main.py` | L3 一致性对账 |
| `scripts/redshift/rsql.py`（`_scalar` 类型还原） | L5 dry-run（DECIMAL 回 string 那个坑） |
| `scripts/redshift/rsql.py`（`split_statements`） | L1 建表/治理脚本执行时（含分号的 COMMENT） |
| `scripts/redshift/load_from_s3.py` | L3 |
| `scripts/consistency/snapshot.py` | L3 |
| `scripts/glue/register_catalog.py` | L1 |
| `scripts/glue/reconcile.py`、`database/redshift/data_classification.yaml` | L0 分类自测 + L2 + **L8 七类负测** |
| `database/redshift/01_tables.sql` | L2 |
| `database/redshift/02_mart.sql`、`03_derived.sql` | L5 四条 GMV 路径 |
| `database/redshift/04_governance.sql` | L4 |
| `backend/db.py`（`system_query` / `backend_info` / 默认后端） | L6 `/health` 断言 + L5 dry-run |
| `backend/catalog.py`（元数据装配、Glue 降级、行数不重复计） | L6 `/api/catalog` 断言 |
| `backend/server.py`（`/health` 修正、新增 `/api/catalog`） | L6 |
| `backend/run.sh`（Redshift 模式） | L6（test_all.sh 用同一套 env 起服务） |
| `web/index.html` 存活探针改回 `/health` | L6（探针端点可用性断言） |
| `web/index.html` 动态元数据渲染 + 治理面板 | L6 `render_test.mjs`（中英双路径） |
| `web/index.html` 展示用 SQL 的方言修正 | **无自动化覆盖**，人工核对（那是给观众看的字符串，不进数据库） |
| `eval/run_eval.py`（`_adapt_sql` / `--cases`） | L5 主/陷阱 dry-run + L7 陷阱实跑 |
| `scripts/audit/**` | L9 人工审计（无机器 PASS/FAIL） |
| `scripts/genlib/**` | 不覆盖：v1 legacy |
| `knowledge/**` 的 5 处幽灵列修正 | L2 reconcile 的 B 类为 0 |
| `database/05_marketing_domain.sql` 枚举修正 | L7（`L2-coupon-usage-rate`） |
| `docker-compose.cloud.yml` 钉住 `DB_BACKEND=postgres` | **无自动化覆盖**，改默认后端的连带修改，需人工确认那套部署仍能起 |
| `docs/**` | **无自动化覆盖**，人工评审 |

## 一键跑 L0–L6

```bash
bash scripts/test_all.sh              # 默认跳过 L3 的真实快照
bash scripts/test_all.sh --full       # 含 L3 真实快照（多花 3–5 分钟）
```

任一步失败立即 exit 1 并指出是哪一层。L7 / L8 / L9 不在里面：L7 烧 token，L8 临时
改文件，L9 需要人判断分布现实性，都该有人看着跑。
