# Glue federated catalog 三方对账（迁移后）

catalog: `123456789012:analytics_agent_rs`  ·  region: ap-northeast-1

```
声明态（DDL）      35 张表
实际态（Glue）     48 张表
语义层（卡片）     47 张表卡片
治理指标           4 张表被指标引用

目录可见面实测（不是缺陷，是必须知道的边界）：
  GetTable 被挡住的 DDM 表   ['users', 'user_profiles']
  但 GetTables 仍暴露列名   {'users': ['email', 'phone'], 'user_profiles': ['birth_date']}
  未 GRANT 的表仍在目录里    ['user_messages']（目录不反映 GRANT）
  → Glue federated catalog 是 schema 投影，不是权限投影。
    真正生效的防线是查询时的 GRANT 与 DDM，别把目录可见性当访问控制。

对账通过 ✅  三方一致，DDM 在 GetTable 路径上确认生效
```

exit 0 = 当时六类检查全绿；当前版本另有 G 类治理覆盖检查。
