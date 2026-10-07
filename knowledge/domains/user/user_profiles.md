# user_profiles - 用户画像表

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | BIGINT | 主键，关联 users.user_id |
| age | INT | 年龄 |
| gender | VARCHAR(10) | 性别 |
| birth_date | DATE | 出生日期 |
| city | VARCHAR(50) | 城市 |
| province | VARCHAR(50) | 省份 |
| country | VARCHAR(50) | 国家。**实测 500/500 行都是 `'中国'`**（中文，不是 `'China'`）——退化列，别用来分组，按它 `GROUP BY` 只会得到一行 |
| interests | `array<string>` | 兴趣标签数组（不是 Postgres 的 `TEXT[]`；Trino 里下标从 1 起，取元素用 `element_at(interests, 1)`） |
| occupation | VARCHAR(50) | 职业 |
| income_level | VARCHAR(20) | 收入水平 |
| created_at | TIMESTAMP | 记录创建时间 |
| updated_at | TIMESTAMP | 记录更新时间 |

> **`birth_date` 查不到。** 它在表里，但不在 agent 角色的授权面里（Lake Formation
> 列级排除，见 `scripts/lakehouse/governance.py`）：`SELECT *` 里没有它，点名查报
> `COLUMN_NOT_FOUND`。要年龄分层用 `age`——它是同一份事实的低精度版本，
> 精确到天的出生日期是可直接定位到人的 PII，所以只留 `age`。

## 字段枚举值

### gender 性别
| 值 | 说明 | 实测行数 |
|----|------|------|
| female | 女 | 258 |
| male | 男 | 242 |

> 500 行只有这两个值，没有 `unknown`（旧文档写过），这一列也没有 NULL。

### income_level 收入水平
| 值 | 说明 | 实测行数 |
|----|------|------|
| medium | 中等收入 | 230 |
| low | 低收入 | 119 |
| high | 高收入 | 105 |
| very_high | 超高收入 | 46 |

### occupation 职业
| 值 | 说明 | 实测行数 |
|----|------|------|
| 公务员 | | 49 |
| 教师 | | 41 |
| 工程师 | | 38 |
| 产品经理 | | 37 |
| 设计师 | | 35 |
| 金融从业者 | | 34 |
| 医生 | | 34 |
| 程序员 | | 33 |
| 自由职业 | | 33 |
| 律师 | | 32 |
| 运营 | | 30 |
| 学生 | | 28 |
| 销售 | | 27 |
| 市场营销 | | 25 |
| 企业主 | | 24 |

> 15 个取值、无 NULL、无"其他"兜底档。这一列以前**整个没在卡片里声明过**，
> 于是它的字面值只能靠猜——而它们全是中文，猜英文（`'engineer'`、`'student'`）
> 会**返回 0 行且不报错**。2026-08-28 按库里实测补上。
> 注意 `工程师` 与 `程序员` 是**两个独立取值**，问"技术岗"要把两个都算进去。

### interests 兴趣标签
| 值 | 说明 | 实测行数 |
|----|------|------|
| 运动健身 | | 119 |
| 摄影 | | 113 |
| 汽车 | | 112 |
| 美食烹饪 | | 110 |
| 读书学习 | | 106 |
| 美妆护肤 | | 105 |
| 旅游出行 | | 97 |
| 音乐影视 | | 96 |
| 时尚穿搭 | | 95 |
| 金融理财 | | 95 |
| 数码科技 | | 95 |
| 家居生活 | | 93 |
| 宠物 | | 93 |
| 游戏电竞 | | 93 |
| 母婴育儿 | | 86 |

> **这一列是 `array<string>`，不是标量。** 上面的"实测行数"是**含该标签的用户数**，
> 一个用户带 1–4 个标签，所以这一列的行数合计（1,458）大于表行数（500）。
> 过滤用 `contains(interests, '摄影')`，展开用
> `CROSS JOIN UNNEST(interests) AS t(tag)`——Trino 里数组**不能**用 `[]` 下标当过滤条件。
>
> 标签全是中文。这一列以前在卡片里写的是 16 个**英文**标签
> （`fashion, electronics, sports, ...`），与库里 15 个取值**交集为空**——
> `contains(interests, 'electronics')` 返回 0 行且不报错。2026-08-28 按库里实测改回来。

## 索引

- PRIMARY KEY: `user_id`
- INDEX: `gender`, `city`, `province`

## 常用查询

### 用户年龄分布
```sql
SELECT
    CASE
        WHEN age < 18 THEN '0-17'
        WHEN age < 25 THEN '18-24'
        WHEN age < 35 THEN '25-34'
        WHEN age < 45 THEN '35-44'
        ELSE '45+'
    END AS age_group,
    COUNT(*) AS user_count
FROM user_profiles
GROUP BY 1
ORDER BY 1;
```

### 性别分布
```sql
SELECT
    gender,
    COUNT(*) AS user_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) AS pct
FROM user_profiles
GROUP BY gender;
```

### 城市 TOP 10
```sql
SELECT city, COUNT(*) AS user_count
FROM user_profiles
WHERE city IS NOT NULL
GROUP BY city
ORDER BY user_count DESC
LIMIT 10;
```

### 兴趣标签分布
```sql
-- 数组展开只能放在 FROM 里（Trino 不允许 SELECT unnest(...)）；
-- CROSS JOIN UNNEST 本身就会丢掉 NULL / 空数组的行，不用再加 IS NOT NULL
SELECT
    t.interest,
    COUNT(*) AS user_count
FROM user_profiles
CROSS JOIN UNNEST(interests) AS t(interest)
GROUP BY 1
ORDER BY user_count DESC;
```
