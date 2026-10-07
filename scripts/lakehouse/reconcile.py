#!/usr/bin/env python3
"""三方元数据对账（湖仓版）：DDL 声明态 ⟷ Glue 实际态 ⟷ 知识库语义层。

## 这个脚本在这套架构里的位置

把元数据从 markdown 换成 Glue Catalog 本身不解决任何问题——**md 的原罪不是格式，
是没人验证它**。改个列名忘了同步卡片，agent 下一秒就开始编字段，而且编得很自信。
这是 NL→SQL 幻觉的头号来源。

所以 Glue 的定位是**环上的验证点，不是真源**：

    database/0[1-8]_*.sql + database/iceberg/02_mart.sql   ← 声明态（人写、进 git、走评审）
              │ scripts/lakehouse/gen_ddl.py（有 --check 守着）
              ▼
        S3 Tables 里真实建出来的 Iceberg 表
              │ S3 Tables 自带的 Glue 联邦目录（不是我们注册的）
              ▼
        Glue Data Catalog                    ← 实际态（生成的，不可手改）
              │
        知识库表卡片 knowledge/domains/**    ← 语义层（人写的判断：何时用/坑/口径）

## 与 v2（Redshift 版 scripts/glue/reconcile.py）的三处实质差别

1. **目录层级不同**。Redshift 联邦目录多一层（database 变成子 catalog），要先
   `get_catalogs` 下潜。S3 Tables 是 `<账号>:s3tablescatalog/<表桶>` 直接就是叶子，
   namespace 就是 Glue database。给顶层 `<账号>:s3tablescatalog` 反而会报
   "The specified bucket does not exist"——两种写法不能混用，见 knowledge/connection.md。

2. **不比 nullability**。Athena 的 Iceberg DDL 不接受 `NOT NULL`（报
   `no viable alternative at input`），v1 的非空约束在生成时降级成了列注释。
   v2 的 `verify_ddl_vs_redshift.py` 比对 nullability，这条路径**没有可比的东西**。
   显式跳过并说明，而不是假装比过了——见 gen_ddl.py 的「主键/外键/NOT NULL 去哪了」。

3. **多比两样 Redshift 路径比不了的**：
   - **列类型**（G 检查）。Glue 里存的是 Iceberg 类型，可以和 `gen_ddl.map_type()`
     的输出逐字比。注意 Glue 把 `array<string>` 显示成 `list<string>`，
     不做这个归一化会 48 张表全报不一致。
   - **列注释**（H 检查）。生成器用 Iceberg 原生 `COMMENT` 而不是 SQL 注释，
     所以业务口径进了 Glue 元数据。实测 Glue 存的是**未转义原文**，可以逐字比。
     这一条是「文档腐烂」检测里最有价值的：它比的是**语义**，不只是名字。

## 检查清单

| 编号 | 症状 | 为什么要紧 |
|---|---|---|
| A | Glue 有表、语义层没卡片 | 未文档化的表，agent 看得见但不知道怎么用 |
| B | 卡片引用的列 Glue 里不存在 | **陈旧文档**，agent 会照着编字段 |
| I | Glue 有列、卡片「表结构」没写 | agent 不知道这列存在，能力凭空少一块 |
| C | 声明的列 Glue 里没有（或整表没有） | 生成链断了：DDL 没跑、迁移半途失败 |
| D | 治理指标 SQL 引用的列不存在 | 官方口径静默失效，最阴险的一类 |
| G | 列类型与声明不一致 | 金额列变 string 之类，SUM 出来是个看着合理的错数 |
| H | 列注释与真源不一致 | 云上看到的口径和 git 里的不是一份（`--fix-comments` 可修） |
| F | Glue 里没有任何表 | 尚未建表，或凭证/目录 ID 不对 |

B 和 I 是同一次比对的两个方向，但**后果不对称**：B 会让查询直接报错，所以迟早被发现；
I 不报错，agent 只是永远绕开那一列。不双向比就查不出 I。

## 治理层（DDM / Lake Formation 最小权限）**不在本脚本的覆盖范围内**

v2 那版有 E 检查（验证挂了 DDM 的表在 GetTable 路径上确实被挡住）。本架构的治理层
在 `scripts/lakehouse/governance.py`（最小权限角色 + Lake Formation 列级排除），
**不在这里验**：本脚本跑的是调用方（通常是 data lake admin）的身份，看到的是完整
目录——它连"哪些列被排除了"都看不见，更别说验证排除生效。治理的断言在 L4
（`governance.py --selftest / --verify / --verify-backend`）。
脚本会明确打印这件事——不打印的话，"对账通过 ✅" 会被读成"治理也验过了"。

## 用法

    python3 scripts/lakehouse/reconcile.py
    python3 scripts/lakehouse/reconcile.py --strict        # 有问题就 exit 1（CI 用）
    python3 scripts/lakehouse/reconcile.py --selftest      # 归一化逻辑自测（无云依赖）
    python3 scripts/lakehouse/reconcile.py --fix-comments  # 打印修 H 的 ALTER（加 --apply 执行）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT / "backend"))

MART_DDL = ROOT / "database" / "iceberg" / "02_mart.sql"

# 不放域卡片、但在别处成文的表：表名 → 成文位置（相对仓库根）。
#
# meta_snapshot 存数据集的"今天"锚点，是**全局**关注点而非某个业务域的表——每个带时间的
# 查询都要用它，塞进 knowledge/domains/<某域>/ 会给出错误的路由信号。
#
# 成文位置必须是 **agent 读得到**的那份。原来这里指的是 knowledge/connection.md，
# 而那个文件在 knowledge/README.md 里明写着「面向人，非 agent 运行时」：于是唯一记着
# 这张表长什么样的文档，恰好是 agent 永远不会打开的那一个，prompt 却在教它
# `SELECT max(as_of_date) FROM meta_snapshot`。列清单现在写在总索引
# knowledge/domains/_index.md（prompt 第 1 步必读）里，豁免跟着指过去。
#
# 这不是盲跳过：A 检查会真去读那个文件、确认它确实提到了这张表。豁免本身也要对账，
# 否则「加进豁免名单」就变成了掩盖问题的后门。
CARD_EXEMPT = {"meta_snapshot": "knowledge/domains/_index.md"}


# ---------------------------------------------------------------- 类型归一化

def norm_type(t: str) -> str:
    """把 Glue / DDL 的类型串归一到可比形式。

    做两件事，都是实测逼出来的：

    1. `list<...>` → `array<...>`。Glue 的 `Columns[].Type` 用 Hive 老写法显示数组，
       而 DDL 里写的是 `array<...>`。不归一化就是 48 张表里每个数组列都报不一致——
       **一个全表误报的检查器等于没有检查器**，人第二次就不看了。
    2. 去掉空白、统一小写。`decimal(12, 2)` 和 `decimal(12,2)` 是同一个类型。

    刻意**不做**的：不把 `varchar(n)` 和 `string` 当同一个。gen_ddl 已经把长度信息
    丢掉了（Iceberg 没有长度上限），所以声明态里本来就只会出现 `string`；
    如果 Glue 里冒出个 `varchar(50)`，那是真有人手工建过表，应该报出来。
    """
    s = re.sub(r"\s+", "", t.strip().lower())
    # 可能嵌套（list<list<string>>），循环替换到不动为止
    while "list<" in s:
        s = s.replace("list<", "array<")
    return s


def norm_comment(c: str) -> str:
    """注释比对前的归一化：只吃掉首尾空白与全/半角空格差异。

    **不做**大小写折叠、不去标点——注释是给人读的业务口径，
    "含运费" 和 "不含运费" 只差一个字，任何"宽松匹配"都可能把它们判成相同。
    """
    return re.sub(r"[ 　\t]+", " ", c.strip())


# ---------------------------------------------------------------- 声明态

def _parse_iceberg_ddl(text: str) -> dict[str, list[tuple[str, str, str]]]:
    """解析手写的 Iceberg DDL，返回 表名 → [(列名, 类型, 注释)]。

    只认 `CREATE TABLE t (\\n  col type COMMENT '...',\\n ...)` 这一种形态——
    这正是 02_mart.sql 的格式约定（每列一行）。认不出的行直接跳过：
    这里宁可漏比一列，也不要把 `PARTITIONED BY` 之类的尾巴当成列。
    """
    import verify_mart_parity as vmp

    out: dict[str, list[tuple[str, str, str]]] = {}
    for st in vmp.statements(text):
        m = re.match(r"\s*create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)\s*\((.*)\)\s*$",
                     st, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        cols = []
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",").strip()
            if not line:
                continue
            cm = re.match(r"^(\w+)\s+([A-Za-z_][\w<>(),\s]*?)"
                          r"(?:\s+comment\s+'((?:[^'\\]|\\.)*)')?$",
                          line, re.IGNORECASE)
            if not cm:
                continue
            note = (cm.group(3) or "")
            # DDL 里单引号是反斜杠转义的，Glue 存的是未转义原文
            note = note.replace("\\'", "'").replace("\\\\", "\\")
            cols.append((cm.group(1), cm.group(2).strip(), note))
        if cols:
            out[m.group(1).lower()] = cols
    return out


def declared() -> dict[str, list[tuple[str, str, str]]]:
    """声明态：35 张基表（v1 DDL 经 gen_ddl 映射）+ 集市/派生层（手写 Iceberg DDL）。

    两个来源的真源方言不同：基表的真源是 Postgres 类型，要过 `map_type` /
    `col_comment`；集市层的真源本来就是 Iceberg DDL，原样解析。

    基表这一半**必须调 gen_ddl 的函数**，不能自己拼期望值。云上那份注释不是 v1 的
    行内注释原文，而是 `col_comment()` 拼出来的三段式（加了 NOT NULL 降级标记和
    原始 Postgres 类型）。自己拼一遍的话，第一版就是 62 条假漂移。
    """
    import gen_ddl

    out: dict[str, list[tuple[str, str, str]]] = {}
    for _src, table, cols in gen_ddl.parse_source():
        out[table.lower()] = [
            (c, gen_ddl.map_type(pg), gen_ddl.col_comment(pg, not_null, note))
            for c, pg, not_null, note in cols]
    if MART_DDL.exists():
        out.update(_parse_iceberg_ddl(MART_DDL.read_text(encoding="utf-8")))
    return out


# ---------------------------------------------------------------- 实际态

def glue_state(catalog_id: str, database: str, region: str
               ) -> dict[str, list[tuple[str, str, str]]]:
    """实际态：从 Glue 读，返回 表名 → [(列名, 类型, 注释)]，**保持列顺序**。

    `CatalogId` 必须是**带账号前缀**的叶子形式 `<账号>:s3tablescatalog/<表桶>`。
    给顶层 `<账号>:s3tablescatalog` 会报 "The specified bucket does not exist"，
    给 Athena 那种不带前缀的形式会报 `EntityNotFoundException`——两种报错都指不到
    成因，所以这里不做"聪明的兜底猜测"，缺前缀直接告诉调用者该怎么写。
    """
    import boto3

    if ":" not in catalog_id:
        raise SystemExit(
            f"--catalog 要带账号前缀：{catalog_id!r} 看起来是 Athena 用的写法。\n"
            f"boto3 glue 的 CatalogId 形如 <账号>:s3tablescatalog/<表桶>，"
            f"见 knowledge/connection.md「两种 catalog ID 写法不能混用」。")

    g = boto3.client("glue", region_name=region)
    out: dict[str, list[tuple[str, str, str]]] = {}
    p = g.get_paginator("get_tables")
    for page in p.paginate(CatalogId=catalog_id, DatabaseName=database):
        for t in page.get("TableList", []):
            cols = t.get("StorageDescriptor", {}).get("Columns", [])
            cols = cols + t.get("PartitionKeys", [])
            out[t["Name"].lower()] = [(c["Name"], c.get("Type", ""),
                                       c.get("Comment", "") or "")
                                      for c in cols]
    return out


# ---------------------------------------------------------------- 注释漂移的修法

def fix_comments(fixes: list[tuple[str, str, str, str]], apply: bool = False) -> int:
    """把注释漂移改回真源：`ALTER TABLE t CHANGE COLUMN c c <type> COMMENT '...'`。

    **方向是单向的：真源 → Glue。** `database/iceberg/*.sql` 进 git、走评审，是被
    评审过的那一份；Glue 里的是部署产物。所以漂移一律理解为"云上落后了"，绝不反过来
    把云上的文本写回文件——那等于让未评审的状态覆盖评审过的状态。

    为什么会漂：Iceberg 的列注释是**建表时写进元数据**的，事后改 DDL 文件不会回灌。
    先建表、后往 DDL 里补口径说明（比如补上"⚠️ 这一列因 join 扇出虚高"），云上就还是
    旧文本。于是**同一列的口径说明在 git 里和在 Athena 控制台里不是一份**——看控制台
    的人得不到那个警告。

    `CHANGE COLUMN` 要求把列名和类型重写一遍（`c c <type>`），类型取自声明态，
    所以这条语句只改注释、不动类型。

    默认只打印不执行。改元数据是 modify 操作，得由人看过语句再点头。
    """
    if not fixes:
        print("没有注释漂移，无需修。")
        return 0

    import gen_ddl

    stmts = [f"ALTER TABLE {t} CHANGE COLUMN {c} {c} {ty} COMMENT {gen_ddl.sql_str(note)}"
             for t, c, ty, note in fixes]

    if not apply:
        print(f"以下 {len(stmts)} 条 ALTER 会把 Glue 里的列注释改回真源"
              f"（**只改注释，类型原样重写**）：\n")
        for s in stmts:
            print(f"  {s};")
        print("\n看过没问题再加 --apply 执行。只动元数据，不动数据。")
        return 0

    import athena
    c = athena.Client()
    for i, (s, (t, col, _ty, _n)) in enumerate(zip(stmts, fixes), 1):
        print(f"[{i}/{len(stmts)}] {t}.{col}")
        c.execute(s, fetch=False)
    print(f"\n{len(stmts)} 列的注释已改回真源。")
    return 0


# ---------------------------------------------- 语义层 / 指标层
#
# 这两个函数（`card_columns` / `metric_columns`）原来是用 `importlib` 从
# `scripts/glue/reconcile.py` 里加载的，理由是"读的是 knowledge/ 和 metrics_def.py，
# 跟查询引擎没关系，复制一份必然漂移"。第一句是对的，结论错了：Redshift 退役之后
# `scripts/glue/` 是**死路径**（AGENTS.md 明写"不要照着走"），而 L2 每次对账都要走进去
# 加载它。后果已经发生了——那份"v2 的"代码里现在写着 Athena 的 `COLUMN_NOT_FOUND`
# 和 `meta_snapshot` 锚点，也就是说它其实一直在被当活代码改，只是放错了地方。一个
# "已退役"的目录里躺着活代码，比两份会漂移的代码更难发现。
#
# 所以搬过来。`scripts/glue/reconcile.py` 原样留着，作为 v2 的历史记录不再被引用。

CARD_FIELD_SECTION = re.compile(r"^##\s*(表结构|字段)\s*$")
CARD_ANY_SECTION = re.compile(r"^##\s+\S")


def card_columns() -> dict[str, set[str]]:
    """语义层：从 knowledge/domains/**/<表>.md 的**「表结构」小节**里抽列名。

    必须限定小节。第一版扫了卡片里所有 markdown 表格，结果把「字段枚举值」小节里的
    枚举行（`| paid | 已支付 |`、`| male | 男 |`）也当成了列名，报出 40 多处假漂移。
    **带假阳性的检查器比没有更糟**：人看两眼就不再信它，真问题也一起被忽略。
    """
    out: dict[str, set[str]] = {}
    base = ROOT / "knowledge" / "domains"
    for p in base.rglob("*.md"):
        name = p.stem
        if name.startswith("_"):
            continue
        cols: set[str] = set()
        in_fields = False
        for line in p.read_text(encoding="utf-8").splitlines():
            if CARD_FIELD_SECTION.match(line.strip()):
                in_fields = True
                continue
            if in_fields and CARD_ANY_SECTION.match(line.strip()):
                in_fields = False                      # 离开表结构小节
                continue
            if not in_fields or not line.strip().startswith("|"):
                continue
            cells = [c.strip().strip("`*") for c in line.strip().strip("|").split("|")]
            if cells and re.fullmatch(r"[a-z_][a-z0-9_]*", cells[0] or ""):
                cols.add(cells[0])
        if cols:
            out[name] = cols
    return out


# SQL 关键字与内建函数。D 检查要从指标 SQL 里挑出「像列名但表里没有」的标识符，
# 靠正则分词必然把关键字一起捞进来，所以需要剔除。
#
# 第一版这个集合是「遇到假阳性就补一个」攒出来的，结果漏了 `IS NOT NULL` 里的 `is`，
# 在 mart_user_summary 上报了一处假漂移。关键字是**已知有限集**，不该增量攒——
# 一次列全，比每次踩到再补可靠。
#
# 搬过来时**一个词都没动**。这是个用来**剔除**的集合：加词只会让 D 检查少报，
# 而少报的正是"口径指向了不存在的列"这类最阴险的漂移。里面留着 Redshift 方言的词
# （`dateadd` / `datediff` / `super` / `timestamptz`）——删掉它们不会增强检查，
# 只会在有人跑历史 SQL 时凭空多几处误报。要加 Trino 侧的词，得先有一处真实误报。
SQL_RESERVED = {
    # 子句与运算符
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "join", "inner", "left", "right", "full", "outer", "on", "using", "as",
    "and", "or", "not", "is", "in", "like", "ilike", "between", "exists", "all",
    "any", "some", "union", "except", "intersect", "distinct", "asc", "desc",
    "case", "when", "then", "else", "end", "over", "partition", "filter",
    "null", "true", "false", "with", "recursive",
    # 类型
    "int", "integer", "bigint", "smallint", "decimal", "numeric", "real",
    "double", "precision", "float", "varchar", "char", "text", "boolean",
    "date", "timestamp", "timestamptz", "interval", "super",
    # 时间单位
    "year", "quarter", "month", "week", "day", "hour", "minute", "second",
    # 常用函数
    "sum", "count", "avg", "min", "max", "abs", "round", "floor", "ceil",
    "cast", "coalesce", "nullif", "greatest", "least", "nvl",
    "date_trunc", "dateadd", "datediff", "to_char", "to_date", "extract",
    "current_date", "current_timestamp", "row_number", "rank", "dense_rank",
    "lag", "lead", "first_value", "last_value", "substring", "concat", "length",
    # 本项目约定：dt 是所有日表的日期分区列名，出现在几乎每条口径里
    "dt",
}


def metric_columns() -> dict[str, set[str]]:
    """治理指标层：每个 metric 的目标表 → 该 metric 会引用到的列。

    不止 SQL 里字面出现的标识符。metric_layer 编译时还会**隐式注入**两类列，
    它们在注册表里是配置、不在 sql 字符串里，原来这个函数扫不到：
      · 时间锚点列（time_col，缺省 dt）—— 出现在 WHERE 和 (SELECT max(...)) 里
      · 维度列（dimensions → DIMENSIONS[d]['sql']）—— 出现在 SELECT / GROUP BY 里
    漏掉的代价是真实发生过的：repurchase_rate_30d 作用在 mart_user_summary（按用户
    建行、无 dt 列），带时间窗调用时在 Athena 上抛 COLUMN_NOT_FOUND，而对账**照样通过**，
    因为 dt 既不在它的 sql 里、又被 SQL_RESERVED 过滤掉。现在两类都算进来，
    这类"配置指向了表里不存在的列"在 L2 就红，不用等运行时。
    """
    import metrics_def
    sql_words = re.compile(r"[a-z_][a-z0-9_]*")
    bare_col = re.compile(r"^[a-z_][a-z0-9_]*$")
    reserved = SQL_RESERVED
    out: dict[str, set[str]] = {}
    for name, m in metrics_def.METRICS.items():
        table = m.get("table")
        if not table:
            continue
        parts = [m.get("sql", ""), m.get("numerator", ""), m.get("denominator", "")]
        words = set()
        for p in parts:
            words |= {w for w in sql_words.findall(str(p).lower()) if w not in reserved}
        # 被过滤的时间列：time_col 显式为 None = 该指标不支持时间窗，没有列要校验。
        time_col = m.get("time_col", "dt")
        if time_col:
            words.add(str(time_col).lower())
        # 全局锚点表（meta_snapshot.as_of_date）：它不是任何指标的 table，但每个带
        # 时间窗的调用都会查它。少了它 = 所有相对窗口在运行时炸，所以也要对账。
        anchor = re.search(r"max\((\w+)\)\s+from\s+(\w+)",
                           str(getattr(metrics_def, "ANCHOR_SQL", "")).lower())
        if anchor and time_col:
            out.setdefault(anchor.group(2), set()).add(anchor.group(1))
        # 维度列：只校验裸列名；表达式型维度（含函数/运算）跳过，交给 verify_doc_sql。
        for d in m.get("dimensions", []):
            dim_sql = str(metrics_def.DIMENSIONS.get(d, {}).get("sql", "")).lower()
            if bare_col.match(dim_sql):
                words.add(dim_sql)
        out.setdefault(table, set())
        out[table] |= words
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="元数据三方对账（S3 Tables + Glue + 卡片）")
    ap.add_argument("--catalog", default=os.getenv("GLUE_CATALOG_ID", ""),
                    help="Glue CatalogId，形如 <账号>:s3tablescatalog/<表桶>"
                         "（默认取 $GLUE_CATALOG_ID）")
    ap.add_argument("--database", default=os.getenv("ICEBERG_NAMESPACE", "app_analytics"))
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-west-2"))
    ap.add_argument("--strict", action="store_true", help="有问题则 exit 1")
    ap.add_argument("--fix-comments", action="store_true",
                    help="把注释漂移（H）打印成 ALTER TABLE 语句；不加 --apply 只打印")
    ap.add_argument("--apply", action="store_true",
                    help="配合 --fix-comments：真的执行那些 ALTER（会改 Glue 元数据）")
    ap.add_argument("--selftest", action="store_true", help="归一化自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if not a.catalog:
        import athena
        a.catalog = athena.glue_catalog_id()

    dec = declared()
    act = glue_state(a.catalog, a.database, a.region)
    cards = card_columns()
    metrics = metric_columns()

    print(f"声明态（DDL）      {len(dec)} 张表")
    print(f"实际态（Glue）     {len(act)} 张表   catalog={a.catalog} db={a.database}")
    print(f"语义层（卡片）     {len(cards)} 张表卡片")
    print(f"治理指标           {len(metrics)} 张表被指标引用")
    print()

    findings: list[tuple[str, str]] = []
    comment_fixes: list[tuple[str, str, str, str]] = []   # (表, 列, 类型, 期望注释)

    if not act:
        findings.append(("F", "Glue 里读不到任何表：表还没建，或 catalog ID / 凭证不对"))

    act_cols = {t: {c for c, _ty, _n in cols} for t, cols in act.items()}

    # A. Glue 有表、语义层没卡片
    for t in sorted(set(act) - set(cards)):
        where = CARD_EXEMPT.get(t)
        if where:
            p = ROOT / where
            if p.exists() and t in p.read_text(encoding="utf-8"):
                continue
            findings.append(("A", f"{t}：声明在 {where} 成文（CARD_EXEMPT），"
                                  f"但该文件里找不到它——豁免已失效"))
            continue
        findings.append(("A", f"{t}：Glue 里有，但 knowledge/ 里没有表卡片（未文档化）"))

    # B / I. 卡片列清单 ⟷ Glue 列清单，**双向**比。
    #
    # 两个方向的后果不一样，所以分两类而不是合成一句"不一致"：
    #   B（卡片有、Glue 没有）= 陈旧文档，agent 会照着编一个不存在的字段 → 查询直接报错
    #   I（Glue 有、卡片没有）= 漏记，agent 根本不知道这列存在 → 悄悄绕远路或答不出来
    # B 会炸所以迟早被发现；I 不炸，只是能力凭空少一块，全靠这里查出来。
    for t, cols in sorted(cards.items()):
        if t not in act_cols:
            continue
        gone = sorted(cols - act_cols[t])
        if gone:
            findings.append(("B", f"{t}：卡片写了但 Glue 里没有的列 {gone}（陈旧文档）"))
        missing = sorted(act_cols[t] - cols)
        if missing:
            findings.append(("I", f"{t}：Glue 里有但卡片「表结构」没写的列 {missing}"
                                  f"（agent 不会知道它存在）"))

    # C / G / H：声明态逐列比 —— 存在性、类型、注释
    for t, cols in sorted(dec.items()):
        if t not in act:
            findings.append(("C", f"{t}：声明了但 Glue 里没有（DDL 没跑完？）"))
            continue
        actual_by_name = {c: (ty, note) for c, ty, note in act[t]}
        for col, ty, note in cols:
            if col not in actual_by_name:
                findings.append(("C", f"{t}.{col}：声明了但 Glue 里没有（手工 ALTER？）"))
                continue
            a_ty, a_note = actual_by_name[col]
            if norm_type(ty) != norm_type(a_ty):
                findings.append(("G", f"{t}.{col}：类型不一致，"
                                      f"声明 {ty} / Glue {a_ty}"))
            # 注释只在真源写了的时候比：没写不算漂移，算"还没写"
            if note and norm_comment(note) != norm_comment(a_note):
                findings.append(("H", f"{t}.{col}：注释与真源不一致\n"
                                      f"        真源 {norm_comment(note)!r}\n"
                                      f"        Glue {norm_comment(a_note)!r}"))
                comment_fixes.append((t, col, ty, note))

    # D. 治理指标引用的列不存在
    for t, words in sorted(metrics.items()):
        if t not in act_cols:
            findings.append(("D", f"指标引用的表 {t} 在 Glue 里不存在（口径将静默失效）"))
            continue
        unknown = sorted(w for w in words if w not in act_cols[t] and w != t)
        if unknown:
            findings.append(("D", f"{t}：指标 SQL 引用了表里没有的标识符 {unknown}"))

    print("本脚本**没有**覆盖的东西（别把「对账通过」读成「全都验过了」）：")
    print("  · nullability —— Athena 的 Iceberg DDL 不接受 NOT NULL，")
    print("    v1 的非空约束在生成时降级成了列注释，这条路径没有可比的东西。")
    print("  · 治理层（最小权限角色 / Lake Formation 列级排除）—— 在 L4 验，不在这里：")
    print("    本脚本跑的是调用方身份（通常是 data lake admin），看到的是完整目录，")
    print("    连「哪些列被排除了」都看不见。断言在 scripts/lakehouse/governance.py。")
    print("  · SQL 能不能真跑 —— 那是 verify_doc_sql.py（拿 Athena EXPLAIN 过一遍文档）。")
    print("  · 列里装的**值** —— 本脚本只比表和列。卡片写 status='active' 而数据是")
    print("    'on_sale' 时这里全绿，SQL 也 EXPLAIN 通过，跑出来是空集。那是 verify_enums.py。")
    print("  · 口径算得对不对 —— 那是 verify_mart_parity.py。")
    print()

    if findings:
        by_kind: dict[str, list[str]] = {}
        for kind, msg in findings:
            by_kind.setdefault(kind, []).append(msg)
        labels = {"A": "未文档化的表", "B": "陈旧文档（幻觉来源）", "C": "生成链断裂",
                  "D": "治理口径失效", "G": "类型漂移", "H": "注释漂移",
                  "I": "卡片漏记的列", "F": "目录不可用"}
        print(f"发现 {len(findings)} 处问题：\n")
        for kind in sorted(by_kind):
            print(f"[{kind}] {labels.get(kind, '')}  {len(by_kind[kind])} 处")
            for msg in by_kind[kind][:20]:
                print(f"    - {msg}")
            if len(by_kind[kind]) > 20:
                print(f"    …… 另有 {len(by_kind[kind]) - 20} 处")
            print()
    else:
        print(f"对账通过 ✅  {len(act)} 张表的列名、类型、注释三方一致")

    # --fix-comments 只管 H 那一类。放在报告**之后**：先前它 return 得太早，
    # 带着一处类型漂移跑 --fix-comments 会什么都看不到，只看到注释被修好了。
    if a.fix_comments:
        print()
        fix_comments(comment_fixes, apply=a.apply)
        if a.apply and comment_fixes:
            print("再跑一次 `reconcile.py` 确认 H 归零。")

    return 1 if (findings and a.strict) else 0


def selftest() -> int:
    bad = 0
    for src, want in [
        ("list<string>", "array<string>"),
        ("array<string>", "array<string>"),
        ("list<list<bigint>>", "array<array<bigint>>"),
        ("decimal(12, 2)", "decimal(12,2)"),
        ("DECIMAL(12,2)", "decimal(12,2)"),
        ("timestamp", "timestamp"),
        # 刻意不等价：长度信息不该被当成 string
        ("varchar(50)", "varchar(50)"),
    ]:
        got = norm_type(src)
        if got != want:
            bad += 1
            print(f"  FAIL norm_type({src!r}) → {got!r}，期望 {want!r}")

    for src, want in [
        ("  主键，用户ID  ", "主键，用户ID"),
        ("实付金额\t（GMV 用这列）", "实付金额 （GMV 用这列）"),
    ]:
        got = norm_comment(src)
        if got != want:
            bad += 1
            print(f"  FAIL norm_comment({src!r}) → {got!r}，期望 {want!r}")

    # 关键负例：注释比对绝不能宽松到把口径相反的两句判成相同
    if norm_comment("含运费") == norm_comment("不含运费"):
        bad += 1
        print("  FAIL 注释归一化把「含运费」和「不含运费」判成了相同")

    ddl = ("CREATE TABLE mart_daily_kpi (\n"
           "  dt date COMMENT '日期',\n"
           "  gmv decimal(14,2) COMMENT '成交额（含运费、不扣退款）',\n"
           "  tags array<string> COMMENT 'don\\'t split me'\n"
           ");")
    parsed = _parse_iceberg_ddl(ddl)
    want = [("dt", "date", "日期"),
            ("gmv", "decimal(14,2)", "成交额（含运费、不扣退款）"),
            ("tags", "array<string>", "don't split me")]
    if parsed.get("mart_daily_kpi") != want:
        bad += 1
        print(f"  FAIL _parse_iceberg_ddl → {parsed.get('mart_daily_kpi')}")
        print(f"       期望 {want}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  类型归一化 7 项、注释归一化 3 项、Iceberg DDL 解析 1 项")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
