#!/bin/bash
set -e

echo "=========================================="
echo "APP Analytics 数据库初始化"
echo "=========================================="

# 1. 执行 DDL 文件创建表结构（仅原始表 01-08；09_mart.sql 是治理层，依赖数据，放到灌数之后）
echo "[1/5] 创建表结构..."
for sql_file in /docker-entrypoint-initdb.d/ddl/0[1-8]_*.sql; do
    if [ -f "$sql_file" ]; then
        echo "  执行: $(basename "$sql_file")"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$sql_file"
    fi
done

# 2. 导入 CSV 数据
echo "[2/5] 导入数据..."

# 定义导入顺序（考虑外键依赖）
tables=(
    # 无依赖的基础表
    "users"
    "user_profiles"
    "user_segments"
    "user_segment_members"
    "categories"
    "products"
    "product_tags"
    "channels"
    "event_definitions"

    # 有依赖的表
    "user_devices"
    "ad_campaigns"
    "ad_creatives"
    "channel_daily_costs"
    "user_attributions"
    "sessions"
    "events"
    "page_views"
    "posts"
    "post_likes"
    "post_comments"
    "post_shares"
    "user_follows"
    "user_messages"
    "campaigns"
    "coupons"
    "user_coupons"
    "banners"
    "push_notifications"
    "ab_tests"
    "ab_test_variants"
    "ab_test_assignments"
    "orders"
    "order_items"
    "payments"
    "subscriptions"
)

for table in "${tables[@]}"; do
    csv_file="/data/csv/${table}.csv"
    if [ -f "$csv_file" ]; then
        count=$(tail -n +2 "$csv_file" | wc -l | tr -d ' ')
        echo "  导入: $table ($count 行)"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
            -c "\copy $table FROM '$csv_file' WITH CSV HEADER"
    else
        echo "  跳过: $table (文件不存在)"
    fi
done

# 2b. 构建治理层 / 数据集市（原始表已灌数，此时 CTAS 才有内容）
echo "[3/5] 构建治理层 (mart)..."
for sql_file in /docker-entrypoint-initdb.d/ddl/09_*.sql; do
    if [ -f "$sql_file" ]; then
        echo "  执行: $(basename "$sql_file")"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$sql_file"
    fi
done

# 3. 重置序列值（确保 SERIAL 列正常工作）
echo "[4/5] 重置序列值..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOF'
-- 重置所有序列到当前最大值
SELECT setval('ab_test_assignments_id_seq', COALESCE((SELECT MAX(id) FROM ab_test_assignments), 1));
SELECT setval('ab_test_variants_variant_id_seq', COALESCE((SELECT MAX(variant_id) FROM ab_test_variants), 1));
SELECT setval('ab_tests_test_id_seq', COALESCE((SELECT MAX(test_id) FROM ab_tests), 1));
SELECT setval('ad_campaigns_ad_campaign_id_seq', COALESCE((SELECT MAX(ad_campaign_id) FROM ad_campaigns), 1));
SELECT setval('ad_creatives_creative_id_seq', COALESCE((SELECT MAX(creative_id) FROM ad_creatives), 1));
SELECT setval('banners_banner_id_seq', COALESCE((SELECT MAX(banner_id) FROM banners), 1));
SELECT setval('campaigns_campaign_id_seq', COALESCE((SELECT MAX(campaign_id) FROM campaigns), 1));
SELECT setval('categories_category_id_seq', COALESCE((SELECT MAX(category_id) FROM categories), 1));
SELECT setval('channel_daily_costs_id_seq', COALESCE((SELECT MAX(id) FROM channel_daily_costs), 1));
SELECT setval('channels_channel_id_seq', COALESCE((SELECT MAX(channel_id) FROM channels), 1));
SELECT setval('coupons_coupon_id_seq', COALESCE((SELECT MAX(coupon_id) FROM coupons), 1));
SELECT setval('order_items_item_id_seq', COALESCE((SELECT MAX(item_id) FROM order_items), 1));
SELECT setval('orders_order_id_seq', COALESCE((SELECT MAX(order_id) FROM orders), 1));
SELECT setval('payments_payment_id_seq', COALESCE((SELECT MAX(payment_id) FROM payments), 1));
SELECT setval('post_comments_comment_id_seq', COALESCE((SELECT MAX(comment_id) FROM post_comments), 1));
SELECT setval('post_shares_share_id_seq', COALESCE((SELECT MAX(share_id) FROM post_shares), 1));
SELECT setval('posts_post_id_seq', COALESCE((SELECT MAX(post_id) FROM posts), 1));
SELECT setval('product_tags_id_seq', COALESCE((SELECT MAX(id) FROM product_tags), 1));
SELECT setval('products_product_id_seq', COALESCE((SELECT MAX(product_id) FROM products), 1));
SELECT setval('push_notifications_push_id_seq', COALESCE((SELECT MAX(push_id) FROM push_notifications), 1));
SELECT setval('subscriptions_subscription_id_seq', COALESCE((SELECT MAX(subscription_id) FROM subscriptions), 1));
SELECT setval('user_attributions_attribution_id_seq', COALESCE((SELECT MAX(attribution_id) FROM user_attributions), 1));
SELECT setval('user_coupons_id_seq', COALESCE((SELECT MAX(id) FROM user_coupons), 1));
SELECT setval('user_messages_message_id_seq', COALESCE((SELECT MAX(message_id) FROM user_messages), 1));
SELECT setval('user_segments_segment_id_seq', COALESCE((SELECT MAX(segment_id) FROM user_segments), 1));
EOF

# 4. 输出统计
echo ""
echo "=========================================="
echo "初始化完成！数据统计："
echo "=========================================="
echo "[5/5] 统计..."
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
SELECT
    (SELECT COUNT(*) FROM users) AS users,
    (SELECT COUNT(*) FROM events) AS events,
    (SELECT COUNT(*) FROM orders) AS orders,
    (SELECT COUNT(*) FROM posts) AS posts;
"
echo "治理层 (mart) 行数："
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
SELECT
    (SELECT COUNT(*) FROM mart_daily_kpi)     AS mart_daily_kpi,
    (SELECT COUNT(*) FROM mart_daily_revenue) AS mart_daily_revenue,
    (SELECT COUNT(*) FROM mart_channel_daily) AS mart_channel_daily,
    (SELECT COUNT(*) FROM mart_user_summary)  AS mart_user_summary;
"
