"""向量化列填充器 —— 声明式 spec 里每种 kind 的实现。

## 确定性契约（最重要的一条）

每列的数据只由 `(seed, 表名, 列名, 块序号)` 决定，**与生成顺序、并行度无关**。
实现方式是 `rng_for()` 用 SeedSequence 按名字的 crc32 派生独立子流，而不是共用一个
可变 Generator。好处：
- 改某一列的 spec 不会扰动其他列的数据（共用 Generator 时会整条流错位）
- 分块生成、乃至将来并行分块，结果都一样
- 同 seed 重跑逐字节相同，mart 物化值才能当 oracle ground truth

## 业务信号在哪

放大规模最容易丢的就是分布形状：几千万行如果是均匀随机，所有分析题都答不出洞察。
带信号的填充器是 `ts_window`（周内波动 + 月度趋势 + 小时曲线）、`fk_skewed`
（少数用户贡献大头，帕累托）、`enum`（加权枚举）、`decimal_lognorm`（金额右偏长尾）。
"""
from __future__ import annotations

import statistics
import zlib

import numpy as np

# ---------------------------------------------------------------- 确定性随机源

def _key(x) -> int:
    return zlib.crc32(str(x).encode("utf-8"))


def rng_for(seed: int, *keys) -> np.random.Generator:
    """由 (seed, keys...) 稳定派生一个独立 Generator。与调用顺序无关。"""
    return np.random.default_rng(np.random.SeedSequence([int(seed), *(_key(k) for k in keys)]))


# ---------------------------------------------------------------- 主键 / 外键

def pk(n: int, start: int = 1) -> np.ndarray:
    """连续 int64 主键区间。子表引用父表时只需要 (start, n)，不必持有父表数组。"""
    return np.arange(start, start + n, dtype=np.int64)


def fk_uniform(rng: np.random.Generator, n: int, parent_start: int, parent_n: int) -> np.ndarray:
    """均匀外键。"""
    return (parent_start + rng.integers(0, parent_n, size=n)).astype(np.int64)


# 集中度用**对数正态权重**，不用 Zipf(rank^-alpha)。
#
# 为什么不用 Zipf：alpha > 1 时级数收敛，排第一的父实体独占 1/ζ(alpha) 的子行
# （alpha=1.3 → 约 25%），top20% 冲到 96%、尾部空掉，复购率/分群/cohort 全部退化。
# 退到 alpha < 1 虽然 top20% 正常了，但头部仍然过重（N=5000 时 top-1 占 2.9%），
# 而且 top-1 份额随父实体数漂移，不是稳定的建模量。
#
# 对数正态更贴合真实的用户活跃度/商品热度：一样的长尾，头部不独占。
# 洛伦兹曲线有闭式解，top-p 份额 = Φ(Φ⁻¹(p) + σ)，与父实体数无关：
#     σ=1.15 → top20% ≈ 62%（电商真实量级，本默认值）
#     σ=0.80 → top20% ≈ 52%（偏平）
#     σ=1.60 → top20% ≈ 74%（偏陡）
#
# 注：genlib.rng.power_law_counts 用的正是 rank^-alpha 且默认 alpha=1.3，
# 照默认值调用会造出退化分布；本模块不复用它。
SIGMA_DEFAULT = 1.15


def _skew_weights(rng: np.random.Generator, parent_n: int, sigma: float) -> np.ndarray:
    """对数正态权重，已归一化。父实体的「受欢迎度」。"""
    w = rng.lognormal(mean=0.0, sigma=sigma, size=parent_n)
    return w / w.sum()


def fk_skewed(rng: np.random.Generator, n: int, parent_start: int, parent_n: int,
              sigma: float = SIGMA_DEFAULT) -> np.ndarray:
    """长尾外键：少数父实体拿到较多子行（用户活跃度、商品热度）。

    帕累托信号的来源。sigma 的取值含义见上方 SIGMA_DEFAULT 注释。
    """
    p = _skew_weights(rng, parent_n, sigma)
    idx = rng.choice(parent_n, size=n, replace=True, p=p)
    return (parent_start + idx).astype(np.int64)


