"""最小全链路冒烟测试：跑一个问题，打印 agent 吐出的事件流。"""
import asyncio
import sys
from agent import run_agent


async def main():
    q = sys.argv[1] if len(sys.argv) > 1 else "最近 7 天每天的 DAU 是多少？"
    print(f"Q: {q}\n" + "=" * 60)
    async for ev in run_agent(q):
        t = ev.get("type")
        if t == "text":
            print(ev["delta"], end="", flush=True)
        elif t == "stage":
            print(f"\n[stage:{ev['key']}] {ev.get('label','')} {ev.get('detail','')}")
        elif t == "sql":
            print(f"\n[SQL]\n{ev['sql']}")
        elif t == "rows":
            print(f"\n[rows] {ev['rowcount']} 行, cols={ev['columns']}, sample={ev['rows'][:3]}")
        elif t == "result":
            print(f"\n[RESULT]\n  interpreted: {ev['interpreted']}\n  kpis: {ev['kpis']}\n  chart.type: {ev.get('chart',{}).get('type')}\n  insight: {ev['insight']}\n  followups: {ev.get('followups')}")
        elif t == "session":
            print(f"\n[session] {ev['session_id']}")
        elif t == "error":
            print(f"\n[ERROR] {ev['message']}")
        elif t == "done":
            print("\n[done]")


if __name__ == "__main__":
    asyncio.run(main())
