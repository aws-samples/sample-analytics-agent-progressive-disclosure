#!/bin/bash
# 本地 Postgres rig —— 启动(或初始化并启动)一个本机集群。
# 背景:本机 Docker Desktop 被组织策略锁(需 amazonians 登录),Docker initdb hook
#       这条路走不通。这里用 brew postgresql@16 起一个本机集群,所有本地验证都走它。
# 端口默认 5433,匹配 backend/run.sh 与 backend/db.py 的 PGPORT 默认值。
#
# 用法:scripts/localpg/up.sh          # init(若需)+ start + createdb
#       PGPORT=5433 scripts/localpg/up.sh
set -euo pipefail

PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
PGDATA="${PGDATA:-$PROJ/.pgdata}"      # gitignored
PGPORT="${PGPORT:-5433}"
PGDATABASE="${PGDATABASE:-app_analytics}"
PGUSER="${PGUSER:-postgres}"

if [ ! -x "$PGBIN/initdb" ]; then
  echo "ERROR: postgresql@16 not found at $PGBIN. Install: brew install postgresql@16" >&2
  exit 1
fi

# 1. init cluster (once)
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "[up] initdb -> $PGDATA"
  "$PGBIN/initdb" -D "$PGDATA" -U "$PGUSER" --encoding=UTF8 >/dev/null
fi

# 2. start (if not running)
if ! "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "[up] starting Postgres on port $PGPORT"
  "$PGBIN/pg_ctl" -D "$PGDATA" \
    -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" \
    -l "$PGDATA/server.log" -w start >/dev/null
else
  echo "[up] Postgres already running"
fi

# 3. create database (if absent)
if ! "$PGBIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -lqt | cut -d'|' -f1 | grep -qw "$PGDATABASE"; then
  echo "[up] createdb $PGDATABASE"
  "$PGBIN/createdb" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" "$PGDATABASE"
fi

echo "[up] ready: postgresql://$PGUSER@127.0.0.1:$PGPORT/$PGDATABASE"
