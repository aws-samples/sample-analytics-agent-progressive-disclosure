"""
实验域数据生成器

生成 A/B 测试相关的数据：
- ab_tests: A/B 测试配置
- ab_test_variants: 实验分支
- ab_test_assignments: 用户分组
"""

import random
from datetime import datetime, timedelta


# A/B 测试模板
AB_TEST_TEMPLATES = [
    {
        "name": "首页推荐算法优化",
        "key": "homepage_recommendation_v2",
        "hypothesis": "使用协同过滤算法替代基于规则的推荐，可以提升首页商品点击率",
        "primary_metric": "homepage_ctr",
        "secondary_metrics": ["add_to_cart_rate", "conversion_rate"],
        "variants": [
            {"name": "control", "description": "原有基于规则的推荐"},
            {"name": "treatment_a", "description": "协同过滤算法"},
            {"name": "treatment_b", "description": "深度学习推荐模型"},
        ]
    },
    {
        "name": "购物车按钮颜色测试",
        "key": "cart_button_color",
        "hypothesis": "将加购按钮颜色从蓝色改为橙色，可以提升加购转化率",
        "primary_metric": "add_to_cart_rate",
        "secondary_metrics": ["checkout_rate"],
        "variants": [
            {"name": "control", "description": "蓝色按钮 (#1890ff)"},
            {"name": "treatment_a", "description": "橙色按钮 (#ff6600)"},
            {"name": "treatment_b", "description": "绿色按钮 (#52c41a)"},
        ]
    },
    {
        "name": "新用户引导流程",
        "key": "onboarding_flow_v3",
        "hypothesis": "简化新用户引导流程（从5步减少到3步），可以提升新用户次日留存",
        "primary_metric": "day1_retention",
        "secondary_metrics": ["onboarding_completion_rate", "first_purchase_rate"],
        "variants": [
            {"name": "control", "description": "5步完整引导"},
            {"name": "treatment_a", "description": "3步简化引导"},
        ]
    },
    {
        "name": "商品详情页布局优化",
        "key": "pdp_layout_test",
        "hypothesis": "将商品评价模块上移至价格下方，可以提升购买转化率",
        "primary_metric": "purchase_conversion_rate",
        "secondary_metrics": ["pdp_bounce_rate", "time_on_page"],
        "variants": [
            {"name": "control", "description": "评价模块在页面底部"},
            {"name": "treatment_a", "description": "评价模块在价格下方"},
        ]
    },
    {
        "name": "优惠券展示方式测试",
        "key": "coupon_display_style",
        "hypothesis": "使用弹窗展示优惠券比页面内嵌入展示更能提升领取率",
        "primary_metric": "coupon_claim_rate",
        "secondary_metrics": ["coupon_usage_rate", "order_value"],
        "variants": [
            {"name": "control", "description": "页面内嵌入式展示"},
            {"name": "treatment_a", "description": "弹窗式展示"},
            {"name": "treatment_b", "description": "浮动气泡展示"},
        ]
    },
    {
        "name": "搜索结果排序算法",
        "key": "search_ranking_v2",
        "hypothesis": "引入个性化排序因子可以提升搜索结果的点击率和转化率",
        "primary_metric": "search_result_ctr",
        "secondary_metrics": ["search_conversion_rate", "search_exit_rate"],
        "variants": [
            {"name": "control", "description": "基于销量和相关性排序"},
            {"name": "treatment_a", "description": "加入用户行为个性化因子"},
        ]
    },
    {
        "name": "支付流程简化",
        "key": "checkout_simplify",
        "hypothesis": "将支付流程从3页合并为1页，可以降低支付放弃率",
        "primary_metric": "checkout_completion_rate",
        "secondary_metrics": ["checkout_time", "payment_failure_rate"],
        "variants": [
            {"name": "control", "description": "3页分步支付流程"},
            {"name": "treatment_a", "description": "1页合并支付流程"},
        ]
    },
    {
        "name": "Push通知发送时间优化",
        "key": "push_timing_test",
        "hypothesis": "根据用户活跃时段发送Push可以提升打开率",
        "primary_metric": "push_open_rate",
        "secondary_metrics": ["push_click_rate", "unsubscribe_rate"],
        "variants": [
            {"name": "control", "description": "统一时间发送（10:00）"},
            {"name": "treatment_a", "description": "基于用户活跃时段发送"},
            {"name": "treatment_b", "description": "基于历史最佳时段发送"},
        ]
    },
    {
        "name": "会员等级权益展示",
        "key": "membership_benefits_display",
        "hypothesis": "突出展示会员专属权益可以提升会员开通率",
        "primary_metric": "membership_conversion_rate",
        "secondary_metrics": ["membership_page_pv", "benefit_click_rate"],
        "variants": [
            {"name": "control", "description": "列表式权益展示"},
            {"name": "treatment_a", "description": "卡片式突出展示"},
        ]
    },
    {
        "name": "商品图片展示数量",
        "key": "product_image_count",
        "hypothesis": "增加商品图片展示数量可以提升用户信任度和购买意愿",
        "primary_metric": "add_to_cart_rate",
        "secondary_metrics": ["image_swipe_count", "pdp_time"],
        "variants": [
            {"name": "control", "description": "展示3张主图"},
            {"name": "treatment_a", "description": "展示5张主图"},
            {"name": "treatment_b", "description": "展示5张主图+视频"},
        ]
    },
    {
        "name": "购物车凑单提示",
        "key": "cart_upsell_prompt",
        "hypothesis": "在购物车页面展示凑单提示可以提升客单价",
        "primary_metric": "average_order_value",
        "secondary_metrics": ["items_per_order", "coupon_usage_rate"],
        "variants": [
            {"name": "control", "description": "无凑单提示"},
            {"name": "treatment_a", "description": "满减凑单提示"},
            {"name": "treatment_b", "description": "满减+推荐商品凑单"},
        ]
    },
    {
        "name": "订单确认页推荐",
        "key": "order_confirmation_recommend",
        "hypothesis": "在订单确认页展示相关商品推荐可以提升复购率",
        "primary_metric": "next_7day_repurchase_rate",
        "secondary_metrics": ["recommend_click_rate", "recommend_conversion"],
        "variants": [
            {"name": "control", "description": "无推荐"},
            {"name": "treatment_a", "description": "基于本单商品推荐"},
        ]
    },
]


