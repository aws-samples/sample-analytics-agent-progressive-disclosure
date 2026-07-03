# 分析方法 · 留存曲线 / Cohort (Retention)

> 套路：按"注册周期"分组(cohort)，看每组用户在第 N 天/周后还回来多少，画留存曲线/热力图。
> 留存是产品健康度的核心，比单看 DAU 更能反映粘性。

## 何时用
"留存怎么样""新用户第7天还在吗""哪批用户留得好""cohort 对比"等。

## 统计公式（Cohort 留存，请照此算并填进 method）
设某 cohort 初始人数 N₀，第 t 天仍活跃人数 Nₜ：
- **第 t 天留存率**：Rₜ = Nₜ / N₀ —— 留存曲线纵轴。
- **常看点**：R₁（次日）、R₇（7日）、R₃₀（30日）。
- **区间流失率**：第 t→t+1 的流失 = (Nₜ − Nₜ₊₁) / Nₜ。
- **断崖点**：Rₜ 相邻两期跌幅最大的地方（通常在 D1，反映激活质量）。
> 曲线一般前陡后平；若长期趋于水平线，那条渐近线 ≈ 产品的"忠实用户占比"。

## 做法（这几步填进 present_result.method.steps）
1. **定义 cohort**：按 users.registered_at 的周(或日)分组。
2. **定义"回来"**：该用户在注册后第 N 天/周有 events 活跃（或有订单，看问题）。
3. 算每个 cohort 在第 0/1/7/14/30 天的留存率 Rₜ = 该期活跃数 / cohort 初始人数（把 R₁/R₇/R₃₀ 填进 method.stats）。
4. 也可用 mart_user_summary 的 last_active_date 做粗略"是否近期活跃"判断。
5. 找规律写 `findings`：哪批 cohort 留得最好/最差、留存曲线在第几天断崖（跌幅最大处）。

## 输出要求
- chart 优先用 `heatmap`：xLabels=第N天/周, yLabels=各 cohort, cells=留存率。
  也可用 line 画几条代表性 cohort 的留存曲线对比。
- KPI：次日留存、7日留存、30日留存（整体）。
- insight 点出留存断崖位置和最佳/最差 cohort。
- 注意：近期注册的 cohort 还没到第30天，**留存窗口不完整不能和老 cohort 比**，放进 risk finding。
