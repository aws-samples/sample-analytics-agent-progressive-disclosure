#!/bin/bash
# 启动 App Analytics Agent 后端。
#
# 两种数据后端，用 DB_BACKEND 切：
#
#   athena（默认）—— Athena 查 S3 Tables（Iceberg）。正式形态：表就是数据湖里的
#                    Iceberg 表，namespace 联邦进 Glue Data Catalog 直接成为
#                    Glue database，授权走 Lake Formation。
#   postgres      —— 本地 Postgres rig。**LEGACY，不再维护**：约 19 万行，没有 Glue
#                    目录，UI 元数据会降级到 information_schema。
#                    保留可跑，但不随架构演进，测试套件不覆盖。见 docs/legacy.md。
#
# 默认不指向本地库，是因为默认指向一个不再维护的本地 Postgres 会让克隆仓库的人
# 第一步就撞上 `ModuleNotFoundError: No module named 'psycopg'`。
#
# Redshift 那一支已删除（整体退役）。要还原历史形态见 git history。
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"

# 本地覆盖（gitignored，见 .env.local.example）。仓库里不放真实账号/资源 ID。
# 都要 export：uvicorn 是子进程，不 export 它看不到这些值。
#
# **命令行传的环境变量优先于 .env.local。** 原来这里是 `set -a; source`，而 source
# 是无条件赋值：`DB_BACKEND=athena ./backend/run.sh` 会被文件里的值悄悄盖掉，
# 然后跑起来的是另一个后端，而日志上看不出你的参数被无视了。
# 这个优先级跟 scripts/deploy/build_catalog_json.py 一致（那边用 setdefault）。
if [[ -f "$PROJ/.env.local" ]]; then
  while IFS= read -r _line || [[ -n "$_line" ]]; do
    _line="${_line%$'\r'}"
    [[ "$_line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$_line" == *=* ]] || continue
    _k="${_line%%=*}"; _v="${_line#*=}"
    _k="${_k//[[:space:]]/}"
    [[ "$_k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!_k+x}" ]] && continue                 # 环境里已有 → 调用方说了算
    export "$_k=$_v"
  done < "$PROJ/.env.local"
  unset _line _k _v
fi

export DB_BACKEND="${DB_BACKEND:-athena}"

# —— Bedrock / 模型 ——
export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-opus-4-8}"

if [[ "$DB_BACKEND" != "postgres" ]]; then
  # Athena 走 HTTPS + IAM：没有 host/port，不进 VPC，不需要连接池，没有密码落地。
  export AWS_REGION="${AWS_REGION:-us-west-2}"
  export ATHENA_WORKGROUP="${ATHENA_WORKGROUP:-analytics-agent-wg}"
  # 两个 workgroup：管理侧那个的结果 CSV 里有明文 PII（治理探针自己就查 email），
  # 所以治理角色（AGENT_ROLE_ARN）生效时 db.py 会切到这个，结果落在
  # athena-staging/agent/ 子前缀下、管理侧那半边它读不到。见 scripts/lakehouse/athena.py。
  export ATHENA_AGENT_WORKGROUP="${ATHENA_AGENT_WORKGROUP:-analytics-agent-ro-wg}"
  export ICEBERG_NAMESPACE="${ICEBERG_NAMESPACE:-app_analytics}"
  S3_TABLE_BUCKET="${S3_TABLE_BUCKET:-analytics-agent-tables}"
  # Athena 的 Catalog 参数用的是**不带账号前缀**的形式，Glue API 的 CatalogId 才带。
  # 两者混用会报 EntityNotFoundException，而那个报错完全看不出是这个原因。
  export ATHENA_CATALOG="${ATHENA_CATALOG:-s3tablescatalog/$S3_TABLE_BUCKET}"

  ACCT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
  if [[ ! "$ACCT" =~ ^[0-9]{12}$ ]]; then
    echo "[run] ✗ 没有可用的 AWS 凭证。Athena 靠 IAM 认证，先登录：" >&2
    echo "        aws sso login   （或设好 AWS_PROFILE）" >&2
    exit 1
  fi
  # UI 的元数据来源。这个是 Glue API 的 CatalogId，**带**账号前缀，
  # 账号刚从 sts 拿到，不写死。不配也能跑，只是 /api/catalog 会降级到
  # information_schema 并在界面上标出来（少了「元数据来自统一目录」这个演示点）。
  export GLUE_CATALOG_ID="${GLUE_CATALOG_ID:-$ACCT:s3tablescatalog/$S3_TABLE_BUCKET}"
  # 打印**生效的那个**，不是管理侧那个：设了 AGENT_ROLE_ARN 时查询实际走 agent
  # workgroup，这一行印错的话，「治理接上了没有」在启动日志上就看不出来了。
  if [[ -n "${AGENT_ROLE_ARN:-}" ]]; then
    echo "[run] backend=athena  workgroup=$ATHENA_AGENT_WORKGROUP（治理角色）  namespace=$ICEBERG_NAMESPACE"
  else
    echo "[run] backend=athena  workgroup=$ATHENA_WORKGROUP  namespace=$ICEBERG_NAMESPACE"
  fi
  echo "[run] region=$AWS_REGION  catalog=$ATHENA_CATALOG"
else
  PGBIN=/opt/homebrew/opt/postgresql@16/bin
  export AWS_REGION="${AWS_REGION:-us-east-1}"
  # 本地 Postgres 里的表**没有**进 Glue。留着 GLUE_CATALOG_ID 会让 /api/catalog
  # 从云上取表清单、从本地库取行数——两个不同的库拼成一份元数据，而且接口照样 200。
  # catalog.py 会把这种混搭写进 warning，这里直接把成因掐掉。
  if [[ -n "${GLUE_CATALOG_ID:-}" ]]; then
    echo "[run] postgres 路径：忽略 GLUE_CATALOG_ID（本地库不在 Glue 里）"
    unset GLUE_CATALOG_ID
  fi
  export PGHOST=127.0.0.1
  export PGPORT="${PGPORT:-5433}"
  export PGDATABASE=app_analytics
  export PGUSER=postgres
  if ! "$PGBIN/pg_ctl" -D "$HERE/.pgdata" status >/dev/null 2>&1; then
    echo "[run] 启动本地 Postgres ..."
    "$PGBIN/pg_ctl" -D "$HERE/.pgdata" \
      -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" \
      -l "$HERE/.pgdata/server.log" -w start
  fi
  echo "[run] backend=postgres  db=$PGHOST:$PGPORT  （LEGACY 路径，约 19 万行，不再维护）"
fi

echo "[run] model=$ANTHROPIC_MODEL"
echo "[run] 打开 http://127.0.0.1:${PORT:-8000}/"
cd "$HERE"
exec "$HERE/.venv/bin/python" -m uvicorn server:app --host 127.0.0.1 --port "${PORT:-8000}"
