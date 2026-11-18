# 基线归档

> ## 当前有效的基线只有 eval 这一条
> 
> | 类型 | 文件 | 状态 |
> |---|---|---|
> | **eval**（语义退化） | `eval.lakehouse-athena.post-funnel-retention-fix.md` / `.json` | **现行基线**（漏斗与留存口径修完，**27/27**，含新增的 `L5-retention-cohort`） |
> | eval（历史） | `eval.lakehouse-athena.post-anchor-fix.*`（26/26，但 `L4-funnel` 是照**错**金标判过的，见下）、`eval.lakehouse-athena.md`（同架构、锚点修之前）、`eval.pre-migration.*`、`eval.post-migration-redshift.*`、`eval.post-datafix-redshift.*` | 保留，通过率与现行**可比** |
> | **consistency**（搬迁损耗） | `consistency.*.json` | **已退役归档**，不再取新快照，见下文「为什么不再存一致性基线」 |
> | reconcile（文档漂移） | `reconcile.glue.md` | 历史（v2 Redshift federated catalog）。现行对账在 `scripts/lakehouse/reconcile.py` |
>
> 两类基线的性质根本不同，这决定了哪一类能留：
>
> - **eval 的期望值是运行时从数据现算的**（`cases.json` 里存的是 golden SQL，不是数），
>   所以换数据集之后通过率仍然可比。它是可以跨版本传下去的基线。
> - **consistency 存的是绝对值**。数据一换，基线里的数字全成历史，而**比较仍然会"通过"
>   或"失败"**，唯独不会说"我参照的那批数据已经不存在了"。基线会过期，而且过期时是绿的。

---

## 现行：漏斗与留存口径修完之后（Athena + S3 Tables，2026-08-23）

| 文件 | 内容 |
|------|------|
| `eval.lakehouse-athena.post-funnel-retention-fix.md` | 全量 eval：**27/27（100%）**，模型 `global.anthropic.claude-opus-4-8`，平均 48.3s/题 · 读文档 2.6 次 · SQL 0.8 条 |
| `eval.lakehouse-athena.post-funnel-retention-fix.json` | **同一次跑**的机器可读版（`meta.ts` 与 `.md` 一致，可逐题核对） |

逐层：L1 7/7 · L2 6/6 · L3 7/7 · L4 4/4 · L5 3/3（新增 `L5-retention-cohort`）。

**这一轮和上一轮的 26/26 不是同一个意思**，差别不在通过率而在判分对象：

- `L4-funnel` 的判定理由从「命中 golden[all-time **distinct users**] 4/4 步」变成
  「命中 golden[all-time **subset funnel**] 4/4 步」。上一轮那个 golden 的口径是每步各
  `count(DISTINCT user_id)` 一遍（394/385/396/373，结算 396 > 浏览 394，根本不是漏斗），
  agent 照它答「漏斗几乎不衰减」也判过了。**通过率一样，判的是两件事。**
  这一轮它还多读了一篇文档（3 → 4），是新加的方法卡路由生效了。
- 新增 `L5-retention-cohort`，判分模式 `retention`：**先验结论、再比数值**。
  它是唯一一道判分器会因为"结论写错"而判错的题——数值全对但把这份数据的 P0 缺陷
  报成「曲线平稳、留得住」时必须红。这一轮的实际理由是
  「结论已声明数据限制；命中 golden[weekly matrix pct (full-window cohorts)] 16/16 个数值」。

其余 25 题状态与理由全部未变。读文档次数有几处小漂移（`L1-dau-latest` 3 → 0、
`L5-repurchase-rate` 0 → 1、`L5-wow-gmv` 2 → 3、`L3-top-products-gmv` 5 → 4、
`L4-arpu-by-channel` 5 → 4），通过率与判定理由都没动——渐进式披露的读取路径本来就有
运行间抖动，**它不是判定依据**，列出来只是免得下次有人把它当回归。

---

## 现行：锚点/全量口径修完之后（Athena + S3 Tables，2026-08-20）

