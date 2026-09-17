#!/usr/bin/env python3
"""枚举取值对账：`knowledge/domains/**/<表>.md` 的「字段枚举值」⟷ Athena 里的实际取值。

## 为什么单独有这个检查

`reconcile.py` 比的是**表和列**（声明态 DDL ⟷ Glue 实际态 ⟷ 知识库卡片），它管不到
列里装的**值**。而 agent 写 SQL 时抄的正是卡片里的枚举表：

    -- 卡片写 registration_source ∈ app / web / mini_program / h5
    SELECT count(*) FROM users WHERE registration_source = 'app';   -- → 0

实际数据里这一列是 referral / organic / huawei_store / web / ad_campaign /
wechat_mini / app_store / google_play。**查询不报错，返回 0，结论是「APP 渠道没人注册」。**
这就是本项目反复踩的那类缺陷：不报错、数看着合理、结论是反的。首跑（2026-08-19）抓出
9 列这样的漂移，包括 `products.status`（卡片写 `active`，数据是 `on_sale`）——而且
`products.md` 里三条示例 SQL 正照着 `active` 写。

`eval/` 的金标覆盖不到这里：金标问的题恰好没按这些枚举筛过。所以补一层专门的检查。

## 判定规则（两个方向都是失败）

| 情形 | 判定 | 为什么 |
|---|---|---|
| 数据里有、卡片没写 | ❌ FAIL | agent 无从得知这个值，GROUP BY 出来还会多一行没人解释 |
| 卡片写了、数据里没有 | ❌ FAIL | 按它写 WHERE 就是空集——最典型的静默错答案 |

**没有「容忍」开关**：某个值业务上存在但数据里没有时，正确做法是把它从表格里删掉、
在下面的说明里用文字讲清楚（`payments.status` 就是这么写的：表里只留 success /
refunded，文字说明「一单一支付的设计下没有 pending / failed，所以算不了支付成功率」）。
表格是给 agent 抄的，散文是给人读的——只有表格进对账。

## 只看得懂这一种写法

    ### <列名> <随便写的中文标题>
    | 值 | 说明 | …… |
    |----|------|----|
    | active | 正常 | …… |

第一列取值、其余列随意。跳过的情形（都会打印成 `·`，不算失败）：

- `### 后面的词不是这张表的列`（例如「### 价格相关字段说明」）——按
  `information_schema.columns` 判，不猜
- **boolean 列**——写 true/false 是在描述类型，不是枚举
- 取值超过 50 种的列——那不是枚举，是维度（避免把 `city` 之类拖进来）

**数组列（`array(varchar)`，例如 `user_profiles.interests`）照样进对账**，走 UNNEST
展开元素比对，不跳过——见 `actual_values()` 里那两处口径差的说明。

用法：

    python3 scripts/lakehouse/verify_enums.py              # 全部卡片
    python3 scripts/lakehouse/verify_enums.py -t users -t products
    python3 scripts/lakehouse/verify_enums.py --selftest   # 解析器自测（无云依赖）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

KNOWLEDGE = ROOT / "knowledge" / "domains"
MAX_DISTINCT = 50          # 超过就当维度列，不当枚举


# ---------------------------------------------------------------- 卡片解析

def parse_card(text: str) -> dict[str, list[str]]:
    """从一份表卡片里抠出 {列名: [枚举值, ...]}。

    只认「`### <标识符>` 开头 + 紧随其后的 markdown 表格」这一种结构。表格行里
    `|----|` 这种分隔行要滤掉，否则会多出一个叫 `----` 的假枚举值。
    """
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"^###\s+(\S+)[^\n]*\n(.*?)(?=^#{2,3}\s|\Z)", text, re.S | re.M):
        col, body = m.group(1).strip("`"), m.group(2)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
            continue                                     # 中文小标题 / 非列名
        vals: list[str] = []
        seen_header = False
        for line in body.splitlines():
            if not line.lstrip().startswith("|"):
                if vals:
                    break                                # 表格结束，后面是散文
                continue
            cell = line.strip().strip("|").split("|")[0].strip().strip("`")
            if not seen_header:
                seen_header = True                       # 第一行必是表头，按位置跳过
                continue                                 # （不按内容认——枚举值可能就叫 value）
            if not cell or set(cell) <= {"-", ":"}:
                continue                                 # 分隔行
            vals.append(cell)
        if len(vals) >= 2:
            out[col] = vals
    return out


def cards(tables: list[str] | None) -> dict[str, dict[str, list[str]]]:
    """扫 `knowledge/domains/*/*.md`，文件名即表名。"""
    out: dict[str, dict[str, list[str]]] = {}
    for path in sorted(KNOWLEDGE.glob("*/*.md")):
        table = path.stem
        if table.startswith("_") or (tables and table not in tables):
            continue
        enums = parse_card(path.read_text(encoding="utf-8"))
        if enums:
            out[table] = enums
    return out


# ---------------------------------------------------------------- Athena 侧

def column_types(client, tables: list[str]) -> dict[tuple[str, str], str]:
    """(表, 列) → Athena 类型。一次问全，省掉 N 次云调用。"""
    quoted = ", ".join(f"'{t}'" for t in tables)
    rows = client.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema = current_schema AND table_name IN ({quoted})")["rows"]
    return {(t, c): ty for t, c, ty in rows}


def actual_values(client, table: str, cols: list[str],
                  types: dict[tuple[str, str], str]) -> dict[str, dict[str, int]]:
    """一张表一条 SQL：UNION ALL 把每列的取值分布并起来。

    NULL 单独记成 `<NULL>`：它不是枚举值，但「整列 NULL」这件事必须能看见
    （`payments.payment_channel` 就是这样，卡片里原本列了 10 个渠道值）。

    **数组列走 UNNEST，不走 CAST。** `CAST(array(varchar) AS VARCHAR)` 在 Trino 里
    直接报 `TYPE_MISMATCH`，所以这一列以前只要被声明就会把整个检查打挂——
    `user_profiles.interests` 2026-08-28 补上表格声明时就是这么红的。要留意两处口径差：

    · n 的含义变了。标量列的 n 是行数，数组列的 n 是**含该取值的行数之和**，
      加起来会超过表行数（interests 15 个标签 / 500 行 → 合计 1458）。
    · `<NULL>` 的含义变了。数组列的空集有两种写法（列本身 NULL、或长度 0 的数组），
      UNNEST 对两者都产出零行，所以单独补一条把它们并起来数——否则"这一列没人填"
      和"这一列全填了"在输出里长得一样。
    """
    parts = []
    for c in cols:
        if types[(table, c)].startswith("array("):
            parts.append(
                f"SELECT '{c}' AS col, COALESCE(CAST(t.v AS VARCHAR), '<NULL>') AS v, "
                f'COUNT(*) AS n FROM "{table}" CROSS JOIN UNNEST("{c}") AS t(v) GROUP BY 2')
            # HAVING 是必须的：无 GROUP BY 的聚合恒返回一行，n=0 时会在输出里
            # 伪造出一个「+0 NULL」，也会覆盖掉真有 '<NULL>' 这个元素值的情形。
            parts.append(
                f"SELECT '{c}' AS col, '<NULL>' AS v, COUNT(*) AS n "
                f'FROM "{table}" WHERE "{c}" IS NULL OR cardinality("{c}") = 0 '
                "HAVING COUNT(*) > 0")
        else:
            parts.append(
                f"SELECT '{c}' AS col, COALESCE(CAST(\"{c}\" AS VARCHAR), '<NULL>') AS v, "
                f'COUNT(*) AS n FROM "{table}" GROUP BY 2')
    out: dict[str, dict[str, int]] = {c: {} for c in cols}
    for col, val, n in client.execute("\nUNION ALL\n".join(parts))["rows"]:
        out[col][val] = int(n)
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="知识库枚举 ⟷ Athena 实际取值对账")
    ap.add_argument("-t", "--table", action="append", default=[],
                    help="只查这些表（可重复）；默认全部")
    ap.add_argument("--selftest", action="store_true", help="解析器自测（无云依赖）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    documented = cards(a.table or None)
    if not documented:
        print("没找到带「字段枚举值」的卡片")
        return 2

    import athena
    client = athena.Client()

    types = column_types(client, sorted(documented))
    problems: list[str] = []
    checked = skipped = 0

    for table in sorted(documented):
        cols = [c for c in documented[table] if (table, c) in types]
        for c in sorted(set(documented[table]) - set(cols)):
            skipped += 1
            print(f"  ·  {table}.{c:<22} 跳过（不是这张表的列）")
        cols = [c for c in cols if "bool" not in types[(table, c)]]
        if not cols:
            continue

        actual = actual_values(client, table, cols, types)
        for col in cols:
            got = actual[col]
            if len(got) > MAX_DISTINCT:
                skipped += 1
                print(f"  ·  {table}.{col:<22} 跳过（{len(got)} 种取值，是维度不是枚举）")
                continue
            doc = documented[table][col]
            in_data = {v for v in got if v != "<NULL>"}
            extra = sorted(in_data - set(doc))            # 数据有、卡片没写
            absent = sorted(set(doc) - in_data)           # 卡片写了、数据没有
            checked += 1
            if not extra and not absent:
                shown = ", ".join(f"{v}({got[v]})" for v in
                                  sorted(in_data, key=lambda v: -got[v])[:4])
                nul = f"  +{got['<NULL>']} NULL" if "<NULL>" in got else ""
                print(f"  ✅ {table}.{col:<22} {len(in_data)} 种取值{nul}  {shown}")
                continue
            print(f"  ❌ {table}.{col:<22} " +
                  " ".join(filter(None, [
                      f"数据有卡片没写：{', '.join(extra)}" if extra else "",
                      f"卡片写了数据没有：{', '.join(absent)}" if absent else ""])))
            if extra:
                problems.append(f"{table}.{col} 数据里有但卡片没写：{', '.join(extra)}")
            if absent:
                problems.append(f"{table}.{col} 卡片写了但数据里没有：{', '.join(absent)}"
                                + ("（整列 NULL）" if list(got) == ["<NULL>"] else ""))

    print()
    if problems:
        print(f"发现 {len(problems)} 处枚举漂移 ❌（卡片是 agent 抄 SQL 的依据）\n")
        for p in problems:
            print(f"  - {p}")
        print("\n改法：把卡片改成实际取值；业务上存在但数据里没有的值，从表格里删掉、"
              "在下面用文字说明。")
        return 1
    print(f"枚举一致 ✅  {checked} 列取值与卡片相符"
          + (f"，{skipped} 列跳过" if skipped else ""))
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    bad = 0

    # 枚举值本身可能就叫 `value`：表头必须按位置跳，不能按内容认
    if parse_card("### segment_type x\n| 值 | 说明 |\n|----|----|\n"
                  "| value | 价值分群 |\n| device | 设备分群 |\n"
                  ).get("segment_type") != ["value", "device"]:
        bad += 1
        print("  FAIL 叫 value 的枚举值被当成表头丢掉了")

    got = parse_card("""
