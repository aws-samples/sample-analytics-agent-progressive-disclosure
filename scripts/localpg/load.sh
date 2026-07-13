#!/bin/bash
# 本地 Postgres rig —— 建表 + 灌 CSV + 建 mart + 重置序列 + 建 meta_snapshot。
# 是 scripts/docker-init.sh 的"对已运行集群"幂等版本(取代 Docker initdb hook)。
# 运行顺序契约(P0 版,35 表):
#   01-08 DDL → COPY(FK 依赖序)→ 09 mart(CTAS)→ 重置序列 → meta_snapshot → 统计
# 注:P1 会把"先灌数后建索引 + 分区"并进来;P0 先做到与现有 docker-init.sh 等价(parity)。
#
# 用法:scripts/localpg/up.sh && scripts/localpg/load.sh
set -euo pipefail

PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
PGPORT="${PGPORT:-5433}"
PGDATABASE="${PGDATABASE:-app_analytics}"
PGUSER="${PGUSER:-postgres}"
DDL_DIR="$PROJ/database"
CSV_DIR="$PROJ/data/csv"

psql() { "$PGBIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1 "$@"; }

# 0. 清空 schema(幂等:load 是"从零重建",反复本地跑不报 already exists)
echo "[load 0/5] 重置 public schema"
psql -q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 1. DDL 01-08(原始表)
echo "[load 1/5] 建表 (01-08)"
for f in "$DDL_DIR"/0[1-8]_*.sql; do
  [ -f "$f" ] && { echo "  $(basename "$f")"; psql -q -f "$f"; }
done

# 2. COPY CSV(FK 依赖序,与 docker-init.sh 一致)
echo "[load 2/5] 灌数 (COPY)"
tables=(
  users user_profiles user_segments user_segment_members
  categories products product_tags channels event_definitions
  user_devices ad_campaigns ad_creatives channel_daily_costs user_attributions
  sessions events page_views
  posts post_likes post_comments post_shares user_follows user_messages
  campaigns coupons user_coupons banners push_notifications
  ab_tests ab_test_variants ab_test_assignments
  orders order_items payments subscriptions
)
for t in "${tables[@]}"; do
  csv="$CSV_DIR/$t.csv"
  if [ -f "$csv" ]; then
    psql -q -c "\copy $t FROM '$csv' WITH CSV HEADER"
    echo "  $t"
  else
    echo "  (skip $t — no csv)"
  fi
done

# 3. mart + 派生层(CTAS,依赖已灌数据;10_derived.sql 由 scripts/manifest/render.py 生成)
echo "[load 3/5] 建 mart + 派生层 (09-10)"
for f in "$DDL_DIR"/09_*.sql "$DDL_DIR"/10_*.sql; do
  [ -f "$f" ] && { echo "  $(basename "$f")"; psql -q -f "$f"; }
done

# 4. 重置序列(SERIAL 列接着 max 走)
echo "[load 4/5] 重置序列"
psql -q <<'EOF'
SELECT setval('ab_test_assignments_id_seq', COALESCE((SELECT MAX(id) FROM ab_test_assignments),1));
SELECT setval('ab_test_variants_variant_id_seq', COALESCE((SELECT MAX(variant_id) FROM ab_test_variants),1));
SELECT setval('ab_tests_test_id_seq', COALESCE((SELECT MAX(test_id) FROM ab_tests),1));
SELECT setval('ad_campaigns_ad_campaign_id_seq', COALESCE((SELECT MAX(ad_campaign_id) FROM ad_campaigns),1));
SELECT setval('ad_creatives_creative_id_seq', COALESCE((SELECT MAX(creative_id) FROM ad_creatives),1));
SELECT setval('banners_banner_id_seq', COALESCE((SELECT MAX(banner_id) FROM banners),1));
SELECT setval('campaigns_campaign_id_seq', COALESCE((SELECT MAX(campaign_id) FROM campaigns),1));
SELECT setval('categories_category_id_seq', COALESCE((SELECT MAX(category_id) FROM categories),1));
SELECT setval('channel_daily_costs_id_seq', COALESCE((SELECT MAX(id) FROM channel_daily_costs),1));
SELECT setval('channels_channel_id_seq', COALESCE((SELECT MAX(channel_id) FROM channels),1));
SELECT setval('coupons_coupon_id_seq', COALESCE((SELECT MAX(coupon_id) FROM coupons),1));
SELECT setval('order_items_item_id_seq', COALESCE((SELECT MAX(item_id) FROM order_items),1));
SELECT setval('orders_order_id_seq', COALESCE((SELECT MAX(order_id) FROM orders),1));
SELECT setval('payments_payment_id_seq', COALESCE((SELECT MAX(payment_id) FROM payments),1));
SELECT setval('post_comments_comment_id_seq', COALESCE((SELECT MAX(comment_id) FROM post_comments),1));
SELECT setval('post_shares_share_id_seq', COALESCE((SELECT MAX(share_id) FROM post_shares),1));
SELECT setval('posts_post_id_seq', COALESCE((SELECT MAX(post_id) FROM posts),1));
SELECT setval('product_tags_id_seq', COALESCE((SELECT MAX(id) FROM product_tags),1));
SELECT setval('products_product_id_seq', COALESCE((SELECT MAX(product_id) FROM products),1));
SELECT setval('push_notifications_push_id_seq', COALESCE((SELECT MAX(push_id) FROM push_notifications),1));
SELECT setval('subscriptions_subscription_id_seq', COALESCE((SELECT MAX(subscription_id) FROM subscriptions),1));
SELECT setval('user_attributions_attribution_id_seq', COALESCE((SELECT MAX(attribution_id) FROM user_attributions),1));
SELECT setval('user_coupons_id_seq', COALESCE((SELECT MAX(id) FROM user_coupons),1));
SELECT setval('user_messages_message_id_seq', COALESCE((SELECT MAX(message_id) FROM user_messages),1));
SELECT setval('user_segments_segment_id_seq', COALESCE((SELECT MAX(segment_id) FROM user_segments),1));
EOF

# 5. meta_snapshot(数据"今天"锚点)
echo "[load 5/5] meta_snapshot"
psql -q -f "$PROJ/scripts/snapshot_date.sql"

# 统计
echo ""
echo "=== 行数统计 ==="
psql -c "SELECT
  (SELECT count(*) FROM users)  AS users,
  (SELECT count(*) FROM events) AS events,
  (SELECT count(*) FROM orders) AS orders,
  (SELECT count(*) FROM posts)  AS posts;"
psql -c "SELECT
  (SELECT count(*) FROM mart_daily_kpi)     AS mart_daily_kpi,
  (SELECT count(*) FROM mart_daily_revenue) AS mart_daily_revenue,
  (SELECT count(*) FROM mart_channel_daily) AS mart_channel_daily,
  (SELECT count(*) FROM mart_user_summary)  AS mart_user_summary;"
psql -c "SELECT as_of_date, data_start FROM meta_snapshot;"
