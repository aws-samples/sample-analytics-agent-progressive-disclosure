-- ============================================
-- 注册表(早建)—— 分区与索引的元数据来源
-- ============================================
-- part_registry / idx_registry 必须在 _partitions.sql / _indexes.sql 之前存在,
-- 这样扩表 DDL 与生成器可以先往里登记,两个消费脚本再据此动态建分区/索引。
-- 幂等:IF NOT EXISTS。

-- 需要按月 RANGE 分区的父表登记处。
CREATE TABLE IF NOT EXISTS part_registry (
    parent_table TEXT PRIMARY KEY,   -- 已 CREATE ... PARTITION BY RANGE(part_key) 的父表
    part_key     TEXT NOT NULL        -- 分区键列名
);

-- 待建索引登记处(灌数后统一建)。
CREATE TABLE IF NOT EXISTS idx_registry (
    idx_name TEXT PRIMARY KEY,
    tbl      TEXT NOT NULL,
    cols     TEXT NOT NULL            -- 逗号分隔列名,原样进 CREATE INDEX
);