# users

## 字段枚举值

### status 账号状态
| 值 | 说明 | 实测行数 |
|----|------|------|
| active | 正常 | 376 |
| suspended | 已封禁 | 26 |

> 封禁态的值是 `suspended`，不是 `banned`。

### user_level 用户等级
| 值 | 说明 | 条件 |
|----|------|------|
| 1 | 新用户 | 注册<30天 |
| 2 | 普通用户 | 默认 |

### 价格相关字段说明
| 字段 | 说明 |
|------|------|
| price | 当前售价 |
| cost | 采购成本 |

### interests 常见兴趣标签
```
fashion, electronics
```

## 索引
""")
    for want_col, want_vals in [("status", ["active", "suspended"]),
                                ("user_level", ["1", "2"])]:
        if got.get(want_col) != want_vals:
            bad += 1
            print(f"  FAIL {want_col} → {got.get(want_col)!r}，期望 {want_vals!r}")
    # 中文小标题不是列名，不能被当成枚举列
    if "价格相关字段说明" in got:
        bad += 1
        print("  FAIL 中文小标题被当成了列名")
    # 分隔行不能变成枚举值
    if any("---" in v for vs in got.values() for v in vs):
        bad += 1
        print(f"  FAIL 分隔行进了枚举值：{got}")
    # 只有代码块、没有表格的小节不产出列
    if "interests" in got:
        bad += 1
        print("  FAIL 无表格的小节不该产出枚举")
    if len(got) != 2:
        bad += 1
        print(f"  FAIL 期望解析出 2 列，实得 {sorted(got)}")

    # 单值表格不算枚举表（避免把一行的说明表误认）
    if parse_card("### status x\n| 值 | 说明 |\n|----|----|\n| active | 正常 |\n"):
        bad += 1
        print("  FAIL 单值表格不该产出枚举")

    bad += _no_fenced_enums()

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  解析器 7 项：列名识别、表头按位置跳、分隔行过滤、中文小标题、代码块小节、单值表格、列数")
    print("全部通过 ✅")
    return 0


# 上面那条「只有代码块、没有表格的小节不产出列」是**故意**的解析器行为：围栏块里
# 一行逗号分隔的值不是结构化声明，认它会把 SQL 示例、字段说明表全都误收进来。
#
# 但这个刻意行为有个代价，2026-08-28 才被发现：把枚举写成围栏块 = **写了等于没写**。
# 卡片上看得见、任何一层都比不到。实测当时有 6 列这样：
# `user_profiles.interests`（卡片 16 个英文标签 ⟷ 库里 15 个中文标签，**交集为空**）、
# `user_devices.device_brand`（8 个英文牌名 ⟷ 库里 11 个、6 个是中文）、
# `sessions.utm_source` / `utm_medium` / `utm_campaign`，外加 `user_profiles.occupation`
# 压根没声明。`utm_medium` 那条最狠：卡片正文明写「旧文档写的 cpc / social 都不存在」，
# 而生成器一直在产 cpc / social——两边都在仓库里，六个月没人看见。
#
# 所以解析器不改，改的是**不许再有隐形声明**：下面这条扫真实卡片树，凡是
# 「### 列名」小节里只有围栏块、没有表格，就判失败并给出改法。判据落在"形态"上，
# 不落在"值"上——值该由 verify_enums 主流程（卡片 ⟷ 线上库）和 selftest_closures
# （卡片 ⟷ 生成器产出）各自去比，前提是声明得先看得见。
def _no_fenced_enums() -> int:
    fenced = re.compile(r"^###\s+(\S+)[^\n]*\n+```[a-z]*\n(.*?)\n```", re.S | re.M)
    bad = 0
    for path in sorted(KNOWLEDGE.glob("*/*.md")):
        if path.stem.startswith("_"):
            continue
        for col, body in fenced.findall(path.read_text(encoding="utf-8")):
            col = col.strip("`")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
                continue                          # 中文小标题，不是列名
            if "SELECT" in body.upper() or "\n\n" in body:
                continue                          # SQL 示例 / JSON 样例，不是枚举清单
            vals = [x.strip() for x in body.replace("\n", " ").split(",") if x.strip()]
            if len(vals) < 3 or any(" " in v for v in vals):
                continue                          # 散文，不是逗号分隔的取值清单
            bad += 1
            print(f"  FAIL {path.relative_to(ROOT)} 的「### {col}」把枚举写在围栏代码块里"
                  f"（{len(vals)} 个值）——parse_card 不收围栏块，这是**隐形声明**。"
                  f"改成「| 值 | 说明 | 实测行数 |」表格。")
    if not bad:
        print("  卡片树里没有围栏块形态的枚举声明（隐形声明扫描）")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
