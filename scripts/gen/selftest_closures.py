#!/usr/bin/env python3
"""跨表闭环自测：scale=1 全内存生成，逐条断言审计抓过的缺陷已修。

## 与 selftest_fillers 的分工

selftest_fillers 测**单个填充函数**的分布形状；本文件测**表与表之间的闭环**——
数据审计（docs/data-audit.md）证明单表全对、跨表全错正是旧版的失败模式，
所以这层自测断言的全部是跨表性质：

    头=明细和（精确到分）、item_count=真实行数、三计数器=bincount、
    等级规则成立、每个付钱订单一条支付、券核销三元对齐、
    漏斗单调递减、留存单调衰减、先注册后行为。

**一处例外**：文末的 L9 字面值真实性是**单表**性质的（列内形态 + 同行列间一致性），
按上面的分工本该进 selftest_fillers。放这里的理由是它必须看**已生成的表**：占位符是不是
占位符，取决于整列凑在一起的形态（共用前缀 + 数字尾）；标题正文有没有互相打脸，取决于
同一行的两列并排看。而 selftest_fillers 只拿得到填充函数的返回值，看不到某张表的
某一列、某一行最终长什么样。

## 两侧：正例断言 + 反例注入

    python3 scripts/gen/selftest_closures.py             # 正例：106 条断言全绿，约 1 秒
    python3 scripts/gen/selftest_closures.py --negative  # 反例：42 个缺陷注入全红，约 7 秒

反例一侧存在的理由是：正例全绿**同时**判据写歪了（比错列、把违例算成 0、条件恒真）
在这个文件上长得一模一样，而它跑在最前面、绿得最快，后面每一层的"通过"都会被读成
"数据是对的"。所以每个反例往一份深拷贝里注入一个缺陷，要求整套自测变红，**并且红在
指定的那条断言上**——只要求"有断言红了"不够，注入的缺陷可能先撞到无关的断言。
它盖不到什么，以及为什么，写在 `NEG_CASES` 上方。

在 test-plan 里排 L0 之后（改生成器后先跑 fillers 再跑这个），全过再谈重灌。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "lakehouse"))

import budget                              # noqa: E402
import profiles as PROF                    # noqa: E402
import semantics as SEM                    # noqa: E402
import tables as T                         # noqa: E402
from main import load_dim_ids              # noqa: E402
# 只用它的 cards() / parse_card()——纯读 knowledge/*.md，无云依赖。共用一份解析器，
# 不另写一份：两份解析器会各自漂，然后两边的结论打架却都自称通过。
import verify_enums as VE                  # noqa: E402

PASS = 0


def ok(cond, msg: str) -> None:
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ok  {msg}")


def cents(a) -> np.ndarray:
    return np.round(np.asarray(a, dtype=float) * 100).astype(np.int64)


# ---------------------------------------------------------------- L9 字面值真实性
#
# 判据刻意**不**写成 `^user_\d+$` / `^model-\d+$` 这类逐列正则。原因在
# fillers.by_type() 的最后一行：spec 没声明的 TEXT 列一律兜底成 f"{colname}_{i}"
# —— 占位符生成器**就是**类型兜底本身。逐列正则只盖得住今天已知的那几列，明天
# 新加一个未声明的文本列照样静默变成占位符，断言不会响。所以这里判通用形态：
#
#     序号占位符 = 「全列 ≥99% 的值共用同一个前缀」+「该前缀后面是纯数字尾」
#
# 99% 这条线同时把版本号排除在外：os_version 的 5 个取值劈出 4 种前缀
# （"15." / "16." / "17." / ""），最大前缀只占 30%，够不着门槛，不必进豁免表。
# 反过来，一个 40 项的词池若全长成「iPhone 13/14/15…」，前缀仍是 100%，会被判中
# —— 这是想要的：那种池子换了词也还是退化的。
#
# 检出后对照下面这张显式豁免表。业务单号天生就是「前缀 + 流水号」，那是真实世界
# 的正确写法，不是占位符。
SERIAL_OK = {
    "orders.order_no":          "订单号：真实电商就是前缀+流水号",
    "payments.payment_no":      "支付流水号，同上",
    "payments.transaction_id":  "第三方交易号，真实形态同为前缀+流水",
    "user_coupons.coupon_code":  "券码按券种发放（基数=券种数），不是逐行序号",
    # 手机号是纯数字，前缀恒为空，形态规则对它必然误判。判据没有丢，是**委派**给了
    # 下面 CN_MOBILE_RE 那条更严的格式断言（要求真实号段，比"有没有数字尾"强得多）。
    # 空前缀这一支仍然保留：将来若有别的纯数字文本列冒出来，它照样会被拦下。
    "users.phone":               "纯数字列，真实性由 CN_MOBILE_RE 格式断言负责",
}

# 有既定格式的列。手机号按工信部号段：1 + [3-9] + 9 位。
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

# ---------------------------------------------------------------- 词沙拉
#
# 上面那条序号判据有个盲区，而且盲区里正好躺着当前 data/csv 的主缺陷：Faker 的
# 中文 lorem ipsum。`一下参加方面.直接活动因为商品时候.` 这种值既没有共用前缀、
# 也没有数字尾，序号判据完全放过它——可它比 `post_1` 更糟，因为一眼看不出是假的，
# 会被当成真实用户内容读进去。
#
# 指纹是**标点**，不是词汇：Faker 按英文句子模板组装中文词，句末落的是 ASCII 句点，
# 全值没有一个「。」。真人写中文不会这样。所以判：
#
#     词沙拉 = 存在「中文字 + 紧跟 ASCII 句点」的片段，且整个值里没有「。」
#
# 「紧跟」这一条是精度的来源，不能省。只查「含中文且含点」会误伤 `满50享2.0折`、
# `养生壶 1.5L`——那些点夹在数字之间。加上「前一个字符是中文」后，实测
# 38.7 万个真实值上零误报（含 coupons.coupon_name / app_version / ip_address /
# page_url 这些天生带点的列）。
#
# 不设占比阈值，命中一条就算破。理由是命中率并不稳定：v1 的 posts.content 是
# 100%，但 post_comments.content 只有 29%、user_messages.content 只有 20%
# （模板句和 Faker 句混在一列里）。任何 ≥50% 的门槛都会把后两列漏掉。
# 而中文字后面紧跟英文句点在真实中文里本就是标点错用，0 容忍不会冤枉谁。
WORD_SALAD_RE = re.compile(r"[一-鿿]\.")

_TAIL = re.compile(r"^(.*?)(\d+)$", re.S)


def serial_shape(vals: list[str]) -> tuple[float, str, int]:
    """判「共用前缀 + 数字尾」形态。

    返回 (最大前缀占比, 该前缀, 数字尾去重数)。分母是**全部**非空字符串值，不是
    只统计带数字尾的那些——否则「1% 的行恰好以数字结尾且前缀相同」会假阳性。
    """
    pref: dict[str, int] = {}
    tails: set[str] = set()
    for v in vals:
        m = _TAIL.match(v)
        if m is None:
            continue                      # 无数字尾：不参与前缀统计，只进分母
        pref[m.group(1)] = pref.get(m.group(1), 0) + 1
        tails.add(m.group(2))
    if not pref:
        return 0.0, "", 0
    p, c = max(pref.items(), key=lambda kv: kv[1])
    return c / len(vals), p, len(tails)


def text_cols(t: dict) -> list[tuple[str, list[str]]]:
    """列出所有文本列（含字符串的列），按 表.列 命名。非字符串值一律丢弃：
    properties/interests/media_urls 这些是 dict/list，不属字面值检查范围。"""
    out = []
    for tbl in sorted(t):
        for col, arr in t[tbl].items():
            vals = [x for x in np.asarray(arr, dtype=object).tolist()
                    if isinstance(x, str) and x != ""]
            if vals:
                out.append((f"{tbl}.{col}", vals))
    return out


# ---------------------------------------------------------------- 枚举 ⟷ 知识卡片
#
# 这条断言以前不存在，而它盖住的缺陷在整套测试里是**隐形**的。
#
# 全项目唯一比过枚举的是 scripts/lakehouse/verify_enums.py，它比的是「卡片 ⟷ 线上库」。
# **生成器自己产什么值，在这条断言之前没有任何一层看过**——它当年是照 database/*.sql
# 的行内注释写的，而那批注释后来被卡片按真实数据校准过（卡片里逐条写着「旧文档写的 X
# 不存在」），生成器没跟上。实测：22 组可比对的枚举列里 13 组产出了卡片否认的值。
#
# 判的方向是**单向**的：生成器产出 ⊆ 卡片声明。
#
#   · 产出了卡片没声明的值 → 硬 FAIL。agent 读的是卡片；卡片说 users.status 只有
#     active/inactive/deleted/suspended，数据里却躺着 banned，那么照卡片写的
#     `WHERE status IN (...)` 会**静默漏行**——不报错的错，最难发现的那种。
#   · 卡片有、生成器没产出 → 只打印，不判死。scale=1 只有几百行，稀有取值抽不到是
#     正常的；判死会造出一条随规模闪烁的断言，那种断言迟早被人加豁免绕过去。
#
# 2026-08-28 补两处盲区，起因是这里原先写着「线上库装的是 data/csv，取值本来就和卡片
# 一致，所以 verify_enums 那条恒绿」——**那句话是错的，而且错得正是本项目命名过的假绿灯**。
# 实测线上库（500 行 v1 样本）与当时的卡片有三列对不上：`user_profiles.interests`
# 卡片写 16 个英文标签、库里是 15 个中文标签，**交集为空**；`user_devices.device_brand`
# 卡片写 8 个英文牌名、库里 11 个取值只有 3 个对得上（`华为`/`小米`/`三星`/`一加`/`荣耀`
# 以中文入库）；`user_profiles.occupation` 卡片**整个没声明**。verify_enums 之所以绿，
# 不是因为两端一致，是因为**这三列它根本没在比**：
#
#   ① 卡片把它们写在 ``` 围栏代码块里（「### 常见 device_brand」+ 一行逗号分隔），
#      而 VE.parse_card() 只认「### 列名」+「| 值 | 说明 |」表格。围栏块 = 隐形声明。
#      → 已把三列改写成表格形态（声明列数 46 → 49）。判据不动，只是终于看得见了。
#   ② `interests` 是 array<string>，元素是 list 而不是 str，原来那句
#      `isinstance(x, str)` 会把整列过滤成空集，然后 `if not vals: continue` 静默跳过。
#      于是「声明了」和「比过了」是两件事，而 n_cmp 只报后者、读起来像前者。
#      → 数组列现在摊平后比对；被跳过的声明列一律打印出来，不再有静默的 continue。

# 判据的**唯一**豁免口：生成器刻意产出比卡片更宽的取值集合。
#
# 这跟"生成器产了卡片否认的值"是两件不同的事，别混：
#   · utm_medium 产 `cpc` / `social` —— 卡片明文写着这些**不存在**。那是缺陷，已改生成器。
#   · utm_campaign 产 39 个真实活动名（`618_main_app` / `d11_yushou_kol` …）—— 卡片写的
#     是线上库那 6 个（`618` / `new_user` / …）。这里**生成器是对的一侧**：任务书 D-04
#     要的就是替掉占位符，而 `GROUP BY utm_campaign` 只出 6 行本身就是 v1 那批
#     低基数占位符的残留（tables.py 顶部注释记着「出 50 行照样露馅」）。
#
# 所以这一条不是"放过"，是**记账**：卡片描述的是**当前湖里的 500 行样本**，生成器描述的是
# 重灌之后的样子。两边此刻本就该不一致，而这个不一致的消除条件是**全量重灌 + 重生卡片**，
# 不是把生成器改回去。豁免带一条护栏（见下），免得它变成"以后什么都能塞进来"的口子。
ENUM_SUPERSET_OK = {
    "sessions.utm_campaign":
        "D-04：生成器产真实活动名，卡片写的是当前 500 行样本里的 6 个 v1 遗留值。"
        "消除条件是全量重灌后重生卡片，不是缩回生成器。",
}


def _enum_values(arr) -> set[str]:
    """列里的字符串取值集合。**数组列摊平一层**——`interests` 是 array<string>，
    元素是 list，不摊平的话整列被过滤成空集，然后这一列被静默跳过（见上方盲区 ②）。
    只摊一层：本仓库没有嵌套数组，摊到底反而会把将来某个 struct 列拆成一堆碎串。
    """
    out: set[str] = set()
    for x in np.asarray(arr, dtype=object).tolist():
        items = x if isinstance(x, (list, tuple)) else [x]
        out.update(v for v in items if isinstance(v, str) and v != "")
    return out


def check_enums_vs_cards(t: dict) -> None:
    print("\n枚举取值 ⟷ knowledge 卡片（单向判据：产出 ⊆ 声明，理由见上方注释）")
    declared_by = VE.cards(None)
    extra_hits: list[str] = []
    absent: list[str] = []          # 卡片声明了，但这张表/这一列不在本次产出里
    empty: list[str] = []           # 列在，但一个字符串取值都没有
    wider: list[str] = []           # 命中 ENUM_SUPERSET_OK 的刻意超集
    n_cmp = 0
    for tbl in sorted(declared_by):
        for col, declared in sorted(declared_by[tbl].items()):
            if tbl not in t or col not in t[tbl]:
                absent.append(f"{tbl}.{col}")
                continue
            vals = _enum_values(t[tbl][col])
            if not vals:
                empty.append(f"{tbl}.{col}")
                continue
            n_cmp += 1
            key = f"{tbl}.{col}"
            extra, missing = vals - set(declared), set(declared) - vals
            if extra and key in ENUM_SUPERSET_OK:
                # 护栏：豁免的理由是"生成器产得**更宽**"，那就必须真的更宽。有人把池子
                # 缩回三个占位符时基数会掉到声明之下，豁免随即失效、这条重新变红——
                # 否则这个口子就成了"写进字典就永久免检"。
                wider.append(f"{key}（产 {len(vals)} ⊃ 声明 {len(declared)}）")
                print(f"    {key:<26} 产出 {len(vals)} 个值 ⊋ 卡片 {len(declared)} 个"
                      f"  ← 豁免：{ENUM_SUPERSET_OK[key]}")
                if len(vals) <= len(declared):
                    extra_hits.append(f"{key} 豁免失效：产 {len(vals)} ≤ 声明 "
                                      f"{len(declared)}，不是更宽而是不同")
                continue
            if extra:
                extra_hits.append(f"{key}={sorted(extra)}")
                print(f"    {key:<26} 产出卡片没声明的值 {sorted(extra)}")
            if missing:
                print(f"    {key:<26} 卡片有、本次没抽到 {sorted(missing)}（不判死）")

    # 「声明了」不等于「比过了」。这两行以前是两个静默的 continue，于是 n_cmp 报的是
    # 比对数、读起来却像覆盖数——盲区 ② 就是这么藏了很久。不判死（维度表来自现成 CSV、
    # 不由 BUILDERS 产出，缺列是正常的），但必须**印出来**。
    n_declared = sum(len(v) for v in declared_by.values())
    print(f"    声明 {n_declared} 列 = 比对 {n_cmp} + 未产出 {len(absent)} + 空列 {len(empty)}")
    if absent:
        print(f"      未产出（多为维度表，不由 BUILDERS 生成）：{absent}")
    if empty:
        print(f"      在表里但无字符串取值：{empty}")
    ok(n_cmp + len(absent) + len(empty) == n_declared,
       f"每个声明列的归属都有交代（比对/未产出/空列三类，共 {n_declared} 列）")
    ok(not extra_hits,
       f"生成器枚举产出全部在卡片声明内（比对 {n_cmp} 组列，"
       f"刻意超集豁免 {len(ENUM_SUPERSET_OK)} 列：{wider or '未命中'}）"
       + (f"；越界 {len(extra_hits)} 组：{extra_hits}" if extra_hits else ""))


def check_literals(t: dict) -> None:
    """L9：占位符形态 + 手机号/邮箱格式。通过线是占位符命中 0。"""
    print("\nL9 字面值真实性（仅覆盖 BUILDERS 生成的事实表；维度表来自现成 CSV，"
          "见 main.load_dim_ids）")

    hits = []
    for key, vals in text_cols(t):
        share, prefix, ntail = serial_shape(vals)
        if share < 0.99:
            continue
        why = SERIAL_OK.get(key)
        line = (f"    {key:<34} {share:>4.0%} 前缀 {prefix!r} + 数字尾"
                f"（{ntail} 种尾） 例 {vals[0]!r}")
        print(line + (f"   ← 豁免：{why}" if why else ""))
        if why is None:
            hits.append(key)
    ok(not hits, f"无占位符形态的文本列（豁免表 {len(SERIAL_OK)} 列，理由见 SERIAL_OK）"
                 + (f"；命中 {len(hits)} 列：{hits}" if hits else ""))

    # 词沙拉：序号判据的盲区，判据与理由见 WORD_SALAD_RE 上方注释
    salad = []
    for key, vals in text_cols(t):
        bad = [v for v in vals if WORD_SALAD_RE.search(v) and "。" not in v]
        if bad:
            salad.append((key, len(bad), len(vals), bad[0]))
            print(f"    {key:<34} {len(bad)}/{len(vals)} 中文字后紧跟 ASCII 句点"
                  f" 例 {bad[0][:40]!r}")
    ok(not salad, "无 Faker 中文词沙拉形态的文本列（判据：中文字+ASCII 句点 且 无「。」）"
                  + (f"；命中 {len(salad)} 列：{[s[0] for s in salad]}" if salad else ""))

    # 邮箱：本地部分不得是序号形态，且域名要有真实的多样性（真实用户散在多家邮箱）
    email = [x for x in np.asarray(t["users"]["email"], dtype=object).tolist()
             if isinstance(x, str)]
    bad_fmt = sum(1 for x in email if not EMAIL_RE.match(x))
    ok(bad_fmt == 0, f"users.email 格式合规率 {1 - bad_fmt / len(email):.2%}")
    doms = {x.rsplit("@", 1)[1] for x in email}
    ok(len(doms) >= 5, f"users.email 域名基数 {len(doms)} ≥ 5（实测 {sorted(doms)[:6]}）")
    share, prefix, _ = serial_shape([x.rsplit("@", 1)[0] for x in email])
    ok(share < 0.99, f"users.email 本地部分非序号形态（最大前缀 {prefix!r} 占 {share:.0%}）")

    # 手机号：真实号段
    phone = [x for x in np.asarray(t["users"]["phone"], dtype=object).tolist()
             if isinstance(x, str)]
    bad = sum(1 for x in phone if not CN_MOBILE_RE.match(x))
    ok(bad == 0, f"users.phone 100% 符合 ^1[3-9]\\d{{9}}$"
                 f"（不合规 {bad}/{len(phone)}，例 {phone[0]!r}）")

    check_row_coherence(t)


def check_row_coherence(t: dict) -> None:
    """同一行里的多个文本列不许互相打脸。

    替占位符时最容易新造的缺陷：标题和正文各抽一次词池，于是出现「标题写加湿器、
    正文写跑鞋」「标题『包裹已签收』、正文『正在为你打包』」。这比 `post_N` 更糟——
    占位符一眼假，自相矛盾的文案会被当成真数据读进去。所以这两条单独立断言。
    """
    # posts：正文 = 标题核心 + "。" + 补充句；标题 = 同一核心 + 后缀。
    # 于是「正文第一句」必须是标题的前缀。核心里不含"。"，切第一段即得核心。
    ti = np.asarray(t["posts"]["title"], dtype=object).tolist()
    co = np.asarray(t["posts"]["content"], dtype=object).tolist()
    bad = [(a, b) for a, b in zip(ti, co) if not a.startswith(b.split("。")[0])]
    ok(not bad, f"posts 正文首句是标题前缀（标题/正文同源，违例 {len(bad)}/{len(ti)}"
                + (f"，例 {bad[0]}" if bad else "") + "）")

    # push_notifications：(title, content, deep_link) 必须是 PUSH_COPY 里声明过的三元组，
    # 且要落在本行的 push_type 上——跨类型混搭同样是矛盾。落地页进这个三元组的理由与
    # 标题/正文相同：「优惠券将过期」跳 app://cart 一样是自相矛盾。
    pn = t["push_notifications"]
    declared = {(k, a, b, dl) for k, rows in T.PUSH_COPY.items() for a, b, dl in rows}
    got = set(zip(np.asarray(pn["push_type"], dtype=object).tolist(),
                  np.asarray(pn["title"], dtype=object).tolist(),
                  np.asarray(pn["content"], dtype=object).tolist(),
                  np.asarray(pn["deep_link"], dtype=object).tolist()))
    ok(got <= declared,
       f"push 文案 (push_type, title, content, deep_link) 全部来自 PUSH_COPY 声明的组合"
       f"（实测 {len(got)} 种组合，声明 {len(declared)} 种"
       + (f"，越界 {sorted(got - declared)[:1]}" if got - declared else "") + "）")


# ------------------------------------------------------- L5 行为大表的定义性闭环

def _ts_or_nat(col) -> np.ndarray:
    """可空时间列（object 数组，None 与 datetime 混在一起）→ datetime64[s]，空值成 NaT。

    NaT 参与任何比较都得 False，所以「该有值的行没有值」会连带被下面的时序断言抓到，
    不会因为跳过空值而静默放过。
    """
    return np.array([np.datetime64(x, "s") if x is not None else np.datetime64("NaT")
                     for x in np.asarray(col, dtype=object)], dtype="datetime64[s]")


def check_behavior_closures(t: dict, reg: np.ndarray, as_of_end) -> None:
    """四张行为大表里**定义性**的那几条闭环：不成立就是矛盾，不是「分布不够像」。

    分布形状那一半（集中度、重尾、互关率、打开率差异）由
    `scripts/lakehouse/verify_behavior.py` 的 27 条判据量，那边的通过线是自算随机基线
    的倍数。这里只放「靠定义就能判对错」的：先有帖才有赞、草稿没有赞、推送的三个
    时间戳逐级递增、事务型推送不挂运营活动、取值域没退化。

    分工的理由是**运行成本**：verify_behavior.py 的正向那一支要连库（或指 --from-csv
    一份产出），挂不进 L0；这一批在 scale 1 上 1 秒跑完，是迭代规模上唯一的闸门。
    """
    # ---- 配置自身：生成侧与判据侧共用的那几个常量不许悄悄变形 ----
    ok(len(T.PAGE_VIEW_WEIGHTS) == len(T.PAGES),
       f"PAGE_VIEW_WEIGHTS 与 PAGES 等长（{len(T.PAGE_VIEW_WEIGHTS)} / {len(T.PAGES)}）")
    ok(len(T.REFERRER_W) == len(T.REFERRERS),
       f"REFERRER_W 与 REFERRERS 等长（{len(T.REFERRER_W)} / {len(T.REFERRERS)}）")
    ok(set(T.PUSH_OPEN_RATE) == set(T.PUSH_COPY),
       f"PUSH_OPEN_RATE 的键集 == PUSH_COPY 的键集"
       f"（差集 {set(T.PUSH_OPEN_RATE) ^ set(T.PUSH_COPY)}）")
    # 下面两条把 verify_behavior 的两条通过线写在**声明**上，而不是等产出去量：
    # 权重是拍在配置里的，产出只是它的回声，所以配置退化时这里先红，且与规模无关。
    hm = T.PAGE_VIEW_WEIGHTS[T.PAGES.index("home")]
    cm = T.PAGE_VIEW_WEIGHTS[T.PAGES.index("checkout")]
    ok(hm >= 3.0 * cm, f"home 页权重 {hm} ≥ checkout 的 3 倍（{cm}）——判据 pv_funnel 的线")
    rates = list(T.PUSH_OPEN_RATE.values())
    ok(max(rates) >= 1.5 * min(rates),
       f"推送打开率 max/min = {max(rates) / min(rates):.2f}× ≥ 1.5×——判据 push_open_by_type 的线")

    # ---- post_likes：先有帖、再有赞；草稿和待审帖没有赞 ----
    p, lk = t["posts"], t["post_likes"]
    status = np.asarray(p["status"], dtype=object)
    pub_ts = _ts_or_nat(p["published_at"])
    published = status == "published"
    ok((np.isnat(pub_ts) == ~published).all(),
       "posts.published_at 非空 ⟺ status='published'（与 v1 生成器和线上数据一致）")
    lp, lu = np.asarray(lk["post_id"]), np.asarray(lk["user_id"])
    lk_ts = np.asarray(lk["created_at"]).astype("datetime64[s]")
    bad_invisible = int((~published[lp - 1]).sum())
    ok(bad_invisible == 0,
       f"赞全部落在已发布的帖子上（落在草稿/待审/已删帖上的 {bad_invisible} 行）")
    bad_early = int((lk_ts < pub_ts[lp - 1]).sum())
    ok(bad_early == 0, f"赞的时刻 ≥ 所赞帖子的发布时刻（违例 {bad_early} 行）")
    ok((lk_ts >= reg[lu - 1]).all(), "赞的时刻 ≥ 点赞者本人的注册时刻")
    ok((lk_ts <= as_of_end).all(), "赞的时刻不越窗")

    # ---- user_follows：两端都注册了才可能关注；无自关注 ----
    fo = t["user_follows"]
    fa, fb = np.asarray(fo["follower_id"]), np.asarray(fo["following_id"])
    fo_ts = np.asarray(fo["created_at"]).astype("datetime64[s]")
    ok((fa != fb).all(), "没有自关注")
    ok((fo_ts >= np.maximum(reg[fa - 1], reg[fb - 1])).all(),
       "关注的时刻 ≥ 两端注册时刻的较晚者")
    ok((fo_ts <= as_of_end).all(), "关注的时刻不越窗")

    # ---- push_notifications：漏斗嵌套、标记⟷时间戳、三个时间戳逐级递增 ----
    # 旧版 delivered_at 和 opened_at **都从 scheduled_at 起算**，偏移区间还重叠
    # （1~30 分钟 vs 2~1440 分钟），于是约三成已打开的推送「打开早于送达」。
    pn = t["push_notifications"]
    sch = np.asarray(pn["scheduled_at"]).astype("datetime64[s]")
    dlv, opn = _ts_or_nat(pn["delivered_at"]), _ts_or_nat(pn["opened_at"])
    isd = np.asarray(pn["is_delivered"]).astype(bool)
    iso = np.asarray(pn["is_opened"]).astype(bool)
    ok((np.isnat(dlv) == ~isd).all(), "is_delivered ⟺ delivered_at 非空")
    ok((np.isnat(opn) == ~iso).all(), "is_opened ⟺ opened_at 非空")
    ok((~iso | isd).all(), "已打开 ⇒ 已送达（漏斗嵌套）")
    ok((dlv[isd] >= sch[isd]).all(), "delivered_at ≥ scheduled_at")
    ok((opn[iso] >= dlv[iso]).all(), "opened_at ≥ delivered_at")
    fr = np.asarray(pn["failure_reason"], dtype=object)
    ok(all(x is not None for x in fr[~isd]),
       "未送达 ⇒ failure_reason 非空（卡片的「发送失败」判据只认这一列）")
    ok(all(x is None for x in fr[isd]), "已送达 ⇒ failure_reason 为空")
    # 事务型推送不挂运营活动（docs/data-audit.md 的 L3 一节）。旧版在**全部行**上按
    # 18% 置空，于是 82% 的订单通知挂着 campaign_id。
    cid = np.asarray(pn["campaign_id"], dtype=object)
    mk = np.isin(np.asarray(pn["push_type"], dtype=object), list(T.MARKETING_PUSH_TYPES))
    ok(all((x is not None) == m for x, m in zip(cid.tolist(), mk.tolist())),
       f"campaign_id 非空 ⟺ 营销型推送（营销型 {T.MARKETING_PUSH_TYPES}，"
       f"非空 {int(sum(x is not None for x in cid))} 行 / 营销型 {int(mk.sum())} 行）")

    # ---- 取值域：这几列曾经整列同值或只有 1 种非空取值 ----
    pv = t["page_views"]
    refs = set(np.asarray(pv["referrer"], dtype=object).tolist())
    ok(refs <= set(T.REFERRERS),
       f"page_views.referrer ⊆ REFERRERS 声明的取值域（越界 {sorted(refs - set(T.REFERRERS))[:2]}）")
    ok(len(refs - {""}) >= 2,
       f"page_views.referrer 的非空取值 {len(refs - {''})} 种 ≥ 2"
       f"（原来只有 1 种，按来源页做站内路径分析恒得一行）")
    ok(len(set(np.asarray(pn["deep_link"], dtype=object).tolist())) >= 2,
       "push_notifications.deep_link 不是整列同值（原来整列 app://home）")


# ------------------------------------------------------- D-02 / D-03 商品域语义
#
# 判据全部取自 scripts/gen/semantics.yaml —— 生成器抽样和这里判违例读**同一份**配置，
# 而且价格区间走 `SEM.Semantics.price_range()` 这**同一个函数**。配置共用还不够：把
# 区间算错的方式有很多种，公式也必须共用，否则「修完生成器再改检查」还是自证。
#
# ## 这里判什么、不判什么
#
# 只判**与规模无关**的性质。分辨率类的指标（每类目 SKU 数、每 SKU 被下单次数、每品牌
# SKU 数）是规模属性：本文件跑 scale=1，products 只有 200 行摊 126 个叶子类目，
# 任何「每类目 ≥ N 个 SKU」的门槛在这里都必然假红，然后被人加豁免绕过去——那种随规模
# 闪烁的断言比没有更糟。它们归 L6，用目标 scale 算，通过线由 budget 的目标规模推出。
#
# 与规模无关的那一半在这里，逐条对应审计条目：
#
#   D-02  products / product_tags 必须由 BUILDERS 生成（曾经整个 SUB 类都是死声明：
#         budget.summary() 照样打印 4133，而 LOAD_ORDER 里根本没有这两张表）；
#         每个叶子类目至少 1 个 SKU（类目覆盖，旧数据 200 SKU 摊 126 叶子必然大量空）。
#   D-03  品牌→类目白名单违例 0；价格越界 0（含分档窗口）；
#         product_name 不再是「修饰词+品牌+类目」模板；description 不是几句轮换。
#
# 「至少 1 个 SKU」这条在 scale=1 下成立（200 ≥ 126），所以它不闪烁——这正是把它
# 放这里、而把「≥ 32 个」放 L6 的分界线。

def check_product_semantics(t: dict) -> None:
    print("\nD-02/D-03 商品域语义（判据来自 scripts/gen/semantics.yaml，与生成器同源）")

    # D-02 的结构性判据：SUB 类不能是死声明。这条先判，因为下面全部依赖它。
    ok("products" in T.BUILDERS,
       "products 由 BUILDERS 生成（不是从 data/csv 原样读进来）")
    ok("product_tags" in T.BUILDERS, "product_tags 由 BUILDERS 生成")
    ok("products" in T.LOAD_ORDER and "product_tags" in T.LOAD_ORDER,
       "products / product_tags 在 LOAD_ORDER 里（否则 main.py 不会写出它们）")

    s = SEM.load()
    leaf_of = {cid: path for path, cid in s.tree.leaf_ids.items()}
    p = t["products"]
    n = len(p["product_id"])

    brand = np.asarray(p["brand"], dtype=object)
    cid = np.asarray(p["category_id"])
    name = np.asarray(p["product_name"], dtype=object)
    price = np.asarray(p["price"], dtype=float)

    # 商品必须挂在叶子类目上。挂到一级/二级去，「各类目 SKU 数」这类分析会双计。
    not_leaf = sorted({int(c) for c in cid if int(c) not in leaf_of})
    ok(not not_leaf, f"全部 {n} 个 SKU 的 category_id 都是叶子类目（level 3）"
                     + (f"；非叶子 {not_leaf[:5]}" if not_leaf else ""))

    leaf = [leaf_of[int(c)] for c in cid]

    # D-03.1 品牌 → 类目白名单
    bad_pair = [(b, lf) for b, lf in zip(brand, leaf) if not s.allows(b, lf)]
    ok(not bad_pair,
       f"品牌→类目白名单违例 0（{n} 个 SKU，{len(s.brands)} 个品牌）"
       + (f"；违例 {len(bad_pair)} 个，例 {sorted(set(bad_pair))[:3]}" if bad_pair else ""))

    # D-03.2 价格越界。上一条过了才有意义（allows 为假时 price_range 无定义），
    # 所以只对合法配对判价格；不合法的配对已经由上一条判死。
    over = []
    for b, lf, v in zip(brand, leaf, price):
        if not s.allows(b, lf):
            continue
        lo, hi = s.price_range(b, lf)
        if not (lo <= v <= hi):
            ref = v / hi if v > hi else lo / v
            over.append((b, lf.split(SEM.SEP)[-1], round(float(v), 2),
                         round(lo, 2), round(hi, 2), round(float(ref), 2)))
    ok(not over,
       f"价格越界 0（区间 = 类目带 ∩ 品牌分档窗口，走 SEM.price_range 同一函数）"
       + (f"；越界 {len(over)} 个，例 {over[:3]}（末位是偏离倍数）" if over else ""))

    # D-03.3 商品名结构：必须是「品牌 叶子类目 规格」，且规格来自本二级类目的池子。
    bad_name = []
    for b, lf, nm in zip(brand, leaf, name):
        want_pre = f"{b} {lf.split(SEM.SEP)[-1]} "
        if not nm.startswith(want_pre) or nm[len(want_pre):] not in s.specs_for(lf):
            bad_name.append(nm)
    ok(not bad_name,
       f"product_name 全部形如「品牌 叶子类目 规格」且规格来自该二级类目词池"
       + (f"；违例 {len(bad_name)}/{n}，例 {bad_name[:3]}" if bad_name else ""))

    # D-03.4 v1 模板反查：名字不该再由营销修饰词打头（`优质戴森 电热水壶`）。
    # 判据存在 semantics.yaml 的 legacy_name_prefixes，不写死在这里。
    legacy = [nm for nm in name if nm.startswith(tuple(s.legacy_name_prefixes))]
    ok(not legacy,
       f"无 v1「修饰词+品牌+类目」模板残留（修饰词 {list(s.legacy_name_prefixes)}）"
       + (f"；命中 {len(legacy)}，例 {legacy[:3]}" if legacy else ""))

    # 重名会让「GMV Top10 商品」这类按名字聚合的题把不同 SKU 并成一行。
    ok(len(set(name.tolist())) == n,
       f"product_name 在本次生成内唯一（{len(set(name.tolist()))}/{n}；"
       f"配置容量上界 {s.capacity()}）")

    # D-02 类目覆盖：每个叶子类目至少 1 个 SKU。与规模无关的部分（200 ≥ 126）。
    covered = set(leaf)
    miss = sorted(set(s.tree.leaf_paths) - covered)
    ok(not miss, f"126 个叶子类目全部至少 1 个 SKU（本次覆盖 {len(covered)}）"
                 + (f"；空类目 {len(miss)} 个，例 {miss[:3]}" if miss else ""))

    # D-03.5 description：v1 是 4 句模板轮换（distinct=4，与 SKU 数无关）。
    # 判据取「去重数 ≥ 0.9 × SKU 数」而不是「≥ 某个常数」：常数门槛在 scale=1 的
    # 200 行上和 scale=427 的 4133 行上含义完全不同，而比例是规模无关的。
    desc = np.asarray(p["description"], dtype=object)
    nd = len(set(desc.tolist()))
    ok(nd >= 0.9 * n,
       f"description 去重数 {nd}/{n} ≥ 90%（v1 是 4 句模板轮换，去重恒为 4）")

    # 同行一致性：description 必须提到本行的品牌和类目。呼应 check_row_coherence——
    # 描述另抽一次词池就会出现「标题写加湿器、描述写跑鞋」。
    incoh = [(b, lf, d) for b, lf, d in zip(brand, leaf, desc)
             if b not in d or lf.split(SEM.SEP)[-1] not in d]
    ok(not incoh, f"description 含本行 brand 与叶子类目名（同行不打脸）"
                  + (f"；违例 {len(incoh)}，例 {incoh[:1]}" if incoh else ""))

    # 价格三兄弟的关系。DDL 有 cost / original_price / price 三列，
    # 「毛利」「折扣率」这类题直接算它们的差，关系反了会算出负毛利。
    orig = np.asarray(p["original_price"], dtype=float)
    cost = np.asarray(p["cost"], dtype=float)
    ok((price <= orig).all(), f"price ≤ original_price（违例 {int((price > orig).sum())}）")
    ok((cost < price).all(), f"cost < price（违例 {int((cost >= price).sum())}）")

    # ---- product_tags ----
    pt = t["product_tags"]
    tag_name = np.asarray(pt["tag_name"], dtype=object)
    tag_type = np.asarray(pt["tag_type"], dtype=object)
    pid = np.asarray(pt["product_id"])
    pool = {k: set(v) for k, v in s.tag_pools.items()}

    bad_tag = [(ty, nm) for ty, nm in zip(tag_type, tag_name)
               if nm not in pool.get(ty, set())]
    ok(not bad_tag, f"product_tags 的 (tag_type, tag_name) 全部来自 tag_pools 声明"
                    + (f"；越界 {len(bad_tag)}，例 {sorted(set(bad_tag))[:3]}"
                       if bad_tag else ""))

    ids = set(p["product_id"].tolist())
    dangling = sorted({int(x) for x in pid if int(x) not in ids})
    ok(not dangling, f"product_tags.product_id 全部指向存在的 SKU"
                     + (f"；悬挂 {dangling[:5]}" if dangling else ""))

    # DDL 里 product_tags 有 UNIQUE(product_id, tag_name)，生成器必须自己保证。
    pairs = list(zip(pid.tolist(), tag_name.tolist()))
    ok(len(set(pairs)) == len(pairs),
       f"UNIQUE(product_id, tag_name) 成立（{len(set(pairs))}/{len(pairs)}）")


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """秩相关。用平均秩处理并列——窗内销量有大量并列（含 0），普通秩会凭结果顺序
    给同值不同秩，相关系数因此随输入顺序漂。"""
    def rank(x: np.ndarray) -> np.ndarray:
        order = np.argsort(x, kind="stable")
        r = np.empty(len(x), dtype=float)
        i = 0
        while i < len(x):
            j = i
            while j + 1 < len(x) and x[order[j + 1]] == x[order[i]]:
                j += 1
            r[order[i:j + 1]] = (i + j) / 2.0
            i = j + 1
        return r
    ra, rb = rank(np.asarray(a, dtype=float)), rank(np.asarray(b, dtype=float))
    ra, rb = ra - ra.mean(), rb - rb.mean()
    den = float(np.sqrt((ra * ra).sum() * (rb * rb).sum()))
    return float((ra * rb).sum() / den) if den else 0.0


# 累计 ÷ 窗内 的结构上界：窗前销量 = 窗内日均 × 窗前天数 × 爬坡系数，
# 而窗前天数 ≤ PRODUCT_LEAD_DAYS、爬坡系数 ≤ 1，所以比值 ≤ 1 + 180/91 < 3。
# 这条线不是"实测值加余量"，是由生成器的构造推出来的——改 PRODUCT_LEAD_DAYS
# 或爬坡上限必须同时改它，否则断言会在生成器合法变化时误报。
SOLD_RATIO_MAX = 1.0 + T.PRODUCT_LEAD_DAYS / budget.WINDOW_DAYS
MIN_SOLD_RANK_CORR = 0.5


def check_product_counters(t: dict) -> None:
    """products 的五个计数器 ⟷ order_items 的跨表数值闭环。

    docs/test-plan.md 把这一类（`products.sold_count` ⟷ order_items 行数）逐字列为
    "还没有自动化覆盖"的真缺口，并指定归属在本文件——跨表数值闭环不放进
    verify_behavior.py，两份实现只会各自漂。这一组就是那个缺口的落地。

    旧版这五列是独立随机数，与 order_items 毫无关系。目标规模（scale=427）实测：
    Σsold_count 是 Σ窗内件数的 26.9 倍、154 个 SKU（3.7%）的累计销量**小于**自己
    窗内的销量、Spearman +0.02、Top20 爆款重叠 0/20。「卖得最好的 20 个商品」按
    两条路径查会得到两份没有交集的名单，两条路径都不报错。

    判据不是相等：sold_count 是累计，order_items 只有 91 天窗口，而 created_at 还
    落在窗口之前最多 PRODUCT_LEAD_DAYS 天。所以判 `累计 ≥ 窗内` + 强正相关 + 比值
    落在构造上界内。
    """
    p, it = t["products"], t["order_items"]
    pid = np.asarray(p["product_id"])
    n = len(pid)
    # product_id 是 1..n 的主键（F.pk），所以 id 即下标 +1
    win = np.bincount(np.asarray(it["product_id"]),
                      weights=np.asarray(it["quantity"], dtype=float),
                      minlength=n + 1)[1:].astype(np.int64)

    sold = np.asarray(p["sold_count"], dtype=np.int64)
    view = np.asarray(p["view_count"], dtype=np.int64)
    fav = np.asarray(p["favorite_count"], dtype=np.int64)
    rate_n = np.asarray(p["rating_count"], dtype=np.int64)
    rating = np.asarray(p["rating_avg"], dtype=float)

    bad = int((sold < win).sum())
    ok(bad == 0, f"每个 SKU 的 sold_count ≥ 它在窗内的实际销量（违例 {bad}/{n}"
                 + (f"，最糟 sold_count={int(sold[sold < win].min())}"
                    f" 而窗内 {int(win[sold < win].max())} 件" if bad else "") + "）")
    ratio = sold.sum() / max(int(win.sum()), 1)
    ok(1.0 <= ratio <= SOLD_RATIO_MAX,
       f"Σsold_count ÷ Σ窗内件数 = {ratio:.2f}× 落在 [1.00, {SOLD_RATIO_MAX:.2f}]"
       f"（上界由 PRODUCT_LEAD_DAYS/91 推出，不是实测值加余量）")
    rho = _spearman(sold, win)
    ok(rho >= MIN_SOLD_RANK_CORR,
       f"Spearman(累计销量, 窗内销量) = {rho:+.4f} ≥ {MIN_SOLD_RANK_CORR}"
       f"——「爆款榜」两条路径要指向同一批 SKU")

    # 转化率：浏览量是从销量反推的，所以每一行的 sold/view 必须落在声明区间内。
    # 这条同时钉住量级——旧版先抽百万级浏览再乘 0.05%~3%，转化率会掉到区间之下。
    lo, hi = T.PRODUCT_CONV_RATE
    m = sold > 0
    conv = np.divide(sold, np.maximum(view, 1), dtype=float)[m]
    ok(m.sum() == 0 or ((conv >= lo * 0.99).all() and (conv <= hi * 1.01).all()),
       f"有销量的 SKU 转化率 sold/view 全部落在 PRODUCT_CONV_RATE"
       f" [{lo:.1%}, {hi:.1%}]（实测 {conv.min():.2%}~{conv.max():.2%}）"
       if m.sum() else "没有任何 SKU 有销量，转化率判据无输入")
    ok((view >= sold).all(), f"view_count ≥ sold_count（违例 {int((view < sold).sum())}）")
    ok((fav <= view).all(), f"favorite_count ≤ view_count（违例 {int((fav > view).sum())}）")
    ok((rate_n <= sold).all(), f"rating_count ≤ sold_count（违例 {int((rate_n > sold).sum())}）")
    ok(((rate_n == 0) == (rating == 0.0)).all(),
       "rating_count == 0 ⟺ rating_avg == 0（v1 有 0 条评价却 3.5 分的行）")

    # 库存现在走独立 RNG 流（见 _prep_products），这条钉住它唯一的行内不变量：
    # 只有卖光的 SKU 库存为 0，其余仍落在声明区间内。
    status = np.asarray(p["status"], dtype=object)
    stock = np.asarray(p["stock"], dtype=np.int64)
    ok(((stock == 0) == (status == "sold_out")).all(),
       f"stock == 0 ⟺ status == 'sold_out'"
       f"（违例 {int(((stock == 0) != (status == 'sold_out')).sum())}）")
    live = stock[status != "sold_out"]
    ok(live.size == 0 or ((live >= 20).all() and (live <= 5000).all()),
       f"未卖光的 SKU 库存落在 [20, 5000]（实测 {int(live.min())}~{int(live.max())}）")


def build_tables() -> tuple:
    """scale=1 全内存生成一份表集合。正例跑它，每个反例跑它的深拷贝。

    拆出来的唯一理由是**反例一侧**：造数要 5 秒，而反例有 26 个，每个都重造一遍就
    从秒级掉到分钟级，那样它迟早会被从 test_all.sh 里拿掉。
    """
    rows = budget.table_rows(1.0)
    start = np.datetime64(budget.DATA_START, "s")
    as_of_end = (np.datetime64(budget.AS_OF, "D").astype("datetime64[s]")
                 + np.timedelta64(86399, "s"))
    ctx = T.Ctx(seed=42, rows=rows, start=start, days=budget.WINDOW_DAYS,
                as_of_end=as_of_end, dim_ids=load_dim_ids())
    T.prepare_globals(ctx)
    t = {name: build(ctx, 0, ctx.n(name)) for name, build in T.BUILDERS.items()}
    return ctx, t


def run_all_checks(t: dict, ctx) -> None:
    """全部断言。**只读 t 和 ctx**（ctx 只用到 `n()` / `cache` / `as_of_end`），
    所以往一份深拷贝里注入缺陷就能整套重跑——这是反例一侧能存在的前提。
    """
    nu = ctx.n("users")
    as_of_end = ctx.as_of_end

    # ---------- L4.1 / L4.2 订单头 vs 明细 ----------
    o, it = t["orders"], t["order_items"]
    no = len(o["order_id"])
    item_sum_c = np.bincount(it["order_id"], weights=cents(it["actual_amount"]),
                             minlength=no + 1)[1:].astype(np.int64)
    ok((item_sum_c == cents(o["total_amount"])).all(),
       "每单明细 actual_amount 之和 == 订单头 total_amount（精确到分）")
    real_cnt = np.bincount(it["order_id"], minlength=no + 1)[1:]
    ok((o["item_count"] == real_cnt).all(), "orders.item_count == 明细真实行数")
    ok((cents(it["unit_price"]) * it["quantity"] - cents(it["discount_amount"])
        == cents(it["actual_amount"])).all(),
       "明细行内公式 actual = unit×qty − discount 精确成立")
    ok((np.asarray(it["actual_amount"]) > 0).all(), "明细金额全部为正")
    ok((cents(o["actual_amount"]) == cents(o["total_amount"])
        - cents(o["discount_amount"]) + cents(o["shipping_fee"])).all(),
       "订单头公式 actual = total − discount + shipping 成立")

    # ---------- L4.3 / L4.4 计数器回填 ----------
    p = t["posts"]
    npost = len(p["post_id"])
    for src, col in (("post_likes", "like_count"), ("post_comments", "comment_count"),
                     ("post_shares", "share_count")):
        real = np.bincount(t[src]["post_id"], minlength=npost + 1)[1:]
        ok((p[col] == real).all(), f"posts.{col} == {src} 真实行数")
    ok((p["view_count"] >= p["like_count"]).all(), "view_count ≥ like_count")

    s = t["sessions"]
    ns = len(s["session_id"])
    ev_real = np.bincount(t["events"]["session_id"], minlength=ns + 1)[1:]
    pv_real = np.bincount(t["page_views"]["session_id"], minlength=ns + 1)[1:]
    ok((s["event_count"] == ev_real).all(), "sessions.event_count == events 真实行数")
    ok((s["page_view_count"] == pv_real).all(),
       "sessions.page_view_count == page_views 真实行数")
    ok((np.asarray(s["is_bounce"]) == (pv_real <= 1)).all(), "is_bounce == (pv ≤ 1)")

    # ---------- L4.5 user_level 规则 ----------
    u = t["users"]
    valid_m = np.isin(o["status"], list(T.VALID_STATUS))
    spend = np.bincount(o["user_id"][valid_m],
                        weights=np.asarray(o["actual_amount"], float)[valid_m],
                        minlength=nu + 1)[1:]
    lvl, vip = np.asarray(u["user_level"]), np.asarray(u["is_vip"])
    ok((lvl[(spend >= 10000) | vip] == 5).all(), "消费≥10000 或 VIP ⇒ level 5")
    ok((spend[lvl == 4] >= 1000).all() and (~vip[lvl == 4]).all(),
       "level 4 全部满足消费≥1000 且非 VIP")
    ok((spend[lvl <= 3] < 1000).all(), "level ≤3 消费全部 <1000（优先级正确）")
    share_1k = [float((spend[lvl == k] >= 1000).mean()) for k in (1, 2, 3)]
    ok(max(share_1k) == 0.0, "低等级里没有漏网的高消费用户（旧版五档全是 21%）")

    # ---------- D-01 画像 ⟷ 消费的那条边 ----------
    # 这一组管三件事，L8（verify_correlation.py）管不了其中任何一件：L8 在**产出**上
    # 量档间极差，看得见效应大小，看不见效应是怎么接上的。三件事分别是：
    #   1. 向量化的倍率乘积 == profiles.yaml 声明的那个函数（防两边各写一份公式）；
    #   2. 倍率真的传到了 GMV 上、而且没有下标错位（错位后极差照样很大，L8 全绿）；
    #   3. 底部截断没有失控（倍率跨度调过头会把一堆单压到 9.9 这一个值上）。
    prof, mult = ctx.cache["user_profiles"], ctx.cache["user_spend_multiplier"]
    dims, _j, gen = PROF.load()
    want = np.array([gen.raw_multiplier(prof["income_level"][i], prof["gender"][i],
                                        prof["occupation"][i], int(prof["age"][i]))
                     for i in range(nu)], float)
    ok(np.allclose(mult, want / want.mean(), rtol=1e-12),
       "每用户倍率 == profiles.yaml 的 raw_multiplier 四表之积（归一后，逐行）")
    ok(abs(float(mult.mean()) - 1.0) < 1e-12,
       "倍率均值归一到 1（不归一会顺带把全库 GMV 抬高约两成，而 L5 恒等式抓不到）")
    ok(set(np.unique(prof["occupation"]).tolist()) <= set(dims.occupation),
       "occupation 产出 ⊆ profiles.yaml 声明的取值域")

    # 倍率必须真的到达 GMV。判据不查效应有多大（那是 L8 的事），只查**传导**：
    # 按倍率分三档，高档人均 GMV 与低档的比值，应当追上倍率本身的比值。
    #
    # 为什么 L8 不够——实测过下标错位（`mult[uid]` 而不是 `mult[uid-1]`，即每个用户
    # 拿到隔壁用户的倍率）：
    #     scale 1   本断言 0.49×（阈值 1.48×，差 3 倍，必红） · L8 3 FAIL · 4 WEAK
    #     scale 1   修好的生成器                              · L8 0 FAIL · 4 WEAK
    # L8 在 scale 1 上四条极差判据**两种情况下都是 WEAK**，只有方向约束偶然抓到了错位，
    # 而方向约束每维只有 1 bit、还看抽样运气。L8 要给出确定结论得跑到 scale 500（造数
    # 51s），而硬约束 #1 要求在小样本上迭代。所以这条断言的作用不是重复 L8，
    # 是在**迭代规模上**把错位变成确定性的红——1 秒，而不是 51 秒。
    q = np.quantile(mult, [1 / 3, 2 / 3])
    lo_m, hi_m = mult <= q[0], mult >= q[1]
    want_ratio = float(mult[hi_m].mean() / mult[lo_m].mean())
    got_ratio = float(spend[hi_m].mean() / spend[lo_m].mean())
    # 取声明比值的 40% 作下限：截断、订单数长尾、scale 1 只有 500 用户，三样都压缩它。
    # 分离度极大（接上是 ~4×，断开是 ~1×），所以这个阈值不需要精确。
    ok(got_ratio >= 0.40 * want_ratio,
       f"倍率传导到 GMV：高倍率三分之一 / 低倍率三分之一 人均 GMV = {got_ratio:.2f}×"
       f"，≥ 声明比值 {want_ratio:.2f}× 的 40%")

    floor_share = float((np.asarray(o["total_amount"], float) <= 9.9).mean())
    ok(floor_share < 0.02,
       f"贴 9.9 地板的订单占 {floor_share:.3%} < 2%（超了说明倍率跨度调过头，"
       f"一个字面值会变成低价段的众数）")

    # ---------- L4.6 支付覆盖与生命周期 ----------
    pay = t["payments"]
    st_by_oid = dict(zip(pay["order_id"].tolist(), np.asarray(pay["status"]).tolist()))
    valid_ids = o["order_id"][valid_m]
    ok(all(st_by_oid.get(i) == "success" for i in valid_ids.tolist()),
       "每个有效订单恰有一条 success 支付")
    refund_ids = o["order_id"][np.asarray(o["status"]) == "refunded"]
    ok(all(st_by_oid.get(i) == "refunded" for i in refund_ids.tolist()),
       "每个退款订单恰有一条 refunded 支付")
    amt_by_oid = dict(zip(pay["order_id"].tolist(),
                          cents(pay["amount"]).tolist()))
    ok(all(amt_by_oid[i] == c for i, c in
           zip(o["order_id"].tolist(), cents(o["actual_amount"]).tolist())
           if i in amt_by_oid), "支付金额 == 订单 actual_amount")
    for stt, col in (("refunded", "refunded_at"), ("cancelled", "cancelled_at")):
        m = np.asarray(o["status"]) == stt
        ok(all(x is not None for x in np.asarray(o[col], dtype=object)[m]),
           f"status={stt} ⇒ {col} 非空")
    paid_m = np.isin(o["status"], ["paid", "shipped", "delivered", "refunded"])
    ok(all(x is not None for x in np.asarray(o["paid_at"], dtype=object)[paid_m]),
       "已付状态 ⇒ paid_at 非空")

    # ---------- L4.7 券核销闭环 ----------
    uc = t["user_coupons"]
    used_m = np.asarray(uc["status"]) == "used"
    cp_orders = np.flatnonzero(
        np.array([x is not None for x in np.asarray(o["coupon_id"], dtype=object)]))
    ok(int(used_m.sum()) == len(cp_orders), "used 券数 == 用券订单数（1:1）")
    uc_oid = np.array([x for x in np.asarray(uc["order_id"], dtype=object)[used_m]],
                      dtype=np.int64)
    ok((np.sort(uc_oid) == np.sort(cp_orders + 1)).all(), "核销行 order_id 与用券订单集合一致")
    idx = uc_oid - 1
    ok((np.asarray(uc["user_id"])[used_m] == o["user_id"][idx]).all(),
       "核销行 user 与订单 user 一致")
    ok((np.asarray(uc["coupon_id"])[used_m]
        == np.array([int(x) for x in np.asarray(o["coupon_id"], dtype=object)[idx]])).all(),
       "核销行 coupon 与订单 coupon 一致")
    ok(all(x is None for x in np.asarray(uc["order_id"], dtype=object)[~used_m]),
       "非核销行 order_id 全为空")
    exp_ts = np.asarray(uc["expire_at"]).astype("datetime64[s]")
    ok((exp_ts[np.asarray(uc["status"]) == "expired"] < as_of_end).all()
       and (exp_ts[np.asarray(uc["status"]) == "unused"] >= as_of_end).all(),
       "expired/unused 与 expire_at 是否过窗一致")

    # ---------- L4.8 / L5.6 事件保底与漏斗 ----------
    ev = t["events"]
    name = np.asarray(ev["event_name"], dtype=object)
    cnt = {n_: int((name == n_).sum())
           for n_ in ("register", "purchase", "use_coupon", "view_home",
                      "view_product", "add_to_cart", "begin_checkout")}
    ok(cnt["register"] == nu, "register 事件数 == 用户数")
    ok(cnt["purchase"] == int(valid_m.sum()), "purchase 事件数 == 有效订单数")
    ok(cnt["use_coupon"] == len(cp_orders), "use_coupon 事件数 == 核销券数")
    ok(cnt["view_home"] > cnt["view_product"] > cnt["add_to_cart"]
       > cnt["begin_checkout"] > cnt["purchase"],
       f"漏斗单调递减 {[cnt[k] for k in ('view_home','view_product','add_to_cart','begin_checkout','purchase')]}")

    # ---------- L5.8 留存衰减 + 先注册后行为 ----------
    reg = np.asarray(u["registered_at"]).astype("datetime64[s]")
    ev_uid = np.asarray(ev["user_id"])
    ev_ts = np.asarray(ev["event_time"]).astype("datetime64[s]")
    ok((ev_ts >= reg[ev_uid - 1] - np.timedelta64(1, "s")).all(), "事件时刻 ≥ 本人注册时刻")
    sess_day = (np.asarray(s["start_time"]).astype("datetime64[D]")
                - reg[s["user_id"] - 1].astype("datetime64[D]")).astype(np.int64)
    ok((sess_day >= 0).all(), "会话日期 ≥ 本人注册日期")
    ok((o["placed_at"].astype("datetime64[s]") >= reg[o["user_id"] - 1]).all(),
       "下单时刻 ≥ 本人注册时刻")
    ok((np.asarray(uc["received_at"]).astype("datetime64[s]")
        >= reg[np.asarray(uc["user_id"]) - 1]).all(), "领券时刻 ≥ 本人注册时刻")
    sess_uid = np.zeros(ns + 1, np.int64)
    sess_uid[s["session_id"]] = s["user_id"]
    ok((ev_uid == sess_uid[ev["session_id"]]).all(), "事件 user == 所属会话 user")

    lag = ((ev_ts - reg[ev_uid - 1]).astype("timedelta64[D]").astype(np.int64))
    h = np.bincount(lag[(lag >= 0) & (lag <= 30)], minlength=31)
    ok(h[0] > h[7] > h[14] > h[28],
       f"活跃量随注册后天数单调衰减 D0={h[0]} D7={h[7]} D14={h[14]} D28={h[28]}")

    # ---------- 最后活跃 ----------
    la = np.asarray(u["last_active_at"]).astype("datetime64[s]")
    ok((la >= reg).all(), "last_active_at ≥ registered_at")
    ok((la <= as_of_end).all(), "last_active_at 不越窗")

    # ---------- L5 行为大表的定义性闭环 ----------
    check_behavior_closures(t, reg, as_of_end)

    # ---------- D-02/D-03 商品域语义 ----------
    check_product_semantics(t)

    # ---------- 商品计数器 ⟷ order_items 跨表闭环 ----------
    check_product_counters(t)

    # ---------- 枚举 ⟷ 卡片 ----------
    check_enums_vs_cards(t)

    # ---------- L9 字面值真实性 ----------
    check_literals(t)


# ============================================================ 反例一侧（--negative）
#
# 为什么要有这一侧：上面 106 条 `ok()` **全部是正向的**，而本项目已经把「比的两端不
# 独立」「名字比覆盖面大」这类假绿灯逐条记进了 docs/test-plan.md。同一种形态在这个
# 文件上的样子是：判据写歪了（比错列、把违例算成 0、条件恒真），它照样一片绿，而且
# 因为它绿得最快、跑在 L0，后面每一层的"通过"都会被读成"数据是对的"。
# 任务书硬约束 #5 要求「写断言 → 跑，确认它失败 → 改生成器 → 跑，确认它通过」——
# 那个「确认它失败」在这个文件里此前只发生在写代码的当时，没有任何东西把它留下来。
#
# ## 判据：不只要求变红，还要求**红在指定的那条断言上**
#
# 只要求"注入缺陷后有断言红了"是不够的，那是本项目栽过的形态：注入的缺陷很可能先撞
# 到一条无关的断言，于是反例"通过"了，而它本来该盯的那条判据其实已经坏掉。所以每个
# 反例声明一个期望消息片段，红在别处 = 反例失败，并把实际消息打出来。
#
# ## 这一侧盖不到什么（写清楚，免得名字比覆盖面大）
#
# 1. 反例注入是**表级**的：改的是已生成的表。病灶在生成器内部的缺陷注入不出来——
#    典型是 D-01 那条「倍率传导到 GMV」，它的病灶是 `mult[uid]` 与 `mult[uid-1]` 的
#    下标错位，而在表级上倍率与 GMV 都已经落定。那条判据的红是 2026-08 实测记录下来
#    的（错位时 0.49× vs 阈值 1.48×），不在这一侧。
# 2. 同一个 check 家族里**先判的那条会挡住后判的那条**。比如商品名的「v1 修饰词模板」
#    判据在结构判据之后，而任何带修饰词前缀的名字必然先破结构判据；`Spearman(累计,
#    窗内)` 之前有 `累计 ≥ 窗内` 和比值上界两条，任何打乱排名的注入都先破那两条。
#    所以下面覆盖的是**每个家族至少一条**，不是每条断言各一个反例。
# 3. 注入必须让**判据**变红，而不是让代码崩。undeclared 的 occupation 会让
#    `raw_multiplier` 直接 KeyError（字典直接下标），那是崩不是判红，所以枚举域这一类
#    走 `check_enums_vs_cards`（users.status 塞 banned）而不是 profiles 那一路。

def _inject_brand_violation(t: dict, cache: dict) -> None:
    """给第 0 个 SKU 换一个**真实存在但不许经营该类目**的品牌——D-03 的原始形态。

    不用「不存在的品牌」是刻意的：那种值 `allows()` 也会返回 False，但它同时是个
    更粗的错误（品牌枚举越界），盖不住"海尔卖跑鞋"这种每个字段单看都合法的缺陷。
    """
    s = SEM.load()
    leaf_of = {cid: path for path, cid in s.tree.leaf_ids.items()}
    leaf = leaf_of[int(t["products"]["category_id"][0])]
    for b in s.brands:
        if not s.allows(b, leaf):
            t["products"]["brand"][0] = b
            return
    raise RuntimeError("semantics.yaml 里没有任何品牌被禁止经营该类目，注入失效")


def _inject_like_on_draft(t: dict, cache: dict) -> None:
    """把一个**有赞的**帖子改成草稿。`published_at` 必须同时置空，否则先破的是
    「published_at 非空 ⟺ status='published'」那条，反例就红在了别处。"""
    pid = int(t["post_likes"]["post_id"][0])
    t["posts"]["status"][pid - 1] = "draft"
    t["posts"]["published_at"][pid - 1] = None


def _inject_rating_incoherent(t: dict, cache: dict) -> None:
    p = t["products"]
    p["rating_count"][0] = 0
    p["rating_avg"][0] = 3.5


def _inject_stock_incoherent(t: dict, cache: dict) -> None:
    p = t["products"]
    i = int(np.flatnonzero(np.asarray(p["status"], dtype=object) != "sold_out")[0])
    p["stock"][i] = 0


def _inject_push_open_before_deliver(t: dict, cache: dict) -> None:
    pn = t["push_notifications"]
    i = int(np.flatnonzero(np.asarray(pn["is_opened"]).astype(bool))[0])
    pn["delivered_at"][i] = pn["opened_at"][i] + np.timedelta64(3600, "s")


def _inject_push_campaign_on_txn(t: dict, cache: dict) -> None:
    pn = t["push_notifications"]
    types = np.asarray(pn["push_type"], dtype=object)
    i = int(np.flatnonzero(~np.isin(types, list(T.MARKETING_PUSH_TYPES)))[0])
    pn["campaign_id"][i] = 1


def _inject_coupon_dangling(t: dict, cache: dict) -> None:
    uc = t["user_coupons"]
    i = int(np.flatnonzero(np.asarray(uc["status"]) != "used")[0])
    uc["order_id"][i] = 1


def _inject_flat_retention(t: dict, cache: dict) -> None:
    """把所有事件挪到「注册后第 28 天」：留存曲线从衰减变成一根杆子。

    时刻仍在注册之后，所以「事件时刻 ≥ 本人注册时刻」不会先红——这个反例要的就是
    绕过时序那条、单独打衰减那条。
    """
    reg = np.asarray(t["users"]["registered_at"]).astype("datetime64[s]")
    uid = np.asarray(t["events"]["user_id"])
    t["events"]["event_time"] = reg[uid - 1] + np.timedelta64(28 * 86400, "s")


def _inject_placeholder_username(t: dict, cache: dict) -> None:
    n = len(t["users"]["username"])
    t["users"]["username"] = np.array([f"user_{i + 1}" for i in range(n)], dtype=object)


def _inject_single_email_domain(t: dict, cache: dict) -> None:
    """域名收敛到 1 个，但格式仍然合法——格式那条在前面，必须让它过去。"""
    em = np.asarray(t["users"]["email"], dtype=object)
    t["users"]["email"] = np.array([f"{x.rsplit('@', 1)[0]}@example.com" for x in em],
                                   dtype=object)


# (反例 id, 期望消息片段, 注入函数)。片段取断言消息里**独一无二**的那一小段，
# 不取整句：整句里含实测数字，数字会随生成器合法变化而变。
NEG_CASES: list[tuple[str, str, object]] = [
    # ---- 订单头 ⟷ 明细 ----
    ("head-sum-drift", "明细 actual_amount 之和",
     lambda t, c: t["orders"]["total_amount"].__setitem__(0, t["orders"]["total_amount"][0] + 1.0)),
    ("item-count-drift", "item_count == 明细真实行数",
     lambda t, c: t["orders"]["item_count"].__setitem__(0, t["orders"]["item_count"][0] + 1)),
    ("line-formula-broken", "明细行内公式",
     lambda t, c: t["order_items"]["discount_amount"].__setitem__(
         0, t["order_items"]["discount_amount"][0] + 0.01)),
    ("head-formula-broken", "订单头公式",
     lambda t, c: t["orders"]["shipping_fee"].__setitem__(
         0, t["orders"]["shipping_fee"][0] + 1.0)),
    # ---- 计数器回填 ----
    ("like-counter-drift", "posts.like_count == post_likes 真实行数",
     lambda t, c: t["posts"]["like_count"].__setitem__(0, t["posts"]["like_count"][0] + 1)),
    ("session-event-counter-drift", "sessions.event_count == events 真实行数",
     lambda t, c: t["sessions"]["event_count"].__setitem__(
         0, t["sessions"]["event_count"][0] + 1)),
    ("is-bounce-drift", "is_bounce == (pv ≤ 1)",
     lambda t, c: t["sessions"]["is_bounce"].__setitem__(
         0, not bool(t["sessions"]["is_bounce"][0]))),
    # ---- 等级 / 支付 / 券 ----
    ("user-level-rule-broken", "消费≥10000 或 VIP ⇒ level 5",
     lambda t, c: t["users"]["user_level"].__setitem__(slice(None), 1)),
    ("payment-coverage-broken", "每个有效订单恰有一条 success 支付",
     lambda t, c: t["payments"]["status"].__setitem__(slice(None), "failed")),
    ("payment-amount-drift", "支付金额 == 订单 actual_amount",
     lambda t, c: t["payments"]["amount"].__setitem__(0, t["payments"]["amount"][0] + 1.0)),
    ("coupon-redeem-count", "used 券数 == 用券订单数",
     lambda t, c: t["user_coupons"]["status"].__setitem__(
         np.flatnonzero(np.asarray(t["user_coupons"]["status"]) == "used")[0], "unused")),
    ("coupon-unused-has-order", "非核销行 order_id 全为空", _inject_coupon_dangling),
    # ---- 事件保底 / 漏斗 / 时序 / 留存 ----
    ("register-event-count", "register 事件数 == 用户数",
     lambda t, c: t["events"]["event_name"].__setitem__(
         int(np.flatnonzero(np.asarray(t["events"]["event_name"], dtype=object)
                            == "register")[0]), "view_home")),
    ("funnel-not-monotonic", "漏斗单调递减",
     lambda t, c: t["events"]["event_name"].__setitem__(
         np.flatnonzero(np.asarray(t["events"]["event_name"], dtype=object)
                        == "add_to_cart"), "begin_checkout")),
    ("event-before-register", "事件时刻 ≥ 本人注册时刻",
     lambda t, c: t["events"]["event_time"].__setitem__(
         0, t["events"]["event_time"][0] - np.timedelta64(3650 * 86400, "s"))),
    ("order-before-register", "下单时刻 ≥ 本人注册时刻",
     lambda t, c: t["orders"]["placed_at"].__setitem__(
         0, t["orders"]["placed_at"][0] - np.timedelta64(3650 * 86400, "s"))),
    ("retention-flat", "活跃量随注册后天数单调衰减", _inject_flat_retention),
    # ---- 行为大表的定义性闭环 ----
    ("like-on-draft-post", "赞全部落在已发布的帖子上", _inject_like_on_draft),
    ("like-before-publish", "赞的时刻 ≥ 所赞帖子的发布时刻",
     lambda t, c: t["post_likes"]["created_at"].__setitem__(
         0, t["post_likes"]["created_at"][0] - np.timedelta64(3650 * 86400, "s"))),
    ("self-follow", "没有自关注",
     lambda t, c: t["user_follows"]["following_id"].__setitem__(
         0, t["user_follows"]["follower_id"][0])),
    ("push-opened-before-delivered", "opened_at ≥ delivered_at",
     _inject_push_open_before_deliver),
    ("push-campaign-on-transactional", "campaign_id 非空 ⟺ 营销型推送",
     _inject_push_campaign_on_txn),
    ("referrer-degenerate", "page_views.referrer 的非空取值",
     lambda t, c: t["page_views"]["referrer"].__setitem__(slice(None), "")),
    ("deep-link-single-value", "不是整列同值",
     lambda t, c: t["push_notifications"]["deep_link"].__setitem__(
         slice(None), "app://home")),
    ("push-copy-mixed", "全部来自 PUSH_COPY 声明的组合",
     lambda t, c: t["push_notifications"]["title"].__setitem__(
         slice(None), t["push_notifications"]["title"][0])),
    # ---- 商品域语义（D-02 / D-03）----
    ("brand-category-violation", "品牌→类目白名单违例 0", _inject_brand_violation),
    ("price-out-of-band", "价格越界 0",
     lambda t, c: t["products"]["price"].__setitem__(0, t["products"]["price"][0] * 20)),
    ("product-name-structure", "product_name 全部形如",
     lambda t, c: t["products"]["product_name"].__setitem__(
         0, "优质" + str(t["products"]["product_name"][0]))),
    ("description-template", "description 去重数",
     lambda t, c: t["products"]["description"].__setitem__(
         slice(None), t["products"]["description"][0])),
    ("tag-out-of-pool", "全部来自 tag_pools 声明",
     lambda t, c: t["product_tags"]["tag_name"].__setitem__(0, "热销爆款XYZ")),
    # 把第 1 行整行覆盖成第 0 行。tag_type 必须一起搬：tag_pools 是**按 tag_type 分池**
    # 的，只搬 tag_name 会让它对第 1 行的 type 越界，于是先破分池那条——这正是"红在
    # 别处"，第一次跑就是这么失败的。
    ("tag-not-unique", "UNIQUE(product_id, tag_name)",
     lambda t, c: [t["product_tags"][col].__setitem__(1, t["product_tags"][col][0])
                   for col in ("product_id", "tag_type", "tag_name")]),
    # ---- 商品计数器 ⟷ order_items ----
    ("sold-below-window", "sold_count ≥ 它在窗内的实际销量",
     lambda t, c: t["products"]["sold_count"].__setitem__(slice(None), 0)),
    ("conv-rate-out-of-band", "转化率 sold/view",
     lambda t, c: t["products"]["view_count"].__setitem__(
         slice(None), np.maximum(t["products"]["sold_count"], 1) * 1000000)),
    ("rating-incoherent", "rating_count == 0 ⟺ rating_avg == 0", _inject_rating_incoherent),
    ("stock-incoherent", "stock == 0 ⟺ status == 'sold_out'", _inject_stock_incoherent),
    # ---- 枚举 ⟷ 卡片 ----
    ("enum-not-declared", "生成器枚举产出全部在卡片声明内",
     lambda t, c: t["users"]["status"].__setitem__(0, "banned")),
    # 数组列这一路单独要一个反例：`interests` 是 array<string>，元素是 list 而不是 str。
    # 2026-08-28 之前 _enum_values 的前身会把整列过滤成空集，于是这一列被**静默跳过**，
    # 上面那个 users.status 的反例照样红、这一列却怎么改都绿。注入一个卡片没声明的英文
    # 标签（正是卡片以前误写的那批值之一），要求它红在同一条断言上。
    ("enum-not-declared-array", "生成器枚举产出全部在卡片声明内",
     lambda t, c: t["user_profiles"]["interests"].__setitem__(0, ["electronics"])),
    # 豁免口的护栏：ENUM_SUPERSET_OK 的理由是"产得更宽"，那就必须真的更宽。把
    # utm_campaign 缩成 3 个卡片没声明的值——比声明的 6 个还少，豁免必须失效。
    # 没有这一条，那个字典就是"写进去即永久免检"。
    ("enum-superset-exemption-void", "豁免失效",
     lambda t, c: t["sessions"].__setitem__(
         "utm_campaign",
         np.array(["x_a", "x_b", "x_c"] * len(t["sessions"]["utm_campaign"]),
                  dtype=object)[:len(t["sessions"]["utm_campaign"])])),
    # ---- L9 字面值 ----
    ("placeholder-column", "无占位符形态的文本列", _inject_placeholder_username),
    ("word-salad-content", "无 Faker 中文词沙拉形态",
     lambda t, c: t["posts"]["content"].__setitem__(
         0, "一下参加方面.直接活动因为商品时候.")),
    ("email-format-broken", "users.email 格式合规率",
     lambda t, c: t["users"]["email"].__setitem__(0, "not-an-email")),
    ("email-single-domain", "域名基数", _inject_single_email_domain),
    ("phone-bad-segment", "users.phone 100% 符合",
     lambda t, c: t["users"]["phone"].__setitem__(0, "12345678901")),
    ("post-title-content-mismatch", "posts 正文首句是标题前缀",
     lambda t, c: t["posts"]["content"].__setitem__(0, "完全无关的另一段正文。补充说明")),
]


class _CtxView:
    """反例用的 ctx 替身：`cache` 换成可改的深拷贝，其余转发给真 ctx。

    需要它是因为 D-01 那组断言读的是 `ctx.cache`，不是 `t`；而真 ctx 是所有反例共用
    的，直接改它会污染后面每一个反例。
    """

    def __init__(self, ctx, cache: dict) -> None:
        self._ctx, self.cache = ctx, cache
        self.as_of_end = ctx.as_of_end

    def n(self, table: str) -> int:
        return self._ctx.n(table)


def _run_quiet(t: dict, ctx) -> str | None:
    """整套断言跑一遍，吞掉输出。返回 None = 全过；否则返回第一条失败消息。"""
    global PASS
    import contextlib
    import io
    saved, PASS = PASS, 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run_all_checks(t, ctx)
        return None
    except AssertionError as e:
        return str(e)
    finally:
        PASS = saved


def run_negative() -> int:
    import copy
    ctx, t0 = build_tables()

    # 先证明这份干净数据是绿的。少了这一步，「注入后变红」可能只是因为它本来就红。
    base = _run_quiet(t0, _CtxView(ctx, copy.deepcopy(ctx.cache)))
    if base is not None:
        print(f"  基线就不绿，反例无意义：{base}")
        return 1

    print(f"反例一侧：{len(NEG_CASES)} 个缺陷注入，每个要求整套自测变红"
          f"**且红在指定断言上**")
    bad = []
    for cid, want, inject in NEG_CASES:
        t = copy.deepcopy(t0)
        cache = copy.deepcopy(ctx.cache)
        inject(t, cache)
        got = _run_quiet(t, _CtxView(ctx, cache))
        if got is None:
            bad.append(cid)
            print(f"  \033[31mMISS\033[0m {cid:<32} 注入了缺陷却全绿——判据没在测它")
        elif want not in got:
            bad.append(cid)
            print(f"  \033[31mWRONG\033[0m {cid:<31} 红在别处")
            print(f"        期望片段 {want!r}")
            print(f"        实际消息 {got}")
        else:
            print(f"  red  {cid:<32} {want}")
    if bad:
        print(f"\n反例失败 {len(bad)}/{len(NEG_CASES)}：{bad}")
        return 1
    print(f"\n反例全部按预期变红（{len(NEG_CASES)} 个注入）")
    return 0


def main() -> int:
    ctx, t = build_tables()
    run_all_checks(t, ctx)
    print(f"\n全部通过（{PASS} 项断言）")
    return 0


def _cli() -> int:
    if "--negative" in sys.argv[1:]:
        return run_negative()
    return main()


if __name__ == "__main__":
    raise SystemExit(_cli())