def random_date(start, end):
    """生成随机日期"""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))  # nosec B311
    return start + timedelta(seconds=random_seconds)


def generate_experiment_domain(config, fake, users):
    """
    生成实验域数据

    Args:
        config: 配置字典，包含数据规模参数
        fake: Faker 实例
        users: 用户列表

    Returns:
        tuple: (ab_tests, variants, assignments)
    """
    ab_tests = []
    variants = []
    assignments = []

    # 时间范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=config.get("date_range_days", 365))

    # 实验数量（使用配置或默认值）
    num_tests = config.get("num_ab_tests", len(AB_TEST_TEMPLATES))

    # 生成 A/B 测试
    variant_id_counter = 0
    assignment_id_counter = 0
    for i, template in enumerate(AB_TEST_TEMPLATES[:num_tests]):
        test_id = i + 1

        # 测试创建时间
        created_at = random_date(start_date, end_date - timedelta(days=30))

        # 测试状态和时间
        status = random.choices(  # nosec B311
            ["draft", "running", "paused", "completed"],
            weights=[5, 40, 10, 45]
        )[0]

        started_at = None
        ended_at = None

        if status in ["running", "paused", "completed"]:
            started_at = created_at + timedelta(days=random.randint(1, 7))  # nosec B311

        if status == "completed":
            ended_at = started_at + timedelta(days=random.randint(7, 60))  # nosec B311
        elif status == "paused":
            ended_at = started_at + timedelta(days=random.randint(3, 30))  # nosec B311

        # 流量分配
        traffic_percentage = random.choice([10, 20, 30, 50, 100])  # nosec B311

        # 最小样本量
        min_sample_size = random.choice([1000, 5000, 10000, 20000])  # nosec B311

        ab_test = {
            "test_id": test_id,
            "test_name": template["name"],
            "test_key": template["key"],
            "hypothesis": template["hypothesis"],
            "primary_metric": template["primary_metric"],
            "secondary_metrics": template["secondary_metrics"],
            "status": status,
            "traffic_percentage": traffic_percentage,
            "min_sample_size": min_sample_size,
            "owner": fake.name(),
            "created_at": created_at.isoformat(),
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "updated_at": (ended_at or started_at or created_at).isoformat(),
        }
        ab_tests.append(ab_test)

        # 生成变体
        num_variants = len(template["variants"])
        base_weight = 100 // num_variants
        remainder = 100 % num_variants

        for j, variant_template in enumerate(template["variants"]):
            variant_id_counter += 1
            variant_id = variant_id_counter

            # 分配权重
            weight = base_weight + (1 if j < remainder else 0)

            variant = {
                "variant_id": variant_id,
                "test_id": test_id,
                "variant_name": variant_template["name"],
                "variant_key": f"{template['key']}_{variant_template['name']}",
                "description": variant_template["description"],
                "is_control": variant_template["name"] == "control",
                "weight": weight,
                "created_at": created_at.isoformat(),
            }
            variants.append(variant)

        # 生成用户分组（只对运行中和已完成的测试）
        if status in ["running", "completed"]:
            # 根据流量比例选择参与用户
            eligible_users = random.sample(  # nosec B311
                users,
                min(len(users), int(len(users) * traffic_percentage / 100))
            )

            # 获取该测试的变体
            test_variants = [v for v in variants if v["test_id"] == test_id]
            variant_weights = [v["weight"] for v in test_variants]

            for user in eligible_users:
                # 按权重分配变体
                assigned_variant = random.choices(test_variants, weights=variant_weights)[0]  # nosec B311

                # 分组时间（在测试开始后）
                assigned_at = random_date(
                    started_at,
                    ended_at if ended_at else end_date
                )

                # 是否转化（模拟实验效果）
                base_conversion = 0.05  # 基础转化率 5%
                if assigned_variant["variant_name"] == "control":
                    converted = random.random() < base_conversion  # nosec B311
                elif assigned_variant["variant_name"] == "treatment_a":
                    converted = random.random() < base_conversion * 1.15  # +15%  # nosec B311
                else:
                    converted = random.random() < base_conversion * 1.08  # +8%  # nosec B311

                assignment_id_counter += 1
                assignment = {
                    "assignment_id": assignment_id_counter,
                    "test_id": test_id,
                    "user_id": user["user_id"],
                    "variant_id": assigned_variant["variant_id"],
                    "variant_name": assigned_variant["variant_name"],
                    "assigned_at": assigned_at.isoformat(),
                    "first_exposure_at": assigned_at.isoformat(),
                    "converted": converted,
                    "conversion_at": (assigned_at + timedelta(
                        hours=random.randint(1, 72)  # nosec B311
                    )).isoformat() if converted else None,
                }
                assignments.append(assignment)

    print(f"  - A/B 测试: {len(ab_tests)} 个")
    print(f"  - 实验变体: {len(variants)} 个")
    print(f"  - 用户分组: {len(assignments)} 条")

    return ab_tests, variants, assignments
