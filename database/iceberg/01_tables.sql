/* Iceberg DDL —— S3 Tables 表桶里的 35 张基表。
 *
 * ⚠️ 本文件是**生成物**，不要手改。改了会被 --check 抓出来。
 *     生成： python3 scripts/lakehouse/gen_ddl.py -o database/iceberg/01_tables.sql
 *     校验： python3 scripts/lakehouse/gen_ddl.py --check
 *
 * 真源：database/01_user_domain.sql, database/02_behavior_domain.sql, database/03_attribution_domain.sql, database/04_social_domain.sql, database/05_marketing_domain.sql, database/06_experiment_domain.sql, database/07_transaction_domain.sql, database/08_product_domain.sql
 * 类型映射、主键为何消失、注释为何不用 --，见 scripts/lakehouse/gen_ddl.py。
 *
 * 执行方式（catalog 名带斜杠，不能写进 SQL，必须走 QueryExecutionContext）：
 *     python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql \
 *         --tolerate 'already exists'
 */

/* ========== 来自 database/01_user_domain.sql ========== */
CREATE TABLE IF NOT EXISTS users (
    user_id             bigint,
    username            string    COMMENT 'pg: VARCHAR(50)',
    email               string    COMMENT 'pg: VARCHAR(100)',
    phone               string    COMMENT 'pg: VARCHAR(20)',
    registered_at       timestamp COMMENT 'NOT NULL（Iceberg 侧不强制）',
    registration_source string    COMMENT '\'referral\', \'organic\', \'huawei_store\', \'web\', \'ad_campaign\', \'wechat_mini\', \'app_store\', \'google_play\'（小程序渠道叫 wechat_mini）; pg: VARCHAR(50)',
    status              string    COMMENT '\'active\', \'inactive\', \'deleted\', \'suspended\'（封禁态叫 suspended）; pg: VARCHAR(20)',
    user_level          int       COMMENT '1-5 用户等级',
    is_vip              boolean,
    last_active_at      timestamp,
    created_at          timestamp,
    updated_at          timestamp
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id      bigint,
    age          int,
    gender       string        COMMENT '\'female\', \'male\'（业务上还有 unknown，本批数据没有）; pg: VARCHAR(10)',
    birth_date   date,
    city         string        COMMENT 'pg: VARCHAR(50)',
    province     string        COMMENT 'pg: VARCHAR(50)',
    country      string        COMMENT 'pg: VARCHAR(50)',
    interests    array<string> COMMENT 'array of interest tags; pg: TEXT[]',
    occupation   string        COMMENT 'pg: VARCHAR(50)',
    income_level string        COMMENT '\'medium\', \'low\', \'high\', \'very_high\'; pg: VARCHAR(20)',
    created_at   timestamp,
    updated_at   timestamp
);

CREATE TABLE IF NOT EXISTS user_devices (
    device_id     string    COMMENT 'pg: VARCHAR(100)',
    user_id       bigint,
    device_type   string    COMMENT '\'ios\', \'android\', \'web\', \'mini_program\'; pg: VARCHAR(20)',
    os_version    string    COMMENT 'pg: VARCHAR(20)',
    device_model  string    COMMENT 'pg: VARCHAR(50)',
    device_brand  string    COMMENT 'pg: VARCHAR(50)',
    app_version   string    COMMENT 'pg: VARCHAR(20)',
    push_token    string    COMMENT 'pg: VARCHAR(200)',
    is_primary    boolean,
    first_seen_at timestamp,
    last_seen_at  timestamp,
    created_at    timestamp
);

CREATE TABLE IF NOT EXISTS user_segments (
    segment_id   int       COMMENT 'pg: SERIAL',
    segment_name string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    segment_type string    COMMENT '\'lifecycle\', \'membership\', \'behavior\', \'demographic\', \'value\', \'device\', \'geographic\', \'engagement\', \'acquisition\'; pg: VARCHAR(50)',
    description  string    COMMENT 'pg: TEXT',
    rules_json   string    COMMENT '分群规则定义; pg: JSONB',
    owner        string    COMMENT 'pg: VARCHAR(50)',
    status       string    COMMENT 'pg: VARCHAR(20)',
    created_at   timestamp,
    updated_at   timestamp
);

