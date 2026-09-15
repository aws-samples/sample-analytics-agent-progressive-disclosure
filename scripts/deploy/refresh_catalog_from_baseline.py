#!/usr/bin/env python3
"""用已归档的 Redshift post-data-fix baseline 刷新 catalog 行数。

只用于无法访问 AWS 时修复已提交快照；schema 仍来自原 Glue 快照。正式部署默认调用
build_catalog_json.py 从 Glue/Redshift 全量重建，不走此离线路径。
"""
from __future__ import annotations

import argparse
import json
import time
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
    counts = {name: data["count"] for name, data in baseline["tables"].items()}
    seen = set()
    for domain in catalog.get("domains", []):
        for table in domain.get("tables", []):
            name = table["name"]
            if name not in counts:
                raise SystemExit(f"baseline 缺表：{name}")
            table["rows"] = counts[name]
            seen.add(name)
    missing = sorted(set(counts) - seen)
    if missing:
        raise SystemExit(f"catalog 缺表：{missing}")

    totals = catalog["totals"]
    totals["rows_all_layers"] = sum(counts.values())
    base_names = {
        table["name"]
        for domain in catalog.get("domains", [])
        for table in domain.get("tables", [])
        if table.get("layer") == "base"
    }
    totals["rows"] = sum(counts[name] for name in base_names)
    catalog["snapshot"] = True
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    catalog["generated_at"] = now
    catalog["generated_at_utc"] = now + " UTC"
    try:
        source = args.baseline.relative_to(ROOT)
    except ValueError:
        source = args.baseline
    catalog["row_counts_source"] = str(source)

    args.catalog.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已从 {catalog['row_counts_source']} 刷新 {len(seen)} 张表；"
          f"rows={totals['rows']} rows_all_layers={totals['rows_all_layers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
