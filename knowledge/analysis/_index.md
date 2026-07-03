# 分析方法库 (Analysis Playbook) — 路由索引

> 资深分析师的可复用分析套路。做判断/诊断题时，先按问题类型 read_doc 对应方法文档，
> 按它的套路展开（多角度连发查询 + 结构化 findings），而不是只给"一个数+一句话"。

| 问题类型关键词 | 读哪个方法 |
|---|---|
| 为什么涨/跌、归因、谁带动、收入波动、拆成X×Y | `analysis/ratio_decomposition.md`（比率拆解/驱动归因） |
| 哪些渠道/品类贡献最大、二八、集中度、增长来自谁 | `analysis/contribution_pareto.md`（贡献度+帕累托） |
| 趋势、最近怎么样、环比同比、有没有异常 | `analysis/trend_anomaly.md`（趋势+异常检测） |
| 转化率、流失在哪、漏斗、注册→付费 | `analysis/funnel_analysis.md`（漏斗） |
| 留存、第7天还在吗、cohort、粘性 | `analysis/retention_curve.md`（留存曲线） |

用法：判断/诊断/深度分析题 → 读对应方法 + 相关 mart 表卡片 + metrics/governed_metrics.md →
能用 `call_metric` 的官方指标优先调用 → 按方法的 SOP 多角度连发查询 →
**present_result 用 charts(复数)给 2-3 个图**（趋势+拆解+占比，深度分析别只给一张图）→
带 findings（driver/anomaly/risk，每条要有数据支撑）。

**每个方法文档里都有"统计公式"小节**（异常检测的 Z-score/3σ、比率拆解的中点分解、帕累托的累计占比/HHI、
漏斗的分步转化、留存的 Rₜ）。深度分析题**必须**把用到的方法名/步骤/公式/算出的统计量填进
`present_result.method`（{name, steps, formula, stats}），前端会在结果最上方展示——让人看到我们
"在分析方法上确实做了干预"，而不是只吐一个数。公式照文档抄、统计量用你真算出来的。
