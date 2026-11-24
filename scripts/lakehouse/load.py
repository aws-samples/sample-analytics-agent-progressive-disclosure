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

## 幂等：`DELETE FROM` 还是 `DROP` + 重建

默认每张表灌之前先 `DELETE FROM`（Iceberg 支持行级删除，Athena engine v3 起），
重跑不叠加。外部表每次 DROP + CREATE，所以改了列名也不会留下旧定义。

`--recreate` 换成 DROP + 按 `database/iceberg/01_tables.sql` 重建。**8000 万行的
那一次必须用它**，两个原因，第二个是硬的：

1. `DELETE FROM` 在 Iceberg 上是一次真实的重写：它要读全表、标记删除、生成新快照，
   代价随表大小走。DROP 是一次元数据操作，跟行数无关。
2. **`CREATE TABLE IF NOT EXISTS` 不会改已经建好的表**，所以分区规格
   （`gen_ddl.PARTITION_SPEC`，events / page_views / post_likes / orders 四张）
   加不上去——DELETE 之后灌进去的还是不分区的旧表，而且没有任何一步会报错。
   要让分区生效只能先 DROP。

代价说清楚：**DROP 会连带掉这张表的 Lake Formation 授权**（列级排除是挂在表上的），
所以 `--recreate` 跑完必须重跑 `scripts/lakehouse/governance.py`，否则 L4 的边界
静默消失——`AGENT_ROLE_ARN` 那条路径会变成「查不到表」或「什么列都能查」。这条不靠
记性，`--recreate` 结束时会打出来，L0 也有一条断言守着这段提示还在。

用法：

    python3 scripts/lakehouse/load.py --preflight        # 只做本地校验，无云调用
    python3 scripts/lakehouse/load.py                    # 全量（DELETE + INSERT）
    python3 scripts/lakehouse/load.py --recreate         # DROP + 建表 + INSERT
    python3 scripts/lakehouse/load.py --only users posts
    python3 scripts/lakehouse/load.py --skip-upload      # CSV 已在 S3，只重灌
    python3 scripts/lakehouse/load.py --verify           # 只核对行数
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import re
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import gen_ddl  # noqa: E402  只依赖标准库，--preflight 要保持无云依赖

# 环境变量 CSV_DIR 覆盖：全量那 8000 万行是 6.8GB，不能落在仓库的 data/csv 里
# （那目录进 git，也进 `COPY knowledge/` 那类镜像构建）。
#
# **`verify_load.py` 认同一个变量**，因为它是这次装载唯一的对账口。真设错了也不会静默
# 过去：它比的是 CSV 侧现算 ⟷ Athena 现查，两侧读的不是同一批数据时行数直接差几个数量级，
# 结果是刷屏的红，不是绿灯。这条是刻意留成"会响的错"，而不是加一道相互校验。
CSV_DIR = os.environ.get("CSV_DIR") or os.path.join(ROOT, "data", "csv")
RAW_DB = os.environ.get("RAW_GLUE_DB", "analytics_agent_raw")
CSV_PREFIX = "csv"

# Athena 的 **DML query timeout 配额**是 30 分钟（us-west-2 实测 1800s，配额码
# L-E80DC288，可提额，本账号没提过）。DELETE / INSERT 都是 DML，走这个上限；
# 外部表的 DROP / CREATE 是 DDL，另一个上限 600s 且不可调，但那两条是元数据操作。
#
# 客户端超时刻意压在配额下面一分钟，而不是写成同一个 1800：
#
# - 写成 1800 时两边同时到点，谁先响不确定。服务端先响是一条读不出成因的 FAILED，
#   客户端先响是 athena.py 那条「超时（已取消）」——同一个故障两种面貌，看到的人
#   没法判断该改代码还是该提配额。
# - 压低之后一定是客户端先响，而它会主动 `stop_query_execution`（否则查询继续扫字节
#   继续计费）。于是"灌不完"这件事有唯一的、可控的表现。
#
# **真的撞上这条线时，要做的是提配额，不是把这个数字改大。** 8000 万行里最大的两张是
# page_views ≈1290 万行和 post_likes ≈1520 万行，单表 CSV 1.6GB 上下。
DML_TIMEOUT = 1740

