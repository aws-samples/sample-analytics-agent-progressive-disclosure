"""
交易域数据生成器

生成交易相关的数据：
- orders: 订单主表
- order_items: 订单明细
- payments: 支付记录
- subscriptions: 会员订阅
"""

import random
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP


# 订单状态及其转移概率
ORDER_STATUS_FLOW = {
    "pending": ["paid", "cancelled"],
    "paid": ["shipped", "refunded"],
    "shipped": ["delivered", "refunded"],
    "delivered": ["refunded"],  # 已签收后还可申请退款
    "cancelled": [],
    "refunded": [],
}

# 支付方式
PAYMENT_METHODS = [
    {"method": "wechat", "name": "微信支付", "weight": 45},
    {"method": "alipay", "name": "支付宝", "weight": 40},
    {"method": "credit_card", "name": "信用卡", "weight": 10},
    {"method": "balance", "name": "余额支付", "weight": 5},
]

# 订阅计划
SUBSCRIPTION_PLANS = [
    {
        "plan_id": "monthly",
        "plan_name": "月度会员",
        "duration_days": 30,
        "price": 25.00,
        "original_price": 30.00,
    },
    {
        "plan_id": "quarterly",
        "plan_name": "季度会员",
        "duration_days": 90,
        "price": 68.00,
        "original_price": 90.00,
    },
    {
        "plan_id": "yearly",
        "plan_name": "年度会员",
        "duration_days": 365,
        "price": 198.00,
        "original_price": 360.00,
    },
]

# 收货地址城市（根据权重）
CITIES = [
    ("北京市", "北京市", 15),
    ("上海市", "上海市", 15),
    ("广州市", "广东省", 10),
    ("深圳市", "广东省", 10),
    ("杭州市", "浙江省", 8),
    ("成都市", "四川省", 6),
    ("武汉市", "湖北省", 5),
    ("南京市", "江苏省", 5),
    ("西安市", "陕西省", 4),
    ("重庆市", "重庆市", 4),
    ("苏州市", "江苏省", 4),
    ("天津市", "天津市", 3),
    ("长沙市", "湖南省", 3),
    ("郑州市", "河南省", 3),
    ("青岛市", "山东省", 2),
    ("宁波市", "浙江省", 2),
    ("东莞市", "广东省", 1),
]


def random_date(start, end):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def round_price(value):
    """四舍五入到2位小数"""
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def generate_shipping_address(fake):
    """生成收货地址"""
    city_info = random.choices(  # nosec B311
        CITIES,
        weights=[c[2] for c in CITIES]
    )[0]
    city, province = city_info[0], city_info[1]

    return {
        "receiver_name": fake.name(),
        "receiver_phone": fake.phone_number(),
        "province": province,
        "city": city,
        "district": fake.district(),
        "address": fake.street_address(),
        "postal_code": fake.postcode(),
    }


def calculate_final_status(created_at, end_date):
    """根据订单创建时间计算最终状态"""
    days_since_created = (end_date - created_at).days

    if days_since_created < 1:
        # 1天内的订单可能还在 pending
        return random.choices(  # nosec B311
            ["pending", "paid", "cancelled"],
            weights=[30, 60, 10]
        )[0]
    elif days_since_created < 3:
        # 1-3天的订单
        return random.choices(  # nosec B311
            ["pending", "paid", "shipped", "cancelled"],
            weights=[5, 30, 55, 10]
        )[0]
    elif days_since_created < 7:
        # 3-7天的订单
        return random.choices(  # nosec B311
            ["paid", "shipped", "delivered", "cancelled", "refunded"],
            weights=[10, 30, 45, 10, 5]
        )[0]
    else:
        # 7天以上的订单
        return random.choices(  # nosec B311
            ["delivered", "cancelled", "refunded"],
            weights=[80, 12, 8]
        )[0]