| 文件 | 内容 |
|------|------|
| `eval.lakehouse-athena.post-anchor-fix.md` | 全量 eval：**26/26（100%）**，模型 `global.anthropic.claude-opus-4-8`，平均 51.9s/题 · 读文档 2.6 次 · SQL 0.9 条 |
| `eval.lakehouse-athena.post-anchor-fix.json` | **同一次跑**的机器可读版（`meta.ts` 与 `.md` 的运行时间一致，可以逐题核对） |

逐层：L1 7/7 · L2 6/6 · L3 7/7 · L4 4/4 · L5 2/2。与上一轮同架构、同数据集、同金标，
差别只在 `backend/agent.py` 的系统提示词：统一业务日历锚点 + 「问句没点明时间范围时按全量算」。

这一轮的价值不在通过率（两轮都是 26/26），在于**中间那次红**。锚点改完重跑时
`L3-refund-total`（「这段时间一共退了多少钱」）从 ✅ 变 ❌：agent 把没点明范围的问题默认成
「近 30 天」，答 28.11 万，而全量是 96.36 万——差 3.4 倍。它 0 条 SQL、0 次读文档
（走 `call_metric`），所以连"SQL 写错了"这种线索都没有；答案里还大方写着「我按近 30 天来算」。
修在提示词里，并且必须连着写第二条：**全量 ≠ 不加日期条件**，比值类要补 `dt <= 锚点` 的上界，
否则全量 CAC 会拿铺到 2026-09-01 的成本去除一段根本没有新客的日子，
把本来过的 `L3-cac-overall` / `L4-cac-lowest-channel` 弄错。

所以这两轮基线放在一起看才完整:**两个 26/26 之间藏着一次真回归**。
只留最新那一份，就看不出这里曾经有个口径缺口。

---

## 上一轮：湖仓 eval 基线（Athena + S3 Tables，2026-08-19）

| 文件 | 内容 |
|------|------|
| `eval.lakehouse-athena.md` | 全量 eval：**26/26（100%）**，模型 `global.anthropic.claude-opus-4-8`，平均 52.7s/题 · 读文档 2.4 次 · SQL 1.0 条 |
| `eval.lakehouse-athena.goldens-dryrun.json` | **不是**上面那次跑的机器可读版，见下文「这一轮的 JSON 为什么缺」 |

逐层：L1 7/7 · L2 6/6 · L3 7/7 · L4 4/4 · L5 2/2。

### 用例从 21 条长到 26 条

新增 5 条，全部围绕「治理层官方指标 vs agent 自己写 SQL」这条缝：

| 用例 | 考什么 |
|---|---|
| `L3-refund-total` | 退款口径。正确指标名是 `refund_amount`，轴建在 `refunded_at` 上而不是下单日 |
| `L3-channel-cost-total` | 真实花费要从 `channel_daily_costs` 明细取，且该表的时间列是 **`date`** 不是 `dt` |
| `L3-cac-overall` | 全量 CAC。成本必须限制在业务日历轴内，否则把 2026-09-01 的成本算进来 |
| `L4-cac-lowest-channel` | 只有 `paid` + `kol` 两类渠道有成本，"最便宜的渠道"不能把 NULL 渠道算进去 |
| `L4-roi-cac-by-channel` | ROI 与 CAC 同锚点、带上界，按渠道切 |

这 5 条里有 4 条 agent 读了 **0 篇文档、发了 0 条 SQL**——它直接调 `call_metric` 拿治理层
口径。这正是想要的行为：**有官方口径的指标不该让 agent 现推**。这 4 条同时也是在验
`metrics_def.py` 里的口径本身对不对，因为 agent 完全没有自己的算法作为对照。

### 跨版本可比的部分与不可比的部分

**可比**：通过率。pre-migration 21/21 → post-migration 21/21 → post-datafix 21/21 →
lakehouse 26/26 → lakehouse post-anchor-fix **26/26**。

