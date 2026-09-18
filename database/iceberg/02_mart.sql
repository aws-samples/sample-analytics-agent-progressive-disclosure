/* 集市层 + 派生层 —— S3 Tables（Iceberg）里的 13 张衍生表。
 *
 * 真源：database/09_mart.sql（4 张 mart）、database/10_derived.sql（8 张派生）
 *       外加 meta_snapshot（v2 引入的"今天"锚点，见文件末尾）。
 *
 * 4 + 8 + 1 = 13；加上 01_tables.sql 的 35 张基表，合计 48 张。
 *
 * ⚠️ 这份是**手写的**，不像 01_tables.sql 那样能生成 —— 见下面「为什么不生成」。
 *    手写就必须有人守着：`python3 scripts/lakehouse/verify_mart_parity.py`
 *    会把真源里的过滤谓词逐条比到这份文件上，漏一条就红灯。
 *
 * 执行（catalog 名带斜杠不能进 SQL，必须走 QueryExecutionContext）：
 *     python3 scripts/lakehouse/athena.py --file database/iceberg/02_mart.sql
 *
 * 依赖 01_tables.sql 的 35 张基表已建好且已灌数（本层全靠读它们聚合）。
 * 每张表 DROP + CREATE + INSERT，可重复执行。
 */

/* ============================================================
 * 为什么不生成
 * ============================================================
 * 01_tables.sql 是生成物（gen_ddl.py），因为建表 DDL 是**列清单的机械映射**。
 * 这一层不是：Postgres → Trino 有三处**结构性**改写，不是替换 token 能做到的 ——
 *
 *   1. generate_series(d0, d1, interval '1 day')::date  是**投影里的集合返回函数**，
 *      Trino 没有对应写法，必须搬到 FROM 子句：
 *      CROSS JOIN UNNEST(sequence(d0, d1, interval '1' day)) AS t(dt)
 *      （改的是查询的形状，不是某个函数名。）
 *
 *   2. DISTINCT ON (user_id) ... ORDER BY user_id, attributed_at DESC NULLS LAST
 *      要拆成子查询 + ROW_NUMBER() OVER (PARTITION BY ...) = 1，
 *      多出一层嵌套，原来的 ORDER BY 要搬进 OVER 里。
 *
 *   3. (array_agg(placed_at ORDER BY placed_at))[2]  →  element_at(..., 2)
 *      不只是换函数名：Trino 的下标 `[]` **越界会抛异常**
 *      （INVALID_FUNCTION_ARGUMENT: Array subscript must be less than or equal to
 *      array length），而这里"只下过一单的用户"必然越界。element_at 返回 NULL，
 *      这才是 Postgres 的语义。实测两者都跑得通，但只有一个是对的。
 *
 * 写一个能做这三件事的转译器，等于写半个 SQL 前端，它自己的正确性又没人验。
 * 所以这里的选择是：**手写，但加一道谓词对账**。理由见下一段 —— 这个项目里
 * 唯一真出过漂移的地方，恰好就是手写且没人对账的那半边。
 */

/* ============================================================
 * v1 ⟷ v2 的一处真实漂移（本文件按 v1 写，不继承它）
 * ============================================================
 * v2 的 Redshift 集市层是手写的，退款口径在搬运中丢了状态过滤：
 *
 *   v1  database/09_mart.sql:67           WHERE status = 'refunded' AND refunded_at IS NOT NULL
 *   v2  database/redshift/02_mart.sql:67  WHERE refunded_at IS NOT NULL
 *
 * 生成器保证 refunded_at 非空的行只有 status='refunded'（v1 那批 145 行都算出
 * 963560.92；2026-09-17 重灌后 41995 行，两侧同为 9897495.82）—— **数值恰好相同，
 * bug 是潜伏的**，换一批数据也照旧潜伏，因为这个不变式是生成器给的。哪天部分退款
 * 也写 refunded_at（业务上完全正常），v2 就会静默把它算进退款，而没有任何测试会红。
 *
 * 对照组：v2 的 03_derived.sql 是 pg_to_redshift.py 生成的，同一份真源，没有漂移。
 * 一个仓库里，手写的那半边漂了，生成的那半边没漂 —— 这就是 gen_ddl.py 那段
 * 「手写 DDL 的原罪」想说的事，只是这次有了物证。
 *
 * 本文件用 v1 的口径（带状态过滤），并由 verify_mart_parity.py 守住。
 */

