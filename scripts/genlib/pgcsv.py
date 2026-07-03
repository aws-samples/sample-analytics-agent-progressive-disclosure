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
- bool → 'true'/'false'。datetime64 → ISO 文本。
- 数组列用 to_pg_array 转 '{...}';dict 列用 json.dumps。
"""
from __future__ import annotations
import csv
import io
import json
import numpy as np

CHUNK_ROWS = 500_000   # 每块行数;内存/吞吐折中


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
        return np.datetime_as_string(v, unit='s')
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
            prepared[k] = np.datetime_as_string(arr, unit='s')
        else:
            prepared[k] = arr

    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)  # nosemgrep: use-defusedcsv —— 写出的是本项目生成的演示数据,非不可信输入
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