CREATE TABLE IF NOT EXISTS user_segment_members (
    user_id    bigint,
    segment_id int,
    entered_at timestamp,
    exited_at  timestamp
);


/* ========== 来自 database/02_behavior_domain.sql ========== */
CREATE TABLE IF NOT EXISTS event_definitions (
    event_name        string    COMMENT 'pg: VARCHAR(100)',
    event_category    string    COMMENT '\'engagement\', \'commerce\', \'social\', \'system\'; pg: VARCHAR(50)',
    description       string    COMMENT 'pg: TEXT',
    properties_schema string    COMMENT '事件属性的 schema 定义; pg: JSONB',
    owner             string    COMMENT 'pg: VARCHAR(50)',
    is_core_event     boolean,
    created_at        timestamp,
    updated_at        timestamp
);

CREATE TABLE IF NOT EXISTS events (
    event_id   bigint,
    user_id    bigint,
    device_id  string    COMMENT 'pg: VARCHAR(100)',
    session_id bigint,
    event_name string    COMMENT 'pg: VARCHAR(100)',
    event_time timestamp COMMENT 'NOT NULL（Iceberg 侧不强制）',
    properties string    COMMENT '事件自定义属性; pg: JSONB',
    page_name  string    COMMENT 'pg: VARCHAR(100)',
    referrer   string    COMMENT 'pg: VARCHAR(200)',
    ip_address string    COMMENT 'pg: VARCHAR(50)',
    created_at timestamp
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       bigint,
    user_id          bigint,
    device_id        string    COMMENT 'pg: VARCHAR(100)',
    start_time       timestamp COMMENT 'NOT NULL（Iceberg 侧不强制）',
    end_time         timestamp,
    duration_seconds int,
    event_count      int,
    page_view_count  int,
    is_bounce        boolean   COMMENT '只有1个页面浏览',
    entry_page       string    COMMENT 'pg: VARCHAR(100)',
    exit_page        string    COMMENT 'pg: VARCHAR(100)',
    traffic_source   string    COMMENT 'pg: VARCHAR(50)',
    utm_source       string    COMMENT 'pg: VARCHAR(50)',
    utm_medium       string    COMMENT 'pg: VARCHAR(50)',
    utm_campaign     string    COMMENT 'pg: VARCHAR(100)',
    created_at       timestamp
);

CREATE TABLE IF NOT EXISTS page_views (
    page_view_id     bigint,
    user_id          bigint,
    session_id       bigint,
    page_name        string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    page_url         string    COMMENT 'pg: VARCHAR(500)',
    referrer         string    COMMENT 'pg: VARCHAR(500)',
    duration_seconds int,
    scroll_depth_pct int       COMMENT '0-100',
    view_time        timestamp COMMENT 'NOT NULL（Iceberg 侧不强制）',
    created_at       timestamp
);


/* ========== 来自 database/03_attribution_domain.sql ========== */
CREATE TABLE IF NOT EXISTS channels (
    channel_id   int       COMMENT 'pg: SERIAL',
    channel_name string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    channel_type string    COMMENT '\'organic\', \'paid\', \'kol\', \'referral\', \'direct\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    platform     string    COMMENT '\'douyin\', \'weixin\', \'xiaohongshu\', \'baidu\', etc.; pg: VARCHAR(50)',
    description  string    COMMENT 'pg: TEXT',
    is_active    boolean,
    created_at   timestamp
);

CREATE TABLE IF NOT EXISTS ad_campaigns (
    ad_campaign_id  int           COMMENT 'pg: SERIAL',
    channel_id      int,
    campaign_name   string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(200)',
    campaign_type   string        COMMENT '\'awareness\', \'acquisition\', \'retargeting\'; pg: VARCHAR(50)',
    objective       string        COMMENT '\'installs\', \'purchases\', \'engagement\'; pg: VARCHAR(50)',
    budget_total    decimal(12,2),
    budget_daily    decimal(12,2),
    start_date      date,
    end_date        date,
    target_audience string        COMMENT '投放人群定向; pg: JSONB',
    status          string        COMMENT '\'active\', \'paused\', \'ended\'（业务上还有 draft，本批数据没有）; pg: VARCHAR(20)',
    created_at      timestamp,
    updated_at      timestamp
);