/* ============================================================
 * v1 自带的两处缺陷 —— 本文件**照搬**，没有偷偷修
 * ============================================================
 * 验数时这两张表的数对不上"显而易见的期望值"。逐个用 Python 独立复刻 v1 的语义
 * 重算过，两个数**都被精确命中**，所以是 v1 SQL 的性质，不是移植错误。
 * 记在这里，免得下一个人以为是灌数失败。
 *
 * 下面的绝对数字都标了实测日期，**它们会随灌进湖里的那批数据变**（见文末）。
 * 要判断缺陷还在不在，看机制，别背数字。
 *
 * 1. **mart_daily_kpi.refund_amt 会丢掉日期轴以外的退款。**
 *    日期轴（spine）取的是 events / orders.placed_at / users.registered_at 的最早最晚日，
 *    **没算 refunded_at**。而退款天然发生在下单之后，于是轴末之后的退款被
 *    `LEFT JOIN rf ON rf.dt = s.dt` 静默丢弃。v1 那批数据上是这样：
 *
 *        日期轴          = [2025-10-26 .. 2026-01-24]，91 天
 *        退款总额        = 963560.92
 *        mart_daily_kpi  = 925471.33   ← 少 38089.59（8 笔，退款日 01-25 ~ 02-02）
 *        fin_daily_revenue = 963560.92 ← 这张表不挂日期轴，是全的
 *
 *    所以**同一个仓库里两张表的退款额本来就不一致**。要对齐口径就得把
 *    min/max(refunded_at) 也纳入 spine —— 那是改口径，得走评审，不在移植范围内。
 *
 *    ⚠️ 2026-09-17 在全量重灌（scale 427）后实测：这一条**当前批次上不显现**。
 *    日期轴还是 [2025-10-26 .. 2026-01-24]，但 max(refunded_at) 也正好是 2026-01-24，
 *    轴外退款 0 笔，于是三个数全等（9897495.82）。缺陷代码一字未改，只是这批数据
 *    没有轴外退款去触发它。别把"三个数相等"读成"口径已经对齐了"。
 *
 * 2. **dws_channel_weekly.cost 被 join 扇出重复计数。**
 *    `LEFT JOIN user_attributions` 发生在聚合**之前**，一条成本行匹配到 N 条归因
 *    就被复制 N 份，`sum(d.cost)` 于是把同一笔钱数了 N 遍：
 *
 *        v1 那批：channel_daily_costs 合计 1449872.13 → 本表 1758934.34（1.21 倍）
 *        2026-09-17：             合计 4351988.47 → 本表 524993074.37（120.6 倍）
 *
 *    虚高的倍数 = 每条成本行平均匹配到几条归因，所以它跟着归因表的规模走：归因行数
 *    涨 100 倍，虚高就涨 100 倍。这一条在任何非空归因表上都显现。
 *    连带 weekly_cac（= cost / new_users）分子也是虚高的。正确写法是把归因新客数
 *    先在子查询里聚合好再 join，同样属于改口径。
 *
 * 这两条不是本次迁移引入的 —— v2 的 Redshift 版逐字继承了同样的结构（03_derived.sql
 * 由 pg_to_redshift.py 从同一份真源生成，扇出照旧）。写在这里是为了让它们
 * **有名字**：verify_mart_parity.py 的 QUIRKS 把它们钉成恒等式，将来谁修了口径，
 * 对账会红，而不是悄悄换了个数。钉的是**机制不是数值** —— 等号右边用 SQL 现场复刻
 * "只算轴内退款" / "聚合前扇出"，两边一起随数据走，所以重灌不会让它假红；
 * 代价是缺陷一旦像上面第 1 条那样在某批数据上不显现，这条钉子也就暂时没有鉴别力
 * （L5 的 refund clamp 断言踩过这个坑，那边改成钉编译出的 SQL 里有没有 clamp 谓词）。
 */

/* ============================================================
 * 为什么是 CREATE TABLE + INSERT 而不是 CTAS
 * ============================================================
 * v1/v2 用的是 `CREATE TABLE x AS SELECT ...`。Athena 在 S3 Tables 上**支持** CTAS
 * （实测建出来就是 table_type=iceberg / PARQUET / ZSTD），但这里刻意不用，两个理由：
 *
 * 1. **CTAS 无处声明列注释。** 而列注释会落进 Glue 的
 *    StorageDescriptor.Columns[].Comment，`/api/catalog` 和对账脚本读得到。
 *    冻结口径（GMV 怎么算、归因取哪条、复购怎么判）写在 SQL 注释里云上没人看得见，
 *    写在 COMMENT 里才是元数据。这一层存在的全部意义就是这些口径，不能让它只留在
 *    本地文件里。
 *
 * 2. **CTAS 的列类型是推断出来的。** 显式声明才能钉住 decimal(14,2)：金额列一旦被
 *    推成 double，SUM 出来的分位就开始飘，而且不报错。v1 用 `::numeric(14,2)` 表达
 *    同一个意图，这里用列类型 + CAST 双写，比 CTAS 更接近原意。
 *
 * 代价是每张表要把列清单写两遍（DDL 一遍、INSERT 的列清单一遍）。这正是
 * verify_mart_parity.py 要检查列数是否对齐的原因。
 *
 * 表级注释（v1 的 COMMENT ON TABLE，4 处）**在这条路上存不下来**，两条路都堵死：
 *     COMMENT ON TABLE t IS '...'                    → mismatched input 'COMMENT'
 *     ALTER TABLE t SET TBLPROPERTIES ('comment'=...) → Unsupported table property key
 *     glue update_table(Description=...)             → FederationSourceException:
 *                                                       versionToken must not be null
 * （tblproperties 是白名单，任意键都拒；联邦表的 UpdateTable 被转发给 S3Tables，
 *  而 Glue API 没有传 versionToken 的地方。）
 * 所以 v1 那 4 段表级口径被**下放到列注释**里 —— 粒度更细，且真能读回来。
 * 这是相对 Redshift 的一处能力下降，记在这里而不是藏起来。
 */

