"""一次性 Aurora 灌数 Lambda（VPC 内,纯 psycopg,无需公网）。

复刻 scripts/docker-init.sh 的顺序:
  1) 跑 01-08 域 DDL 建原始表
  2) 按外键依赖顺序 COPY 35 个 CSV
  3) 跑 09_mart.sql（CTAS 治理层,依赖已灌数据）
  4) 重置 SERIAL 序列
种子文件(DDL + CSV)从 S3 读(S3 网关端点),DB 口令从 Secrets Manager 读(接口端点),
连 Aurora 走 VPC。触发一次即可。
"""
import io
import json
import os

import boto3
import psycopg
from psycopg import pq

S3 = boto3.client("s3")
SM = boto3.client("secretsmanager")

BUCKET = os.environ["SEED_BUCKET"]
PREFIX = os.environ.get("SEED_PREFIX", "seed/")

DDL_FILES = [
    "01_user_domain.sql", "02_behavior_domain.sql", "03_attribution_domain.sql",
    "04_social_domain.sql", "05_marketing_domain.sql", "06_experiment_domain.sql",
    "07_transaction_domain.sql", "08_product_domain.sql",
]

# 按外键依赖的 COPY 顺序（同 docker-init.sh）
TABLES = [
    "users", "user_profiles", "user_segments", "user_segment_members", "categories",
    "products", "product_tags", "channels", "event_definitions",
    "user_devices", "ad_campaigns", "ad_creatives", "channel_daily_costs",
    "user_attributions", "sessions", "events", "page_views", "posts", "post_likes",
    "post_comments", "post_shares", "user_follows", "user_messages", "campaigns",
    "coupons", "user_coupons", "banners", "push_notifications", "ab_tests",
    "ab_test_variants", "ab_test_assignments", "orders", "order_items", "payments",
    "subscriptions",
]

SETVAL = """
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
"""


def _pg():
    sec = json.loads(SM.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    return psycopg.connect(
        host=os.environ["PGHOST"], port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ["PGDATABASE"], user=sec["username"], password=sec["password"],
        connect_timeout=20, autocommit=True,
    )


def _get(key: str) -> bytes:
    return S3.get_object(Bucket=BUCKET, Key=PREFIX + key)["Body"].read()


def _run_script(conn, sql: str) -> None:
    """用 libpq 简单查询协议跑多语句脚本(psycopg execute 只允许单语句)。
    PQexec 遇错即停并返回错误结果,检查末个结果状态即可捕获失败。"""
    res = conn.pgconn.exec_(sql.encode("utf-8"))
    if res.status not in (pq.ExecStatus.COMMAND_OK, pq.ExecStatus.TUPLES_OK):
        raise RuntimeError(res.error_message.decode("utf-8", "replace"))


def handler(event, context):
    reset = bool((event or {}).get("reset"))
    conn = _pg()
    log = []
    try:
        if reset:
            _run_script(conn, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            log.append("dropped+recreated public schema")
        # 1) DDL 01-08
        for f in DDL_FILES:
            _run_script(conn, _get("ddl/" + f).decode("utf-8"))
        log.append(f"ddl: {len(DDL_FILES)} files")
        # 2) COPY CSV（FK 顺序）
        loaded = 0
        with conn.cursor() as cur:
            for t in TABLES:
                data = _get("csv/" + t + ".csv")
                with cur.copy(f"COPY {t} FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                    cp.write(data)
                loaded += 1
        log.append(f"copied: {loaded} tables")
        # 3) 治理层 mart
        _run_script(conn, _get("ddl/09_mart.sql").decode("utf-8"))
        log.append("mart built")
        # 4) 序列重置
        _run_script(conn, SETVAL)
        log.append("sequences reset")
        # 校验
        with conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM events),"
                        "(SELECT count(*) FROM orders),(SELECT count(*) FROM mart_daily_kpi)")
            users, events, orders, mart = cur.fetchone()
        return {"ok": True, "log": log,
                "counts": {"users": users, "events": events, "orders": orders, "mart_daily_kpi": mart}}
    finally:
        conn.close()
