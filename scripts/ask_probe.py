#!/usr/bin/env python3
"""`POST /ask` 的流式契约测试：真起一轮问答，断言 SSE 事件形状和会话降级行为。

## 为什么单独有这个脚本

L6 里那几条（`/health`、`/api/catalog`、`render_test.mjs`）覆盖的是**页面加载**那一段。
`/ask` 这条流——也就是用户真正在用的那条——此前**零自动化覆盖**：`test_all.sh` 不碰它，
`eval/run_eval.py` 是直接 import `run_agent()`，不走 HTTP、不解析 SSE。于是这中间的东西
（SSE 分帧、事件键名、会话接续）只能靠人开浏览器点，而它坏掉的样子是**界面上一句红字**，
看起来像后端挂了。

所以这里断言两类事，一次问答同时验完（只烧一次 Bedrock 调用）：

1. **过期会话不许表现成故障**。`session_id` 是客户端给的，会原样进 CLI 的 `--resume`。
   CLI 侧会话被回收（标签页开久了就会）或调用方给了个非 UUID 时，CLI 启动即退出。
   实测过：整轮死在一个 `error` 事件上，用户看到「问不出来了」，而真相只是上文没了。
   `backend/agent.py::run_agent()` 现在会丢掉上文重跑一轮 —— 这条就是盯着那个降级别失效。
   注意它上一版**是有**这个承诺的：注释写着「resume 失败则退回新会话」，但 try 只包住了
   赋值，一次都没进过。所以这里不能只读代码，得真发一个坏 session_id 看结果。

2. **前端要读的键必须在**。`web/index.html` 读的是 `ev.delta`（不是 `ev.text`）、
   `ev.type==='result'`、`stage`。键名改了不会有人报错，页面只是**不再显示答案**。

## 用法

    ./backend/.venv/bin/python scripts/ask_probe.py [http://127.0.0.1:8000]

约 45–60 秒，一次 Opus 调用。默认不进 `test_all.sh`（要花钱），加 `--ask` 才跑。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

# 刻意选一道最便宜的题：单表 count，不触发深度分析后缀。这里验的是管道不是分析质量。
QUESTION = "一共有多少个用户？"

# 刻意不是 UUID。CLI 的 --resume 只接 UUID 或已存在的会话标题，所以这个值必然接不上——
# 也就必然走降级路径。用一个"看起来像 id"的值，是因为真实的过期 UUID 也只是接不上而已，
# 两者在 CLI 侧是同一种失败，而这个值不依赖任何已存在的会话，可重复。
STALE_SESSION = "ask-probe-stale-session-not-a-uuid"

TIMEOUT_S = 300


def sse_events(url: str, body: dict) -> list[dict]:
    """打 /ask，把 SSE 逐帧解析成事件列表。"""
    req = urllib.request.Request(
        url.rstrip("/") + "/ask",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    out: list[dict] = []
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # nosec B310 —— 本机 http
        for raw in resp:
            line = raw.decode("utf-8", "replace")
            if not line.startswith("data: "):
                continue
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                # 坏帧本身就是缺陷，但报在这里比静默跳过好。
                out.append({"type": "__unparsable__", "raw": line[:200]})
    return out


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    try:
        evs = sse_events(base, {"question": QUESTION, "session_id": STALE_SESSION})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"✗ 打不到 {base}/ask：{type(e).__name__}: {e}")
        return 1

    types = [e.get("type") for e in evs]
    fails: list[str] = []

    def check(ok: bool, desc: str, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {desc}" + (f" —— {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(desc)

    # —— 1. 过期会话降级 ——
    errs = [e for e in evs if e.get("type") == "error"]
    check(not errs, "过期 session_id 没有把整轮问答打死",
          f"收到 error: {errs[0].get('message', '')[:200]}" if errs else "")
    check("done" in types, "问答正常收尾（有 done 事件）", f"事件序列: {types}")

    resume_notes = [e for e in evs
                    if e.get("type") == "stage" and e.get("key") == "resume"]
    check(bool(resume_notes), "重开这件事对用户可见（发了 stage/resume）",
          "降级悄悄发生了，界面上看不出上文已丢")

    # 新会话 id 必须回传，否则前端会一直拿着那个接不上的值，每轮都要降级一次。
    sids = [e.get("session_id") for e in evs if e.get("type") == "session"]
    fresh = [s for s in sids if s and s != STALE_SESSION and _is_uuid(s)]
    check(bool(fresh), "回传了新的 UUID 会话 id（前端据此自愈）", f"收到的 session: {sids[:3]}")

    # —— 2. 前端读的键 ——
    texts = [e for e in evs if e.get("type") == "text"]
    check(bool(texts), "有 text 事件")
    check(all("delta" in e for e in texts),
          "text 事件用的键是 delta（web/index.html 读这个）",
          f"实际键: {sorted({k for e in texts for k in e})}")

    # **别改成「恰好一个」**。agent 可以多次 present_result（实测过一题发 3 个），
    # 前端是按"每个都覆盖 payload 再重渲染、最后一个生效"设计的
    # （`web/index.html:1610-1617`，`payload.source` 那行还专门保留旧来源）。
    # 所以要断言的是**最后那个**能撑起界面，写成 len==1 会把正常行为判成失败。
    results = [e for e in evs if e.get("type") == "result"]
    check(bool(results), "至少一个 result 事件", f"收到 {len(results)} 个")
    if results:
        last = results[-1]
        check(isinstance(last.get("kpis"), list) and bool(last["kpis"]),
              "最后一个 result 带 KPI 卡片（那才是界面上真正渲染的那份）",
              f"kpis={last.get('kpis')!r}")

    check(not [e for e in evs if e.get("type") == "__unparsable__"],
          "SSE 每一帧都能解析")

    print()
    if fails:
        print(f"/ask 流式契约 {len(fails)} 项失败 ✗")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("/ask 流式契约 全部通过 ✅")
    return 0


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


if __name__ == "__main__":
    sys.exit(main())
