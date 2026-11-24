#!/usr/bin/env python3
"""profiles.yaml 的加载器 + 校验器：把画像取值域、判据、效应量三者对死。

## 为什么加载要顺带校验

同 `semantics.py`：配置里一个打字错会变成**静默的数据缺陷**，而不是报错。
具体到这份配置：

  · `occupation_weights` 少写一个职业 → 该职业权重缺失 → 生成器抽不到它 → 该档 0 人
    → 「职业人数极差」那条判据反而更容易通过（少了一档），于是缺陷把检查带偏了；
  · `spend_multiplier.income_level` 的键拼成 `very high`（空格）→ 该档倍率取不到 →
    落到默认 1.0 → 顶档变成中性 → 单调性那条判据红，而红的原因看起来像生成器逻辑错。

所以校验放在 `load()` 里而不是 `--selftest` 里：任何 import 这份配置的代码（生成器、
`verify_correlation.py`）都被迫先过一遍，没有「跳过检查直接用」的路径。纯本地、毫秒级。

## 判据（全部硬 FAIL）

  1. `version == 1`；
  2. `dimensions` 的三个枚举维度非空、无重复；`age` 满足 `18 ≤ min < max`；
  3. `judge.ranges` 的键集合 == 四个维度名（**双向**），每条 `min_range > 0` 且有 `why`；
  4. `judge.order` 每条的 `dim` 在维度里、`kind` 在已实现的四种里、`buckets` 落在该维度
     的合法档位内、且有 `why`。`monotone` 不带 buckets（顺序就是 `dimensions` 的顺序）；
  5. `judge.shape` / `judge.noise_floor` 的数 > 0；
  6. `generate.spend_multiplier` 的四个子表键集合与对应维度**双向**相等
     （`age_decade` 与 `[min,max]` 覆盖到的十年档双向相等），倍率全部 > 0；
  7. `generate.occupation_weights` 键集合 == 职业池（双向），权重全部 > 0；
  8. **生成侧的方向必须与 `judge.order` 一致**：收入倍率按声明顺序单调、
     `outranks` 的两档倍率方向对、`peak_within` 的峰值倍率落在允许档内；
  9. **生成侧的效应量留出余量高于通过线**（`MARGIN` 倍）。这一条的定位见 profiles.yaml
     文件头：它不是拿倍率算通过线（那是自证），是拦「生成的数据先天过不了自己的检查」；
 10. `preserve_marginals` 里的维度**不许**在 `generate` 里出现人数权重——那两列的边际
     被 knowledge 卡片钉住，真源在 `tables.py` 的 `F.enum` 权重里，写第二份就会漂。

## 边界

`load()` 返回 `(Dimensions, Judge, Generate)` 三件套而不是一个大对象，因为
`verify_correlation.py` 只接前两个。检查侧拿不到 `Generate`，「检查器偷看生成器参数」
这件事就不是靠自觉，而是拿不到那个变量。

## 用法

    python3 scripts/gen/profiles.py              # 打印摘要（倍率跨度 ⟷ 通过线对照表）
    python3 scripts/gen/profiles.py --selftest   # 跑断言，L0 用这个

无云依赖、无 numpy 依赖（只用 yaml），秒级。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "profiles.yaml"

# 第 9 条断言的余量倍数。倍率跨度要 ≥ 通过线 × MARGIN。
#
# 1.6 不是从任何测量来的，是为「压缩」留的头寸：分档倍率是同档用户的**期望**强度，
# 而实测极差是它经过订单数长尾、有效状态过滤、per-user 平均之后的残余。压缩多少取决于
# 订单数分布，没有闭式解，所以这里给一个明确的、可以被反驳的整数余量，而不是去拟合。
# 定得太大会逼人把倍率越调越夸张（数据变得比现实更分层），太小则生成完才发现过不了线。
MARGIN = 1.6

_ORDER_KINDS = ("monotone", "peak_within", "not_top", "outranks")
_ENUM_DIMS = ("income_level", "gender", "occupation")
_ALL_DIMS = ("income_level", "gender", "occupation", "age")


class ProfilesError(Exception):
    """配置自相矛盾。刻意用异常而不是返回 False：调用方没有忽略它的路径。"""


def age_decade(age):
    """年龄 → 十年档下界。**生成侧和检查侧必须都调这个函数。**

    口径与任务书 L8 的查询骨架 `age/10*10` 一致。这不是「顺手抽个函数」：生成器按
    `age//10*10` 索引倍率、而检查器按 `(age-18)//10` 分组的话，两边量的是不同的划分，
    红绿都读不出意思——和 `semantics.price_range()` 必须两侧共用是同一个理由。

    整数标量和整数 ndarray 都接（生成侧是 25 万行的向量调用，检查侧是逐行标量）。
    用裸 `//` 而不是 `np.floor_divide`：本模块只依赖 pyyaml，不引 numpy，这样
    `--selftest` 能挂在 test_all.sh 的 L0 上、不受 numpy 分支约束（同 semantics.py）。
    """
    return (age // 10) * 10


@dataclass(frozen=True)
class Dimensions:
    """取值域。两侧共用（值域是业务事实，不是判据也不是效应量）。"""
    income_level: tuple[str, ...]          # 顺序有意义：low → very_high
    gender: tuple[str, ...]
    occupation: tuple[str, ...]
    age_min: int
    age_max: int
    not_generated: dict[str, tuple]        # 维度 -> 值域里存在但生成器不产的档位

    @property
    def age_decades(self) -> tuple[int, ...]:
        """[age_min, age_max] 覆盖到的十年档，升序。"""
        return tuple(sorted({age_decade(a) for a in range(self.age_min, self.age_max + 1)}))

    def buckets(self, dim: str) -> tuple:
        """该维度的**全部**合法档位（含生成器不产的）。配置的键集合按这个比。"""
        if dim == "age":
            return self.age_decades
        return getattr(self, dim)

    def reachable(self, dim: str) -> tuple:
        """数据里真会出现的档位。效应量的余量断言、检查侧的「无输入」判断都用这个。"""
        skip = set(self.not_generated.get(dim, ()))
        return tuple(b for b in self.buckets(dim) if b not in skip)


@dataclass(frozen=True)
class OrderRule:
    dim: str
    kind: str
    buckets: tuple
    why: str


@dataclass(frozen=True)
class Judge:
    """检查侧读的全部内容。**不含任何生成参数。**"""
    min_range: dict[str, float]                   # 维度 -> 极差通过线
    why: dict[str, str]
    order: tuple[OrderRule, ...]
    occupation_count_min_range: float
    occupation_count_why: str
    n_sigma: float
    n_sigma_why: str

    def rules_for(self, dim: str) -> tuple[OrderRule, ...]:
        return tuple(r for r in self.order if r.dim == dim)


@dataclass(frozen=True)
class Generate:
    """生成侧读的全部内容。**检查侧拿不到这个对象。**"""
    income_mult: dict[str, float]
    gender_mult: dict[str, float]
    occupation_mult: dict[str, float]
    age_decade_mult: dict[int, float]
    occupation_weights: dict[str, float]

    def raw_multiplier(self, income: str, gender: str, occupation: str, age: int) -> float:
        """四个维度倍率之积（**未**归一）。标量参考实现。

        `tables.py` 用向量化的查表乘法算同一件事，`selftest_closures.py` 拿这个函数
        逐行核对少量样本——两份实现是为了性能，不是为了两套公式。
        """
        return (self.income_mult[income] * self.gender_mult[gender]
                * self.occupation_mult[occupation] * self.age_decade_mult[age_decade(age)])

    def table(self, dim: str) -> dict:
        return {"income_level": self.income_mult, "gender": self.gender_mult,
                "occupation": self.occupation_mult, "age": self.age_decade_mult}[dim]

    def span(self, dim: str, dims: Dimensions) -> float:
        """该维度倍率的极差（max/min - 1），与 judge 的 min_range 同口径。

        **只算 `dims.reachable(dim)`**：拿生成器不产的档位撑跨度，就是用一个永远不会
        出现在数据里的档位去证明效应够大（gender 算上 unknown 是 64%，实际只有 35%）。
        """
        m = self.table(dim)
        vs = [m[b] for b in dims.reachable(dim)]
        return max(vs) / min(vs) - 1.0


# ---------------------------------------------------------------- 加载 + 校验

def _need(d: dict, key: str, where: str):
    if key not in d:
        raise ProfilesError(f"{where} 缺少 {key!r}")
    return d[key]


def _pos(v, where: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ProfilesError(f"{where} 不是数：{v!r}") from None
    if f <= 0:
        raise ProfilesError(f"{where} 必须 > 0，实际 {f}")
    return f


def _same_keys(got, want, where: str) -> None:
    """双向比。只查一边的话，多写一个键会被静默忽略——那正是最难发现的一类配置错。"""
    g, w = set(got), set(want)
    if g != w:
        raise ProfilesError(
            f"{where} 与取值域不一致：缺 {sorted(w - g, key=str)}；"
            f"多出 {sorted(g - w, key=str)}")


def load(yaml_path: Path = YAML_PATH) -> tuple[Dimensions, Judge, Generate]:
    """读配置 + 全量校验。任何一条不成立都抛 ProfilesError，不返回半成品。"""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if raw.get("version") != 1:
        raise ProfilesError(f"profiles.yaml version={raw.get('version')}，本加载器只认 1")

    # ---- 2. 取值域 ----
    dims_raw = _need(raw, "dimensions", "profiles.yaml")
    vals: dict[str, tuple[str, ...]] = {}
    for d in _ENUM_DIMS:
        v = [str(x) for x in _need(dims_raw, d, "dimensions")]
        if not v:
            raise ProfilesError(f"dimensions.{d} 为空")
        if len(set(v)) != len(v):
            raise ProfilesError(f"dimensions.{d} 有重复取值：{v}")
        vals[d] = tuple(v)
    age_raw = _need(dims_raw, "age", "dimensions")
    a_min, a_max = int(_need(age_raw, "min", "dimensions.age")), int(_need(age_raw, "max", "dimensions.age"))
    if not (18 <= a_min < a_max):
        raise ProfilesError(f"dimensions.age 需满足 18 ≤ min < max，实际 {a_min}/{a_max}")
    # `bucket` 目前只实现了 decade。显式校验而不是忽略：写成 `bucket: quintile` 时
    # 静默按十年档跑，报告里的分档口径就和配置说的不是一回事了。
    if age_raw.get("bucket") != "decade":
        raise ProfilesError(
            f"dimensions.age.bucket={age_raw.get('bucket')!r}，只实现了 'decade'"
            f"（口径见 age_decade()，与任务书 L8 的 age/10*10 一致）")
    dims = Dimensions(income_level=vals["income_level"], gender=vals["gender"],
                      occupation=vals["occupation"], age_min=a_min, age_max=a_max,
                      not_generated={})
    ng: dict[str, tuple] = {}
    for d, items in (dims_raw.get("not_generated") or {}).items():
        if d not in _ALL_DIMS:
            raise ProfilesError(f"dimensions.not_generated 里的 {d!r} 不是画像维度")
        legal = dims.buckets(d)
        bk = tuple(type(legal[0])(x) for x in items)
        bad = [x for x in bk if x not in legal]
        if bad:
            raise ProfilesError(
                f"dimensions.not_generated.{d} 里 {bad} 不在取值域内（合法：{list(legal)}）")
        if len(bk) >= len(legal):
            raise ProfilesError(
                f"dimensions.not_generated.{d} 把整个维度都排除了，那这个维度不该存在")
        ng[d] = bk
    dims = Dimensions(income_level=vals["income_level"], gender=vals["gender"],
                      occupation=vals["occupation"], age_min=a_min, age_max=a_max,
                      not_generated=ng)

    # ---- 3. 极差通过线 ⟷ 四个维度，双向 ----
    j_raw = _need(raw, "judge", "profiles.yaml")
    ranges_raw = _need(j_raw, "ranges", "judge")
    _same_keys(ranges_raw, _ALL_DIMS, "judge.ranges")
    min_range, why = {}, {}
    for d, spec in ranges_raw.items():
        min_range[d] = _pos(_need(spec, "min_range", f"judge.ranges.{d}"),
                            f"judge.ranges.{d}.min_range")
        w = str(_need(spec, "why", f"judge.ranges.{d}")).strip()
        if len(w) < 20:
            raise ProfilesError(
                f"judge.ranges.{d}.why 太短（{len(w)} 字）。通过线的推导必须写下来，"
                f"否则下一个人只能把它当成执行者的行业印象——那正是任务书要避免的。")
        why[d] = w

    # ---- 4. 顺序约束 ----
    rules = []
    for i, spec in enumerate(_need(j_raw, "order", "judge")):
        at = f"judge.order[{i}]"
        d = str(_need(spec, "dim", at))
        if d not in _ALL_DIMS:
            raise ProfilesError(f"{at}.dim={d!r} 不是画像维度（{_ALL_DIMS}）")
        kind = str(_need(spec, "kind", at))
        if kind not in _ORDER_KINDS:
            raise ProfilesError(f"{at}.kind={kind!r} 未实现，只有 {_ORDER_KINDS}")
        w = str(_need(spec, "why", at)).strip()
        if len(w) < 20:
            raise ProfilesError(f"{at}.why 太短，顺序约束的依据必须写下来")
        legal = dims.buckets(d)
        if kind == "monotone":
            # 顺序就是 dimensions 里声明的顺序，不再抄一份（抄了两边会各自漂）。
            if spec.get("buckets"):
                raise ProfilesError(
                    f"{at} 是 monotone，不要再写 buckets：顺序的真源是 "
                    f"dimensions.{d}（{list(legal)}），写第二份就会与它漂开")
            bk = tuple(legal)
        else:
            bk_raw = _need(spec, "buckets", at)
            bk = tuple(type(legal[0])(x) for x in bk_raw)
            bad = [x for x in bk if x not in legal]
            if bad:
                raise ProfilesError(f"{at}.buckets 里 {bad} 不在 dimensions.{d} 的档位内"
                                    f"（合法：{list(legal)}）")
            if kind == "outranks" and len(bk) != 2:
                raise ProfilesError(f"{at} 是 outranks，buckets 必须恰好两档（高, 低）")
            if kind == "peak_within" and not bk:
                raise ProfilesError(f"{at} 是 peak_within，buckets 不能为空")
        rules.append(OrderRule(dim=d, kind=kind, buckets=bk, why=w))

    # ---- 5. 形态 + 功效 ----
    shape = _need(j_raw, "shape", "judge")
    nf = _need(j_raw, "noise_floor", "judge")
    judge = Judge(
        min_range=min_range, why=why, order=tuple(rules),
        occupation_count_min_range=_pos(
            _need(shape, "occupation_user_count_min_range", "judge.shape"),
            "judge.shape.occupation_user_count_min_range"),
        occupation_count_why=str(_need(shape, "occupation_why", "judge.shape")).strip(),
        n_sigma=_pos(_need(nf, "n_sigma", "judge.noise_floor"), "judge.noise_floor.n_sigma"),
        n_sigma_why=str(_need(nf, "why", "judge.noise_floor")).strip(),
    )

    # ---- 6/7. 生成侧的键集合 ----
    g_raw = _need(raw, "generate", "profiles.yaml")
    sm = _need(g_raw, "spend_multiplier", "generate")
    _same_keys(sm, ("income_level", "gender", "occupation", "age_decade"),
               "generate.spend_multiplier")
    mult: dict[str, dict] = {}
    for d in _ENUM_DIMS:
        _same_keys(sm[d], dims.buckets(d), f"generate.spend_multiplier.{d}")
        mult[d] = {str(k): _pos(v, f"generate.spend_multiplier.{d}.{k}")
                   for k, v in sm[d].items()}
    _same_keys(sm["age_decade"], dims.age_decades, "generate.spend_multiplier.age_decade")
    mult["age"] = {int(k): _pos(v, f"generate.spend_multiplier.age_decade.{k}")
                   for k, v in sm["age_decade"].items()}

    ow_raw = _need(g_raw, "occupation_weights", "generate")
    _same_keys(ow_raw, dims.occupation, "generate.occupation_weights")
    occ_w = {str(k): _pos(v, f"generate.occupation_weights.{k}") for k, v in ow_raw.items()}

    gen = Generate(income_mult=mult["income_level"], gender_mult=mult["gender"],
                   occupation_mult=mult["occupation"], age_decade_mult=mult["age"],
                   occupation_weights=occ_w)

    # ---- 10. 被卡片钉住的边际不许在这里出现第二份权重 ----
    preserved = [str(x) for x in raw.get("preserve_marginals", [])
                 or g_raw.get("preserve_marginals", [])]
    for d in preserved:
        if d not in _ALL_DIMS:
            raise ProfilesError(f"preserve_marginals 里的 {d!r} 不是画像维度")
        if f"{d}_weights" in g_raw:
            raise ProfilesError(
                f"generate.{d}_weights 与 preserve_marginals 冲突：{d} 的边际分布被 "
                f"knowledge 卡片钉住（真源是 tables.py 里那个 F.enum 的权重），"
                f"在这里写第二份权重，两份不一致时数据会静默偏离卡片而 verify_enums 才报")

    # ---- 8. 生成侧的方向必须与 judge.order 一致 ----
    for r in judge.order:
        m = {"income_level": gen.income_mult, "gender": gen.gender_mult,
             "occupation": gen.occupation_mult, "age": gen.age_decade_mult}[r.dim]
        if r.kind == "monotone":
            seq = [m[b] for b in r.buckets]
            if seq != sorted(seq):
                raise ProfilesError(
                    f"judge 要求 {r.dim} 按 {list(r.buckets)} 单调，而 "
                    f"generate.spend_multiplier.{r.dim} 是 {seq}——生成的数据先天过不了"
                    f"这条顺序约束")
        elif r.kind == "outranks":
            hi, lo = r.buckets
            if not m[hi] > m[lo]:
                raise ProfilesError(
                    f"judge 要求 {r.dim} 的 {hi} 高于 {lo}，而倍率是 "
                    f"{m[hi]} / {m[lo]}——方向反了")
        elif r.kind == "peak_within":
            peak = max(m, key=lambda k: m[k])
            if peak not in r.buckets:
                raise ProfilesError(
                    f"judge 要求 {r.dim} 的峰值落在 {list(r.buckets)}，而倍率峰值在 "
                    f"{peak}（{m[peak]}）")
        elif r.kind == "not_top":
            top = max(m, key=lambda k: m[k])
            if top in r.buckets:
                raise ProfilesError(
                    f"judge 要求 {r.dim} 的 {list(r.buckets)} 不得居首，而倍率最高的"
                    f"就是 {top}")

    # ---- 9. 效应量留出余量高于通过线 ----
    for d in _ALL_DIMS:
        span, line = gen.span(d, dims), judge.min_range[d]
        if span < line * MARGIN:
            raise ProfilesError(
                f"generate 的 {d} 倍率跨度 {span:.0%} < 通过线 {line:.0%} × MARGIN "
                f"{MARGIN}。实测极差只会比倍率跨度更小（订单数长尾 + per-user 平均会压缩），"
                f"所以这样生成出来的数据**先天过不了自己的检查**。要么加大倍率，"
                f"要么先说清为什么通过线该降——不要靠改检查器让它变绿。")
    # 职业人数权重同理。
    ow_span = max(occ_w.values()) / min(occ_w.values()) - 1.0
    if ow_span < judge.occupation_count_min_range * MARGIN:
        raise ProfilesError(
            f"generate.occupation_weights 跨度 {ow_span:.0%} < 通过线 "
            f"{judge.occupation_count_min_range:.0%} × MARGIN {MARGIN}")

    return dims, judge, gen


# ---------------------------------------------------------------- 摘要 / 自测

def summary(dims: Dimensions, judge: Judge, gen: Generate) -> None:
    print(f"取值域   income_level {len(dims.income_level)} 档（{'→'.join(dims.income_level)}）"
          f" · gender {len(dims.gender)} · occupation {len(dims.occupation)}"
          f" · age {dims.age_min}~{dims.age_max} → {len(dims.age_decades)} 个十年档 "
          f"{list(dims.age_decades)}")
    for d, bk in dims.not_generated.items():
        print(f"         生成器不产 {d}={list(bk)}（值域里有，数据里没有；"
              f"跨度和检查都不算它）")
    print("\n倍率跨度 ⟷ 通过线（两列独立推导，不是一列算出另一列；"
          f"加载时只断言 跨度 ≥ 通过线 × {MARGIN}）")
    print(f"  {'维度':<14}{'倍率跨度':>10}{'通过线':>10}{'余量':>8}")
    for d in _ALL_DIMS:
        span, line = gen.span(d, dims), judge.min_range[d]
        print(f"  {d:<14}{span:>9.0%}{line:>10.0%}{span / line:>7.1f}×")
    ow = gen.occupation_weights
    print(f"  {'职业人数':<14}{max(ow.values()) / min(ow.values()) - 1:>9.0%}"
          f"{judge.occupation_count_min_range:>10.0%}"
          f"{(max(ow.values()) / min(ow.values()) - 1) / judge.occupation_count_min_range:>7.1f}×")

    print(f"\n顺序约束 {len(judge.order)} 条")
    for r in judge.order:
        arg = "" if r.kind == "monotone" else f" {list(r.buckets)}"
        print(f"  {r.dim:<14}{r.kind}{arg}")
    print(f"\n统计功效 n_sigma={judge.n_sigma}（先问「这批数据分辨得出通过线那么大的"
          f"差异吗」，答案是否则判 WEAK 而不是 FAIL）")

    lo = min(gen.raw_multiplier(i, g, o, a)
             for i in dims.income_level for g in dims.gender
             for o in dims.occupation for a in (dims.age_min, 25, 35, 45, 55, dims.age_max))
    hi = max(gen.raw_multiplier(i, g, o, a)
             for i in dims.income_level for g in dims.gender
             for o in dims.occupation for a in (dims.age_min, 25, 35, 45, 55, dims.age_max))
    print(f"\n合成倍率（四维之积，归一前）min {lo:.3f} · max {hi:.2f} · 比 {hi / lo:.0f}×"
          f"\n  归一后除以实际均值，全库 GMV 量级不漂移。")


def selftest() -> int:
    """L0 用。分两半：正向断言 + **反向**（把配置改坏，load 必须抛）。

    只验正向证明不了校验器有效——这是 Batch 1 的教训：`verify_enums.py` 当年两端都是
    同一份数据的副本，绿灯不含任何信息。所以下面每条校验都配一个必须抛异常的用例。
    """
    npass = 0

    def ok(cond, msg: str) -> None:
        nonlocal npass
        assert cond, f"FAIL: {msg}"
        npass += 1
        print(f"  ok  {msg}")

    raw = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    dims, judge, gen = load()
    ok(True, f"profiles.yaml 全量校验通过（{len(_ALL_DIMS)} 维度 · "
             f"{len(judge.order)} 条顺序约束）")

    # ---- 分档函数：两侧共用的那一个 ----
    ok(age_decade(18) == 10 and age_decade(29) == 20 and age_decade(60) == 60,
       "age_decade 与任务书 L8 的 age/10*10 口径一致（18→10, 29→20, 60→60）")
    ok(dims.age_decades == (10, 20, 30, 40, 50, 60),
       f"age {dims.age_min}~{dims.age_max} 覆盖 6 个十年档 {list(dims.age_decades)}"
       f"（10 档和 60 档各只有 2 岁 / 1 岁，人数天然少）")

    # ---- 通过线必须真的判审计里那四个值失败 ----
    # 这是「不迁就测量」和「仍然抓住缺陷」两件事同时成立的证据。
    audited = {"income_level": 0.014, "age": 0.032, "gender": 0.049, "occupation": 0.070}
    still = {d: v for d, v in audited.items() if v >= judge.min_range[d]}
    ok(not still,
       f"审计实测的四个极差（{', '.join(f'{d} {v:.1%}' for d, v in audited.items())}）"
       f"全部低于按分析需要推导出的通过线"
       + (f"；漏判 {still}" if still else "——阈值没有贴着数据定，也没有放过缺陷"))

    # ---- 效应量 ⟷ 通过线的余量 ----
    tight = min((gen.span(d, dims) / judge.min_range[d], d) for d in _ALL_DIMS)
    ok(tight[0] >= MARGIN,
       f"最紧的维度是 {tight[1]}，倍率跨度 / 通过线 = {tight[0]:.1f}× ≥ MARGIN {MARGIN}")

    # 余量必须按**数据里真会出现的档位**算。gender 算上生成器不产的 unknown 是 64%，
    # 只算 female/male 是 35%——差 1.8 倍，正好跨过 MARGIN 的判定线。
    ok(gen.span("gender", dims) < max(gen.gender_mult.values()) / min(gen.gender_mult.values()) - 1,
       f"gender 跨度按可达档位算 {gen.span('gender', dims):.0%}，"
       f"算上 not_generated 的 unknown 会虚报成 "
       f"{max(gen.gender_mult.values()) / min(gen.gender_mult.values()) - 1:.0%}")

    # ---- 合成倍率：归一后不许把 GMV 量级带走 ----
    # 四个维度独立抽样，所以合成倍率的期望 = 四个期望之积。这里用卡片钉住的边际
    # （gender 258/242、income 230/119/105/46）和配置里的职业权重算，年龄按均匀。
    card_gender = {"female": 258, "male": 242, "unknown": 0}
    card_income = {"low": 119, "medium": 230, "high": 105, "very_high": 46}

    def wmean(m: dict, w: dict) -> float:
        tot = sum(w.get(k, 0) for k in m)
        return sum(m[k] * w.get(k, 0) for k in m) / tot

    age_w = {d: sum(1 for a in range(dims.age_min, dims.age_max + 1) if age_decade(a) == d)
             for d in dims.age_decades}
    e = (wmean(gen.income_mult, card_income) * wmean(gen.gender_mult, card_gender)
         * wmean(gen.occupation_mult, gen.occupation_weights)
         * wmean(gen.age_decade_mult, age_w))
    ok(0.5 < e < 2.5,
       f"合成倍率在卡片边际下的期望 {e:.3f}（归一时要除掉它；离 1 太远说明倍率整体偏了，"
       f"归一虽能救回量级，但每档的相对位置会被整体缩放掩盖）")

    # ---- 标量参考实现 ----
    # 四个查表值写成字面量而不是 `gen.*_mult[...]`：后者是拿被测函数自己的输入去验它，
    # 一致不携带信息。代价是池子改档位时这里要跟着改——2026-08-28 职业池对齐线上库、
    # `管理者`(1.85) 换成 `企业主`(2.05) 时这一条就是这么红的（KeyError，不是断言失败，
    # 所以 test_all.sh 那层按 /全部通过/ 抓才看得见，按 /FAIL/ 抓会漏）。
    ok(abs(gen.raw_multiplier("very_high", "female", "企业主", 35)
           - 2.60 * 1.15 * 2.05 * 1.40) < 1e-12,
       "raw_multiplier 就是四个查表值之积（tables.py 的向量化版本按这个核对）")

    # ---- 反向：每条校验都要能抛 ----
    import copy

    def must_raise(tag: str, mutate) -> None:
        nonlocal npass
        bad = copy.deepcopy(raw)
        mutate(bad)
        p = HERE / ".profiles_selftest.tmp.yaml"
        p.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
        try:
            load(p)
        except ProfilesError as exc:
            npass += 1
            print(f"  ok  {tag} → {str(exc).splitlines()[0][:78]}")
        else:
            raise AssertionError(f"FAIL: {tag} 没有抛异常——这条校验是死的")
        finally:
            p.unlink(missing_ok=True)

    print("\n---- 反向：把配置改坏，load() 必须抛 ----")

    def drop_occ_weight(d):
        d["generate"]["occupation_weights"].pop("医生")
    must_raise("职业权重少一档（该职业会静默 0 人，反而让人数极差更容易通过）",
               drop_occ_weight)

    def typo_income_key(d):
        d["generate"]["spend_multiplier"]["income_level"]["very high"] = \
            d["generate"]["spend_multiplier"]["income_level"].pop("very_high")
    must_raise("倍率键拼错成 'very high'（顶档倍率取不到，单调性会红但成因指向别处）",
               typo_income_key)

    def break_monotone(d):
        sm = d["generate"]["spend_multiplier"]["income_level"]
        sm["medium"], sm["very_high"] = sm["very_high"], sm["medium"]
    must_raise("收入倍率不再单调（正是审计里 'medium 档最低' 那个形态）", break_monotone)

    def flip_outranks(d):
        sm = d["generate"]["spend_multiplier"]["occupation"]
        sm["学生"], sm["企业主"] = sm["企业主"], sm["学生"]
    must_raise("学生倍率高于企业主（审计里 '高消费职业垫底、学生更高' 那个形态）",
               flip_outranks)

    def peak_at_old(d):
        d["generate"]["spend_multiplier"]["age_decade"][60] = 9.0
    must_raise("年龄倍率峰值挪到 60 档（审计里 '60 后高于 30 后' 那个形态）", peak_at_old)

    def shrink_effect(d):
        d["generate"]["spend_multiplier"]["gender"] = {
            "female": 1.02, "male": 1.0, "unknown": 0.99}
        d["judge"]["order"] = [r for r in d["judge"]["order"] if r["dim"] != "gender"]
    must_raise("性别倍率缩到 2%（低于通过线 × MARGIN，生成的数据先天过不了自己的检查）",
               shrink_effect)

    def dup_marginal(d):
        d["generate"]["gender_weights"] = {"female": 1, "male": 1, "unknown": 1}
    must_raise("给被卡片钉住的 gender 又写一份人数权重（第二个副本，会静默偏离卡片）",
               dup_marginal)

    def monotone_with_buckets(d):
        for r in d["judge"]["order"]:
            if r["kind"] == "monotone":
                r["buckets"] = ["low", "high", "medium", "very_high"]
    must_raise("monotone 规则自带一份 buckets（顺序的真源只能是 dimensions）",
               monotone_with_buckets)

    def bad_not_generated(d):
        d["dimensions"]["not_generated"]["gender"] = ["unkown"]      # 拼错
    must_raise("not_generated 把 unknown 拼错（那一档会被当成可达，跨度虚报）",
               bad_not_generated)

    def bad_age_bucket(d):
        d["dimensions"]["age"]["bucket"] = "quintile"
    must_raise("age.bucket 写成未实现的 'quintile'（否则会静默按十年档跑）",
               bad_age_bucket)

    def empty_why(d):
        d["judge"]["ranges"]["gender"]["why"] = "太松了"
    must_raise("通过线的 why 写成一句空话（判据的推导必须可复核）", empty_why)

    def unknown_dim(d):
        d["judge"]["ranges"]["city"] = {"min_range": 0.5, "why": "x" * 30}
    must_raise("judge.ranges 多出一个没有取值域的维度 city", unknown_dim)

    def bad_order_bucket(d):
        for r in d["judge"]["order"]:
            if r["kind"] == "outranks":
                # 尾空格。档位名本身必须是真实存在的那个，否则「拼错」和「带空格」两种
                # 成因混在一起，这条反例就变成在验前者。
                r["buckets"] = ["企业主 ", "学生"]
    must_raise("outranks 的档位名带尾空格（匹配不上，那条约束会静默失效）",
               bad_order_bucket)

    print(f"\n全部通过（{npass} 项断言）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="profiles.yaml 加载 / 校验")
    ap.add_argument("--selftest", action="store_true", help="跑断言（L0 用）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    summary(*load())
    print("\n校验通过 ✅（判据见模块 docstring；跑 --selftest 追加反向用例）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
