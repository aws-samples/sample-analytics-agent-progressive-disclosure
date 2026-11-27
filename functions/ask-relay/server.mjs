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
//   ④ /health 上带出 Runtime 的治理身份(缓存 5 分钟,不进模型)。这一条 Lambda 版没有,
//      理由见下面「治理探针」那段:云上「以谁的身份查数」原来没有任何外部证据。

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
    // 细节只进服务端日志,不回显给客户端(异常信息可能带内部实现细节)
    console.error("JWT verify failed:", e?.name, "|", e?.message, "| tokenLen:", token.length);
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "unauthorized" }));
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
    // 只回错误类名(足够前端提示"限流/权限"类别),消息细节留在服务端日志
    console.error("InvokeAgentRuntime failed:", e?.name, "|", e?.message);
    if (!res.writableEnded) res.write(sse({ type: "error", message: e?.name || "UpstreamError" }));
  } finally {
    clearInterval(heartbeat);
    if (!res.writableEnded) res.end();
  }
}

// —— 治理探针 ——
//
// /health 原来只回 {ok, runtime}:那两个字段说明"中继活着、ARN 配着",但说不出
// **Runtime 到底以谁的身份查数**。而这件事只有一个外部证据,就是 db.backend_info()
// 的 `identity` —— 原来它只露在本地 FastAPI 的 /health 上,所以 docs/test-plan.md 的
// L4 那条断言实际验的是本地进程,云上那半边没人看得见。AGENT_ROLE_ARN 掉了的容器会
// 用 Runtime exec role 查数(没有列级边界),而它答起来和正常容器**一模一样**。
//
// 三个约束决定了下面这个形状:
//   ① ALB 拿 /health 探活,必须**永远快、永远 200**。所以绝不 await 上游:回缓存,
//      顺手在后台刷新。刷不到就是 wired:null(不知道),不是 false(没接上)——
//      把"探不到"说成"没接上"会让人去查一个不存在的治理故障。
//   ② 不能每次探活都打一次 InvokeAgentRuntime。所以 TTL 5 分钟 + 单飞(in-flight 去重)。
//   ③ /health **不带认证**(ALB 探活没法带 JWT),而完整 ARN 里有账号 ID。所以默认只回
//      `governance.role`(角色名)和 `wired`,不回 ARN。要完整 ARN 的话带上有效 JWT 打
//      `GET /health?identity=1` —— 那条路径走同一份缓存,不额外调上游。
const GOV_TTL_MS = 5 * 60 * 1000;
let gov = { at: 0, wired: null, role: "", identity: "", engine: "", workgroup: "" };
let govInFlight = null;

async function fetchGovernance() {
  const out = await agent.send(new InvokeAgentRuntimeCommand({
    agentRuntimeArn: RUNTIME_ARN,
    // 固定会话名:探针不该把 Runtime 的暖客户端按 session 反复重建
    // (main.py 的 _get_client 换 session 就 disconnect 重连)。`op: health` 那条
    // 分支根本不碰暖客户端,但会话 ID 仍然会带进 Runtime,所以钉死它。
    runtimeSessionId: runtimeSessionId("health-probe"),
    qualifier: "DEFAULT",
    contentType: "application/json",
    accept: "text/event-stream",
    payload: new TextEncoder().encode(JSON.stringify({ op: "health" })),
  }));
  let buf = "";
  for await (const chunk of out.response) buf += Buffer.from(chunk).toString("utf-8");
  // 上游是 SSE。取第一帧 type=health 的 data:,别假设整个 body 就是一个 JSON。
  for (const line of buf.split("\n")) {
    if (!line.startsWith("data:")) continue;
    let ev;
    try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
    if (ev?.type !== "health") continue;
    return {
      at: Date.now(),
      wired: !!ev.governance?.wired,
      role: ev.governance?.role || "",
      identity: ev.identity || "",
      engine: ev.engine || "",
      workgroup: ev.workgroup || "",
    };
  }
  throw new Error("上游 /health 没回 type=health 帧");
}

function refreshGovernance() {
  if (!RUNTIME_ARN || govInFlight) return;
  if (Date.now() - gov.at < GOV_TTL_MS && gov.wired !== null) return;
  govInFlight = fetchGovernance()
    .then((g) => { gov = g; })
    // 探不到就保持 wired:null。刷新失败**不影响** /health 的 200:那是探活用的,
    // 让它 5xx 会把整个 task 从 ALB 目标组里摘掉,代价远大于少一个诊断字段。
    .catch((e) => { console.error("governance probe failed:", e?.name, "|", e?.message); })
    .finally(() => { govInFlight = null; });
}

async function govPayload(req) {
  refreshGovernance();                            // 不 await:回当前缓存
  const body = { wired: gov.wired, role: gov.role, ageMs: gov.at ? Date.now() - gov.at : null };
  // 完整 ARN 带账号 ID,要认证才给。
  const q = new URL(req.url || "/", "http://x").searchParams;
  if (q.get("identity") === "1") {
    let token = req.headers["x-id-token"] || "";
    if (!token) {
      const auth = req.headers["authorization"] || "";
      token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    }
    try {
      await verifier.verify(token);
      Object.assign(body, { identity: gov.identity, engine: gov.engine, workgroup: gov.workgroup });
    } catch {
      body.identity = null;                       // 明说"没给",而不是装作没这个字段
    }
  }
  return body;
}

const server = http.createServer(async (req, res) => {
  const path = (req.url || "/").split("?")[0];
  try {
    if (req.method === "GET" && (path === "/health" || path === "/ask")) {
      // /health 给 ALB 探活;GET /ask 给前端在线探测(与 Lambda 版语义一致)。
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        ok: true, runtime: !!RUNTIME_ARN, governance: await govPayload(req),
      }));
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
server.listen(PORT, () => {
  console.log(`ask-relay listening :${PORT} runtime=${!!RUNTIME_ARN}`);
  // 起来就先探一次,别让第一个 /health 只能回 wired:null。仍然不 await——
  // 监听不该等上游,Runtime 冷启动要十几秒。
  refreshGovernance();
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => { server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 3000); });
}
