"""
运营域数据生成器

生成表:
- campaigns: 运营活动
- push_notifications: 推送记录
- coupons: 优惠券
- user_coupons: 用户领券记录
- banners: Banner 资源位
"""

import random
from datetime import datetime, timedelta


# 时间范围
END_DATE = datetime.now()
START_DATE = END_DATE - timedelta(days=365)


def random_date(start=START_DATE, end=END_DATE):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def random_date_after(base_date, max_days=30):
    """生成 base_date 之后的随机日期"""
    days = random.randint(0, max_days)  # nosec B311
    result = base_date + timedelta(days=days, hours=random.randint(0, 23), minutes=random.randint(0, 59))  # nosec B311
    if result > END_DATE:
        return END_DATE
    return result


def generate_marketing_domain(config, fake, users, segments):
    """
    生成运营域数据

    Args:
        config: 配置字典，包含 num_campaigns 等参数
        fake: Faker 实例 (zh_CN)
        users: 用户列表
        segments: 用户分群列表

    Returns:
        tuple: (campaigns, push_notifications, coupons, user_coupons, banners)
    """
    num_campaigns = config.get("num_campaigns", 100)

    # 提取 ID 列表
    user_ids = [u["user_id"] for u in users]
    segment_ids = [s["segment_id"] for s in segments] if segments else []

    # 1. 生成运营活动
    campaigns = generate_campaigns(num_campaigns, segment_ids, fake)
    print(f"  - 运营活动: {len(campaigns)} 条")

    campaign_ids = [c["campaign_id"] for c in campaigns]

    # 2. 生成优惠券
    coupons = generate_coupons(campaign_ids, fake)
    print(f"  - 优惠券: {len(coupons)} 条")

    coupon_ids = [c["coupon_id"] for c in coupons]
    coupon_map = {c["coupon_id"]: c for c in coupons}

    # 3. 生成用户领券记录
    user_coupons = generate_user_coupons(coupon_ids, coupon_map, user_ids, fake)
    print(f"  - 用户领券: {len(user_coupons)} 条")

    # 4. 生成推送记录
    push_notifications = generate_push_notifications(campaign_ids, user_ids, fake)
    print(f"  - 推送记录: {len(push_notifications)} 条")

    # 5. 生成 Banner 资源位
    banners = generate_banners(campaign_ids, fake)
    print(f"  - Banner: {len(banners)} 条")

    return campaigns, push_notifications, coupons, user_coupons, banners


