# App Analytics Agent —— 云上镜像
# 关键:Claude Agent SDK 底层会拉起 `claude` Code CLI 子进程,所以镜像必须带 Node + claude CLI,光有 Python 不够。
# nosemgrep: dockerfile-source-not-pinned —— demo 镜像按 tag 固定即可,无需 digest 锁定
FROM python:3.11-slim

# 用 bash + pipefail 跑 RUN,使管道任一步失败即整条失败(下面 curl | bash 需要)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Node 20 + claude-code CLI(Agent SDK 依赖的子进程)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖,利用层缓存
COPY backend/requirements.txt /app/backend/requirements.txt
# 依赖优先从 backend/wheelhouse/ 装,装不到才回 pypi。这不是加速,是为了让构建能完成:
# claude-agent-sdk 的 manylinux wheel 有 96MB,而这条链路上大文件传到一半就断
# (主机上 pip 也一样,6 次尝试只拿到 10.7MB/96MB;同一批里 46 个小 wheel 全部一次过,
# 所以是尺寸相关而非 pypi 或 pip 的问题)。wheelhouse 用 curl -C - 断点续传攒齐,
# 之后构建不再碰网络。
#
# 用 --mount=type=bind 而不是 COPY:wheelhouse 有 250MB,COPY 会把它留在镜像层历史里,
# 而这个镜像要推 ECR——在一条 96MB 都传不完的链路上,白胖 250MB 是实打实的风险。
# bind mount 只在这一步存在,不进任何层。
#
# wheelhouse 为空时行为与原来完全一致(--find-links 指向空目录就是纯走 pypi),
# 所以 docker-compose.cloud.yml 那条路径不受影响。重建 wheelhouse:
#   ./backend/.venv/bin/python -m pip download --platform manylinux_2_17_x86_64 \
#     --only-binary=:all: --python-version 3.11 -d backend/wheelhouse -r backend/requirements.txt
RUN --mount=type=bind,source=backend/wheelhouse,target=/wheels \
    pip install --no-cache-dir --retries 10 --timeout 120 \
      --find-links /wheels -r /app/backend/requirements.txt

# 业务代码:server.py 用相对路径找 ../web、tools.py 找 ../knowledge,
# 目录布局必须是 /app/backend + /app/web + /app/knowledge
COPY backend/ /app/backend/
COPY web/ /app/web/
COPY knowledge/ /app/knowledge/
# Athena 客户端。db.py::_athena() 先裸 import 一次(那是 AgentCore 那份平铺布局),
# 失败才回退到仓库布局 ../scripts/lakehouse —— 这个镜像走的是后者,所以这行必须有:
# 少了它 DB_BACKEND=athena(现在是默认值)会在第一条查询上抛 SqlError,
# 而 /health 之前一切正常,报错指向"找不到模块"而不是"镜像少拷了一个文件"。
COPY scripts/lakehouse/athena.py /app/scripts/lakehouse/athena.py
# 安全网:本地产物若混入则清掉(正常打包已剔除)
RUN rm -rf /app/backend/.venv /app/backend/.pgdata /app/backend/__pycache__

# 默认走 Bedrock。区域必须是 us-west-2:S3 Tables 表桶、Glue 目录、Athena 工作组
# 都在那儿。这里原来兜底成 us-east-1(v1 时代 Bedrock 的习惯值),而 db.py 的默认
# 后端现在是 athena —— 区域错了不会报"区域错",报的是工作组/表不存在。
#
# PG* 是 v1 那条自带库路径的兜底(docker-compose.cloud.yml 会连同 DB_BACKEND=postgres
# 一起覆盖)。DB_BACKEND 不在这里钉:留空就用 db.py 的默认值 athena。
ENV CLAUDE_CODE_USE_BEDROCK=1 \
    AWS_REGION=us-west-2 \
    ANTHROPIC_MODEL=global.anthropic.claude-opus-4-8 \
    PGHOST=db PGPORT=5432 PGDATABASE=app_analytics PGUSER=postgres PGPASSWORD=postgres \
    PORT=8000

# 关键:claude CLI 拒绝以 root 跑 --dangerously-skip-permissions(SDK 的 bypassPermissions 会下发该 flag)。
# 故用非 root 用户运行,HOME 可写供 CLI 缓存。
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

WORKDIR /app/backend
EXPOSE 8000
# 健康检查:打 /health(curl 在装 Node 时已安装且未被清理)
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1
# 注意 --host 0.0.0.0:容器内必须对外监听,而非 server.py __main__ 里的 127.0.0.1
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