def children_per_parent(rng: np.random.Generator, parent_n: int, total: int,
                        sigma: float = SIGMA_DEFAULT, min_count: int = 0) -> np.ndarray:
    """把 total 个子行按长尾分给 parent_n 个父行，返回每父的子数（和恰为 total）。

    配 `expand_ids` 用于订单行、帖子评论这类严格的父展子（每个子行必须属于一个父）。
    """
    p = _skew_weights(rng, parent_n, sigma)
    remaining = max(total - parent_n * min_count, 0)
    return rng.multinomial(remaining, p) + min_count


def expand_ids(parent_ids: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """按子计数展开父 id（np.repeat，O(N)）。counts.sum() 即结果长度。"""
    return np.repeat(parent_ids, counts)


def unique_pairs(rng: np.random.Generator, n: int, a_start: int, a_n: int,
                 b_start: int, b_n: int, sigma: float = SIGMA_DEFAULT,
                 sigma_b: float = 0.0, recip: float = 0.0
                 ) -> tuple[np.ndarray, np.ndarray]:
    """生成 n 对互不相同、且两端不相等的 (a, b)。用于关注关系这类无向/有向唯一边。

    做法：超量抽样 → 复合键去重 → 去自环 → 截断到 n。超采样系数按经验 1.6 起，
    不够就加倍重试，避免稀疏图上死循环。

    ## 两端各自的集中度必须能分开设（sigma / sigma_b）

    原来只有一个 `sigma`，且**固定作用在 a 端**、b 端一律 `fk_uniform`。用在
    `post_likes(user_id, post_id)` 上就是「少数用户点很多赞、而每个帖子拿到的赞
    几乎一样多」——把偏斜给错了一侧。真实形态是反过来的：内容热度的长尾远比
    用户勤奋度的长尾陡。`user_follows` 同理，粉丝数（入度）该重尾，关注数（出度）
    不该那么重。这两处的判据见 `scripts/lakehouse/verify_behavior.py` 的
    `like_post_tail` / `follow_in_tail` / `follow_gini_gap`。

    `sigma_b=0` 时走 `fk_uniform`，**与加这个参数之前逐位相同**（rng 调用序列不变），
    所以 `user_segment_members` / `ab_test_assignments` 这些老调用点不受影响。

    ## recip：有向图的互关比例

    `recip>0` 时把约 `recip` 比例的边改写成「互关」（同时存在 a→b 和 b→a）。
    独立抽两端的话互关率就是随机基线（约 `n/(a_n·b_n)`），社交图不长这样。

    实现是**原地改写**，不是追加：从「反向边尚不存在」的行里取头部 k 条当种子，
    把尾部 k 条的槽位改写成种子的反向边。不能写成「先 append 反向边再截断到 n」——
    追加的边全落在尾部，而截断恰好把尾部切掉，于是互关率一条都没涨、还完全静默。
    结果集 = (原边 − 尾部 k 条) ∪ (头部 k 条的反向边)，三条性质由构造保证：行数仍是
    `n`、复合键仍唯一（反向边原本不在集合里）、无自环（非自环的反向边也非自环）。

    只在两端同域（`a_start==b_start` 且 `a_n==b_n`，即关注关系这类同构图）时有意义。
    """
    need = n
    factor = 1.6
    for _ in range(8):
        m = int(need * factor)
        a = fk_skewed(rng, m, a_start, a_n, sigma)
        b = (fk_uniform(rng, m, b_start, b_n) if sigma_b <= 0
             else fk_skewed(rng, m, b_start, b_n, sigma_b))
        keep = a != b
        a, b = a[keep], b[keep]
        composite = a.astype(np.int64) * (b_n + 1) + (b - b_start)
        _, uidx = np.unique(composite, return_index=True)
        a, b = a[np.sort(uidx)], b[np.sort(uidx)]
        if len(a) >= need:
            return _add_recip(a[:need], b[:need], b_start, b_n, recip)
        factor *= 2
    return _add_recip(a, b, b_start, b_n, recip)   # 图太稀疏，返回能拿到的最大唯一集


def _add_recip(a: np.ndarray, b: np.ndarray, b_start: int, b_n: int, recip: float
               ) -> tuple[np.ndarray, np.ndarray]:
    """把约 recip 比例的边原地改写成互关。理由与不变量见 `unique_pairs` docstring。"""
    if recip <= 0 or len(a) == 0:
        return a, b
    key = a * (b_n + 1) + (b - b_start)
    rev = b * (b_n + 1) + (a - b_start)
    free = np.flatnonzero(~np.isin(rev, key))       # 反向边尚不存在的行
    k = min(int(len(a) * recip / 2.0), len(free) // 2)
    if k <= 0:
        return a, b
    seeds, slots = free[:k], free[len(free) - k:]
    a2, b2 = a.copy(), b.copy()
    a2[slots], b2[slots] = b[seeds], a[seeds]
    return a2, b2


# ---------------------------------------------------------------- 枚举 / 标量

def enum(rng: np.random.Generator, n: int, values: list, weights: list | None = None
         ) -> np.ndarray:
    """加权枚举。weights 不必归一化；省略则均匀。"""
    vals = np.asarray(values, dtype=object)
    if weights is None:
        return vals[rng.integers(0, len(vals), size=n)]
    w = np.asarray(weights, dtype=np.float64)
    return vals[rng.choice(len(vals), size=n, replace=True, p=w / w.sum())]


def int_uniform(rng: np.random.Generator, n: int, lo: int, hi: int) -> np.ndarray:
    """[lo, hi] 闭区间整数。"""
    return rng.integers(lo, hi + 1, size=n).astype(np.int64)


def int_weighted(rng: np.random.Generator, n: int, values: list[int],
                 weights: list[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=np.float64)
    vals = np.asarray(values, dtype=np.int64)
    return vals[rng.choice(len(vals), size=n, replace=True, p=w / w.sum())]


_ND = statistics.NormalDist()


def _z_cuts(weights: list[float]) -> np.ndarray:
    """加权档位的累积概率 → 标准正态分位点。用于把「按权重抽档」表达成「切 z 轴」。"""
    w = np.asarray(weights, dtype=np.float64)
    cum = np.cumsum(w / w.sum())[:-1]
    return np.array([_ND.inv_cdf(float(c)) for c in cum], dtype=np.float64)


def coupled_ladders(rng: np.random.Generator, n: int,
                    values_a: list[int], weights_a: list[float],
                    values_b: list[int], weights_b: list[float],
                    rho: float) -> tuple[np.ndarray, np.ndarray]:
    """两列有序档位按相关系数 rho 联动，**两边的边缘分布逐档精确不变**。

    用在「停留时长 ⟷ 滚动深度」这类本该同向的一对列上：各自独立抽的话相关系数是 0，
    于是「读得久的页面滚得更深」这条最基本的行为常识在数据里不成立
    （判据 `pv_dwell_scroll_corr`）。

    做法是**高斯 copula**：两个标准正态 z1、z2 → `z_b = ρ·z1 + √(1−ρ²)·z2`，
    z_b 仍是标准正态，所以两列各按自己的 z 分位点切档，边缘分布不受影响。
    先试过更朴素的「先抽 u，再加噪声 u+N(0,σ) 后裁剪到 [0,1)」——裁剪把质量堆到两端，
    实测把时长均值从 33.2 顶到 39.4，**为了造相关性把分布本身改掉了**。

    性能上刻意不逐行算 `erf`：改成把累积权重一次性映到 z 空间（`_z_cuts`），
    再对原始正态做 `searchsorted`。page_views 在全量规模上是千万行级，
    `np.vectorize(math.erf)` 在那个量级是分钟级的开销。
    """
    z1 = rng.standard_normal(n)
    z2 = rng.standard_normal(n)
    zb = rho * z1 + np.sqrt(max(1.0 - rho * rho, 0.0)) * z2
    va = np.asarray(values_a, dtype=np.int64)
    vb = np.asarray(values_b, dtype=np.int64)
    return (va[np.searchsorted(_z_cuts(weights_a), z1, side="right")],
            vb[np.searchsorted(_z_cuts(weights_b), zb, side="right")])


def bool_p(rng: np.random.Generator, n: int, p: float) -> np.ndarray:
    return rng.random(n) < p


def decimal_lognorm(rng: np.random.Generator, n: int, median: float, sigma: float = 0.7,
                    lo: float = 0.01, hi: float = 1e9, dp: int = 2) -> np.ndarray:
    """对数正态金额：右偏长尾，符合真实消费金额分布（均匀分布会让客单价分析变平）。

    median 是中位数（不是均值）；sigma 控制长尾厚度。
    """
    v = rng.lognormal(mean=np.log(median), sigma=sigma, size=n)
    return np.round(np.clip(v, lo, hi), dp)


# ---------------------------------------------------------------- 时间

_SEC_PER_DAY = 86_400

# 周内波动：周末活跃度高一些（索引 0=周一 … 6=周日）
DOW_WEIGHTS = np.array([0.92, 0.90, 0.94, 0.98, 1.12, 1.28, 1.22])
# 小时曲线：早高峰、午间、晚高峰三峰
HOUR_WEIGHTS = np.array([
    0.25, 0.15, 0.10, 0.08, 0.08, 0.15, 0.40, 0.85,
    1.10, 1.05, 1.00, 1.15, 1.35, 1.10, 0.95, 0.95,
    1.05, 1.20, 1.45, 1.70, 1.85, 1.60, 1.05, 0.55,
])


def ts_window(rng: np.random.Generator, n: int, start: np.datetime64, days: int,
              trend: float = 0.35, dow: np.ndarray | None = None,
              hour: np.ndarray | None = None) -> np.ndarray:
    """在 [start, start+days) 内生成带信号的时间戳（datetime64[s]）。

    三层信号叠加：
      trend —— 全窗线性增长幅度（0.35 = 末期日均比初期高 35%），让趋势题有东西可分析
      dow   —— 周内波动，让「周环比」「残周」有意义
      hour  —— 小时三峰，让按小时切片不是平的

    刻意**不**读系统时间：start 由调用方从 budget.DATA_START 传入。窗口末端是
    AS_OF，首尾都落在月中，天然形成知识库里写的「残周残月」。
    """
    dow = DOW_WEIGHTS if dow is None else dow
    hour = HOUR_WEIGHTS if hour is None else hour

    d = np.arange(days)
    start_dow = int((start.astype("datetime64[D]").astype(int) + 3) % 7)   # 1970-01-01 是周四
    w = (1.0 + trend * d / max(days - 1, 1)) * dow[(start_dow + d) % 7]
    day_idx = rng.choice(days, size=n, replace=True, p=w / w.sum())

    hr = rng.choice(24, size=n, replace=True, p=hour / hour.sum())
    sec_in_hour = rng.integers(0, 3600, size=n)

    offs = day_idx.astype(np.int64) * _SEC_PER_DAY + hr.astype(np.int64) * 3600 + sec_in_hour
    return (start.astype("datetime64[s]") + offs.astype("timedelta64[s]"))


def ts_offset(rng: np.random.Generator, base: np.ndarray, min_min: int, max_min: int,
              cap: np.datetime64 | None = None, null_p: float = 0.0) -> np.ndarray:
    """在 base 之后偏移 [min_min, max_min] 分钟。cap 之后的置空（静态样本不能穿越 AS_OF）。"""
    off = rng.integers(min_min * 60, max_min * 60 + 1, size=len(base))
    out = base.astype("datetime64[s]") + off.astype("timedelta64[s]")
    out = out.astype("datetime64[s]").astype(object)
    arr = np.array(out, dtype=object)
    if cap is not None:
        over = np.array([x is not None and np.datetime64(x, "s") > cap for x in arr])
        arr[over] = None
    if null_p > 0:
        arr[rng.random(len(arr)) < null_p] = None
    return arr


def date_of(ts: np.ndarray) -> np.ndarray:
    """时间戳 → 日期（datetime64[D]）。"""
    return ts.astype("datetime64[D]")


# ---------------------------------------------------------------- 文本

def from_pool(rng: np.random.Generator, n: int, pool: np.ndarray) -> np.ndarray:
    """从预生成池里取（整型索引，O(1)/行，替代 per-row Faker 调用）。"""
    return pool[rng.integers(0, len(pool), size=n)]


def serial_text(prefix: str, ids: np.ndarray, width: int = 12) -> np.ndarray:
    """形如 NO000000000123 的业务单号。向量化：np.char 而非逐行 f-string。"""
    body = np.char.zfill(ids.astype("U20"), width)
    return np.char.add(prefix, body)


def token_text(rng: np.random.Generator, n: int, prefix: str, width: int = 16) -> np.ndarray:
    """伪随机 token（push_token / transaction_id 这类）。"""
    v = rng.integers(0, 16 ** 8, size=n, dtype=np.int64)
    hexed = np.array([f"{x:08x}" for x in v], dtype=object)   # n 通常不大的列才用
    return np.char.add(prefix, hexed.astype("U32"))


def combine(rng: np.random.Generator, n: int, *pools: np.ndarray) -> np.ndarray:
    """从多个词池各抽一次、按位拼接（组合式词池）。

    单池的基数就是列的基数——旧 `device_model` 只有 40 种、旧 `utm_campaign` 只有
    50 种，`GROUP BY` 一下就露馅。组合后基数是各池之积，既有多样性又不必手写几万条。
    各槽位共用该列的 rng（逐槽抽取），确定性契约不变：仍只由 (seed, 表, 列, 块) 决定。
    """
    parts = [np.asarray(from_pool(rng, n, p), dtype=str) for p in pools]
    out = parts[0]
    for p in parts[1:]:
        out = np.char.add(out, p)
    return out


# 后缀数字的空间：4 位数 1000~9999。STRIDE 是素数且不整除 SPAN=9000（=2³·3²·5³），
# 于是「组内序次 ↦ 数字」在一组内是单射——唯一性靠这条，不靠随机不撞。
_SUF_LO, _SUF_SPAN, _SUF_STRIDE, _SUF_SKEW = 1000, 9000, 7919, 4093


def _group_rank(vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """→ (每行在「同值组」内的序次, 组号)。序次 0 给组内行号最小的那行。

    实现是一次稳定排序 + 相邻比较，O(n log n)、无 Python 循环：`users` 在 8000 万
    规模上是 21 万行，逐行 dict 计数也跑得动，但这个函数是给"任何列"用的。
    """
    n = len(vals)
    if n == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    order = np.argsort(vals, kind="stable")        # stable ⇒ 组内保持行号升序
    srt = vals[order]
    head = np.empty(n, dtype=bool)
    head[0] = True
    head[1:] = srt[1:] != srt[:-1]
    idx = np.arange(n)
    rank, gid = np.empty(n, dtype=np.int64), np.empty(n, dtype=np.int64)
    rank[order] = idx - np.maximum.accumulate(np.where(head, idx, 0))
    gid[order] = np.cumsum(head) - 1
    return rank, gid


def combine_unique(rng: np.random.Generator, n: int, *pools: np.ndarray,
                   sep: str = "_") -> np.ndarray:
    """组合式词池 + 「先到先得」数字后缀，产出**全列唯一**的字符串。

    `combine` 的基数是各池之积，多样性够，但**不保证唯一**：n 行落进 M 种组合，
    期望撞掉的行数约 n²/2M。users 在 scale 427 上是 213,500 行落进 24,576 种昵称
    组合，88.5% 的行与别人同名；邮箱本地部分只有 6,000 种，72.7%（P1-6/7）。
    真实系统在注册那一刻就查重，所以「9 个用户叫同一个名字」是一眼可见的假数据，
    而且 `COUNT(DISTINCT username)` 会比用户数少一大截。

    机制照抄真实注册流程：**先到先得**。同一个组合里 user_id 最小的那个人拿干净的
    名字（`小鱼干88`），后来的人在后面接一串四位数（`小鱼干88_2049`）——这正是国内
    社交平台昵称的实际形态。数字由组内序次经乘性置换得到，不是随机抽的，所以
    「组内不撞」是算术保证；跨组不撞则由最后那道 np.unique 兜住（词池里带 `_`
    的尾巴让前缀切分不再显然，不适合靠手工推导）。

    调用方必须整列一次算完——唯一性是全列的性质，分块抽拼不出来。
    """
    base = combine(rng, n, *pools)
    if n == 0:
        return base
    rank, gid = _group_rank(base)
    top = int(rank.max())
    if top >= _SUF_SPAN:
        raise ValueError(
            f"combine_unique：最大同名组有 {top + 1} 行，超出四位后缀的 {_SUF_SPAN} 个"
            f"取值，唯一性无法保证。要么扩词池（当前 {len(base)} 行 / "
            f"{len(np.unique(base))} 种组合），要么改宽后缀")
    num = _SUF_LO + ((rank - 1) * _SUF_STRIDE + gid * _SUF_SKEW) % _SUF_SPAN
    suffixed = np.char.add(base, np.char.add(sep, num.astype("U4")))
    out = np.where(rank == 0, base, suffixed)
    if len(np.unique(out)) != n:
        raise ValueError(
            f"combine_unique：加完后缀仍有 {n - len(np.unique(out))} 行重复。"
            f"词池里某个尾巴与分隔符 {sep!r} 撞出了同一个串，换分隔符或改词池")
    return out


def unique_digits(rng: np.random.Generator, n: int, prefixes: np.ndarray,
                  width: int) -> np.ndarray:
    """前缀池 + 定宽数字尾，**全列唯一**（碰撞重抽）。

    手机号这类列不能走 `combine_unique`：加后缀会破坏 11 位定长，`^1[3-9]\\d{9}$`
    立刻不合规。所以改成抽完查重、只把撞了的那几行重抽。号段 40 × 10⁸ 的空间对
    21 万行来说期望撞 6 行左右，一两轮收敛；空间不够时抛错而不是死转。
    """
    space = len(prefixes) * 10 ** width
    if n > space:
        raise ValueError(f"unique_digits：{n} 行放不进 {len(prefixes)} × 10^{width} "
                         f"= {space} 的空间")
    hi = 10 ** width - 1

    def draw(k: int) -> np.ndarray:
        seg = np.asarray(from_pool(rng, k, prefixes), dtype=str)
        tail = np.char.zfill(int_uniform(rng, k, 0, hi).astype(f"U{width}"), width)
        return np.char.add(seg, tail)

    out = draw(n)
    for _ in range(64):
        _, first = np.unique(out, return_index=True)
        if len(first) == n:
            return out
        dup = np.setdiff1d(np.arange(n), first)   # 每组留第一行，其余重抽
        out[dup] = draw(len(dup))
    raise ValueError(f"unique_digits：重抽 64 轮后仍有 {n - len(np.unique(out))} 行重复，"
                     f"空间 {space} 对 {n} 行太挤")


_HEX = np.array(list("0123456789abcdef"), dtype="U1")


def _hex_of(v: np.ndarray, width: int) -> np.ndarray:
    """uint64 → 定长小写十六进制。

    向量化实现：查表填一个 (n, width) 的 U1 矩阵，再把相邻 U1 直接重解释成定长串
    （U1 每字符 4 字节，view 到 U{width} 即为逐行拼接）。不逐行 f-string——
    `events.device_id` 在 8000 万规模上有 850 万行，per-row f-string 会跑成分钟级。
    """
    digits = np.empty((len(v), width), dtype="U1")
    for k in range(width):
        digits[:, width - 1 - k] = _HEX[(v >> np.uint64(4 * k)) & np.uint64(0xF)]
    return np.ascontiguousarray(digits).view(f"U{width}").reshape(len(v))


_MIX = np.uint64(0x9E3779B97F4A7C15)          # 黄金比例奇数


def opaque_id(ids: np.ndarray, width: int = 16) -> np.ndarray:
    """int64 id → 稳定的不透明十六进制串（形如 `9e1c4a07b3d2f865`）。

    device_id 要同时满足三件事，缺一件都不行：
    1. 看起来像真实设备标识，不是 `dev_1`；
    2. 同一个 id 在任何表、任何分块里映射到同一个串——`sessions` / `events` /
       `user_devices` 三张表的 device_id 必须继续对得上（它们各自从 user_id 或
       行号推，这里只换编码，不换被编码的那个整数）；
    3. 不撞：`user_devices.device_id` 是 PK。

    乘奇数与 xorshift 在 mod 2^64 上都是双射，复合仍是双射，所以第 3 点由构造保证、
    不靠概率。**前提是 width=16（64 bit 全留）**；调小会截断，双射性随之失效。
    """
    v = np.asarray(ids, dtype=np.int64).astype(np.uint64) * _MIX
    v ^= v >> np.uint64(29)
    v *= _MIX
    v ^= v >> np.uint64(32)
    return _hex_of(v, width)


def rand_hex(rng: np.random.Generator, n: int, width: int = 64) -> np.ndarray:
    """随机定长十六进制串（push_token 这类不透明令牌，真实形态就是一串 hex）。"""
    d = _HEX[rng.integers(0, 16, size=(n, width))]
    return np.ascontiguousarray(d).view(f"U{width}").reshape(n)


def null_out(rng: np.random.Generator, arr: np.ndarray, p: float) -> np.ndarray:
    """按比例置空。整型列会被转成 object（CSV/Parquet 里表现为 NULL）。"""
    if p <= 0:
        return arr
    out = arr.astype(object)
    out[rng.random(len(out)) < p] = None
    return out


def const(n: int, v) -> np.ndarray:
    """常量列。

    不能用 `np.full(n, v, dtype=object)`：v 是 list/dict 时 numpy 会尝试把它当作
    要广播的数组，空 list 直接报 shape (0,) 无法广播到 (n,)。这里对容器类型改成
    逐位置赋**同一引用**（不是拷贝，所以 850 万行也只是一个指针数组）。
    """
    a = np.empty(n, dtype=object)
    if isinstance(v, (list, dict, tuple, set)):
        a[:] = [v] * n
    else:
        a.fill(v)
    return a


# ---------------------------------------------------------------- 类型兜底

def by_type(rng: np.random.Generator, n: int, ctype: str, colname: str = "") -> np.ndarray:
    """spec 没声明的列按类型给一个合理默认值。

    刻意保守：兜底列不产生业务信号，只保证类型合法、可 COPY。真正参与分析的列
    必须在 spec 里显式声明，否则就是「默默生成了没意义的数」——那比报错更糟。
    """
    if ctype == "INT":
        return int_uniform(rng, n, 1, 100)
    if ctype == "DECIMAL":
        return decimal_lognorm(rng, n, 50.0)
    if ctype == "BOOL":
        return bool_p(rng, n, 0.5)
    if ctype in ("TIMESTAMP", "DATE"):
        raise ValueError(f"时间列必须在 spec 里显式声明（列 {colname}），不能兜底")
    if ctype == "JSON":
        return const(n, {})
    if ctype == "ARRAY":
        return const(n, [])
    return from_pool(rng, n, np.array([f"{colname or 'v'}_{i}" for i in range(64)], dtype=object))
