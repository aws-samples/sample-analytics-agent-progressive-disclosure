# 迁移基线（pre-migration baseline）

Redshift + Glue 迁移的验收参照物。**动数据/动存储之前取的**，迁移后重跑对比。

| 文件 | 内容 | 迁移后是否可比 |
|------|------|----------------|
| `eval.pre-migration.md` / `.json` | eval harness 全量结果：21/21，模型 `global.anthropic.claude-opus-4-8` | **可比**。金标是 golden SQL 运行时现算的口径，不是写死的数；数据重造后期望值自动跟着变，通过率仍然可比。 |
| `consistency.postgres.json` | 48 张表、475 项指标（count / 数值列 sum / 时间列 min-max） | **不可比**。存的是绝对值，数据一重造就失效。 |

## 两条基线的分工

- **eval** 抓语义退化：选错表、口径跑偏、路由失灵。跨数据版本有效。
- **consistency** 抓搬迁损耗：COPY 丢行、类型转换掉精度、时区偏移。只在同一份数据的搬迁前后有效。

## 数据重造后的取法变化

行数扩到 8000 万级后，本机 Postgres 装不下，所以 consistency 基线**不再从 Postgres 查**，
而由生成器在生成时直接吐出预期值（genlib 是确定性列式生成，count/sum 在内存里免费拿到）。
落库后用 `scripts/consistency/snapshot.py --compare` 拿 Redshift 实际快照跟它对账。
这比原方案强：验的是「生成 → CSV → S3 → COPY」全链路无损，而不只是两个库之间一致。

## 现有 consistency 基线的残余用途

新生成器是重写，`--scale 1` 也不会逐行复现当前 CSV。所以这份快照不能用来校验生成器，
但可以当**业务比例的参照**：人均 40 事件 / 60 页面浏览 / 4 订单 / 71 点赞、归因覆盖缺口、
社交互动比例等，放大后应等比保持。

---

## 迁移后（Redshift，2026-08-03）

| 文件 | 内容 |
|------|------|
| `eval.post-migration-redshift.md` / `.json` | Redshift + 8000 万行上的 eval：**21/21**，与迁移前基线持平 |
| `consistency.generator-expected.json` | 生成器在生成时吐出的预期值（21 张事实表） |
| `consistency.redshift.json` | Redshift 实际快照（48 张表） |

对账命令与结果：

```
python3 scripts/consistency/snapshot.py --compare \
  eval/baseline/consistency.generator-expected.json \
  eval/baseline/consistency.redshift.json --subset
# → 一致 ✅  21 张表，逐表 count/sum/min-max 全等
```

### 脱敏列不参与对账

`users.email` / `users.phone` / `user_profiles.birth_date` 挂了 masking policy，
查询读到的是掩码值，与生成器的原始预期值必然不等。这两个机制在被脱敏的列上互斥，
取舍是**脱敏优先**。`snapshot.py` 的 `MASKED_COLUMNS` 常量负责跳过，加新策略时要同步。

---

## Glue 元数据对账（2026-08-04）

`reconcile.glue.md` —— 接上真 Glue federated catalog（`123456789012:analytics_agent_rs`）
之后的三方对账结果，当时六类检查全绿（`--strict` exit 0）。

这是第三条基线，跟前两条分工不同：

| 基线 | 抓什么 |
|---|---|
| `eval` | 语义退化：选错表、口径跑偏、路由失灵 |
| `consistency` | 搬迁损耗：COPY 丢行、类型转换掉精度 |
| `reconcile` | **文档漂移**：卡片写了库里没有的列、DDL 与实际建表不一致、指标口径引用了不存在的字段 |

第三条是这次架构升级真正新增的能力。前两条都需要「跑一遍」才知道有没有问题，
reconcile 是静态的、秒级的，可以直接挂 CI 门。

复现：

```
python3 scripts/glue/register_catalog.py --verify        # 确认目录状态
python3 scripts/glue/reconcile.py \
  --catalog 123456789012:analytics_agent_rs --strict
```

---

## 数据修复后（Redshift，2026-08-06）

数据审计（`docs/data-audit.md`）抓出跨表语义缺陷后生成器重写、8000 万行重灌，
本节是重灌后的新基线。

| 文件 | 内容 |
|------|------|
| `eval.post-datafix-redshift.md` / `.json` | 修复数据上的 eval：**21/21**，模型 `global.anthropic.claude-opus-4-8`，平均 40.9s/题 |
| `consistency.generator-expected.postdatafix.json` | 新生成器吐出的预期值（21 张事实表） |
| `consistency.redshift.postdatafix.json` | 重灌后的 Redshift 快照（48 张表，471 项指标） |

consistency 对账结果：471 项指标 2 处差异，均为 `user_profiles.birth_date`——
生成侧预期值含原始日期，快照侧被 masking policy 按设计跳过（见上文「脱敏列不参与
对账」）。非搬迁丢数据。

### 本轮对 eval 本身的两处修正

1. **`L2-coupon-usage-rate` 金标去掉了自带舍入**。旧金标 `round(...,1)` 在核销率
   49.7% 时舍入粒度只占 0.04%，数据修复把核销率锁到真实的 3.34%（used 券数 =
   用券订单数）后，同样 0.1 的粒度放大到 3%，超过 0.5% 默认容差，把 agent 的正确
   答案（3.34）判成错。金标改为输出精确值，该题容差放宽到 2%（覆盖双侧四舍五入的
   最坏差）。教训：**金标 SQL 不要自带舍入，宽容度交给 judge 容差**——舍入粒度是
   绝对的，容差是相对的，数值变小时两者会打架。
2. **`run_eval.py` 加了 infra 签名重试**。连跑 40+ 次 agent 时每轮随机有 1~2 题
   被限流砸中（签名：agent 一条 SQL 都没发、7~15s 即返回），三轮实测 21 题各自
   都能通过、失败题每轮不同。重试仅在 `n_sql == 0 且无结果集` 时触发一次，
   语义性答错必然带 SQL、不会触发，评测口径不变。

### 与前两条 eval 基线的可比性

金标是运行时现算的口径，通过率跨数据版本可比：pre-migration 21/21 →
post-migration 21/21 → post-datafix **21/21**。新数据额外的意义是 L4-funnel、
L5-repurchase-rate 这类题从「数字碰巧对」变成「业务形状也对」——漏斗单调递减、
留存单调衰减、等级与消费真实相关，题目终于是在有意义的数据上答对的。
