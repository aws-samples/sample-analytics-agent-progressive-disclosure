# 本地 Postgres rig(localpg)

本机 Docker Desktop 被组织策略锁(需 amazonians 登录),Docker initdb hook 这条路走不通。
这套脚本用 **brew postgresql@16** 起一个本机集群,作为所有本地验证的统一环境。

## 前置

```bash
brew install postgresql@16
```

## 用法

```bash
# 1. 启动集群(首次自动 initdb;端口默认 5433,匹配 backend/db.py)
scripts/localpg/up.sh

# 2. 建表 + 灌 CSV + 建 mart + 重置序列 + 建 meta_snapshot(从零重建,幂等)
scripts/localpg/load.sh

# 3. 停止(加 --destroy 连数据目录一起删)
scripts/localpg/down.sh
scripts/localpg/down.sh --destroy
```

数据目录是 `./.pgdata`(已 gitignore)。可用环境变量覆盖:`PGPORT`(默认 5433)、
`PGDATA`、`PGDATABASE`(默认 app_analytics)、`PGBIN`、`PGUSER`。

## 与现有产物的关系

- 取代 `scripts/docker-init.sh`(Docker initdb hook 版)的本地用法;COPY 顺序、序列重置逻辑一致。
- `load.sh` 第 0 步 `DROP SCHEMA public CASCADE` 让它可反复重跑(从零重建,不报 already exists)。
- `scripts/snapshot_date.sql` 建 `meta_snapshot` 单行表,存数据集"今天"(`as_of_date`),
  口径与 `database/09_mart.sql` 的 bounds CTE 一致。查询"最近 N 天"用它,**禁用 current_date/now()**。

## 验证基线(P0)

从零 `up + load` 应得到:**35 原始表 + 4 mart + 1 meta = 40 表,约 19.2 万行**,
`meta_snapshot.as_of_date = 2026-01-24`(数据窗口末日)。两次 `load` 行数与 mart 值完全一致。
