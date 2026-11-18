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

import budget
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
# 三类来源都要有，缺哪一类都会让某一整类分析没有输入：直接打开（NULL）、
# 站内跳转（本站页面）、站外引流（搜索引擎 / 社交平台 / 推送）。
#
# 「直接打开」2026-09-01 从空串改成 `None`，原因是**两种产出格式对空串的表达能力不同**：
# pgcsv 走 Postgres COPY 约定（`QUOTE_MINIMAL`，None 与 "" 都写成不带引号的空字段，
# COPY 一律读成 NULL），而 Parquet 保留 `''`。于是同一个 seed 产出的两份数据里
# `referrer` 一边是 NULL、一边是空串——Athena arm（装 CSV）上
# `WHERE referrer IS NULL` 命中 28%，Redshift arm（COPY Parquet）上命中 0 行，
# 两边都不报错。三种架构要吃同一份数据，就不能让"取值"依赖于中间格式的引号约定；
# 「没有来源页」这件事本来也就是 NULL 而不是空串。
# **不要把空串加回池子里**：加回来的代价是三个 arm 的口径重新分叉。
REFERRERS = [None, "https://m.example.com/home", "https://m.example.com/category",
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

# 用户名：中文昵称，前缀 × 主体 × 可选数字尾 = 32 × 32 × 24 = 24,576 种组合。
# 组合数 < 用户数（scale 427 有 213,500 个），所以这三个池只负责**多样性**，
# 唯一性由 F.combine_unique 的「先到先得 + 四位数后缀」负责（P1-6，见 _prep_users）。
# DDL 是 VARCHAR(50)，按**字节**算长度，中文 3 字节：最长组合 25 字节 + 后缀 5 字节，够。
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
# 本地部分 20 × 20 × 15 = 6,000 种，同样靠 F.combine_unique 补唯一性（P1-7）；
# 本地部分唯一就够了，域名照旧按份额权重独立抽。
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
# 号段 40 × 8 位尾 = 40 亿的空间，21 万行期望仍要撞 6 行左右，所以走 F.unique_digits
# （碰撞重抽）而不是 combine_unique——加后缀会破坏 11 位定长。
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
# 帖子说的是哪件东西。这一段原来是 32 个手写短语（吹风机 / 口红 / 猫粮 …），与
# `posts.product_ids` 毫无关系——而那一列当时**整列是空数组**（P1-10）：
# `knowledge/relationships.md:61` 声明了 products ↔ posts 的 N:N，卡片
# `domains/social/posts.md` 还写了一整段「商品种草效果分析」的 UNNEST 示例查询，
# 在那份数据上恒返回 0 行。空数组还有一层更麻烦的地方：`verify_constants.py` 的
# 全库常量普查**跳过所有数组列**（Trino 的 min/max 在数组上语义不清），所以云上那三列
# 空了多久都不会有任何一层报出来。
#
# 所以对象短语改成从 semantics.yaml 的**叶子类目反推**：量词 + 叶子名。这样每个帖子
# 天生知道自己说的是哪个品类，`product_ids` 就能指到那个品类里的真实 SKU。反过来
# （先随机挑 SKU 再写标题）做不到：那会造出「标题说吹风机、关联商品是猫粮」的同帖
# 自相矛盾，比整列空更糟——同 title/content 共用抽样那条注释里的理由。
#
# 键是量词、值是叶子类目名。`_post_objects()` 断言两侧叶子集合**完全相等**：
# semantics.yaml 增删类目时这里立刻停，而不是静默留下几个"从没人种草过"的品类
# （空类目在任何聚合结果里都只是**不出现**，正是 D-02 那一类看不见的缺陷）。
POST_MEASURE: dict[str, tuple[str, ...]] = {
    "台": ("冰箱", "洗衣机", "空调", "电视", "电饭煲", "电热水壶", "吹风机", "榨汁机",
           "油烟机", "燃气灶", "热水器", "消毒柜", "扫地机器人", "空气净化器", "加湿器",
           "笔记本", "台式机", "平板电脑", "智能音箱"),
    "部": ("智能手机", "老人机", "游戏手机"),
    "辆": ("婴儿推车", "学步车", "儿童自行车"),
    "支": ("口红", "眉笔", "牙膏", "洁面", "精华", "防晒"),
    "盒": ("面膜", "眼影", "腮红", "粉底", "饼干", "糖果", "蜜饯"),
    "瓶": ("洗发水", "沐浴露", "身体乳", "女士香水", "男士香水", "中性香水", "面霜",
           "食用油", "调味品", "清洁剂", "矿泉水", "果汁", "碳酸饮料"),
    "罐": ("婴儿奶粉", "儿童奶粉", "孕妇奶粉"),
    "袋": ("大米", "面粉", "坚果", "膨化食品", "咖啡", "茶饮", "垃圾袋"),
    "包": ("婴儿纸尿裤", "拉拉裤", "成人纸尿裤", "纸巾"),
    "箱": ("水果",),
    "份": ("蔬菜", "肉类", "海鲜"),
    "件": ("T恤", "衬衫", "外套", "卫衣", "上衣", "睡衣", "保暖内衣", "文胸", "运动T恤"),
    "条": ("裤子", "运动裤", "内裤", "毛巾", "连衣裙", "半身裙", "数据线"),
    "双": ("跑步鞋", "篮球鞋", "足球鞋", "健身鞋"),
    "套": ("床上用品", "餐具", "锅具", "刀具", "套装", "运动套装"),
    "副": ("耳机", "窗帘"),
    "把": ("拖把", "登山杖"),
    "张": ("瑜伽垫", "地毯"),
    "只": ("智能手表", "智能手环", "毛绒玩具"),
    "个": ("手机壳", "充电器", "收纳箱", "收纳袋", "衣架", "置物架", "保鲜盒", "帐篷",
           "睡袋", "户外背包", "哑铃", "跳绳", "拉力带", "益智玩具", "积木", "遥控玩具"),
}

# 话题标签池。真实平台的 tags 是「品类词 + 运营话题」的混合，所以每帖必带自己的
# 叶子类目名（让 tags 与 product_ids 指向同一件事），再从这里补 1~3 个。
# 元素里不能有逗号 / 空格 / 花括号 / 引号：CSV 走 Postgres 数组字面量 `{a,b}`，
# 装载侧是 `split(c, ',')`，`load.preflight` 直接拒绝带引号的元素。
POST_TAGS = ["好物推荐", "平价替代", "踩坑记录", "开箱实拍", "双十一囤货", "618好价",
             "新手入门", "回购清单", "性价比之王", "居家好物", "通勤日常", "送礼清单",
             "自用分享", "真实测评", "懒人必备", "学生党必备", "一人食", "小户型",
             "换季必备", "断舍离"]

# 每种内容形态带几个媒体文件、关联几件商品（左右闭区间）。形态差异来自卡片
# `domains/social/posts.md` 的 content_type 说明：image 是「1-9 张图片为主」、
# short_video 是「<60 秒」的单条视频、article 是「长内容，多段文字+图片」、
# review 是商品评测，必然关联被测的那件商品。
POST_MEDIA_N = {"article": (1, 5), "image": (1, 9), "short_video": (1, 1),
                "review": (1, 6)}
POST_PROD_N = {"article": (0, 3), "image": (0, 2), "short_video": (0, 1),
               "review": (1, 1)}
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


_POST_OBJ_CACHE: tuple[np.ndarray, np.ndarray] | None = None


def _post_objects() -> tuple[np.ndarray, np.ndarray]:
    """→ (对象短语数组, 对应叶子类目名数组)。两者同序，下标即"第几个对象"。

    校验两侧叶子集合完全相等（理由见 POST_MEASURE 上方）。memo 一份：
    每个分块都要用，而 SEM.load() 要读盘解析 yaml。
    """
    global _POST_OBJ_CACHE
    if _POST_OBJ_CACHE is not None:
        return _POST_OBJ_CACHE
    leaves = {p.rsplit(SEM.SEP, 1)[-1] for p in SEM.load().tree.leaf_paths}
    mapped = [lf for lfs in POST_MEASURE.values() for lf in lfs]
    if len(mapped) != len(set(mapped)):
        dup = sorted({x for x in mapped if mapped.count(x) > 1})
        raise ValueError(f"POST_MEASURE 里这些叶子类目挂了多个量词：{dup}")
    if set(mapped) != leaves:
        raise ValueError(
            f"POST_MEASURE 与 semantics.yaml 的叶子类目对不上——"
            f"没有量词的类目 {sorted(leaves - set(mapped))}；"
            f"semantics 里不存在的 {sorted(set(mapped) - leaves)}。"
            f"补全它，别删断言：漏掉的类目会变成"
            f"「一条种草内容都没有」，而空类目在聚合结果里只是不出现")
    phrase = [f"这{m}{lf}" for m, lfs in POST_MEASURE.items() for lf in lfs]
    leaf = [lf for lfs in POST_MEASURE.values() for lf in lfs]
    _POST_OBJ_CACHE = (np.array(phrase, dtype=object), np.array(leaf, dtype=object))
    return _POST_OBJ_CACHE


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

# 收货地址池。原来这一列是 `F.const(n, {"province": "广东", "city": "深圳"})`——85 万单
# 全在深圳，「各省 GMV 分布」恒得一行，而它**不是 NULL**，所以 verify_literals（JSONB 列
# 不在它的普查面里）和 verify_enums（这一列没有声明枚举）都拦不住。
# 只放 province / city / district 三个字段，**故意不放 receiver_name / phone**：
# L4 治理把 `users.phone` 排除在授权外，若把同一个手机号抄进这张表的 JSONB 里，
# 列级排除就被绕过去了——地址这一列是授权可读的。
SHIP_ADDRESSES = [
    {"province": "广东省", "city": "深圳市", "district": "南山区"},
    {"province": "广东省", "city": "广州市", "district": "天河区"},
    {"province": "上海市", "city": "上海市", "district": "浦东新区"},
    {"province": "北京市", "city": "北京市", "district": "朝阳区"},
    {"province": "浙江省", "city": "杭州市", "district": "西湖区"},
    {"province": "江苏省", "city": "南京市", "district": "鼓楼区"},
    {"province": "四川省", "city": "成都市", "district": "武侯区"},
    {"province": "湖北省", "city": "武汉市", "district": "洪山区"},
    {"province": "陕西省", "city": "西安市", "district": "雁塔区"},
    {"province": "福建省", "city": "厦门市", "district": "思明区"},
]
SHIP_ADDRESSES_W = [16, 13, 13, 12, 11, 9, 8, 7, 6, 5]

# 取消 / 退款原因池。这两列原来是 `np.where(mask, "用户取消"/"商品问题", None)`——
# 7.5 万条取消、4.2 万条退款各自只有一个值，「取消原因 TOP5」这类问题恒得一行。
# 同 remark：不带 ASCII 句点，避开 verify_literals 的词沙拉判据。
ORDER_CANCEL_REASONS = [
    "用户取消", "超时未支付", "不想要了", "地址填错了", "拍错了重新下单",
    "价格比别处贵", "缺货商家取消", "支付失败",
]
ORDER_CANCEL_REASONS_W = [28, 20, 14, 10, 9, 8, 6, 5]
ORDER_REFUND_REASONS = [
    "商品问题", "尺码不合适", "与描述不符", "签收时已破损", "物流太慢不想要了",
    "买重复了", "商家发错货", "无理由退货",
]
ORDER_REFUND_REASONS_W = [24, 18, 15, 12, 11, 8, 7, 5]

# 推送失败原因池。原来是 `np.where(delivered, None, "token 失效")`——51 万条失败全是
# 同一个原因，卡片只好写成「这个问题在这份数据上只有一个桶，做不出分布」。
# 值取自真实推送通道的常见失败类，仍让 token 失效占大头。
PUSH_FAILURE_REASONS = [
    "token 失效", "设备已卸载应用", "用户关闭了通知权限", "通道限流", "网络超时",
    "厂商通道返回未知错误",
]
PUSH_FAILURE_REASONS_W = [42, 20, 15, 10, 8, 5]

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
    # 帖子作者要读注册时间，而赞的时间下界要读（可能被后移过的）发帖时间，所以夹在中间
    _prep_post_owners(ctx)
    # 社交边的时间要读注册时间和发帖时间，所以在 users / posts / pairs 三者之后。
    _prep_edge_times(ctx)
    # user_profiles 必须在 orders 之前：orders 的金额要按每用户消费倍率缩放（D-01）。
    # 这条顺序就是修复本身——两者顺序反过来，画像与消费之间又没有边了。
    _prep_user_profiles(ctx)
    _prep_orders(ctx)
    # 必须在 _prep_order_items 之前：券决定订单头的优惠与 total_amount，明细按头缩放
    _prep_order_coupons(ctx)
    _prep_order_items(ctx)
    # 必须在 _prep_order_items 之后：products 的五个计数器从明细的实际售出回填。
    _prep_product_counters(ctx)
    _prep_payments(ctx)
    _prep_user_coupons(ctx)
    _prep_behavior(ctx)
    # 设备挂在会话之后：每个会话落在本人的某台设备上，设备的首末出现时间从会话回填
    _prep_devices(ctx)
    _prep_social(ctx)
    _prep_user_level(ctx)
    # 分群成员按 user_segments.rules_json 从事实推导，要读消费、活跃、画像、设备
    _prep_segments(ctx)

    _prep_ab_assignments(ctx)
    _prep_attributions(ctx)
    # 必须在 _prep_attributions 之后：成本表的 installs / cost 从「归因到本渠道、
    # 且当天注册的新客数」派生，那份真值在上一步才落地。
    _prep_channel_costs(ctx)


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

    # 叶子类目名 → 该类目下的 product_id。`build_posts` 用它把 `posts.product_ids`
    # 指到「标题说的那个品类」的真实 SKU（P1-10）。键用叶子**名**而不是 category_id：
    # 帖子那边只有一个品类词（`这台吹风机`），重名叶子（裤子/外套…）在这里合并成一个
    # 键，正是想要的——「这条裤子」不必区分它挂在女装还是男装下。
    by_lname: dict[str, list[int]] = {}
    for i, ln in enumerate(lname.tolist()):
        by_lname.setdefault(ln, []).append(int(pid[i]))
    ctx.cache["products_by_leaf"] = {k: np.array(v, dtype=np.int64)
                                     for k, v in by_lname.items()}

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
    """注册时间 / VIP / 三个唯一标识全局化。

    注册时间与 VIP：orders、sessions 都要满足「先注册后行为」，user_level 要读 VIP
    和注册期，所以这几列不能分块抽。

    username / email / phone（P1-6/7）：**唯一性是全列的性质，分块抽拼不出来**。
    这三列原来在 `build_users` 里逐块 `F.combine` / `F.from_pool`，组合空间分别是
    24,576 / 6,000 / 40×10⁸，而 scale 427 有 213,500 个用户——前两列的重复率
    88.5% / 72.7%，`COUNT(DISTINCT username)` 只有用户数的 11.5%。真实系统注册时
    就查重，所以这里换成 `F.combine_unique` / `F.unique_digits`（唯一性由它们保证
    并在里面自查）。判据在 selftest_closures.py::check_literals，负例
    literal-username-duplicated / literal-phone-duplicated 盯的正是旧形态。

    代价是这三列整列驻留内存：213,500 行 × 三列 U 串约 80MB，在 prepare_globals
    的量级里可以忽略。
    """
    nu = ctx.n("users")
    reg = F.ts_window(ctx.rng("users", "registered_at", "global"), nu,
                      ctx.start, ctx.days, trend=0.55)
    ctx.cache["users_registered_at"] = reg
    # 昵称的分隔符只能是 `_`：用 `.` 会造出「中文字 + ASCII 句点」，正好是
    # check_literals 认的 Faker 词沙拉指纹（WORD_SALAD_RE）。邮箱本地部分是拼音，
    # 没有这个问题，那边用 `.` 更像真邮箱。
    ctx.cache["users_username"] = F.combine_unique(
        ctx.rng("users", "username", "global"), nu,
        _pool(NICK_A), _pool(NICK_B), _pool(NICK_TAIL))
    local = F.combine_unique(ctx.rng("users", "email_local", "global"), nu,
                             _pool(PY_FAMILY), _pool(PY_GIVEN), _pool(MAIL_TAIL),
                             sep=".")
    # 本地部分唯一 ⇒ 整个邮箱唯一，所以域名照旧按份额权重独立抽。
    domain = F.enum(ctx.rng("users", "email_domain", "global"), nu,
                    MAIL_DOMAIN, MAIL_DOMAIN_W)
    ctx.cache["users_email"] = np.char.add(np.char.add(local, "@"),
                                           np.asarray(domain, dtype=str))
    ctx.cache["users_phone"] = F.unique_digits(
        ctx.rng("users", "phone", "global"), nu, _pool(PHONE_SEGMENTS), 8)
    ctx.cache["users_reg_day"] = (
        reg.astype("datetime64[D]") - ctx.start.astype("datetime64[D]")
    ).astype(np.int64)
    ctx.cache["users_is_vip"] = F.bool_p(ctx.rng("users", "is_vip", "global"), nu, 0.12)
    # 分群规则要读注册来源（推广注册用户），所以提到全局。随机流键沿用 builder 原来的
    # (users, 列, off=0)：users 远小于 SHARD_ROWS，原来就是单分片 off=0，取值一字不变。
    assert nu <= budget.SHARD_ROWS, "users 超过单分片，registration_source 的随机流身份会变"
    ctx.cache["users_reg_source"] = F.enum(
        ctx.rng("users", "registration_source", 0), nu,
        ["referral", "organic", "huawei_store", "web",
         "ad_campaign", "wechat_mini", "app_store", "google_play"],
        [73, 68, 65, 62, 61, 59, 57, 55])
    ctx.cache["users_status"] = F.enum(ctx.rng("users", "status", 0), nu,
                                       ["active", "inactive", "deleted", "suspended"],
                                       [376, 68, 30, 26])


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

    # 城市同理提到全局（一线城市用户分群要读），随机流键沿用 builder 的 off=0
    ci = ctx.rng("user_profiles", "city", 0).choice(
        len(CITIES), size=nu, replace=True,
        p=np.array([c[2] for c in CITIES], float) / sum(c[2] for c in CITIES))
    ctx.cache["user_profiles"] = {"age": age, "gender": gender,
                                  "income_level": income, "occupation": occupation,
                                  "city_idx": ci}
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
    # 生命周期时间戳全局算一次（2026-11 数据普查）：原来在 build_orders 里逐块抽，
    # payments 又按自己的流另抽一份 paid_at / refunded_at，同一笔支付两张表的时间
    # 99.99% 对不上（相差可达 ±3.4 小时）。现在两张表读同一份。
    life_status = status
    pl = ctx.cache["orders"]["placed_at"]
    ctx.cache["orders_life"] = {
        "paid_at": _cond_ts(ctx.rng("orders", "paid_at", "global"), pl,
                            np.isin(life_status, ["paid", "shipped", "delivered", "refunded"]),
                            1, 240, ctx.as_of_end),
        "shipped_at": _cond_ts(ctx.rng("orders", "shipped_at", "global"), pl,
                               np.isin(life_status, ["shipped", "delivered"]), 240, 2880, ctx.as_of_end),
        "delivered_at": _cond_ts(ctx.rng("orders", "delivered_at", "global"), pl,
                                 life_status == "delivered", 2880, 10080, ctx.as_of_end),
        "cancelled_at": _cond_ts(ctx.rng("orders", "cancelled_at", "global"), pl,
                                 life_status == "cancelled", 5, 1440, ctx.as_of_end),
        "refunded_at": _cond_ts(ctx.rng("orders", "refunded_at", "global"), pl,
                                life_status == "refunded", 1440, 20160, ctx.as_of_end),
    }

    # 每单件数：长尾、至少 1 行。orders.item_count 必须从这里读——
    # 旧版 builder 另抽了一份 int_weighted，70% 的单与明细行数不符（审计 L4.2）。
    cnt = F.children_per_parent(ctx.rng("order_items", "counts"), no,
                                ctx.n("order_items"), min_count=1)
    ctx.cache["orders_item_count"] = cnt
    ctx.cache["order_items_order_id"] = F.expand_ids(F.pk(no, 1), cnt)

    # 哪些订单用了券，全局定下来：user_coupons 的核销行要反向对齐到 (user, coupon, order)。
    # **具体是哪张券在这里不定**，由 _prep_user_coupons 回写 ctx.cache["orders_coupon_id"]：
    # 用户核销的券必须是他持有的那一张，而"持有"要受 coupons.per_user_limit 约束
    # （P1-11）。方向只能是 user_coupons → orders：在这里先按 150 张券均匀抽的话，
    # 一个用户在同一张限领 1 张的券上下 26 单也照样"合法"——实测就是这个形态。
    # 2026-11：这里只定「想用券」的候选单；能不能用上、用哪张，由 _prep_order_coupons
    # 按券的有效期 / 门槛 / 每人限领判定，用不上的候选单回退为不用券。
    has_cp = ctx.rng("orders", "coupon_id", "global").random(no) < 0.38
    ctx.cache["orders_coupon_rows"] = np.flatnonzero(has_cp)      # 0-based


def _coupon_arrays(ctx: Ctx) -> dict:
    """150 张券的属性按 coupon_id 顺序排成数组（向量化判定用）。"""
    if "_coupon_arrays" in ctx.cache:
        return ctx.cache["_coupon_arrays"]
    info, lim = ctx.dim_ids["_coupon_info"], ctx.dim_ids["_coupon_limits"]
    cid = np.array(sorted(info), dtype=np.int64)
    a = {
        "id": cid,
        "type": np.array([info[c]["type"] for c in cid], dtype=object),
        "value": np.array([info[c]["value"] for c in cid], float),
        "min": np.array([info[c]["min"] for c in cid], float),
        "max": np.array([np.nan if info[c]["max"] is None else info[c]["max"] for c in cid], float),
        "valid_days": np.array([info[c]["valid_days"] for c in cid], np.int64),
        "start": np.array([info[c]["start"] for c in cid], dtype="datetime64[s]"),
        "end": np.array([info[c]["end"] for c in cid], dtype="datetime64[s]"),
        "limit": np.array([lim[int(c)] for c in cid], np.int64),
    }
    ctx.cache["_coupon_arrays"] = a
    return a


def _coupon_discount(ca: dict, goods: np.ndarray, ship: np.ndarray) -> tuple:
    """每单 × 每张券的 (优惠额, 商品总额)，实付 actual 固定不变（GMV 不漂移）。

    goods = actual − shipping_fee（扣掉运费后实付的商品钱）。三类券：
      fixed_amount   优惠 = 面值；商品总额 = goods + 面值
      percentage     discount_value 是「减百分之几」（20 = 8 折）：商品总额 = goods / (1 − v%)，
                     优惠 = 总额 − goods；超过 max_discount 时封顶，总额 = goods + 封顶额
      free_shipping  优惠 = 运费；商品总额 = goods + 运费（= actual）
    返回的两个矩阵形状都是 (单数, 券数)，按分取整。
    """
    g = goods[:, None]
    typ, v, mx = ca["type"][None, :], ca["value"][None, :], ca["max"][None, :]
    pv = np.where(typ == "percentage", v, 0.0)          # 只有百分比券走这条，别的券面值可能是 100
    pct_total = g / (1.0 - pv / 100.0)
    pct_disc = pct_total - g
    pct_disc = np.where(np.isnan(mx), pct_disc, np.minimum(pct_disc, mx))
    disc = np.where(typ == "fixed_amount", v,
                    np.where(typ == "percentage", pct_disc, ship[:, None] + 0 * v))
    disc = np.round(disc, 2)
    return disc, np.round(g + disc, 2)


def _prep_order_coupons(ctx: Ctx) -> None:
    """给候选用券单分配一张**用得上**的券，并由券反推订单头的优惠与商品总额（2026-11）。

    原来订单的 discount_amount 按 0/5/10/20% 独立抽（与用没用券无关），券号又在
    _prep_user_coupons 里从持有槽位随机配：37% 的用券单不满门槛、56% 不在券有效期内、
    max_discount 封顶被突破，没用券的单也有优惠。

    现在：实付 actual_amount、运费不动（GMV 及全部金标不漂移），对每张候选单判定
    「下单时刻在券有效期内 ∧ 商品总额 ≥ 门槛 ∧ 每人限领未用完 ∧（包邮券要有运费）」，
    在用得上的券里均匀随机挑一张；用不上任何券的候选单回退为不用券。
    订单头 discount_amount = 券的优惠（不用券为 0），total_amount = actual − 运费 + 优惠，
    恒等式 actual = total − discount + shipping 照旧精确成立。order_items 的
    discount_amount 是单品级促销，与订单头的券优惠是两层，不相加也不相等。
    """
    og = ctx.cache["orders"]
    ca = _coupon_arrays(ctx)
    nc = len(ca["id"])
    cand = ctx.cache["orders_coupon_rows"]
    uid = ctx.cache["orders_user_id"][cand]
    placed = og["placed_at"][cand].astype("datetime64[s]")
    ship = og["shipping_fee"][cand].astype(float)
    goods = np.round(og["actual_amount"][cand] - ship, 2)

    disc_m = np.empty((len(cand), nc), np.float32)
    elig = np.empty((len(cand), nc), bool)
    CH = 50_000
    for a in range(0, len(cand), CH):
        b = min(a + CH, len(cand))
        d, t = _coupon_discount(ca, goods[a:b], ship[a:b])
        disc_m[a:b] = d
        p = placed[a:b, None]
        elig[a:b] = ((ca["start"][None, :] <= p) & (p <= ca["end"][None, :])
                     & (t >= ca["min"][None, :]) & (d > 0))

    # 逐轮挑券：每轮每张未定单在剩余可用券里随机挑一张；同一 (用户, 券) 上按下单
    # 先后排队，超出每人限领的退回、把这张券从它的可选里划掉，下一轮再挑
    r = ctx.rng("orders", "coupon_pick", "global")
    chosen = np.full(len(cand), -1, np.int64)
    used = {}                                            # (uid*1000+券下标) → 已用张数
    todo = np.flatnonzero(elig.any(axis=1))
    for _ in range(12):
        if len(todo) == 0:
            break
        sc = r.random((len(todo), nc)).astype(np.float32)
        sc[~elig[todo]] = -1.0
        pick = sc.argmax(axis=1)
        ok_any = sc[np.arange(len(todo)), pick] >= 0
        todo, pick = todo[ok_any], pick[ok_any]
        key = uid[todo] * 1000 + pick
        o = np.lexsort((placed[todo], key))
        ks = key[o]
        rank = np.arange(len(ks)) - np.searchsorted(ks, ks, side="left")
        prev = np.array([used.get(int(k), 0) for k in ks], np.int64)
        acc = prev + rank < ca["limit"][pick[o]]
        chosen[todo[o[acc]]] = pick[o[acc]]
        uk, uc = np.unique(ks[acc], return_counts=True)
        for k, c in zip(uk.tolist(), uc.tolist()):
            used[k] = used.get(k, 0) + c
        rej = todo[o[~acc]]
        elig[rej, pick[o[~acc]]] = False
        todo = rej[elig[rej].any(axis=1)]

    got = chosen >= 0
    rows = cand[got]
    disc = np.zeros(ctx.n("orders"))
    disc[rows] = disc_m[np.flatnonzero(got), chosen[got]].astype(float)
    disc = np.round(disc, 2)
    total = np.round(og["actual_amount"] - og["shipping_fee"] + disc, 2)
    assert np.allclose(total - disc + og["shipping_fee"], og["actual_amount"], atol=0.005)
    og["discount_amount"] = disc
    og["total_amount"] = total
    ctx.cache["orders_coupon_rows"] = rows
    coupon = np.full(ctx.n("orders"), None, dtype=object)
    coupon[rows] = ca["id"][chosen[got]]
    ctx.cache["orders_coupon_id"] = coupon
    ctx.cache["orders_coupon_idx"] = chosen[got]          # 券下标（与 rows 一一对应）


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
    rate = F.enum(ctx.rng("order_items", "disc", "global"), ni,
                  [0.0, 0.05, 0.15], [70, 20, 10]).astype(float)
    total = ctx.cache["orders"]["total_amount"]
    head_c = np.round(total * 100).astype(np.int64)

    # —— 选 SKU：按「价格贴合」挑，成交单价 = 商品标价（2026-11 数据普查）——
    # 原来 unit_price 是独立抽的对数正态再按单缩放，与所购商品的 price 相关系数 ≈ 0
    # （¥12,359 的商品按 ¥3 成交、¥4.9 的商品按 ¥800 成交）。现在：先把订单头的商品总额
    # 按权重拆到各行，得到每行的目标标价 P* = 行份额 /（件数 ×（1 − 单品促销率）），
    # 再在标价 ∈ [P*, 1.10·P*] 的商品里按热度加权挑一个（窗口为空就取标价不低于 P* 的
    # 最便宜那个）。头的 total_amount 不动，行实付按单缩放后 ≤ 标价 × 件数，差额就是
    # 单品优惠 discount_amount（非负）。
    p = ctx.cache["products"]
    price_c_all = np.round(np.asarray(p["price"], float) * 100).astype(np.int64)
    np_ = len(price_c_all)
    order_p = np.argsort(price_c_all, kind="stable")
    price_sorted = price_c_all[order_p]
    pop = ctx.rng("order_items", "popularity", "global").lognormal(0.0, 1.2, np_)
    cw = np.concatenate(([0.0], np.cumsum(pop[order_p])))

    split = ctx.rng("order_items", "split", "global").lognormal(0.0, 0.6, ni)
    share = split / np.repeat(np.add.reduceat(split, starts), cnt)
    gross_c = np.repeat(head_c, cnt) * share                     # 行份额（分）
    pmax = float(price_sorted[-1])
    big = gross_c / (qty * (1.0 - rate)) > 0.8 * pmax              # 单价放不下就加件数
    qty = np.where(big, np.ceil(gross_c / (0.8 * pmax)).astype(np.int64), qty)
    target = gross_c / (qty * (1.0 - rate))
    lo = np.searchsorted(price_sorted, np.ceil(target), side="left")
    hi = np.searchsorted(price_sorted, target * 1.10, side="right")
    lo = np.minimum(lo, np_ - 1)
    hi = np.maximum(hi, lo + 1)
    u = ctx.rng("order_items", "product_id", "global").random(ni)
    pos = np.searchsorted(cw, cw[lo] + u * (cw[hi] - cw[lo]), side="right") - 1
    pos = np.clip(pos, lo, hi - 1)
    prod_idx = order_p[pos]
    unit_c = price_c_all[prod_idx]

    # 标价 × 件数之和不足头总额的单（窗口为空、取了更便宜的 SKU）：给该单最贵的一行加件数补足
    for _ in range(3):
        list_c = unit_c * qty
        short = head_c - np.add.reduceat(list_c, starts)
        bad = np.flatnonzero(short > 0)
        if len(bad) == 0:
            break
        seg = [np.arange(starts[o], ends[o] + 1) for o in bad]
        top = np.array([g[np.argmax(unit_c[g])] for g in seg], np.int64)
        qty[top] += -(-short[bad] // unit_c[top])
    list_c = unit_c * qty
    assert (np.add.reduceat(list_c, starts) >= head_c).all(), "有订单的标价合计仍低于头总额"

    # 行实付：按「标价 ×（1 − 单品促销率）」的比例分摊头总额，末行吸收舍入差
    raw = list_c * (1.0 - rate)
    act_c = np.floor(raw * np.repeat(head_c / np.add.reduceat(raw, starts), cnt)).astype(np.int64)
    act_c = np.minimum(act_c, list_c)
    act_c[ends] += head_c - np.add.reduceat(act_c, starts)
    # 末行吸收后若超过自身标价，把超出部分挪回同单其它行（仍不超各自标价）
    over = np.flatnonzero(act_c[ends] > list_c[ends])
    for o in over:
        e = ends[o]
        extra = act_c[e] - list_c[e]
        act_c[e] = list_c[e]
        for j in range(starts[o], e):
            room = list_c[j] - act_c[j]
            take = min(room, extra)
            act_c[j] += take
            extra -= take
            if extra == 0:
                break
        assert extra == 0, "订单头总额超过标价合计"
    assert (act_c > 0).all(), "明细出现非正金额，检查件数分布与订单头下限"
    assert (act_c <= list_c).all()

    ctx.cache["items"] = {
        "product_idx": prod_idx,
        "quantity": qty,
        "unit_price": unit_c / 100.0,                         # = products.price
        "discount_amount": (unit_c * qty - act_c) / 100.0,   # 公式精确成立，非负
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
    """领券持有 + 核销闭环，整列全局产。

    ## 核销闭环（审计 L4.7，v2 已修）

    orders 里带 coupon_id 的每一单，反向生成一行 status='used' 的券，
    (user_id, coupon_id, order_id) 三元对齐、used_at = 下单时刻；其余行只有
    unused / expired，且 expired 由 expire_at 是否已过窗末决定，不再随机抽。
    旧版三个数字互相打架：orders 说 32.4 万单用券、user_coupons 说 485 万张已用、
    还挂到了 85 万个不同订单上（平均 5.7 张/单）。

    ## 每人限领（P1-11，这一轮修）

    `coupons.per_user_limit`（取值 {1,2,3,5}，150 张券合计 412）是维表里明写、卡片里
    明写、DDL 注释里明写的约束，而 v2 完全没管它：券号在 build 里按 150 张均匀抽，
    实测 10.0% 的 (user, coupon) 配对越限，最糟的一行是**限领 1 张、发了 26 张**。
    v1 的 `scripts/generators/marketing_domain.py` 是靠逐行拒绝采样守住这条的，
    向量化重写时丢了——和 device_brand/device_model 配对丢失是同一类回归。

    修法是把"持有"建模成**槽位**：券 c 贡献 limit(c) 个槽位，全库 412 个。每个用户
    从这 412 个槽位里**无放回**抽 cnt 个，于是同一张券最多拿到 limit(c) 次——限领
    成了算术保证，不是概率。抽法是"给每个用户抽 412 个随机数、取最小的 cnt 个"，
    等价于无放回抽样且无偏；一次性做要 21 万 × 412 的随机矩阵（约 700MB），所以按
    用户分块。

    两处连带后果：

    1. **每个用户最多持有 412 张券**。fk_skewed 的集中度会让头部用户想要 3,331 张
       （实测 scale 427），所以超出的部分要搬到还有余量的用户身上：1,339 个用户溢出
       31.6 万行，占全表 3.3%。搬运按剩余余量加权，不是平摊——平摊会在"恰好 412 张"
       上堆出一个可见的尖峰。
    2. **订单的券号由这里回写**，方向从 orders → user_coupons 反了过来。用户核销的券
       必须是他持有的那一张，反方向做不到（见 _prep_orders 里那段注释）。

    内存（scale 427，970 万行）：user_id + coupon_id 两条 int64 约 155MB，
    槽位下标 int16 约 19MB。
    """
    n_uc, nu = ctx.n("user_coupons"), ctx.n("users")
    ca = _coupon_arrays(ctx)
    nc = len(ca["id"])
    # 槽位表：券 k（下标）连续出现 limit(k) 次
    slot_k = np.repeat(np.arange(nc, dtype=np.int64), ca["limit"])
    slot_start = np.concatenate(([0], np.cumsum(ca["limit"])[:-1]))
    cap = len(slot_k)

    # 核销段：每个用券订单一行，券 = _prep_order_coupons 分配的那张（已守住每人限领）
    cp_rows = ctx.cache["orders_coupon_rows"]
    o_uid = ctx.cache["orders_user_id"][cp_rows]
    o_k = ctx.cache["orders_coupon_idx"]
    n_used = len(cp_rows)
    if n_used > n_uc:
        raise ValueError(f"用券订单 {n_used} 超过 user_coupons 行预算 {n_uc}，先调 budget")
    used_cnt = np.bincount(o_uid, minlength=nu + 1)[1:]
    # 核销行占用的槽位：(用户, 券) 组内第 r 张占该券的第 r 个槽位
    key = o_uid * 1000 + o_k
    o = np.argsort(key, kind="stable")
    ks = key[o]
    rank = np.arange(n_used) - np.searchsorted(ks, ks, side="left")
    used_slot = np.empty(n_used, np.int64)
    used_slot[o] = slot_start[o_k[o]] + rank
    assert (rank < ca["limit"][o_k[o]]).all(), "核销行越过每人限领"

    # 每个用户的可领槽位：券的结束时间不早于他的注册时间（注册前就结束的券领不到——
    # 原来领券时刻只看注册，12.9% 的领券早于券开始、大量晚于券结束）
    reg = ctx.cache["users_registered_at"].astype("datetime64[s]")
    ends_sorted = np.sort(ca["end"][slot_k])
    feas = cap - np.searchsorted(ends_sorted, reg, side="left")
    room_u = feas - used_cnt
    assert (room_u >= 0).all(), "有用户的核销张数超过他可领的槽位"

    want = np.bincount(F.fk_skewed(ctx.rng("user_coupons", "user_id", "global"),
                                   n_uc - n_used, 1, nu), minlength=nu + 1)[1:]
    g = np.minimum(want, room_u)
    spill = int((want - g).sum())
    sr = ctx.rng("user_coupons", "spill", "global")
    for _ in range(32):
        if spill == 0:
            break
        room = (room_u - g).astype(float)
        pick = sr.choice(nu, size=spill, replace=True, p=room / room.sum())
        add = np.minimum(np.bincount(pick, minlength=nu), room_u - g)
        g += add
        spill -= int(add.sum())
    if spill:
        raise ValueError(f"槽位余量搬运 32 轮后仍剩 {spill} 行安置不下")
    n_free = n_uc - n_used
    assert int(g.sum()) == n_free

    # 每个用户在「可领 ∧ 未被核销占用」的槽位里无放回抽 g[u] 个
    free_start = np.concatenate(([0], np.cumsum(g)))
    free_slot = np.empty(n_free, dtype=np.int16)
    slot_end = ca["end"][slot_k]
    used_u_order = np.argsort(o_uid, kind="stable")
    used_u_sorted = o_uid[used_u_order]
    r = ctx.rng("user_coupons", "slots", "global")
    CHUNK = 20_000
    for a in range(0, nu, CHUNK):
        b = min(a + CHUNK, nu)
        k = g[a:b]
        if int(k.max()) == 0:
            continue
        sc = r.random((b - a, cap))
        sc[slot_end[None, :] < reg[a:b, None]] = 2.0          # 注册前已结束的券
        lo, hi = np.searchsorted(used_u_sorted, [a + 1, b + 1])
        jj = used_u_order[lo:hi]
        sc[o_uid[jj] - 1 - a, used_slot[jj]] = 2.0              # 已被核销行占用
        rank_m = np.argsort(sc, axis=1)
        free_slot[free_start[a]:free_start[b]] = rank_m[np.arange(cap)[None, :] < k[:, None]]
    free_uid = np.repeat(np.arange(1, nu + 1, dtype=np.int64), g)
    shuf = ctx.rng("user_coupons", "shuffle", "global").permutation(n_free)

    ctx.cache["uc_user_id"] = np.concatenate([o_uid, free_uid[shuf]])
    ctx.cache["uc_coupon_idx"] = np.concatenate([o_k, slot_k[free_slot[shuf]]])
    ctx.cache["uc_coupon_id"] = ca["id"][ctx.cache["uc_coupon_idx"]]
    ctx.cache["uc_n_used"] = n_used
    _prep_coupon_times(ctx)


def _prep_coupon_times(ctx: Ctx) -> None:
    """领券 / 到期 / 状态，以及 coupons 维表的发放量回填。

    核销行：received ∈ [max(注册, 券开始, 下单 − valid_days), 下单)，所以下单时券既已
    领到、也没过期；used_at = 下单时刻。
    未用行：received ∈ [max(注册, 券开始), min(券结束, 窗末)]。
    expire_at = received + valid_days（原来恒为 +30 天，与券的 valid_days 无关）；
    status：used / 到期早于快照时刻（锚点日末）为 expired / 否则 unused。
    """
    ca = _coupon_arrays(ctx)
    uid = ctx.cache["uc_user_id"]
    k = ctx.cache["uc_coupon_idx"]
    n_used = ctx.cache["uc_n_used"]
    n = len(uid)
    reg = ctx.cache["users_registered_at"][uid - 1].astype("datetime64[s]")
    vd = ca["valid_days"][k].astype("timedelta64[D]").astype("timedelta64[s]")
    c_st, c_en = ca["start"][k], ca["end"][k]
    anchor = ctx.as_of_end.astype("datetime64[s]")
    r = ctx.rng("user_coupons", "recv", "global")
    u = r.random(n)

    placed = np.full(n, anchor)
    placed[:n_used] = ctx.cache["orders"]["placed_at"][ctx.cache["orders_coupon_rows"]].astype("datetime64[s]")
    one = np.timedelta64(1, "s")
    lo_u = np.maximum(np.maximum(reg, c_st), placed - vd + one)
    hi_u = placed - one
    lo_f = np.maximum(reg, c_st)
    hi_f = np.minimum(c_en, anchor)
    used = np.arange(n) < n_used
    lo = np.where(used, lo_u, lo_f)
    hi = np.maximum(np.where(used, hi_u, hi_f), lo)
    span = (hi - lo).astype("timedelta64[s]").astype(np.int64)
    # 领券偏向靠近下单 / 靠近券上线：核销行取 u^0.5（贴近下单），未用行取 u^1.8（贴近起点）
    w = np.where(used, u ** 0.5, u ** 1.8)
    recv = lo + (w * span).astype(np.int64).astype("timedelta64[s]")
    expire = recv + vd
    assert (recv >= reg).all() and (recv >= c_st).all() and (recv <= anchor).all()
    assert (expire[used] >= placed[used]).all() and (recv[used] < placed[used]).all()
    ctx.cache["uc_received_at"] = recv
    ctx.cache["uc_expire_at"] = expire
    ctx.cache["uc_status"] = np.where(used, "used",
                                      np.where(expire < anchor, "expired", "unused"))
    ctx.cache["coupons_issued"] = np.bincount(k, minlength=len(ca["id"]))


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
    reg_rep = np.repeat(ctx.cache["users_registered_at"], per_user).astype("datetime64[s]")
    start_ts = np.maximum(start_ts, reg_rep + np.timedelta64(60, "s"))
    # 每用户的首会话就是注册那一次会话：从注册时刻开始（register 事件挂在它上面，
    # 原来首会话的开始时间是随机抽的，register 事件 4.9% 落在会话开始之前）
    start_ts[first_sess[1:] - 1] = reg_rep[first_sess[1:] - 1]
    start_ts = np.minimum(start_ts, win_end - np.timedelta64(1, "s"))
    dur = F.int_weighted(ctx.rng("sessions", "duration_seconds", "global"), ns,
                         [12, 45, 120, 300, 900, 2400], [18, 22, 24, 20, 12, 4])
    # 会话不越过窗末。原来下限钳在 12 秒，窗末最后几秒开始的会话会越界，其上的
    # page_view 被钳回窗末后反而早于会话开始（审计 235 行）
    dur = np.minimum(dur, (win_end - start_ts).astype("timedelta64[s]").astype(np.int64) - 1)

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
    life = ctx.cache["orders_life"]
    # register：每用户一条，挂本人首会话（从注册时刻开始），时刻 = 注册时刻
    res_uid = [np.arange(1, nu + 1, dtype=np.int64)]
    res_sess = [first_sess[1:]]
    res_time = [reg_ts]
    res_name = [np.full(nu, _idx_of("register"), np.int16)]
    # purchase：每有效单一条，时刻 = 支付时刻 paid_at（与 orders / payments 同一份）；
    # use_coupon：每个用券订单一条，时刻 = 下单时刻。这两类是服务端事件，所属会话取
    # 「该用户覆盖此刻的会话」，没有就是 NULL——原来一律挂在下单人的**首会话**上，
    # 时刻是下单后随机 1–4 小时，13% 的事件落在所属会话窗口之外。
    o_uid = ctx.cache["orders_user_id"][valid_idx]
    p_time = np.minimum(life["paid_at"][valid_idx].astype("datetime64[s]"),
                        win_end - np.timedelta64(1, "s"))
    res_uid.append(o_uid)
    res_time.append(p_time)
    res_name.append(np.full(len(valid_idx), _idx_of("purchase"), np.int16))
    c_uid = ctx.cache["orders_user_id"][cp_rows]
    c_time = og["placed_at"][cp_rows].astype("datetime64[s]")
    res_uid.append(c_uid)
    res_time.append(c_time)
    res_name.append(np.full(len(cp_rows), _idx_of("use_coupon"), np.int16))

    t0 = ctx.start.astype("datetime64[s]")
    s_sec = (start_ts - t0).astype("timedelta64[s]").astype(np.int64)
    e_sec = s_sec + dur
    K = np.int64(1) << 33
    order = np.lexsort((s_sec, sess_user))
    keys = sess_user[order] * K + s_sec[order]

    def _covering(uid: np.ndarray, t: np.ndarray) -> np.ndarray:
        """该用户在 t 时刻所处的会话（session_id），不在任何会话内为 -1。"""
        ts = (t - t0).astype("timedelta64[s]").astype(np.int64)
        j = np.searchsorted(keys, uid * K + ts, side="right") - 1
        jj = order[np.maximum(j, 0)]
        ok = (j >= 0) & (sess_user[jj] == uid) & (e_sec[jj] >= ts)
        return np.where(ok, jj + 1, -1)

    res_sess.append(_covering(o_uid, p_time))
    res_sess.append(_covering(c_uid, c_time))

    ev_sess = np.concatenate([g_sess] + res_sess)
    ev_time = np.concatenate([g_time.astype("datetime64[s]")]
                             + [t.astype("datetime64[s]") for t in res_time])
    ev_time = np.minimum(ev_time, win_end - np.timedelta64(1, "s"))
    # purchase 段在 events 数组里的位置：泛化段 n_generic 行 + register 段 nu 行之后，
    # 第 k 行对应 valid_idx[k] 那一单。记下来给 build_events 填 properties——
    # `{"amount", "order_id"}` 两个键因此指向**真的那一单**，而不是另抽一份
    # （v1 就是另抽的：order_id 是不接任何表的 12 位随机数，卡片专门写了一段
    # ⚠️ 说它 JOIN 不上）。order_id 用 valid_idx + 1 是因为 build_orders 的主键是
    # `F.pk(no, 1)`，即 1..no 与行下标一一对应。
    ctx.cache["events_purchase"] = {
        "lo": n_generic + nu,
        "order_id": (valid_idx + 1).astype(np.int64),
        "amount": og["actual_amount"][valid_idx],
    }
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
    ev_cnt = np.bincount(ev_sess[ev_sess > 0], minlength=ns + 1)[1:]
    pv_cnt = np.bincount(pv_sess, minlength=ns + 1)[1:]
    ctx.cache["sessions"] = {
        "user_id": sess_user, "start_time": start_ts,
        "duration_seconds": dur, "event_count": ev_cnt,
        "page_view_count": pv_cnt, "is_bounce": pv_cnt <= 1,
    }


def _prep_devices(ctx: Ctx) -> None:
    """设备归属与首末出现时间从会话推导（2026-11 数据普查）。

    原来 user_devices 的 user_id 是独立抽的、首次出现时间全窗随机（48% 早于注册），
    is_primary 按 62% 独立抽（每用户主设备数 0~5 台都有），而 sessions / events 的
    device_id = opaque_id(user_id)——会话挂的那台设备在 user_devices 里属于**另一个用户**。

    现在：每用户至少一台设备、恰好一台主设备（块内第一台）；每个会话落在本人的某台
    设备上（70% 主设备）；注册会话固定在主设备上；有会话的设备 first_seen / last_seen =
    其上会话的最早开始 / 最晚结束，没有会话的设备在注册之后出现。
    """
    nu, nd = ctx.n("users"), ctx.n("user_devices")
    assert nd >= nu, f"user_devices({nd}) 少于 users({nu})，做不到每用户至少一台设备"
    per = F.children_per_parent(ctx.rng("user_devices", "counts"), nu, nd, min_count=1)
    d_uid = F.expand_ids(np.arange(1, nu + 1, dtype=np.int64), per)
    first_dev = np.concatenate(([0], np.cumsum(per)[:-1])).astype(np.int64)   # 0-based
    is_primary = np.zeros(nd, bool)
    is_primary[first_dev] = True

    sg = ctx.cache["sessions"]
    su = sg["user_id"]
    ns = len(su)
    r = ctx.rng("sessions", "device")
    other = np.floor(r.random(ns) * per[su - 1]).astype(np.int64)
    pick = np.where(r.random(ns) < 0.7, 0, other)
    # sessions 按用户成块排列（_prep_behavior 的 expand_ids），块首 = 注册会话
    first_sess = np.concatenate(([0], np.cumsum(np.bincount(su, minlength=nu + 1)[1:])[:-1]))
    pick[first_sess] = 0                                   # 注册会话在主设备上
    s_dev = first_dev[su - 1] + pick                       # 0-based 设备行
    sg["device_row"] = s_dev

    t0 = ctx.start.astype("datetime64[s]")
    st = (sg["start_time"].astype("datetime64[s]") - t0).astype("timedelta64[s]").astype(np.int64)
    en = st + sg["duration_seconds"].astype(np.int64)
    big = np.iinfo(np.int64).max
    fs = np.full(nd, big, np.int64)
    np.minimum.at(fs, s_dev, st)
    ls = np.full(nd, -1, np.int64)
    np.maximum.at(ls, s_dev, en)
    has = fs != big
    reg = ctx.cache["users_registered_at"][d_uid - 1].astype("datetime64[s]")
    idle_first = _ts_since(ctx.rng("user_devices", "first_seen_at", "global"), reg,
                           ctx.as_of_end, 2.0).astype("datetime64[s]")
    first = np.where(has, t0 + np.where(has, fs, 0).astype("timedelta64[s]"), idle_first)
    idle_last = F.ts_offset(ctx.rng("user_devices", "last_seen_at", "global"), first,
                            60, 80 * 1440, cap=ctx.as_of_end).astype("datetime64[s]")
    last = np.where(has, np.minimum(t0 + ls.clip(0).astype("timedelta64[s]"),
                                    ctx.as_of_end.astype("datetime64[s]")), idle_last)
    ctx.cache["user_devices"] = {
        "user_id": d_uid, "is_primary": is_primary,
        # 权重用现行库实测行数（744 行：android 372 / ios 245 / web 83 / mini_program 44）
        "device_type": F.enum(ctx.rng("user_devices", "device_type", "global"), nd,
                              ["android", "ios", "web", "mini_program"], [372, 245, 83, 44]),
        "first_seen_at": first.astype("datetime64[s]"),
        "last_seen_at": last.astype("datetime64[s]"),
        "primary_row": first_dev,
    }


# 一线城市用户分群的城市清单，与 data/csv/user_segments.csv 第 6 条 rules_json 一致
SEG_FIRST_TIER = ("北京", "上海", "广州", "深圳")


def _prep_segments(ctx: Ctx) -> None:
    """分群成员按 user_segments.rules_json 从事实推导（2026-11 数据普查）。

    原来 (user, segment) 对是随机抽的、entered_at 全窗随机：「高价值用户」里的人
    累计消费和全体没有差别，「iOS 用户」里大半没有 iOS 设备。现在逐条规则判定，
    entered_at = 规则开始成立的时刻，exited_at = 规则不再成立的时刻（仍成立为 NULL）：

    1 高价值   累计有效消费 ≥ 5000：进入 = 越过 5000 的那一单的支付时刻；累计不退出
    2 新注册   注册 ≤ 30 天：进入 = 注册时刻，退出 = 注册 + 30 天（晚于锚点则 NULL）
    3 沉睡     最后活跃距锚点 ≥ 30 天：进入 = 最后活跃 + 30 天
    4 VIP      is_vip：进入 = 注册时刻
    5 年轻     18 ≤ age ≤ 25：进入 = 注册时刻
    6 一线城市 city ∈ 北上广深：进入 = 注册时刻
    7 高活跃   某个自然周（窗口起点起每 7 天）会话 ≥ 5 次：进入 = 首个达标周的第 5 次会话，
               退出 = 最后一个达标周的周末（该周已过完时）
    8 购物车   最后一次 add_to_cart 之前 30 天内没有有效支付：进入 = 那次加购，
               退出 = 其后第一笔有效支付（没有则 NULL）
    9 iOS      拥有 iOS 设备：进入 = 最早一台 iOS 设备的首次出现
    10 推广注册 registration_source = ad_campaign：进入 = 注册时刻

    主键 (user_id, segment_id) 唯一，每对一行；行数由规则决定，回写 ctx.rows
    （main.ROWS_WRITEBACK 登记了容差）。当前成员 = exited_at IS NULL。
    """
    nu = ctx.n("users")
    t0 = ctx.start.astype("datetime64[s]")
    NONE = np.datetime64("NaT", "s")
    SH = np.int64(1) << 33
    reg = ctx.cache["users_registered_at"].astype("datetime64[s]")
    anchor = ctx.as_of_end.astype("datetime64[s]")
    d30 = np.timedelta64(30 * 86400, "s")
    uids = np.arange(1, nu + 1, dtype=np.int64)
    out_u, out_s, out_in, out_ex = [], [], [], []

    def sec(ts):
        return (ts.astype("datetime64[s]") - t0).astype("timedelta64[s]").astype(np.int64)

    def add(seg, mask, ent, ex=None):
        m = np.asarray(mask, bool)
        k = int(m.sum())
        out_u.append(uids[m])
        out_s.append(np.full(k, seg, np.int64))
        out_in.append(np.asarray(ent, "datetime64[s]")[m])
        out_ex.append(np.full(k, NONE) if ex is None else np.asarray(ex, "datetime64[s]")[m])

    # 1 高价值：按支付时刻累加有效订单金额
    og, life = ctx.cache["orders"], ctx.cache["orders_life"]
    v = np.flatnonzero(og["is_valid"])
    ou = ctx.cache["orders_user_id"][v]
    pt = life["paid_at"][v].astype("datetime64[s]")
    amt = og["actual_amount"][v].astype(float)
    o = np.lexsort((pt, ou))
    ou_s, pt_s = ou[o], pt[o]
    c = np.cumsum(amt[o])
    first_of_user = np.searchsorted(ou_s, ou_s, side="left")
    cum = c - np.concatenate(([0.0], c))[first_of_user]
    cross = np.full(nu + 1, NONE)
    hit = np.flatnonzero(cum >= 5000)
    if len(hit):
        u_hit, i_hit = np.unique(ou_s[hit], return_index=True)
        cross[u_hit] = pt_s[hit[i_hit]]
    add(1, ~np.isnat(cross[1:]), cross[1:])

    # 2 新注册
    exit2 = reg + d30
    add(2, np.ones(nu, bool), reg, np.where(exit2 <= anchor, exit2, NONE))

    # 3 沉睡
    la = ctx.cache["users_last_active"].astype("datetime64[s]")
    add(3, la + d30 <= anchor, la + d30)

    # 4 / 5 / 6 / 10：注册时即确定的属性
    add(4, ctx.cache["users_is_vip"], reg)
    age = ctx.cache["user_profiles"]["age"]
    add(5, (age >= 18) & (age <= 25), reg)
    city = np.array([c_[0] for c_ in CITIES], dtype=object)[ctx.cache["user_profiles"]["city_idx"]]
    add(6, np.isin(city, SEG_FIRST_TIER), reg)

    # 7 高活跃：每 7 天一桶的会话数
    sg = ctx.cache["sessions"]
    su = sg["user_id"]
    ssec = sec(sg["start_time"])
    wk = ssec // (7 * 86400)
    nwk = int(wk.max()) + 1
    o = np.lexsort((ssec, wk, su))
    key = su[o] * nwk + wk[o]
    rank = np.arange(len(key)) - np.searchsorted(key, key, side="left")   # 桶内第几次（0 起）
    fifth = np.flatnonzero(rank == 4)
    ent7 = np.full(nu + 1, NONE)
    ex7 = np.full(nu + 1, NONE)
    if len(fifth):
        fu, fw = su[o][fifth], wk[o][fifth]
        ft = t0 + ssec[o][fifth].astype("timedelta64[s]")
        u1, i1 = np.unique(fu, return_index=True)          # 按 (user, wk) 有序 ⇒ 首个达标周
        ent7[u1] = ft[i1]
        last_i = np.r_[i1[1:], len(fu)] - 1                # 每用户最后一个达标周
        lw_end = t0 + ((fw[last_i] + 1) * 7 * 86400).astype("timedelta64[s]")
        ex7[u1] = np.where(lw_end <= anchor, lw_end, NONE)
    add(7, ~np.isnat(ent7[1:]), ent7[1:], ex7[1:])

    # 8 购物车：最后一次加购，且加购前 30 天内没有有效支付
    ev = ctx.cache["events"]
    names = ctx.dim_ids["event_definitions"]
    atc = int(np.flatnonzero(names == "add_to_cart")[0])
    m = ev["name_idx"] == atc
    last_atc = np.full(nu + 1, -1, np.int64)
    np.maximum.at(last_atc, ev["user_id"][m], sec(ev["event_time"][m]))
    has8 = last_atc[1:] >= 0
    lsec = last_atc[1:].clip(0)
    lt = t0 + lsec.astype("timedelta64[s]")
    pk = ou_s * SH + sec(pt_s)                             # 有效支付按 (user, 时刻) 有序
    q = uids * SH + lsec
    j = np.searchsorted(pk, q, side="right") - 1           # 加购及之前最后一笔支付
    jj = np.clip(j, 0, len(pk) - 1)
    recent = (j >= 0) & (ou_s[jj] == uids) & (pt_s[jj] > lt - d30)
    k = j + 1                                              # 加购之后第一笔支付
    kk = np.clip(k, 0, len(pk) - 1)
    nxt = (k < len(pk)) & (ou_s[kk] == uids)
    add(8, has8 & ~recent, lt, np.where(nxt, pt_s[kk], NONE))

    # 9 iOS：最早一台 iOS 设备的首次出现
    dv = ctx.cache["user_devices"]
    im = dv["device_type"] == "ios"
    big = np.iinfo(np.int64).max
    ios_first = np.full(nu + 1, big, np.int64)
    np.minimum.at(ios_first, dv["user_id"][im], sec(dv["first_seen_at"][im]))
    has9 = ios_first[1:] != big
    add(9, has9, t0 + np.where(has9, ios_first[1:], 0).astype("timedelta64[s]"))

    # 10 推广注册
    add(10, ctx.cache["users_reg_source"] == "ad_campaign", reg)

    a = np.concatenate(out_u)
    b = np.concatenate(out_s)
    ent = np.concatenate(out_in)
    ex = np.concatenate(out_ex)
    segs = set(int(x) for x in ctx.dim_ids["user_segments"])
    assert set(np.unique(b).tolist()) <= segs, "规则分群号不在 user_segments 里"
    assert (ent <= anchor).all() and (ent >= reg[a - 1]).all(), "分群进入时间越界"
    has_ex = ~np.isnat(ex)
    assert (ex[has_ex] > ent[has_ex]).all() and (ex[has_ex] <= anchor).all(), "分群退出时间越界"
    o = np.lexsort((b, a))
    ctx.pairs["user_segment_members"] = (a[o], b[o])
    ctx.cache["seg_entered_at"] = ent[o]
    ctx.cache["seg_exited_at"] = ex[o]
    ctx.rows["user_segment_members"] = len(a)


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


def _day_of(ctx: Ctx, ts: np.ndarray) -> np.ndarray:
    """时间戳 → 相对 DATA_START 的天偏移（_pick_registered_user 的入参）。"""
    return (ts.astype("datetime64[D]") - ctx.start.astype("datetime64[D]")).astype(np.int64)


def _after_reg(ctx: Ctx, ts: np.ndarray, uid: np.ndarray) -> np.ndarray:
    """同日内把时间后移到用户注册后 60 秒（日期不变），上限压在窗内。orders 用的同一手法。"""
    reg = ctx.cache["users_registered_at"][uid - 1].astype("datetime64[s]")
    win_last = ctx.start.astype("datetime64[s]") + np.timedelta64(ctx.days * 86400 - 1, "s")
    return np.maximum(ts.astype("datetime64[s]"),
                      np.minimum(reg + np.timedelta64(60, "s"), win_last))


def _prep_post_owners(ctx: Ctx) -> None:
    """帖子作者全局定：发帖日当天已注册（2026-11 数据普查：原来 build_posts 里 fk_skewed 抽，
    51% 的帖子早于作者注册）。发帖时间同日后移到注册之后，赞的时间下界随之更新。"""
    created = ctx.cache["posts_created_at"]
    uid = _pick_registered_user(ctx, ctx.rng("posts", "user_id", "global"), _day_of(ctx, created))
    ctx.cache["posts_user_id"] = uid
    ctx.cache["posts_created_at"] = _after_reg(ctx, created, uid)


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
    # 评论和分享只落在已发布的帖子上（与赞同一条规则，见 _prep_posts）：2026-11 数据普查
    # 查出 91 万条评论挂在草稿 / 待审 / 已删帖子上。抽「已发布帖子下标」再映回 post_id。
    pub_ids = ctx.cache["posts_published_ids"]
    cmt_post = pub_ids[F.fk_skewed(ctx.rng("post_comments", "post_id", "global"),
                                   ctx.n("post_comments"), 1, len(pub_ids)) - 1]
    shr_post = pub_ids[F.fk_skewed(ctx.rng("post_shares", "post_id", "global"),
                                   ctx.n("post_shares"), 1, len(pub_ids)) - 1]
    reg = ctx.cache["users_registered_at"].astype("datetime64[s]")
    pub = ctx.cache["posts_created_at"].astype("datetime64[s]")
    nu = ctx.n("users")
    # 评论：人按长尾抽，时间不早于「帖子已发布、本人已注册」的较晚者，偏向发帖后不久
    ncm = ctx.n("post_comments")
    c_uid = F.fk_skewed(ctx.rng("post_comments", "user_id", "global"), ncm, 1, nu)
    c_time = _ts_since(ctx.rng("post_comments", "created_at", "global"),
                       np.maximum(pub[cmt_post - 1], reg[c_uid - 1]), ctx.as_of_end, 2.0)
    # 回复：约 28% 的评论是回复，父评论必须在**同一帖子**下、且**更早**。按 (帖子, 时间)
    # 排好序后，回复指向同帖里排在它前面的某一条（最近 50 条内）。原来是「本块内 id 往前
    # 1~50」，99.999% 跨了帖子、一半还早于父评论。
    order = np.lexsort((c_time, cmt_post))
    sp = cmt_post[order]
    first_in_post = np.r_[True, sp[1:] != sp[:-1]]
    grp_start = np.maximum.accumulate(np.where(first_in_post, np.arange(ncm), 0))
    rr = ctx.rng("post_comments", "parent", "global")
    want_reply = (rr.random(ncm) < 0.28) & ~first_in_post
    back = np.minimum(rr.integers(1, 51, ncm), np.arange(ncm) - grp_start)
    parent_pos = np.where(want_reply & (back >= 1), np.arange(ncm) - back, -1)
    parent = np.full(ncm, None, dtype=object)
    has = parent_pos >= 0
    parent[order[has]] = (order[parent_pos[has]] + 1).astype(object)   # comment_id = 行号 + 1
    ctx.cache["post_comments"] = {"user_id": c_uid, "created_at": c_time, "parent": parent}
    # 分享：同样不早于发帖和本人注册
    nsh = ctx.n("post_shares")
    s_uid = F.fk_skewed(ctx.rng("post_shares", "user_id", "global"), nsh, 1, nu)
    ctx.cache["post_shares"] = {"user_id": s_uid, "created_at": _ts_since(
        ctx.rng("post_shares", "created_at", "global"),
        np.maximum(pub[shr_post - 1], reg[s_uid - 1]), ctx.as_of_end, 2.4)}
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

    # user_segment_members 不在这里抽：成员按规则从事实推导，见 _prep_segments。

    # 草稿实验没有开始，不该有分组（原来 1/3 的分组落在草稿实验上）。分组时间要读
    # 注册时间和实验档期，在 _prep_ab_assignments 里定，并剔除「用户注册时实验已结束」的对。
    per = ctx.dim_ids["_test_periods"]
    tests = np.array([t for t in ctx.dim_ids["ab_tests"].tolist()
                      if per[int(t)][2] != "draft" and per[int(t)][0] is not None], dtype=np.int64)
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
    # 三列都是 D-04 占位符（旧：user_N / user_N@example.com / "103"+序号），
    # 现在全部来自全局 cache——它们要**全列唯一**，分块抽做不到（见 _prep_users）。
    # email 不再由 username 派生：username 是中文昵称，进不了邮箱本地部分。
    # 手机号 = 真实号段 + 8 位随机尾，满足 ^1[3-9]\d{9}$（旧版第二位是 0，全不合规）。
    return {
        "user_id": uid,
        "username": ctx.cache["users_username"][sl],
        "email": ctx.cache["users_email"][sl],
        "phone": ctx.cache["users_phone"][sl],
        "registered_at": reg,
        # 取值与权重都取自 knowledge/domains/user/users.md 的实测表（权重直接用实测
        # 行数，F.enum 不要求归一化——这样每个数字都能在卡片里指回原处）。旧值
        # app / mini_program 和 banned 都来自 database/01_user_domain.sql 的行内注释，
        # 卡片明文写着数据里没有：封禁态是 suspended，注册来源那 8 个值里没有 app。
        "registration_source": ctx.cache["users_reg_source"][sl],
        "status": ctx.cache["users_status"][sl],
        "user_level": ctx.cache["users_level"][sl],
        "is_vip": ctx.cache["users_is_vip"][sl],
        "last_active_at": ctx.cache["users_last_active"][sl],
        "created_at": reg,
        "updated_at": reg,
    }


def build_user_profiles(ctx: Ctx, off: int, n: int) -> dict:
    uid = F.pk(n, off + 1)
    ci = ctx.cache["user_profiles"]["city_idx"][off:off + n]
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
        # 画像随注册建档；更新时间在注册之后（原来两列都全窗独立抽，51% 早于注册）
        "created_at": ctx.cache["users_registered_at"][sl].astype("datetime64[s]"),
        "updated_at": _ts_since(ctx.rng("user_profiles", "updated_at", off),
                                ctx.cache["users_registered_at"][sl], ctx.as_of_end, 2.0),
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
    # device_id = opaque_id(行号)；sessions / events 用同一映射指向设备行。归属、主设备、
    # 类型、首末出现时间来自 _prep_devices 的全局推导，builder 只切片。
    sl = slice(off, off + n)
    g = ctx.cache["user_devices"]
    did = F.opaque_id(F.pk(n, off + 1))
    dtype = g["device_type"][sl]
    brand, model, osv = _device_identity(ctx, off, n, dtype)
    # push_token 真实形态是一串 hex（v1 用的 fake.sha256()[:64]）；web 与小程序都没有
    # 推送令牌（实测这两类共 127 行，push_token 非空 0 行）
    ptok = F.rand_hex(ctx.rng("user_devices", "push_token", off), n, 64).astype(object)
    ptok[np.isin(dtype, ["web", "mini_program"])] = None
    first = g["first_seen_at"][sl]
    return {
        "device_id": did,
        "user_id": g["user_id"][sl],
        "device_type": dtype,
        "os_version": osv,
        "device_model": model,
        "device_brand": brand,
        "app_version": F.enum(ctx.rng("user_devices", "app_version", off), n,
                              ["5.1.0", "5.2.0", "5.3.1", "6.0.0"], [10, 20, 40, 30]),
        "push_token": ptok,
        "is_primary": g["is_primary"][sl],
        "first_seen_at": first,
        "last_seen_at": g["last_seen_at"][sl],
        "created_at": first,
    }


def build_user_segment_members(ctx: Ctx, off: int, n: int) -> dict:
    # 成员关系、进入 / 退出时间全部来自 _prep_segments（按 rules_json 推导）
    a, b = ctx.pairs["user_segment_members"]
    sl = slice(off, off + n)
    ex = ctx.cache["seg_exited_at"][sl]
    return {
        "user_id": a[sl],
        "segment_id": b[sl],
        "entered_at": ctx.cache["seg_entered_at"][sl],
        "exited_at": np.where(np.isnat(ex), None, ex).astype(object),
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
        "device_id": F.opaque_id(g["device_row"][sl] + 1),   # 本人的某台设备（_prep_devices）
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


# 带属性的 4 种事件。其余 21 种是 `{}`（空 JSON，不是 NULL），这一条写在
# knowledge/domains/behavior/events.md 的「properties 事件属性（实测形状）」里。
_PROP_EVENTS = ("view_product", "add_to_cart", "purchase", "search")


def _search_keywords(ctx: Ctx) -> tuple[np.ndarray, np.ndarray]:
    """搜索词池与权重。词池就是 semantics.yaml 的 120 个叶子类目名。

    不另立一份关键词表：搜索词必须是站内真的存在的东西，否则「搜索词 TOP 10 里
    哪些类目缺货」这类问题会拿到一批 `products` 里根本没有的词。v1 的池只有 5 个词
    （连衣裙 / 运动鞋 / 护肤品 / 零食 / 手机），其中「护肤品」是中间层类目而不是叶子。

    权重按一个平移 Zipf `1/(10+rank)` 打在**打乱后**的顺序上，不用均匀分布：120 个词
    均匀分下来每个约 0.83%，TOP 10 是哪十个词就完全由抽样噪声决定，而「搜索词 TOP 10」
    是卡片里的参考查询之一。分母上的 10 是压头部用的：不平移时头部词独占约 18%
    （调偏移之前实测 507 条搜索里「腮红」92 条），平移后头部约 3.9%、头尾差 12.9 倍，
    像个长尾而不像一个词把榜霸了。打乱的种子只吃 ctx.seed、不吃 off，所以各分块共用
    同一套热度——热度表若随分块变，分片数一改 TOP 10 就换一批。
    """
    key = "_search_kw"
    if key not in ctx.cache:
        pool = _post_objects()[1]
        order = ctx.rng("events", "keyword_pop").permutation(len(pool))
        w = 1.0 / (10.0 + np.arange(len(pool), dtype=np.float64))
        ctx.cache[key] = (pool[order], w / w.sum())
    return ctx.cache[key]


def _event_properties(ctx: Ctx, off: int, n: int, ev_name: np.ndarray) -> np.ndarray:
    """events.properties：4 种事件填真属性，其余 21 种给 `{}`。

    原来整列是 `F.const(n, {})`。后果是 `events.md` / `event_definitions.md` 里三段
    `json_extract_scalar(properties, '$.…')` 的参考 SQL 在新数据上**返回空集且不报错**，
    以及 L2 退化列体检把它报成「整列同一个值 '{}'」。四种形状逐键照卡片抄，键的顺序
    也照抄——卡片里贴的是实测样例，agent 会按那个样例写下钻 SQL。

    取值一律引用已生成的事实，不另抽一份：product_id / product_name 取 products 缓存
    （所以 `$.product_id` JOIN 得回 `products`），amount / order_id 取该 purchase 事件
    对应的那一单（所以按属性金额求和 == 按 orders.actual_amount 求和），keyword 取叶子
    类目名。判据方向：这四条 JOIN 有任意一条落空，就是这里或 _prep_behavior 的段位算错了。
    """
    out = F.const(n, {})                     # 默认 `{}`，同一引用，21 种事件走这条
    rng = ctx.rng("events", "properties", off)
    prods = ctx.cache["products"]
    pid, pname = prods["product_id"], prods["product_name"]

    m = ev_name == "view_product"
    k = int(m.sum())
    if k:
        pick = rng.integers(0, len(pid), k)
        out[m] = [{"product_id": int(pid[i]), "product_name": str(pname[i])}
                  for i in pick.tolist()]

    m = ev_name == "add_to_cart"
    k = int(m.sum())
    if k:
        # 件数 1/2/3 权重取 v1 实测（293/299/246，基本均匀，偏向 1~2 件）
        qty = F.int_weighted(rng, k, [1, 2, 3], [293, 299, 246])
        pick = rng.integers(0, len(pid), k)
        out[m] = [{"quantity": int(q), "product_id": int(pid[i])}
                  for q, i in zip(qty.tolist(), pick.tolist())]

    m = ev_name == "search"
    k = int(m.sum())
    if k:
        pool, w = _search_keywords(ctx)
        kw = pool[rng.choice(len(pool), size=k, replace=True, p=w)]
        out[m] = [{"keyword": str(x)} for x in kw.tolist()]

    # purchase 不抽：它在 events 数组里是「每有效单一条」的保底段，第几行对应第几单
    # 是定死的（见 _prep_behavior 里的 events_purchase）。这里把本分块落在那段里的
    # 行切出来，逐行填它自己那一单的金额与单号。
    m = ev_name == "purchase"
    k = int(m.sum())
    if k:
        pu = ctx.cache["events_purchase"]
        gidx = np.flatnonzero(m) + off - pu["lo"]
        assert gidx.size and gidx[0] >= 0 and gidx[-1] < len(pu["order_id"]), (
            f"purchase 事件落在保底段之外：off={off} n={n} "
            f"段起点={pu['lo']} 段长={len(pu['order_id'])} 命中下标"
            f"[{gidx[0]}, {gidx[-1]}]。段位算错了，别放宽这条断言——"
            f"放宽的后果是 properties 里的金额和单号张冠李戴，而且不报错")
        out[m] = [{"amount": float(pu["amount"][i]), "order_id": int(pu["order_id"][i])}
                  for i in gidx.tolist()]
    return out


def build_events(ctx: Ctx, off: int, n: int) -> dict:
    # user/session/时间/事件名全部来自 _prep_behavior：事件继承所属会话的用户与
    # 时间窗，事件名按漏斗权重（旧版三者独立抽——漏斗恒 100%、留存不衰减、
    # 事件的 user 与所属 session 的 user 互相矛盾，审计 L5.6/L5.8）。
    sl = slice(off, off + n)
    g = ctx.cache["events"]
    names = ctx.dim_ids["event_definitions"]
    uid = g["user_id"][sl]
    # 设备 = 所属会话的设备；不在会话内的服务端事件（purchase / use_coupon）记本人主设备
    sess = g["session_id"][sl]
    srow = ctx.cache["sessions"]["device_row"]
    prow = ctx.cache["user_devices"]["primary_row"]
    dev_row = np.where(sess > 0, srow[np.maximum(sess, 1) - 1], prow[uid - 1])
    ev_time = g["event_time"][sl]
    ev_name = names[g["name_idx"][sl]]
    return {
        "event_id": F.pk(n, off + 1),
        "user_id": uid,
        "device_id": F.opaque_id(dev_row + 1),
        "session_id": np.where(sess > 0, sess, None).astype(object),
        "event_name": ev_name,
        "event_time": ev_time,
        "properties": _event_properties(ctx, off, n, ev_name),
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
#
# 下面三个 `_post_*` 是 posts 那三个数组列的产出点（P1-10）。它们都逐行拼 Python
# list——数组列在 numpy 里只能是 object 数组，向量化拼不出「每行长度不同的列表」。
# 代价可控：posts 是 FACT×1000，8000 万规模上 42.7 万行，不是千万行级的那几张表。

def _post_n(rng: np.random.Generator, ctype: np.ndarray,
            spec: dict[str, tuple[int, int]]) -> np.ndarray:
    """按 content_type 逐行抽个数。spec 是 {类型: (下限, 上限)}，左右闭区间。"""
    lo = np.array([spec[c][0] for c in ctype.tolist()], dtype=np.int64)
    hi = np.array([spec[c][1] for c in ctype.tolist()], dtype=np.int64)
    return lo + (rng.random(len(lo)) * (hi - lo + 1)).astype(np.int64)


def _post_media(ctx: Ctx, off: int, pid: np.ndarray, ctype: np.ndarray) -> np.ndarray:
    """媒体文件 URL。路径形态照抄 products.image_urls，短视频给 .mp4。

    URL 里带 post_id，所以整列天然唯一、也能一眼看出属于哪个帖子。不做「草稿没有
    媒体」这种区分：草稿是写了没发，图片早就传上去了。
    """
    k = _post_n(ctx.rng("posts", "media_urls", off), ctype, POST_MEDIA_N)
    ext = np.where(ctype == "short_video", "mp4", "jpg")
    out = np.empty(len(pid), dtype=object)
    out[:] = [[f"https://cdn.example.com/posts/{p}/{j}.{e}" for j in range(1, m + 1)]
              for p, m, e in zip(pid.tolist(), k.tolist(), ext.tolist())]
    return out


def _post_tags(ctx: Ctx, off: int, n: int, leaf: np.ndarray) -> np.ndarray:
    """话题标签。**第一个标签恒为本帖的品类词**，后面接 1~3 个运营话题。

    品类词打头是这一列的用处所在：`tags` 与 `product_ids`、标题三者指向同一件事，
    「按话题看内容表现」和「按品类看种草效果」才对得上。运营话题无放回抽，
    免得同一帖出现两个一样的标签。
    """
    r = ctx.rng("posts", "tags", off)
    k = F.int_uniform(r, n, 1, 3)
    pick = np.argsort(r.random((n, len(POST_TAGS))), axis=1)[:, :3]
    pool = np.array(POST_TAGS, dtype=object)
    out = np.empty(n, dtype=object)
    out[:] = [[lf] + pool[p[:m]].tolist()
              for lf, p, m in zip(leaf.tolist(), pick, k.tolist())]
    return out


def _post_products(ctx: Ctx, off: int, n: int, ctype: np.ndarray,
                   leaf: np.ndarray) -> np.ndarray:
    """关联商品。取值只从**本帖品类**的 SKU 里挑，个数按 content_type（review 恒 1）。

    这是 P1-10 的落点：`knowledge/relationships.md` 声明的 products ↔ posts N:N
    此前没有任何一行数据兑现，卡片里那段 `UNNEST(product_ids)` 的种草分析恒返回 0 行。
    同品类内无放回挑，所以「对比了五家」那种多商品帖也讲得通；跨品类不挑，因为那会
    让标题和关联商品互相打脸。
    """
    by_leaf = ctx.cache["products_by_leaf"]
    r = ctx.rng("posts", "product_ids", off)
    k = _post_n(r, ctype, POST_PROD_N)
    out = np.empty(n, dtype=object)
    out[:] = [[] if m == 0 else
              r.choice(by_leaf[lf], size=min(m, len(by_leaf[lf])),
                       replace=False).tolist()
              for lf, m in zip(leaf.tolist(), k.tolist())]
    return out


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
    # 对象短语单独抽一条流（不再和开场 / 结论挤在 F.combine 的一条流里）：下标 oi
    # 要留给 tags 和 product_ids 用，那两列必须和标题说的是同一个品类（P1-10）。
    obj_ph, obj_leaf = _post_objects()
    oi = F.int_uniform(ctx.rng("posts", "obj", off), n, 0, len(obj_ph) - 1)
    core = np.char.add(np.char.add(
        np.asarray(F.from_pool(ctx.rng("posts", "open", off), n, _pool(POST_OPEN)),
                   dtype=str),
        np.asarray(obj_ph[oi], dtype=str)),
        np.asarray(F.from_pool(ctx.rng("posts", "verdict", off), n,
                               _pool(POST_VERDICT)), dtype=str))
    _t = np.char.add(core, np.asarray(
        F.from_pool(ctx.rng("posts", "title_tail", off), n, _pool(POST_TAIL)), dtype=str))
    _c = np.char.add(np.char.add(core, "。"), np.asarray(
        F.from_pool(ctx.rng("posts", "body", off), n, _pool(POST_BODY)), dtype=str))
    ctype = F.enum(ctx.rng("posts", "content_type", off), n,
                   ["article", "short_video", "image", "review"], [20, 32, 28, 20])
    return {
        "post_id": pid,
        "user_id": ctx.cache["posts_user_id"][sl],
        "content_type": ctype,
        "title": _t,
        "content": _c,
        "media_urls": _post_media(ctx, off, pid, ctype),
        "tags": _post_tags(ctx, off, n, obj_leaf[oi]),
        "location": F.from_pool(ctx.rng("posts", "location", off), n,
                                _pool([c[0] for c in CITIES])),
        "product_ids": _post_products(ctx, off, n, ctype, obj_leaf[oi]),
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
        # user / 时间 / 父评论全局定（_prep_social）：回复要找同帖里更早的评论，分块做不到
        "user_id": ctx.cache["post_comments"]["user_id"][off:off + n],
        "parent_comment_id": ctx.cache["post_comments"]["parent"][off:off + n],
        "content": F.combine(ctx.rng("post_comments", "content", off), n,
                             _pool(COMMENT_A), _pool(COMMENT_B), _pool(COMMENT_TAIL)),
        "like_count": F.int_weighted(ctx.rng("post_comments", "like_count", off), n,
                                     [0, 1, 3, 8, 25], [45, 25, 18, 9, 3]),
        "status": F.enum(ctx.rng("post_comments", "status", off), n,
                         ["visible", "hidden", "deleted"], [92, 5, 3]),
        "created_at": ctx.cache["post_comments"]["created_at"][off:off + n],
    }


def build_post_shares(ctx: Ctx, off: int, n: int) -> dict:
    return {
        "share_id": F.pk(n, off + 1),
        "user_id": ctx.cache["post_shares"]["user_id"][off:off + n],
        # post_id 全局生成（posts.share_count 要 bincount 它回填），builder 只切片
        "post_id": ctx.cache["post_shares_post_id"][off:off + n],
        # 卡片：6 个值。朋友圈是 wechat_moment**s**（旧值少个 s，按卡片筛恒为空集），
        # 零散渠道归进 other——旧清单缺 qq / other 两档。
        "share_channel": F.enum(ctx.rng("post_shares", "share_channel", off), n,
                                ["wechat_friend", "wechat_moments", "weibo",
                                 "copy_link", "qq", "other"],
                                [2420, 1748, 1043, 725, 715, 349]),
        "created_at": ctx.cache["post_shares"]["created_at"][off:off + n],
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
    # 收发双方都按发送日挑「当天已注册」的用户（原来全库均匀抽，68% 早于某一方注册），
    # 再把发送时间同日后移到两人都注册之后；收件人与发件人相同时顺延一位。
    d = _day_of(ctx, sent)
    snd = _pick_registered_user(ctx, ctx.rng("user_messages", "sender_id", off), d)
    rcv = _pick_registered_user(ctx, ctx.rng("user_messages", "receiver_id", off), d)
    same = rcv == snd
    rcv[same] = _pick_registered_user(ctx, ctx.rng("user_messages", "receiver_retry", off), d[same])
    rcv[rcv == snd] = np.where(snd[rcv == snd] > 1, snd[rcv == snd] - 1, snd[rcv == snd] + 1)
    sent = np.maximum(_after_reg(ctx, sent, snd), _after_reg(ctx, sent, rcv))
    read = F.ts_offset(ctx.rng("user_messages", "read_at", off), sent, 1, 4320,
                       cap=ctx.as_of_end, null_p=0.32)
    # 引用的帖子必须在发送时已发布：在「按发布时间排好序的已发布帖子」里取发送时刻之前的前缀
    if "_pub_posts_by_time" not in ctx.cache:
        pid = ctx.cache["posts_published_ids"]
        pt = ctx.cache["posts_created_at"][pid - 1].astype("datetime64[s]")
        o = np.argsort(pt, kind="stable")
        ctx.cache["_pub_posts_by_time"] = (pid[o], pt[o])
    pp, ptime = ctx.cache["_pub_posts_by_time"]
    k = np.searchsorted(ptime, sent.astype("datetime64[s]"), side="right")
    rp_pick = pp[np.minimum((ctx.rng("user_messages", "rp", off).random(n) * k).astype(np.int64),
                            np.maximum(k - 1, 0))]
    rp_keep = (ctx.rng("user_messages", "related_post_id", off).random(n) >= 0.85) & (k > 0)
    related_post = np.where(rp_keep, rp_pick, None).astype(object)
    return {
        "message_id": mid,
        "sender_id": snd,
        "receiver_id": rcv,
        "content": F.combine(ctx.rng("user_messages", "content", off), n,
                             _pool(DM_A), _pool(["，", "，", "，", " "]), _pool(DM_B),
                             _pool(DM_TAIL)),
        # 卡片：只有 text / image / link 三个值，分享类消息统一是 link
        # （旧值 product 以及 post_share / system 数据里都不存在）。
        "message_type": F.enum(ctx.rng("user_messages", "message_type", off), n,
                               ["text", "image", "link"], [7868, 907, 478]),
        "related_post_id": related_post,
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
    life = ctx.cache["orders_life"]
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
        # 见 SHIP_ADDRESSES：原来整列同一个深圳地址，且因为非空、又是 JSONB，没有任何判据会拦。
        "shipping_address": F.enum(ctx.rng("orders", "ship_addr", off), n,
                                   SHIP_ADDRESSES, SHIP_ADDRESSES_W),
        # null_out 仍用原来那条随机流，所以**哪些行为空一行没动**；变的只是非空行
        # 从「同一句话」换成 ORDER_REMARKS 里的一条（取值用独立流）。
        "remark": F.null_out(ctx.rng("orders", "remark", off),
                             F.enum(ctx.rng("orders", "remark_text", off), n,
                                    ORDER_REMARKS, ORDER_REMARKS_W), 0.9),
        "placed_at": placed,
        "paid_at": life["paid_at"][sl],
        "shipped_at": life["shipped_at"][sl],
        "delivered_at": life["delivered_at"][sl],
        "cancelled_at": life["cancelled_at"][sl],
        # 哪些行非空一行没动（还是 canc_m / refund_m），变的只是非空行的取值不再是同一句话。
        "cancel_reason": np.where(canc_m,
                                  F.enum(ctx.rng("orders", "cancel_reason", off), n,
                                         ORDER_CANCEL_REASONS, ORDER_CANCEL_REASONS_W),
                                  None),
        "refunded_at": life["refunded_at"][sl],
        "refund_reason": np.where(refund_m,
                                  F.enum(ctx.rng("orders", "refund_reason", off), n,
                                         ORDER_REFUND_REASONS, ORDER_REFUND_REASONS_W),
                                  None),
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
        "paid_at": ctx.cache["orders_life"]["paid_at"][idx],      # 与订单同一时刻
        "failure_reason": F.const(n, None),
        "refund_amount": np.where(refunded, amt, 0.0),
        "refunded_at": ctx.cache["orders_life"]["refunded_at"][idx],
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
    start_ts = F.ts_window(ctx.rng("subscriptions", "start_date", off), n, ctx.start, ctx.days)
    # 订阅人按开通日挑当天已注册的用户（原来 fk_skewed，51% 早于注册）
    s_uid = _pick_registered_user(ctx, ctx.rng("subscriptions", "user_id", off),
                                  _day_of(ctx, start_ts))
    start_ts = _after_reg(ctx, start_ts, s_uid)
    start = start_ts.astype("datetime64[D]")
    # 卡片：种子数据只有 active 36 / expired 14。`cancelled` 在业务上成立，但这份数据里
    # 没有——卡片连带把「cancel_reason 整列 NULL」和一条恒返回空集的示例查询都写成了
    # 教学点。所以这里不产 cancelled，下面 cancelled_at / cancel_reason 的掩码恒为假，
    # 那两列跟着整列 NULL，与卡片一致。要造有取消的数据集，改这一行的取值即可，
    # 但卡片得同步改，否则枚举断言会红。
    # 状态由到期日推出（原来独立抽，26% 标 active 却已过期）
    end = start + days.astype("timedelta64[D]")
    st = np.where(end >= np.datetime64(ctx.as_of_end, "D"), "active", "expired")
    # 支付关联：本人在开通时刻或之前的最近一笔支付（原来全库均匀抽，100% 是别人的支付）。
    # 约 15% 没有关联（与原来的空值率一致）；本人此前没有支付的也留空。
    if "_pay_by_user" not in ctx.cache:
        pidx = ctx.cache["payments_order_idx"]
        pu = ctx.cache["orders_user_id"][pidx]
        pt = ctx.cache["orders_life"]["paid_at"][pidx].astype("datetime64[s]").astype(np.int64)
        key = pu.astype(np.int64) * 10**11 + pt
        o = np.argsort(key, kind="stable")
        ctx.cache["_pay_by_user"] = (key[o], (o + 1).astype(np.int64))   # payment_id = 行号 + 1
    pkey, pid_sorted = ctx.cache["_pay_by_user"]
    # 截止到开通时刻（不是开通当天日末）：关联的支付必须先于订阅记录（原来 21% 晚于 created_at）
    q = s_uid.astype(np.int64) * 10**11 + start_ts.astype("datetime64[s]").astype(np.int64)
    j = np.searchsorted(pkey, q, side="right") - 1
    ok = (j >= 0) & (pkey[np.maximum(j, 0)] // 10**11 == s_uid)
    keep = ctx.rng("subscriptions", "payment_id", off).random(n) >= 0.15
    pay = np.where(ok & keep, pid_sorted[np.maximum(j, 0)], None).astype(object)
    return {
        "subscription_id": sid,
        "user_id": s_uid,
        "plan_name": plan,
        "plan_price": price,
        "start_date": start,
        "end_date": end,
        "auto_renew": F.bool_p(ctx.rng("subscriptions", "auto_renew", off), n, 0.68),
        "status": st,
        "payment_id": pay,
        "cancelled_at": _cond_ts(ctx.rng("subscriptions", "cancelled_at", off),
                                 start.astype("datetime64[s]"), st == "cancelled",
                                 1440, 60 * 1440, ctx.as_of_end),
        "cancel_reason": np.where(st == "cancelled", "不再需要", None),
        "created_at": start_ts,
        "updated_at": start_ts,
    }


# ---------------------------------------------------------------- 归因域

def build_user_attributions(ctx: Ctx, off: int, n: int) -> dict:
    """全部列都在 `_prep_attributions` 里算好，这里只切片。

    提到 prep 的理由不是省事：`channel_daily_costs` 的 installs / cost 必须从
    「归因到本渠道、且当天注册的新客数」派生，否则 CAC = cost / new_users 两边
    各说各话。builder 里现抽的话，成本表拿不到这份真值。
    """
    s = slice(off, off + n)
    click = ctx.cache["attr_click"][s]
    d2i = ctx.cache["attr_d2i"][s]
    return {
        "attribution_id": F.pk(n, off + 1),
        "user_id": ctx.cache["attr_user"][s],
        "channel_id": ctx.cache["attr_channel"][s],
        "ad_campaign_id": ctx.cache["attr_acid"][s],
        "creative_id": ctx.cache["attr_crid"][s],
        # 只有 first_touch / last_touch 两种，**没有** linear。被归因的用户两种各一行
        # （同一用户的两个视角，见 _prep_attributions）。
        "attribution_type": ctx.cache["attr_type"][s],
        "click_time": click,
        "install_time": ctx.cache["attr_install"][s],
        "attributed_at": ctx.cache["attr_at"][s],
        "days_to_install": d2i,
        # utm_source 取本行渠道自己的 platform（channels.csv 的那一列），不是常量。
        # 原来这里是 F.const(n, {"utm_source": "douyin"})：App Store、直接访问这些行
        # 也写着 douyin，按它统计渠道会得到「100% 来自抖音」而同一行的 channel_id
        # 就在旁边。这一列是 JSONB，不在 verify_literals 的形态普查面里，也不是 NULL，
        # 所以没有任何一层会拦——只有 verify_constants 的「整列同一个值」抓得到它。
        "tracking_params": ctx.cache["attr_tracking"][s],
    }


def _prep_attributions(ctx: Ctx) -> None:
    """归因表：被归因的用户各有一条 first_touch + 一条 last_touch（2026-11 数据普查）。

    原来每个用户恰好一行、类型随机二选一（first_touch 与 last_touch 分给不同的人），
    点击时间全窗随机、与注册无关（51% 晚于注册），广告活动在点击时 27% 尚未开始。

    现在的时序：点击广告 → 安装 → 注册。
      last_touch  click = install − days_to_install 天 − 1~60 分钟；install = 注册前 1~120 分钟
      first_touch 45% 的用户只有一次触点（两行渠道 / 活动 / 点击相同）；其余在 last_touch
                  之前 1 小时 ~ 14 天另有一次点击，渠道独立抽（可能与 last_touch 相同）
      attributed_at = 注册时刻（归因在转化时落定），两行相同
    广告活动只挑本渠道**点击当天在档期内**的活动，没有就 NULL（渠道级投放）；
    自然量渠道（organic / referral / direct）恒为 NULL。

    行数 = 2 × 被归因用户数，与 budget 持平；被 last_touch 覆盖的用户数也与旧版相当
    （旧版 last_touch 占一半行），所以 mart 里「(未归因)」的 GMV 桶量级不变。
    """
    nu = ctx.n("users")
    natt = min(ctx.n("user_attributions") // 2, nu)
    ar = ctx.rng("user_attributions", "user_pick")
    users = np.sort(ar.choice(np.arange(1, nu + 1), size=natt, replace=False))
    reg = ctx.cache["users_registered_at"][users - 1].astype("datetime64[s]")
    chans = ctx.dim_ids["channels"]
    r = ctx.rng("user_attributions", "touches", "global")

    d2i = F.int_weighted(ctx.rng("user_attributions", "days_to_install", 0), natt,
                         [0, 1, 2, 5], [62, 22, 10, 6])
    install = reg - r.integers(60, 7200, natt).astype("timedelta64[s]")
    click_l = (install - (d2i * 86400).astype("timedelta64[s]")
               - r.integers(60, 3600, natt).astype("timedelta64[s]"))
    # last_touch 渠道均衡分配（打乱后轮转）：各渠道新客数相同，CAC 的排序就只由
    # CHANNEL_CPI_MULT 决定，小规模（selftest 的 scale=1，每渠道十来个人）也不翻
    chans_arr = np.asarray(chans)
    ch_l = chans_arr[ctx.rng("user_attributions", "channel_id", 0).permutation(natt) % len(chans_arr)]
    single = r.random(natt) < 0.45
    ch_f = np.where(single, ch_l, F.from_pool(ctx.rng("user_attributions", "channel_first", 0),
                                              natt, chans))
    click_f = np.where(single, click_l,
                       click_l - r.integers(3600, 14 * 86400, natt).astype("timedelta64[s]"))

    # 行布局：每用户相邻两行，先 first_touch 后 last_touch
    uid = np.repeat(users, 2)
    ch = np.empty(2 * natt, dtype=ch_l.dtype)
    ch[0::2], ch[1::2] = ch_f, ch_l
    click = np.empty(2 * natt, dtype="datetime64[s]")
    click[0::2], click[1::2] = click_f, click_l
    typ = np.tile(np.array(["first_touch", "last_touch"], dtype=object), natt)
    ctx.rows["user_attributions"] = 2 * natt
    ctx.cache["attr_user"] = uid
    ctx.cache["attr_channel"] = ch
    ctx.cache["attr_type"] = typ
    ctx.cache["attr_click"] = click
    ctx.cache["attr_install"] = np.repeat(install, 2)
    ctx.cache["attr_at"] = np.repeat(reg, 2)
    # 每行自己的「点击到安装」天数（first_touch 多触点那行的点击更早，天数也更大）
    ctx.cache["attr_d2i"] = ((ctx.cache["attr_install"] - click).astype("timedelta64[s]")
                             .astype(np.int64) // 86400)

    # ad_campaign_id：本渠道、点击当天在档期内的活动；素材：该活动的素材，约 20% 追不到
    camps = ctx.dim_ids["_campaigns_by_channel"]
    creas = ctx.dim_ids["_creatives_by_campaign"]
    spans = ctx.dim_ids["_campaign_spans"]
    cday = ((click.astype("datetime64[D]") - ctx.start.astype("datetime64[D]"))
            .astype(np.int64))
    r_ac = ctx.rng("user_attributions", "acid", 0)
    r_cr = ctx.rng("user_attributions", "crid", 0)
    u_ac = r_ac.random(2 * natt)
    u_cr = r_cr.random(2 * natt)
    drop = r_cr.random(2 * natt) < 0.20
    acid = np.full(2 * natt, None, dtype=object)
    crid = np.full(2 * natt, None, dtype=object)
    for cid, pool in camps.items():
        m = np.flatnonzero(ch == cid)
        if len(m) == 0:
            continue
        pool = np.asarray(sorted(pool), np.int64)
        lo = np.array([spans[int(a)][0] for a in pool])
        hi = np.array([spans[int(a)][1] for a in pool])
        live = (lo[None, :] <= cday[m, None]) & (cday[m, None] <= hi[None, :])
        nlive = live.sum(axis=1)
        has = nlive > 0
        kth = np.floor(u_ac[m] * np.maximum(nlive, 1)).astype(np.int64)
        # 第 kth 个在档的活动
        csum = np.cumsum(live, axis=1)
        pick = np.argmax(csum > kth[:, None], axis=1)
        for j in np.flatnonzero(has):
            row = m[j]
            a = int(pool[pick[j]])
            acid[row] = a
            cr = creas.get(a)
            if cr and not drop[row]:
                crid[row] = cr[int(u_cr[row] * len(cr))]
    # 单触点用户的两行必须一模一样（同一次点击）
    first_rows = np.flatnonzero(np.repeat(single, 2) & (typ == "first_touch"))
    acid[first_rows] = acid[first_rows + 1]
    crid[first_rows] = crid[first_rows + 1]
    ctx.cache["attr_acid"] = acid
    ctx.cache["attr_crid"] = crid

    # tracking_params：utm_source = 本行渠道的 platform
    plat = ctx.dim_ids["_channel_platforms"]
    missing = sorted({int(c) for c in np.unique(ch)} - set(plat))
    if missing:
        raise ValueError(f"channels.csv 缺 platform 的 channel_id：{missing}")
    ctx.cache["attr_tracking"] = np.array(
        [{"utm_source": plat[int(c)]} for c in ch], dtype=object)


# ------------------------------------------------------------ 投放成本（P0-5）

# 单客成本乘数：键 = channel_id，值 = 相对本 channel_type 基准 CPI 的倍数。
#
# 为什么显式钉一份而不是随机抽：
#  1. eval 有「CAC 最高 / 最低的渠道是哪个」这类 Top-1 题（#25），答案必须唯一且稳定。
#     **两端**刻意留 ≥1.55 倍间隔（4.30 vs 2.70、0.63 vs 0.40），Top-1 / Bottom-1 因此
#     不可能被取整和保底花费翻过去。中间几档只差约 1.25 倍，全序在 scale=20 实测与
#     乘数序完全一致，但**只有两端是算出来有保证的**——别拿中间的名次当断言。
#  2. 语义要说得通：搜索类渠道意图强、转化好，CAC 最低；KOL 种草最贵。乘数按这个排，
#     **不按 channel_id 排**——按 id 单调递增会留下"CAC 随渠道编号递增"这种一眼假的痕迹。
# 键集合必须恰好等于 channels.csv 里 paid+kol 那 9 个，_prep_channel_costs 有断言。
CHANNEL_CPI_MULT = {
    1: 1.30,     # 抖音信息流
    2: 0.63,     # 抖音搜索
    3: 2.05,     # 小红书种草
    4: 0.40,     # 小红书搜索 —— 最低 CAC
    5: 1.65,     # 微信朋友圈广告
    7: 1.05,     # 百度搜索
    8: 0.80,     # 快手信息流
    9: 4.30,     # 微博 KOL —— 最高 CAC
    14: 2.70,    # B 站 UP 主
}

# 按 channel_type 的投放基准。这四个数一起决定漏斗上四列的量级：
#   impressions --ctr--> clicks --cvr--> installs --reg_rate--> 归因新客
# cpi 是每次激活的成本（元）。CAC = cpi / reg_rate，两类算下来分别是 16.4 / 15.8，
# **刻意取得几乎相等**：让渠道之间的 CAC 差异只来自 CHANNEL_CPI_MULT，
# 不被 channel_type 混进来，否则那张乘数表就不再是 CAC 排序的唯一依据。
CHANNEL_TYPE_ECON = {
    "paid": {"cpi": 9.0, "reg_rate": 0.55, "cvr": 0.030, "ctr": 0.020},
    "kol":  {"cpi": 6.0, "reg_rate": 0.38, "cvr": 0.045, "ctr": 0.012},
}

# 零新客那天的保底激活量，按该渠道**窗内日均激活量**的比例给。真实投放不会因为
# 某天没拉到注册就停投；而且这些天是 `cost / nullif(new_users, 0)` 里 NULL 分支的
# 唯一来源——knowledge/metrics/governed_metrics.md 专门讲了这个分支，数据里必须真有它。
COST_FLOOR_SHARE = 0.10
COST_JITTER = 0.12       # 逐日花费抖动 ±12%
FUNNEL_JITTER = 0.15     # 逐日 clicks / impressions 抖动 ±15%


def _prep_channel_costs(ctx: Ctx) -> None:
    """9 个投放渠道 × 311 天的成本网格；窗内 91 天的 installs / cost 从归因新客反推。

    ## 为什么必须从归因反推

    `mart_channel_daily` 的 CAC 是 `cost / nullif(new_users_attributed, 0)`，分母口径写死在
    `database/iceberg/02_mart.sql:296-369`：「`attribution_type = 'last_touch'` 归因到本渠道、
    且 `CAST(users.registered_at AS date)` 落在这一天的用户数」。分子若独立乱抽，CAC 就是
    两个无关随机数的比值——v1 数据实测 244,774 installs 配全库 500 个用户（490 倍），
    CAC ¥2,900/人，凡是碰 CAC / ROI 的题全错，而每一层校验都是绿的。

    方向只能是**分母 → 分子**：新客数是 user_attributions 和 users 已经落地的事实，
    成本是唯一还没落地的那一侧，所以它去适配前者。这也是 `prepare_globals()` 里
    本函数必须排在 `_prep_attributions` 之后的原因。

    ## 为什么轴仍然铺到 2026-09-01

    见 `budget.COST_AXIS_END` 的注释：那是「禁用每张表自己的 max(时间列)」这条铁律的
    唯一具体例子，写进了提示词和 8 张卡片。这里只修量，不动轴。

    ## 轴外那 220 天为什么是平的

    轴外每天的激活量 = 该渠道**窗内日均**，只叠 ±12% 抖动，不编趋势。那段没有任何
    业务事实可对照（没有归因、没有订单），编出来的趋势会被当成真的拿去解读。
    """
    paid = ctx.dim_ids["_paid_channels"]
    ch_type = ctx.dim_ids["_channel_types"]
    camps = ctx.dim_ids["_campaigns_by_channel"]
    spans = ctx.dim_ids["_campaign_spans"]
    nch, naxis, win = len(paid), budget.COST_AXIS_DAYS, ctx.days

    missing = sorted(set(paid.tolist()) - set(CHANNEL_CPI_MULT))
    extra = sorted(set(CHANNEL_CPI_MULT) - set(paid.tolist()))
    if missing or extra:
        raise ValueError(
            f"CHANNEL_CPI_MULT 与 channels.csv 的 paid/kol 集合不符：缺 {missing}，"
            f"多 {extra}——改过渠道表就要跟着改乘数表，否则某个渠道的 CAC 无定义")
    if nch * naxis != ctx.n("channel_daily_costs"):
        raise ValueError(
            f"成本网格 {nch} 渠道 × {naxis} 天 = {nch * naxis:,} 行，但 budget 声明 "
            f"{ctx.n('channel_daily_costs'):,} 行——改 budget.PAID_CHANNELS / "
            f"COST_AXIS_END（那一项写成两者相乘，不是字面量）")
    if naxis < win:
        raise ValueError(f"成本轴 {naxis} 天短于数据窗 {win} 天，窗内会缺成本行")

    # —— 分母：last_touch 归因到各投放渠道的用户，按其**注册日**归集 ——
    # 用 registered_at 而不是 attributed_at：口径由 mart 那边定，这里只能跟。
    last = ctx.cache["attr_type"] == "last_touch"
    uid = ctx.cache["attr_user"][last]
    cid = ctx.cache["attr_channel"][last]
    reg_day = ctx.cache["users_reg_day"][uid - 1]
    if reg_day.size and (reg_day.min() < 0 or reg_day.max() >= win):
        raise ValueError(
            f"注册日偏移越界 [{reg_day.min()}, {reg_day.max()}]，应在 [0, {win - 1}]"
            f"——users.registered_at 跑出数据窗了")
    slot = {int(c): i for i, c in enumerate(paid.tolist())}
    keep = np.isin(cid, paid)          # 自然量渠道的归因不进成本表
    idx = np.array([slot[int(c)] for c in cid[keep].tolist()], dtype=np.int64)
    new_users = np.bincount(idx * win + reg_day[keep],
                            minlength=nch * win).reshape(nch, win)

    # —— 分子：逐渠道由新客数反推激活 → 花费，再由激活上推点击 / 曝光 ——
    r_cost = ctx.rng("channel_daily_costs", "cost")
    r_fun = ctx.rng("channel_daily_costs", "funnel")
    ins = np.empty((nch, naxis), dtype=np.int64)
    cost = np.empty((nch, naxis), dtype=np.float64)
    clk = np.empty((nch, naxis), dtype=np.int64)
    imp = np.empty((nch, naxis), dtype=np.int64)
    for i, c in enumerate(paid.tolist()):
        e = CHANNEL_TYPE_ECON[ch_type[c]]
        cpi = e["cpi"] * CHANNEL_CPI_MULT[c]
        need = np.ceil(new_users[i] / e["reg_rate"]).astype(np.int64)
        floor = max(1, int(round(COST_FLOOR_SHARE * need.sum() / win)))
        ins[i, :win] = np.maximum(need, floor)
        # 轴外：窗内日均，不带趋势（见 docstring）
        ins[i, win:] = max(1, int(round(ins[i, :win].mean())))
        jit = 1.0 + r_cost.uniform(-COST_JITTER, COST_JITTER, naxis)
        cost[i] = np.round(ins[i] * cpi * jit, 2)
        clk[i] = np.ceil(ins[i] / e["cvr"]
                         * (1.0 + r_fun.uniform(-FUNNEL_JITTER, FUNNEL_JITTER, naxis)))
        imp[i] = np.ceil(clk[i] / e["ctr"]
                         * (1.0 + r_fun.uniform(-FUNNEL_JITTER, FUNNEL_JITTER, naxis)))
    # 漏斗单调性是**算出来的**：cvr ≤ 0.045、ctr ≤ 0.020，抖动上限 ±15%，
    # 所以 clicks ≥ installs × 18、impressions ≥ clicks × 42，抖动不可能翻过来。
    # 但还是断言一遍——这四个常量以后被人调小了，静默失去单调性的成本太高。
    if not ((imp > clk).all() and (clk > ins).all() and (ins >= 1).all()):
        raise ValueError("成本表漏斗不单调：应恒有 impressions > clicks > installs ≥ 1，"
                         "检查 CHANNEL_TYPE_ECON 的 ctr / cvr 与 FUNNEL_JITTER")

    # —— ad_campaign_id：只挂到**档期覆盖这一天**的本渠道活动上 ——
    # 多个活动同时在投时按天轮转，让花费摊到更多活动上（取 min 会让一批活动一分钱没有）。
    # 没有活动覆盖的日子给 NULL，语义是「渠道级投放，未归到具体活动」。
    day = np.arange(naxis)
    acid = np.full((nch, naxis), None, dtype=object)
    for i, c in enumerate(paid.tolist()):
        pool = sorted(camps[c])
        for d in range(naxis):
            live = [a for a in pool if spans[a][0] <= d <= spans[a][1]]
            if live:
                acid[i, d] = live[d % len(live)]

    ctx.cache["cost_channel"] = np.repeat(paid, naxis)
    ctx.cache["cost_date"] = (ctx.start.astype("datetime64[D]")
                              + np.tile(day, nch).astype("timedelta64[D]"))
    ctx.cache["cost_acid"] = acid.reshape(-1)
    ctx.cache["cost_impressions"] = imp.reshape(-1)
    ctx.cache["cost_clicks"] = clk.reshape(-1)
    ctx.cache["cost_installs"] = ins.reshape(-1)
    ctx.cache["cost_cost"] = cost.reshape(-1)


def build_channel_daily_costs(ctx: Ctx, off: int, n: int) -> dict:
    """全部列在 `_prep_channel_costs` 里算好，这里只切片。

    `creative_id` 恒为 NULL：成本按活动汇总，不拆到素材。这一列登记在
    `scripts/lakehouse/verify_constants.py::ALL_NULL_PINNED` 里，改成有值的话那边会红
    （要求先把登记删掉），所以不是"忘了填"。

    `created_at` 挂在该行自己那一天的 23:30——「当日汇总，当晚落库」。轴外的行因此
    created_at 也在锚点之后，这与 `date` 列同源、是一致的；成本表的身份本来就是
    「轴比业务日历长」。不钉成常量：v1 那 910 行的 created_at 全等于灌数那一瞬。
    """
    s = slice(off, off + n)
    d = ctx.cache["cost_date"][s]
    return {
        "id": F.pk(n, off + 1),
        "channel_id": ctx.cache["cost_channel"][s],
        "ad_campaign_id": ctx.cache["cost_acid"][s],
        "creative_id": F.const(n, None),
        "date": d,
        "impressions": ctx.cache["cost_impressions"][s],
        "clicks": ctx.cache["cost_clicks"][s],
        "installs": ctx.cache["cost_installs"][s],
        "cost": ctx.cache["cost_cost"][s],
        "currency": F.const(n, "CNY"),
        "created_at": d.astype("datetime64[s]") + np.timedelta64(23 * 3600 + 1800, "s"),
    }


# ---------------------------------------------------------------- 营销域

def build_user_coupons(ctx: Ctx, off: int, n: int) -> dict:
    """前 uc_n_used 行是核销闭环行：与用券订单一一对应，(user, coupon, order) 三元
    对齐、used_at = 下单时刻。领券 / 到期 / 状态全部来自 _prep_coupon_times。"""
    sl = slice(off, off + n)
    gidx = np.arange(off, off + n, dtype=np.int64)
    n_used = ctx.cache["uc_n_used"]
    used_m = gidx < n_used
    picked = ctx.cache["uc_coupon_id"][sl]
    safe = np.minimum(gidx, max(n_used - 1, 0))
    o = ctx.cache["orders_coupon_rows"][safe] if n_used else np.zeros(n, np.int64)
    placed = ctx.cache["orders"]["placed_at"][o].astype("datetime64[s]")
    used_at = np.array(placed.astype(object), dtype=object)
    used_at[~used_m] = None
    # 未使用的券不该带订单号：必须是 NULL 而不是 0
    order_id = (o + 1).astype(object)
    order_id[~used_m] = None
    return {
        "id": F.pk(n, off + 1),
        "user_id": ctx.cache["uc_user_id"][sl],
        "coupon_id": picked,
        "coupon_code": np.char.add("CP", np.char.zfill(picked.astype("U10"), 6)),
        "received_at": ctx.cache["uc_received_at"][sl],
        "expire_at": ctx.cache["uc_expire_at"][sl],
        "used_at": used_at,
        "order_id": order_id,
        # 枚举取 'unused' 而非 DDL 注释里的 'available'（卡片、金标、v1 CSV 都用 unused）
        "status": ctx.cache["uc_status"][sl],
        # 卡片：主动领取是 claim（占 60%）、赠送是 gift。
        "source": F.enum(ctx.rng("user_coupons", "source", off), n,
                         ["claim", "gift", "reward", "system"],
                         [13649, 3466, 3327, 2293]),
    }


def build_coupons(ctx: Ctx, off: int, n: int) -> dict:
    """券维表：模板来自 data/csv/coupons.csv（dims_coherence.py 已自洽化），
    total_quota 与 status 按实际发放回填（2026-11）。

    原来这张表直通 CSV：150 张券的实际发放量全部超过 total_quota（发放 6.5 万张、
    配额 1~10000），status 与有效期也互相矛盾。现在：
      status = expired（券结束早于快照） / inactive（模板下架，保留） /
               depleted（模板已领完：配额 = 实发量） / active
      total_quota：depleted 等于实发量；其余 = 实发量向上取到整百后再留 10%~100% 余量
    """
    import csv as _csv
    from pathlib import Path as _P
    rows = list(_csv.DictReader((_P(__file__).resolve().parents[2] / "data" / "csv" / "coupons.csv")
                                .open(encoding="utf-8")))
    rows.sort(key=lambda r: int(r["coupon_id"]))
    rows = rows[off:off + n]
    ca = _coupon_arrays(ctx)
    pos = {int(c): i for i, c in enumerate(ca["id"])}
    issued = ctx.cache["coupons_issued"]
    anchor = ctx.as_of_end.astype("datetime64[s]")
    rr = ctx.rng("coupons", "quota", off)
    status, quota = [], []
    for r_ in rows:
        i = pos[int(r_["coupon_id"])]
        iss = int(issued[i])
        if ca["end"][i] < anchor:
            st = "expired"
        elif r_["status"] in ("inactive", "depleted"):
            st = r_["status"]
        else:
            st = "active"
        q = iss if st == "depleted" else int(np.ceil(iss * (1.1 + 0.9 * rr.random()) / 100.0) * 100)
        status.append(st)
        quota.append(max(q, 1))

    def ts(k):
        return np.array([np.datetime64(r_[k].replace(" ", "T"), "us") for r_ in rows])

    def num(k):
        return np.array([float(r_[k]) if r_[k] != "" else None for r_ in rows], dtype=object)
    return {
        "coupon_id": np.array([int(r_["coupon_id"]) for r_ in rows], np.int64),
        "coupon_code": np.array([r_["coupon_code"] for r_ in rows], dtype=object),
        "coupon_name": np.array([r_["coupon_name"] for r_ in rows], dtype=object),
        "coupon_type": np.array([r_["coupon_type"] for r_ in rows], dtype=object),
        "discount_value": np.array([float(r_["discount_value"]) for r_ in rows]),
        "min_purchase": np.array([float(r_["min_purchase"] or 0) for r_ in rows]),
        "max_discount": num("max_discount"),
        "valid_days": np.array([int(r_["valid_days"]) for r_ in rows], np.int64),
        "start_date": ts("start_date"),
        "end_date": ts("end_date"),
        "total_quota": np.array(quota, np.int64),
        "per_user_limit": np.array([int(r_["per_user_limit"]) for r_ in rows], np.int64),
        "applicable_products": np.full(len(rows), None, dtype=object),
        "status": np.array(status, dtype=object),
        "created_at": ts("created_at"),
    }

def build_push_notifications(ctx: Ctx, off: int, n: int) -> dict:
    pid = F.pk(n, off + 1)
    sched = F.ts_window(ctx.rng("push_notifications", "scheduled_at", off), n, ctx.start,
                        ctx.days)
    # 收件人按推送日挑当天已注册的用户，时间同日后移到注册之后（原来 51% 早于注册）
    p_uid = _pick_registered_user(ctx, ctx.rng("push_notifications", "user_id", off),
                                  _day_of(ctx, sched))
    sched = _after_reg(ctx, sched, p_uid)
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
    # 活动推送只挂「推送当时在档期内」的活动（原来 94% 落在档期外）；当时没有在档期的
    # 活动就不挂（campaign_id 为空）。草稿 / 已取消 / 待开始的活动不发推送。
    per = ctx.dim_ids["_campaign_periods"]
    cids = np.array([c for c, (a, b, st) in per.items()
                     if st not in ("draft", "cancelled", "scheduled") and a is not None], dtype=np.int64)
    c_lo = np.array([per[c][0] for c in cids], dtype="datetime64[s]")
    c_hi = np.array([per[c][1] for c in cids], dtype="datetime64[s]")
    st_s = sched.astype("datetime64[s]")
    u = ctx.rng("push_notifications", "cid", off).random(n)
    chosen = np.zeros(n, dtype=np.int64)
    B = 200_000                                     # 分块：行 × 活动的布尔矩阵控制在几十 MB
    for a in range(0, n, B):
        t = st_s[a:a + B, None]
        live = (c_lo[None, :] <= t) & (t <= c_hi[None, :])
        cnt = live.sum(1)
        pick = (u[a:a + B] * cnt).astype(np.int64)
        rank = np.cumsum(live, axis=1) - 1
        chosen[a:a + B] = np.where(live & (rank == pick[:, None]), cids[None, :], 0).max(axis=1)
    campaign = np.where(is_mk & (chosen > 0), chosen, None).astype(object)
    return {
        "push_id": pid,
        "user_id": p_uid,
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
        # 失败率（= ~is_delivered 的比例）一点没变，变的是失败原因不再只有一个桶。
        "failure_reason": np.where(delivered, None,
                                   F.enum(ctx.rng("push_notifications", "fail_reason", off),
                                          n, PUSH_FAILURE_REASONS, PUSH_FAILURE_REASONS_W)),
        "created_at": sched,
    }


# ---------------------------------------------------------------- 实验域

def _prep_ab_assignments(ctx: Ctx) -> None:
    """分组时间落在 [max(实验开始, 用户注册), min(实验结束, 窗末)]（2026-11 数据普查：原来全窗
    独立抽，51% 早于注册、27% 落在实验档期外）。区间为空的对（用户注册时实验已结束）剔除，
    行数因此回写（main.ROWS_WRITEBACK 登记了容差）。"""
    a, b = ctx.pairs["ab_test_assignments"]
    per = ctx.dim_ids["_test_periods"]
    reg = ctx.cache["users_registered_at"][a - 1].astype("datetime64[s]")
    t_lo = np.array([per[int(t)][0] for t in b], dtype="datetime64[s]")
    t_hi = np.array([per[int(t)][1] if per[int(t)][1] is not None else ctx.as_of_end
                     for t in b], dtype="datetime64[s]")
    # 实验开始时刻带微秒，截到秒会早于开始——向上取整到下一秒
    lo = np.maximum(reg + np.timedelta64(60, "s"), t_lo + np.timedelta64(1, "s"))
    hi = np.minimum(t_hi, ctx.as_of_end)
    keep = lo < hi
    a, b, lo, hi = a[keep], b[keep], lo[keep], hi[keep]
    room = (hi - lo).astype("timedelta64[s]").astype(np.int64)
    r = ctx.rng("ab_test_assignments", "assigned_at", "global")
    ctx.pairs["ab_test_assignments"] = (a, b)
    ctx.cache["ab_assigned_at"] = lo + (r.random(len(a)) ** 1.6 * room).astype("timedelta64[s]")
    ctx.rows["ab_test_assignments"] = len(a)


def build_ab_test_assignments(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["ab_test_assignments"]
    uid, tid = a[off:off + n], b[off:off + n]
    # variant 必须属于所分配的 test：从该 test 的 variant 里挑
    v_by_test = ctx.dim_ids["_variants_by_test"]
    r = ctx.rng("ab_test_assignments", "variant_id", off)
    variant = np.array([vs[r.integers(0, len(vs))] for vs in (v_by_test[t] for t in tid)],
                       dtype=np.int64)
    assigned = ctx.cache["ab_assigned_at"][off:off + n]
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
    "channel_daily_costs": build_channel_daily_costs,
    "user_coupons": build_user_coupons,
    "coupons": build_coupons,
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
    # channel_daily_costs 依赖 user_attributions（成本从归因新客反推），
    # 也依赖 ad_campaigns 那张透传维表（挂活动 id），所以排在归因之后。
    "user_attributions", "channel_daily_costs",
    # coupons 的配额 / 状态从 user_coupons 的实际发放回填（2026-11 移出直通维表）
    "user_coupons", "coupons", "push_notifications",
    "ab_test_assignments",
    "subscriptions",          # 依赖 payments
]
