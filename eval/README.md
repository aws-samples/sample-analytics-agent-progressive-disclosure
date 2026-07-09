# Eval Harness — 自动评测 text-to-SQL 准确率

这个目录把 `test_questions.md` 的人工清单变成**可自动运行的评测**：驱动真实 agent
（`backend/agent.py`，读文档 → 写 SQL → present_result），把它的查询结果与「金标 SQL」
现算出的期望值自动比对，产出通过率报告。

项目的核心主张——**文档路由（progressive disclosure）比塞全 schema 更准更省**——
以前只有叙述；这个 harness 把它变成可复现实验的数字。

## 用法

```bash
# 前置：本地库已就绪（scripts/localpg/up.sh + load.sh），Bedrock 凭证可用
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cd ../eval
../backend/.venv/bin/python run_eval.py --dry-run     # 先验金标 SQL（不调模型，秒级）
../backend/.venv/bin/python run_eval.py               # 全量 21 题（每题 25~70s）
../backend/.venv/bin/python run_eval.py --level 1 2   # 只跑 L1/L2
../backend/.venv/bin/python run_eval.py --case L4-funnel
```

产出：`report.md`（通过率汇总 + 逐题明细 + 失败题的 agent SQL）与 `report.json`
（原始记录，供跨配置横向对比）。

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
| `funnel` | 漏斗题 | 各步骤数值命中，允许漏 1 步 |

判分对象是 agent 的**全部数值/文本证据**：`run_sql` 的每个结果集、`call_metric` 的
权威数、`present_result` 的 kpis 和 chart 标签。这样"SQL 对但只在 KPI 卡片里展示"
不会被误判为错。

### 隐含考点
用例继承了 `test_questions.md` 的口径陷阱（`trap` 字段有注明），典型的三个：
- **时间锚点**：静态样本数据（至 2026-01-24），"最近 N 天"必须以 `max(时间列)` 为锚，
  用 `current_date` 会查出空——L1-dau、L2-top-pages、L3-gmv 都在考这个。
- **有效订单状态**：GMV 类题必须 `status IN ('paid','shipped','delivered')`。
- **事件名以表文档为准**：漏斗题的真实事件名（`view_product`/`begin_checkout`/
  `purchase`）与知识库 `metrics/core_metrics.md` 示例里的名字不一致——**读对表文档
  的枚举值**才写得对，这正是 progressive disclosure 要证明的能力。

### 附带度量
除对错外每题还记录：**耗时、read_doc 次数、SQL 条数**。要做「文档路由 vs 无路由」的
对照实验，跑两种配置各生成一份 `report.json` 对比这三列即可（无路由配置可把系统提示里
的强制读文档工作流去掉、或直接给全量 schema）。

## 已知边界

- 判分基于数值/文本证据匹配，是**充分不必要**判定：极端情况下 agent 靠巧合数字蒙对
  会误判为过（数值题都带小数容差，概率很低）；insight 文本质量不在评测范围。
- `run_eval.py` 顺序执行（每题一个新 session），21 题全量约 15~25 分钟、
  每题一次 Opus 多轮调用，注意 Bedrock 费用。
- 用例锚定 schema 而非行数据的具体值，但 `value_hint` 是按当前 CSV 写的注释，
  重新生成数据后 hint 会过时（不影响判分）。
