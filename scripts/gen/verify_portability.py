#!/usr/bin/env python3
"""同一份数据在三种架构上的可移植性判据（架构对比测试的前置条件）。

## 为什么要单独一层

架构对比测试要跑三个 arm：Redshift（`COPY ... FORMAT AS PARQUET`）、
Athena + S3 Tables（CSV → Iceberg）、DuckDB + S3 Tables（读同一批 Iceberg 表）。
三个 arm 的**数据必须是同一份**，否则量出来的差异分不清是引擎的还是数据的。

而"同一份"这件事没有任何现成的一层在看：

- `verify_load.py` 比的是 CSV ⟷ Athena，只覆盖一个 arm 的一条通路。
- `verify_ddl_vs_redshift.py` 比的是 DDL ⟷ Redshift 的 `information_schema`，
  **需要活着的集群**，而且它只看表结构，不看数据装不装得进去。
- 生成器有两个写出器（`write_csv` / `write_parquet`），**编码约定不同**：
  数组列在 CSV 里是 Postgres 字面量 `{a,b}`，在 Parquet 里是 JSON 字符串。
  两个写出器各自正确，也可能表达出不同的值。

2026-09-01 实测到的就是这种情形：`events.referrer` / `page_views.referrer` 的
「直接打开」在生成器里是空串 `""`，pgcsv 按 Postgres COPY 约定把它写成不带引号的
空字段（COPY 读成 NULL），而 Parquet 保留 `''`。于是 Athena arm 上
`WHERE referrer IS NULL` 命中 28%，Redshift arm 上命中 0 行，**两边都不报错**。
处置是把池子里的空串改成 `None`（见 `tables.REFERRERS` 上面那段），
判据是本文件的 `csv_pq_equal`。

## 八条判据

| # | 判据 | 形态 | 拦什么 |
|---|---|---|---|
| 1 | `rs_type_compat` | 静态 | Parquet 物理类型与 Redshift 列类型不兼容 → `COPY` 报 `incompatible Parquet schema` |
| 2 | `iceberg_type_known` | 静态 | Iceberg 侧出现 DuckDB iceberg reader 读不了的类型 |
| 3 | `varchar_bytes` | 读数据 | 取值字节数超过 Redshift 的 `VARCHAR(n)`（**n 按字节算**）→ `COPY` 报 value too long |
| 4 | `int_width` | 读数据 | 取值超出目标位宽（int16 / int32）→ Spectrum 报类型不兼容 |
| 5 | `decimal_scale` | 读数据 | 小数位数超过声明标度 → 静默舍入，两个 arm 的金额对不上 |
| 6 | `null_vs_empty` | 读数据 | 同一字符串列里空串与 NULL 并存 → CSV 分不开这两者，两个 arm 分叉 |
| 7 | `super_json_valid` | 读数据 | SUPER 列（JSONB / 数组）在 Parquet 侧不是合法 JSON → `SERIALIZETOJSON` 失败 |
| 8 | `csv_pq_equal` | 读两份 | 同一 seed 的两种格式产出逐列逻辑不等 → 三个 arm 吃的不是同一份数据 |

判据 1、2 不读数据，任何时候都能跑。3–7 要一份产出目录。8 要两份（同 seed、同 scale）。

**判据 6 在 CSV 一侧接近空转，别把那个 PASS 当证据。** pgcsv 走 `QUOTE_MINIMAL`，
空串写出来就是不带引号的空字段，读回来一律是 NULL——所以 CSV 侧**永远**量不到空串，
`null_vs_empty` 恒绿。它真正能发现问题的地方是 `--from <parquet 目录> --format parquet`，
以及判据 8（空串与 NULL 在那里判不等，有 selftest 钉着）。2026-09-01 的 `referrer`
缺陷就是这个形态：CSV 侧看什么都干净，分叉发生在 Parquet 侧。

## 用法

    python3 scripts/gen/verify_portability.py --static
    python3 scripts/gen/verify_portability.py --from ~/analytics-agent-data/genFULL_csv
    python3 scripts/gen/verify_portability.py --from /tmp/cmp_pq --format parquet
    python3 scripts/gen/verify_portability.py --compare /tmp/cmp_csv /tmp/cmp_pq
    python3 scripts/gen/verify_portability.py --selftest

判据 8 在全量规模上跑一遍要读两份 7G，所以**它的定位是重新生成之后跑一次小规模**
（`--scale 2` 就覆盖了全部 35 张表的全部列），不是每次装载都跑。
全量那一侧靠 3–7，它们是单份扫描，与规模线性。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "lakehouse"))

import arrow_types  # noqa: E402
import ddl  # noqa: E402
import gen_ddl  # noqa: E402
import redshift_ddl  # noqa: E402

# ---------------------------------------------------------------- 判据 1 的兼容表

# Redshift `COPY ... FORMAT AS PARQUET` 走 Spectrum 读文件，Parquet 的物理类型必须与
# 目标列类型兼容。这张表是**白名单**：不在表里的组合一律判红，而不是"看着差不多就放过"。
# 踩过的两个 case 都写在 arrow_types.py 的 docstring 里（int64 灌不进 INTEGER、
# double 灌不进 DECIMAL(12,2)）。
RS_PARQUET_OK: dict[str, tuple[str, ...]] = {
    "SMALLINT":         ("int16",),
    "INTEGER":          ("int32",),
    "BIGINT":           ("int64",),
    "BOOLEAN":          ("bool",),
    "TIMESTAMP":        ("timestamp[us]",),
    "DATE":             ("date32[day]",),
    "REAL":             ("float",),
    "DOUBLE PRECISION": ("double",),
    "SUPER":            ("string",),          # JSON 文本 + COPY 的 SERIALIZETOJSON
}

# DuckDB 的 iceberg 扩展能读的 Iceberg 类型。数组另外按元素类型递归判。
DUCKDB_ICEBERG_OK = ("boolean", "int", "long", "bigint", "float", "double",
                     "date", "time", "timestamp", "timestamptz", "string",
                     "uuid", "binary")


def _arrow_name(raw: str) -> str:
    return str(arrow_types.arrow_type(raw))


def _rs_base(rs: str) -> tuple[str, int | None]:
    """Redshift 类型串 → (基础类型, VARCHAR 字节宽度或 None)。"""
    t = rs.strip().upper()
    m = re.match(r"^VARCHAR\((\d+)\)$", t)
    if m:
        return "VARCHAR", int(m.group(1))
    m = re.match(r"^(DECIMAL|NUMERIC)\((\d+),\s*(\d+)\)$", t)
    if m:
        return "DECIMAL", None
    return t, None


def judge_rs_type_compat(raw_tables: dict) -> list[str]:
    """判据 1：逐列比 Parquet 物理类型与 Redshift 列类型。"""
    bad = []
    for table in sorted(raw_tables):
        for col, raw, _ in raw_tables[table]:
            rs = redshift_ddl.map_type(raw)
            base, width = _rs_base(rs)
            at = _arrow_name(raw)
            if base == "VARCHAR":
                if at != "string":
                    bad.append(f"{table}.{col}：Redshift {rs} 期望 Parquet string，"
                               f"实际 {at}（源 DDL {raw}）")
                continue
            if base == "DECIMAL":
                m = re.match(r"^decimal128\((\d+), (\d+)\)$", at)
                want = re.match(r"^(DECIMAL|NUMERIC)\((\d+),\s*(\d+)\)$", rs.upper())
                if not m or not want or (m.group(1), m.group(2)) != (want.group(2),
                                                                    want.group(3)):
                    bad.append(f"{table}.{col}：Redshift {rs} 与 Parquet {at} "
                               f"精度/标度不一致（源 DDL {raw}）")
                continue
            ok = RS_PARQUET_OK.get(base)
            if ok is None:
                bad.append(f"{table}.{col}：Redshift 类型 {rs} 不在兼容白名单里"
                           f"（源 DDL {raw}）——补白名单前先确认 COPY 真的接受它")
            elif at not in ok:
                bad.append(f"{table}.{col}：Redshift {rs} 只接受 Parquet {ok}，"
                           f"实际 {at}（源 DDL {raw}）")
    return bad


def judge_iceberg_type_known(raw_tables: dict) -> list[str]:
    """判据 2：Iceberg 侧的类型都在 DuckDB iceberg reader 支持的集合里。"""
    bad = []

    def check(t: str, where: str) -> None:
        m = re.match(r"^array<(.+)>$", t)
        if m:
            check(m.group(1), where)
            return
        if re.match(r"^decimal\(\d+,\d+\)$", t):
            return
        if t not in DUCKDB_ICEBERG_OK:
            bad.append(f"{where}：Iceberg 类型 {t} 不在 DuckDB iceberg reader "
                       f"已知支持的集合里")

    for table in sorted(raw_tables):
        for col, raw, _ in raw_tables[table]:
            check(gen_ddl.map_type(raw), f"{table}.{col}")
    return bad


# ---------------------------------------------------------------- 判据 3–7

INT_RANGE = {"int16": (-(2**15), 2**15 - 1),
             "int32": (-(2**31), 2**31 - 1),
             "int64": (-(2**63), 2**63 - 1)}


def _int_kind(raw: str) -> str | None:
    up = raw.strip().upper()
    if "[]" in up.split()[0]:
        return None
    if up.startswith("SMALLINT"):
        return "int16"
    if up.startswith(("BIGSERIAL", "BIGINT")):
        return "int64"
    if up.startswith(("SERIAL", "INTEGER", "INT")):
        return "int32"
    return None


def _dec_scale(raw: str) -> int | None:
    m = re.match(r"(?i)^(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", raw.strip())
    return int(m.group(3)) if m else None


def _open_table(path: Path, fmt: str, names: list[str]):
    """→ 逐批 pyarrow.RecordBatch。CSV 全列按 string 读，Parquet 按原类型读。"""
    import pyarrow as pa

    if fmt == "csv":
        import pyarrow.csv as pacsv
        copts = pacsv.ConvertOptions(
            column_types={n: pa.string() for n in names},
            strings_can_be_null=True, quoted_strings_can_be_null=False)
        with pacsv.open_csv(path, read_options=pacsv.ReadOptions(block_size=64 << 20),
                            convert_options=copts) as rdr:
            for batch in rdr:
                yield batch
    else:
        import pyarrow.parquet as pq
        for shard in _pq_shards(path):
            f = pq.ParquetFile(shard)
            for batch in f.iter_batches(batch_size=200_000):
                yield batch


def _pq_shards(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob("*.parquet"))
    return [path] if path.exists() else []


def scan_dir(src: Path, fmt: str, raw_tables: dict) -> dict[str, list[str]]:
    """判据 3–7：单份产出目录的逐列扫描。→ {判据名: [违规描述]}"""
    import pyarrow as pa
    import pyarrow.compute as pc

    out: dict[str, list[str]] = {k: [] for k in
                                 ("varchar_bytes", "int_width", "decimal_scale",
                                  "null_vs_empty", "super_json_valid", "missing")}
    for table in sorted(raw_tables):
        path = (src / f"{table}.csv") if fmt == "csv" else _table_pq_path(src, table)
        if path is None or not path.exists():
            out["missing"].append(f"{table}：{fmt} 侧不存在")
            continue
        cols = raw_tables[table]
        names = [c for c, _, _ in cols]
        acc = {c: {"maxb": 0, "nempty": 0, "nnull": 0,
                   "imin": None, "imax": None, "scale": 0,
                   "badjson": 0, "jsample": None} for c in names}
        header = None
        for batch in _open_table(path, fmt, names):
            if header is None:
                header = list(batch.schema.names)
            for col, raw, ctype in cols:
                if col not in header:
                    continue
                a = batch.column(header.index(col))
                s = acc[col]
                s["nnull"] += a.null_count
                nn = a.drop_null()
                if len(nn) == 0:
                    continue
                if pa.types.is_string(nn.type):
                    s["maxb"] = max(s["maxb"],
                                    pc.max(pc.binary_length(nn.cast(pa.binary()))
                                           ).as_py() or 0)
                    s["nempty"] += pc.sum(pc.equal(pc.utf8_length(nn), 0)).as_py() or 0
                if _int_kind(raw) and ctype == "INT":
                    v = nn if pa.types.is_integer(nn.type) else pc.cast(
                        nn, pa.int64(), safe=False)
                    lo, hi = pc.min(v).as_py(), pc.max(v).as_py()
                    if lo is not None:
                        s["imin"] = lo if s["imin"] is None else min(s["imin"], lo)
                        s["imax"] = hi if s["imax"] is None else max(s["imax"], hi)
                if _dec_scale(raw) is not None and pa.types.is_string(nn.type):
                    idx = pc.find_substring(nn, ".")
                    has = pc.greater_equal(idx, 0)
                    if pc.any(has).as_py():
                        sub = nn.filter(has)
                        after = pc.subtract(pc.utf8_length(sub),
                                            pc.add(pc.find_substring(sub, "."), 1))
                        s["scale"] = max(s["scale"], pc.max(after).as_py() or 0)
                if ctype in ("JSON", "ARRAY") and fmt == "parquet" \
                        and pa.types.is_string(nn.type):
                    for v in nn.slice(0, min(len(nn), 200)).to_pylist():
                        try:
                            json.loads(v)
                        except Exception:                       # noqa: BLE001
                            s["badjson"] += 1
                            s["jsample"] = s["jsample"] or v[:60]
        if header is None:
            continue
        for col, raw, ctype in cols:
            s = acc[col]
            rs = redshift_ddl.map_type(raw)
            base, width = _rs_base(rs)
            if width is not None and s["maxb"] > width:
                out["varchar_bytes"].append(
                    f"{table}.{col}：实测最长 {s['maxb']} 字节 > Redshift {rs}"
                    f"（源 DDL {raw}；Redshift 的长度按字节算）")
            k = _int_kind(raw)
            if k and ctype == "INT" and s["imax"] is not None:
                lo, hi = INT_RANGE[k]
                if s["imin"] < lo or s["imax"] > hi:
                    out["int_width"].append(
                        f"{table}.{col}：实测 [{s['imin']}, {s['imax']}] 超出 "
                        f"{raw} → {k} 的 [{lo}, {hi}]")
            ds = _dec_scale(raw)
            if ds is not None and s["scale"] > ds:
                out["decimal_scale"].append(
                    f"{table}.{col}：实测小数位 {s['scale']} 位 > {raw} 的标度 {ds}")
            if s["nempty"] and s["nnull"]:
                out["null_vs_empty"].append(
                    f"{table}.{col}：空串 {s['nempty']} 个与 NULL {s['nnull']} 个并存，"
                    f"CSV 分不开这两者（pgcsv 走 Postgres COPY 约定，两者都写成空字段）")
            if s["badjson"]:
                out["super_json_valid"].append(
                    f"{table}.{col}：Parquet 侧 {s['badjson']} 个取值不是合法 JSON，"
                    f"样例 {s['jsample']!r}——SUPER 列的 COPY 会失败")
    return out


def _table_pq_path(src: Path, table: str) -> Path | None:
    d = src / table
    if d.is_dir():
        return d
    f = src / f"{table}.parquet"
    return f if f.exists() else None


# ---------------------------------------------------------------- 判据 8

def _pg_array(text: str):
    """Postgres 数组字面量 → list[str]。`{}` → []。"""
    t = text.strip()
    if not t.startswith("{"):
        return text
    inner = t[1:-1]
    if inner == "":
        return []
    out, cur, q, i = [], [], False, 0
    while i < len(inner):
        ch = inner[i]
        if ch == '"':
            q = not q
        elif ch == "\\" and q:
            i += 1
            cur.append(inner[i])
        elif ch == "," and not q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _norm(v, ctype: str, raw: str):
    """两种格式各自的编码 → 同一个 Python 值。比的是逻辑值，不是字节。"""
    if v is None:
        return None
    if ctype == "ARRAY":
        if isinstance(v, str):
            lst = _pg_array(v) if v.startswith("{") else json.loads(v)
        else:
            lst = list(v)
        return [int(x) for x in lst] if "INT" in raw.upper() else [str(x) for x in lst]
    if ctype == "JSON":
        if isinstance(v, str):
            s = v.strip()
            return json.loads(s) if s.startswith(("{", "[")) else s
        return v
    if ctype == "INT":
        return int(v)
    if ctype == "DECIMAL":
        from decimal import Decimal
        return Decimal(str(v)).normalize()
    if ctype == "BOOL":
        return str(v).lower() in ("t", "true", "1")
    if ctype in ("TIMESTAMP", "DATE"):
        # **按时刻比，不按字面比。** CSV 侧是文本、Parquet 侧是 datetime，
        # 而同一个时刻的两种文本形态不唯一：`.26222` 与 `.262220` 是同一微秒，
        # `str()` 出来却不等。这三列（ad_creatives.created_at / campaigns.updated_at /
        # event_definitions.updated_at）的尾零本来就在 `data/csv` 里，
        # 2026-09-01 第一版判据把它们报成了三条假红。
        import datetime
        if isinstance(v, (datetime.datetime, datetime.date)):
            return (v if isinstance(v, datetime.datetime)
                    else datetime.datetime(v.year, v.month, v.day))
        s = str(v).strip().replace("T", " ")
        s = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", s)
        if "." in s:
            head, frac = s.split(".", 1)
            s = f"{head}.{frac[:6].ljust(6, '0')}"          # 尾零补齐到微秒
        return datetime.datetime.fromisoformat(s)
    return str(v)


def judge_csv_pq_equal(csv_dir: Path, pq_dir: Path, raw_tables: dict,
                       dim_pq_dir: Path | None = None) -> list[str]:
    """判据 8：同 seed 的两份产出逐列逻辑相等。

    35 张表分两拨，两拨的 CSV 源头不是一个地方，别混：

    - **24 张生成表**由 `main.py` 两次写出（`--format csv` / `--format parquet`），
      CSV 在 `<csv_dir>/<表>.csv`。
    - **11 张维表不重新生成**（见 `dims_to_parquet.DIMS` 上面那段：量级两万行，
      而且 eval 金标钉着现有取值）。它们的 CSV 是**仓库里的** `data/csv/<表>.csv`，
      Redshift 侧的 Parquet 由 `dims_to_parquet.py` 单独转。生成目录里根本没有这 11 张，
      所以照着 `<csv_dir>` 找必然 11 条「两侧不成对」——那是判据找错了地方，不是数据缺了。

    `dim_pq_dir` 给 `dims_to_parquet.py` 的产出目录。不给就跳过这 11 张并说明跳了。
    """
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    import dims_to_parquet

    bad = []
    for table in sorted(raw_tables):
        if table in dims_to_parquet.DIMS:
            if dim_pq_dir is None:
                continue
            cpath = ROOT / "data" / "csv" / f"{table}.csv"
            ppath = _table_pq_path(dim_pq_dir, table)
        else:
            cpath = csv_dir / f"{table}.csv"
            ppath = _table_pq_path(pq_dir, table)
        if not cpath.exists() or ppath is None:
            bad.append(f"{table}：两侧不成对（CSV {cpath.exists()} / "
                       f"Parquet {ppath is not None}）")
            continue
        cols = raw_tables[table]
        names = [c for c, _, _ in cols]
        ct = pacsv.read_csv(cpath, convert_options=pacsv.ConvertOptions(
            column_types={n: pa.string() for n in names},
            strings_can_be_null=True, quoted_strings_can_be_null=False))
        pt = pa.concat_tables([pq.read_table(s) for s in _pq_shards(ppath)])
        if ct.num_rows != pt.num_rows:
            bad.append(f"{table}：行数不等，CSV {ct.num_rows:,} / "
                       f"Parquet {pt.num_rows:,}")
            continue
        if list(pt.schema.names) != names:
            bad.append(f"{table}：Parquet 列序与 DDL 不同")
        for col, raw, ctype in cols:
            if col not in ct.schema.names or col not in pt.schema.names:
                bad.append(f"{table}.{col}：有一侧缺列")
                continue
            cv = ct.column(col).to_pylist()
            pv = pt.column(col).to_pylist()
            diff, first = 0, None
            for i, (a, b) in enumerate(zip(cv, pv)):
                try:
                    na, nb = _norm(a, ctype, raw), _norm(b, ctype, raw)
                except Exception as e:                          # noqa: BLE001
                    diff += 1
                    first = first or f"规约失败 {e!r} csv={a!r} pq={b!r}"
                    continue
                if na != nb:
                    diff += 1
                    if first is None:
                        first = f"行 {i} csv={a!r} → {na!r} | pq={b!r} → {nb!r}"
            if diff:
                bad.append(f"{table}.{col} ({raw})：{diff:,}/{ct.num_rows:,} 行不等；"
                           f"{first}")
    return bad


# ---------------------------------------------------------------- 报告

def _report(groups: dict[str, list[str]], title: str) -> int:
    print(f"\n==== {title} ====")
    nbad = 0
    for name, bad in groups.items():
        if bad:
            nbad += 1
            print(f"  FAIL  {name}（{len(bad)} 条）")
            for line in bad[:15]:
                print(f"          {line}")
            if len(bad) > 15:
                print(f"          …另 {len(bad) - 15} 条")
        else:
            print(f"  PASS  {name}")
    return nbad


def selftest() -> int:
    """红绿两侧。夹具是**构造出来的 DDL 片段**，不读真数据。"""
    n = fails = 0

    def ok(cond: bool, msg: str) -> None:
        nonlocal n, fails
        n += 1
        if cond:
            print(f"  ok  {msg}")
        else:
            fails += 1
            print(f"  RED {msg}")

    green = {"t": [("a", "BIGINT", "INT"), ("b", "VARCHAR(50)", "TEXT"),
                   ("c", "DECIMAL(12,2)", "DECIMAL"), ("d", "TIMESTAMP", "TS"),
                   ("e", "TEXT[]", "ARRAY"), ("f", "JSONB", "JSON"),
                   ("g", "BOOLEAN", "BOOL"), ("h", "DATE", "DATE"),
                   ("i", "SMALLINT", "INT"), ("j", "INTEGER", "INT")]}
    ok(judge_rs_type_compat(green) == [], "判据 1：常规类型的 Parquet ⟷ Redshift 兼容")
    ok(judge_iceberg_type_known(green) == [], "判据 2：常规类型都在 DuckDB 支持集合里")

    # 红：把 arrow 侧换成错的宽度。直接改 RS_PARQUET_OK 的白名单来模拟不兼容，
    # 比伪造一个 arrow_type 更贴近真实失败——白名单收窄就等于"COPY 不接受这个组合"。
    saved = RS_PARQUET_OK["BIGINT"]
    RS_PARQUET_OK["BIGINT"] = ("int32",)
    bad = judge_rs_type_compat(green)
    RS_PARQUET_OK["BIGINT"] = saved
    ok(len(bad) == 1 and "t.a" in bad[0], f"判据 1 反例：不兼容组合被点名（{bad[:1]}）")

    ok(judge_iceberg_type_known({"t": [("x", "MONEY", "?")]}) != []
       if _iceberg_maps("MONEY") else True,
       "判据 2 反例：未知类型被拦（MONEY 在 gen_ddl 就会抛，等价于拦住）")

    # 判据 3–7 的红绿：造两张极小的表落地再扫。
    import tempfile

    import pyarrow as pa
    import pyarrow.parquet as pqw

    raw = {"t": [("a", "SMALLINT", "INT"), ("b", "VARCHAR(2)", "TEXT"),
                 ("c", "JSONB", "JSON")]}
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        pqw.write_table(pa.table({
            "a": pa.array([1, 2], pa.int16()),
            "b": pa.array(["一二三", ""]),                 # 9 字节 > VARCHAR(2)*4=8
            "c": pa.array(['{"k":1}', "not json"]),
        }), d / "t.parquet")
        got = scan_dir(d, "parquet", raw)
        ok(len(got["varchar_bytes"]) == 1, f"判据 3 反例：字节超宽被点名（{got['varchar_bytes']}）")
        ok(got["int_width"] == [], "判据 4：int16 范围内不判红")
        ok(len(got["super_json_valid"]) == 1,
           f"判据 7 反例：非法 JSON 被点名（{got['super_json_valid']}）")
        ok(got["null_vs_empty"] == [],
           "判据 6：只有空串没有 NULL 时不判红（分不开的前提是两者并存）")

        pqw.write_table(pa.table({
            "a": pa.array([1, 2], pa.int16()),
            "b": pa.array(["x", None]),
            "c": pa.array(['{"k":1}', '{"k":2}']),
        }), d / "t.parquet")
        got = scan_dir(d, "parquet", raw)
        ok(all(not v for v in got.values()), f"判据 3–7 正例：干净数据全绿（{got}）")

        pqw.write_table(pa.table({
            "a": pa.array([1, 2], pa.int16()),
            "b": pa.array(["", None]),
            "c": pa.array(['{"k":1}', '{"k":2}']),
        }), d / "t.parquet")
        got = scan_dir(d, "parquet", raw)
        ok(len(got["null_vs_empty"]) == 1,
           f"判据 6 反例：空串与 NULL 并存被点名（{got['null_vs_empty']}）")

    # 判据 8 的时间戳容差。它**只**该容忍"同一时刻的两种文本形态"，不该容忍真实偏移；
    # 2026-09-01 修 `dims_to_parquet` 的 `[:19]` 截断靠的就是这条判据，容差放宽一步
    # 就会把那个缺陷重新放过去。
    import datetime as _dt
    ok(_norm("2026-01-25 02:16:15.26222", "TIMESTAMP", "TIMESTAMP")
       == _norm(_dt.datetime(2026, 1, 25, 2, 16, 15, 262220), "TIMESTAMP", "TIMESTAMP"),
       "判据 8：尾零形态不同的同一微秒判等（.26222 == .262220）")
    ok(_norm("2026-01-25 02:16:15.262220", "TIMESTAMP", "TIMESTAMP")
       != _norm(_dt.datetime(2026, 1, 25, 2, 16, 15), "TIMESTAMP", "TIMESTAMP"),
       "判据 8 反例：截到秒的时间戳判不等（这就是 dims_to_parquet 的 [:19] 缺陷）")
    ok(_norm("{数码,美妆}", "ARRAY", "TEXT[]")
       == _norm('["数码","美妆"]', "ARRAY", "TEXT[]"),
       "判据 8：Postgres 数组字面量与 JSON 数组判等")
    ok(_norm("", "TEXT", "VARCHAR(10)") != _norm(None, "TEXT", "VARCHAR(10)"),
       "判据 8：空串与 NULL 判不等（正是 referrer 那个缺陷的形态）")

    print(f"\n---- {n} 条断言"
          + ("，全部通过 ✅" if fails == 0 else f"，{fails} 条红 ❌"))
    return 1 if fails else 0


def _iceberg_maps(raw: str) -> bool:
    try:
        gen_ddl.map_type(raw)
        return True
    except Exception:                                          # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="三种架构的数据可移植性判据")
    ap.add_argument("--static", action="store_true", help="只跑判据 1、2（不读数据）")
    ap.add_argument("--from", dest="src", help="产出目录（判据 3–7）")
    ap.add_argument("--format", choices=["csv", "parquet"], default="csv")
    ap.add_argument("--compare", nargs=2, metavar=("CSV_DIR", "PQ_DIR"),
                    help="两份同 seed 产出（判据 8）")
    ap.add_argument("--dim-pq", help="dims_to_parquet.py 的产出目录；给了才比那 11 张维表")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    raw_tables = ddl.parse_all_raw()
    nbad = 0
    if a.static or not (a.src or a.compare):
        nbad += _report({"rs_type_compat": judge_rs_type_compat(raw_tables),
                         "iceberg_type_known": judge_iceberg_type_known(raw_tables)},
                        f"静态类型兼容（{len(raw_tables)} 张表）")
    if a.src:
        got = scan_dir(Path(a.src), a.format, raw_tables)
        nbad += _report(got, f"产出扫描 {a.src}（{a.format}）")
    if a.compare:
        nbad += _report({"csv_pq_equal": judge_csv_pq_equal(
            Path(a.compare[0]), Path(a.compare[1]), raw_tables,
            Path(a.dim_pq) if a.dim_pq else None)},
            f"跨格式逐列比对 {a.compare[0]} ⟷ {a.compare[1]}"
            + ("" if a.dim_pq else "（未给 --dim-pq，11 张透传维表跳过）"))

    print(f"\n{'全部通过 ✅' if nbad == 0 else f'{nbad} 组判据判红 ❌'}")
    return 1 if nbad else 0


if __name__ == "__main__":
    raise SystemExit(main())