/* ============================================================
 * Postgres → Trino 的改写清单（本文件实际用到的）
 * ============================================================
 *   x::date / x::numeric(p,s)     →  CAST(x AS date) / CAST(x AS decimal(p,s))
 *                                    Trino 不认 `::`，也不认类型名 numeric
 *   generate_series(...)::date    →  CROSS JOIN UNNEST(sequence(...)) + CAST AS date
 *                                    ⚠️ sequence(date, date, interval '1' day) 返回的是
 *                                    **array(timestamp)** 不是 array(date)，
 *                                    不 CAST 的话 dt 列会变成 timestamp
 *   DISTINCT ON (k) ... ORDER BY  →  ROW_NUMBER() OVER (PARTITION BY k ORDER BY ...) = 1
 *   (array_agg(x ORDER BY x))[2]  →  element_at(array_agg(x ORDER BY x), 2)
 *   date + 7                      →  date + interval '7' day
 *                                    Trino: Cannot apply operator: date + integer
 *   interval '30 days'            →  interval '30' day
 *   CREATE INDEX                  →  删掉（Iceberg 没有二级索引）
 *   末尾 ORDER BY                  →  删掉（写入顺序对 Iceberg 无意义，纯浪费）
 *   JOIN ... USING (c)            →  显式 ON c1.c = c2.c（等价，只为少一处方言差异）
 *
 * **不需要**改写的（Trino 原生支持，实测确认）：
 *   AGG(x) FILTER (WHERE cond)      count(*) / count(DISTINCT x) 两种都行。
 *                                   这一项比 Redshift 强 —— v2 的 pg_to_redshift.py
 *                                   必须把它拆成 CASE WHEN，这里原样保留，
 *                                   离 v1 真源更近。
 *   least() / greatest()、date_trunc('week', d)、UNION（去重）、
 *   FULL OUTER JOIN ... USING、窗口函数、NULLIF、COALESCE、
 *   名为 `date` 的列（裸写、限定、双引号三种都能过）
 */

/* ==================================================================
 * 1. mart_daily_kpi —— 每日业务大盘（粒度：dt）
 *    服务场景：综合周报判断（"这周业务咋样"）
 * ================================================================== */
DROP TABLE IF EXISTS mart_daily_kpi;

CREATE TABLE mart_daily_kpi (
    dt                date          COMMENT '自然日；粒度=dt。日期轴由 events/orders/users 的最早最晚日撑满，无数据的天补 0 而不是缺行',
    dau               bigint        COMMENT '当日活跃用户：events 按 user_id 去重（含未登录? 不含 —— events.user_id 为 NULL 的不计入 DISTINCT）',
    new_users         bigint        COMMENT '当日新客：users.registered_at 落在当天的用户数',
    orders            bigint        COMMENT '当日有效订单数：status IN (paid, shipped, delivered)',
    paying_users      bigint        COMMENT '当日付费用户：有效订单按 user_id 去重',
    gmv               decimal(14,2) COMMENT '冻结口径 GMV = sum(orders.actual_amount) WHERE status IN (paid, shipped, delivered)；实付口径，排除退款/取消',
    refund_amt        decimal(14,2) COMMENT '当日退款额 = sum(actual_amount) WHERE status = refunded AND refunded_at IS NOT NULL；按 refunded_at 归日。⚠️ 日期轴不含 refunded_at，轴末之后的退款会被丢掉，本表退款合计比 fin_daily_revenue 少；要全量退款请查 fin_daily_revenue',
    new_subscriptions bigint        COMMENT '当日新增订阅：subscriptions.start_date 落在当天的条数'
);

