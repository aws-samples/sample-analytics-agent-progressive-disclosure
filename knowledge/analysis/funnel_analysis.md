# 分析方法 · 漏斗转化 (Funnel Analysis)

> 套路：用户从一步到下一步会流失，漏斗分析定位**最大流失环节**，那就是优化杠杆。

## 何时用
"转化率""从浏览到下单流失在哪""注册→激活→付费漏斗""加购转化"等。

## 标准漏斗（电商）
常见链路（按业务取其一）：
- 浏览/曝光 → 加购 → 下单 → 支付（成交）
- 注册 → 激活(首次行为) → 首单 → 复购
事件来自 events / sessions / orders；先 read_doc 对应表文档拿事件名/状态枚举。

## 两条硬约束（不满足就不是漏斗，先看这里）

下面的公式全都假设 n₁≥n₂≥…≥nₖ。**这个单调性不会自己成立，要靠 SQL 保证。**

**约束 A · 逐层收窄**：第 i+1 步的用户集必须是第 i 步的**子集**
（`AND user_id IN (上一步的用户集)`）。把每步各数一遍再排在一起**不是漏斗**——
那是四个互不相干的集合，完全可能出现"结算人数 > 浏览人数"。

**约束 B · 时间窗要说出来**：漏斗的每一步都是"做过没有"，所以窗口越宽人数越接近，
形状越平。问句点明了范围（"近 30 天"）就按它算，锚点取
`(SELECT max(as_of_date) FROM meta_snapshot)`，**不要用某张表自己的 `max()`**；
问句没点明，按全局规则走**全量**（不要自己默认成近 30 天）。
两种都是合法口径，但**必须在 method 里写出来是哪一种**——同一份数据全量
`394/314/258/199`、近 30 天 `197/101/58/32`，量级完全不同。
"窗口太宽所以看着不衰减"不是可以省掉这句话的理由，它是**更该写出来**的理由。

**算完自检**：核对 n₁≥n₂≥…≥nₖ。**不成立 ⟹ 你的 SQL 违反了约束 A，回去改 SQL。**
不要把它解释成"数据质量问题"或"这不是真实业务漏斗形态"——那是把自己的口径错误
归因给了数据，结论会整个反过来。（本项目真发生过：独立计数得到
`394/385/396/373`，据此断言"数据不衰减"，而正确口径下是 `394/314/258/199`，
逐层流失 20%/18%/23%，健康得很。）

### 参考写法（无序子集漏斗，本项目的默认口径）

```sql
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
w AS (
  SELECT user_id, event_name
  FROM events, a
  WHERE CAST(event_time AS date) > a.d - interval '30' day
    AND CAST(event_time AS date) <= a.d
    AND event_name IN ('view_product','add_to_cart','begin_checkout','purchase')
),
s1 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'view_product'),
s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'add_to_cart'
       AND user_id IN (SELECT user_id FROM s1)),
s3 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'begin_checkout'
       AND user_id IN (SELECT user_id FROM s2)),
s4 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'purchase'
       AND user_id IN (SELECT user_id FROM s3))
SELECT (SELECT count(*) FROM s1) AS step1_view,
       (SELECT count(*) FROM s2) AS step2_cart,
       (SELECT count(*) FROM s3) AS step3_checkout,
       (SELECT count(*) FROM s4) AS step4_purchase
```

### 口径声明（必须写进 method，别默认读者知道）

- **无序子集漏斗**（上面那条，默认用它）：只要求"也做过上一步"，不要求先后次序。
  各家产品分析工具的 unordered funnel 就是这个。
- **严格时序漏斗**：还要求每步时间晚于上一步（按 `min(event_time)` 逐步比较）。
  更严格，但**本项目的数据集上它退化**——生成器按独立事件产出、不是因果序列，
  近 30 天算下来末步是 0。要用它就得同时说明这一点，否则"支付转化 0%"会被当成业务结论。
- **绝对值不可比**（口径对了也要写这句）：本数据集的转化率**显著高于真实业务，
  绝对值不具参考性**——全量端到端 `199/394 = 50.5%`、近 30 天 `32/197 = 16.2%`，
  真实电商同口径（用户级、长窗）通常在个位数百分比。原因在分母：漏斗顶端
  "看过就走"这一侧没按真实比例生成——实测浏览过商品的 394 人里，一次都没加购的
  只有 80 人（20.3%），真实电商这一侧是绝大多数；25 种事件各自的行数又被抽得
  近似均匀（736–866，极差比 1.18），"顶宽底窄"的量级差因此根本不存在。
  所以结论只能写**相对**判断（"加购→下单是全链路最大流失环节"），不能写
  "整体转化率 50.5%，转化表现优异"——后者是把生成器的抽样方式当成了业务表现。
  这条**不能**用来解释单调性不成立：n 不递减一定是约束 A 没做到，回去改 SQL，
  跟绝对值偏高是两件事，别混用。

## 统计公式（分步转化，请照此算并填进 method）
设各步人数 n₁≥n₂≥…≥nₖ（n₁=首步）：
- **步间转化率**：CRᵢ = nᵢ₊₁ / nᵢ —— 第 i→i+1 步的转化。
- **步间流失率**：Lossᵢ = 1 − CRᵢ = (nᵢ − nᵢ₊₁) / nᵢ
- **整体转化率**：CR_total = nₖ / n₁ = Π CRᵢ（各步转化率连乘）。
- **绝对流失人数**：Dropᵢ = nᵢ − nᵢ₊₁ —— 定位"损失最多人"的环节（和流失率高不一定同一处）。
- **瓶颈环节**：Lossᵢ 最大的那一跳 = 最该优化的杠杆。

## 做法（这几步填进 present_result.method.steps）
1. 逐步算每一步的用户数（去重 user_id），**按约束 A 逐层收窄**，得到单调递减序列；
   算完按上面的"算完自检"核一遍单调性。
2. 算**步间转化率** CRᵢ = 下一步/上一步。
3. 算**整体转化率** CR_total = 末步/首步，并核对是否 = 各步转化连乘。
4. 找出 Lossᵢ 最大的一跳 = 最大流失环节（把整体转化率、瓶颈环节转化率填进 method.stats），写成 `findings`：
   `{kind:'driver', text:'加购→下单转化仅 32%（流失68%），是全链路最大流失点', evidence:'...'}`。
5. 可按渠道/新老客切漏斗，看不同人群流失点是否不同。

## 输出要求
- chart 用 `funnel`（items 按 value 从大到小）。
- KPI：整体转化率 + 最大流失环节的转化率。
- insight 点出瓶颈环节；findings 给优化建议方向。
- 注意分母口径：每步分母是"上一步人数"还是"首步人数"要说清，别混。
- method 里写明用的是**哪种漏斗口径**（无序子集 / 严格时序），以及时间窗和锚点。
  同一份数据这三样换一个，末步人数能差 50 倍，不说清等于没给结论。
