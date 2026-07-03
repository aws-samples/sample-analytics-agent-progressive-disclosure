"""
用户域数据生成器

生成表：
- users: 用户基本信息
- user_profiles: 用户画像
- user_devices: 用户设备
- user_segments: 用户分群定义
- user_segment_members: 用户分群成员
"""

import random
from datetime import datetime, timedelta


def random_date(start, end):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def generate_user_domain(config, fake):
    """
    生成用户域数据

    Args:
        config: 配置字典，包含 num_users, date_range_days 等
        fake: Faker 实例（中文）

    Returns:
        tuple: (users, profiles, devices, segments, segment_members)
    """
    num_users = config.get("num_users", 10000)
    date_range_days = config.get("date_range_days", 365)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=date_range_days)

    users = []
    profiles = []
    devices = []

    # 注册来源选项
    registration_sources = [
        "app_store", "google_play", "wechat_mini", "web",
        "referral", "ad_campaign", "organic", "huawei_store"
    ]

    # 用户状态
    user_statuses = ["active", "inactive", "suspended", "deleted"]
    status_weights = [0.75, 0.15, 0.05, 0.05]

    # 用户等级 (1-5 对应 bronze, silver, gold, platinum, diamond)
    user_levels = [1, 2, 3, 4, 5]
    level_weights = [0.40, 0.30, 0.15, 0.10, 0.05]

    # 兴趣标签
    interests_pool = [
        "数码科技", "时尚穿搭", "美妆护肤", "运动健身", "美食烹饪",
        "旅游出行", "母婴育儿", "家居生活", "金融理财", "汽车",
        "游戏电竞", "音乐影视", "读书学习", "宠物", "摄影"
    ]

    # 职业选项
    occupations = [
        "学生", "程序员", "设计师", "产品经理", "销售",
        "教师", "医生", "公务员", "自由职业", "企业主",
        "金融从业者", "律师", "工程师", "运营", "市场营销"
    ]

    # 收入水平
    income_levels = ["low", "medium", "high", "very_high"]
    income_weights = [0.25, 0.45, 0.22, 0.08]

    # 设备类型和品牌
    device_types = ["ios", "android", "web", "mini_program"]
    device_type_weights = [0.35, 0.50, 0.10, 0.05]

    ios_models = ["iPhone 15 Pro Max", "iPhone 15 Pro", "iPhone 15", "iPhone 14 Pro", "iPhone 14", "iPhone 13", "iPhone SE"]
    android_brands = ["华为", "小米", "OPPO", "vivo", "荣耀", "三星", "一加", "realme"]
    android_models = {
        "华为": ["Mate 60 Pro", "P60 Pro", "Mate 50", "nova 11"],
        "小米": ["14 Pro", "13 Ultra", "Redmi K70", "Redmi Note 13"],
        "OPPO": ["Find X7 Ultra", "Reno 11 Pro", "A2 Pro"],
        "vivo": ["X100 Pro", "S18 Pro", "Y100"],
        "荣耀": ["Magic 6 Pro", "90 GT", "X50"],
        "三星": ["Galaxy S24 Ultra", "Galaxy S23", "Galaxy A54"],
        "一加": ["12 Pro", "Ace 3", "Nord CE"],
        "realme": ["GT5 Pro", "真我 GT Neo5"]
    }

    # 中国城市和省份
    cities_by_province = {
        "北京市": ["北京"],
        "上海市": ["上海"],
        "广东省": ["广州", "深圳", "东莞", "佛山", "珠海"],
        "浙江省": ["杭州", "宁波", "温州", "嘉兴"],
        "江苏省": ["南京", "苏州", "无锡", "常州"],
        "四川省": ["成都", "绵阳", "德阳"],
        "湖北省": ["武汉", "宜昌", "襄阳"],
        "湖南省": ["长沙", "株洲", "湘潭"],
        "山东省": ["济南", "青岛", "烟台"],
        "河南省": ["郑州", "洛阳", "开封"],
        "福建省": ["福州", "厦门", "泉州"],
        "陕西省": ["西安", "咸阳", "宝鸡"],
        "重庆市": ["重庆"],
        "天津市": ["天津"],
    }

    print(f"  生成 {num_users} 个用户...")

    for i in range(num_users):
        user_id = i + 1  # 使用数值型 ID

        # 注册时间
        registered_at = random_date(start_date, end_date)

        # 最后活跃时间（注册后到现在之间）
        if random.random() < 0.8:  # 80%用户有活跃记录  # nosec B311
            last_active_at = random_date(registered_at, end_date)
        else:
            last_active_at = registered_at

        # 用户基本信息
        user = {
            "user_id": user_id,
            "username": fake.user_name(),
            "email": fake.email(),
            "phone": fake.phone_number(),
            "registered_at": registered_at.isoformat(),
            "registration_source": random.choice(registration_sources),  # nosec B311
            "status": random.choices(user_statuses, weights=status_weights)[0],  # nosec B311
            "user_level": random.choices(user_levels, weights=level_weights)[0],  # nosec B311
            "is_vip": random.random() < 0.12,  # 12% VIP用户  # nosec B311
            "last_active_at": last_active_at.isoformat()
        }
        users.append(user)

        # 用户画像
        province = random.choice(list(cities_by_province.keys()))  # nosec B311
        city = random.choice(cities_by_province[province])  # nosec B311

        # 年龄分布（18-65岁，偏年轻）
        age = int(random.triangular(18, 65, 28))  # nosec B311
        birth_year = datetime.now().year - age
        birth_date = f"{birth_year}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"  # nosec B311

        # 兴趣标签（1-5个）
        num_interests = random.randint(1, 5)  # nosec B311
        user_interests = random.sample(interests_pool, num_interests)  # nosec B311

        profile = {
            "user_id": user_id,
            "age": age,
            "gender": random.choice(["male", "female"]),  # nosec B311
            "birth_date": birth_date,
            "city": city,
            "province": province,
            "country": "中国",
            "interests": user_interests,
            "occupation": random.choice(occupations),  # nosec B311
            "income_level": random.choices(income_levels, weights=income_weights)[0]  # nosec B311
        }
        profiles.append(profile)

        # 用户设备（1-3个设备）
        num_devices = random.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]  # nosec B311
        primary_set = False

        for j in range(num_devices):
            device_id = f"D{user_id:08d}_{j+1}"
            device_type = random.choices(device_types, weights=device_type_weights)[0]  # nosec B311

            if device_type == "ios":
                device_brand = "Apple"
                device_model = random.choice(ios_models)  # nosec B311
                os_version = f"iOS {random.choice(['16.0', '16.5', '17.0', '17.1', '17.2'])}"  # nosec B311
            elif device_type == "android":
                device_brand = random.choice(android_brands)  # nosec B311
                device_model = random.choice(android_models[device_brand])  # nosec B311
                os_version = f"Android {random.choice(['12', '13', '14'])}"  # nosec B311
            elif device_type == "web":
                device_brand = "Browser"
                device_model = random.choice(["Chrome", "Safari", "Firefox", "Edge"])  # nosec B311
                os_version = random.choice(["Windows 11", "macOS 14", "Ubuntu 22.04"])  # nosec B311
            else:  # mini_program
                device_brand = "WeChat"
                device_model = "Mini Program"
                os_version = "WeChat 8.0"

            # App 版本
            app_version = f"{random.randint(3, 5)}.{random.randint(0, 9)}.{random.randint(0, 20)}"  # nosec B311

            # Push token（移动设备才有）
            push_token = None
            if device_type in ["ios", "android"]:
                push_token = fake.sha256()[:64]

            device = {
                "device_id": device_id,
                "user_id": user_id,
                "device_type": device_type,
                "os_version": os_version,
                "device_model": device_model,
                "device_brand": device_brand,
                "app_version": app_version,
                "push_token": push_token,
                "is_primary": not primary_set  # 第一个设备为主设备
            }
            devices.append(device)
            primary_set = True

    # 生成用户分群
    print("  生成用户分群...")
    segments = []
    segment_members = []

    segment_definitions = [
        {
            "name": "高价值用户",
            "type": "value",
            "description": "累计消费金额超过5000元的用户",
            "rules": {"total_spent": {"operator": ">=", "value": 5000}}
        },
        {
            "name": "新注册用户",
            "type": "lifecycle",
            "description": "最近30天内注册的用户",
            "rules": {"registered_days_ago": {"operator": "<=", "value": 30}}
        },
        {
            "name": "沉睡用户",
            "type": "lifecycle",
            "description": "超过30天未活跃的用户",
            "rules": {"inactive_days": {"operator": ">=", "value": 30}}
        },
        {
            "name": "VIP会员",
            "type": "membership",
            "description": "开通VIP会员的用户",
            "rules": {"is_vip": {"operator": "==", "value": True}}
        },
        {
            "name": "年轻用户",
            "type": "demographic",
            "description": "18-25岁的用户群体",
            "rules": {"age": {"operator": "between", "value": [18, 25]}}
        },
        {
            "name": "一线城市用户",
            "type": "geographic",
            "description": "北上广深用户",
            "rules": {"city": {"operator": "in", "value": ["北京", "上海", "广州", "深圳"]}}
        },
        {
            "name": "高活跃用户",
            "type": "engagement",
            "description": "周登录次数超过5次的用户",
            "rules": {"weekly_logins": {"operator": ">=", "value": 5}}
        },
        {
            "name": "购物车用户",
            "type": "behavior",
            "description": "购物车有商品但未购买的用户",
            "rules": {"cart_items": {"operator": ">", "value": 0}, "recent_purchase": {"operator": "==", "value": False}}
        },
        {
            "name": "iOS用户",
            "type": "device",
            "description": "使用iOS设备的用户",
            "rules": {"device_type": {"operator": "==", "value": "ios"}}
        },
        {
            "name": "推广注册用户",
            "type": "acquisition",
            "description": "通过广告推广注册的用户",
            "rules": {"registration_source": {"operator": "==", "value": "ad_campaign"}}
        },
    ]

    owners = ["marketing_team", "product_team", "data_team", "operations_team"]

    for i, seg_def in enumerate(segment_definitions):
        segment_id = i + 1  # 使用整数 ID

        segment = {
            "segment_id": segment_id,
            "segment_name": seg_def["name"],
            "segment_type": seg_def["type"],
            "description": seg_def["description"],
            "rules_json": seg_def["rules"],
            "owner": random.choice(owners),  # nosec B311
            "status": random.choice(["active", "active", "active", "inactive"])  # 75% active  # nosec B311
        }
        segments.append(segment)

        # 为每个分群随机分配成员（10%-40%的用户）
        member_ratio = random.uniform(0.10, 0.40)  # nosec B311
        member_count = int(num_users * member_ratio)
        member_users = random.sample(users, member_count)  # nosec B311

        for user in member_users:
            user_registered = datetime.fromisoformat(user["registered_at"])
            entered_at = random_date(user_registered, end_date)

            segment_member = {
                "user_id": user["user_id"],
                "segment_id": segment_id,
                "entered_at": entered_at.isoformat()
            }
            segment_members.append(segment_member)

    print(f"  完成: {len(users)} 用户, {len(profiles)} 画像, {len(devices)} 设备, {len(segments)} 分群, {len(segment_members)} 分群成员")

    return users, profiles, devices, segments, segment_members
