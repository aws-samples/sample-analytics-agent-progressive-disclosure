# genlib —— 向量化数据生成框架(P1)

旧生成器(`scripts/generators/*.py`)是逐行 + Faker + 全量驻留内存,在 30-50M 行会 OOM /
跑数小时,且有 4 个硬 blocker。genlib 用 numpy 列式生成 + 分块流式 CSV 把它们从根上设计掉,
并保证**确定性**(同 seed → 同数据 → mart 物化值可当 oracle ground truth)。

## blocker → 解法 对照

| 旧 blocker(已诊断) | genlib 解法 |
|---|---|
| `fake.unique.random_number()` 造 PK(transaction_domain.py:188/337/385)—— 增长 set + 碰撞重试 | `ids.IdAllocator` / `id_range`:连续 int64 区间,零碰撞零内存 |
| per-order `random.choices(users, weights=...)`(transaction_domain.py:191)—— O(N)/调用 | `rng.weighted_choice`:numpy `Generator.choice(p=...)` 一次性向量化 |
| 评论找父帖全表自扫描(social_domain.py:317)—— O(N²) | `sampling.expand_by_counts`:`np.repeat` 按子计数展开父 id,O(N) |
| user_coupons 计数全表扫(marketing_domain.py:270-271)—— O(N²) | `sampling.counts_per_group`:预算各组配额,不在循环里数 |
| 无界 follow 去重 set(social_domain.py:96-104)—— 10 亿 tuple | `sampling.unique_pairs`:`np.unique` 复合键去重,列式 |
| per-row `fake.user_name/email/sentence`—— 数小时墙钟 | `textpool`:预生成有限池,整型索引 O(1) 取用 |
| 全量 data + `flat_data` 双拷贝(export_to_csv.py:87)—— 峰值内存翻倍 | `pgcsv.write_table`:按列持有 + 固定行块流式写,峰值内存与表大小解耦 |

## 模块

- `rng.py` —— `make_rng(seed=42)`(全框架唯一随机源)、`weighted_choice`、`power_law_counts`
- `ids.py` —— `id_range`、`IdAllocator`(跨表单调 PK)
- `sampling.py` —— `expand_by_counts`、`unique_pairs`、`counts_per_group`
- `pgcsv.py` —— `write_table`(列式 → COPY 兼容 CSV,分块)、`to_pg_array`
- `textpool.py` —— `build_*_pool` + `pick`

## 分区/索引骨架(配套)

- `database/_registries.sql` —— 早建 `part_registry` / `idx_registry`
- `database/_partitions.sql` —— 按 part_registry 动态建月分区 + DEFAULT(灌数前,幂等)
- `database/_indexes.sql` —— 按 idx_registry 建索引(灌数后,「先灌数后建索引」)
- 运行顺序:`_registries` → 建父表 → 登记 → `_partitions` → COPY → `_indexes`

## 跑自测

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
.venv/bin/python scripts/genlib/selftest.py   # 17 checks,退出码 0 = 全过
```

验证过(本机 throwaway PG16):genlib 产出的 CSV 能被 `\copy` 正确吃下(BIGINT/DECIMAL/
BOOLEAN/TIMESTAMP/TEXT[] 全对,含逗号的数组元素 `{a,"b,c"}` 转义正确),且行能正确路由到月分区、索引级联到子分区。