def generate_transaction_domain(config, fake, users, products, coupons):
    """
    生成交易域数据

    Args:
        config: 配置字典，包含数据规模参数
        fake: Faker 实例
        users: 用户列表
        products: 商品列表
        coupons: 优惠券列表

    Returns:
        tuple: (orders, order_items, payments, subscriptions)
    """
    orders = []
    order_items = []
    payments = []
    subscriptions = []

    # 时间范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=config.get("date_range_days", 365))

    # 数据规模
    num_orders = config.get("num_orders", 50000)
    num_subscriptions = config.get("num_subscriptions", int(len(users) * 0.1))  # 10%用户有订阅

    # 创建商品ID到商品的映射
    product_map = {p["product_id"]: p for p in products}
    product_ids = list(product_map.keys())

    # 创建优惠券映射
    coupon_map = {c["coupon_id"]: c for c in coupons} if coupons else {}

    # 用户购买频率分布（模拟帕累托分布）
    user_order_weights = []
    for user in users:
        # 根据用户等级设置购买频率
        level = user.get("level", 1)
        base_weight = level * 2
        # 添加随机波动
        weight = base_weight * (0.5 + random.random())  # nosec B311
        user_order_weights.append(weight)

    # 生成订单
    for i in range(num_orders):
        order_id = f"ORD{fake.unique.random_number(digits=12, fix_len=True)}"

        # 选择用户（按购买频率权重）
        user = random.choices(users, weights=user_order_weights)[0]  # nosec B311
        user_id = user["user_id"]

        # 订单创建时间
        created_at = random_date(start_date, end_date)

        # 计算最终状态
        final_status = calculate_final_status(created_at, end_date)

        # 生成订单商品（1-5件）
        num_items = random.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0]  # nosec B311
        selected_products = random.sample(product_ids, min(num_items, len(product_ids)))  # nosec B311

        items_subtotal = 0
        order_item_list = []

        for j, product_id in enumerate(selected_products):
            product = product_map[product_id]

            # 购买数量（大部分是1-2件）
            quantity = random.choices([1, 2, 3, 4, 5], weights=[60, 25, 10, 3, 2])[0]  # nosec B311

            # 单价（可能有折扣）
            original_price = product["price"]
            discount_rate = random.choices(  # nosec B311
                [1.0, 0.95, 0.9, 0.85, 0.8],
                weights=[50, 20, 15, 10, 5]
            )[0]
            unit_price = round_price(original_price * discount_rate)

            item_total = round_price(unit_price * quantity)
            items_subtotal += item_total

            order_item = {
                "item_id": f"{order_id}_item_{j+1}",
                "order_id": order_id,
                "product_id": product_id,
                "product_name": product["product_name"],
                "sku_id": product.get("sku_id", product_id),
                "quantity": quantity,
                "unit_price": unit_price,
                "original_price": original_price,
                "discount_amount": round_price((original_price - unit_price) * quantity),
                "total_amount": item_total,
                "created_at": created_at.isoformat(),
            }
            order_item_list.append(order_item)

        order_items.extend(order_item_list)

        # 计算订单金额
        shipping_fee = 0 if items_subtotal >= 99 else random.choice([6, 8, 10, 12])  # nosec B311

        # 优惠券抵扣
        coupon_discount = 0
        used_coupon_id = None
        if coupon_map and random.random() < 0.3:  # 30%使用优惠券  # nosec B311
            available_coupons = [c for c in coupons if c["min_purchase"] <= items_subtotal]
            if available_coupons:
                selected_coupon = random.choice(available_coupons)  # nosec B311
                used_coupon_id = selected_coupon["coupon_id"]
                if selected_coupon["coupon_type"] == "fixed_amount":
                    coupon_discount = selected_coupon["discount_value"]
                elif selected_coupon["coupon_type"] == "percentage":
                    coupon_discount = round_price(
                        items_subtotal * selected_coupon["discount_value"] / 100
                    )
                    if selected_coupon.get("max_discount"):
                        coupon_discount = min(coupon_discount, selected_coupon["max_discount"])

        # 最终金额
        total_amount = round_price(max(0.01, items_subtotal + shipping_fee - coupon_discount))

        # 收货地址
        shipping_address = generate_shipping_address(fake)

        # 状态时间线
        paid_at = None
        shipped_at = None
        delivered_at = None
        cancelled_at = None
        refunded_at = None

        if final_status != "pending" and final_status != "cancelled":
            paid_at = created_at + timedelta(minutes=random.randint(5, 60))  # nosec B311

        if final_status in ["shipped", "delivered", "refunded"]:
            shipped_at = paid_at + timedelta(hours=random.randint(2, 48))  # nosec B311

        if final_status in ["delivered", "refunded"]:
            delivered_at = shipped_at + timedelta(days=random.randint(1, 7))  # nosec B311

        if final_status == "cancelled":
            cancelled_at = created_at + timedelta(minutes=random.randint(10, 1440))  # nosec B311

        if final_status == "refunded":
            refunded_at = (delivered_at or paid_at) + timedelta(days=random.randint(1, 7))  # nosec B311

        # 订单备注
        remarks = random.choices(  # nosec B311
            [None, "请尽快发货", "周末送货", "放快递柜", "送货前电话联系"],
            weights=[70, 10, 5, 10, 5]
        )[0]

        order = {
            "order_id": order_id,
            "order_no": f"DD{created_at.strftime('%Y%m%d')}{fake.random_number(digits=8, fix_len=True)}",
            "user_id": user_id,
            "status": final_status,
            "item_count": len(order_item_list),
            "items_subtotal": round_price(items_subtotal),
            "shipping_fee": shipping_fee,
            "coupon_id": used_coupon_id,
            "coupon_discount": coupon_discount,
            "total_amount": total_amount,
            "paid_amount": total_amount if final_status not in ["pending", "cancelled"] else 0,
            "refund_amount": total_amount if final_status == "refunded" else 0,
            "receiver_name": shipping_address["receiver_name"],
            "receiver_phone": shipping_address["receiver_phone"],
            "shipping_province": shipping_address["province"],
            "shipping_city": shipping_address["city"],
            "shipping_district": shipping_address["district"],
            "shipping_address": shipping_address["address"],
            "shipping_postal_code": shipping_address["postal_code"],
            "remarks": remarks,
            "created_at": created_at.isoformat(),
            "paid_at": paid_at.isoformat() if paid_at else None,
            "shipped_at": shipped_at.isoformat() if shipped_at else None,
            "delivered_at": delivered_at.isoformat() if delivered_at else None,
            "cancelled_at": cancelled_at.isoformat() if cancelled_at else None,
            "refunded_at": refunded_at.isoformat() if refunded_at else None,
            "updated_at": (
                refunded_at or cancelled_at or delivered_at or
                shipped_at or paid_at or created_at
            ).isoformat(),
        }
        orders.append(order)

        # 生成支付记录（已支付的订单）
        if final_status not in ["pending", "cancelled"]:
            payment_method = random.choices(  # nosec B311
                PAYMENT_METHODS,
                weights=[m["weight"] for m in PAYMENT_METHODS]
            )[0]

            payment = {
                "payment_id": f"PAY{fake.unique.random_number(digits=12, fix_len=True)}",
                "order_id": order_id,
                "user_id": user_id,
                "payment_method": payment_method["method"],
                "payment_method_name": payment_method["name"],
                "amount": total_amount,
                "currency": "CNY",
                "status": "refunded" if final_status == "refunded" else "success",
                "transaction_id": fake.uuid4(),
                "paid_at": paid_at.isoformat(),
                "refunded_at": refunded_at.isoformat() if final_status == "refunded" else None,
                "created_at": paid_at.isoformat(),
            }
            payments.append(payment)

    # 生成会员订阅
    subscribed_users = random.sample(users, min(num_subscriptions, len(users)))  # nosec B311

    for user in subscribed_users:
        user_id = user["user_id"]

        # 选择订阅计划（月度最多，年度最少）
        plan = random.choices(  # nosec B311
            SUBSCRIPTION_PLANS,
            weights=[50, 30, 20]
        )[0]

        # 订阅开始时间（确保有足够的时间范围）
        latest_start = end_date - timedelta(days=plan["duration_days"])
        if latest_start < start_date:
            latest_start = start_date
        started_at = random_date(start_date, latest_start) if latest_start > start_date else start_date
        expires_at = started_at + timedelta(days=plan["duration_days"])

        # 订阅状态
        if expires_at > end_date:
            status = "active"
        elif random.random() < 0.6:  # 60%续费  # nosec B311
            status = "active"
            # 续费，更新过期时间
            expires_at = end_date + timedelta(days=random.randint(1, plan["duration_days"]))  # nosec B311
        else:
            status = "expired"

        # 是否自动续费
        auto_renew = random.random() < 0.4  # 40%开启自动续费  # nosec B311

        subscription = {
            "subscription_id": f"SUB{fake.unique.random_number(digits=10, fix_len=True)}",
            "user_id": user_id,
            "plan_id": plan["plan_id"],
            "plan_name": plan["plan_name"],
            "status": status,
            "price": plan["price"],
            "original_price": plan["original_price"],
            "currency": "CNY",
            "duration_days": plan["duration_days"],
            "started_at": started_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "auto_renew": auto_renew,
            "cancelled_at": None,
            "created_at": started_at.isoformat(),
            "updated_at": started_at.isoformat(),
        }
        subscriptions.append(subscription)

    print(f"  - 订单: {len(orders)} 条")
    print(f"  - 订单明细: {len(order_items)} 条")
    print(f"  - 支付记录: {len(payments)} 条")
    print(f"  - 会员订阅: {len(subscriptions)} 条")

    return orders, order_items, payments, subscriptions
