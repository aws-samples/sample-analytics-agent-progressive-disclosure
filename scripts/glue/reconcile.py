#!/usr/bin/env python3
"""三方元数据对账：DDL 声明态 ⟷ Glue 实际态 ⟷ 知识库语义层。

## 这个脚本是本次架构升级真正的产出物

元数据从 markdown 换成 Glue Catalog 本身不解决任何问题——**md 的原罪不是格式，
是没人验证它**。原来的双真源（`database/*.sql` 的 DDL 与 `knowledge/` 的表卡片）
在 35 张表规模下人还能盯住，改个字段名忘了同步卡片，agent 下一秒就开始编字段，
而且编得很自信。这是 NL→SQL 幻觉的头号来源。

所以 Glue 在这套架构里的定位是**环上的验证点，不是真源**：

    schema_manifest.yaml / database/*.sql   ← 声明态（人写、进 git、走评审）
              │ render / DDL
              ▼
        Redshift 实际建出来的表
              │ scripts/glue/register_catalog.py
              ▼
        Glue Data Catalog                   ← 实际态（生成的，不可手改）
              │
        知识库表卡片 knowledge/domains/**   ← 语义层（人写的判断：何时用/坑/口径）

这跟 Terraform 的 plan/apply、K8s 的 desired/actual 是同构的：数据字典也该有
声明态和实际态，只有一份的那种叫文档，两份加对账的那种叫平台。

## 七类检查

| 编号 | 症状 | 说明 |
|---|---|---|
| A | Glue 有表、语义层没卡片 | 未文档化的表，agent 看得见但不知道怎么用 |
| B | 卡片引用的列 Glue 里不存在 | **陈旧文档**，agent 会照着编字段 |
| C | DDL 声明的列 Glue 里没有 | 生成链断了：手工 ALTER、迁移半途失败 |
| D | 治理指标 SQL 引用的列不存在 | 官方口径静默失效，最阴险的一类 |
| E | 挂了 DDM 的表能被 `GetTable` 读到 | 脱敏没挂上，治理兜底层失效 |
| F | Glue 里没有任何表 | 尚未注册，或注册失败 |
| G | 分类清单与 schema / DDM / GRANT 不一致 | 新列未审阅，或声明的治理处置没有落地 |

## E 的语义：一条被实测推翻的设计假设

原先这里写的是反向断言「挂了 DDM 的 PII 表**必须**缺席于 Glue 目录」，依据是对
Redshift 文档的一处误读。实测结论相反，记录在此以免重犯：

- `GetTables`（列表）返回**全部** 48 张表，**带完整列清单**，`users.email`、
  `users.phone`、`user_profiles.birth_date` 这些 PII 列名照常暴露。
- `GetTable`（单表）对且仅对这 2 张挂了 DDM 的表报 `EntityNotFoundException`，
  其余 46 张全部成功。
- `svv_attached_masking_policy.is_masking_datashare_on = 't'` 证明这些表**是**被
  共享出去的，且脱敏在 datashare 路径上生效——不是「被排除」。

误读的原文是「DDM 策略**不能挂到** datasharing 表上」（约束的是策略的附着对象），
不是「挂了 DDM 的表不进 datashare」。方向搞反了。

所以：**Glue federated catalog 是 schema 投影，不是权限投影**。它既不反映 Redshift
的 GRANT，也不完整反映 DDM。把「目录里看不见」当成一层访问控制是错的。真正生效的是
查询时的两层：GRANT 挡表、DDM 脱敏列。E 检查因此改成验证**机制确实生效**（DDM 表在
GetTable 路径上确实被挡），而 GetTables 的泄露作为常驻说明打印，不计入 findings——
把永远修不掉的已知行为反复报成问题，只会训练人忽略 findings，正是本项目踩过的第 10 坑。

## 用法

    python3 scripts/glue/reconcile.py --catalog <federated-catalog-id>
    python3 scripts/glue/reconcile.py --catalog <id> --strict   # 有问题就 exit 1（CI 用）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "gen"))
sys.path.insert(0, str(ROOT / "scripts" / "governance"))
sys.path.insert(0, str(ROOT / "backend"))

from classification import (  # noqa: E402
    expected_governance,
    governance_findings,
    load_inventory,
    structural_findings,
)

# DDM 表与未授权表不再在这里手工维护；它们由 data_classification.yaml 派生，
# 避免“治理 SQL 改了但检查器名单没改”的第二真源。

# 不放域卡片、但在别处成文的表：表名 → 成文位置（相对仓库根）。
#
# meta_snapshot 存数据集的"今天"锚点，是**全局**关注点而非某个业务域的表——每个带时间的
# 查询都要用它，塞进 knowledge/domains/<某域>/ 会给出错误的路由信号。
#
# 这不是盲跳过：下面的 A 检查会真去读那个文件、确认它确实提到了这张表。豁免本身也要对账，
# 否则「加进豁免名单」就变成了掩盖问题的后门。
CARD_EXEMPT = {"meta_snapshot": "knowledge/connection.md"}


def declared_columns() -> dict[str, set[str]]:
    """声明态：从 database/01-08_*.sql 解析。"""
    import ddl
    return {t: {c for c, _ in cols} for t, cols in ddl.parse_all().items()}


def _leaf_catalogs(g, catalog_id: str) -> list[str]:
    """把顶层 federated catalog 展开成「装得下 database 的那一层」catalog id 列表。

    Redshift federated catalog 比普通 Glue catalog **多一层**，实测层级是：

        123456789012:analytics_agent_rs                 ← 顶层（create-catalog 建的）
          └─ 123456789012:analytics_agent_rs/app_analytics   ← Redshift 的 database
               └─ database "public"                          ← Redshift 的 schema
                    └─ table

    也就是 Redshift 的 *database* 成了子 catalog，Redshift 的 *schema* 才映射成 Glue
    的 database。顶层 catalog 上直接 get_databases 是取不到东西的，必须先下潜一层。
    调用者给顶层或子 catalog 都能用。
    """
    from botocore.exceptions import ClientError
    try:
        kids = g.get_catalogs(ParentCatalogId=catalog_id).get("CatalogList", [])
    except ClientError:
        return [catalog_id]
    # 只有顶层才有子 catalog；`dev` 是 Redshift 自带的空默认库，跳过以免噪声
    leaves = [c["CatalogId"] for c in kids
              if not c["CatalogId"].endswith("/dev")]
    return leaves or [catalog_id]


def glue_columns(catalog_id: str, database: str | None,
                 region: str) -> dict[str, set[str]]:
    """实际态：从 Glue Data Catalog 读，返回 表名 → 列名集合。

    用 GetTables（列表）而不是逐表 GetTable：列表路径能拿到全部表且自带完整列清单，
    而 GetTable 在挂了 DDM 的表上会报 EntityNotFound（见模块 docstring「E 的语义」）。
    对账要的是「Redshift 里实际有什么」，所以取列表路径这个更完整的视图。
    """
    import boto3
    g = boto3.client("glue", region_name=region)
    out: dict[str, set[str]] = {}

    for cat in _leaf_catalogs(g, catalog_id) if catalog_id else [""]:
        dbs: list[str] = [database] if database else []
        if not dbs:
            kw = {"CatalogId": cat} if cat else {}
            try:
                p = g.get_paginator("get_databases")
                for page in p.paginate(**kw):
                    dbs += [d["Name"] for d in page.get("DatabaseList", [])]
            except Exception as e:
                print(f"  （列 Glue catalog '{cat}' 的 database 失败："
                      f"{type(e).__name__}: {e}）")
                continue
        for db in dbs:
            kw = {"DatabaseName": db}
            if cat:
                kw["CatalogId"] = cat
            try:
                p = g.get_paginator("get_tables")
                for page in p.paginate(**kw):
                    for t in page.get("TableList", []):
                        cols = {c["Name"] for c in
                                t.get("StorageDescriptor", {}).get("Columns", [])}
                        cols |= {c["Name"] for c in t.get("PartitionKeys", [])}
                        out[t["Name"]] = cols
            except Exception as e:                   # 权限/目录形态问题不该整体崩掉
                print(f"  （读 Glue database '{cat}/{db}' 失败："
                      f"{type(e).__name__}: {e}）")
    return out


def glue_gettable_readable(catalog_id: str, database: str | None,
                           region: str, tables: list[str]) -> dict[str, bool]:
    """逐表探 GetTable 是否能读到，用于验证 DDM 是否真的在目录路径上生效。

    这是 E 检查的取证手段：挂了 DDM 的表在这条路径上**应当**报 EntityNotFound。
    能读到说明脱敏没挂上（比如 04_governance.sql 没跑、或 DETACH 了没重挂）。
    """
    import boto3
    from botocore.exceptions import ClientError
    g = boto3.client("glue", region_name=region)
    out: dict[str, bool] = {}
    for cat in _leaf_catalogs(g, catalog_id) if catalog_id else [""]:
        for db in ([database] if database else ["public"]):
            for t in tables:
                if out.get(t):
                    continue
                kw = {"DatabaseName": db, "Name": t}
                if cat:
                    kw["CatalogId"] = cat
                try:
                    g.get_table(**kw)
                    out[t] = True
                except ClientError:
                    out.setdefault(t, False)
    return out


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
    """治理指标层：每个 metric 的目标表 → 其 SQL 里引用的标识符。"""
    import metrics_def
    sql_words = re.compile(r"[a-z_][a-z0-9_]*")
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
        out.setdefault(table, set())
        out[table] |= words
    return out


def redshift_governance_state(region: str) -> tuple[dict[tuple[str, str], str], set[str]]:
    """用管理员/owner 视角读取实际 DDM 与角色授权；只读角色看这些视图会得到 0 行。"""
    sys.path.insert(0, str(ROOT / "scripts" / "redshift"))
    from rsql import Client

    client = Client(region=region)
    mask_rows = client.execute(
        "SELECT table_name, input_columns, policy_name "
        "FROM svv_attached_masking_policy WHERE lower(grantee)='public'"
    ).get("rows", [])
    grant_rows = client.execute(
        "SELECT relation_name FROM svv_relation_privileges "
        "WHERE identity_name='analytics_agent_ro' AND privilege_type='SELECT'"
    ).get("rows", [])
    if not mask_rows or not grant_rows:
        raise RuntimeError(
            "治理系统视图返回 0 行；G1/G2 必须使用 admin/owner 凭证，"
            "只读审计角色请改做掩码值与 permission denied 负测"
        )

    attached: dict[tuple[str, str], str] = {}
    for table, raw_columns, policy in mask_rows:
        columns = re.findall(r"[a-z_][a-z0-9_]*", str(raw_columns).lower())
        for column in columns:
            attached[(str(table), column)] = str(policy)
    granted = {str(row[0]) for row in grant_rows}
    return attached, granted


def main() -> int:
    ap = argparse.ArgumentParser(description="元数据三方对账")
    ap.add_argument("--catalog", default="", help="Glue federated catalog id（形如 acct:name）")
    ap.add_argument("--database", help="只查这个 Glue database（省略则遍历全部）")
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument("--strict", action="store_true", help="有问题则 exit 1")
    ap.add_argument("--governance-state", action="store_true",
                    help="以 admin/owner 凭证对账实际 DDM 与 analytics_agent_ro GRANT")
    a = ap.parse_args()

    declared = declared_columns()
    cards = card_columns()
    metrics = metric_columns()
    actual = glue_columns(a.catalog, a.database, a.region)
    inventory = load_inventory()
    expected_masks, ungranted_tables = expected_governance(inventory)
    ddm_tables = sorted({table for table, _ in expected_masks})

    findings: list[tuple[str, str]] = []

    print(f"声明态（DDL）      {len(declared)} 张表")
    print(f"实际态（Glue）     {len(actual)} 张表")
    print(f"语义层（卡片）     {len(cards)} 张表卡片")
    print(f"治理指标           {len(metrics)} 张表被指标引用")
    print(f"数据分类清单       {len(inventory['tables'])} 张表已审阅")
    print()

    if not actual:
        findings.append(("F", "Glue Catalog 里读不到任何表：尚未注册，或注册失败/无权限"))
    else:
        findings += [("G", msg) for msg in structural_findings(actual, inventory)]

    # A. Glue 有表、语义层没卡片
    for t in sorted(set(actual) - set(cards)):
        where = CARD_EXEMPT.get(t)
        if where:
            # 豁免也要对账：真去读那个文件，确认它确实提到了这张表。
            # 否则豁免名单就成了掩盖问题的后门。
            p = ROOT / where
            if p.exists() and t in p.read_text(encoding="utf-8"):
                continue
            findings.append(("A", f"{t}：声明在 {where} 成文（CARD_EXEMPT），"
                                  f"但该文件里找不到它——豁免已失效"))
            continue
        findings.append(("A", f"{t}：Glue 里有，但 knowledge/ 里没有表卡片（未文档化）"))

    # B. 卡片引用的列，Glue 里不存在（陈旧文档 → agent 会编字段）
    for t, cols in sorted(cards.items()):
        if t not in actual:
            continue
        gone = sorted(cols - actual[t])
        if gone:
            findings.append(("B", f"{t}：卡片写了但 Glue 里没有的列 {gone}（陈旧文档）"))

    # C. DDL 声明了、Glue 里没有（生成链断裂）
    for t, cols in sorted(declared.items()):
        if t not in actual:
            findings.append(("C", f"{t}：DDL 声明了但 Glue 里没有（迁移未完成？）"))
            continue
        gone = sorted(cols - actual[t])
        if gone:
            findings.append(("C", f"{t}：DDL 有但 Glue 没有的列 {gone}（手工 ALTER？）"))

    # D. 治理指标引用的列不存在
    for t, words in sorted(metrics.items()):
        if t not in actual:
            findings.append(("D", f"指标引用的表 {t} 在 Glue 里不存在（口径将静默失效）"))
            continue
        unknown = sorted(w for w in words if w not in actual[t] and w != t)
        # 只报"看起来像列名但表里没有"的，函数名等已在 reserved 里剔除
        if unknown:
            findings.append(("D", f"{t}：指标 SQL 引用了表里没有的标识符 {unknown}"))

    # E. 挂了 DDM 的表在 GetTable 路径上必须被挡住（验证机制生效，不是验证缺席）
    readable = glue_gettable_readable(a.catalog, a.database, a.region, ddm_tables)
    for t in ddm_tables:
        if readable.get(t):
            findings.append(("E", f"{t}：挂了 DDM 的表却能被 GetTable 读到，"
                                  f"脱敏可能没挂上（检查 04_governance.sql 是否跑过）"))

    # 常驻说明：不计入 findings。见 docstring「E 的语义」——这是 AWS 的既有行为，
    # 修不掉，反复报成问题只会让人不再信 findings。
    masked_column_names = {column for _, column in expected_masks}
    leaked = {t: sorted(actual[t] & masked_column_names)
              for t in ddm_tables if t in actual}
    leaked = {t: v for t, v in leaked.items() if v}
    print("目录可见面实测（不是缺陷，是必须知道的边界）：")
    print(f"  GetTable 被挡住的 DDM 表   {[t for t in ddm_tables if not readable.get(t)]}")
    if leaked:
        print(f"  但 GetTables 仍暴露列名   {leaked}")
    print(f"  未 GRANT 的表仍在目录里    "
          f"{[t for t in sorted(ungranted_tables) if t in actual]}（目录不反映 GRANT）")
    print("  → Glue federated catalog 是 schema 投影，不是权限投影。")
    print("    真正生效的防线是查询时的 GRANT 与 DDM，别把目录可见性当访问控制。")
    print()

    if a.governance_state:
        try:
            attached, granted = redshift_governance_state(a.region)
            findings += [("G", msg) for msg in governance_findings(inventory, attached, granted)]
            print(f"治理实际态对账：DDM {len(attached)} 列，SELECT {len(granted)} 张表")
        except Exception as exc:  # 身份不可见也必须显式失败，不能把 0 行当成无策略
            findings.append(("G", f"治理实际态不可验证：{type(exc).__name__}: {exc}"))
    else:
        print("治理实际态未查询（加 --governance-state；需要 admin/owner 凭证）")
    print()

    if not findings:
        if a.governance_state:
            print("对账通过 ✅  三方一致，分类覆盖完整，DDM/GRANT 与声明一致")
        else:
            print("对账通过 ✅  三方一致，分类覆盖完整（未查询 GRANT 实际态）")
        return 0

    by_kind: dict[str, list[str]] = {}
    for kind, msg in findings:
        by_kind.setdefault(kind, []).append(msg)
    labels = {"A": "未文档化的表", "B": "陈旧文档（幻觉来源）", "C": "生成链断裂",
              "D": "治理口径失效", "E": "治理防护失效", "F": "目录不可用",
              "G": "治理覆盖缺口"}
    print(f"发现 {len(findings)} 处问题：\n")
    for kind in sorted(by_kind):
        print(f"[{kind}] {labels.get(kind, '')}  {len(by_kind[kind])} 处")
        for msg in by_kind[kind][:20]:
            print(f"    - {msg}")
        if len(by_kind[kind]) > 20:
            print(f"    …… 另有 {len(by_kind[kind]) - 20} 处")
        print()
    return 1 if a.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