INSERT INTO mart_daily_kpi (
    dt, dau, new_users, orders, paying_users, gmv, refund_amt, new_subscriptions
)
WITH bounds AS (
    SELECT least(
               CAST((SELECT min(event_time)   FROM events) AS date),
               CAST((SELECT min(placed_at)    FROM orders) AS date),
               CAST((SELECT min(registered_at) FROM users) AS date)
           ) AS d0,
           greatest(
               CAST((SELECT max(event_time)   FROM events) AS date),
               CAST((SELECT max(placed_at)    FROM orders) AS date),
               CAST((SELECT max(registered_at) FROM users) AS date)
           ) AS d1
),
spine AS (
    /* generate_series 在投影里返回集合，Trino 只能靠 UNNEST 在 FROM 里展开。
     * sequence(date, date, interval) 给的是 array(timestamp)，必须 CAST 回 date。 */
    SELECT CAST(d AS date) AS dt
    FROM bounds CROSS JOIN UNNEST(sequence(d0, d1, interval '1' day)) AS t(d)
),
dau AS (
    SELECT CAST(event_time AS date) AS dt, count(DISTINCT user_id) AS dau
    FROM events GROUP BY 1
),
nu AS (
    SELECT CAST(registered_at AS date) AS dt, count(*) AS new_users
    FROM users GROUP BY 1
),
ord AS (
    SELECT CAST(placed_at AS date) AS dt,
           count(*)                AS orders,
           count(DISTINCT user_id) AS paying_users,
           sum(actual_amount)      AS gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY 1
),
rf AS (
    SELECT CAST(refunded_at AS date) AS dt, sum(actual_amount) AS refund_amt
    FROM orders
    WHERE status = 'refunded' AND refunded_at IS NOT NULL
    GROUP BY 1
),
sub AS (
    SELECT start_date AS dt, count(*) AS new_subscriptions
    FROM subscriptions GROUP BY 1
)
SELECT s.dt,
       COALESCE(dau.dau, 0),
       COALESCE(nu.new_users, 0),
       COALESCE(ord.orders, 0),
       COALESCE(ord.paying_users, 0),
       CAST(COALESCE(ord.gmv, 0) AS decimal(14,2)),
       CAST(COALESCE(rf.refund_amt, 0) AS decimal(14,2)),
       COALESCE(sub.new_subscriptions, 0)
FROM spine s
LEFT JOIN dau ON dau.dt = s.dt
LEFT JOIN nu  ON nu.dt  = s.dt
LEFT JOIN ord ON ord.dt = s.dt
LEFT JOIN rf  ON rf.dt  = s.dt
LEFT JOIN sub ON sub.dt = s.dt;

/* ==================================================================
 * 2. mart_daily_revenue —— 收入事实表（粒度：dt × 渠道 × 新老客）
 *    服务场景：自动归因（"上周 GMV 为什么跌"），AI 在这一张表上连发多角度切片
 * ================================================================== */
DROP TABLE IF EXISTS mart_daily_revenue;

CREATE TABLE mart_daily_revenue (
    dt              date          COMMENT '下单日 = CAST(orders.placed_at AS date)',
    channel_id      int           COMMENT 'last_touch 归因渠道；未归因用户落在 0（channels 里不存在的哨兵值）',
    channel_name    string        COMMENT '渠道名；未归因为 未归因',
    channel_type    string        COMMENT '渠道类型；未归因为 unknown',
    is_new_user     boolean       COMMENT '该订单是否为此用户的首笔有效订单（= 新客订单）',
    order_cnt       bigint        COMMENT '有效订单数',
    paying_user_cnt bigint        COMMENT '付费用户数（按 user_id 去重）',
    gmv             decimal(14,2) COMMENT '实付口径 GMV；全表合计应等于 mart_daily_kpi.gmv 合计'
);

INSERT INTO mart_daily_revenue (
    dt, channel_id, channel_name, channel_type, is_new_user,
    order_cnt, paying_user_cnt, gmv
)
WITH user_channel AS (
    /* 每用户的 last_touch 归因渠道（取最近一条）。
     * DISTINCT ON 是 Postgres 专有，改成 ROW_NUMBER = 1，
     * 原来的 ORDER BY attributed_at DESC NULLS LAST 搬进 OVER。 */
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) WHERE rn = 1
),
first_order AS (
    /* 每用户的首笔有效订单（= 新客订单）。同样是 DISTINCT ON → ROW_NUMBER。 */
    SELECT order_id, user_id FROM (
        SELECT order_id, user_id,
               ROW_NUMBER() OVER (PARTITION BY user_id
                                  ORDER BY placed_at ASC, order_id ASC) AS rn
        FROM orders
        WHERE status IN ('paid','shipped','delivered')
    ) WHERE rn = 1
)
SELECT CAST(o.placed_at AS date),
       COALESCE(c.channel_id, 0),
       COALESCE(c.channel_name, '未归因'),
       COALESCE(c.channel_type, 'unknown'),
       (fo.order_id IS NOT NULL),
       count(*),
       count(DISTINCT o.user_id),
       CAST(sum(o.actual_amount) AS decimal(14,2))
FROM orders o
LEFT JOIN user_channel uc ON uc.user_id = o.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN first_order fo  ON fo.order_id = o.order_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1, 2, 3, 4, 5;

