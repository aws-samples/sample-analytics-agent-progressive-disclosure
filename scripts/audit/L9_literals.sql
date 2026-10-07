-- ═══ L9 字面值真实性：文本列是不是占位符 ═══
--
-- ## 跑不动？对，本文件不属于当前项目的现行链路
--
-- 目标环境是 **v2 的 Redshift**，方言也是 Redshift（regexp_substr / regexp_instr /
-- left() / chr()）。当前项目的数据层是 Athena over S3 Tables/Iceberg，SQL 是 Trino，
-- 这些函数一半不存在，直接拿去跑只会报语法错。`scripts/audit/` 整个目录都是 v2 遗留，
-- 见 docs/legacy.md:39：「方法有价值，但连不上现行数据层」；它不被任何 .py/.sh 引用，
-- 也不在 scripts/test_all.sh 里。
--
-- 保留它的理由是**判据本身**——占位符怎么判、哪些列该豁免、同行列间怎么对，这些结论
-- 与后端无关，换到任何 SQL 引擎都成立。所以它在这里是**判据的规范说明书**，不是可执行的
-- 验收脚本。
--
-- 三处落地，按能不能跑区分：
--
--   scripts/gen/selftest_closures.py   ← 能跑，L0 的一环。生成器改完的回归网，
--                                        覆盖全部文本列（含本文件判不了的脱敏列）
--   本文件                             ← 跑不动，仅作 v2 环境的验收方案 + 判据说明
--   scripts/lakehouse/verify_literals.py  ← 还没写。要在**现行** Athena 库上做活体
--                                        验收，得有一份 Trino 版
--
-- 改判据要三处同步改；豁免清单必须与 selftest_closures.SERIAL_OK 逐项一致。
--
-- ────────────────────────────────────────────────────────────────────────
--
-- 数值结论正确、字面值却是 user_1 / post_2 / dev_3 的库，凡是要展示明细行的场景
-- （演示截图、卡片示例、结果表格）一眼可见是假数据。L1–L5 全绿也拦不住这一类，
-- 因为它们查的是数量关系，不看字符串长什么样。
--
-- 判据刻意**不**写成 `^user_\d+$` 这类逐列正则：生成器里的占位符来自类型兜底
-- （见 scripts/gen/fillers.py by_type 最后一行），逐列正则只盖得住今天已知的几列，
-- 明天新加一个未声明的文本列照样静默变占位符。所以判通用形态：
--
--     序号占位符 = 「该列 ≥99% 的值共用同一个前缀」+「前缀后面是纯数字尾」
--
-- 99% 这条线自动放过版本号：os_version 的取值劈出多种前缀，最大前缀够不着门槛。
--
-- ## 为什么生成侧已经断言了，还要有库侧的一份
--
-- 生成侧证明的是「生成器产出的值是真实的」，库侧证明的是「进了库之后还是真实的」。
-- 中间那段路会改字面值：装载时的类型截断（Redshift VARCHAR(n) 按**字节**算，中文
-- 三字节，20 字符的标题会被砍成 6 个汉字）、动态脱敏、ETL 里的 trim/replace。
-- 这些缺陷生成侧一条都看不见。反过来，库侧看不见脱敏列的明文。两侧互补，不是冗余。
--
-- ## 在旧数据上跑会全红——那正是它的用途
--
-- 未重灌的库里 username / posts.title / device_id 等列本来就是占位符，L9.1 会给出
-- 一串 FAIL。重灌后应当只剩 EXEMPT 与 DEFER。
--
-- ## 脱敏列判不了
--
-- v2 对 users.email / users.phone 做动态脱敏，本连接读到的是掩码后的值
-- （`***@masked.invalid` 之类），形态检查对它们没有意义：L9.1 里标 DEFER，
-- 交给 L9.2 / L9.3 两条格式检查；那两条在掩码下自己判 UNDECIDABLE。
-- 要拿到可信结论必须用能读明文的连接跑；否则这两列只能在生成侧验
-- （scripts/gen/selftest_closures.py 有 EMAIL_RE / CN_MOBILE_RE 两条断言）。