# 超过这个大小的 CSV 传上去之前先 gzip。**这不是为了省上传时间，是为了不撞成本护栏。**
#
# workgroup 上的 `BytesScannedCutoffPerQuery` 是 1 GiB（`setup.py::BYTES_CUTOFF`，
# 配 `EnforceWorkGroupConfiguration=true`，单条查询绕不过去）。而外部 CSV 表没有分区，
# 每条 INSERT 都要**整表扫一遍源文件**——所以 `month_batches` 把 91 个分区切成 4 批之后，
# 扫描量不是变成四分之一，而是变成 4 倍：每一批各扫 1 遍完整 CSV。8000 万行那一版里
# `page_views.csv` 1.64GB、`events.csv` 1.31GB，第一批就报
# `CANCELLED: Bytes scanned limit was exceeded`，两张表整表没灌进去（其余 33 张成功，
# 于是合计行数少 2143 万——一个不像装载失败的失败）。
#
# gzip 之后 Athena 读的是压缩后的字节，扫描量按同一个比例降（实测中文 CSV 约 8.8 倍，
# 1.64GB → 0.19GB）。OpenCSVSerde + STORED AS TEXTFILE 按扩展名自动解压，DDL 一个字不用改。
#
# 为什么不去把护栏调大：那是这套架构里唯一的成本上限（`setup.py` 第 4 条），一条漏了
# join 条件的 SQL 能扫掉很多钱，调它属于「关掉安全措施来适配数据」。压缩是反过来的做法——
# 让数据适配护栏，而且顺带让**查询侧**也便宜，护栏原样留着。
#
# 阈值取 512MiB 而不是贴着 1GiB：护栏是对**扫描量**的限制，而扫描量还会随
# `SELECT` 里的表达式和将来加的列长。留一半余量，免得下次数据涨 20% 又撞线。
GZIP_ABOVE = 512 * 1024 ** 2

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
#
# 8000 万行（6.8GB）下这一层的成本从"忽略不计"变成了瓶颈：原来是三趟 Python 层循环
# ——`csv.reader` 逐行、逐行扫数组列里的引号、再逐行数一遍物理行。有 pyarrow 时改走
# 它的流式 CSV reader，逐列做向量化的子串匹配，字节那趟单独走一次大块读。
#
# **两条路径的判据必须给出同一个结论**，所以 stdlib 那条留着（`PREFLIGHT_ENGINE=stdlib`
# 可以强制走它），而不是删掉：一是 `--preflight` 是文档承诺的"无云、无第三方依赖"检查，
# 二是留着才能互相验。2026-08-31 拿 scale=1 的 24 张表两条路径逐字节比过输出，相同。

def _byte_scan(path: str) -> tuple[int, int, bool]:
    """一趟字节扫描 → (换行符数, CRLF 数, 末尾是否有换行)。

    行尾这件事**只能按字节查**：`csv.reader`（newline=""）和文本模式的 `open()` 都会
    把 '\\r\\n' 吸收掉，CRLF 对它们完全不可见。而装载链路上的 Hadoop LineRecordReader
    只按 '\\n' 切行，于是每行末尾多出的 '\\r' 留在最后一列的值里：最后一列是时间戳时
    `CAST(... AS timestamp(6))` 装载报错、且报错指不到成因；是文本列时更糟——不报错，
    值里静默多一个不可见字符。

    原来 CRLF 只查前 1MB。这里查全文件：物理行数那趟本来就要读完，顺手数完不多花钱。
    """
    nl = crlf = 0
    tail = b""
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            nl += chunk.count(b"\n")
            crlf += (tail + chunk).count(b"\r\n")   # 跨块的 '\r' + '\n' 也要算上
            tail = chunk[-1:]
    return nl, crlf, tail in (b"\n", b"")


