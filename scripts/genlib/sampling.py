"""
sampling —— 加权采样、分组计数、复合键去重(全向量化)。

替换的 blocker:
- social_domain.py:317 评论找父帖:`[c for c in comments if c["post_id"]==pid]` 每行全表扫 → O(N²)。
  正解:用 np.repeat 按"每个父实体的子计数"展开父 id,O(N)。
- marketing_domain.py:270-271 user_coupons 计数:`sum(1 for uc in ... if ...)` 每行全表扫 → O(N²)。
  正解:预先算好每组配额(counts_to_parent_ids),不在循环里数。
- social_domain.py:96-104 follow 去重用无界 set,30M 用户 ×[20..50] ≈ 10 亿条 tuple → 几百 GB。
  正解:unique_pairs 用 np.unique 对 (a,b) 复合键去重,列式,内存可控。
"""
from __future__ import annotations
import numpy as np


def expand_by_counts(parent_ids: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """
    把每个 parent_id 重复 counts[i] 次,返回展开后的子→父映射数组。
    例:parent=[10,20], counts=[3,1] → [10,10,10,20]。
    这是替换"子表逐行去父表里找/数"的核心原语:父子关系一次性物化成列。
    """
    return np.repeat(parent_ids, counts)


def unique_pairs(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    对 (a,b) 复合键去重(无向?不,这里是有向 pair,如 follower→following)。
    替换无界 Python set。返回去重后的 (a', b')。
    """
    # 用结构化视图把两列拼成一个可比较的复合键,np.unique 去重
    pairs = np.stack([a, b], axis=1)
    # 过滤自指(a==b),如 follow 自己
    mask = a != b
    pairs = pairs[mask]
    uniq = np.unique(pairs, axis=0)
    return uniq[:, 0], uniq[:, 1]


def counts_per_group(rng, n_groups: int, total: int,
                     max_per_group: int | None = None) -> np.ndarray:
    """
    把 total 个子记录尽量均匀(带随机抖动)分配到 n_groups 个父,返回各组计数。
    可选 max_per_group 上限(替换 marketing 里"每用户每券最多 N 张"的 O(N²) 计数检查)。
    """
    if n_groups <= 0:
        return np.array([], dtype=np.int64)
    base = np.full(n_groups, total // n_groups, dtype=np.int64)
    base[: total % n_groups] += 1
    rng.shuffle(base)
    if max_per_group is not None:
        np.clip(base, 0, max_per_group, out=base)
    return base