CREATE TABLE IF NOT EXISTS ad_creatives (
    creative_id     int       COMMENT 'pg: SERIAL',
    ad_campaign_id  int,
    creative_name   string    COMMENT 'pg: VARCHAR(200)',
    creative_type   string    COMMENT '\'image\', \'video\', \'carousel\'（业务上还有 text，本批数据没有）; pg: VARCHAR(50)',
    creative_format string    COMMENT '\'1080x1920\', \'750x1334\', etc.; pg: VARCHAR(50)',
    content_url     string    COMMENT 'pg: VARCHAR(500)',
    headline        string    COMMENT 'pg: VARCHAR(200)',
    description     string    COMMENT 'pg: TEXT',
    call_to_action  string    COMMENT 'pg: VARCHAR(50)',
    status          string    COMMENT 'pg: VARCHAR(20)',
    created_at      timestamp
);

CREATE TABLE IF NOT EXISTS user_attributions (
    attribution_id   bigint    COMMENT 'pg: BIGSERIAL',
    user_id          bigint,
    channel_id       int,
    ad_campaign_id   int,
    creative_id      int,
    attribution_type string    COMMENT '\'first_touch\', \'last_touch\'（linear 等多触点模型业务上成立，本批数据只做了这两种）; pg: VARCHAR(50)',
    click_time       timestamp,
    install_time     timestamp,
    attributed_at    timestamp,
    days_to_install  int,
    tracking_params  string    COMMENT 'utm_source, utm_medium, etc.; pg: JSONB'
);

CREATE TABLE IF NOT EXISTS channel_daily_costs (
    id             bigint        COMMENT 'pg: BIGSERIAL',
    channel_id     int,
    ad_campaign_id int,
    creative_id    int,
    date           date          COMMENT 'NOT NULL（Iceberg 侧不强制）',
    impressions    bigint,
    clicks         bigint,
    installs       int,
    cost           decimal(12,2),
    currency       string        COMMENT 'pg: VARCHAR(10)',
    created_at     timestamp
);


/* ========== 来自 database/04_social_domain.sql ========== */
CREATE TABLE IF NOT EXISTS user_follows (
    follower_id  bigint,
    following_id bigint,
    created_at   timestamp
);

CREATE TABLE IF NOT EXISTS posts (
    post_id       bigint        COMMENT 'pg: BIGSERIAL',
    user_id       bigint,
    content_type  string        COMMENT '\'article\', \'short_video\', \'image\', \'review\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    title         string        COMMENT 'pg: VARCHAR(200)',
    content       string        COMMENT 'pg: TEXT',
    media_urls    array<string> COMMENT '图片/视频链接; pg: TEXT[]',
    tags          array<string> COMMENT 'pg: TEXT[]',
    location      string        COMMENT 'pg: VARCHAR(100)',
    product_ids   array<bigint> COMMENT '关联的商品; pg: BIGINT[]',
    view_count    int,
    like_count    int,
    comment_count int,
    share_count   int,
    status        string        COMMENT '\'published\', \'under_review\', \'draft\', \'deleted\'（审核态叫 under_review；没有 hidden）; pg: VARCHAR(20)',
    is_featured   boolean,
    published_at  timestamp,
    created_at    timestamp,
    updated_at    timestamp
);

CREATE TABLE IF NOT EXISTS post_likes (
    user_id    bigint,
    post_id    bigint,
    created_at timestamp
);

CREATE TABLE IF NOT EXISTS post_comments (
    comment_id        bigint    COMMENT 'pg: BIGSERIAL',
    post_id           bigint,
    user_id           bigint,
    parent_comment_id bigint    COMMENT '支持回复',
    content           string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: TEXT',
    like_count        int,
    status            string    COMMENT '\'visible\', \'hidden\', \'deleted\'; pg: VARCHAR(20)',
    created_at        timestamp
);

CREATE TABLE IF NOT EXISTS post_shares (
    share_id      bigint    COMMENT 'pg: BIGSERIAL',
    user_id       bigint,
    post_id       bigint,
    share_channel string    COMMENT '\'wechat_friend\', \'wechat_moments\', \'weibo\', \'copy_link\', \'qq\', \'other\'（朋友圈是 wechat_moments，带 s）; pg: VARCHAR(50)',
    created_at    timestamp
);

