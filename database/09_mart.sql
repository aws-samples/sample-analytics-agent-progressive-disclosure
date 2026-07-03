-- ============================================
-- 治理层 / 数据集市 (Mart Layer) - 4 张预聚合表
-- ============================================
-- 这一层模拟"治理之后"的数据集:原始 35 张表里的脏活(多表 join、
-- 口径判断、去重)在这里一次性做完,结果固化成几张干净的宽表/事实表。
-- 这段 SQL 本身就是"text-to-ETL 的成品答案"。
--
-- 上层 AI 在这一层做的是 text-to-insight:对干净表写简单 SELECT,
-- 自由切片做归因、综合判断,而不是每次从原始表重推逻辑。
--
-- 冻结的官方口径(原始模式下 AI 每次要现推、可能推错的东西):
--   · GMV      = sum(orders.actual_amount) WHERE status IN ('paid','shipped','delivered')
--                (实付口径,排除退款/取消)
--   · 新客      = 按 users.registered_at 当天;新客订单 = 用户的首笔有效订单
--   · 渠道归因  = last_touch(user_attributions.attribution_type='last_touch',每用户取最近一条)
--   · CAC      = 渠道成本 / 该渠道归因新客数
--   · 复购      = 首笔有效订单后 30 天内再次产生有效订单
--
-- 本文件由 docker-init.sh 在原始表灌数完成之后执行(CTAS 依赖原始数据)。
-- 全部 DROP IF EXISTS + CREATE TABLE AS,可重复执行。
-- ============================================

-- 公共口径:每用户的 last_touch 归因渠道(取最近一条)
-- 下面每张表各自内联了同样的逻辑,保持单文件可独立重跑。

-- --------------------------------------------
-- 1. mart_daily_kpi —— 每日业务大盘(粒度:dt)
--    服务场景:综合周报判断("这周业务咋样")
-- --------------------------------------------
DROP TABLE IF EXISTS mart_daily_kpi;
CREATE TABLE mart_daily_kpi AS
WITH bounds AS (
    SELECT LEAST(
               (SELECT min(event_time)::date   FROM events),
               (SELECT min(placed_at)::date    FROM orders),
               (SELECT min(registered_at)::date FROM users)
           ) AS d0,
           GREATEST(
               (SELECT max(event_time)::date   FROM events),
               (SELECT max(placed_at)::date    FROM orders),
               (SELECT max(registered_at)::date FROM users)
           ) AS d1
),
spine AS (
    SELECT generate_series(d0, d1, interval '1 day')::date AS dt FROM bounds
),
dau AS (
    SELECT event_time::date AS dt, count(DISTINCT user_id) AS dau
    FROM events GROUP BY 1
),
nu AS (
    SELECT registered_at::date AS dt, count(*) AS new_users
    FROM users GROUP BY 1
),
ord AS (
    SELECT placed_at::date AS dt,
           count(*)                       AS orders,
           count(DISTINCT user_id)        AS paying_users,
           sum(actual_amount)             AS gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY 1
),
rf AS (
    SELECT refunded_at::date AS dt, sum(actual_amount) AS refund_amt
    FROM orders
    WHERE status = 'refunded' AND refunded_at IS NOT NULL
    GROUP BY 1
),
sub AS (
    SELECT start_date AS dt, count(*) AS new_subscriptions
    FROM subscriptions GROUP BY 1
)
SELECT s.dt,
       COALESCE(dau.dau, 0)                       AS dau,
       COALESCE(nu.new_users, 0)                  AS new_users,
       COALESCE(ord.orders, 0)                    AS orders,
       COALESCE(ord.paying_users, 0)              AS paying_users,
       COALESCE(ord.gmv, 0)::numeric(14,2)        AS gmv,
       COALESCE(rf.refund_amt, 0)::numeric(14,2)  AS refund_amt,
       COALESCE(sub.new_subscriptions, 0)         AS new_subscriptions
