#!/usr/bin/env bash
# v2 迁移验收：跑完 L0–L6，任一层失败立即退出。
#
# 完整流程与每步的预期值见 docs/test-plan-v2.md。这里只做能自动判定的部分：
#   L7（端到端 agent）要烧 LLM token，L8（负测）会临时改文件，都该有人看着跑。
#
#   bash scripts/test_all.sh           # 跳过 L3 的真实快照（查 48 张表，慢）
#   bash scripts/test_all.sh --full    # 含真实快照
#
# 用 bash 不用 sh：需要 pipefail。也别改成 zsh——`${PIPESTATUS[0]}` 在 zsh 里是
# `${pipestatus[1]}`，脚本在两个 shell 间不可移植。
set -uo pipefail

cd "$(dirname "$0")/.."

FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

# 本地覆盖（gitignored，见 .env.local.example）。账号相关值不硬编码：
# 账号在下面「前置」步骤从 sts 现算，Glue catalog ID 用它拼。
# set -a：L2/L6 会起 python 子进程，不 export 它们看不到这些值。
[[ -f .env.local ]] && { set -a; source .env.local; set +a; }
export AWS_REGION="${AWS_REGION:-ap-northeast-1}"

PASS=0
FAIL=0

hdr() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

# run <描述> <命令…>：命令 exit 0 记 PASS，否则记 FAIL 并打印输出
run() {
  local desc="$1"; shift
  local out
  if out=$("$@" 2>&1); then
    printf '  \033[32mPASS\033[0m  %s\n' "$desc"
    PASS=$((PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$desc"
    printf '%s\n' "$out" | sed 's/^/        /'
    FAIL=$((FAIL + 1))
  fi
}

# grep_run <描述> <期望正则> <命令…>：exit 0 **且**输出匹配正则才算过。
# 有些脚本对"软失败"也返回 0（比如 reconcile 不加 --strict 时），只看 exit code 会漏。
grep_run() {
  local desc="$1" want="$2"; shift 2
  local out
  out=$("$@" 2>&1)
  if [[ $? -eq 0 ]] && grep -qE "$want" <<<"$out"; then
    printf '  \033[32mPASS\033[0m  %s\n' "$desc"
    PASS=$((PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m  %s  (期望匹配 /%s/)\n' "$desc" "$want"
    printf '%s\n' "$out" | tail -20 | sed 's/^/        /'
    FAIL=$((FAIL + 1))
  fi
}

hdr "前置：AWS 身份"
ACCT=$(aws sts get-caller-identity --query Account --output text 2>&1)
if [[ ! "$ACCT" =~ ^[0-9]{12}$ ]]; then
  printf '  \033[31mFAIL\033[0m  拿不到 AWS 身份：%s\n' "$ACCT"
  echo "        后面全是云调用，先登录（aws sso login / AWS_PROFILE）。"
  exit 1
fi
# EXPECT_ACCOUNT 是可选守卫：设了（通常在 .env.local）才强制相等，防多账号环境串号。
if [[ -n "${EXPECT_ACCOUNT:-}" && "$ACCT" != "$EXPECT_ACCOUNT" ]]; then
  printf '  \033[31mFAIL\033[0m  account=%s，期望 %s（.env.local 里钉的）\n' "$ACCT" "$EXPECT_ACCOUNT"
  echo "        身份不对，后面全是云调用，先停下来。"
  exit 1
fi
printf '  \033[32mPASS\033[0m  account=%s region=%s\n' "$ACCT" "$AWS_REGION"
PASS=$((PASS + 1))
# Glue catalog ID = <账号>:<catalog 名>，账号刚从 sts 拿到，不用写死。
CATALOG="${GLUE_CATALOG_ID:-$ACCT:analytics_agent_rs}"

hdr "L0 静态自测（无云依赖）"
grep_run "生成器 fillers 自测"   "全部通过" python3 scripts/gen/selftest_fillers.py
grep_run "生成器跨表闭环自测"    "全部通过" python3 scripts/gen/selftest_closures.py
grep_run "pg→redshift 方言自测" "全部通过" python3 scripts/gen/pg_to_redshift.py --selftest
grep_run "manifest 声明态校验" "manifest OK" python3 scripts/manifest/render.py --check
grep_run "数据分类清单自测" "全部通过" python3 scripts/governance/selftest_classification.py

hdr "L1 云资源状态"
grep_run "Glue catalog 注册状态（5 步幂等 + 48 张表）" \
  "合计 48 张表" python3 scripts/glue/register_catalog.py --verify

hdr "L2 元数据对账（两条独立路径）"
grep_run "DDL ⟷ Redshift information_schema（含列顺序）" \
  "一致 ✅" python3 scripts/gen/verify_ddl_vs_redshift.py
grep_run "DDL ⟷ Glue ⟷ 知识库 ⟷ 治理覆盖（七类检查）" \
  "对账通过 ✅" python3 scripts/glue/reconcile.py --catalog "$CATALOG" --governance-state --strict

hdr "L3 数据一致性"
grep_run "归档回归（生成器预期值 ⟷ 已归档 Redshift 快照）" "一致 ✅" \
  python3 scripts/consistency/snapshot.py --compare \
    eval/baseline/consistency.generator-expected.postdatafix.json \
    eval/baseline/consistency.redshift.postdatafix.json --subset

if [[ $FULL -eq 1 ]]; then
  TMP=$(mktemp -t rs-live-XXXXXX.json)
  echo "  （真实快照：查 48 张表，约 3–5 分钟…）"
  if SNAPSHOT_BACKEND=redshift-data python3 scripts/consistency/snapshot.py \
       --out "$TMP" >/dev/null 2>&1; then
    grep_run "真实检查（生成器预期值 ⟷ 现查 Redshift）" "一致 ✅" \
      python3 scripts/consistency/snapshot.py --compare \
        eval/baseline/consistency.generator-expected.postdatafix.json "$TMP" --subset
  else
    printf '  \033[31mFAIL\033[0m  真实快照取不到\n'
    FAIL=$((FAIL + 1))
  fi
  rm -f "$TMP"
else
  echo "  （跳过真实快照，加 --full 启用。改过数据或重灌过库必须跑）"
fi

hdr "L4 治理层实测"
# 以管理员身份也应看到掩码值——TO PUBLIC 的意义就在这里。看到明文即治理失效。
grep_run "DDM：users.email 返回掩码值" \
  "masked\.invalid" python3 scripts/redshift/rsql.py "SELECT email FROM users LIMIT 1"
grep_run "DDM：3 条策略挂在 public 且 datashare 路径生效" \
  "^[[:space:]]*3 行" python3 scripts/redshift/rsql.py \
  "SELECT policy_name FROM svv_attached_masking_policy WHERE grantee='public' AND is_masking_datashare_on='t'"
# 正则必须锚定整行：rsql 会在末尾打印"耗时 45ms"这类文本，宽松匹配会假通过。
grep_run "GRANT：角色可 SELECT 45 张表" \
  "^[[:space:]]*45[[:space:]]*$" python3 scripts/redshift/rsql.py \
  "SELECT count(*) FROM svv_relation_privileges WHERE identity_name='analytics_agent_ro' AND privilege_type='SELECT'"
grep_run "GRANT：user_messages 未授权（0 行）" \
  "^[[:space:]]*0 行" python3 scripts/redshift/rsql.py \
  "SELECT relation_name FROM svv_relation_privileges WHERE identity_name='analytics_agent_ro' AND relation_name='user_messages'"

hdr "L5 查询路径"
grep_run "四条独立路径 GMV 完全相等" "GMV_PATHS_AGREE" \
  python3 scripts/redshift/rsql.py "
WITH paths AS (
  SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) v FROM mart_daily_kpi
  UNION ALL SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) FROM mart_daily_revenue
  UNION ALL SELECT CAST(SUM(gmv) AS DECIMAL(20,2)) FROM growth_daily_gmv
  UNION ALL SELECT CAST(SUM(actual_amount) AS DECIMAL(20,2)) FROM orders
    WHERE status IN ('paid','shipped','delivered')
)
SELECT CASE WHEN COUNT(*) = 4 AND COUNT(v) = 4 AND MIN(v) = MAX(v)
            THEN 'GMV_PATHS_AGREE' ELSE 'GMV_PATHS_DIFFER' END AS verdict,
       MAX(v) AS gmv
FROM paths"

grep_run "21 条金标 SQL 均可在 Redshift 执行" \
  "金标验证: 21/21 OK" env DB_BACKEND=redshift python3 eval/run_eval.py --dry-run
grep_run "3 条陷阱金标 SQL 均可在 Redshift 执行" \
  "金标验证: 3/3 OK" env DB_BACKEND=redshift python3 eval/run_eval.py \
  --cases eval/cases_traps.json --dry-run

hdr "L6 服务与前端渲染契约"
# 这一层自己起后端、测完就关，不依赖外部已有服务。
# 覆盖的是「UI 显示的是不是真的」——原来表数/行数/引擎名全写死在 HTML 里，
# 数据一变就静默错，静态检查看不出来。
UIPORT="${UIPORT:-8917}"
SRVLOG=$(mktemp -t aa-srv-XXXXXX.log)
DB_BACKEND=redshift AWS_REGION="$AWS_REGION" \
  GLUE_CATALOG_ID="$CATALOG" REDSHIFT_WORKGROUP=analytics-agent-wg \
  REDSHIFT_DATABASE=app_analytics CLAUDE_CODE_USE_BEDROCK=1 \
  ./backend/.venv/bin/python -m uvicorn server:app --app-dir backend \
  --host 127.0.0.1 --port "$UIPORT" > "$SRVLOG" 2>&1 &
SRVPID=$!
# 轮询等就绪，别用固定 sleep：Data API 首次建连有几秒抖动
UIUP=0
for _ in $(seq 1 30); do
  sleep 1
  if curl -sf "http://127.0.0.1:$UIPORT/health" >/dev/null 2>&1; then UIUP=1; break; fi
done

if [[ $UIUP -eq 1 ]]; then
  grep_run "/health 报正确的后端引擎（不是写死的 Postgres 坐标）" \
    '"engine": *"Redshift Serverless"' curl -s "http://127.0.0.1:$UIPORT/health"
  grep_run "/api/catalog 元数据来自 Glue（未降级）" \
    '"source": *"glue"' curl -s "http://127.0.0.1:$UIPORT/api/catalog"
  grep_run "前端存活探针打的 /health 可用（曾误改成 POST /ask → 405 → 永久假数据）" \
    '"ok": *true' curl -s "http://127.0.0.1:$UIPORT/health"
  if command -v node >/dev/null 2>&1; then
    grep_run "前端渲染契约（表数/行数/派生层/治理面板，中英双路径）" \
      "全部通过" node scripts/ui/render_test.mjs "http://127.0.0.1:$UIPORT"
  else
    echo "  （跳过渲染契约测试：未装 node）"
  fi
else
  printf '  \033[31mFAIL\033[0m  后端起不来，见 %s\n' "$SRVLOG"
  tail -15 "$SRVLOG" | sed 's/^/        /'
  FAIL=$((FAIL + 1))
fi
kill "$SRVPID" 2>/dev/null; wait "$SRVPID" 2>/dev/null
rm -f "$SRVLOG"

# 线上路径是另一套取数逻辑，必须单独测：CloudFront 只把 /ask 转给 relay，
# GET /api/catalog 落到 S3 拿 403，前端要退到同域静态快照 ./catalog.json。
# 这条路没法人工验（要浏览器 + Cognito 登录），而它失败时页面**照常渲染**，
# 只是「背后的数据」停在 v1 静态原文。不依赖后端和凭证，只要快照在就能跑。
if command -v node >/dev/null 2>&1 && [[ -f web/catalog.json ]]; then
  grep_run "线上 catalog 快照与 post-data-fix baseline 一致" \
    "catalog 快照新鲜度通过" python3 scripts/deploy/check_catalog_freshness.py
  grep_run "线上路径渲染契约（/api/catalog 403 → ./catalog.json 兜底）" \
    "全部通过" node scripts/ui/render_test_prod.mjs
else
  echo "  （跳过线上路径渲染契约：缺 node 或 web/catalog.json）"
fi

hdr "结果"
printf '  通过 %d · 失败 %d\n' "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo "  L0–L6 未全绿，先修再跑 L7 / L8。"
  exit 1
fi
cat <<'EOF'
  L0–L6 全绿 ✅

  还没覆盖的三层（见 docs/test-plan-v2.md）：
    L7  端到端 agent：./backend/.venv/bin/python eval/run_eval.py
        （会烧 token，并覆盖 eval/report.md；基线在 eval/baseline/）
    L8  负测：七类注入，验证检查器该报时真会报（会临时改文件）
    L9  数据质量人读审计（无 PASS/FAIL，不进门禁）：python3 scripts/audit/run.py all
EOF
