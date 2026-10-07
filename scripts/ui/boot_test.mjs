// boot() 的行为契约：探针慢 ⟹ 仍然上线；探针死 ⟹ 明确降级。不连云、不起后端。
//
// ## 为什么需要它
//
// `web/index.html` 的 `boot()` 决定整页走**真实链路**还是**离线烘焙数据**。它坏掉的样子
// 不是报错，是页面照常渲染、答案照常给出——只不过那个答案是写死的，和你问的可能不是
// 一回事（问"这段时间退了多少钱"给你 DAU 走势）。这是本项目那类缺陷最贵的一种形态。
//
// 它真的坏过一次，而且是确定性的：探针预算写死 2500ms（本地 Postgres 时代照 `SELECT 1`
// 的几十毫秒定的），换成 Athena 之后**冷启动第一发 /health 实测 5.0s**。于是"后端刚起来
// 时第一次打开页面"必然落进离线模式，界面还提示"后端未连接，请启动后端"——而后端好好
// 跑着。归因被指向了完全相反的方向。
//
// 第一次修法是把预算改成 12000ms，**紧接着又被 L6 顶爆了**：同一台机器上冷启动 ping
// 实测 12.5s（另一次 5.0s）。写死的毫秒数只是在猜云上的一个分位数。所以判据换成了三态：
// `/health` 回 `dataLayer: warming` 表示"后端在、第一发 ping 还没回来"，前端**继续等**；
// 只有 `error` 和"连不上"才算失败。场景⑥⑦守的就是这条——**"数据层还在预热"不等于
// "后端不在"**，这是整件事的要害。
//
// 然后它**第三次**以同样的面貌回来：三态和重试都上线之后，用户报的现象一个字没变。
// 这次不在阈值上——`boot()` 一旦 break 出重试循环就再也不看一眼后端，而失败额度只有
// 3 发 ≈ 3s，比 uvicorn 打开端口还短。「重启后端 → 立刻刷新」于是把页面永久锁死在离线
// 演示模式，**看起来就像修改没生效**。场景⑧⑨守的是修法：降级必须**可逆**（后台退避
// 重探 / 可见时重探 / 提问前重探）。场景⑩守的是另一种同貌不同因的情形：用 file:// 双击
// 打开时探针必然连不上（`Origin: null` 不在后端 CORS 白名单里），这时提示"启动后端"
// 是把人指向错误方向——后端很可能正跑着。
//
// 那时的覆盖面是零：
//   · `render_test.mjs` 把 `fetch` 打成必抛，并用一个**假的** boot()（直接 `MODE='live'`）
//     ——它测的是渲染，从不执行真 boot()。
//   · `test_all.sh` L6 那几条 curl **不带超时**，而且跑之前用轮询等后端就绪（= 已经热了），
//     所以永远不会跨过浏览器那个预算。
// 结论是那条工程原则的又一例：**没有跨过真实阈值的测试，对那个阈值零覆盖。**
//
// ## 做法
//
// 打一份最小 DOM 桩（同 render_test.mjs），把主 script 整块跑起来，只换 `fetch`
// （必要时连 `location` 一起换）：分别模拟「慢但可达」「完全不可达」「后端在但库不通」
// 「后端正在起（前两发失败）」「后端说自己在预热」「一直预热不完」「页面先开、后端后起」
// 「降级过的页面提问时后端已经活了」「用 file:// 双击打开」。断言最终的 MODE、右上角那个
// 徽标文案，以及底部那句话指的方向对不对。
//
// **时间统一按 SCALE 缩放**（`setTimeout` 被整体换掉）：预算、重试间隔、模拟延迟全部
// 乘同一个系数，比值不变而整个测试跑在两秒内。所以这里**不写死任何毫秒阈值**——
// 「慢」的定义是 3000ms > 旧的 2500ms 预算，谁把预算改回去，场景①就红。
//
// 用法：
//     node scripts/ui/boot_test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const html = readFileSync(join(ROOT, "web", "index.html"), "utf8");
const main = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).pop();                       // 业务脚本是最后也最大的一块

