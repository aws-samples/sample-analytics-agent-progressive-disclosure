#!/usr/bin/env python3
"""把 `backend/` 里共享的那部分代码同步到云上镜像目录 `analyticsagent/app/analytics/`。

## 为什么需要这个东西

`analyticsagent/` 是 AgentCore Runtime 的镜像目录，构建上下文就是
`app/analytics/`（`agentcore/agentcore.json` 里的 `codeLocation`），**拷不到上一级**，
所以它不能像 `backend/Dockerfile` 那样 `COPY backend/ web/ knowledge/` 了事——数据层、
工具层、指标层必须在那个目录里各有一份。

于是它变成了**手工副本**，而手工副本的失效方式不是"报错"，是"两边都看起来在正常
工作、答出来的数不一样"。迁移到湖仓时实测的漂移量：

    db.py          518 ⟷ 165 行（云上那份还是纯 psycopg，连 Athena 都不认识）
    agent.py 提示词 8696 ⟷ 6203 字符（云上还在说「PostgreSQL，35 张表」）
    metrics_def.py 324 ⟷ 177 行（云上没有 CAC/ROI 的轴内 clamp）
    stats.py       逐字相同（凑巧没人改过它）

最后一行才是重点：**它们看起来是同步的**，因为确实有几份真的一样。

所以这里照本仓库既有的范式办：**生成物 + `--check`**（同 `scripts/manifest/render.py`、
`scripts/lakehouse/gen_ddl.py`）。云上那几份是生成物，顶上有"不要手改"横幅；L0 跑
`--check`，源文件改了没同步过去就红。

## 同步面（只同步"两边必须一样"的部分）

**整份逐字拷**（内容 = 横幅 + 源文件字节，一字不改）：

| 源 | 目标 | 为什么能整份拷 |
|---|---|---|
| `backend/db.py` | `db.py` | `DB_BACKEND` / `AGENT_ROLE_ARN` 全走 env，两边形态差异不在代码里 |
| `backend/tools.py` | `tools.py` | `DOCS_ROOT` 改成读 `KNOWLEDGE_DIR`（云上冷启动从 S3 同步到那儿）后两边同字节 |
| `backend/metric_layer.py` | `metric_layer.py` | 纯函数，编译指标 SQL |
| `backend/metrics_def.py` | `metrics_def.py` | 指标定义，就该只有一份 |
| `backend/stats.py` | `stats.py` | 纯统计 |
| `scripts/lakehouse/athena.py` | `athena.py` | 云上拷不到 `scripts/`，放成 `db.py` 的同级文件（`db.py::_athena()` 两种布局都试） |

**按名字拷单个顶层节点**（`agent.py`）：两侧 `agent.py` 的**驱动是刻意不同的**——
本地是 `run_agent(question, session_id, deep)`（FastAPI 每问一个 `ClaudeSDKClient`），
云上是 `build_options()` + `stream_events(client, question, deep)`（`main.py` 持一个
暖客户端跨调用复用，省掉每次 8-10s 的 CLI 拉起）。整份对拷会把这个设计拷没了。

真正必须一致的是**提示词和共享辅助函数**：`SYSTEM` / `LITE_SUFFIX` / `DEEP_SUFFIX` /
`ALLOWED`，加上 `_engine_label`、`_metric_sig`、`_stats_summary`、`_norm_method` 这些
两边逐字相同的解析辅助。锚点 bug 就住在 `SYSTEM` 里：本地改成全局
`meta_snapshot` 锚点之后，云上那份还在教「用该表自身的 max(dt)」——同一个问题，
本地答 GMV 100.8 万，云上答 0。

**按 kwarg 比值**（`ClaudeAgentOptions`）：`max_turns` / `disallowed_tools` /
`permission_mode` 这些不在节点清单里——它们写在两侧各自的驱动里，整段拷不了。
但节点清单只保证 `DENIED_BUILTINS` 的**定义**同步，不保证它**被传进去了**：
一侧收紧了边界、另一侧漏传，节点检查照样绿。所以选项逐 kwarg 比字面值
（`ast.unparse` 归一化后），名字或值不一致都红，**新加的 kwarg 只加一侧也红**。
刻意不同的只有 `system_prompt` 和 `cli_path`，见 `OPTS_DIFFER_OK`。这几条
`--apply` 修不了（驱动不同），只能手工对齐。

## 这个检查器**不覆盖**什么（名字别比覆盖面大）

- **`knowledge/` 那棵文档树。** 云上不烤进镜像，冷启动从 S3 同步
  （`knowledge_store.sync_down()`）。改了文档要重传 S3，本脚本管不到那一步。
- **`backend/server.py` / `catalog.py` / `web/`。** 云上没有它们（HTTP 契约是 AgentCore
  的 `/invocations`，元数据面板与前端在 CloudFront + Lambda 那一侧）。
- **部署本身。** 这里只同步文件；镜像构建/推送是 `agentcore deploy`。

## 用法

    python3 scripts/deploy/sync_agent_code.py --check      # L0 用：不一致就 exit 1
    python3 scripts/deploy/sync_agent_code.py --apply      # 改完 backend/ 之后跑
    python3 scripts/deploy/sync_agent_code.py --selftest    # 检查器自己的自测（无 IO 依赖）
"""
from __future__ import annotations

