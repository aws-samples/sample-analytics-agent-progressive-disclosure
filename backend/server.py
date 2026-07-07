"""FastAPI 服务：把 agent 的事件流经 SSE 推给前端，并托管 web/ 静态页。

启动：见 backend/run.sh
路由：
  GET  /health        —— DB 与配置自检（公开）
  GET  /api/config    —— 前端据此初始化 app 层 Cognito 登录（公开，只含公开值）
  POST /ask           —— body {question, session_id?}，返回 text/event-stream（AUTH_ENABLED 时需 Bearer ID token）
  GET  /              —— 重定向到前端
  /app/*              —— 托管 ../web 静态资源

认证：app 层做 Cognito 登录（前端 amazon-cognito-identity-js SRP 拿 idToken），
本服务用 JWKS 校验 ID token。CloudFront / Lambda@Edge 全程不碰认证——这样静态资源可正常缓存。
"""
from __future__ import annotations

import os
import json
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import db
from agent import run_agent, MODEL

log = logging.getLogger("server")

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.normpath(os.path.join(HERE, "..", "web"))

# —— app 层 Cognito 认证配置（运行时由 compose env 注入；本地不设则关闭认证）——
AUTH_ENABLED = os.getenv("AUTH_ENABLED") == "1"
COGNITO_REGION = os.getenv("COGNITO_REGION", os.getenv("AWS_REGION", "us-east-1"))
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID", "")

app = FastAPI(title="App Analytics Agent")
# CORS：前端与后端同源（FastAPI 直接托管 web/），默认只放本地开发来源。
# 云上部署时把你的前端域名（如 CloudFront 分发域名）放进 CORS_ORIGINS 环境变量（逗号分隔）。
_DEFAULT_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ———————————————————————— 认证 ————————————————————————
_jwk_client = None


def _verify_jwt(token: str) -> dict:
    """校验 Cognito ID token，返回 claims；失败抛异常。延迟 import / 初始化 JWKS。"""
    global _jwk_client
    import jwt
    from jwt import PyJWKClient
    issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{issuer}/.well-known/jwks.json")
    signing_key = _jwk_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token, signing_key.key, algorithms=["RS256"],
        audience=COGNITO_CLIENT_ID, issuer=issuer,
    )
    if claims.get("token_use") != "id":
        raise ValueError("not an id token")
    return claims


def _auth_error(req: Request):
    """返回 None 表示放行；否则返回 401 响应。"""
    if not AUTH_ENABLED:
        return None
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        _verify_jwt(auth[7:])
    except Exception as e:  # noqa: BLE001
        # 细节只进服务端日志,不回显给客户端(异常文本可能含内部实现细节)
        log.warning("JWT verify failed: %s: %s", type(e).__name__, e)
        return JSONResponse({"error": "invalid token"}, status_code=401)
    return None


# ——— 按路径设 Cache-Control：静态库可长缓存（边缘+浏览器），壳/接口 no-store ———
@app.middleware("http")
async def _cache_headers(req: Request, call_next):
    resp = await call_next(req)
    path = req.url.path
    if path.startswith("/app/vendor/"):
        # echarts / 字体等公开静态库：内容稳定，长缓存
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        # HTML 壳 / auth / 接口：不缓存，保证改了立刻生效、token 校验每次走源
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/health")
async def health():
    return JSONResponse({
        "ok": db.ping(),
        "db": {"host": db.PG["host"], "port": db.PG["port"], "name": db.PG["dbname"]},
        "model": MODEL,
        "bedrock": os.getenv("CLAUDE_CODE_USE_BEDROCK") == "1",
        "region": os.getenv("AWS_REGION"),
        "authEnabled": AUTH_ENABLED,
    })


@app.get("/api/config")
async def api_config():
    # 公开：前端据此初始化 Cognito 登录。pool/client id 本就是公开值，不含敏感信息。
    return JSONResponse({
        "authEnabled": AUTH_ENABLED,
        "region": COGNITO_REGION,
        "userPoolId": COGNITO_USER_POOL_ID,
        "clientId": COGNITO_CLIENT_ID,
    })


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/ask")
async def ask(req: Request):
    err = _auth_error(req)
    if err is not None:
        return err
    body = await req.json()
    question = (body.get("question") or "").strip()
    session_id = body.get("session_id")
    deep = bool(body.get("deep"))
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    # CloudFront 源站读超时 60s：静默期每 HEARTBEAT_SECS 发一个 SSE 注释帧保活，
    # 否则深度分析(读多份 SOP + 调 metric/stats)的长静默会被 CloudFront 掐断 → 前端 network error。
    # 前端 data: 解析对注释帧(无 data: 行)会自动 continue 跳过，无需前端改动。
    HEARTBEAT_SECS = 15

    async def gen():
        yield _sse({"type": "start", "question": question})
        try:
            agen = run_agent(question, session_id, deep=deep).__aiter__()
            while True:
                task = asyncio.ensure_future(agen.__anext__())
                while True:
                    done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECS)
                    if task in done:
                        break
                    yield ": ping\n\n"                 # 静默保活，重置 CloudFront 读超时
                    if await req.is_disconnected():
                        task.cancel()
                        return
                try:
                    ev = task.result()
                except StopAsyncIteration:
                    break
                yield _sse(ev)
                if await req.is_disconnected():
                    break
        except Exception as e:  # noqa: BLE001
            # 只回错误类名,消息细节留在服务端日志
            log.exception("agent stream failed")
            yield _sse({"type": "error", "message": type(e).__name__})
        yield _sse({"type": "end"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
async def root():
    return RedirectResponse("/app/index.html")


if os.path.isdir(WEB):
    app.mount("/app", StaticFiles(directory=WEB, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
