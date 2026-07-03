"""
genlib 自测 —— 证明 4 个 blocker-killer 正确 + 确定性 + CSV 可被 Postgres COPY 吃下。
独立可跑:  python scripts/genlib/selftest.py
退出码 0 = 全过。
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from genlib import rng as R
from genlib import ids as I
from genlib import sampling as S
from genlib import pgcsv
from genlib import textpool as TP

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'XX '} {name}")


def test_ids():
    print("[ids] 单调 PK,零碰撞(替换 fake.unique)")
    alloc = I.IdAllocator()
    a = alloc.allocate('orders', 1_000_000)
    b = alloc.allocate('orders', 1_000_000)
    check('orders id 连续且不重叠', a[0] == 1 and a[-1] == 1_000_000 and b[0] == 1_000_001)
    check('无重复(2M)', len(np.unique(np.concatenate([a, b]))) == 2_000_000)
    check('peek_max 正确', alloc.peek_max('orders') == 2_000_000)


def test_weighted():
    print("[rng] 向量化加权抽样(替换 per-row random.choices over 大列表)")
    rng = R.make_rng()
    users = np.arange(1, 1_000_001, dtype=np.int64)
    weights = (np.arange(1, 1_000_001) ** -1.2).astype(np.float64)
    picks = R.weighted_choice(rng, users, weights, size=5_000_000)
    check('抽样规模正确(5M)', len(picks) == 5_000_000)
    check('低 id(高权重)更高频', (picks < 1000).sum() > (picks > 999_000).sum())


def test_powerlaw():
    print("[rng] 幂律计数,sum 守恒")
    rng = R.make_rng()
    counts = R.power_law_counts(rng, n_entities=100_000, total=5_000_000, min_count=1)
    check('计数和 == total', counts.sum() == 5_000_000)
    check('每实体 >= min_count', counts.min() >= 1)


def test_expand():
    print("[sampling] expand_by_counts(替换 O(N²) 父子自扫描)")
    parent = np.array([10, 20, 30], dtype=np.int64)
    counts = np.array([3, 0, 2], dtype=np.int64)
    child2parent = S.expand_by_counts(parent, counts)
    check('展开正确', list(child2parent) == [10, 10, 10, 30, 30])


def test_unique_pairs():
    print("[sampling] unique_pairs(替换无界 follow_set)")
    rng = R.make_rng()
    n = 2_000_000
    a = rng.integers(1, 100_000, size=n)
    b = rng.integers(1, 100_000, size=n)
    ua, ub = S.unique_pairs(a, b)
    check('无自指', (ua != ub).all())
    check('无重复 pair', len(np.unique(np.stack([ua, ub], 1), axis=0)) == len(ua))


def test_determinism():
    print("[determinism] 同 seed → 同数据(oracle 前提)")
    def run():
        rng = R.make_rng(42)
        return R.weighted_choice(rng, np.arange(1, 1001), np.ones(1000), 10000)
    check('两次运行完全相同', np.array_equal(run(), run()))


def test_csv_roundtrip():
    print("[pgcsv] 列式 → CSV(分块流式,COPY 兼容)")
    rng = R.make_rng()
    n = 1_000_000
    base = np.datetime64('2026-01-01T00:00:00')
    cols = {
        'id': I.id_range(1, n),
        'user_id': rng.integers(1, 50_000, size=n),
        'amount': np.round(rng.uniform(1, 1000, size=n), 2),
        'is_active': rng.integers(0, 2, size=n).astype(bool),
        'ts': base + rng.integers(0, 86400 * 30, size=n).astype('timedelta64[s]'),
    }
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    written = pgcsv.write_table(path, cols, chunk_rows=250_000)
    with open(path, encoding='utf-8') as f:
        header = f.readline().strip()
        first = f.readline().strip()
        line_count = 1 + sum(1 for _ in f) + 1  # header + remaining + first
    os.remove(path)
    check('写出行数正确(1M)', written == n)
    check('header 正确', header == 'id,user_id,amount,is_active,ts')
    check('bool 渲染为 true/false', first.split(',')[3] in ('true', 'false'))
    check('文件行数 = 1M + header', line_count == n + 1)


def test_textpool():
    print("[textpool] O(1) 文本池(替换 per-row fake.*)")
    rng = R.make_rng()
    pool = TP.build_username_pool(rng, 5000)
    picks = TP.pick(pool, rng, 2_000_000)
    check('取用规模正确(2M)', len(picks) == 2_000_000)
    check('池大小受限(不随行数增长)', len(pool) == 5000)


if __name__ == '__main__':
    for t in [test_ids, test_weighted, test_powerlaw, test_expand,
              test_unique_pairs, test_determinism, test_csv_roundtrip, test_textpool]:
        t()
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    sys.exit(1 if FAIL else 0)
