"""
textpool —— 预生成文本池,O(1) 向量化取用。

替换的 blocker:旧生成器每行调 fake.user_name()/email()/sentence() 等。Faker 每次调用
~10-50µs,几千万行 ×多列 = 数小时墙钟。正解:预生成一个有限大小的池(几千~几万条),
然后用整型索引向量化地从池里取 —— 文本有重复是可接受的(真实数据本就有重名/模板文案),
但生成成本从 O(rows) 降到 O(pool_size)。

不依赖 Faker:用可组合的词缀拼装,纯 numpy 索引,确定性。
"""
from __future__ import annotations
import numpy as np

_ADJ = ['quick', 'lazy', 'happy', 'cool', 'super', 'mega', 'tiny', 'bold',
        'silent', 'lucky', 'wild', 'calm', 'bright', 'dark', 'red', 'blue']
_NOUN = ['fox', 'panda', 'tiger', 'eagle', 'whale', 'koala', 'lion', 'wolf',
         'otter', 'hawk', 'bear', 'crane', 'lynx', 'moth', 'newt', 'ray']
_DOMAINS = ['example.com', 'mail.com', 'test.org', 'demo.net', 'inbox.io']
_WORDS = ['great', 'love', 'nice', 'buy', 'fast', 'cheap', 'quality', 'ship',
          'size', 'color', 'style', 'value', 'gift', 'sale', 'new', 'hot']


def build_username_pool(rng: np.random.Generator, size: int) -> np.ndarray:
    """返回 size 个 username 文本的池(object ndarray)。"""
    adj = rng.choice(_ADJ, size=size)
    noun = rng.choice(_NOUN, size=size)
    num = rng.integers(0, 10000, size=size)
    return np.array([f'{a}_{n}{d}' for a, n, d in zip(adj, noun, num)], dtype=object)


def build_email_pool(usernames: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """基于 username 池派生 email 池。"""
    dom = rng.choice(_DOMAINS, size=len(usernames))
    return np.array([f'{u}@{d}' for u, d in zip(usernames, dom)], dtype=object)


def build_sentence_pool(rng: np.random.Generator, size: int,
                        words: int = 6) -> np.ndarray:
    """返回 size 条短句的池(评论/文案用)。"""
    out = []
    for _ in range(size):
        w = rng.choice(_WORDS, size=words)
        out.append(' '.join(w))
    return np.array(out, dtype=object)


def pick(pool: np.ndarray, rng: np.random.Generator, n: int) -> np.ndarray:
    """从池里向量化取 n 个(有放回)。O(n) 索引,不调 Faker。"""
    idx = rng.integers(0, len(pool), size=n)
    return pool[idx]
