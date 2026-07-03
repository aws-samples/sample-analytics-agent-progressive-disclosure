#!/usr/bin/env python3
"""导出数据为 CSV 格式，用于 PostgreSQL COPY 导入"""

import os
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from faker import Faker
from generators import *

fake = Faker('zh_CN')
Faker.seed(42)

# 小规模测试配置
CONFIG = {
    "num_users": 500,
    "num_products": 200,
    "num_categories": 30,
    "num_channels": 15,
    "num_campaigns": 50,
    "num_posts": 1000,
    "num_orders": 2000,
    "num_events": 20000,
    "num_sessions": 5000,
    "date_range_days": 90,
}

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "csv"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def to_pg_array(lst):
    """将 Python list 转换为 PostgreSQL 数组格式 {item1,item2,...}"""
    if not lst:
        return '{}'
    # 处理列表中的每个元素
    escaped_items = []
    for item in lst:
        if item is None:
            escaped_items.append('NULL')
        elif isinstance(item, str):
            # 转义双引号和反斜杠
            escaped = item.replace('\\', '\\\\').replace('"', '\\"')
            # 如果包含逗号、空格、大括号或引号，需要用双引号包裹
            if any(c in escaped for c in [',', ' ', '{', '}', '"', '\\']):
                escaped_items.append(f'"{escaped}"')
            else:
                escaped_items.append(escaped)
        else:
            escaped_items.append(str(item))
    return '{' + ','.join(escaped_items) + '}'


def flatten_dict(d, parent_key='', sep='_'):
    """将嵌套字典展平，数组转为 PostgreSQL 格式，字典转为 JSON"""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            # 字典类型保持 JSON 格式
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, list):
            # 列表类型转为 PostgreSQL 数组格式
            v = to_pg_array(v)
        elif v is None:
            v = ''
        elif isinstance(v, bool):
            # 布尔值转为小写字符串
            v = 'true' if v else 'false'
        items.append((new_key, v))
    return dict(items)


def save_csv(data, filename, fieldnames=None):
    """保存数据到 CSV"""
    if not data:
        print(f"  跳过 {filename} (无数据)")
        return
    
    filepath = OUTPUT_DIR / filename
    
    # 展平嵌套字典
    flat_data = [flatten_dict(row) for row in data]
    
    if fieldnames is None:
        fieldnames = list(flat_data[0].keys())
    
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(flat_data)
    
    print(f"  保存: {filename} ({len(data)} 条)")


def main():
    print("=" * 50)
    print("生成 CSV 数据文件")
    print("=" * 50)

    # 1. 商品域
    print("\n[1/8] 商品域...")
    categories, products, product_tags = generate_product_domain(CONFIG, fake)
    save_csv(categories, "categories.csv")
    save_csv(products, "products.csv")
    save_csv(product_tags, "product_tags.csv")

    # 2. 用户域
    print("\n[2/8] 用户域...")
    users, profiles, devices, segments, segment_members = generate_user_domain(CONFIG, fake)
    save_csv(users, "users.csv")
    save_csv(profiles, "user_profiles.csv")
    save_csv(devices, "user_devices.csv")
    save_csv(segments, "user_segments.csv")
    save_csv(segment_members, "user_segment_members.csv")

    # 3. 渠道域
    print("\n[3/8] 渠道域...")
    channels, ad_campaigns, ad_creatives, attributions, channel_costs = generate_channel_domain(CONFIG, fake, users)
    save_csv(channels, "channels.csv")
    save_csv(ad_campaigns, "ad_campaigns.csv")
    save_csv(ad_creatives, "ad_creatives.csv")
    save_csv(attributions, "user_attributions.csv")
    save_csv(channel_costs, "channel_daily_costs.csv")

    # 4. 行为域
    print("\n[4/8] 行为域...")
    event_defs, events, sessions, page_views = generate_behavior_domain(CONFIG, fake, users, products)
    save_csv(event_defs, "event_definitions.csv")
    save_csv(sessions, "sessions.csv")
    save_csv(page_views, "page_views.csv")
    save_csv(events, "events.csv")

    # 5. 社交域
    print("\n[5/8] 社交域...")
    follows, posts, likes, comments, shares, messages = generate_social_domain(CONFIG, fake, users, products)
    save_csv(follows, "user_follows.csv")
    save_csv(posts, "posts.csv")
    save_csv(likes, "post_likes.csv")
    save_csv(comments, "post_comments.csv")
    save_csv(shares, "post_shares.csv")
    save_csv(messages, "user_messages.csv")

    # 6. 运营域
    print("\n[6/8] 运营域...")
    campaigns, push_notifications, coupons, user_coupons, banners = generate_marketing_domain(CONFIG, fake, users, segments)
    save_csv(campaigns, "campaigns.csv")
    save_csv(coupons, "coupons.csv")
    save_csv(user_coupons, "user_coupons.csv")
    save_csv(banners, "banners.csv")
    # push_notifications 太大，限制数量
    save_csv(push_notifications[:10000], "push_notifications.csv")

    # 7. 实验域
    print("\n[7/8] 实验域...")
    ab_tests, variants, assignments = generate_experiment_domain(CONFIG, fake, users)
    save_csv(ab_tests, "ab_tests.csv")
    save_csv(variants, "ab_test_variants.csv")
    save_csv(assignments, "ab_test_assignments.csv")

    # 8. 交易域
    print("\n[8/8] 交易域...")
    orders, order_items, payments, subscriptions = generate_transaction_domain(CONFIG, fake, users, products, coupons)
    save_csv(orders, "orders.csv")
    save_csv(order_items, "order_items.csv")
    save_csv(payments, "payments.csv")
    save_csv(subscriptions, "subscriptions.csv")

    print("\n" + "=" * 50)
    print(f"CSV 文件已保存到: {OUTPUT_DIR}")
    print("=" * 50)


if __name__ == "__main__":
    main()
