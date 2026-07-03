# 商品域 (Product Domain)

## 域概述

商品域管理商品目录和分类体系，是电商分析的基础数据层。支撑商品分析、类目运营、选品策略等业务场景，与订单域、行为域紧密关联。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| categories | 商品分类表，多级分类体系 | `categories.md` |
| products | 商品主表，核心商品信息 | `products.md` |
| product_tags | 商品标签表，运营标签 | `product_tags.md` |

## 表间关系

```
categories (分类体系)
  ├── [self-join] parent_id -> category_id (层级结构)
  └── products (1:N)
        └── product_tags (1:N)
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 分类、类目、一级/二级/三级分类 | `categories.md` |
| 商品、SKU、价格、库存、销量、评分 | `products.md` |
| 标签、促销、新品、热卖、推荐 | `product_tags.md` |
| 毛利、成本、定价 | `products.md` |

## 常见分析场景

1. **类目销售分析**: 加载 `categories.md` + `products.md`
2. **商品排行榜**: 加载 `products.md`
3. **库存预警**: 加载 `products.md`
4. **标签效果分析**: 加载 `product_tags.md` + `products.md`
5. **商品转化漏斗**: 加载 `products.md`
