#!/usr/bin/env bash
# 把前端推到线上：S3 静态桶 + CloudFront 失效 + 部署后实测。
#
# 只上传两个文件，**显式 cp，绝不用 s3 sync**：桶里的 config.js 是线上真实配置
# (Cognito 池/客户端 ID)，本地只有 config.example.js。sync 会把它覆盖成示例值或
# 直接 --delete 掉，登录立刻挂。vendor/ 同理，没变就不动。
#
#   bash scripts/deploy/deploy_web.sh              # 默认重新查 Glue 生成快照再发
#   bash scripts/deploy/deploy_web.sh --reuse-snapshot  # 复用现有快照（仍做 baseline 新鲜度校验）
#   bash scripts/deploy/deploy_web.sh --fresh      # 兼容旧调用；与默认行为相同
#   bash scripts/deploy/deploy_web.sh --dry-run    # 只打印要做什么
#
# 前置：web/catalog.json 必须存在且来源为 glue（线上元数据就靠它，见
# scripts/deploy/build_catalog_json.py 里的那道闸）。
set -uo pipefail
cd "$(dirname "$0")/../.."

# 本地覆盖（gitignored，见 .env.local.example）。仓库里不放任何真实账号/资源 ID：
# 账号运行时从 sts 现算，桶名按 infra/edge.yaml 的命名规律拼。
# set -a 让变量对子进程可见——--fresh 会调 build_catalog_json.py（python），
# 不 export 的话它看不到 .env.local 里的值。
[[ -f .env.local ]] && { set -a; source .env.local; set +a; }

REGION="${SITE_REGION:-us-west-2}"
STACK="${EDGE_STACK:-analytics-agent-edge}"

FRESH=1; DRY=0
for a in "$@"; do
  case "$a" in
    --fresh) FRESH=1 ;;
    --reuse-snapshot) FRESH=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "未知参数: $a"; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$1"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# ---- 身份 ----
ACCT=$(aws sts get-caller-identity --query Account --output text 2>&1) \
  || die "拿不到 AWS 身份：$ACCT"
# EXPECT_ACCOUNT 是可选守卫：设了（通常在 .env.local）才强制相等，防多账号环境串号；
# 没设就用当前凭证的账号继续——克隆仓库的人跑的本来就是自己的账号。
# 变量紧邻中文标点时必须写 ${VAR}：bash 会把全角字符的字节算进标识符，
# `$ACCT，` 会被解析成名叫 "ACCT，" 的变量，set -u 下直接 unbound variable 退出。
if [[ -n "${EXPECT_ACCOUNT:-}" && "$ACCT" != "$EXPECT_ACCOUNT" ]]; then
  die "账号是 ${ACCT}，期望 ${EXPECT_ACCOUNT}（.env.local 里钉的）"
fi
# 桶名跟 infra/deploy.local.sh 里建栈时的参数同一套命名规律，从账号+区域拼出来。
BUCKET="${SITE_BUCKET:-analytics-agent-site-${ACCT}-${REGION}}"
say "账号 $ACCT · 桶 $BUCKET · 区域 $REGION"

# ---- 快照 ----
if [[ $FRESH -eq 1 ]]; then
  if [[ $DRY -eq 1 ]]; then
    say "（--dry-run）正式部署会先重新生成 catalog.json"
  else
    say "重新生成 catalog.json（查 Glue，约 1 分钟）"
    PY=./backend/.venv/bin/python
    [[ -x "$PY" ]] || PY=python3
    "$PY" scripts/deploy/build_catalog_json.py || die "快照生成失败，未部署"
  fi
fi

[[ -f web/catalog.json ]] || die "缺 web/catalog.json，先跑 build_catalog_json.py"
SRC=$(python3 -c 'import json;print(json.load(open("web/catalog.json")).get("source"))')
TBL=$(python3 -c 'import json;print(json.load(open("web/catalog.json"))["totals"]["tables"])')
GEN=$(python3 -c 'import json;d=json.load(open("web/catalog.json"));print(d.get("generated_at_utc") or d.get("generated_at"))')
[[ "$SRC" == "glue" ]] || die "catalog.json 来源是 $SRC 不是 glue，拒绝部署（会把降级元数据固化上线）"
python3 scripts/deploy/check_catalog_freshness.py \
  || die "catalog.json 与 post-data-fix baseline 不一致，拒绝部署"