-- L9.1 占位符形态普查（全表扫描，events / page_views 较慢）
WITH v AS (
    SELECT 'users.username'              AS col, username        AS val FROM users
    UNION ALL SELECT 'users.email',              email                  FROM users
    UNION ALL SELECT 'users.phone',              phone                  FROM users
    UNION ALL SELECT 'user_profiles.city',       city                   FROM user_profiles
    UNION ALL SELECT 'user_profiles.province',   province               FROM user_profiles
    UNION ALL SELECT 'user_profiles.occupation', occupation             FROM user_profiles
    UNION ALL SELECT 'user_devices.device_id',   device_id              FROM user_devices
    UNION ALL SELECT 'user_devices.device_brand',device_brand           FROM user_devices
    UNION ALL SELECT 'user_devices.device_model',device_model           FROM user_devices
    UNION ALL SELECT 'user_devices.os_version',  os_version             FROM user_devices
    UNION ALL SELECT 'user_devices.app_version', app_version            FROM user_devices
    UNION ALL SELECT 'user_devices.push_token',  push_token             FROM user_devices
    UNION ALL SELECT 'sessions.device_id',       device_id              FROM sessions
    UNION ALL SELECT 'sessions.utm_campaign',    utm_campaign           FROM sessions
    UNION ALL SELECT 'sessions.entry_page',      entry_page             FROM sessions
    UNION ALL SELECT 'sessions.exit_page',       exit_page              FROM sessions
    UNION ALL SELECT 'events.device_id',         device_id              FROM events
    UNION ALL SELECT 'events.page_name',         page_name              FROM events
    UNION ALL SELECT 'events.referrer',          referrer               FROM events
    UNION ALL SELECT 'events.ip_address',        ip_address             FROM events
    UNION ALL SELECT 'page_views.page_name',     page_name              FROM page_views
    UNION ALL SELECT 'page_views.page_url',      page_url               FROM page_views
    UNION ALL SELECT 'posts.title',              title                  FROM posts
    UNION ALL SELECT 'posts.content',            content                FROM posts
    UNION ALL SELECT 'posts.location',           location               FROM posts
    UNION ALL SELECT 'post_comments.content',    content                FROM post_comments
    UNION ALL SELECT 'user_messages.content',    content                FROM user_messages
    UNION ALL SELECT 'push_notifications.title', title                  FROM push_notifications
    UNION ALL SELECT 'push_notifications.content', content              FROM push_notifications
    UNION ALL SELECT 'orders.order_no',          order_no               FROM orders
    UNION ALL SELECT 'orders.remark',            remark                 FROM orders
    UNION ALL SELECT 'order_items.product_name', product_name           FROM order_items
    UNION ALL SELECT 'order_items.sku_name',     sku_name               FROM order_items
    UNION ALL SELECT 'payments.payment_no',      payment_no             FROM payments
    UNION ALL SELECT 'payments.transaction_id',  transaction_id         FROM payments
    UNION ALL SELECT 'user_coupons.coupon_code', coupon_code            FROM user_coupons
    UNION ALL SELECT 'subscriptions.plan_name',  plan_name              FROM subscriptions
),
nonblank AS (
    -- 分母是全部非空值，不是只统计带数字尾的那些：否则「1% 的行恰好以数字结尾且
    -- 前缀相同」会被算成 100%，变成假阳性。
    SELECT col, val FROM v WHERE val IS NOT NULL AND btrim(val) <> ''
),
tot AS (SELECT col, count(*) AS n, count(DISTINCT val) AS uniq FROM nonblank GROUP BY col),
tailed AS (
    SELECT col,
           regexp_replace(val, '[0-9]+$', '') AS prefix,
           regexp_substr(val, '[0-9]+$')      AS tail
    FROM nonblank
    WHERE regexp_instr(val, '[0-9]+$') > 0
),
byprefix AS (
    SELECT col, prefix, count(*) AS c, count(DISTINCT tail) AS tails,
           row_number() OVER (PARTITION BY col ORDER BY count(*) DESC, prefix) AS rk
    FROM tailed GROUP BY col, prefix
),
top1 AS (SELECT col, prefix, c, tails FROM byprefix WHERE rk = 1)
SELECT t.col,
       t.n                                              AS rows_nonblank,
       t.uniq                                           AS distinct_vals,
       coalesce(p.prefix, '')                           AS top_prefix,
       round(100.0 * coalesce(p.c, 0) / t.n, 1)         AS prefix_share_pct,
       coalesce(p.tails, 0)                             AS tail_variants,
       CASE
         -- 邮箱 / 手机号在这里一律不判：脱敏连接读到的是掩码值，形态与生成器无关；
         -- 即便读到明文，形态规则对纯数字的手机号也必然误判（前缀恒为空）。
         -- 判据没有丢，是**委派**给下面 L9.2 / L9.3 两条更严的格式检查。
         WHEN t.col IN ('users.email', 'users.phone') THEN 'DEFER(见 L9.2/L9.3)'
         WHEN coalesce(p.c, 0) * 100 < t.n * 99 THEN 'PASS'
         -- 业务单号天生是「前缀 + 流水号」，那是真实世界的正确写法。
         -- 这四行 + 上面委派掉的 users.phone，正是 selftest_closures.SERIAL_OK 的 5 项。
         WHEN t.col IN ('orders.order_no',
                        'payments.payment_no',
                        'payments.transaction_id',
                        'user_coupons.coupon_code') THEN 'EXEMPT(业务单号)'
         ELSE 'FAIL(占位符)'
       END                                              AS verdict
