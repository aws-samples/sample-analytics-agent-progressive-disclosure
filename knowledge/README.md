# knowledge/ —— 数据分析 Agent 的知识库

这棵 md 文档树是 Agent 的**数据字典**:表结构、字段枚举、指标口径、跨表关系、分析方法 SOP。
Agent 运行时通过 `read_doc` 工具按路由**逐层按需读取**(渐进式披露),读到哪张表就拿哪张表的结构,再据此写 SQL。

> 表结构不写死在模型脑子里,而是写在这里。这是有意为之的设计:知识与代码解耦,改口径/加表只动文档,不改 agent。

## 目录结构

| 路径 | 内容 | Agent 何时读 |
|------|------|-------------|
| `domains/_index.md` | **入口**:8 个原始业务域 + 治理层 mart 的路由表 | 每次分析第一步 |
| `domains/<域>/_index.md` | 单个域的表清单与关系 | 按关键词定位到域后 |
| `domains/<域>/<表>.md` | 单表:字段、类型、枚举值、示例 SQL | 用到某张表前(必读) |
| `metrics/core_metrics.md` | DAU/WAU/MAU、留存、粘性、漏斗的口径 | 涉及这些指标时 |
| `metrics/business_kpis.md` | GMV/ARPU/LTV/CAC/ROI/付费率的口径 | 涉及这些指标时 |
| `metrics/governed_metrics.md` | **治理层官方口径**(与 `call_metric` 工具一一对应) | 判断/复盘题 |
| `analysis/_index.md` + 5 个方法 | 异常检测/漏斗/留存/帕累托/比率分解的 SOP + 统计公式 | 深度分析题 |
| `relationships.md` | 跨表 JOIN 的关联键 | 多表关联时 |
| `connection.md` | 本地/容器连库方式(面向人,非 agent 运行时) | 人工排查 |

## 单一真源约定

- **表结构**:以 `database/*.sql` 的 DDL 为准;本目录的表卡片是给 agent 读的说明层。
- **治理指标口径**:结构化定义在 `backend/metrics_def.py`(机器读,`call_metric` 用),`metrics/governed_metrics.md` 是同一套口径的人读版。**两者以代码为准**;主要指标一律走 `call_metric` 工具,不手写 SQL。
- **静态样本铁律**:数据止于 2026-01-24,"最近/上周/本月"一律以 `max(dt)` 为今天,**禁用 `current_date`/`now()`**(否则查空)。

## 打包 / 运行时

`backend/tools.py` 的 `read_doc` 读的就是这棵树(`DOCS_ROOT` 指向 `../knowledge`),Docker 构建时 `COPY knowledge/ /app/knowledge/` 一起进镜像。改文档无需改代码。