def _scan_arrow(path: str, want: list[str], arr_cols: list[str]):
    """pyarrow 流式扫描 → (表头, 逻辑行数, 带引号的数组值个数)。"""
    import pyarrow as pa
    import pyarrow.compute as pc
    from pyarrow import csv as pacsv

    # 全列按 string 读：这一层要看的是**字面量**，任何类型推断都会把要查的东西改掉
    # （空字段变 null、`{a,b}` 变别的）。newlines_in_values=True 让解析器认引号内的
    # 换行，这样它数出来的是**逻辑**行数——和字节那趟的物理行数比，差值就是字段内换行。
    #
    # 空文件 pyarrow 直接抛 ArrowInvalid，标准库那条返回空表头。统一成后者：调用方按
    # 「表头对不上」报一条干净的 ❌，而不是甩一段 traceback。
    if os.path.getsize(path) == 0:
        return [], 0, 0
    with pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(block_size=1 << 26),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(
            column_types={c: pa.string() for c in want},
            strings_can_be_null=False),
    ) as rd:
        head = list(rd.schema.names)
        if head != want:
            return head, 0, 0
        n = quoted = 0
        for batch in rd:
            n += batch.num_rows
            for c in arr_cols:
                col = batch.column(head.index(c))
                hit = pc.or_(pc.match_substring(col, '"'),
                             pc.match_substring(col, "\\"))
                quoted += pc.sum(pc.cast(hit, pa.int64())).as_py() or 0
    return head, n, quoted


def _scan_stdlib(path: str, want: list[str], arr_cols: list[str]):
    """标准库扫描 → 与 `_scan_arrow` 同形的三元组。"""
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = next(rd, [])
        if head != want:
            return head, 0, 0
        idx = [head.index(c) for c in arr_cols]
        n = quoted = 0
        for row in rd:
            n += 1
            for i in idx:
                v = row[i] if i < len(row) else ""
                if v and ('"' in v or "\\" in v):
                    quoted += 1
    return head, n, quoted


def _scanner():
    """→ (扫描函数, 引擎名)。`PREFLIGHT_ENGINE=stdlib` 强制走标准库那条。"""
    if os.environ.get("PREFLIGHT_ENGINE") == "stdlib":
        return _scan_stdlib, "stdlib"
    try:
        import pyarrow.csv  # noqa: F401
    except ImportError:
        return _scan_stdlib, "stdlib（没装 pyarrow）"
    return _scan_arrow, "pyarrow"


def csv_rows(table: str) -> int:
    """CSV 的逻辑行数（不含表头）。"""
    scan, _ = _scanner()
    path = os.path.join(CSV_DIR, f"{table}.csv")
    with open(path, encoding="utf-8", newline="") as f:
        want = next(csv.reader(f), [])
    return scan(path, want, [])[1]


# Athena 的 Iceberg INSERT 一次最多同时开 **100 个分区写入器**，超了直接
# `ICEBERG_TOO_MANY_OPEN_PARTITIONS`。报错原文说「Athena will not delete data in your
# account」，也就是**可能**在 staging 桶里留下一个 manifest 和一批孤儿数据文件，得自己清。
# 措辞是有条件的（"If a data manifest file was generated at ..."）：2026-09-01 那两次
# 失败核查过，manifest 没生成，孤儿文件也随后被那张表的 DROP 一并带走了。别把"没留下"
# 当成常态——它取决于失败发生在写入的哪一步。
#
# 这条限制 2026-09-01 真的撞上了，撞的方式值得记下来：`post_likes.created_at` 在
# 仓库里那份 `data/csv`（v1 产出）上跨 **365 天**，而新生成器产出的同一列只跨 91 天。
# 也就是说同一个分区规格在两份数据上一个失败一个成功——只在云上、只在灌到那张表时
# 才暴露，而前面每一层都是绿的。所以判据放在这里：**本地、灌之前、按分区列现数**。
MAX_OPEN_PARTITIONS = 100


def _partition_keys(path: str, col: str, width: int) -> int:
    """分区键基数。`_partition_key_set` 的行数版，读起来更直白。"""
    return len(_partition_key_set(path, col, width))