FROM tot t LEFT JOIN top1 p ON p.col = t.col
ORDER BY prefix_share_pct DESC, t.col;

-- L9.2 邮箱：格式合规 + 域名多样性（真实用户散在多家邮箱，不会全在一个域）
-- 脱敏连接下 domains 恒为 1、verdict 恒 FAIL —— 那是掩码的结果，不是数据缺陷，
-- 判 UNDECIDABLE。要拿到可信结论必须用能读明文的连接跑。
SELECT count(*)                                                      AS n,
       sum(CASE WHEN email NOT LIKE '%_@_%._%' THEN 1 ELSE 0 END)     AS bad_format,
       count(DISTINCT split_part(email, '@', 2))                      AS domains,
       max(CASE WHEN email LIKE '%masked%' THEN 1 ELSE 0 END)         AS looks_masked,
       CASE WHEN max(CASE WHEN email LIKE '%masked%' THEN 1 ELSE 0 END) = 1
              THEN 'UNDECIDABLE(脱敏)'
            WHEN sum(CASE WHEN email NOT LIKE '%_@_%._%' THEN 1 ELSE 0 END) = 0
             AND count(DISTINCT split_part(email, '@', 2)) >= 5 THEN 'PASS'
            ELSE 'FAIL' END                                          AS verdict
FROM users;

-- L9.3 手机号：必须落在真实号段 1[3-9] + 9 位数字
-- 同样受脱敏影响，判据见 L9.2 的说明。
SELECT count(*)                                                          AS n,
       sum(CASE WHEN regexp_instr(phone, '^1[3-9][0-9]{9}$') = 0
                THEN 1 ELSE 0 END)                                       AS bad_format,
       count(DISTINCT left(phone, 3))                                    AS segments,
       CASE WHEN sum(CASE WHEN regexp_instr(phone, '^[0-9]+$') = 0 THEN 1 ELSE 0 END) > 0
              THEN 'UNDECIDABLE(脱敏)'
            WHEN sum(CASE WHEN regexp_instr(phone, '^1[3-9][0-9]{9}$') = 0
                     THEN 1 ELSE 0 END) = 0
             AND count(DISTINCT left(phone, 3)) >= 10 THEN 'PASS'
            ELSE 'FAIL' END                                              AS verdict
FROM users;

