#!/usr/bin/env bash
# arm4（Aurora/pg_duckdb）自管实例的 cloud-init —— EC2 首次启动跑一次。
# 做四件事：装 docker → 跑官方 pg_duckdb 镜像 → 预热 extension → 建只读角色并打开
# 非超级用户的 duckdb 执行权限。provision.py 会把下面的 __占位符__ 替换掉再上传。
#
# 为什么是「EC2 上跑官方 docker 镜像」而不是裸机编 pg_duckdb：
#   pg_duckdb 把 DuckDB 作为 git submodule 编进扩展，自己编容易撞 API 漂移、也就不
#   「可复现」了。官方镜像 pgduckdb/pgduckdb:17-v1.1.1 是离线验证过的那一个
#   （内嵌 DuckDB 1.4.3，arm2 已钉到同一版本 —— 见 README「引擎版本」一节）。
#   换版本只改下面 PG_IMAGE 这一行的 tag。
#
# 为什么 --network host：
#   DuckDB 的 credential_chain 要读实例角色，需访问 IMDS(169.254.169.254)。容器默认
#   比实例多一跳，而 IMDSv2 默认 hop limit=1 会把这一跳挡掉（provision.py 另把实例的
#   hop limit 设成 2 做双保险）。host 网络让容器与实例同网络栈，IMDS 在 hop=1 下就够
#   得着；顺带把 5432 直接绑到实例网卡——入站由 SG 限定只对 Fargate 任务 SG 开。
set -euxo pipefail

PG_IMAGE="pgduckdb/pgduckdb:17-v1.1.1"
REGION="__REGION__"
PW_SECRET_ARN="__PW_SECRET_ARN__"
PGDATABASE="app_analytics"
RO_USER="analytics_agent_ro"
DUCKDB_ROLE="duckdb_users"

dnf install -y docker jq awscli
systemctl enable --now docker

# 从这里起关掉 xtrace：下面要碰明文口令（docker 的 -e、CREATE ROLE PASSWORD）。开着
# set -x 会把口令回显进串口控制台和 /var/log/cloud-init-output.log，凡有
# ec2:GetConsoleOutput 的人都能读。`{ set +x; } 2>/dev/null` 让关闭动作自身也不回显。
# 后续步骤用显式 echo 报进度，不靠 xtrace。
{ set +x; } 2>/dev/null

# 口令从 Secrets Manager 取（实例角色有 GetSecretValue，限定这一个 secret）。
# superuser 与只读角色用同一串即可——这是只读对比台，不是多租户库。
PGPW="$(aws secretsmanager get-secret-value --region "$REGION" \
          --secret-id "$PW_SECRET_ARN" --query SecretString --output text | jq -r .password)"

docker rm -f pgduck 2>/dev/null || true
docker run -d --name pgduck --restart always --network host \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB="$PGDATABASE" \
  -v pgduck-data:/var/lib/postgresql/data \
  "$PG_IMAGE"

# 等库起来
for i in $(seq 1 60); do docker exec pgduck pg_isready -U postgres && break; sleep 2; done

# 第一遍：建扩展、预热 extension 下载、建角色、打开非超级用户的 duckdb 执行权限。
#   - 预热 INSTALL：否则第一条真实查询要现连公网装 httpfs/aws/iceberg，叠加 db.py
#     的 15s statement_timeout（SQL_TIMEOUT_MS）会被掐掉（实测现象）。
#   - duckdb.postgres_role：pg_duckdb 默认只有 superuser 能跑 duckdb.*。把它指向
#     duckdb_users 这个组角色、让 analytics_agent_ro 作为其成员，db.py 就能以最小权限
#     的 ro 角色连进来跑 duckdb.query()。**这个 GUC 要重启才生效，reload 不够**（实测）。
docker exec -i pgduck psql -U postgres -d "$PGDATABASE" -v ON_ERROR_STOP=1 <<SQL
CREATE EXTENSION IF NOT EXISTS pg_duckdb;
SELECT duckdb.raw_query('INSTALL httpfs; INSTALL aws; INSTALL iceberg;');
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${DUCKDB_ROLE}') THEN
    CREATE ROLE ${DUCKDB_ROLE} NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${RO_USER}') THEN
    CREATE ROLE ${RO_USER} LOGIN PASSWORD '${PGPW}' IN ROLE ${DUCKDB_ROLE};
  END IF;
END \$\$;
ALTER SYSTEM SET duckdb.postgres_role = '${DUCKDB_ROLE}';
SQL

# 重启让 duckdb.postgres_role 生效（数据卷持久化，角色/扩展都还在）。
docker restart pgduck
for i in $(seq 1 60); do docker exec pgduck pg_isready -U postgres && break; sleep 2; done

# 冒烟：以 ro 角色确认它真能跑 duckdb.query（而不是只有 superuser 能）。
docker exec -i pgduck psql -U postgres -d "$PGDATABASE" -v ON_ERROR_STOP=1 <<SQL
SET ROLE ${RO_USER};
SELECT * FROM duckdb.query('SELECT 1 AS bootstrap_ok');
SQL

echo "arm4 pg_duckdb bootstrap 完成：ro 角色 ${RO_USER} 可执行 duckdb 查询。"
