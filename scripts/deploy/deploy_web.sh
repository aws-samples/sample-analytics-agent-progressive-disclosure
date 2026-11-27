#!/usr/bin/env bash
# 把前端推到线上：S3 静态桶 + CloudFront 失效 + 部署后实测。
#
# **绝不用 `s3 sync --delete`，也绝不上传 config.example.js。** 桶里的 config.js 是
# 线上真实配置(Cognito 池/客户端 ID)；把示例值 cp 上去或 --delete 掉，登录立刻挂。
#
# config.js 不是"手工在桶里维护、脚本绕开不动"的文件了 —— 那个做法有个静默失效：
# 池换了(比如 foundation 栈重建)而桶里那份还是旧 ID，页面能打开、登录框也在，
# 只有点了登录才报 `ResourceNotFoundException`，而部署脚本全绿。现在改成
# **每次部署都从 foundation 栈的输出现算一份再传**：栈是 ID 的唯一事实来源，
# 读不到就 die，不会传一份猜的上去。
#   ⚠️ 生成物落在 `$TMPDIR`，**故意不落 `web/config.js`**：那个路径一存在，
#      本地 `backend/run.sh` 就会把线上配置喂给本地页面(index.html 第 11-14 行：
#      本地靠"这份文件不存在 → server.py 回空脚本 → 回退 /api/config"来分流)，
#      于是本地页面开始拿线上 Cognito 池登录、askUrl 短路成 /ask。顺带也不存在
#      "账号相关 ID 被误提交进仓库"的可能。
#
# vendor/(echarts + cognito SDK + 字体，约 1.1MB)只在桶里没有时传一次：它们不随
# 代码变，而且 index.html 是懒加载 cognito SDK 的 —— 漏传的表现是页面正常显示、
# 点登录才 404，属于"部署脚本全绿但登录不了"那一类。
#
#   bash scripts/deploy/deploy_web.sh              # 用现有 web/catalog.json
#   bash scripts/deploy/deploy_web.sh --fresh      # 先重新查 Glue 生成快照再发
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
FOUNDATION="${FOUNDATION_STACK:-analytics-agent-foundation}"

FRESH=0; DRY=0
for a in "$@"; do
  case "$a" in
    --fresh) FRESH=1 ;;
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
# ---- foundation 栈的输出：桶名 + Cognito ----
# 桶名不再在这里靠命名规律拼。以前是 `analytics-agent-site-${ACCT}-${REGION}`，
# 那个"运行时现算账号"只避免了把账号写进仓库文件，桶**本身**的名字里还是那串数字；
# 现在 infra/foundation.yaml 建的是 `analytics-agent-site`(短名，桶名全球唯一，
# 撞了就给栈传 SiteBucketName)。名字既然可变，就只能问栈要，不能猜。
FOUT=$(aws cloudformation describe-stacks --stack-name "$FOUNDATION" --region "$REGION" \
  --query "Stacks[0].Outputs" --output json 2>&1) \
  || die "读 $FOUNDATION 输出失败（栈没建？先 aws cloudformation deploy infra/foundation.yaml）：$FOUT"
