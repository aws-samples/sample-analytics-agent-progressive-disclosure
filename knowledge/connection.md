# 数据仓库连接信息

> 本文面向**人**（排查、手工查数），不是 agent 运行时读的。agent 走 `run_sql` /
> `call_metric` 工具，连接细节由 `backend/db.py` 封装。

## 当前形态：S3 Tables（Iceberg）+ Glue Data Catalog + Athena（us-west-2）

数据是数据湖里的 **Iceberg 表**，存在 S3 Table bucket 里；表的元数据联邦进
**Glue Data Catalog** 成为 Glue database；查询引擎是 **Amazon Athena**。
没有常驻集群，没有库用户，没有密码。

| 项 | 值 |
|---|---|
| S3 Table bucket | `analytics-agent-tables` |
| Namespace（= Glue database） | `app_analytics` |
| Athena workgroup（管理侧） | `analytics-agent-wg`（engine v3，单查询扫描上限 1 GiB 且强制生效），结果落 `s3://analytics-agent-raw/athena-staging/` |
| Athena workgroup（agent） | `analytics-agent-ro-wg`，结果落 `s3://analytics-agent-raw/athena-staging/agent/`。**两个是分开的**：结果集是明文行数据的 CSV，共用一个前缀时 agent 能从管理侧的结果文件里读回 LF 已排除的列 |
| Athena 的 Catalog 参数 | `s3tablescatalog/analytics-agent-tables`（**不带**账号前缀） |
| Glue API 的 CatalogId | `<账号>:s3tablescatalog/analytics-agent-tables`（**带**账号前缀） |
| 区域 | `us-west-2`（与 AgentCore Runtime 同区，不再跨区） |
| 原始 CSV 桶 | `s3://analytics-agent-raw/`（初始装载来源，查询不经过它） |
| 授权 | Lake Formation 列级授权。agent 用的是专属角色 `analytics-agent-ro`：`user_messages` 整表读不到，`users.email` / `phone`、`user_profiles.birth_date` 不在授权面里（`SELECT *` 里没有这几列，点名查报 `COLUMN_NOT_FOUND`）。见 `scripts/lakehouse/governance.py` |

### 两种 catalog ID 写法不能混用

这是这套架构上最容易踩、而且**报错完全指不到成因**的一处：

- Athena 的 `QueryExecutionContext.Catalog`：`s3tablescatalog/analytics-agent-tables`
- boto3 `glue` 的 `CatalogId`：`<账号>:s3tablescatalog/analytics-agent-tables`

写反了报 `EntityNotFoundException`，看不出是前缀问题。Glue 的层级也是两层：
`<账号>:s3tablescatalog` 是父目录，`<账号>:s3tablescatalog/<桶名>` 才是叶子；
对父目录 `get_databases` 会报 "The specified bucket does not exist"，
对叶子 `get_catalogs` 会报 `InvalidInputException`。

### 为什么走 Athena 而不是连数据库

Athena 是 HTTPS + IAM 的 AWS API，不是数据库连接。因此：

- **AgentCore Runtime 不需要进 VPC**，也不需要连接池
- 容器里**不存数据库密码**——根本没有密码这个东西，认证全走 IAM
- 没有常驻计算，不查就不花钱；扫描上限在 workgroup 上强制，跑飞的 SQL 会被掐掉

代价是它异步（提交 → 轮询 → 取结果），已由 `scripts/lakehouse/athena.py` 包成同步调用。

## 手工查数

```bash
# 执行任意 SQL
backend/.venv/bin/python scripts/lakehouse/athena.py \
  "SELECT count(*) FROM orders"

# 批量执行一个 .sql 文件（按语句拆分，尊重字符串里的分号）
backend/.venv/bin/python scripts/lakehouse/athena.py \
  --file database/iceberg/02_mart.sql
```

凭证走调用者的 IAM 身份（`aws sso login` 或 `AWS_PROFILE`），没有独立的库账号。

## 数据规模与静态样本铁律

当前样本全库 **220,087 行 / 48 张表** = 35 张原始表 189,672 行 + 13 张派生表
（mart / dws / dwd / ads / noise / meta）30,415 行：

