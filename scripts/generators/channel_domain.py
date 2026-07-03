"""渠道归因域数据生成器"""
import random
from datetime import datetime, timedelta

CHANNELS = [
    {"name": "抖音信息流", "type": "paid", "platform": "douyin"},
    {"name": "抖音搜索", "type": "paid", "platform": "douyin"},
    {"name": "小红书种草", "type": "kol", "platform": "xiaohongshu"},
    {"name": "小红书搜索", "type": "paid", "platform": "xiaohongshu"},
    {"name": "微信朋友圈广告", "type": "paid", "platform": "weixin"},
    {"name": "微信公众号", "type": "organic", "platform": "weixin"},
    {"name": "百度搜索", "type": "paid", "platform": "baidu"},
    {"name": "快手信息流", "type": "paid", "platform": "kuaishou"},
    {"name": "微博KOL", "type": "kol", "platform": "weibo"},
    {"name": "App Store", "type": "organic", "platform": "apple"},
    {"name": "应用宝", "type": "organic", "platform": "tencent"},
    {"name": "老用户邀请", "type": "referral", "platform": "app"},
    {"name": "直接访问", "type": "direct", "platform": "direct"},
    {"name": "B站UP主", "type": "kol", "platform": "bilibili"},
]

CAMPAIGN_TYPES = ["awareness", "acquisition", "retargeting"]
OBJECTIVES = ["installs", "purchases", "engagement"]
CREATIVE_TYPES = ["image", "video", "carousel"]
CTAS = ["立即下载", "查看详情", "限时优惠", "马上抢购"]


def generate_channel_domain(config, fake, users):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=config.get("date_range_days", 365))

    # 1. 渠道
    channels = []
    for i, ch in enumerate(CHANNELS, 1):
        channels.append({
            "channel_id": i,
            "channel_name": ch["name"],
            "channel_type": ch["type"],
            "platform": ch["platform"],
            "is_active": True,
            "created_at": start_date.isoformat(),
        })

    # 2. 广告活动
    ad_campaigns = []
    camp_id = 1
    for _ in range(config.get("num_campaigns", 100)):
        channel = random.choice([c for c in channels if c["channel_type"] in ["paid", "kol"]])  # nosec B311
        camp_start = start_date + timedelta(days=random.randint(0, 300))  # nosec B311
        ad_campaigns.append({
            "ad_campaign_id": camp_id,
            "channel_id": channel["channel_id"],
            "campaign_name": f"{channel['channel_name']}-活动{camp_id}",
            "campaign_type": random.choice(CAMPAIGN_TYPES),  # nosec B311
            "objective": random.choice(OBJECTIVES),  # nosec B311
            "budget_total": round(random.uniform(10000, 500000), 2),  # nosec B311
            "budget_daily": round(random.uniform(500, 10000), 2),  # nosec B311
            "start_date": camp_start.date().isoformat(),
            "end_date": (camp_start + timedelta(days=random.randint(7, 60))).date().isoformat(),  # nosec B311
            "status": random.choice(["active", "paused", "ended"]),  # nosec B311
            "created_at": camp_start.isoformat(),
        })
        camp_id += 1

    # 3. 广告素材
    ad_creatives = []
    creative_id = 1
    for camp in ad_campaigns:
        for _ in range(random.randint(2, 4)):  # nosec B311
            ad_creatives.append({
                "creative_id": creative_id,
                "ad_campaign_id": camp["ad_campaign_id"],
                "creative_name": f"素材{creative_id}",
                "creative_type": random.choice(CREATIVE_TYPES),  # nosec B311
                "call_to_action": random.choice(CTAS),  # nosec B311
                "status": "active",
            })
            creative_id += 1

    # 4. 用户归因
    attributions = []
    for i, user in enumerate(random.sample(users, int(len(users) * 0.7)), 1):  # nosec B311
        channel = random.choice(channels)  # nosec B311
        reg_time = datetime.fromisoformat(user["registered_at"]) if isinstance(user["registered_at"], str) else user["registered_at"]
        attributions.append({
            "attribution_id": i,
            "user_id": user["user_id"],
            "channel_id": channel["channel_id"],
            "attribution_type": random.choice(["first_touch", "last_touch"]),  # nosec B311
            "click_time": (reg_time - timedelta(hours=random.randint(1, 48))).isoformat(),  # nosec B311
            "install_time": reg_time.isoformat(),
        })

    # 5. 渠道每日成本
    channel_costs = []
    cost_id = 1
    for camp in ad_campaigns[:50]:  # 限制数量
        camp_start = datetime.fromisoformat(camp["start_date"])
        for day_offset in range(min(30, random.randint(7, 30))):  # nosec B311
            current = camp_start + timedelta(days=day_offset)
            impressions = random.randint(10000, 200000)  # nosec B311
            clicks = int(impressions * random.uniform(0.01, 0.04))  # nosec B311
            channel_costs.append({
                "id": cost_id,
                "channel_id": camp["channel_id"],
                "ad_campaign_id": camp["ad_campaign_id"],
                "date": current.date().isoformat(),
                "impressions": impressions,
                "clicks": clicks,
                "installs": int(clicks * random.uniform(0.05, 0.15)),  # nosec B311
                "cost": round(random.uniform(200, 3000), 2),  # nosec B311
                "currency": "CNY",
            })
            cost_id += 1

    return channels, ad_campaigns, ad_creatives, attributions, channel_costs