const realSetTimeout = setTimeout, realClearTimeout = clearTimeout;
// 0.05 而不是 0.1：场景⑦要陪 PROBE_WARM_ATTEMPTS(30) 发重试跑完，缩得更小才不至于
// 让这个离线测试跑成十几秒。比值不变，所以断言的含义一点没变。
const SCALE = 0.05;                            // 时间统一缩放，比值不变
const scaled = ms => Math.max(1, Math.round((ms || 0) * SCALE));
const tick = ms => new Promise(r => realSetTimeout(r, scaled(ms)));
/** 轮询等某个条件成立（同样按 SCALE 缩放），超时返回 false。场景⑧等的是"自愈"。 */
async function waitFor(pred, budgetMs) {
  const deadline = Date.now() + scaled(budgetMs);
  while (Date.now() < deadline) {
    if (pred()) return true;
    await tick(200);
  }
  return pred();
}

// 后端真活着时 /health 的样子（字段名按 backend/server.py 的 /health）
const HEALTH_BODY = {
  ok: true, dataLayer: "ok", model: "global.anthropic.claude-opus-4-8", bedrock: true,
  region: "us-west-2", authEnabled: false,
  db: { engine: "Athena + S3 Tables (Iceberg)", name: "app_analytics" },
};

// ---------------------------------------------------------------- DOM 桩

function mkEl(tag = "div", sel = "?") {
  const el = {
    tagName: tag, _sel: sel, dataset: {}, style: {}, hidden: false,
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    getAttribute: () => null, setAttribute() {}, removeAttribute() {},
    appendChild(c) { (this.children ||= []).push(c); return c; },
    removeChild() {}, remove() {}, scrollIntoView() {},
    addEventListener() {}, querySelectorAll: () => [],
    // 返回一个新桩而不是 null：`newTurn()` 会去拿气泡内部的子节点挂事件，返回 null
    // 就在那儿炸掉，于是"提问走了哪条路"这件事根本测不到。
    querySelector: s => mkEl("div", sel + " >> " + s),
    closest: () => null, focus() {}, blur() {}, click() {}, insertAdjacentHTML() {},
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
  };
  let _html = "", _text = "";
  Object.defineProperty(el, "innerHTML", { get: () => _html, set(v) { _html = String(v); } });
  Object.defineProperty(el, "textContent", { get: () => _text, set(v) { _text = String(v); } });
  return el;
}

/** 起一个独立实例：只换 fetch，其余桩每次重建，场景之间不串味。 */
function instantiate(fetchImpl, loc) {
  const registry = new Map();
  const q = sel => {
    if (!registry.has(sel)) {
      const el = mkEl("div", sel);
      if (sel.startsWith("#")) el.id = sel.slice(1);
      registry.set(sel, el);
    }
    return registry.get(sel);
  };
  globalThis.document = {
    querySelector: q, getElementById: id => q("#" + id), querySelectorAll: () => [],
    createElement: t => mkEl(t, "created:" + t), documentElement: mkEl("html", "html"),
    addEventListener() {}, body: mkEl("body", "body"), title: "",
  };
  globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false }) };
  globalThis.location = loc || { protocol: "http:", origin: "http://127.0.0.1:8000" };
  globalThis.localStorage = { getItem: () => "zh", setItem() {} };
  globalThis.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {} }),
                         graphic: { LinearGradient: function () {} } };
  // 时间整体缩放。clearTimeout 用真的：桩返回的就是真 handle。
  globalThis.setTimeout = (fn, ms) => realSetTimeout(fn, scaled(ms));
  globalThis.clearTimeout = realClearTimeout;
  globalThis.AbortController = AbortController;         // 用 Node 真的，abort 要真的生效
  globalThis.fetch = fetchImpl;
  new Function(main + `
    ;globalThis.__B={done:()=>BOOT,mode:()=>MODE,ask,
      badge:()=>document.querySelector('#modelLabel').textContent,
      foot:()=>document.querySelector('#foot').innerHTML};`)();
  return globalThis.__B;
}

// ---------------------------------------------------------------- fetch 桩

/** 可被 AbortSignal 打断的等待——探针的超时预算全靠这条路径才测得到。 */
function abortable(ms, signal) {
  return new Promise((res, rej) => {
    const id = realSetTimeout(res, scaled(ms));
    if (signal) signal.addEventListener("abort", () => {
      realClearTimeout(id);
      const e = new Error("aborted"); e.name = "AbortError"; rej(e);
    });
  });
}