CREATE TABLE IF NOT EXISTS user_messages (
    message_id         bigint    COMMENT 'pg: BIGSERIAL',
    sender_id          bigint,
    receiver_id        bigint,
    content            string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: TEXT',
    message_type       string    COMMENT '\'text\', \'image\', \'link\'（没有 product）; pg: VARCHAR(20)',
    related_post_id    bigint,
    related_product_id bigint,
    is_read            boolean,
    sent_at            timestamp,
    read_at            timestamp
);


/* ========== 来自 database/05_marketing_domain.sql ========== */
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id        int           COMMENT 'pg: SERIAL',
    campaign_name      string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(200)',
    campaign_type      string        COMMENT '\'promotion\', \'festival\', \'new_user\', \'recall\'; pg: VARCHAR(50)',
    description        string        COMMENT 'pg: TEXT',
    start_date         timestamp,
    end_date           timestamp,
    target_segment_ids array<int>    COMMENT '目标用户分群; pg: INT[]',
    budget             decimal(12,2),
    status             string        COMMENT '\'draft\', \'scheduled\', \'active\', \'paused\', \'completed\', \'cancelled\'（结束态叫 completed，不叫 ended）; pg: VARCHAR(20)',
    owner              string        COMMENT 'pg: VARCHAR(50)',
    created_at         timestamp,
    updated_at         timestamp
);

CREATE TABLE IF NOT EXISTS push_notifications (
    push_id        bigint    COMMENT 'pg: BIGSERIAL',
    user_id        bigint,
    campaign_id    int,
    push_type      string    COMMENT '按业务场景分：\'reminder\', \'social\', \'promotion\', \'order\', \'system\'（不是按 marketing/transactional 这种投递属性分）; pg: VARCHAR(50)',
    title          string    COMMENT 'pg: VARCHAR(200)',
    content        string    COMMENT 'pg: TEXT',
    deep_link      string    COMMENT 'pg: VARCHAR(500)',
    scheduled_at   timestamp,
    sent_at        timestamp,
    delivered_at   timestamp,
    opened_at      timestamp,
    is_delivered   boolean,
    is_opened      boolean,
    failure_reason string    COMMENT 'pg: VARCHAR(200)',
    created_at     timestamp
);

CREATE TABLE IF NOT EXISTS coupons (
    coupon_id           int           COMMENT 'pg: SERIAL',
    coupon_code         string        COMMENT 'pg: VARCHAR(50)',
    coupon_name         string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(200)',
    coupon_type         string        COMMENT '\'fixed_amount\', \'percentage\', \'free_shipping\'; pg: VARCHAR(50)',
    discount_value      decimal(10,2) COMMENT '金额或百分比',
    min_purchase        decimal(10,2) COMMENT '最低消费门槛',
    max_discount        decimal(10,2) COMMENT '最大优惠金额（百分比券用）',
    valid_days          int           COMMENT '领取后有效天数',
    start_date          timestamp,
    end_date            timestamp,
    total_quota         int           COMMENT '总发放量',
    per_user_limit      int           COMMENT '每人限领',
    applicable_products string        COMMENT '适用商品/分类; pg: JSONB',
    status              string        COMMENT 'pg: VARCHAR(20)',
    created_at          timestamp
);

CREATE TABLE IF NOT EXISTS user_coupons (
    id          bigint    COMMENT 'pg: BIGSERIAL',
    user_id     bigint,
    coupon_id   int,
    coupon_code string    COMMENT 'pg: VARCHAR(50)',
    received_at timestamp,
    expire_at   timestamp,
    used_at     timestamp,
    order_id    bigint    COMMENT '使用时关联的订单',
    status      string    COMMENT '\'unused\', \'used\', \'expired\'; pg: VARCHAR(20)',
    source      string    COMMENT '\'claim\', \'gift\', \'reward\', \'system\'（旧注释的 campaign/share/purchase/new_user 一个都不存在）; pg: VARCHAR(50)'
);