import argparse
import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLOUD = ROOT / "analyticsagent" / "app" / "analytics"
SELF_REL = "scripts/deploy/sync_agent_code.py"

# 整份逐字拷：(源相对路径, 目标文件名)
COPIES: list[tuple[str, str]] = [
    ("backend/db.py", "db.py"),
    ("backend/tools.py", "tools.py"),
    ("backend/metric_layer.py", "metric_layer.py"),
    ("backend/metrics_def.py", "metrics_def.py"),
    ("backend/stats.py", "stats.py"),
    ("scripts/lakehouse/athena.py", "athena.py"),
]

# agent.py 里按名字同步的顶层节点。**不在这份清单里的两侧可以不同**，那是刻意的
# （本地 run_agent ⟷ 云上 build_options/stream_events），见模块 docstring。
AGENT_SRC = "backend/agent.py"
AGENT_DST = "agent.py"
AGENT_NODES = [
    # 提示词：锚点/方言/口径全在这里，两侧必须逐字相同
    "SYSTEM", "LITE_SUFFIX", "DEEP_SUFFIX", "ALLOWED",
    # 工具边界：两侧必须一样宽。云上那份的 build_options 是本地 _run_agent_once 之外的
    # 独立驱动，所以「怎么装进 options」两边各写一次，但**装的是什么**只有一份定义。
    "DENIED_BUILTINS", "GATE_ALLOWED",
    # 引擎名标签（写死成 "PostgreSQL" 的代价不是难看，是说谎）
    "_engine_name", "_engine_label",
    # 事件流解析辅助：两侧吐给前端的事件形状必须一样
    "_cls", "_metric_sig", "_fmt_num", "_stats_summary", "_as_text",
    "_as_list", "_as_dict", "_norm_method", "_make_gate",
]

# `ClaudeAgentOptions` 的 kwargs **不在**上面那份节点清单里，也进不去：它写在
# 两侧各自的驱动里（本地 `_run_agent_once` 的 `ClaudeAgentOptions(...)`，云上
# `build_options()` 的 `kwargs = dict(...)`），整段不能对拷。但**装的是什么**必须一样：
# `max_turns` / `disallowed_tools` / `permission_mode` 各写一份、无人比对的话，
# 一侧收紧了边界另一侧不会红——`DENIED_BUILTINS` 的定义同步了，可"有没有传进去"
# 没同步，等于同步面对着一个能绕开它的洞。所以这里逐 kwarg 比字面值。
#
# 刻意不同的只有这两个，其余任何名字/值不一致都算漂移（**包括新加的 kwarg 只加了一侧**）：
OPTS_DIFFER_OK = {
    # 本地是 `SYSTEM + (DEEP_SUFFIX|LITE_SUFFIX)`（每次调用拼）；云上暖客户端的选项是
    # 静态的，只放 `SYSTEM`，深度/常规差异走 per-turn 前缀。`SYSTEM` 本身在节点清单里。
    "system_prompt",
    # 云上镜像用系统装的 claude CLI（条件塞进 kwargs），本地用 SDK 内置那份。
    "cli_path",
}


def banner(src_rel: str) -> str:
    return (
        "# ⚠️ 本文件是**生成物**，不要手改。\n"
        f"# 由 {SELF_REL} 从 {src_rel} 逐字拷来（除本横幅）。\n"
        "# 要改请改源文件，再跑 `python3 scripts/deploy/sync_agent_code.py --apply`。\n"
        "# L0 会跑 `--check`：两侧不一致就红。\n"
    )