/**
 * @param plan 每次 /health 的行为，按顺序取；用尽后重复最后一项。
 *             {delay, body} → 延迟后返回 body；{delay, dead:true} → 延迟后连接失败
 * @param calls 外部数组，记录被打过的 URL
 */
function mkFetch(plan, calls) {
  let n = 0;
  return async (url, opt = {}) => {
    const u = String(url);
    calls.push(u);
    if (u.includes("/health")) {
      const step = plan[Math.min(n++, plan.length - 1)];
      await abortable(step.delay, opt.signal);
      if (step.dead) throw new TypeError("Failed to fetch");
      return { ok: true, status: 200, json: async () => step.body };
    }
    if (u.includes("/api/catalog") || u.includes("catalog.json")) {
      return { ok: false, status: 404, json: async () => ({}) };  // loadCatalog 自带兜底
    }
    // /ask：不模拟 SSE，只要记下"被打过"就够——本测试关心的是**分派到了哪条路**
    throw new TypeError("stubbed: " + u);
  };
}

// ---------------------------------------------------------------- 场景

const fails = [];
const must = (c, m) => { if (!c) fails.push(m); };

// ① 后端活着、但 /health 慢。3000ms 是刻意的：> 旧的 2500ms 预算，< 现在的预算。
//    冷启动实测 5.0s，比这更慢——所以这条过了不等于宽裕，只等于"没退回原来那个值"。
{
  const calls = [];
  const B = instantiate(mkFetch([{ delay: 3000, body: HEALTH_BODY }], calls));
  await B.done();
  must(B.mode() === "live",
       `① /health 慢(3000ms)但可达 ⟹ 应上线，实际 MODE=${B.mode()}` +
       "（预算被改小了？这就是「后端起着却显示离线演示」那个缺陷）");
  must(!/离线|Offline/.test(B.badge()), `① 徽标不该是离线：${B.badge()}`);
  must(/Bedrock/.test(B.badge()), `① 徽标应显示模型：${B.badge()}`);
  console.log(`  ① 慢探针(3000ms > 旧的 2500 预算)  MODE=${B.mode()}  徽标「${B.badge()}」`);
}

// ② 完全不可达（后端真没起）⟹ 必须明确降级，并且徽标说得出"离线"。
//    这条守的是反向：把降级路径删了、或者失败时假装上线，同样是骗人。
{
  const calls = [];
  const B = instantiate(mkFetch([{ delay: 5, dead: true }], calls));
  await B.done();
  must(B.mode() === "baked", `② 探不通 ⟹ 应降级，实际 MODE=${B.mode()}`);
  must(/离线|Offline/.test(B.badge()), `② 徽标应标明离线：${B.badge()}`);
  must(calls.filter(u => u.includes("/health")).length >= 2,
       `② 应重试（后端可能正在起），实际只探了 ${calls.filter(u => u.includes("/health")).length} 发`);
  console.log(`  ② 不可达  MODE=${B.mode()}  徽标「${B.badge()}」  探了 ${calls.filter(u => u.includes("/health")).length} 发`);
}

// ③ 后端在、库不通（/health 回 ok:false）⟹ 也算不活。
//    不能只看 HTTP 200：那样 Athena 挂了页面还会说"实时"，然后 /ask 一问一个红字。
{
  const calls = [];
  const B = instantiate(mkFetch(
    [{ delay: 5, body: { ...HEALTH_BODY, ok: false, dataLayer: "error" } }], calls));
  await B.done();
  must(B.mode() === "baked", `③ /health ok:false ⟹ 应降级，实际 MODE=${B.mode()}`);
  console.log(`  ③ ok:false(dataLayer=error)  MODE=${B.mode()}`);
}

// ④ 后端正在起：前两发失败，第三发成功 ⟹ 应上线。
//    真实场景就是这个——uvicorn 打印端口早于数据层预热完成，用户已经在刷页面了。
{
  const calls = [];
  const B = instantiate(mkFetch(
    [{ delay: 5, dead: true }, { delay: 5, dead: true }, { delay: 100, body: HEALTH_BODY }], calls));
  await B.done();
  must(B.mode() === "live", `④ 第三发才通 ⟹ 应上线，实际 MODE=${B.mode()}`);
  console.log(`  ④ 前两发失败、第三发通  MODE=${B.mode()}`);
}

