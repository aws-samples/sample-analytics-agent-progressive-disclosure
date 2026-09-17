#!/usr/bin/env python3
"""DDL 行内注释里的枚举清单 ⟷ `knowledge/domains/**/<表>.md` 的「字段枚举值」。

## 为什么需要这一层

`knowledge/README.md` 明写「表结构以 `database/*.sql` 为准」。而那个「为准」的源里，
可比对的 34 行枚举注释有 23 行是错的（本文件写成当天实测）——错法还很硬：

    database/08_product_domain.sql:36   -- 'draft', 'active', 'inactive', 'deleted'
    knowledge/domains/product/products.md   on_sale / off_sale / pre_sale / sold_out

四个值一个都不对。照注释写 `WHERE status = 'active'` 得到 0 行，**不报错**，
结论是「一件在售商品都没有」。

这不是假想的风险，是已经发生过的事故的**成因**：`scripts/gen/tables.py` 当年就是
照这批注释写的枚举池，于是 22 组可比对的列里 13 组产出了卡片否认的值
（见 `scripts/gen/selftest_closures.py` 的 `check_enums_vs_cards`）。卡片后来被按
真实数据校准过（`PROJECT_STATUS.md` 记着 `verify_enums.py` 首跑抓出 46 处漂移，
改的是卡片），注释没跟上，而**没有任何一层比过注释**。这一层就是那个缺口。

注释还会往下游走：`gen_ddl.py` 把 `--` 后面的文字原样搬进
`database/iceberg/01_tables.sql` 的 `COMMENT '...'`，也就是进了 Glue 的列元数据。
所以错注释不只误导读源码的人。

## 与另外两个枚举检查的分工

    verify_enums.py            卡片 ⟷ 线上 Athena 库的实际取值。要云。
    selftest_closures.py       卡片 ⟷ 生成器产出的值。不要云，L0 的一环。
    本文件                     卡片 ⟷ database/0*.sql 的行内注释。不要云，L0 的一环。

三条边围成一个三角：卡片是那个被三方共同参照的中心。**卡片本身**由
`verify_enums.py` 对着真实数据守着，所以拿它当基准不是循环论证。

## 判据：注释里的引号取值集合 == 卡片表格的取值集合

两个方向都算错，理由和 `verify_enums.py` 那张表一样：注释多出的值让人写出静默空集，
注释漏掉的值让人写出静默漏行的 `IN (...)`。

### 业务上合法但本批数据没有的值怎么写

**去掉引号，写进括号里的散文。** 例如 `payments.status`：

    -- 'success', 'refunded'（业务上还有 pending / failed；本项目一单一支付、
    --  支付即终态，所以数据里没有这两态，支付成功率算不出来）

这不是绕过检查，是本项目已有的约定：`verify_enums.py` 的开头就写着卡片的同一条规则
——「某个值业务上存在但数据里没有时，正确做法是把它从表格里删掉、在下面的说明里用
文字讲清楚」，因为「表格是给 agent 抄的，散文是给人读的」。注释里的引号取值就是那张
表格：它是会被人和机器**直接抄进 SQL** 的部分，必须只装真实存在的值；schema 契约
层面的信息放散文里，一个字都不会丢。

好处是本文件**不需要豁免表**。豁免表要逐条维护、会腐烂、会被当成绕过检查的口子；
去引号这个动作把「合法但没有」这件事表达在了正确的层次上。

## 不可比对的情形（打印成 `·`，不算失败）

- 卡片没有这一列的枚举表（5 行：`channels.platform`、`ad_creatives.creative_format`、
  `ab_tests.primary_metric`、`ab_test_variants.variant_key`、`payments.payment_channel`）。
  **刻意不判死**：卡片该不该给这些列列枚举，是卡片那边的问题，由 `verify_enums.py`
  的覆盖面去管；在这里判死会把两个不同的问题搅成一个。
- 注释里引号取值少于 2 个。单个引号值多半是 `-- 默认 'active'` 这类说明，不是清单。

用法：

    python3 scripts/lakehouse/verify_ddl_comments.py           # 全部
    python3 scripts/lakehouse/verify_ddl_comments.py -t users -t products
    python3 scripts/lakehouse/verify_ddl_comments.py --selftest   # 判据自测

退出码非 0 表示有不一致（这是闸门，不是诊断工具——与 `verify_literals.py` 相反，
理由是这里的失败**可以靠改注释修完**，不依赖任何一次数据重灌）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import verify_enums as VE          # noqa: E402  只用 cards()/parse_card()，纯读 md，无云依赖

# 声明态真源。09_mart / 10_derived 不在内：那两份是派生层，列由 SELECT 决定，
# 枚举取值继承自基表，注释里重复一遍只会多一处要同步的地方。
DDL_GLOB = "database/0[1-8]_*.sql"

_TBL = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?(\w+)", re.I)
_COL = re.compile(r"^\s+(\w+)\s+[A-Za-z]")
_CMT = re.compile(r"--\s*(.+?)\s*$")
# 只认单引号包起来的取值。`--` 之前的 `DEFAULT 'active'` 不会进来（先切注释再找引号），
# 括号散文里的值按约定不加引号，也不会进来。
_QUOTED = re.compile(r"'([^']*)'")

MIN_VALUES = 2


# ---------------------------------------------------------------- 判据（纯函数）

def judge(comment_vals: list[str], card_vals: list[str] | None) -> tuple[str, str]:
    """返回 (verdict, 说明)。verdict ∈ PASS / FAIL / SKIP。

    比的是**集合**不是顺序：注释按业务生命周期排、卡片按实测行数降序排，两种排法都
    合理，要求顺序一致只会制造无意义的红灯。
    """
    if card_vals is None:
        return "SKIP", "卡片没有这一列的枚举表"
    extra = [v for v in comment_vals if v not in card_vals]
    miss = [v for v in card_vals if v not in comment_vals]
    if not extra and not miss:
        return "PASS", f"{len(comment_vals)} 个取值一致"
    notes = []
    if extra:
        notes.append(f"注释多出 {extra}（按它写 WHERE 是空集；若业务上合法请去掉引号"
                     f"、移进括号散文）")
    if miss:
        notes.append(f"注释漏掉 {miss}（数据里真有，照注释写 IN(...) 会静默漏行）")
    return "FAIL", "；".join(notes)


# ---------------------------------------------------------------- 扫描

def scan_ddl(root: Path) -> list[tuple[str, int, str, str, list[str]]]:
    """扫声明态 DDL，返回 [(文件名, 行号, 表, 列, 注释里的引号取值), ...]。

    只收引号取值 >= MIN_VALUES 的行。表名靠 `CREATE TABLE` 跟踪：这批 DDL 一个文件里
    多张表，不跟踪的话列会挂到错的表上，然后对着错的卡片比、结论全乱。
    """
    out = []
    for path in sorted(root.glob(DDL_GLOB)):
        table = None
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _TBL.match(line)
            if m:
                table = m.group(1)
                continue
            mc = _COL.match(line)
            if table is None or mc is None:
                continue
            cmt = _CMT.search(line)
            if cmt is None:
                continue
            vals = _QUOTED.findall(cmt.group(1))
            if len(vals) < MIN_VALUES:
                continue
            out.append((path.name, lineno, table, mc.group(1), vals))
    return out


MARK = {"PASS": "  ok  ", "FAIL": "  FAIL", "SKIP": "  ·   "}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="database/0*.sql 的枚举注释 ⟷ knowledge 卡片（纯读文件，无云依赖）")
    ap.add_argument("-t", "--table", action="append", default=[], help="只查这些表（可重复）")
    ap.add_argument("--selftest", action="store_true", help="判据自测")
    ap.add_argument("-v", "--verbose", action="store_true", help="连 PASS 一起打印")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    all_cards = VE.cards(None)
    rows = scan_ddl(ROOT)
    if a.table:
        rows = [r for r in rows if r[2] in a.table]

    n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    print(f"\n=== 枚举注释对账：{DDL_GLOB} 里 {len(rows)} 行带 ≥{MIN_VALUES} 个引号取值 ===")
    for fname, lineno, table, col, vals in rows:
        verdict, why = judge(vals, all_cards.get(table, {}).get(col))
        n[verdict] += 1
        if verdict == "PASS" and not a.verbose:
            continue
        print(f"{MARK[verdict]} {table}.{col:<24} {fname}:{lineno}")
        print(f"        {why}")
        if verdict == "FAIL":
            print(f"        注释 {vals}")
            print(f"        卡片 {all_cards.get(table, {}).get(col)}")

    print(f"\n  —— 一致 {n['PASS']} · 不一致 {n['FAIL']} · 不可比对 {n['SKIP']}")
    if n["FAIL"]:
        print(f"\n枚举注释对账：{n['FAIL']} 行不一致 ❌\n"
              "  卡片是基准（它由 verify_enums.py 对着真实数据守着）。改注释，别改卡片——\n"
              "  除非你先用 verify_enums.py 证明卡片错了。改完记得重跑\n"
              "  `python3 scripts/lakehouse/gen_ddl.py -o database/iceberg/01_tables.sql`，\n"
              "  注释会被搬进生成物，L0 的 --check 盯着它。")
        return 1
    print("\n枚举注释对账：全部一致 ✅")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判据 + 解析器自测。喂的是本项目真实出现过的值，不是手编的极端值。"""
    bad = 0

    def eq(got, want, msg):
        nonlocal bad
        if got != want:
            bad += 1
            print(f"  FAIL {msg}：期望 {want}，实得 {got}")
        else:
            print(f"  ok   {msg}")

    # ---- 判据。四种真实错法各一条
    eq(judge(["active", "inactive", "banned"],
             ["active", "inactive", "deleted", "suspended"])[0], "FAIL",
       "users.status：banned 不存在且漏两个 → FAIL")
    eq(judge(["draft", "active", "inactive", "deleted"],
             ["on_sale", "off_sale", "pre_sale", "sold_out"])[0], "FAIL",
       "products.status：四个值全错 → FAIL")
    eq(judge(["wechat_friend", "wechat_moment"],
             ["wechat_friend", "wechat_moments"])[0], "FAIL",
       "post_shares.share_channel：只差一个 s 也要判中（拼写错 = 空集）")
    eq(judge(["home_top", "home_middle"],
             ["home_top", "home_middle", "splash"])[0], "FAIL",
       "banners.position：只漏不多也判中（IN(...) 会静默漏行）")

    # ---- 顺序不同不算错：注释按生命周期排，卡片按实测行数降序排
    eq(judge(["draft", "published", "deleted"],
             ["published", "deleted", "draft"])[0], "PASS",
       "同一集合、不同顺序 → PASS（两种排法都合理，不制造无意义红灯）")

    # ---- 卡片没枚举表 → SKIP，不判死
    eq(judge(["a", "b"], None)[0], "SKIP", "卡片无枚举表 → SKIP")

    # ---- 解析器。这几条是真实会踩的坑
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "database"
        base.mkdir()
        (base / "01_x_domain.sql").write_text(
            "CREATE TABLE users (\n"
            # DEFAULT 的引号在 `--` 之前，不能被当成枚举取值
            "    status VARCHAR(20) DEFAULT 'active',  -- 'active', 'deleted'\n"
            # 括号散文里的值按约定不加引号，所以不会进清单
            "    kind VARCHAR(20),  -- 'a', 'b'（业务上还有 c，本批数据没有）\n"
            # 单个引号值不是清单
            "    note TEXT,  -- 默认 'none'\n"
            # 没有注释
            "    plain TEXT,\n"
            ");\n"
            "CREATE TABLE orders (\n"
            "    status VARCHAR(20),  -- 'paid', 'refunded'\n"
            ");\n", encoding="utf-8")
        got = {(t, c): v for _f, _l, t, c, v in scan_ddl(Path(d))}
    eq(got.get(("users", "status")), ["active", "deleted"],
       "DEFAULT 'active' 不被算进枚举清单（先切 `--` 再找引号）")
    eq(got.get(("users", "kind")), ["a", "b"],
       "括号散文里未加引号的 c 不进清单（这就是「合法但没有」的写法）")
    eq(("users", "note") in got, False, "单个引号值不当清单")
    eq(("users", "plain") in got, False, "无注释的列不进结果")
    eq(got.get(("orders", "status")), ["paid", "refunded"],
       "跟踪 CREATE TABLE：第二张表的列挂到 orders 而不是 users")
    eq(len(got), 3, "一共只收 3 行")

    # ---- 卡片解析器确实读到了东西（防「基准是空集所以全绿」）
    c = VE.cards(None)
    eq(len(c) >= 20, True, f"读到 {len(c)} 张卡片的枚举表（基准非空）")
    eq(c.get("products", {}).get("status") is not None, True,
       "products.status 的卡片枚举读得到（判据自测里引用了它）")

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