say "快照 $TBL 张表 · 生成于 $GEN"

# ---- 分发 ID ----
DIST=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
  --output text 2>&1) || die "读 $STACK 输出失败：$DIST"
DOMAIN=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue" \
  --output text 2>/dev/null)
[[ -n "$DIST" && "$DIST" != "None" ]] || die "拿不到 DistributionId"
say "分发 $DIST · https://$DOMAIN"

if [[ $DRY -eq 1 ]]; then
  echo
  echo "（--dry-run，以下不会执行）"
  echo "  aws s3 cp web/index.html   s3://$BUCKET/index.html   （no-store, text/html）"
  echo "  aws s3 cp web/catalog.json s3://$BUCKET/catalog.json （no-store, application/json）"
  echo "  aws cloudfront create-invalidation --distribution-id $DIST --paths /index.html /catalog.json /"
  exit 0
fi

# ---- 上传 ----
# no-store：这两个文件必须每次拿最新的。CloudFront 默认行为用的是
# Managed-CachingOptimized，不显式声明的话浏览器/边缘都会长期缓存旧版，
# 于是"发了但看不到变化"——这轮踩的正是这个类别的坑（虽然那次是压根没发）。
say "上传"
aws s3 cp web/index.html "s3://$BUCKET/index.html" --region "$REGION" \
  --content-type "text/html; charset=utf-8" --cache-control "no-store" \
  || die "index.html 上传失败"
aws s3 cp web/catalog.json "s3://$BUCKET/catalog.json" --region "$REGION" \
  --content-type "application/json; charset=utf-8" --cache-control "no-store" \
  || die "catalog.json 上传失败"

# ---- 失效 ----
say "CloudFront 失效"
INV=$(aws cloudfront create-invalidation --distribution-id "$DIST" \
  --paths /index.html /catalog.json / --query "Invalidation.Id" --output text) \
  || die "创建失效失败"
echo "  ${INV}（等生效…）"
aws cloudfront wait invalidation-completed --distribution-id "$DIST" --id "$INV" \
  2>/dev/null || echo "  （wait 超时，失效仍在后台进行）"

# ---- 部署后实测 ----
# 不只看命令退出码：真去边缘上取一次，确认拿到的是新版本。
say "线上验证"
FAIL=0
ck() { # ck <描述> <url> <期望正则>
  local d="$1" u="$2" want="$3" body
  body=$(curl -fsSL --max-time 20 "$u" 2>&1)
  if [[ $? -eq 0 ]] && grep -qE "$want" <<<"$body"; then
    printf '  \033[32mPASS\033[0m  %s\n' "$d"
  else
    printf '  \033[31mFAIL\033[0m  %s  (期望匹配 /%s/)\n' "$d" "$want"
    FAIL=$((FAIL+1))
  fi
}
BASE="https://$DOMAIN"
ck "catalog.json 可取且是 glue 来源" "$BASE/catalog.json" '"source": *"glue"'
ck "catalog.json 表数为 $TBL"        "$BASE/catalog.json" "\"tables\": *$TBL"
ck "index.html 是新版（含 catalog.json 兜底逻辑）" "$BASE/" "catalog\.json"
ck "index.html 含快照标注 i18n 键"   "$BASE/" "lbl_snapshot"

echo
if [[ $FAIL -gt 0 ]]; then
  die "$FAIL 项线上验证失败"
fi
cat <<EOF
  部署完成 ✅  $BASE

  浏览器里应能看到（登录后欢迎页「背后的数据」那行）：
    app_analytics · $TBL 张表 · 约 7992 万行 · Redshift Serverless
    · 元数据来自 Glue Data Catalog · 快照时间 $GEN

  若仍是旧内容：强刷（Cmd+Shift+R）；本地 Service Worker/浏览器缓存不受
  CloudFront 失效影响。
EOF
