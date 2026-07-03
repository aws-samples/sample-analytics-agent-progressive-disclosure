-- ============================================
-- 分区 bootstrap(通用)—— 灌数的硬前提(PRE-LOAD)
-- ============================================
-- 从 exchange 场景的 09_partitions.sql 吸收并泛化:不再硬编码父表名,而是从一张
-- 注册表 part_registry 读取"哪些表按月 RANGE 分区、分区键叫什么",对每张表动态建
-- 月子分区 + DEFAULT 兜底分区。这样新增分区胖事实表只需往 part_registry 插一行。
--
-- 运行顺序契约(全仓统一):
--   建父表/维表 → _partitions.sql(本文件,COPY 前)→ COPY 灌数 → _indexes.sql(COPY 后)
-- 裸父表无子分区时 INSERT/COPY 会报 "no partition of relation found for row"。
--
-- 设计要点(沿用 exchange 已验证的实现):
--   1. 按月 RANGE,DO 循环按可配置时间窗 [v_start, v_end) 动态生成。
--   2. 每张表挂 DEFAULT 兜底分区:窗口外的行也不会插入失败。
--   3. 幂等:全部 IF NOT EXISTS。

-- 前置:part_registry 由 _registries.sql 建好,扩表 DDL/生成器已往里登记父表。
DO $$
DECLARE
    -- ⬇️ 覆盖生成器产出的数据时间范围(含首尾月)。默认宽松窗口。
    v_start DATE := '2025-01-01';
    v_end   DATE := '2027-01-01';   -- 不含
    p          RECORD;
    m          DATE;
    part_name  TEXT;
BEGIN
    FOR p IN SELECT parent_table FROM part_registry ORDER BY parent_table LOOP
        m := v_start;
        WHILE m < v_end LOOP
            part_name := format('%s_%s', p.parent_table, to_char(m, 'YYYY_MM'));
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part_name, p.parent_table,
                to_char(m, 'YYYY-MM-DD'),
                to_char(m + INTERVAL '1 month', 'YYYY-MM-DD')
            );
            m := m + INTERVAL '1 month';
        END LOOP;
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %I DEFAULT',
                       p.parent_table || '_default', p.parent_table);
    END LOOP;
END $$;
