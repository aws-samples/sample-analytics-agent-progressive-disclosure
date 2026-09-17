"""21 张事实表的向量化构建函数。

## 约定

每个 `build_xxx(ctx, offset, n)` 返回 `dict[列名 -> ndarray]`，只负责 [offset, offset+n)
这一段行。分块的意义有两层：把峰值内存与表大小解耦，以及产出多个 Parquet 分片让
Redshift COPY 能并行加载（单文件 COPY 是单线程的）。

引用完整性靠「父表 id 是连续 int64 区间」这一条撑住：子表引用父表只需要 (start, n)，
不必在内存里持有父表数组。这也是 genlib 用 `id_range` 而非随机 id 的原因。

## 唯一对表的例外

`post_likes` / `user_follows` / `user_segment_members` / `ab_test_assignments` 的主键是
复合唯一对，没法分块生成（跨块会撞）。这些表在 ctx 里一次性预生成全局 int64 对数组
（15M 对 × 2 × 8B ≈ 240MB，内存可接受），builder 只做切片。

## 业务信号的落点

- `fk_skewed` → 用户活跃度、商品热度、帖子热度的长尾（帕累托题的素材）
- `ts_window` → 周内波动 + 月度趋势 + 小时三峰（趋势/异常/周环比题的素材）
- `orders.status` 权重 → paid+shipped+delivered 恰好 75%，复现 `dwd_orders_valid` 的行数比
- `user_attributions` 只覆盖约 70% 用户 → mart LEFT JOIN 后形成「约六成 GMV 未归因」
  这个知识库明确写了的治理发现。**这个缺口是刻意的，别"修好"它。**
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

import numpy as np

import fillers as F
import profiles as PROF
import semantics as SEM

# ---------------------------------------------------------------- 文本池

CITIES = [
    ("北京", "北京", 12), ("上海", "上海", 12), ("广州", "广东", 8), ("深圳", "广东", 9),
    ("杭州", "浙江", 6), ("成都", "四川", 6), ("南京", "江苏", 5), ("武汉", "湖北", 5),
    ("西安", "陕西", 4), ("重庆", "重庆", 4), ("苏州", "江苏", 4), ("天津", "天津", 3),
    ("长沙", "湖南", 3), ("郑州", "河南", 3), ("青岛", "山东", 3), ("合肥", "安徽", 2),
    ("福州", "福建", 2), ("昆明", "云南", 2), ("沈阳", "辽宁", 2), ("哈尔滨", "黑龙江", 1),
]
# 页面名与流量来源都对齐 knowledge/domains/behavior/ 下的卡片实测取值。
# 旧清单是照 database/*.sql 的行内注释写的，而那批注释后来被卡片按真实数据校准过：
# order_confirm / my_orders / feed / activity / login 在数据里一个都不存在，
# kol 属于 channels.channel_type 而不是 sessions.traffic_source（卡片明文点了这几条）。
# 两套值都是 15 / 6 个。TRAFFIC 实测均匀（811–868）就用均匀池；PAGES **不能**均匀——
# 见下面 PAGE_VIEW_WEIGHTS。
PAGES = ["home", "category", "search", "product_detail", "cart", "checkout",
         "order_list", "order_detail", "profile", "post_feed", "post_detail",
         "coupon_center", "favorites", "messages", "settings"]
# page_views 的页面权重，**逐位对齐 PAGES**（改一个必须同时改另一个，长度断言在
# selftest_closures.py）。均匀摊在 15 个页面上会让「热门页面榜」变成噪声排名，
# 也让漏斗形状消失：判据 `pv_funnel` 要求 home 至少是 checkout 的 3 倍。
# 形状按真实 APP 的逐级收窄取：入口页 → 列表/搜索 → 详情 → 购物车 → 结算，
# 加上几个与主漏斗并行的低频页（社区、券中心、设置）。
PAGE_VIEW_WEIGHTS = [1000, 520, 430, 610, 260, 120,
                     180, 150, 140, 210, 165,
                     95, 70, 80, 45]
TRAFFIC = ["referral", "social", "paid", "organic", "direct", "email"]
# page_views.referrer 的来源池。原来只有两个值 ["", "https://m.example.com/home"]，
# 于是「非空的 referrer 只有 1 种取值」——按来源页做站内路径分析恒得一行
# （判据 `col_domain[page_views.referrer]` 要求非空率 ≥ 50% 且不同取值 ≥ 2）。
# 三类来源都要有，缺哪一类都会让某一整类分析没有输入：直接打开（空串）、
# 站内跳转（本站页面）、站外引流（搜索引擎 / 社交平台 / 推送）。
REFERRERS = ["", "https://m.example.com/home", "https://m.example.com/category",
             "https://m.example.com/search", "https://m.example.com/post/feed",
             "https://www.baidu.com/s", "https://m.weibo.cn/", "https://www.google.com/",
             "https://mp.weixin.qq.com/s", "app://push"]
REFERRER_W = [280, 150, 110, 95, 70, 120, 60, 45, 50, 20]

# 社交图两端各自的集中度。`F.unique_pairs` 的 sigma 作用在**第一个** id 上，
# sigma_b 作用在第二个上（见那个函数的 docstring）。
#
# post_likes(user_id, post_id)：内容热度的长尾远比用户勤奋度的长尾陡，所以帖子那侧
# 用 1.6、用户那侧留默认 1.15。原来帖子那侧是 `fk_uniform`——「每个帖子拿到的赞几乎
# 一样多」，热门内容榜排不出东西，零赞帖也只有 0.8%（真实社区是两成上下）。
SIGMA_POST_VIRALITY = 1.6
# user_follows(follower_id, following_id)：粉丝数（入度）重尾、关注数（出度）偏平。
# 原来偏斜给在了 follower 那一侧，正好反了（判据 `follow_gini_gap` 的 why 里点了这条）。
SIGMA_FOLLOW_OUT = 0.55
SIGMA_FOLLOW_IN = F.SIGMA_DEFAULT
# 互关比例。独立抽两端时互关率就是随机边密度（n/nu² 量级，本规模约 1.5%），
# 而真实社交图的互关是**结构性**的（判据 `follow_recip` 要求 ≥ 3× 随机基线）。
FOLLOW_RECIP = 0.30
# 职业池搬到 scripts/gen/profiles.yaml（D-01）。这里原来是一份写死的 12 值列表配
# 等概率抽样，而「12 档人数极差仅 3.3%」正是 D-01 的旁证缺陷。搬走而不是原地加权重，
# 是因为检查侧（verify_correlation.py）要按同一份取值域分档，两份清单会静默漂移——
# 本仓库已经有一次实证：`scripts/generators/user_domain.py` 那份是 15 个值，
# 与这份只交集 9 个，而 DDL 和 knowledge 卡片都没声明这一列的枚举，没人抓得到。
# 取值对齐 knowledge/domains/user/user_profiles.md 的「### interests 兴趣标签」，
# 那张表又是按线上库实测写的。原来这里是 12 个两字短标签（数码 / 美妆 / 旅行 …），
# 与库里 15 个四字标签（数码科技 / 美妆护肤 / 旅游出行 …）**交集只有 宠物、汽车**——
# 而这一列写在卡片的围栏代码块里，VE.parse_card() 不认围栏块，所以两边差了 13 个值
# 却没有任何一层报过。见 selftest_closures.check_enums_vs_cards 上方 2026-08-28 的注释。
INTEREST_TAGS = ["运动健身", "摄影", "汽车", "美食烹饪", "读书学习", "美妆护肤",
                 "旅游出行", "音乐影视", "时尚穿搭", "金融理财", "数码科技",
                 "家居生活", "宠物", "游戏电竞", "母婴育儿"]

# ---------------------------------------------------------------- D-04 真实字面值词池
#
# 替掉 user_N / model-N / camp_N / 内容正文 N 这一类占位符。占位符不改变任何数值
# 结论，但凡是要展示明细行的场景（演示截图、卡片示例、结果表格）一眼可见是假数据。
#
# 一律用**组合式**词池（F.combine），不用单池：单池的基数就是列的基数，
# `GROUP BY device_model` 出 40 行、`GROUP BY utm_campaign` 出 50 行照样露馅。
# 判据与回归断言在 scripts/gen/selftest_closures.py 的 check_literals()。

# 用户名：中文昵称，前缀 × 主体 × 可选数字尾。1024 种组合再乘数字尾。
# DDL 是 VARCHAR(50)，Redshift 按**字节**算长度，中文 3 字节：最长组合约 21 字节，够。
NICK_A = ["小", "大", "阿", "一只", "是", "超级", "甜甜的", "会飞的", "不加冰的", "慢慢的",
          "爱睡觉的", "三分甜", "半糖", "元气", "佛系", "打工人", "隔壁", "楼上", "深夜",
          "清晨", "咸鱼", "躺平", "干饭", "摸鱼", "柠檬味", "西瓜味", "奶油", "星期五",
          "下雨天", "一颗", "两只", "加班中"]
NICK_B = ["鱼干", "柠檬", "柴犬", "布丁", "芒果", "山楂", "汽水", "云朵", "橘子", "面包",
          "可乐", "栗子", "青提", "荔枝", "咖啡", "糯米", "团子", "土豆", "西蓝花", "小狗",
          "橙子", "杏仁", "饼干", "牛奶", "豆浆", "泡芙", "乌龙", "抹茶", "桂花", "雪梨",
          "海苔", "年糕"]
NICK_TAIL = ["", "", "", "", "0", "1", "7", "23", "66", "77", "88", "99", "233", "520",
             "1024", "1988", "1995", "2001", "2023", "_", "_x", "酱", "呀", "吖"]

# 邮箱：拼音姓 + 拼音名 + 数字 @ 真实服务商。username 是中文，进不了邮箱本地部分，
# 所以这里不再由 username 派生（旧版 email = username + "@example.com"）。
PY_FAMILY = ["wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou",
             "xu", "sun", "ma", "zhu", "hu", "guo", "lin", "he", "gao", "luo"]
PY_GIVEN = ["wei", "fang", "min", "jing", "tao", "lei", "yan", "hui", "na", "bo",
            "xin", "yu", "chao", "qi", "ning", "rui", "ke", "xuan", "meng", "yi"]
MAIL_TAIL = ["", "88", "99", "123", "521", "666", "1988", "1993", "1996", "2000",
             "_88", "_cn", ".xy", "0517", "0918"]
# 权重贴国内实际份额；域名基数 10 > 1，才判得出「不是同一个域」
MAIL_DOMAIN = ["qq.com", "163.com", "gmail.com", "126.com", "foxmail.com",
               "outlook.com", "sina.com", "hotmail.com", "aliyun.com", "yeah.net"]
MAIL_DOMAIN_W = [30, 22, 11, 10, 8, 6, 5, 4, 2, 2]

# 手机号段：工信部实际号段（移动 20 / 联通 11 / 电信 9），全部满足 1[3-9] 开头。
# 旧版是 "103" + 序号，11 位里第二位是 0，任何号码校验都过不了。
PHONE_SEGMENTS = ["134", "135", "136", "137", "138", "139", "147", "150", "151", "152",
                  "157", "158", "159", "178", "182", "183", "184", "187", "188", "198",
                  "130", "131", "132", "145", "155", "156", "166", "175", "176", "185",
                  "186", "133", "149", "153", "173", "177", "180", "181", "189", "199"]

# 设备型号：按 device_type 分支，与 device_brand / os_version 同源。
# v1 的 scripts/generators/user_domain.py 本来是配对的（ios→Apple→iPhone 系列），
# v2 向量化重写时丢了：型号退化成 model-0..39，品牌另抽一次。只换型号池会造出
# 「device_brand=Apple, device_model=Redmi K70」这种新的可见矛盾，所以这一组必须一起给。
# 品牌名对齐 knowledge/domains/user/user_devices.md 的「### device_brand 设备品牌」，
# 那张表按线上库实测写的：**牌名以中文入库**（华为 / 小米 / 三星 / 一加 / 荣耀），
# 只有 Apple / OPPO / vivo / realme 是拉丁字母，realme 还是小写。原来这里的键是
# Huawei / Xiaomi / Samsung / OnePlus / Realme，5 个英文名库里一个都没有，而且漏了
# 荣耀整个品牌。这一列当年也写在卡片的围栏块里，所以没被比过（见 INTEREST_TAGS 上方）。
IOS_MODELS = ["iPhone 15 Pro Max", "iPhone 15 Pro", "iPhone 15", "iPhone 14 Pro Max",
              "iPhone 14 Pro", "iPhone 14", "iPhone 13", "iPhone 13 mini", "iPhone 12",
              "iPhone SE", "iPad Pro 11", "iPad Air"]
# 型号也跟着改：中文牌名配 "Xiaomi 14 Pro" 这种带英文厂牌前缀的型号是新的可见矛盾。
# 各品牌在国内的实际命名（库里实测形态：小米 → "14 Pro" / "Redmi K70"，
# 一加 → "12 Pro" / "Ace 3"，realme → "真我 GT Neo5"，荣耀 → "Magic 6 Pro" / "90 GT"）。
ANDROID_MODELS = {
    "华为": ["Mate 60 Pro", "Mate 60", "Mate 50", "P60 Pro", "P60", "nova 12", "nova 11"],
    "荣耀": ["Magic 6 Pro", "Magic 6", "Magic 5", "90 GT", "100 Pro", "X50"],
    "小米": ["14 Pro", "14", "13 Ultra", "Redmi K70 Pro", "Redmi K70", "Redmi Note 13"],
    "三星": ["Galaxy S24 Ultra", "Galaxy S24", "Galaxy S23", "Galaxy A54",
             "Galaxy Z Flip5"],
    "OPPO": ["Find X7 Ultra", "Find X7", "Reno 11 Pro", "Reno 11", "A2 Pro"],
    "vivo": ["X100 Pro", "X100", "S18 Pro", "Y100", "iQOO 12"],
    "一加": ["12 Pro", "12", "Ace 3", "Nord CE"],
    "realme": ["真我 GT Neo5", "GT5 Pro", "GT Neo6", "真我 12 Pro+"],
}
WEB_MODELS = ["Chrome 121", "Chrome 120", "Safari 17", "Safari 16", "Firefox 122",
              "Edge 121"]
OS_IOS = ["iOS 17.3", "iOS 17.1", "iOS 16.6", "iOS 16.2", "iOS 15.7"]
OS_ANDROID = ["Android 14", "Android 13", "Android 12", "Android 11"]
OS_WEB = ["Windows 11", "Windows 10", "macOS 14", "macOS 13", "Ubuntu 22.04"]

# 投放计划名：真实命名习惯是「活动_玩法_渠道」。旧版 camp_0..49。
CAMPAIGNS = ["618_main_app", "618_yushou_kol", "618_flashsale_h5", "d11_presale",
             "d11_zhubo_live", "d11_hongbao", "d12_qingcang", "newuser_lijin",
             "newuser_0yuan_gou", "winback_30d", "winback_90d_coupon",
             "retarget_cart_7d", "retarget_view_3d", "brand_kol_xhs", "brand_kol_dy",
             "brand_zhihu_qa", "member_day_0918", "member_day_1018", "chunjie_nianhuo",
             "wuyi_travel", "kaixue_season", "mid_autumn_gift", "app_download_dy",
             "app_download_ks", "search_brand_baidu", "search_generic_baidu",
             "wechat_moments_ad", "wechat_mini_share", "sms_recall_v3",
             "push_daily_deal", "email_weekly_edm", "offline_qr_store",
             "kol_livestream_0801", "seed_user_invite", "fenxiao_share",
             "group_buy_3ren", "lottery_1yuan", "points_mall", "vip_upgrade"]

# UGC 标题：开场 × 对象 × 结论 × 尾缀。VARCHAR(200)，最长组合约 100 字节。
POST_OPEN = ["入手一个月了，", "犹豫很久终于买了，", "双十一囤的，", "被闺蜜种草的，",
             "第三次回购，", "踩了个小坑，", "客观说两句，", "用了半年来反馈，",
             "刚拆快递，", "对比了五家，", "冲动消费一次，", "618 抢到的，",
             "朋友推荐的，", "看直播下单的，", "蹲了两个月降价，", "说个反向种草，",
             "新手第一次买，", "换季刚好需要，", "旧的用坏了才换，", "同事都在用的",
             "刷到广告点进去的，", "凑单买的，", "给爸妈买的，", "自用一周后，"]
POST_OBJ = ["这个吹风机", "这支口红", "这台扫地机器人", "这双跑鞋", "这袋猫粮",
            "这条牛仔裤", "这只保温杯", "这台空气炸锅", "这套护肤品", "这个背包",
            "这款洗面奶", "这台显示器", "这副耳机", "这张瑜伽垫", "这盒面膜",
            "这件冲锋衣", "这个咖啡机", "这袋大米", "这台加湿器", "这双拖鞋",
            "这瓶精华", "这个键盘", "这条围巾", "这罐奶粉", "这台电饭煲",
            "这个收纳箱", "这支牙刷", "这件卫衣", "这盒零食", "这台风扇",
            "这只手表", "这个台灯"]
POST_VERDICT = ["真的值", "有点后悔", "闭眼入", "不推荐", "香到起飞", "性价比拉满",
                "一般般", "回购不犹豫", "翻车了", "超出预期", "智商税", "居然还不错",
                "劝你别买", "打折时值得", "细节拉分", "比想象中好", "只能说凑合",
                "买早了", "算是刚需", "没必要"]
POST_TAIL = ["", "", "！", "。", "，姐妹们冲", "，附实拍", "，避坑指南", "，别买贵了",
             "，附购买链接", "，说下缺点", "，谨慎参考", "，图在二楼"]
# 正文第二句：评测口吻的补充说明。与标题拼在一起要读得通，所以不复用评论池。
# 每一条都必须是**单点事实**（重量、噪音、尺码、售后…），不能带整体结论。正文与标题
# 共用同一次抽样，但结论词（POST_VERDICT）只在标题里；这里若冒出「我会推荐给朋友」，
# 碰上「不推荐」的标题就是同帖打自己的脸。
POST_BODY = ["用起来比宣传的轻，单手拿久了也不累。", "包装很稳，快递一路没磕碰。",
             "客服回复挺快，问题基本当天解决。", "做工细节到位，接缝没有毛刺。",
             "噪音比旧的那台小一半，晚上用不吵人。", "续航比标称的短一点，但够一天。",
             "颜色和图片基本一致，只是实物略深。", "上手五分钟就会用，说明书都没翻。",
             "重量偏大，出门带不太方便。", "配件给得挺全，不用再单独买。",
             "第一次用有点味道，通风两天就散了。", "价格随大促浮动明显，建议等活动。",
             "尺码偏小半号，建议往上选一档。", "清洗麻烦是唯一的槽点。",
             "和旗舰款对比过，差的主要是材质不是功能。",
             "用了两周没出问题，稳定性还行。", "耗材不贵，长期用得起。",
             "app 连接偶尔要重试，多按一次就好。",
             "售后寄回换新很顺利，运费也是商家出的。",
             "同价位里算能打，再贵就不值了。", "适合一个人用，家里人多会不够。",
             "说明书写得含糊，靠自己摸索。",
             "回来称过重量，和页面标的一致。", "冬天用效果更明显，夏天感受一般。",
             "赠品比想象中实用。", "发货第二天就到了，比预计快。",
             "包装盒有点压痕，东西本身没事。", "同批次的朋友也买了，情况差不多。"]

# 评论：开场 × 主体。真实评论会重复，但基数不能只有几十——组合后约 900 种。
COMMENT_A = ["", "", "哈哈哈", "说真的，", "同款！", "求问，", "已下单，", "看完想买了，",
             "我也这么觉得，", "楼主，", "刚好在看这个，", "谢谢分享，", "蹲个后续，",
             "不是吧，", "真的吗，", "我买过，", "刚退货，", "收藏了，", "路过，",
             "作为老用户，", "冲！", "醒醒，", "补充一下，", "帮顶，"]
COMMENT_B = ["这个我用了两年了", "求链接", "多少钱买的", "有没有平替", "颜色好好看",
             "感觉一般吧", "已经加购物车了", "什么时候有活动", "值这个价",
             "我踩过一样的坑", "还是等降价", "客服态度怎么样", "发货快吗",
             "尺码正常吗", "续航怎么样", "会不会掉色", "洗过之后缩水吗",
             "比隔壁那家好用", "退货麻烦吗", "有没有色差", "官旗和第三方差别大吗",
             "我妈也想要一个", "看着就很好用", "谢谢楼主，帮我省钱了",
             "别买，我后悔了", "这价格离谱了", "赞同", "学到了", "已入，等收货",
             "同问"]
COMMENT_TAIL = ["", "", "", "。", "！", "～", "？", "，哈哈", "，谢谢"]

# 私信：偏交易与商品咨询，符合电商站内私信的实际内容
DM_A = ["在吗", "你好", "打扰一下", "亲", "老板", "hi", "请问一下", "麻烦问下",
        "刚下单了", "看到你的帖子", "同城的话", "急问"]
DM_B = ["这个还有货吗", "能包邮吗", "可以议价吗", "什么时候发货", "有实拍图吗",
        "尺码怎么选", "支持七天无理由吗", "有优惠券吗", "能开发票吗",
        "帮我留一件", "顺丰能到吗", "颜色还有别的吗", "链接失效了",
        "订单号发你了", "什么时候能退款", "我想换个尺码", "地址填错了能改吗",
        "麻烦帮我看下物流", "还接单吗", "一起拼单吗", "这个型号停产了吗",
        "保修多久", "赠品还有吗", "能不能小刀"]
DM_TAIL = ["？", "？", "。", "，谢谢！", "，急", "，麻烦了", "～"]

# 推送文案：按 push_type 分池。真实 APP 的营销推送、交易通知、系统通知风格差得很远，
# 混着发最显眼。标题、正文与**落地页**三元成对给，不各抽一次——否则会出现「包裹已签收」配
# 「正在为你打包」这种同条推送自相矛盾。
#
# deep_link 进这份三元组而不是单独抽，理由同上：「优惠券将过期」跳 app://cart 一样是
# 自相矛盾。它原来是 `F.const(n, "app://home")` 整列一个值——卡片把 deep_link 描述成
# 「点击跳转深度链接」，而整列同值时任何按落地页分组的分析都只有一行
# （判据 `col_domain[push_notifications.deep_link]` 要求不同取值 ≥ 2）。
#
# 键必须是 knowledge/domains/marketing/push_notifications.md 声明的那 5 个值。最初这里
# 分的是 marketing / transactional / system 三池——那是 database/05_marketing_domain.sql
# 行内注释里的旧枚举，卡片和线上数据都是 reminder / social / promotion / order / system，
# 卡片里还专门写着「营销推送是 promotion **不是** marketing，交易推送是 order **不是**
# transactional」。分池分错的后果比取值写错更重：整列 title/content 会因为掩码一个都不
# 匹配而留在 None，而不是报错。按卡片重分后新增了 social 一整池（旧三池里没有任何社交
# 文案）和 reminder 池里的签到/券到期两条。
PUSH_COPY = {
    "reminder": [
        ("购物车提醒", "购物车里有 2 件正在打折", "app://cart"),
        ("订单待付款", "订单将在 30 分钟后关闭，尽快完成支付", "app://order/pending"),
        ("评价得积分", "确认收货后可获得 50 积分", "app://order/review"),
        ("签到提醒", "今天还没签到，连续 7 天可领券", "app://checkin"),
        ("优惠券将过期", "你有 1 张券今晚 24 点过期", "app://coupon/center"),
    ],
    "social": [
        ("新增关注", "有人关注了你，去看看 TA 的主页", "app://profile/followers"),
        ("收到新评论", "你的帖子有 1 条新评论", "app://post/comments"),
        ("收到私信", "有人给你发了条消息", "app://message/list"),
        ("帖子被点赞", "你的帖子收到 3 个赞", "app://post/likes"),
        ("好友有新动态", "你关注的人发布了新内容", "app://post/feed"),
    ],
    "promotion": [
        ("限时开抢", "你收藏的商品降价了，点进来看看", "app://promo/flashsale"),
        ("今日特惠", "满 300 减 50，今晚 12 点结束", "app://promo/today"),
        ("猜你喜欢", "为你挑了 5 件可能会喜欢的", "app://product/recommend"),
        ("新人礼包", "新人礼包还有 3 天过期，别浪费", "app://promo/newcomer"),
        ("同城包邮", "同城好物今日包邮，最快次日达", "app://promo/local"),
        ("新品首发", "你关注的品牌上新了", "app://product/new"),
        ("积分可用", "积分可抵 20 元，去看看", "app://points/mall"),
        ("会员日来了", "会员日双倍积分，仅限今天", "app://promo/memberday"),
        ("清仓最后一天", "冬季清仓最后一天，低至 3 折", "app://promo/clearance"),
    ],
    "order": [
        ("订单已发货", "你的包裹已由顺丰揽收，点击查看物流", "app://order/logistics"),
        ("支付成功", "本次订单已支付，正在为你打包", "app://order/detail"),
        ("快递派送中", "快递员正在派送，请留意来电", "app://order/logistics"),
        ("包裹已签收", "包裹已签收，觉得还不错就来晒个图", "app://order/review"),
        ("退款已到账", "退款已退回原支付账户，1-3 个工作日到账", "app://order/refund"),
        ("已为你退货", "退货已签收，退款将在审核后发起", "app://order/refund"),
    ],
    "system": [
        ("安全提醒", "检测到新设备登录，如非本人操作请及时修改密码",
         "app://settings/security"),
        ("账号通知", "你的手机号绑定已生效", "app://settings/account"),
        ("版本更新", "新版本已发布，修复了若干问题", "app://about/version"),
        ("服务协议更新", "《用户服务协议》将于下月更新，点击查看变更点",
         "app://about/agreement"),
    ],
}

# 各 push_type 的打开率。原来是整表一个 0.21，于是「哪类推送值得发」这个最基本的
# 推送运营问题在数据里没有答案（判据 `push_open_by_type` 要求 max/min ≥ 1.5×）。
# 形状按真实 APP 取：交易通知用户在等，打开率最高；营销推送最低，差 3~4 倍。
# 键必须与上面 PUSH_COPY 完全一致（长度与键集断言在 selftest_closures.py）。
PUSH_OPEN_RATE = {
    "order": 0.42,
    "social": 0.30,
    "reminder": 0.22,
    "system": 0.15,
    "promotion": 0.11,
}
# 只有营销型推送挂运营活动。事务型推送（订单、系统、社交、提醒）由业务事件触发，
# 不属于任何 campaign —— 这是 docs/data-audit.md 的 L3 一节明确写下的语义
# （「事务型推送不挂运营活动」），判据 `push_campaign_scope` 把它写成 0 行违例。
# 原来 `campaign_id` 是在**全部行**上按 18% 置空，于是 82% 的订单通知挂着营销活动，
# 「活动带来多少推送触达」这类查询会把交易通知也算进去。
MARKETING_PUSH_TYPES = ("promotion",)


def _pool(items, n=None):
    return np.array(items, dtype=object)


# ---------------------------------------------------------------- 上下文

@dataclass
class Ctx:
    """一次生成任务的全局参数与父表区间。"""
    seed: int
    rows: dict[str, int]                  # 表 → 目标行数（来自 budget.table_rows）
    start: np.datetime64                  # 数据窗起点
    days: int
    as_of_end: np.datetime64              # 窗末（含），静态样本的硬上限
    dim_ids: dict[str, np.ndarray]        # 维度表实际 id 列表（从现有 CSV 读）
    # 唯一对表的全局预生成结果
    pairs: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    # 派生依赖：orders 的状态/金额要给 payments、user_coupons 复用
    cache: dict = field(default_factory=dict)

    def n(self, table: str) -> int:
        return self.rows[table]

    def rng(self, *keys):
        return F.rng_for(self.seed, *keys)


# orders 状态权重：paid+shipped+delivered 约 75%，复现 dwd_orders_valid ≈ 75% 的行数比。
# 窗末订单会按剩余时间下调状态（见 _prep_orders），所以实际略高于 75%（约 75.8%）。
ORDER_STATUS = ["pending", "paid", "shipped", "delivered", "cancelled", "refunded"]
ORDER_STATUS_W = [10, 25, 22, 28, 9, 6]
VALID_STATUS = {"paid", "shipped", "delivered"}

# 买家备注池。原来这一列是 `F.const(n, "尽快发货")` 套 90% 置空——也就是说 8.5 万条
# 非空备注**全都是同一句话**，「按买家备注做文本分类 / 挑出要求发票的单」这类问题
# 恒得一行。取值来自真实网购备注的常见几类：时效、包装、发票、代收、门卫寄放。
# 不带 ASCII 句点（verify_literals 的词沙拉判据认「汉字紧跟 ASCII 句点且无『。』」
# 是 Faker 造句的特征）；权重让「尽快发货」仍是最常见的一条。
ORDER_REMARKS = [
    "尽快发货", "麻烦尽快安排发货，谢谢", "请勿放快递柜，送货上门",
    "工作日白天不在家，请放门卫", "需要开发票，公司名见后台",
    "请用礼品包装，不要放价签", "轻拿轻放，里面是易碎品",
    "到货前请电话联系", "周末再送，谢谢", "少放胶带，好拆一点",
]
ORDER_REMARKS_W = [30, 14, 12, 10, 9, 8, 6, 5, 4, 2]

# 事件名 → 漏斗权重。权重顺序保证 view_product > add_to_cart > begin_checkout。
# 权重 0 的三个事件不做泛化抽样，只从事实表反向保底生成（purchase 每有效单一条、
# use_coupon 每核销券一条、register 每用户一条）——否则「purchase 事件数 < 订单数」
# 这类方向性矛盾修不干净（审计 L4.8）。
# 注意：行预算天然压扁漏斗——85 万订单配 850 万事件意味着 purchase 占比约 7.5%，
# 真实 APP 是 0.5%~2%。这里只保证各级**单调递减**，不追真实转化率。
# 权重刻度：purchase 保底量 ≈ 泛化事件池的 8.7%（64 万有效单 / 736 万泛化事件），
# 所以链上每级的份额必须压着它往上排（begin_checkout ≈ 10.2% > 8.7%），
# 否则「漏斗单调」在保底量面前会翻车——这是算出来的，不是拍的。
EVENT_FUNNEL_W = {
    "view_home": 195, "view_product": 160, "add_to_cart": 130,
    "begin_checkout": 105, "app_open": 120, "app_close": 85,
    "view_post": 40, "login": 35, "search": 30, "view_category": 26,
    "logout": 22, "like_post": 20, "receive_push": 16, "add_favorite": 10,
    "click_banner": 8, "remove_from_cart": 7, "comment_post": 6,
    "view_profile": 5, "share": 4, "follow_user": 3, "click_push": 2,
    "edit_profile": 1,
    "register": 0, "purchase": 0, "use_coupon": 0,
}

# 用户活跃半衰期（天）与人群占比：会话落在注册后第 k 天的概率 ∝ exp(-k/h)。
# 混合出的留存曲线约 D1≈0.68、D7≈0.30、D14≈0.19——单调衰减，这是审计 L5.8
# 抓出的 P0（旧版 D14 比 D0 还高，留存类分析全部不可用）。
ENGAGEMENT_HALFLIFE = [1.0, 5.0, 20.0, 60.0]
ENGAGEMENT_W = [40, 30, 20, 10]


def prepare_globals(ctx: Ctx) -> None:
    """预生成跨块共享的状态。

    v2 之前只有 orders 一份全局切片（payments 复用它，所以支付金额从来是对的）。
    数据审计（docs/data-audit.md）证明了这个机制是对的、没走它的列全错了：
    凡是 builder 撇开 cache 自己另抽的声明列（item_count、like_count、event_count、
    user_level……），与明细必然对不上。所以 v2 把所有「一列的真值由另一张表决定」
    的东西全部提到这里：**先算事实，再把声明列从事实回填**，builder 只做切片。

    内存量级（80M 行规模）：orders 切片几十 MB；events/page_views 的归属与时间数组
    约 (8.5M + 12.9M) × 24B ≈ 530MB。生成机内存按 8GB 起步。
    """
    # products 必须最先：它回写 ctx.dim_ids["products"] / ["_product_names"]，
    # 而 build_order_items 的外键和反范式商品名都读这两项。
    _prep_products(ctx)
    _prep_product_tags(ctx)
    # posts 必须在 prepare_pairs 之前：赞的对表只能落在已发布的帖子上（见 _prep_posts）。
    _prep_posts(ctx)
    prepare_pairs(ctx)
    _prep_users(ctx)
    # 社交边的时间要读注册时间和发帖时间，所以在 users / posts / pairs 三者之后。
    _prep_edge_times(ctx)
    # user_profiles 必须在 orders 之前：orders 的金额要按每用户消费倍率缩放（D-01）。
    # 这条顺序就是修复本身——两者顺序反过来，画像与消费之间又没有边了。
    _prep_user_profiles(ctx)
    _prep_orders(ctx)
    _prep_order_items(ctx)
    # 必须在 _prep_order_items 之后：products 的五个计数器从明细的实际售出回填。
    _prep_product_counters(ctx)
    _prep_payments(ctx)
    _prep_user_coupons(ctx)
    _prep_behavior(ctx)
    _prep_social(ctx)
    _prep_user_level(ctx)

    # user_attributions：刻意只覆盖部分用户。mart LEFT JOIN 后形成知识库写明的
    # 「约六成 GMV 落在未归因」——这个缺口是治理发现的素材，不是缺陷。
    nu = ctx.n("users")
    ar = ctx.rng("user_attributions", "user_pick")
    natt = min(ctx.n("user_attributions"), nu)
    ctx.cache["attributed_users"] = np.sort(ar.choice(np.arange(1, nu + 1), size=natt,
                                                      replace=False))
    ctx.rows["user_attributions"] = natt


# ------------------------------------------------------- 商品域（D-02 / D-03）

# products.status 取值沿用 knowledge 卡片声明的四个值（selftest_closures 的
# check_enums_vs_cards 单向核对「产出 ⊆ 声明」），权重取 v1 实测比例 149/18/20/13。
PRODUCT_STATUS = ["on_sale", "off_sale", "pre_sale", "sold_out"]
PRODUCT_STATUS_W = [74, 9, 10, 7]

# 商品建档提前期（天）。理由见 _prep_products 的「建档时间早于分析窗口」一节。
PRODUCT_LEAD_DAYS = 180

# ---- 商品计数器的派生系数（用在 _prep_product_counters，判据共用同一份常量）----
#
# 窗前爬坡：建档到窗口开始那段时间的日均销量 ÷ 窗内日均销量。下限不取 0——"上架就
# 没动静"那类 SKU 由「窗内件数本身为 0」表达，不需要再叠一个 0 系数；上限取 1（窗前
# 卖得比窗内还好的商品当然存在，但那要靠趋势模型，一个常数区间表达不了）。
PRE_WINDOW_RAMP = (0.30, 1.00)
# 下单转化率 = 累计销量 ÷ 累计浏览量。1%~6% 是电商详情页的常见区间。
# **方向是浏览量从销量上推**，与 posts.view_count 同一手法（见 _prep_posts）。旧版是
# 反过来的——先抽 40 万~240 万浏览量、再乘 0.05%~3% 得销量，于是 4133 个 SKU 每一个
# 都是百万级曝光（实测中位数 116 万），而销量与真实订单毫无关系。
PRODUCT_CONV_RATE = (0.010, 0.060)
PRODUCT_FAV_RATE = (0.001, 0.05)       # 收藏 ÷ 浏览
PRODUCT_RATING_RATE = (0.02, 0.25)     # 评价 ÷ 累计销量

# 描述的中段与尾段。商品名三段（品牌/叶子类目/规格）已经保证描述互不相同，这两个池子
# 只负责让句子读起来像商品页文案。**刻意不含营销修饰词**（精选/优质/热卖…）：那批词是
# v1 商品名的模板残留，semantics.yaml 的 legacy_name_prefixes 正在反查它们。
DESC_FIT = ["适合日常使用", "口碑款，回购率稳定", "上架即进入类目热销", "同类里性价比突出",
            "细节做工扎实", "老客复购比例高", "尺码与描述一致", "参数与官方一致"]
DESC_SERVICE = ["官方正品，全国联保。", "顺丰发货，七天无理由退换。", "品牌直营，假一赔十。",
                "满额包邮，支持分期。", "现货速发，售后无忧。", "旗舰店发货，开具发票。"]


def _snap_price(v: np.ndarray) -> np.ndarray:
    """把连续价格吸到真实电商的心理价位上。

    真实商品价几乎不会是 `413.67`：百元以上落在 x99/x9（1299、4999），百元以下落在
    x.9（39.9、5.9）。少了这一步，「价格带对了」但每个数字一眼像随机数——D-03 抓的
    正是这种「统计上说得过去、逐条看就是假的」形态。

    吸附会把值推出原区间（104 → 99），所以调用方必须紧跟 clip 回 [lo, hi]。
    """
    return np.where(v >= 100.0, np.round(v / 10.0) * 10.0 - 1.0, np.floor(v) + 0.9)


def _prep_products(ctx: Ctx) -> None:
    """products 全局生成。判据全部来自 scripts/gen/semantics.yaml。

    那份配置是**声明态**，生成器和 L6/L7 检查集共用它，价格区间还共用
    `SEM.Semantics.price_range()` 这一个函数——配置共用还不够，把区间算错的方式有很多
    种，公式也必须共用，否则「修完生成器再改检查」还是自证（docs/test-plan.md 里
    「比的两端不独立」那一节）。

    ## 为什么这张表以前不存在（D-02）

    budget.py 的 docstring 郑重声明了三类缩放，而 SUB 类（商品/活动/券/广告素材）
    **整类没有 builder**：`budget.summary()` 照样打印 products 4133 行，LOAD_ORDER 里
    却根本没有这张表，`load_dim_ids()` 从 data/csv 原样读 200 行 v1 数据。于是 180 万条
    订单明细摊在 200 个 SKU 上（9021 单/SKU），126 个叶子类目里 25 个一个商品都没有。
    这不是"参数调小了"，是一整类声明是死的——和 Batch 1 抓的形态同科。

    ## 槽位法：为什么不逐行抽

    商品名 = `品牌 叶子类目 规格`，三段都来自配置，所以可产出的**不重名**名字是一个有限
    枚举。做法是把全部合法三元组展开成槽位表、按名字去重，再无放回选 n 个。逐行独立抽
    加"撞了重抽"在 65% 占用率下尾部会退化成大量拒绝采样，重试次数还随种子变化，破坏
    确定性。

    去重按**叶子名**而不是叶子路径：`T恤` 在女装和男装下各有一个（衬衫/外套/裤子同理，
    共 4 个重名叶子），优衣库两边都卖、`修身版` 两个池子都有——不按名字去重就会产出两个
    同名不同 category_id 的 SKU，「GMV Top10 商品」这类按名字聚合的题会把它们并成一行。

    ## 类目覆盖

    先给每个叶子类目保底 1 个 SKU，余量再按类目权重（对数正态，σ=0.6）分下去。保底这步
    是 D-02 的核心：不保底则热门类目吃掉全部配额、冷类目继续空着，而"空类目"在任何聚合
    结果里都是**不出现**——不报错，看不出来。

    ## 建档时间早于分析窗口

    created_at 落在 [start − 180d, start)。真实目录先于分析窗口存在；更要紧的是
    order_items 的商品外键是随机的，只要有一个 SKU 的建档时间晚于引用它的订单，就出现
    「买了还没上架的东西」。把目录整体推到窗口之前，这个矛盾由构造消除。v1 的 created_at
    铺在窗口内（2025-10-27 ~ 2026-01-24），必然带这类矛盾。

    ## 行内一致性

    v1 在这里也有实测矛盾：`status='sold_out'` 的 13 个 SKU 库存是 3749/4940/…（卖光了
    还有四千件），另有 1 行收藏数大于浏览数。这几列因此按依赖关系派生而不是各抽一次：
    sold_out ⇒ stock=0；收藏 ⊆ 浏览；评价数 ⊆ 销量；评价数为 0 时均分必须是 0。
    """
    s = SEM.load()
    n = ctx.n("products")
    leaves = s.tree.leaf_paths

    # ---- 槽位表（按商品名去重） ----
    slot_leaf: list[str] = []
    slot_brand: list[str] = []
    slot_spec: list[str] = []
    slot_name: list[str] = []
    seen: set[str] = set()
    for leaf in leaves:
        lname = leaf.rsplit(SEM.SEP, 1)[-1]
        for b in s.brands_for_leaf[leaf]:
            for sp in s.specs_for(leaf):
                nm = f"{b} {lname} {sp}"
                if nm in seen:
                    continue
                seen.add(nm)
                slot_leaf.append(leaf)
                slot_brand.append(b)
                slot_spec.append(sp)
                slot_name.append(nm)
    nslot = len(slot_name)
    if nslot < n:
        raise ValueError(
            f"semantics.yaml 只能产出 {nslot} 个不重名商品名，少于目标 {n} 行。"
            f"扩品牌白名单或规格词池，别去掉唯一性——重名会让按商品名聚合的题合行。")

    by_leaf: dict[str, list[int]] = {lf: [] for lf in leaves}
    for i, lf in enumerate(slot_leaf):
        by_leaf[lf].append(i)
    empty = [lf for lf in leaves if not by_leaf[lf]]
    if empty:
        raise ValueError(f"这些叶子类目按名字去重后没有剩余槽位，无法保证覆盖：{empty}")

    # ---- 选槽：每叶子先保底若干个，余量按类目权重无放回抽 ----
    #
    # 保底数随规模走：`n // 叶子数` 封顶 4，至少 1。全量（4133 SKU）保底 4 个，
    # scale=1 的小样本（200 SKU < 126×4）退回 1 个。**不写死 4**：写死会让小样本直接抛
    # 异常，而小样本是 hard constraint #1 要求的迭代方式，等于逼着人跳过它。
    #
    # 保底存在的理由是纯权重抽样会给低权重叶子只留下 1 个 SKU（实测过：min 1、中位 28），
    # 「某个叶子只有 1 个 SKU」在任何按类目的聚合里都看不出异常，只会让那一格的均值由
    # 单个 SKU 决定——D-02 的轻量版。
    r = ctx.rng("products", "slots")
    per = max(1, min(4, n // len(leaves)))
    base = np.concatenate([
        r.choice(by_leaf[lf], size=min(per, len(by_leaf[lf])), replace=False)
        for lf in leaves])

    # 品牌也要保底，理由与类目对称：「品牌 GMV 排名」「品牌客单价」和类目分析同构，
    # 一个品牌只有 1~2 个 SKU 时，品牌间对比同样退化成单商品对比。类目保底顺带覆盖了
    # 大部分品牌（每个叶子至少 1 个品牌），这里只补齐还欠的那些，所以增量很小。
    #
    # 这条不是为了让某个数字好看：没有它，实测每品牌 SKU 数 min = 2，而 L6 的下限如果
    # 跟着定成 2，就成了「贴着实测值定阈值」——检查只会在有人改生成器时响，不会在
    # 数据退化时响。先把下限按分析需要定成 4，再让生成器去满足它，方向才是对的。
    by_brand: dict[str, list[int]] = {}
    for i, b in enumerate(slot_brand):
        by_brand.setdefault(b, []).append(i)
    have = collections.Counter(slot_brand[i] for i in base.tolist())
    taken = set(base.tolist())
    per_b = max(1, min(4, n // len(by_brand)))
    extra: list[int] = []
    for b, idxs in by_brand.items():
        # 小样本（scale=1，200 行）撑不起 126 类目 + 115 品牌两条保底线，到额就停。
        # 于是「每品牌 ≥4」是**随规模成立**的性质，属于 L6 而不属于 scale=1 的 selftest
        # ——把它写进 selftest 会造出一条随规模闪烁的断言，最后一定被豁免掉。
        if len(taken) >= n:
            break
        short = min(per_b - have[b], n - len(taken))
        if short <= 0:
            continue
        cand = [i for i in idxs if i not in taken]
        if not cand:
            continue
        pick = r.choice(cand, size=min(short, len(cand)), replace=False).tolist()
        extra.extend(pick)
        taken.update(pick)
    if extra:
        base = np.concatenate([base, np.array(extra, dtype=base.dtype)])

    need = n - len(base)
    if need < 0:
        raise ValueError(f"products 目标 {n} 行 < 叶子类目数 {len(leaves)}，无法保证覆盖")
    if len(base) > n:
        raise ValueError(f"保底槽位 {len(base)} 超过目标 {n} 行")
    if need:
        rest = np.setdiff1d(np.arange(nslot), base)
        w_leaf = dict(zip(leaves, np.exp(r.normal(0.0, 0.6, len(leaves)))))
        w = np.array([w_leaf[slot_leaf[i]] for i in rest])
        sel = np.concatenate([base, r.choice(rest, size=need, replace=False, p=w / w.sum())])
    else:
        sel = base
    sel = sel[r.permutation(len(sel))]     # 打散：product_id 不该按类目成块

    leaf = [slot_leaf[i] for i in sel]
    brand = np.array([slot_brand[i] for i in sel], dtype=object)
    spec = np.array([slot_spec[i] for i in sel], dtype=object)
    name = np.array([slot_name[i] for i in sel], dtype=object)
    lname = np.array([lf.rsplit(SEM.SEP, 1)[-1] for lf in leaf], dtype=object)
    cat = np.array([s.tree.leaf_ids[lf] for lf in leaf], dtype=np.int64)
    pid = F.pk(n, 1)

    # ---- 价格：类目带 ∩ 品牌分档窗口，右偏后吸到心理价位 ----
    bounds = np.array([s.price_range(b, lf) for b, lf in zip(brand, leaf)])
    lo, hi = bounds[:, 0], bounds[:, 1]
    # beta(2,3) 右偏：同一类目里便宜款远多于顶配款，均匀分布会把中高价段抬平
    u = ctx.rng("products", "price").beta(2.0, 3.0, size=n)
    price = np.round(np.clip(_snap_price(lo + (hi - lo) * u), lo, hi), 2)

    mr = ctx.rng("products", "margin")
    # 划线价也吸到心理价位：真实商品页的划线价同样是 1699 / 59.9，不是 593.3。
    # 吸附可能把它压到现价以下，所以取 max——「划线价 ≥ 现价」是断言，不能靠运气。
    orig = np.maximum(np.round(_snap_price(price / mr.uniform(0.60, 1.00, n)), 2), price)
    # 成本不吸附：它是内部字段，不出现在商品页上，没有心理价位这回事。
    cost = np.round(price * mr.uniform(0.30, 0.70, n), 2)      # 成本 < 现价

    # ---- 描述：含本行品牌与类目名，且天然唯一（三段名字已唯一） ----
    dr = ctx.rng("products", "desc")
    fit = np.array(DESC_FIT, dtype=object)[dr.integers(0, len(DESC_FIT), n)]
    svc = np.array(DESC_SERVICE, dtype=object)[dr.integers(0, len(DESC_SERVICE), n)]
    desc = np.array([f"{b}{ln}（{sp}）。{f_}。{sv}"
                     for b, ln, sp, f_, sv in zip(brand, lname, spec, fit, svc)],
                    dtype=object)

    # ---- 状态与库存 ----
    #
    # 五个销量计数器**不在这里生成**：它们要从 order_items 的实际售出回填，而那份事实
    # 到 _prep_order_items 才存在。见 _prep_product_counters——本函数刻意不给这五列
    # 占位值，缺了那一步 build_products 会因为缺列直接炸，而不是静默灌一批零。
    #
    # 库存用自己的 RNG 流。上一版它与计数器共用 `ctx.rng("products","counts")`，
    # 后果是「改计数器的抽法会连带把库存全换一遍」——两件毫无依赖关系的事被一条
    # 随机流绑在一起，改动的影响面因此说不清。
    status = F.enum(ctx.rng("products", "status"), n, PRODUCT_STATUS, PRODUCT_STATUS_W)
    stock = F.int_uniform(ctx.rng("products", "stock"), n, 20, 5000)
    stock = np.where(status == "sold_out", 0, stock)                    # 卖光就是 0 件

    lead = ctx.start - np.timedelta64(PRODUCT_LEAD_DAYS, "D").astype("timedelta64[s]")
    created = F.ts_window(ctx.rng("products", "created_at"), n, lead, PRODUCT_LEAD_DAYS)
    nimg = F.int_uniform(ctx.rng("products", "nimg"), n, 3, 8)

    ctx.cache["products"] = {
        "product_id": pid,
        "product_name": name,
        "category_id": cat.astype(np.int32),
        "brand": brand,
        "description": desc,
        "price": price,
        "original_price": orig,
        "cost": cost,
        "stock": stock.astype(np.int32),
        # sold_count / view_count / favorite_count / rating_avg / rating_count
        # 由 _prep_product_counters 补齐（要先有 order_items）。这里刻意不占位。
        "main_image_url": np.array(
            [f"https://cdn.example.com/products/{i}/main.jpg" for i in pid.tolist()],
            dtype=object),
        "image_urls": np.array(
            [[f"https://cdn.example.com/products/{i}/{k}.jpg" for k in range(1, m + 1)]
             for i, m in zip(pid.tolist(), nimg.tolist())], dtype=object),
        "status": status,
        "is_featured": F.bool_p(ctx.rng("products", "featured"), n, 0.065),
        "created_at": created,
        "updated_at": created,
    }

    # 回写维度 id：build_order_items 的外键与反范式商品名从这里取，不再读 data/csv。
    # 顺序必须在 prepare_globals 的最前面，否则 order_items 会拿到旧的 200 行。
    ctx.dim_ids["products"] = pid
    ctx.dim_ids["_product_names"] = name


def _prep_product_tags(ctx: Ctx) -> None:
    """每个 SKU 挑若干标签，(product_id, tag_name) 全局唯一（DDL 有 UNIQUE 约束）。

    标签池在 semantics.yaml 的 tag_pools。**必须无放回抽**：同一商品抽到两次同名标签
    就违反 UNIQUE，而生成器里没有数据库帮你挡——真灌库时才炸，或者（Iceberg 不强制
    约束）静默留下重复行。

    tag_type 由标签名反查，不独立抽：独立抽会产出「tag_name=秒杀 / tag_type=season」
    这种同行打脸，形态与 check_row_coherence 抓的「标题写加湿器、正文写跑鞋」一样。

    行数按每商品标签数之和回写 ctx.rows（与 _prep_payments 同一手法）：预算给的是
    目标量级，真实行数由构造决定，两者不必强等。
    """
    s = SEM.load()
    np_ = ctx.n("products")
    flat = [(ty, nm) for ty, names in s.tag_pools.items() for nm in names]
    want = ctx.n("product_tags")

    # 每商品标签数：均值贴着 预算/商品数（本项目两个规模下都是 2.56），上限是池子大小
    kr = ctx.rng("product_tags", "k")
    k = F.int_weighted(kr, np_, [1, 2, 3, 4], [15, 30, 35, 20])
    k = np.minimum(k, len(flat))

    pick_r = ctx.rng("product_tags", "pick")
    prod_created = ctx.cache["products"]["created_at"]
    pids, names, types, created = [], [], [], []
    for i in range(np_):
        for j in pick_r.choice(len(flat), size=int(k[i]), replace=False):
            ty, nm = flat[j]
            pids.append(int(ctx.cache["products"]["product_id"][i]))
            names.append(nm)
            types.append(ty)
            created.append(prod_created[i])

    ctx.cache["product_tags"] = {
        "product_id": np.array(pids, dtype=np.int64),
        "tag_name": np.array(names, dtype=object),
        "tag_type": np.array(types, dtype=object),
        "created_at": np.array(created, dtype="datetime64[s]"),
    }
    ctx.rows["product_tags"] = len(pids)
    if abs(len(pids) - want) > 0.35 * want:
        raise ValueError(
            f"product_tags 实际 {len(pids)} 行与预算 {want} 差超过 35%，"
            f"调 _prep_product_tags 的 k 权重或 budget.BASE，别让两个数字长期打架")


def build_products(ctx: Ctx, off: int, n: int) -> dict:
    # 全局生成于 _prep_products（品牌↔类目白名单、价格带、分档窗口都在那里落地），
    # 这里只切片——与 orders/order_items 同一手法。
    sl = slice(off, off + n)
    return {k: v[sl] for k, v in ctx.cache["products"].items()}


def build_product_tags(ctx: Ctx, off: int, n: int) -> dict:
    sl = slice(off, off + n)
    g = ctx.cache["product_tags"]
    return {"id": F.pk(n, off + 1), **{k: v[sl] for k, v in g.items()}}


def _prep_users(ctx: Ctx) -> None:
    """注册时间 / VIP 全局化。orders、sessions 都要满足「先注册后行为」，
    user_level 要读 VIP 和注册期，所以这几列不能分块抽。"""
    nu = ctx.n("users")
    reg = F.ts_window(ctx.rng("users", "registered_at", "global"), nu,
                      ctx.start, ctx.days, trend=0.55)
    ctx.cache["users_registered_at"] = reg
    ctx.cache["users_reg_day"] = (
        reg.astype("datetime64[D]") - ctx.start.astype("datetime64[D]")
    ).astype(np.int64)
    ctx.cache["users_is_vip"] = F.bool_p(ctx.rng("users", "is_vip", "global"), nu, 0.12)


def _prep_user_profiles(ctx: Ctx) -> None:
    """画像四列 + 每用户消费强度倍率，全局一次算完（D-01）。

    ## 为什么必须提到全局

    这四列原来在 `build_user_profiles` 里分块抽，每列一条独立的 rng 流。分块抽本身没错，
    错的是**没有任何下游能读到它们**：`_prep_orders` 在 `prepare_globals` 阶段就把
    `total_amount` 抽完了，那时 user_profiles 的块还没生成。于是「收入档」和「消费额」
    结构上不可能相关——审计量到的 1.4% 极差不是权重没调好，是两侧之间没有边。

    修法只有一条：把画像提到 `_prep_orders` **之前**，算出每用户倍率，让订单金额读它。
    builder 退化成纯切片，和 v2 对 item_count / user_level 那批列的做法一致
    （见 prepare_globals 的文档：先算事实，再把声明列从事实回填）。

    ## 倍率怎么组合

    四个维度的倍率相乘，再整体除以样本均值归一到 1。归一是必须的：不归一，D-01 的修复
    会顺带把全库 GMV 抬高约 23%（四个维度的期望倍率之积），而 L5 的口径恒等式两边都是
    现算的 SQL、两边一起变，红不了——量级漂移会无人察觉地混进去。

    ## 边际分布不动

    gender 的 258/242、income_level 的 230/119/105/46 是 knowledge 卡片钉住的，
    `verify_enums.py` 和 `reconcile.py` 都在比，所以权重仍然写在这里、不搬去 YAML
    （`profiles.yaml` 的 `preserve_marginals` 声明了这件事，加载时断言那边没有第二份）。
    只有 occupation 换成了 YAML 的加权池——它没有卡片、也没有 DDL 枚举，本来就没有第二方。
    """
    nu = ctx.n("user_profiles")
    # mult 按 user_id 索引（mult[uid-1]），所以两张表的行数必须一致。这里不是"应该相等"
    # 而是"不等就必须炸"：budget.BASE 里两张表都是 (500, FACT)，若哪天有人改了一边，
    # 静默的后果是订单倍率错位到别人身上，数据看起来仍然正常。
    if nu != ctx.n("users"):
        raise ValueError(f"user_profiles({nu}) 与 users({ctx.n('users')}) 行数必须相等："
                         f"消费倍率按 user_id 下标取，错位后画像与消费会对到别人身上")

    dims, _judge, gen = PROF.load()
    age = F.int_uniform(ctx.rng("user_profiles", "age", "global"), nu,
                        dims.age_min, dims.age_max)
    # 卡片：500 行只有 female 258 / male 242，**没有** unknown，也没有 NULL。
    gender = F.enum(ctx.rng("user_profiles", "gender", "global"), nu,
                    ["female", "male"], [258, 242])
    # very_high 是补上的：卡片实测有 46 行，旧清单三档产不出它，
    # 于是「超高收入人群」这类问题在生成的数据上恒为空集。
    income = F.enum(ctx.rng("user_profiles", "income_level", "global"), nu,
                    ["medium", "low", "high", "very_high"], [230, 119, 105, 46])
    occs = list(dims.occupation)
    occupation = F.enum(ctx.rng("user_profiles", "occupation", "global"), nu, occs,
                        [gen.occupation_weights[o] for o in occs])

    # 四个维度的倍率查表 → 相乘。每个取值域最多 12 档，所以按档做 12 趟向量化掩码，
    # 而不是 searchsorted 那种"把键排序后按下标取"的写法：后者对键的顺序有隐含假设
    # （'100' < '20'），而且**取不到的值会静默拿到邻居的倍率**。这里少一个键就炸。
    def _lookup(values: np.ndarray, table: dict) -> np.ndarray:
        out = np.zeros(len(values), float)
        hit = np.zeros(len(values), bool)
        for k, v in table.items():
            m = values == k
            out[m] = v
            hit |= m
        if not hit.all():
            missing = sorted({str(x) for x in np.asarray(values)[~hit]})
            raise ValueError(f"profiles.yaml 里没有这些取值的倍率：{missing}")
        return out

    decade = PROF.age_decade(age)            # 与检查侧共用的同一个分档函数
    mult = (_lookup(income, gen.income_mult)
            * _lookup(gender, gen.gender_mult)
            * _lookup(occupation, gen.occupation_mult)
            * _lookup(decade, gen.age_decade_mult))
    mult /= mult.mean()                      # 归一：全库 GMV 量级不漂移

    ctx.cache["user_profiles"] = {"age": age, "gender": gender,
                                  "income_level": income, "occupation": occupation}
    ctx.cache["user_spend_multiplier"] = mult


def _pick_registered_user(ctx: Ctx, rng, event_day: np.ndarray) -> np.ndarray:
    """按「行为日不得早于注册日」采样 user_id，保留长尾偏斜。

    做法：用户按注册日排序，行为日 d 的候选集是排序数组的前缀；在前缀的偏斜权重
    累积和上做逆变换采样，全程向量化。旧版 fk_skewed 无视注册日，会造出
    「注册前就下单」的行（审计前尚未查这条，但代码上必错）。
    """
    if "_users_by_reg" not in ctx.cache:
        regday = ctx.cache["users_reg_day"]
        order = np.argsort(regday, kind="stable")
        w = F._skew_weights(ctx.rng("users", "activity_weight"), len(regday), 1.15)
        ctx.cache["_users_by_reg"] = (
            (order + 1).astype(np.int64), regday[order], np.cumsum(w[order]))
    uid_sorted, regday_sorted, cw = ctx.cache["_users_by_reg"]
    elig = np.searchsorted(regday_sorted, event_day, side="right")
    elig = np.maximum(elig, 1)          # 第 0 天也有注册用户（trend 抽样量级保证）
    target = rng.random(len(event_day)) * cw[elig - 1]
    idx = np.minimum(np.searchsorted(cw, target, side="right"), elig - 1)
    return uid_sorted[idx]


def _prep_orders(ctx: Ctx) -> None:
    no = ctx.n("orders")
    placed = F.ts_window(ctx.rng("orders", "placed_at", "global"), no, ctx.start,
                         ctx.days, trend=0.42)
    placed_s = placed.astype("datetime64[s]")
    remain = (ctx.as_of_end - placed_s).astype("timedelta64[s]").astype(np.int64)

    status = F.enum(ctx.rng("orders", "status", "global"), no, ORDER_STATUS,
                    ORDER_STATUS_W)
    # 状态按剩余窗口下调：窗末订单来不及走完生命周期。阈值与 _cond_ts 的 hi_min
    # 一致（refunded 14 天、delivered 7 天、shipped 2 天、cancelled 1 天、paid 4 小时）。
    # 不下调的话 status 与时间戳必然打架——审计 L4.6 抓到 5052 行 status=refunded
    # 而 refunded_at 为空，就是旧版「超窗置空但状态不回退」造成的。
    day = 86400
    status = np.where((status == "refunded") & (remain < 14 * day), "paid", status)
    status = np.where((status == "delivered") & (remain < 7 * day), "shipped", status)
    status = np.where((status == "shipped") & (remain < 2 * day), "paid", status)
    status = np.where((status == "cancelled") & (remain < 1 * day), "pending", status)
    status = np.where((status == "paid") & (remain < 4 * 3600), "pending", status)

    # user_id 全局算，且必须尊重注册日：payments 要与所属订单同一用户，
    # 「注册前下单」是真实库不可能出现的行。
    #
    # **这一段必须排在金额之前**（D-01 的第二条断开的边）。旧版顺序是先抽 total、
    # 再分配 uid，于是金额与「谁下的单」在结构上独立——哪怕画像列全部改成相关的，
    # 档间极差仍然是 0，因为金额根本不知道自己属于谁。任务书只点了画像列那一处。
    placed_day = (placed.astype("datetime64[D]")
                  - ctx.start.astype("datetime64[D]")).astype(np.int64)
    uid = _pick_registered_user(ctx, ctx.rng("orders", "user_id", "global"), placed_day)
    ctx.cache["orders_user_id"] = uid

    # 每单金额 = 全局长尾 × 下单人的消费强度倍率（均值已归一到 1，全库量级不变）。
    # 乘完再夹回 [9.9, 99999]：倍率最低的那一小撮用户会被 9.9 的地板托起来一部分，
    # 代价是他们的人均被抬高（压缩极差，方向保守，不会伪造出更大的差异）。
    # 底部截断的比例在 selftest_closures 里有断言盯着，越界就说明倍率跨度调过头了。
    mult = ctx.cache["user_spend_multiplier"][uid - 1]
    total = F.decimal_lognorm(ctx.rng("orders", "total_amount", "global"), no, 168.0,
                              sigma=0.85, lo=9.9, hi=99999)
    total = np.round(np.clip(total * mult, 9.9, 99999), 2)
    disc = np.round(total * F.enum(ctx.rng("orders", "disc_rate", "global"), no,
                                   [0.0, 0.05, 0.10, 0.20], [55, 20, 15, 10]).astype(float), 2)
    ship = F.enum(ctx.rng("orders", "shipping_fee", "global"), no,
                  [0.0, 6.0, 12.0], [62, 26, 12]).astype(float)
    # 注册当天的单可能抽在注册时刻之前的小时——同日内后移到注册后 60 秒
    # （不改日期，日级 GMV 不受影响），上限压在窗内防止注册在窗末最后一分钟的用户越窗
    reg_s = ctx.cache["users_registered_at"][uid - 1].astype("datetime64[s]")
    win_last = (ctx.start.astype("datetime64[s]")
                + np.timedelta64(ctx.days * 86400 - 1, "s"))
    placed = np.maximum(placed.astype("datetime64[s]"),
                        np.minimum(reg_s + np.timedelta64(60, "s"), win_last))
    ctx.cache["orders"] = {
        "status": status,
        "placed_at": placed,
        "total_amount": total,
        "discount_amount": disc,
        "shipping_fee": ship,
        "actual_amount": np.round(total - disc + ship, 2),
        "is_valid": np.isin(status, list(VALID_STATUS)),
    }

    # 每单件数：长尾、至少 1 行。orders.item_count 必须从这里读——
    # 旧版 builder 另抽了一份 int_weighted，70% 的单与明细行数不符（审计 L4.2）。
    cnt = F.children_per_parent(ctx.rng("order_items", "counts"), no,
                                ctx.n("order_items"), min_count=1)
    ctx.cache["orders_item_count"] = cnt
    ctx.cache["order_items_order_id"] = F.expand_ids(F.pk(no, 1), cnt)

    # 用券订单全局定下来：user_coupons 的核销行要反向对齐到 (user, coupon, order)
    has_cp = ctx.rng("orders", "coupon_id", "global").random(no) < 0.38
    cp_pick = F.from_pool(ctx.rng("orders", "cid", "global"), no,
                          ctx.dim_ids["coupons"])
    coupon = cp_pick.astype(object)
    coupon[~has_cp] = None
    ctx.cache["orders_coupon_id"] = coupon
    ctx.cache["orders_coupon_rows"] = np.flatnonzero(has_cp)      # 0-based


def _prep_order_items(ctx: Ctx) -> None:
    """明细金额全局生成并按单缩放：sum(items.actual_amount) 精确等于订单头 total_amount。

    审计 L4.1：85.4 万单里 99.99% 头合计对不上明细（差值双向随机），根因是两侧各自
    独立抽样。对齐方向选「明细缩放到头」而不是「头改成明细和」：mart 口径、eval 金标、
    知识卡片引用的全是头口径，反向会把 GMV 抬 41%、作废所有已冻结数字。

    金额一律走整数分（int64 cents）：浮点 round 在 .005 边界不稳定，且逐行 round 后
    按单求和有 ±0.005×件数 的漂移，审计阈值 0.01 会抓出来。每单末行吸收舍入差，
    行内公式 actual = unit×qty − discount 通过 ceil 分定价精确成立。
    """
    no, ni = ctx.n("orders"), ctx.n("order_items")
    cnt = ctx.cache["orders_item_count"]
    starts = np.concatenate(([0], np.cumsum(cnt)[:-1]))
    ends = np.cumsum(cnt) - 1

    qty = F.int_weighted(ctx.rng("order_items", "quantity", "global"), ni,
                         [1, 2, 3, 5], [68, 20, 8, 4])
    unit = F.decimal_lognorm(ctx.rng("order_items", "unit_price", "global"), ni,
                             79.0, sigma=0.8, lo=4.9, hi=9999)
    rate = F.enum(ctx.rng("order_items", "disc", "global"), ni,
                  [0.0, 0.05, 0.15], [70, 20, 10]).astype(float)

    raw = unit * qty * (1.0 - rate)                     # 未缩放的行净额
    scale = np.repeat(ctx.cache["orders"]["total_amount"]
                      / np.add.reduceat(raw, starts), cnt)

    head_c = np.round(ctx.cache["orders"]["total_amount"] * 100).astype(np.int64)
    act_c = np.round(raw * scale * 100).astype(np.int64)
    act_c[ends] += head_c - np.add.reduceat(act_c, starts)
    # 末行吸收的舍入差 < 件数×0.5 分，而缩放后行净额至少若干分，不该出现非正金额；
    # 万一行预算/件数分布改到极端值，这里要炸出来而不是灌进库
    assert (act_c > 0).all(), "明细缩放出现非正金额，检查件数分布与订单头下限"

    disc_c = np.clip(np.round(unit * qty * rate * scale * 100), 0, None).astype(np.int64)
    unit_c = -(-(act_c + disc_c) // qty)                # ceil 到分，保证行折扣非负

    # 卖的是哪个 SKU **也在这里定**（原来在 build_order_items 里逐分片抽）。挪上来是因为
    # products.sold_count 要从这份归属回填，而回填必须看到全量明细。分片抽样在当前规模下
    # 本来就是单分片（order_items 180 万行 < SHARD_ROWS 500 万），换到全局只改随机流身份、
    # 不改分布族；而它换来的是「累计销量与窗内销量指向同一批 SKU」这条跨表闭环。
    prod_idx = F.fk_skewed(ctx.rng("order_items", "product_id", "global"), ni,
                           1, len(ctx.dim_ids["products"])) - 1

    ctx.cache["items"] = {
        "product_idx": prod_idx,
        "quantity": qty,
        "unit_price": unit_c / 100.0,
        "discount_amount": (unit_c * qty - act_c) / 100.0,   # 公式精确成立
        "actual_amount": act_c / 100.0,
        "created_at": ctx.cache["orders"]["placed_at"][
            F.expand_ids(np.arange(no), cnt)],
    }


def _prep_product_counters(ctx: Ctx) -> None:
    """products 的五个计数器从 order_items 的实际售出回填，而不是各自独立抽样。

    审计缺口（docs/test-plan.md 的「跨表数值口径自洽」）：旧版在 _prep_products 里
    先抽 40 万~240 万浏览量，再乘 0.05%~3% 得 sold_count，与订单明细毫无关系。
    目标规模实测的后果——
      · Σsold_count ÷ Σ窗内件数 = 26.9×，比 PRODUCT_LEAD_DAYS 允许的上界还高一个量级；
      · 154 个 SKU（3.7%）是硬矛盾：最糟的一行 sold_count=1 而窗内真卖了 163 件；
      · Spearman(累计销量, 窗内销量) = +0.02，即两条「爆款榜」互不相干，
        Top20 重合 0/20——同一个问题走 products 和走 order_items 会给出两份榜单；
      · view_count 中位数 116 万，4133 个 SKU 里 4130 个落进 view_tier 的 1000+ 桶，
        那个 CASE 分档等于只有一档。

    方向选「从事实上推」而不是「改事实去凑计数器」：order_items 的金额闭环、订单头
    合计、mart 口径、eval 金标全都锚在明细上，反向会把它们一起作废。同一手法在
    _prep_posts（view_count 从 like 上推）和 _prep_behavior（sessions 的三列从事实
    回填）已经用过两次。

    sold_count 是**累计**，order_items 只覆盖 91 天窗口，商品建档还能早于窗口最多
    PRODUCT_LEAD_DAYS 天，所以口径是「窗内件数 + 窗前推算」而不是相等——判据那边
    对应地写成 sold ≥ 窗内 + 比值上界 + 秩相关，见 selftest_closures.check_product_counters。
    """
    p = ctx.cache["products"]
    n = len(p["product_id"])
    it = ctx.cache["items"]

    # 窗内实际售出件数：按 SKU 汇总明细的 quantity
    win = np.bincount(it["product_idx"],
                      weights=it["quantity"].astype(float),
                      minlength=n).astype(np.int64)

    # 窗前销量 = 窗内日均 × 建档到窗口开始的天数 × 爬坡系数。created_at 落在窗口内的
    # 商品（age=0）就没有窗前销量，于是 sold_count 恰好等于窗内件数——这是对的：
    # 一个窗口中途才上架的 SKU，它的「累计」就只有窗内这一段。
    r = ctx.rng("products", "counters")
    age = np.clip((ctx.start - p["created_at"]).astype("timedelta64[D]").astype(float),
                  0.0, None)
    ramp = r.uniform(*PRE_WINDOW_RAMP, n)
    pre = np.round(win / float(ctx.days) * age * ramp).astype(np.int64)
    sold = win + pre

    # 浏览量从销量上推（与 _prep_posts 同向）。maximum 兜住 sold=0 的情形——转化率
    # 除法给 0，而 view ≥ sold 这条不变量在两边都是 0 时仍然成立。
    conv = r.uniform(*PRODUCT_CONV_RATE, n)
    view = np.maximum(np.round(sold / conv).astype(np.int64), sold)
    fav = np.minimum(np.round(view * r.uniform(*PRODUCT_FAV_RATE, n)).astype(np.int64),
                     view)
    rate_n = np.minimum(np.round(sold * r.uniform(*PRODUCT_RATING_RATE, n)).astype(np.int64),
                        sold)
    # 无人评价时均分必须是 0：v1 有 rating_count=0 却 rating_avg=3.5 的行。
    # 窗内 0 件的 SKU 现在会真的落到 rate_n=0，这条不变量因此比旧版更常被走到。
    rating = np.where(rate_n > 0, np.round(r.uniform(3.5, 5.0, n), 1), 0.0)

    p["sold_count"] = sold.astype(np.int32)
    p["view_count"] = view.astype(np.int32)
    p["favorite_count"] = fav.astype(np.int32)
    p["rating_avg"] = rating
    p["rating_count"] = rate_n.astype(np.int32)


def _prep_payments(ctx: Ctx) -> None:
    """支付行 = 每个付过钱的订单恰好一条：valid → success，refunded → refunded。

    旧版从可付款订单里无放回抽 min(预算, 可付款数) 个、再叠 10% failed/pending，
    结果 64,214 个有效单查无 success 支付（审计 L4.6，占有效单 10%）。行预算本来
    就是按 (75%+6%)×订单数 定的，直接一一对应，行数差异用 ctx.rows 回写。
    """
    st = ctx.cache["orders"]["status"]
    payable = np.flatnonzero(np.isin(st, list(VALID_STATUS | {"refunded"})))
    ctx.cache["payments_order_idx"] = payable           # 0-based
    ctx.rows["payments"] = len(payable)


def _prep_user_coupons(ctx: Ctx) -> None:
    """核销闭环：orders 里带 coupon_id 的每一单，反向生成一行 status='used' 的券，
    (user_id, coupon_id, order_id) 三元对齐、used_at = 下单时刻；其余行只有
    unused / expired，且 expired 由 expire_at 是否已过窗末决定，不再随机抽。

    旧版三个数字互相打架（审计 L4.7）：orders 说 32.4 万单用券、user_coupons 说
    485 万张已用、还挂到了 85 万个不同订单上（平均 5.7 张/单）。
    """
    n_used = len(ctx.cache["orders_coupon_rows"])
    if n_used > ctx.n("user_coupons"):
        raise ValueError(f"用券订单 {n_used} 超过 user_coupons 行预算 "
                         f"{ctx.n('user_coupons')}，先调 budget")
    ctx.cache["uc_n_used"] = n_used


def _prep_behavior(ctx: Ctx) -> None:
    """行为域全局化：会话挂用户生命周期，事件/页面浏览挂会话。

    修的是两个 P0（审计 L5.6/L5.8）：旧版 events 的 user_id、event_time、event_name
    三者各自独立抽——事件名等权（25 类 max/min=1.006，漏斗恒 100% 转化）、活跃与
    注册无关（留存曲线不衰减）、事件所属 session 的 user 与事件的 user 互相矛盾。

    结构：每用户至少 1 个会话（保底事件要挂到本人会话上）→ 会话落在注册后第 k 天，
    k ~ 按活跃半衰期截断指数 → 事件继承会话的 user 与时间窗，事件名按漏斗权重 →
    sessions 的 event_count / page_view_count / is_bounce 从事实回填。
    """
    nu, ns = ctx.n("users"), ctx.n("sessions")
    ne, npv = ctx.n("events"), ctx.n("page_views")
    reg_day = ctx.cache["users_reg_day"]
    win_end = (ctx.start.astype("datetime64[s]")
               + np.timedelta64(ctx.days * 86400, "s"))

    # --- 会话 ---
    per_user = F.children_per_parent(ctx.rng("sessions", "counts"), nu, ns, min_count=1)
    sess_user = F.expand_ids(np.arange(1, nu + 1, dtype=np.int64), per_user)
    first_sess = np.zeros(nu + 1, np.int64)
    first_sess[1:] = np.concatenate(([0], np.cumsum(per_user)[:-1])) + 1

    r = ctx.rng("sessions", "lifecycle")
    h = np.repeat(F.enum(ctx.rng("users", "halflife"), nu,
                         ENGAGEMENT_HALFLIFE, ENGAGEMENT_W).astype(float), per_user)
    horizon = np.repeat((ctx.days - 1 - reg_day).astype(float), per_user)
    u = r.random(ns)
    k = np.floor(-h * np.log1p(-u * (1.0 - np.exp(-(horizon + 1.0) / h))))
    day = np.repeat(reg_day, per_user) + np.clip(k, 0, horizon).astype(np.int64)
    hr = r.choice(24, size=ns, p=F.HOUR_WEIGHTS / F.HOUR_WEIGHTS.sum())
    start_ts = (ctx.start.astype("datetime64[s]")
                + (day * 86400 + hr * 3600 + r.integers(0, 3600, ns))
                .astype("timedelta64[s]"))
    # 注册当天的会话（k=0）小时可能抽在注册时刻之前——按天对齐挡不住小时倒挂，
    # 钳到注册后 60 秒（selftest_closures 抓过这个）
    start_ts = np.maximum(
        start_ts, np.repeat(ctx.cache["users_registered_at"], per_user)
        .astype("datetime64[s]") + np.timedelta64(60, "s"))
    dur = F.int_weighted(ctx.rng("sessions", "duration_seconds", "global"), ns,
                         [12, 45, 120, 300, 900, 2400], [18, 22, 24, 20, 12, 4])
    dur = np.minimum(dur, np.maximum(
        (win_end - start_ts).astype("timedelta64[s]").astype(np.int64) - 1, 12))

    # --- 事件：泛化段（漏斗权重）+ 保底段（从事实表反向） ---
    names = ctx.dim_ids["event_definitions"]
    w = np.array([EVENT_FUNNEL_W.get(str(x), 5) for x in names], dtype=float)

    og = ctx.cache["orders"]
    valid_idx = np.flatnonzero(og["is_valid"])
    cp_rows = ctx.cache["orders_coupon_rows"]
    n_reserved = nu + len(valid_idx) + len(cp_rows)
    n_generic = ne - n_reserved
    assert n_generic >= ns, (f"事件预算 {ne} 扣掉保底 {n_reserved} 后不足以给 "
                             f"{ns} 个会话每个至少一条")

    er = ctx.rng("events", "global")
    per_sess = F.children_per_parent(ctx.rng("events", "counts"), ns, n_generic,
                                     min_count=1)
    g_sess = F.expand_ids(np.arange(1, ns + 1, dtype=np.int64), per_sess)
    g_time = (start_ts[g_sess - 1]
              + (er.random(n_generic) * np.maximum(dur[g_sess - 1], 1))
              .astype("timedelta64[s]"))
    g_name = er.choice(len(names), size=n_generic, p=w / w.sum()).astype(np.int16)

    def _idx_of(name: str) -> int:
        pos = np.flatnonzero(names == name)
        assert len(pos) == 1, f"event_definitions 里找不到 {name}"
        return int(pos[0])

    reg_ts = ctx.cache["users_registered_at"].astype("datetime64[s]")
    rr = ctx.rng("events", "reserved")
    # register：每用户一条，挂本人首会话，时刻 = 注册时刻
    res_uid = [np.arange(1, nu + 1, dtype=np.int64)]
    res_sess = [first_sess[1:]]
    res_time = [reg_ts]
    res_name = [np.full(nu, _idx_of("register"), np.int16)]
    # purchase：每有效单一条，挂下单人首会话，时刻 ≈ 支付窗口内
    o_uid = ctx.cache["orders_user_id"][valid_idx]
    res_uid.append(o_uid)
    res_sess.append(first_sess[o_uid])
    res_time.append(og["placed_at"][valid_idx].astype("datetime64[s]")
                    + rr.integers(60, 4 * 3600, len(valid_idx))
                    .astype("timedelta64[s]"))
    res_name.append(np.full(len(valid_idx), _idx_of("purchase"), np.int16))
    # use_coupon：每核销券一条（= 每个用券订单），时刻 = 下单时刻
    c_uid = ctx.cache["orders_user_id"][cp_rows]
    res_uid.append(c_uid)
    res_sess.append(first_sess[c_uid])
    res_time.append(og["placed_at"][cp_rows].astype("datetime64[s]"))
    res_name.append(np.full(len(cp_rows), _idx_of("use_coupon"), np.int16))

    ev_sess = np.concatenate([g_sess] + res_sess)
    ev_time = np.concatenate([g_time.astype("datetime64[s]")]
                             + [t.astype("datetime64[s]") for t in res_time])
    ev_time = np.minimum(ev_time, win_end - np.timedelta64(1, "s"))
    ctx.cache["events"] = {
        "session_id": ev_sess,
        "user_id": np.concatenate([sess_user[g_sess - 1]] + res_uid),
        "event_time": ev_time,
        "name_idx": np.concatenate([g_name] + res_name),
    }

    # --- 页面浏览：同样挂会话 ---
    pr = ctx.rng("page_views", "global")
    per_sess_pv = F.children_per_parent(ctx.rng("page_views", "counts"), ns, npv,
                                        min_count=1)
    pv_sess = F.expand_ids(np.arange(1, ns + 1, dtype=np.int64), per_sess_pv)
    pv_time = np.minimum(
        (start_ts[pv_sess - 1]
         + (pr.random(npv) * np.maximum(dur[pv_sess - 1], 1))
         .astype("timedelta64[s]")).astype("datetime64[s]"),
        win_end - np.timedelta64(1, "s"))
    ctx.cache["page_views"] = {"session_id": pv_sess,
                               "user_id": sess_user[pv_sess - 1],
                               "view_time": pv_time}

    # --- 会话计数从事实回填（旧版 event_count 拍脑袋，虚高 5.75 倍，审计 L4.4）---
    ev_cnt = np.bincount(ev_sess, minlength=ns + 1)[1:]
    pv_cnt = np.bincount(pv_sess, minlength=ns + 1)[1:]
    ctx.cache["sessions"] = {
        "user_id": sess_user, "start_time": start_ts,
        "duration_seconds": dur, "event_count": ev_cnt,
        "page_view_count": pv_cnt, "is_bounce": pv_cnt <= 1,
    }


def _prep_posts(ctx: Ctx) -> None:
    """帖子的 status 与发布时间提到全局。**必须在 prepare_pairs 之前**。

    两件事都是定义性的，靠分块抽样做不到：
    1. 赞只能落在**已发布**的帖子上。草稿和待审帖对外不可见，没有可点赞的对象
       （判据 `like_invisible`）。所以 `prepare_pairs` 需要先知道哪些 post_id 已发布。
    2. 赞的时间不能早于发帖时间（判据 `like_before_publish`）。原来 status 和
       `published_at` 各自独立抽、赞的时间又是全窗独立抽，三者互不相关，于是
       「先有赞、后有帖」和「赞落在草稿上」两件事都会成规模出现。

    `published_at` 只在 status='published' 时非空，与 v1 生成器
    （`scripts/generators/social_domain.py:212`）和线上数据一致；创建时间照旧全都有。
    """
    npost = ctx.n("posts")
    ctx.cache["posts_created_at"] = F.ts_window(
        ctx.rng("posts", "published_at", "global"), npost, ctx.start, ctx.days, trend=0.40)
    # 卡片：审核态是 under_review，**不是** hidden（hidden 那个值数据里不存在）。
    status = F.enum(ctx.rng("posts", "status", "global"), npost,
                    ["published", "under_review", "draft", "deleted"],
                    [847, 55, 49, 49])
    ctx.cache["posts_status"] = status
    ctx.cache["posts_published_ids"] = (np.flatnonzero(status == "published") + 1
                                        ).astype(np.int64)


def _ts_since(rng, lo: np.ndarray, cap: np.datetime64, decay: float) -> np.ndarray:
    """在 [lo, cap] 内按幂律偏向 lo 一侧取时间戳，小时仍走 HOUR_WEIGHTS 曲线。

    社交边（赞、关注）的时间下界是逐行不同的「两端都已存在」的那一刻，`ts_window`
    给不了这个约束（它只认全窗）。偏向 lo 也是真实形态：帖子发布后头一两天拿掉
    大部分赞。小时曲线保留下来，否则「按小时看社区活跃」会变成一条平线。

    `decay` 越大越集中在 lo 一侧（1.0 = 窗口内均匀）。逐行 clip 到 [1, room]，
    所以既严格晚于 lo、又不越过 cap。
    """
    lo_s = lo.astype("datetime64[s]")
    room = np.maximum((cap - lo_s).astype("timedelta64[s]").astype(np.int64), 1)
    frac = rng.random(len(room)) ** decay
    day = (room * frac).astype(np.int64) // 86_400
    hw = F.HOUR_WEIGHTS
    hr = rng.choice(24, size=len(room), replace=True, p=hw / hw.sum())
    sec = (day * 86_400 + hr.astype(np.int64) * 3600
           + rng.integers(0, 3600, size=len(room)))
    return lo_s + np.clip(sec, 1, room).astype("timedelta64[s]")


def _prep_edge_times(ctx: Ctx) -> None:
    """社交边的时间：不早于「两端都已存在」的那一刻。理由见 `_ts_since` 与 `_prep_posts`。

    必须在 `_prep_users`（拿注册时间）与 `prepare_pairs`（拿对表）之后。
    """
    reg = ctx.cache["users_registered_at"].astype("datetime64[s]")
    pub = ctx.cache["posts_created_at"].astype("datetime64[s]")
    lu, lp = ctx.pairs["post_likes"]
    ctx.cache["post_likes_created_at"] = _ts_since(
        ctx.rng("post_likes", "created_at", "global"),
        np.maximum(reg[lu - 1], pub[lp - 1]), ctx.as_of_end, 2.2)
    fa, fb = ctx.pairs["user_follows"]
    ctx.cache["user_follows_created_at"] = _ts_since(
        ctx.rng("user_follows", "created_at", "global"),
        np.maximum(reg[fa - 1], reg[fb - 1]), ctx.as_of_end, 1.4)


def _prep_social(ctx: Ctx) -> None:
    """帖子三计数器从明细回填（旧版 like_count = views×0.06，虚高 3.3 倍，审计 L4.3）。

    likes 的对表本来就是全局生成的（prepare_pairs），bincount 一下就是真值；
    comments / shares 的 post_id 原先分块抽，这里挪到全局，builder 只切片。
    view_count 反过来从 like 数上推（保证 view ≥ like，点赞率 4.5%~8%）。
    """
    npost = ctx.n("posts")
    _, like_post = ctx.pairs["post_likes"]
    cmt_post = F.fk_skewed(ctx.rng("post_comments", "post_id", "global"),
                           ctx.n("post_comments"), 1, npost)
    shr_post = F.fk_skewed(ctx.rng("post_shares", "post_id", "global"),
                           ctx.n("post_shares"), 1, npost)
    likes = np.bincount(like_post, minlength=npost + 1)[1:]
    r = ctx.rng("posts", "views")
    ctx.cache["post_comments_post_id"] = cmt_post
    ctx.cache["post_shares_post_id"] = shr_post
    ctx.cache["posts_counters"] = {
        "like_count": likes,
        "comment_count": np.bincount(cmt_post, minlength=npost + 1)[1:],
        "share_count": np.bincount(shr_post, minlength=npost + 1)[1:],
        "view_count": (likes * r.uniform(12, 22, npost)
                       + r.integers(30, 800, npost)).astype(np.int64),
    }


def _prep_user_level(ctx: Ctx) -> None:
    """user_level 按 knowledge/domains/user/users.md 的规则从事实推导，
    让卡片写的规则在数据里真实成立（审计 L4.5：旧版独立抽样，五档「消费≥1000
    占比」全是 21% 上下，agent 按等级筛高价值用户会拿到与消费无关的人群）。

    优先级：5（累计消费≥10000 或 VIP）> 4（≥1000）> 3（近 30 天活跃≥10 天）
    > 1（注册<30 天）> 2（默认）。last_active_at 同步从事件真值取 max。
    """
    nu = ctx.n("users")
    og = ctx.cache["orders"]
    valid = og["is_valid"]
    spend = np.bincount(ctx.cache["orders_user_id"][valid],
                        weights=og["actual_amount"][valid], minlength=nu + 1)[1:]

    ev = ctx.cache["events"]
    ev_day = (ev["event_time"].astype("datetime64[D]")
              - ctx.start.astype("datetime64[D]")).astype(np.int64)
    recent = ev_day >= ctx.days - 30
    key = ev["user_id"][recent] * 32 + (ev_day[recent] - (ctx.days - 30))
    act30 = np.bincount(np.unique(key) // 32, minlength=nu + 1)[1:]

    is_vip = ctx.cache["users_is_vip"]
    reg_day = ctx.cache["users_reg_day"]
    ctx.cache["users_level"] = np.select(
        [(spend >= 10000) | is_vip, spend >= 1000, act30 >= 10,
         reg_day >= ctx.days - 30],
        [5, 4, 3, 1], default=2).astype(np.int64)

    sec = (ev["event_time"]
           - ctx.start.astype("datetime64[s]")).astype("timedelta64[s]").astype(np.int64)
    la = np.zeros(nu + 1, np.int64)
    np.maximum.at(la, ev["user_id"], sec)
    ctx.cache["users_last_active"] = (ctx.start.astype("datetime64[s]")
                                      + la[1:].astype("timedelta64[s]"))


def prepare_pairs(ctx: Ctx) -> None:
    """一次性生成所有唯一对表的复合主键（无法分块，见模块 docstring）。"""
    nu = ctx.n("users")

    # 赞只落在已发布的帖子上：先在 [1, 已发布帖数] 上抽，再映回真实 post_id。
    # 这个「抽下标再映射」的写法与下面 user_segment_members / ab_test_assignments 一致。
    # 副作用是「零赞帖」有一个天然下界（未发布的那批一条赞也拿不到），正好也是真实形态。
    pub_ids = ctx.cache["posts_published_ids"]
    a, b = F.unique_pairs(ctx.rng("post_likes", "pk"), ctx.n("post_likes"),
                          1, nu, 1, len(pub_ids), sigma_b=SIGMA_POST_VIRALITY)
    ctx.pairs["post_likes"] = (a, pub_ids[b - 1])

    a, b = F.unique_pairs(ctx.rng("user_follows", "pk"), ctx.n("user_follows"),
                          1, nu, 1, nu, sigma=SIGMA_FOLLOW_OUT,
                          sigma_b=SIGMA_FOLLOW_IN, recip=FOLLOW_RECIP)
    ctx.pairs["user_follows"] = (a, b)

    segs = ctx.dim_ids["user_segments"]
    a, b = F.unique_pairs(ctx.rng("user_segment_members", "pk"),
                          ctx.n("user_segment_members"), 1, nu, 1, len(segs))
    ctx.pairs["user_segment_members"] = (a, segs[b - 1])

    tests = ctx.dim_ids["ab_tests"]
    a, b = F.unique_pairs(ctx.rng("ab_test_assignments", "pk"),
                          ctx.n("ab_test_assignments"), 1, nu, 1, len(tests))
    ctx.pairs["ab_test_assignments"] = (a, tests[b - 1])


# ---------------------------------------------------------------- 用户域

def build_users(ctx: Ctx, off: int, n: int) -> dict:
    # 注册时间 / VIP / 等级 / 最后活跃全部来自全局 cache：等级由消费+活跃+注册期
    # 推导（见 _prep_user_level），最后活跃 = 该用户事件时刻的真实 max。
    sl = slice(off, off + n)
    uid = F.pk(n, off + 1)
    reg = ctx.cache["users_registered_at"][sl]
    # 三列都是 D-04 占位符（旧：user_N / user_N@example.com / "103"+序号）。
    # email 不再由 username 派生：username 是中文昵称，进不了邮箱本地部分。
    # 手机号 = 真实号段 + 8 位随机尾，满足 ^1[3-9]\d{9}$（旧版第二位是 0，全不合规）。
    local = F.combine(ctx.rng("users", "email_local", off), n,
                      _pool(PY_FAMILY), _pool(PY_GIVEN), _pool(MAIL_TAIL))
    domain = F.enum(ctx.rng("users", "email_domain", off), n, MAIL_DOMAIN, MAIL_DOMAIN_W)
    tail8 = np.char.zfill(F.int_uniform(ctx.rng("users", "phone_tail", off), n,
                                        0, 9999_9999).astype("U8"), 8)
    return {
        "user_id": uid,
        "username": F.combine(ctx.rng("users", "username", off), n,
                              _pool(NICK_A), _pool(NICK_B), _pool(NICK_TAIL)),
        "email": np.char.add(np.char.add(local, "@"), np.asarray(domain, dtype=str)),
        "phone": np.char.add(np.asarray(F.from_pool(ctx.rng("users", "phone_seg", off), n,
                                                    _pool(PHONE_SEGMENTS)), dtype=str),
                             tail8),
        "registered_at": reg,
        # 取值与权重都取自 knowledge/domains/user/users.md 的实测表（权重直接用实测
        # 行数，F.enum 不要求归一化——这样每个数字都能在卡片里指回原处）。旧值
        # app / mini_program 和 banned 都来自 database/01_user_domain.sql 的行内注释，
        # 卡片明文写着数据里没有：封禁态是 suspended，注册来源那 8 个值里没有 app。
        "registration_source": F.enum(
            ctx.rng("users", "registration_source", off), n,
            ["referral", "organic", "huawei_store", "web",
             "ad_campaign", "wechat_mini", "app_store", "google_play"],
            [73, 68, 65, 62, 61, 59, 57, 55]),
        "status": F.enum(ctx.rng("users", "status", off), n,
                         ["active", "inactive", "deleted", "suspended"],
                         [376, 68, 30, 26]),
        "user_level": ctx.cache["users_level"][sl],
        "is_vip": ctx.cache["users_is_vip"][sl],
        "last_active_at": ctx.cache["users_last_active"][sl],
        "created_at": reg,
        "updated_at": reg,
    }


def build_user_profiles(ctx: Ctx, off: int, n: int) -> dict:
    uid = F.pk(n, off + 1)
    ci = ctx.rng("user_profiles", "city", off).choice(
        len(CITIES), size=n, replace=True,
        p=np.array([c[2] for c in CITIES], float) / sum(c[2] for c in CITIES))
    cities = np.array([c[0] for c in CITIES], dtype=object)[ci]
    provs = np.array([c[1] for c in CITIES], dtype=object)[ci]
    # 四列画像从全局切片，不在这里抽（D-01）：_prep_orders 要在 prepare_globals 阶段
    # 读到它们才能让金额与画像相关。见 _prep_user_profiles 的文档。
    prof = ctx.cache["user_profiles"]
    sl = slice(off, off + n)
    age = prof["age"][sl]
    # 生日由年龄反推，锚在 AS_OF 而不是系统时间
    birth = (ctx.as_of_end.astype("datetime64[D]")
             - (age.astype("timedelta64[D]") * 365))
    ir = ctx.rng("user_profiles", "interests", off)
    tags = np.array(INTEREST_TAGS, dtype=object)
    k = F.int_weighted(ir, n, [1, 2, 3, 4], [30, 35, 25, 10])
    interests = np.array([list(tags[ir.choice(len(tags), size=kk, replace=False)])
                          for kk in k], dtype=object)
    return {
        "user_id": uid,
        "age": age,
        "gender": prof["gender"][sl],
        "birth_date": birth,
        "city": cities,
        "province": provs,
        "country": F.const(n, "China"),
        "interests": interests,
        "occupation": prof["occupation"][sl],
        "income_level": prof["income_level"][sl],
        "created_at": F.ts_window(ctx.rng("user_profiles", "created_at", off), n,
                                  ctx.start, ctx.days),
        "updated_at": F.ts_window(ctx.rng("user_profiles", "updated_at", off), n,
                                  ctx.start, ctx.days),
    }


def _device_identity(ctx: Ctx, off: int, n: int, dtype: np.ndarray) -> tuple:
    """按 device_type 给出同源的 (品牌, 型号, OS 版本)。

    v1（scripts/generators/user_domain.py）本来是配对的：ios→Apple→iPhone 系列、
    android→先挑品牌再从该品牌的型号里挑、web→浏览器。v2 向量化重写时丢了这层——
    型号退化成 `model-0..39`（D-04），品牌与 OS 各自独立再抽一次。所以只换型号池
    是不够的：那会造出「device_brand=Apple, device_model=Redmi K70」这种**新的**
    可见矛盾，比原来的占位符更糟。三列必须同源。

    品牌名对齐 knowledge/domains/user/user_devices.md 的「### device_brand 设备品牌」，
    那张表是按线上库实测写的（牌名以中文入库）。2026-08-28 之前这里用的是英文牌名，
    且另有一个从未被引用的 `BRANDS = [..., "HUAWEI", ..., "honor", ...]` 常量，
    三套写法互不一致；死常量已删，只留 ANDROID_MODELS 这一份真源。
    """
    brand = np.empty(n, dtype=object)
    model = np.empty(n, dtype=object)
    osv = np.empty(n, dtype=object)

    m_ios = dtype == "ios"
    if m_ios.any():
        k = int(m_ios.sum())
        brand[m_ios] = "Apple"
        model[m_ios] = F.from_pool(ctx.rng("user_devices", "ios_model", off), k,
                                   _pool(IOS_MODELS))
        osv[m_ios] = F.from_pool(ctx.rng("user_devices", "ios_os", off), k, _pool(OS_IOS))

    m_and = dtype == "android"
    if m_and.any():
        k = int(m_and.sum())
        names = list(ANDROID_MODELS)
        bi = ctx.rng("user_devices", "and_brand", off).integers(0, len(names), size=k)
        b = np.array(names, dtype=object)[bi]
        # 型号必须落在所选品牌的型号表里（与 ab_test_assignments 挑 variant 同一手法）
        r = ctx.rng("user_devices", "and_model", off)
        model[m_and] = np.array([ANDROID_MODELS[x][r.integers(0, len(ANDROID_MODELS[x]))]
                                 for x in b], dtype=object)
        brand[m_and] = b
        osv[m_and] = F.from_pool(ctx.rng("user_devices", "and_os", off), k, _pool(OS_ANDROID))

    m_web = dtype == "web"
    if m_web.any():
        k = int(m_web.sum())
        brand[m_web] = "Browser"
        model[m_web] = F.from_pool(ctx.rng("user_devices", "web_model", off), k,
                                   _pool(WEB_MODELS))
        osv[m_web] = F.from_pool(ctx.rng("user_devices", "web_os", off), k, _pool(OS_WEB))

    # 小程序：三列都是**单一取值**，不抽池子。这不是偷懒，是照现行库实测——44 行
    # mini_program 设备的 brand/model/os_version 全是 WeChat / Mini Program / WeChat 8.0，
    # 一个变体都没有（小程序运行在宿主 APP 里，本来就没有机型和系统版本可言）。
    # 硬塞一个池子进去反而是凭空编数据。
    m_mp = dtype == "mini_program"
    if m_mp.any():
        brand[m_mp] = "WeChat"
        model[m_mp] = "Mini Program"
        osv[m_mp] = "WeChat 8.0"

    return brand, model, osv


def build_user_devices(ctx: Ctx, off: int, n: int) -> dict:
    # device_id 换成不透明十六进制（旧 `dev_N`）。**被编码的整数不变**，仍是行号，
    # 所以与 sessions/events 的对应关系一字不差地保留（见 F.opaque_id 的 docstring）。
    did = F.opaque_id(F.pk(n, off + 1))
    uid = F.fk_skewed(ctx.rng("user_devices", "user_id", off), n, 1, ctx.n("users"))
    first = F.ts_window(ctx.rng("user_devices", "first_seen_at", off), n, ctx.start, ctx.days)
    # mini_program 是补上的：卡片（knowledge/domains/user/user_devices.md）四个取值里
    # 写着它，旧清单只有三个，于是「小程序端有多少设备」在生成的数据上恒为空集。
    # 权重用现行库实测行数（744 行：android 372 / ios 245 / web 83 / mini_program 44）。
    # 这一列的卡片没有实测行数表，所以数字的出处是库而不是卡片——与其他枚举列不同，
    # 特此注明；F.enum 不要求归一化，原样贴进来便于回溯。
    dtype = F.enum(ctx.rng("user_devices", "device_type", off), n,
                   ["android", "ios", "web", "mini_program"], [372, 245, 83, 44])
    brand, model, osv = _device_identity(ctx, off, n, dtype)
    # push_token 真实形态是一串 hex（v1 用的 fake.sha256()[:64]）；web 与小程序都没有
    # 推送令牌（实测这两类共 127 行，push_token 非空 0 行）
    ptok = F.rand_hex(ctx.rng("user_devices", "push_token", off), n, 64).astype(object)
    ptok[np.isin(dtype, ["web", "mini_program"])] = None
    return {
        "device_id": did,
        "user_id": uid,
        "device_type": dtype,
        "os_version": osv,
        "device_model": model,
        "device_brand": brand,
        "app_version": F.enum(ctx.rng("user_devices", "app_version", off), n,
                              ["5.1.0", "5.2.0", "5.3.1", "6.0.0"], [10, 20, 40, 30]),
        "push_token": ptok,
        "is_primary": F.bool_p(ctx.rng("user_devices", "is_primary", off), n, 0.62),
        "first_seen_at": first,
        "last_seen_at": F.ts_offset(ctx.rng("user_devices", "last_seen_at", off), first,
                                    60, 80 * 1440, cap=ctx.as_of_end),
        "created_at": first,
    }


def build_user_segment_members(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["user_segment_members"]
    ent = F.ts_window(ctx.rng("user_segment_members", "entered_at", off), n,
                      ctx.start, ctx.days)
    return {
        "user_id": a[off:off + n],
        "segment_id": b[off:off + n],
        "entered_at": ent,
        "exited_at": F.ts_offset(ctx.rng("user_segment_members", "exited_at", off), ent,
                                 1440, 60 * 1440, cap=ctx.as_of_end, null_p=0.78),
    }


# ---------------------------------------------------------------- 行为域

# 卡片实测：utm_campaign 有 739/5000 ≈ 14.8% 未标记，且是**真 NULL**。
# 原来"未标记"是靠往 CAMPAIGNS 池里塞一个字符串 `"none"` 表示的，那是两个错：
#   1. 卡片明写「做归因时记得 `COALESCE(utm_campaign, '(未标记)')`」——对着字符串
#      `'none'` 这句 COALESCE **一行都不会命中**，15% 的会话会以 `none` 这个看起来
#      像正常活动名的值混进 GROUP BY，而且 `WHERE utm_campaign IS NULL` 恒空。
#      不报错、结果还挺像对的，正是本项目盯的那类缺陷。
#   2. `'none'` 本身就是占位符形态（D-04 要替掉的东西）。
# 空值率单独开一路随机流：ctx.rng 按 (表, 列, off) 命名播种，加一路不扰动其他列。
_UTM_NULL_P = 739 / 5000


def _utm_campaign(ctx: Ctx, off: int, n: int):
    v = F.from_pool(ctx.rng("sessions", "utm_campaign", off), n,
                    _pool(CAMPAIGNS)).astype(object)
    v[ctx.rng("sessions", "utm_campaign_null", off).random(n) < _UTM_NULL_P] = None
    return v


def build_sessions(ctx: Ctx, off: int, n: int) -> dict:
    # user/start/dur 来自生命周期模型，event_count/page_view_count/is_bounce
    # 从事实回填（旧版 event_count 拍脑袋，与 events 明细 93% 不符，审计 L4.4）。
    sl = slice(off, off + n)
    g = ctx.cache["sessions"]
    sid = F.pk(n, off + 1)
    uid = g["user_id"][sl]
    st = g["start_time"][sl]
    dur = g["duration_seconds"][sl]
    pv = g["page_view_count"][sl]
    end = st.astype("datetime64[s]") + dur.astype("timedelta64[s]")
    return {
        "session_id": sid,
        "user_id": uid,
        "device_id": F.opaque_id(uid),          # 编码前的整数仍是 user_id，对应关系不变
        "start_time": st,
        "end_time": end,
        "duration_seconds": dur,
        "event_count": g["event_count"][sl],
        "page_view_count": pv,
        "is_bounce": g["is_bounce"][sl],
        "entry_page": F.from_pool(ctx.rng("sessions", "entry_page", off), n, _pool(PAGES)),
        "exit_page": F.from_pool(ctx.rng("sessions", "exit_page", off), n, _pool(PAGES)),
        # 权重取卡片实测行数（868/846/826/825/824/811，基本均匀）；取值见 TRAFFIC。
        "traffic_source": F.enum(ctx.rng("sessions", "traffic_source", off), n,
                                 TRAFFIC, [868, 846, 826, 825, 824, 811]),
        # utm 两列的取值与权重都取卡片实测（knowledge/domains/behavior/sessions.md）。
        # 原来这里产的是照**旧文档**写的一套，而卡片早已逐条否认过它们：utm_medium 产
        # `cpc` / `social` / `email`，卡片原文是「旧文档写的 cpc / cpm / social … 都不存在；
        # email 也不在这里（它是 traffic_source 的值）」；utm_source 产字符串 `"none"`，
        # 卡片说这一列只有 6 个国内平台值、也没有 NULL。两列都写在卡片的围栏代码块里，
        # 所以「生成器产出 ⊆ 卡片声明」那条断言从来没看见过它们。
        "utm_source": F.enum(ctx.rng("sessions", "utm_source", off), n,
                             ["weixin", "douyin", "baidu", "xiaohongshu",
                              "organic", "direct"],
                             [861, 849, 835, 832, 816, 807]),
        "utm_medium": F.enum(ctx.rng("sessions", "utm_medium", off), n,
                             ["organic", "push", "paid", "referral", "banner"],
                             [1008, 1007, 1007, 1004, 974]),
        "utm_campaign": _utm_campaign(ctx, off, n),
        "created_at": st,
    }


def build_events(ctx: Ctx, off: int, n: int) -> dict:
    # user/session/时间/事件名全部来自 _prep_behavior：事件继承所属会话的用户与
    # 时间窗，事件名按漏斗权重（旧版三者独立抽——漏斗恒 100%、留存不衰减、
    # 事件的 user 与所属 session 的 user 互相矛盾，审计 L5.6/L5.8）。
    sl = slice(off, off + n)
    g = ctx.cache["events"]
    names = ctx.dim_ids["event_definitions"]
    uid = g["user_id"][sl]
    ev_time = g["event_time"][sl]
    return {
        "event_id": F.pk(n, off + 1),
        "user_id": uid,
        "device_id": F.opaque_id(uid),          # 与 sessions 同一映射，两表必须一致
        "session_id": g["session_id"][sl],
        "event_name": names[g["name_idx"][sl]],
        "event_time": ev_time,
        "properties": F.const(n, {}),
        "page_name": F.from_pool(ctx.rng("events", "page_name", off), n, _pool(PAGES)),
        # 与 page_views 共用同一份来源池：两张表都在描述「用户从哪儿来的」，各自维护
        # 一份取值域的话，同一个来源在两表里拼不起来（events 这边原来只有两个非空值，
        # 而且都不在 REFERRERS 里，按来源做跨表口径核对必然对不上账）。
        "referrer": F.enum(ctx.rng("events", "referrer", off), n,
                           REFERRERS, REFERRER_W),
        "ip_address": np.char.add("10.", np.char.add(
            (F.int_uniform(ctx.rng("events", "ip_a", off), n, 0, 255)).astype("U4"),
            np.char.add(".", np.char.add(
                (F.int_uniform(ctx.rng("events", "ip_b", off), n, 0, 255)).astype("U4"),
                np.char.add(".", (F.int_uniform(ctx.rng("events", "ip_c", off), n, 1, 254)
                                  ).astype("U4")))))),
        "created_at": ev_time,
    }


def build_page_views(ctx: Ctx, off: int, n: int) -> dict:
    # user/session/时间与 sessions 一致（来自 _prep_behavior），页面浏览数
    # 由 sessions.page_view_count 反向可对账。
    sl = slice(off, off + n)
    g = ctx.cache["page_views"]
    vt = g["view_time"][sl]
    # 页面按 PAGE_VIEW_WEIGHTS 加权，不再均匀摊在 15 个页面上（见那个常量的注释）。
    pages = F.enum(ctx.rng("page_views", "page_name", off), n, PAGES, PAGE_VIEW_WEIGHTS)
    # 停留时长与滚动深度用高斯 copula 联动：两列各自的档位分布不变，但相关系数从 0
    # 提到 0.53 左右（判据 `pv_dwell_scroll_corr` 要求 ≥ 0.30）。各自独立抽的话，
    # 「读得久的页面滚得更深」这条最基本的行为常识在数据里不成立。
    #
    # 时长档位同时兼顾重尾（判据 `pv_dwell_tail`：top10% 的时长要占总时长 ≥ 30%）：
    # 180 秒那一档占 7% 的行、却占约四成的总时长。
    dur, scroll = F.coupled_ladders(
        ctx.rng("page_views", "dwell_scroll", off), n,
        [3, 10, 25, 60, 180], [20, 30, 25, 18, 7],
        [10, 25, 50, 75, 100], [18, 22, 25, 20, 15], rho=0.70)
    return {
        "page_view_id": F.pk(n, off + 1),
        "user_id": g["user_id"][sl],
        "session_id": g["session_id"][sl],
        "page_name": pages,
        "page_url": np.char.add("https://m.example.com/", pages.astype("U32")),
        "referrer": F.enum(ctx.rng("page_views", "referrer", off), n,
                           REFERRERS, REFERRER_W),
        "duration_seconds": dur,
        "scroll_depth_pct": scroll,
        "view_time": vt,
        "created_at": vt,
    }


# ---------------------------------------------------------------- 社交域

def build_posts(ctx: Ctx, off: int, n: int) -> dict:
    pid = F.pk(n, off + 1)
    # status 与创建时间来自 _prep_posts（赞的对表和时间都依赖它们，不能分块抽）。
    sl = slice(off, off + n)
    status = ctx.cache["posts_status"][sl]
    created = ctx.cache["posts_created_at"][sl]
    # published_at 只在已发布时非空：草稿和待审帖还没发出去，删除的帖子在 v1 里同样为空。
    pub = np.array(created.astype(object), dtype=object)
    pub[status != "published"] = None
    # 三个计数器从明细回填（旧版 like_count = views×0.06 拍脑袋，虚高 3.3 倍，
    # 审计 L4.3）；view_count 从 like 数上推，保证 view ≥ like。
    cn = ctx.cache["posts_counters"]
    # D-04：旧版 post_N / 内容正文 N。标题与正文**共用同一次抽样**（core），
    # 各抽一次会造出「标题写加湿器、正文写跑鞋」的同帖自相矛盾——那比占位符更糟。
    core = F.combine(ctx.rng("posts", "title", off), n,
                     _pool(POST_OPEN), _pool(POST_OBJ), _pool(POST_VERDICT))
    _t = np.char.add(core, np.asarray(
        F.from_pool(ctx.rng("posts", "title_tail", off), n, _pool(POST_TAIL)), dtype=str))
    _c = np.char.add(np.char.add(core, "。"), np.asarray(
        F.from_pool(ctx.rng("posts", "body", off), n, _pool(POST_BODY)), dtype=str))
    return {
        "post_id": pid,
        "user_id": F.fk_skewed(ctx.rng("posts", "user_id", off), n, 1, ctx.n("users")),
        "content_type": F.enum(ctx.rng("posts", "content_type", off), n,
                               ["article", "short_video", "image", "review"],
                               [20, 32, 28, 20]),
        "title": _t,
        "content": _c,
        "media_urls": F.const(n, []),
        "tags": F.const(n, []),
        "location": F.from_pool(ctx.rng("posts", "location", off), n,
                                _pool([c[0] for c in CITIES])),
        "product_ids": F.const(n, []),
        "view_count": cn["view_count"][sl],
        "like_count": cn["like_count"][sl],
        "comment_count": cn["comment_count"][sl],
        "share_count": cn["share_count"][sl],
        "status": status,
        "is_featured": F.bool_p(ctx.rng("posts", "is_featured", off), n, 0.05),
        "published_at": pub,
        "created_at": created,
        "updated_at": created,
    }


def build_post_likes(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["post_likes"]
    # created_at 来自 _prep_edge_times：逐行下界是 max(发帖时间, 点赞者注册时间)。
    return {
        "user_id": a[off:off + n],
        "post_id": b[off:off + n],
        "created_at": ctx.cache["post_likes_created_at"][off:off + n],
    }


def build_post_comments(ctx: Ctx, off: int, n: int) -> dict:
    cid = F.pk(n, off + 1)
    return {
        "comment_id": cid,
        # post_id 全局生成（posts.comment_count 要 bincount 它回填），builder 只切片
        "post_id": ctx.cache["post_comments_post_id"][off:off + n],
        "user_id": F.fk_skewed(ctx.rng("post_comments", "user_id", off), n, 1,
                               ctx.n("users")),
        # 回复：指向本块内更早的评论，保证外键落在已存在的 id 上
        "parent_comment_id": F.null_out(
            ctx.rng("post_comments", "parent_comment_id", off),
            np.maximum(off + 1, cid - F.int_uniform(
                ctx.rng("post_comments", "parent_off", off), n, 1, 50)), 0.72),
        "content": F.combine(ctx.rng("post_comments", "content", off), n,
                             _pool(COMMENT_A), _pool(COMMENT_B), _pool(COMMENT_TAIL)),
        "like_count": F.int_weighted(ctx.rng("post_comments", "like_count", off), n,
                                     [0, 1, 3, 8, 25], [45, 25, 18, 9, 3]),
        "status": F.enum(ctx.rng("post_comments", "status", off), n,
                         ["visible", "hidden", "deleted"], [92, 5, 3]),
        "created_at": F.ts_window(ctx.rng("post_comments", "created_at", off), n,
                                  ctx.start, ctx.days, trend=0.40),
    }


def build_post_shares(ctx: Ctx, off: int, n: int) -> dict:
    return {
        "share_id": F.pk(n, off + 1),
        "user_id": F.fk_skewed(ctx.rng("post_shares", "user_id", off), n, 1, ctx.n("users")),
        # post_id 全局生成（posts.share_count 要 bincount 它回填），builder 只切片
        "post_id": ctx.cache["post_shares_post_id"][off:off + n],
        # 卡片：6 个值。朋友圈是 wechat_moment**s**（旧值少个 s，按卡片筛恒为空集），
        # 零散渠道归进 other——旧清单缺 qq / other 两档。
        "share_channel": F.enum(ctx.rng("post_shares", "share_channel", off), n,
                                ["wechat_friend", "wechat_moments", "weibo",
                                 "copy_link", "qq", "other"],
                                [2420, 1748, 1043, 725, 715, 349]),
        "created_at": F.ts_window(ctx.rng("post_shares", "created_at", off), n,
                                  ctx.start, ctx.days, trend=0.40),
    }


def build_user_follows(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["user_follows"]
    return {
        "follower_id": a[off:off + n],
        "following_id": b[off:off + n],
        # created_at 来自 _prep_edge_times：逐行下界是两端注册时间的较晚者。
        "created_at": ctx.cache["user_follows_created_at"][off:off + n],
    }


def build_user_messages(ctx: Ctx, off: int, n: int) -> dict:
    mid = F.pk(n, off + 1)
    sent = F.ts_window(ctx.rng("user_messages", "sent_at", off), n, ctx.start, ctx.days)
    read = F.ts_offset(ctx.rng("user_messages", "read_at", off), sent, 1, 4320,
                       cap=ctx.as_of_end, null_p=0.32)
    return {
        "message_id": mid,
        "sender_id": F.fk_skewed(ctx.rng("user_messages", "sender_id", off), n, 1,
                                 ctx.n("users")),
        "receiver_id": F.fk_uniform(ctx.rng("user_messages", "receiver_id", off), n, 1,
                                    ctx.n("users")),
        "content": F.combine(ctx.rng("user_messages", "content", off), n,
                             _pool(DM_A), _pool(["，", "，", "，", " "]), _pool(DM_B),
                             _pool(DM_TAIL)),
        # 卡片：只有 text / image / link 三个值，分享类消息统一是 link
        # （旧值 product 以及 post_share / system 数据里都不存在）。
        "message_type": F.enum(ctx.rng("user_messages", "message_type", off), n,
                               ["text", "image", "link"], [7868, 907, 478]),
        "related_post_id": F.null_out(ctx.rng("user_messages", "related_post_id", off),
                                      F.fk_uniform(ctx.rng("user_messages", "rp", off), n,
                                                   1, ctx.n("posts")), 0.85),
        "related_product_id": F.null_out(
            ctx.rng("user_messages", "related_product_id", off),
            F.from_pool(ctx.rng("user_messages", "rprod", off), n,
                        ctx.dim_ids["products"]), 0.88),
        "is_read": np.array([x is not None for x in read], dtype=bool),
        "sent_at": sent,
        "read_at": read,
    }


# ---------------------------------------------------------------- 交易域

def _cond_ts(rng, base: np.ndarray, mask: np.ndarray, lo_min: int, hi_min: int,
             cap: np.datetime64) -> np.ndarray:
    """按条件生成后续时间戳：mask 为假的位置置空，偏移上限逐行对 cap 截断。

    订单生命周期的时间列（paid/shipped/delivered/cancelled/refunded_at）必须与 status
    一致——status='pending' 却有 paid_at 是脏数据，会让「支付转化」类分析出错。

    v2 改动：旧版对超出 cap 的时间戳**置空但不回退 status**，造出 5052 行
    status='refunded' 而 refunded_at 为空（审计 L4.6）。现在状态生成侧已按剩余窗口
    下调（见 _prep_orders），这里再把偏移上限压到 [lo, cap-base]，双保险后
    mask 为真的行必有非空时间戳。
    """
    arr = np.array(_ts_after(rng, base, lo_min, hi_min, cap).astype(object), dtype=object)
    arr[~mask] = None
    return arr


def _ts_after(rng, base: np.ndarray, lo_min: int, hi_min: int,
              cap: np.datetime64) -> np.ndarray:
    """`_cond_ts` 的无掩码版：base 之后 [lo_min, hi_min] 分钟，逐行对 cap 截断。

    单独拆出来是给**链式**时间戳用的：push 的 opened_at 要接在 delivered_at 后面，
    而 delivered_at 已经按 is_delivered 置过空，object 数组里带 None，
    `astype("datetime64[s]")` 会当场报错。所以链上先用这个拿到未置空的数组，
    最后一步才置空。
    """
    base_s = base.astype("datetime64[s]")
    lo = lo_min * 60
    room = (cap - base_s).astype("timedelta64[s]").astype(np.int64)
    hi_row = np.maximum(np.minimum(hi_min * 60, room), lo + 1)
    off = (lo + rng.random(len(base_s)) * (hi_row - lo)).astype(np.int64)
    return base_s + off.astype("timedelta64[s]")


def build_orders(ctx: Ctx, off: int, n: int) -> dict:
    g = ctx.cache["orders"]
    sl = slice(off, off + n)
    oid = F.pk(n, off + 1)
    status = g["status"][sl]
    placed = g["placed_at"][sl]
    paid_m = np.isin(status, ["paid", "shipped", "delivered", "refunded"])
    ship_m = np.isin(status, ["shipped", "delivered"])
    deliv_m = status == "delivered"
    canc_m = status == "cancelled"
    refund_m = status == "refunded"
    return {
        "order_id": oid,
        "order_no": F.serial_text("NO", oid, width=12),
        "user_id": ctx.cache["orders_user_id"][sl],
        "status": status,
        "total_amount": g["total_amount"][sl],
        "discount_amount": g["discount_amount"][sl],
        "shipping_fee": g["shipping_fee"][sl],
        "actual_amount": g["actual_amount"][sl],
        # 件数 = order_items 真实行数（旧版另抽一份，70% 不符，审计 L4.2）；
        # 用券订单全局定（user_coupons 的核销行要对齐它，审计 L4.7）
        "item_count": ctx.cache["orders_item_count"][sl],
        "coupon_id": ctx.cache["orders_coupon_id"][sl],
        "shipping_address": F.const(n, {"province": "广东", "city": "深圳"}),
        # null_out 仍用原来那条随机流，所以**哪些行为空一行没动**；变的只是非空行
        # 从「同一句话」换成 ORDER_REMARKS 里的一条（取值用独立流）。
        "remark": F.null_out(ctx.rng("orders", "remark", off),
                             F.enum(ctx.rng("orders", "remark_text", off), n,
                                    ORDER_REMARKS, ORDER_REMARKS_W), 0.9),
        "placed_at": placed,
        "paid_at": _cond_ts(ctx.rng("orders", "paid_at", off), placed, paid_m, 1, 240,
                            ctx.as_of_end),
        "shipped_at": _cond_ts(ctx.rng("orders", "shipped_at", off), placed, ship_m,
                               240, 2880, ctx.as_of_end),
        "delivered_at": _cond_ts(ctx.rng("orders", "delivered_at", off), placed, deliv_m,
                                 2880, 10080, ctx.as_of_end),
        "cancelled_at": _cond_ts(ctx.rng("orders", "cancelled_at", off), placed, canc_m,
                                 5, 1440, ctx.as_of_end),
        "cancel_reason": np.where(canc_m, "用户取消", None),
        "refunded_at": _cond_ts(ctx.rng("orders", "refunded_at", off), placed, refund_m,
                                1440, 20160, ctx.as_of_end),
        "refund_reason": np.where(refund_m, "商品问题", None),
        "created_at": placed,
        "updated_at": placed,
    }


def build_order_items(ctx: Ctx, off: int, n: int) -> dict:
    # 金额来自 _prep_order_items 的全局缩放结果：每单明细 actual_amount 之和
    # 精确（到分）等于订单头 total_amount（审计 L4.1 抓的 99.99% 不符）。
    # created_at = 所属订单下单时刻，不再独立乱抽。
    sl = slice(off, off + n)
    it = ctx.cache["items"]
    oids = ctx.cache["order_items_order_id"][sl]
    prods = ctx.dim_ids["products"]
    pnames = ctx.dim_ids["_product_names"]
    # SKU 归属在 _prep_order_items 里全局抽好（products.sold_count 要按它回填），
    # 这里只切片。逐分片重抽的话每片会各自重画一次长尾权重，跨表就对不上账了。
    idx = it["product_idx"][sl]
    pid = prods[idx]
    return {
        "item_id": F.pk(n, off + 1),
        "order_id": oids,
        "product_id": pid,
        # 反范式的真实商品名（不是占位符），与 products.product_name 一致
        "product_name": pnames[idx],
        "sku_id": pid * 100 + F.int_uniform(ctx.rng("order_items", "sku", off), n, 1, 9),
        "sku_name": np.char.add(pnames[idx].astype("U80"), "-标准装"),
        "quantity": it["quantity"][sl],
        "unit_price": it["unit_price"][sl],
        "discount_amount": it["discount_amount"][sl],
        "actual_amount": it["actual_amount"][sl],
        "created_at": it["created_at"][sl],
    }


def build_payments(ctx: Ctx, off: int, n: int) -> dict:
    # 每个付过钱的订单恰好一条支付：valid → success、refunded → refunded，
    # 与订单状态由构造保证一致（旧版抽样式覆盖漏掉 10% 有效单，审计 L4.6）。
    g = ctx.cache["orders"]
    idx = ctx.cache["payments_order_idx"][off:off + n]     # 0-based 订单下标
    oids = idx + 1
    pid = F.pk(n, off + 1)
    base = g["placed_at"][idx]
    amt = g["actual_amount"][idx]
    refunded = g["status"][idx] == "refunded"
    always = np.ones(n, dtype=bool)
    return {
        "payment_id": pid,
        "payment_no": F.serial_text("PAY", pid, width=12),
        "order_id": oids,
        "user_id": ctx.cache["orders_user_id"][idx],   # 与所属订单同一用户
        "amount": amt,
        "payment_method": F.enum(ctx.rng("payments", "payment_method", off), n,
                                 ["wechat", "alipay", "credit_card", "balance"],
                                 [45, 40, 10, 5]),
        "payment_channel": F.enum(ctx.rng("payments", "payment_channel", off), n,
                                  ["app", "h5", "mini_program"], [58, 22, 20]),
        "status": np.where(refunded, "refunded", "success"),
        "transaction_id": F.serial_text("TXN", pid, width=16),
        "paid_at": _cond_ts(ctx.rng("payments", "paid_at", off), base, always,
                            1, 240, ctx.as_of_end),
        "failure_reason": F.const(n, None),
        "refund_amount": np.where(refunded, amt, 0.0),
        "refunded_at": _cond_ts(ctx.rng("payments", "refunded_at", off), base, refunded,
                                1440, 20160, ctx.as_of_end),
        "created_at": base,
    }


def build_subscriptions(ctx: Ctx, off: int, n: int) -> dict:
    sid = F.pk(n, off + 1)
    # 卡片：plan_name 是**中文值**（月度会员 / 季度会员 / 年度会员），不是 monthly
    # 那套英文枚举；价格另看 plan_price 列。三档的实测行数 29 / 12 / 9。
    plan = F.enum(ctx.rng("subscriptions", "plan_name", off), n,
                  ["月度会员", "季度会员", "年度会员"], [29, 12, 9])
    price = np.where(plan == "月度会员", 19.9, np.where(plan == "季度会员", 49.9, 168.0))
    days = np.where(plan == "月度会员", 30, np.where(plan == "季度会员", 90, 365))
    start = F.ts_window(ctx.rng("subscriptions", "start_date", off), n, ctx.start,
                        ctx.days).astype("datetime64[D]")
    # 卡片：种子数据只有 active 36 / expired 14。`cancelled` 在业务上成立，但这份数据里
    # 没有——卡片连带把「cancel_reason 整列 NULL」和一条恒返回空集的示例查询都写成了
    # 教学点。所以这里不产 cancelled，下面 cancelled_at / cancel_reason 的掩码恒为假，
    # 那两列跟着整列 NULL，与卡片一致。要造有取消的数据集，改这一行的取值即可，
    # 但卡片得同步改，否则枚举断言会红。
    st = F.enum(ctx.rng("subscriptions", "status", off), n,
                ["active", "expired"], [36, 14])
    return {
        "subscription_id": sid,
        "user_id": F.fk_skewed(ctx.rng("subscriptions", "user_id", off), n, 1,
                               ctx.n("users")),
        "plan_name": plan,
        "plan_price": price,
        "start_date": start,
        "end_date": start + days.astype("timedelta64[D]"),
        "auto_renew": F.bool_p(ctx.rng("subscriptions", "auto_renew", off), n, 0.68),
        "status": st,
        "payment_id": F.null_out(ctx.rng("subscriptions", "payment_id", off),
                                 F.fk_uniform(ctx.rng("subscriptions", "pay", off), n, 1,
                                              ctx.n("payments")), 0.15),
        "cancelled_at": _cond_ts(ctx.rng("subscriptions", "cancelled_at", off),
                                 start.astype("datetime64[s]"), st == "cancelled",
                                 1440, 60 * 1440, ctx.as_of_end),
        "cancel_reason": np.where(st == "cancelled", "不再需要", None),
        "created_at": start.astype("datetime64[s]"),
        "updated_at": start.astype("datetime64[s]"),
    }


# ---------------------------------------------------------------- 归因域

def build_user_attributions(ctx: Ctx, off: int, n: int) -> dict:
    users = ctx.cache["attributed_users"][off:off + n]
    aid = F.pk(n, off + 1)
    chans = ctx.dim_ids["channels"]
    click = F.ts_window(ctx.rng("user_attributions", "click_time", off), n, ctx.start,
                        ctx.days)
    d2i = F.int_weighted(ctx.rng("user_attributions", "days_to_install", off), n,
                         [0, 1, 2, 5], [62, 22, 10, 6])
    return {
        "attribution_id": aid,
        "user_id": users,
        "channel_id": F.from_pool(ctx.rng("user_attributions", "channel_id", off), n, chans),
        "ad_campaign_id": F.null_out(
            ctx.rng("user_attributions", "ad_campaign_id", off),
            F.from_pool(ctx.rng("user_attributions", "acid", off), n,
                        ctx.dim_ids["ad_campaigns"]), 0.35),
        "creative_id": F.null_out(
            ctx.rng("user_attributions", "creative_id", off),
            F.from_pool(ctx.rng("user_attributions", "crid", off), n,
                        ctx.dim_ids["ad_creatives"]), 0.40),
        # 卡片：只有 first_touch / last_touch，各 175 行，**没有** linear
        # （按 linear 筛是空集）。两种归因分给的是**不同的用户**，不是同一用户的两个视角。
        "attribution_type": F.enum(ctx.rng("user_attributions", "attribution_type", off),
                                   n, ["first_touch", "last_touch"], [175, 175]),
        "click_time": click,
        "install_time": click.astype("datetime64[s]") + (d2i * 86400).astype("timedelta64[s]"),
        "attributed_at": click,
        "days_to_install": d2i,
        "tracking_params": F.const(n, {"utm_source": "douyin"}),
    }


# ---------------------------------------------------------------- 营销域

def build_user_coupons(ctx: Ctx, off: int, n: int) -> dict:
    """前 uc_n_used 行是核销闭环行：与用券订单一一对应，(user, coupon, order) 三元
    对齐、used_at = 下单时刻、received_at 落在 [注册, 下单] 且距下单 ≤30 天。
    其余行只有 unused / expired，expired 由 expire_at 是否已过窗末决定。

    旧版 status 以 50% 概率抽 used、order_id 均匀乱指，与 orders.coupon_id 各说
    各话，核销数是订单数的 5.7 倍（审计 L4.7）。
    """
    cid = F.pk(n, off + 1)
    gidx = np.arange(off, off + n, dtype=np.int64)
    n_used = ctx.cache["uc_n_used"]
    used_m = gidx < n_used

    og = ctx.cache["orders"]
    reg_all = ctx.cache["users_registered_at"].astype("datetime64[s]")
    win_end = (ctx.start.astype("datetime64[s]")
               + np.timedelta64(ctx.days * 86400, "s"))
    r = ctx.rng("user_coupons", "recv", off)

    # —— 核销段（未用行按 0 号占位算，最后 where 合并，保持向量化）——
    safe = np.minimum(gidx, max(n_used - 1, 0))
    o = ctx.cache["orders_coupon_rows"][safe] if n_used else np.zeros(n, np.int64)
    uid_u = ctx.cache["orders_user_id"][o]
    cp_u = ctx.cache["orders_coupon_id"][o].astype(np.int64)   # 用券订单必非空
    placed = og["placed_at"][o].astype("datetime64[s]")
    span = np.minimum(np.maximum(
        (placed - reg_all[uid_u - 1]).astype("timedelta64[s]").astype(np.int64), 120),
        30 * 86400)
    recv_u = np.maximum(
        placed - (60 + r.random(n) * (span - 60)).astype("timedelta64[s]"),
        reg_all[uid_u - 1])

    # —— 未用段：领券时刻落在 [本人注册, 窗末) ——
    uid_g = F.fk_skewed(ctx.rng("user_coupons", "user_id", off), n, 1, ctx.n("users"))
    reg_g = reg_all[uid_g - 1]
    room = np.maximum(
        (win_end - reg_g).astype("timedelta64[s]").astype(np.int64) - 60, 60)
    recv_g = reg_g + (r.random(n) * room).astype("timedelta64[s]")
    cp_g = F.from_pool(ctx.rng("user_coupons", "coupon_id", off), n,
                       ctx.dim_ids["coupons"])

    picked = np.where(used_m, cp_u, cp_g)
    recv = np.where(used_m, recv_u, recv_g).astype("datetime64[s]")
    expire = recv + np.timedelta64(30 * 86400, "s")
    # 枚举取 'unused' 而非 DDL 注释里的 'available'：卡片、示例 SQL、eval 金标、
    # 以及 v1 已提交的 CSV 四方都用 unused，只有 DDL 注释是孤例。跟注释走会让
    # agent 照卡片写 status='unused' 查出 0 行（我们在 eval 里踩过这一刀）。
    st = np.where(used_m, "used",
                  np.where(expire < ctx.as_of_end, "expired", "unused"))
    used_at = np.array(placed.astype(object), dtype=object)
    used_at[~used_m] = None
    # 未使用的券不该带订单号：必须是 NULL 而不是 0（0 不是合法 order_id，
    # 会让「用券订单数」这类统计凭空多出一批）
    order_id = (o + 1).astype(object)
    order_id[~used_m] = None
    return {
        "id": cid,
        "user_id": np.where(used_m, uid_u, uid_g),
        "coupon_id": picked,
        "coupon_code": np.char.add("CP", np.char.zfill(picked.astype("U10"), 6)),
        "received_at": recv,
        "expire_at": expire,
        "used_at": used_at,
        "order_id": order_id,
        "status": st,
        # 卡片：主动领取是 claim（占 60%）、赠送是 gift。旧四档 campaign / new_user /
        # share / purchase 在 22,735 行里一个都不存在。
        "source": F.enum(ctx.rng("user_coupons", "source", off), n,
                         ["claim", "gift", "reward", "system"],
                         [13649, 3466, 3327, 2293]),
    }


def build_push_notifications(ctx: Ctx, off: int, n: int) -> dict:
    pid = F.pk(n, off + 1)
    sched = F.ts_window(ctx.rng("push_notifications", "scheduled_at", off), n, ctx.start,
                        ctx.days)
    delivered = F.bool_p(ctx.rng("push_notifications", "is_delivered", off), n, 0.88)
    # D-04：旧版 推送_N / 推送内容 N。文案按 push_type 分池——真实 APP 的提醒、社交、
    # 营销、交易、系统五类风格差得很远，混着发最显眼，所以 push_type 要先算出来。
    # 取值与权重取自卡片实测（2066/2033/1999/1967/1935，五类基本均匀）；旧的三值
    # marketing / transactional / system 是 database/*.sql 行内注释里的旧枚举。
    ptype = F.enum(ctx.rng("push_notifications", "push_type", off), n,
                   ["reminder", "social", "promotion", "order", "system"],
                   [2066, 2033, 1999, 1967, 1935])
    title = np.empty(n, dtype=object)
    content = np.empty(n, dtype=object)
    deep = np.empty(n, dtype=object)
    open_rate = np.zeros(n, dtype=np.float64)   # 不用 np.empty：漏了某类就该是 0，不是脏值
    for k, pairs in PUSH_COPY.items():
        m = ptype == k
        open_rate[m] = PUSH_OPEN_RATE[k]
        if not m.any():
            continue
        cnt = int(m.sum())
        # 标题、正文、落地页取**同一个下标**。各自独抽会配出「包裹已签收」＋「正在为你
        # 打包」这种同条推送自相矛盾，比占位符更糟。
        j = F.int_uniform(ctx.rng("push_notifications", f"copy_{k}", off), cnt,
                          0, len(pairs) - 1)
        title[m] = _pool([p[0] for p in pairs])[j]
        content[m] = _pool([p[1] for p in pairs])[j]
        deep[m] = _pool([p[2] for p in pairs])[j]
    # 打开率按 push_type 分（原来整表一个 0.21，见 PUSH_OPEN_RATE）。
    opened = delivered & (ctx.rng("push_notifications", "is_opened", off).random(n)
                          < open_rate)
    # 时间戳必须逐级递增：scheduled ≤ delivered ≤ opened。原来 delivered_at 和
    # opened_at **都从 sched 起算**、偏移区间还重叠（1~30 分钟 vs 2~1440 分钟），
    # 于是约三成已打开的推送「打开时间早于送达时间」。opened_at 要接在 delivered_at
    # 后面，所以这里先拿未置空的 deliv_ts。
    deliv_ts = _ts_after(ctx.rng("push_notifications", "delivered_at", off), sched,
                         1, 30, ctx.as_of_end)
    delivered_at = np.array(deliv_ts.astype(object), dtype=object)
    delivered_at[~delivered] = None
    # 只有营销型推送挂 campaign_id（见 MARKETING_PUSH_TYPES）。
    is_mk = np.isin(ptype, MARKETING_PUSH_TYPES)
    campaign = np.array(F.from_pool(ctx.rng("push_notifications", "cid", off), n,
                                    ctx.dim_ids["campaigns"]).astype(object), dtype=object)
    campaign[~is_mk] = None
    return {
        "push_id": pid,
        "user_id": F.fk_skewed(ctx.rng("push_notifications", "user_id", off), n, 1,
                               ctx.n("users")),
        "campaign_id": campaign,
        "push_type": ptype,
        "title": title,
        "content": content,
        "deep_link": deep,
        "scheduled_at": sched,
        "sent_at": sched,
        "delivered_at": delivered_at,
        "opened_at": _cond_ts(ctx.rng("push_notifications", "opened_at", off), deliv_ts,
                              opened, 2, 1440, ctx.as_of_end),
        "is_delivered": delivered,
        "is_opened": opened,
        "failure_reason": np.where(delivered, None, "token 失效"),
        "created_at": sched,
    }


# ---------------------------------------------------------------- 实验域

def build_ab_test_assignments(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["ab_test_assignments"]
    uid, tid = a[off:off + n], b[off:off + n]
    # variant 必须属于所分配的 test：从该 test 的 variant 里挑
    v_by_test = ctx.dim_ids["_variants_by_test"]
    r = ctx.rng("ab_test_assignments", "variant_id", off)
    variant = np.array([vs[r.integers(0, len(vs))] for vs in (v_by_test[t] for t in tid)],
                       dtype=np.int64)
    assigned = F.ts_window(ctx.rng("ab_test_assignments", "assigned_at", off), n,
                           ctx.start, ctx.days)
    return {
        "id": F.pk(n, off + 1),
        "user_id": uid,
        "test_id": tid,
        "variant_id": variant,
        "assigned_at": assigned,
        "first_exposure_at": F.ts_offset(
            ctx.rng("ab_test_assignments", "first_exposure_at", off), assigned,
            1, 4320, cap=ctx.as_of_end, null_p=0.12),
    }


# ---------------------------------------------------------------- 注册表

BUILDERS = {
    "products": build_products,
    "product_tags": build_product_tags,
    "users": build_users,
    "user_profiles": build_user_profiles,
    "user_devices": build_user_devices,
    "user_segment_members": build_user_segment_members,
    "sessions": build_sessions,
    "events": build_events,
    "page_views": build_page_views,
    "posts": build_posts,
    "post_likes": build_post_likes,
    "post_comments": build_post_comments,
    "post_shares": build_post_shares,
    "user_follows": build_user_follows,
    "user_messages": build_user_messages,
    "orders": build_orders,
    "order_items": build_order_items,
    "payments": build_payments,
    "subscriptions": build_subscriptions,
    "user_attributions": build_user_attributions,
    "user_coupons": build_user_coupons,
    "push_notifications": build_push_notifications,
    "ab_test_assignments": build_ab_test_assignments,
}

# 灌库顺序（外键依赖序）。其余维度表（categories/coupons/ad_creatives/…）仍由 load
# 脚本从 data/csv 直灌，只有商品域两张表移到这里生成——它们原先在 budget.py 里声明了
# SUB 缩放，却整类没有 builder（D-02），所以 180 万条订单明细只摊在 200 个 SKU 上。
LOAD_ORDER = [
    "products", "product_tags",      # order_items 的外键，必须在它之前
    "users", "user_profiles", "user_devices", "user_segment_members",
    "sessions", "events", "page_views",
    "posts", "post_likes", "post_comments", "post_shares", "user_follows",
    "user_messages",
    "orders", "order_items", "payments",
    "user_attributions", "user_coupons", "push_notifications",
    "ab_test_assignments",
    "subscriptions",          # 依赖 payments
]