CREATE TABLE IF NOT EXISTS banners (
    banner_id        int       COMMENT 'pg: SERIAL',
    position         string    COMMENT '\'home_top\', \'home_middle\', \'category_top\', \'search_top\', \'detail_bottom\', \'cart_bottom\', \'splash\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    banner_name      string    COMMENT 'pg: VARCHAR(200)',
    image_url        string    COMMENT 'pg: VARCHAR(500)',
    target_url       string    COMMENT 'pg: VARCHAR(500)',
    target_type      string    COMMENT '\'product\', \'category\', \'campaign\', \'external\', \'content\'; pg: VARCHAR(50)',
    target_id        string    COMMENT 'pg: VARCHAR(100)',
    sort_order       int,
    start_date       timestamp,
    end_date         timestamp,
    is_active        boolean,
    click_count      int,
    impression_count int,
    created_at       timestamp,
    updated_at       timestamp
);


/* ========== 来自 database/06_experiment_domain.sql ========== */
CREATE TABLE IF NOT EXISTS ab_tests (
    test_id            int           COMMENT 'pg: SERIAL',
    test_name          string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(200)',
    test_key           string        COMMENT '代码中引用的 key; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    hypothesis         string        COMMENT 'pg: TEXT',
    description        string        COMMENT 'pg: TEXT',
    primary_metric     string        COMMENT '\'conversion_rate\', \'retention_d7\', \'revenue_per_user\'; pg: VARCHAR(100)',
    secondary_metrics  array<string> COMMENT 'pg: TEXT[]',
    target_segment_ids array<int>    COMMENT '目标用户群; pg: INT[]',
    traffic_percentage int           COMMENT '整体流量占比',
    min_sample_size    int,
    start_date         timestamp,
    end_date           timestamp,
    status             string        COMMENT '\'draft\', \'running\', \'completed\'（结束态叫 completed，不叫 concluded；业务上还有 paused，本批数据没有）; pg: VARCHAR(20)',
    conclusion         string        COMMENT 'pg: TEXT',
    winner_variant_id  int,
    owner              string        COMMENT 'pg: VARCHAR(50)',
    created_at         timestamp,
    updated_at         timestamp
);

CREATE TABLE IF NOT EXISTS ab_test_variants (
    variant_id         int       COMMENT 'pg: SERIAL',
    test_id            int,
    variant_name       string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    variant_key        string    COMMENT '\'control\', \'treatment_a\', \'treatment_b\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    description        string    COMMENT 'pg: TEXT',
    traffic_percentage int       COMMENT '该分支的流量占比; NOT NULL（Iceberg 侧不强制）',
    config_json        string    COMMENT '分支的具体配置; pg: JSONB',
    is_control         boolean,
    created_at         timestamp
);

CREATE TABLE IF NOT EXISTS ab_test_assignments (
    id                bigint    COMMENT 'pg: BIGSERIAL',
    user_id           bigint,
    test_id           int,
    variant_id        int,
    assigned_at       timestamp,
    first_exposure_at timestamp COMMENT '首次曝光时间'
);


/* ========== 来自 database/07_transaction_domain.sql ========== */
CREATE TABLE IF NOT EXISTS orders (
    order_id         bigint        COMMENT 'pg: BIGSERIAL',
    order_no         string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    user_id          bigint,
    status           string        COMMENT '\'pending\', \'paid\', \'shipped\', \'delivered\', \'cancelled\', \'refunded\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(30)',
    total_amount     decimal(12,2) COMMENT 'NOT NULL（Iceberg 侧不强制）',
    discount_amount  decimal(12,2),
    shipping_fee     decimal(10,2),
    actual_amount    decimal(12,2) COMMENT '实付金额; NOT NULL（Iceberg 侧不强制）',
    item_count       int,
    coupon_id        int,
    shipping_address string        COMMENT 'pg: JSONB',
    remark           string        COMMENT 'pg: TEXT',
    placed_at        timestamp     COMMENT '下单时间; NOT NULL（Iceberg 侧不强制）',
    paid_at          timestamp,
    shipped_at       timestamp,
    delivered_at     timestamp,
    cancelled_at     timestamp,
    cancel_reason    string        COMMENT 'pg: VARCHAR(200)',
    refunded_at      timestamp,
    refund_reason    string        COMMENT 'pg: VARCHAR(200)',
    created_at       timestamp,
    updated_at       timestamp
);

