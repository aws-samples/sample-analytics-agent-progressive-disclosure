#!/usr/bin/env python3
"""L5 分布真实性 · 四张行为大表：`post_likes` / `page_views` / `user_follows` / `push_notifications`。

## 为什么要单独有这一组

任务书把它标成「现有检查集里覆盖缺口最大的一块」，理由是行数：v2 那批数据里这四张表
合计 40,581,089 行 = 全库的 44.45%（v3 现行库是 v1 规模，四张表 95,029 行，占比同量级）。
而在本文件之前，它们的自动化覆盖**只有计数对账**：

    posts.like_count      == post_likes 的行数        （生成侧 selftest_closures.py）
    sessions.page_view_count == page_views 的行数     （同上）
    PUSH_COPY 的标题/正文配对                          （verify_literals.py）

也就是说「有多少行」被查了，「这些行长成什么形状」一条都没查。形状退化不会让任何一层
变红：主键唯一、外键不悬空、枚举合法、行数对账全过，而

    · 500 个用户的点赞数落在 49 ~ 100 之间（真实社交是重尾，多数人 0 次）
    · 19,165 条关注边里互关 7.74%，而同一张图**完全随机连边**的期望互关率是 7.68%
    · 15 个页面的 PV 极差 6%，`checkout` 比 `home` 还多
    · `is_bounce` 与实际页面数完全脱钩：103 个「跳出」会话的页面数是 2~10，没有一个是 1

这四条里没有一条会被现有任何检查器抓到，而它们各自否掉一整类分析（内容热度榜、KOL
识别、页面漏斗、流量质量）。

## 与已有四个检查器的分工

    verify_load.py         装载等值：CSV 真源 ⟷ Athena 现查，行数/求和/边界
    verify_enums.py        枚举列的**取值集合**对不对（卡片 ⟷ 实际）
    verify_literals.py     文本列的**字面值形态**（占位符 / 词沙拉）
    verify_correlation.py  画像列与消费额**之间有没有边**（D-01）
    本文件                 四张行为大表**自身的形状**：度分布、集中度、漏斗形状、
                           标记与事实是否一致、取值域有没有退化

界线是「问的是哪一类问题」，不是「查的是哪张表」。`verify_enums` 会说 `page_name` 的
15 个值全对，`verify_literals` 会说它们不是占位符——两者都对，而 PV 均匀这件事在它们的
判据里根本不存在。反过来本文件不判任何字面值、不判任何取值集合。

## 判据从哪来

有三类可能的来源，本文件**只用前两类**：

**(a) 随机基线（自校准，不含任何外部数字）。** 所有集中度判据的通过线都写成
「随机基线的若干倍」，基线由样本自己算出来：等概率多项抽样下，父行的子行数近似
Poisson(μ)，μ = 子行总数 / 父行数，于是

    top-k 份额的期望 ≈ k · (1 + z̄_k / √μ)        z̄_k = φ(Φ⁻¹(1−k)) / k
    Gini 的期望       ≈ 1 / √(π·μ)

对现行库逐项核对过：每帖被赞 top10% 实测 0.130 / 公式 0.1294；每用户点赞 0.121 /
0.1208；粉丝数 top1% 0.0143 / 0.0143；粉丝数 Gini 0.087 / 0.0912。**这件事很重要**：
「完全均匀」的基线不是 0.10 而是 0.12~0.13——多项抽样的噪声本身就制造一点集中度。
把通过线写成 0.15 之类的绝对数，会在小样本上误判成「有重尾」。写成基线的倍数则与规模
无关，而且样本越大基线越趋近 k，门槛自动收紧。

**(b) 定义性判据（卡片自己声明的规则，或不可争议的事实）。** 例：

    knowledge/domains/behavior/sessions.md:41 「is_bounce 跳出判定规则：
        page_view_count = 1 → TRUE；> 1 → FALSE」
    knowledge/domains/marketing/push_notifications.md 「状态判断逻辑：
        发送失败 = failure_reason IS NOT NULL；待发送 = scheduled_at IS NOT NULL AND sent_at IS NULL」

这两条不是我的行业印象，是卡片写给 agent 照抄的规则。现行库里第一条 103 个会话全不成立、
第二条在 `failure_reason` 整列为空 + `scheduled_at` 整列为空的情况下**永远返回空集**——
也就是卡片教了 agent 一个在这份数据上恒假的判据。这类判据的通过线是 0，没有可调余地。

**(c) 行业绝对数字（「真实电商跳出率 40–60%」这类）——不用。** 唯一一处沾边的是跳出率，
判的也不是「等于 40–60%」而是「落在 (5%, 90%) 里」，即**这个指标带不带信息**：0 和 1
都意味着 `is_bounce` 这一列没有区分度。判「有信息」而不判「等于某个数」，是因为后者
无法复核。

## 口径

**集中度全部用 LEFT JOIN，零度父行必须进分母。** 这与 `verify_correlation.py` 的
`JOIN` 相反，两处都是刻意的：那边问「已经在消费的人里画像能不能区分消费额」，没下过单的
人不该进分母；这边问「有多少内容没人看」，零赞帖正是信号本身。同一个仓库里两种口径并存，
所以每个报告标题都写清分母是什么。

**所有集中度都从「度数 → 父行数」直方图精确算出**，不抽样、不用 `approx_percentile`。
好处是同一个纯函数 `concentration()` 同时服务 Athena 路径和 CSV 路径——两条路不会漂
（Batch 3 的教训：两侧各写一份实现，一致时你不知道是都对还是都错）。直方图的行数受
最大度数约束，与库的规模无关，所以这套查询在 4 万行和 4 千万行上一样便宜。

**布尔值和时间戳两种写法都要认。** 现行库 CSV 写 `t`/`f`，`scripts/gen` 写 `true`/`false`；
时间戳 `scripts/gen` 的 CSV 里同一张表内混着 `2025-12-13T18:06:33` 和
`2025-12-13 18:24:04` 两种格式（见文末「顺带发现」）。字符串比较会因此给出假的时序违规，
所以本文件一律先 parse 再比。

## 预期结论：两份数据都不全绿，而且各自过的不是同一批

现行库（`data/csv`，v1 生成器）和新生成器（`scripts/gen`，scale 1）的实测：

下表是两份数据各跑一遍的**实测**（`--from-csv data/csv` 和 `--from-csv /tmp/gen4`，
后者 = `scripts/gen/main.py --scale 1 --format csv --seed 42`）。两份数据的四张表
行数完全相同（95,029 行），所以左右两列可以直接对着看：

    检查（通过线）                        现行库 data/csv   scripts/gen scale 1
    每帖被赞集中度  ≥ 2× 基线            0.1299 (1.00×) F  0.1299 (1.00×) F
    零赞帖占比      ≥ 10%                0/1000         F  0/1000         F
    每用户点赞集中度 ≥ 2× 基线            0.1207 (1.00×) F  0.3518 (2.91×) ok
    赞不落在草稿/待审帖上                 3,749 行       F  4,005 行       F
    赞时间 ≥ 发帖时间                     0 行           ok **17,802 行**  F
    互关率 / 随机边密度 ≥ 3               1.008×         F  1.071×         F
    粉丝数集中度    ≥ 3× 基线            0.0143 (1.00×) F  0.0137 (0.96×) F
    Gini(粉丝) − Gini(关注) ≥ +0.10      −0.000         F  **−0.473**     F
    无自关注                              0 行           ok 0 行           ok
    每会话页面数集中度 ≥ 2× 基线          0.1656 (0.97×) F  0.4000 (2.33×) ok
    每用户浏览量集中度 ≥ 2× 基线          0.1628 (1.33×) F  0.4538 (3.70×) ok
    跳出率落在 (5%, 90%)                  0.000          F  0.175          ok
    is_bounce ⟺ 页面数 = 1                103 个不符     F  0 个不符       ok
    home PV / checkout PV ≥ 3             0.985×         F  0.974×         F
    停留时长 top10% 份额 ≥ 0.30           0.183          F  0.433          ok
    corr(停留时长, 滚动深度) ≥ +0.30      +0.0002        F  −0.0009        F
    view_time 落在会话窗口内              1,508 行越界   F  0 行           ok
    推送漏斗单调 + 标记⟷时间戳一致        成立           ok 成立           ok
    推送时序 sched ≤ deliv ≤ open         第一段无输入   ok **13 行**      F
    未送达必须有 failure_reason ≥ 90%     0/470          F  1176/1176      ok
    各 push_type 打开率 max/min ≥ 1.5     1.102×         F  1.176×         F
    每用户推送数集中度 ≥ 2× 基线          0.1423 (1.02×) F  0.4159 (2.99×) ok
    campaign_id 只挂营销型推送            0 行           ok **6,539 行**   F
    page_views.referrer 取值域            整列空         F  **1 个取值**    F
    page_views.page_url 取值域            整列空         F  15 个取值      ok
    push_notifications.scheduled_at 取值域 整列空        F  9,986 个取值   ok
    push_notifications.deep_link 取值域   4,275 个取值   ok **1 个取值**    F

    合计                                  21 FAIL / 27      14 FAIL / 27

**「各自过的不是同一批」这一点是本文件唯一的自证。** 27 条判据里有 **4** 条现行库过
而新生成器不过（赞的时序、推送时序、campaign 归属、deep_link 取值域），**11** 条反过来
（两个人均集中度、会话深度、跳出率两条、停留时长、view_time 窗口、failure_reason、
推送量集中度、page_url、scheduled_at）。所以这一组里不存在「恒红」的判据（那种只是在
描述一个已知缺陷，不携带信息），也不存在「恒绿」的（那种是死掉的灯）。

只有 4 条在两份数据上同为绿（无自关注、推送漏斗嵌套、赞的时序、campaign 归属——后两条
各只在一侧绿）。**这 4 条要留着**：它们是本文件里唯一「现有数据这件事做对了」的正向
结论，删掉就没人证明它还成立。

`--selftest` 不依赖上面这张表，它用构造夹具把「有区分力」对 27 条逐一钉住：同一条判据，
均匀夹具必须红、重尾夹具必须绿，两侧都验（只验一侧证明不了判据有方向）。

**两条路跑出来的 27 个结论逐位相同**（不带 `--from-csv` 查 Athena vs `--from-csv
data/csv`，21 FAIL、每个小数位都一样）。这件事说明的是**两份实现**对得上（Trino 聚合
⟷ Python 逐行），**不**说明数据对——`data/csv` 就是灌进 Athena 的那份，两侧同源，
这正是 `docs/test-plan.md` 里记了六次的「两边都是同一份拷贝」型假绿。数据侧的
CSV ⟷ Athena 等值是 `verify_load.py` 的事，不在这里重复。

## 不接进 scripts/test_all.sh

同 `verify_literals.py` / `verify_semantics.py` / `verify_resolution.py` /
`verify_correlation.py`：现行库不会重灌（决定已定），本文件对它跑必然一片红，而默认
流程里挂一盏永远红的灯，等于把所有人训练成无视红灯。只有 `--selftest` 那一支
（不连云、不读库、纯判据）挂在 L0 上。

## 顺带发现（本批只加检查，一律不修，记录在此）

1. **`scripts/gen` 的 `opened_at` 可能早于 `delivered_at`（scale 1 实测 13/1,885）。**
   `build_push_notifications` 里两个时间戳都从 `sched` 起算：`delivered_at` 偏移
   1~30 分钟、`opened_at` 偏移 2~1440 分钟，两个区间重叠，所以打开可以发生在送达之前。
   现行库没这个问题（它的 `opened_at` 从 `delivered_at` 起算）。
2. **`scripts/gen` 给 `draft` / `under_review` / `deleted` 的帖子也填了
   `published_at`。** 现行库这三类共 153 帖的 `published_at` 全为空（正确）。这就是
   为什么两份数据在「赞的时序」上一红一绿：新生成器的 `published_at` 非空，于是
   「赞早于发帖」这条判据第一次拿到输入，立刻抓到 17,802 行。所以本文件把「赞不落在
   不可见帖上」判在 **`status`** 上而不是 `published_at is null` 上——后者会被这个缺陷
   变成永远绿的灯。
3. **`scripts/gen` 的 CSV 时间戳序列化不一致：** 同一张 `push_notifications.csv` 里
   `scheduled_at` 写 `2025-12-13T18:06:33`（ISO T 分隔），`delivered_at` 写
   `2025-12-13 18:24:04`（空格分隔）。字符串比较下空格 < `T`，于是「送达早于计划」
   会假报 8,756 行。装载走 Trino 的 timestamp 解析，两种都收，所以库里没有问题——
   受害者是任何按文本比较这两列的工具。
4. **`page_views.page_url` / `referrer` 在现行库里是空列**，`push_notifications.
   scheduled_at` / `failure_reason` 也整列为空。`verify_literals.py` 把 `page_url`
   列在 `TYPE_EXEMPT` 里（按字面值**形态**豁免，是对的），所以「整列为空」这件事此前
   无人看管——它不是形态问题，是取值域退化。新生成器修好了三列里的两列，但
   `referrer` 只是从「整列空」变成「54.4% 非空、**只有 1 个取值**」，
   而 `deep_link` 反过来从 4,275 个取值退成了 1 个（`F.const(n, "app://home")`）。
   非空率和取值个数要一起判，只判前者会把这两种情形都放过去。
5. **卡片已经把均匀性写成了「注意事项」。** `knowledge/domains/behavior/page_views.md`
   写着「30,000 行摊在 15 个页面上几乎完全均匀（极差 6%），排不出有意义的热门页面榜」，
   `sessions.md` 对 `traffic_source` / `utm_source` 也这么写。写下来是好事（agent 不会
   拿噪声排名当结论），但副作用是**缺陷被降级成了现状**：没有任何东西会因为它变红，
   于是也没有任何东西会在它被修好或变得更糟时告诉你。这一组把它变回一条会红的判据。

## 量到了但**没有**立判据的几个数

写在报告末尾的「诊断」区，只报数不判。不判的理由逐条如下——设一条无法复核的线，
比不设更坏：

    自赞占比            现行库 0.174% / 新生成器 0.160%，两者都≈随机期望 1/500。
                        产品语义上作者能不能赞自己的帖，各家不同，没有可复核的判据。
    五类推送量 max/min  1.068 / 1.061。事务型推送跟订单量走、营销型跟活动走，等量是
                        可疑的，但「不可能等量」推不出来，所以只报数。
    推送量 ⟷ 消费额     现行库 GMV 最低 1/3 人均 19.98 条、最高 1/3 20.01 条，完全无关。
                        不判方向：真实产品既会向高价值用户加投，也会向流失用户做召回，
                        方向本身是产品策略，不是数据真实性。
    声明列 page_view_count / like_count 的对账
                        属于 L4（跨表数值闭环），审计已记录为 P1，这里不重复造第二份。

用法：

    python3 scripts/lakehouse/verify_behavior.py                    # 查云上现行库
    python3 scripts/lakehouse/verify_behavior.py --from-csv /tmp/gen1
    python3 scripts/lakehouse/verify_behavior.py --selftest         # 判据自测（无云依赖）
    python3 scripts/lakehouse/verify_behavior.py --why              # 连判据理由一起打印

## 全量实测基线（2026-08-28）

`--from-csv` 指向 `scripts/gen/main.py` 的**全量**产出（scale 427）：**27 项全部通过**，
耗时 **119.39s real / 116.88s user**，读 7 张 CSV 共 43,356,843 行（四张行为大表
40,581,089 行）。这是这批判据第一次在目标规模上给出结论，此前所有绿灯都来自 scale 1。

**scale 1 适合迭代判据，但对复杂度天生盲**——改到取数那一段（`_rows` 循环、`load_csv`
里的累加器）就不能只跑 scale 1。这条不是设想：`load_csv` 原先的取值域累加器写成
`col_acc[c] = (cnt + 1, seen | {val})`，`seen | {val}` 是重建集合而非插入，整循环
O(n·k)。四个被查的列里三个只有 9–26 个不同取值，scale 1 几秒跑完；`scheduled_at`
是时间戳、全量 302 万个不同值，实测增长指数 2.25–2.56、全量外推 ≈57 小时 CPU。
改成原地 `add` 后指数回到 0.94–1.04（详见 `load_csv` 里 `col_cnt` 上方的注释）。
**所以上面那个耗时数字是判据的一部分**：下次它从两分钟变成两小时，就是取数码退化了。
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import math
import statistics
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, NamedTuple

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

MARK = {"PASS": "\033[32m  ok  \033[0m", "FAIL": "\033[31m FAIL \033[0m",
        "WEAK": "\033[33m WEAK \033[0m", "NOINPUT": "\033[90m  --  \033[0m"}
CSV_DIR = ROOT / "data" / "csv"

# 不可见状态：草稿和待审帖对外不可见，所以不可能被点赞。判在 status 上而不是
# published_at 上，理由见模块 docstring 的「顺带发现 2」。`deleted` **不在**这里：
# 删帖之前的历史点赞是合理的，删除是后发生的事。
INVISIBLE_POST_STATUS = ("draft", "under_review")

# 营销型推送。只有它由 campaigns（营销活动）触发；order / reminder / social / system
# 是事务型，由订单状态、购物车、社交事件、系统事件触发，不属于任何活动。
MARKETING_PUSH_TYPES = ("promotion",)

# 功效下限。三个数各管一件事，混成一个就分不清「判不动」的原因：
MIN_PARENTS = 30      # 父行太少，集中度全是噪声
MIN_MU = 4.0          # 平均度数太低，Poisson 的正态近似不可靠（基线公式失准）
MIN_CELL = 30         # 分组比值（互关、按类型打开率）的最小格子数


class Th(NamedTuple):
    """一条判据的通过线 + 它凭什么是这个数。`why` 会在该条判据变红时打印。"""
    value: object
    why: str


TH: dict[str, Th] = {
    # ---------------- post_likes ----------------
    "like_post_tail": Th(2.0, """
        每帖被赞数的 top10% 份额 ≥ 随机基线的 2 倍。基线由样本自算（μ = 赞数/帖数），
        现行库 μ=35.7 → 基线 0.1294。内容互动的头部效应是「爆款识别 / 内容热度榜」
        这一整类分析的前提；没有头部，排名就是噪声排名（page_views.md 已经为
        page_name 写过这句提醒）。取 2 倍是「看得出头部」的最低要求：真实 UGC 平台
        的 top10% 内容通常占互动的一半以上，2×基线 ≈ 0.26 远低于它。"""),
    "like_zero_posts": Th(0.10, """
        零赞帖占全部帖子的比例 ≥ 10%。这条不是集中度的重复：Poisson(35.7) 下
        P(度数=0) = e^-35.7 ≈ 10^-16，也就是等概率抽样**结构上不可能**产出零赞帖，
        所以它是一条独立的、判「有没有长尾左端」的判据。真实平台多数内容零互动，
        10% 只是要求「零互动这件事在数据里存在」——「哪些内容没人看」本身就是结论。"""),
    "like_user_tail": Th(2.0, """
        每用户点赞数的 top10% 份额 ≥ 随机基线的 2 倍（分母是**全部用户**，
        没点过赞的人也进分母）。用户互动活跃度的分层是「高互动人群圈选 / 社区
        健康度」的前提。同上，2× 是最低要求。"""),
    "like_invisible": Th(0, """
        落在 status ∈ ('draft','under_review') 的帖子上的赞必须是 0 行。定义性判据：
        草稿和待审帖对外不可见，不存在被点赞的路径。通过线没有可调余地。
        `deleted` 不算违规（历史点赞合理，删除是后发生的）。"""),
    "like_before_publish": Th(0.0, """
        `post_likes.created_at < posts.published_at` 的赞必须是 0 行。定义性判据：
        帖子发布之前不存在可点赞的对象。分母只算 published_at 非空的帖子上的赞；
        全为空时判「无输入」而不是通过。"""),
    # ---------------- user_follows ----------------
    "follow_recip": Th(3.0, """
        互关边占比 / 同一张图完全随机连边时的期望互关率 ≥ 3。**这条完全自校准**：
        分母就是边密度 E/(N(N−1))，即「互关全是巧合」时该指标的值。比值 ≈ 1 是
        随机图的签名，说明生成器没有互惠机制。真实有向社交图的互惠性显著高于密度
        （量级差几十到上千倍），3 倍只是「机制存在」的最低门槛。互惠性是「社群发现 /
        双向关系」这类分析的唯一信号。"""),
    "follow_in_tail": Th(3.0, """
        粉丝数（入度）的 top1% 份额 ≥ 随机基线的 3 倍。大 V 的存在是「KOL 识别 /
        影响力分析」的前提。用 top1% 而不是 top10%：头部效应集中在极少数账号上，
        top10% 会把它摊平。基线同样自算（现行库 μ=38.3 → 0.0143）。"""),
    "follow_gini_gap": Th(0.10, """
        Gini(粉丝数) − Gini(关注数) ≥ 0.10。**这条也完全自校准**：两个分布的 μ 相同，
        所以随机基线下这个差值的期望是 0，采样噪声量级 ~0.01。判据本身是一条结构性
        事实：一个人能关注多少人受精力上限约束（真实社交里关注数有天花板），粉丝数
        没有上限，所以入度的不平等**严格大于**出度的不平等。现行库差值 −0.000（两侧
        都是随机图，持平）；新生成器 −0.473，即方向是**反**的（`unique_pairs` 用
        `fk_skewed` 生成 follower、`fk_uniform` 生成 following，把偏斜给错了一侧）。
        反向比持平更糟：照它算会得出「关注别人的行为比被关注更集中」，
        而且这个结论看起来很像一个发现。"""),
    "follow_self": Th(0, """
        follower_id = following_id 的边必须是 0 行。定义性判据。两份数据都已成立，
        这条钉的是「不许退化」。"""),
    # ---------------- page_views ----------------
    "pv_session_tail": Th(2.0, """
        每会话页面数的 top10% 份额 ≥ 随机基线的 2 倍（分母是**全部会话**）。会话深度
        的重尾是「会话质量分层 / 深度浏览人群」的前提。现行库这一项实测 0.166，
        **低于**随机基线 0.171——因为它是 uniform(2,10)，比 Poisson 还平。"""),
    "pv_user_tail": Th(2.0, """
        每用户页面浏览数的 top10% 份额 ≥ 随机基线的 2 倍（分母是全部用户）。与上一条
        不重复：会话深度和人均活跃度是两个不同的维度，一个用户可以有很多浅会话。
        人均浏览量的重尾是 DAU 质量、粘性分层的前提。"""),
    "pv_bounce_rate": Th((0.05, 0.90), """
        单页会话占全部会话的比例落在 (5%, 90%) 开区间内。判的**不是**「等于真实电商
        的 40–60%」（那个数无法复核），而是这个指标带不带信息：0 和 1 都意味着
        `is_bounce` 这一列没有区分度，跳出率、流量质量、落地页评估全部退化成常数。
        现行库实测 0.000——一个单页会话都没有。"""),
    "pv_bounce_flag": Th(0.0, """
        `sessions.is_bounce` ⟺ 该会话的 page_views 行数 = 1，两个方向都查，违规必须
        是 0。定义性判据，出处是卡片自己：knowledge/domains/behavior/sessions.md:41
        的「is_bounce 跳出判定规则」表写着 page_view_count = 1 → TRUE、> 1 → FALSE。
        现行库 103 个 is_bounce=true 的会话页面数是 2~10，**没有一个是 1**，
        也就是卡片教给 agent 的规则在这份数据上恒假。"""),
    "pv_funnel": Th(3.0, """
        home 的 PV / checkout 的 PV ≥ 3。定义性判据的一种：这个比值就是「首页 → 结算」
        的粗转化率的倒数，3 对应 33%，是任何电商都到不了的上界（真实是个位数百分比），
        所以取 3 不是行业印象而是一个极宽松的**上界**。现行库 0.98——结算页 PV 比首页
        还多，任何页面漏斗都会算出 100% 转化。"""),
    "pv_dwell_tail": Th(0.30, """
        `duration_seconds` 的 top10% 份额 ≥ 0.30。这一项的基线可以精确算：**任何**
        均匀分布 U(a,b) 的 top10% 份额 = 0.1·(b−0.05(b−a))/((a+b)/2)，在 a→0 时取到
        上界 0.19；所以「> 0.19」等价于「不是均匀分布」。取 0.30 留出余量。停留时长的
        重尾是「内容吸引力 / 深度阅读」分析的全部信号。现行库实测 0.183，与
        U(5,120) 的解析值 0.183 逐位吻合——这一列就是均匀抽的。"""),
    "pv_dwell_scroll_corr": Th(0.30, """
        corr(duration_seconds, scroll_depth_pct) ≥ 0.30。两列在任何真实埋点里都强
        正相关（看得久 ⟹ 滚得深）——它们测的是同一件事的两个侧面。相关系数 ≈ 0 意味着
        两列各自独立抽样，于是「用滚动深度交叉验证停留时长」这种最基本的埋点质量
        校验算出的是纯噪声。0.30 是「有关系」的最低要求。"""),
    "pv_in_session": Th(0.0, """
        `page_views.view_time` 必须落在所属会话的 [start_time, start_time+duration]
        窗口内（放 1 秒容差），违规 0 行。定义性判据：一次页面浏览属于某个会话，
        就不可能发生在这个会话之外。现行库 1,508 行越界（5.0%），意味着任何「会话内
        路径 / 页面停留序列」查询（page_views.md 里就有一条这样的示例 SQL）会把
        窗口外的行也算进去。"""),
    # ---------------- push_notifications ----------------
    "push_funnel": Th(0, """
        推送漏斗必须逐级收窄且嵌套：总数 ≥ 送达 ≥ 打开，且「打开但未送达」= 0，
        且布尔标记与时间戳一致（is_delivered ⟺ delivered_at 非空，is_opened 同理）。
        定义性判据。两份数据都已成立，这条钉的是「不许退化」——它是本文件里唯一
        一条「现有数据是对的」的正向结论，删掉它就没人证明这件事还成立。"""),
    "push_ts": Th(0.0, """
        scheduled_at ≤ delivered_at ≤ opened_at，违规 0 行。定义性判据。两段分别算
        分母：某一段的两列有一列整列为空时判「无输入」而不是通过（现行库
        scheduled_at 整列为空，所以第一段无输入——这正是为什么还需要下面那条
        取值域判据）。"""),
    "push_fail_reason": Th(0.90, """
        未送达的行里 `failure_reason` 非空的比例 ≥ 90%。出处是卡片自己：
        knowledge/domains/marketing/push_notifications.md 的「状态判断逻辑」表写着
        「发送失败 = failure_reason IS NOT NULL」。现行库 470 行未送达、
        failure_reason 整列为空——照卡片的规则查，失败数恒为 0，而实际有 470 条。
        留 10% 余量是给「未送达但原因未知」这种真实存在的情形。"""),
    "push_open_by_type": Th(1.5, """
        各 push_type 的打开率 max/min ≥ 1.5（只算送达数 ≥ 30 的类型）。事务型推送
        （order：订单状态、物流）的打开率在任何产品里都显著高于营销型（promotion）
        ——前者是用户在等的信息。五类打开率相等意味着 is_opened 与 push_type 独立
        抽样，于是「哪类推送效果好」这个问题在这份数据上无解。1.5 是「能分辨」的
        最低要求。现行库 1.102，新生成器 1.176。"""),
    "push_user_tail": Th(2.0, """
        每用户推送数的 top10% 份额 ≥ 随机基线的 2 倍（分母是全部用户）。推送量是运营
        决策的产物：按用户价值、生命周期阶段、订阅偏好分层触达。均匀撒意味着
        「推送疲劳 / 触达分层 / 频控」这一类分析全部无解。注意这条判的是**集中度**，
        不判方向（谁多谁少）——方向是产品策略，见 docstring 末尾。"""),
    "push_campaign_scope": Th(0.0, """
        事务型推送（order / reminder / social / system）的 campaign_id 必须为空，
        违规 0 行。`campaign_id` 指向 campaigns（营销活动）表；一条「您的订单已发货」
        不由任何活动触发。把事务型推送也挂上活动 id，`campaigns` 的触达/打开归因会把
        事务型推送算进活动效果，活动 ROI 系统性虚高——而且不报错。现行库成立
        （campaign_id 只出现在 promotion 上）；新生成器 6,539 行违规。"""),
    # ---------------- 通用：取值域退化 ----------------
    "col_domain": Th((0.50, 2), """
        声明为分析入口的列，非空（非空白）率必须 ≥ 50% 且不同取值 ≥ 2 个。一个整列
        为空或只有一个取值的列，分布是退化的：它在 SQL 里可用、不报错、返回一个恒定
        结论。这类列名单在 DEGENERATE_COLS 里逐条声明，附上「它是哪类分析的入口」。
        与 verify_literals.py 不重叠：那边判**值长什么样**（占位符/词沙拉），
        `page_url` 在它那里是 TYPE_EXEMPT；这边判**有多少个不同的值**。"""),
}

# 声明为「分析入口」的列：这几列一旦退化，某一整类问题就静默变成常数答案。
# 每条都要写清是哪类分析的入口，否则这份名单会退化成「我随手想到的几列」。
# 类型标 "text"（空白算空）或 "raw"（只判 NULL）。
DEGENERATE_COLS: dict[str, tuple[str, str]] = {
    "page_views.referrer": ("text", "站外/站内来源页。流量来源、落地页归因的唯一输入"),
    "page_views.page_url": ("text", "页面路径。路径级下钻（同一 page_name 下的不同 URL）的唯一输入"),
    "push_notifications.scheduled_at": ("raw", "计划发送时间。卡片的「待发送」判据"
                                               "（scheduled_at IS NOT NULL AND sent_at IS NULL）只认它"),
    "push_notifications.deep_link": ("text", "点击落地页。推送 → 站内转化归因的唯一输入"),
}


# ---------------------------------------------------------------- 纯统计（判据的地基）

@dataclass(frozen=True)
class Conc:
    """一个「度数 → 父行数」直方图的集中度摘要。judge 函数只吃这个。

    从直方图精确算，与规模无关：`total` 是 4 万还是 4 千万，输入都是几十行。
    """
    n: int = 0                  # 父行数（含度数为 0 的）
    total: int = 0              # 子行总数
    dmax: int = 0
    dmin: int = 0
    p50: int = 0
    zero_frac: float = 0.0
    top1: float = 0.0
    top10: float = 0.0
    gini: float = 0.0

    @property
    def mu(self) -> float:
        return self.total / self.n if self.n else 0.0


def concentration(hist: Mapping[int, int]) -> Conc:
    """把 {度数: 父行数} 算成 Conc。空直方图返回全 0（judge 会判无输入）。"""
    hist = {int(d): int(c) for d, c in hist.items() if int(c) > 0}
    n = sum(hist.values())
    total = sum(d * c for d, c in hist.items())
    if n == 0 or total == 0:
        return Conc(n=n, total=total)
    asc = sorted(hist.items())

    # Gini：S = Σ rank_i · x_i（rank 从 1 开始，升序）。一个 (度数 d, 个数 m) 的桶
    # 占据 rank r+1 … r+m，贡献 d·m·(2r+m+1)/2。整数算术，不逐行展开。
    s, r = 0.0, 0
    for d, m in asc:
        s += d * m * (2 * r + m + 1) / 2.0
        r += m
    gini = 2.0 * s / (n * total) - (n + 1) / n

    def top_share(frac: float) -> float:
        """最大的 k = max(1, ⌊n·frac⌋) 个父行占子行总数的份额（桶可以取一部分）。"""
        k = max(1, int(n * frac))
        got = 0
        for d, m in sorted(hist.items(), reverse=True):
            take = min(m, k)
            got += d * take
            k -= take
            if k == 0:
                break
        return got / total

    # 中位数：第 ⌈n/2⌉ 小
    want, seen, p50 = (n + 1) // 2, 0, asc[0][0]
    for d, m in asc:
        seen += m
        if seen >= want:
            p50 = d
            break
    return Conc(n=n, total=total, dmax=asc[-1][0], dmin=asc[0][0], p50=p50,
                zero_frac=hist.get(0, 0) / n, top1=top_share(0.01),
                top10=top_share(0.10), gini=gini)


def baseline_top_share(frac: float, mu: float) -> float:
    """等概率多项抽样下 top-frac 份额的期望：frac·(1 + z̄/√μ)，z̄ = φ(Φ⁻¹(1−frac))/frac。

    正态近似 Poisson(μ)。μ 小时不准（所以有 MIN_MU 闸），μ 大时趋近 frac。
    """
    if mu <= 0:
        return frac
    nd = statistics.NormalDist()
    ztail = nd.pdf(nd.inv_cdf(1.0 - frac)) / frac
    return frac * (1.0 + ztail / math.sqrt(mu))


def baseline_gini(mu: float) -> float:
    """同上分布下 Gini 的期望：σ/(μ√π) = 1/√(πμ)。"""
    return 1.0 / math.sqrt(math.pi * mu) if mu > 0 else 0.0


def pearson(m: tuple[int, float, float, float, float, float]) -> float:
    """从 (n, Σx, Σy, Σx², Σy², Σxy) 算相关系数。SQL 和 CSV 两路都只送这六个数。"""
    n, sx, sy, sxx, syy, sxy = m
    if n < 2:
        return float("nan")
    cov = sxy / n - (sx / n) * (sy / n)
    vx = sxx / n - (sx / n) ** 2
    vy = syy / n - (sy / n) ** 2
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


# ---------------------------------------------------------------- 事实容器

@dataclass(frozen=True)
class Facts:
    """一次取数的全部结果。judge 只吃这个，所以 Athena / CSV / 构造夹具同权。"""
    src: str = ""
    # post_likes
    likes_total: int = 0
    likes_per_post: dict = field(default_factory=dict)
    likes_per_user: dict = field(default_factory=dict)
    invisible_posts: int = 0
    likes_on_invisible: int = 0
    likes_pub_checkable: int = 0
    likes_before_publish: int = 0
    self_likes: int = 0
    # user_follows
    follow_rows: int = 0
    follow_edges: int = 0
    follow_self: int = 0
    follow_recip: int = 0
    follow_nodes: int = 0
    followers_hist: dict = field(default_factory=dict)
    following_hist: dict = field(default_factory=dict)
    # page_views
    pv_total: int = 0
    pv_per_session: dict = field(default_factory=dict)
    pv_per_user: dict = field(default_factory=dict)
    sessions_total: int = 0
    single_page_sessions: int = 0
    bounce_flagged: int = 0
    bounce_mismatch: int = 0
    page_name_counts: dict = field(default_factory=dict)
    dwell_hist: dict = field(default_factory=dict)
    dwell_scroll: tuple = (0, 0.0, 0.0, 0.0, 0.0, 0.0)
    pv_session_checkable: int = 0
    pv_out_of_session: int = 0
    # push_notifications
    push_total: int = 0
    push_delivered: int = 0
    push_opened: int = 0
    push_opened_undelivered: int = 0
    push_flag_ts_mismatch: int = 0
    push_undelivered: int = 0
    push_undelivered_reasoned: int = 0
    push_ts_deliv_checkable: int = 0
    push_ts_deliv_bad: int = 0
    push_ts_open_checkable: int = 0
    push_ts_open_bad: int = 0
    push_by_type: dict = field(default_factory=dict)   # type -> (n, deliv, open, cid)
    push_per_user: dict = field(default_factory=dict)
    # 通用
    col_domain: dict = field(default_factory=dict)     # "表.列" -> (rows, nonblank, distinct)
    # 只报数不判
    push_type_counts: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 判据（纯函数）
#
# 全部返回 (verdict, 一行说明)。说明里必须带**实测值和通过线**，否则一份 20 条红的
# 报告读不出「差多少」——而「差 3% 还是差 30 倍」决定要不要动手。

def _tail(c: Conc, frac: float, mult: float, what: str) -> tuple[str, str]:
    """集中度判据的公共骨架：与自算的随机基线比。"""
    got = c.top1 if frac == 0.01 else c.top10
    if c.n < MIN_PARENTS or c.total == 0:
        return "NOINPUT", f"{what}：父行 {c.n} 个 / 子行 {c.total} 行，判不了"
    if c.mu < MIN_MU:
        return "WEAK", (f"{what}：平均度数 μ={c.mu:.2f} < {MIN_MU}，"
                        f"随机基线的正态近似在这里不可靠，加数据量再判")
    base = baseline_top_share(frac, c.mu)
    line = base * mult
    v = "PASS" if got >= line else "FAIL"
    return v, (f"{what} top{frac:.0%} 份额 {got:.4f} = 随机基线 {base:.4f} 的 "
               f"{got / base:.2f}×（通过线 {mult:.1f}× = {line:.4f}；"
               f"n={c.n} μ={c.mu:.1f} max={c.dmax} p50={c.p50} min={c.dmin}）")


def judge_like_post_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.likes_per_post), 0.10,
                 TH["like_post_tail"].value, "每帖被赞数（分母=全部帖子）")


def judge_like_zero_posts(f: Facts) -> tuple[str, str]:
    c = concentration(f.likes_per_post)
    if c.n < MIN_PARENTS:
        return "NOINPUT", f"帖子只有 {c.n} 个，判不了"
    line = TH["like_zero_posts"].value
    v = "PASS" if c.zero_frac >= line else "FAIL"
    return v, (f"零赞帖 {round(c.zero_frac * c.n)}/{c.n} = {c.zero_frac:.2%}"
               f"（通过线 {line:.0%}；等概率抽样下 P(0)=e^-{c.mu:.1f}≈0，"
               f"所以这条独立于集中度）")


def judge_like_user_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.likes_per_user), 0.10,
                 TH["like_user_tail"].value, "每用户点赞数（分母=全部用户）")


def judge_like_invisible(f: Facts) -> tuple[str, str]:
    if f.likes_total == 0:
        return "NOINPUT", "没有点赞行"
    if f.invisible_posts == 0:
        return "NOINPUT", (f"没有 status ∈ {INVISIBLE_POST_STATUS} 的帖子，"
                           f"这条判据拿不到输入")
    v = "PASS" if f.likes_on_invisible <= TH["like_invisible"].value else "FAIL"
    return v, (f"落在 {f.invisible_posts} 个草稿/待审帖上的赞 {f.likes_on_invisible} 行"
               f" = 全部赞的 {f.likes_on_invisible / f.likes_total:.2%}（通过线 0 行）")


def judge_like_before_publish(f: Facts) -> tuple[str, str]:
    if f.likes_pub_checkable == 0:
        return "NOINPUT", ("没有一条赞落在 published_at 非空的帖子上，这条判据拿不到"
                           "输入（现行库里 draft/under_review/deleted 的 published_at "
                           "本就为空，是对的）")
    v = "PASS" if f.likes_before_publish <= 0 else "FAIL"
    return v, (f"赞早于发帖 {f.likes_before_publish} 行 / 可判 {f.likes_pub_checkable} 行"
               f" = {f.likes_before_publish / f.likes_pub_checkable:.2%}（通过线 0 行）")


def judge_follow_recip(f: Facts) -> tuple[str, str]:
    n, e = f.follow_nodes, f.follow_edges
    if n < 3 or e == 0:
        return "NOINPUT", f"节点 {n} 个 / 边 {e} 条，判不了"
    density = e / (n * (n - 1))
    got = f.follow_recip / e
    if f.follow_recip < MIN_CELL:
        return "WEAK", (f"互关边只有 {f.follow_recip} 条 < {MIN_CELL}，比值噪声太大")
    ratio = got / density if density > 0 else float("inf")
    line = TH["follow_recip"].value
    v = "PASS" if ratio >= line else "FAIL"
    return v, (f"互关边 {f.follow_recip}/{e} = {got:.4%}，随机边密度 {density:.4%}，"
               f"比值 {ratio:.3f}×（通过线 {line:.1f}×）")


def judge_follow_in_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.followers_hist), 0.01,
                 TH["follow_in_tail"].value, "粉丝数（入度，分母=全部用户）")


def judge_follow_gini_gap(f: Facts) -> tuple[str, str]:
    ci = concentration(f.followers_hist)
    co = concentration(f.following_hist)
    if min(ci.n, co.n) < MIN_PARENTS or min(ci.total, co.total) == 0:
        return "NOINPUT", f"入度父行 {ci.n} / 出度父行 {co.n}，判不了"
    if min(ci.mu, co.mu) < MIN_MU:
        return "WEAK", f"平均度数 μ={min(ci.mu, co.mu):.2f} < {MIN_MU}，判不动"
    gap = ci.gini - co.gini
    line = TH["follow_gini_gap"].value
    v = "PASS" if gap >= line else "FAIL"
    tail = "（方向是反的：出度比入度更不平等）" if gap < 0 else ""
    return v, (f"Gini(粉丝) {ci.gini:.3f} − Gini(关注) {co.gini:.3f} = {gap:+.3f}"
               f"（通过线 +{line:.2f}；随机基线下两者都 ≈ {baseline_gini(ci.mu):.3f}，"
               f"差值期望 0）{tail}")


def judge_follow_self(f: Facts) -> tuple[str, str]:
    if f.follow_rows == 0:
        return "NOINPUT", "没有关注行"
    v = "PASS" if f.follow_self <= TH["follow_self"].value else "FAIL"
    return v, f"自关注 {f.follow_self} 行 / {f.follow_rows} 行（通过线 0 行）"


def judge_pv_session_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.pv_per_session), 0.10,
                 TH["pv_session_tail"].value, "每会话页面数（分母=全部会话）")


def judge_pv_user_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.pv_per_user), 0.10,
                 TH["pv_user_tail"].value, "每用户浏览量（分母=全部用户）")


def judge_pv_bounce_rate(f: Facts) -> tuple[str, str]:
    if f.sessions_total < MIN_PARENTS:
        return "NOINPUT", f"会话只有 {f.sessions_total} 个，判不了"
    got = f.single_page_sessions / f.sessions_total
    lo, hi = TH["pv_bounce_rate"].value
    v = "PASS" if lo < got < hi else "FAIL"
    return v, (f"单页会话 {f.single_page_sessions}/{f.sessions_total} = {got:.3%}"
               f"（通过区间 ({lo:.0%}, {hi:.0%})，判的是「这个指标带不带信息」）")


def judge_pv_bounce_flag(f: Facts) -> tuple[str, str]:
    if f.sessions_total == 0:
        return "NOINPUT", "没有会话"
    v = "PASS" if f.bounce_mismatch <= 0 else "FAIL"
    return v, (f"is_bounce 与「页面数=1」不符 {f.bounce_mismatch}/{f.sessions_total} 个会话"
               f"（is_bounce=true 共 {f.bounce_flagged} 个，单页会话共 "
               f"{f.single_page_sessions} 个；通过线 0，判据出处 sessions.md:41）")


def judge_pv_funnel(f: Facts) -> tuple[str, str]:
    home = f.page_name_counts.get("home", 0)
    ckt = f.page_name_counts.get("checkout", 0)
    if home == 0 or ckt == 0:
        return "NOINPUT", f"home={home} checkout={ckt}，有一边不存在，判不了"
    ratio = home / ckt
    line = TH["pv_funnel"].value
    v = "PASS" if ratio >= line else "FAIL"
    return v, (f"home PV {home} / checkout PV {ckt} = {ratio:.3f}×"
               f"（通过线 {line:.1f}× ⟺ 首页→结算的粗转化率 ≤ {1 / line:.0%}）")


def judge_pv_dwell_tail(f: Facts) -> tuple[str, str]:
    c = concentration({1: 0} if not f.dwell_hist else f.dwell_hist)
    # 这里直方图的语义是「停留秒数 → 行数」，父行 = 浏览行，度数 = 秒数。
    if c.total == 0 or c.n < MIN_PARENTS:
        return "NOINPUT", f"浏览行 {c.n} 行，判不了"
    line = TH["pv_dwell_tail"].value
    v = "PASS" if c.top10 >= line else "FAIL"
    return v, (f"停留时长 top10% 份额 {c.top10:.4f}（通过线 {line:.2f}；"
               f"任何均匀分布的这个值 ≤ 0.19，所以 >0.19 等价于「不是均匀分布」；"
               f"均值 {c.mu:.1f}s max={c.dmax} p50={c.p50} min={c.dmin}）")


def judge_pv_dwell_scroll_corr(f: Facts) -> tuple[str, str]:
    n = f.dwell_scroll[0]
    if n < MIN_PARENTS:
        return "NOINPUT", f"只有 {n} 行，判不了"
    r = pearson(f.dwell_scroll)
    if not math.isfinite(r):
        return "NOINPUT", "有一列是常数，相关系数无定义"
    line = TH["pv_dwell_scroll_corr"].value
    v = "PASS" if r >= line else "FAIL"
    return v, (f"corr(停留时长, 滚动深度) = {r:+.4f}（通过线 +{line:.2f}，n={n}）")


def judge_pv_in_session(f: Facts) -> tuple[str, str]:
    if f.pv_session_checkable == 0:
        return "NOINPUT", "没有能关联到会话的浏览行"
    v = "PASS" if f.pv_out_of_session <= 0 else "FAIL"
    return v, (f"view_time 落在会话窗口外 {f.pv_out_of_session}/{f.pv_session_checkable} 行"
               f" = {f.pv_out_of_session / f.pv_session_checkable:.2%}（通过线 0 行，"
               f"窗口 = [start_time, start_time+duration]，1s 容差）")


def judge_push_funnel(f: Facts) -> tuple[str, str]:
    if f.push_total == 0:
        return "NOINPUT", "没有推送行"
    bad = []
    if not (f.push_total >= f.push_delivered >= f.push_opened):
        bad.append(f"漏斗不单调（{f.push_total} / {f.push_delivered} / {f.push_opened}）")
    if f.push_opened_undelivered:
        bad.append(f"打开但未送达 {f.push_opened_undelivered} 行")
    if f.push_flag_ts_mismatch:
        bad.append(f"布尔标记与时间戳不一致 {f.push_flag_ts_mismatch} 行")
    v = "FAIL" if bad else "PASS"
    detail = "；".join(bad) if bad else (
        f"总 {f.push_total} ≥ 送达 {f.push_delivered} ≥ 打开 {f.push_opened}，"
        f"嵌套与时间戳一致")
    return v, detail


def judge_push_ts(f: Facts) -> tuple[str, str]:
    segs, bad, none = [], 0, []
    for tag, ck, b in (("scheduled≤delivered", f.push_ts_deliv_checkable, f.push_ts_deliv_bad),
                       ("delivered≤opened", f.push_ts_open_checkable, f.push_ts_open_bad)):
        if ck == 0:
            none.append(f"{tag}（两列中有一列无可比行）")
        else:
            segs.append(f"{tag} 违规 {b}/{ck}")
            bad += b
    if not segs:
        return "NOINPUT", "；".join(none) + "，判不了"
    note = "；".join(segs) + (f"；无输入：{'、'.join(none)}" if none else "")
    return ("PASS" if bad == 0 else "FAIL"), note + "（通过线 0 行）"


def judge_push_fail_reason(f: Facts) -> tuple[str, str]:
    if f.push_undelivered == 0:
        return "NOINPUT", "没有未送达的推送，这条判据拿不到输入"
    got = f.push_undelivered_reasoned / f.push_undelivered
    line = TH["push_fail_reason"].value
    v = "PASS" if got >= line else "FAIL"
    return v, (f"未送达 {f.push_undelivered} 行里有 failure_reason 的 "
               f"{f.push_undelivered_reasoned} 行 = {got:.2%}（通过线 {line:.0%}；"
               f"卡片的「发送失败」判据只认 failure_reason IS NOT NULL）")


def judge_push_open_by_type(f: Facts) -> tuple[str, str]:
    rates = {t: o / d for t, (_n, d, o, _c) in f.push_by_type.items()
             if d >= MIN_CELL and o > 0}
    if len(rates) < 2:
        return "NOINPUT", (f"送达数 ≥ {MIN_CELL} 且有打开的 push_type 只有 "
                           f"{len(rates)} 个，判不了")
    hi, lo = max(rates.values()), min(rates.values())
    ratio = hi / lo
    line = TH["push_open_by_type"].value
    v = "PASS" if ratio >= line else "FAIL"
    detail = "、".join(f"{t} {r:.2%}" for t, r in sorted(rates.items(), key=lambda kv: -kv[1]))
    return v, f"打开率 max/min = {ratio:.3f}×（通过线 {line:.1f}×）：{detail}"


def judge_push_user_tail(f: Facts) -> tuple[str, str]:
    return _tail(concentration(f.push_per_user), 0.10,
                 TH["push_user_tail"].value, "每用户推送数（分母=全部用户）")


def judge_push_campaign_scope(f: Facts) -> tuple[str, str]:
    tx = {t: v for t, v in f.push_by_type.items() if t not in MARKETING_PUSH_TYPES}
    if not tx:
        return "NOINPUT", f"没有事务型 push_type（营销型 = {MARKETING_PUSH_TYPES}）"
    bad = sum(v[3] for v in tx.values())
    rows = sum(v[0] for v in tx.values())
    mk = sum(v[3] for t, v in f.push_by_type.items() if t in MARKETING_PUSH_TYPES)
    mkn = sum(v[0] for t, v in f.push_by_type.items() if t in MARKETING_PUSH_TYPES)
    v = "PASS" if bad <= 0 else "FAIL"
    return v, (f"事务型推送带 campaign_id 的 {bad}/{rows} 行（通过线 0 行）；"
               f"营销型 {mk}/{mkn} 行带 campaign_id（这一侧不判，只作对照）")


def judge_col_domain(f: Facts, col: str) -> tuple[str, str]:
    if col not in f.col_domain:
        return "NOINPUT", "没取到这一列"
    rows, nonblank, distinct = f.col_domain[col]
    if rows == 0:
        return "NOINPUT", "表是空的"
    min_rate, min_distinct = TH["col_domain"].value
    rate = nonblank / rows
    ok = rate >= min_rate and distinct >= min_distinct
    why = DEGENERATE_COLS.get(col, ("", ""))[1]
    return ("PASS" if ok else "FAIL"), (
        f"非空 {nonblank}/{rows} = {rate:.1%}（线 {min_rate:.0%}）、"
        f"不同取值 {distinct} 个（线 {min_distinct}）—— {why}")


# 报告顺序 = 检查清单。(组, 标题, 判据 id, 函数)
CHECKS: list[tuple[str, str, str, object]] = [
    ("post_likes", "每帖被赞数的集中度", "like_post_tail", judge_like_post_tail),
    ("post_likes", "零赞帖占比", "like_zero_posts", judge_like_zero_posts),
    ("post_likes", "每用户点赞数的集中度", "like_user_tail", judge_like_user_tail),
    ("post_likes", "赞不落在草稿/待审帖上", "like_invisible", judge_like_invisible),
    ("post_likes", "赞的时间不早于发帖", "like_before_publish", judge_like_before_publish),
    ("user_follows", "互关率 vs 随机边密度", "follow_recip", judge_follow_recip),
    ("user_follows", "粉丝数的头部集中度", "follow_in_tail", judge_follow_in_tail),
    ("user_follows", "粉丝数比关注数更不平等", "follow_gini_gap", judge_follow_gini_gap),
    ("user_follows", "无自关注", "follow_self", judge_follow_self),
    ("page_views", "每会话页面数的集中度", "pv_session_tail", judge_pv_session_tail),
    ("page_views", "每用户浏览量的集中度", "pv_user_tail", judge_pv_user_tail),
    ("page_views", "跳出率带信息", "pv_bounce_rate", judge_pv_bounce_rate),
    ("page_views", "is_bounce ⟺ 页面数=1", "pv_bounce_flag", judge_pv_bounce_flag),
    ("page_views", "home PV 远大于 checkout PV", "pv_funnel", judge_pv_funnel),
    ("page_views", "停留时长是重尾", "pv_dwell_tail", judge_pv_dwell_tail),
    ("page_views", "停留时长与滚动深度正相关", "pv_dwell_scroll_corr",
     judge_pv_dwell_scroll_corr),
    ("page_views", "view_time 落在会话窗口内", "pv_in_session", judge_pv_in_session),
    ("push_notifications", "漏斗单调 + 标记⟷时间戳一致", "push_funnel", judge_push_funnel),
    ("push_notifications", "时间戳逐级递增", "push_ts", judge_push_ts),
    ("push_notifications", "未送达必须有 failure_reason", "push_fail_reason",
     judge_push_fail_reason),
    ("push_notifications", "打开率按类型有差异", "push_open_by_type", judge_push_open_by_type),
    ("push_notifications", "每用户推送数的集中度", "push_user_tail", judge_push_user_tail),
    ("push_notifications", "campaign_id 只挂营销型", "push_campaign_scope",
     judge_push_campaign_scope),
] + [("取值域", f"{c} 没有退化", "col_domain",
      (lambda col: lambda f: judge_col_domain(f, col))(c)) for c in DEGENERATE_COLS]


# ---------------------------------------------------------------- 取数：Athena
#
# 每个查询都只回**聚合后的形状**（直方图或标量），行数与库的规模无关。
# 判读一律不在 SQL 里做——SQL 只聚合，PASS/FAIL 全在上面那些纯函数里，
# 这样 --selftest 测得到真东西。

def _hist(rows) -> dict[int, int]:
    return {int(r[0]): int(r[1]) for r in rows}


def load_athena(client) -> Facts:
    q = client.execute
    inv = ", ".join(f"'{s}'" for s in INVISIBLE_POST_STATUS)

    lpp = _hist(q("WITH c AS (SELECT p.post_id AS k, count(l.post_id) AS cnt "
                  "FROM posts p LEFT JOIN post_likes l ON l.post_id = p.post_id "
                  "GROUP BY p.post_id) "
                  "SELECT cnt, count(*) FROM c GROUP BY cnt")["rows"])
    lpu = _hist(q("WITH c AS (SELECT u.user_id AS k, count(l.user_id) AS cnt "
                  "FROM users u LEFT JOIN post_likes l ON l.user_id = u.user_id "
                  "GROUP BY u.user_id) "
                  "SELECT cnt, count(*) FROM c GROUP BY cnt")["rows"])
    r = q(f"SELECT count(*), "
          f"  sum(CASE WHEN p.status IN ({inv}) THEN 1 ELSE 0 END), "
          f"  sum(CASE WHEN p.published_at IS NOT NULL THEN 1 ELSE 0 END), "
          f"  sum(CASE WHEN p.published_at IS NOT NULL "
          f"           AND l.created_at < p.published_at THEN 1 ELSE 0 END), "
          f"  sum(CASE WHEN p.user_id = l.user_id THEN 1 ELSE 0 END) "
          f"FROM post_likes l JOIN posts p ON p.post_id = l.post_id")["rows"][0]
    likes_total, on_inv, pub_ck, early, selfl = (int(x or 0) for x in r)
    inv_posts = int(q(f"SELECT sum(CASE WHEN status IN ({inv}) THEN 1 ELSE 0 END) "
                      f"FROM posts")["rows"][0][0] or 0)

    r = q("SELECT count(*), count(DISTINCT (follower_id, following_id)), "
          "  sum(CASE WHEN follower_id = following_id THEN 1 ELSE 0 END) "
          "FROM user_follows")["rows"][0]
    f_rows, f_edges, f_self = (int(x or 0) for x in r)
    f_recip = int(q("SELECT count(*) FROM (SELECT DISTINCT follower_id, following_id "
                    "FROM user_follows) a JOIN (SELECT DISTINCT follower_id, following_id "
                    "FROM user_follows) b ON a.follower_id = b.following_id "
                    "AND a.following_id = b.follower_id")["rows"][0][0] or 0)
    f_nodes = int(q("SELECT count(*) FROM users")["rows"][0][0] or 0)
    fol = _hist(q("WITH c AS (SELECT u.user_id AS k, count(f.following_id) AS cnt "
                  "FROM users u LEFT JOIN user_follows f ON f.following_id = u.user_id "
                  "GROUP BY u.user_id) SELECT cnt, count(*) FROM c GROUP BY cnt")["rows"])
    fing = _hist(q("WITH c AS (SELECT u.user_id AS k, count(f.follower_id) AS cnt "
                   "FROM users u LEFT JOIN user_follows f ON f.follower_id = u.user_id "
                   "GROUP BY u.user_id) SELECT cnt, count(*) FROM c GROUP BY cnt")["rows"])

    # 每会话页面数 + is_bounce 一致性：一个查询给全，避免两次扫描口径不同。
    rows = q("WITH c AS (SELECT s.session_id, s.is_bounce, count(v.page_view_id) AS cnt "
             "FROM sessions s LEFT JOIN page_views v ON v.session_id = s.session_id "
             "GROUP BY s.session_id, s.is_bounce) "
             "SELECT cnt, count(*), sum(CASE WHEN is_bounce THEN 1 ELSE 0 END) "
             "FROM c GROUP BY cnt")["rows"]
    pv_sess, sess_total, bounce_flag, single, mism = {}, 0, 0, 0, 0
    for cnt, n, nb in rows:
        cnt, n, nb = int(cnt), int(n), int(nb or 0)
        pv_sess[cnt] = pv_sess.get(cnt, 0) + n
        sess_total += n
        bounce_flag += nb
        if cnt == 1:
            single += n
            mism += n - nb          # 单页却没标 is_bounce
        else:
            mism += nb              # 标了 is_bounce 却不是单页
    pv_user = _hist(q("WITH c AS (SELECT u.user_id AS k, count(v.page_view_id) AS cnt "
                      "FROM users u LEFT JOIN page_views v ON v.user_id = u.user_id "
                      "GROUP BY u.user_id) SELECT cnt, count(*) FROM c "
                      "GROUP BY cnt")["rows"])
    pn = {str(a): int(b) for a, b in
          q("SELECT page_name, count(*) FROM page_views GROUP BY 1")["rows"]}
    dwell = _hist(q("SELECT duration_seconds, count(*) FROM page_views "
                    "WHERE duration_seconds IS NOT NULL GROUP BY 1")["rows"])
    m = q("SELECT count(*), sum(CAST(duration_seconds AS double)), "
          "  sum(CAST(scroll_depth_pct AS double)), "
          "  sum(CAST(duration_seconds AS double) * duration_seconds), "
          "  sum(CAST(scroll_depth_pct AS double) * scroll_depth_pct), "
          "  sum(CAST(duration_seconds AS double) * scroll_depth_pct) "
          "FROM page_views WHERE duration_seconds IS NOT NULL "
          "AND scroll_depth_pct IS NOT NULL")["rows"][0]
    moments = (int(m[0] or 0),) + tuple(float(x or 0.0) for x in m[1:])
    r = q("SELECT count(*), sum(CASE WHEN date_diff('second', s.start_time, v.view_time) < 0 "
          "  OR date_diff('second', s.start_time, v.view_time) > s.duration_seconds + 1 "
          "  THEN 1 ELSE 0 END) "
          "FROM page_views v JOIN sessions s ON s.session_id = v.session_id")["rows"][0]
    pv_ck, pv_oob = int(r[0] or 0), int(r[1] or 0)
    pv_total = int(q("SELECT count(*) FROM page_views")["rows"][0][0] or 0)

    r = q("SELECT count(*), "
          "  sum(CASE WHEN is_delivered THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN is_opened THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN is_opened AND NOT is_delivered THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN (is_delivered AND delivered_at IS NULL) "
          "        OR (NOT is_delivered AND delivered_at IS NOT NULL) "
          "        OR (is_opened AND opened_at IS NULL) "
          "        OR (NOT is_opened AND opened_at IS NOT NULL) THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN NOT is_delivered THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN NOT is_delivered AND failure_reason IS NOT NULL "
          "        AND trim(failure_reason) <> '' THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN scheduled_at IS NOT NULL AND delivered_at IS NOT NULL "
          "        THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN delivered_at < scheduled_at THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN delivered_at IS NOT NULL AND opened_at IS NOT NULL "
          "        THEN 1 ELSE 0 END), "
          "  sum(CASE WHEN opened_at < delivered_at THEN 1 ELSE 0 END) "
          "FROM push_notifications")["rows"][0]
    (p_total, p_del, p_open, p_ou, p_mis, p_und, p_undr,
     p_dck, p_dbad, p_ock, p_obad) = (int(x or 0) for x in r)
    by_type = {str(a): (int(b), int(c or 0), int(d or 0), int(e or 0)) for a, b, c, d, e in
               q("SELECT push_type, count(*), "
                 "  sum(CASE WHEN is_delivered THEN 1 ELSE 0 END), "
                 "  sum(CASE WHEN is_opened THEN 1 ELSE 0 END), "
                 "  count(campaign_id) FROM push_notifications GROUP BY 1")["rows"]}
    p_user = _hist(q("WITH c AS (SELECT u.user_id AS k, count(p.push_id) AS cnt "
                     "FROM users u LEFT JOIN push_notifications p ON p.user_id = u.user_id "
                     "GROUP BY u.user_id) SELECT cnt, count(*) FROM c "
                     "GROUP BY cnt")["rows"])

    col: dict[str, tuple[int, int, int]] = {}
    for table in sorted({c.split(".")[0] for c in DEGENERATE_COLS}):
        cols = [c for c in DEGENERATE_COLS if c.startswith(table + ".")]
        sel = ["count(*)"]
        for c in cols:
            name = c.split(".", 1)[1]
            expr = f"nullif(trim({name}), '')" if DEGENERATE_COLS[c][0] == "text" else name
            sel += [f"count({expr})", f"count(DISTINCT {expr})"]
        r = q(f"SELECT {', '.join(sel)} FROM {table}")["rows"][0]
        rows_n = int(r[0] or 0)
        for i, c in enumerate(cols):
            col[c] = (rows_n, int(r[1 + 2 * i] or 0), int(r[2 + 2 * i] or 0))

    return Facts(
        src="Athena 现行库",
        likes_total=likes_total, likes_per_post=lpp, likes_per_user=lpu,
        invisible_posts=inv_posts, likes_on_invisible=on_inv,
        likes_pub_checkable=pub_ck, likes_before_publish=early, self_likes=selfl,
        follow_rows=f_rows, follow_edges=f_edges, follow_self=f_self,
        follow_recip=f_recip, follow_nodes=f_nodes,
        followers_hist=fol, following_hist=fing,
        pv_total=pv_total, pv_per_session=pv_sess, pv_per_user=pv_user,
        sessions_total=sess_total, single_page_sessions=single,
        bounce_flagged=bounce_flag, bounce_mismatch=mism,
        page_name_counts=pn, dwell_hist=dwell, dwell_scroll=moments,
        pv_session_checkable=pv_ck, pv_out_of_session=pv_oob,
        push_total=p_total, push_delivered=p_del, push_opened=p_open,
        push_opened_undelivered=p_ou, push_flag_ts_mismatch=p_mis,
        push_undelivered=p_und, push_undelivered_reasoned=p_undr,
        push_ts_deliv_checkable=p_dck, push_ts_deliv_bad=p_dbad,
        push_ts_open_checkable=p_ock, push_ts_open_bad=p_obad,
        push_by_type=by_type, push_per_user=p_user, col_domain=col,
        push_type_counts={t: v[0] for t, v in by_type.items()},
    )


# ---------------------------------------------------------------- 取数：CSV
#
# 给 `scripts/gen/main.py --format csv --out DIR` 的产出用，也能读 data/csv。
# 逐行流式，不整表进内存（page_views 在 scale 500 上是 1500 万行）。

def _b(v: str | None) -> bool:
    """布尔：现行库 CSV 写 t/f，scripts/gen 写 true/false。"""
    return (v or "").strip().lower() in ("t", "true", "1", "yes", "y")


def _ts(v: str | None) -> dt.datetime | None:
    """时间戳：`T` 和空格两种分隔都收，小数秒可有可无（理由见 docstring 顺带发现 3）。"""
    s = (v or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _rows(src: Path, name: str):
    p = src / f"{name}.csv"
    if not p.exists():
        raise SystemExit(f"缺 {p}。本文件要 users / posts / post_likes / user_follows / "
                         f"sessions / page_views / push_notifications 七张表。")
    with p.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def load_csv(src: Path) -> Facts:
    src = src.resolve()
    users = [int(r["user_id"]) for r in _rows(src, "users")]
    n_users = len(users)

    posts_status: dict[str, str] = {}
    posts_pub: dict[str, dt.datetime | None] = {}
    posts_owner: dict[str, str] = {}
    for r in _rows(src, "posts"):
        posts_status[r["post_id"]] = r["status"]
        posts_pub[r["post_id"]] = _ts(r.get("published_at"))
        posts_owner[r["post_id"]] = r["user_id"]
    inv_posts = sum(1 for s in posts_status.values() if s in INVISIBLE_POST_STATUS)

    per_post: collections.Counter = collections.Counter()
    per_user: collections.Counter = collections.Counter()
    likes_total = on_inv = pub_ck = early = selfl = 0
    for r in _rows(src, "post_likes"):
        likes_total += 1
        per_post[r["post_id"]] += 1
        per_user[r["user_id"]] += 1
        if posts_status.get(r["post_id"]) in INVISIBLE_POST_STATUS:
            on_inv += 1
        pub = posts_pub.get(r["post_id"])
        if pub is not None:
            pub_ck += 1
            t = _ts(r.get("created_at"))
            if t is not None and t < pub:
                early += 1
        if posts_owner.get(r["post_id"]) == r["user_id"]:
            selfl += 1

    def hist_of(counter: collections.Counter, keys) -> dict[int, int]:
        """把 {父: 子数} 补齐成含 0 度父行的直方图。keys = 全部父行的键。"""
        h: collections.Counter = collections.Counter()
        for k in keys:
            h[counter.get(str(k), 0)] += 1
        return dict(h)

    lpp = hist_of(per_post, posts_status.keys())
    lpu = hist_of(per_user, users)

    edges: set[tuple[str, str]] = set()
    f_rows = f_self = 0
    out_deg: collections.Counter = collections.Counter()
    in_deg: collections.Counter = collections.Counter()
    for r in _rows(src, "user_follows"):
        f_rows += 1
        a, b = r["follower_id"], r["following_id"]
        if a == b:
            f_self += 1
        if (a, b) not in edges:
            edges.add((a, b))
            out_deg[a] += 1
            in_deg[b] += 1
    f_recip = sum(1 for (a, b) in edges if (b, a) in edges)

    sess_start: dict[str, dt.datetime | None] = {}
    sess_dur: dict[str, int] = {}
    sess_bounce: dict[str, bool] = {}
    for r in _rows(src, "sessions"):
        sess_start[r["session_id"]] = _ts(r.get("start_time"))
        try:
            sess_dur[r["session_id"]] = int(float(r.get("duration_seconds") or 0))
        except ValueError:
            sess_dur[r["session_id"]] = 0
        sess_bounce[r["session_id"]] = _b(r.get("is_bounce"))

    pv_sess_cnt: collections.Counter = collections.Counter()
    pv_user_cnt: collections.Counter = collections.Counter()
    pn: collections.Counter = collections.Counter()
    dwell: collections.Counter = collections.Counter()
    n = sx = sy = sxx = syy = sxy = 0
    pv_total = pv_ck = pv_oob = 0
    # 非空计数与不同取值**分开存**，不要用 `(cnt, seen)` 元组每行写回。原先是
    # `col_acc[c] = (cnt + 1, seen | {val})`，而 `seen | {val}` 不是插入、是
    # **重建一个集合并把已攒下的全部元素拷一遍**，单行代价 O(|seen|)、整循环 O(n·k)。
    # 这个写法在小规模上完全看不出来：名单里 `referrer` / `page_url` / `deep_link`
    # 各只有 10–26 个不同值，scale 1 几秒跑完、test_all.sh 一路绿。现形的是
    # `push_notifications.scheduled_at`——它是**时间戳**，全量（scale 427）427 万行
    # 里有 302 万个不同值，累计元素拷贝 4.6×10¹² 次。实测增长指数 2.25–2.56
    # （scale 4/12/40 耗时 7.0s → 83.2s → 1819.3s），全量外推 ≈57 小时 CPU，
    # 等于这条路在千万行量级不可用。改成原地 add 后复杂度回到 O(n)。
    # 只有 len() 被消费（见下面 `col` 的构造），所以原地插入与重建结果逐字相同。
    col_cnt: dict[str, int] = {c: 0 for c in DEGENERATE_COLS}
    col_seen: dict[str, set] = {c: set() for c in DEGENERATE_COLS}
    # 列名单和 kind 在行循环里是常量：按表预分组一次，别每行重做 items() + split()。
    deg_by_table: dict[str, list[tuple[str, str, str]]] = {}
    for _c, (_kind, _) in DEGENERATE_COLS.items():
        _t, _n = _c.split(".", 1)
        deg_by_table.setdefault(_t, []).append((_c, _n, _kind))
    deg_pv = deg_by_table.get("page_views", [])
    deg_push = deg_by_table.get("push_notifications", [])
    for r in _rows(src, "page_views"):
        pv_total += 1
        pv_sess_cnt[r["session_id"]] += 1
        pv_user_cnt[r["user_id"]] += 1
        pn[r["page_name"]] += 1
        d = r.get("duration_seconds") or ""
        s = r.get("scroll_depth_pct") or ""
        if d.strip():
            dv = int(float(d))
            dwell[dv] += 1
            if s.strip():
                sv = int(float(s))
                n += 1
                sx += dv
                sy += sv
                sxx += dv * dv
                syy += sv * sv
                sxy += dv * sv
        st = sess_start.get(r["session_id"])
        if st is not None:
            pv_ck += 1
            t = _ts(r.get("view_time"))
            if t is None or not (st <= t <= st + dt.timedelta(
                    seconds=sess_dur.get(r["session_id"], 0) + 1)):
                pv_oob += 1
        for c, name, kind in deg_pv:
            raw = r.get(name)
            val = (raw or "").strip() if kind == "text" else (raw or "")
            if val:
                col_cnt[c] += 1
                col_seen[c].add(val)

    pv_sess_hist: collections.Counter = collections.Counter()
    single = bounce_flag = mism = 0
    for sid in sess_start:
        cnt = pv_sess_cnt.get(sid, 0)
        pv_sess_hist[cnt] += 1
        flag = sess_bounce.get(sid, False)
        bounce_flag += 1 if flag else 0
        if cnt == 1:
            single += 1
        if flag != (cnt == 1):
            mism += 1
    pv_user_hist = hist_of(pv_user_cnt, users)

    p_total = p_del = p_open = p_ou = p_mis = p_und = p_undr = 0
    p_dck = p_dbad = p_ock = p_obad = 0
    by_type: dict[str, list[int]] = {}
    p_user_cnt: collections.Counter = collections.Counter()
    for r in _rows(src, "push_notifications"):
        p_total += 1
        p_user_cnt[r["user_id"]] += 1
        dl, op = _b(r.get("is_delivered")), _b(r.get("is_opened"))
        p_del += dl
        p_open += op
        p_ou += op and not dl
        d_at, o_at, s_at = (_ts(r.get("delivered_at")), _ts(r.get("opened_at")),
                            _ts(r.get("scheduled_at")))
        if (dl and d_at is None) or (not dl and d_at is not None) \
                or (op and o_at is None) or (not op and o_at is not None):
            p_mis += 1
        if not dl:
            p_und += 1
            if (r.get("failure_reason") or "").strip():
                p_undr += 1
        if s_at is not None and d_at is not None:
            p_dck += 1
            p_dbad += d_at < s_at
        if d_at is not None and o_at is not None:
            p_ock += 1
            p_obad += o_at < d_at
        t = r["push_type"]
        acc = by_type.setdefault(t, [0, 0, 0, 0])
        acc[0] += 1
        acc[1] += dl
        acc[2] += op
        acc[3] += 1 if (r.get("campaign_id") or "").strip() else 0
        for c, name, kind in deg_push:
            raw = r.get(name)
            val = (raw or "").strip() if kind == "text" else (raw or "")
            if val:
                col_cnt[c] += 1
                col_seen[c].add(val)

    rows_of = {"page_views": pv_total, "push_notifications": p_total}
    # 与 load_athena 同形：(表行数, 非空行数, 不同取值数)。
    col = {c: (rows_of[c.split(".")[0]], col_cnt[c], len(col_seen[c]))
           for c in DEGENERATE_COLS}

    return Facts(
        src=f"生成器产出 {src}",
        likes_total=likes_total, likes_per_post=lpp, likes_per_user=lpu,
        invisible_posts=inv_posts, likes_on_invisible=on_inv,
        likes_pub_checkable=pub_ck, likes_before_publish=early, self_likes=selfl,
        follow_rows=f_rows, follow_edges=len(edges), follow_self=f_self,
        follow_recip=f_recip, follow_nodes=n_users,
        followers_hist=hist_of(in_deg, users), following_hist=hist_of(out_deg, users),
        pv_total=pv_total, pv_per_session=dict(pv_sess_hist), pv_per_user=pv_user_hist,
        sessions_total=len(sess_start), single_page_sessions=single,
        bounce_flagged=bounce_flag, bounce_mismatch=mism,
        page_name_counts=dict(pn), dwell_hist=dict(dwell),
        dwell_scroll=(n, float(sx), float(sy), float(sxx), float(syy), float(sxy)),
        pv_session_checkable=pv_ck, pv_out_of_session=pv_oob,
        push_total=p_total, push_delivered=p_del, push_opened=p_open,
        push_opened_undelivered=p_ou, push_flag_ts_mismatch=p_mis,
        push_undelivered=p_und, push_undelivered_reasoned=p_undr,
        push_ts_deliv_checkable=p_dck, push_ts_deliv_bad=p_dbad,
        push_ts_open_checkable=p_ock, push_ts_open_bad=p_obad,
        push_by_type={t: tuple(v) for t, v in by_type.items()},
        push_per_user=hist_of(p_user_cnt, users), col_domain=col,
        push_type_counts={t: v[0] for t, v in by_type.items()},
    )


# ---------------------------------------------------------------- 报告

def run(f: Facts, show_why: bool = False) -> tuple[int, int]:
    counts: collections.Counter = collections.Counter()
    group = None
    for grp, title, tid, fn in CHECKS:
        if grp != group:
            print(f"\n  \033[1m{grp}\033[0m")
            group = grp
        verdict, note = fn(f)
        counts[verdict] += 1
        print(f"  [{MARK[verdict]}] {title}")
        for line in textwrap.wrap(note, 92):
            print(f"          {line}")
        # 失败的那条一定把判据理由打出来：一份 20 条红的报告，光有数字读不出
        # 「这条线凭什么是这个数」，而那正是要不要动手改生成器的依据。
        if show_why or verdict in ("FAIL", "WEAK"):
            for i, line in enumerate(textwrap.wrap(" ".join(TH[tid].why.split()), 86)):
                head = "判据：" if i == 0 else "      "
                print(f"          \033[90m{head}{line}\033[0m")
    print("\n  \033[1m诊断（只报数，不判）\033[0m")
    if f.likes_total:
        print(f"          自赞 {f.self_likes} 行 = {f.self_likes / f.likes_total:.3%}"
              f"（随机配对的期望 ≈ 1/作者数）")
    if f.push_type_counts:
        v = sorted(f.push_type_counts.values())
        print(f"          五类推送量 {dict(sorted(f.push_type_counts.items()))}，"
              f"max/min = {v[-1] / v[0]:.3f}" if v[0] else "          push_type 有空类")
    c = concentration(f.pv_per_session)
    print(f"          每会话页面数 max={c.dmax} p50={c.p50} min={c.dmin}；"
          f"每会话事件/页面的声明列对账属于 L4，不在这里重复")
    return counts["FAIL"], counts["WEAK"]


def main() -> int:
    ap = argparse.ArgumentParser(description="L5 分布真实性 · 四张行为大表")
    ap.add_argument("--from-csv", type=Path, metavar="DIR",
                    help="读 CSV 目录而不是查云上库（data/csv 或生成器 --out 的产出）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（无云依赖）")
    ap.add_argument("--why", action="store_true", help="每条判据都打印理由，不只失败的那些")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    if a.from_csv:
        f = load_csv(a.from_csv)
    else:
        import athena                                   # noqa: PLC0415  只有云路径要
        f = load_athena(athena.Client())
    print(f"=== L5 分布真实性 · post_likes / page_views / user_follows / "
          f"push_notifications · {f.src} ===")
    print(f"    规模：赞 {f.likes_total} · 关注 {f.follow_rows} · 浏览 {f.pv_total} · "
          f"推送 {f.push_total}，合计 "
          f"{f.likes_total + f.follow_rows + f.pv_total + f.push_total} 行")
    print("    口径：集中度一律 LEFT JOIN（零度父行进分母），通过线写成**自算随机基线**"
          "的倍数，不写死绝对数")
    fails, weaks = run(f, a.why)

    print()
    if fails:
        print(f"L5 行为大表：{fails} 项 FAIL"
              + (f" · {weaks} 项 WEAK（判不动）" if weaks else "")
              + f" / 共 {len(CHECKS)} 项")
        if not a.from_csv:
            print("  现行库是 v1 生成器那批数据，项目已决定不重灌，所以这些 FAIL 是对现状"
                  "的记录。\n  生成侧：verify_behavior.py --from-csv <生成目录>。")
    elif weaks:
        print(f"L5 行为大表：0 项 FAIL，但 {weaks} 项 WEAK —— **这不是全绿**。"
              f"\n  WEAK 的意思是这批数据分辨不出通过线那么大的差异，加样本量再判。")
    else:
        print(f"L5 行为大表：{len(CHECKS)} 项全部通过 ✅")
    return 0                    # 诊断工具，不是闸门。理由同 verify_literals.py


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判据自测。四组，每组都必须**两侧都验**。

    只验一侧证明不了判据有区分力（Batch 1 的教训）。这里对 27 条判据里的每一条都要求
    「一份夹具红、一份夹具绿」，另外单独验三件容易悄悄坏掉的事：
      · concentration() 的统计量与逐行朴素实现在随机直方图上逐位相等；
      · baseline_top_share() 与蒙特卡洛无关，是解析式——用**审计实测值**核它；
      · WEAK 与 FAIL 分得开（同一组形状，只改 μ / 格子数）。
    """
    bad = 0

    def expect(tag: str, got, want) -> None:
        nonlocal bad
        if got == want:
            print(f"  ok  {tag}")
        else:
            bad += 1
            print(f"  FAIL {tag}\n       期望 {want}，实际 {got}")

    # ---- 一、concentration()：与朴素实现逐位相等 ----
    print("---- concentration() 对齐朴素实现 ----")

    def naive(hist):
        v = []
        for d, m in hist.items():
            v += [d] * m
        v.sort()
        nn, tot = len(v), sum(v)
        k1, k10 = max(1, int(nn * 0.01)), max(1, int(nn * 0.10))
        s = sum((i + 1) * x for i, x in enumerate(v))
        return (nn, tot, v[-1], v[0], v[(nn + 1) // 2 - 1],
                v.count(0) / nn, sum(v[-k1:]) / tot, sum(v[-k10:]) / tot,
                2.0 * s / (nn * tot) - (nn + 1) / nn)

    rng = 12345
    for case in range(6):
        h = {}
        for d in range(0, 40):
            rng = (rng * 1103515245 + 12345) % (2 ** 31)
            h[d] = rng % (7 + case * 3)
        h[0] = h.get(0, 0) + case            # 保证有些 case 有零度父行
        if sum(d * m for d, m in h.items()) == 0:
            continue
        c = concentration(h)
        got = (c.n, c.total, c.dmax, c.dmin, c.p50, c.zero_frac, c.top1, c.top10, c.gini)
        ref = naive(h)
        same = all(abs(a - b) < 1e-9 if isinstance(a, float) else a == b
                   for a, b in zip(got, ref))
        expect(f"随机直方图 #{case}（n={c.n} total={c.total}）九个统计量逐位相等", same, True)

    # ---- 二、随机基线公式 ⟷ 现行库实测 ----
    # 公式若写错，所有集中度判据的通过线一起错，而报告照样打印得很像样。用四个**实测**
    # 值核它：这些数是从 data/csv 量出来的，不是从公式反推的。
    print("\n---- 随机基线公式 ⟷ 现行库实测（四个独立的点）----")
    for tag, frac, mu, measured in (
            ("每帖被赞 top10%", 0.10, 35668 / 1000, 0.130),
            ("每用户点赞 top10%", 0.10, 35668 / 500, 0.121),
            ("粉丝数 top1%", 0.01, 19165 / 500, 0.0143),
            ("每会话页面数 top10%", 0.10, 30196 / 5000, 0.166)):
        pred = baseline_top_share(frac, mu)
        expect(f"{tag}：公式 {pred:.4f} ⟷ 实测 {measured:.4f}（相对误差 < 5%）",
               abs(pred - measured) / measured < 0.05, True)
    expect(f"粉丝数 Gini：公式 {baseline_gini(19165 / 500):.4f} ⟷ 实测 0.087"
           f"（相对误差 < 10%）",
           abs(baseline_gini(19165 / 500) - 0.087) / 0.087 < 0.10, True)

    # ---- 三、每条判据：坏夹具必须红、好夹具必须绿 ----
    # 夹具刻意造得极端（均匀 vs 强重尾），因为要验的是**判据的方向**，不是它的精度。
    print("\n---- 27 条判据各自的红/绿两侧 ----")

    def flat_hist(nparent: int, deg: int) -> dict[int, int]:
        """完全均匀：每个父行同一个度数。集中度必然 = 基线以下。"""
        return {deg: nparent}

    def heavy_hist(nparent: int, total: int) -> dict[int, int]:
        """强重尾：1% 的父行拿走 60%，9% 拿 25%，其余 90% 分 15%（含一批零度）。"""
        a = max(1, nparent // 100)
        b = max(1, nparent // 10) - a
        c = nparent - a - b
        zero = c // 2
        return {total * 60 // 100 // a: a, total * 25 // 100 // b: b,
                max(1, total * 15 // 100 // max(1, c - zero)): c - zero, 0: zero}

    NP, TOT = 1000, 40_000
    bad_f = Facts(
        src="夹具·全均匀",
        likes_total=TOT, likes_per_post=flat_hist(NP, TOT // NP),
        likes_per_user=flat_hist(500, TOT // 500),
        invisible_posts=100, likes_on_invisible=3749,
        likes_pub_checkable=TOT, likes_before_publish=17802, self_likes=62,
        follow_rows=19165, follow_edges=19165, follow_self=7, follow_recip=1484,
        follow_nodes=500, followers_hist=flat_hist(500, 38),
        following_hist=flat_hist(500, 38),
        pv_total=30196, pv_per_session=flat_hist(5000, 6),
        pv_per_user=flat_hist(500, 60), sessions_total=5000,
        single_page_sessions=0, bounce_flagged=103, bounce_mismatch=103,
        page_name_counts={"home": 2026, "checkout": 2057},
        dwell_hist={d: 260 for d in range(5, 121)},
        dwell_scroll=(30196, 30196 * 62.6, 30196 * 60.0,
                      30196 * (62.6 ** 2 + 1100.0), 30196 * (60.0 ** 2 + 700.0),
                      30196 * 62.6 * 60.0),                       # 协方差 = 0
        pv_session_checkable=30196, pv_out_of_session=1508,
        push_total=10000, push_delivered=9530, push_opened=1435,
        push_opened_undelivered=5, push_flag_ts_mismatch=3,
        push_undelivered=470, push_undelivered_reasoned=0,
        push_ts_deliv_checkable=9530, push_ts_deliv_bad=11,
        push_ts_open_checkable=1435, push_ts_open_bad=13,
        push_by_type={"order": (1967, 1884, 276, 12), "promotion": (1999, 1894, 301, 1606),
                      "reminder": (2066, 1969, 307, 0), "social": (2033, 1934, 279, 0),
                      "system": (1935, 1849, 272, 0)},
        push_per_user=flat_hist(500, 20),
        col_domain={"page_views.referrer": (30196, 30196, 1),
                    "page_views.page_url": (30196, 0, 0),
                    "push_notifications.scheduled_at": (10000, 0, 0),
                    "push_notifications.deep_link": (10000, 10000, 1)},
        push_type_counts={"order": 1967},
    )
    good_f = Facts(
        src="夹具·重尾且自洽",
        likes_total=TOT, likes_per_post=heavy_hist(NP, TOT),
        likes_per_user=heavy_hist(500, TOT),
        invisible_posts=100, likes_on_invisible=0,
        likes_pub_checkable=TOT, likes_before_publish=0, self_likes=0,
        follow_rows=19165, follow_edges=19165, follow_self=0, follow_recip=6000,
        follow_nodes=500, followers_hist=heavy_hist(500, 19165),
        following_hist=flat_hist(500, 38),
        pv_total=30196, pv_per_session=heavy_hist(5000, 30196),
        pv_per_user=heavy_hist(500, 30196), sessions_total=5000,
        single_page_sessions=2200, bounce_flagged=2200, bounce_mismatch=0,
        page_name_counts={"home": 9000, "checkout": 1200},
        # 重尾停留时长：90% 落 10s，10% 落 600s → top10% 份额 0.87
        dwell_hist={10: 27176, 600: 3020},
        dwell_scroll=(30196, 30196 * 60.0, 30196 * 50.0,
                      30196 * (60.0 ** 2 + 400.0), 30196 * (50.0 ** 2 + 400.0),
                      30196 * (60.0 * 50.0 + 240.0)),             # r = 0.6
        pv_session_checkable=30196, pv_out_of_session=0,
        push_total=10000, push_delivered=8800, push_opened=900,
        push_opened_undelivered=0, push_flag_ts_mismatch=0,
        push_undelivered=1200, push_undelivered_reasoned=1200,
        push_ts_deliv_checkable=8800, push_ts_deliv_bad=0,
        push_ts_open_checkable=900, push_ts_open_bad=0,
        push_by_type={"order": (2000, 1800, 400, 0), "promotion": (2000, 1800, 100, 1700),
                      "reminder": (2000, 1750, 200, 0), "social": (2000, 1720, 150, 0),
                      "system": (2000, 1730, 120, 0)},
        push_per_user=heavy_hist(500, 10000),
        col_domain={"page_views.referrer": (30196, 20000, 12),
                    "page_views.page_url": (30196, 30196, 15),
                    "push_notifications.scheduled_at": (10000, 10000, 9000),
                    "push_notifications.deep_link": (10000, 10000, 40)},
        push_type_counts={"order": 2000},
    )
    for grp, title, tid, fn in CHECKS:
        expect(f"{grp} · {title} → 均匀夹具判 FAIL", fn(bad_f)[0], "FAIL")
        expect(f"{grp} · {title} → 重尾夹具判 PASS", fn(good_f)[0], "PASS")

    # ---- 四、WEAK / NOINPUT 不许当通过 ----
    print("\n---- WEAK ⟷ FAIL ⟷ NOINPUT 三者分得开 ----")
    # μ 低于 MIN_MU：同一个「完全均匀」的形状，只把平均度数压到 2，就该判 WEAK 而不是
    # FAIL——基线公式在那里不可靠，报 FAIL 会把人推去改生成器，而生成器可能是对的。
    expect(f"均匀 + μ=2 (< {MIN_MU}) → WEAK",
           judge_like_post_tail(Facts(likes_total=2000,
                                      likes_per_post=flat_hist(1000, 2)))[0], "WEAK")
    expect("均匀 + μ=40 → FAIL（证明上一条不是恒 WEAK）",
           judge_like_post_tail(Facts(likes_total=40000,
                                      likes_per_post=flat_hist(1000, 40)))[0], "FAIL")
    expect(f"父行只有 {MIN_PARENTS - 1} 个 → NOINPUT",
           judge_like_post_tail(Facts(likes_total=1160,
                                      likes_per_post=flat_hist(MIN_PARENTS - 1, 40)))[0],
           "NOINPUT")
    expect("没有草稿/待审帖 → 「赞不落在不可见帖上」判 NOINPUT 而不是 PASS",
           judge_like_invisible(Facts(likes_total=100, invisible_posts=0,
                                      likes_on_invisible=0))[0], "NOINPUT")
    expect("published_at 整列为空 → 「赞的时间」判 NOINPUT 而不是 PASS",
           judge_like_before_publish(Facts(likes_total=100, likes_pub_checkable=0))[0],
           "NOINPUT")
    expect("scheduled_at 整列为空 → 推送时序只判还剩的那一段（有违规 → FAIL）",
           judge_push_ts(Facts(push_total=10, push_ts_deliv_checkable=0,
                               push_ts_open_checkable=100, push_ts_open_bad=13))[0], "FAIL")
    expect("两段都无可比行 → 推送时序判 NOINPUT",
           judge_push_ts(Facts(push_total=10))[0], "NOINPUT")
    expect(f"互关边 < {MIN_CELL} → WEAK 而不是 FAIL",
           judge_follow_recip(Facts(follow_nodes=500, follow_edges=200,
                                    follow_recip=3, follow_rows=200))[0], "WEAK")

    # ---- 五、阈值必须是闭的 ----
    print("\n---- 边界：通过线是闭的（≥ 算过）----")
    # 造一个 top10% 份额恰好压在通过线上的直方图：n=1000，头部 100 个父行度数 h、
    # 其余 900 个度数 5（尾部取 5 而不是 1，是为了让 μ 稳定在 MIN_MU 以上——否则
    # 这条边界测试会撞上功效闸判 WEAK，测的就不是「线闭不闭」了）。线扫找 h。
    mult = TH["like_post_tail"].value

    def crosses(h: int) -> bool:
        c = concentration({h: 100, 5: 900})
        return c.top10 >= baseline_top_share(0.10, c.mu) * mult

    head = next((h for h in range(6, 20000) if crosses(h)), None)
    expect("能扫到恰好越线的头部度数", head is not None, True)
    if head:
        expect(f"恰好越线（头部度数 {head}）→ PASS",
               judge_like_post_tail(Facts(likes_per_post={head: 100, 5: 900}))[0], "PASS")
        expect(f"差一档（{head - 1}）→ FAIL",
               judge_like_post_tail(Facts(likes_per_post={head - 1: 100, 5: 900}))[0],
               "FAIL")
        expect("边界两侧的 μ 都在功效闸以上（否则上面两条测的不是「线闭不闭」）",
               concentration({head - 1: 100, 5: 900}).mu >= MIN_MU, True)

    # ---- 六、声明的列名单必须和判据对得上 ----
    print("\n---- 声明一致性 ----")
    expect("CHECKS 里每条判据都在 TH 里有通过线和理由",
           sorted({t for _g, _t, t, _f in CHECKS} - set(TH)), [])
    expect("TH 里每条通过线都被某条判据用到",
           sorted(set(TH) - {t for _g, _t, t, _f in CHECKS}), [])
    expect("DEGENERATE_COLS 的每一列都进了 CHECKS",
           sorted(c for c in DEGENERATE_COLS
                  if not any(c in title for _g, title, _t, _f in CHECKS)), [])
    expect("DEGENERATE_COLS 的类型标记只有 text / raw",
           sorted({k for k, _ in DEGENERATE_COLS.values()} - {"text", "raw"}), [])

    print(f"\n{'全部通过' if not bad else f'{bad} 项 FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