def generate_campaigns(num_campaigns, segment_ids, fake):
    """生成运营活动"""
    campaigns = []

    campaign_types = ["promotion", "festival", "new_user", "recall"]
    campaign_type_weights = [0.4, 0.25, 0.2, 0.15]

    # 活动名称模板
    promotion_names = [
        "限时特惠", "满减狂欢", "品类日", "会员专享",
        "周末特卖", "月末清仓", "新品首发", "爆款返场",
        "超级品牌日", "秒杀专场"
    ]

    festival_names = [
        "双十一狂欢节", "双十二购物节", "618年中大促",
        "春节不打烊", "元宵节特惠", "情人节礼遇",
        "38女神节", "端午特惠", "中秋团圆季",
        "国庆黄金周", "年货节"
    ]

    new_user_names = [
        "新人专享礼包", "首单立减", "新手大礼包",
        "注册有礼", "首购特惠", "新人福利社"
    ]

    recall_names = [
        "老用户回馈", "许久不见", "专属回归礼",
        "想你了", "回来看看", "会员召回"
    ]

    for i in range(num_campaigns):
        campaign_type = random.choices(campaign_types, weights=campaign_type_weights)[0]  # nosec B311

        # 根据类型选择名称
        if campaign_type == "promotion":
            name = random.choice(promotion_names)  # nosec B311
        elif campaign_type == "festival":
            name = random.choice(festival_names)  # nosec B311
        elif campaign_type == "new_user":
            name = random.choice(new_user_names)  # nosec B311
        else:  # recall
            name = random.choice(recall_names)  # nosec B311

        # 活动时间
        start_date = random_date()
        duration_days = random.randint(1, 30)  # nosec B311
        end_date = start_date + timedelta(days=duration_days)

        # 目标分群
        target_segments = []
        if segment_ids and random.random() < 0.7:  # 70% 有目标分群  # nosec B311
            num_target = random.randint(1, min(3, len(segment_ids)))  # nosec B311
            target_segments = random.sample(segment_ids, k=num_target)  # nosec B311

        campaign = {
            "campaign_id": i + 1,
            "name": f"{name}_{fake.word()}",
            "campaign_type": campaign_type,
            "description": fake.paragraph(nb_sentences=2),
            "target_segments": target_segments,
            "budget": random.choice([10000, 50000, 100000, 500000, 1000000]),  # nosec B311
            "spent": 0,  # 实际花费，后续计算
            "target_metric": random.choice(["conversion", "retention", "revenue", "engagement"]),  # nosec B311
            "target_value": random.randint(100, 10000),  # nosec B311
            "actual_value": 0,  # 实际达成，后续计算
            "status": random.choices(  # nosec B311
                ["draft", "scheduled", "active", "paused", "completed", "cancelled"],
                weights=[0.05, 0.1, 0.2, 0.05, 0.55, 0.05]
            )[0],
            "priority": random.randint(1, 5),  # nosec B311
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "created_at": (start_date - timedelta(days=random.randint(1, 14))).isoformat(),  # nosec B311
            "created_by": f"admin_{random.randint(1, 10)}"  # nosec B311
        }

        # 计算花费和达成（如果活动已完成）
        if campaign["status"] == "completed":
            campaign["spent"] = int(campaign["budget"] * random.uniform(0.6, 1.0))  # nosec B311
            campaign["actual_value"] = int(campaign["target_value"] * random.uniform(0.5, 1.5))  # nosec B311
        elif campaign["status"] == "active":
            campaign["spent"] = int(campaign["budget"] * random.uniform(0.2, 0.6))  # nosec B311
            campaign["actual_value"] = int(campaign["target_value"] * random.uniform(0.2, 0.6))  # nosec B311

        campaigns.append(campaign)

    return campaigns


def generate_coupons(campaign_ids, fake):
    """生成优惠券"""
    coupons = []

    coupon_types = ["fixed_amount", "percentage", "free_shipping"]
    coupon_type_weights = [0.5, 0.35, 0.15]

    # 每个活动关联 0-5 张优惠券，额外生成一些独立优惠券
    total_coupons = len(campaign_ids) * 2 + 50

    # 优惠券名称
    coupon_names = [
        "满减券", "折扣券", "包邮券", "新人券",
        "会员券", "专属券", "限时券", "无门槛券"
    ]

    for i in range(total_coupons):
        coupon_type = random.choices(coupon_types, weights=coupon_type_weights)[0]  # nosec B311

        # 关联活动（70% 概率）
        campaign_id = None
        if campaign_ids and random.random() < 0.7:  # nosec B311
            campaign_id = random.choice(campaign_ids)  # nosec B311

        # 根据类型设置金额/折扣
        if coupon_type == "fixed_amount":
            discount_value = random.choice([5, 10, 20, 30, 50, 100, 200])  # nosec B311
            min_purchase = discount_value * random.choice([2, 3, 5, 10])  # nosec B311
            name = f"满{min_purchase}减{discount_value}"
        elif coupon_type == "percentage":
            discount_value = random.choice([5, 10, 15, 20, 30])  # 百分比  # nosec B311
            min_purchase = random.choice([0, 50, 100, 200, 500])  # nosec B311
            max_discount = random.choice([None, 50, 100, 200]) if min_purchase > 0 else None  # nosec B311
            name = f"{discount_value}折券" if min_purchase == 0 else f"满{min_purchase}享{discount_value/10}折"
        else:  # free_shipping
            discount_value = 0
            min_purchase = random.choice([0, 29, 49, 99])  # nosec B311
            max_discount = None
            name = "包邮券" if min_purchase == 0 else f"满{min_purchase}包邮"

        # 有效期
        start_date = random_date()
        valid_days = random.choice([3, 7, 15, 30, 90])  # nosec B311
        end_date = start_date + timedelta(days=valid_days)

        # 发放数量和领取数量
        total_quantity = random.choice([100, 500, 1000, 5000, 10000, -1])  # -1 表示不限量  # nosec B311
        claimed_quantity = 0 if total_quantity == -1 else random.randint(0, total_quantity)  # nosec B311
        used_quantity = random.randint(0, claimed_quantity) if claimed_quantity > 0 else 0  # nosec B311

        coupon = {
            "coupon_id": i + 1,
            "campaign_id": campaign_id,
            "name": name,
            "coupon_type": coupon_type,
            "discount_value": discount_value,
            "min_purchase": min_purchase,
            "max_discount": max_discount if coupon_type == "percentage" else None,
            "applicable_categories": random.sample(
                ["electronics", "clothing", "food", "beauty", "home", "sports", "all"],
                k=random.randint(1, 3)  # nosec B311
            ) if random.random() < 0.3 else ["all"],  # nosec B311
            "total_quantity": total_quantity,
            "claimed_quantity": claimed_quantity,
            "used_quantity": used_quantity,
            "per_user_limit": random.choice([1, 2, 3, 5]),  # nosec B311
            "is_stackable": random.random() < 0.2,  # 20% 可叠加  # nosec B311
            "status": random.choices(  # nosec B311
                ["active", "inactive", "expired", "depleted"],
                weights=[0.4, 0.1, 0.4, 0.1]
            )[0],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "created_at": (start_date - timedelta(days=random.randint(1, 7))).isoformat()  # nosec B311
        }
        coupons.append(coupon)

    return coupons


