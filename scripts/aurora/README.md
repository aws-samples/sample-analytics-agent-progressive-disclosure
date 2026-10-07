# arm4 — Aurora / pg_duckdb

第 4 条对比 arm：一个**统一的 Postgres 线协议入口**，查询由内嵌的 DuckDB（pg_duckdb）
**直读与 athena/duckdb 两条 arm 同一批** S3 Tables Iceberg。它对应 mentor「数据后台
统一走 Aurora 入口 + 直查 Iceberg」的方向。

## 为什么是「自管 PG」而不是托管 Aurora

托管 Aurora PostgreSQL 装不了 pg_duckdb——它是 C 扩展（共享库），不在 Aurora 的扩展
白名单里（PG 12–18 都核实过），pg_tle 又只能装 SQL/PL 扩展、装不了 C 库。而「直查
Iceberg」这件事**必须**靠 pg_duckdb。两者不能同时成立于托管 Aurora，于是保留「统一的
Postgres 入口」这一面，用一台自管 PG + pg_duckdb 顶上。对一个只读对比台，运维成本可忽略；
代价是失去 Aurora 的托管品牌，但对比的变量（引擎 + 入口）一点不少。

## 它在对比里的位置

三条已有 arm 跑在同一个 Fargate 任务里（见 `scripts/cloud_arms.sh`）。arm4 的 backend
进程仍在那个任务里，经 PG 线协议连到这台 EC2 上的 pg_duckdb——所以它像 **athena/
redshift**（backend 在任务里、隔一跳网络连一个引擎），**不像** in-process 的 duckdb arm。
读的是同一批 Iceberg（ATTACH 的 ARN/catalog/namespace 由 `backend/db.py` 复用 duckdb arm
的 `scripts/duckdb/conn.py`），残差只落在「引擎 + 入口」上。

## 文件

| 文件 | 作用 |
|---|---|
| `provision.py` | boto3 provisioning：`setup` / `up` / `status` / `down` / `destroy`。只在带子命令时动云资源。 |
| `user-data.sh` | EC2 cloud-init：装 docker → 跑官方 pg_duckdb 镜像 → 预热 extension → 建只读角色并打开非超级用户的 duckdb 执行权限。 |

## 怎么起

```bash
# Isengard dev 凭证，us-west-2
python3 scripts/aurora/provision.py setup    # 建 secret + 实例角色 + SG（不起实例）
python3 scripts/aurora/provision.py up       # 起 EC2，打印 AURORA_PGHOST
python3 scripts/aurora/provision.py status
python3 scripts/aurora/provision.py rotate   # 换 secret 口令（泄露补救，需随后 down+up）
python3 scripts/aurora/provision.py down     # 终止实例（secret/角色/SG 留着）
python3 scripts/aurora/provision.py destroy  # 连 secret/角色/SG 一起删
```

起好后把 `up` 打印的私网 IP 填进 Fargate 任务环境变量：

```
DB_BACKEND=aurora
AURORA_PGHOST=<实例私网 IP>   AURORA_PGPORT=5432
AURORA_PGDATABASE=app_analytics   AURORA_PGUSER=analytics_agent_ro
AURORA_PGPASSWORD  ← 从 provision.py 建的 secret 注入（任务角色需 GetSecretValue）
```

`db.py` 不读明文口令：`AURORA_PGPASSWORD` 走 secret 注入。

## 已离线验证（throwaway 容器，`pgduckdb/pgduckdb:17-v1.1.1`）

本 arm 的代码路径与关键配置都在真容器上跑通过，不是纸面推导：

- pg_duckdb 用与 duckdb arm **同一条 ATTACH 咒语**连上同一批 S3 Tables Iceberg，
  `events` 读出 8,541,400 行（= arm1/arm2，证明是同一份物理文件）。
- agent SQL 必须包进 `SELECT * FROM duckdb.query(<字面量>)`：透明执行会在 Postgres
  解析器层就拒掉三段式名（`cross-database references`）。
- SQL 必须**内联成字面量**（`psycopg.sql.Literal`），不能用绑定参数——扩展协议把
  参数标成 `UNKNOWN` 类型，pg_duckdb 转不动。
- 非超级用户要跑 `duckdb.*`：须 `ALTER SYSTEM SET duckdb.postgres_role=<组角色>` + 让
  连接角色作为其成员，且**要重启才生效，reload 不够**。`user-data.sh` 已据此编排。

## 引擎版本（与 arm2 的差异）

pg_duckdb 把 DuckDB 作为 git submodule 编进扩展，不能独立换版本。最新 release：

