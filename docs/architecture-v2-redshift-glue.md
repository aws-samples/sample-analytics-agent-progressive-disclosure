# v2 架构：Redshift + Glue Catalog

> ## 📜 历史文档：**v2 已被 v3(湖仓)取代**,正文内容原样保留
>
> **Redshift Serverless 已整体退役。** 现行数据层是 Athena + S3 Tables (Iceberg) +
> Glue Data Catalog,见 [../PROJECT_STATUS.md](../PROJECT_STATUS.md) 阶段七与
> [deployment.md](deployment.md)。
>
> 这份文档不改内容——它记的是 v2 当时的设计决策和踩过的坑,改了就是伪造历史。
> 读它是为了理解「为什么从 Postgres 走到 Redshift、又为什么从 Redshift 走到湖仓」,
> **不要照着它部署**:`database/redshift/`、`scripts/redshift/`、`scripts/glue/`
> 都已是死路径。文中所有行数(8000 万)属于 v2 那批数据。**注意别被量级骗了**:v3 的湖后来
> 也被灌成了 8000 万行(新生成器,和 v2 这批同量级但不同源),而仓库交付的 `data/csv/` 种子
> 只有约 22 万行 —— 三者是三份不同的数据。当前规模看
> [deployment.md](deployment.md#数据说明),不要从本文推。

v1 的数据在 Aurora/Postgres，元数据是一棵手写的 markdown 树。这份文档记录 v2 改了什么、
为什么这么改，以及迁移过程中踩到的坑。

验收流程见 [test-plan-v2.md](test-plan-v2.md)（L0–L8 九层，`bash scripts/test_all.sh` 跑前七层）。

## 一句话概括

**数据搬到 Redshift Serverless（8000 万行），元数据拆成「声明态 / 实际态 / 语义层」三方并加对账，
护栏从应用层下推到数据层。**

## 一、为什么不是简单换个库

v1 的两个短板是自认的：

1. **说服力短板**：35 张表、19 万行。渐进披露的价值证明不足——全部 schema 塞进 context
   也就几千 token，「读对文档才查得对」是演示而非刚需。
2. **可信度短板**：元数据是手写 markdown。对专业数据平台而言，表结构靠人手同步是不现实的。

但**「把 md 搬进 Glue」是错的**。划清 Glue 的能力边界之后就明白了：

| `knowledge/` 里的内容 | Glue 能装吗 |
|---|---|
| 表卡片的字段表（列名/类型/说明） | 能（`Columns` + `Comment`） |
| `layer` / `owner` / `status` / `grain` | 能（塞 `Parameters`） |
| `use_when` / `avoid_when` / `caveats` | 勉强能塞 `Parameters`，等于把散文塞进数据库字段 |
| 域路由表、关键词映射 | **不能**，Glue 是 per-table 的，没有「域索引」这个对象 |
| 跨表 JOIN 关联键 | **不能**，Glue 没有外键概念 |
| 指标口径（`metrics/*.md`，545 行） | **不能**，Glue 没有 metric 对象 |
| 分析方法 SOP（`analysis/*.md`，203 行） | **不能**，这根本不是表元数据 |

按行数算，`knowledge/` 树里过半的内容跟 Glue 完全无关，而且恰好是最值钱的那半。
Glue 能碰的只有 47 张表卡片里「字段表」那一段，也就是最机械的部分。

## 二、Glue 的定位：环上的验证点，不是真源

`schema_manifest.yaml` 已经能从一条记录生成 DDL + 卡片 + 域索引。**生成 DDL 是 Glue
给不了的能力，`use_when`/`caveats`/`eval_trap` 是 Glue 装不下的内容**，所以 manifest
注定不会被 Glue 取代。链条是：

```
schema_manifest.yaml / database/*.sql     ← 声明态（人写、进 git、走评审）
          │ render.py / DDL
          ▼
   Redshift 实际建出来的表
          │ scripts/glue/register_catalog.py
          ▼
   Glue Data Catalog                       ← 实际态（生成的，不可手改）
          │
   knowledge/domains/** 表卡片              ← 语义层（人的判断：何时用/坑/口径）

   对账：三方两两比，漂移即告警（scripts/glue/reconcile.py）
```

这跟 Terraform 的 plan/apply、K8s 的 desired/actual 同构：**数据字典也该有声明态和
实际态，只有一份的那种叫文档，两份加对账的那种叫平台。**

`md` 的原罪不是格式，是**没人验证它**。所以这次升级真正的产出物是对账，不是存储格式。

### 对账当场抓到的问题

在一个看起来没问题的 repo 上，reconcile 抓出 **5 处真实文档漂移**：卡片的字段表里写了
DDL 里不存在的列（`channels.updated_at`、`ad_creatives.updated_at`、
`ab_test_variants.updated_at`、`coupons.updated_at`、`user_attributions.created_at`）。
agent 照卡片写 `SELECT updated_at FROM channels` 会直接报错。

还有一处**枚举值漂移**，是 eval 抓到的：`user_coupons.status` 的 DDL 注释写 `available`，
而卡片、卡片里的示例 SQL、v1 已提交的 CSV、eval 金标四方都用 `unused`。agent 照卡片
写 `status='unused'` 在新数据上查出 0 行。四方里三方一致，孤例是 DDL 注释，所以按
`unused` 对齐。**列名对账查不出枚举值漂移，这是 reconcile 当前的已知缺口。**

接上真 Glue catalog 后再跑，六类检查全绿（`--strict` exit 0）：

```
声明态（DDL）      35 张表
实际态（Glue）     48 张表
语义层（卡片）     47 张表卡片
治理指标           4 张表被指标引用
对账通过 ✅
```

其中 B 类（卡片写了但库里没有的列）和 C 类（DDL 有但库里没有的列）都是 0，
意味着前面修的 5 处幽灵列在真目录上确认干净，且 DDL 声明的列在 Redshift 里一列不缺——
**这是「生成 → 建表」这一段真正被验证过的第一次**，之前只是拿本地 Postgres 自证。

`meta_snapshot` 走的是带校验的豁免（`CARD_EXEMPT`）：它存数据集的「今天」锚点，是全局
关注点而非某个业务域的表，塞进 `knowledge/domains/<某域>/` 会给出错误的路由信号，
所以成文在 `knowledge/connection.md`。豁免本身也要对账——A 检查会真去读那个文件、
确认它确实提到这张表，否则「加进豁免名单」就成了掩盖问题的后门。

## 三、治理：两层防护，各自独立

v1 的护栏全在应用代码里（`db.py` 正则挡写操作、只读连接、`metric_layer` 冻结口径）。
能演，但审计视角下是「单层、应用自证」。v2 加了数据层的硬约束：

| 层 | 机制 | 何时生效 | 失效时另一层是否仍在 |
|---|---|---|---|
| 权限不给 | `analytics_agent_ro` 角色只在需要的表上有 SELECT。`user_messages`（私信正文）**连表都不授权** | 查询时 | 是 |
| 脱敏兜底 | `users.email` / `users.phone` / `user_profiles.birth_date` 挂 DDM，`TO PUBLIC` 生效 | 查询时 | 是 |

两层都在**查询时**生效，都经过实测：以管理员身份查 `users.email` 拿到
`***@masked.invalid`、`phone` 拿到 `103****0752`、生日只剩年份。
`svv_attached_masking_policy` 里三条策略 grantee 均为 `public`，
`is_masking_datashare_on = 't'`。

### 一条被实测推翻的设计假设：没有「目录不可见」这一层

原先这里写的是三层，第一层是「挂了 DDM 的表不会被注册进 Glue，agent 的元数据来自目录，
目录里没有 = 发现不了」。**这一层不存在**，实测数据：

| 路径 | `users` / `user_profiles` |
|---|---|
| `GetTables`（列表） | **出现**，且带完整列清单，`email` / `phone` / `birth_date` 列名照常暴露 |
| `GetTable`（单表） | 报 `EntityNotFoundException`（46 张非 DDM 表全部成功） |

误读的原文是「DDM 策略**不能挂到** datasharing 表上」——约束的是策略的附着对象，
不是「挂了 DDM 的表不进 datashare」。方向搞反了。而 `is_masking_datashare_on = 't'`
正好反证：这些表**是**被共享出去的，脱敏在 datashare 路径上生效，不是被排除。

正确的说法是：**Glue federated catalog 是 schema 投影，不是权限投影。** 它既不反映
Redshift 的 GRANT（未授权的 `user_messages` 照样出现在目录里），也不完整反映 DDM。
把「目录里看不见」当成一层访问控制是错的。

实测的两个面并不重合：

| | 张数 | 差集 |
|---|---|---|
| Glue 目录可见 | 48 | — |
| `analytics_agent_ro` 可 SELECT | 45 | 目录里有、角色读不到：`user_messages`、`orders_backup_20251201`、`tmp_campaign_roi_analysis` |

后两张是本 demo 故意埋的陷阱表（考 agent 会不会误用过期备份和临时表）。它们**在目录里
可见但拿不到 SELECT**，等于陷阱同时也有数据层兜底。反方向的差集是空的——角色没有任何
目录之外的孤儿权限。

这个纠正反而让 A3 选 C 的理由更干净了：护栏的价值不在「让 agent 发现不了」，而在
**发现了也拿不到明文**。前者是隐藏，后者是控制；隐藏会被任何一条列表 API 绕过，
控制不会。`reconcile.py` 的 E 检查因此改成验证「DDM 在 GetTable 路径上确实生效」，
并把列表路径的泄露作为常驻边界说明打印，不计入 findings——永远修不掉的已知行为反复
报成问题，只会训练人忽略 findings（正是下面第 10 条坑）。

## 四、迁移踩到的坑（按发现顺序）

这些都是文档里写着但容易漏、或者只有实测才会暴露的。

### 1. Parquet COPY 按位置映射，不按名字

官方原文：「COPY 按数据文件里列出现的顺序插入目标表的列」，列数必须相等。
**列序错了不报错，会把数据静默灌进相邻的列**，比崩掉难查得多。
`scripts/gen/main.py: align_to_ddl()` 强制按 DDL 顺序重排并校验列集合。

### 2. Parquet 物理类型必须与 Redshift 列类型兼容

`user_level` 在 Postgres 是 `INT` → Redshift `INTEGER`（int32），numpy 默认给 int64，
整表 COPY 失败并报 `incompatible Parquet schema for column ...`。金额列同理，
`DECIMAL(12,2)` 不接受 Parquet 的 double。类型映射集中在 `scripts/gen/arrow_types.py`。

### 3. SUPER 列的 COPY 要加 `SERIALIZETOJSON`

否则报 `SUPER column in COPY query requires SERIALIZETOJSON option`。
Redshift 既没有 `JSONB` 也没有数组类型，两者都映射到 `SUPER`，Parquet 侧写 JSON 文本。

### 4. Redshift 的 VARCHAR 长度按字节算，不是字符

中文一字 3 字节，`VARCHAR(50)` 装不下 50 个汉字。`redshift_ddl.py` 统一放大 4 倍。

### 5. Postgres 专有 SQL 构造（4 类）

| 不支持 | 替代 | 出现处 |
|---|---|---|
| `AGG(x) FILTER (WHERE c)` | `AGG(CASE WHEN c THEN x END)` | 派生层 4 处、eval 金标 15 处 |
| `DISTINCT ON (col)` | `ROW_NUMBER() OVER (PARTITION BY ...) = 1` | mart 4 处 |
| `generate_series`（引用用户表时） | 从够长的表 `ROW_NUMBER()` + `DATEADD` | mart 日期轴 |
| `(array_agg(x ORDER BY x))[2]` | `ROW_NUMBER() = 2` | mart 复购判断 |

`generate_series` 那条尤其隐蔽：它在 Redshift 里是 **leader-node-only 函数**，
不能出现在引用用户表的查询里。

金标 SQL 不为换库分叉两份（分叉必然漂移）：`cases.json` 保持 Postgres 方言，
`run_eval.py` 在 Redshift 后端下用 `scripts/gen/pg_to_redshift.py` 运行时改写。

### 6. Data API 的返回类型与 psycopg 不一致（最难查的一个）

Data API 对 `DECIMAL`/`NUMERIC` 一律回 `stringValue`（如 `"55679342.35"`），
而 psycopg 回 `Decimal` 并被转成 float。不还原类型的话，上层拿到字符串，
**eval 的数值提取直接漏掉，表现为「SQL 完全正确但判定失败」**——21 题里踩掉 5 题，
而且每条 SQL 单独跑都对，极难定位。`rsql._scalar()` 按 `ColumnMetadata.typeName`
还原成与 psycopg 同类型的值。时间戳同理（`'2026-01-24 00:00:00.0'` → isoformat）。

### 7. 按分号拆 SQL 会切断字符串字面量

`COMMENT ON TABLE t IS '粒度=dt;归因=last_touch'` 里的 ASCII 分号被当成语句分隔符，
报 `Unterminated string literal`。`rsql.split_statements()` 做字符级扫描，
单引号（含 `''` 转义）、双引号标识符、`--` 与 `/* */` 注释内的分号都不算分隔符。

### 8. 幂律分布的默认参数会造出退化数据

`rank^-alpha` 在 `alpha > 1` 时级数收敛，排第一的实体独占 `1/ζ(alpha)`（alpha=1.3 时
约 25%），top20% 占比冲到 96%、尾部几乎空掉，复购率/分群/cohort 全部失去意义。
改用对数正态权重（σ=1.15，top20% ≈ 62%），并实测验证尺度不变性：父实体从 2 千到
21 万，top20% 稳定在 65.0-65.3%、Gini 0.635-0.641。
注：`genlib.rng.power_law_counts` 的默认值正是 1.3，照默认调用会中这一刀。

### 9. 分布形状的断言必须卡区间

第一版自测写的是单边「top20% > 50%」，结果 95.8% 那个退化分布大摇大摆通过了。
另外「单个父实体占比」是尺度相关量（21 万父实体时 0.061%，2 千时 1.1%），
拿它当不变量会在换规模时假失败。断言要用有闭式解、与规模无关的量（top-p 份额、Gini）。

### 10. 带假阳性的检查器比没有更糟

reconcile 第一版扫了卡片里所有 markdown 表格，把「字段枚举值」小节的枚举行
（`| paid | 已支付 |`）也当成列名，报出 40 多处假漂移。人看两眼就不再信它，
真问题也一起被忽略。解析必须限定在「表结构」小节。

同一个坑在 D 类检查上又踩了一次：从指标 SQL 里正则分词挑「像列名的标识符」，
关键字表是「遇到假阳性补一个」攒出来的，漏了 `IS NOT NULL` 里的 `is`，
在 `mart_user_summary` 上报了一处假漂移。SQL 关键字是**已知有限集**，
该一次列全而不是增量攒（`reconcile.py: SQL_RESERVED`）。

### 11. `lakeformation register-resource` 是必需步骤，但极易看漏

官方文档给它标了「This is a mandatory step」，位置却夹在两段 console 操作说明之间。
漏了它，`glue create-catalog` 会报 `Insufficient Lake Formation permission(s) on
<datashare-arn>`——错误信息指向 datashare 而不是缺失的注册动作，很容易误判成
IAM 权限不足，然后往策略上乱加权限。

### 12. 建 federated catalog 必须是 Lake Formation data lake admin，没有更窄的替代

`AdministratorAccess` 的 IAM 权限**不够**：Lake Formation 有独立的授权层。
而且没法只在那个 datashare 上发一条资源级授权——`lakeformation grant-permissions`
只支持 9 种资源类型（Catalog / Database / Table / TableWithColumns / DataLocation /
DataCellsFilter / LFTag / LFTagPolicy / LFTagExpression），**没有 DataShare**。

这是个账号级权限。`register_catalog.py` 的追加逻辑做成读-改-写，并在写完后断言原有
admin 仍在（`put-data-lake-settings` 是整体替换语义，不回填会静默抹掉别人的配置）。

**撤回 admin 有个前置条件，容易想漏**：catalog 是用空的
`CreateDatabaseDefaultPermissions` / `CreateTableDefaultPermissions` 建的，刻意不给任何
principal 默认全权；而 admin 会**绕过** LF 的权限检查，所以只要还挂着 admin，就看不出来
非 admin 能不能读这个目录。直接撤，`reconcile.py` 会立刻读不到表。要安全收紧成「最小权限 +
无常驻 admin」，得先给跑对账的身份显式发三条只读授权（Catalog `DESCRIBE`、
Database `DESCRIBE`、Table 通配 `SELECT`+`DESCRIBE`），再 `--revoke-admin`。
当前仓库的状态是保留 admin、对账正常工作，那三条 grant 留白待批。

### 13. Redshift federated catalog 比普通 Glue catalog 多一层

```
<acct>:analytics_agent_rs                       ← create-catalog 建的顶层
  ├─ <acct>:analytics_agent_rs/dev              ← Redshift 自带的空默认库
  └─ <acct>:analytics_agent_rs/app_analytics    ← Redshift 的 database
       └─ Glue database "public"                ← Redshift 的 schema
            └─ table
```

Redshift 的 *database* 成了子 catalog，*schema* 才映射成 Glue 的 database。
在顶层 catalog 上直接 `get-databases` 什么也取不到，必须先 `get-catalogs
--parent-catalog-id` 下潜一层。reconcile 的第一版就是照普通 catalog 的两层结构写的，
读到 0 张表。

### 14. 存活探针打错端点，让 demo 静默退化成假数据

前端 boot 时用一个探针判断"后端在不在"，成功就走实时链路，失败退回烘焙好的离线演示。
某次改造把探针从 `GET /health` 改成了 `GET /ask`——而 `/ask` 是 **POST**。GET 过去必然
405，`h.ok` 是 `undefined`，于是每次都掉进 `catch`，**本地跑的 demo 一直在放假数据**。

界面并没有完全静默——模型芯片会显示"离线演示模式"、页脚写"模拟数据"、状态点是灭的。
真正的坑是**归因指错方向**：页脚提示"后端未连接，启动后端 `backend/run.sh`"，
而后端明明起着。照着提示去查启动流程只会一无所获，没人会想到是探针的 URL 写错了。

现在 `test_all.sh` 的 L6 里有一条断言专门盯这个端点。

### 15. 默认值指向已退役的资源

`db.py` 的 `DB_BACKEND` 默认曾是 `postgres`。Aurora 全搬走之后，任何人不设环境变量直接
跑 eval 或起服务，都会撞上 `ModuleNotFoundError: No module named 'psycopg'`——而真正的库
在云上好着。报错信息指向一个缺失的包，跟真实原因（默认后端过时）差着两层。

默认值改成 `redshift` 之后，`docker-compose.cloud.yml`（自带 db 容器、走 v1 路径）
必须显式钉 `DB_BACKEND: postgres`，否则轮到它连错后端。**改默认值的代价就是把所有
依赖旧默认的地方都找出来**，这次是一处。

## 八、UI：让升级在演示里看得见

这次改造有个容易漏的收尾：**数据搬完了、目录建好了、治理生效了，但 UI 一行没变。**
对一个靠界面演示的项目，等于升级没发生。而且 UI 上的旧数字不只是"没更新"，是**在说错话**：

| UI 原来显示 | 实际 |
|---|---|
| `~19万行` | 约 8000 万（差 420 倍） |
| `PostgreSQL`（11 处，含顶栏「实时」标签） | Redshift Serverless |
| `35 明细表 + 4 治理表` = 39 | Glue 目录里 48 |
| 派生层（dwd/dws/ads/noise）零提及 | 8 张，且已 GRANT 给 agent |
| 展示给观众看的 SQL 用 `FILTER (WHERE)`、`JSONB` | Redshift 两个都不支持 |

根因不是"忘了改"，是**元数据在前端有第二份手抄本**。表清单、表数、字段全写死在 HTML 与
i18n 字典里，它们不会因为库变了而报警——跟这次升级要解决的问题（md 手抄本没人验证）
是同一个病，只是换了个位置。

所以修法不是把 39 改成 48，而是**让 UI 读 Glue**：新增 `GET /api/catalog`
（`backend/catalog.py`）把四个来源聚成一份 UI 可直接渲染的结构：

```
Glue Data Catalog      → 表清单 + 字段（实际态）
Redshift svv_*         → 行数估算 + 治理现状（GRANT 覆盖面、脱敏策略）
knowledge/domains/     → 域分组（与 agent 的路由结构同源）
schema_manifest.yaml   → 派生层的 layer / status
```

演示时这句话就可以直说了：**这些不是我写死在前端的，是从 Glue 目录读的，改了库它自己跟着变。**

三个实现上的取舍：

- **行数只算 base 层。** 派生层是基表的副本或汇总，48 张硬加得 9649 万，而数据集真实规模
  是 7992 万（`dwd_events_app` 就是 `events` 的过滤版，行数几乎相同）。报大数字很诱人，
  但那个数字经不起"这些行是同一批事实吗"这一问。接口同时给 `rows_all_layers` 备查。
- **行数用 `svv_table_info.tbl_rows` 而不是 `count(*)`。** 后者扫 8000 万行要几十秒，
  48 张串起来把接口拖成分钟级。前者 2–3 秒返回全部表，实测与生成器的精确值完全吻合。
- **Glue 读不到就降级到 `information_schema`，但把 `source` 回传并显示在界面上。**
  克隆仓库没做 Glue 那步的人照样能看到正确数字，只是少了"元数据来自统一目录"这个演示点。
  静默降级是有害的——那会让人以为自己看到的是 Glue。

治理面板同理：它回答的是客户最常问的那个问题（「AI 会不会不小心读到 PII」），
给的不是承诺而是现状——授权面 45/48、哪三张连表都没给、哪些列挂了脱敏，全部现查库。
顺带纠正了一个容易误读的地方：UI 原有的 🔒 图标指的是**口径冻结**，不是访问控制，
两者混在一起讲会让人以为看到了权限保证。

前端的自动化覆盖见 `scripts/ui/render_test.mjs`：不用浏览器也不用 jsdom，打 DOM 桩把
主 script 跑起来，拿真实接口响应断言。它抓到过一个差 10 倍的 bug——行数缩写按 `n/1e4`
算却配英文单位 `K`，7992 万渲染成 `7,992K`。**单位错误比数字缺失危险**，因为它看起来
是个正常数字；中文用万/亿（1e4/1e8）、英文用 K/M/B（1e3/1e6/1e9），阈值不同，
不能只换单位串。

## 五、数据规模

`scripts/gen/budget.py` 用**相对当前已提交数据的倍数**表达规模，而不是各表拍绝对值。
好处是所有业务比例原封不动（人均 40 事件 / 60 页面浏览 / 4 订单 / 71 点赞、归因覆盖缺口、
社交互动比例），知识库里写的口径特征放大后自然重现。

`--target-rows 80000000` 反解出 scale=427.039，实际落库 79,922,056 行：

| 表 | 行数 |
|---|---|
| post_likes | 15,231,627 |
| page_views | 12,894,870 |
| user_coupons | 9,708,732 |
| events | 8,540,780 |
| user_follows | 8,184,202 |
| orders | 854,078 |
| users | 213,520 |

事实表（21 张）用 genlib 向量化重写；维度表（14 张，合计 2,392 行）直接复用 v1 的 CSV
转 Parquet——行数几乎全在事实表上，维度表在真实业务里本来也就几十到几百行。

时间窗固定 2025-10-26 → 2026-01-24（91 天），放大只让每天更密，不拉长历史。
拉长会改掉知识库已声明的 `data_start`、mart 行数、以及首尾残周残月的形状。

## 六、两条基线的分工

| 基线 | 抓什么 | 跨数据重造是否有效 |
|---|---|---|
| `eval/` harness | 语义退化：选错表、口径跑偏、路由失灵 | **有效**。金标是运行时现算的 golden SQL，不是写死的数 |
| `scripts/consistency/snapshot.py` | 搬迁损耗：COPY 丢行、类型转换掉精度、时区偏移 | 无效（存的是绝对值） |

8000 万行本机装不下，所以一致性基线**不再从 Postgres 查**，而由生成器在生成时直接吐出
预期值（genlib 是确定性列式生成，count/sum 在内存里免费拿到）。落库后拿 Redshift 实际
快照跟它对账。这比原方案强：验的是「生成 → 序列化 → 传输 → COPY」全链路无损。

求和必须整数化（放大 10^4 取 int64 累加）：`snapshot.py` 的 SQL 是
`SUM(CAST(col AS DECIMAL(38,4)))`，DECIMAL 求和精确，而 float64 累加 1500 万行会攒出
尾数误差，对账时全是假阳性。

## 七、成本

Redshift Serverless **空闲不计费**，只按查询实际跑的 RPU-秒算（warehouse 级 60 秒起步价）。
对"有人提问才跑"的 demo，可能比常开的 Aurora 更便宜。两个必须设的护栏：

- `base-capacity 4`（**默认是 128 RPU**，不显式设会让 clone 的人破产）
- 每月 RPU-小时 usage limit（本项目设 20，超限记录告警）

4 RPU 的约束：单表 100 列上限、32TB 上限。本项目最宽的表 24 列，无碍。