/* ==================================================================
 * 3. mart_channel_daily —— 渠道效果表（粒度：dt × 渠道）
 *    服务场景：渠道 CAC / ROI
 * ================================================================== */
DROP TABLE IF EXISTS mart_channel_daily;

CREATE TABLE mart_channel_daily (
    dt                   date          COMMENT '自然日；key 集合 = 成本日 ∪ 新客注册日 ∪ 下单日 的并集去重',
    channel_id           int           COMMENT '渠道 ID；本表 INNER JOIN channels，所以不含未归因',
    channel_name         string        COMMENT '渠道名',
    channel_type         string        COMMENT '渠道类型',
    cost                 decimal(14,2) COMMENT '当日渠道花费 = sum(channel_daily_costs.cost)',
    impressions          bigint        COMMENT '曝光',
    clicks               bigint        COMMENT '点击',
    installs             bigint        COMMENT '激活',
    new_users_attributed bigint        COMMENT '冻结口径：按 users.registered_at 归日 + last_touch 归因渠道统计的新客数',
    gmv_attributed       decimal(14,2) COMMENT '按下单日 + last_touch 归因渠道统计的实付 GMV。冻结口径 CAC = cost / nullif(new_users_attributed, 0)；ROI = gmv_attributed / nullif(cost, 0)'
);

INSERT INTO mart_channel_daily (
    dt, channel_id, channel_name, channel_type, cost,
    impressions, clicks, installs, new_users_attributed, gmv_attributed
)
WITH user_channel AS (
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) WHERE rn = 1
),
cost AS (
    SELECT d.date AS dt, d.channel_id,
           sum(d.impressions) AS impressions,
           sum(d.clicks)      AS clicks,
           sum(d.installs)    AS installs,
           CAST(sum(d.cost) AS decimal(14,2)) AS cost
    FROM channel_daily_costs d
    GROUP BY 1, 2
),
new_user_by_channel AS (
    SELECT CAST(u.registered_at AS date) AS dt, uc.channel_id,
           count(*) AS new_users_attributed
    FROM users u
    JOIN user_channel uc ON uc.user_id = u.user_id
    GROUP BY 1, 2
),
gmv_by_channel AS (
    SELECT CAST(o.placed_at AS date) AS dt, uc.channel_id,
           CAST(sum(o.actual_amount) AS decimal(14,2)) AS gmv_attributed
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
       CAST(COALESCE(cost.cost, 0) AS decimal(14,2)),
       COALESCE(cost.impressions, 0),
       COALESCE(cost.clicks, 0),
       COALESCE(cost.installs, 0),
       COALESCE(nu.new_users_attributed, 0),
       CAST(COALESCE(g.gmv_attributed, 0) AS decimal(14,2))
FROM keys k
JOIN channels c ON c.channel_id = k.channel_id
LEFT JOIN cost                   ON cost.dt = k.dt AND cost.channel_id = k.channel_id
LEFT JOIN new_user_by_channel nu ON nu.dt   = k.dt AND nu.channel_id   = k.channel_id
LEFT JOIN gmv_by_channel g       ON g.dt    = k.dt AND g.channel_id    = k.channel_id;

/* ==================================================================
 * 4. mart_user_summary —— 用户汇总表（粒度：用户）
 *    服务场景：复购率（口径守门）、LTV、cohort
 * ================================================================== */
DROP TABLE IF EXISTS mart_user_summary;

CREATE TABLE mart_user_summary (
    user_id            bigint        COMMENT '用户 ID；粒度=用户，users 全量左连，无单用户也在表里',
    register_date      date          COMMENT '注册日 = CAST(users.registered_at AS date)',
    register_channel   string        COMMENT '注册渠道 = last_touch 归因；无归因为 未归因',
    first_paid_date    date          COMMENT '首笔有效订单日；无有效订单为 NULL',
    paid_order_cnt     bigint        COMMENT '有效订单数；无单为 0',
    total_gmv          decimal(14,2) COMMENT '累计实付 GMV（= LTV 的收入侧）；无单为 0',
    is_repurchaser_30d boolean       COMMENT '冻结口径 复购 = 首笔有效订单后 30 天内再次产生有效订单。复购率 = avg(is_repurchaser_30d) 且只在有首单的用户上取。注意第二笔订单用 element_at 取，只下过一单时为 NULL 而不是报错',
    last_active_date   date          COMMENT '最后活跃日 = CAST(max(events.event_time) AS date)'
);

INSERT INTO mart_user_summary (
    user_id, register_date, register_channel, first_paid_date,
    paid_order_cnt, total_gmv, is_repurchaser_30d, last_active_date
)
WITH user_channel AS (
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) WHERE rn = 1
),
paid AS (
    SELECT user_id,
           min(placed_at)     AS first_paid_ts,
           count(*)           AS paid_order_cnt,
           CAST(sum(actual_amount) AS decimal(14,2)) AS total_gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY user_id
),
second_order AS (
    /* 第二笔有效订单时间（复购判断）。
     * Postgres 的 (array_agg(...))[2] 越界返回 NULL；Trino 的 [] 越界**抛异常**，
     * 而"只下过一单"的用户必然越界。element_at 才是等价语义。 */
    SELECT user_id,
           element_at(array_agg(placed_at ORDER BY placed_at), 2) AS second_paid_ts
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY user_id
),
last_act AS (
    SELECT user_id, max(event_time) AS last_active FROM events GROUP BY user_id
)
SELECT u.user_id,
       CAST(u.registered_at AS date),
       COALESCE(c.channel_name, '未归因'),
       CAST(p.first_paid_ts AS date),
       COALESCE(p.paid_order_cnt, 0),
       CAST(COALESCE(p.total_gmv, 0) AS decimal(14,2)),
       (so.second_paid_ts IS NOT NULL
        AND so.second_paid_ts <= p.first_paid_ts + interval '30' day),
       CAST(la.last_active AS date)
