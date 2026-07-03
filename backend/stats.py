"""统计计算的纯函数实现（不连库、不依赖 numpy，只用 Python stdlib）。

把深度分析要用的统计量做成【确定性、可审计、可复现】的计算，agent 通过 compute_stats
工具调用这里，而不是自己心算——数学交给确定性代码，模型只负责取数、编排、解读。

覆盖 5 类方法（对应 analysis/ 方法库）：
- describe / zscore：均值 μ、总体标准差 σ、变异系数 CV、Z 分数、3σ 异常检测。
- ratio_decompose：两因子中点分解（GMV = U × P），无残差。
- pareto：贡献度 + 累计占比 + 集中度 CR₃ / HHI。
- funnel：分步转化率、整体转化率、最大流失环节。
- retention：Cohort 留存率 Rₜ、断崖点。
"""
from __future__ import annotations

import statistics


class StatsError(Exception):
    """方法未知或参数不足/非法时抛出——显式失败，不静默乱算。"""


def _nums(xs) -> list[float]:
    """把输入清洗成 float 列表：跳过 None / 非数值，容忍字符串数字。"""
    out: list[float] = []
    for x in xs or []:
        if x is None:
            continue
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def describe(values, labels=None) -> dict:
    v = _nums(values)
    n = len(v)
    if n == 0:
        return {"n": 0}
    mu = statistics.fmean(v)
    sigma = statistics.pstdev(v)          # 总体标准差（分母 n，样本量固定时用总体）
    return {
        "n": n, "mean": mu, "std": sigma,
        "cv": (sigma / mu) if mu else None,
        "min": min(v), "max": max(v),
        "median": statistics.median(v), "sum": sum(v),
    }


def zscore(values, labels=None, threshold: float = 2.0) -> dict:
    """Z-score / kσ 异常检测。|Z|>threshold 判异常（默认 2σ≈95%）。"""
    v = _nums(values)
    n = len(v)
    if n == 0:
        return {"n": 0, "points": []}
    mu = statistics.fmean(v)
    sigma = statistics.pstdev(v)
    labels = labels or list(range(1, n + 1))
    points = []
    for i, x in enumerate(v):
        z = (x - mu) / sigma if sigma else 0.0
        points.append({
            "label": labels[i] if i < len(labels) else i + 1,
            "value": x, "z": round(z, 3), "dev": x - mu,
            "pct_vs_mean": ((x / mu - 1) * 100) if mu else None,
            "is_anomaly": abs(z) > threshold,
        })
    anomalies = [p for p in points if p["is_anomaly"]]
    max_pt = max(points, key=lambda p: abs(p["z"])) if points else None
    return {
        "n": n, "mean": mu, "std": sigma,
        "cv": (sigma / mu) if mu else None,
        "threshold": threshold,
        "upper": mu + threshold * sigma, "lower": mu - threshold * sigma,
        "upper_3sigma": mu + 3 * sigma, "lower_3sigma": mu - 3 * sigma,
        "n_anomalies": len(anomalies), "anomalies": anomalies,
        "max_abs_z": max_pt, "points": points,
    }


def ratio_decompose(u0, u1, p0, p1) -> dict:
    """两因子中点分解：GMV = U × P。用中点法（对称、无残差），比顺序法准。
    因子U贡献 = ΔU × (P₀+P₁)/2；因子P贡献 = ΔP × (U₀+U₁)/2；两者相加 = ΔGMV。"""
    u0, u1, p0, p1 = float(u0), float(u1), float(p0), float(p1)
    total0, total1 = u0 * p0, u1 * p1
    d_total, d_u, d_p = total1 - total0, u1 - u0, p1 - p0
    contrib_u = d_u * (p0 + p1) / 2
    contrib_p = d_p * (u0 + u1) / 2
    return {
        "total0": total0, "total1": total1, "d_total": d_total,
        "total_change_pct": (d_total / total0 * 100) if total0 else None,
        "u0": u0, "u1": u1, "d_u": d_u,
        "u_change_pct": (d_u / u0 * 100) if u0 else None,
        "p0": p0, "p1": p1, "d_p": d_p,
        "p_change_pct": (d_p / p0 * 100) if p0 else None,
        "contrib_u": contrib_u, "contrib_p": contrib_p,
        "contrib_u_share": (contrib_u / d_total * 100) if d_total else None,
        "contrib_p_share": (contrib_p / d_total * 100) if d_total else None,
        "residual": d_total - (contrib_u + contrib_p),   # 中点法应 ≈ 0
    }


def pareto(values, labels=None) -> dict:
    """贡献度 + 帕累托累计占比 + 集中度（CR₃ / HHI）。"""
    v = _nums(values)
    n = len(v)
    if n == 0:
        return {"n": 0, "items": []}
    labels = labels or list(range(1, n + 1))
    pairs = sorted(zip(labels, v), key=lambda t: t[1], reverse=True)
    total = sum(v)
    items, cum, n80 = [], 0.0, None
    for i, (lab, val) in enumerate(pairs):
        share = (val / total) if total else 0.0
        cum += share
        if n80 is None and cum >= 0.8:
            n80 = i + 1
        items.append({"label": lab, "value": val,
                      "share": share * 100, "cum_share": cum * 100})
    shares = [(val / total) if total else 0.0 for _, val in pairs]
    return {
        "n": n, "total": total,
        "top1_share": shares[0] * 100 if shares else 0,
        "cr3": sum(shares[:3]) * 100,
        "hhi": sum(s * s for s in shares),      # 赫芬达尔指数 ∈ (0,1]，越大越集中
        "n_for_80pct": n80, "items": items,
    }


