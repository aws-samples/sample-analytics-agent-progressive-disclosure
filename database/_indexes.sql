-- ============================================
-- 巨表索引(通用)—— 灌数之后建(POST-LOAD)
-- ============================================
-- 从 exchange 场景的 10_indexes.sql 吸收。落实「先灌数后建索引」:巨表边灌边维护
-- B-tree 极慢,COPY 完再一次性建快得多。故本文件必须在 COPY 之后、与 _partitions.sql 分离。
--
-- 索引清单从 idx_registry 读,新增分区胖事实表只需往里登记若干行。
-- 在分区父表上建索引会自动级联到所有子分区(含 DEFAULT)。幂等:IF NOT EXISTS。

-- 前置:idx_registry 由 _registries.sql 建好,扩表 DDL/生成器已往里登记索引。
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT idx_name, tbl, cols FROM idx_registry ORDER BY idx_name LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (%s)', r.idx_name, r.tbl, r.cols);
    END LOOP;
END $$;