FROM users u
LEFT JOIN user_channel uc ON uc.user_id = u.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN paid p          ON p.user_id = u.user_id
LEFT JOIN second_order so ON so.user_id = u.user_id
LEFT JOIN last_act la     ON la.user_id = u.user_id;

/* ==================================================================
 * 派生层 DWD 清洗层 —— 来自 database/10_derived.sql
 * ================================================================== */
DROP TABLE IF EXISTS dwd_orders_valid;

CREATE TABLE dwd_orders_valid (
    order_id        bigint        COMMENT '订单 ID',
    order_no        string        COMMENT '订单号',
    user_id         bigint        COMMENT '下单用户',
    status          string        COMMENT '订单状态；本表已过滤为 paid / shipped / delivered',
    total_amount    decimal(12,2) COMMENT '订单原价',
    discount_amount decimal(12,2) COMMENT '优惠金额',
    shipping_fee    decimal(10,2) COMMENT '运费',
    actual_amount   decimal(12,2) COMMENT '实付金额（GMV 的计量列）',
    item_count      int           COMMENT '商品件数',
    coupon_id       int           COMMENT '使用的优惠券',
    placed_at       timestamp     COMMENT '下单时间',
    paid_at         timestamp     COMMENT '支付时间'
);

INSERT INTO dwd_orders_valid (
    order_id, order_no, user_id, status, total_amount, discount_amount,
    shipping_fee, actual_amount, item_count, coupon_id, placed_at, paid_at
)
SELECT order_id, order_no, user_id, status,
       total_amount, discount_amount, shipping_fee, actual_amount,
       item_count, coupon_id, placed_at, paid_at
FROM orders
WHERE status IN ('paid','shipped','delivered');

DROP TABLE IF EXISTS dwd_events_app;

CREATE TABLE dwd_events_app (
    event_id   bigint    COMMENT '事件 ID',
    user_id    bigint    COMMENT '用户；本表已过滤 user_id IS NOT NULL（去掉未登录事件）',
    session_id bigint    COMMENT '会话 ID',
    event_name string    COMMENT '事件名',
    event_time timestamp COMMENT '事件时间',
    page_name  string    COMMENT '页面名'
);

INSERT INTO dwd_events_app (
    event_id, user_id, session_id, event_name, event_time, page_name
)
SELECT event_id, user_id, session_id, event_name, event_time, page_name
FROM events
WHERE user_id IS NOT NULL;

/* ==================================================================
 * 派生层 DWS 汇总层
 * ================================================================== */
DROP TABLE IF EXISTS dws_user_daily;

CREATE TABLE dws_user_daily (
    user_id     bigint        COMMENT '用户；事件侧与订单侧 FULL OUTER JOIN，两边任一有数据就出行',
    dt          date          COMMENT '自然日',
    event_cnt   bigint        COMMENT '当日事件数；无事件为 0',
    session_cnt bigint        COMMENT '当日会话数（session_id 去重）；无事件为 0',
    order_cnt   bigint        COMMENT '当日订单数（**不限状态**，与 paid_amount 口径不同）；无单为 0',
    paid_amount decimal(14,2) COMMENT '当日实付额，只算 status IN (paid, shipped, delivered)；无单为 0'
);