| pg_duckdb | 内嵌 DuckDB |
|---|---|
| v1.1.1（最新） | 1.4.3 |
| v1.1.0 | 1.4.2 |
| v1.0.0 | 1.3.2 |

没有任何 pg_duckdb release 带 1.5.x，arm4 封顶 1.4.3。为让两条 DuckDB arm 是**字节同一个
引擎**（残差只落在「入口」上、引擎不再是变量），已把 arm2 从浮动的 1.5.5 **钉到 1.4.3**，
而不是把 arm4 升到 1.5.5（后者做不到）。改动落在两处 pip pin，改一处要改两处：

| 文件 | 行 | 版本 |
|---|---|---|
| `backend/Dockerfile.arms` | ~40 | `duckdb==1.4.3` |
| `scripts/bench/Dockerfile` | ~38 | `duckdb==1.4.3` |

**对已有结果的影响**：correctness / consistency 用精确聚合（count/SUM/MIN/MAX/bool），
跨这两个 minor 版本稳定——离线已实测 `events` 两版同为 8,541,400。方言特性
（INTERVAL/strftime/substr/approx_count_distinct）自 ≤1.0 起稳定，extension 随核心版本自动匹配。
**只有延迟数需要换 1.4.3 后重跑**（换引擎版本本就该重测），而对比报告尚未定稿，没有锁定值被作废。
唯一理论边角：`approx_count_distinct`（HLL）两版可能有极小差异，但 consistency 门用的是精确 count，不受影响。

**重建提醒**：`scripts/bench/Dockerfile` 从上一版镜像 COPY 预编的 extension，首次降版到 1.4.3
时 ext 基底仍是旧版，构建期的版本校验 LOAD 会报错——须在能连 extensions.duckdb.org 的网络里
重新自举一遍 ext 基底（见该 Dockerfile 顶部注释）。arm2 的 serving 镜像（`Dockerfile.arms`）运行时
现装 extension、无此自举耦合，改 pip pin 即可。

## 接进 arms 任务（让 arm4 在浏览器对比里可用）

实例起好后，arm4 还要进云上那个四合一 arms 任务才能在浏览器里切到。顺序**不能乱**，
镜像的 extension 自举有前置：

```bash
# 0) 前提：provision.py setup + up 已跑过，arm4 实例在跑（status 看得到私网 IP）。

# 1) 重建 arms 镜像到 duckdb 1.4.3、带 8003 新 CMD。extension 改成**运行时**装
#    （见 backend/Dockerfile.arms：镜像在 Fargate 有公网出网，第一次连接时 conn.py 装
#    httpfs/aws/iceberg）。所以不再需要 ext 基底、也没有版本自举前置，两步直构即可：
docker build -f backend/Dockerfile      -t analytics-agent-backend:base .
docker build -f backend/Dockerfile.arms -t analytics-agent-backend:arms .

# 2) 推 ECR（arms 任务的镜像是 ...amazonaws.com/analytics-agent-backend:arms）
aws ecr get-login-password --region us-west-2 | docker login --username AWS \
  --password-stdin 111122223333.dkr.ecr.us-west-2.amazonaws.com
docker tag analytics-agent-backend:arms \
  111122223333.dkr.ecr.us-west-2.amazonaws.com/analytics-agent-backend:arms
docker push 111122223333.dkr.ecr.us-west-2.amazonaws.com/analytics-agent-backend:arms

# 3) 注册带 arm4 的新一版 task-def + 给执行角色补 GetSecretValue（现查实例 IP、不写死）
python3 scripts/aurora/wire_arms.py            # 预演，看将写入什么
python3 scripts/aurora/wire_arms.py --apply    # 真注册

# 4) 让新版 + 新镜像生效
bash scripts/cloud_arms.sh down && bash scripts/cloud_arms.sh up

# 5) 验证：开隧道到 aurora，浏览器 http://127.0.0.1:8000/ ；或 health 看四条引擎
bash scripts/cloud_arms.sh tunnel aurora
```

注：arms 镜像已采用运行时装 extension（去掉了 `COPY --from=ext` 和构建期版本校验 LOAD），
所以上面没有 ext 自举那一步。`scripts/bench/Dockerfile`（延迟基准那条链）仍是构建期烘
extension 的老路子，与本 arm4 部署无关；它若要降到 1.4.3，仍需一次 ext 基底重自举。

## 成本与闲置

实例按 `AURORA_INSTANCE_TYPE`（默认 `t3.large`）计费。不用时 `down` 终止；需要时
`up` 重起（数据卷会随实例一起销毁，重起后 user-data 会重新 bootstrap——这是只读台，
不保存状态）。与其它 arm 的闲置策略一致：留栈/角色、停算力。
