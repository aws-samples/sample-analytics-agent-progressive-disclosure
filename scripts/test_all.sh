#!/usr/bin/env bash
# 湖仓架构验收：跑完 L0–L6，全部记账后一次性报结果。
#
# 这个脚本验的是**湖仓那条 arm**：S3 Tables（Iceberg）+ Glue Data Catalog + Athena。
# L1/L2/L4/L5/L6 里原来的 Redshift 专属检查都换成了对应的湖仓检查；治理层（L4）换的不是
# 同一个原语：Redshift 的动态脱敏在 LF 里没有等价物，落成了列级排除 —— 见那一节。
#
# 但**别读成「Redshift 退役了」**（这行注释一度就是那么写的）。项目现在要的是三种架构的
# 横向对比，Redshift Serverless 和 DuckDB 都是在跑的 arm，不是历史包袱：
#
#   · 跨 arm 的取值对账     python3 eval/compare_goldens.py
#   · 某条 arm 的金标可跑性  DB_BACKEND=redshift python3 eval/run_eval.py --dry-run
#
# 这个脚本只锚 athena（L5 那条金标门就写死了 DB_BACKEND=athena），另外两条 arm 的验收
# 走上面两条命令，别指望在这里看到它们的红绿。
#
#   bash scripts/test_all.sh           # L3 只抽查 3 张表（35 张要 ~1 分钟）
#   bash scripts/test_all.sh --l0      # 只跑 L0（不连云、不要凭证；CI 用这一档）
#   bash scripts/test_all.sh --full    # L3 全量 35 张表
#   bash scripts/test_all.sh --l8      # 追加 L8 负测（会临时改文件再还原，见下）
#   bash scripts/test_all.sh --ask     # 追加 L6 的 /ask 流式契约（**烧一次 Opus**，约 1 分钟）
#
# --ask 默认关，因为它要花钱。但不跑的代价要知道：L6 其余几条只覆盖**页面加载**那一段，
# 用户真正在用的 `POST /ask` 那条流（SSE 分帧、事件键名、过期会话降级）就没人盯了。
#
# 用 bash 不用 sh：需要 pipefail。也别改成 zsh——`${PIPESTATUS[0]}` 在 zsh 里是
# `${pipestatus[1]}`，脚本在两个 shell 间不可移植。
set -uo pipefail

cd "$(dirname "$0")/.."

FULL=0
L8=0
ASK=0
L0ONLY=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --l8)   L8=1 ;;
    --ask)  ASK=1 ;;
    # 只跑 L0 并**以 0 退出**。没有这一档时"只想跑离线那几十条"只有一个办法：
    # 整套跑下去、在 AWS 身份那道闸上吃一个 exit 1 —— 结果是绿的 L0 被包在一个
    # 非零退出码里，任何按退出码判成败的东西（CI、pre-commit）都读成失败。
    --l0)   L0ONLY=1 ;;
    # `${arg}` 的花括号是必需的：紧跟其后的全角「（」在 bash 眼里算标识符字符，
    # 写成 `$arg（…` 会被解析成变量名 `arg（`，配上 `set -u` 就是 unbound variable ——
    # 于是"参数打错了"这件事的提示变成了一句看不懂的 shell 内部报错。
    *) echo "未知参数 ${arg}（支持 --l0 / --full / --l8 / --ask）" >&2; exit 2 ;;
  esac
done

# --l0 会在 L1 之前收工，所以它跟那三个"往后加东西"的开关放一起是自相矛盾的：
# 静默忽略的话，`--l0 --l8` 会打印一份 L0 全绿、退出 0、而**负测一条没跑**的报告。
if [[ $L0ONLY -eq 1 && ( $L8 -eq 1 || $ASK -eq 1 || $FULL -eq 1 ) ]]; then
  echo "--l0 只跑 L0，与 --full / --l8 / --ask 冲突（那三个都在 L1 之后，要凭证）。" >&2
  echo "只想跑离线负测就直接：\$PY scripts/negative_tests.py --offline（27 个用例，不连云）" >&2
  exit 2
fi

PY=./backend/.venv/bin/python
[[ -x "$PY" ]] || PY=python3

# 本地覆盖（gitignored，见 .env.local.example）。账号相关值不硬编码：
# 账号在下面「前置」步骤从 sts 现算，Glue catalog ID 用它拼。
#
# **命令行传的环境变量优先于 .env.local。** 原来这里是 `set -a; source`，而 source
# 是无条件赋值：`AWS_REGION=us-east-1 bash scripts/test_all.sh` 会被文件里的值悄悄
# 盖掉，然后测的是另一个区域，而输出里看不出你的参数被无视了。
# 优先级与 backend/run.sh 一致（那边有同一段循环）。
if [[ -f .env.local ]]; then
  while IFS= read -r _line || [[ -n "$_line" ]]; do
    _line="${_line%$'\r'}"
    [[ "$_line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$_line" == *=* ]] || continue
    _k="${_line%%=*}"; _v="${_line#*=}"
    _k="${_k//[[:space:]]/}"
    [[ "$_k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!_k+x}" ]] && continue                 # 环境里已有 → 调用方说了算
    export "$_k=$_v"
  done < .env.local
  unset _line _k _v
fi
export AWS_REGION="${AWS_REGION:-us-west-2}"

PASS=0
FAIL=0

hdr() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
note() { printf '  \033[90m·\033[0m     %s\n' "$1"; }