FROM spine s
LEFT JOIN dau ON dau.dt = s.dt
LEFT JOIN nu  ON nu.dt  = s.dt
LEFT JOIN ord ON ord.dt = s.dt
LEFT JOIN rf  ON rf.dt  = s.dt
LEFT JOIN sub ON sub.dt = s.dt
ORDER BY s.dt;

COMMENT ON TABLE mart_daily_kpi IS
'每日业务大盘。粒度=自然日。gmv=实付口径有效订单;dau=events去重;new_users=注册;new_subscriptions=订阅start_date。';

-- --------------------------------------------
-- 2. mart_daily_revenue —— 收入事实表(粒度:dt × 渠道 × 新老客)
--    服务场景:自动归因("上周 GMV 为什么跌")
--    AI 在这一张表上连发多角度切片(按渠道、按新老客)定位原因。
-- --------------------------------------------
DROP TABLE IF EXISTS mart_daily_revenue;
CREATE TABLE mart_daily_revenue AS
WITH user_channel AS (   -- 每用户的 last_touch 归因渠道
    SELECT DISTINCT ON (ua.user_id) ua.user_id, ua.channel_id
    FROM user_attributions ua
    WHERE ua.attribution_type = 'last_touch'
    ORDER BY ua.user_id, ua.attributed_at DESC NULLS LAST
),
first_order AS (         -- 每用户的首笔有效订单(= 新客订单)
    SELECT DISTINCT ON (user_id) order_id, user_id
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    ORDER BY user_id, placed_at ASC, order_id ASC
)
SELECT o.placed_at::date                                    AS dt,
       COALESCE(c.channel_id, 0)                            AS channel_id,
       COALESCE(c.channel_name, '未归因')                   AS channel_name,
       COALESCE(c.channel_type, 'unknown')                  AS channel_type,
       (fo.order_id IS NOT NULL)                            AS is_new_user,
       count(*)                                             AS order_cnt,
       count(DISTINCT o.user_id)                            AS paying_user_cnt,
       sum(o.actual_amount)::numeric(14,2)                  AS gmv
FROM orders o
LEFT JOIN user_channel uc ON uc.user_id = o.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN first_order fo  ON fo.order_id = o.order_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 5;

COMMENT ON TABLE mart_daily_revenue IS
'收入事实表。粒度=日×渠道×新老客。渠道=last_touch归因;is_new_user=该用户首笔有效订单;gmv=实付。GMV合计可对齐 mart_daily_kpi.gmv。';

-- --------------------------------------------
-- 3. mart_channel_daily —— 渠道效果表(粒度:dt × 渠道)
--    服务场景:渠道 CAC / ROI
--    CAC = cost / nullif(new_users_attributed,0)
--    ROI = gmv_attributed / nullif(cost,0)
-- --------------------------------------------
DROP TABLE IF EXISTS mart_channel_daily;
CREATE TABLE mart_channel_daily AS
WITH user_channel AS (
    SELECT DISTINCT ON (ua.user_id) ua.user_id, ua.channel_id
    FROM user_attributions ua
    WHERE ua.attribution_type = 'last_touch'
    ORDER BY ua.user_id, ua.attributed_at DESC NULLS LAST
),
cost AS (
    SELECT date AS dt, channel_id,
           sum(impressions)        AS impressions,
           sum(clicks)             AS clicks,
           sum(installs)           AS installs,
           sum(cost)::numeric(14,2) AS cost
    FROM channel_daily_costs
    GROUP BY 1, 2
),
new_user_by_channel AS (   -- 按注册日 + 归因渠道统计新客
    SELECT u.registered_at::date AS dt, uc.channel_id,
           count(*) AS new_users_attributed
    FROM users u
    JOIN user_channel uc ON uc.user_id = u.user_id
    GROUP BY 1, 2
),
gmv_by_channel AS (        -- 按下单日 + 归因渠道统计 GMV
    SELECT o.placed_at::date AS dt, uc.channel_id,
           sum(o.actual_amount)::numeric(14,2) AS gmv_attributed
    FROM orders o
    JOIN user_channel uc ON uc.user_id = o.user_id
    WHERE o.status IN ('paid','shipped','delivered')
    GROUP BY 1, 2
),
keys AS (
    SELECT dt, channel_id FROM cost
    UNION SELECT dt, channel_id FROM new_user_by_channel
    UNION SELECT dt, channel_id FROM gmv_by_channel
)
SELECT k.dt,
       c.channel_id, c.channel_name, c.channel_type,
       COALESCE(cost.cost, 0)::numeric(14,2)         AS cost,
       COALESCE(cost.impressions, 0)                 AS impressions,
       COALESCE(cost.clicks, 0)                      AS clicks,
       COALESCE(cost.installs, 0)                    AS installs,
       COALESCE(nu.new_users_attributed, 0)          AS new_users_attributed,
       COALESCE(g.gmv_attributed, 0)::numeric(14,2)  AS gmv_attributed
