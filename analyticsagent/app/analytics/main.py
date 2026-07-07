"""AgentCore Runtime entrypoint —— 数据分析 Agent（Claude Agent SDK on Bedrock）。

冷启动设计（本项目的重点）：
  两层成本。(1) 容器冷启（新 microVM）：镜像拉起 + 进程启动 + 【runtime_config 在
  import 时从 S3 同步知识树】+ 首个请求懒连接暖客户端（拉起 claude CLI + 连 MCP，约
  8-10s）。(2) 暖调用：复用同一个已连接的 ClaudeSDKClient，跳过 CLI 重拉，后续问题快。

  runtime_config 必须**最先** import：它在 module 载入时就把 DB 口令(Secrets Manager)
  和知识树(S3)准备好，之后 tools/db 才拿得到正确配置与本地文档。

暖客户端复用（借鉴 lark-claude-tag 的验证过的做法）：
  AgentCore 的 microVM 会话隔离、串行处理请求。我们连接一个 module 级 ClaudeSDKClient
  并跨调用复用（CLI + MCP 保持热）。按 session_id 绑定，session 变了/到 turn 上限就重建，
  防跨会话上下文串味、并给上下文增长封顶。坏客户端下次请求重建。

输出契约（被 /ask Lambda 消费，逐条转成 SSE data: 帧发给前端）：
  yield {"type": "stage"|"sql"|"rows"|"text"|"metric"|"stats"|"result"|"done"|"error", ...}
"""
import asyncio
import logging

import runtime_config  # noqa: F401 —— 必须最先 import：载入 DB 口令 + 同步 S3 知识树

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from claude_agent_sdk import ClaudeSDKClient

from agent import build_options, stream_events

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()

# —— 暖客户端生命周期 ——
import os
MAX_WARM_TURNS = int(os.environ.get("AGENT_MAX_WARM_TURNS", "25"))

_client: ClaudeSDKClient | None = None
_client_session: str | None = None
_client_turns = 0
_client_lock = asyncio.Lock()


async def _reset_client() -> None:
    global _client, _client_session, _client_turns
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001 —— 尽力拆除
            logger.warning("[warm] disconnect 失败(忽略)", exc_info=True)
    _client = None
    _client_session = None
    _client_turns = 0


async def _get_client(session_id: str) -> ClaudeSDKClient:
    """返回绑定到 session_id 的暖客户端；session 变了或到 turn 上限则重建。"""
    global _client, _client_session, _client_turns
    if _client is not None and (_client_session != session_id or _client_turns >= MAX_WARM_TURNS):
        reason = "session 变化" if _client_session != session_id else f"turn 上限 {MAX_WARM_TURNS}"
        logger.info("[warm] 重建客户端(%s)", reason)
        await _reset_client()
    if _client is None:
        client = ClaudeSDKClient(options=build_options())
        await client.connect()  # 一次性拉起 claude CLI + 连接 MCP server
        _client = client
        _client_session = session_id
        _client_turns = 0
        logger.info("[warm] ClaudeSDKClient 已连接(CLI + MCP 热)session=%s", session_id)
    _client_turns += 1
    return _client


@app.entrypoint
async def agent_invocation(payload, context):
    # /ask Lambda 发 {question, session_id?, deep?}；`agentcore invoke` 发 {prompt}。都接。
    question = (payload.get("question") or payload.get("prompt") or "").strip()
    session_id = payload.get("session_id") or getattr(context, "session_id", None) or "default"
    deep = bool(payload.get("deep"))
    if not question:
        yield {"type": "error", "message": "empty question"}
        return
    logger.info("[req] session=%s deep=%s q_len=%d", session_id, deep, len(question))

    yield {"type": "start", "question": question}
    async with _client_lock:
        try:
            client = await _get_client(session_id)
            async for ev in stream_events(client, question, deep=deep):
                yield ev
        except Exception as e:  # noqa: BLE001 —— 本轮失败：丢弃暖客户端下次重连，并回一个 error 事件
            logger.warning("agent turn 失败；重置暖客户端", exc_info=True)
            await _reset_client()
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
    yield {"type": "end"}


if __name__ == "__main__":
    app.run()