def _partition_key_set(path: str, col: str, width: int) -> set[str]:
    """CSV 里某个时间列的分区键集合 —— 这个变换会开出哪些分区。

    按前 `width` 个字符切（`day` 是 10 → `2026-01-07`，`month` 是 7 → `2026-01`），
    不解析时间戳：这一层要的是**分组**，不是值。CSV 里的时间戳是 ISO 前缀格式，
    字符串截断和真解析给出同一个分组，而截断能向量化。
    """
    scan_arrow = True
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        from pyarrow import csv as pacsv
    except ImportError:
        scan_arrow = False

    if scan_arrow:
        keys: set[str] = set()
        with pacsv.open_csv(
            path,
            read_options=pacsv.ReadOptions(block_size=1 << 26),
            parse_options=pacsv.ParseOptions(newlines_in_values=True),
            convert_options=pacsv.ConvertOptions(
                include_columns=[col], column_types={col: pa.string()},
                null_values=[""], strings_can_be_null=True),
        ) as rd:
            for batch in rd:
                arr = pc.drop_null(batch.column(0))
                keys.update(pc.unique(pc.utf8_slice_codeunits(arr, 0, width)).to_pylist())
        return keys

    keys = set()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            v = row.get(col) or ""
            if v:
                keys.add(v[:width])
    return keys


def check_partitions(tables: list[tuple[str, str, list]]) -> int:
    """每一**批** INSERT 开出的分区数必须放得进 100 个并发写入器。返回失败数。

    判的是单批而不是整表：整表的分区数可以远超 100（`post_likes` 在 `data/csv` 上
    是 365 个），切批之后每批只落在一个月里。所以这里现算每批的分区键数，判最大那批。

    只查配了分区的表（`gen_ddl.PARTITION_SPEC`）。不配分区的表没有这条约束。
    """
    bad = 0
    width = {"day": 10, "month": 7, "year": 4, "hour": 13}
    for _, tbl, _ in tables:
        spec = gen_ddl.PARTITION_SPEC.get(tbl)
        if not spec:
            continue
        transform, col = spec
        path = os.path.join(CSV_DIR, f"{tbl}.csv")
        if not os.path.isfile(path):
            continue                       # 缺文件那条由 preflight 报，不重复
        keys = _partition_key_set(path, col, width[transform])
        # 每批一个月：把分区键按它的年月前缀归堆，最大那堆就是单批的写入器数。
        # `month` / `year` 粒度下分区键本身就比月粗，一批只有 1 个键。
        heaps: dict[str, int] = {}
        for k in keys:
            heaps[k[:7]] = heaps.get(k[:7], 0) + 1
        worst = max(heaps.values()) if heaps else 0
        nbatch = len(heaps)
        if worst >= MAX_OPEN_PARTITIONS:
            print(f"  ❌ {tbl}: {transform}({col}) 单批最多开 {worst} 个分区，"
                  f"≥ {MAX_OPEN_PARTITIONS} 上限。按月切批已经是最细的切法，"
                  f"所以只能把 PARTITION_SPEC 里这张表换成更粗的粒度")
            bad += 1
        else:
            print(f"  {tbl:<24} {transform}({col}) → 共 {len(keys)} 个分区 / "
                  f"{nbatch} 批，单批最多 {worst} 个（上限 {MAX_OPEN_PARTITIONS}）")
    return bad


