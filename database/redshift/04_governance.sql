-- ============================================
-- 治理层：把护栏从应用层下推到数据层
-- ============================================
-- 迁移前，agent 的护栏全在应用代码里（backend/db.py 的正则挡写操作、只读连接、
-- metric_layer 冻结口径）。能演，但审计视角下是"单层、应用自证"。
--
-- Redshift 让同一件事有了数据层的硬约束。本文件建两层，**任一层失效时另一层仍在**：
--
--   第 1 层｜权限不给：agent 角色只在需要的表上有 SELECT。
--   第 2 层｜脱敏兜底：真读到了也是掩码值。
--
-- 两层都在**查询时**生效。这里刻意**没有**「目录不可见」那一层：早期设计假设
-- 「挂了 DDM 的表不会被注册进 Glue Data Catalog」，实测推翻了——这些表在
-- Glue 的 GetTables 列表里照常出现，连 email / phone / birth_date 的列名都在，
-- 只有单表 GetTable 会报 EntityNotFound。svv_attached_masking_policy 里
-- is_masking_datashare_on = 't' 也反证了它们是被共享出去的，不是被排除。
--
-- 结论：Glue federated catalog 是 schema 投影，不是权限投影，它既不反映 GRANT
-- 也不完整反映 DDM。别把目录可见性当访问控制。细节见
-- docs/architecture-v2-redshift-glue.md 第三节与 scripts/glue/reconcile.py。
-- ============================================

-- 注：本文件的 DROP / DETACH 语句用于可重复执行。Redshift 的 DROP ROLE 与
-- DROP MASKING POLICY 不支持 IF EXISTS，首次运行时它们会报「不存在」，
-- 用 rsql 的 --tolerate 'does not exist|not exist|已不存在' 放过即可。
-- 重跑（数据重灌后恢复 mart/derived 授权）时角色已存在且持有基表授权，
-- DROP ROLE 会报 cannot be dropped、CREATE ROLE 会报 already exists，
-- 把这两个 pattern 也加进 --tolerate：跳过这两条不影响后面的 GRANT（幂等）与脱敏重挂。

-- --------------------------------------------
-- 1. agent 的只读角色
-- --------------------------------------------
DROP ROLE analytics_agent_ro;
CREATE ROLE analytics_agent_ro;

-- 治理层（mart / 派生层）是 agent 的主路径：口径已冻结，写简单 SELECT 即可
GRANT SELECT ON mart_daily_kpi      TO ROLE analytics_agent_ro;
GRANT SELECT ON mart_daily_revenue  TO ROLE analytics_agent_ro;
GRANT SELECT ON mart_channel_daily  TO ROLE analytics_agent_ro;
GRANT SELECT ON mart_user_summary   TO ROLE analytics_agent_ro;
GRANT SELECT ON meta_snapshot       TO ROLE analytics_agent_ro;
GRANT SELECT ON dwd_orders_valid    TO ROLE analytics_agent_ro;
GRANT SELECT ON dwd_events_app      TO ROLE analytics_agent_ro;
GRANT SELECT ON dws_user_daily      TO ROLE analytics_agent_ro;
GRANT SELECT ON dws_channel_weekly  TO ROLE analytics_agent_ro;
GRANT SELECT ON fin_daily_revenue   TO ROLE analytics_agent_ro;
GRANT SELECT ON growth_daily_gmv    TO ROLE analytics_agent_ro;

-- 明细表：取数/探口径要用，给 SELECT。只有 data_classification.yaml 中明确标为
-- treatment: mask 的列由第 2 层脱敏兜底；其余敏感候选列的保留理由也在清单中显式记录，
-- 覆盖完整性由 scripts/glue/reconcile.py 的 G 类检查保证。
GRANT SELECT ON users, user_profiles, user_devices, user_segment_members TO ROLE analytics_agent_ro;
GRANT SELECT ON sessions, events, page_views TO ROLE analytics_agent_ro;
GRANT SELECT ON posts, post_likes, post_comments, post_shares, user_follows TO ROLE analytics_agent_ro;
GRANT SELECT ON orders, order_items, payments, subscriptions TO ROLE analytics_agent_ro;
GRANT SELECT ON user_attributions, user_coupons, push_notifications TO ROLE analytics_agent_ro;
GRANT SELECT ON ab_tests, ab_test_variants, ab_test_assignments TO ROLE analytics_agent_ro;
GRANT SELECT ON categories, products, product_tags, channels, event_definitions TO ROLE analytics_agent_ro;
GRANT SELECT ON user_segments, campaigns, coupons, banners TO ROLE analytics_agent_ro;
GRANT SELECT ON ad_campaigns, ad_creatives, channel_daily_costs TO ROLE analytics_agent_ro;

-- 私信正文不给：这不是分析素材，是通信内容。**连表都不授权**，比脱敏更彻底。
-- （user_messages 刻意不出现在上面任何一条 GRANT 里）

-- --------------------------------------------
-- 2. PII 脱敏策略
-- --------------------------------------------
-- 输入类型必须与列类型精确匹配（Redshift 要求 input/output 类型一致）。
-- 注意本库列宽是 Postgres 定义的 4 倍：Redshift VARCHAR 按字节计长，中文一字 3 字节。
DETACH MASKING POLICY mask_email ON users(email) FROM PUBLIC;
DROP MASKING POLICY mask_email;
CREATE MASKING POLICY mask_email
WITH (email VARCHAR(400))
USING ('***@masked.invalid'::VARCHAR(400));

DETACH MASKING POLICY mask_phone ON users(phone) FROM PUBLIC;
DROP MASKING POLICY mask_phone;
CREATE MASKING POLICY mask_phone
WITH (phone VARCHAR(80))
USING (SUBSTRING(phone, 1, 3) || '****' || SUBSTRING(phone, 8, 4));

DETACH MASKING POLICY mask_birthdate ON user_profiles(birth_date) FROM PUBLIC;
DROP MASKING POLICY mask_birthdate;
CREATE MASKING POLICY mask_birthdate
WITH (birth_date DATE)
USING (DATE_TRUNC('year', birth_date)::DATE);   -- 只保留出生年，够做年龄段分析

-- TO PUBLIC：对所有访问者生效，不区分是谁连进来的。
-- 这一条是本次治理最有说服力的地方——不是"我们叮嘱模型别查 PII"，
-- 而是查了也拿不到明文。
ATTACH MASKING POLICY mask_email     ON users(email)             TO PUBLIC;
ATTACH MASKING POLICY mask_phone     ON users(phone)             TO PUBLIC;
ATTACH MASKING POLICY mask_birthdate ON user_profiles(birth_date) TO PUBLIC;