INSERT INTO dws_user_daily (
    user_id, dt, event_cnt, session_cnt, order_cnt, paid_amount
)
WITH ev AS (
    SELECT user_id, CAST(event_time AS date) AS dt,
           count(*) AS event_cnt, count(DISTINCT session_id) AS session_cnt
    FROM events WHERE user_id IS NOT NULL GROUP BY 1, 2
),
od AS (
    /* FILTER (WHERE ...) 是 Trino 原生语法，原样保留。
     * v2 的 Redshift 版必须拆成 CASE WHEN（pg_to_redshift.py 干的活），这里不用。 */
    SELECT user_id, CAST(placed_at AS date) AS dt,
           count(*) AS order_cnt,
           sum(actual_amount) FILTER (
               WHERE status IN ('paid','shipped','delivered')) AS paid_amount
    FROM orders GROUP BY 1, 2
)
SELECT COALESCE(ev.user_id, od.user_id),
       COALESCE(ev.dt, od.dt),
       COALESCE(ev.event_cnt, 0),
       COALESCE(ev.session_cnt, 0),
       COALESCE(od.order_cnt, 0),
       CAST(COALESCE(od.paid_amount, 0) AS decimal(14,2))
FROM ev FULL OUTER JOIN od ON ev.user_id = od.user_id AND ev.dt = od.dt;

DROP TABLE IF EXISTS dws_channel_weekly;

CREATE TABLE dws_channel_weekly (
    channel_id  int           COMMENT '渠道 ID',
    channel_name string       COMMENT '渠道名',
    week_start  date          COMMENT '周起始日 = date_trunc(week, channel_daily_costs.date)，周一为一周之始',
    cost        decimal(14,2) COMMENT '本周花费。⚠️ 虚高：归因表在聚合前 LEFT JOIN，一条成本行匹配 N 条归因就被数 N 遍。虚高的倍数 = 每条成本行平均匹配到几条归因，所以它随数据规模变（2026-09-17 实测 120.6 倍，v1 那批 1.21 倍）——记机制不要记数字。要准确花费请查 channel_daily_costs 或 mart_channel_daily',
    installs    bigint        COMMENT '本周激活。⚠️ 同 cost，被 join 扇出重复计数',
    new_users   bigint        COMMENT '本周 last_touch 归因新客（按 attributed_at 落在 [week_start, week_start+7) 判定）。这一列用了 count(DISTINCT)，不受扇出影响',
    weekly_cac  decimal(12,2) COMMENT '周 CAC = cost / nullif(new_users, 0)；无归因新客时为 NULL 而不是 0。⚠️ 两件事：① 分子 cost 虚高，此列同样偏高；② NULL 集中在业务日历之外——成本轴铺到 2026-09-01 而归因止于 2026-01-24，那些周有花费零新客，NULL 是正确答案（2026-09-17 实测 414 行里 288 行 NULL，全部 week_start 大于 2026-01-24；日历内 126 行全部有值）。要干净的序列加 week_start <= (SELECT max(as_of_date) FROM meta_snapshot)'
);

INSERT INTO dws_channel_weekly (
    channel_id, channel_name, week_start, cost, installs, new_users, weekly_cac
)
SELECT c.channel_id, c.channel_name,
       CAST(date_trunc('week', d.date) AS date) AS week_start,
       CAST(sum(d.cost) AS decimal(14,2)),
       sum(d.installs),
       count(DISTINCT ua.user_id),
       CAST(sum(d.cost) / NULLIF(count(DISTINCT ua.user_id), 0) AS decimal(12,2))
FROM channel_daily_costs d
JOIN channels c ON c.channel_id = d.channel_id
LEFT JOIN user_attributions ua
  ON ua.channel_id = d.channel_id
 AND ua.attribution_type = 'last_touch'
 AND CAST(ua.attributed_at AS date) >= CAST(date_trunc('week', d.date) AS date)
 AND CAST(ua.attributed_at AS date) <  CAST(date_trunc('week', d.date) AS date)
                                       + interval '7' day
GROUP BY 1, 2, 3;

/* ==================================================================
 * 派生层 ADS 应用层
 * ================================================================== */
DROP TABLE IF EXISTS fin_daily_revenue;

CREATE TABLE fin_daily_revenue (
    dt            date          COMMENT '自然日；收入侧按 paid_at 归日、退款侧按 refunded_at 归日，两侧 FULL OUTER JOIN',
    gross_revenue decimal(14,2) COMMENT '毛收入 = sum(actual_amount - shipping_fee) WHERE status IN (paid, shipped, delivered) AND paid_at IS NOT NULL。**扣掉运费**，所以不等于 GMV',
    refund_amount decimal(14,2) COMMENT '退款额 = sum(actual_amount) WHERE status = refunded AND refunded_at IS NOT NULL',
    net_revenue   decimal(14,2) COMMENT '净收入 = gross_revenue - refund_amount'
);

