r"""
pgcsv —— 列式(dict[str, ndarray])→ Postgres COPY 兼容 CSV,分块流式写。

替换的 blocker:
- 旧 export_to_csv.py 先建完整 data 列表,再 `flat_data = [flatten_dict(r) for r in data]`
  造第二份全量拷贝 → 峰值内存翻倍。30-50M 行直接 OOM。
- 这里以"列"为单位持有数据(numpy ndarray,紧凑),按固定行块流式写出,
  任何时刻只在内存里放一个 chunk 的字符串,峰值内存与表大小解耦。

CSV 约定(与 \copy ... WITH CSV HEADER 对齐):
- 用 csv.writer(QUOTE_MINIMAL),逗号分隔,带 header。
- None / NaN → 空串(COPY 视为 NULL,DDL 列允许 NULL 时正确)。
- bool → 'true'/'false'。datetime64 → `YYYY-MM-DD HH:MM:SS`(**空格**分隔,见 _TS_SEP)。
- 数组列用 to_pg_array 转 '{...}';dict 列用 json.dumps。
"""
from __future__ import annotations
import csv
import datetime as dt
import io
import json
import numpy as np

CHUNK_ROWS = 500_000   # 每块行数;内存/吞吐折中


def _ts_text(arr: np.ndarray) -> np.ndarray:
    """
    datetime64 整列 → CSV 文本,**空格**分隔日期与时间。

    不能用 np.datetime_as_string 的原样输出:它给的是 ISO 的 `T` 分隔
    (`2026-01-23T22:35:38`),而下游 `scripts/lakehouse/load.py` 的
    `CAST(col AS timestamp(6))` 在 Athena/Trino 上对 `T` 分隔的文本直接抛
    INVALID_CAST_ARGUMENT——已实测。Trino 只认 `2026-01-23 22:35:38`
    这种空格分隔形式(秒精度和微秒精度都收)。

    分隔符是固定第 10 个字符,但仍走 np.char.replace 而不是切片拼接:
    这里每列只跑一次,可读性比那点开销值钱。

    **日精度的列(datetime64[D])必须按日输出**,不能一律按秒。`fillers.ts_to_date`
    返回的就是 datetime64[D],对应 DDL 里的 date 列,而 load.py 给 date 列生成的是
    `CAST(col AS date)`——喂它 `2026-01-23 00:00:00` 会失败。仓库里现有的
    data/csv 也是这个约定(channel_daily_costs.date = `2026-05-26`)。
    """
    unit = 'D' if arr.dtype == np.dtype('datetime64[D]') else 's'
    out = np.datetime_as_string(arr, unit=unit)
    return np.char.replace(out, 'T', ' ') if unit == 's' else out


def to_pg_array(lst) -> str:
    """Python list → Postgres 数组字面量 {a,b,...}。沿用 export_to_csv.py 的转义逻辑。"""
    if lst is None or len(lst) == 0:
        return '{}'
    out = []
    for item in lst:
        if item is None:
            out.append('NULL')
        elif isinstance(item, str):
            esc = item.replace('\\', '\\\\').replace('"', '\\"')
            if any(c in esc for c in [',', ' ', '{', '}', '"', '\\']):
                out.append(f'"{esc}"')
            else:
                out.append(esc)
        else:
            out.append(str(item))
    return '{' + ','.join(out) + '}'


def _cell(v):
    """单元格 → CSV 文本,贴合 Postgres COPY 的 NULL/bool/数组/JSON 约定。"""
    if v is None:
        return ''
    if isinstance(v, float) and np.isnan(v):
        return ''
    if isinstance(v, (bool, np.bool_)):
        return 'true' if v else 'false'
    if isinstance(v, (list, tuple, np.ndarray)):
        return to_pg_array(list(v))
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, np.datetime64):
        return str(_ts_text(np.asarray([v]))[0])
    if isinstance(v, dt.datetime):
        # 走到这里的是 **object 列里的** 时间戳：条件时间列（`_cond_ts` / `ts_offset`
        # 那类要按掩码置空的）只能用 object 数组表达,None 与 datetime 混在一起,
        # 所以 write_table 上面那段整列向量化转换不认它,逐格落到这里。
        #
        # 必须显式给一支,不能落到下面的 str()——虽然 `str(datetime)` 恰好也给空格分隔,
        # 但它在 microsecond==0 时省掉小数部分、非 0 时带上,同一列里两种宽度混着出现。
        # 这里钉死成与 _ts_text 逐字符一致的秒精度形式,让「可空的时间列」和普通时间列
        # 在 CSV 里长得完全一样——这类列恰好是最少被抽查的。
        return v.replace(microsecond=0).isoformat(sep=' ')
    if isinstance(v, dt.date):
        # date 没有时间部分,isoformat() 就是 'YYYY-MM-DD',DDL 里对应 date 列。
        return v.isoformat()
    return str(v)


def write_table(path: str, columns: dict[str, np.ndarray],
                chunk_rows: int = CHUNK_ROWS) -> int:
    """
    把列式数据写成一个 CSV 文件(header + 行),分块、流式。
    columns: 有序 dict,键=列名,值=等长 ndarray/序列。返回写出的行数。
    """
    names = list(columns.keys())
    n = len(columns[names[0]]) if names else 0
    # 预处理:datetime64 整列一次性转字符串(向量化,远快于逐格)
    prepared = {}
    for k, col in columns.items():
        arr = col
        if isinstance(arr, np.ndarray) and np.issubdtype(arr.dtype, np.datetime64):
            prepared[k] = _ts_text(arr)
        else:
            prepared[k] = arr

    with open(path, 'w', encoding='utf-8', newline='') as f:
        # lineterminator 必须显式给 '\n'：csv.writer 的默认值是 **'\r\n'**,而这些文件
        # 的下游是 Hadoop 的 LineRecordReader（S3 → Iceberg 装载),它只按 '\n' 切行,
        # 于是每行末尾多出的 '\r' 会留在最后一列的值里。最后一列往往是 created_at,
        # 结果是 `CAST(... AS timestamp(6))` 在装载时报错,而报错完全指不到成因;
        # 若最后一列恰好是文本列则更糟——不报错,值里静默多一个不可见字符。
        # 仓库里现有的 data/csv/*.csv 是 LF,load.py 的 preflight 有一条断言盯着这件事。
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator='\n')  # nosemgrep: use-defusedcsv —— 写出的是本项目生成的演示数据,非不可信输入
        w.writerow(names)
        for start in range(0, n, chunk_rows):
            end = min(start + chunk_rows, n)
            cols = [prepared[k][start:end] for k in names]
            # object 列(数组/dict)逐格处理;标量列可直接迭代
            rows = []
            for i in range(end - start):
                rows.append([_cell(c[i]) for c in cols])
            w.writerows(rows)
    return n