FROM keys k
JOIN channels c ON c.channel_id = k.channel_id
LEFT JOIN cost                ON cost.dt = k.dt AND cost.channel_id = k.channel_id
LEFT JOIN new_user_by_channel nu ON nu.dt = k.dt AND nu.channel_id = k.channel_id
LEFT JOIN gmv_by_channel g    ON g.dt = k.dt AND g.channel_id = k.channel_id
ORDER BY k.dt, c.channel_id;

COMMENT ON TABLE mart_channel_daily IS
'渠道效果表。粒度=日×渠道。cost来自channel_daily_costs;归因=last_touch。CAC=cost/new_users_attributed;ROI=gmv_attributed/cost。';

-- --------------------------------------------
-- 4. mart_user_summary —— 用户汇总表(粒度:用户)
--    服务场景:复购率(口径守门)、LTV、cohort
-- --------------------------------------------
DROP TABLE IF EXISTS mart_user_summary;
CREATE TABLE mart_user_summary AS
WITH user_channel AS (
    SELECT DISTINCT ON (ua.user_id) ua.user_id, ua.channel_id
    FROM user_attributions ua
    WHERE ua.attribution_type = 'last_touch'
    ORDER BY ua.user_id, ua.attributed_at DESC NULLS LAST
),
paid AS (
    SELECT user_id,
           min(placed_at)                  AS first_paid_ts,
           count(*)                        AS paid_order_cnt,
           sum(actual_amount)::numeric(14,2) AS total_gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY user_id
),
second_order AS (   -- 第二笔有效订单时间(用于复购判断)
    SELECT user_id,
           (array_agg(placed_at ORDER BY placed_at))[2] AS second_paid_ts
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY user_id
),
last_act AS (
    SELECT user_id, max(event_time) AS last_active FROM events GROUP BY user_id
)
SELECT u.user_id,
       u.registered_at::date                          AS register_date,
       COALESCE(c.channel_name, '未归因')             AS register_channel,
       p.first_paid_ts::date                          AS first_paid_date,
       COALESCE(p.paid_order_cnt, 0)                  AS paid_order_cnt,
       COALESCE(p.total_gmv, 0)::numeric(14,2)        AS total_gmv,
       (so.second_paid_ts IS NOT NULL
        AND so.second_paid_ts <= p.first_paid_ts + interval '30 days') AS is_repurchaser_30d,
       la.last_active::date                           AS last_active_date
FROM users u
LEFT JOIN user_channel uc ON uc.user_id = u.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN paid p          ON p.user_id = u.user_id
LEFT JOIN second_order so ON so.user_id = u.user_id
LEFT JOIN last_act la     ON la.user_id = u.user_id;

COMMENT ON TABLE mart_user_summary IS
'用户汇总表。粒度=用户。register_channel=last_touch;复购口径=首单后30天内第二笔有效订单。复购率=avg(is_repurchaser_30d) over 有首单用户。';

-- --------------------------------------------
-- 轻量索引(表很小,主要为示例查询体验)
-- --------------------------------------------
CREATE INDEX idx_mart_daily_revenue_dt ON mart_daily_revenue(dt);
CREATE INDEX idx_mart_channel_daily_dt ON mart_channel_daily(dt);
CREATE INDEX idx_mart_user_summary_chan ON mart_user_summary(register_channel);