def preflight(tables: list[tuple[str, str, list]]) -> tuple[int, dict[str, int]]:
    """本地校验，不碰云。返回 (失败数, {表名: 行数})。

    四件事：CSV 存在且表头与 DDL 列名逐字同序；行尾是 LF 不是 CRLF；没有字段内换行；
    数组字面量里没有带引号的元素。前三条是 OpenCSVSerde / LineRecordReader 的硬约束，
    第四条是 `split(',')` 能不能用的前提。

    分区列基数那一条单独放在 `check_partitions()` 里，由 `main()` 接着调——它只对
    配了分区的表成立，混进这个循环会让"每张表四件事"这个结构读不出来。
    """
    bad = 0
    counts: dict[str, int] = {}
    scan, engine = _scanner()
    print(f"  （扫描引擎：{engine}）")
    for _, tbl, cols in tables:
        path = os.path.join(CSV_DIR, f"{tbl}.csv")
        if not os.path.isfile(path):
            print(f"  ❌ {tbl}: 缺 {os.path.relpath(path, ROOT)}")
            bad += 1
            continue
        want = [c for c, _, _, _ in cols]
        arr_cols = [c for c, pg, _, _ in cols
                    if gen_ddl.map_type(pg).startswith("array<")]

        head, n, quoted = scan(path, want, arr_cols)
        if head != want:
            extra = set(head) - set(want)
            miss = set(want) - set(head)
            print(f"  ❌ {tbl}: CSV 表头与 DDL 不一致"
                  f"{f'  CSV 多 {sorted(extra)}' if extra else ''}"
                  f"{f'  CSV 缺 {sorted(miss)}' if miss else ''}"
                  f"{'' if extra or miss else '（列名相同但顺序不同）'}")
            bad += 1
            continue
        counts[tbl] = n

        nl, crlf, ends_nl = _byte_scan(path)
        if crlf:
            print(f"  ❌ {tbl}: 行尾是 CRLF（{crlf} 处）。"
                  f"LineRecordReader 只切 '\\n'，'\\r' 会留在最后一列的值里")
            bad += 1

        # 字段内换行：物理行数应等于逻辑行数
        phys = nl - 1 + (0 if ends_nl else 1)
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


def upload_csv(s3, bucket: str, table: str) -> str:
    """把一张表的本地 CSV 传到 `s3://bucket/csv/<table>/`，返回实际传上去的对象名。

    大文件先 gzip（见 `GZIP_ABOVE`）。两件事必须一起做，少一件就错：

    1. **传之前把前缀清空。** 外部表的 LOCATION 是整个前缀，Athena 读前缀下的**所有**
       对象。上一轮留下的 `events.csv` 和这一轮的 `events.csv.gz` 会被一起读成
       8541400 × 2 行——而且不报错，`verify` 那步只会说行数不对，看不出是重复。
    2. **压缩走临时文件而不是内存。** 1.6GB 的源文件在内存里 gzip 掉 1.6GB 常驻，
       临时文件只占压缩后的 0.19GB，用完就删。
    """
    prefix = f"{CSV_PREFIX}/{table}/"
    old = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    if old:
        s3.delete_objects(Bucket=bucket,
                          Delete={"Objects": [{"Key": o["Key"]} for o in old]})
    src = os.path.join(CSV_DIR, f"{table}.csv")
    if os.path.getsize(src) <= GZIP_ABOVE:
        s3.upload_file(src, bucket, f"{prefix}{table}.csv")
        return f"{table}.csv"
    with tempfile.NamedTemporaryFile(suffix=".csv.gz", delete=False) as tmp:
        gz_path = tmp.name
    try:
        with open(src, "rb") as fi, gzip.open(gz_path, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, length=8 * 1024 ** 2)
        print(f"    {table} gzip {os.path.getsize(src)/1024**3:.2f}GB → "
              f"{os.path.getsize(gz_path)/1024**3:.2f}GB", flush=True)
        s3.upload_file(gz_path, bucket, f"{prefix}{table}.csv.gz")
    finally:
        os.unlink(gz_path)
    return f"{table}.csv.gz"


def insert_sql(table: str, cols: list, where: str = "") -> str:
    """INSERT INTO <iceberg 表> SELECT <还原表达式> FROM <awsdatacatalog 的 CSV 表>。

    源表写全限定名 `awsdatacatalog.<db>.<t>_csv`：目标 catalog 名带斜杠不能进 SQL
    （走 QueryExecutionContext），源 catalog 名没有斜杠，可以直接写在 FROM 里。
    一条语句跨两个 catalog，这是 Athena 少见但成立的用法。

    `where` 用于分月切批（见 `month_batches`）。它加在**源表**上，而源表全列是
    string，所以过滤要按字符串前缀写，不能先 CAST——CAST 之后 Athena 未必把谓词推
    到扫描侧，而这里要的只是把行分开，字符串比较就够。
    """
    names = ",\n  ".join(f'"{c}"' for c, _, _, _ in cols)
    exprs = ",\n  ".join(cast_expr(c, gen_ddl.map_type(pg))
                         for c, pg, _, _ in cols)
    tail = f"\nWHERE {where}" if where else ""
    return (f"INSERT INTO {table} (\n  {names}\n)\nSELECT\n  {exprs}\n"
            f"FROM awsdatacatalog.{RAW_DB}.{table}_csv{tail}")