// ⑤ 探针还没跑完就提问 ⟹ 必须等探针，不能就地拿烘焙数据答。
//    这是那次事故里最贵的一环：用户打开页面立刻点了预置问题，拿到一个写死的答案。
{
  const calls = [];
  const B = instantiate(mkFetch([{ delay: 3000, body: HEALTH_BODY }], calls));
  await tick(50);                                    // 探针还在飞
  must(B.mode() !== "live", "⑤ 探针未完成时 MODE 不该已经是 live（那说明没真探）");
  // /ask 的桩必抛（不模拟 SSE），这里只关心**分派到了哪条路**，抛在哪一步无所谓
  try { await B.ask("这段时间一共退了多少钱"); } catch (e) { /* 见上 */ }
  must(calls.some(u => u.includes("/ask")),
       "⑤ 探针期间提问应等探针完成再走真实链路，实际没打到 /ask（= 答了烘焙数据）");
  console.log(`  ⑤ 探针期间提问  打到的端点 ${[...new Set(calls.map(u => u.replace(/^https?:\/\/[^/]+/, "")))].join(" ")}`);
}

// ⑥ 后端明说"我在，数据层还在预热"（ok:false + dataLayer:'warming'）⟹ **必须继续等**。
//    这是第二次翻车的正解：预热要多久由 Athena 决定（实测 5.0s ～ 13.5s），页面不能拿一个
//    写死的毫秒数去裁决"后端在不在"。warming 的发数刻意超过 PROBE_FAIL_ATTEMPTS(3)：
//    谁把 warming 混进失败计数里，这条就红。
{
  const calls = [];
  const WARM = { ...HEALTH_BODY, ok: false, dataLayer: "warming" };
  const B = instantiate(mkFetch(
    [{ delay: 5, body: WARM }, { delay: 5, body: WARM }, { delay: 5, body: WARM },
     { delay: 5, body: WARM }, { delay: 5, body: WARM }, { delay: 5, body: HEALTH_BODY }], calls));
  await B.done();
  must(B.mode() === "live",
       `⑥ 连续 5 发 warming 后探通 ⟹ 应上线，实际 MODE=${B.mode()}` +
       "（warming 被当成了失败？那正是「后端起着却显示离线演示」）");
  must(!/离线|Offline/.test(B.badge()), `⑥ 徽标不该是离线：${B.badge()}`);
  console.log(`  ⑥ warming×5 后探通  MODE=${B.mode()}  探了 ${calls.filter(u => u.includes("/health")).length} 发`);
}

// ⑦ 一直 warming（后端卡在预热上）⟹ 也必须**有限**地放弃并降级。
//    没有这条，"继续等"就会变成永远转圈：界面停在「连接中…」，用户以为页面卡死了。
//    转圈也是一种不说实话——只不过骗的是"再等等就好了"。
{
  const calls = [];
  const B = instantiate(mkFetch(
    [{ delay: 5, body: { ...HEALTH_BODY, ok: false, dataLayer: "warming" } }], calls));
  await B.done();
  must(B.mode() === "baked", `⑦ 一直 warming ⟹ 应有限放弃并降级，实际 MODE=${B.mode()}`);
  must(/离线|Offline/.test(B.badge()), `⑦ 徽标应标明离线：${B.badge()}`);
  const n = calls.filter(u => u.includes("/health")).length;
  must(n > 3, `⑦ 放弃前应比"失败"路径等得久得多，实际只探了 ${n} 发`);
  console.log(`  ⑦ 一直 warming  MODE=${B.mode()}  探了 ${n} 发才放弃`);
}

