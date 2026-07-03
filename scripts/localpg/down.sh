#!/bin/bash
# 本地 Postgres rig —— 停止集群。加 --destroy 连数据目录一起删(下次 up 从零 init)。
# 用法:scripts/localpg/down.sh            # 仅停止
#       scripts/localpg/down.sh --destroy  # 停止并删除 .pgdata
set -euo pipefail

PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
PGDATA="${PGDATA:-$PROJ/.pgdata}"

if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "[down] stopping Postgres"
  "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop >/dev/null
else
  echo "[down] not running"
fi

if [ "${1:-}" = "--destroy" ]; then
  echo "[down] destroying $PGDATA"
  rm -rf "$PGDATA"
fi
