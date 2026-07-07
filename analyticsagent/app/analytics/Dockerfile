# AgentCore Runtime 镜像 —— 数据分析 Agent（Claude Agent SDK on Bedrock）。
# 构建上下文 = app/analytics/（agentcore.json 的 codeLocation）。CodeBuild 云端构建为 ARM64。
# 关键:Claude Agent SDK 底层拉起 `claude` Code CLI 子进程,镜像必须带 Node + claude CLI。
# 知识库【不烤进镜像】——冷启动时由 runtime_config 从 S3 同步到 KNOWLEDGE_DIR。
# nosemgrep: dockerfile-source-not-pinned —— demo 镜像按 tag 固定即可
FROM python:3.11-slim

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
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 业务代码(app/analytics/ 的全部 .py):main.py / agent.py / tools.py / db.py /
# metric_layer.py / metrics_def.py / stats.py / knowledge_store.py / runtime_config.py
COPY . /app/
RUN rm -rf /app/.venv /app/__pycache__ /app/knowledge

# 只烤非敏感指针 RUNTIME_SECRET_ID + 静态默认值。动态/敏感配置(Aurora 端点、DB 口令、
# 知识桶名)全在 Secrets Manager 的 analytics-agent/runtime,由 runtime_config 冷启动时载入。
ENV RUNTIME_SECRET_ID=analytics-agent/runtime \
    AWS_REGION=us-west-2 \
    KNOWLEDGE_DIR=/app/knowledge \
    CLAUDE_CODE_USE_BEDROCK=1 \
    ANTHROPIC_MODEL=global.anthropic.claude-opus-4-8

# 关键:claude CLI 拒绝以 root 跑 --dangerously-skip-permissions(SDK 的 bypassPermissions
# 会下发该 flag)。故用非 root 用户;HOME 可写供 CLI 缓存;KNOWLEDGE_DIR 可写供 S3 同步。
RUN useradd -m -u 1000 bedrock_agentcore \
 && mkdir -p /app/knowledge \
 && chown -R bedrock_agentcore:bedrock_agentcore /app
USER bedrock_agentcore
ENV HOME=/home/bedrock_agentcore

# AgentCore Runtime HTTP 契约端口
EXPOSE 8080
# checkov:skip=CKV_DOCKER_2: AgentCore 用自己的 GET /ping 契约做健康检查,非 Docker HEALTHCHECK
CMD ["python", "-m", "main"]
