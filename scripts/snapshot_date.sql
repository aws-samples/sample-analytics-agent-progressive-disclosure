-- meta_snapshot —— 数据集"今天"的单一锚点。
-- 背景:本库是静态样本(数据落在 2025-10-27 ~ 2026-01-24)。所有"最近/上周/本月"
--       类查询必须以数据自身最大日期为"今天",禁用 current_date/now()(否则查出空)。
-- 这张表把那个锚点物化成单行,mart / oracle / agent 提示词统一从这里读,避免各处各算。
--
-- 锚点口径与 09_mart.sql 的 bounds CTE 完全一致:取 events/orders/users 三个事实时间的
-- 最大值作为 as_of_date(d1),最小值作为 data_start(d0)。
-- 可重复执行(DROP + CTAS)。

DROP TABLE IF EXISTS meta_snapshot;
CREATE TABLE meta_snapshot AS
SELECT
    GREATEST(
        (SELECT max(event_time)::date   FROM events),
        (SELECT max(placed_at)::date    FROM orders),
        (SELECT max(registered_at)::date FROM users)
    ) AS as_of_date,     -- 数据集的"今天"
    LEAST(
        (SELECT min(event_time)::date   FROM events),
        (SELECT min(placed_at)::date    FROM orders),
        (SELECT min(registered_at)::date FROM users)
    ) AS data_start,     -- 数据起点
    now() AS built_at;   -- 这张元数据表的构建时刻(非数据时间)

COMMENT ON TABLE meta_snapshot IS
'数据集时间锚点(单行)。as_of_date = 数据"今天"(events/orders/users 最大事实时间)。查询"最近N天"用 as_of_date,不要用 current_date/now()。';
