"""数据分类清单：声明态校验与治理实际态对账。

清单为每张表保存 reviewed_columns。普通列可继承表/全局默认分类，但新增列不会
自动进入 reviewed_columns，因此 Glue 出现新列时一定报错，避免“默认无保护”。
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "database" / "redshift" / "data_classification.yaml"
LEVELS = {"public", "internal", "pii", "restricted"}
TREATMENTS = {"none", "mask", "no_grant"}


def load_inventory(path: Path = INVENTORY) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("version") != 1 or not isinstance(doc.get("tables"), dict):
        raise ValueError("data_classification.yaml 必须是 version: 1 且包含 tables 映射")
    doc.setdefault("defaults", {})
    return doc


def catalog_schema(path: Path | None = None) -> dict[str, set[str]]:
    import json

    p = path or ROOT / "web" / "catalog.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for domain in doc.get("domains", []):
        for table in domain.get("tables", []):
            cols = table.get("columns", [])
            out[table["name"]] = {
                c["name"] if isinstance(c, dict) else str(c) for c in cols
            }
    return out


def structural_findings(actual: dict[str, set[str]], inventory: dict) -> list[str]:
    """检查全部表/列已审阅、枚举合法，且敏感处置有充分声明。"""
    findings: list[str] = []
    defaults = inventory.get("defaults", {})
    tables = inventory["tables"]

    for name in sorted(set(actual) - set(tables)):
        findings.append(f"{name}：整张表未登记")
    for name in sorted(set(tables) - set(actual)):
        findings.append(f"{name}：清单登记了但实际 schema 中不存在")

    for name in sorted(set(actual) & set(tables)):
        spec = tables[name] or {}
        reviewed = set(spec.get("reviewed_columns") or [])
        overrides = spec.get("columns") or {}
        missing = sorted(actual[name] - reviewed)
        orphan = sorted(reviewed - actual[name])
        unknown_overrides = sorted(set(overrides) - reviewed)
        if missing:
            findings.append(f"{name}：新增列未审阅 {missing}")
        if orphan:
            findings.append(f"{name}：reviewed_columns 含已删除列 {orphan}")
        if unknown_overrides:
            findings.append(f"{name}：列级分类不在 reviewed_columns {unknown_overrides}")

        table_meta = {k: spec[k] for k in ("level", "treatment", "reason") if k in spec}
        for col in sorted(reviewed & actual[name]):
            col_meta = overrides.get(col) or {}
            meta = {**defaults, **table_meta, **col_meta}
            level, treatment = meta.get("level"), meta.get("treatment")
            if level not in LEVELS:
                findings.append(f"{name}.{col}：非法 level={level!r}")
            if treatment not in TREATMENTS:
                findings.append(f"{name}.{col}：非法 treatment={treatment!r}")
            if treatment == "mask" and not col_meta.get("policy"):
                findings.append(f"{name}.{col}：mask 必须声明 policy")
            if col_meta.get("treatment") == "no_grant":
                findings.append(f"{name}.{col}：no_grant 只能用于整表；列级请 mask 或拆表")
            if level in {"pii", "restricted"} and treatment == "none" \
                    and not col_meta.get("reason"):
                findings.append(f"{name}.{col}：敏感列保留明文必须写列级 reason")
    return findings


def expected_governance(inventory: dict) -> tuple[dict[tuple[str, str], str], set[str]]:
    """返回期望 DDM {(table,column): policy} 与整表 no_grant 集合。"""
    defaults = inventory.get("defaults", {})
    masks: dict[tuple[str, str], str] = {}
    no_grant: set[str] = set()
    for table, raw in inventory["tables"].items():
        spec = raw or {}
        table_treatment = spec.get("treatment", defaults.get("treatment", "none"))
        if table_treatment == "no_grant":
            no_grant.add(table)
        for column, meta in (spec.get("columns") or {}).items():
            treatment = meta.get("treatment", table_treatment)
            if treatment == "mask":
                masks[(table, column)] = meta["policy"]
    return masks, no_grant


def governance_findings(inventory: dict,
                        attached: dict[tuple[str, str], str],
                        granted: set[str]) -> list[str]:
    expected_masks, no_grant = expected_governance(inventory)
    findings: list[str] = []
    for key, policy in sorted(expected_masks.items()):
        if attached.get(key) != policy:
            findings.append(f"{key[0]}.{key[1]}：期望 DDM {policy}，实际 {attached.get(key)!r}")
    for key, policy in sorted(attached.items()):
        if expected_masks.get(key) != policy:
            findings.append(f"{key[0]}.{key[1]}：实际挂了未声明的 DDM {policy}")

    expected_granted = set(inventory["tables"]) - no_grant
    for table in sorted(no_grant & granted):
        findings.append(f"{table}：清单标 no_grant，但 analytics_agent_ro 仍可 SELECT")
    for table in sorted(expected_granted - granted):
        findings.append(f"{table}：清单允许读取，但 analytics_agent_ro 没有 SELECT")
    return findings
