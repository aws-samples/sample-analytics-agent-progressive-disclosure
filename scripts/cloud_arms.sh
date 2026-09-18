#!/usr/bin/env bash
# 三 arm 网页后端 —— 云上起停与隧道
#
# ## 这个脚本解决什么
#
# 三条 arm 的后端跑在同区（us-west-2）一个 Fargate 任务里，本地浏览器通过 SSM
# 端口转发连上去。为什么要搬上云：DuckDB 是**进程内**引擎，在笔记本上跑它、
# 同时从笔记本调云上的 Athena 和 Redshift，量到的三个延迟不是同一样东西
# （同一条查询笔记本 229.8s / 区内 40-239ms，差的全是网络位置）。三条 arm 放进
# 同一个任务后，机器、算力、区域、网络位置完全相同，笔记本到后端只剩一条共用的
# HTTP 跳，残差才只剩引擎。
#
# 手工做这件事有个坑:任务 ID 和容器运行时 ID **每次启动都变**，而 SSM 的 target
# 要求 `ecs:<集群>_<任务ID>_<运行时ID>` 三段拼全。抄错一段报的是找不到目标，
# 不指向"你抄的是上一次那个"。所以这里一律现查。
#
# ## 用法
#
#   bash scripts/cloud_arms.sh up                  # 起任务（约 1 分钟）
#   bash scripts/cloud_arms.sh health              # 三条 arm 各自报自己的引擎
#   bash scripts/cloud_arms.sh tunnel duckdb       # 开隧道，占住这个终端
#   bash scripts/cloud_arms.sh status
#   bash scripts/cloud_arms.sh down                # 停任务，计费到此为止
#
# 隧道开着的时候浏览器一律开 http://127.0.0.1:8000/ —— 本地端口固定 8000，
# 换 arm 只换远端端口，所以 URL 永远不变。换 arm：Ctrl-C 掉当前隧道再起下一条。
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"

CLUSTER=analytics-agent-relay
FAMILY=analytics-agent-arms
CONTAINER=arms
LOCAL_PORT=8000

# 复用 ask-relay 的网络配置。assignPublicIp 必须 ENABLED:这些子网没挂 NAT,
# 容器靠公网 IP 出网（要访问 Athena / Redshift Data API / Bedrock / S3）。
SUBNETS='["subnet-0bac5e670766638dd","subnet-06a03383d4a1698bd"]'
SGS='["sg-0a9ab97aa4da85e87"]'

# arm 到远端端口。`DB_BACKEND` 是 db.py 在 import 时读一次的模块级常量,所以三条
# arm 必须是三个独立进程 —— 镜像的 CMD 就是三个 uvicorn,分别监听这三个端口。
arm_port() {
  case "$1" in
    athena)   echo 8000 ;;
    duckdb)   echo 8001 ;;
    redshift) echo 8002 ;;
    *) echo "未知 arm: $1（可选 athena / duckdb / redshift）" >&2; return 1 ;;
  esac
}

# 现查正在跑的任务,不用状态文件:状态文件会在换终端、换机器、任务被别的方式停掉
# 之后变成谎报,而这里问的是 ECS 本身。
# 这里原来把 aws 的 stderr 扔进 /dev/null。配上开头的 `set -e`,后果是
# **aws 一失败整个脚本静默退出**:`up` 和 `status` 都以 `existing="$(running_task)"`
# 开头,凭证过期或 aws 不在 PATH 时它们一个字都不打印,长得跟「什么都没发生」
# 完全一样。零输出这种症状连从哪查都不知道,所以现在把 aws 的原话照传出来。
#
# 还要**校验返回值的形状**而不是假定它对:aws 有可能退出码 0 却在 stdout 上留下
# 别的东西（弃用警告之类）,那时 `!= "None"` 会把警告当成任务 ARN,于是 `up`
# 报「已经有一个在跑了」而其实没有——那是个比报错更难查的错。
running_task() {
  local out
  if ! out="$(aws ecs list-tasks --cluster "$CLUSTER" --family "$FAMILY" \
              --desired-status RUNNING --query 'taskArns[0]' --output text 2>&1)"; then
    {
      echo "查 ECS 任务列表失败,aws 原话:"
      echo "$out" | sed 's/^/  /'
      echo "常见三个原因:"
      echo "  1. 凭证过期 —— 跑 mwinit,再 ~/.aws/ada-credential-process.sh --force"
      echo "  2. aws 不在 PATH —— command -v aws"
      echo "  3. 区域没设 —— 本脚本第 29 行自己 export AWS_REGION,一般轮不到这条"
    } >&2
    return 1
  fi
  case "$out" in
    None|arn:aws:ecs:*) echo "$out" ;;
    *)
      echo "aws 退出码 0,但回了没预料到的东西（期望 None 或 arn:aws:ecs:…）:" >&2
      echo "$out" | sed 's/^/  /' >&2
      return 1 ;;
  esac
}

require_task() {
  local arn; arn="$(running_task)"
  if [ "$arn" = "None" ] || [ -z "$arn" ]; then
    echo "没有在跑的 $FAMILY 任务。先跑:bash scripts/cloud_arms.sh up" >&2
    return 1
  fi
  echo "${arn##*/}"
}

