# ⚠️ 本文件是**生成物**，不要手改。
# 由 scripts/deploy/sync_agent_code.py 从 backend/metric_layer.py 逐字拷来（除本横幅）。
# 要改请改源文件，再跑 `python3 scripts/deploy/sync_agent_code.py --apply`。
# L0 会跑 `--check`：两侧不一致就红。
"""Metric 编译器：把 call_metric 调用编译成作用于 mart 表的 SQL（Trino / Athena 方言）。

思路类似语义层的 compile()，但更简单：mart 表已经预聚合 + 口径冻结（脏活在建表时做完），
所以无需 BFS join 图——单表聚合即可。

调用形态（对齐 blog："上季度美国 GMV" → gmv(country=US, date=last_quarter)）：
    compile_metric("gmv", time_window="last_quarter", group_by=["channel"], filters={"channel":"Google"})
返回 {sql, metric, owner, 口径声明, unit, label, version}，由 call_metric 工具执行并附 source footer。

方言与口径要点：
- 时间锚点用全局业务日历 (SELECT max(as_of_date) FROM meta_snapshot) 而非 current_date、
  也不是各表自己的 max(dt)——各表轴末不齐，用表自身 max 会静默给错数（见 metrics_def 注释）。
- 比率类 ratio 用 NULLIF 防除零；as_pct 的 metric 乘 100。
"""
from __future__ import annotations

from metrics_def import (METRICS, DIMENSIONS, TIME_WINDOWS, ANCHOR_SQL,
                         is_bounded_above)


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

    # --- WHERE: 时间窗 + 口径域上界 + filters ---
    where_parts = []
    time_col = m.get("time_col", "dt")
    if time_window:
        if time_window not in TIME_WINDOWS:
            raise MetricNotCovered(
                f"时间窗 '{time_window}' 未定义。可用: {sorted(TIME_WINDOWS)}")
        # 时间列由 metrics_def 显式声明，缺省 "dt"。声明成 None = 该指标**不支持时间窗**。
        #
        # 原来这里把 "dt" 写死在锚点里，于是 repurchase_rate_30d（作用在
        # mart_user_summary，那张表按用户建行、没有 dt 列）任何带 time_window 的调用
        # 都在 Athena 上抛 COLUMN_NOT_FOUND —— 而且是**运行时**才炸：编译期看不出来，
        # 对账器也扫不到（它只扫 sql/numerator/denominator，锚点列是这里隐式注入的）。
        # 现在锚点列变成注册表里的数据，reconcile 的 D 检查因此能一起校验它存在。
        if not time_col:
            raise MetricNotCovered(
                f"metric '{metric}' 不支持时间窗（作用在 {table}，该表不是按日建行）。"
                + (m.get("time_window_hint") or "请去掉 time_window 参数取全量值。"))
        # 锚点是全局业务日历（meta_snapshot.as_of_date），**不是**该表自己的 max(dt)：
        # 各表轴末不齐，用表自身 max 会让同一句"最近 30 天"在不同指标上落在不同日期，
        # 且在成本表上直接落到没有归因数据的区间，静默返回 NULL/0。详见 metrics_def。
        where_parts.append(TIME_WINDOWS[time_window]
                           .replace("<ANCHOR>", ANCHOR_SQL)
                           .replace("<COL>", time_col))

    # 口径域上界（clamp_to_anchor）：**不管有没有传 time_window**，都把行限制在
    # 业务日历轴内。给作用在"轴比业务日历长、且轴外那段没有配对数据"的表上的指标用。
    #
    # 只有 cac/roi 声明了它：mart_channel_daily 的成本铺到 2026-09-01，归因止于
    # 2026-01-24。不 clamp 的话 time_window='all'（= 1=1）会拿轴外成本除轴内新客，
    # 实测把「哪个渠道获客成本最低」答成"微博KOL 938 元"——那个渠道轴内一分钱没花。
    #
    # 为什么不直接给 TIME_WINDOWS['all'] 加上界（那样更省事）：对 refund_amount 是错的。
    # 它轴外那 9 天是轴内订单真实产生的退款，clamp 掉总额就从 963560.92 掉回 925471.33。
    # 同一个"全量"在两个指标上语义相反，所以这必须是**按指标声明**，不能是全局开关。
    #
    # 有界窗口（last_7d 等）自己已经带 `<COL> <= <ANCHOR>`，不重复加——判定交给
    # metrics_def.is_bounded_above 看模板，而不是在拼好的字符串里找子串。
    if m.get("clamp_to_anchor") and time_col and not is_bounded_above(time_window):
        where_parts.append(f"{time_col} <= {ANCHOR_SQL}")

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
    # 未覆盖演示。第三条钉住"无日期轴的表 + 时间窗"必须**编译期**显式拒绝：
    # 曾经它会编译出 `WHERE dt > ...` 然后在 Athena 上抛 COLUMN_NOT_FOUND，
    # 报错指不到成因；退化回那个行为时这条自测会挂。
    for bad in [("nonexistent", {}), ("gmv", {"group_by": ["channel"]}),
                ("repurchase_rate_30d", {"time_window": "last_30d"})]:
        try:
            compile_metric(bad[0], **bad[1])
        except MetricNotCovered as e:
            print(f"\nMetricNotCovered(预期): {e}")


if __name__ == "__main__":
    _selftest()
