#!/usr/bin/env python3
"""从 v1 的 PostgreSQL DDL 机械生成 Iceberg DDL。

## 为什么是生成而不是手写

v2 的 Redshift 建表语句（`database/redshift/01_tables.sql`）**是生成的**
（`scripts/gen/redshift_ddl.py`），这一点值得说清楚，别冤枉它。但那个脚本**没有
`--check`**：仓库里那份文件跟真源脱节了也没有任何一步会发现——生成器存在，
不等于生成物是新的。

真正手写的是**集市层** `database/redshift/02_mart.sql`（`03_derived.sql` 由
`pg_to_redshift.py` 转换而来，是生成物）。而漂移就恰好出现在手写那一半：

    v1  database/09_mart.sql:67          WHERE status = 'refunded' AND refunded_at IS NOT NULL
    v2  database/redshift/02_mart.sql:67 WHERE refunded_at IS NOT NULL          ← 少了状态过滤

这批数据里 `refunded_at` 非空的行只有 `status='refunded'`（145 行），所以两边算出来
都是 963560.92——**数值恰好相同，bug 是潜伏的**。换一批数据（比如部分退款也写
`refunded_at`）就会静默错，而没有任何测试会红。

所以本项目那句「手写 md 的原罪不是格式，是没人验证它」（见
`scripts/glue/register_catalog.py` 开头）在 DDL 上同样成立，而且要补一句：
**有生成器但没有 `--check`，等于没有**。这一版把 Iceberg DDL 做成生成物**并且**
守住它：

    database/0[1-8]_*.sql   ← 声明态（唯一真源，进 git，走评审）
            │
            ▼  本脚本（类型映射有自测）
    database/iceberg/01_tables.sql   ← 生成物（也进 git，但由 --check 守着）

`--check` 模式断言仓库里那份和现在生成的逐字相同。CI / test_all.sh 挂上它之后，
「改了 v1 DDL 但忘了同步 Iceberg DDL」这类漂移就变成一次红灯，而不是几个月后
某条 SQL 莫名报 COLUMN_NOT_FOUND。

## 类型映射

实测的对应关系（用 `DESCRIBE` 从真实 Iceberg 表反查确认过）：

    VARCHAR(n) / TEXT        → string        Iceberg 没有长度限制，varchar(n) 的 n 无处安放
    TIMESTAMP                → timestamp     Iceberg 的 timestamp **原生就是微秒**
    INT / SERIAL             → int
    BIGINT / BIGSERIAL      → bigint         SERIAL 只是「int + 自增序列」，Iceberg 无序列
    DECIMAL(p,s)            → decimal(p,s)   精度原样保留，金额列不能变 double
    BOOLEAN                  → boolean
    DATE                     → date
    JSONB                    → string        Trino 用 json_extract_scalar() 读，见下
    TEXT[] / INT[] / BIGINT[] → array<string / int / bigint>

复杂类型用 **Hive 风格尖括号** `array<string>`，不是 Trino 风格 `array(string)`。
Athena 的 DDL 解析器只认前者，写后者会报 `no viable alternative at input`。
但**查询里**要写 Trino 风格：`CAST(x AS array(bigint))`。同一个概念两种写法，
按「DDL 用尖括号、DML 用圆括号」记。

**JSONB 落成 string 是刻意的。** Trino 有 json 类型，但它对 `json_extract_scalar`
这类函数没有优势（函数本身接受 varchar），而 json 类型会让 `SELECT *` 的输出和
Postgres 路径不一致。存 string、读的时候 `json_extract_scalar(properties, '$.key')`，
行为可预测。代价是不做 JSON 合法性校验——这批数据由生成器产出，本来就合法。

## 主键 / 外键 / NOT NULL 去哪了

Iceberg **没有** PRIMARY KEY、FOREIGN KEY、UNIQUE、索引这些概念（它是表格式，不是
数据库）。v1 DDL 里的约束在这里全部落成列注释，保留可读性但不假装它们在生效。
表间关系的真源是 `knowledge/relationships.md`，agent 读的也是那份。

`NOT NULL` **也保不住**：Iceberg 规范支持 required 字段，但 Athena 的 Iceberg DDL
不接受这个修饰符（`CREATE TABLE t (a bigint NOT NULL)` 直接报
`no viable alternative at input`）。所以非空约束同样降级成注释。

这是相对 Redshift 的一处真实能力下降，记在这里而不是藏起来：v2 的
`verify_ddl_vs_redshift.py` 会比对 nullability，Iceberg 路径没有可比对的东西，
对账脚本必须显式跳过这一项，而不是假装比过了。

## 注释为什么全是 `/* */` 和 `COMMENT`，一个 `--` 都没有

**Athena 的 Iceberg DDL 会吃掉换行符**，于是 `--` 行注释会把下一行代码一起吞掉。
最小复现：

    -- 头
                                  ← 空行
    CREATE TABLE t (a bigint)
    → FAILED: line 1:35: no viable alternative at input '<EOF>'

`'-- 头' + 'CREATE TABLE t (a bigint)'` 拼成一行正好 34 字符，报错落在第 35 列的
`<EOF>`——整条语句被当成了注释。同一个机制下：

    -- 头 \n CREATE ...        单个换行，能过
    -- 头 \n\n CREATE ...      连续换行被折叠 → 全被注释吃掉 → 失败
    (a bigint, -- 说明\n b string)   列清单里的行内注释 → 吞掉下一列 → 失败

报错位置离真因很远（曾报在「第 13 行的 `)`」，而第 13 行是空行），极难反推。
所以这份生成物的规则是硬性的：

- 文件头、分节标题 → `/* ... */` 块注释。块注释有显式结束符，不依赖换行，
  内部可以有空行、反斜杠、单引号，实测全部安全。
- 列注释 → Iceberg 原生 `COMMENT '...'` 子句。

用 `COMMENT` 不只是绕坑，是**更好**：注释会落进 Glue 表元数据（`GetTable` 的
`StorageDescriptor.Columns[].Comment`），`/api/catalog` 和对账脚本能读回来。
写在 SQL 注释里的字，云上任何人都看不见。

`COMMENT` 字符串里的单引号用**反斜杠**转义（`\\'`），不是 SQL 标准的 `''`——
后者在这个 Hive 风味解析器上直接报 `no viable alternative`。实测转义后 Glue 里
存的是**未转义的原文**，所以注释文本逐字保真，对账可以直接拿 Glue 的 Comment
和真源比。

（另记：`array<string>` 在 Glue 的 `Columns[].Type` 里显示成 `list<string>`。
对账脚本比类型时要按这个映射，否则会误判成不一致。）

## 一个错误信息极度误导的坑

S3 Tables 的**表名必须以小写字母或数字开头**。以下划线开头（比如临时表叫 `_t_tmp`）
会报：

    FAILED: Exception encountered when executing Iceberg query

没有任何字样提到表名。这个通用错误和「类型不支持」「权限不足」长得一模一样，
极易误判——排查时曾据此错误地断定 S3 Tables 不支持 array/map/struct，
实际上三者都完全可用（建表、写入、UNNEST、cardinality 都实测通过）。
遇到这个错误先检查表名，再怀疑别的。

用法：

    python3 scripts/lakehouse/gen_ddl.py --selftest    # 类型映射自测，无云依赖
    python3 scripts/lakehouse/gen_ddl.py --check       # 断言仓库里那份没漂移
    python3 scripts/lakehouse/gen_ddl.py -o database/iceberg/01_tables.sql
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

# 仓库根 = 本文件往上三层（scripts/lakehouse/gen_ddl.py）
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_GLOB = os.path.join(ROOT, "database", "0[1-8]_*.sql")
DEFAULT_OUT = os.path.join(ROOT, "database", "iceberg", "01_tables.sql")

# 表级约束行，整行跳过（Iceberg 没有这些概念）
_TABLE_CONSTRAINT = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|CONSTRAINT|EXCLUDE)\b",
    re.IGNORECASE)

# ---------------------------------------------------------------- 分区
#
# `{表: (Iceberg 变换, 列)}`。**只有列在这张表里出现过的表才分区**，别的表不写进来，
# 而不是给所有表配一条默认规则——分区是拿存储和写入换扫描，不是白送的。
#
# 这份名单和它的粒度是**量出来的**，不是按行数拍的。2026-09-01 拿 scale=100 的
# page_views（302 万行）和 orders（20 万行）在真表上比过 flat / month / day 三种：
#
#     page_views     文件数   行/文件    存储     7 天窗口扫描   全表聚合扫描
#     不分区              2  1,509,800  36.4 MB      15.01 MB        1.26 MB
#     month(view_time)    4    754,900  36.6 MB       5.83 MB        1.38 MB
#     day(view_time)     91     33,182  42.5 MB       2.84 MB        2.27 MB
#
#     orders         文件数   行/文件    存储    7 天扫描  30 天扫描  全表按状态
#     不分区              1    200,000   8.1 MB   1.71 MB    1.71 MB     0.63 MB
#     day(placed_at)     91      2,197   8.4 MB   0.15 MB    0.56 MB     0.57 MB
#
# 三条结论决定了下面的选择：
#
# 1. ~~**`day()` 的剪枝在子查询边界上也成立。**~~ **这条 2026-09-01 复测不成立了，
#    别再按它做判断。** 原文记的是 scale=100 上「字面量少扫 80.2%、子查询少扫 81.1%，
#    两者几乎相同」，并留了一句「改这份名单前先确认这条还成立」。加那三张表时照做了确认，
#    结论是反的：**全量（scale=427）下七张分区表无一例外，子查询锚点比字面量锚点多扫
#    1.9×–7.8×**，同一个答案、同一份数据，重复三轮字节数完全一致（不是缓存也不是噪声）。
#
#        表              全表 MB   字面量 MB   子查询 MB   子查询/字面量
#        events            10.07       1.37       6.36        4.63×
#        page_views         6.15       1.00       7.78        7.77×
#        post_likes        47.26      16.73      31.39        1.88×
#        orders             2.70       0.27       0.70        2.58×
#        user_coupons      32.04       9.17      19.19        2.09×
#        user_follows      29.32      11.75      22.22        1.89×
#        post_comments     19.03       1.99       5.02        2.53×
#
#    `page_views` 那一行是最刺眼的证据：子查询形态扫 7.78 MB，**比全表聚合的 6.15 MB
#    还多**——说明分区一点没剪，Trino 是把 `max(as_of_date)` 当成运行期才知道的值，
#    没有折成常量拿去挑分区文件。复现命令（把 `page_views/view_time` 换成任意一行）：
#
#        SELECT count(distinct user_id) FROM page_views
#         WHERE view_time >= DATE '2026-01-24' - interval '7' day;          -- 1.00 MB
#        SELECT count(distinct user_id) FROM page_views WHERE view_time >=
#              (SELECT max(as_of_date) FROM meta_snapshot) - interval '7' day;  -- 7.78 MB
#
#    **后果分两头，都不是"把分区撤掉"。** 撤掉只会更差：字面量那一列证明剪枝本身是好的，
#    卡片将来改写成两段式（先取锚、再代入字面量）就立刻拿到 1.9×–7.8×。真正要记住的是
#    **别拿分区当已经生效的优化去解释查询耗时**，尤其在三种架构对比里：Redshift 的
#    SORTKEY 区块裁剪是运行期按实际值做的，子查询锚照样裁得动，而 Iceberg 这边的分区
#    在同一条 SQL 上裁不动——这是一处**真实存在、对 Iceberg 两个 arm 不利**的引擎差异，
#    要在报告里写成结论，不能当噪声抹掉。
#
#    附带量到的一件事，跟分区无关但会误导读数：`user_follows` 最后 7 天占全表 **39.55%**、
#    `user_coupons` 占 **26.48%**（均匀的话应是 7.7%）。所以这两张表上"七天窗口只少扫 40%"
#    不是剪枝没生效——字面量锚实测 11.75/29.32 = 40.1%，和行数占比对得上，是**满效**。
#    看剪枝效果要先看窗口里到底装了多少行。
# 2. **粒度选 day 而不是 month。** 主流窗口是 7 天，month 只能剪到 1/3
#    （15.01→5.83），day 能剪到 1/5（→2.84）。
# 3. **小文件的代价是有的，但落在计费下限以下。** day 分区把全表聚合的扫描量抬高了
#    （page_views 1.26→2.27 MB），代价随「行/文件」变差；orders 反而略降（0.63→0.57），
#    所以这个惩罚不是普适的。但两边的绝对值都是 1~2 MB，而 Athena 按查询有 10 MB
#    的计费下限——这个量级的差别根本进不了账单。真正进账单的是 7 天窗口那一列，
#    全量下 page_views 是 64 MB 对 12 MB。
#
# 全量（scale=427）下一个 day 分区约 9 千（orders）到 17 万行（post_likes），
# 比上面量的还大一档，所以小文件只会更轻。
#
# 下面三张 2026-09-01 补上（在此之前它们按同一条判据够格却没写进来，理由是那一轮的
# 改动范围只限 P2-14 点名的四张表）。补的动因不是它们自己的查询变快，而是**三种架构
# 对比测试的公平性**：`redshift_ddl.DIST_SORT` 给 **21 张事实表全部**配了时间 SORTKEY，
# 而这份名单当时只有 4 张有 `day()` 分区。同一个七天窗口的问题，Redshift arm 靠 SORTKEY
# 的区块裁剪只读该窗口，Iceberg arm（Athena 与 DuckDB 都是）要全表扫——量出来的差距里
# 混着一份纯粹的物理布局差异，而这不是要比的东西。**偏向是对着 Iceberg 两个 arm 去的**，
# 所以补分区是把偏差抹平，不是给 Iceberg 开后门。
#
# 只补这三张、不补剩下 14 张，判据仍是上面那条体量线：这三张全量 970 万 / 818 万 /
# 598 万行，比 orders（85 万）大一个数量级；剩下的够不到，加了只多摊小文件的账。
#
# 分区是**装载期**的决定，不是生成期的：改这个 dict 之后不需要重新生成数据，
# 只要 `load.py --recreate --only <表>` 把这几张重建再灌一遍，然后 `governance.py --apply`
# 重新授权。要加就在这个 dict 里加一行，`--check` 会把生成物拉着一起变。
PARTITION_SPEC: dict[str, tuple[str, str]] = {
    "events":        ("day", "event_time"),
    "page_views":    ("day", "view_time"),
    "post_likes":    ("day", "created_at"),
    "orders":        ("day", "placed_at"),
    "user_coupons":  ("day", "received_at"),
    "user_follows":  ("day", "created_at"),
    "post_comments": ("day", "created_at"),
}

# Iceberg 的时间变换。`day` 之外都没在本仓库用上，列出来是为了让 `--selftest`
# 能挡住手滑写成 `days` / `date` 这类不存在的变换名。
_TIME_TRANSFORMS = ("year", "month", "day", "hour")


def partition_clause(table: str, cols: list) -> str:
    """→ `PARTITIONED BY (day(view_time))`，没配分区的表返回空串。

    这里**校验列真的存在且是时间类型**，而不是把字符串直接拼进 DDL。理由是失败位置：
    列名写错的话，拼进去要等到云上 `CREATE TABLE` 才报，而 S3 Tables 那条报错是
    通用的 `Exception encountered when executing Iceberg query`，不提列名（见模块
    docstring 最后那个坑）。在这里抛，`--selftest` 和 `--check` 就都能拦住。
    """
    spec = PARTITION_SPEC.get(table)
    if not spec:
        return ""
    transform, col = spec
    if transform not in _TIME_TRANSFORMS:
        raise ValueError(f"{table}: 不认识的分区变换 {transform!r}，"
                         f"可用的是 {_TIME_TRANSFORMS}")
    kinds = {c: map_type(pg) for c, pg, _, _ in cols}
    if col not in kinds:
        raise ValueError(f"{table} 没有列 {col!r}，分区配置写错了"
                         f"（该表的列：{sorted(kinds)}）")
    if kinds[col] not in ("timestamp", "date"):
        raise ValueError(f"{table}.{col} 是 {kinds[col]}，"
                         f"{transform}() 变换只能用在 timestamp / date 上")
    return f"PARTITIONED BY ({transform}({col}))"


def map_type(pg: str) -> str:
    """Postgres 类型 → Iceberg 类型。未知类型抛错，不静默降级成 string。

    静默降级是这里最危险的失败模式：DECIMAL(12,2) 变成 string 之后，
    `SUM(actual_amount)` 会报错或（更糟）按字典序聚合出一个看起来合理的错数。
    宁可现在炸，也不要在 GMV 上错。
    """
    t = " ".join(pg.split()).upper()

    # 数组：先剥 [] 再递归映射元素类型。尖括号不是圆括号——见模块 docstring。
    m = re.match(r"^(.*?)\s*\[\s*\]$", t)
    if m:
        return f"array<{map_type(m.group(1))}>"

    # 带参数的类型
    m = re.match(r"^(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$", t)
    if m:
        return f"decimal({m.group(2)},{m.group(3)})"
    m = re.match(r"^(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*\)$", t)
    if m:
        return f"decimal({m.group(2)},0)"
    if t in ("DECIMAL", "NUMERIC"):
        # 无参 DECIMAL 在 Postgres 里是任意精度，Iceberg 必须给定值。
        # 本仓库的 DDL 不该出现这种写法；出现了就是该显式声明精度。
        raise ValueError("DECIMAL 必须带精度（Iceberg 不支持任意精度）")

    # VARCHAR(n) / CHAR(n) —— 长度信息丢弃，Iceberg 的 string 无长度上限
    if re.match(r"^(VARCHAR|CHARACTER\s+VARYING|CHAR|CHARACTER|TEXT)\s*(\(\s*\d+\s*\))?$", t):
        return "string"

    simple = {
        "TIMESTAMP": "timestamp",
        "TIMESTAMP WITHOUT TIME ZONE": "timestamp",
        "TIMESTAMPTZ": "timestamp",
        "TIMESTAMP WITH TIME ZONE": "timestamp",
        "DATE": "date",
        "BOOLEAN": "boolean",
        "BOOL": "boolean",
        "INT": "int",
        "INTEGER": "int",
        "INT4": "int",
        "SERIAL": "int",             # 自增序列在 Iceberg 里不存在，只保留底层宽度
        "SMALLINT": "int",           # Iceberg 有 int 但没有 smallint，向上取
        "BIGINT": "bigint",
        "INT8": "bigint",
        "BIGSERIAL": "bigint",
        "JSONB": "string",           # 见模块 docstring
        "JSON": "string",
        "REAL": "float",
        "DOUBLE PRECISION": "double",
        "UUID": "string",
    }
    if t in simple:
        return simple[t]
    raise ValueError(f"未知类型：{pg!r}（请在 map_type 里显式加映射，不要靠默认值）")


def col_comment(pg: str, not_null: bool, note: str) -> str:
    """一列的 Iceberg `COMMENT` 文本：v1 的行内注释 + 迁移中丢掉的约束/类型信息。

    分号分隔，最多三段：

        '实付金额; NOT NULL（Iceberg 侧不强制）; pg: DECIMAL(12,2)'

    后两段是**迁移损耗的记账**。Athena 的 Iceberg DDL 不接受 `NOT NULL`，
    `VARCHAR(50)` 的长度也留不住——不写下来，这些信息就只存在于 v1 的 DDL 里，
    在云上看表的人无从得知。

    抽成函数是给 `reconcile.py` 用的：它要拿这个和 Glue 里的 `Columns[].Comment`
    逐字比。比对方**必须**用同一个函数生成期望值，各写一份的话，改了这里就会
    收到 60 多条假漂移——那时人只会去改对账脚本，不会去看真问题。
    """
    marks = []
    if note:
        marks.append(note)
    # NOT NULL 不进 DDL：Athena 的 Iceberg DDL 不接受这个修饰符。
    # 降级成注释里的标记，让读的人知道 v1 声明过非空，只是这里不强制。
    if not_null:
        marks.append("NOT NULL（Iceberg 侧不强制）")
    # 类型被改写过就把原类型记下来，读的人不用回去翻 v1 DDL
    ice = map_type(pg)
    if pg.upper().replace(" ", "") not in (ice.upper(), ice.upper() + "(N)"):
        marks.append(f"pg: {pg}")
    return "; ".join(marks)


def parse_columns(body: str) -> list[tuple[str, str, bool, str]]:
    """解析 CREATE TABLE 的列定义体。

    返回 [(列名, Postgres 类型, NOT NULL, 行内注释)]，**保持原始列顺序**。
    列顺序是被对账检查的（见 scripts/gen/verify_ddl_vs_redshift.py 的
    「含列顺序」），乱序会直接判失败，所以这里绝不排序。
    """
    out = []
    for raw in body.split("\n"):
        # 先摘出行内注释再清理——注释里有业务口径信息，值得带到生成文件里
        note = ""
        m = re.search(r"--\s*(.*)$", raw)
        if m:
            note = m.group(1).strip()
        line = re.sub(r"--.*$", "", raw).strip().rstrip(",").strip()
        if not line or _TABLE_CONSTRAINT.match(line):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        col, rest = parts[0], parts[1]
        # 类型 = 开头那一段（可能带 (n) 或 (p,s) 或 []）
        m = re.match(
            r"^([A-Za-z_]+(?:\s+[A-Za-z_]+)*?)\s*(\(\s*\d+(?:\s*,\s*\d+)?\s*\))?\s*(\[\s*\])?",
            rest)
        if not m:
            continue
        pg = (m.group(1) or "") + (m.group(2) or "") + (m.group(3) or "")
        not_null = bool(re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE))
        out.append((col, pg.strip(), not_null, note))
    return out


def parse_source() -> list[tuple[str, str, list]]:
    """读 v1 DDL，返回 [(源文件名, 表名, 列定义)]，按文件名与文件内顺序。"""
    tables = []
    for path in sorted(glob.glob(SRC_GLOB)):
        txt = open(path, encoding="utf-8").read()
        for m in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", txt, re.DOTALL):
            tables.append((os.path.basename(path), m.group(1),
                           parse_columns(m.group(2))))
    return tables


def sql_str(s: str) -> str:
    """把注释文本包成 Athena DDL 能接受的字符串字面量。

    单引号用**反斜杠**转义。SQL 标准的 `''` 在这个 Hive 风味解析器上直接报
    `no viable alternative at input`——见模块 docstring。反斜杠本身先转义，
    否则 `C:\\x` 这类文本会把后面的引号吃掉。
    """
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render(tables: list[tuple[str, str, list]]) -> str:
    """生成 Iceberg DDL 文本。

    没有 LOCATION、没有 TBLPROPERTIES('table_type'='ICEBERG')：S3 Tables 是
    **Iceberg 原生**的表桶，在 s3tablescatalog 下 CREATE TABLE 出来就是 Iceberg，
    存储位置由服务托管。普通 Glue + S3 建 Iceberg 表才需要那两样。

    注释一律 `/* */` + `COMMENT`，一个 `--` 都不出现——理由见模块 docstring
    「注释为什么全是 /* */ 和 COMMENT」。selftest 里有回归闸挡着。
    """
    src_files = sorted({f for f, _, _ in tables})
    lines = [
        "/* Iceberg DDL —— S3 Tables 表桶里的 35 张基表。",
        " *",
        " * ⚠️ 本文件是**生成物**，不要手改。改了会被 --check 抓出来。",
        " *     生成： python3 scripts/lakehouse/gen_ddl.py -o database/iceberg/01_tables.sql",
        " *     校验： python3 scripts/lakehouse/gen_ddl.py --check",
        " *",
        f" * 真源：{', '.join('database/' + f for f in src_files)}",
        " * 类型映射、主键为何消失、注释为何不用 --，见 scripts/lakehouse/gen_ddl.py。",
        " *",
        " * 其中 " + "、".join(f"{t}（{tr}({c})）"
                              for t, (tr, c) in PARTITION_SPEC.items())
        + " 带分区。",
        " * 分区只对**已存在的表不生效**：CREATE TABLE IF NOT EXISTS 不会改已建好的表，",
        " * 给旧表加分区要先 DROP。DROP 会连带掉 Lake Formation 授权，重建后必须重跑",
        " * scripts/lakehouse/governance.py。粒度是量出来的，数据见 gen_ddl.py 的",
        " * PARTITION_SPEC 上方。",
        " *",
        " * 执行方式（catalog 名带斜杠，不能写进 SQL，必须走 QueryExecutionContext）：",
        " *     python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql \\",
        " *         --tolerate 'already exists'",
        " */",
    ]
    cur_file = None
    for src, name, cols in tables:
        if src != cur_file:
            cur_file = src
            lines += ["", f"/* ========== 来自 database/{src} ========== */"]
        lines.append(f"CREATE TABLE IF NOT EXISTS {name} (")
        width = max(len(c) for c, _, _, _ in cols)
        body = []
        for col, pg, not_null, note in cols:
            body.append((col, map_type(pg), col_comment(pg, not_null, note)))
        typew = max(len(t) for _, t, _ in body)
        for i, (col, ice, note) in enumerate(body):
            piece = f"    {col.ljust(width)} {ice}"
            if note:
                # COMMENT 必须在逗号**之前**：`a bigint COMMENT 'x',`
                piece = f"{piece.ljust(5 + width + typew)} COMMENT {sql_str(note)}"
            lines.append(piece + ("," if i < len(body) - 1 else ""))
        # PARTITIONED BY 在列清单的 `)` 之后、结尾分号之前。分区表的表名后面不能
        # 再有别的子句（本文件没有 LOCATION / TBLPROPERTIES，见本函数 docstring）。
        part = partition_clause(name, cols)
        lines.append(")" if part else ");")
        if part:
            lines.append(part + ";")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 自测

def _strip_literals(text: str) -> str:
    """把 `/* */` 块注释和 `'...'` 字符串字面量的内容抹成空格，保留行结构。

    回归闸要区分「DDL 正文里出现 `--`」和「注释文本里出现 `--`」：后者完全合法
    （`COMMENT 'pg: VARCHAR(50) -- 长度丢弃'` 实测能过）。抹成等长空格而不是删掉，
    这样报错行号和列号还对得上原文。
    """
    out = []
    i, n = 0, len(text)
    in_block = in_s = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_block:
            if ch == "*" and nxt == "/":
                out.append("  ")
                i += 2
                in_block = False
                continue
            out.append("\n" if ch == "\n" else " ")
        elif in_s:
            if ch == "\\":                    # 反斜杠转义：连吃两个字符
                out.append("  " if nxt != "\n" else " \n")
                i += 2
                continue
            if ch == "'":
                in_s = False
            out.append("\n" if ch == "\n" else " ")
        elif ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            in_block = True
            continue
        elif ch == "'":
            out.append(" ")
            in_s = True
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_CASES = [
    # (Postgres 写法, 期望的 Iceberg 类型)
    ("VARCHAR(50)", "string"),
    ("VARCHAR(500)", "string"),
    ("TEXT", "string"),
    ("CHAR(2)", "string"),
    ("TIMESTAMP", "timestamp"),
    ("timestamp", "timestamp"),
    ("DATE", "date"),
    ("BOOLEAN", "boolean"),
    ("INT", "int"),
    ("INTEGER", "int"),
    ("SERIAL", "int"),
    ("SMALLINT", "int"),
    ("BIGINT", "bigint"),
    ("BIGSERIAL", "bigint"),
    ("DECIMAL(12,2)", "decimal(12,2)"),
    ("DECIMAL(10, 2)", "decimal(10,2)"),
    ("DECIMAL(2,1)", "decimal(2,1)"),
    ("JSONB", "string"),
    # 尖括号，不是圆括号：Athena 的 DDL 解析器只认 Hive 风格
    ("TEXT[]", "array<string>"),
    ("INT[]", "array<int>"),
    ("BIGINT[]", "array<bigint>"),
    ("VARCHAR(50)[]", "array<string>"),
    ("DOUBLE PRECISION", "double"),
]

_MUST_FAIL = ["DECIMAL", "NUMERIC", "MONEY", "INET", "HSTORE", "POINT"]


def selftest() -> int:
    bad = 0
    for pg, want in _CASES:
        try:
            got = map_type(pg)
        except ValueError as e:
            print(f"  ❌ {pg!r} 抛错了：{e}")
            bad += 1
            continue
        if got != want:
            print(f"  ❌ {pg!r} → {got!r}，期望 {want!r}")
            bad += 1
    # 未知类型必须抛错，不能静默降级成 string
    for pg in _MUST_FAIL:
        try:
            got = map_type(pg)
        except ValueError:
            continue
        print(f"  ❌ {pg!r} 本该抛错，却映射成了 {got!r}（静默降级是最危险的失败模式）")
        bad += 1

    # 列解析：NOT NULL 与列顺序
    cols = parse_columns("""
    order_id BIGSERIAL PRIMARY KEY,
    order_no VARCHAR(50) UNIQUE NOT NULL,
    status VARCHAR(30) NOT NULL,  -- 'pending', 'paid'
    total_amount DECIMAL(12,2) NOT NULL,
    shipping_address JSONB,
    PRIMARY KEY (order_id)
    """)
    names = [c for c, _, _, _ in cols]
    if names != ["order_id", "order_no", "status", "total_amount",
                 "shipping_address"]:
        print(f"  ❌ 列解析顺序不对：{names}")
        bad += 1
    nn = [c for c, _, n, _ in cols if n]
    if nn != ["order_no", "status", "total_amount"]:
        print(f"  ❌ NOT NULL 识别不对：{nn}")
        bad += 1
    if cols[2][3] != "'pending', 'paid'":
        print(f"  ❌ 行内注释没抓到：{cols[2][3]!r}")
        bad += 1

    # 真源必须解析出 35 张表 —— 这条挡的是「正则悄悄漏了一张表」
    tables = parse_source()
    if len(tables) != 35:
        print(f"  ❌ 从 database/0[1-8]_*.sql 解析出 {len(tables)} 张表，期望 35")
        bad += 1
    ncols = sum(len(c) for _, _, c in tables)
    if ncols != 388:
        print(f"  ❌ 总列数 {ncols}，期望 388")
        bad += 1
    # 全部 388 列都要能映射
    for _, name, cols in tables:
        for col, pg, _, _ in cols:
            try:
                map_type(pg)
            except ValueError as e:
                print(f"  ❌ {name}.{col}: {e}")
                bad += 1

    # ---- 分区配置 ----
    # PARTITION_SPEC 里的每张表都要真存在，列也要真存在且是时间类型。这三条错
    # 在云上都只报一句不提列名的 `Exception encountered when executing Iceberg
    # query`，所以必须在本地拦住。
    by_name = {n: c for _, n, c in tables}
    for tbl in PARTITION_SPEC:
        if tbl not in by_name:
            print(f"  ❌ PARTITION_SPEC 配了 {tbl!r}，但真源里没有这张表")
            bad += 1
            continue
        try:
            got = partition_clause(tbl, by_name[tbl])
        except ValueError as e:
            print(f"  ❌ {e}")
            bad += 1
            continue
        if not got.startswith("PARTITIONED BY ("):
            print(f"  ❌ {tbl} 的分区子句渲染成了 {got!r}")
            bad += 1
    # 反向：没配的表不能凭空长出分区子句
    unpart = [n for _, n, c in tables
              if n not in PARTITION_SPEC and partition_clause(n, c)]
    if unpart:
        print(f"  ❌ 这些表没配分区却渲染出了子句：{unpart}")
        bad += 1
    # 三种写错法都必须抛错，而不是拼进 DDL 等云上报错：列不存在、列不是时间类型、
    # 变换名不存在（`days` 是最容易手滑的一个）。
    _probe_cols = [("order_id", "BIGINT", False, ""),
                   ("placed_at", "TIMESTAMP", False, "")]
    for bogus, why in ((("day", "no_such_column"), "列不存在"),
                       (("day", "order_id"), "列不是 timestamp/date"),
                       (("days", "placed_at"), "变换名不存在")):
        PARTITION_SPEC["_probe"] = bogus
        try:
            partition_clause("_probe", _probe_cols)
            print(f"  ❌ 分区配置 {bogus} 本该抛错（{why}）")
            bad += 1
        except ValueError:
            pass
        finally:
            PARTITION_SPEC.pop("_probe", None)

    # 渲染产物的三条回归闸。每一条都会让整份 DDL 在 Athena 上直接语法失败，
    # 而失败信息（`no viable alternative at input`）指向的位置离真因很远
    # ——第 3 条那次报在「第 13 行的 `)`」，而第 13 行是空行。
    text = render(tables)
    code_lines = _strip_literals(text).splitlines()
    for lineno, code in enumerate(code_lines, 1):
        line = text.splitlines()[lineno - 1]
        if re.search(r"\bNOT\s+NULL\b", code, re.IGNORECASE):
            print(f"  ❌ 第 {lineno} 行的 DDL 正文里有 NOT NULL："
                  f"Athena 的 Iceberg DDL 不接受它\n     {line.strip()}")
            bad += 1
        if re.search(r"\barray\s*\(", code, re.IGNORECASE):
            print(f"  ❌ 第 {lineno} 行用了 Trino 风格 array(...)："
                  f"DDL 必须用 Hive 风格 array<...>\n     {line.strip()}")
            bad += 1
        if "--" in code:
            print(f"  ❌ 第 {lineno} 行有 -- 行注释：Athena 的 Iceberg DDL 会吃掉换行，"
                  f"-- 会把下一行代码一起吞进注释\n     {line.strip()}")
            bad += 1
    if "array<string>" not in text:
        print("  ❌ 生成结果里没有 array<string>，数组列可能被静默丢掉了")
        bad += 1
    if "COMMENT '" not in text:
        print("  ❌ 生成结果里没有 COMMENT 子句，列注释可能被丢掉了"
              "（它是要落进 Glue 元数据的，不是装饰）")
        bad += 1
    # 分区子句的位置：必须是 `)` 换行 `PARTITIONED BY (...);`，且这一段之前**没有**
    # 分号。写成 `);` 再跟 PARTITIONED BY 会让后者变成一条独立语句，Athena 报的是
    # 一句和分区毫无关系的语法错。
    npart = len(re.findall(r"^\)\nPARTITIONED BY \(\w+\(\w+\)\);$", text, re.M))
    if npart != len(PARTITION_SPEC):
        print(f"  ❌ 生成结果里有 {npart} 条格式正确的 PARTITIONED BY，"
              f"期望 {len(PARTITION_SPEC)}（要么位置错了，要么少渲染了）")
        bad += 1
    if re.search(r"\);\nPARTITIONED BY", text):
        print("  ❌ PARTITIONED BY 前面多了分号，它会被当成独立语句")
        bad += 1
    # 转义：反斜杠不是 ''
    if sql_str("'app', 'web'") != r"'\'app\', \'web\''":
        print(f"  ❌ 单引号转义不对：{sql_str(chr(39) + 'a' + chr(39))!r}")
        bad += 1
    if sql_str("C:\\x") != "'C:\\\\x'":
        print(f"  ❌ 反斜杠没先转义：{sql_str('C:' + chr(92) + 'x')!r}")
        bad += 1

    # _strip_literals 自己也要测：它错了上面三条闸就会静默失效。
    # 期望值用拼接写，不手数空格——数错过一次。
    _blank = lambda s: " " * len(s)
    for src, want in [
        # 字面量里的 -- 要被抹掉（那是合法的，闸不该报）
        ("a COMMENT 'x -- y'", "a COMMENT " + _blank("'x -- y'")),
        # 块注释里的 -- 同理
        ("/* -- */ a", _blank("/* -- */") + " a"),
        # 反斜杠转义的引号不能提前结束字面量
        (r"COMMENT '\'q\'' b", "COMMENT " + _blank(r"'\'q\''") + " b"),
        # 跨行块注释：换行要留着，否则行号对不上
        ("a\n/* x\ny */\nb", "a\n" + _blank("/* x") + "\n" + _blank("y */") + "\nb"),
        # 正文里的 -- 必须留下来，否则闸永远绿
        ("a bigint, -- 说明", "a bigint, -- 说明"),
    ]:
        got = _strip_literals(src)
        if got != want:
            print(f"  ❌ _strip_literals({src!r}) → {got!r}，期望 {want!r}")
            bad += 1
        if len(got) != len(src):
            print(f"  ❌ _strip_literals 没保长度：{len(src)} → {len(got)}（行列号会错位）")
            bad += 1

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  类型映射 {len(_CASES)} 例、拒绝降级 {len(_MUST_FAIL)} 例、"
          f"列解析 3 项、真源 35 表 388 列、渲染回归 7 项、注释抹除 5 项")
    print(f"  分区 {len(PARTITION_SPEC)} 表（"
          + "、".join(f"{t} {tr}({c})" for t, (tr, c) in PARTITION_SPEC.items())
          + "），列存在性/类型/变换名各拒绝 1 例")
    print("全部通过 ✅")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="v1 Postgres DDL → Iceberg DDL")
    ap.add_argument("-o", "--out", help="输出文件；省略则打到 stdout")
    ap.add_argument("--check", action="store_true",
                    help=f"断言 {os.path.relpath(DEFAULT_OUT, ROOT)} 与现在生成的一致")
    ap.add_argument("--selftest", action="store_true", help="类型映射自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    tables = parse_source()
    if not tables:
        print(f"没从 {SRC_GLOB} 解析出任何表", file=sys.stderr)
        return 1
    text = render(tables)

    if a.check:
        if not os.path.isfile(DEFAULT_OUT):
            print(f"❌ 缺文件：{os.path.relpath(DEFAULT_OUT, ROOT)}\n"
                  f"   跑一次： python3 scripts/lakehouse/gen_ddl.py -o "
                  f"{os.path.relpath(DEFAULT_OUT, ROOT)}")
            return 1
        have = open(DEFAULT_OUT, encoding="utf-8").read()
        if have != text:
            import difflib
            d = list(difflib.unified_diff(
                have.splitlines(), text.splitlines(),
                fromfile="仓库里的", tofile="现在生成的", lineterm="", n=1))
            print(f"❌ {os.path.relpath(DEFAULT_OUT, ROOT)} 与真源漂移了，"
                  f"差异 {len([x for x in d if x[:1] in '+-'])} 行：")
            for line in d[:40]:
                print("   " + line)
            print("\n   v1 DDL 改了就重新生成一次，别手改生成物。")
            return 1
        print(f"一致 ✅  {len(tables)} 张表 "
              f"{sum(len(c) for _, _, c in tables)} 列")
        return 0

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        open(a.out, "w", encoding="utf-8").write(text)
        print(f"已写出 {a.out}：{len(tables)} 张表 "
              f"{sum(len(c) for _, _, c in tables)} 列 ✅")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