> **你自己数不出这两个数，别拿它们当"我查到的"。** 上面是**全量数据集**的规模
> （真源是 `data/csv/`，由 `scripts/lakehouse/verify_load.py` 钉住）。而你用的是
> `analytics-agent-ro` 角色，`user_messages`（9,253 行）不在授权面里，`count(*)` 会失败。
> 所以从你这个视角数出来的全库是 **210,834 行**、原始表 **180,419 行**——少的正是那 9,253 行。
>
> 被问到"一共多少行"时说清是哪一个：想报全量就引用上面这个数并说明它来自数据集清单，
> 想报自己实测的就说"我这个角色能看到的是 18 万行，另有 1 张表不授权"。
> 两个都对，混着用就是个**看起来正常但少 5% 的数**——正好是那种没人会去查的错。
> `/api/catalog` 的 `totals.rows` 同理是治理视角的数，它另有
> `rows_uncounted_governed` 把差额的来源列出来。

| 表 | 行数 |
|---|---|
| post_likes | 35,668 |
| page_views | 30,196 |
| user_coupons | 22,735 |
| events | 20,000 |
| user_follows | 19,165 |
| order_items | 4,225 |
| orders | 2,000 |
| users | 500 |

Iceberg 的 `count(*)` 是**读元数据**得出的，精确且扫描 0 字节——所以上面这些数
不是估算，`/api/catalog` 每次都真数一遍。

**数据止于 2026-01-24**（`meta_snapshot.as_of_date`），起于 2025-10-26。
「最近 / 上周 / 本月」一律以 `(SELECT max(as_of_date) FROM meta_snapshot)` 为今天，
**禁用 `current_date` / `now()`**，否则查空。注意这是**口径**问题不是方言问题：
`current_date` 在 Trino 里语法完全合法，所以它不会报错，只会安静地返回 0 行。

**同样禁止拿该表自己的 `max(时间列)` 当今天。** 这条比上一条隐蔽：它不返回空集，
而是返回一个错的数。`channel_daily_costs` / `mart_channel_daily` 的轴铺到 2026-09-01
（投放成本铺了近一年），最远的 `subscriptions.end_date` 到 2027-01-24。
用各表自己的 max 当锚点，「最近 30 天渠道 GMV」会算成 0，ROI 跟着变成 0。
全库 91 个时间列的轴末普查表写在 `metrics/governed_metrics.md` §时间锚点。

> 本文件**面向人**（见 `README.md` 的文档表），agent 运行时不会读它。所以
> `meta_snapshot` 的列清单和上面这套锚点规则，真源在 agent 读得到的两处：
> `domains/_index.md`（路由第一步必读）和 `metrics/governed_metrics.md`。
> 这里是同一套规则的人读副本，改了那边记得对一遍。`reconcile.py` 的 `CARD_EXEMPT`
> 钉的是 `domains/_index.md`，不是本文件。

## Trino 与 Postgres 的方言差异（写 SQL 时注意）

Athena 的 SQL 引擎是 **Trino**，跟 Postgres 是两个血统（Redshift 是 Postgres 的
分叉，Trino 不是）。另外 Athena 有**两套解析器**：DDL 是 Spark/Hive 味的，
DML 是 Trino——所以建表语句和查询语句的类型名写法可能不一样。

本库的 SQL 从 Postgres 迁过来，以下构造已在迁移时改写：

> 下面这张表里**左列是不能照抄的反例**。别拿转换脚本去改这个文件——
> `pg_to_trino.py` 会把左列也一起"修好"，表就变成 `A → A`，看着全对，实际废了。