def month_batches(table: str, col: str) -> list[tuple[str, str]]:
    """把一张分区表的 INSERT 切成按月的若干批 → [(批名, WHERE 子句)]。

    为什么必须切：Athena 的 Iceberg INSERT 一次最多开 100 个分区写入器
    （`MAX_OPEN_PARTITIONS`）。`day()` 粒度下 100 天就到顶，而 `post_likes.created_at`
    在 `data/csv` 上跨 365 天。**实测 `ORDER BY <分区列>` 不解决问题**——加了之后照样
    报 `ICEBERG_TOO_MANY_OPEN_PARTITIONS`，所以这不是"让写入器顺序打开"能绕的，
    只能真的分批提交。

    按月切而不是按固定行数切，因为**批次之间必须按分区键不相交**：一个月最多 31 天，
    单批稳稳落在 100 以内；而不相交意味着每个 day 分区只被**一批**写到，于是不会因为
    分批而多出小文件。按行数切就没有这个性质——同一天会被切进两批，文件数翻倍。

    最后额外挂一批捞分区列为空的行。现在这四列都没有空值，但少了这一批的后果是
    **静默丢行**：`WHERE substr(...) = '2026-01'` 天然排除 NULL。行数断言
    （`n == want`）会兜住，可这条判据不该依赖另一条判据才成立。
    """
    path = os.path.join(CSV_DIR, f"{table}.csv")
    months = sorted(_partition_key_set(path, col, 7))
    out = [(m, f"substr(\"{col}\", 1, 7) = '{m}'") for m in months]
    out.append(("(空值)", f"NULLIF(\"{col}\", '') IS NULL"))
    return out


DDL_FILE = os.path.join(ROOT, "database", "iceberg", "01_tables.sql")


