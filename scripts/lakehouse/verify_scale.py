#!/usr/bin/env python3
"""规模对账：`docs/scale.json` 声明的规模 ⟷ 湖里实际的规模。

## 为什么需要这一条

本仓库的对账层比表、比列、比枚举取值、比退化列、比装载完整性——**从不比规模**。
2026-09-17 的实测状态说明了后果：五处文档写着 19 万行，开发账号的湖里装的是
8000 万行，L0–L6 全绿。数量级差 **427 倍**而没有一盏灯亮，因为没有任何一条断言
的对象是「有多少行」。

`verify_load.py` 的 `check_doc_totals()` 已经守住了**种子那一侧**（文档声明
⟷ `data/csv/` 现数）。缺的是另一侧：**湖里现在装的是哪一批**。这个脚本补它。

## 判据

`docs/scale.json` 登记每一批数据的每表行数。检查分两层：

1. **离线**（`--selftest`，不连云）
   - `seed` 批次的每表行数 ≡ `data/csv/` 现数出来的（仓库里躺着的就是真源）
   - 每批的 `rows_total` ≡ 该批 `rows` 的和
   - `groups` 恰好划分 35 张表，不重不漏
   - `declarations` 里每条声明的原文在对应文件里的**出现次数**等于登记的
     `occurrences`，且声明值与它自称描述的那一批相等。钉次数而不是钉"只准一处"：
     同一个数在一个文件里正当地出现多次（正文、启动日志、对照表），但**改了一处
     漏了另一处**就会让次数变化，这里红；掉到 0 则说明这条声明已经不指向任何东西，
     该从 `docs/scale.json` 里摘掉（理由同 `verify_load.py::check_doc_totals()`）
   - `full` 批次的数字自身要满足 groups 的结构性约束（纯 JSON 自洽，不连云）

2. **在线**（默认）
   - 现查 35 张表的 `count(*)`（Iceberg 走元数据，扫描 0 字节）
   - 必须**逐表命中某一批**已登记的规模。命中不了就红，并印出它最像哪一批、
     差在哪些表——装了第三批数据就必须先来 `docs/scale.json` 登记
   - 结构性约束按实测的 scale 再验一遍：`scaled` 组等比、`fixed` 组不变、
     `sublinear` 组严格落在两者之间

第 2 层的结构性约束本身就是 #14 的 PR「Not included」里那句话的机器版：
「转化侧放大 427 倍，而广告/券/活动维度表没有」。它是缺口不是特性——CAC、ROI、
券核销率会随 scale 漂移。写成断言之后，哪天维度表也开始放大，这里会红。

用法：

    python3 scripts/lakehouse/verify_scale.py              # 连云，比湖里的实际规模
    python3 scripts/lakehouse/verify_scale.py --selftest   # 离线，比声明与 data/csv
    python3 scripts/lakehouse/verify_scale.py --json       # 机器读（含实测每表行数）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

SCALE_JSON = ROOT / "docs" / "scale.json"
CSV_DIR = ROOT / "data" / "csv"

# scaled 组的容差。生成器按比例取整，实测 subscriptions ×427.080 而基准 users
# ×427.070——差 0.002%。给 0.2% 的余量，足够吸收取整、又拦得住"少灌了一张表"。
SCALED_TOL = 0.002


def load_decl() -> dict:
    with SCALE_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def csv_row_counts() -> dict[str, int]:
    """`data/csv/*.csv` 每张表的数据行数（不含表头）。"""
    out: dict[str, int] = {}
    for p in sorted(CSV_DIR.glob("*.csv")):
        with p.open(encoding="utf-8", newline="") as f:
            out[p.stem] = max(sum(1 for _ in csv.reader(f)) - 1, 0)
    return out


# ---------------------------------------------------------------- 结构性约束

def check_groups(decl: dict, counts: dict[str, int], label: str) -> list[str]:
    """按 groups 验一批实际行数。counts 是某一批的每表行数（声明的或实测的）。"""
    bad: list[str] = []
    seed = decl["batches"]["seed"]["rows"]
    ref = decl["scale_ref_table"]
    if ref not in counts or not seed.get(ref):
        return [f"{label}：拿不到基准表 {ref} 的行数，无法定 scale"]
    scale = counts[ref] / seed[ref]

    for t in decl["groups"]["scaled"]:
        want = seed[t] * scale
        got = counts.get(t)
        if got is None:
            bad.append(f"{label}：{t} 缺行数")
        elif want and abs(got - want) / want > SCALED_TOL:
            bad.append(f"{label}：{t} 应随 scale 等比（×{scale:.3f} → {want:,.0f}），"
                       f"实际 {got:,}（×{got / seed[t]:.3f}）")
    for t in decl["groups"]["fixed"]:
        got = counts.get(t)
        if got is None:
            bad.append(f"{label}：{t} 缺行数")
        elif got != seed[t]:
            bad.append(f"{label}：{t} 是 fixed 组、不该随 scale 变，"
                       f"种子 {seed[t]:,} → 实际 {got:,}（×{got / seed[t]:.3f}）。"
                       f"若生成器改了行为，把它挪出 fixed 组并说明")
    for t in decl["groups"]["sublinear"]:
        got = counts.get(t)
        if got is None:
            bad.append(f"{label}：{t} 缺行数")
        elif scale > 1 and not (seed[t] <= got < seed[t] * scale):
            bad.append(f"{label}：{t} 是 sublinear 组，应落在 "
                       f"[{seed[t]:,}, {seed[t] * scale:,.0f}) 之间，实际 {got:,}")
    return bad


def scale_of(decl: dict, counts: dict[str, int]) -> float:
    """实测 scale = 基准表的行数 / 它在种子里的行数。"""
    seed = decl["batches"]["seed"]["rows"]
    ref = decl["scale_ref_table"]
    return counts.get(ref, 0) / seed[ref] if seed.get(ref) else 0.0


# ---------------------------------------------------------------- 离线自测

def selftest(decl: dict) -> int:
    bad: list[str] = []
    seed_rows = decl["batches"]["seed"]["rows"]

    # 一、seed 声明 ⟷ data/csv 现数。CSV 是真源，scale.json 是抄的那一份。
    real = csv_row_counts()
    if not real:
        bad.append(f"{CSV_DIR.relative_to(ROOT)} 里没有 CSV，无法验 seed 声明")
    else:
        for t in sorted(set(seed_rows) | set(real)):
            if t not in real:
                bad.append(f"seed 声明里有 {t}，data/csv/ 里没有这个文件")
            elif t not in seed_rows:
                bad.append(f"data/csv/{t}.csv 存在，但 seed 声明里没登记")
            elif seed_rows[t] != real[t]:
                bad.append(f"seed.{t}：声明 {seed_rows[t]:,}，"
                           f"data/csv/ 现数 {real[t]:,}")

    # 二、每批 rows_total ≡ rows 的和；seed 还要对上 lines_total（含表头）
    for name, b in decl["batches"].items():
        s = sum(b["rows"].values())
        if b["rows_total"] != s:
            bad.append(f"{name}.rows_total 写 {b['rows_total']:,}，rows 求和 {s:,}")
    sd = decl["batches"]["seed"]
    if sd["lines_total"] != sd["rows_total"] + len(sd["rows"]):
        bad.append(f"seed.lines_total 写 {sd['lines_total']:,}，"
                   f"应为 rows_total + 表头数 = "
                   f"{sd['rows_total'] + len(sd['rows']):,}")

    # 三、groups 恰好划分 35 张表
    grouped: list[str] = []
    for g in decl["groups"].values():
        grouped += g
    dup = sorted({t for t in grouped if grouped.count(t) > 1})
    if dup:
        bad.append(f"groups 里重复登记：{', '.join(dup)}")
    missing = sorted(set(seed_rows) - set(grouped))
    extra = sorted(set(grouped) - set(seed_rows))
    if missing:
        bad.append(f"groups 漏了这些表（新表加进来必须归组）：{', '.join(missing)}")
    if extra:
        bad.append(f"groups 里有表不在 seed 声明里：{', '.join(extra)}")

    # 四、full 批次的数字自身满足结构性约束（纯 JSON 自洽，不连云）
    if not dup and not missing and not extra:
        for name, b in decl["batches"].items():
            if name == "seed":
                continue
            bad += check_groups(decl, b["rows"], f"{name}（声明值）")

    # 五、declarations：原文的出现次数对得上，值与它自称的那一批相等
    for d in decl["declarations"]:
        p = ROOT / d["file"]
        if not p.is_file():
            bad.append(f"declarations：找不到 {d['file']}")
            continue
        n = len(re.findall(re.escape(d["text"]), p.read_text(encoding="utf-8")))
        want_n = d.get("occurrences", 1)
        if n != want_n:
            why = ("这条声明已经不指向任何东西了，确认文档改对了就把它从 "
                   "docs/scale.json 摘掉" if n == 0 else
                   "同一份文件里抄了多份，很可能只改了其中一处、漏了另一处；"
                   "都改完就把 occurrences 更新过来")
            bad.append(f"{d['file']}：声明原文 {d['text']!r} 命中 {n} 次"
                       f"（登记 {want_n} 次）。{why}")
        if "value" not in d:
            continue
        batch = d["describes"]
        if batch not in decl["batches"]:
            bad.append(f"{d['file']}：describes={batch!r} 不是已登记的批次")
            continue
        b = decl["batches"][batch]
        want = b["lines_total"] if d.get("unit") == "lines" else b["rows_total"]
        if d["value"] != want:
            bad.append(f"{d['file']}：声明 {d['value']:,} 描述 {batch} 批次，"
                       f"该批实际是 {want:,}")

    for x in bad:
        print(f"  ❌ {x}")
    if bad:
        print(f"\n{len(bad)} 项不符 ❌")
        return 1
    print(f"  seed 声明 ⟷ data/csv 现数：{len(seed_rows)} 张表逐表相等")
    print(f"  批次登记 {len(decl['batches'])} 批，rows_total 与求和一致")
    print(f"  groups 恰好划分 {len(seed_rows)} 张表"
          f"（scaled {len(decl['groups']['scaled'])} / "
          f"sublinear {len(decl['groups']['sublinear'])} / "
          f"fixed {len(decl['groups']['fixed'])}）")
    print(f"  文档声明 {len(decl['declarations'])} 条"
          f"（共 {sum(d.get('occurrences', 1) for d in decl['declarations'])} 处原文）"
          f"，出现次数与数值都与所述批次相符")
    print("规模声明自洽 ✅")
    return 0


# ---------------------------------------------------------------- 连云

def lake_counts(tables: list[str]) -> dict[str, int]:
    import athena
    c = athena.Client()
    sql = "\nUNION ALL\n".join(
        f"SELECT '{t}' t, count(*) n FROM {t}" for t in tables)
    rows = c.execute(sql, timeout=600)["rows"]
    return {r[0]: int(r[1]) for r in rows}


def run(decl: dict, as_json: bool) -> int:
    seed_rows = decl["batches"]["seed"]["rows"]
    tables = sorted(seed_rows)
    got = lake_counts(tables)
    total = sum(got.values())

    # 逐表命中哪一批
    hits = {name: [t for t in tables if got.get(t) != b["rows"].get(t)]
            for name, b in decl["batches"].items()}
    matched = [n for n, diff in hits.items() if not diff]
    closest = min(hits, key=lambda n: len(hits[n]))

    bad: list[str] = []
    if not as_json:
        print(f"\n湖里实际：{len(got)} 张基表 {total:,} 行")
    if matched:
        if not as_json:
            for n in matched:
                b = decl["batches"][n]
                print(f"  ✅ 命中已登记批次 {n}（{b['desc']}）："
                      f"{b['rows_total']:,} 行，逐表相等")
    else:
        b = decl["batches"][closest]
        msg = (f"湖里的规模没有命中任何已登记批次。最接近的是 {closest}"
               f"（{b['rows_total']:,} 行），{len(hits[closest])} 张表不符")
        bad.append(msg)
        if not as_json:
            print(f"  ❌ {msg}")
            for t in hits[closest][:12]:
                print(f"     {t:<22} 声明 {b['rows'].get(t, 0):>12,}  "
                      f"实际 {got.get(t, 0):>12,}")
            if len(hits[closest]) > 12:
                print(f"     …… 还有 {len(hits[closest]) - 12} 张")
            print("     装了新一批数据？先在 docs/scale.json 的 batches 里登记它，"
                  "再回来跑这条。")

    scale = scale_of(decl, got)
    gbad = check_groups(decl, got, "湖实测")
    bad += gbad
    if not as_json:
        print(f"\n结构性约束（实测 scale = ×{scale:.3f}，基准表 "
              f"{decl['scale_ref_table']}）")
        if not gbad:
            print(f"  ✅ scaled {len(decl['groups']['scaled'])} 张等比"
                  f"（容差 ±{SCALED_TOL:.1%}）")
            print(f"  ✅ fixed {len(decl['groups']['fixed'])} 张不随 scale 变")
            print(f"  ✅ sublinear {len(decl['groups']['sublinear'])} 张落在两者之间")
        for x in gbad:
            print(f"  ❌ {x}")

    if as_json:
        print(json.dumps({"total": total, "counts": got, "scale": scale,
                          "matched": matched, "problems": bad},
                         ensure_ascii=False, indent=2))
        return 1 if bad else 0

    if bad:
        print(f"\n{len(bad)} 项不符 ❌")
        return 1
    print("\n规模与声明一致 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="docs/scale.json 声明的规模 ⟷ 湖里实际的规模")
    ap.add_argument("--selftest", action="store_true",
                    help="离线：声明自洽 + 与 data/csv 对账（无云依赖）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出实测结果")
    a = ap.parse_args()

    if not SCALE_JSON.is_file():
        print(f"找不到 {SCALE_JSON.relative_to(ROOT)} ❌")
        return 1
    decl = load_decl()
    return selftest(decl) if a.selftest else run(decl, a.json)


if __name__ == "__main__":
    raise SystemExit(main())
