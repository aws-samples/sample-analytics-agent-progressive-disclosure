"""`/api/catalog` 的数据装配：把元数据的三个来源聚成一份 UI 能直接渲染的结构。

## 为什么要有这个接口

前端原来把表清单、表数、行数、字段全部写死在 HTML 和 i18n 字典里（39 张表、
「~19万行」、「PostgreSQL」）。数据涨到 8000 万行、表数到 48 之后这些数字全错了，
而且**错得看不出来**——它们是静态文本，不会因为库变了就报警。

上 Glue Data Catalog 的意义正在于元数据有了一份机器可读的实际态。所以 UI 不该再自己
抄一份，而应该读它。这个接口就是那条通道：

    Glue Data Catalog     → 表清单 + 字段（**实际态**：库里到底有什么）
    Athena count(*)        → 行数（Iceberg 走元数据，精确且零扫描）
    Lake Formation         → 治理现状（授权面）
    knowledge/domains/     → 域分组（agent 读的就是这套目录结构，UI 与它同源）
    schema_manifest.yaml   → 派生层的 layer / status（dwd / dws / ads / noise）

每个来源各管一段，没有一段是前端硬编码的。改了库、加了表，UI 下次刷新自动跟上。

## 行数：从「快而估」换成了「慢而准」

v2 用 `svv_table_info.tbl_rows`——Redshift 自己维护的统计值，2–3 秒返回全部表，
但本质是估算，UI 上因此标着「约」。

Iceberg 这边不一样：`count(*)` **走清单文件的元数据，不读数据文件**，实测扫描
0 字节。所以这里拿到的是**精确值**，不是估算（UI 上那个「约」现在其实可以去掉，
但那是前端的事）。

代价是延迟。Athena 每条查询有约 2 秒的固定开销（提交 → 排队 → 规划 → 返回），
48 张表怎么组织都躲不开：

    48 分支 UNION ALL             26.0s   ← 规划在 Athena 内部串行
    单行 48 个标量子查询           24.2s
    48× UNION ALL 走 $partitions  15.8s
    并发 count(*) ×8              13.2s
    并发 count(*) ×16              7.8s   ← 采用

所以走并发。整个接口有 300 秒缓存，8 秒落在缓存未命中的那一次上；
`timings.row_counts_ms` 会一并回传，让这笔开销是可见的而不是神秘的卡顿。

## 治理面板：只在**接上了**的时候才画

v2 的治理数据来自 Redshift 的 `svv_relation_privileges` / `svv_attached_masking_policy`。
Redshift 退役后这两个视图不存在了，v3 的等价物是 Lake Formation 的列级授权
（`scripts/lakehouse/governance.py`）。

**没有拿当前身份的授权面充数。** 原因很直接：跑这个服务的身份通常是 data lake admin，
LF 的权限检查对它整体绕过，列出来会是「48/48 全部授权」——那等于把**我们没有做限制**
渲染成**治理生效了**，比空着更糟。所以只有显式设了 `AGENT_ROLE_ARN`（agent 专用的
最小权限角色）才去读 LF，否则 `available=False` 并回传 `reason`，让 UI 说清是
「这一层没接上」而不是「查不到」。v2 的 `web/catalog.json` 快照真干过前一件事。

## 降级行为

Glue 读不到时（没建 catalog、没配 `GLUE_CATALOG_ID`、或权限不足）**不报错**，
退回 `information_schema` 并在响应里把 `source` 标成 `information_schema`。
克隆本仓库但没做 Glue 那一步的人照样能跑起来看到正确数字，只是少了「元数据来自
统一目录」这个演示点。静默降级是有害的，所以 `source` 一定回传，UI 会显示出来。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import db

ROOT = Path(__file__).resolve().parent.parent

# 目录内容变化很慢（改表结构这种动作），但 UI 每次开页都会拉一次。
# 缓存尤其要紧：行数那一段是 48 条 Athena 查询（约 8 秒，见 docstring），
# 不缓存等于每次开页都掏这笔钱。演示时改了库想立刻看到，
# 用 /api/catalog?refresh=1 强制重算。
CACHE_TTL = float(os.getenv("CATALOG_CACHE_TTL", "300"))

GLUE_CATALOG_ID = os.getenv("GLUE_CATALOG_ID", "")
REGION = os.getenv("AWS_REGION", "us-west-2")
NAMESPACE = os.getenv("ICEBERG_NAMESPACE", "app_analytics")
# S3 Tables 的表桶里会自带一个空的 `default` namespace（Athena 建的），不是业务库。
SKIP_DATABASES = {"default"}
# agent 专用的最小权限角色 ARN。**只有显式设了才读 LF**，见模块 docstring
# 「治理面板现在是空的」——拿 admin 身份的授权面充数比空着更糟。
AGENT_ROLE_ARN = os.getenv("AGENT_ROLE_ARN", "")
# 行数查询的并发度。8 → 13.2s，16 → 7.8s（实测，见 docstring）。
# Athena 默认的 DML 并发配额是 20+，留点余量给 agent 自己的查询。
ROW_COUNT_WORKERS = int(os.getenv("CATALOG_ROW_COUNT_WORKERS", "16"))

# 域的展示顺序。跟前端原有的卡片顺序一致，避免每次刷新顺序乱跳；
# 出现不在此列的新域时追加到末尾，不丢。
DOMAIN_ORDER = ["user", "behavior", "transaction", "product", "social",
                "marketing", "attribution", "experiment", "mart"]

_cache: dict | None = None
_cache_at = 0.0


# ------------------------------------------------------------------ 实际态

def _glue_tables() -> dict[str, list[str]]:
    """从 Glue Data Catalog 读表清单与字段。读不到就抛，由调用方降级。

    用 `GetTables`（列表）而不是逐表 `GetTable`：列表路径一次拿全并自带完整字段。

    ## GLUE_CATALOG_ID 的两种写法都收

    S3 Tables 联邦目录是两级：

        <账号>:s3tablescatalog                    ← 顶层，一个账号一个
          └─ <账号>:s3tablescatalog/<表桶名>       ← 每个表桶一个子目录
               └─ Glue database "app_analytics"   ← Iceberg namespace 直接映射

    给顶层：`get_databases` 会报 `EntityNotFoundException: The specified bucket does
    not exist`（顶层的 Identifier 是 `bucket/*`，不指向任何具体桶），必须先
    `get_catalogs` 下潜一层。
    给子目录：`get_catalogs` 反过来报 `InvalidInputException: GetCatalogs is only
    supported for name...`。

    两种 ID 在本仓库里都在用（`athena.glue_catalog_id()` 给的是子目录形式），所以
    这里两种都收：先试下潜，下潜不了就当它已经是叶子。写错一种就报
    EntityNotFound，而那个报错完全看不出是层级问题——踩过一次就够了。
    """
    if not GLUE_CATALOG_ID:
        raise RuntimeError("未配置 GLUE_CATALOG_ID")
    import boto3
    from botocore.exceptions import ClientError
    g = boto3.client("glue", region_name=REGION)

    try:
        leaves = [c["CatalogId"] for c in
                  g.get_catalogs(ParentCatalogId=GLUE_CATALOG_ID).get("CatalogList", [])
                  ] or [GLUE_CATALOG_ID]
    except ClientError:
        leaves = [GLUE_CATALOG_ID]          # 给的已经是叶子

    out: dict[str, list[str]] = {}
    for cat in leaves:
        for dbname in [d["Name"] for d in
                       g.get_databases(CatalogId=cat).get("DatabaseList", [])
                       if d["Name"] not in SKIP_DATABASES]:
            tok = None
            while True:
                kw = {"CatalogId": cat, "DatabaseName": dbname}
                if tok:
                    kw["NextToken"] = tok
                r = g.get_tables(**kw)
                for t in r.get("TableList", []):
                    out[t["Name"]] = [c["Name"] for c in
                                      t.get("StorageDescriptor", {}).get("Columns", [])]
                tok = r.get("NextToken")
                if not tok:
                    break
    if not out:
        raise RuntimeError("Glue 目录里没有表")
    return out


def _schema_tables() -> dict[str, list[str]]:
    """降级路径：直接读引擎的 information_schema。"""
    schema = "public" if db.BACKEND == "postgres" else NAMESPACE
    rows = db.system_query(
        "SELECT table_name, column_name FROM information_schema.columns "
        f"WHERE table_schema='{schema}' ORDER BY table_name, ordinal_position")
    out: dict[str, list[str]] = {}
    for t, c in rows:
        out.setdefault(t, []).append(c)
    return out


def _row_counts(names: list[str]) -> dict[str, int]:
    """行数。取不到就返回空 dict，UI 不显示行数而不是显示 0。

    Athena 侧是**精确值**（Iceberg 的 count(*) 走清单元数据，零扫描），代价是每张表
    一次查询。并发发，见模块 docstring 里那张实测对比表。单张失败不影响其余：
    少一个数字比整块行数消失好。
    """
    if db.BACKEND == "postgres":
        try:
            rows = db.system_query(
                "SELECT relname, reltuples FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind='r'")
            return {t: int(float(n or 0)) for t, n in rows}
        except Exception:
            return {}

    from concurrent.futures import ThreadPoolExecutor
    safe = [t for t in names if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t)]

    def one(t: str):
        try:
            rows = db.system_query(f'SELECT count(*) FROM "{t}"')
            return t, int(rows[0][0])
        except Exception:
            return t, None

    if not safe:
        return {}
    with ThreadPoolExecutor(min(len(safe), ROW_COUNT_WORKERS)) as ex:
        return {t: n for t, n in ex.map(one, safe) if n is not None}


# ------------------------------------------------------------------ 治理现状

def _governance(tables: "dict | None" = None) -> dict:
    """agent 角色的授权面。从 Lake Formation 现查，不写死。

    `tables` 是本 namespace 的表名清单（值不用，只用键）。要它是因为 LF 的
    `list_permissions` 只能逐表问——见下面 `scan()` 上方的注释。传空就只查得到
    表通配那一条，等于什么都查不到。

    这一段是 UI 治理面板的数据源。它回答的是客户最常问的那个问题：
    「AI 会不会不小心读到 PII」——答案不该是"我们叮嘱它别读"，而该是这里列出来的
    硬约束。

    设了 `AGENT_ROLE_ARN` 才查，否则 `available=False` 并带上 `reason`，让 UI 说
    「这一层还没接上」而不是「查不到」或者更糟的「一切正常」。理由是这个字段没配时
    跑服务的身份通常是 data lake admin，LF 的权限检查对它整体绕过——去查它的授权面
    会得到"全部授权"，那是把**我们没做限制**渲染成**治理生效了**。v2 的
    `web/catalog.json` 快照真干过这事。

    `masked` 这个键的语义在 v3 变了，**名字为了前端契约保留**：v2 是 Redshift 动态
    脱敏（列在，值是掩码），v3 是 Lake Formation 列级排除（列**根本不在**授权面里，
    `SELECT *` 里没有它，点名查它报 COLUMN_NOT_FOUND）。LF 没有值级掩码原语，
    取舍写在 `scripts/lakehouse/governance.py` 的 docstring 里。
    """
    out: dict = {"role": AGENT_ROLE_ARN or None, "granted": [], "masked": [],
                 "available": False}
    if not AGENT_ROLE_ARN:
        out["reason"] = ("未设 AGENT_ROLE_ARN：后端在用自己的凭证查数（本地开发通常是"
                         "管理员身份），那不是 agent 的边界。建角色并接上："
                         "python3 scripts/lakehouse/governance.py --apply")
        return out
    if db.BACKEND == "postgres":
        out["reason"] = "postgres 后端没有 Lake Formation"
        return out
    try:
        import boto3
        lf = boto3.client("lakeformation", region_name=REGION)
        granted: set[str] = set()
        excluded: dict[str, set[str]] = {}

        def scan(resource: dict) -> None:
            tok = None
            while True:
                kw = {"Principal": {"DataLakePrincipalIdentifier": AGENT_ROLE_ARN},
                      "Resource": resource}
                if tok:
                    kw["NextToken"] = tok
                r = lf.list_permissions(**kw)
                for p in r.get("PrincipalResourcePermissions", []):
                    if "SELECT" not in p.get("Permissions", []):
                        continue
                    res = p.get("Resource", {})
                    # 治理层发的是列级授权（TableWithColumns + ColumnWildcard），
                    # 表级（Table）那种只在有人手工发过宽授权时出现——两种都要认，
                    # 少认一种的表现是治理面板显示"0 张表已授权"，看起来像坏了。
                    if "TableWithColumns" in res:
                        t = res["TableWithColumns"]
                        name = t.get("Name", "")
                        cw = t.get("ColumnWildcard")
                        if cw is not None and cw.get("ExcludedColumnNames"):
                            excluded.setdefault(name, set()).update(
                                cw["ExcludedColumnNames"])
                    else:
                        t = res.get("Table", {})
                        # TableWildcard 的一条授权覆盖整库，展开成"全部表"由调用方处理
                        name = "*" if "TableWildcard" in t else t.get("Name", "")
                    granted.add(name)
                tok = r.get("NextToken")
                if not tok:
                    return

        # **必须逐表问**。`list_permissions` 的 `Resource` 是精确过滤器而不是范围
        # 过滤器：拿 `Table{TableWildcard:{}}` 去问，只会返回真的发在"表通配"这个
        # 资源上的授权，那 47 条 `TableWithColumns` 一条都不返回。这个洞的表现最坏
        # 的地方在于它**不报错**：`available=True` 而 `granted` 为空，面板显示
        # "0 / 48 已授权" 并把全部表列成未授权——UI 看起来在正常工作，说的却全是反的。
        # 同一个坑在 scripts/lakehouse/governance.py::actual_grants 里踩过一次。
        for name in sorted(tables or ()):
            scan({"Table": {"CatalogId": GLUE_CATALOG_ID,
                            "DatabaseName": NAMESPACE, "Name": name}})
        # 表通配授权只在这个形状下查得到；它一条就能盖掉全部列级排除。
        scan({"Table": {"CatalogId": GLUE_CATALOG_ID, "DatabaseName": NAMESPACE,
                        "TableWildcard": {}}})
        out["granted"] = sorted(granted - {""})
        out["masked"] = [{"table": t, "columns": sorted(c)}
                         for t, c in sorted(excluded.items())]
        out["available"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ------------------------------------------------------------------ 分组与分层

def _domain_map() -> dict[str, str]:
    """表 → 域。来源是 `knowledge/domains/<域>/<表>.md` 的目录结构。

    刻意与 agent 读的是同一套目录：UI 上的分组和 agent 的路由结构同源，
    演示时「左边看到的域」就是「它按域去翻文档」的那个域，不会各说一套。
    """
    out: dict[str, str] = {}
    base = ROOT / "knowledge" / "domains"
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in d.glob("*.md"):
            if not f.stem.startswith("_"):
                out[f.stem] = d.name
    return out


def _layer_map() -> dict[str, dict]:
    """表 → {layer, status, summary}。派生层读 schema_manifest.yaml，其余按名字推断。"""
    out: dict[str, dict] = {}
    mf = ROOT / "schema_manifest.yaml"
    if mf.is_file():
        try:
            import yaml
            spec = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            for t in spec.get("tables", []) or []:
                out[t["name"]] = {
                    "layer": t.get("layer", "base"),
                    "status": t.get("status", "active"),
                    "summary": t.get("summary", ""),
                }
        except Exception:
            pass
    return out


def _infer_layer(name: str, declared: dict) -> dict:
    if name in declared:
        return declared[name]
    if name.startswith("mart_"):
        return {"layer": "mart", "status": "active", "summary": "口径已冻结的预聚合表"}
    if name == "meta_snapshot":
        return {"layer": "meta", "status": "active",
                "summary": "数据集「今天」的锚点（静态样本铁律）"}
    return {"layer": "base", "status": "active", "summary": ""}


# ------------------------------------------------------------------ 装配

def build(refresh: bool = False) -> dict:
    global _cache, _cache_at
    if _cache is not None and not refresh and (time.time() - _cache_at) < CACHE_TTL:
        return _cache

    t_start = time.perf_counter()
    source, warn = "glue", None
    try:
        tables = _glue_tables()
    except Exception as e:
        # 降级但不静默：source 会回传给前端并显示出来。
        source, warn = "information_schema", f"{type(e).__name__}: {e}"
        tables = _schema_tables()
    meta_ms = round((time.perf_counter() - t_start) * 1000, 1)

    t0 = time.perf_counter()
    counts = _row_counts(list(tables))
    row_counts_ms = round((time.perf_counter() - t0) * 1000, 1)

    # 表清单来自 Glue（云上），行数来自 Postgres（本地容器）——**两个不同的库**。
    # 这个组合会跑出来:设了 GLUE_CATALOG_ID 又设了 DB_BACKEND=postgres 就是。
    # 而它的表现极其温和:接口 200、48 张表齐全、行数也都是像样的数字,只是那些数字
    # 描述的是另一个库。这正是这个文件存在的理由所要防的那种失真,所以必须说出来。
    if source == "glue" and db.BACKEND == "postgres":
        mix = ("表清单来自 Glue，行数来自本地 Postgres —— 两个不同的库，"
               "行数不描述 Glue 里那些表。设 DB_BACKEND=athena，"
               "或清掉 GLUE_CATALOG_ID 走纯本地。")
        warn = f"{warn}；{mix}" if warn else mix

    gov = _governance(tables)
    dom = _domain_map()
    declared = _layer_map()
    granted = set(gov.get("granted") or [])
    # LF 的 TableWildcard 是一条覆盖整库的授权，展开成每张表都授权。
    if "*" in granted:
        granted = set(tables)
    masked_by_table: dict[str, list[str]] = {}
    for m in gov.get("masked") or []:
        masked_by_table.setdefault(m["table"], []).extend(m["columns"])

    grouped: dict[str, list[dict]] = {}
    for name in sorted(tables):
        meta = _infer_layer(name, declared)
        # 没有域卡片的表默认落 "other"——那是"有表没文档"的信号，应该看得见。
        # 唯一的例外是 meta_snapshot：它是全局日期锚点，不属于任何业务域，
        # 成文在 knowledge/connection.md（与 reconcile.py 的 CARD_EXEMPT 同一处约定）。
        key = dom.get(name) or ("meta" if meta["layer"] == "meta" else "other")
        grouped.setdefault(key, []).append({
            "name": name,
            "columns": tables[name],
            "column_count": len(tables[name]),
            "rows": counts.get(name),
            "layer": meta["layer"],
            "status": meta["status"],
            "summary": meta["summary"],
            # granted 只在能查到治理信息时才有意义；查不到时给 None 让 UI 别显示，
            # 而不是显示成"未授权"（那是把"不知道"渲染成了"否"，比不显示更糟）。
            "granted": (name in granted) if gov.get("available") else None,
            "masked_columns": sorted(set(masked_by_table.get(name, []))),
        })

    order = DOMAIN_ORDER + [k for k in sorted(grouped) if k not in DOMAIN_ORDER]
    domains = [{"key": k, "tables": grouped[k], "table_count": len(grouped[k])}
               for k in order if k in grouped]

    # 行数分两个：headline 只算 base 层，因为**派生层是基表的副本或汇总**，
    # 加在一起会把同一批事实重复计。`dwd_events_app` 就是 `events` 的过滤版，
    # 两者行数几乎相同；48 张表硬加得 220,087 行，而明细事实只有 189,672 行。
    # 报大数字很诱人，但那个数字经不起「这些行是同一批事实吗」这一问。
    #
    # 那两个数是**全量数据集**的规模（真源 data/csv/，由 verify_load.py 钉住）。
    # 这里实际算出来的会比它小：跑在治理角色下时 `user_messages` 数不到（见下面
    # rows_uncounted_* 那段），于是 180,419 / 210,834。别把这个差额当成数据丢了。
    base_names = {t["name"] for tabs in grouped.values() for t in tabs
                  if t["layer"] == "base"}
    rows_base = sum(v for k, v in counts.items() if k in base_names and v)
    rows_all = sum(v for v in counts.values() if v)

    # 数不出来的表**不许静默消失**。`_row_counts()` 失败即丢键（见那个函数结尾的
    # `if n is not None`），于是上面两个 sum 会把它们一并吞掉：治理不授权的
    # `user_messages`（9,253 行）不进 `rows`，界面显示「约 18 万行」，而仓库里 7 处
    # 文档写的是 19 万。少 5%、看起来完全正常、没有任何痕迹——正是本项目反复踩的那类。
    #
    # 分两个键报，因为成因不同、该做的事也不同：
    #   - 不在授权面里 → 治理层按设计挡的，是**正常现状**，但必须在数字旁边说出来；
    #   - 授权了却仍数不出来 → 真故障（Athena 报错被 `one()` 吞了），不该跟前者混着看。
    # 注意别用 `row_counts_exact` 表达这件事：那个键说的是**精度**（Iceberg 精确
    # ⟷ pg_class 估算），而这里缺的是**覆盖面**。两者混用会把人引到错的方向去查。
    uncounted = sorted(n for n in tables if counts.get(n) is None)
    ungranted = (set(tables) - granted) if gov.get("available") else set()
    rows_unc_gov = [n for n in uncounted if n in ungranted]
    rows_unc_err = [n for n in uncounted if n not in ungranted]
    info = db.backend_info()
    out = {
        "source": source,                    # glue | information_schema
        "warning": warn,                     # 降级原因，前端会显示
        "catalog_id": GLUE_CATALOG_ID or None,
        "engine": info.get("engine"),
        "database": info.get("name"),
        "transport": info.get("transport"),
        "region": info.get("region"),
        "totals": {
            "tables": len(tables),
            "rows": rows_base,           # 明细事实规模（UI 顶栏/欢迎页用这个）
            "rows_all_layers": rows_all,  # 含派生层，会重复计同一批事实，仅供参考
            # 上面两个数**没算进**哪些表。空列表 = 全都数出来了。见上方注释：
            # 前端必须把它渲染出来，否则 rows 就是个"看起来正常但少一截"的数。
            "rows_uncounted_governed": rows_unc_gov,  # 治理不授权，按设计数不到
            "rows_uncounted_failed": rows_unc_err,    # 授权了却数不出来 = 真故障
            "domains": len(domains),
            "columns": sum(len(v) for v in tables.values()),
            # 分层计数：让 UI 能说清"48 张表里有几张是派生层/噪音表"
            "by_layer": _count_by(grouped, "layer"),
        },
        "governance": {
            "role": gov.get("role"),
            "available": gov.get("available"),
            # 治理层未建时说明原因，别让 UI 只看到一个空面板。见 _governance()。
            "reason": gov.get("reason"),
            "granted_tables": len(granted),
            "ungranted_tables": sorted(set(tables) - granted) if gov.get("available") else [],
            "masked": gov.get("masked") or [],
            "error": gov.get("error"),
        },
        # 这两笔开销是可见的：行数那一段是 48 条 Athena 查询，缓存未命中时约 8 秒。
        # 藏起来的话它会表现成"偶尔开页很慢"，那种问题最难查。
        "timings": {"metadata_ms": meta_ms, "row_counts_ms": row_counts_ms},
        # **只说精度，不说覆盖面**：Iceberg 的 count(*) 精确，pg_class.reltuples 是估算。
        # 有表数不出来时它仍然是 true——那不是矛盾，`rows` 是它数到的那些表的精确和。
        # 覆盖面看 totals.rows_uncounted_*。别把这个键改成"有表没数到就 false"：
        # 那会把「不准」和「不全」混成一个信号，而且目前前端并不消费它，改了不显示。
        "row_counts_exact": db.BACKEND != "postgres",
        "domains": domains,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache, _cache_at = out, time.time()
    return out


def _count_by(grouped: dict[str, list[dict]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for tabs in grouped.values():
        for t in tabs:
            out[t[field]] = out.get(t[field], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    import json
    d = build()
    print(json.dumps({k: v for k, v in d.items() if k != "domains"},
                     ensure_ascii=False, indent=2))
    for dm in d["domains"]:
        names = ", ".join(f"{t['name']}({t['layer']})" for t in dm["tables"])
        print(f"\n{dm['key']:<12} {dm['table_count']:>2} 张  {names}")