// ⑧ **页面先开、后端后起** ⟹ 页面必须自己回到实时，不能一降级就锁死。
//    这条是真实事故的第二幕，也是"改完了怎么还一样"的答案：失败额度只有
//    PROBE_FAIL_ATTEMPTS(3) 发 ≈ 3s，而 uvicorn 打开端口就要一两秒。用户重启后端后
//    立刻刷新（最自然的动作）三发全是 connection refused，页面就永久停在离线演示模式，
//    之后后端热了也没人再问——从用户角度看，"重启 + 刷新"这个万能操作失效了。
{
  const calls = [];
  const plan = [];
  for (let i = 0; i < 10; i++) plan.push({ delay: 5, dead: true });  // 前 10 发：后端没起
  plan.push({ delay: 5, body: HEALTH_BODY });                        // 之后：起来了
  const B = instantiate(mkFetch(plan, calls));
  await B.done();
  must(B.mode() === "baked", `⑧ 前 10 发不可达 ⟹ boot() 应先降级，实际 MODE=${B.mode()}`);
  // 现在后端活了。页面自己得看见——这里等的是"自愈"，不是又一次刷新。
  const healed = await waitFor(() => B.mode() === "live", 60000);
  must(healed, `⑧ 后端起来之后页面应自愈回实时，实际一直是 MODE=${B.mode()}` +
               "（降级后不再探后端？那用户只能靠刷新，而刷新可能又赶在端口打开之前）");
  must(!/离线|Offline/.test(B.badge()), `⑧ 自愈后徽标不该还写离线：${B.badge()}`);
  console.log(`  ⑧ 页面先开、后端后起  自愈后 MODE=${B.mode()}  徽标「${B.badge()}」  共探 ${calls.filter(u => u.includes("/health")).length} 发`);
}

// ⑨ 降级之后提问，而后端此时已经活了 ⟹ **必须**先探一发再决定走哪条路。
//    这是整件事最贵的一环：没有这一发，用户拿到的是一个"看起来正常、其实和问题无关"的
//    烘焙答案（问退款给 DAU 走势）。紧接着 done() 提问，背景重探的第一发还在 sleep，
//    所以这里打出去的那发 /health 只能来自 ask()。
{
  const calls = [];
  const B = instantiate(mkFetch(
    [{ delay: 5, dead: true }, { delay: 5, dead: true }, { delay: 5, dead: true },
     { delay: 5, body: HEALTH_BODY }], calls));
  await B.done();
  must(B.mode() === "baked", `⑨ 前置条件：应先降级，实际 MODE=${B.mode()}`);
  const before = calls.filter(u => u.includes("/health")).length;
  try { await B.ask("这段时间一共退了多少钱"); } catch (e) { /* /ask 桩必抛 */ }
  must(calls.filter(u => u.includes("/health")).length > before,
       "⑨ 降级后提问应先重探 /health，实际没探");
  must(calls.some(u => u.includes("/ask")),
       "⑨ 后端已经活了，提问必须走真实链路，实际没打到 /ask（= 给了烘焙答案）");
  must(B.mode() === "live", `⑨ 重探通过后应切到实时，实际 MODE=${B.mode()}`);
  console.log(`  ⑨ 降级后提问(后端已活)  MODE=${B.mode()}  打到的端点 ${[...new Set(calls.map(u => u.replace(/^https?:\/\/[^/]+/, "")))].join(" ")}`);
}

// ⑩ 用 file:// 双击打开 ⟹ 降级是对的，但**不许**提示"启动后端"。
//    这时探针必然连不上（跨源请求带 `Origin: null`，后端 CORS 白名单里没有它），
//    而后端很可能正跑着。两种情形的现象一字不差（右上角离线演示 + 烘焙答案），
//    所以底部那句话是唯一能把它们分开的东西——它指错方向就会白花一轮排查。
{
  const calls = [];
  const B = instantiate(mkFetch([{ delay: 5, dead: true }], calls),
                        { protocol: "file:", origin: "null" });
  await B.done();
  must(B.mode() === "baked", `⑩ file:// 打开 ⟹ 应降级，实际 MODE=${B.mode()}`);
  must(/file:\/\//.test(B.foot()),
       `⑩ 底部应说明是 file:// 打不到后端，实际「${B.foot()}」`);
  must(!/run\.sh/.test(B.foot()),
       `⑩ 底部不该提示"启动后端"（后端可能正跑着，指错方向），实际「${B.foot()}」`);
  console.log(`  ⑩ file:// 打开  MODE=${B.mode()}  底部「${B.foot().replace(/<[^>]+>/g, "")}」`);
}

if (fails.length) {
  console.log(`\n✗ ${fails.length} 项断言失败：`);
  fails.forEach(f => console.log("   - " + f));
  process.exit(1);
}
console.log("\nboot() 行为契约 全部通过 ✅");
