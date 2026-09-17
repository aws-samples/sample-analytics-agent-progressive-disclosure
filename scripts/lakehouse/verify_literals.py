#!/usr/bin/env python3
"""字面值真实性体检：现行 Athena/Iceberg 库里的文本列是不是占位符 / 词沙拉。

## 为什么单独有这个检查

L1–L5 查的全是**数量关系**——行数对不对、求和等不等、漏斗单不单调。它们一个字符串都
不看。所以一个库可以做到每层全绿，而明细行长这样：

    user_devices.device_id = 'D00000001_1'
    posts.content          = '一下参加方面.直接活动因为商品时候.发表会员音乐的是.'

凡是要展示明细的场景（演示截图、卡片示例、agent 答案里的样例行）一眼可见是假数据，
而现有任何一层都不会红。`verify_enums.py` 管的是**枚举列里的取值集合**，管不到自由
文本列长什么样；这一层补的正是那块。

## 与另外两处的关系

同一套判据在三个地方各有一份，能跑的只有两处：

    scripts/gen/selftest_closures.py   生成侧回归网。跑 scale=1 的内存表，L0 的一环。
                                       改生成器后立刻知道有没有新造占位符。
    本文件（默认）                     库侧体检。跑**已装载的现行库**，Trino 方言。
    本文件 --from-csv <目录>            生成侧全量体检。跑 scripts/gen/main.py 的 CSV
                                       产出，不碰云。2026-08-28 补的，理由见下一段。
    scripts/audit/L9_literals.sql      v2 Redshift 环境的同一套判据，跑不动，
                                       留作判据的规范说明书（见那份文件头部）。

生成侧证明「生成器产出的值是真实的」，库侧证明「进了库之后还是真实的」。中间那段路会
改字面值：类型截断、ETL 的 trim/replace、列级治理。两侧互补，不是冗余。改判据要三处同改。

`--from-csv` 与 `selftest_closures.py` 也不是冗余：那一环跑 scale=1 的内存表，为的是
迭代快；这一条跑**全量产出**（实测 79,934,303 行 / 19 张表 / 70 个文本列，157s、
峰值 456 MB），为的是把「小规模下看不见」的那一类问题捞出来——`user_coupons.coupon_code`
的 150 个券种、`posts` 42.7 万行的标题⟷正文同源，都要到目标规模才有结论。补它的直接
原因是：云上是 v1 数据、已决定不重灌，所以**生成器的字面值修复原本没有任何一条路验得到**。

判据在两条路之间是**共用同一份**的，不是各写一份：`merge_col_verdicts` /
`judge_placeholder` / `judge_salad` / `judge_posts` / `judge_device` 都在取数之上。
合成规则（谁盖谁、EMPTY 算不算过）本身是判据的一部分，复制两份就会出现「云上判 FAIL、
离线判 PASS」这种只由报告代码引起的分歧。两条路已知的口径差只有一处，写在 `HAN_DOT_RE`
上方（Trino 的 RE2J 认 `\\p{Han}`，Python 的 `re` 不认，离线用 CJK 区间近似）。

## 预期结论：现在跑必然是红的

当前库里的数据不是 `scripts/gen` 产的，是 v1（`scripts/generate_data.py` + Faker）产的
`data/csv/` 灌进去的，而项目已决定不重灌。所以本文件跑出来会有一串 FAIL，**那是对
现状的正确描述，不是本文件坏了**。正因如此它**不接进 `scripts/test_all.sh`**——默认
流程里挂一盏永远红的灯，等于把所有人训练成无视红灯。它是手动诊断工具：要一份「线上库
字面值现状」的结论时跑它。

## 判据

1. **序号占位符** = 「该列 ≥99% 的值共用同一个前缀」+「前缀后面是纯数字尾」。
   刻意**不**写成 `^user_\\d+$` 这类逐列正则：占位符的来源是
   `scripts/gen/fillers.by_type()` 的类型兜底（`f"{colname}_{i}"`），逐列正则只盖得住
   今天已知的几列，明天新加一个未声明的文本列照样静默变占位符。
   99% 这条线自动放过版本号——`os_version` 的取值劈出多种前缀，最大前缀够不着门槛。

2. **词沙拉** = 存在「中文字 + 紧跟 ASCII 句点」的片段，且整个值里没有「。」。
   Faker 的中文 lorem ipsum 按英文句子模板拼中文词，句末落 ASCII 句点。这条抓的是
   **标点**不是词汇——第 1 条完全放过词沙拉（没有共用前缀也没有数字尾），而它比
   `post_1` 更糟：一眼看不出是假的，会被当成真实用户内容读进去。
   「紧跟」不能省，否则误伤 `满50享2.0折`、`养生壶 1.5L`（点夹在数字之间）。

3. **同行列间一致性**。替占位符时最容易新造的缺陷是标题和正文各抽一次词池，于是出现
   标题「包裹已签收」配正文「正在为你打包」。自相矛盾比占位符更糟，理由同上。

## 列级治理会让两列查不到

`scripts/lakehouse/governance.py` 把 `users.email` / `users.phone` /
`user_profiles.birth_date` 从授权面里排除了，`user_messages` 整表未授权。**这是列级
排除，不是 v2 那种 `***@masked.invalid` 值脱敏**——列在 `information_schema` 里直接
不存在。所以以受限角色跑本文件时，那几列判 `SKIP(列级排除)`；要看它们必须用有权限的
身份（默认凭证链就是你自己）。这两种情形的区别本身值得记录，别混成一句「查不到」。

## 扫描范围：48 张表都扫，不只是 35 张基表

列清单从 `information_schema` 读，所以 `02_mart.sql` 建的 13 张派生表也在内（实测
39 张表有 varchar 列 / 144 个文本列）。**这是有意的**：agent 优先查的就是集市层和派生层，
它答案里贴出来的样例行来自那里。派生层的字面值是基表 SELECT 出来的副本，基表脏则副本脏，
但副本还多一种独立风险——`02_mart.sql` 是手写的，搬运时可能截断或改写字面值。

代价是同一个缺陷会在基表和它的副本上各报一次。业务单号那类豁免因此按**列名**给，
不按 `表.列`——理由写在 SERIAL_OK 上方。

用法：

    python3 scripts/lakehouse/verify_literals.py            # 全部 48 张表
    python3 scripts/lakehouse/verify_literals.py -t posts -t users
    python3 scripts/lakehouse/verify_literals.py --selftest # 判据自测（无云依赖）
    python3 scripts/lakehouse/verify_literals.py --coherence-only
    python3 scripts/lakehouse/verify_literals.py --from-csv /tmp/genFULL_csv

`--from-csv` 的实测基线（全量产出，2026-08-28）：**PASS 63 · FAIL 0 · 豁免 5 · 空列 2**，
一致性五条全 ok。改生成器或改判据后对着比。注意它的列清单真源是 DDL
（`scripts/gen/ddl.py::parse_all_raw`）而不是 `information_schema`——后者只给 varchar，
数组 / JSONB 列压根不出现，于是「没被查过」和「查过没问题」在报告上长得一模一样。
离线这条路把它们连同 TYPE_EXEMPT 一起印在「普查之外」那两行，缺口还在但可见。
"""
from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

