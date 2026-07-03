"""
ids —— 单调整型主键 / 外键区间。

替换的 blocker:旧 transaction_domain.py:188/337/385 用 fake.unique.random_number() 造 PK。
fake.unique 维护一个不断增长的"已用值"集合并在碰撞时重试 —— 几千万行时碰撞频繁、
unique 集合本身吃内存,且会抛 UniquenessException。这是硬 blocker。

正解:PK 就是连续整数区间。简单、零碰撞、零内存、对 COPY 友好,也天然适配分区表
(BIGINT 范围可预分配给各域/各分区)。
"""
from __future__ import annotations
import numpy as np


def id_range(start: int, count: int) -> np.ndarray:
    """返回 [start, start+count) 的 int64 PK 数组。"""
    return np.arange(start, start + count, dtype=np.int64)


class IdAllocator:
    """
    跨表的单调 id 分配器。每个表/实体一个命名计数器,从 1 起、连续递增。
    保证全程无碰撞、可复现(只要调用顺序固定)。
    """
    def __init__(self):
        self._next: dict[str, int] = {}

    def allocate(self, name: str, count: int) -> np.ndarray:
        """给 name 这张表分配 count 个连续 id,返回数组,并推进计数器。"""
        start = self._next.get(name, 1)
        self._next[name] = start + count
        return id_range(start, count)

    def peek_max(self, name: str) -> int:
        """当前已分配到的最大 id(用于 setval 序列重置)。"""
        return self._next.get(name, 1) - 1
