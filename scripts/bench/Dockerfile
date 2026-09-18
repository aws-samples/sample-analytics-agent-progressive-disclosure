# 三 arm 基准 harness —— Fargate 镜像
#
# 为什么要有这个镜像：DuckDB 是**进程内**引擎，它的耗时就是跑它那台机器的耗时。
# 在笔记本上跑 DuckDB、同时从笔记本调云上的 Athena 和 Redshift，量到的是三样
# 不同的东西——本机算力、加上一段跨区网络往返。把三条 arm 全放进同一个 Fargate
# 任务里跑，网络位置、区域、算力口径才是同一个，差异才只剩引擎。
#
# 所以这个镜像不只装 DuckDB：它把三条 arm 的客户端都带上，三条一起在云上跑。
#
# DuckDB extension 的来源。**这一层是为了绕开一个网络封锁，不是为了省时间。**
# 构建机所在的网络到 extensions.duckdb.org 不通（http 和 https 都是 Connection
# timed out），所以 `INSTALL httpfs` 在构建时会失败。extension 是平台相关的
# （linux_amd64），本机 macOS 上那份拿不过来用，于是从**上一个构建成功的镜像**里
# 取——它是 ECR 里的，而 ECR 通。
#
# 代价说清楚：这样一来镜像有了自举关系，第一个镜像必须在能连 extensions.duckdb.org
# 的网络里构建。网络恢复之后要回到自己装，把下面那段 COPY --from=ext 换回
#   RUN python -c "import duckdb; c=duckdb.connect(); ... INSTALL/LOAD ..."
# 就行，版本校验那一步不用动——它对两种来源都成立。
# 默认取本机上一次构建出来的同名 tag。推到 ECR 之后按自己的账号覆盖：
#   docker build --build-arg \
#     EXT_SOURCE=<account-id>.dkr.ecr.<region>.amazonaws.com/analytics-agent-bench:latest ...
ARG EXT_SOURCE=analytics-agent-bench:latest
# nosemgrep: dockerfile-source-not-pinned —— 上一版镜像，按 tag 取
FROM ${EXT_SOURCE} AS ext

# nosemgrep: dockerfile-source-not-pinned —— 与 backend/Dockerfile 同口径，按 tag 固定
# 走 ECR Public 而不是 Docker Hub：这台机器所在的网络到 registry-1.docker.io 是断的
# （报的是 `Head https://registry-1.docker.io/... EOF`，看起来像超时，实际是连不通），
# ECR Public 通。镜像内容是 Docker Hub 官方 python 的同一份镜像。
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# 只装 harness 要的东西。这里**故意不装** claude-agent-sdk / node / claude CLI：
# 这个镜像跑的是 SQL 对账和计时，不跑 agent。少一层 Node 就少一处版本漂移，
# 而且镜像小拉起来快——Fargate 冷启动时间会算进第一条查询的墙钟里。
RUN pip install --no-cache-dir duckdb>=1.1 boto3>=1.34 pyarrow>=17

# 三条 arm 的客户端 + 共用 harness。lakehouse 里的 verify_load 是指标定义的唯一出处，
# gen_ddl 是列定义的唯一出处，两个都必须在。
COPY scripts/lakehouse/ /app/scripts/lakehouse/
COPY scripts/duckdb/   /app/scripts/duckdb/
COPY scripts/redshift/ /app/scripts/redshift/
COPY scripts/bench/    /app/scripts/bench/
# gen_ddl.parse_source() 读 database/0[1-8]_*.sql，那是列名列序的真源。
COPY database/ /app/database/
# 行数基准。--rows 模式拿它当基准，没有它只能做 arm 互比。
COPY data/loaded_row_counts.json /app/data/loaded_row_counts.json
# 单价缓存。带进来有两个理由，第二个才是主要的：任务角色没有 pricing:GetProducts
# （那是只读元数据，但没必要为此加权限），以及**云上和本机必须用同一份价目**，
# 否则两次跑出来的金额栏不可比，而差异来自价目更新这件事没人会想到。
# 缓存里带 fetched_at，出处不因为进了镜像而丢失。
COPY data/bench/prices-us-west-2.json /app/data/bench/prices-us-west-2.json

RUN find /app -name __pycache__ -type d -prune -exec rm -rf {} + || true

# 算力口径必须与本机一致，否则「云上更快」量的是机器不是引擎。这两个值与
# scripts/duckdb/conn.py 的默认值相同，改一处要改两处——conn.py 的 docstring 记着这件事。
# 任务本身是 4 vCPU / 16GB，内存额度仍钉在 4GB：额度决定什么时候溢写，
# 而溢写与否是耗时的主要来源，两边额度不同等于在比两种执行计划。
ENV AWS_REGION=us-west-2 \
    DUCKDB_THREADS=4 \
    DUCKDB_MEMORY_LIMIT=4GB \
    DUCKDB_TEMP_DIR=/tmp/duckdb-spill \
    PYTHONUNBUFFERED=1

# 非 root 跑。HOME 要可写：DuckDB 的 INSTALL 往 home_directory 写 extension，
# 而 conn.py 把 home_directory 指到 DUCKDB_TEMP_DIR，所以那个目录必须归它。
RUN useradd -m -u 10001 bench \
 && mkdir -p /tmp/duckdb-spill /app/data/bench \
 && chown -R bench:bench /app /tmp/duckdb-spill
USER bench
ENV HOME=/home/bench

# extension 在**构建时**就位，不留到运行时拉。运行时拉有两个问题：
# 每次冷启动多几秒且这几秒会混进第一条查询的耗时，以及任务从此依赖出网到
# extensions.duckdb.org——那条链路断了，报的是 extension 装不上，看起来像凭证问题。
#
# 两个目录都要：conn.py 把 home_directory 指到 DUCKDB_TEMP_DIR，httpfs/aws/iceberg
# 在那儿；iceberg 运行时还会自己拉 avro，那份在默认的 $HOME/.duckdb 下。少拷一个
# 就变成运行时出网，也就是上面说的那种误导性报错。
COPY --from=ext --chown=bench:bench /tmp/duckdb-spill/.duckdb /tmp/duckdb-spill/.duckdb
COPY --from=ext --chown=bench:bench /home/bench/.duckdb /home/bench/.duckdb

# 只 LOAD 不 INSTALL：这一步是**版本校验**。pip 装的 duckdb 和拷进来的 extension
# 必须是同一个版本号，否则 LOAD 会在这里就报错，而不是等到跑基准时才现形。
RUN python -c "import duckdb; c=duckdb.connect(); \
c.execute(\"SET home_directory='/tmp/duckdb-spill'\"); \
[c.execute(f'LOAD {e};') for e in ('httpfs','aws','iceberg')]; \
print('预装 extension：', duckdb.__version__, [r[0] for r in c.execute('SELECT extension_name FROM duckdb_extensions() WHERE loaded').fetchall()])"

WORKDIR /app
# 默认跑行数闸门。要换别的（计时、只跑两条 arm）在 run-task 时覆盖 command。
CMD ["python", "scripts/bench/correctness.py", "--rows"]