# 序号形态的门槛。99 而不是 100：真实数据里总有个别行破例（手工修过的、导入时截断的），
# 一条破例就把整列判成「不是占位符」会漏掉几乎全是占位符的列。
PLACEHOLDER_SHARE = 0.99

# 业务单号天生是「前缀 + 流水号」，那是真实世界的正确写法，不是占位符。
#
# **按列名豁免，不按 `表.列`**。首跑（本文件写成当天）踩的就是这个：豁免表原本写
# `orders.order_no`，结果派生层的两个逐字副本 `dwd_orders_valid.order_no` 和
# `orders_backup_20251201.order_no` 各报一条 FAIL——同一列、同一批值、同一个 `DD`
# 前缀，只因为换了张表就判成缺陷。派生层是 `02_mart.sql` 从基表 SELECT 出来的，
# 这种副本以后还会有，逐张表往豁免表里补是治症状。
#
# 代价是精度：将来若有另一张表冒出个真占位符恰好叫 `order_no`，会被一起放过。
# 接受这个代价，因为这几个列名在本库里语义唯一（都是业务单号），而误报会让一份
# 本就全是红字的报告更难读——诊断工具的可读性就是它的全部价值。
#
# 与 scripts/gen/selftest_closures.py 的 SERIAL_OK 必须一致，selftest() 里逐项比
# （那边按 `表.列` 写，因为生成侧只有基表、不存在副本问题；比的时候取列名部分）。
SERIAL_OK = {
    "order_no":       "订单号：真实电商就是前缀+流水号",
    "payment_no":     "支付流水号，同上",
    "transaction_id": "第三方交易号，真实形态同为前缀+流水",
    "coupon_code":    "券码按券种发放（基数=券种数），不是逐行序号",
    # 手机号是纯数字，前缀恒为空，形态规则对它必然误判。判据没有丢，是**委派**给
    # judge_phone() 的号段正则（比"有没有数字尾"强得多）。
    "phone":          "纯数字列，真实性由号段正则负责",
}

# 不参与字面值检查的列：形态上就该长成这样，判它没有意义。
# 只列**类型性**的豁免，不列「这列现在是占位符但我不想管」——后者要么修要么进 SERIAL_OK。
TYPE_EXEMPT = {
    "ip_address",     # 点分十进制，天生带点带数字
    "app_version",    # 版本号
    "os_version",     # 同上（99% 那条线本来也够不着，这里显式跳过省一次判读）
    "page_url",       # URL
    "deep_link",      # 同上
    "media_urls", "tags", "product_ids", "interests", "properties",   # 数组/JSON
}

# Trino 的 regexp_like 走 RE2J，支持 \p{Han}。比写死 Unicode 码点范围清楚，
# 也比 `[^ -~]`（v2 那份 SQL 里的写法，为了不赌 Redshift 的 Unicode 支持）更准确。
HAN_DOT = r"\p{Han}\."