**不可比**：耗时与读文档次数。上三条基线跑在 21 万用户 / 8000 万行的 v2 数据上，
这一条跑在仓库自带的 `data/csv/` 种子数据上（48 张表约 22 万行），引擎也从 Redshift
Data API 换成 Athena。52.7s/题 高于 post-datafix 的 40.9s，**不能读成"agent 变慢了"**——
Athena 每条查询有固定的排队与元数据开销，跟数据量关系不大，跟引擎关系很大。
要横向比性能，只能在同一引擎、同一数据集上比。

### 这一轮的 JSON 为什么缺

全量跑（17:52）产出了 `report.md` 和 `report.json`。**六分钟后一次 `--dry-run` 把
`report.json` 覆盖掉了**：旧版 `run_eval.py` 两种模式写同一个文件名，而 `--dry-run`
只写 json 不写 md。结果是 `report.md` 还是 26/26 的全量结果，`report.json` 已经变成
26 条 `golden_ok` 记录（没有 `ok` 字段、没有耗时），两个文件都存在、都看着正常，
但说的不是同一件事。

处理方式：

- 全量跑的 JSON **没有保留**，本目录只有 `.md`。不去伪造一份。
- 那份 dry-run 产物按它真实的内容改名为 `eval.lakehouse-athena.goldens-dryrun.json`
  留着——它本身是有效证据：**26 条金标 SQL 在 Athena 上全部可执行**（Trino 方言、
  catalog 路由、时间锚点都对），只是不含 agent 的判定。
- `run_eval.py` 已改：`--dry-run` 写 `report.dryrun.json`，不再碰 `report.json`。

这件事本身是本项目那个失效模式的又一例：**没有报错，文件都在，只是其中一个悄悄
换了含义**。所以记在这里而不是直接补跑掩盖过去。

**这个缺口已经在下一轮补上**（不是回填这一轮）：2026-08-20 那次全量的
`.md` 与 `.json` 是同一次跑的产物，`meta.ts` 与 `.md` 头部的运行时间一致，
所以现在有一份可以逐题核对的机器可读基线。这一轮的 `.md` 保持原样孤零零地放着——
它记的就是"当时只剩 md"这个事实。

再存一轮基线的做法（约 23 分钟、烧 LLM token）：

```bash
./backend/.venv/bin/python eval/run_eval.py
cp eval/report.md   eval/baseline/eval.lakehouse-athena.<这一轮改了什么>.md
cp eval/report.json eval/baseline/eval.lakehouse-athena.<这一轮改了什么>.json
```

**用新文件名，不要覆盖旧的**：两轮之间的差别（比如上面那次退款口径回归）只有把
两份放在一起才看得见，覆盖掉就只剩一句"一直是 26/26"。

---

## 为什么不再存一致性基线

`scripts/consistency/snapshot.py` 那套做法是把一批指标的**绝对值**存成 JSON，下次跑再比。
问题不在它当时不好用（v2 搬迁时它抓的正是 COPY 丢行、类型掉精度、时区偏移这类损耗），
在于它的产物**会过期，而且过期时是绿的**。

现行 L3 换成 `scripts/lakehouse/verify_load.py`：`data/csv/` 真源与 Athena 现查**两边都
当场算**，没有会过期的中间文件。想验的东西没变（装载有没有丢行、有没有掉精度），
只是参照物从"归档的数字"变成"当场重算的真源"。

`consistency.*.json` **保留不删**，因为还有两处在引用它们：

- `scripts/gen/budget.py` 的 `BASE` 表 —— 每张表的"当前实测行数"注明来源是
  `consistency.postgres.json`，缩放倍数是相对这批数算的。删了基线，那些数字就没有出处了。
- `docs/test-plan-v2.md` —— v2 验收流程的原文里有 `--compare` 的完整命令（历史文档，正文不改）

它们现在的身份是**历史归档**，不是可比基线。别拿 `--compare` 去跑它们判断当前数据对不对。

### 归档清单（v2 Redshift 时期，2026-08-03 / 08-06）

| 文件 | 内容 |
|------|------|
| `consistency.postgres.json` | v1 Aurora/Postgres 快照：48 张表、475 项指标 |
| `consistency.generator-expected.json` | 生成器在生成时吐出的预期值（21 张事实表） |
| `consistency.redshift.json` | 8000 万行落库后的 Redshift 快照 |
| `consistency.generator-expected.postdatafix.json` | 数据修复后的生成侧预期值 |
| `consistency.redshift.postdatafix.json` | 数据修复后重灌的快照（48 张表、471 项指标） |