fout() { python3 -c '
import json,sys
for o in json.load(sys.stdin):
    if o["OutputKey"]==sys.argv[1]: print(o["OutputValue"]); break
' "$1" <<<"$FOUT"; }

BUCKET="${SITE_BUCKET:-$(fout SiteBucket)}"
POOL_ID="${COGNITO_USER_POOL_ID:-$(fout UserPoolId)}"
CLIENT_ID="${COGNITO_CLIENT_ID:-$(fout UserPoolClientId)}"
for v in BUCKET POOL_ID CLIENT_ID; do
  [[ -n "${!v}" ]] || die "$FOUNDATION 输出里取不到 $v（栈是旧版模板？）"
done
say "账号 $ACCT · 桶 $BUCKET · 区域 $REGION"
say "Cognito $POOL_ID · 客户端 $CLIENT_ID"

# ---- 快照 ----
if [[ $FRESH -eq 1 ]]; then
  say "重新生成 catalog.json（查 Glue，约 1 分钟）"
  PY=./backend/.venv/bin/python
  [[ -x "$PY" ]] || PY=python3
  "$PY" scripts/deploy/build_catalog_json.py || die "快照生成失败，未部署"
fi

[[ -f web/catalog.json ]] || die "缺 web/catalog.json，先跑 build_catalog_json.py"
SRC=$(python3 -c 'import json;print(json.load(open("web/catalog.json")).get("source"))')
TBL=$(python3 -c 'import json;print(json.load(open("web/catalog.json"))["totals"]["tables"])')
# 行数从快照里现取，不写死。以前这里的成功提示钉着「约 7992 万行」，那是 v1 造数规模的
# 遗留；湖仓版重灌之后是十几万行的量级，而提示照样打印，读起来像部署成功了个别的项目。
ROWS=$(python3 -c 'import json;print(json.load(open("web/catalog.json"))["totals"]["rows"])')
UNC=$(python3 -c 'import json;d=json.load(open("web/catalog.json"))["totals"];print(len(d.get("rows_uncounted_governed") or []))')
GEN=$(python3 -c 'import json;d=json.load(open("web/catalog.json"));print(d.get("generated_at_utc") or d.get("generated_at"))')
[[ "$SRC" == "glue" ]] || die "catalog.json 来源是 $SRC 不是 glue，拒绝部署（会把降级元数据固化上线）"
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

# ---- 生成 config.js（临时文件，不落仓库，理由见文件头）----
CFG="${TMPDIR:-/tmp}/analytics-agent-config.$$.js"
trap 'rm -f "$CFG"' EXIT
cat >"$CFG" <<EOF
// 由 scripts/deploy/deploy_web.sh 在部署期生成，勿手改桶里这份：下次部署会按
// $FOUNDATION 栈的输出重写。Cognito 池/客户端是公开值（浏览器登录必需）。
window.APP_CONFIG={
  authEnabled:true,
  region:"$REGION",
  userPoolId:"$POOL_ID",
  clientId:"$CLIENT_ID",
  // 同域相对路径：让 boot() 短路进实时模式（后端常驻，不做 2.5s 网络探针）。
  askUrl:"/ask"
};
EOF

# vendor/ 只在桶里没有时传。探针挑 cognito SDK 而不是 echarts：它是懒加载的，
# 缺了不影响首屏，只在点登录时 404 —— 最容易漏、最难发现的那一个。
# 用 head-object 而不是 `s3 ls`：后者是**前缀**匹配，判断单个键存不存在不该用它。
VENDOR_NEEDED=0
aws s3api head-object --bucket "$BUCKET" --region "$REGION" \
  --key "vendor/amazon-cognito-identity.min.js" >/dev/null 2>&1 || VENDOR_NEEDED=1

if [[ $DRY -eq 1 ]]; then
  echo
  echo "（--dry-run，以下不会执行）"
  echo "  aws s3 cp web/index.html   s3://$BUCKET/index.html   （no-store, text/html）"
  echo "  aws s3 cp web/catalog.json s3://$BUCKET/catalog.json （no-store, application/json）"
  echo "  aws s3 cp $CFG             s3://$BUCKET/config.js    （no-store, text/javascript）"
  if [[ $VENDOR_NEEDED -eq 1 ]]; then
    echo "  aws s3 sync web/vendor/    s3://$BUCKET/vendor/      （桶里还没有，首次上传；immutable）"
  else
    echo "  （vendor/ 桶里已有，跳过）"
  fi
  echo "  aws cloudfront create-invalidation --distribution-id $DIST --paths /index.html /catalog.json /config.js /"
  echo
  echo "  要传的 config.js 内容："
  sed 's/^/    /' "$CFG"
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
aws s3 cp "$CFG" "s3://$BUCKET/config.js" --region "$REGION" \
  --content-type "text/javascript; charset=utf-8" --cache-control "no-store" \
  || die "config.js 上传失败"
if [[ $VENDOR_NEEDED -eq 1 ]]; then
  say "首次上传 vendor/（约 1.1MB）"
  # immutable 长缓存：这些文件按内容固定，换版本要连文件名一起换。
  # 不带 --delete —— 这里只补，永不删。
  aws s3 sync web/vendor/ "s3://$BUCKET/vendor/" --region "$REGION" \
    --cache-control "public,max-age=31536000,immutable" \
    || die "vendor/ 上传失败"
fi

# ---- 失效 ----
say "CloudFront 失效"
INV=$(aws cloudfront create-invalidation --distribution-id "$DIST" \
  --paths /index.html /catalog.json /config.js / --query "Invalidation.Id" --output text) \
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
# config.js 只查"能取到"是不够的：桶里留着一份旧池 ID 也照样 200。比的是具体 ID。
ck "config.js 里的池 ID 是 $POOL_ID" "$BASE/config.js" "userPoolId:\"$POOL_ID\""
ck "config.js 里的客户端 ID 是 $CLIENT_ID" "$BASE/config.js" "clientId:\"$CLIENT_ID\""
# 登录用的 SDK 是懒加载的，缺了首屏完全正常，只有点登录才 404。
ck "vendor/amazon-cognito-identity.min.js 可取" \
   "$BASE/vendor/amazon-cognito-identity.min.js" "AmazonCognitoIdentity"

echo
if [[ $FAIL -gt 0 ]]; then
  die "$FAIL 项线上验证失败"
fi
cat <<EOF
  部署完成 ✅  $BASE

  浏览器里应能看到（登录后欢迎页「背后的数据」那行）：
    app_analytics · $TBL 张表 · $ROWS 行（另有 $UNC 张表不授权、未计入）
    · Athena + S3 Tables (Iceberg) · 元数据来自 Glue Data Catalog · 快照时间 $GEN

  行数那句由 index.html 的 rowsPhrase() 渲染成「约 N 万行」并补上未计入的说明，
  所以页面上的数字会比上面这个精确值圆一些——不是对不上。

  若仍是旧内容：强刷（Cmd+Shift+R）；本地 Service Worker/浏览器缓存不受
  CloudFront 失效影响。
EOF
