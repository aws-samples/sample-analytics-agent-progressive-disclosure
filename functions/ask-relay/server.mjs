// /ask 中继(Fargate 版)。浏览器 → CloudFront(VPC origin,私网)→ 内网 ALB → 本服务
//   → InvokeAgentRuntime → AgentCore Runtime。
//
// 为什么从 Lambda Function URL 迁到 Fargate:本账号组织 SCP 拒绝浏览器侧
// (公开 URL / Cognito 联合角色)调用 Function URL,而 CloudFront OAC 又签不了
// POST body。VPC origin 回源不做任何签名 → 没有 SCP/OAC 参与,JWT 直达应用层
// (同账号 CMDB 项目已验证此模式可行)。
//
// 职责与 Lambda 版(functions/ask/index.mjs)一致:
//   ① 验 Cognito ID token(应用层认证;链路本身靠「ALB 仅放行 CloudFront 前缀列表」封闭)
//   ② 调 AgentCore Runtime;③ 把 Runtime 的 SSE 字节流原样透传,块间隔 >15s 补 ": ping"
//      心跳注释帧,防 CloudFront OriginReadTimeout(60s)在长静默时掐断连接。

import http from "node:http";
import { CognitoJwtVerifier } from "aws-jwt-verify";
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from "@aws-sdk/client-bedrock-agentcore";

const PORT = Number(process.env.PORT || 8000);
const REGION = process.env.AWS_REGION || "us-west-2";
const RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN;

const verifier = CognitoJwtVerifier.create({
  userPoolId: process.env.COGNITO_USER_POOL_ID,
  clientId: process.env.COGNITO_CLIENT_ID,
  tokenUse: "id",
});

const agent = new BedrockAgentCoreClient({ region: REGION });

// AgentCore runtimeSessionId 约束:[a-zA-Z0-9_-],长度 33-100。
function runtimeSessionId(raw) {
  const base = String(raw || "web").replace(/[^a-zA-Z0-9_-]/g, "-");
  return (base + "-" + "0".repeat(40)).slice(0, 48);
}

const sse = (obj) => `data: ${JSON.stringify(obj)}\n\n`;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

async function handleAsk(req, res) {
  // —— 认证:X-Id-Token 优先(与旧前端兼容),Authorization: Bearer 兜底 ——
  let token = req.headers["x-id-token"] || "";
  if (!token) {
    const auth = req.headers["authorization"] || "";
    token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  }
  try {
    await verifier.verify(token);
  } catch (e) {
    console.error("JWT verify failed:", e?.name, "|", e?.message, "| tokenLen:", token.length);
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "unauthorized", detail: String(e?.message || e).slice(0, 200) }));
    return;
  }

  let body = {};
  try { body = JSON.parse((await readBody(req)) || "{}"); } catch { /* 走下方 empty 校验 */ }
  const question = (body.question || "").trim();

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
  });
  if (!question) {
    res.end(sse({ type: "error", message: "empty question" }));
    return;
  }

  // 心跳:距上一帧 >15s 就发 SSE 注释帧(浏览器 EventSource/手写解析器都会忽略),
  // 保住 CloudFront/ALB 的读超时。收到真实帧即重置。
  let lastWrite = Date.now();
  const heartbeat = setInterval(() => {
    if (Date.now() - lastWrite > 15000 && !res.writableEnded) {
      res.write(": ping\n\n");
      lastWrite = Date.now();
    }
  }, 5000);

  try {
    const payload = new TextEncoder().encode(JSON.stringify({
      question,
      session_id: body.session_id || null,
      deep: !!body.deep,
    }));
    const out = await agent.send(new InvokeAgentRuntimeCommand({
      agentRuntimeArn: RUNTIME_ARN,
      runtimeSessionId: runtimeSessionId(body.session_id),
      qualifier: "DEFAULT",
      contentType: "application/json",
      accept: "text/event-stream",
      payload,
    }));
    for await (const chunk of out.response) {
      res.write(chunk);
      lastWrite = Date.now();
    }
  } catch (e) {
    console.error("InvokeAgentRuntime failed:", e?.name, "|", e?.message);
    if (!res.writableEnded) res.write(sse({ type: "error", message: `${e?.name || "Error"}: ${e?.message || e}` }));
  } finally {
    clearInterval(heartbeat);
    if (!res.writableEnded) res.end();
  }
}

const server = http.createServer(async (req, res) => {
  const path = (req.url || "/").split("?")[0];
  try {
    if (req.method === "GET" && (path === "/health" || path === "/ask")) {
      // /health 给 ALB 探活;GET /ask 给前端在线探测(与 Lambda 版语义一致)。
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, runtime: !!RUNTIME_ARN }));
      return;
    }
    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
    if (req.method === "POST" && path === "/ask") { await handleAsk(req, res); return; }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
  } catch (e) {
    console.error("Unhandled:", e);
    if (!res.headersSent) res.writeHead(500);
    res.end();
  }
});

// ALB 空闲超时 120s;服务端 keepAlive 要比它长,否则 ALB 复用已被服务端关掉的连接会 502。
server.keepAliveTimeout = 125_000;
server.headersTimeout = 130_000;
server.listen(PORT, () => console.log(`ask-relay listening :${PORT} runtime=${!!RUNTIME_ARN}`));

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => { server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 3000); });
}