CREATE TABLE IF NOT EXISTS order_items (
    item_id         bigint        COMMENT 'pg: BIGSERIAL',
    order_id        bigint,
    product_id      bigint        COMMENT 'REFERENCES products(product_id)',
    product_name    string        COMMENT 'pg: VARCHAR(200)',
    sku_id          bigint,
    sku_name        string        COMMENT 'pg: VARCHAR(200)',
    quantity        int           COMMENT 'NOT NULL（Iceberg 侧不强制）',
    unit_price      decimal(10,2) COMMENT 'NOT NULL（Iceberg 侧不强制）',
    discount_amount decimal(10,2),
    actual_amount   decimal(10,2) COMMENT 'NOT NULL（Iceberg 侧不强制）',
    created_at      timestamp
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id      bigint        COMMENT 'pg: BIGSERIAL',
    payment_no      string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    order_id        bigint,
    user_id         bigint,
    amount          decimal(12,2) COMMENT 'NOT NULL（Iceberg 侧不强制）',
    payment_method  string        COMMENT '\'wechat\', \'alipay\', \'credit_card\', \'balance\'; pg: VARCHAR(50)',
    payment_channel string        COMMENT '\'app\', \'h5\', \'mini_program\'; pg: VARCHAR(50)',
    status          string        COMMENT '\'success\', \'refunded\'; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(20)',
    transaction_id  string        COMMENT '第三方支付流水号; pg: VARCHAR(100)',
    paid_at         timestamp,
    failure_reason  string        COMMENT 'pg: VARCHAR(200)',
    refund_amount   decimal(12,2),
    refunded_at     timestamp,
    created_at      timestamp
);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id bigint        COMMENT 'pg: BIGSERIAL',
    user_id         bigint,
    plan_name       string        COMMENT '中文取值：\'月度会员\', \'季度会员\', \'年度会员\'（不是 monthly/quarterly/yearly）; NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    plan_price      decimal(10,2),
    start_date      date          COMMENT 'NOT NULL（Iceberg 侧不强制）',
    end_date        date          COMMENT 'NOT NULL（Iceberg 侧不强制）',
    auto_renew      boolean,
    status          string        COMMENT '\'active\', \'expired\'（业务上还有 cancelled，本批数据没有，所以 cancelled_at / cancel_reason 整列为空）; pg: VARCHAR(20)',
    payment_id      bigint,
    cancelled_at    timestamp,
    cancel_reason   string        COMMENT 'pg: VARCHAR(200)',
    created_at      timestamp,
    updated_at      timestamp
);


/* ========== 来自 database/08_product_domain.sql ========== */
CREATE TABLE IF NOT EXISTS categories (
    category_id   int       COMMENT 'pg: SERIAL',
    parent_id     int,
    category_name string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(100)',
    level         int       COMMENT '1, 2, 3...; NOT NULL（Iceberg 侧不强制）',
    sort_order    int,
    icon_url      string    COMMENT 'pg: VARCHAR(500)',
    is_active     boolean,
    created_at    timestamp,
    updated_at    timestamp
);

CREATE TABLE IF NOT EXISTS products (
    product_id     bigint        COMMENT 'pg: BIGSERIAL',
    product_name   string        COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(200)',
    category_id    int,
    brand          string        COMMENT 'pg: VARCHAR(100)',
    description    string        COMMENT 'pg: TEXT',
    price          decimal(10,2) COMMENT 'NOT NULL（Iceberg 侧不强制）',
    original_price decimal(10,2),
    cost           decimal(10,2) COMMENT '成本价（用于计算毛利）',
    stock          int,
    sold_count     int,
    view_count     int,
    favorite_count int,
    rating_avg     decimal(2,1),
    rating_count   int,
    main_image_url string        COMMENT 'pg: VARCHAR(500)',
    image_urls     array<string> COMMENT 'pg: TEXT[]',
    status         string        COMMENT '\'on_sale\', \'off_sale\', \'pre_sale\', \'sold_out\'; pg: VARCHAR(20)',
    is_featured    boolean,
    created_at     timestamp,
    updated_at     timestamp
);

CREATE TABLE IF NOT EXISTS product_tags (
    id         int       COMMENT 'pg: SERIAL',
    product_id bigint,
    tag_name   string    COMMENT 'NOT NULL（Iceberg 侧不强制）; pg: VARCHAR(50)',
    tag_type   string    COMMENT '\'promotion\', \'feature\', \'season\', \'audience\', \'style\'（旧注释的 category / scene 不存在）; pg: VARCHAR(30)',
    created_at timestamp
);