# ---------------------------------------------------------------- 整份拷贝

def expected_copy(src: Path, src_rel: str) -> str:
    return banner(src_rel) + src.read_text(encoding="utf-8")


def check_copy(src: Path, dst: Path, src_rel: str) -> list[str]:
    want = expected_copy(src, src_rel)
    if not dst.exists():
        return [f"{dst.name}：云上这份不存在（源是 {src_rel}）"]
    got = dst.read_text(encoding="utf-8")
    if got == want:
        return []
    # 报到行号：说"不一致"不够，得让人知道漂了多少、漂在哪
    wl, gl = want.splitlines(), got.splitlines()
    first = next((i for i in range(max(len(wl), len(gl)))
                  if (wl[i] if i < len(wl) else None) != (gl[i] if i < len(gl) else None)), 0)
    return [f"{dst.name}：与 {src_rel} 漂移了（云上 {len(gl)} 行 ⟷ 源 {len(wl) - 4} 行 + 横幅，"
            f"首个差异在第 {first + 1} 行）"]


def apply_copy(src: Path, dst: Path, src_rel: str) -> bool:
    want = expected_copy(src, src_rel)
    if dst.exists() and dst.read_text(encoding="utf-8") == want:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(want, encoding="utf-8")
    return True


# ---------------------------------------------------------------- 按名字拷节点

def _top_nodes(text: str) -> dict[str, ast.stmt]:
    """顶层 `名字 -> 节点`。只认赋值与函数定义——同步面里就这两类。"""
    out: dict[str, ast.stmt] = {}
    for n in ast.parse(text).body:
        names: list[str] = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [n.name]
        elif isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names = [n.target.id]
        for nm in names:
            out[nm] = n
    return out


def _segment(lines: list[str], node: ast.stmt) -> list[str]:
    return lines[node.lineno - 1:node.end_lineno]


def check_nodes(src: Path, dst: Path, names: list[str]) -> list[str]:
    src_text, dst_text = src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")
    src_lines, dst_lines = src_text.splitlines(), dst_text.splitlines()
    sn, dn = _top_nodes(src_text), _top_nodes(dst_text)
    bad: list[str] = []
    for name in names:
        if name not in sn:
            bad.append(f"{dst.name}：源 {src.name} 里找不到 `{name}`——同步清单过期了，先改清单")
            continue
        if name not in dn:
            bad.append(f"{dst.name}：缺 `{name}`，需人工先加一次（本脚本只替换已存在的节点，"
                       f"不猜该插在哪）")
            continue
        want, got = _segment(src_lines, sn[name]), _segment(dst_lines, dn[name])
        if want != got:
            what = "字符" if len("\n".join(want)) > 200 else "行"
            n_want = len("\n".join(want)) if what == "字符" else len(want)
            n_got = len("\n".join(got)) if what == "字符" else len(got)
            bad.append(f"{dst.name}::{name} 漂移了（云上 {n_got} {what} ⟷ 源 {n_want} {what}）")
    return bad


def apply_nodes(src: Path, dst: Path, names: list[str]) -> tuple[bool, list[str]]:
    """把源里的节点逐个覆盖进目标。返回 (是否改动, 无法处理的原因清单)。"""
    src_text, dst_text = src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")
    src_lines, dst_lines = src_text.splitlines(), dst_text.splitlines()
    sn, dn = _top_nodes(src_text), _top_nodes(dst_text)
    blocked = [b for b in check_nodes(src, dst, names)
               if "找不到" in b or "需人工先加一次" in b]
    if blocked:
        return False, blocked

    # 从后往前替换，前面的行号才不会被前一次替换的行数变化带偏
    todo = sorted((dn[n].lineno - 1, dn[n].end_lineno, _segment(src_lines, sn[n]))
                  for n in names if n in dn and n in sn)
    changed = False
    for start, end, want in reversed(todo):
        if dst_lines[start:end] != want:
            dst_lines[start:end] = want
            changed = True
    if not changed:
        return False, []
    new_text = "\n".join(dst_lines) + ("\n" if dst_text.endswith("\n") else "")
    try:
        ast.parse(new_text)          # 拷完必须还能 parse，否则宁可不落盘
    except SyntaxError as e:
        return False, [f"{dst.name}：同步后语法错（{e}），已放弃写入"]
    dst.write_text(new_text, encoding="utf-8")
    return True, []