# ---------------------------------------------------------------- 判据（纯函数）
#
# 判读逻辑刻意与 SQL 分开：SQL 只做聚合，判 PASS/FAIL 全在 Python 里。
# 这样 --selftest 才测得到真东西——否则自测只能验「SQL 字符串拼对了没」，
# 而那正是最不容易错的部分。

def judge_placeholder(col: str, n: int, top_c: int, top_prefix: str) -> tuple[str, str]:
    """返回 (verdict, 说明)。col 形如 `users.username`；豁免按列名判，理由见 SERIAL_OK。"""
    if n == 0:
        return "EMPTY", "整列为空"
    if top_c * 1.0 < n * PLACEHOLDER_SHARE:
        return "PASS", ""
    why = SERIAL_OK.get(col.rsplit(".", 1)[-1])
    if why:
        return "EXEMPT", why
    return "FAIL", f"{top_c}/{n} 共用前缀 {top_prefix!r} + 数字尾"


def judge_salad(n: int, salad_c: int, example: str | None) -> tuple[str, str]:
    """词沙拉不设占比门槛，命中一条即 FAIL。

    因为命中率并不稳定：v1 的 `posts.content` 是 100%，但 `post_comments.content`
    只有 29%、`user_messages.content` 只有 20%（模板句和 Faker 句混在同一列）。
    任何 ≥50% 的门槛都会把后两列漏掉。而中文字后紧跟英文句点在真实中文里本就是标点
    错用，0 容忍不会冤枉谁——实测 38.7 万个真实值上零误报。
    """
    if n == 0:
        return "EMPTY", "整列为空"
    if salad_c == 0:
        return "PASS", ""
    return "FAIL", f"{salad_c}/{n} 中文字后紧跟 ASCII 句点，例 {(example or '')[:36]!r}"


def judge_email(n: int, bad_fmt: int, domains: int) -> tuple[str, str]:
    if bad_fmt:
        return "FAIL", f"{bad_fmt}/{n} 不符合 x@y.z"
    if domains < 5:
        return "FAIL", f"域名基数只有 {domains}（真实用户散在多家邮箱，<5 就是词池太小）"
    return "PASS", f"域名基数 {domains}"


def judge_phone(n: int, bad_fmt: int, segments: int) -> tuple[str, str]:
    if bad_fmt:
        return "FAIL", f"{bad_fmt}/{n} 不符合 ^1[3-9][0-9]{{9}}$"
    if segments < 10:
        return "FAIL", f"号段基数只有 {segments}（工信部在用号段有几十个，<10 是编的）"
    return "PASS", f"号段基数 {segments}"


def merge_col_verdicts(key: str, n: int, uniq: int,
                       v1: str, w1: str, v2: str, w2: str) -> tuple:
    """把「占位符」与「词沙拉」两条判读合成一行结果。

    抽出来是因为 Athena 路径和 `--from-csv` 路径都要用它：合并规则本身是判据的一部分
    （谁盖谁、EMPTY 怎么算），复制两份就会出现「云上判 FAIL、离线判 PASS」这种
    只由报告代码引起的分歧。
    """
    # 一列可能同时中两条。占位符优先报（更严重、更好修），词沙拉附在后面。
    if v1 == "FAIL" or v2 == "FAIL":
        notes = [f"占位符：{w1}"] if v1 == "FAIL" else []
        if v2 == "FAIL":
            notes.append(f"词沙拉：{w2}")
        return (key, "FAIL", "；".join(notes), n, uniq)
    if v1 == "EXEMPT":
        return (key, "EXEMPT", w1, n, uniq)
    if "EMPTY" in (v1, v2):
        return (key, "EMPTY", "整列为空", n, uniq)
    return (key, "PASS", w2, n, uniq)


def judge_posts(n: int, no_end: int, mismatch: int) -> tuple[str, str]:
    """posts 标题 ⟷ 正文同源。两条路共用。"""
    if mismatch == 0:
        return "PASS", f"{n} 行全部同源"
    return "FAIL", (f"{mismatch}/{n} 行正文首句不是标题前缀"
                    f"（其中 {no_end} 行正文里连「。」都没有）")


def judge_device(ios_brand: int, ios_os: int, and_os: int, cross: int) -> tuple[str, str]:
    """user_devices 品牌/机型/系统三列自洽。两条路共用。"""
    bad = {"ios 品牌非 Apple": ios_brand, "ios 系统非 iOS": ios_os,
           "android 系统非 Android": and_os, "同机型横跨多品牌": cross}
    hit = {k: v for k, v in bad.items() if v}
    if not hit:
        return "PASS", "品牌/机型/系统三列自洽"
    return "FAIL", "；".join(f"{k} {v}" for k, v in hit.items())


def judge_push_pairing(titles: int, pairs: int) -> tuple[str, str]:
    """标题与正文必须成对。

    光看 titles == pairs 不够：标题若是 `推送_1` 这种逐行唯一的占位符，每个标题天然
    只对应一条正文，等式恒成立、检查变成空转。所以先要求标题是**有限模板集**
    （真实 APP 的推送文案就是几十条模板），再看配对。
    """
    if titles > 50:
        return "FAIL", f"标题 {titles} 种，不是模板集——疑似逐行生成，配对检查在这种数据上无意义"
    if titles != pairs:
        return "FAIL", f"标题 {titles} 种但 (标题,正文) 组合 {pairs} 种——同一标题配了多种正文"
    return "PASS", f"{titles} 种模板，一一对应"


