# 本地 Postgres rig(localpg)

> **这是 v1 的 legacy 路径,保留但不再维护。** 现行数据层是 Athena + S3 Tables (Iceberg),
> 建法见 [../../docs/deployment.md](../../docs/deployment.md),对应脚本在
> `scripts/lakehouse/`。这套 rig 只服务 `DB_BACKEND=postgres`,不在
> `scripts/test_all.sh` 的覆盖范围内(它跑的是 athena 后端),边界见
> [../../docs/legacy.md](../../docs/legacy.md)。
>
> 湖仓侧的对应关系:建表 = `scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql`,
> 灌数 = `load.py`,mart = `02_mart.sql`,`meta_snapshot` 同样存 `as_of_date`,
> 「最近 N 天」的锚点口径两条路径一致。

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
