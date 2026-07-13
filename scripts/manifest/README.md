# schema manifest — 表数轴扩展的单一真源

## 动机

35 张表的 schema 全塞进 context 也就几千 token，progressive disclosure 的价值证明不充分；
enterprise 真实库有一两百张表，塞不进去，"读对文档才查得对"才从演示变成刚需。
**表数轴（35 → 100-200 张）是这个 demo 说服力的主要短板。**

企业的表长到几百张，靠的不是几百个业务，而是**同一实体在不同层、不同粒度、
不同口径、不同新鲜度下的重复存在**。所以扩表的正确姿势不是发明 165 张新业务表，
而是复刻这种"结构性相似"——它同时正好是对 agent 选表能力的四类考点：

| 相似性 | layer | 考点 | 试点表 |
|--------|-------|------|--------|
| 清洗副本（字段几乎同名） | `dwd` | 选原始表还是清洗表 | `dwd_orders_valid`、`dwd_events_app` |
| 预聚合（粒度各异） | `dws` | 粒度对不对 | `dws_user_daily`、`dws_channel_weekly` |
| 部门口径分叉（数字对不上是故意的） | `ads` | 口径归属路由 | `fin_daily_revenue` vs `growth_daily_gmv` |
| 企业噪音（backup/tmp/废弃） | `noise` | 读文档才知道别用 | `orders_backup_20251201`、`tmp_campaign_roi_analysis` |

## 为什么要 manifest

200 张表如果 DDL、知识卡片、域索引、eval 陷阱各写各的，一定失控。
`schema_manifest.yaml` 每表一条记录（层、粒度、owner、use_when/avoid_when、CTAS SQL、
字段表、坑），`render.py` 生成全部下游，**加一张表 = 加一条 YAML 记录**：

```
schema_manifest.yaml ──render.py──▶ database/10_derived.sql            （CTAS DDL）
                                 ├▶ knowledge/domains/<域>/<表>.md      （数据字典卡片）
                                 ├▶ knowledge/domains/<域>/_index.derived.md（域内消歧路由）
                                 └▶ knowledge/domains/_derived_overview.md （派生层总览）
```

一致性由生成保证：卡片里的"何时用/何时别用"、索引里的"选哪张"、DDL 里的口径，
全部出自同一条记录。`eval_trap` 字段登记每张表设计要考什么，是 eval 出题的素材库。

## 用法

```bash
pip install pyyaml   # scripts/requirements.txt 已含

python3 scripts/manifest/render.py --check   # 只校验（CI 可用）
python3 scripts/manifest/render.py           # 生成全部产物

# 装载：load.sh 已挂 10_derived.sql（在 09_mart 之后）
scripts/localpg/load.sh
```

约定：
- 生成物都带 "generated" 头，**不要手改**；改表就改 manifest 重跑。
- 手写资产（35 基表卡片、mart 卡片、各域 `_index.md`）渲染器一概不碰。
  域索引通过结尾一段固定文案链接到 `_index.derived.md`（一次性接线，render 会提示检查）。
- deprecated 表必填 `replaced_by`，卡片自动生成 ⛔ 横幅——噪音表"能查到但不该用"的
  考点落在这里。

## 试点验证（本分支）

8 张试点表已端到端跑通：render → DDL 在本地库执行 → 行数与交叉校验通过——
- `growth_daily_gmv.sum(gmv)` == `mart_daily_revenue.sum(gmv)`（8,571,786.74，同口径互证）
- `fin_daily_revenue` 12 月净收入 2,438,362 vs 增长口径 GMV 2,829,581——**分叉是设计使然**
  （差 = 运费 + 退款 + paid_at/placed_at 记账日差异）
- `orders_backup_20251201` 只有 765 行（< 2025-12-01），拿它算"最近 30 天"得空结果

## 裂变路线图（后续 PR）

- **P1a 系统裂变**：把 8 张试点扩到 ~60 张派生表（每域 1-2 张 DWD、每主题日/周/月 DWS、
  4-6 对 ads 口径分叉、8-10 张噪音），35 基表 + 4 mart + 派生层 ≈ 100-110 张。
  manifest 格式不变，纯增量记录。
- **P1b eval 联动**：按 `eval_trap` 字段出 wrong-table 题（"总订单数"用 dwd 会错、
  "财务净收入"用 growth 口径会偏高、ROI 题掉进 tmp 表 NULL 列）；
  跑 routing vs no-routing 对照——200 表 schema 塞不进 context，
  对照组预期显著掉分，差值就是 progressive disclosure 的量化价值。
- **P2 行数轴**：genlib 已在库（`scripts/genlib/`），把 events/orders 放大到千万级，
  激活 `_registries/_partitions/_indexes` 骨架。
- **P3 第二场景**：交易所 schema（8 域 32 表、5000 万行预算、锚定真实 API 的口径陷阱），
  与电商场景构成"表多" vs "行深"两个互补 benchmark。