最后一次对账结果：471 项指标 2 处差异，均为 `user_profiles.birth_date`——生成侧是原始
日期，快照侧被 masking policy 按设计跳过。非搬迁丢数据。
（**注**：那套 DDM 值级脱敏是 v2 Redshift 的能力。v3 治理层用的是 Lake Formation
列级排除——`birth_date` 不在 agent 角色的授权面里，所以那种「列在、值被替换」的差异
在 v3 不会出现：这一列对 agent 干脆不可见。见 `scripts/lakehouse/governance.py`。）

---

## 历史：eval 基线三代（Redshift 时期）

| 文件 | 内容 |
|------|------|
| `eval.pre-migration.md` / `.json` | v1 Aurora/Postgres 上的全量 eval：21/21 |
| `eval.post-migration-redshift.md` / `.json` | 搬到 Redshift + 8000 万行之后：21/21，与迁移前持平 |
| `eval.post-datafix-redshift.md` / `.json` | 生成器重写、数据修复重灌之后：21/21，平均 40.9s/题 |

post-datafix 那一轮的意义不在通过率（本来就满分），而在 `L4-funnel`、`L5-repurchase-rate`
这类题从「数字碰巧对」变成「业务形状也对」——漏斗单调递减、留存单调衰减、等级与消费真实
相关。数据缺陷清单见 `docs/data-audit.md`（该文顶部有横幅说明它描述的是 v2 那批数据）。

### 本项目在 eval 自身上修过的两处（2026-08-06，仍然有效）

1. **金标 SQL 不要自带舍入**。`L2-coupon-usage-rate` 旧金标写 `round(...,1)`，核销率
   49.7% 时舍入粒度只占 0.04%，数据修复把核销率压到 3.34% 后同样的 0.1 粒度放大成 3%，
   超过 0.5% 默认容差，把 agent 的正确答案判成错。**舍入粒度是绝对的、容差是相对的，
   数值变小时两者会打架**：金标输出精确值，宽容度全部交给 judge 容差。
2. **infra 签名重试**。连跑 40+ 次 agent 时每轮随机 1~2 题被瞬时限流砸中（签名：一条
   SQL 都没发、7~15s 即返回）。重试只在 `n_sql == 0 且无结果集` 时触发一次；语义性
   答错必然带 SQL，不会触发，评测口径不变。

---

## 历史：Glue 元数据对账（2026-08-04）

`reconcile.glue.md` —— v2 时期接上 Redshift federated catalog 之后的三方对账结果，
六类检查全绿。

**复现命令已失效**：`scripts/glue/register_catalog.py` 与 `scripts/glue/reconcile.py`
是 Redshift 路径，已随 Redshift 退役成为死路径。现行等价物是：

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
python3 scripts/lakehouse/reconcile.py \
  --catalog "$ACCT:s3tablescatalog/analytics-agent-tables" --strict
```

对账这条基线的性质跟前两条不同，也是这次架构升级真正新增的能力：eval 和 consistency
都得「跑一遍」才知道有没有问题，reconcile 是静态的、秒级的，可以直接挂 CI 门。
现在它有三条独立路径（都在 `scripts/test_all.sh` 的 L2 里）：

| 脚本 | 抓什么 |
|---|---|
| `scripts/lakehouse/gen_ddl.py --check` | 声明态：`database/*.sql` ⟷ 生成的 Iceberg DDL 逐字节一致 |
| `scripts/lakehouse/reconcile.py --strict` | 表与列：DDL ⟷ Glue ⟷ 卡片三方 |
| `scripts/lakehouse/verify_enums.py` | **列里的值**：卡片写的枚举取值 ⟷ Athena 实际取值 |

第三条是新加的。前两条管不到枚举漂移——卡片写 `status='active'` 而数据里是 `'on_sale'`
时，语法正确、目录对账全绿、跑出来是空集，**结论直接反过来**。