def create_stmts() -> dict[str, str]:
    """从生成的 Iceberg DDL 里切出 `{表名: CREATE 语句}`。

    读文件而不是调 `gen_ddl.render()`：仓库里那份 SQL 是**评审过的那一份**，也是
    `athena.py --file` 手工建表时执行的那一份。`--recreate` 必须和它们建出同一张表，
    否则「手工建的表」和「重灌建的表」会分岔。两者不同步的情况由 `gen_ddl --check`
    在 L0 拦住，这里只管用。
    """
    sys.path.insert(0, _HERE)
    import athena
    out = {}
    for stmt in athena.split_statements(open(DDL_FILE, encoding="utf-8").read()):
        m = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\(", stmt)
        if m:
            out[m.group(1)] = stmt.strip().rstrip(";")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="CSV → S3 → Glue 外部表 → Iceberg")
    ap.add_argument("--only", nargs="*", help="只处理这些表")
    ap.add_argument("--preflight", action="store_true", help="只做本地校验，无云调用")
    ap.add_argument("--verify", action="store_true", help="只核对行数")
    ap.add_argument("--skip-upload", action="store_true", help="CSV 已在 S3")
    ap.add_argument("--recreate", action="store_true",
                    help="DROP + 按 database/iceberg/01_tables.sql 重建，而不是 "
                         "DELETE FROM。分区规格只有走这条才生效；跑完必须重跑 "
                         "governance.py 补 Lake Formation 授权")
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
        # 带 --recreate 时把 DROP + CREATE 也打出来。8000 万行那一次是不可回退的
        # 操作，值得先把**真正要执行的那几条**看一遍，而不是照着 docstring 想象。
        if a.recreate:
            stmt = create_stmts().get(a.print_sql)
            if not stmt:
                print(f"{os.path.relpath(DDL_FILE, ROOT)} 里没有 {a.print_sql} 的建表语句",
                      file=sys.stderr)
                return 1
            print(f"DROP TABLE IF EXISTS {a.print_sql};\n")
            print(stmt + ";\n")
        else:
            print(f"DELETE FROM {a.print_sql};\n")
        print(insert_sql(a.print_sql, cols))
        return 0

    print(f"前置校验（本地，{len(tables)} 张表）")
    bad, counts = preflight(tables)
    if bad:
        print(f"\n{bad} 项失败 ❌  先修 CSV/DDL，别灌进去再回滚")
        return 1
    print(f"  表头与列序一致、行尾 LF、无字段内换行、数组字面量可 split  "
          f"合计 {sum(counts.values()):,} 行 ✅")

    # 分区列基数。**不管有没有 --recreate 都查**：`DELETE FROM` 那条路灌进已经建好的
    # 分区表时，INSERT 一样要开分区写入器，一样会撞 100 这条线。
    if any(t in gen_ddl.PARTITION_SPEC for _, t, _ in tables):
        print(f"分区列基数（上限 {MAX_OPEN_PARTITIONS} 个并发写入器）")
        pbad = check_partitions(tables)
        if pbad:
            print(f"\n{pbad} 项失败 ❌  灌下去会在 INSERT 那一步炸，"
                  f"并可能在 staging 桶里留下要手工清的孤儿数据文件")
            return 1
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

    ddl = {}
    if a.recreate:
        ddl = create_stmts()
        missing = [t for _, t, _ in tables if t not in ddl]
        if missing:
            print(f"{os.path.relpath(DDL_FILE, ROOT)} 里没有这些表的建表语句："
                  f"{missing}\n先跑 gen_ddl.py -o {os.path.relpath(DDL_FILE, ROOT)}",
                  file=sys.stderr)
            return 1
        parts = [t for _, t, _ in tables if t in gen_ddl.PARTITION_SPEC]
        print(f"\n--recreate：DROP + 重建 {len(tables)} 张表"
              f"（其中 {len(parts)} 张带分区：{parts}）")

    t0, total, failed = time.time(), 0, []
    for _, tbl, cols in tables:
        t1 = time.time()
        try:
            if not a.skip_upload:
                upload_csv(s3, bucket, tbl)
            # DROP + CREATE：改了列名也不会留下旧定义
            raw.execute(f"DROP TABLE IF EXISTS `{tbl}_csv`", fetch=False)
            raw.execute(external_ddl(tbl, cols, bucket), fetch=False)
            if a.recreate:
                # DROP 是元数据操作，走 DDL 超时（600s，不可调）而不是 DML_TIMEOUT。
                # 顺序不能反：先 DROP 再 CREATE，中间任何一步失败都会留下一张不存在
                # 或空的表，而 INSERT 会紧接着报错——不会出现「灌进了半张旧表」。
                c.execute(f"DROP TABLE IF EXISTS {tbl}", fetch=False, timeout=600)
                c.execute(ddl[tbl], fetch=False, timeout=600)
            else:
                # 幂等：Iceberg 支持行级 DELETE（Athena engine v3 起）
                c.execute(f"DELETE FROM {tbl}", timeout=DML_TIMEOUT, fetch=False)
            spec = gen_ddl.PARTITION_SPEC.get(tbl)
            if spec:
                # 分区表分月切批：一次 INSERT 最多开 100 个分区写入器。批次按分区键
                # 不相交，所以每个 day 分区只被一批写到，不会因为分批多出小文件。
                batches = month_batches(tbl, spec[1])
                for i, (label, where) in enumerate(batches, 1):
                    c.execute(insert_sql(tbl, cols, where),
                              timeout=DML_TIMEOUT, fetch=False)
                    print(f"    {tbl} 批 {i}/{len(batches)} {label}", flush=True)
            else:
                c.execute(insert_sql(tbl, cols), timeout=DML_TIMEOUT, fetch=False)
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
    if a.recreate:
        # 这段话是 --recreate 唯一的安全网：DROP 掉的 Lake Formation 授权不会自己回来，
        # 而缺授权的表现是「查不到表」或「列级排除消失」，两种都不长得像装载出的问题。
        print("\n⚠️  DROP 连带清掉了这些表的 Lake Formation 授权，必须重跑：")
        print("      python3 scripts/lakehouse/governance.py")
        print("    不跑的后果：AGENT_ROLE_ARN 那条路径上 L4 的列级边界静默消失。")
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
