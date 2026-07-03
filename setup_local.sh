#!/bin/bash
# 本地一键搭建(非 Docker):在本 worktree 内自包含地建库+灌数+建 venv。
# 全部产物落在 worktree 内(.pgdata / data / backend/.venv),不碰共享 checkout、不碰系统其它位置。
# 删掉 worktree 即可完全清理。每步自检、可重复跑。
#
# 用法:  bash setup_local.sh
# 完成后启动后端见末尾提示(需 bedrock-byoa 凭据)。
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PGBIN=/opt/homebrew/opt/postgresql@16/bin
PGDATA="$HERE/.pgdata"
PGPORT=5433
PGDB=app_analytics

echo "=========================================="
echo " App Analytics 本地搭建 (worktree 自包含)"
echo " worktree: $HERE"
echo "=========================================="

# ---------- 1. 本地 Postgres: initdb + 启动 ----------
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "[1/6] initdb 初始化本地库 ($PGDATA) ..."
  "$PGBIN/initdb" -D "$PGDATA" -U postgres --auth=trust >/dev/null
else
  echo "[1/6] .pgdata 已存在,跳过 initdb"
fi
if ! "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "      启动 Postgres :$PGPORT ..."
  "$PGBIN/pg_ctl" -D "$PGDATA" -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" -l "$PGDATA/server.log" -w start
else
  echo "      Postgres 已在运行"
fi
export PGHOST=127.0.0.1 PGPORT=$PGPORT PGUSER=postgres

# 建库(若无)
if ! "$PGBIN/psql" -p "$PGPORT" -U postgres -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$PGDB"; then
  echo "      创建数据库 $PGDB"
  "$PGBIN/createdb" -p "$PGPORT" -U postgres "$PGDB"
fi

# 已灌过数据就跳过(看 mart_daily_kpi 是否有行)
ROWS=$("$PGBIN/psql" -p "$PGPORT" -U postgres -d "$PGDB" -tAc "SELECT count(*) FROM mart_daily_kpi" 2>/dev/null || echo "0")
if [ "$ROWS" != "0" ] && [ -n "$ROWS" ]; then
  echo "[done] 数据库已就绪(mart_daily_kpi 有 $ROWS 行),无需重灌。"
  SKIP_LOAD=1
fi

# ---------- 2. venv + 依赖 ----------
echo "[2/6] 建 venv + 装依赖 ..."
if [ ! -f "$HERE/backend/.venv/bin/python" ]; then
  python3 -m venv "$HERE/backend/.venv"
fi
"$HERE/backend/.venv/bin/python" -m pip install -q --upgrade pip
"$HERE/backend/.venv/bin/pip" install -q -r "$HERE/backend/requirements.txt"
"$HERE/backend/.venv/bin/pip" install -q faker psycopg[binary]
echo "      依赖就绪"

if [ "$SKIP_LOAD" = "1" ]; then
  echo ""; echo "=== 搭建完成(数据已在) ==="; exit 0
fi

# ---------- 3-6. 用 repo 自带的 data/csv/(与云上同源, 列已匹配 DDL, 时间 2025-10~2026-01) ----------
# 关键: repo 跟踪的 data/csv/ 35 个 CSV 就是云上 docker-init 灌的同一份种子数据, 列名与 DDL
# 完全一致, 不需要任何转换/生成。照搬 docker-init.sh 的逻辑(建表->灌全部35表->建mart)即可,
# 本地结果与云上一致。(不要用 generate_data.py 现生成——它按当前日期动态生成, 时间会跑偏。)
CSVDIR="$HERE/data/csv"

echo "[3/5] 建表结构(01-08, 先重置 schema 保证幂等) ..."
"$PGBIN/psql" -v ON_ERROR_STOP=1 -p "$PGPORT" -U postgres -d "$PGDB" \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
for f in "$HERE"/database/0[1-8]_*.sql; do
  "$PGBIN/psql" -v ON_ERROR_STOP=1 -p "$PGPORT" -U postgres -d "$PGDB" -f "$f" >/dev/null
done

echo "[4/5] 灌入全部 35 表(data/csv, 按外键顺序, 显式列) ..."
TABLES=(users user_profiles user_segments user_segment_members categories products product_tags channels event_definitions user_devices ad_campaigns ad_creatives channel_daily_costs user_attributions sessions events page_views posts post_likes post_comments post_shares user_follows user_messages campaigns coupons user_coupons banners push_notifications ab_tests ab_test_variants ab_test_assignments orders order_items payments subscriptions)
for t in "${TABLES[@]}"; do
  csv="$CSVDIR/${t}.csv"
  [ -f "$csv" ] || { echo "      ⊘ $t (无 CSV)"; continue; }
  cols=$(head -1 "$csv")
  "$PGBIN/psql" -v ON_ERROR_STOP=1 -p "$PGPORT" -U postgres -d "$PGDB" \
    -c "\copy $t ($cols) FROM '$csv' WITH CSV HEADER" >/dev/null && echo "      ✓ $t"
done

echo "[5/5] 构建治理层 mart(09) ..."
"$PGBIN/psql" -v ON_ERROR_STOP=1 -p "$PGPORT" -U postgres -d "$PGDB" -f "$HERE/database/09_mart.sql" >/dev/null

echo ""
echo "=========================================="
echo " ✅ 搭建完成。验证:"
"$PGBIN/psql" -p "$PGPORT" -U postgres -d "$PGDB" -c "SELECT 'mart_daily_kpi' t, count(*) FROM mart_daily_kpi UNION ALL SELECT 'orders', count(*) FROM orders UNION ALL SELECT 'mart_daily_revenue', count(*) FROM mart_daily_revenue;"
echo ""
echo " 启动后端(需 bedrock-byoa 凭据):"
echo "   cd $HERE/backend && AWS_PROFILE=bedrock-byoa PGPORT=$PGPORT \\"
echo "     ANTHROPIC_MODEL=global.anthropic.claude-opus-4-8 \\"
echo "     .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000"
echo "=========================================="
