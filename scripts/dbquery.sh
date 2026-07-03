#!/bin/bash
# 本地开发用查询脚本 —— 对 docker-compose 起的本地 PostgreSQL 执行一条 SQL。
# 用法: ./scripts/dbquery.sh "SELECT COUNT(*) FROM users;"
#
# 仅供人工排查/探数;Web App 走 backend/db.py(psycopg),不依赖本脚本。
# 容器名以 docker-compose.yml 的 container_name 为准(app-analytics-db)。

if [ -z "$1" ]; then
    echo "用法: ./scripts/dbquery.sh \"SQL语句\""
    echo "示例: ./scripts/dbquery.sh \"SELECT COUNT(*) FROM users;\""
    exit 1
fi

CONTAINER="${DB_CONTAINER:-app-analytics-db}"
docker exec "$CONTAINER" psql -U postgres -d app_analytics -c "$1"
