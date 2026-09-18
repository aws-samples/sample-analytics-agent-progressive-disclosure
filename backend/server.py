"""FastAPI 服务：把 agent 的事件流经 SSE 推给前端，并托管 web/ 静态页。

启动：见 backend/run.sh
路由：
  GET  /health        —— DB 与配置自检（公开）。前端启动探针打的就是这个
  GET  /api/catalog   —— UI 的元数据来源：表清单/字段/行数/分层/治理现状（见 catalog.py）
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
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import (StreamingResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import catalog
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

def _warm_data_layer() -> None:
    """启动就起一发 ping，把冷启动那一下的代价从「浏览器探针」挪到「服务启动」。

    实测：新进程第一次 ping **5.0s ～ 13.5s**（建 boto3 客户端 + AssumeRole 拿凭证 +
    一次真 Athena 查询在 workgroup 里排队），之后 1.2–1.9s。用户「起完后端就打开页面」
    时，浏览器探针打到的正是这一发——预热让它大概率已经在飞（甚至已经回来），
    而不是从零开始。

    这只是省时间，**不是正确性的依赖**：真正保证不误判的是 `/health` 的三态
    （见下面 `_data_layer_state`）。所以预热失败一律不抛——就绪门是 `/health`，
    在启动路径上抛异常只会把一个数据层问题伪装成"服务起不来"。
    """
    try:
        ok, state = _data_layer_state()   # 走同一套状态机，结果能被紧随其后的 /health 直接吃到
        log.info("data layer warm-up: ok=%s state=%s", ok, state)
    except Exception as e:  # noqa: BLE001
        log.warning("data layer warm-up failed (不影响启动): %s: %s", type(e).__name__, e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 后台跑，不 await：uvicorn 要先把端口打开、打印 "running on"，否则用户看着日志
    # 以为还没起来。浏览器发第一个请求还要几百毫秒，这一发在那之前就开始了。
    asyncio.get_running_loop().run_in_executor(None, _warm_data_layer)
    yield


app = FastAPI(title="App Analytics Agent", lifespan=_lifespan)
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


# —————————— /health 的数据层状态：**答得快**，而且把「还没探完」和「探不通」分开 ——————————
# `db.ping()` 是一次**真** Athena 查询（`SELECT 1`）。它有多慢**不由我们决定**：同一台机器
# 上实测过 5.0s，也实测过 13.5s（建 boto3 客户端 + AssumeRole 拿凭证 + 查询在 workgroup
# 里排队）。所以任何「前端等 N 毫秒」的写法都是在猜一个云上的分位数——猜小了页面静默落进
# 离线演示模式，猜大了就是把上限往后挪一点，下一次抖动照样翻车（这个坑我们踩了两次：
# 2500ms 是照本地 Postgres 定的，改成 12000ms 之后 L6 立刻量到 12.5s）。
#
# 换成三态，让前端不必猜：
#   ok=true,  dataLayer="ok"       —— 探通了，走真实链路
#   ok=false, dataLayer="warming"  —— **后端在，只是第一发 ping 还没回来**：前端要继续等
#   ok=false, dataLayer="error"    —— 探完了，不通：这才是降级的理由
# 「连不上 /health」是第四种，前端那边自己看得见。这样重要的那条区分——**后端在不在**——
# 由一个本地就能答的事实决定，不再由 Athena 今天心情如何决定。
_PING_TTL_S = 10.0          # 探通过就缓存这么久：探针重试 + 用户刷新共用一发（真查询要钱）
_PING_WAIT_S = 2.0          # /health 最多等在飞的那一发这么久，等不到就照实回 warming
_ping_lock = threading.Lock()
_ping_result: tuple[float, bool] | None = None      # (完成时刻, ok)
_ping_wait: threading.Event | None = None           # 非 None ⟹ 有一发在飞，等它比再起一发好


def _start_ping() -> threading.Event:
    """起一发后台 ping，返回「它完事」的事件。调用方必须持 _ping_lock。"""
    global _ping_wait
    ev = threading.Event()
    _ping_wait = ev

    def run() -> None:
        global _ping_result, _ping_wait
        ok = False
        try:
            ok = db.ping()
        except Exception as e:  # noqa: BLE001
            # 探不通是**结果**，不是异常：吞在这里，由 dataLayer="error" 报出去。
            log.warning("data layer ping failed: %s: %s", type(e).__name__, e)
        with _ping_lock:
            _ping_result = (time.monotonic(), ok)
            _ping_wait = None
        ev.set()

    threading.Thread(target=run, name="ping", daemon=True).start()
    return ev


def _data_layer_state(fresh: bool = False) -> tuple[bool, str]:
    """返回 (ok, dataLayer)。**不会长时间阻塞**：最多等 _PING_WAIT_S。"""
    with _ping_lock:
        prev = _ping_result
        if not fresh and prev and time.monotonic() - prev[0] < _PING_TTL_S:
            return prev[1], "ok" if prev[1] else "error"
        # 已经有一发在飞就等它，别再起一发：两条真查询谁也不比谁快，只是多花一份钱。
        ev = _ping_wait or _start_ping()
    # 手上有上一发结果（只是过了 TTL，后台正在刷）⟹ **立刻**报它，一秒都不等。
    # 原来这里是先 `ev.wait(_PING_WAIT_S)` 再退回 prev，代价实测就是：一个 ok:true 的
    # /health 也要 2.011s。而前端的单发预算是照"后端答一句话"定的，这 2s 白等把它推向
    # 危险区，也让"重启后端后立刻刷新"更容易赶不上。
    # 代价是这个答案最多旧 TTL + 一发 ping：对"后端在不在"这个判断足够，且它只会让
    # 刚坏掉的数据层多被说成 ok 一个刷新周期，不会把活着的后端说成死的。
    if prev and not fresh:
        return prev[1], "ok" if prev[1] else "error"
    # 从没探过（进程刚起）：只能等一小会儿，等不到就照实回 warming——它不是"不通"。
    # fresh 是人/运维显式要求真探（`?fresh=1`），等久一点是它的语义。
    if ev.wait(60.0 if fresh else _PING_WAIT_S):
        with _ping_lock:
            ok = bool(_ping_result and _ping_result[1])
        return ok, "ok" if ok else "error"
    return False, "warming"


_INFO_WAIT_S = 1.0          # /health 最多等 backend_info() 这么久，超了就先报"详情待补"
_info_cache: dict | None = None
_info_lock = threading.Lock()


def _backend_info() -> dict:
    """缓存 `db.backend_info()`。它的字段进程活着就不会变，而**第一次**要 AssumeRole。

    冷启动实测 1.93s；更糟的是它和那发 ping 抢 `db.py::_ath_lock`（同一份初始化），
    实测排在后面时要 **5.6s**——L6 那条「/health 首发 ≤ ½ 单发预算」当场把它顶红了。
    异常不外抛：`/health` 的职责是"照实说现在知道什么"，这里失败不该让整个探针 500。
    """
    global _info_cache
    with _info_lock:
        if _info_cache is None:
            try:
                _info_cache = db.backend_info()
            except Exception as e:  # noqa: BLE001
                log.warning("backend_info failed: %s: %s", type(e).__name__, e)
                return {"engine": db.ENGINE_LABEL, "error": type(e).__name__}
        return _info_cache


@app.get("/health")
async def health(fresh: int = 0):
    # db 这一坨原来无条件读 db.PG，于是 DB_BACKEND=redshift 时前端顶栏会显示
    # 127.0.0.1:5433——库明明在 Redshift 上。改成按后端分派（db.backend_info()），
    # 并带上 engine，让前端不必自己拼引擎名。
    # 两件事都要**离开事件循环**、**并发**做，而且**两件都要有上限**——否则「/health
    # 答一句话有多快」又变成由云上今天心情决定，而前端的单发预算是照前者定的：
    #   · `_data_layer_state()` 最多等 _PING_WAIT_S，等不到回 warming；
    #   · `_backend_info()` 最多等 _INFO_WAIT_S。它冷启动要 AssumeRole（实测 1.93s，
    #     排在那发 ping 后面抢同一把初始化锁时 **5.6s**），而它产出的东西全是诊断字段。
    # 这里踩过两次：先是它同步调在 async 函数里（串行相加 4.06s），改成 gather 之后
    # **仍然**被它拖着——gather 等最慢的那个。所以现在是"等一小会儿，等不到就照实说
    # 详情还没拿到"：任务不取消（用 shield），它算完会进缓存，下一发就带上了。
    t0 = time.monotonic()
    info_task = asyncio.ensure_future(asyncio.to_thread(_backend_info))
    ok, state = await asyncio.to_thread(_data_layer_state, bool(fresh))
    if fresh:
        info = await info_task                 # 显式要求真探：等到底是它的语义
    else:
        # **从请求开始算**，不是从这一行算：两个上限串起来相加就又变成 3.0s（实测），
        # 而它们本该是并发的。info 在上面那段等待里已经跑了 t0..now，这里只补剩下的。
        left = max(0.0, _INFO_WAIT_S - (time.monotonic() - t0))
        try:
            info = await asyncio.wait_for(asyncio.shield(info_task), left)
        except asyncio.TimeoutError:
            # 少报一个 identity 也比整页因为"探针超时"落进离线演示模式好。
            # engine 仍然报得出（db.ENGINE_LABEL 是它的唯一出处），前端顶栏有兜底。
            info = {"engine": db.ENGINE_LABEL, "detailsPending": True}
            info_task.add_done_callback(lambda t: t.exception())   # 别留"异常没人取"的告警
    return JSONResponse({
        "ok": ok,
        "dataLayer": state,
        "db": info,
        "model": MODEL,
        "bedrock": os.getenv("CLAUDE_CODE_USE_BEDROCK") == "1",
        "region": os.getenv("AWS_REGION"),
        "authEnabled": AUTH_ENABLED,
    })


@app.get("/api/catalog")
async def api_catalog(refresh: int = 0):
    """UI 的元数据来源：表清单 / 字段 / 行数 / 分层 / 治理现状。

    前端原来把这些写死在 HTML 里（39 张表、「~19万行」、「PostgreSQL」），
    数据一变就全错且不会报警。上 Glue Data Catalog 的意义正是元数据有了机器可读的
    实际态，所以 UI 应该读它而不是再抄一份。装配逻辑见 backend/catalog.py。

    `?refresh=1` 跳过缓存（默认缓存 5 分钟）；演示中改了库想立刻看到时用。
    Glue 读不到会降级到 information_schema，并在 `source` 字段里说明，不静默。
    """
    try:
        return JSONResponse(await asyncio.to_thread(catalog.build, bool(refresh)))
    except Exception as e:
        # 目录挂了不该让整个页面白屏：回 200 + error，前端退回静态文案。
        # 异常原文（含 boto3 报错里的 ARN / 账号 / 内部路径）只进服务端日志，
        # 客户端只拿异常类名 —— 与 /ask 的错误事件同一口径（CodeQL py/stack-trace-exposure）。
        log.exception("catalog build failed")
        return JSONResponse({"error": type(e).__name__, "source": "unavailable"})


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


@app.get("/config.js")
async def config_js():
    """线上这份由部署脚本生成在 S3 站点根(注入 Cognito 池等);本地没有。

    回 404 也能跑(shell 会回退 /api/config),但日志里那条 404 看着像故障,排查时
    会误导人。这里回一个空脚本:`window.APP_CONFIG` 依然缺席,回退行为一字不变。
    """
    return Response("// 本地开发:无部署期注入配置,shell 回退 /api/config\n",
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


if os.path.isdir(WEB):
    app.mount("/app", StaticFiles(directory=WEB, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