def generate_user_coupons(coupon_ids, coupon_map, user_ids, fake):
    """生成用户领券记录"""
    user_coupons = []
    user_coupon_set = set()  # 用于控制同一用户领取同一券的次数

    # 生成 20000-50000 条领券记录
    num_records = random.randint(20000, 50000)  # nosec B311

    for i in range(num_records):
        coupon_id = random.choice(coupon_ids)  # nosec B311
        user_id = random.choice(user_ids)  # nosec B311
        coupon = coupon_map.get(coupon_id, {})

        # 检查是否超过领取限制
        per_user_limit = coupon.get("per_user_limit", 1)
        user_coupon_key = (user_id, coupon_id)
        current_count = sum(1 for uc in user_coupons
                          if uc["user_id"] == user_id and uc["coupon_id"] == coupon_id)

        if current_count >= per_user_limit:
            continue

        # 领取时间
        coupon_start = coupon.get("start_date", START_DATE.isoformat())
        if isinstance(coupon_start, str):
            try:
                coupon_start = datetime.fromisoformat(coupon_start)
            except:
                coupon_start = START_DATE

        claimed_at = random_date_after(coupon_start, max_days=30)

        # 使用状态
        status = random.choices(  # nosec B311
            ["unused", "used", "expired"],
            weights=[0.3, 0.5, 0.2]
        )[0]

        used_at = None
        order_id = None
        if status == "used":
            used_at = random_date_after(claimed_at, max_days=15)
            order_id = f"ORD{random.randint(1, 50000):08d}"  # nosec B311

        user_coupon = {
            "user_coupon_id": len(user_coupons) + 1,
            "user_id": user_id,
            "coupon_id": coupon_id,
            "status": status,
            "claimed_at": claimed_at.isoformat(),
            "used_at": used_at.isoformat() if used_at else None,
            "order_id": order_id,
            "source": random.choices(  # nosec B311
                ["claim", "gift", "reward", "system"],
                weights=[0.6, 0.15, 0.15, 0.1]
            )[0]
        }
        user_coupons.append(user_coupon)

    return user_coupons


