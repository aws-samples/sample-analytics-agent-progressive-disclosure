# Eval Harness — 自动评测 text-to-SQL 准确率

这个目录把 `test_questions.md` 的人工清单变成**可自动运行的评测**：驱动真实 agent
（`backend/agent.py`，读文档 → 写 SQL → present_result），把它的查询结果与「金标 SQL」
现算出的期望值自动比对，产出通过率报告。

项目的核心主张——**文档路由（progressive disclosure）比塞全 schema 更准更省**——
以前只有叙述；这个 harness 把它变成可复现实验的数字。

## 用法

```bash
# 前置：湖仓数据层已就绪（scripts/lakehouse/，见 docs/deployment.md），Bedrock 凭证可用
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 从仓库根目录跑（默认 DB_BACKEND=athena，与 agent 走同一条只读边界）
./backend/.venv/bin/python eval/run_eval.py --dry-run   # 先验金标 SQL（不调模型，约 1 分钟）
./backend/.venv/bin/python eval/run_eval.py             # 全量 27 题（每题 35~75s）
./backend/.venv/bin/python eval/run_eval.py --level 1 2 # 只跑 L1/L2
./backend/.venv/bin/python eval/run_eval.py --case L4-funnel
./backend/.venv/bin/python eval/run_eval.py --cases eval/cases_traps.json  # 口径陷阱题 3 题
```

`--cases` 指哪份用例集（默认 `cases.json`）。第二份是 **`cases_traps.json`**（3 题，见下
「口径陷阱题」），它的报告写到 `report.traps.md` / `report.traps.json`，不覆盖主用例集的。

产出：`report.md`（通过率汇总 + 逐题明细 + 失败题的 agent SQL）与 `report.json`
（原始记录，供跨配置横向对比）。`--dry-run` 写的是 **`report.dryrun.json`**，不碰上面
两个——两种模式的记录不同构（dry-run 只有 `golden_ok`，没有判定结果和耗时），
共用一个文件名会让 md 和 json 悄悄脱钩（这事真发生过，见
`baseline/README.md` 的「这一轮的 JSON 为什么缺」）。

⚠️ **全量跑会覆盖 `report.md` 和 `report.json`**。现行基线归档在
`eval/baseline/eval.lakehouse-athena.post-funnel-retention-fix.md`+`.json`（27/27）；
上两轮 `eval.lakehouse-athena.post-anchor-fix.*`（26/26）与 `eval.lakehouse-athena.md`
（锚点修之前，也是 26/26）**并存保留**。三轮放在一起才读得出两件事：前两轮之间有过一次
真回归（退款题被默认成近 30 天），而**第二轮到第三轮通过率没变、判分对象变了**——
`L4-funnel` 上一轮命中的是口径写错的那条 golden。只留最新那份，这两处都看不见。

`report.md` / `report.json` 现在是 **2026-08-23 那次 27/27** 的原件(同一次跑生成,md 与 json 同构)。
`report.dryrun.json` 是 `--dry-run` 的产物,按真实内容命名——早先两种模式共用过文件名,
结果一次 dry-run 把全量跑的 json 覆盖掉,md 和 json 说了两件不同的事。
**别用 `git checkout eval/report.json` 把它"恢复"**:仓库里那一份是 Redshift 时代 21 题的结果。

## 设计

### 金标是"口径"，不是写死的数
每题的期望答案写成 1..N 条 **golden SQL**（`cases.json`），运行时经与 agent 完全相同的
只读边界（`backend/db.py`）现算。数据重新生成后金标自动跟着变，用例不用改。

一题给多条 golden 是**故意的**：很多问题存在多个同样合理的口径（GMV 用
`actual_amount` 还是 `total_amount`；设备分布按设备行数还是去重用户数；"帖子数"含不含
草稿）。agent 命中**任一**可接受口径即判对——评的是"没踩口径陷阱"，不是"猜中出题人偏好"。