# -------------------------------------------------- ClaudeAgentOptions 的 kwargs

def _option_kwargs(text: str, who: str) -> tuple[dict[str, str], list[str]]:
    """找到构选项的那次调用，返回 `kwarg 名 -> 值的源码`。

    认的是**带 `permission_mode` 的调用**，不是写死函数名 `ClaudeAgentOptions`：
    云上那份是 `kwargs = dict(...)` 再 `ClaudeAgentOptions(**kwargs)`，按函数名找会在
    云上那一侧静默漏检——而云上正是没人手测的那一侧。（`backend/agent.py --selftest`
    里的工具边界自测用的是同一个认法，两处别分叉。）

    值用 `ast.unparse` 归一化，比的是**表达式**不是文本，所以换行/缩进/注释的差异不算漂移。
    """
    tree = ast.parse(text)
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and any(kw.arg == "permission_mode" for kw in n.keywords)]
    if not sites:
        return {}, [f"{who}：找不到构 ClaudeAgentOptions 的调用（没有带 permission_mode 的"
                    f"调用）——要么选项没了，要么认法过期了，两种都得人看"]
    if len(sites) > 1:
        return {}, [f"{who}：有 {len(sites)} 处带 permission_mode 的调用，认不出该比哪个；"
                    f"选项应当只构一次"]
    site = sites[0]
    got = {kw.arg: ast.unparse(kw.value) for kw in site.keywords if kw.arg}
    # 云上是 `kwargs = dict(...)` 之后 `if _CLI_PATH: kwargs["cli_path"] = ...`——那一项
    # 不是 keyword，补上，否则会被当成"云上少传了一个"。只认装选项那个变量的下标赋值，
    # 别把函数里别的字典（本地的 toolmap 之类）也当成选项。
    holder = next((t.id for m in ast.walk(tree) if isinstance(m, ast.Assign)
                   and m.value is site and len(m.targets) == 1
                   and isinstance(t := m.targets[0], ast.Name)), None)
    if holder:
        for m in ast.walk(tree):
            if (isinstance(m, ast.Assign) and len(m.targets) == 1
                    and isinstance(t := m.targets[0], ast.Subscript)
                    and isinstance(t.value, ast.Name) and t.value.id == holder
                    and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                got.setdefault(t.slice.value, ast.unparse(m.value))
    return got, []


def check_options(src: Path, dst: Path) -> list[str]:
    s, bad = _option_kwargs(src.read_text(encoding="utf-8"), src.name)
    d, bad2 = _option_kwargs(dst.read_text(encoding="utf-8"), dst.name)
    bad = bad + bad2
    if bad:
        return bad
    for name in sorted(set(s) | set(d)):
        if name in OPTS_DIFFER_OK:
            continue
        if name not in d:
            bad.append(f"{dst.name}：选项少了 `{name}={s[name]}`——本地传了云上没传，"
                       f"两侧的行为/边界不一样宽")
        elif name not in s:
            bad.append(f"{dst.name}：选项多了 `{name}={d[name]}`——云上传了本地没传")
        elif s[name] != d[name]:
            bad.append(f"{dst.name}：选项 `{name}` 值不同（源 `{s[name]}` ⟷ "
                       f"云上 `{d[name]}`）")
    return bad


# ---------------------------------------------------------------- 命令

def run_check(root: Path = ROOT, cloud: Path | None = None) -> int:
    cloud = cloud or (root / "analyticsagent" / "app" / "analytics")
    bad: list[str] = []
    for src_rel, dst_name in COPIES:
        bad += check_copy(root / src_rel, cloud / dst_name, src_rel)
    agent_dst = cloud / AGENT_DST
    if not agent_dst.exists():
        bad.append(f"{AGENT_DST}：云上这份不存在")
    else:
        bad += check_nodes(root / AGENT_SRC, agent_dst, AGENT_NODES)
        opt_bad = check_options(root / AGENT_SRC, agent_dst)
        bad += opt_bad

    if bad:
        print(f"云上副本与 backend/ 漂移了 {len(bad)} 处 ❌")
        for b in bad:
            print(f"  - {b}")
        print("\n改的是 backend/ 就跑：python3 scripts/deploy/sync_agent_code.py --apply")
        print("改的是云上那份 → 那是生成物，改动会被覆盖；请把改动搬回 backend/ 再同步。")
        if opt_bad:
            print("选项 kwargs 那几条 **`--apply` 修不了**：两侧的驱动不一样，整段不能对拷，"
                  "得手工把 build_options() 的 kwargs 对齐（刻意不同的见 OPTS_DIFFER_OK）。")
        return 1
    print(f"同步面一致 ✅（{len(COPIES)} 份整拷 + agent.py 的 {len(AGENT_NODES)} 个节点逐字相同"
          f" + ClaudeAgentOptions 的 kwargs 等值）")
    return 0


def run_apply(root: Path = ROOT, cloud: Path | None = None) -> int:
    cloud = cloud or (root / "analyticsagent" / "app" / "analytics")
    touched: list[str] = []
    for src_rel, dst_name in COPIES:
        if apply_copy(root / src_rel, cloud / dst_name, src_rel):
            touched.append(f"{dst_name} ← {src_rel}")
    changed, blocked = apply_nodes(root / AGENT_SRC, cloud / AGENT_DST, AGENT_NODES)
    if changed:
        touched.append(f"{AGENT_DST} ← {AGENT_SRC} 的 {len(AGENT_NODES)} 个节点")
    if blocked:
        print("有节点没法自动同步 ❌")
        for b in blocked:
            print(f"  - {b}")
        return 1
    if touched:
        print(f"已同步 {len(touched)} 处：")
        for t in touched:
            print(f"  - {t}")
    else:
        print("本来就是一致的，没动任何文件。")
    return run_check(root, cloud)


# ---------------------------------------------------------------- 自测

_SRC_A = '''"""源文件。"""
X = "新口径"
Y = "源里有、云上还没有的新节点"


def f(a):
    """帮助函数。"""
    return a + 1
'''

_DST_A = '''"""云上那份（手工副本）。"""
X = "旧口径"


def f(a):
    return a - 1


def only_cloud():
    return "驱动层，刻意不同，不在同步面里"
'''


# 选项 kwargs 的样板：形状照真实两侧来——本地直接构 `ClaudeAgentOptions(...)`，
# 云上先 `kwargs = dict(...)` 再 `**kwargs`，且带一个条件塞进去的 cli_path。
_OPTS_SRC = '''def _run_agent_once(q):
    toolmap = {}
    toolmap["不是选项"] = "别把它当 kwarg"
    opts = ClaudeAgentOptions(
        system_prompt=SYSTEM + LITE_SUFFIX,
        allowed_tools=ALLOWED,
        disallowed_tools=DENIED_BUILTINS,
        permission_mode="bypassPermissions",
        max_turns=14,
    )
    opts.resume = None
    return opts
'''

_OPTS_DST_OK = '''def build_options():
    kwargs = dict(
        system_prompt=SYSTEM,
        allowed_tools=ALLOWED,
        # 注释和换行不算漂移：比的是表达式
        disallowed_tools=DENIED_BUILTINS,
        permission_mode="bypassPermissions",
        max_turns=14,
    )
    if _CLI_PATH:
        kwargs["cli_path"] = _CLI_PATH
    return ClaudeAgentOptions(**kwargs)
'''


def _selftest() -> int:
    """检查器自己的自测：**在缺陷面前它真的会红吗**。

    永远绿的检查器等价于没有检查器，所以这里三件事都要验：
    干净时说一致、漂了时报红、`--apply` 之后确实变一致（而且没碰不该碰的节点）。
    """
    bad = 0

    def fail(msg: str) -> None:
        nonlocal bad
        bad += 1
        print(f"  FAIL {msg}")

    with tempfile.TemporaryDirectory(prefix="synctest-") as tmp:
        tmp = Path(tmp)
        src, dst = tmp / "src.py", tmp / "dst.py"

        # 1) 整份拷：拷完一致，手改一个字符就该报漂移
        src.write_text(_SRC_A, encoding="utf-8")
        apply_copy(src, dst, "src.py")
        if check_copy(src, dst, "src.py"):
            fail("刚 apply 完就报漂移（假阳性）")
        dst.write_text(dst.read_text(encoding="utf-8").replace("新口径", "旧口径"),
                       encoding="utf-8")
        if not check_copy(src, dst, "src.py"):
            fail("云上那份被手改了，整份拷检查却说一致（假阴性）")
        # 横幅本身也在保护面里：删掉它 = 谁都不知道这是生成物
        dst.write_text(_SRC_A, encoding="utf-8")
        if not check_copy(src, dst, "src.py"):
            fail("横幅被删掉了却说一致——那样没人知道这文件是生成物")

        # 2) 按名字拷节点：只动同步面里的，云上独有的节点不许被碰
        dst.write_text(_DST_A, encoding="utf-8")
        if not check_nodes(src, dst, ["X", "f"]):
            fail("两边 X/f 明显不同，节点检查却说一致（假阴性）")
        changed, blocked = apply_nodes(src, dst, ["X", "f"])
        if blocked:
            fail(f"apply_nodes 被挡住了：{blocked}")
        if not changed:
            fail("apply_nodes 说没改动，但两边本来是不同的")
        if check_nodes(src, dst, ["X", "f"]):
            fail("apply 之后还报漂移")
        after = dst.read_text(encoding="utf-8")
        if "only_cloud" not in after or "驱动层" not in after:
            fail("同步把云上独有的节点弄丢了——那正是刻意不同的部分")
        if "帮助函数" not in after:
            fail("函数体没真的被替换（docstring 没跟过来）")
        if not after.startswith('"""云上那份'):
            fail("模块 docstring 被动了——它不在同步面里")

        # 3) 同步清单写错名字（源里没这个节点）要显式失败，不能静默跳过
        msgs = check_nodes(src, dst, ["NOT_A_NODE"])
        if not msgs or "同步清单过期" not in msgs[0]:
            fail(f"清单里写了源文件没有的名字，提示不对：{msgs}")

        # 4) 目标缺节点：报"需人工先加一次"并挡住 apply，而不是猜该插在哪
        msgs = check_nodes(src, dst, ["Y"])
        if not msgs or "需人工先加一次" not in msgs[0]:
            fail(f"目标缺节点时的提示不对：{msgs}")
        changed, blocked = apply_nodes(src, dst, ["Y"])
        if changed or not blocked:
            fail("目标缺节点时 apply 没被挡住——它会猜位置乱插")

        # 5) 选项 kwargs：干净时说一致（含刻意不同的两个 + 注释/换行差异）
        osrc, odst = tmp / "osrc.py", tmp / "odst.py"
        osrc.write_text(_OPTS_SRC, encoding="utf-8")
        odst.write_text(_OPTS_DST_OK, encoding="utf-8")
        msgs = check_options(osrc, odst)
        if msgs:
            fail(f"两侧选项本来是等值的（system_prompt/cli_path 刻意不同），却报了：{msgs}")

        # 6) 三种真实的漂法都要红：值变了 / 少传一个 / 只在一侧新加一个
        for label, text, want in [
            ("max_turns 被改成 6", _OPTS_DST_OK.replace("max_turns=14", "max_turns=6"),
             "max_turns"),
            ("disallowed_tools 整条漏传",
             _OPTS_DST_OK.replace("        disallowed_tools=DENIED_BUILTINS,\n", ""),
             "disallowed_tools"),
            ("云上偷偷多传一个 kwarg",
             _OPTS_DST_OK.replace("max_turns=14,", "max_turns=14,\n        extra_knob=True,"),
             "extra_knob"),
        ]:
            odst.write_text(text, encoding="utf-8")
            msgs = check_options(osrc, odst)
            if not any(want in m for m in msgs):
                fail(f"{label}，选项检查没报到 `{want}`（假阴性）：{msgs}")

        # 7) 认不出选项调用时必须显式失败，不能"没找到就算一致"——那正是最坏的假绿
        odst.write_text(_OPTS_DST_OK.replace('permission_mode="bypassPermissions",', ""),
                        encoding="utf-8")
        msgs = check_options(osrc, odst)
        if not msgs or "找不到" not in msgs[0]:
            fail(f"认不出选项调用时没显式失败：{msgs}")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  整份拷 3 类漂移全部报红、节点拷只动同步面、清单过期显式失败")
    print("  选项 kwargs：改值/漏传/单侧新增全部报红，认不出调用时显式失败")
    print("全部通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 backend/ 的共享代码同步到 analyticsagent/（生成物 + --check）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只核查，不一致 exit 1（L0 用）")
    g.add_argument("--apply", action="store_true", help="按 backend/ 覆盖云上那几份")
    g.add_argument("--selftest", action="store_true", help="检查器自测（不读仓库文件）")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.apply:
        return run_apply()
    return run_check()


if __name__ == "__main__":
    sys.exit(main())
