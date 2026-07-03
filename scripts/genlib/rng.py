"""
rng —— 统一的 seeded 随机源 + 确定性采样原语。

确定性是 oracle 验证的前提:同一个 seed 必须每次生成完全相同的数据,
这样 mart 物化值才能当 ground truth。所有生成器都从这里取 Generator,
不准各自 import numpy.random 或 Python random(那样无法复现)。

替换的 blocker:
- 旧 transaction_domain.py:191 用 `random.choices(users, weights=...)` 每订单一次,
  对 30M 元素列表是 O(N)/调用 → 几十 M 次调用 = 天级。这里用 numpy 的
  Generator.choice(p=...) 一次性向量化抽样,O(N) 总成本而非 O(N) 每次。
"""
from __future__ import annotations
import numpy as np

DEFAULT_SEED = 42


def make_rng(seed: int = DEFAULT_SEED) -> np.random.Generator:
    """返回一个 PCG64 Generator。全框架唯一的随机源入口。"""
    return np.random.default_rng(seed)


def weighted_choice(rng: np.random.Generator, choices: np.ndarray,
                    weights: np.ndarray, size: int) -> np.ndarray:
    """
    按 weights 从 choices 里有放回抽 size 个,向量化。
    替换旧的 per-row random.choices(...)。weights 不必归一化。
    """
    p = weights / weights.sum()
    return rng.choice(choices, size=size, replace=True, p=p)


def power_law_counts(rng: np.random.Generator, n_entities: int,
                     total: int, alpha: float = 1.3,
                     min_count: int = 0) -> np.ndarray:
    """
    把 total 个事件按幂律分配给 n_entities 个实体(少数实体贡献大头),
    返回长度 n_entities 的 int64 计数数组,sum == total。
    用途:用户活跃度、商品热度这类长尾分布,向量化,无逐行循环。
    """
    # Zipf-ish 权重:rank^-alpha
    ranks = np.arange(1, n_entities + 1, dtype=np.float64)
    w = ranks ** (-alpha)
    rng.shuffle(w)                      # 打散,别让 id 小的总是最热
    p = w / w.sum()
    counts = rng.multinomial(max(total - n_entities * min_count, 0), p)
    return counts + min_count
