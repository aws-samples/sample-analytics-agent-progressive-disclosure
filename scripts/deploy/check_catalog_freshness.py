#!/usr/bin/env python3
"""拒绝部署与提交的 Redshift baseline 行数不一致的 catalog 快照。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "web" / "catalog.json"
DEFAULT_BASELINE = ROOT / "eval" / "baseline" / "consistency.redshift.postdatafix.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = ap.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = {
        table["name"]: table.get("rows")
        for domain in catalog.get("domains", [])
        for table in domain.get("tables", [])
    }
    expected = {name: data.get("count") for name, data in baseline.get("tables", {}).items()}

    problems = []
    for name in sorted(set(expected) - set(current)):
        problems.append(f"{name}: catalog 缺表")
    for name in sorted(set(current) - set(expected)):
        problems.append(f"{name}: baseline 缺表")
    for name in sorted(set(current) & set(expected)):
        if current[name] != expected[name]:
            problems.append(f"{name}: catalog={current[name]} baseline={expected[name]}")

    rows_all = (catalog.get("totals") or {}).get("rows_all_layers")
    expected_total = sum(v for v in expected.values() if isinstance(v, int))
    if rows_all != expected_total:
        problems.append(f"rows_all_layers: catalog={rows_all} baseline_sum={expected_total}")

    if problems:
        print("catalog 快照已过期或 baseline 未同步：")
        for problem in problems[:20]:
            print("  -", problem)
        if len(problems) > 20:
            print(f"  ……另有 {len(problems) - 20} 处")
        return 1
    print(f"catalog 快照新鲜度通过：{len(expected)}/{len(expected)} 张表，rows_all_layers={expected_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
