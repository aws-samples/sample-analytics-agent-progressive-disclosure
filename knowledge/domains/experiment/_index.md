# 实验域 (Experiment Domain)

## 域概述

实验域管理 A/B 测试和实验相关数据，支持产品团队进行功能实验、优化决策和数据驱动迭代。该域覆盖从实验设计、用户分流到结果分析的完整流程。

## 表清单

| 表名 | 说明 | 详情文件 |
|------|------|----------|
| ab_tests | A/B测试主表，实验配置 | `ab_tests.md` |
| ab_test_variants | 测试变体定义 | `ab_test_variants.md` |
| ab_test_assignments | 用户分组记录 | `ab_test_assignments.md` |

## 表间关系

```
ab_tests (主表)
  ├── ab_test_variants (1:N) - 每个测试包含多个变体
  └── ab_test_assignments (1:N) - 记录用户分配到的变体
              └── ab_test_variants (N:1) - 每条分配关联一个变体
```

## 关键词路由

根据具体问题加载对应表文件：

| 关键词 | 加载文件 |
|--------|----------|
| 实验、测试、假设、指标、流量 | `ab_tests.md` |
| 变体、对照组、实验组、配置 | `ab_test_variants.md` |
| 分流、分组、曝光、分配 | `ab_test_assignments.md` |

## 常见分析场景

1. **实验配置查询**: 加载 `ab_tests.md`
2. **变体对比分析**: 加载 `ab_tests.md` + `ab_test_variants.md`
3. **实验效果分析**: 加载全部三个表
4. **流量分配监控**: 加载 `ab_test_variants.md` + `ab_test_assignments.md`
