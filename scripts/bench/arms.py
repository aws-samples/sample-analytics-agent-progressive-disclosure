#!/usr/bin/env python3
"""三条 arm 的统一适配器：名字、客户端、方言探针。

每条 arm 只在**两件事**上不同——怎么连、SQL 怎么写。除此之外的一切（比什么指标、
判据、输出）都必须共用，否则比出来的差异里会混进「两套实现本来就不同」这一类，
而这一类看起来和真差异一模一样。

## 三条 arm

| arm | 存储 | 计算 | 备注 |
|---|---|---|---|
| `athena` | S3 Tables / Iceberg | Athena（Trino） | |
| `duckdb` | **同上，同一批物理文件** | DuckDB | 不需要自己的存储 |
| `redshift` | S3 → Redshift 托管存储 | Redshift Serverless | 多一次物化，`COPY` 读不了 Iceberg |

`athena` 和 `duckdb` 读的是同一批 Parquet 数据文件，所以它们之间的差异**只能**来自
引擎。`redshift` 隔了一次 COPY，它与前两者的差异要先排除物化这一步。

## 方言映射三处，每处都对齐了截断方向

| 指标 | Trino | DuckDB | Redshift |
|---|---|---|---|
| `sum:` | `SUM(CAST(c AS DECIMAL(38,4)))` | 同 | 同 |
| `min:`/`max:` | `format_datetime(…, 'yyyy-MM-dd''T''HH:mm:ss')` | `strftime(…, '%Y-%m-%dT%H:%M:%S')` | `TO_CHAR(…, 'YYYY-MM-DD"T"HH24:MI:SS')` |
| `true:` | `SUM(CASE WHEN c THEN 1 ELSE 0 END)` | 同 | 同 |

时间那一行三边都必须**截断**到秒。三个函数实测都是截断，没有一个进位。要是有一个
进位，`14:11:10.999999` 会在那一边变成 `:11`，而这种假失败只落在个别行上，
排查起来非常像数据问题。

## 基准测试前必须关掉的引擎侧加速

三条 arm 的默认配置并不对等，直接跑会把「引擎快」和「缓存命中」混在一起：

- **Redshift** 有结果缓存（`enable_result_cache_for_session`）和自动物化视图
  （`auto_mv`）。workgroup 建的时候已经把 `auto_mv` 设成 `false`，结果缓存要按会话关。
  另外两条 arm 都没有自动物化视图这种东西，留着它 Redshift 会在**重复查询**上凭空快一截。
- **Athena** 有 query result reuse，按 workgroup 配置，默认关。
- **DuckDB** 有 `enable_object_cache`（缓存 Parquet 元数据，不缓存结果）。

正确性对账不受这些影响（同一条 SQL 缓不缓存结果都一样），所以这里只在
`configure_for_timing()` 里关，不在连接时关——免得正确性这一步白等冷启动。
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LAKE = ROOT / "scripts" / "lakehouse"
_DUCK = ROOT / "scripts" / "duckdb"
_RS = ROOT / "scripts" / "redshift"
for p in (_LAKE, _DUCK, _RS):
    sys.path.insert(0, str(p))

ARMS = ("athena", "duckdb", "redshift")

# Redshift 关会话结果缓存要靠一个保持住的会话（见 rsql 的 docstring）。20 分钟够跑完
# 一轮基准，又不会把连接占一整天。它只在 configure_for_timing() 里用，正确性对账不开。
RS_SESSION_KEEPALIVE = int(__import__("os").environ.get(
    "REDSHIFT_SESSION_KEEPALIVE", "1200"))

# 两个 verify_load 同名（lakehouse 那个和 duckdb 那个），所以一律**按文件路径**加载。
# 写 `import verify_load` 会拿到 sys.path 上先出现的那一个，而且不报 ImportError，
# 只在用到 `athena_probe` 时报 AttributeError —— 报错点离原因很远。
_MODS: dict[str, object] = {}


def _load(alias: str, path: Path):
    if alias not in _MODS:
        import importlib.util
        spec = importlib.util.spec_from_file_location(alias, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[alias] = m
        spec.loader.exec_module(m)
        _MODS[alias] = m
    return _MODS[alias]


def lake_vl():
    """lakehouse 的 verify_load：CSV 侧指标 + Athena 探针 + classify 的唯一定义。"""
    return _load("lakehouse_verify_load", _LAKE / "verify_load.py")


def duck_vl():
    return _load("duckdb_verify_load", _DUCK / "verify_load.py")


# ---------------------------------------------------------------- Redshift 探针

def redshift_probe(table: str, cols: list[tuple[str, str]],
                   want: set[str]) -> tuple[str, list[str]]:
    """Redshift 方言的聚合探针。标签必须与另两条 arm 逐位相同。

    `TO_CHAR` 里字面量 `T` 用双引号包（Redshift 跟 Postgres 一样），而 Trino 那边
    是 `''T''`、DuckDB 那边根本没有字面量转义问题。三种写法，一个结果。
    """
    kinds = {c: lake_vl().classify(t) for c, t in cols}  # 列分类三边共用一套
    sel, labels = ["COUNT(*)"], ["count"]
    for col, kind in kinds.items():
        if kind == "num" and f"sum:{col}" in want:
            sel.append(f'CAST(SUM(CAST("{col}" AS DECIMAL(38,4))) AS VARCHAR)')
            labels.append(f"sum:{col}")
        elif kind in ("ts", "date") and f"min:{col}" in want:
            fmt = "YYYY-MM-DD" if kind == "date" else 'YYYY-MM-DD"T"HH24:MI:SS'
            for agg, tag in (("MIN", "min"), ("MAX", "max")):
                sel.append(f"TO_CHAR({agg}(\"{col}\"), '{fmt}')")
                labels.append(f"{tag}:{col}")
        elif kind == "bool" and f"true:{col}" in want:
            sel.append(f'SUM(CASE WHEN "{col}" THEN 1 ELSE 0 END)')
            labels.append(f"true:{col}")
    return f'SELECT {", ".join(sel)} FROM "{table}"', labels


# ---------------------------------------------------------------- 适配器

class Arm:
    """一条 arm。`client` 懒建：只想跑一条 arm 时不该为另两条付连接成本。"""

    def __init__(self, name: str):
        if name not in ARMS:
            raise ValueError(f"未知的 arm {name!r}，只有 {ARMS}")
        self.name = name
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if self.name == "athena":
                import athena
                self._client = athena.Client()
            elif self.name == "duckdb":
                import conn
                self._client = conn.Client()
            else:
                import rsql
                if not rsql.SECRET_ARN:
                    raise RuntimeError(
                        "redshift arm 需要 REDSHIFT_SECRET_ARN。\n"
                        "  它是 create-namespace --manage-admin-password 生成的那个"
                        " Secrets Manager ARN，形如\n"
                        "  arn:aws:secretsmanager:us-west-2:<账号>:secret:redshift!<namespace>-<user>-xxxxxx\n"
                        "  查：aws redshift-serverless get-namespace"
                        " --namespace-name <ns> --query namespace.adminPasswordSecretArn")
                self._client = rsql.Client()
        return self._client

    def probe(self, table: str, cols: list[tuple[str, str]],
              want: set[str]) -> tuple[str, list[str]]:
        if self.name == "athena":
            return lake_vl().athena_probe(table, cols, want)
        if self.name == "duckdb":
            return duck_vl().duckdb_probe(table, cols, want)
        return redshift_probe(table, cols, want)

    def metrics(self, table: str, cols: list[tuple[str, str]],
                want: set[str]) -> tuple[dict[str, object], float]:
        """→ (指标 dict, 引擎耗时 ms)。三条 arm 的 dict 必须逐键可比。"""
        sql, labels = self.probe(table, cols, want)
        r = self.client.execute(sql)
        row = r["rows"][0]
        out: dict[str, object] = {}
        for label, val in zip(labels, row):
            if label == "count" or label.startswith("true:"):
                out[label] = int(val) if val is not None else 0
            else:
                out[label] = val
        return out, float(r.get("elapsed_ms") or 0)

    #: catalog 面的主机名标记。S3 Tables 的 catalog 是
    #: `s3tables.<区域>.amazonaws.com`，数据面是另一个专用端点
    #: `<token>--table-s3.s3.<区域>.amazonaws.com`。两者的计费口径不同，
    #: 所以分开数——合起来数出来的金额说不清是哪一笔。
    CATALOG_HOST_MARK = "s3tables."

    def count_http(self, sql: str, cold: bool = False) -> dict | None:
        """跑一次这条 SQL，数它发了多少次 HTTP 请求。非 duckdb 返回 None。

        为什么只有 duckdb 这一条 arm 有这一项：Athena 按扫描字节收钱、Redshift 按
        RPU-秒收钱，对象存储的请求费在它们的单价里已经含掉了，而且那些请求是服务端
        发的，客户端根本数不到。DuckDB 直读 S3，请求费是它账上单独的一笔——不数它
        就是少算 DuckDB 的成本，而这个方向的漏算恰好让对比偏向 DuckDB。

        **不能在计时循环里调这个函数**：开日志本身有开销，那一次的墙上时钟不是引擎
        耗时。所以它单独跑一次，只取请求数、把时间丢掉。

        `cold` 决定量的是哪一种，两种都必须量，因为它们差一个量级：

        - `cold=False`（热）：用模块级那条连接，`enable_object_cache` 已经把列块
          留在内存里，所以同一条查询重复跑几乎不发对象请求——实测 `scan-agg` 热态
          只剩 1 次 catalog 请求、0 次对象请求。这个数对应「同一个进程反复跑同一批
          查询」这种用法。
        - `cold=True`（冷）：另建一条临时连接，缓存是空的，量到的是这条查询真正要
          从 S3 拉多少次。这个数对应「每次问的都是新问题」——**agent 就是这种用法**，
          所以单条隔离成本该按它算。拿热态请求数去算单条成本会把 DuckDB 的请求费
          抹成零。

        临时连接只在 `cold` 这一支建，量完就关，不碰 `conn.py` 的模块级单例，
        上面那些计时样本的状态不受影响。
        """
        if self.name != "duckdb":
            return None
        import conn
        run = lambda c: c.execute(sql).fetchall()               # noqa: E731
        if not cold:
            # conn.connect() 返回的是 conn.py 的模块级单例，和 self.client 执行 SQL
            # 用的是同一条连接——日志才数得到那些请求。
            return self._http_around(conn.connect(), run)
        with self._fresh_conn() as c:
            # 日志在 ATTACH **之后**打开：ATTACH 那几次是每进程一次的开销，
            # 混进查询里会把每条查询都算贵。它由 count_http_attach() 单独量。
            return self._http_around(c, run)

    def count_http_attach(self) -> dict | None:
        """连接建立 + `ATTACH` 自己发多少次请求。非 duckdb 返回 None。

        单独量的理由：这一笔是每进程一次，摊到每条查询上会把单条成本抬高一个量级，
        而它真实影响的是**冷启动那一下**。报告里它和查询请求分两行。

        用一条**临时连接**量，不碰 `conn.py` 的模块级单例：日志开关是连接级的，
        想把 `ATTACH` 圈进日志就必须在 `ATTACH` 之前把日志打开，而单例那条连接
        早就 ATTACH 过了。临时连接量完就关，计时状态不受影响。
        """
        if self.name != "duckdb":
            return None
        import duckdb
        import conn
        c = duckdb.connect()
        try:
            conn._configure(c)      # 装 extension、钉线程与内存额度、建凭证
            return self._http_around(c, conn.attach)
        finally:
            c.close()

    @contextlib.contextmanager
    def _fresh_conn(self):
        """一条临时的、已 ATTACH 好的 DuckDB 连接。用完就关。

        走 `conn.py` 自己的 `_configure` 和 `attach`，不在这里重写一遍连接过程：
        重写会引入「基准用的连接和 agent 用的连接设置不同」这种看不见的偏差。
        `USE` 那一句也要照抄——`conn.connect()` 里有它，少了它这条连接上的
        `FROM orders` 会报表不存在，而那个报错看起来像 ATTACH 失败。
        """
        import duckdb
        import conn
        c = duckdb.connect()
        try:
            conn._configure(c)
            conn.attach(c)
            c.execute(f"USE {conn.ALIAS}.{conn.NAMESPACE}")
            yield c
        finally:
            c.close()

    def _http_around(self, c, body) -> dict:
        """在 `body` 外面套一层 HTTP 日志，返回按主机面分类的请求数。

        先 truncate 再开：日志是连接级累积的，不清零就会把上一条查询的请求算进来。
        """
        c.execute("CALL truncate_duckdb_logs()")
        c.execute("CALL enable_logging('HTTP')")
        try:
            body(c)
        finally:
            # 关掉再读，否则读日志这条查询自己也可能进日志。
            c.execute("CALL disable_logging()")
        rows = c.execute(
            "SELECT request.type, "
            f"       request.url LIKE '%{self.CATALOG_HOST_MARK}%' AS is_catalog, "
            "       count(*) "
            "FROM duckdb_logs_parsed('HTTP') GROUP BY 1, 2").fetchall()
        c.execute("CALL truncate_duckdb_logs()")
        out: dict[str, object] = {"object": 0, "catalog": 0, "by_verb": {}}
        for verb, is_catalog, n in rows:
            n = int(n)
            out["catalog" if is_catalog else "object"] += n            # type: ignore[operator]
            out["by_verb"][verb] = out["by_verb"].get(verb, 0) + n     # type: ignore[index,union-attr]
        out["total"] = out["object"] + out["catalog"]                  # type: ignore[operator]
        return out

    def configure_for_timing(self) -> list[str]:
        """关掉只有某一条 arm 才有的加速，然后**回读确认**。返回实际状态，写进 trace。

        正确性对账不调这个（结果缓存不改变结果，白等冷启动没意义）；
        计时前必须调，否则 Redshift 的重复查询会凭空快一截。

        「设完就回读」不是多余的谨慎，是这个函数第一版的真实教训：Redshift 那一支
        原来只发一条 `SET`，而 Data API 每次调用是独立会话，`SET` 发完就没了
        （实测 `SHOW` 回来仍是 `on`）。它于是往 trace 里写「结果缓存已关」而缓存开着——
        比不设更坏，因为报告里有一句假的前提。现在关缓存要靠会话
        （`rsql.Client(keepalive=...)`），并且以回读值为准。
        """
        done = []
        if self.name == "redshift":
            import rsql
            if not self.client.keepalive:
                # 会话是关缓存的**前提**，不是优化。没有会话就别假装设上了。
                self._client = rsql.Client(keepalive=RS_SESSION_KEEPALIVE)
            self.client.execute("SET enable_result_cache_for_session TO off",
                                fetch=False)
            got = self.client.session_setting("enable_result_cache_for_session")
            if got.lower() != "off":
                raise RuntimeError(
                    "关不掉 Redshift 的会话结果缓存：回读到 "
                    f"{got!r}，期望 'off'。会话 id="
                    f"{self.client.session_id or '<没建起来>'}。\n"
                    "  这一条不能降级放过：缓存开着测出来的重复查询耗时不是引擎性能。")
            done.append(f"enable_result_cache_for_session={got}（回读确认，"
                        f"会话 {self.client.session_id[:8]}…）")
            done.append("auto_mv=false（建 workgroup 时已设，workgroup 级不是会话级）")
        elif self.name == "duckdb":
            import conn
            eff = conn.effective_settings()
            if not conn.THREADS:
                raise RuntimeError(
                    "DUCKDB_THREADS 是空串，算力没钉住。DuckDB 会按机器核数定线程，"
                    "笔记本和 Fargate 上就不是同一把尺，而输出里看不出来。")
            done += [f"{k}={eff[k]}（引擎回读）" for k in conn.TIMING_SETTINGS]
        elif self.name == "athena":
            # Athena 的 result reuse 是 workgroup 配置 + 每次调用可覆盖，
            # 不是会话状态。athena.py 从不传 ResultReuseConfiguration，所以是关的。
            done.append("result reuse：客户端从不传 ResultReuseConfiguration")
        return done


def all_arms(names: list[str] | None = None) -> list[Arm]:
    return [Arm(n) for n in (names or ARMS)]


if __name__ == "__main__":
    # 自测：三条 arm 的探针标签必须逐位相同。这一条挂了，后面所有对账都没有意义。
    cols = [("id", "INT"), ("amt", "DECIMAL(12,2)"), ("created_at", "TIMESTAMP"),
            ("d", "DATE"), ("flag", "BOOLEAN"), ("name", "VARCHAR(50)")]
    want = {"sum:id", "sum:amt", "min:created_at", "min:d", "true:flag"}
    ref = None
    bad = 0
    for a in all_arms():
        sql, labels = a.probe("t", cols, want)
        if ref is None:
            ref = labels
        elif labels != ref:
            bad += 1
            print(f"  FAIL {a.name} 标签与 athena 不同：\n    {labels}\n    {ref}")
        print(f"  {a.name:<9} {len(labels)} 项标签 · SQL {len(sql)} 字符")
    for forbidden, wrong_arm in (("format_datetime", "duckdb"), ("format_datetime", "redshift"),
                                 ("strftime", "athena"), ("strftime", "redshift"),
                                 ("TO_CHAR", "athena"), ("TO_CHAR", "duckdb")):
        sql, _ = Arm(wrong_arm).probe("t", cols, want)
        if forbidden in sql:
            bad += 1
            print(f"  FAIL {wrong_arm} 的探针里出现了别家的写法 {forbidden!r}")
    if bad:
        print(f"\n{bad} 项失败 ❌")
        raise SystemExit(1)
    print(f"\n三条 arm 探针标签逐位相同（{len(ref)} 项），方言互不串味 ✅")
