#!/usr/bin/env python3
"""离线验证 data_classification.yaml 覆盖提交的 48 表 schema 与治理快照。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from classification import (  # noqa: E402
    catalog_schema,
    governance_findings,
    load_inventory,
    structural_findings,
)


def main() -> int:
    inventory = load_inventory()
    catalog_path = ROOT / "web" / "catalog.json"
    actual = catalog_schema(catalog_path)
    findings = structural_findings(actual, inventory)

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    governance = catalog.get("governance") or {}
    attached = {}
    for item in governance.get("masked") or []:
        for column in item.get("columns") or []:
            attached[(item["table"], column)] = item["policy"]
    ungranted = set(governance.get("ungranted_tables") or [])
    granted = set(actual) - ungranted
    findings += governance_findings(inventory, attached, granted)

    if findings:
        print("数据分类清单自测失败：")
        for finding in findings:
            print("  -", finding)
        return 1
    print(f"全部通过：{len(actual)} 张表、{sum(map(len, actual.values()))} 列均已审阅，治理声明与快照一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