def funnel(values, labels=None) -> dict:
    """漏斗分步转化：步间转化率、整体转化率（连乘）、最大流失环节。"""
    v = _nums(values)
    n = len(v)
    if n == 0:
        return {"n": 0, "steps": []}
    labels = labels or [f"步骤{i + 1}" for i in range(n)]
    steps = []
    for i in range(n):
        step = {"label": labels[i] if i < len(labels) else f"步骤{i + 1}", "count": v[i]}
        if i > 0 and v[i - 1]:
            step["conv_from_prev"] = v[i] / v[i - 1] * 100
            step["loss_from_prev"] = (1 - v[i] / v[i - 1]) * 100
            step["drop"] = v[i - 1] - v[i]
        steps.append(step)
    overall = (v[-1] / v[0] * 100) if v[0] else None
    losses = [(i, steps[i].get("loss_from_prev", 0.0)) for i in range(1, n)]
    bottleneck = max(losses, key=lambda t: t[1]) if losses else None
    return {
        "n": n, "overall_conv": overall,
        "bottleneck_step": steps[bottleneck[0]]["label"] if bottleneck else None,
        "bottleneck_loss": bottleneck[1] if bottleneck else None,
        "steps": steps,
    }


def retention(values, labels=None, n0=None) -> dict:
    """Cohort 留存：每期留存率 Rₜ = Nₜ / N₀，断崖点（相邻跌幅最大处）。"""
    v = _nums(values)
    n = len(v)
    if n == 0:
        return {"n": 0, "points": []}
    base = float(n0) if n0 else v[0]
    labels = labels or [f"D{i}" for i in range(n)]
    points = []
    for i in range(n):
        r = (v[i] / base) if base else 0.0
        points.append({"label": labels[i] if i < len(labels) else f"D{i}",
                       "count": v[i], "retention": r * 100})
    cliff = None
    for i in range(1, n):
        drop = points[i - 1]["retention"] - points[i]["retention"]
        if cliff is None or drop > cliff[1]:
            cliff = (points[i]["label"], drop)
    return {
        "n": n, "base": base, "points": points,
        "cliff_at": cliff[0] if cliff else None,
        "cliff_drop": cliff[1] if cliff else None,
    }


_DISPATCH = {
    "describe":        lambda values, labels, params: describe(values, labels),
    "zscore":          lambda values, labels, params: zscore(values, labels, float(params.get("threshold", 2.0))),
    "ratio_decompose": lambda values, labels, params: ratio_decompose(params["u0"], params["u1"], params["p0"], params["p1"]),
    "pareto":          lambda values, labels, params: pareto(values, labels),
    "funnel":          lambda values, labels, params: funnel(values, labels),
    "retention":       lambda values, labels, params: retention(values, labels, params.get("n0")),
}


def compute(method: str, values=None, labels=None, params=None) -> dict:
    """统一入口：按 method 分派。未知方法/参数不足显式抛 StatsError。"""
    method = (method or "").strip()
    if method not in _DISPATCH:
        raise StatsError(f"未知统计方法 '{method}'。可用: {sorted(_DISPATCH)}")
    try:
        return _DISPATCH[method](values or [], labels or [], params or {})
    except (KeyError, TypeError, ValueError) as e:
        raise StatsError(f"方法 '{method}' 参数不足或非法: {e}")


def _selftest():
    """离线单测：核对各方法输出，尤其中点分解无残差、帕累托累计=100%。"""
    print("describe:", describe([10, 12, 11, 50, 9]))
    z = zscore([100, 102, 98, 101, 180], labels=["d1", "d2", "d3", "d4", "d5"])
    print("zscore μ/σ/异常:", round(z["mean"], 2), round(z["std"], 2), z["n_anomalies"])
    rd = ratio_decompose(u0=1000, u1=1033, p0=300, p1=270)
    print("ratio 贡献U/P/残差:", round(rd["contrib_u"]), round(rd["contrib_p"]), round(rd["residual"], 6))
    pr = pareto([50, 30, 12, 5, 3], labels=["A", "B", "C", "D", "E"])
    print("pareto CR3/HHI/达80%需:", round(pr["cr3"], 1), round(pr["hhi"], 3), pr["n_for_80pct"])
    fn = funnel([1000, 620, 200, 150], labels=["浏览", "加购", "下单", "支付"])
    print("funnel 整体/瓶颈:", round(fn["overall_conv"], 1), fn["bottleneck_step"], round(fn["bottleneck_loss"], 1))
    rt = retention([1000, 420, 260, 180], labels=["D0", "D1", "D7", "D30"])
    print("retention R1/R7/R30/断崖:", round(rt["points"][1]["retention"], 1),
          round(rt["points"][2]["retention"], 1), round(rt["points"][3]["retention"], 1), rt["cliff_at"])


if __name__ == "__main__":
    _selftest()
