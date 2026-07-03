"""
商品域数据生成器

生成表：
- categories: 商品分类（支持多级）
- products: 商品信息
- product_tags: 商品标签
"""

import random
from datetime import datetime, timedelta


def random_date(start, end):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def generate_product_domain(config, fake):
    """
    生成商品域数据

    Args:
        config: 配置字典，包含 num_products, num_categories 等
        fake: Faker 实例（中文）

    Returns:
        tuple: (categories, products, product_tags)
    """
    num_products = config.get("num_products", 1000)
    num_categories = config.get("num_categories", 50)
    date_range_days = config.get("date_range_days", 365)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=date_range_days)

    # 定义分类层级结构
    category_hierarchy = {
        "服装": {
            "女装": ["连衣裙", "T恤", "衬衫", "外套", "裤子", "半身裙"],
            "男装": ["T恤", "衬衫", "外套", "裤子", "卫衣"],
            "童装": ["上衣", "裤子", "套装", "外套"],
            "内衣": ["文胸", "内裤", "睡衣", "保暖内衣"]
        },
        "数码": {
            "手机": ["智能手机", "老人机", "游戏手机"],
            "电脑": ["笔记本", "台式机", "平板电脑"],
            "配件": ["耳机", "充电器", "手机壳", "数据线"],
            "智能设备": ["智能手表", "智能手环", "智能音箱"]
        },
        "美妆": {
            "护肤": ["面霜", "精华", "面膜", "洁面", "防晒"],
            "彩妆": ["口红", "粉底", "眼影", "腮红", "眉笔"],
            "香水": ["女士香水", "男士香水", "中性香水"],
            "个护": ["洗发水", "沐浴露", "牙膏", "身体乳"]
        },
        "食品": {
            "零食": ["坚果", "饼干", "糖果", "膨化食品", "蜜饯"],
            "饮料": ["矿泉水", "果汁", "碳酸饮料", "茶饮", "咖啡"],
            "生鲜": ["水果", "蔬菜", "肉类", "海鲜"],
            "粮油": ["大米", "食用油", "面粉", "调味品"]
        },
        "家居": {
            "家纺": ["床上用品", "毛巾", "窗帘", "地毯"],
            "收纳": ["收纳箱", "衣架", "置物架", "收纳袋"],
            "厨具": ["锅具", "餐具", "刀具", "保鲜盒"],
            "日用品": ["纸巾", "清洁剂", "垃圾袋", "拖把"]
        },
        "母婴": {
            "奶粉": ["婴儿奶粉", "儿童奶粉", "孕妇奶粉"],
            "纸尿裤": ["婴儿纸尿裤", "拉拉裤", "成人纸尿裤"],
            "玩具": ["益智玩具", "毛绒玩具", "积木", "遥控玩具"],
            "童车": ["婴儿推车", "儿童自行车", "学步车"]
        },
        "运动": {
            "运动鞋": ["跑步鞋", "篮球鞋", "足球鞋", "健身鞋"],
            "运动服": ["运动T恤", "运动裤", "运动套装"],
            "健身器材": ["哑铃", "瑜伽垫", "跳绳", "拉力带"],
            "户外装备": ["帐篷", "登山杖", "睡袋", "户外背包"]
        },
        "家电": {
            "大家电": ["冰箱", "洗衣机", "空调", "电视"],
            "小家电": ["电饭煲", "电热水壶", "吹风机", "榨汁机"],
            "厨卫电器": ["油烟机", "燃气灶", "热水器", "消毒柜"],
            "生活电器": ["扫地机器人", "空气净化器", "加湿器"]
        }
    }

    # 品牌库
    brands_by_category = {
        "服装": ["优衣库", "ZARA", "H&M", "太平鸟", "森马", "海澜之家", "GXG", "UR", "波司登"],
        "数码": ["苹果", "华为", "小米", "OPPO", "vivo", "联想", "戴尔", "惠普", "索尼", "三星"],
        "美妆": ["兰蔻", "雅诗兰黛", "SK-II", "欧莱雅", "完美日记", "花西子", "珀莱雅", "百雀羚"],
        "食品": ["三只松鼠", "良品铺子", "百草味", "洽洽", "伊利", "蒙牛", "农夫山泉", "统一"],
        "家居": ["宜家", "网易严选", "小米有品", "九阳", "苏泊尔", "美的", "德尔玛"],
        "母婴": ["帮宝适", "好奇", "美赞臣", "雅培", "惠氏", "贝亲", "好孩子", "巴拉巴拉"],
        "运动": ["耐克", "阿迪达斯", "李宁", "安踏", "匹克", "特步", "斐乐", "彪马"],
        "家电": ["美的", "海尔", "格力", "小米", "苏泊尔", "九阳", "科沃斯", "戴森"]
    }

    # 标签类型和标签池
    tag_types = {
        "promotion": ["热销", "新品", "限时特惠", "秒杀", "满减", "买一送一", "清仓"],
        "feature": ["包邮", "正品保证", "七天无理由", "急速发货", "自营", "进口"],
        "season": ["春季新款", "夏季清凉", "秋冬保暖", "四季通用"],
        "audience": ["学生专享", "白领首选", "送礼佳品", "家庭装", "情侣款"],
        "style": ["简约", "潮流", "复古", "文艺", "轻奢", "国潮"]
    }

    categories = []
    products = []
    product_tags = []

    # 生成分类数据
    print(f"  生成商品分类...")
    category_id_counter = 1
    category_map = {}  # 用于记录分类名到ID的映射
    leaf_categories = []  # 叶子分类（三级分类）

    for level1_name, level2_dict in category_hierarchy.items():
        # 一级分类
        level1_id = category_id_counter
        category_id_counter += 1

        categories.append({
            "category_id": level1_id,
            "parent_id": None,
            "category_name": level1_name,
            "level": 1,
            "sort_order": len(categories) + 1,
            "icon_url": f"https://cdn.example.com/icons/category/{level1_id}.png",
            "is_active": True,
            "created_at": start_date.isoformat(),
            "updated_at": end_date.isoformat()
        })
        category_map[level1_name] = level1_id

        for level2_name, level3_list in level2_dict.items():
            # 二级分类
            level2_id = category_id_counter
            category_id_counter += 1

            categories.append({
                "category_id": level2_id,
                "parent_id": level1_id,
                "category_name": level2_name,
                "level": 2,
                "sort_order": len(categories) + 1,
                "icon_url": f"https://cdn.example.com/icons/category/{level2_id}.png",
                "is_active": True,
                "created_at": start_date.isoformat(),
                "updated_at": end_date.isoformat()
            })
            category_map[f"{level1_name}>{level2_name}"] = level2_id

            for level3_name in level3_list:
                # 三级分类
                level3_id = category_id_counter
                category_id_counter += 1

                full_path = f"{level1_name}>{level2_name}>{level3_name}"
                categories.append({
                    "category_id": level3_id,
                    "parent_id": level2_id,
                    "category_name": level3_name,
                    "level": 3,
                    "sort_order": len(categories) + 1,
                    "icon_url": f"https://cdn.example.com/icons/category/{level3_id}.png",
                    "is_active": True,
                    "created_at": start_date.isoformat(),
                    "updated_at": end_date.isoformat()
                })
                category_map[full_path] = level3_id
                leaf_categories.append({
                    "id": level3_id,
                    "name": level3_name,
                    "path": full_path,
                    "level1": level1_name
                })

    # 生成商品数据
    print(f"  生成 {num_products} 个商品...")

    # 商品状态
    product_statuses = ["on_sale", "off_sale", "sold_out", "pre_sale"]
    status_weights = [0.70, 0.10, 0.10, 0.10]

    for i in range(num_products):
        product_id = i + 1  # 使用数值型 ID

        # 随机选择叶子分类
        category_info = random.choice(leaf_categories)  # nosec B311
        category_id = category_info["id"]
        level1_name = category_info["level1"]

        # 选择对应品牌
        available_brands = brands_by_category.get(level1_name, ["自有品牌"])
        brand = random.choice(available_brands)  # nosec B311

        # 生成价格（原价、现价、成本）
        base_price = random.choice([  # nosec B311
            random.uniform(10, 100),      # 低价商品  # nosec B311
            random.uniform(100, 500),     # 中价商品  # nosec B311
            random.uniform(500, 2000),    # 高价商品  # nosec B311
            random.uniform(2000, 10000),  # 高端商品  # nosec B311
        ])
        original_price = round(base_price, 2)

        # 折扣率 0.6-1.0
        discount = random.uniform(0.6, 1.0)  # nosec B311
        price = round(original_price * discount, 2)

        # 成本（售价的 30%-70%）
        cost_ratio = random.uniform(0.3, 0.7)  # nosec B311
        cost = round(price * cost_ratio, 2)

        # 库存和销量
        stock = random.randint(0, 5000)  # nosec B311
        sold_count = random.randint(0, 50000)  # nosec B311

        # 评分
        rating = round(random.uniform(3.5, 5.0), 1)  # nosec B311
        review_count = random.randint(0, sold_count // 10 + 1)  # nosec B311

        # 创建时间
        created_at = random_date(start_date, end_date)

        # 更新时间（创建后随机时间）
        updated_at = random_date(created_at, end_date)

        # 生成商品名称
        adjectives = ["精选", "优质", "时尚", "经典", "新款", "热卖", "限定", ""]
        product_name = f"{random.choice(adjectives)}{brand} {category_info['name']}".strip()  # nosec B311

        # 商品描述
        descriptions = [
            f"高品质{category_info['name']}，{brand}出品，品质保证",
            f"{brand}官方正品，支持七天无理由退换",
            f"爆款{category_info['name']}，销量领先，好评如潮",
            f"精选材质，舒适耐用，性价比之选"
        ]

        # 生成额外字段
        view_count = random.randint(sold_count, sold_count * 50 + 100)  # nosec B311
        favorite_count = random.randint(0, sold_count * 2)  # nosec B311

        # 生成图片URL数组 (PostgreSQL ARRAY 格式)
        num_images = random.randint(3, 8)  # nosec B311
        image_urls = [f"https://cdn.example.com/products/{product_id}/{j+1}.jpg" for j in range(num_images)]

        product = {
            "product_id": product_id,
            "product_name": product_name,
            "category_id": category_id,
            "brand": brand,
            "description": random.choice(descriptions),  # nosec B311
            "price": price,
            "original_price": original_price,
            "cost": cost,
            "stock": stock,
            "sold_count": sold_count,
            "view_count": view_count,
            "favorite_count": favorite_count,
            "rating_avg": rating,
            "rating_count": review_count,
            "main_image_url": f"https://cdn.example.com/products/{product_id}/main.jpg",
            "image_urls": image_urls,
            "status": random.choices(product_statuses, weights=status_weights)[0],  # nosec B311
            "is_featured": random.random() < 0.05,  # nosec B311
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat()
        }
        products.append(product)

        # 为商品生成标签（1-4个标签）
        num_tags = random.randint(1, 4)  # nosec B311
        selected_tag_types = random.sample(list(tag_types.keys()), min(num_tags, len(tag_types)))  # nosec B311

        for tag_type in selected_tag_types:
            tag_name = random.choice(tag_types[tag_type])  # nosec B311
            product_tag = {
                "id": len(product_tags) + 1,
                "product_id": product_id,
                "tag_name": tag_name,
                "tag_type": tag_type,
                "created_at": created_at.isoformat()
            }
            product_tags.append(product_tag)

    print(f"  完成: {len(categories)} 分类, {len(products)} 商品, {len(product_tags)} 标签")

    return categories, products, product_tags