-- L9.4 同行列间一致性：推送标题与正文必须成对
-- 替占位符时最容易新造的缺陷是「标题和正文各抽一次词池」，于是出现
-- 标题『包裹已签收』配正文『正在为你打包』。占位符一眼假，自相矛盾的文案
-- 会被当成真数据读进去，更糟。成对则「标题种数 == (标题,正文) 组合种数」。
--
-- 光看 titles = pairs 不够：标题若是 `推送_1`、`推送_2` 这种逐行唯一的占位符，
-- 每个标题天然只对应一条正文，等式恒成立、检查变成空转。所以先要求标题是
-- **有限模板集**（真实 APP 的推送文案就是几十条模板），再看配对。
-- 拼接键的分隔符用 chr(2) 而不是 '|'：文案里真出现 '|' 会让两个不同的配对
-- 拼成同一个键，pairs 偏小、检查漏判。
SELECT push_type,
       count(*)                                             AS n,
       count(DISTINCT title)                                AS titles,
       count(DISTINCT title || chr(2) || content)            AS pairs,
       CASE WHEN count(DISTINCT title) > 50
              THEN 'FAIL(标题非模板集，疑似逐行生成)'
            WHEN count(DISTINCT title) = count(DISTINCT title || chr(2) || content)
              THEN 'PASS'
            ELSE 'FAIL(标题与正文错配)' END                   AS verdict
FROM push_notifications
GROUP BY push_type
ORDER BY push_type;

-- L9.5 同行列间一致性：帖子正文首句必须等于标题核心
-- 生成侧的构造是 title = 核心 + 后缀、content = 核心 + '。' + 补充句，
-- 所以「正文切到第一个句号」应当是标题的前缀。不成立就说明标题与正文各抽了一次，
-- 会出现「标题写加湿器、正文写跑鞋」。
-- 旧数据（title = post_N、content = 内容正文 N）里没有句号也没有共同核心，
-- 会整列判 FAIL —— 预期如此。
SELECT count(*)                                                     AS n,
       sum(CASE WHEN strpos(content, '。') = 0 THEN 1 ELSE 0 END)   AS no_sentence_end,
       sum(CASE WHEN strpos(content, '。') = 0
                  OR strpos(title, split_part(content, '。', 1)) <> 1
                THEN 1 ELSE 0 END)                                  AS mismatch,
       CASE WHEN sum(CASE WHEN strpos(content, '。') = 0
                            OR strpos(title, split_part(content, '。', 1)) <> 1
                          THEN 1 ELSE 0 END) = 0
            THEN 'PASS' ELSE 'FAIL(标题与正文不同源)' END            AS verdict
FROM posts;

-- L9.6 设备三列互相自洽：品牌 → 机型 → 系统
-- v1 的逐行生成器（scripts/generators/user_domain.py）是配对生成的，v2 改写成
-- 向量化时丢了这个配对，于是可能出现 device_brand=Apple、device_model=Redmi K70。
-- 这里查两件事：iOS 设备的品牌只能是 Apple；同一 device_model 不能横跨多个品牌。
SELECT 'ios 设备品牌非 Apple' AS rule,
       sum(CASE WHEN device_type = 'ios' AND device_brand <> 'Apple' THEN 1 ELSE 0 END) AS bad,
       CASE WHEN sum(CASE WHEN device_type = 'ios' AND device_brand <> 'Apple'
                          THEN 1 ELSE 0 END) = 0 THEN 'PASS' ELSE 'FAIL' END AS verdict
FROM user_devices
UNION ALL
SELECT 'ios 设备系统非 iOS',
       sum(CASE WHEN device_type = 'ios' AND os_version NOT LIKE 'iOS%' THEN 1 ELSE 0 END),
       CASE WHEN sum(CASE WHEN device_type = 'ios' AND os_version NOT LIKE 'iOS%'
                          THEN 1 ELSE 0 END) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM user_devices
UNION ALL
SELECT 'android 设备系统非 Android',
       sum(CASE WHEN device_type = 'android' AND os_version NOT LIKE 'Android%'
                THEN 1 ELSE 0 END),
       CASE WHEN sum(CASE WHEN device_type = 'android' AND os_version NOT LIKE 'Android%'
                          THEN 1 ELSE 0 END) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM user_devices
UNION ALL
SELECT '同一机型横跨多个品牌',
       count(*),
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM (SELECT device_model FROM user_devices
      GROUP BY device_model HAVING count(DISTINCT device_brand) > 1) x
ORDER BY 1;

-- L9.7 文本列基数：组合式词池不该退化成几十种取值
-- 单池的基数就是列的基数——旧 device_model 只有 40 种、旧 utm_campaign 只有 50 种，
-- GROUP BY 一下就露馅。这条不设硬门槛（各列合理基数差别很大），给出实测值供人判读；
-- 只对「基数 < 10 且不是枚举列」给告警。
SELECT 'users.username'                AS col, count(DISTINCT username)     AS uniq,
       count(*)                        AS n FROM users
