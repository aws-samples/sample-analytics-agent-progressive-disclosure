#!/bin/bash
# 启动 App Analytics Agent 后端（含本地 Postgres 自检 + Bedrock 路由）
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"
PGBIN=/opt/homebrew/opt/postgresql@16/bin

# —— Bedrock / 模型 ——
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-opus-4-8}"

# —— 本地数据库 ——
export PGHOST=127.0.0.1
export PGPORT="${PGPORT:-5433}"
export PGDATABASE=app_analytics
export PGUSER=postgres

# 确保本地 Postgres 在跑
if ! "$PGBIN/pg_ctl" -D "$HERE/.pgdata" status >/dev/null 2>&1; then
  echo "[run] 启动本地 Postgres ..."
  "$PGBIN/pg_ctl" -D "$HERE/.pgdata" -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" -l "$HERE/.pgdata/server.log" -w start
fi

echo "[run] model=$ANTHROPIC_MODEL region=$AWS_REGION db=$PGHOST:$PGPORT"
echo "[run] 打开 http://127.0.0.1:${PORT:-8000}/"
cd "$HERE"
exec "$HERE/.venv/bin/python" -m uvicorn server:app --host 127.0.0.1 --port "${PORT:-8000}"