INSERT INTO fin_daily_revenue (dt, gross_revenue, refund_amount, net_revenue)
WITH paid AS (
    SELECT CAST(paid_at AS date) AS dt,
           sum(actual_amount - shipping_fee) AS gross_revenue
    FROM orders
    WHERE status IN ('paid','shipped','delivered') AND paid_at IS NOT NULL
    GROUP BY 1
),
refunds AS (
    SELECT CAST(refunded_at AS date) AS dt, sum(actual_amount) AS refund_amount
    FROM orders WHERE status = 'refunded' AND refunded_at IS NOT NULL
    GROUP BY 1
)
SELECT COALESCE(p.dt, r.dt),
       CAST(COALESCE(p.gross_revenue, 0) AS decimal(14,2)),
       CAST(COALESCE(r.refund_amount, 0) AS decimal(14,2)),
       CAST(COALESCE(p.gross_revenue, 0) - COALESCE(r.refund_amount, 0)
            AS decimal(14,2))
FROM paid p FULL OUTER JOIN refunds r ON p.dt = r.dt;

DROP TABLE IF EXISTS growth_daily_gmv;

CREATE TABLE growth_daily_gmv (
    dt           date          COMMENT '下单日 = CAST(orders.placed_at AS date)',
    gmv          decimal(14,2) COMMENT '实付口径 GMV，只算 status IN (paid, shipped, delivered)',
    paid_orders  bigint        COMMENT '有效订单数',
    paying_users bigint        COMMENT '付费用户数（去重）',
    all_orders   bigint        COMMENT '**全部**订单数，不限状态。paid_orders / all_orders 就是当日支付转化'
);

INSERT INTO growth_daily_gmv (dt, gmv, paid_orders, paying_users, all_orders)
SELECT CAST(placed_at AS date),
       CAST(sum(actual_amount) FILTER (
            WHERE status IN ('paid','shipped','delivered')) AS decimal(14,2)),
       count(*) FILTER (WHERE status IN ('paid','shipped','delivered')),
       count(DISTINCT user_id) FILTER (
            WHERE status IN ('paid','shipped','delivered')),
       count(*)
FROM orders
GROUP BY 1;

/* ==================================================================
 * 历史遗留（刻意保留的"脏"表，用来考验 agent 会不会误用）
 * ================================================================== */
DROP TABLE IF EXISTS orders_backup_20251201;

CREATE TABLE orders_backup_20251201 (
    order_id      bigint        COMMENT '订单 ID',
    order_no      string        COMMENT '订单号',
    user_id       bigint        COMMENT '下单用户',
    status        string        COMMENT '订单状态（**未过滤**，含 cancelled / refunded）',
    total_amount  decimal(12,2) COMMENT '订单原价',
    actual_amount decimal(12,2) COMMENT '实付金额',
    placed_at     timestamp     COMMENT '下单时间'
);

INSERT INTO orders_backup_20251201 (
    order_id, order_no, user_id, status, total_amount, actual_amount, placed_at
)
SELECT order_id, order_no, user_id, status, total_amount, actual_amount, placed_at
FROM orders
WHERE placed_at < DATE '2025-12-01';

DROP TABLE IF EXISTS tmp_campaign_roi_analysis;

CREATE TABLE tmp_campaign_roi_analysis (
    channel_id     int           COMMENT '渠道 ID',
    channel_name   string        COMMENT '渠道名',
    total_cost     decimal(14,2) COMMENT '截止 2026-01-10 的累计花费',
    attributed_gmv decimal(14,2) COMMENT '⚠️ 恒为 NULL —— 这张临时表当初没算完就留下了',
    roi            decimal(8,4)  COMMENT '⚠️ 恒为 NULL —— 同上，别拿它算 ROI'
);

INSERT INTO tmp_campaign_roi_analysis (
    channel_id, channel_name, total_cost, attributed_gmv, roi
)
SELECT c.channel_id, c.channel_name,
       CAST(sum(d.cost) AS decimal(14,2)),
       CAST(NULL AS decimal(14,2)),
       CAST(NULL AS decimal(8,4))
FROM channel_daily_costs d
JOIN channels c ON c.channel_id = d.channel_id
WHERE d.date < DATE '2026-01-10'
GROUP BY 1, 2;

/* ==================================================================
 * meta_snapshot —— 数据"今天"的锚点
 *
 * 静态样本铁律：这份数据是生成的、不动的，所以"今天"必须以 max(dt) 为准，
 * 禁用 current_date。任何"最近 7 天"都要从 as_of_date 往回数，否则真实时间
 * 一走过样本区间，所有近期指标一夜之间全变 0，而 SQL 本身没有错。
 *
 * 依赖 mart_daily_kpi 已灌好，所以放在最后。
 * ================================================================== */
DROP TABLE IF EXISTS meta_snapshot;

CREATE TABLE meta_snapshot (
    as_of_date date COMMENT '数据里的"今天" = max(mart_daily_kpi.dt)。所有相对日期从这里起算，不要用 current_date',
    data_start date COMMENT '数据起始日 = min(mart_daily_kpi.dt)'
);

INSERT INTO meta_snapshot (as_of_date, data_start)
SELECT max(dt), min(dt) FROM mart_daily_kpi;
