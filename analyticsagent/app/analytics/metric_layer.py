"""Metric 编译器：把 call_metric 调用编译成作用于 mart 表的 PostgreSQL SQL。

思路类似语义层的 compile()，但更简单：mart 表已经预聚合 + 口径冻结（脏活在建表时做完），
所以无需 BFS join 图——单表聚合即可。

调用形态（对齐 blog："上季度美国 GMV" → gmv(country=US, date=last_quarter)）：
    compile_metric("gmv", time_window="last_quarter", group_by=["channel"], filters={"channel":"Google"})
返回 {sql, metric, owner, 口径声明, unit, label, version}，由 call_metric 工具执行并附 source footer。

PostgreSQL 方言要点：
- 时间锚点用 (SELECT max(dt) FROM <table>) 而非 current_date（静态样本，见 metrics_def 注释）。
- 比率类 ratio 用 NULLIF 防除零；as_pct 的 metric 乘 100。
"""
from __future__ import annotations

from metrics_def import METRICS, DIMENSIONS, TIME_WINDOWS


class MetricNotCovered(Exception):
    """metric / dimension / time_window 未在治理层定义时抛出——显式失败而非乱编。"""


def _quote(v):
    """字面量转 SQL：字符串加引号并转义单引号；布尔/数字原样。"""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def compile_metric(metric: str,
                   group_by: list[str] | None = None,
                   filters: dict | None = None,
                   time_window: str | None = None) -> dict:
    """编译一个 metric 调用成 SQL。未覆盖的 metric/维度/时间窗显式抛 MetricNotCovered。"""
    if metric not in METRICS:
        raise MetricNotCovered(
            f"metric '{metric}' 未在治理层定义。可用: {sorted(METRICS)}")
    m = METRICS[metric]
    group_by = group_by or []
    filters = filters or {}
    table = m["table"]

    # --- 值表达式：simple(sql) 或 ratio(numerator/denominator) ---
    if m.get("type") == "ratio":
        num, den = m["numerator"], m["denominator"]
        scale = " * 100" if m.get("as_pct") else ""
        value_sql = f"CAST({num} AS REAL){scale} / NULLIF({den}, 0)"
    else:
        value_sql = m["sql"]
    value_alias = metric

    # --- 校验维度（group_by + filters 的 key）都已定义 ---
    for d in list(group_by) + list(filters):
        if d not in DIMENSIONS:
            raise MetricNotCovered(
                f"维度 '{d}' 未定义。可用: {sorted(DIMENSIONS)}")
        if d not in m.get("dimensions", []):
            raise MetricNotCovered(
                f"metric '{metric}' 不支持按 '{d}' 切片。该指标维度: {m.get('dimensions', [])}")

    # --- SELECT: 维度列 + 值 ---
    dim_select = [f"{DIMENSIONS[d]['sql']} AS {d}" for d in group_by]
    select_parts = dim_select + [f"{value_sql} AS {value_alias}"]

    # --- WHERE: 时间窗 + filters ---
    where_parts = []
    if time_window:
        if time_window not in TIME_WINDOWS:
            raise MetricNotCovered(
                f"时间窗 '{time_window}' 未定义。可用: {sorted(TIME_WINDOWS)}")
        anchor = f"(SELECT max(dt) FROM {table})"  # nosec B608 —— table 来自可信指标注册表(metrics_def),非用户输入
        where_parts.append(TIME_WINDOWS[time_window].replace("<ANCHOR>", anchor))
    for d, v in filters.items():
        col = DIMENSIONS[d]["sql"]
        where_parts.append(f"{col} = {_quote(v)}")
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # --- GROUP BY / ORDER BY ---
    group_sql = ""
    order_sql = ""
    if group_by:
        cols = ", ".join(DIMENSIONS[d]["sql"] for d in group_by)
        group_sql = f" GROUP BY {cols}"
        order_sql = f" ORDER BY {cols}"

    sql = f"SELECT {', '.join(select_parts)} FROM {table}{where_sql}{group_sql}{order_sql}"  # nosec B608 —— 同上;filter 值经 _quote() 转义,其余均来自代码定义的注册表
    sql = " ".join(sql.split())

    return {
        "sql": sql,
        "metric": metric,
        "label": m["label"],
        "owner": m["owner"],
        "business_line": m["business_line"],
        "unit": m.get("unit", ""),
        "口径声明": m["口径声明"],
        "version": m["version"],
        "group_by": group_by,
        "time_window": time_window,
    }


def _selftest():
    """离线单测：只编译 SQL（不连库），人工核对几个 metric 的输出 SQL 是否正确。"""
    cases = [
        ("gmv", {"time_window": "last_quarter"}),
        ("gmv_by_channel", {"group_by": ["channel"], "time_window": "last_30d"}),
        ("cac", {"group_by": ["channel"]}),
        ("roi", {"filters": {"channel": "Google Ads"}}),
        ("repurchase_rate_30d", {"group_by": ["register_channel"]}),
        ("dau", {"group_by": ["date"], "time_window": "last_7d"}),
    ]
    for metric, kw in cases:
        out = compile_metric(metric, **kw)
        print(f"\ncall_metric({metric}, {kw})")
        print(f"  owner={out['owner']} version={out['version']} unit={out['unit']}")
        print(f"  SQL: {out['sql']}")
    # 未覆盖演示
    for bad in [("nonexistent", {}), ("gmv", {"group_by": ["channel"]})]:
        try:
            compile_metric(bad[0], **bad[1])
        except MetricNotCovered as e:
            print(f"\nMetricNotCovered(预期): {e}")


if __name__ == "__main__":
    _selftest()