cmd_up() {
  local existing; existing="$(running_task)"
  if [ "$existing" != "None" ] && [ -n "$existing" ]; then
    echo "已经有一个在跑了:${existing##*/}"
    echo "（要重起先 down。同时跑两个任务只是多花钱,不会更快。）"
    return 0
  fi

  echo "启动任务…"
  local arn task
  arn="$(aws ecs run-task \
    --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
    --enable-execute-command \
    --network-configuration "awsvpcConfiguration={subnets=$SUBNETS,securityGroups=$SGS,assignPublicIp=ENABLED}" \
    --query 'tasks[0].taskArn' --output text)"
  task="${arn##*/}"
  echo "任务 $task,等 RUNNING…"

  local i status
  for i in $(seq 1 40); do
    status="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
      --query 'tasks[0].lastStatus' --output text)"
    if [ "$status" = "RUNNING" ]; then break; fi
    if [ "$status" = "STOPPED" ]; then
      echo "任务起不来:" >&2
      # 键名只能用 ASCII:JMESPath 的裸标识符不接受非 ASCII,写中文键报的是
      # `Unknown token`,看起来像表达式写错而不是键名不合法。
      aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
        --query 'tasks[0].{reason:stoppedReason,container:containers[0].reason}' --output json >&2
      return 1
    fi
    sleep 5
  done
  [ "$status" = "RUNNING" ] || { echo "等超时,最后状态 $status" >&2; return 1; }

  # ECS Exec 的 agent 比容器晚就绪,health / tunnel 都要用它,所以在这儿等掉。
  for i in $(seq 1 24); do
    if [ "$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
      --query "tasks[0].containers[0].managedAgents[?name=='ExecuteCommandAgent'].lastStatus | [0]" \
      --output text)" = "RUNNING" ]; then break; fi
    sleep 5
  done

  echo "就绪:$task"
  echo "下一步:bash scripts/cloud_arms.sh tunnel duckdb"
}

cmd_health() {
  local task; task="$(require_task)"
  # 用 exec 从容器内部打 /health。这是从外面确认"哪个端口是哪条 arm"的唯一可靠办法:
  # 启动横幅对三条 arm 一律打印 backend=athena,不能用来辨认。
  aws ecs execute-command --cluster "$CLUSTER" --task "$task" --container "$CONTAINER" --interactive \
    --command 'sh -c "for p in 8000 8001 8002; do printf \"%s -> \" $p; curl -s --max-time 25 http://127.0.0.1:$p/health || echo 起不来; echo; done"' \
    2>/dev/null | grep -E '^(8000|8001|8002) ->' || {
      echo "exec 没拿到输出。任务可能刚起、agent 还没就绪,过十几秒再试。" >&2; return 1; }
}

cmd_tunnel() {
  local arm="${1:-}"
  [ -n "$arm" ] || { echo "用法:bash scripts/cloud_arms.sh tunnel <athena|duckdb|redshift>" >&2; return 1; }
  local port; port="$(arm_port "$arm")"
  local task; task="$(require_task)"

  if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "本地 $LOCAL_PORT 已被占用 —— 大概是另一条隧道或本地 run.sh 还开着。" >&2
    echo "本地 $LOCAL_PORT 同时只能挂一条,先把那个关掉。" >&2
    return 1
  fi

  local runtime target
  runtime="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
    --query 'tasks[0].containers[0].runtimeId' --output text)"
  target="ecs:${CLUSTER}_${task}_${runtime}"

  echo "arm=$arm  远端端口=$port  ->  http://127.0.0.1:$LOCAL_PORT/"
  echo "看到 Waiting for connections... 就通了。Ctrl-C 结束(隧道随进程走,别关窗口)。"
  echo
  # 注意文档名是 ...ToRemoteHost:ECS 目标不能用 AWS-StartPortForwardingSession,
  # 那个只对 EC2 实例生效。
  exec aws ssm start-session --target "$target" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"localhost\"],\"portNumber\":[\"$port\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
}

cmd_status() {
  local arn; arn="$(running_task)"
  if [ "$arn" = "None" ] || [ -z "$arn" ]; then
    echo "没有在跑的任务(不计费)。"
    return 0
  fi
  aws ecs describe-tasks --cluster "$CLUSTER" --tasks "${arn##*/}" \
    --query 'tasks[0].{task:taskArn,status:lastStatus,startedAt:startedAt,cpu:cpu,memory:memory,az:availabilityZone}' \
    --output json
  echo "约 \$0.23/小时。测完:bash scripts/cloud_arms.sh down"
}

cmd_down() {
  local arn; arn="$(running_task)"
  if [ "$arn" = "None" ] || [ -z "$arn" ]; then
    echo "没有在跑的任务,无需停。"
    return 0
  fi
  aws ecs stop-task --cluster "$CLUSTER" --task "${arn##*/}" \
    --reason "cloud_arms.sh down" --query 'task.desiredStatus' --output text
  echo "已请求停止 ${arn##*/}(几十秒后彻底停,计费到此为止)。"
}

case "${1:-}" in
  up)     shift; cmd_up "$@" ;;
  health) shift; cmd_health "$@" ;;
  tunnel) shift; cmd_tunnel "$@" ;;
  status) shift; cmd_status "$@" ;;
  down)   shift; cmd_down "$@" ;;
  *)
    cat <<'EOF'
用法:bash scripts/cloud_arms.sh <命令>

  up                              起云上任务(三条 arm 一次全起,约 1 分钟)
  health                          三条 arm 各报自己的引擎,确认端口对应关系
  tunnel <athena|duckdb|redshift> 开隧道,占住当前终端
  status                          在跑没有、跑了多久、多少钱
  down                            停任务

隧道开着时浏览器开 http://127.0.0.1:8000/ —— 本地端口固定,换 arm 不换 URL。
EOF
    exit 1 ;;
esac