UNION ALL SELECT 'user_devices.device_model', count(DISTINCT device_model), count(*) FROM user_devices
UNION ALL SELECT 'user_devices.push_token',   count(DISTINCT push_token),   count(*) FROM user_devices
UNION ALL SELECT 'sessions.utm_campaign',     count(DISTINCT utm_campaign), count(*) FROM sessions
UNION ALL SELECT 'posts.title',               count(DISTINCT title),        count(*) FROM posts
UNION ALL SELECT 'post_comments.content',     count(DISTINCT content),      count(*) FROM post_comments
UNION ALL SELECT 'user_messages.content',     count(DISTINCT content),      count(*) FROM user_messages
ORDER BY 2;

-- L9.8 词沙拉：Faker 中文 lorem ipsum
-- L9.1 的序号判据有个盲区，v1 的数据主缺陷正躺在里面：`一下参加方面.直接活动因为商品
-- 时候.` 这种值既没有共用前缀也没有数字尾，L9.1 完全放过它。它比 post_1 更糟——一眼
-- 看不出是假的，会被当成真实用户内容读进去。
--
-- 指纹是**标点**不是词汇：Faker 按英文句子模板拼中文词，句末落 ASCII 句点，全值没有
-- 一个「。」。判据是「中文字 + 紧跟 ASCII 句点，且整值无「。」」。
--
-- 两处写法差异（结论相同，实测在 38.7 万个真实值上零分歧）：
-- 生成侧用 `[一-鿿]\.`；这里用 `[^ -~][.]` —— 只依赖 ASCII 码点范围，不赌正则引擎
-- 的 Unicode 范围支持；点写成 `[.]` 而不是 `\.`，绕开 SQL 字符串的反斜杠转义。
--
-- 「紧跟」这一条不能省：只查「含中文且含点」会误伤 coupons.coupon_name 的
-- `满50享2.0折`——那个点夹在数字之间。
-- 不设占比门槛，命中一条即 FAIL：v1 的 posts.content 命中 100%，但
-- post_comments.content 只有 29%、user_messages.content 只有 20%（模板句与 Faker
-- 句混在一列），任何 ≥50% 的门槛都会把后两列漏掉。
WITH v AS (
    SELECT 'posts.title'                AS col, title   AS val FROM posts
    UNION ALL SELECT 'posts.content',            content        FROM posts
    UNION ALL SELECT 'post_comments.content',    content        FROM post_comments
    UNION ALL SELECT 'user_messages.content',    content        FROM user_messages
    UNION ALL SELECT 'push_notifications.title', title          FROM push_notifications
    UNION ALL SELECT 'push_notifications.content', content      FROM push_notifications
    UNION ALL SELECT 'orders.remark',            remark         FROM orders
    UNION ALL SELECT 'order_items.product_name', product_name   FROM order_items
    UNION ALL SELECT 'order_items.sku_name',     sku_name       FROM order_items
    UNION ALL SELECT 'user_profiles.occupation', occupation     FROM user_profiles
    UNION ALL SELECT 'user_profiles.city',       city           FROM user_profiles
),
nonblank AS (SELECT col, val FROM v WHERE val IS NOT NULL AND btrim(val) <> '')
SELECT col,
       count(*)                                                        AS n,
       sum(CASE WHEN regexp_instr(val, '[^ -~][.]') > 0
                 AND strpos(val, '。') = 0 THEN 1 ELSE 0 END)          AS salad,
       max(CASE WHEN regexp_instr(val, '[^ -~][.]') > 0
                 AND strpos(val, '。') = 0 THEN val ELSE NULL END)     AS example,
       CASE WHEN sum(CASE WHEN regexp_instr(val, '[^ -~][.]') > 0
                           AND strpos(val, '。') = 0 THEN 1 ELSE 0 END) = 0
            THEN 'PASS' ELSE 'FAIL(词沙拉)' END                         AS verdict
FROM nonblank
GROUP BY col
ORDER BY 3 DESC, col;