# ---------------------------------------------------------------- SQL

def sweep_sql(table: str, cols: list[str]) -> str:
    """一张表一条 SQL：把每个文本列的形态统计 UNION 起来。

    分母是**全部**非空非白值，不是只统计带数字尾的那些——否则「1% 的行恰好以数字结尾
    且前缀相同」会被算成 100%，变成假阳性。
    """
    parts = "\n    UNION ALL ".join(
        f"SELECT '{c}' AS col, CAST(\"{c}\" AS VARCHAR) AS val FROM \"{table}\""
        for c in cols)
    return f"""
WITH v AS (
    {parts}
),
nb AS (SELECT col, val FROM v WHERE val IS NOT NULL AND trim(val) <> ''),
tot AS (SELECT col, count(*) AS n, count(DISTINCT val) AS uniq FROM nb GROUP BY col),
tailed AS (
    SELECT col,
           regexp_replace(val, '[0-9]+$', '') AS prefix,
           regexp_extract(val, '[0-9]+$')     AS tail
    FROM nb WHERE regexp_like(val, '[0-9]+$')
),
byp AS (
    SELECT col, prefix, count(*) AS c, count(DISTINCT tail) AS tails,
           row_number() OVER (PARTITION BY col ORDER BY count(*) DESC, prefix) AS rk
    FROM tailed GROUP BY col, prefix
),
sal AS (
    SELECT col,
           count_if(regexp_like(val, '{HAN_DOT}') AND strpos(val, '。') = 0) AS sc,
           max(CASE WHEN regexp_like(val, '{HAN_DOT}') AND strpos(val, '。') = 0
                    THEN val END) AS sx
    FROM nb GROUP BY col
)
SELECT t.col, t.n, t.uniq,
       coalesce(p.prefix, ''), coalesce(p.c, 0), coalesce(p.tails, 0),
       coalesce(s.sc, 0), s.sx
FROM tot t
LEFT JOIN byp p ON p.col = t.col AND p.rk = 1
LEFT JOIN sal s ON s.col = t.col
ORDER BY 1
"""


EMAIL_SQL = """
SELECT count(*),
       count_if(NOT regexp_like(email, '^[^@\\s]+@[^@\\s]+\\.[A-Za-z]{2,}$')),
       count(DISTINCT split_part(email, '@', 2))
FROM users WHERE email IS NOT NULL
"""

PHONE_SQL = """
SELECT count(*),
       count_if(NOT regexp_like(phone, '^1[3-9][0-9]{9}$')),
       count(DISTINCT substr(phone, 1, 3))
FROM users WHERE phone IS NOT NULL
"""

# 拼接键的分隔符用 chr(2) 而不是 '|'：文案里真出现 '|' 会让两个不同的配对拼成同一个
# 键，pairs 偏小、检查漏判。
PUSH_SQL = """
SELECT push_type, count(*),
       count(DISTINCT title),
       count(DISTINCT title || chr(2) || content)
FROM push_notifications GROUP BY push_type ORDER BY 1
"""

# 生成侧的构造是 title = 核心 + 后缀、content = 核心 + '。' + 补充句，所以「正文切到
# 第一个句号」应当是标题的前缀。不成立就说明标题与正文各抽了一次，会出现「标题写加湿器、
# 正文写跑鞋」。旧数据（title = post_N）里没有共同核心，会整列 FAIL —— 预期如此。
POSTS_SQL = """
SELECT count(*),
       count_if(strpos(content, '。') = 0),
       count_if(strpos(content, '。') = 0
                OR strpos(title, split_part(content, '。', 1)) <> 1)
FROM posts
"""

# v1 的逐行生成器（scripts/generators/user_domain.py）是配对生成的，v2 向量化改写时
# 丢了这个配对，于是可能出现 device_brand=Apple、device_model=Redmi K70。
DEVICE_SQL = """
SELECT count_if(device_type = 'ios' AND device_brand <> 'Apple'),
       count_if(device_type = 'ios' AND os_version NOT LIKE 'iOS%'),
       count_if(device_type = 'android' AND os_version NOT LIKE 'Android%'),
       (SELECT count(*) FROM (SELECT device_model FROM user_devices
                              GROUP BY device_model
                              HAVING count(DISTINCT device_brand) > 1))
FROM user_devices
"""


# ---------------------------------------------------------------- 主流程

def text_columns(client, tables: list[str] | None) -> dict[str, list[str]]:
    """现行库里所有 varchar 列，按表分组。

    从 `information_schema` 读，不写死列清单——判据要能盖住明天新加的列，列清单也一样。
    列级治理排除掉的列在这里天然不出现（那是**列不存在**，不是值被脱敏），
    所以「查不到」和「查了没问题」不会混淆。
    """
    rows = client.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema "
        "AND (data_type = 'varchar' OR data_type LIKE 'varchar(%')")["rows"]
    out: dict[str, list[str]] = {}
    for t, c in rows:
        if tables and t not in tables:
            continue
        if c in TYPE_EXEMPT:
            continue
        out.setdefault(t, []).append(c)
    return {t: sorted(cs) for t, cs in sorted(out.items())}


