"""
genlib —— 向量化数据生成框架。

为什么存在:旧的逐行 + Faker + 全量驻留内存的生成器在 30-50M 行会 OOM / 跑几小时,
且有几个硬 blocker(见各模块 docstring)。genlib 用 numpy 列式生成 + 分块流式 CSV,
把这些 blocker 从根上设计掉,并保证**确定性**(同 seed → 同数据 → oracle 可对照)。

模块:
  rng        —— 统一的 seeded Generator + 确定性采样原语(替换 random.choices over 大列表)
  ids        —— 单调整型 PK / FK 区间(替换 fake.unique 炸弹)
  sampling   —— 加权采样、分组计数、复合键去重(替换 O(N²) 自扫描与无界 set)
  pgcsv      —— 列式 ndarray → Postgres COPY 兼容 CSV,分块流式写(替换全量驻留 + 双拷贝)
  textpool   —— 预生成文本池,O(1) 取用(替换 per-row fake.* 调用)
"""