# 在中途停下时也要把已经跑过的记账报出来，并且**说清停在哪一层**。
# 光 `exit 1` 的话，L0 那几十条的结果就白跑了，读日志的人也分不清
# 「离线断言红了」和「只是没登录」——这两件事的下一步动作完全不同。
summary_and_exit() {
  hdr "结果（提前停止）"
  printf '  通过 %d · 失败 %d\n' "$PASS" "$FAIL"
  echo "  L0 离线自测已跑完；L1 起需要 AWS 凭证，未继续。"
  exit 1
}

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
  local out rc
  out=$("$@" 2>&1); rc=$?
  if [[ $rc -eq 0 ]] && grep -qE "$want" <<<"$out"; then
    printf '  \033[32mPASS\033[0m  %s\n' "$desc"
    PASS=$((PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m  %s  (期望匹配 /%s/)\n' "$desc" "$want"
    # 只印尾 20 行，但**必须说清截掉了多少**。2026-09-17 实测的代价：verify_constants
    # 那条的失败清单是 38 行，尾 20 行只露出后 18 条，于是读日志的人（我）按 18 条
    # 估了问题规模，把 13 条 RELOAD_PENDING + 1 条 DIMS_PASSTHROUGH 过期整个漏掉了。
    # 「被截断」和「就这么多」在报告上长得一样 —— 跟这套测试反复点名的那类缺口同形。
    local total
    total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
    printf '%s\n' "$out" | tail -20 | sed 's/^/        /'
    if (( total > 20 )); then
      printf '        \033[90m…… 上面还截掉了 %d 行（失败清单可能被截了头）。看全：%s\033[0m\n' \
        "$((total - 20))" "$*"
    fi
    FAIL=$((FAIL + 1))
  fi
}

hdr "L0 静态自测（无云依赖）"
# 生成器（scripts/gen/main.py）依赖 numpy，而**本机两个解释器都没装**。
# 它不在被验收的链路上：这套架构验的是 data/csv/ → S3 Tables → Athena，
# CSV 已经在仓库里，生成器只在"要造一批新数据"时才用。
#
# 所以缺 numpy 记成 note 而不是 FAIL —— 跟下面缺 node 的处理一致。红一条与迁移
# 无关的灯，代价不是"少测一项"，是**下次没人认真看这份报告**。反过来也不能
# 直接删掉这两行：那样"L0 全绿"会被读成"生成器也验过了"。
if $PY -c "import numpy" >/dev/null 2>&1; then
  # genlib 是生成器的底座，其中 pgcsv 那段盯的是**装载能不能成**：Athena 的
  # `CAST(x AS timestamp(6))` 只认空格分隔，pgcsv 曾写出 ISO 的 `T` 分隔，
  # 结果 8000 万行那条生成路径整个装不进湖，而 CSV 自己看起来完全正常。
  # 这类"格式对不上"的缺陷只有在写出的那一层才拦得住，装载时报错指不到成因。
  # 跑 8 秒（pgcsv 那段写 100 万行真文件），无云依赖。
  # 判据写全 ", 0 failed ==="：只写 "0 failed" 的话 "10 failed" 也能匹配上。
  grep_run "genlib 底座自测（含 CSV 时间戳形式）" ", 0 failed ===" \
    $PY scripts/genlib/selftest.py
  grep_run "生成器 fillers 自测"          "全部通过" $PY scripts/gen/selftest_fillers.py
  grep_run "生成器跨表闭环自测"           "全部通过" $PY scripts/gen/selftest_closures.py
  # 反例一侧：上面那 106 条断言（跨表闭环自测自己报的数）全是正向的，判据写歪了它照样绿，而它绿在最前面，
  # 后面每层的"通过"都会被读成"数据是对的"。这一条往深拷贝里注入 42 个缺陷，要求整套
  # 自测变红**且红在指定断言上**。跑 7 秒，留在默认档而不是 --l8：它不改任何仓库文件、
  # 不碰云，代价和 fillers 自测同级。
  grep_run "生成器跨表闭环自测的反例一侧"  "反例全部按预期变红" \
    $PY scripts/gen/selftest_closures.py --negative
  # 三种架构对比测试的前置判据（Redshift / Athena+S3Tables / DuckDB+S3Tables 要吃同一份
  # 数据）。这里跑的是它的红绿自测 + 两条不读数据的静态判据：Parquet 物理类型 ⟷ Redshift
  # 列类型、Iceberg 类型 ⟷ DuckDB reader 支持集合。读数据的判据 3–8 不在这里——那要一份
  # 全量产出目录（判据 8 还要两份），跑法写在 verify_portability.py 的 docstring 里。
  grep_run "三架构可移植性判据自测（红绿两侧 · 无云依赖）" "全部通过" \
    $PY scripts/gen/verify_portability.py --selftest
  grep_run "Parquet ⟷ Redshift 类型兼容 + Iceberg ⟷ DuckDB 类型（静态，35 表）" "全部通过" \
    $PY scripts/gen/verify_portability.py --static
else
  note "跳过生成器自测（fillers / 跨表闭环）：未装 numpy，装了自动跑。"
  note "  pip install numpy —— 只在要重新造数据时才需要，湖仓链路不依赖它。"
fi
# 这一条**不放在 numpy 分支里**：semantics.yaml 只要 pyyaml 就能校验，而它是 D-03 三类
# 判据（品牌→类目白名单 / 价格带 / 规格词池）的唯一真源——生成器和 verify_semantics.py
# 都从它读。配置自相矛盾时（价格带上下界写反、白名单指向不存在的叶子、标签名跨池重名），
# 生成侧和检查侧会**一起**用错的那份，于是两边一致、全绿、而数据是错的。
# 它还查一件小样本查不到的事：不重名商品名的上界 ≥ 全量目标 SKU 数。scale=1 只造 200 行
# 永远够用，缩了品牌白名单要到灌全量那一刻才炸。
grep_run "商品语义配置自测（semantics.yaml 真源）" "全部通过" $PY scripts/gen/semantics.py --selftest
# 同上放在 numpy 分支之外：profiles.py 只依赖 pyyaml。
grep_run "画像相关性配置自测（profiles.yaml 真源）" "全部通过" $PY scripts/gen/profiles.py --selftest
# 下面五条是 L2+ 那五个「数据真实性报告器」的判据自测。**只挂 --selftest，不挂正向那一支**：
# 正向对现行库必然是红的（云上是 v1 那批数据，不重灌的决定已定），挂上去就是一盏永远
# 红的灯，会顺带废掉旁边那些真闸门。自测不连云、不读库、秒级，验的是**判据有没有区分力**
# ——构造夹具喂给纯函数判据，红的那一侧必须红、绿的那一侧必须绿。它们是这批判据的唯一闸门。
#
# 前三条比后两条晚挂上，**这不是设计取舍**：写前两批时还没把「自测就是唯一闸门」定成模式，
# 于是 48 项断言（23 + 19 + 6）落在测试之外、没有任何东西在跑。补齐是为了让五个报告器
# 受同一道闸——判据的区分力退化时，退化的那一批不该因为写得早就免检。
grep_run "字面值判据自测（L9 · 无云依赖）" "全部通过" \
    $PY scripts/lakehouse/verify_literals.py --selftest
grep_run "商品语义配对判据自测（L7 · 无云依赖）" "全部通过" \
    $PY scripts/lakehouse/verify_semantics.py --selftest
grep_run "维度分辨率判据自测（L6 · 无云依赖）" "全部通过" \
    $PY scripts/lakehouse/verify_resolution.py --selftest
grep_run "画像相关性判据自测（L8 · 无云依赖）" "全部通过" \
    $PY scripts/lakehouse/verify_correlation.py --selftest
# 四张行为大表（post_likes / page_views / user_follows / push_notifications）的分布真实性
# 判据，外加 events 的一条窗口判据（28 条）。跟上面那条一样只挂 --selftest：正向那一支对
# 现行库必然一片红，挂上去就是一盏永远红的灯——2026-08-31 重灌换掉了分布形状那一批红，
# 但 ev_in_session 的成因在生成器，只有下一次重新生成才可能变绿。
# 自测本身不连云、不读库，验的是判据有没有区分力：每条判据 × 均匀夹具必须红 / 重尾夹具
# 必须绿，外加集中度统计量与朴素实现逐位对齐、随机基线公式与四个实测点吻合、
# WEAK/NOINPUT 不许当通过、通过线是闭的。它自己就是这批判据的唯一闸门。
grep_run "行为大表分布判据自测（28 条 × 红绿两侧 · 无云依赖）" "全部通过" \
    $PY scripts/lakehouse/verify_behavior.py --selftest
grep_run "pg→trino 方言改写自测"          "全部通过" $PY scripts/gen/pg_to_trino.py --selftest
# 三种架构横向对比那一套（scripts/bench/）的判据自测，六条都不连云、不花钱。
# 挂上来的直接原因：在此之前**整套测试里没有一条断言碰过它们**，于是那些判据坏掉
# 也不会让任何东西变红——2026-09-07 一次性查出三处，全是靠人读代码才发现的：
# 整批摊薄那一栏拿整批墙钟给每条 arm 记账（一条 arm 为另两条付钱、同一段时间收两遍）、
# Athena 每条查询的 10MB 起步价被合并成一次（8 条里漏收 7 次）、
# 两个正确性闸门在受治理的三列上**没有缺陷也是红的**（红得没道理和绿灯掩盖缺陷是同一枚
# 硬币，那次真的跨 arm 不一致形态一模一样）。现在那些性质都写成断言了，这几行是它们的闸门。
grep_run "arm 探针标签逐位相同（三种方言不串味 · 无云依赖）" "✅" \
    $PY scripts/bench/arms.py --selftest
grep_run "表级三方对账判据自测（含治理豁免 11 条 · 无云依赖）" "自测通过 ✅" \
    $PY scripts/bench/correctness.py --selftest
grep_run "取值三方对账判据自测（含治理豁免 6 条 · 无云依赖）" "自测通过 ✅" \
    $PY scripts/bench/query_correctness.py --selftest
# 这条钉的是「起步价的单位」：Athena 按查询、Redshift 按活动分钟、Fargate 按任务。
# 三个单位混用正是上面那两处成本错的根源。
grep_run "单价与计费规则换算自测（无云依赖）" "换算逻辑自测通过 ✅" \
    $PY scripts/bench/prices.py --selftest
# 核心断言：**任何一条 arm 的摊薄金额都不许随另一条 arm 的耗时变化**（双向各查一次）。
grep_run "耗时与成本口径自测（摊薄不许串台 · 无云依赖）" "耗时与成本逻辑自测通过 ✅" \
    $PY scripts/bench/timing.py --selftest
grep_run "冷启动结算自测（账单侧口径 · 无云依赖）" "自测通过 ✅" \
    $PY scripts/bench/cost_cold.py --selftest
# 这道闸管的是「不许写」，管不了「不许看」——后者是 L4 的事，两条别互相当替补。
# 在这条自测之前整套测试里没有一条断言碰过它：把 _FORBIDDEN 改松不会让任何东西变红。
grep_run "只读 SQL 边界自测（管「不许写」；「不许看」在 L4）" "全部通过" $PY backend/db.py
# 第三道边界：**agent 只能调那 5 个分析工具**。上面那道只管 run_sql 里的 SQL 写不写，
# 管不了「模型能不能干脆绕过 run_sql」——走 Bash / Read 就能读 .env.local 和本机 AWS
# 凭证，走 Write / Edit 就能改仓库文件，而 run_sql 那道闸压根不在这条路上。
# 这里曾经零覆盖，代价是闸门**死了一整段时间没人知道**：`can_use_tool` 被
# `permission_mode="bypassPermissions"` 架空、一次都没触发过（SDK 自己在打
# CanUseToolShadowedWarning），实测可达工具 25 个，让它 echo 一句 Bash 真的执行了。
# 把闸门整个删掉当时也不会让任何东西变红。现在改成 PreToolUse hook，这条盯着它。
grep_run "工具白名单边界自测（管「不许调用别的工具」；两侧选项都查）" \
  "全部通过" $PY backend/agent.py --selftest
# 文档 SQL 里的**表名**存在性。这一层和 L2 那条 EXPLAIN 不是重复：EXPLAIN 覆盖面更全
# （列名、类型、方言），但它连云、要钱，而且**跳过含参数占位符的语句**——
# `relationships.md` 那两个可复制 JOIN 示例都以 `WHERE u.user_id = ?` 结尾，于是
# EXPLAIN 从没跑过它们，而它们 JOIN 的 `user_levels` / `product_skus` 在本库不存在；
# 同一文件的外键表里一共列了 10 张幻表。跳过占位符语句是对的，但当时「跳过」等于
# 「没有任何检查」。这条不连云、查全集（含被跳过的），所以能站在 L0。
# 两道各有盲区：这条只管表名，列名和方言仍然只有 EXPLAIN 能管，别用一道替另一道。
grep_run "文档 SQL 表名提取器自测"         "全部通过" \
    $PY scripts/lakehouse/verify_doc_sql.py --selftest
grep_run "文档 SQL 只引用真实存在的表（离线 · 含被 EXPLAIN 跳过的语句）" "表名全部存在 ✅" \
    $PY scripts/lakehouse/verify_doc_sql.py --offline
grep_run "Iceberg DDL 生成器自测"         "全部通过" $PY scripts/lakehouse/gen_ddl.py --selftest
grep_run "Iceberg DDL 未被手改（--check）" "一致 ✅"  $PY scripts/lakehouse/gen_ddl.py --check
# knowledge/README.md 写着「表结构以 database/*.sql 为准」，而这两行挂上之前，那个
# 「为准」的源里可比对的 34 行枚举注释有 23 行是错的（products.status 四个值全错，
# 照它写 WHERE status='active' 得到 0 行、不报错、结论是「一件在售商品都没有」）。
# 而这批注释正是 scripts/gen/tables.py 当年抄枚举池的地方——13 个生成器缺陷的成因。
# 注释还会被 gen_ddl.py 搬进 01_tables.sql 的 COMMENT，也就是进 Glue 列元数据。
grep_run "DDL 枚举注释自测"                "全部通过" $PY scripts/lakehouse/verify_ddl_comments.py --selftest
grep_run "DDL 枚举注释 ⟷ 知识卡片对账"      "全部一致 ✅" $PY scripts/lakehouse/verify_ddl_comments.py
# 这一行原来标的是「派生层知识卡片是最新渲染」，而 --check 当时**只校验 manifest**、
# 从不比对生成物：手改一张卡片它照样 exit 0。名字比覆盖面大的检查器比没有更坏。
# 现在 --check 逐字比对 15 个产物，L8 的 manifest-artifact-handedit 盯着这条不再退化。
grep_run "派生层生成物未被手改（manifest ⟷ 卡片/DDL 逐字）" \
  "生成物一致 ✅" $PY scripts/manifest/render.py --check
# analyticsagent/ 曾经是 backend/ 的**手工副本**，分叉到 db.py 518⟷165 行、
# SYSTEM 提示词 8696⟷6203 字符：云上那份还在教「拿该表自己的 max(dt) 当今天」，
# 同一个「近 30 天各渠道 GMV」本地答 100.8 万、云上答 0。整套测试当时一条都没碰过它。
# 现在共享代码是生成物，这两行是它的闸：--selftest 验检查器本身认得出漂移，
# --check 验云上副本确实是从 backend/ 拷来的那份。L8 的 cloud-copy-drift 盯着前者不退化。
grep_run "云上副本同步器自测"               "全部通过" $PY scripts/deploy/sync_agent_code.py --selftest
grep_run "云上副本未与 backend/ 漂移（--check）" \
  "同步面一致 ✅" $PY scripts/deploy/sync_agent_code.py --check
# L7 的判分器此前零覆盖，代价是一条**判错方向**的金标：`L4-funnel` 的口径写成
# 每步各 count(DISTINCT user_id) 一遍，value_hint 就是非单调的 394/385/396/373，
# agent 照这个口径答「漏斗几乎不衰减，是随机种子数据的问题」判 PASS，归档基线 26/26。
# 正确口径（每步约束成上一步子集）是 394/314/258/199，逐层流失 20%/18%/23%。
# 这条盯三样：judge_funnel 的形态闸、stats.funnel 的实时闸、金标 SQL 里逐步的
# 子集约束。**不连库不调模型**，秒级。L8 的 funnel-* 两个用例盯它不退化。
grep_run "L7 判分器 + 漏斗/留存口径自测（形态闸 · 金标逐步约束 · 卡片口径与路由）" \
  "全部通过" $PY eval/run_eval.py --selftest
grep_run "集市层谓词/列数对账自测"         "全部通过" $PY scripts/lakehouse/verify_mart_parity.py --selftest
grep_run "集市层谓词未在手工搬运中丢失"    "全部对齐 ✅" $PY scripts/lakehouse/verify_mart_parity.py
grep_run "元数据对账器自测"               "全部通过" $PY scripts/lakehouse/reconcile.py --selftest
grep_run "装载对账器自测"                 "全部通过" $PY scripts/lakehouse/verify_load.py --selftest
# 规模声明自洽。整个对账层比表、比列、比枚举、比退化列、比装载忠实——**从不比规模**，
# 于是曾出现「五处文档写着 19 万行、湖里装着 8000 万行、L0–L6 全绿」：数量级差 427 倍
# 而没有一盏灯，因为没有任何一条断言的对象是「有多少行」。这条离线管两件：docs/scale.json
# 的 seed 批次逐表等于 data/csv 现数，以及那 6 条文档声明的原文还在、次数没变、数值对得上
# 它自称的批次。湖里到底装的是哪一批要连云，在 L3。
grep_run "规模声明自洽（docs/scale.json ⟷ data/csv · 文档声明原文）" \
  "规模声明自洽 ✅" $PY scripts/lakehouse/verify_scale.py --selftest
grep_run "枚举卡片解析器自测"             "全部通过" $PY scripts/lakehouse/verify_enums.py --selftest
grep_run "退化列分类器 + 登记清单自测"     "全部通过" $PY scripts/lakehouse/verify_constants.py --selftest
# 一致性快照的跨方言渲染。这条挂上来是因为它的失败方式是**静默漏列**：Trino 报
# `decimal(12,2)` / `timestamp(6)`，Postgres 那套类型名一个都对不上，`kind()` 返回
# None，全部金额列和时间列被当成 varchar 跳过——快照只剩 count，脚本 exit 0，
# 输出看起来完全正常。自测同时钉住 Postgres 那一支逐字节没变（两份基线按它产的）。
grep_run "一致性快照跨方言渲染自测"        "全部通过" $PY scripts/consistency/snapshot.py --selftest
grep_run "建站脚本自测"                   "全部通过" $PY scripts/lakehouse/setup.py --selftest
grep_run "CSV 装载前检（表头/列序/行尾/换行/数组）" "✅" $PY scripts/lakehouse/load.py --preflight
# boot() 决定整页走真实链路还是离线烘焙数据，而它坏掉时页面**照常给答案**（问退款答
# DAU 走势）。这条不连云、不起后端，用假 fetch 跑真 boot()：慢探针要能上线、后端说
# 「还在预热」不算「不在」、探不通才明确降级、一直预热不完也要有限放弃、探针期间提问
# 要等探针、**降级之后还要继续看后端**（先开页面再起后端要能自愈）、以及**后端已经
# 活了就不许再给烘焙答案**。曾经零覆盖，代价是一次确定性的静默降级 + 一次"重启也刷新了
# 还是一模一样"的误归因。
if command -v node >/dev/null 2>&1; then
  grep_run "boot() 行为契约（慢探针/预热中仍上线 · 探不通才降级 · 降级后自愈 · 提问等探针）" \
    "全部通过" node scripts/ui/boot_test.mjs
else
  note "跳过 boot() 行为契约：未装 node。"
fi
# 同一份 shell 要在两套挂载布局下跑：线上在站点根、本地被 FastAPI 挂在 /app 下。
# 本地资源写死绝对路径的话线上照旧对、**本地静默 404**：图表框空白 + 字体退回系统
# 默认，页面其余部分照常渲染，没有红字。三个 404 只出现在 uvicorn 日志里，本地图表
# 因此瘸过一段时间才被发现。L6 里还有一条真取一遍的 HTTP 版。
grep_run "shell 资源引用在两套挂载布局下都取得到（静态）" \
  "全部通过" $PY scripts/ui/asset_check.py

# --l0 在这里就收工：**这一档的退出码才是可判的**。下面每一层都要凭证，没凭证时
# summary_and_exit 一律 exit 1，所以"L0 全绿"和"没登录"在退出码上分不开。
# CI（.github/workflows/offline.yml）跑的就是这一档 + negative_tests.py --offline。
# 注意它报的是「L0 全绿」而不是「全绿」：这份报告里没有任何一条碰过云，
# 而 L1–L6 恰恰是最容易被当成已覆盖的部分。
if [[ $L0ONLY -eq 1 ]]; then
  hdr "结果（只跑 L0）"
  printf '  通过 %d · 失败 %d\n' "$PASS" "$FAIL"
  if [[ $FAIL -gt 0 ]]; then
    echo "  L0 未全绿，先修再往下跑。"
    exit 1
  fi
  echo "  L0 全绿 ✅（离线那一档）。L1–L6 需要 AWS 凭证，**本次一条都没跑**："
  echo "    云资源状态、元数据对账、装载完整性、治理层、查询路径、服务与前端契约"
  echo "    都不在这个退出码的保证范围里。要连云就去掉 --l0。"
  exit 0
fi

hdr "前置：AWS 身份"
# **这一段刻意排在 L0 之后**：L0 那几十条一条都不连云，而这个闸门是 `exit 1`。
# 它原来站在最前面，于是没登录的人（CI、刚 clone 的人、SSO 过期的人）一条离线自测
# 都跑不到就被弹出去——那批断言恰好是最该在改完代码后立刻跑的。挪到这里之后，
# 「没有 AWS 凭证」的结果从「整套测试不可用」变成「L0 全绿 + 从 L1 起停住」。
ACCT=$(aws sts get-caller-identity --query Account --output text 2>&1)
if [[ ! "$ACCT" =~ ^[0-9]{12}$ ]]; then
  printf '  \033[31mFAIL\033[0m  拿不到 AWS 身份：%s\n' "$ACCT"
  echo "        L0 已经跑完（见上），后面全是云调用，先登录（aws sso login / AWS_PROFILE）。"
  summary_and_exit
fi
# EXPECT_ACCOUNT 是可选守卫：设了（通常在 .env.local）才强制相等，防多账号环境串号。
if [[ -n "${EXPECT_ACCOUNT:-}" && "$ACCT" != "$EXPECT_ACCOUNT" ]]; then
  printf '  \033[31mFAIL\033[0m  account=%s，期望 %s（.env.local 里钉的）\n' "$ACCT" "$EXPECT_ACCOUNT"
  echo "        身份不对，后面全是云调用，先停下来。"
  summary_and_exit
fi
printf '  \033[32mPASS\033[0m  account=%s region=%s\n' "$ACCT" "$AWS_REGION"
PASS=$((PASS + 1))
# boto3 glue 的 CatalogId **必须带账号前缀**，且要指到叶子（表桶那一层）。
# 给 Athena 用的那个写法不带前缀 —— 两种不能混用，混了报 EntityNotFoundException，
# 而报错完全指不到成因。见 knowledge/connection.md。
TABLE_BUCKET="${TABLE_BUCKET:-analytics-agent-tables}"
CATALOG="${GLUE_CATALOG_ID:-$ACCT:s3tablescatalog/$TABLE_BUCKET}"
export GLUE_CATALOG_ID="$CATALOG"

hdr "L1 云资源状态"
grep_run "S3 Tables + Glue + Athena workgroup 就位（只读核查）" \
  "基建齐备 ✅" $PY scripts/lakehouse/setup.py --verify

hdr "L2 元数据对账（四条独立路径）"
# 路径一：结构。DDL 声明态 ⟷ Glue 实际态 ⟷ 知识库卡片，列名/类型/注释三方比。
grep_run "DDL ⟷ Glue ⟷ 知识库卡片（七类检查，含列注释）" \
  "对账通过 ✅" $PY scripts/lakehouse/reconcile.py --catalog "$CATALOG" --strict
# 路径二：可执行性。卡片里的 SQL 是 agent 照抄的对象，抄不动就是幻觉源头。
# EXPLAIN 做完整的语法 + 目录 + 列 + 类型解析，扫描 0 字节。
# 表名那一层已经在 L0 离线查过（含这里会跳过的占位符语句），这条管列名/类型/方言。
grep_run "知识库里每条 SQL 都能在 Athena 上 EXPLAIN 通过" \
  "全部通过 ✅" $PY scripts/lakehouse/verify_doc_sql.py
# 路径三：列里的值。上面两条都管不到枚举取值——EXPLAIN 不看 WHERE 里的字面量能不能命中，
# reconcile 只比表和列。卡片写 status='active' 而数据是 'on_sale' 时，SQL 语法正确、
# 目录解析通过、返回空集，结论直接反过来。这一条专门盯这类漂移。
grep_run "卡片枚举取值 ⟷ Athena 实际取值" \
  "枚举一致 ✅" $PY scripts/lakehouse/verify_enums.py
# 路径四：列的**基数**。上面三条全绿时，一列仍可能整个恒等于 0 或整列 NULL——
# 灌载忠实（verify_load 比的是 CSV ⟷ Athena，源头本来就是 0）、EXPLAIN 通过、
# 没声明枚举所以 verify_enums 不看它，而 core_metrics.md 的 like_rate 恒等于 0.00
# 且不报错。实测 468 个标量列里 31 个常量 + 46 个整列 NULL，逐列登记在那份清单里；
# 这条的价值全在「清单外新增一个」和「清单里某条已被修好」两种情况都会红。
# 8 个数组列 2026-08-31 起也在普查面里（此前一律跳过）：判 sum(cardinality) = 0，
# 即「整列空数组」——posts 那三列一度就是这个形态，而那一轮没有任何一层会红。
grep_run "线上库退化列（常量 / 整列 NULL / 整列空数组）全部登记在册" \
  "全部登记在册 ✅" $PY scripts/lakehouse/verify_constants.py

hdr "L3 装载完整性（CSV 真源 ⟷ Athena 现查）"
# 不用 scripts/consistency/snapshot.py 的基线 JSON：那两份基线描述的是 v2 那批数据
# （users 21 万行），跟当前部署（500 行）无关，比起来**稳定通过但什么也没验证**。
# 这里两边都现算，没有会过期的中间文件。理由详见 verify_load.py 的模块 docstring。
# 2026-09-01 起 CSV 侧的目录**不再是 data/csv**：云上是 7,994 万行的全量产出，
# 而 data/csv 是 v1 的 19 万行，比起来会得出一屏假差异（"你比错了东西"被印成
# "装载不完整"）。verify_load.py::_resolve_csv_dir 现在从 data/loaded_row_counts.json
# 读装载那次的产出目录并指过去；那个目录不在本机时判红并给出重造命令，不退回 data/csv。
# 显式 `CSV_DIR=` 仍然优先。
if [[ $FULL -eq 1 ]]; then
  echo "  （全量 35 张表，约 1 分钟…）"
  grep_run "35 张基表的行数/数值求和/时间边界/布尔计数全等" \
    "装载完整 ✅" $PY scripts/lakehouse/verify_load.py
else
  grep_run "抽查 3 张表（users/orders/order_items）行数与求和全等" \
    "装载完整 ✅" $PY scripts/lakehouse/verify_load.py -t users -t orders -t order_items
  note "只抽查了 3 张表，加 --full 跑全部 35 张。改过数据或重灌过表必须跑 --full。"
fi
# 上面那条比的是「湖里的行 ⟷ data/csv 的行」，它默认这两侧**本来就该相等**——而这个
# 账号的湖里装的是全量重灌那批（8000 万行），种子是 19 万行。所以 verify_load 的
# 对账对象是「装载有没有丢行」，答不了「湖里现在装的是哪一批」。这条答后者：现查 35 张表
# count(*)（Iceberg 读元数据、扫描 0 字节），必须逐表命中 docs/scale.json 里某一批已登记
# 的规模；命中不了就印出它最像哪一批、差在哪些表——装了新一批数据得先去登记。
# 顺带把 #14 那句「转化侧放大 427 倍而广告/券/活动维度表没有」按实测 scale 验成断言：
# scaled 组等比、fixed 组不变、sublinear 组严格落在两者之间。
grep_run "湖里的规模命中某一批已登记声明（+ 三组缩放不变量）" \
  "规模与声明一致 ✅" $PY scripts/lakehouse/verify_scale.py

hdr "L4 治理层（最小权限角色 + 列级排除）"
# v2 那套 Redshift 的「最小权限角色 + 动态脱敏」在湖仓上的等价物：一个专属 IAM 角色
# + Lake Formation 列级授权。**LF 没有值级掩码原语**，所以"脱敏"落成了"这列不在
# 授权面里"（SELECT * 里没有它，点名查报 COLUMN_NOT_FOUND）——这笔取舍写在
# scripts/lakehouse/governance.py 的 docstring 里，不是悄悄换的。
#
# 三条各自不可替代，少哪条都会留下一种"看起来绿了"：
#   · --selftest（离线）：策略清单 ⟷ 验收契约互相覆盖 + IAM 策略窄不窄。
#     专门盯 csv/ 那个旁路：中转库里是明文 CSV，S3 读一旦扩到 csv/，
#     上面所有列级授权都成了装饰，而云上探针照样全绿。
#   · --verify（连云）：授权面对不对，且**以 agent 角色实测**读得到什么、读不到什么。
#     只比授权面不够——授权齐不等于查得动。
#   · --verify-backend：后端真的在用这个角色查。前两条都发现不了最可能的那种回归：
#     角色建好了、权限发了，而 db.py 仍旧用 admin 凭证——治理全在，只是没接上。
grep_run "治理策略自测（策略清单 ⟷ 验收契约 ⟷ IAM 策略窄度，含 csv/ 旁路）" \
  "治理层自测通过 ✅" $PY scripts/lakehouse/governance.py --selftest
grep_run "以 agent 角色实测：拒绝表读不到、PII 列不在授权面里" \
  "治理层就位 ✅" $PY scripts/lakehouse/governance.py --verify
grep_run "后端确实在用受限角色查数（/health 的 identity 就是它）" \
  "后端确实在用受限角色查数 ✅" $PY scripts/lakehouse/governance.py --verify-backend

hdr "L5 查询路径"
# 原来这里把 GMV 写成字面量 149685621.44，那个值绑死在 v2 那批数据上：
# 重新生成一次数据，测试就红，而代码没问题。现在全部表达成 SQL 恒等式
# （两边都是查询，数据换了一起变），一个数据相关的数字都不写死。
grep_run "口径恒等式：多条路径的 GMV/退款/行数互等 + 两处 v1 缺陷仍被钉住" \
  "全部成立 ✅" $PY scripts/lakehouse/verify_mart_parity.py --numbers

# 不写死金标条数（加一道题不该让测试红）。锚行尾是关键：全通过时那行到 `arm=<名>` 结束，
# 有失败时 run_eval 会在后面接「；失败: [id…]」，正则自然不匹配。别写成宽松的 `OK`。
#
# `arm=` 这一段必须一起匹配，不能只锚到 `OK$`：三条 arm 之后 run_eval 在 OK 与失败清单
# **之间**插了 arm 标记，只锚 `OK$` 会永远不匹配——2026-09-03 就这么红了一次，27 道题
# 全 golden_ok，红的是这条正则。
grep_run "全部金标 SQL 均可在 Athena 执行" \
  "金标验证: [0-9]+/[0-9]+ OK · arm=[a-z]+$" env DB_BACKEND=athena $PY eval/run_eval.py --dry-run

hdr "L6 服务与前端渲染契约"
# 这一层自己起后端、测完就关，不依赖外部已有服务。
# 覆盖的是「UI 显示的是不是真的」——原来表数/行数/引擎名全写死在 HTML 里，
# 数据一变就静默错，静态检查看不出来。
UIPORT="${UIPORT:-8917}"
SRVLOG=$(mktemp -t aa-srv-XXXXXX.log)
DB_BACKEND=athena AWS_REGION="$AWS_REGION" GLUE_CATALOG_ID="$CATALOG" \
  CLAUDE_CODE_USE_BEDROCK=1 \
  ./backend/.venv/bin/python -m uvicorn server:app --app-dir backend \
  --host 127.0.0.1 --port "$UIPORT" > "$SRVLOG" 2>&1 &
SRVPID=$!
# 轮询等就绪，别用固定 sleep：首次 Athena/Glue 调用有几秒抖动。
# 顺手量两个数，它们是前端探针那套判据的两条前提（下面两条断言用）：
#   FIRST_MS —— 后端**答第一句话**要多快。用户刚起后端就打开页面时，浏览器探针打到的
#               就是这一发；`/health` 现在不等 Athena 查完才回，所以这个数应该很小。
#   OKWAIT_S —— 从起进程到 `ok:true` 要多久（= 数据层预热时间，由 Athena 决定，
#               实测 5s ～ 13.5s）。它必须还落在前端愿意等的那个窗口里。
# 这里 curl **带 -m**：不带超时的等待量不出"答得慢"这件事，而那正是要盯的。
UIUP=0
FIRST_MS=0
OKWAIT_S=0
SECONDS=0
for _ in $(seq 1 60); do
  if _out=$(curl -sf -m 30 -w $'\n%{time_total}' "http://127.0.0.1:$UIPORT/health" 2>/dev/null); then
    if [[ $FIRST_MS -eq 0 ]]; then
      FIRST_MS=$(awk -v t="$(printf '%s' "$_out" | tail -1)" 'BEGIN{printf "%d", t*1000}')
    fi
    if grep -q '"ok": *true' <<<"$_out"; then OKWAIT_S=$SECONDS; UIUP=1; break; fi
  fi
  sleep 1
done

if [[ $UIUP -eq 1 ]]; then
  grep_run "/health 报正确的后端引擎（不是写死的 Postgres 坐标）" \
    '"engine": *"Athena \+ S3 Tables \(Iceberg\)"' curl -s "http://127.0.0.1:$UIPORT/health"
  grep_run "/api/catalog 元数据来自 Glue（未降级）" \
    '"source": *"glue"' curl -s "http://127.0.0.1:$UIPORT/api/catalog"
  grep_run "前端存活探针打的 /health 可用（曾误改成 POST /ask → 405 → 永久假数据）" \
    '"ok": *true' curl -s "http://127.0.0.1:$UIPORT/health"
  # 下面两条补的是同一次真实事故：探针预算写死 2500ms（本地 Postgres 时代照 `SELECT 1`
  # 的几十毫秒定的），换成 Athena 后「刚起后端第一次打开页面」**必然**落进离线演示模式，
  # 页面还提示"请启动后端"。上面那些 curl 一条都没红过——它们不带超时，而且跑之前已经
  # 轮询把后端等热了。**没有跨过真实阈值的测试，对那个阈值零覆盖。**
  #
  # 但**别再拿"冷启动有多慢"去卡预算**：第一版这么写，预算 12000ms 当场就被量到的 12.5s
  # 顶爆（另一次同机器实测 5.0s）。那个数由 Athena 决定，写死多少都是猜分位数。
  # 现在盯的是两条前端真正依赖的性质，两者都与 Athena 快慢无关：
  #   (a) /health 答第一句话要快 —— 它不再等 ping 查完才回，而是回 dataLayer=warming；
  #   (b) 数据层预热完的时间仍落在前端愿意等的窗口内（前端等的是 warming，不是超时）。
  # 三个常量都从 web/index.html **grep** 出来，不在这儿抄第二份（抄了两边各自漂）。
  BUDGET_MS=$(grep -oE 'PROBE_TIMEOUT_MS=[0-9]+' web/index.html | head -1 | grep -oE '[0-9]+')
  WARM_N=$(grep -oE 'PROBE_WARM_ATTEMPTS=[0-9]+' web/index.html | head -1 | grep -oE '[0-9]+')
  GAP_MS=$(grep -oE 'PROBE_GAP_MS=[0-9]+' web/index.html | head -1 | grep -oE '[0-9]+')
  if [[ -z "$BUDGET_MS" || -z "$WARM_N" || -z "$GAP_MS" ]]; then
    printf '  \033[31mFAIL\033[0m  在 web/index.html 里找不到 PROBE_TIMEOUT_MS / PROBE_WARM_ATTEMPTS / PROBE_GAP_MS：断言已经不指向那套判据，先修断言\n'
    FAIL=$((FAIL + 1))
  else
    # (a) 单发预算 ⟷ 后端答第一句话的实测
    if [[ "$FIRST_MS" -le 0 ]]; then
      printf '  \033[31mFAIL\033[0m  没量到 /health 首次响应耗时（FIRST_MS=%s）\n' "$FIRST_MS"
      FAIL=$((FAIL + 1))
    elif [[ $((FIRST_MS * 2)) -le "$BUDGET_MS" ]]; then
      printf '  \033[32mPASS\033[0m  /health 答得够快（不等 Athena 查完）：实测首发 %sms，单发预算 %sms ≥ 2×\n' "$FIRST_MS" "$BUDGET_MS"
      PASS=$((PASS + 1))
    else
      printf '  \033[31mFAIL\033[0m  /health 首发 %sms，超过单发预算 %sms 的一半\n' "$FIRST_MS" "$BUDGET_MS"
      echo "        大概率是 /health 又变成「等 ping 查完才回」了（见 server.py 的三态）。"
      echo "        后果不是转圈或红字，是页面静默落进离线演示模式：答案换成写死的烘焙数据，"
      echo "        底部还提示「后端未连接」，而后端好好跑着。"
      FAIL=$((FAIL + 1))
    fi
    # (b) 数据层预热耗时 ⟷ 前端愿意等 warming 的窗口
    WINDOW_S=$(( WARM_N * GAP_MS / 1000 ))
    if [[ $((OKWAIT_S * 2)) -le "$WINDOW_S" ]]; then
      printf '  \033[32mPASS\033[0m  数据层预热 %ss，落在前端等 warming 的窗口内（%s 发 × %sms ≈ %ss，≥ 2×）\n' \
        "$OKWAIT_S" "$WARM_N" "$GAP_MS" "$WINDOW_S"
      PASS=$((PASS + 1))
    else
      printf '  \033[31mFAIL\033[0m  数据层预热 %ss，前端只等 ≈%ss（%s 发 × %sms）\n' \
        "$OKWAIT_S" "$WINDOW_S" "$WARM_N" "$GAP_MS"
      echo "        等不到就照旧掉进离线演示模式。要么查为什么预热这么慢，要么放宽"
      echo "        web/index.html 的 PROBE_WARM_ATTEMPTS——但先弄清是不是数据层出了问题。"
      FAIL=$((FAIL + 1))
    fi
    # /health 得真的带 dataLayer：少了这个字段，前端就分不出「还在预热」和「探不通」，
    # 于是又退回"拿一个写死的毫秒数裁决后端在不在"。
    grep_run "/health 带 dataLayer 三态（前端据此区分「还在预热」与「探不通」）" \
      '"dataLayer": *"(ok|warming|error)"' curl -s -m 30 "http://127.0.0.1:$UIPORT/health"
  fi
  if command -v node >/dev/null 2>&1; then
    grep_run "前端渲染契约（表数/行数/派生层/引擎名，中英双路径）" \
      "全部通过" node scripts/ui/render_test.mjs "http://127.0.0.1:$UIPORT"
  else
    note "跳过渲染契约测试：未装 node"
  fi
  # L0 那条只静态核对"路径是相对的、文件在 web/ 下"；这条**真取一遍**，因此还覆盖了
  # 挂载点本身和 /config.js 那条兜底路由——静态检查看不见路由，路由没了就又是一条 404。
  grep_run "shell 引用的资源在本地服务上逐个取得到（含 /config.js 兜底路由）" \
    "全部通过" $PY scripts/ui/asset_check.py "http://127.0.0.1:$UIPORT"
  # 上面三条 + render_test 全在「页面加载」这一段。用户真正在用的是 POST /ask，
  # 而它此前零覆盖：坏掉的样子是界面上一句红字，看起来像后端挂了。
  # 默认不跑是因为要烧一次 Opus（约 1 分钟），不是因为它不重要。
  if [[ $ASK -eq 1 ]]; then
    echo "  （/ask 流式契约，一次真问答，约 1 分钟…）"
    grep_run "POST /ask 流式契约（SSE 分帧 / delta 键 / 过期会话降级）" \
      "全部通过" $PY scripts/ask_probe.py "http://127.0.0.1:$UIPORT"
  else
    note "跳过 /ask 流式契约（加 --ask 跑，会烧一次 Opus）。不跑的代价：SSE 事件键名"
    note "  和过期 session_id 的降级路径没人盯——两者失败时都表现为「界面上一句红字」。"
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
# 只是「背后的数据」停在旧的静态原文。不依赖后端和凭证，只要快照在就能跑。
if command -v node >/dev/null 2>&1 && [[ -f web/catalog.json ]]; then
  grep_run "线上路径渲染契约（/api/catalog 403 → ./catalog.json 兜底）" \
    "全部通过" node scripts/ui/render_test_prod.mjs
else
  note "跳过线上路径渲染契约：缺 node 或 web/catalog.json"
fi

if [[ $L8 -eq 1 ]]; then
  hdr "L8 负测（缺陷注入 → 断言变红 → 还原）"
  # 上面 L0–L6 全绿只说明「现在没问题」，**不说明检查器还有效**。L8 往每个检查器
  # 对应的真实缺陷上打一枪，要求它变红并给出正确消息。见 scripts/negative_tests.py。
  #
  # 前面有红就不跑：负测第一步要求「注入前是绿的」，否则整批会因为同一个既存故障
  # 全部 ERROR，输出里看不出真正的原因。这不是省时间，是别产出误导性的报告。
  if [[ $FAIL -gt 0 ]]; then
    note "跳过：上面已有 $FAIL 项失败。负测要求注入前是绿的，先修正向检查。"
  else
    printf '  会临时改仓库内文件（含 data/csv/）并在退出时还原 + sha256 核对。\n'
    printf '  \033[90m约 3 分钟，逐个用例打印（清单：negative_tests.py --list）…\033[0m\n\n'
    if $PY scripts/negative_tests.py 2>&1 | sed 's/^/  /'; then
      printf '  \033[32mPASS\033[0m  L8：每个检查器都在对应缺陷面前变红并给出了正确消息\n'
      PASS=$((PASS + 1))
    else
      printf '  \033[31mFAIL\033[0m  L8：有检查器在缺陷面前保持沉默（见上）\n'
      FAIL=$((FAIL + 1))
    fi
  fi
fi

hdr "结果"
printf '  通过 %d · 失败 %d\n' "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo "  未全绿，先修再往下跑。"
  exit 1
fi
if [[ $L8 -eq 1 ]]; then
  printf '  L0–L6 + L8 全绿 ✅\n\n  还没覆盖的：\n'
else
  printf '  L0–L6 全绿 ✅\n\n  还没覆盖的：\n'
fi
cat <<'EOF'
    L7  端到端 agent：./backend/.venv/bin/python eval/run_eval.py
        （会烧 token，并覆盖 eval/report.md；基线在 eval/baseline/）
EOF
# 这两条的措辞刻意说「本次没跑」而不只是「可选」：一份写着"全绿"的报告读起来像
# "全都验过了"，而这两块恰恰是最容易被当成已覆盖的。
if [[ $ASK -eq 0 ]]; then
  cat <<'EOF'
    L6+ /ask 流式契约：SSE 分帧、事件键名、过期会话降级（本次没跑，加 --ask）
        不跑的代价：这三样坏了都表现成「界面上一句红字」，静态检查看不出来。
EOF
fi
if [[ $L8 -eq 0 ]]; then
  cat <<'EOF'
    L8  负测：证明这些检查器该报时真会报（本次没跑，加 --l8）
        不跑 L8 的代价：上面每一个 PASS 都只说明「现在没问题」，
        不说明那个检查器还有效——它可能已经变成一盏永远绿的灯。
EOF
fi
echo "    其余覆盖面缺口逐条写在 docs/test-plan.md 的「没有自动化覆盖」一节。"
