"""
社交互动域数据生成器

生成表:
- user_follows: 用户关注关系
- posts: UGC 内容 (文章、短视频、图片、评测)
- post_likes: 点赞
- post_comments: 评论
- post_shares: 分享
- user_messages: 私信
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


def generate_social_domain(config, fake, users, products):
    """
    生成社交互动域数据

    Args:
        config: 配置字典，包含 num_posts 等参数
        fake: Faker 实例 (zh_CN)
        users: 用户列表
        products: 商品列表

    Returns:
        tuple: (follows, posts, likes, comments, shares, messages)
    """
    num_posts = config.get("num_posts", 20000)
    num_users = len(users)

    # 提取 user_id 列表
    user_ids = [u["user_id"] for u in users]
    product_ids = [p["product_id"] for p in products] if products else []

    # 1. 生成关注关系
    follows = generate_follows(user_ids, fake)
    print(f"  - 关注关系: {len(follows)} 条")

    # 2. 生成 UGC 内容
    posts = generate_posts(num_posts, user_ids, product_ids, fake)
    print(f"  - UGC 内容: {len(posts)} 条")

    # 提取 post_id 列表和发布时间映射
    post_ids = [p["post_id"] for p in posts]
    post_times = {p["post_id"]: datetime.fromisoformat(p["created_at"]) for p in posts}

    # 3. 生成点赞
    likes = generate_likes(post_ids, post_times, user_ids, fake)
    print(f"  - 点赞记录: {len(likes)} 条")

    # 4. 生成评论
    comments = generate_comments(post_ids, post_times, user_ids, fake)
    print(f"  - 评论记录: {len(comments)} 条")

    # 5. 生成分享
    shares = generate_shares(post_ids, post_times, user_ids, fake)
    print(f"  - 分享记录: {len(shares)} 条")

    # 6. 生成私信
    messages = generate_messages(user_ids, fake)
    print(f"  - 私信记录: {len(messages)} 条")

    return follows, posts, likes, comments, shares, messages


def generate_follows(user_ids, fake):
    """生成用户关注关系"""
    follows = []
    follow_set = set()  # 用于去重

    num_users = len(user_ids)
    # 平均每人关注 20-50 人
    num_follows = num_users * random.randint(20, 50)  # nosec B311

    for _ in range(num_follows):
        follower_id = random.choice(user_ids)  # nosec B311
        following_id = random.choice(user_ids)  # nosec B311

        # 不能关注自己，不能重复关注
        if follower_id != following_id and (follower_id, following_id) not in follow_set:
            follow_set.add((follower_id, following_id))
            follows.append({
                "follow_id": len(follows) + 1,
                "follower_id": follower_id,
                "following_id": following_id,
                "created_at": random_date().isoformat(),
                "status": random.choices(["active", "unfollowed"], weights=[0.9, 0.1])[0]  # nosec B311
            })

    return follows


def generate_posts(num_posts, user_ids, product_ids, fake):
    """生成 UGC 内容"""
    posts = []

    post_types = ["article", "short_video", "image", "review"]
    post_type_weights = [0.2, 0.35, 0.3, 0.15]  # 短视频和图片更多

    # 帖子标题/内容模板
    article_titles = [
        "分享一下我的{}使用心得",
        "{}开箱测评，真香！",
        "入手{}一个月，说说感受",
        "{}真的值得买吗？亲测告诉你",
        "新手必看！{}选购指南",
        "{}使用技巧分享",
        "为什么我推荐{}",
        "{}避坑指南，这些你要知道",
    ]

    video_titles = [
        "{}开箱视频",
        "一分钟了解{}",
        "{}使用教程",
        "{}真实评测",
        "{}对比测试",
        "带你看看我的{}",
    ]

    image_titles = [
        "晒晒我的{}",
        "{}到货啦",
        "{}实拍图",
        "今日OOTD feat. {}",
        "{}氛围感",
    ]

    review_titles = [
        "{}详细评测",
        "{}优缺点分析",
        "{}深度体验报告",
        "{}值不值得买",
    ]

    # 话题标签
    topics = [
        "#好物推荐", "#开箱", "#种草", "#测评", "#生活分享",
        "#日常vlog", "#购物分享", "#使用心得", "#真实评价", "#晒单",
        "#性价比之选", "#新品体验", "#好物分享", "#生活好物", "#必买清单"
    ]

    # 位置信息
    locations = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "苏州", "西安", None]

    for i in range(num_posts):
        post_type = random.choices(post_types, weights=post_type_weights)[0]  # nosec B311
        user_id = random.choice(user_ids)  # nosec B311
        created_at = random_date()

        # 根据帖子类型选择标题模板
        if post_type == "article":
            title_template = random.choice(article_titles)  # nosec B311
        elif post_type == "short_video":
            title_template = random.choice(video_titles)  # nosec B311
        elif post_type == "image":
            title_template = random.choice(image_titles)  # nosec B311
        else:  # review
            title_template = random.choice(review_titles)  # nosec B311

        # 关联商品（review 必须关联，其他类型概率关联）- 改为数组
        related_product_ids = []
        product_name = fake.word()

        if product_ids:
            if post_type == "review":
                related_product_ids = [random.choice(product_ids)]  # nosec B311
            elif random.random() < 0.4:  # 40% 概率关联商品  # nosec B311
                num_related = random.randint(1, 3)  # nosec B311
                related_product_ids = random.sample(product_ids, min(num_related, len(product_ids)))  # nosec B311

        # 生成内容
        title = title_template.format(product_name if not related_product_ids else f"商品{related_product_ids[0]}")

        # 生成正文
        content_sentences = [fake.sentence() for _ in range(random.randint(2, 8))]  # nosec B311
        content = "".join(content_sentences)

        # 添加话题标签
        post_tags = random.sample(topics, k=random.randint(1, 4))  # nosec B311

        # 生成 media_urls (数组)
        media_count = random.randint(1, 9) if post_type in ["image", "short_video"] else 0  # nosec B311
        media_urls = [f"https://cdn.example.com/posts/{i+1}/media_{j+1}.jpg" for j in range(media_count)]

        # published_at (已发布状态才有)
        status = random.choices(["published", "draft", "deleted", "under_review"],  # nosec B311
                                weights=[0.85, 0.05, 0.05, 0.05])[0]
        published_at = created_at if status == "published" else None

        post = {
            "post_id": i + 1,
            "user_id": user_id,
            "content_type": post_type,
            "title": title,
            "content": content,
            "media_urls": media_urls,
            "tags": post_tags,
            "product_ids": related_product_ids,
            "location": random.choice(locations),  # nosec B311
            "video_duration": random.randint(15, 300) if post_type == "short_video" else None,  # nosec B311
            "view_count": random.randint(0, 100000),  # nosec B311
            "like_count": 0,  # 后面计算
            "comment_count": 0,  # 后面计算
            "share_count": 0,  # 后面计算
            "is_featured": random.random() < 0.05,  # 5% 精选  # nosec B311
            "status": status,
            "published_at": published_at.isoformat() if published_at else None,
            "created_at": created_at.isoformat(),
            "updated_at": random_date_after(created_at, max_days=7).isoformat() if random.random() < 0.2 else created_at.isoformat()  # nosec B311
        }
        posts.append(post)

    return posts


def generate_likes(post_ids, post_times, user_ids, fake):
    """生成点赞数据"""
    likes = []
    like_set = set()  # 用于去重 (user_id, post_id)

    # 平均每个帖子 10-50 个赞
    num_likes = len(post_ids) * random.randint(10, 50)  # nosec B311

    for _ in range(num_likes):
        post_id = random.choice(post_ids)  # nosec B311
        user_id = random.choice(user_ids)  # nosec B311

        if (user_id, post_id) not in like_set:
            like_set.add((user_id, post_id))
            post_time = post_times.get(post_id, START_DATE)

            likes.append({
                "like_id": len(likes) + 1,
                "user_id": user_id,
                "post_id": post_id,
                "created_at": random_date_after(post_time, max_days=60).isoformat()
            })

    return likes


def generate_comments(post_ids, post_times, user_ids, fake):
    """生成评论数据"""
    comments = []

    # 评论模板
    positive_comments = [
        "太棒了，已收藏！",
        "感谢分享，很有帮助",
        "写得真好，学到了",
        "同款，确实好用",
        "已下单，期待效果",
        "求链接！",
        "这个必须支持",
        "终于等到你的分享了",
        "一直关注你，内容很棒",
        "收藏了，以后买",
    ]

    question_comments = [
        "请问在哪买的？",
        "多少钱入手的？",
        "用了多久了？",
        "有优惠券吗？",
        "和XX比怎么样？",
        "适合新手吗？",
        "有售后问题吗？",
    ]

    neutral_comments = [
        "看看效果",
        "路过",
        "了解一下",
        "先码后看",
        "有点心动",
    ]

    all_comment_templates = positive_comments + question_comments + neutral_comments

    # 平均每个帖子 5-20 条评论
    num_comments = len(post_ids) * random.randint(5, 20)  # nosec B311

    for i in range(num_comments):
        post_id = random.choice(post_ids)  # nosec B311
        user_id = random.choice(user_ids)  # nosec B311
        post_time = post_times.get(post_id, START_DATE)
        comment_time = random_date_after(post_time, max_days=60)

        # 20% 概率是回复其他评论
        parent_comment_id = None
        if comments and random.random() < 0.2:  # nosec B311
            # 找同一个帖子下的评论
            same_post_comments = [c for c in comments if c["post_id"] == post_id]
            if same_post_comments:
                parent_comment_id = random.choice(same_post_comments)["comment_id"]  # nosec B311

        # 生成评论内容
        if random.random() < 0.7:  # nosec B311
            content = random.choice(all_comment_templates)  # nosec B311
        else:
            content = fake.sentence()

        comments.append({
            "comment_id": i + 1,
            "post_id": post_id,
            "user_id": user_id,
            "parent_comment_id": parent_comment_id,
            "content": content,
            "like_count": random.randint(0, 100),  # nosec B311
            "status": random.choices(["visible", "deleted", "hidden"], weights=[0.95, 0.03, 0.02])[0],  # nosec B311
            "created_at": comment_time.isoformat()
        })

    return comments


def generate_shares(post_ids, post_times, user_ids, fake):
    """生成分享数据"""
    shares = []

    share_platforms = [
        ("wechat_friend", 0.35),      # 微信好友
        ("wechat_moments", 0.25),     # 朋友圈
        ("weibo", 0.15),              # 微博
        ("qq", 0.10),                 # QQ
        ("copy_link", 0.10),          # 复制链接
        ("other", 0.05),              # 其他
    ]
    platforms = [p[0] for p in share_platforms]
    platform_weights = [p[1] for p in share_platforms]

    # 平均每个帖子 2-10 次分享
    num_shares = len(post_ids) * random.randint(2, 10)  # nosec B311

    for i in range(num_shares):
        post_id = random.choice(post_ids)  # nosec B311
        user_id = random.choice(user_ids)  # nosec B311
        post_time = post_times.get(post_id, START_DATE)

        shares.append({
            "share_id": i + 1,
            "user_id": user_id,
            "post_id": post_id,
            "platform": random.choices(platforms, weights=platform_weights)[0],  # nosec B311
            "created_at": random_date_after(post_time, max_days=60).isoformat()
        })

    return shares


def generate_messages(user_ids, fake):
    """生成私信数据"""
    messages = []

    # 消息模板
    message_templates = [
        "你好，想问一下{}",
        "请问方便加个微信吗？",
        "你的分享太棒了！",
        "能分享一下链接吗？",
        "谢谢你的回复",
        "好的，收到",
        "{}",  # 使用 faker 生成
        "有时间聊聊吗？",
        "关注你很久了",
        "想请教一下",
    ]

    # 生成 5000-10000 条私信
    num_messages = random.randint(5000, 10000)  # nosec B311

    # 生成一些会话（两个用户之间的对话）
    num_conversations = num_messages // 5  # 平均每个会话 5 条消息

    conversation_pairs = []
    for _ in range(num_conversations):
        user_a = random.choice(user_ids)  # nosec B311
        user_b = random.choice(user_ids)  # nosec B311
        if user_a != user_b:
            conversation_pairs.append((user_a, user_b))

    message_id = 0
    for user_a, user_b in conversation_pairs:
        # 每个会话 1-10 条消息
        num_msgs = random.randint(1, 10)  # nosec B311
        conversation_start = random_date()

        for j in range(num_msgs):
            # 交替发送
            if j % 2 == 0:
                sender_id, receiver_id = user_a, user_b
            else:
                sender_id, receiver_id = user_b, user_a

            send_time = conversation_start + timedelta(minutes=j * random.randint(1, 60))  # nosec B311
            if send_time > END_DATE:
                break

            # 生成消息内容
            template = random.choice(message_templates)  # nosec B311
            if "{}" in template:
                content = template.format(fake.sentence()[:20])
            else:
                content = template

            message_id += 1
            messages.append({
                "message_id": message_id,
                "sender_id": sender_id,
                "receiver_id": receiver_id,
                "content": content,
                "message_type": random.choices(["text", "image", "link"], weights=[0.85, 0.10, 0.05])[0],  # nosec B311
                "is_read": random.random() < 0.8,  # nosec B311
                "created_at": send_time.isoformat()
            })

    return messages