### 判分模式（`judge.mode`）
| mode | 适用 | 规则 |
|---|---|---|
| `scalar` | 单数值题（用户数、GMV、比率） | agent 的任何数值证据落进容差内（`unit:"pct"` 同时接受 62.4 与 0.624） |
| `set` | 分布/清单题（性别分布、渠道列表） | 键集合 ≥80% 命中；`val_col` 指定时对应数值也须 ≥80% 命中 |
| `toplist` | 排行题（Top10 页面、Top5 帖子） | 前 K 名命中率 ≥ `min_hit`（默认 60%，模糊排行题放宽） |
| `pair` | 两值对比题（有券 vs 无券客单价、周环比） | 两个值都须命中 |
| `funnel` | 漏斗题 | **先验形态**：交付的 funnel 图必须单调不增，非单调直接判错（理由点明形态，不是"数值没命中"）；再比各步骤数值，允许漏 1 步 |
| `retention` | 留存题（`L5-retention-cohort`） | 除数值外还有一道**结论闸**：这份数据算不出真实留存（活跃度与注册生命周期独立抽样），交付里必须声明这条数据限制，只解释右删失不算 |

判分对象是 agent 的**全部数值/文本证据**：`run_sql` 的每个结果集、`call_metric` 的
权威数、`present_result` 的 kpis 和 chart 标签。这样"SQL 对但只在 KPI 卡片里展示"
不会被误判为错。

### 隐含考点
用例继承了 `test_questions.md` 的口径陷阱（`trap` 字段有注明），典型的三个：
- **时间锚点**：静态样本数据（至 2026-01-24），"最近 N 天"必须以 `max(时间列)` 为锚，
  用 `current_date` 会查出空——L1-dau、L2-top-pages、L3-gmv 都在考这个。
- **有效订单状态**：GMV 类题必须 `status IN ('paid','shipped','delivered')`。
- **事件名以表文档为准**：真实事件名是 `view_product` / `begin_checkout` / `purchase`，
  而凭常识容易写成 `product_view` / `checkout`——后者在这份数据里**一行都查不到，
  也不报错**，漏斗直接算出全零。枚举取值只写在 `knowledge/domains/behavior/events.md`
  里，**读对表文档**才写得对，这正是 progressive disclosure 要证明的能力。
  （卡片里的枚举取值本身由 `scripts/lakehouse/verify_enums.py` 盯着，防止它自己漂移。）

### 口径陷阱题（`cases_traps.json`，3 题）
和 `cases.json` 同构、同一套判分器，但考的不是"算得对不对"，而是**会不会掉进另一张
长得很像的表**——这是这套架构上最典型的错法：不报错、数看着合理、口径是别的。

| id | 问题 | 正确的源 | 掉进去会怎样 |
|---|---|---|---|
| `TRAP-total-orders` | 总订单数 | `orders` 全表 | `dwd_orders_valid` 只含有效订单，数偏小 |
| `TRAP-fin-net-revenue-dec` | 某月财务口径净收入 | `fin_daily_revenue.net_revenue` | 拿 GMV 口径顶：没减退款，且轴是下单日不是退款发生日 |
| `TRAP-roi-tmp-table` | 各渠道 ROI 排名 | `mart_channel_daily` 现算 | `tmp_campaign_roi_analysis` 的 `attributed_gmv` / `roi` **整列 NULL**（登记在 `scripts/lakehouse/verify_constants.py`），排名全 NULL |

这份用例集**不进默认全量跑**（它问的是判断题、判分靠数值证据，误判成本比主用例集高），
是一份需要时手动跑的补充集。结构与"金标不许直接查陷阱表"这两件事由
`run_eval.py --selftest` 第 10 组盯着（L0 就会红），所以文件坏了不必等一次带模型的跑。

### 附带度量
除对错外每题还记录：**耗时、read_doc 次数、SQL 条数**。要做「文档路由 vs 无路由」的
对照实验，跑两种配置各生成一份 `report.json` 对比这三列即可（无路由配置可把系统提示里
的强制读文档工作流去掉、或直接给全量 schema）。

## 已知边界

- 判分基于数值/文本证据匹配，是**充分不必要**判定：极端情况下 agent 靠巧合数字蒙对
  会误判为过（数值题都带小数容差，概率很低）；insight 文本质量不在评测范围。
- `run_eval.py` 顺序执行（每题一个新 session），27 题全量约 23 分钟（实测平均
  51.9s/题）、每题一次 Opus 多轮调用，注意 Bedrock 费用。
- 用例锚定 schema 而非行数据的具体值，但 `value_hint` 是按当前 CSV 写的注释，
  重新生成数据后 hint 会过时（不影响判分）。
