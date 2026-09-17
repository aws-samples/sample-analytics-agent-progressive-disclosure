#!/usr/bin/env python3
r"""把 data/csv 的 35 张表灌进 S3 Tables（Iceberg）。

## ⚠️ 跑之前先确认湖里现在装的是哪一批

本脚本的"幂等"是**逐表 `DELETE FROM` 再 `INSERT`**——幂等的前提是两次灌的是**同一份源**。
在一个已经装了别的批次的湖上跑它，就是覆盖，而且是静默的：命令正常退出，随后
`verify_load.py` 还会全绿（它比的就是 `data/csv/` ⟷ Athena，两边一致正是覆盖成功的结果）。

`data/csv/` 是 `scripts/gen/main.py --scale 1` 的小样（`orders` 2,000 行）。本仓库开发账号
2026-09-17 实测湖里是 **8000 万行**那一批（`orders` 854,140，由 `feat/data-reload-80m` 的
`load_parquet.py` 从 parquet 灌的，那个脚本不在本分支上）。在那样的湖上跑本脚本，等于拿
2,000 单订单覆盖掉 854,140 单。判据一条命令：

    SELECT count(*) FROM orders     -- 2,000 → 种子，可以跑；854,140 → 别跑

背景与逐表对照见 `docs/deployment.md` 的「数据说明」。

## 路径：CSV → S3 → Glue 外部表 → INSERT INTO Iceberg

    data/csv/users.csv
        │  ① boto3 upload
        ▼
    s3://analytics-agent-raw/csv/users/users.csv
        │  ② CREATE EXTERNAL TABLE analytics_agent_raw.users_csv（全列 string）
        ▼
    awsdatacatalog.analytics_agent_raw.users_csv
        │  ③ INSERT INTO ... SELECT CAST(...)  ← 类型在这一步还原
        ▼
    s3tablescatalog/analytics-agent-tables 里的 app_analytics.users

为什么不直接一步到位（比如本地转 Parquet 再 INSERT，或者生成 INSERT ... VALUES）：

- **Parquet 路线**要在本地装 pyarrow/numpy 并保证列序与 DDL 一致，等于把类型正确性
  的责任搬回本地脚本。CSV + 外部表把类型转换写成 SQL，转换规则是**可查询、可复现**
  的——出问题时能直接 `SELECT` 那条表达式看它到底把什么变成了什么。
- **INSERT ... VALUES** 要为 19 万行生成上百条语句，每条产生一批 Iceberg 数据文件，
  小文件数量炸掉，而且没有任何一步能单独验证。

这条路径也正好是数据湖该有的样子：原始文件留在 S3（可回溯、可重放），Glue 记录它的
结构，Athena 负责把它读成有类型的行。灌完之后 `*_csv` 外部表可以留着——它是**唯一
的原始态**，对账时能拿来和 Iceberg 侧逐值比。

## CSV 编码约定（都是实测确认的，不是猜的）

    NULL          空字段。Postgres COPY CSV 的约定：不带引号的空 = NULL。
                  注意 pgcsv 对 None 和真空串都写成空字段，两者在 CSV 里**无法区分**，
                  所以一律还原成 NULL（与 v1 的 Postgres 装载行为一致）。
    boolean       't' / 'f'（不是 'true'/'false'）。Trino 的 CAST 两种都吃。
    timestamp     '2026-01-07 14:11:10.53532'，小数 5~6 位
    数组          Postgres 字面量 '{a,b,c}'，空数组是 '{}'（≠ NULL）
    JSONB         json.dumps 的结果，空对象是 '{}'

**时间戳必须 `CAST(... AS timestamp(6))`。** 写成 `CAST(... AS timestamp)` 只到毫秒，
`.53532` 会静默变成 `.535`——不报错、不告警。一致性对账会在「明明灌成功了」的前提下
莫名对不上。这是本次迁移里最贵的一个坑，见 athena.py 模块 docstring 第 3 点。

**数组用朴素 `split(',')`**，因为实测这批数据里 8 个数组列、2869 个非空值**没有任何
一个**元素带引号或转义（Postgres 字面量只在元素含逗号/空格/引号时才加引号）。
朴素 split 因此是安全的——但这是**数据的性质，不是格式的保证**。所以 `--preflight`
每次都重新验证这一条，一旦生成器改了文本池冒出带逗号的 tag，这里会红灯而不是
静默把一个元素切成两个。

## OpenCSVSerde 的三个约束

1. **所有列必须声明成 string。** 它不做类型转换，声明 int 会在读的时候报错。
   类型还原全部放在 ③ 的 SELECT 里。
2. **不支持字段内换行。** `--preflight` 会比对物理行数与 csv 模块解析出的逻辑行数，
   不等就拒绝上传（这批数据实测相等）。
3. `skip.header.line.count=1` 跳表头。少了这一行，表头会变成一行全是列名的数据，
   而所有列都是 string，**不会报错**——它会安静地多出一行。

## 幂等

每张表灌之前先 `DELETE FROM`（Iceberg 支持行级删除，Athena engine v3 起）。
重跑不叠加。外部表每次 DROP + CREATE，所以改了列名也不会留下旧定义。

用法：

    python3 scripts/lakehouse/load.py --preflight        # 只做本地校验，无云调用
    python3 scripts/lakehouse/load.py                    # 全量
    python3 scripts/lakehouse/load.py --only users posts
    python3 scripts/lakehouse/load.py --skip-upload      # CSV 已在 S3，只重灌
    python3 scripts/lakehouse/load.py --verify           # 只核对行数
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import gen_ddl  # noqa: E402  只依赖标准库，--preflight 要保持无云依赖

CSV_DIR = os.path.join(ROOT, "data", "csv")
RAW_DB = os.environ.get("RAW_GLUE_DB", "analytics_agent_raw")
CSV_PREFIX = "csv"

# CSV 里可能有很长的单元格（posts.content 是整段中文），默认上限会抛
# _csv.Error: field larger than field limit
csv.field_size_limit(10 ** 8)


# ---------------------------------------------------------------- 类型还原

# Iceberg（DDL，尖括号）→ Trino（DML，圆括号）。同一个类型两种写法，
# 见 gen_ddl.py 模块 docstring。
_ELEM = {"string": "varchar", "int": "integer", "bigint": "bigint"}


def cast_expr(col: str, ice: str) -> str:
    """生成把 CSV 的 string 列还原成 Iceberg 列类型的 Trino 表达式。

    所有分支都先 `NULLIF(c,'')`：空字段在 Postgres COPY CSV 里就是 NULL，
    不先转 NULL 的话 `CAST('' AS bigint)` 直接报错，`CAST('' AS varchar)` 又会
    悄悄留下一个空串——两种都和 v1 的 Postgres 装载结果不一致。
    """
    c = f'"{col}"'
    nn = f"NULLIF({c}, '')"

    if ice.startswith("array<"):
        elem = ice[len("array<"):-1]
        if elem not in _ELEM:
            raise ValueError(f"不认识的数组元素类型 {elem!r}（{col}）")
        trino_elem = _ELEM[elem]
        # 剥掉 {}，按逗号切。空数组 '{}' 必须单独处理：
        # split('', ',') 返回 [''] 而不是 []，会凭空多出一个空元素。
        inner = f"split(substr({c}, 2, length({c}) - 2), ',')"
        if trino_elem != "varchar":
            inner = f"transform({inner}, x -> CAST(x AS {trino_elem}))"
        return (f"CASE WHEN {nn} IS NULL THEN NULL"
                f" WHEN {c} = '{{}}' THEN CAST(ARRAY[] AS array({trino_elem}))"
                f" ELSE {inner} END")

    if ice == "string":
        return nn
    if ice == "timestamp":
        # (6) 不是可选的：不写就只到毫秒，且不报错。见模块 docstring。
        return f"CAST({nn} AS timestamp(6))"
    if ice in ("int", "bigint", "float", "double", "date", "boolean") \
            or ice.startswith("decimal("):
        trino = "integer" if ice == "int" else ice
        return f"CAST({nn} AS {trino})"
    raise ValueError(f"不认识的 Iceberg 类型 {ice!r}（{col}）")


# ---------------------------------------------------------------- 本地校验

def csv_rows(table: str) -> int:
    """CSV 的逻辑行数（不含表头）。"""
    with open(os.path.join(CSV_DIR, f"{table}.csv"), encoding="utf-8",
              newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1


def preflight(tables: list[tuple[str, str, list]]) -> tuple[int, dict[str, int]]:
    """本地校验，不碰云。返回 (失败数, {表名: 行数})。

    四件事：CSV 存在且表头与 DDL 列名逐字同序；行尾是 LF 不是 CRLF；没有字段内换行；
    数组字面量里没有带引号的元素。前三条是 OpenCSVSerde / LineRecordReader 的硬约束，
    第四条是 `split(',')` 能不能用的前提。
    """
    bad = 0
    counts: dict[str, int] = {}
    for _, tbl, cols in tables:
        path = os.path.join(CSV_DIR, f"{tbl}.csv")
        if not os.path.isfile(path):
            print(f"  ❌ {tbl}: 缺 {os.path.relpath(path, ROOT)}")
            bad += 1
            continue
        want = [c for c, _, _, _ in cols]
        arr_idx = {i: gen_ddl.map_type(pg)
                   for i, (_, pg, _, _) in enumerate(cols)
                   if gen_ddl.map_type(pg).startswith("array<")}

        with open(path, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = next(rd, [])
            if head != want:
                extra = set(head) - set(want)
                miss = set(want) - set(head)
                print(f"  ❌ {tbl}: CSV 表头与 DDL 不一致"
                      f"{f'  CSV 多 {sorted(extra)}' if extra else ''}"
                      f"{f'  CSV 缺 {sorted(miss)}' if miss else ''}"
                      f"{'' if extra or miss else '（列名相同但顺序不同）'}")
                bad += 1
                continue
            n = 0
            quoted = 0
            for row in rd:
                n += 1
                for i, ice in arr_idx.items():
                    v = row[i] if i < len(row) else ""
                    if v and v != "{}" and ('"' in v or "\\" in v):
                        quoted += 1
        counts[tbl] = n

        # 行尾必须是 LF。这一条要**按字节**查：上面 csv.reader（newline=""）和下面
        # 文本模式的 open() 都会把 '\r\n' 吸收掉，CRLF 对那两条断言完全不可见。
        # 而装载链路上的 Hadoop LineRecordReader 只按 '\n' 切行，于是每行末尾多出的
        # '\r' 留在最后一列的值里：最后一列是时间戳时 `CAST(... AS timestamp(6))`
        # 装载报错、且报错指不到成因；是文本列时更糟——不报错，值里静默多一个不可见字符。
        with open(path, "rb") as f:
            crlf = f.read(1 << 20).count(b"\r\n")
        if crlf:
            print(f"  ❌ {tbl}: 行尾是 CRLF（前 1MB 里 {crlf} 处）。"
                  f"LineRecordReader 只切 '\\n'，'\\r' 会留在最后一列的值里")
            bad += 1

        # 字段内换行：物理行数应等于逻辑行数
        with open(path, encoding="utf-8") as f:
            phys = sum(1 for _ in f) - 1
        if phys != n:
            print(f"  ❌ {tbl}: 有字段内换行（物理 {phys} 行 / 逻辑 {n} 行）。"
                  f"OpenCSVSerde 不支持，会把一行读成多行")
            bad += 1
        if quoted:
            print(f"  ❌ {tbl}: {quoted} 个数组值的元素带引号或转义，"
                  f"split(',') 会把它切错。需要改成正确解析 Postgres 数组字面量")
            bad += 1
    return bad, counts


# ---------------------------------------------------------------- 云侧

def external_ddl(table: str, cols: list, bucket: str) -> str:
    """原始 CSV 的 Glue 外部表 DDL。全列 string —— OpenCSVSerde 不做类型转换。"""
    body = ",\n".join(f"  `{c}` string" for c, _, _, _ in cols)
    return (
        f"CREATE EXTERNAL TABLE `{table}_csv` (\n{body}\n)\n"
        f"ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'\n"
        f"WITH SERDEPROPERTIES ("
        f"'separatorChar'=',', 'quoteChar'='\"', 'escapeChar'='\\\\')\n"
        f"STORED AS TEXTFILE\n"
        f"LOCATION 's3://{bucket}/{CSV_PREFIX}/{table}/'\n"
        f"TBLPROPERTIES ('skip.header.line.count'='1')")


def insert_sql(table: str, cols: list) -> str:
    """INSERT INTO <iceberg 表> SELECT <还原表达式> FROM <awsdatacatalog 的 CSV 表>。

    源表写全限定名 `awsdatacatalog.<db>.<t>_csv`：目标 catalog 名带斜杠不能进 SQL
    （走 QueryExecutionContext），源 catalog 名没有斜杠，可以直接写在 FROM 里。
    一条语句跨两个 catalog，这是 Athena 少见但成立的用法。
    """
    names = ",\n  ".join(f'"{c}"' for c, _, _, _ in cols)
    exprs = ",\n  ".join(cast_expr(c, gen_ddl.map_type(pg))
                         for c, pg, _, _ in cols)
    return (f"INSERT INTO {table} (\n  {names}\n)\nSELECT\n  {exprs}\n"
            f"FROM awsdatacatalog.{RAW_DB}.{table}_csv")


def main() -> int:
    ap = argparse.ArgumentParser(description="CSV → S3 → Glue 外部表 → Iceberg")
    ap.add_argument("--only", nargs="*", help="只处理这些表")
    ap.add_argument("--preflight", action="store_true", help="只做本地校验，无云调用")
    ap.add_argument("--verify", action="store_true", help="只核对行数")
    ap.add_argument("--skip-upload", action="store_true", help="CSV 已在 S3")
    ap.add_argument("--print-sql", metavar="TABLE", help="打印某表的两条 SQL 就退出")
    a = ap.parse_args()

    tables = gen_ddl.parse_source()
    if a.only:
        known = {t for _, t, _ in tables}
        unknown = [t for t in a.only if t not in known]
        if unknown:
            print(f"没有这些表：{unknown}", file=sys.stderr)
            return 1
        tables = [x for x in tables if x[1] in a.only]

    if a.print_sql:
        cols = next(c for _, t, c in tables if t == a.print_sql)
        print(external_ddl(a.print_sql, cols, "<桶>"))
        print(";\n")
        print(insert_sql(a.print_sql, cols))
        return 0

    print(f"前置校验（本地，{len(tables)} 张表）")
    bad, counts = preflight(tables)
    if bad:
        print(f"\n{bad} 项失败 ❌  先修 CSV/DDL，别灌进去再回滚")
        return 1
    print(f"  表头与列序一致、行尾 LF、无字段内换行、数组字面量可 split  "
          f"合计 {sum(counts.values()):,} 行 ✅")
    if a.preflight:
        return 0

    import boto3
    import athena
    c = athena.Client()
    raw = athena.Client(catalog="awsdatacatalog", database=RAW_DB)
    s3 = boto3.client("s3", region_name=athena.REGION)
    bucket = athena.RAW_BUCKET

    if a.verify:
        return verify(c, tables, counts)

    t0, total, failed = time.time(), 0, []
    for _, tbl, cols in tables:
        t1 = time.time()
        try:
            if not a.skip_upload:
                s3.upload_file(os.path.join(CSV_DIR, f"{tbl}.csv"), bucket,
                               f"{CSV_PREFIX}/{tbl}/{tbl}.csv")
            # DROP + CREATE：改了列名也不会留下旧定义
            raw.execute(f"DROP TABLE IF EXISTS `{tbl}_csv`", fetch=False)
            raw.execute(external_ddl(tbl, cols, bucket), fetch=False)
            # 幂等：Iceberg 支持行级 DELETE（Athena engine v3 起）
            c.execute(f"DELETE FROM {tbl}", timeout=1800, fetch=False)
            c.execute(insert_sql(tbl, cols), timeout=1800, fetch=False)
            n = c.execute(f"SELECT count(*) FROM {tbl}")["rows"][0][0]
        except Exception as e:
            print(f"  {tbl:<24} FAIL  {str(e)[:400]}")
            failed.append(tbl)
            continue
        want = counts[tbl]
        mark = "✅" if n == want else f"❌ 期望 {want:,}"
        total += n
        print(f"  {tbl:<24} {n:>9,} 行  {time.time()-t1:>6.1f}s  {mark}",
              flush=True)
        if n != want:
            failed.append(tbl)

    print(f"\n合计 {total:,} 行，耗时 {time.time()-t0:.1f}s")
    if failed:
        print(f"失败 {len(failed)} 张：{failed}")
        return 1
    print("全部一致 ✅")
    return 0


def verify(c, tables: list, counts: dict[str, int]) -> int:
    """只核对行数：Iceberg count(*) ⟷ 本地 CSV 行数。

    Iceberg 的 count(*) 走清单文件元数据，扫描 0 字节，所以这一步几乎免费——
    可以放进 test_all.sh 每次都跑。
    """
    bad = 0
    for _, tbl, _ in tables:
        try:
            r = c.execute(f"SELECT count(*) FROM {tbl}")
            n = r["rows"][0][0]
        except Exception as e:
            print(f"  {tbl:<24} FAIL  {str(e)[:120]}")
            bad += 1
            continue
        want = counts[tbl]
        if n == want:
            print(f"  {tbl:<24} {n:>9,} 行  ✅")
        else:
            print(f"  {tbl:<24} {n:>9,} 行  ❌ CSV 是 {want:,}")
            bad += 1
    if bad:
        print(f"\n{bad} 张表行数不符 ❌")
        return 1
    print(f"\n{len(tables)} 张表行数与 CSV 完全一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
