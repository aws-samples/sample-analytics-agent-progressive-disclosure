#!/usr/bin/env python3
"""shell 引用的每个本地资源,在**两套挂载布局**下都必须真的取得到。

为什么单独立一条检查:`web/index.html` 同一份文件要在两个地方跑——

    线上   S3 站点根            index.html 在 /            → `vendor/x` 解析成 /vendor/x
    本地   FastAPI StaticFiles  index.html 在 /app/        → `vendor/x` 解析成 /app/vendor/x

所以本地资源必须写**相对**路径。写成 `/vendor/x` 的话线上照旧对,本地却是 404,
而这个 404 的现象极其安静:页面照常出、解读/KPI/SQL/数据表全在,只有

    · 图表框空白(`echarts` 未定义,`renderChart` 在 setTimeout 里抛 ReferenceError,
      不影响已经同步渲染好的那些块)
    · 字体退回系统默认(Fraunces/Hanken Grotesk 全丢)

没有红字、没有报错弹窗、控制台之外看不出来。这正是这套验收要抓的那类缺陷:
**不报错、看着正常、其实少了东西**。2026-08-21 汇报前一晚才从 uvicorn 日志里
那三条 404 发现它,在此之前本地图表已经瘸了一段时间而没人知道。

例外只有一个:`/config.js`。它的语义是"站点根上那份部署期生成的产物",不跟着
shell 的挂载点走,所以**故意**保持绝对路径;本地由 server.py 回一个空脚本
(缺 `window.APP_CONFIG` → shell 回退 `/api/config`)。这里要求那条路由确实存在,
否则这个例外就成了一个没人管的 404。

用法:
    python3 scripts/ui/asset_check.py                      # 静态:相对性 + 文件在不在(无云依赖,L0)
    python3 scripts/ui/asset_check.py http://127.0.0.1:PORT  # HTTP:逐个真取一遍(L6)
"""
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHELL = os.path.join(ROOT, "web", "index.html")
WEB = os.path.join(ROOT, "web")

# 唯一允许的绝对路径:部署期产物,本地由 server.py 兜一个空脚本(见模块 docstring)
ALLOW_ABSOLUTE = {"/config.js"}
SKIP_PREFIX = ("http://", "https://", "//", "data:", "#", "mailto:", "blob:")


def refs(html: str) -> list:
    """抠出 shell 里所有本地资源引用,保留出现顺序、去重。"""
    out = []
    pats = (
        r'<(?:script|link|img|source)\b[^>]*?(?:src|href)\s*=\s*"([^"]+)"',
        r"loadScript\(\s*['\"]([^'\"]+)['\"]",
    )
    for pat in pats:
        for m in re.finditer(pat, html, re.I):
            u = m.group(1).strip()
            if u.startswith(SKIP_PREFIX) or "${" in u or not u:
                continue
            if u not in out:
                out.append(u)
    return out


def check_static(urls: list) -> list:
    errs = []
    server_py = ""
    p = os.path.join(ROOT, "backend", "server.py")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            server_py = f.read()
    for u in urls:
        path = u.split("?")[0].split("#")[0]
        if path in ALLOW_ABSOLUTE:
            # 这个例外只有在后端真的接了它的时候才成立
            if f'"{path}"' in server_py and "@app.get" in server_py:
                print(f"  ok    {u}  ← 部署期产物,本地由 server.py 兜底")
            else:
                errs.append(f"{u} 是允许的绝对路径例外,但 backend/server.py 里没有对应路由 → 本地 404")
                print(f"  ❌    {u}  后端没接这条路由")
            continue
        if path.startswith("/"):
            errs.append(f"绝对路径资源 {u}:线上对、**本地静默 404**(shell 挂在 /app 下)。改成相对路径 {path.lstrip('/')}")
            print(f"  ❌    {u}  绝对路径")
            continue
        fp = os.path.join(WEB, path.lstrip("./"))
        if os.path.isfile(fp):
            print(f"  ok    {u}  ({os.path.getsize(fp)}B)")
        else:
            errs.append(f"{u} 在 web/ 下找不到对应文件({fp})")
            print(f"  ❌    {u}  文件不存在")
    return errs


def check_http(base: str, urls: list) -> list:
    base = base.rstrip("/")
    errs = []
    for u in urls:
        # 相对路径按 shell 的真实位置解析:本地它在 /app/index.html
        target = base + u if u.startswith("/") else f"{base}/app/{u.lstrip('./')}"
        try:
            with urllib.request.urlopen(target, timeout=20) as r:
                n = len(r.read())
                if r.status == 200 and n > 0:
                    print(f"  ok    {u}  → {target}  200 {n}B")
                else:
                    errs.append(f"{u} → {target} 回了 {r.status} / {n}B")
                    print(f"  ❌    {u}  → {target}  {r.status} {n}B")
        except urllib.error.HTTPError as e:
            errs.append(f"{u} → {target} 取不到:HTTP {e.code}")
            print(f"  ❌    {u}  → {target}  HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{u} → {target} 取不到:{type(e).__name__}: {e}")
            print(f"  ❌    {u}  → {target}  {type(e).__name__}")
    return errs


def main() -> int:
    with open(SHELL, encoding="utf-8") as f:
        html = f.read()
    urls = refs(html)
    if not urls:
        print("❌ 在 web/index.html 里一个本地资源引用都没抠到——检查器已经不指向实际写法,先修检查器")
        return 1
    base = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"  shell 引用的本地资源 {len(urls)} 个" + (f",逐个真取({base})" if base else ",静态核对"))
    errs = check_http(base, urls) if base else check_static(urls)
    if errs:
        print(f"\n❌ 资源可达性检查 {len(errs)} 项不通过:")
        for e in errs:
            print(f"   · {e}")
        print("\n后果不是报错,是**静默缺件**:页面照常渲染,图表框空白、字体退回系统默认。")
        return 1
    print("  资源可达性检查 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
