#!/usr/bin/env python3
"""
APP Analytics Demo - Data Generator

生成 35 张表的示例数据，用于演示 Agent Skill 的按需加载能力。
数据规模：
- 用户: 10,000
- 商品: 1,000
- 订单: 50,000
- 事件: 500,000
"""

import os
import sys
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

# 尝试导入 faker，如果没有则提示安装
try:
    from faker import Faker
except ImportError:
    print("请先安装 faker: pip install faker")
    sys.exit(1)

# 初始化
fake = Faker('zh_CN')
random.seed(42)
Faker.seed(42)

# 配置
OUTPUT_DIR = Path(__file__).parent.parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

# 数据规模配置
CONFIG = {
    "num_users": 10000,
    "num_products": 1000,
    "num_categories": 50,
    "num_channels": 20,
    "num_campaigns": 100,
    "num_posts": 20000,
    "num_orders": 50000,
    "num_events": 500000,
    "num_sessions": 100000,
    "date_range_days": 365,  # 过去一年的数据
}

# 时间范围
END_DATE = datetime.now()
START_DATE = END_DATE - timedelta(days=CONFIG["date_range_days"])


def random_date(start=START_DATE, end=END_DATE):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def random_date_after(base_date, max_days=30):
    """生成 base_date 之后的随机日期"""
    days = random.randint(0, max_days)  # nosec B311
    return base_date + timedelta(days=days, hours=random.randint(0, 23))  # nosec B311


def save_to_json(data, filename):
    """保存数据到 JSON 文件"""
    filepath = OUTPUT_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"已保存: {filepath} ({len(data)} 条记录)")


def save_to_csv(data, filename):
    """保存数据到 CSV 文件"""
    import csv
    if not data:
        return
    filepath = OUTPUT_DIR / filename
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    print(f"已保存: {filepath} ({len(data)} 条记录)")


# 导入各域的生成器
from generators.user_domain import generate_user_domain
from generators.product_domain import generate_product_domain
from generators.channel_domain import generate_channel_domain
from generators.behavior_domain import generate_behavior_domain
from generators.social_domain import generate_social_domain
from generators.marketing_domain import generate_marketing_domain
from generators.experiment_domain import generate_experiment_domain
from generators.transaction_domain import generate_transaction_domain


def main():
    print("=" * 60)
    print("APP Analytics Demo - 数据生成器")
    print("=" * 60)

    # 1. 基础域（无依赖）
    print("\n[1/8] 生成商品域数据...")
    categories, products, product_tags = generate_product_domain(CONFIG, fake)

    print("\n[2/8] 生成用户域数据...")
    users, profiles, devices, segments, segment_members = generate_user_domain(CONFIG, fake)

    print("\n[3/8] 生成渠道归因域数据...")
    channels, ad_campaigns, ad_creatives, attributions, channel_costs = generate_channel_domain(
        CONFIG, fake, users
    )

    # 2. 依赖用户的域
    print("\n[4/8] 生成行为域数据...")
    event_defs, events, sessions, page_views = generate_behavior_domain(
        CONFIG, fake, users, products
    )

    print("\n[5/8] 生成社交域数据...")
    follows, posts, likes, comments, shares, messages = generate_social_domain(
        CONFIG, fake, users, products
    )

    print("\n[6/8] 生成运营域数据...")
    campaigns, push_notifications, coupons, user_coupons, banners = generate_marketing_domain(
        CONFIG, fake, users, segments
    )

    print("\n[7/8] 生成实验域数据...")
    ab_tests, variants, assignments = generate_experiment_domain(CONFIG, fake, users)

    print("\n[8/8] 生成交易域数据...")
    orders, order_items, payments, subscriptions = generate_transaction_domain(
        CONFIG, fake, users, products, coupons
    )

    # 3. 保存所有数据
    print("\n" + "=" * 60)
    print("保存数据文件...")
    print("=" * 60)

    # 用户域
    save_to_json(users, "users.json")
    save_to_json(profiles, "user_profiles.json")
    save_to_json(devices, "user_devices.json")
    save_to_json(segments, "user_segments.json")
    save_to_json(segment_members, "user_segment_members.json")

    # 商品域
    save_to_json(categories, "categories.json")
    save_to_json(products, "products.json")
    save_to_json(product_tags, "product_tags.json")

    # 渠道域
    save_to_json(channels, "channels.json")
    save_to_json(ad_campaigns, "ad_campaigns.json")
    save_to_json(ad_creatives, "ad_creatives.json")
    save_to_json(attributions, "user_attributions.json")
    save_to_json(channel_costs, "channel_daily_costs.json")

    # 行为域
    save_to_json(event_defs, "event_definitions.json")
    save_to_json(sessions, "sessions.json")
    save_to_json(page_views, "page_views.json")
    # events 太大，用 CSV
    save_to_csv(events, "events.csv")

    # 社交域
    save_to_json(follows, "user_follows.json")
    save_to_json(posts, "posts.json")
    save_to_json(likes, "post_likes.json")
    save_to_json(comments, "post_comments.json")
    save_to_json(shares, "post_shares.json")
    save_to_json(messages, "user_messages.json")

    # 运营域
    save_to_json(campaigns, "campaigns.json")
    save_to_json(push_notifications, "push_notifications.json")
    save_to_json(coupons, "coupons.json")
    save_to_json(user_coupons, "user_coupons.json")
    save_to_json(banners, "banners.json")

    # 实验域
    save_to_json(ab_tests, "ab_tests.json")
    save_to_json(variants, "ab_test_variants.json")
    save_to_json(assignments, "ab_test_assignments.json")

    # 交易域
    save_to_json(orders, "orders.json")
    save_to_json(order_items, "order_items.json")
    save_to_json(payments, "payments.json")
    save_to_json(subscriptions, "subscriptions.json")

    print("\n" + "=" * 60)
    print("数据生成完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
