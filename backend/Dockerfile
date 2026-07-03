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
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 业务代码:server.py 用相对路径找 ../web、tools.py 找 ../knowledge,
# 目录布局必须是 /app/backend + /app/web + /app/knowledge
COPY backend/ /app/backend/
COPY web/ /app/web/
COPY knowledge/ /app/knowledge/
# 安全网:本地产物若混入则清掉(正常打包已剔除)
RUN rm -rf /app/backend/.venv /app/backend/.pgdata /app/backend/__pycache__

# 默认走 Bedrock;PG 指向 compose 里的 db 服务。运行时可被 compose env 覆盖。
ENV CLAUDE_CODE_USE_BEDROCK=1 \
    AWS_REGION=us-east-1 \
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