def generate_push_notifications(campaign_ids, user_ids, fake):
    """生成推送记录"""
    notifications = []

    # 推送类型和模板
    push_types = [
        ("promotion", ["限时特惠！{}折起", "今日爆款，立省{}元", "专属福利，速来领取"]),
        ("order", ["您的订单已发货", "订单即将送达", "订单已完成，期待您的评价"]),
        ("social", ["{}关注了你", "你的帖子收到了新评论", "{}给你发来了私信"]),
        ("reminder", ["购物车商品降价了", "收藏的商品即将售罄", "会员即将到期，续费享优惠"]),
        ("system", ["系统维护通知", "隐私政策更新", "新功能上线"])
    ]

    # 生成 50000-100000 条推送记录
    num_notifications = random.randint(50000, 100000)  # nosec B311

    for i in range(num_notifications):
        push_type_info = random.choice(push_types)  # nosec B311
        push_type = push_type_info[0]
        template = random.choice(push_type_info[1])  # nosec B311

        # 填充模板
        if "{}" in template:
            if push_type == "promotion":
                content = template.format(random.randint(3, 9))  # nosec B311
            elif push_type == "social":
                content = template.format(fake.name())
            else:
                content = template.format(random.randint(10, 100))  # nosec B311
        else:
            content = template

        # 关联活动（promotion 类型）
        campaign_id = None
        if push_type == "promotion" and campaign_ids and random.random() < 0.8:  # nosec B311
            campaign_id = random.choice(campaign_ids)  # nosec B311

        sent_at = random_date()

        # 送达和点击状态
        is_delivered = random.random() < 0.95  # 95% 送达率  # nosec B311
        is_clicked = is_delivered and random.random() < 0.15  # 15% 点击率（在送达基础上）  # nosec B311

        notification = {
            "notification_id": i + 1,
            "user_id": random.choice(user_ids),  # nosec B311
            "campaign_id": campaign_id,
            "push_type": push_type,
            "title": fake.sentence(nb_words=4)[:20],
            "content": content,
            "deep_link": f"app://page/{push_type}/{random.randint(1, 1000)}",  # nosec B311
            "is_delivered": is_delivered,
            "is_clicked": is_clicked,
            "delivered_at": sent_at.isoformat() if is_delivered else None,
            "clicked_at": random_date_after(sent_at, max_days=1).isoformat() if is_clicked else None,
            "platform": random.choices(["ios", "android"], weights=[0.45, 0.55])[0],  # nosec B311
            "sent_at": sent_at.isoformat()
        }
        notifications.append(notification)

    return notifications


def generate_banners(campaign_ids, fake):
    """生成 Banner 资源位"""
    banners = []

    # Banner 位置
    positions = [
        ("home_top", "首页顶部轮播"),
        ("home_middle", "首页中部"),
        ("category_top", "分类页顶部"),
        ("cart_bottom", "购物车底部"),
        ("search_top", "搜索页顶部"),
        ("detail_bottom", "商品详情底部"),
        ("splash", "开屏广告"),
    ]

    # 每个位置生成多个 Banner（不同时间段）
    banner_id = 0

    for position_code, position_name in positions:
        # 每个位置 10-30 个 Banner
        num_banners = random.randint(10, 30)  # nosec B311

        for _ in range(num_banners):
            banner_id += 1

            # 关联活动（70% 概率）
            campaign_id = None
            if campaign_ids and random.random() < 0.7:  # nosec B311
                campaign_id = random.choice(campaign_ids)  # nosec B311

            start_date = random_date()
            duration = random.choice([1, 3, 7, 14, 30])  # nosec B311
            end_date = start_date + timedelta(days=duration)

            # 点击数据
            impressions = random.randint(1000, 1000000)  # nosec B311
            clicks = int(impressions * random.uniform(0.01, 0.1))  # 1-10% CTR  # nosec B311

            banner = {
                "banner_id": banner_id,
                "campaign_id": campaign_id,
                "position": position_code,
                "position_name": position_name,
                "title": fake.sentence(nb_words=5)[:30],
                "image_url": f"https://cdn.example.com/banners/{banner_id}.jpg",
                "target_url": f"app://campaign/{campaign_id}" if campaign_id else f"app://page/{random.randint(1, 100)}",  # nosec B311
                "target_type": random.choice(["campaign", "product", "category", "external", "content"]),  # nosec B311
                "sort_order": random.randint(1, 10),  # nosec B311
                "impressions": impressions,
                "clicks": clicks,
                "ctr": round(clicks / impressions * 100, 2) if impressions > 0 else 0,
                "status": random.choices(  # nosec B311
                    ["active", "inactive", "scheduled", "expired"],
                    weights=[0.3, 0.1, 0.1, 0.5]
                )[0],
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "created_at": (start_date - timedelta(days=random.randint(1, 7))).isoformat()  # nosec B311
            }
            banners.append(banner)

    return banners
