"""行为域数据生成器"""
import random
from datetime import datetime, timedelta

# 事件定义
EVENT_DEFINITIONS = [
    {"name": "app_open", "category": "engagement", "is_core": True},
    {"name": "app_close", "category": "engagement", "is_core": True},
    {"name": "view_home", "category": "engagement", "is_core": True},
    {"name": "view_product", "category": "commerce", "is_core": True},
    {"name": "view_category", "category": "commerce", "is_core": False},
    {"name": "search", "category": "engagement", "is_core": True},
    {"name": "add_to_cart", "category": "commerce", "is_core": True},
    {"name": "remove_from_cart", "category": "commerce", "is_core": False},
    {"name": "begin_checkout", "category": "commerce", "is_core": True},
    {"name": "purchase", "category": "commerce", "is_core": True},
    {"name": "view_post", "category": "social", "is_core": True},
    {"name": "like_post", "category": "social", "is_core": False},
    {"name": "comment_post", "category": "social", "is_core": False},
    {"name": "share", "category": "social", "is_core": False},
    {"name": "follow_user", "category": "social", "is_core": False},
    {"name": "register", "category": "system", "is_core": True},
    {"name": "login", "category": "system", "is_core": True},
    {"name": "logout", "category": "system", "is_core": False},
    {"name": "view_profile", "category": "engagement", "is_core": False},
    {"name": "edit_profile", "category": "engagement", "is_core": False},
    {"name": "receive_push", "category": "system", "is_core": False},
    {"name": "click_push", "category": "engagement", "is_core": False},
    {"name": "click_banner", "category": "engagement", "is_core": False},
    {"name": "use_coupon", "category": "commerce", "is_core": False},
    {"name": "add_favorite", "category": "commerce", "is_core": False},
]

PAGES = [
    "home", "search", "category", "product_detail", "cart", "checkout",
    "order_list", "order_detail", "profile", "settings", "post_feed",
    "post_detail", "messages", "favorites", "coupon_center"
]

UTM_SOURCES = ["douyin", "weixin", "xiaohongshu", "baidu", "direct", "organic"]
UTM_MEDIUMS = ["paid", "organic", "referral", "push", "banner"]
UTM_CAMPAIGNS = ["spring_sale", "new_user", "recall", "double11", "618", "brand_day", None]
TRAFFIC_SOURCES = ["organic", "paid", "referral", "direct", "social", "email"]


def generate_behavior_domain(config, fake, users, products):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=config.get("date_range_days", 365))

    # 1. 事件定义
    event_defs = []
    for evt in EVENT_DEFINITIONS:
        event_defs.append({
            "event_name": evt["name"],
            "event_category": evt["category"],
            "description": f"{evt['name']} 事件",
            "is_core_event": evt["is_core"],
            "created_at": start_date.isoformat(),
        })

    # 2. 会话
    sessions = []
    num_sessions = config.get("num_sessions", 100000)
    
    for i in range(1, num_sessions + 1):
        user = random.choice(users)  # nosec B311
        session_start = start_date + timedelta(
            seconds=random.randint(0, int((end_date - start_date).total_seconds()))  # nosec B311
        )
        duration = random.randint(30, 1800)  # 30秒到30分钟  # nosec B311
        
        sessions.append({
            "session_id": i,
            "user_id": user["user_id"],
            "device_id": f"device_{user['user_id']}_{random.randint(1,3)}",  # nosec B311
            "start_time": session_start.isoformat(),
            "end_time": (session_start + timedelta(seconds=duration)).isoformat(),
            "duration_seconds": duration,
            "event_count": random.randint(3, 50),  # nosec B311
            "page_view_count": random.randint(2, 20),  # nosec B311
            "is_bounce": duration < 60,
            "entry_page": random.choice(["home", "product_detail", "post_feed"]),  # nosec B311
            "exit_page": random.choice(PAGES),  # nosec B311
            "traffic_source": random.choice(TRAFFIC_SOURCES),  # nosec B311
            "utm_source": random.choice(UTM_SOURCES),  # nosec B311
            "utm_medium": random.choice(UTM_MEDIUMS),  # nosec B311
            "utm_campaign": random.choice(UTM_CAMPAIGNS),  # nosec B311
        })

    # 3. 页面浏览
    page_views = []
    pv_id = 1
    for session in sessions[:50000]:  # 限制数量
        num_pvs = random.randint(2, 10)  # nosec B311
        session_start = datetime.fromisoformat(session["start_time"])
        
        for j in range(num_pvs):
            view_time = session_start + timedelta(seconds=j * random.randint(10, 60))  # nosec B311
            page_views.append({
                "page_view_id": pv_id,
                "user_id": session["user_id"],
                "session_id": session["session_id"],
                "page_name": random.choice(PAGES),  # nosec B311
                "duration_seconds": random.randint(5, 120),  # nosec B311
                "scroll_depth_pct": random.randint(20, 100),  # nosec B311
                "view_time": view_time.isoformat(),
            })
            pv_id += 1

    # 4. 事件流 (核心大表)
    events = []
    event_id = 1
    num_events = config.get("num_events", 500000)
    
    # 基于会话生成事件
    events_per_session = num_events // len(sessions)
    
    for session in sessions:
        session_start = datetime.fromisoformat(session["start_time"])
        num_evts = random.randint(3, min(events_per_session * 2, 30))  # nosec B311
        
        for j in range(num_evts):
            evt_time = session_start + timedelta(seconds=j * random.randint(5, 30))  # nosec B311
            evt_name = random.choice(EVENT_DEFINITIONS)["name"]  # nosec B311
            
            props = {}
            if evt_name == "view_product" and products:
                prod = random.choice(products)  # nosec B311
                props = {"product_id": prod["product_id"], "product_name": prod["product_name"]}
            elif evt_name == "search":
                props = {"keyword": random.choice(["连衣裙", "手机", "护肤品", "零食", "运动鞋"])}  # nosec B311
            elif evt_name == "add_to_cart" and products:
                prod = random.choice(products)  # nosec B311
                props = {"product_id": prod["product_id"], "quantity": random.randint(1, 3)}  # nosec B311
            elif evt_name == "purchase":
                props = {"order_id": random.randint(1, 50000), "amount": round(random.uniform(50, 2000), 2)}  # nosec B311
            
            events.append({
                "event_id": event_id,
                "user_id": session["user_id"],
                "device_id": session["device_id"],
                "session_id": session["session_id"],
                "event_name": evt_name,
                "event_time": evt_time.isoformat(),
                "properties": props,
                "page_name": random.choice(PAGES),  # nosec B311
            })
            event_id += 1
            
            if event_id > num_events:
                break
        if event_id > num_events:
            break

    return event_defs, events, sessions, page_views