def run_sweep(client, cols_by_table: dict[str, list[str]]) -> list[tuple]:
    """返回 [(col_key, verdict, note, n, uniq), ...]，已判读。"""
    found = []
    for table, cols in cols_by_table.items():
        try:
            rows = client.execute(sweep_sql(table, cols))["rows"]
        except Exception as exc:                       # 表未授权 / 列被排除
            found.append((f"{table}.*", "SKIP", f"查不到：{type(exc).__name__}", 0, 0))
            continue
        for col, n, uniq, prefix, top_c, _tails, sal_c, sal_x in rows:
            key = f"{table}.{col}"
            n, uniq, top_c, sal_c = int(n), int(uniq), int(top_c), int(sal_c)
            v1, w1 = judge_placeholder(key, n, top_c, prefix)
            v2, w2 = judge_salad(n, sal_c, sal_x)
            found.append(merge_col_verdicts(key, n, uniq, v1, w1, v2, w2))
    return found


def run_coherence(client) -> list[tuple]:
    """同行列间一致性 + 有既定格式的两列。返回 [(名称, verdict, 说明)]。"""
    out = []

    def guarded(name, fn):
        try:
            out.append((name, *fn()))
        except Exception as exc:
            out.append((name, "SKIP", f"查不到：{type(exc).__name__}"))

    def email():
        n, bad, dom = client.execute(EMAIL_SQL)["rows"][0]
        return judge_email(int(n), int(bad), int(dom))

    def phone():
        n, bad, seg = client.execute(PHONE_SQL)["rows"][0]
        return judge_phone(int(n), int(bad), int(seg))

    def push():
        worst, notes = "PASS", []
        for ptype, _n, titles, pairs in client.execute(PUSH_SQL)["rows"]:
            v, w = judge_push_pairing(int(titles), int(pairs))
            notes.append(f"{ptype}: {w}")
            if v == "FAIL":
                worst = "FAIL"
        return worst, "；".join(notes)

    def posts():
        n, no_end, mismatch = client.execute(POSTS_SQL)["rows"][0]
        return judge_posts(int(n), int(no_end), int(mismatch))

    def device():
        return judge_device(*[int(x) for x in client.execute(DEVICE_SQL)["rows"][0]])

    guarded("users.email 格式与域名多样性", email)
    guarded("users.phone 号段", phone)
    guarded("push 标题⟷正文成对", push)
    guarded("posts 正文首句 == 标题核心", posts)
    guarded("user_devices 品牌/机型/系统自洽", device)
    return out


# -------------------------------------------- 离线路径（生成器产出的 CSV）
#
# 为什么要这条路：本文件原先只能查 Athena，而 Athena 上是 v1 数据、项目已决定不重灌，
# 于是 D-04 的字面值修复在**新生成的全量数据上一次都没被 L9 验过**——任务书第一批就是
# 「D-04 + L9」，缺了这半边这一批不算闭环。五个 reporter 里这是最后一个补上 --from-csv 的。
#
# 分工与 verify_behavior.py 的 load_athena / load_csv 完全一致：**判读函数（judge_* 与
# merge_col_verdicts）是判据本身，两条路共用**。这里只把 SQL 里那几个聚合用 Python
# 重算成同形的输入，一行 PASS/FAIL 逻辑都不复制——否则修完生成器再改检查就是自证。

# Trino 的 regexp_like 走 RE2J、认 \p{Han}；Python 的 re 不认，这里用 CJK 基本区 +
# 扩展 A + 兼容表意文字近似。**覆盖面略窄于 RE2J**（缺 Ext-B 以上的罕用字），对「词沙拉」
# 这条判据没有实质影响：它判的是常用中文句子缺句号，用不到罕用字。差异写在这里，
# 是为了让两条路的结论不一致时能先想到这个原因，而不是去怀疑数据。
HAN_DOT_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]\.")
_TAIL_RE = re.compile(r"[0-9]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^1[3-9][0-9]{9}$")