| Postgres 写法 | Trino 写法 |
|---|---|
| `x::type` | `CAST(x AS type)` |
| `numeric` / `numeric(p,s)` | `decimal(38,6)` / `decimal(p,s)`，**别写裸 `decimal`** |
| `TEXT` | `varchar`（Trino 没有 `TEXT`，会报 `Unknown type: TEXT`） |
| `interval '7 days'` | `interval '7' day`（数字在引号里，单位在引号外且单数） |
| `interval '2 weeks'` | `interval '14' day`——**Trino 没有 week / quarter 单位** |
| `some_date + 7` | `some_date + interval '7' day` |
| `ts_col >= '2024-01-01'` | `ts_col >= date '2024-01-01'`（Trino 不把字符串隐式转成时间） |
| `to_char(d,'YYYY-MM')` | `date_format(d,'%Y-%m')` |
| `j->>'k'` | `json_extract_scalar(j, '$.k')` |
| `EXTRACT(EPOCH FROM (a - b))` | `date_diff('second', b, a)`（**参数是「早，晚」，顺序反过来**） |
| `PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)` | `approx_percentile(x, p)`（Trino 无有序集聚合；**近似值**） |
| `JOIN t USING (c)` 之后写 `t.c` | 用 `ON a.c = b.c`——`USING` 会把连接列**合并成一列**，之后无法限定 |
| `GROUP BY <SELECT 里的别名>` | `GROUP BY 1`（序号）；Trino 的 `GROUP BY` 不认 SELECT 别名，`ORDER BY` 才认 |
| `WITH RECURSIVE t AS (...)` | 递归 CTE **必须显式写列名**：`WITH RECURSIVE t(a,b) AS (...)` |
| `SELECT unnest(arr)` | `CROSS JOIN UNNEST(arr) AS t(x)`（只能放 FROM 里） |
| `x = ANY(arr)` | `contains(arr, x)`（Trino 的 `ANY` 只接子查询） |
| `arr[i]` | 能用，但**越界会报错**；要 NULL 语义用 `element_at(arr, i)` |
| `ILIKE` | `lower(a) LIKE lower(b)` |
| `string_agg(x, ',')` | `array_join(array_agg(x), ',')` |
| `DISTINCT ON (col)` | `ROW_NUMBER() OVER (PARTITION BY col ORDER BY ...) = 1` |
| `generate_series(a,b)` | `UNNEST(sequence(a,b))`（注意日期序列出来是 timestamp） |
| `CREATE INDEX` | 无索引；靠只选用到的列 + 时间列范围条件让 Iceberg 跳文件 |

**裸 `decimal` 这一条要特别小心**：Trino 里它等于 `decimal(38,0)`，
`CAST(1.55 AS decimal)` 得到 `2.0`。查询照样成功、结果照样像个数，只是分和角
被抹掉了——这是本页所有条目里唯一**不报错**的那种错。

**week / quarter 那条的报错会骗人**：`interval '2' week` 报
`TYPE_NOT_FOUND: Unknown resolvedType: interval`，跟 Postgres 式的
`interval '7 day'`（单位写进引号里）报的是**同一句话**。于是很容易以为是引号位置
不对，反复调引号——而真正的原因是这个单位在 Trino 里根本不存在，只能换算成天/月。

Trino 原生支持、无需改写的：`AGG(x) FILTER (WHERE cond)`（这条恰好和 Redshift
相反，Redshift 不支持而 Trino 支持）、`date_trunc`、`NULLIF`、`COALESCE`、
窗口函数、`UNION`、`||` 拼接、`%` 取模、`GROUP BY 1`、`NULLS LAST`、
`count(DISTINCT x)`、`approx_percentile`。

整数除法**截断**（`7/2` = 3），算比率前先 `CAST(... AS decimal(38,6))`。

转换工具：`scripts/gen/pg_to_trino.py`（带自测，`--selftest`）。

## 本地 Postgres（v1 遗留，不再验证）

`scripts/localpg/` 和 `docker-compose.yml` 是 v1 的本地 rig，仅用于小规模离线校验
（`scripts/gen/main.py --scale 1 --format csv` 加 `scripts/gen/load_local.sh`）。
方言、Iceberg 表结构和 Glue 集成都只在 Athena 上验证，本地路径不再跟进。
`backend/run.sh` 的 `DB_BACKEND=postgres` 分支保留可跑，但会主动忽略
`GLUE_CATALOG_ID`——否则 `/api/catalog` 会从 Glue 取表清单、从本地库取行数，
拼出一份"接口 200、数字像样、但描述的是另一个库"的元数据。