def _csv_rows(src: Path, table: str):
    p = src / f"{table}.csv"
    if not p.exists():
        raise FileNotFoundError(2, "no such file", str(p))
    with p.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def text_columns_csv(src: Path,
                     tables: list[str] | None) -> tuple[dict[str, list[str]], dict]:
    """离线版 text_columns。类型真源取 DDL（`scripts/gen/ddl.py`），非 information_schema。

    第二个返回值是刻意加的。Athena 路径从 information_schema 只捞 varchar，
    **数组 / JSONB 列压根不出现在结果里**，于是报告上「没被查过」和「查过没问题」
    长得一模一样——这正是本轮发现的一个真实盲区（生成器把 6 个这类列写成了单一常量，
    L9 一声不响）。离线手上有完整 DDL，就把落在普查之外的列数出来、明写在报告里。
    """
    sys.path.insert(0, str(ROOT / "scripts" / "gen"))
    import ddl as _ddl
    raw = _ddl.parse_all_raw()
    have = {p.stem for p in src.glob("*.csv") if not p.stem.startswith("_")}
    out: dict[str, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    for t, cols in raw.items():
        if t not in have or (tables and t not in tables):
            continue
        for c, ty, _pg in cols:
            u = ty.upper()
            if u.endswith("[]") or u == "JSONB":
                skipped.setdefault("数组 / JSONB：形态判据对它无定义", []).append(f"{t}.{c}")
            elif u.startswith("VARCHAR") or u == "TEXT":
                if c in TYPE_EXEMPT:
                    skipped.setdefault("类型豁免（TYPE_EXEMPT）", []).append(f"{t}.{c}")
                else:
                    out.setdefault(t, []).append(c)
    return ({t: sorted(cs) for t, cs in sorted(out.items())},
            {k: sorted(v) for k, v in sorted(skipped.items())})


def run_sweep_csv(src: Path, cols_by_table: dict[str, list[str]]) -> list[tuple]:
    """与 run_sweep 同形：[(col_key, verdict, note, n, uniq), ...]。

    一张表读一遍、该表所有列一起累加（对应 SQL 里那次 UNION ALL），读完立刻判读并
    **释放该表的累加器**：`sessions.device_id` 这类高基数列的前缀直方图能到百万级，
    逐表释放让内存峰值只取决于最大的那张表，而不是全部表之和。
    """
    found = []
    for table, cols in cols_by_table.items():
        n = dict.fromkeys(cols, 0)
        sal_c = dict.fromkeys(cols, 0)
        uniq: dict[str, set] = {c: set() for c in cols}
        pref: dict[str, collections.Counter] = {c: collections.Counter() for c in cols}
        ptail: dict[str, dict[str, set]] = {c: {} for c in cols}
        sal_x: dict[str, str | None] = dict.fromkeys(cols, None)
        for r in _csv_rows(src, table):
            for c in cols:
                val = r.get(c)
                # 对应 SQL 的 `val IS NOT NULL AND trim(val) <> ''`。CSV 里 NULL 就是空串，
                # 所以两者在这一步等价。分母是**全部**非空非白值，不是只数带数字尾的那些。
                if not val or not val.strip():
                    continue
                n[c] += 1
                uniq[c].add(val)
                m = _TAIL_RE.search(val)
                if m:
                    p = val[:m.start()]
                    pref[c][p] += 1
                    ptail[c].setdefault(p, set()).add(m.group())
                if "。" not in val and HAN_DOT_RE.search(val):
                    sal_c[c] += 1
                    if sal_x[c] is None or val > sal_x[c]:
                        sal_x[c] = val      # 对应 SQL 的 max(...)：字典序最大
        for c in cols:
            key = f"{table}.{c}"
            # 对应 SQL 的 row_number() OVER (PARTITION BY col ORDER BY count(*) DESC, prefix)
            top_p, top_c = (min(pref[c].items(), key=lambda kv: (-kv[1], kv[0]))
                            if pref[c] else ("", 0))
            v1, w1 = judge_placeholder(key, n[c], top_c, top_p)
            v2, w2 = judge_salad(n[c], sal_c[c], sal_x[c])
            found.append(merge_col_verdicts(key, n[c], len(uniq[c]), v1, w1, v2, w2))
    return found


def run_coherence_csv(src: Path) -> list[tuple]:
    """与 run_coherence 同形：[(名称, verdict, 说明)]。

    离线这边比云上多查得到两列：`users.email` / `users.phone` 在 Athena 上被列级治理
    排除，那条路只能报 SKIP。这不是覆盖面差异带来的隐患，是**离线路径的额外价值**——
    生成侧的字面值质量本来就该在生成侧验，不该依赖一份被脱敏的库。
    """
    out = []

    def guarded(name, fn):
        try:
            out.append((name, *fn()))
        except FileNotFoundError as exc:
            out.append((name, "SKIP", f"产出里没有 {Path(exc.filename).name}"))

    def email():
        n = bad = 0
        dom: set[str] = set()
        for r in _csv_rows(src, "users"):
            v = r.get("email") or ""
            if not v:
                continue
            n += 1
            bad += not _EMAIL_RE.match(v)
            if "@" in v:
                dom.add(v.split("@", 1)[1])
        return judge_email(n, bad, len(dom))

    def phone():
        n = bad = 0
        seg: set[str] = set()
        for r in _csv_rows(src, "users"):
            v = r.get("phone") or ""
            if not v:
                continue
            n += 1
            bad += not _PHONE_RE.match(v)
            seg.add(v[:3])
        return judge_phone(n, bad, len(seg))

    def push():
        per: dict[str, tuple[set, set]] = {}
        for r in _csv_rows(src, "push_notifications"):
            titles, pairs = per.setdefault(r["push_type"], (set(), set()))
            t, c = r.get("title") or "", r.get("content") or ""
            titles.add(t)
            # 分隔符用 chr(2) 而不是 '|'，理由同 PUSH_SQL 的注释。
            pairs.add(t + "\x02" + c)
        worst, notes = "PASS", []
        for k in sorted(per):
            titles, pairs = per[k]
            v, w = judge_push_pairing(len(titles), len(pairs))
            notes.append(f"{k}: {w}")
            if v == "FAIL":
                worst = "FAIL"
        return worst, "；".join(notes)

    def posts():
        n = no_end = mismatch = 0
        for r in _csv_rows(src, "posts"):
            n += 1
            content, title = r.get("content") or "", r.get("title") or ""
            if "。" not in content:
                no_end += 1
                mismatch += 1
            elif not title.startswith(content.split("。", 1)[0]):
                mismatch += 1
        return judge_posts(n, no_end, mismatch)

    def device():
        ios_brand = ios_os = and_os = 0
        by_model: dict[str, set] = {}
        for r in _csv_rows(src, "user_devices"):
            typ = r.get("device_type") or ""
            brand = r.get("device_brand") or ""
            osv = r.get("os_version") or ""
            if typ == "ios":
                ios_brand += brand != "Apple"
                ios_os += not osv.startswith("iOS")
            elif typ == "android":
                and_os += not osv.startswith("Android")
            by_model.setdefault(r.get("device_model") or "", set()).add(brand)
        return judge_device(ios_brand, ios_os, and_os,
                            sum(1 for v in by_model.values() if len(v) > 1))

    guarded("users.email 格式与域名多样性", email)
    guarded("users.phone 号段", phone)
    guarded("push 标题⟷正文成对", push)
    guarded("posts 正文首句 == 标题核心", posts)
    guarded("user_devices 品牌/机型/系统自洽", device)
    return out


MARK = {"PASS": "  ok  ", "FAIL": "  FAIL", "EXEMPT": "  ex  ",
        "SKIP": "  ·   ", "EMPTY": "  ·   "}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="现行 Athena 库的字面值真实性体检（纯读；不进 test_all.sh）")
    ap.add_argument("-t", "--table", action="append", default=[],
                    help="只查这些表（可重复）；默认全部 48 张（含派生层，理由见 docstring）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（无云依赖）")
    ap.add_argument("--coherence-only", action="store_true",
                    help="只跑同行一致性与格式检查，跳过全列形态普查（快）")
    ap.add_argument("--from-csv", type=Path, metavar="DIR",
                    help="改查 scripts/gen/main.py 的 CSV 产出（离线，不碰云）。"
                         "云上是 v1 数据、已决定不重灌，所以生成器的字面值修复"
                         "只有这条路验得到")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    offline = a.from_csv is not None
    skipped: dict[str, list[str]] = {}
    if offline:
        src = a.from_csv.resolve()
        cols, skipped = text_columns_csv(src, a.table or None)
        sweep = (lambda: run_sweep_csv(src, cols))
        coherence = (lambda: run_coherence_csv(src))
        head_tail = f"（离线：{src}）"
    else:
        import athena
        client = athena.Client()
        cols = text_columns(client, a.table or None)
        sweep = (lambda: run_sweep(client, cols))
        coherence = (lambda: run_coherence(client))
        head_tail = f"（类型豁免 {len(TYPE_EXEMPT)} 类列名不计）"

    fails = 0
    if not a.coherence_only:
        total = sum(len(v) for v in cols.values())
        print(f"\n=== 形态普查：{len(cols)} 张表 / {total} 个文本列 {head_tail}===")
        for kind, names in skipped.items():
            print(f"  ·    普查之外（{len(names)} 列）· {kind}：{', '.join(names)}")
        rows = sweep()
        for key, verdict, note, n, uniq in rows:
            if verdict == "PASS":
                continue                       # 只打印非 PASS，全绿时下面给汇总
            print(f"{MARK[verdict]} {key:<34} n={n:<7} 基数={uniq:<7} {note}")
            fails += verdict == "FAIL"
        n_pass = sum(1 for r in rows if r[1] == "PASS")
        print(f"  —— PASS {n_pass} · FAIL {fails} · "
              f"豁免 {sum(1 for r in rows if r[1] == 'EXEMPT')} · "
              f"空列/跳过 {sum(1 for r in rows if r[1] in ('EMPTY', 'SKIP'))}")

    print("\n=== 同行列间一致性 + 既定格式 ===")
    for name, verdict, note in coherence():
        print(f"{MARK[verdict]} {name:<34} {note}")
        fails += verdict == "FAIL"

    print(f"\n{'字面值体检：全部通过 ✅' if not fails else f'字面值体检：{fails} 项 FAIL'}")
    if fails and not offline:
        print("  当前库的数据来自 v1（data/csv 里 Faker 产的），项目已决定不重灌，"
              "所以这些 FAIL 是对现状的记录。\n"
              "  生成器侧的同一套判据在 scripts/gen/selftest_closures.py 里守着，L0 会跑。")
    elif fails:
        print("  这是**生成器产出**上的 FAIL，不是历史包袱：改 scripts/gen/ 直到它绿。"
              "\n  同一批判据在 scripts/gen/selftest_closures.py 里也守着，L0 会跑；"
              "两处结论不一致说明有一边写错了。")
    return 0                    # 刻意不用退出码报错：这是诊断工具，不是闸门


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判读逻辑自测。无云依赖。

    喂的是**真实观测过的数值**（当前库实测、以及修复后生成器的实测），不是手编的
    极端值——自测要能证明这套判读在真数据上给出的结论是对的。
    """
    bad = 0

    def eq(got, want, msg):
        nonlocal bad
        if got != want:
            bad += 1
            print(f"  FAIL {msg}：期望 {want}，实得 {got}")
        else:
            print(f"  ok   {msg}")

    # ---- 占位符形态
    # 当前库实测：user_devices.device_id = 'D00000001_1'，744 行全部共用前缀
    eq(judge_placeholder("user_devices.device_id", 744, 744, "D00000001_")[0],
       "FAIL", "744/744 共用前缀 → 占位符")
    # 修复后：opaque_id 出的 16 位 hex，没有数字尾的行占多数
    eq(judge_placeholder("user_devices.device_id", 744, 41, "9e1c4a07b3d2f8")[0],
       "PASS", "hex 形态 → 放过")
    # 业务单号：100% 共用前缀但豁免
    eq(judge_placeholder("orders.order_no", 2000, 2000, "NO")[0],
       "EXEMPT", "orders.order_no 100% 共用前缀但豁免")
    # 边界：恰好 99% 要判中（>= 而不是 >）
    eq(judge_placeholder("x.y", 1000, 990, "p")[0], "FAIL", "恰好 99% 判中")
    eq(judge_placeholder("x.y", 1000, 989, "p")[0], "PASS", "98.9% 放过")
    eq(judge_placeholder("x.y", 0, 0, "")[0], "EMPTY", "空列判 EMPTY 不判 PASS")

    # ---- 词沙拉。三个命中率都是当前库实测值，覆盖「高中低」
    eq(judge_salad(1000, 1000, "一下参加方面.直接活动因为商品时候.")[0],
       "FAIL", "posts.content 100% → FAIL")
    eq(judge_salad(14000, 4085, "名称价格中文.")[0],
       "FAIL", "post_comments.content 29% → FAIL（占比门槛会漏掉它）")
    eq(judge_salad(9253, 1811, "电影很多以后公司业务系统城市.")[0],
       "FAIL", "user_messages.content 20% → FAIL")
    eq(judge_salad(150, 0, None)[0], "PASS", "coupons.coupon_name 0 命中 → PASS")

    # ---- 邮箱 / 手机号。数值取自当前库实测（域名 3 个）与修复后生成器（10 个）
    eq(judge_email(500, 0, 3)[0], "FAIL", "邮箱域名只有 3 个 → FAIL")
    eq(judge_email(500, 0, 10)[0], "PASS", "邮箱域名 10 个 → PASS")
    eq(judge_email(500, 7, 10)[0], "FAIL", "格式不合规优先判 FAIL")
    eq(judge_phone(500, 0, 40)[0], "PASS", "号段 40 个 → PASS")
    eq(judge_phone(500, 0, 1)[0], "FAIL", "号段 1 个 → FAIL")

    # ---- push 配对。第一条是本项目真踩过的坑：标题逐行唯一时等式恒成立，
    #      检查会空转报 PASS。当前库实测 title 基数 9988/10000。
    eq(judge_push_pairing(9988, 9988)[0], "FAIL",
       "标题 9988 种 → 判非模板集，不被 titles==pairs 的恒等式骗过")
    eq(judge_push_pairing(21, 21)[0], "PASS", "21 种模板一一对应 → PASS")
    eq(judge_push_pairing(21, 37)[0], "FAIL", "同一标题配多种正文 → FAIL")

    # ---- 派生层副本必须跟着基表一起豁免。首跑就是在这里报了两条假 FAIL：
    #      dwd_orders_valid / orders_backup_20251201 里的 order_no 是 orders 的逐字副本。
    for t in ("orders", "dwd_orders_valid", "orders_backup_20251201"):
        eq(judge_placeholder(f"{t}.order_no", 1601, 1601, "DD")[0], "EXEMPT",
           f"{t}.order_no 豁免（派生层副本不该单独报红）")
    # 反面：豁免是按列名给的，不是「凡是共用前缀就放过」
    eq(judge_placeholder("campaigns.owner", 50, 50, "admin_")[0], "FAIL",
       "campaigns.owner 不在豁免名单 → 仍判 FAIL")

    # ---- 豁免表必须与生成侧一致，否则两处结论会打架
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "gen"))
        import selftest_closures as sc
        # 生成侧按 `表.列` 写（那边只有基表，没有副本问题），比较时取列名部分
        eq(set(SERIAL_OK), {k.rsplit(".", 1)[-1] for k in sc.SERIAL_OK},
           "豁免表与 selftest_closures.SERIAL_OK 一致（按列名比）")
    except ImportError as exc:
        bad += 1
        print(f"  FAIL 读不到生成侧豁免表做比对：{exc}")

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
