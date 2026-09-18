#!/usr/bin/env python3
r"""L8 负测：证明检查器该报的时候真会报（缺陷注入 → 断言变红 → 撤销）。

## 为什么这一层不能省

L0–L6 全绿只说明「现在没问题」，**不说明检查器还有效**。一个永远绿的检查器和
没有检查器等价，而且更糟——它让人以为这块有人看着。本项目已经在这上面栽过：

- 两次**假阳性**：卡片的枚举行被当成列名、`IS NOT NULL` 里的 `is` 被当成列名。
  这类会自己暴露（测试红了、有人来看）。
- 一次**名字比覆盖面大**：`scripts/manifest/render.py --check` 只校验 manifest 合法，
  **从不比对生成物**，而 `test_all.sh` 把它标成「派生层知识卡片是最新渲染」。
  手改一张生成的卡片，它照样打印 `manifest OK` 并 exit 0。这条是写本脚本时抓出来的，
  已修（`--check` 现在逐字比对 15 个产物）。
- 一处**零覆盖**：`backend/db.py` 的只读闸（在 L4 治理层建起来之前，那是唯一生效的
  安全边界），而整套测试里没有一条断言碰过它。把 `_FORBIDDEN` 改松不会让任何测试变红。
  已修（`python3 backend/db.py` 是它的自测，进 L0）。
- L4 治理层带着这三种形态一起来的：它的三个用例（`gov-*`）分别盯"策略被改松"
  （离线就该红）、"探针其实没在探"（`--as-caller` 全绿＝零覆盖）、"治理建好了但
  后端没接上"（授权面全对，查数用的却是 admin 凭证）。

后两条都不是「检查器写错了」，是**没人验证过检查器**。这就是 L8 的职责。

## 每个用例做三件事，不是一件

    1. 先跑一遍未注入的命令，要求 exit 0        ← 否则"变红"可能与注入无关
    2. 注入缺陷，要求 exit 非 0 且输出匹配预期消息  ← 要求消息匹配，不只看退出码：
                                                    换个原因红了不算这个用例过
    3. 无论成败都还原，并用 sha256 核对还原成功

第 1 步是关键。少了它，一个本来就红的检查器会让所有负测"通过"——负测自己变成
那种最坏的测试：绿着，但什么也没证明。`--fast` 可以跳过它，代价写在下面。

## 安全性

- 只改仓库内文件，改前整份复制到临时目录，`finally` + 信号处理里还原，
  再用 sha256 核对字节一致。**不用 `git checkout` 还原**——工作树里有大量未提交改动，
  checkout 会一起冲掉（v2 时代踩过）。
- 注入用的是「唯一子串替换」：锚点在文件里必须**恰好出现一次**，否则用例直接 ERROR。
  这样文件被改动导致锚点漂移时，负测是显式失败，而不是静默改了别的地方。
- `data/csv/` 也在注入范围里（要验装载对账）。那是仓库自带的种子数据、是数据的真源，
  所以还原后的 sha256 校验对它尤其重要：这里出错等于污染真源。

## 用法

    python3 scripts/negative_tests.py --list        # 看用例清单
    python3 scripts/negative_tests.py --offline     # 只跑不连云的 32 个（秒级）
    python3 scripts/negative_tests.py               # 全部 49 个（要 AWS 凭证，约 4 分钟）
    python3 scripts/negative_tests.py -c enum-value-absent -c doc-sql-rotten

退出码非 0 = 有检查器在缺陷面前保持了沉默（或负测自己的锚点失效了），两种都要处理。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "backend" / ".venv" / "bin" / "python")
if not os.access(PY, os.X_OK):
    PY = sys.executable

# 不存在的目录/桶，用于「目录不可用」那一类。账号号段全 0 不可能是真账号。
BOGUS_CATALOG = "000000000000:s3tablescatalog/no-such-table-bucket"


@dataclass
class Case:
    id: str
    cloud: bool
    guards: str                       # 这个用例守的是哪个检查器
    defect: str                       # 注入模拟的真实缺陷（不是"改个字符"）
    patches: list[tuple[str, str, str]]   # (相对路径, 原文, 替换成)
    cmd: list[str]                    # 注入后要跑的命令
    expect: str                       # 输出必须匹配的正则
    baseline: list[str] | None = None  # 未注入时跑什么（None = 跳过第 1 步）
    forbid: str = ""                  # 输出不得匹配的正则（防假阳性回归）
    delete: list[str] = field(default_factory=list)   # 整份删掉的文件（一样会还原）
    requires: str = ""                # 需要的外部命令（缺了就**明说跳过**，不假红也不静默少跑）

    def touched(self) -> list[str]:
        return sorted({rel for rel, _, _ in self.patches} | set(self.delete))

    def baseline_cmd(self) -> list[str] | None:
        if self.baseline is not None:
            return self.baseline
        return self.cmd if self.touched() else None


CASES: list[Case] = [
    # ---------------------------------------------------------- 不连云（秒级）
    Case(
        id="readonly-guard-hole",
        cloud=False,
        guards="backend/db.py 的 validate()（只读边界自测）",
        defect="有人放宽了禁写关键字清单——L4 只管「不许看」，"
               "「不许写」全靠这一道",
        # 注入必须打在**只有 `_FORBIDDEN` 拦得住**的缺口上：拿掉 `delete` 没用，
        # `DELETE FROM …` 会先被「必须以 select/with 开头」那条拦下，负测就会
        # 因为另一条断言（已知过度拒绝）变红——那不算这个用例过。拿掉 `drop`
        # 才命中真正的第二道闸：以 SELECT 开头、后面不带分号跟一个写动作。
        patches=[("backend/db.py", r"insert|update|delete|drop|alter",
                  r"insert|update|delete|alter")],
        cmd=[PY, "backend/db.py"],
        expect=r"该拒的没拒（不带分号的尾随写动作）",
    ),
    Case(
        id="tool-gate-shadowed",
        cloud=False,
        guards="backend/agent.py --selftest（工具白名单边界）",
        defect="闸门退回 can_use_tool 接法——它被 permission_mode='bypassPermissions' "
               "架空，回调一次都不会触发，agent 又能走 Bash/Read 绕过 run_sql",
        # 这不是"改个字符"：这一行**就是**当初那个真实缺陷的样子，原样存在过很久。
        patches=[("backend/agent.py",
                  'hooks={"PreToolUse": [HookMatcher(hooks=[_make_gate()])]},',
                  "can_use_tool=_make_gate(),")],
        cmd=[PY, "backend/agent.py", "--selftest"],
        # 点名到**哪一侧**：注入只打本地那份，所以红的必须是 backend/agent.py 这一条。
        # 原来的 `can_use_tool|架空` 里 `can_use_tool` 连自测的绿输出都能匹配上
        # （"两侧选项都走 hooks 且无 can_use_tool"），只是被 exit 0 那道判定挡住而已。
        expect=r"backend/agent\.py：选项里还挂着 can_use_tool",
        forbid=r"全部通过",
    ),
    Case(
        id="tool-gate-toolsearch-locked",
        cloud=False,
        guards="backend/agent.py --selftest（白名单里的 ToolSearch）",
        defect="白名单收得太紧，把 ToolSearch 也挡了——MCP 工具是延迟加载的，"
               "于是 agent 连自己的 read_doc 都调不出来。这条不盯的话要等 L7 跑 23 分钟才红",
        patches=[("backend/agent.py",
                  'GATE_ALLOWED = set(ALLOWED) | {"ToolSearch"}',
                  "GATE_ALLOWED = set(ALLOWED)")],
        cmd=[PY, "backend/agent.py", "--selftest"],
        expect=r"GATE_ALLOWED 里没有 ToolSearch",
        forbid=r"全部通过",
    ),
    Case(
        id="iceberg-ddl-handedit",
        cloud=False,
        guards="scripts/lakehouse/gen_ddl.py --check",
        defect="有人手改了生成的 Iceberg DDL，而不是改 v1 真源再重新生成",
        patches=[("database/iceberg/01_tables.sql",
                  "registration_source", "registration_src")],
        cmd=[PY, "scripts/lakehouse/gen_ddl.py", "--check"],
        expect=r"与真源漂移了",
    ),
    # 这条守的缺陷本项目真发生过，而且是 13 个生成器枚举缺陷的成因：DDL 行内注释里的
    # 枚举清单和知识卡片漂开，没有任何一层比过。注入方式刻意用 on_sale → active——
    # 那正是 products.status 上原本写着的错值，照它写 WHERE 得到 0 行且不报错。
    Case(
        id="ddl-enum-comment-drift",
        cloud=False,
        guards="scripts/lakehouse/verify_ddl_comments.py",
        defect="DDL 行内注释里的枚举取值与知识卡片漂开（照注释写 WHERE 得到静默空集）",
        patches=[("database/08_product_domain.sql",
                  "-- 'on_sale', 'off_sale'", "-- 'active', 'off_sale'")],
        cmd=[PY, "scripts/lakehouse/verify_ddl_comments.py"],
        expect=r"1 行不一致",
        forbid=r"全部一致",
    ),
    # 上一条能红，不等于「合法但没有」的去引号约定还活着：把括号散文里的值加上引号，
    # 对账就该立刻红。没有这条，将来有人给散文加回引号、注释重新开始说谎，而闸门沉默。
    Case(
        id="ddl-enum-prose-requoted",
        cloud=False,
        guards="scripts/lakehouse/verify_ddl_comments.py",
        defect="有人给「业务上合法但本批数据没有」的值加回了引号，注释重新变成能抄进 SQL 的假清单",
        patches=[("database/03_attribution_domain.sql",
                  "（业务上还有 draft，", "（业务上还有 'draft'，")],
        cmd=[PY, "scripts/lakehouse/verify_ddl_comments.py"],
        expect=r"1 行不一致",
        forbid=r"全部一致",
    ),
    Case(
        id="mart-predicate-dropped",
        cloud=False,
        guards="scripts/lakehouse/verify_mart_parity.py（谓词/列数对账）",
        defect="集市层某条 CTAS 少了一个状态过滤——GMV 会悄悄多算取消单",
        patches=[("database/iceberg/02_mart.sql",
                  "WHERE status IN ('paid','shipped','delivered');",
                  "WHERE status IN ('paid','shipped');")],
        cmd=[PY, "scripts/lakehouse/verify_mart_parity.py"],
        # 认到「哪张表、丢了几条、丢的是哪一条」。原来写的是 `谓词|不对齐|缺|❌`——
        # 那个正则几乎空匹配：这脚本任何一种红（缺文件、列数不对、表对不上）都带 ❌，
        # 换个原因红了也算过，而这条用例要证的偏偏是**丢谓词能被抓到**。
        expect=r"dwd_orders_valid\s+丢了 1 条谓词[\s\S]*delivered",
        forbid=r"谓词与列数全部对齐",
    ),
    Case(
        id="manifest-artifact-handedit",
        cloud=False,
        guards="scripts/manifest/render.py --check",
        defect="有人手改了生成的派生层卡片，下次 render 会把它悄悄覆盖掉",
        patches=[("knowledge/domains/user/dws_user_daily.md",
                  "| event_cnt | BIGINT | 当天事件数 |",
                  "| event_cnt | BIGINT | 当天事件数（手改注入） |")],
        cmd=[PY, "scripts/manifest/render.py", "--check"],
        expect=r"产物漂移",
    ),
    Case(
        id="cloud-copy-drift",
        cloud=False,
        guards="scripts/deploy/sync_agent_code.py --check",
        defect="有人直接改了 analyticsagent/ 里那份生成的 agent.py 提示词，"
               "backend/ 那份没动——两边又开始分叉了",
        # 注入的是**那个真实发生过的分叉**：云上副本被改回「用该表自己的 max(时间列)
        # 当今天」。这不是随便挑一行——SYSTEM 提示词里就这一句的对错决定了
        # 「近 30 天各渠道 GMV」是 100.8 万还是 0，而两边分叉时本地全绿、
        # 云上答 0，整套测试当时一条都没碰过 analyticsagent/。
        #
        # 打在 agent.py 上是有意的：它走的是 check_nodes（AST 按节点比）这条
        # 更容易写错的路径。六份整拷走的 check_copy 由 --selftest 覆盖，
        # 那里连"横幅被删"也一起验了。
        patches=[("analyticsagent/app/analytics/agent.py",
                  "**也禁止拿该表自己的 `max(时间列)` 当今天**（理由见下表，比查空更危险）",
                  "**可以拿该表自己的 `max(时间列)` 当今天**（省事）")],
        cmd=[PY, "scripts/deploy/sync_agent_code.py", "--check"],
        # 必须认到**是 SYSTEM 这个节点漂了**。原来只认「漂移」二字，可这脚本报任何
        # 一处不一致（哪份整拷、哪个节点）都印这两个字，等于只验了"它会红"。
        expect=r"agent\.py::SYSTEM 漂移",
        forbid=r"同步面一致",
    ),
    Case(
        id="csv-header-renamed",
        cloud=False,
        guards="scripts/lakehouse/load.py --preflight",
        defect="CSV 表头与 DDL 列名不一致——灌进去会整列错位且不报错",
        patches=[("data/csv/channels.csv", "channel_type,platform",
                  "channel_kind,platform")],
        cmd=[PY, "scripts/lakehouse/load.py", "--preflight"],
        # 认到「哪张表、多了什么、缺了什么」。原来写的是 `channel|表头|列`——
        # `channel` 在这脚本的正常输出里就有（它逐表打印），基本等于永真。
        expect=r"channels: CSV 表头与 DDL 不一致.*channel_kind.*channel_type",
        forbid=r"表头与列序一致",
    ),
    Case(
        id="gov-policy-loosened",
        cloud=False,
        guards="scripts/lakehouse/governance.py --selftest（策略 ⟷ 验收契约）",
        defect="有人把一个 PII 列从治理策略里拿掉了——云上照发照过，"
               "而 agent 从此能读到明文 email",
        # 这个用例存在的理由就是 governance.py 里那两份清单为什么要分开：
        # 验收契约要是从 EXCLUDE_COLUMNS 现算，这里注入完自测照样全绿。
        patches=[("scripts/lakehouse/governance.py",
                  '"users": ("email", "phone"),', '"users": ("phone",),')],
        cmd=[PY, "scripts/lakehouse/governance.py", "--selftest"],
        expect=r"策略与契约不一致",
    ),
    Case(
        id="probe-budget-too-tight",
        cloud=False,
        requires="node",
        guards="scripts/ui/boot_test.mjs（boot() 行为契约）",
        defect="前端存活探针的超时预算被改回本地 Postgres 时代的 2500ms——而湖仓冷启动"
               "第一发 /health 实测 5.0s，于是**后端刚起来时第一次打开页面必然落进离线"
               "演示模式**：答案换成写死的烘焙数据（问退款给 DAU 走势），底部还提示"
               "「后端未连接，请启动后端」，而后端好好跑着",
        # 这个数就是真实事故的那个值。注入它必须让 boot_test 的场景①（慢探针仍应上线）
        # 变红——那条断言里 3000ms 的模拟延迟正是为了卡在 2500 和现值之间。
        patches=[("web/index.html",
                  "PROBE_TIMEOUT_MS=8000", "PROBE_TIMEOUT_MS=2500")],
        cmd=["node", "scripts/ui/boot_test.mjs"],
        expect=r"应上线，实际 MODE=baked",
    ),
    Case(
        id="probe-warming-treated-as-dead",
        cloud=False,
        requires="node",
        guards="scripts/ui/boot_test.mjs（boot() 行为契约，场景⑥）",
        defect="后端明说「我在，数据层还在预热」（/health 回 dataLayer=warming），前端却把它"
               "算进「失败」的额度——于是预热稍慢一点，页面照旧静默落进离线演示模式。"
               "这是同一个缺陷的第二种走法：第一次是超时预算猜小了，这次是**把「还在预热」"
               "读成了「后端不在」**",
        # 只把 warming 那一支的计数器换成 fails，别的一个字不动：这正是"看起来只是少写了
        # 一个分支"的那种改动，而它的后果和写死 2500ms 完全一样。
        patches=[("web/index.html",
                  "if(r.state==='warming'){ if(++warms>=PROBE_WARM_ATTEMPTS) break; }",
                  "if(r.state==='warming'){ if(++fails>=PROBE_FAIL_ATTEMPTS) break; }")],
        cmd=["node", "scripts/ui/boot_test.mjs"],
        expect=r"warming 后探通 ⟹ 应上线",
    ),
    Case(
        id="probe-degrade-is-permanent",
        cloud=False,
        requires="node",
        guards="scripts/ui/boot_test.mjs（boot() 行为契约，场景⑧）",
        defect="降级之后这一页再也不看一眼后端。失败额度只有 3 发 ≈ 3s，比 uvicorn 打开端口"
               "还短，所以「重启后端 → 立刻刷新」这个最自然的动作会把页面永久锁在离线演示"
               "模式。表现出来是「重启也刷新了，还是跟之前一模一样」，归因于是指向"
               "「改的东西没生效」——这是同一个缺陷的第三种走法，也是最难归因的一种",
        patches=[("web/index.html",
                  "  recheckLoop();                     // 降级不是终局，见下\n", "")],
        cmd=["node", "scripts/ui/boot_test.mjs"],
        expect=r"应自愈回实时",
    ),
    Case(
        id="probe-baked-answer-while-backend-alive",
        cloud=False,
        requires="node",
        guards="scripts/ui/boot_test.mjs（boot() 行为契约，场景⑨）",
        defect="页面降级过之后，提问不再确认后端是否已经活了，直接给烘焙答案——后端就在旁边"
               "好好跑着，而用户拿到的是一个「看起来正常、其实和问题无关」的答案"
               "（问退款给 DAU 走势）。这是这条链路上最贵的一种失败：它不报错",
        patches=[("web/index.html",
                  "  if(MODE!=='live') await recheckOnce();\n", "")],
        cmd=["node", "scripts/ui/boot_test.mjs"],
        expect=r"提问必须走真实链路",
    ),
    Case(
        id="shell-asset-absolute-path",
        cloud=False,
        guards="scripts/ui/asset_check.py（shell 资源在两套挂载布局下都取得到）",
        defect="shell 里的本地资源写死绝对路径 `/vendor/...`：线上（站点根）照旧对，本地"
               "（挂在 /app 下）**静默 404**。页面照常出，解读/KPI/SQL/数据表全在，只有图表框"
               "空白、字体退回系统默认；`renderChart` 的 ReferenceError 发生在 setTimeout 里，"
               "不影响已渲染的块，所以界面上一句红字都没有，只有 uvicorn 日志里三条 404",
        patches=[("web/index.html",
                  'href="vendor/fonts/fonts.css"', 'href="/vendor/fonts/fonts.css"')],
        cmd=[PY, "scripts/ui/asset_check.py"],
        expect=r"绝对路径资源",
    ),
    Case(
        id="shell-config-js-route-gone",
        cloud=False,
        guards="scripts/ui/asset_check.py（唯一的绝对路径例外必须有人接）",
        defect="`/config.js` 是**故意**保持绝对路径的那一个（语义是站点根上的部署期产物），"
               "本地靠 server.py 回一个空脚本兜住。那条路由一没，这个例外就变成一条没人管的"
               "404——行为上仍然回退 /api/config，所以照旧没有任何症状，只剩日志噪音，"
               "而下一个人排查时会把它当成故障线索",
        patches=[("backend/server.py",
                  '@app.get("/config.js")', '@app.get("/__disabled_config_js")')],
        cmd=[PY, "scripts/ui/asset_check.py"],
        expect=r"没有对应路由",
    ),
    Case(
        id="funnel-shape-gate-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（judge_funnel 的形态闸 + stats.funnel 的实时闸）",
        defect="漏斗判分只比数值、不验形态。真实代价已经发生过一次：`L4-funnel` 的金标"
               "口径写成每步各 `count(DISTINCT user_id)` 一遍，得到 **394/385/396/373**"
               "（结算 396 > 浏览 394，根本不是漏斗），`value_hint` 就写着这四个数，"
               "agent 照这个口径答「漏斗几乎不衰减、是随机种子数据的问题」——判分 PASS，"
               "进了归档基线 26/26。正确口径（每步约束成上一步子集）是 394/314/258/199，"
               "逐层流失 20%/18%/23%，健康得很。结论整个反了，而全链路没有一处报错",
        patches=[("eval/run_eval.py",
                  "    for seq in _delivered_funnels(evidence):", "    for seq in []:")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"非单调的理由应点明形态",
    ),
    Case(
        id="funnel-golden-subset-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（金标逐步的子集约束 = 约束 A）",
        defect="金标是这道题的**真源**，真源错了下游全错——判官比的就是它。这里只删掉 30 天"
               "那条金标里 s2 的 `IN (SELECT user_id FROM s1)`，口径当场退回独立计数，"
               "而 SQL 照样跑得通、照样出四个数。这条同时钉住检查器必须**逐步**核："
               "第一版只核「某处有就算过」，删掉一步照样全绿，又是一个名字比覆盖面大的检查器",
        # 锚点必须**恰好出现一次**，而 `AND user_id IN (SELECT user_id FROM s1)` 在
        # cases.json 里有两条（全期 + 30 天两份金标）。所以从 30 天那条独有的
        # `FROM meta_snapshot))` 起手，把整段带上，只有那一条能匹配。
        patches=[("eval/cases.json",
                  "FROM meta_snapshot)), s1 AS (SELECT DISTINCT user_id FROM w WHERE "
                  "event_name='view_product'), s2 AS (SELECT DISTINCT user_id FROM w WHERE "
                  "event_name='add_to_cart' AND user_id IN (SELECT user_id FROM s1))",
                  "FROM meta_snapshot)), s1 AS (SELECT DISTINCT user_id FROM w WHERE "
                  "event_name='view_product'), s2 AS (SELECT DISTINCT user_id FROM w WHERE "
                  "event_name='add_to_cart')")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"的 s2 缺少",
    ),
    Case(
        id="kb-funnel-subset-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（knowledge/ 参考 SQL 的子集约束）",
        defect="前两条闸（判分器形态闸 + 金标口径）全绿之后，agent 在浏览器上照旧答出 "
               "`394/385/396/373`。因为它读的不是金标、也不进判分器——它读 "
               "`domains/behavior/events.md` 和 `metrics/core_metrics.md`，"
               "那两张卡片里的「购买漏斗」参考 SQL 本身就是每步各数一遍，"
               "还配了一句「这份种子数据漏斗不衰减、别当业务结论」。**agent 是照抄的，"
               "抄得很忠实。** `verify_doc_sql.py` 只 EXPLAIN 语法不看口径，"
               "所以这两个文件此前零覆盖——闸装在了 agent 不走的路上",
        patches=[("knowledge/domains/behavior/events.md",
                  "s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'add_to_cart'\n"
                  "       AND user_id IN (SELECT user_id FROM s1)),",
                  "s2 AS (SELECT DISTINCT user_id FROM w WHERE event_name = 'add_to_cart'),")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"events\.md 的漏斗参考 SQL 里 s2 缺少",
    ),
    Case(
        id="kb-funnel-route-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（漏斗题到方法卡的路由）",
        defect="事故的另一半是**路由**：`analysis/_index.md` 把方法卡的适用面写成"
               "「判断/诊断/深度分析题」，于是一道朴素的「转化漏斗」取数题只加载行为域"
               "表卡片，`analysis/funnel_analysis.md` 从头到尾没被读过——SOP 里的约束 A/B "
               "一条都没生效。内容对不对是一回事，**送不送得到**是另一回事，"
               "这条钉的是后者",
        # 指针在这个文件里有两处（关键词路由表 + 常见分析场景），删一处剩一处照样绿。
        # 所以两处都要摘掉，而 patch 锚点必须唯一 —— 各自带上前缀文字。
        patches=[("knowledge/domains/behavior/_index.md",
                  "`events.md` **+ `analysis/funnel_analysis.md`**",
                  "`events.md`"),
                 ("knowledge/domains/behavior/_index.md",
                  "4. **漏斗转化分析**: 加载 `events.md` + `analysis/funnel_analysis.md`",
                  "4. **漏斗转化分析**: 加载 `events.md`")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"没有指向 `analysis/funnel_analysis\.md`",
    ),
    Case(
        id="kb-funnel-absolute-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（「转化率绝对值不具参考性」这条结论约束）",
        defect="口径修对之后剩下的那一半：漏斗**数值全对、形态也全对**（394/314/258/199，"
               "逐层流失 20%/18%/23%，单调递减），结论仍然可以错——端到端转化 50.5%"
               "（近 30 天 16.2%）被答成「转化表现优异」，也就是把生成器的抽样方式"
               "报成了业务表现。根因在分母：漏斗顶端「看过就走」这一侧没按真实比例生成，"
               "浏览过商品的 394 人里一次都没加购的只有 80 人（20.3%），而 25 种事件"
               "各自的行数被抽得近似均匀（736–866，极差比 1.18）。"
               "跟 `kb-retention-verdict-gone` 同一个病、不同的器官：钉**结论层**，"
               "不是数值层——数值闸对这种错一律判 PASS",
        patches=[("knowledge/analysis/funnel_analysis.md",
                  "- **绝对值不可比**（口径对了也要写这句）",
                  "- **绝对值**（口径对了也要写这句）")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"找不到 `绝对值不可比`",
    ),
    Case(
        id="kb-retention-cohort-column",
        cloud=False,
        guards="eval/run_eval.py --selftest（留存 cohort 的时间列）",
        defect="`users` 上 `created_at` 和 `registered_at` **两列都存在**，所以 cohort "
               "写错列时 EXPLAIN 通过、SQL 出结果、行数也正常——只是分出来的是另一批人"
               "（种子那一批实测 500/500 行两列不相等）。这是「检查器验的是另一个维度」的"
               "第三种形态：`verify_doc_sql.py` 查语法，语法没错，错的是语义。"
               "旧版本这段确实写的是 `created_at`。全量重灌之后两列**逐行相等**（实测），"
               "所以现在写错列不再分出另一批人——这条钉的是**卡片点名哪一列**，"
               "不是当下的数值差异：两列相等是这一批数据的偶然，不是口径",
        patches=[("knowledge/metrics/core_metrics.md",
                  "DATE_TRUNC('week', CAST(registered_at AS date)) AS cohort_week",
                  "DATE_TRUNC('week', CAST(created_at AS date)) AS cohort_week")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"cohort 段里出现了 `created_at`",
    ),
    Case(
        id="kb-retention-numerator-unbounded",
        cloud=False,
        guards="eval/run_eval.py --selftest（留存分子必须限定在 cohort 内）",
        defect="Rₜ 的分子是「**这个 cohort 里**第 t 期还活跃的人数」。分子若数成全站活跃，"
               "留存率会超过 100%——实测第 1 周 219 人 / cohort 43 人 = **509%**。"
               "这个缺陷至少会自己炸出来，比漏斗那次（悄悄给一个像样的数）友好，"
               "但只有装了闸才会在提交前炸",
        patches=[("knowledge/metrics/core_metrics.md",
                  "THEN c.user_id END) AS week1",
                  "THEN act.user_id END) AS week1")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"实测这么写第 1 周是 219/43 = 509%",
    ),
    Case(
        id="kb-retention-verdict-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（「这份数据算不出留存」这条结论约束）",
        defect="数字全对、右删失说明也完全正确，结论仍然是错的：v1 种子样本的留存曲线平坦"
               "（45.0/43.0/42.1/41.1/43.0），agent 把它答成「曲线平稳、留得住」——"
               "**把项目自己的 P0 数据缺陷报成了正面业务发现**，而那句右删失说明让整段话"
               "听起来很严谨。根因是这条事实此前只写在 `docs/data-audit.md`，"
               "`knowledge/` 里一个字都没有，而 agent 只读 `knowledge/`。"
               "这条钉的是**结论层**，不是数值层。文档现在写的是判据（W4/W1 ≥ 0.8 算不衰减）"
               "而不是结论——全量批次的曲线已经衰减了——所以注入打在判据那句话上",
        # 三处一起改：那句话在这份文档里出现三次（判据的前提、对照表的落笔栏、
        # risk finding 的写法），只改一处不算"这条约束消失了"，剩下两处照样把它教会。
        patches=[("knowledge/analysis/retention_curve.md",
                  "因为「这份数据算不出留存」这句话",
                  "因为「这份数据的留存怎么写」这件事"),
                 ("knowledge/analysis/retention_curve.md",
                  "**数字照给 + 明说这份数据算不出留存**",
                  "**数字照给 + 说明观测窗**"),
                 ("knowledge/analysis/retention_curve.md",
                  '"这一批数据算不出留存"写进',
                  '"观测窗不完整"写进')],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"找不到 `算不出留存`",
    ),
    Case(
        id="retention-verdict-gate-gone",
        cloud=False,
        guards="eval/run_eval.py --selftest（judge_retention 的结论闸）",
        defect="上一条钉的是知识卡片里那句话还在，这条钉的是**判分器会不会因此判错**——"
               "少了它，L7 的留存金标就退回「数值对就 PASS」，而那份把 P0 数据缺陷答成"
               "「曲线平稳、留得住」的答案，数值恰恰全对，会安安静静进归档基线。"
               "闸是白名单（必须出现「算不出/不可信/独立抽样/假象」这类声明）而不是黑名单，"
               "因为**正确答案里就带着「别当成\"留存好\"的正面结论」**，任何按「留存好」"
               "拦的写法都会打到正确答案身上。第一版白名单还放了「别当」，结果被那份缺陷"
               "答案的右删失句「别当真实下跌」满足了——本该不算数的 caveat 成了放行凭证",
        patches=[("eval/run_eval.py",
                  "    if not decaying and not any(k in prose for k in _RETENTION_CREDIBILITY):",
                  "    if False:")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"结论报成「留得住」应判错",
    ),
    Case(
        id="doc-row-total-drift",
        cloud=False,
        guards="scripts/lakehouse/verify_load.py --selftest（文档规模声明 ⟷ 装载真源实测）",
        defect="knowledge/connection.md 里的全库规模与真源不符——**agent 数不出这个数**"
               "（治理角色读不到 user_messages），只能照抄卡片，于是自信地报一个错的规模",
        # 注入打在**张数**上而不是行数上：行数随重灌变（这里曾写死种子那一批的
        # 189,672，全量重灌后锚点命中 0 次，负测自己先炸了），张数不变。同一条判据、
        # 同一条报错路径（check_doc_totals 的 eq()），但锚点不再绑在某一批数据上。
        patches=[("knowledge/connection.md",
                  "35 张原始表", "34 张原始表")],
        cmd=[PY, "scripts/lakehouse/verify_load.py", "--selftest"],
        expect=r"文档行数声明",
    ),
    # 上一条守的是**文档 ⟷ 装载真源**这一侧。规模判据另有三条守另一侧：湖里装的是哪一批
    # —— 紧跟着的两条离线（声明原文、声明次数），加上离线段末尾那条连云的
    # `scale-lake-unregistered-batch`（实测 ⟷ 已登记批次）。三条各盯一种失效，
    # 缺哪条都会留下一种"看起来绿了"。
    Case(
        id="scale-prompt-hardcoded",
        cloud=False,
        guards="scripts/lakehouse/verify_scale.py --selftest（文档声明原文）",
        defect="prompt 里写死了某一批的行数。同一份 prompt 会跑在种子库和全量湖上，"
               "写死就必然有一边是错的——本库真实发生过：prompt 教 agent「35 张明细表，"
               "约 19 万行」，而湖里装着 7994 万行，L0–L6 全绿，因为没有一条断言的对象是「有多少行」",
        patches=[("backend/agent.py",
                  "35 张明细表 + 4 张 mart + 8 张派生表，**行数取决于湖里装的是哪一批**"
                  "——种子样本约 19 万行，全量重灌约 8000 万行，要精确行数就 `count(*)`，"
                  "Iceberg 读元数据、零扫描",
                  "35 张明细表，约 19 万行")],
        cmd=[PY, "scripts/lakehouse/verify_scale.py", "--selftest"],
        expect=r"backend/agent\.py：声明原文 '行数取决于湖里装的是哪一批' 命中 0 次",
        forbid=r"规模声明自洽",
    ),
    Case(
        id="scale-decl-half-edited",
        cloud=False,
        guards="scripts/lakehouse/verify_scale.py --selftest（声明的出现次数）",
        defect="同一个数在一份文件里抄了两处，重灌后只改了其中一处。判据钉的是**次数**"
               "而不是「只准出现一次」——真要求去重，判据就变成了在管别人的散文；"
               "钉次数则一改一漏立刻红，而合法的多处引用不受干扰",
        patches=[("backend/run.sh",
                  '（LEGACY 路径，约 19 万行，不再维护）',
                  '（LEGACY 路径，约 8000 万行，不再维护）')],
        cmd=[PY, "scripts/lakehouse/verify_scale.py", "--selftest"],
        expect=r"backend/run\.sh：声明原文 '约 19 万行' 命中 1 次（登记 2 次）",
        forbid=r"规模声明自洽",
    ),
    Case(
        id="kb-golden-pertable-max",
        cloud=False,
        guards="eval/run_eval.py --selftest（金标的时间锚点）",
        defect="金标把「今天」写成该表自己的 max(时间列)——**评测于是在奖励知识库明令禁止的写法**。"
               "这比 current_date 那种错难发现得多：多数表的轴末恰好等于锚点，逐表 max 算出来"
               "和正确答案一样，只有踩到那十几根伸出业务日历的轴时才分叉"
               "（`sessions.start_time` 越到 2026-01-25，于是 L3-churn-30d 的窗口整体推后一天）",
        patches=[("eval/cases.json",
                  "WHERE event_time::date=(SELECT max(as_of_date) FROM meta_snapshot)",
                  "WHERE event_time::date=(SELECT max(event_time)::date FROM events)")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"L1-dau-latest 金标.*时间锚点写成了逐表 max\(event_time\)",
        forbid=r"全部通过",
    ),
    Case(
        id="lite-mode-analysis-banned",
        cloud=False,
        guards="eval/run_eval.py --selftest（常规模式的文档路由）",
        defect="`LITE_SUFFIX` 里那句「不要读 analysis/」被写回一刀切，而域索引写着"
               "「两个都要读，**哪怕只是取数**」——两句话对冲、系统提示赢，于是常规模式下"
               "留存题的分子约束（写错出 509%）和曲线可信度判据一起读不到。"
               "deep 模式只有 4 个预设按钮能进，用户手打的问题一律走 lite，所以这不是"
               "评测保真度问题而是产品缺陷。注入只把例外那句话换个说法，两边就不再对上",
        patches=[("backend/agent.py", "哪怕只是取数", "就算只是取数")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"LITE_SUFFIX 里没有「哪怕只是取数」这个例外",
        forbid=r"全部通过",
    ),
    Case(
        id="retention-gate-hardcoded",
        cloud=False,
        guards="eval/run_eval.py --selftest（留存结论闸按曲线形状开合）",
        defect="留存的结论闸写死成「必须声明这份数据算不出留存」。它在 v1 种子样本上是对的"
               "（曲线平坦，W4/W1≈0.99），数据换成全量批次之后（71.6→37.2，W4/W1≈0.52）"
               "就开始**要求 agent 说一句假话**——这一批是算得出留存的。判据钉在数据的性质上"
               "而数据会换，就必然有这种反转；正确做法是现从金标的 pct 那一份量曲线形状",
        patches=[("eval/run_eval.py",
                  "        decaying = ratio < RETENTION_DECAY_MAX",
                  "        decaying = False")],
        cmd=[PY, "eval/run_eval.py", "--selftest"],
        expect=r"曲线在衰减时不该再要求声明「算不出留存」: 得到 False，期望 True",
        forbid=r"全部通过",
    ),
    Case(
        id="scale-lake-unregistered-batch",
        cloud=True,
        guards="scripts/lakehouse/verify_scale.py（湖实测 ⟷ 已登记批次）",
        defect="湖里换了一批数据而没人来 docs/scale.json 登记。verify_load 答不了这件事——"
               "它比的是「湖 ⟷ data/csv」，默认两侧本来就该相等，而这个账号本来就装着"
               "另一批。注入把已登记批次的一张表改成别的数，实测就命中不了任何一批",
        patches=[("docs/scale.json",
                  '"orders": 854140,', '"orders": 854141,')],
        cmd=[PY, "scripts/lakehouse/verify_scale.py"],
        expect=r"没有命中任何已登记批次[\s\S]*orders",
        forbid=r"规模与声明一致",
    ),

    # ---------------------------------------------------------- 连云
    Case(
        id="card-ghost-column",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（B 类：陈旧文档）",
        defect="卡片写了一个 Glue 里不存在的列——agent 照抄就写出查不动的 SQL",
        patches=[("knowledge/domains/attribution/channels.md",
                  "| channel_type | VARCHAR(30) | 渠道类型 |",
                  "| channel_type | VARCHAR(30) | 渠道类型 |\n"
                  "| ghost_col_b | INT | 负测注入 |")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"卡片写了但 Glue 里没有的列 \['ghost_col_b'\]",
        # 同一张卡片里的枚举行（paid / kol / organic…）不该被当成列名 —— 假阳性回归。
        # 注入一处就只该报一处。
        forbid=r"发现 (?!1 处)",
    ),
    Case(
        id="mart-ddl-ghost-column",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（C 类：生成链断裂）",
        defect="DDL 里声明了列但 Glue 里没有——建表脚本没跑完/迁移半途失败",
        patches=[("database/iceberg/02_mart.sql",
                  "    dau               bigint",
                  "    ghost_col_c       bigint        COMMENT '负测注入',\n"
                  "    dau               bigint")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"mart_daily_kpi\.ghost_col_c：声明了但 Glue 里没有",
    ),
    Case(
        id="table-undocumented",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（A 类：未文档化）",
        defect="Glue 里有表但 knowledge/ 里没有卡片——agent 的路由到不了它，"
               "这张表等于不存在",
        patches=[],
        delete=["knowledge/domains/attribution/channels.md"],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"channels：Glue 里有，但 knowledge/ 里没有表卡片",
    ),
    Case(
        id="ddl-type-drift",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（G 类：类型漂移）",
        defect="真源 DDL 的列类型与 Glue 里的不一致——重建表时会静默换类型",
        # 打在 v1 真源（reconcile 的声明态经 gen_ddl.parse_source 从这里来），
        # 不是打在生成的 iceberg DDL 上：那条路由 gen_ddl --check 管，已有别的用例。
        patches=[("database/03_attribution_domain.sql",
                  "    channel_name VARCHAR(100) NOT NULL,",
                  "    channel_name INTEGER NOT NULL,")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"channels\.channel_name：类型不一致",
    ),
    Case(
        id="ddl-comment-drift",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（H 类：注释漂移）",
        defect="真源改了列注释但没重灌 Glue——注释是 agent 读到的枚举/口径来源，"
               "漂移了它照旧信旧的",
        patches=[("database/03_attribution_domain.sql",
                  "    platform VARCHAR(50),  -- 'douyin', 'weixin', 'xiaohongshu', "
                  "'baidu', etc.",
                  "    platform VARCHAR(50),  -- 负测注入：改了真源注释但没重灌 Glue")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"channels\.platform：注释与真源不一致",
    ),
    Case(
        id="card-missing-column",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（I 类：卡片漏列）",
        defect="Glue 里有的列卡片没写——agent 不知道这列存在，"
               "该用它的时候会绕远路或答不出",
        patches=[("knowledge/domains/attribution/channels.md",
                  "| description | TEXT | 渠道描述 |\n", "")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"卡片「表结构」没写的列 \['description'\]",
    ),
    Case(
        id="metrics-ghost-identifier",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（D 类：治理口径失效）",
        defect="治理指标的 SQL 引用了表里没有的列——call_metric 会静默算错或报错",
        patches=[("backend/metrics_def.py", "is_repurchaser_30d", "ghost_col_d")],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict"],
        expect=r"指标 SQL 引用了表里没有的标识符 \['ghost_col_d'\]",
    ),
    Case(
        id="bogus-catalog",
        cloud=True,
        guards="scripts/lakehouse/reconcile.py --strict（F 类：目录不可用）",
        defect="catalog ID 写错/联邦掉了——对账无表可比，最容易被读成「没问题」",
        patches=[],
        cmd=[PY, "scripts/lakehouse/reconcile.py", "--strict",
             "--catalog", BOGUS_CATALOG],
        expect=r"读不到任何表|EntityNotFound|AccessDenied|不存在",
        baseline=None,               # 注入在参数里，不改文件，没有可比的基线
    ),
    Case(
        id="enum-value-absent",
        cloud=True,
        guards="scripts/lakehouse/verify_enums.py（方向一）",
        defect="卡片写了数据里不存在的枚举值——按它写 WHERE 就是空集，"
               "结论变成「这一类没有数据」",
        patches=[("knowledge/domains/attribution/channels.md",
                  "| direct | 直接访问 | 1 |",
                  "| direct | 直接访问 | 1 |\n| ghost_type | 负测注入 | 0 |")],
        cmd=[PY, "scripts/lakehouse/verify_enums.py", "-t", "channels"],
        expect=r"卡片写了但数据里没有：ghost_type",
    ),
    Case(
        id="enum-value-undocumented",
        cloud=True,
        guards="scripts/lakehouse/verify_enums.py（方向二）",
        defect="数据里有、卡片没写——agent 不知道这个值存在，GROUP BY 出来还多一行没人解释",
        patches=[("knowledge/domains/attribution/channels.md",
                  "| direct | 直接访问 | 1 |\n", "")],
        cmd=[PY, "scripts/lakehouse/verify_enums.py", "-t", "channels"],
        expect=r"数据里有但卡片没写：direct",
    ),
    # 下面两条守 verify_constants.py 的**两个**方向。只守一个的话它会退化成一份
    # 只增不减的白名单：漏了「清单条目过期」那一侧，某列被修好之后清单里那条会永远
    # 留着，而"已修"和"未修"在报告上长得一模一样——这正是那份清单要避免的东西。
    Case(
        id="degenerate-col-unlisted",
        cloud=True,
        guards="scripts/lakehouse/verify_constants.py（方向一：清单外新增退化列）",
        defect="线上库新出现一个整列同一个值的列而没人登记——灌载忠实、EXPLAIN 通过、"
               "没声明枚举所以 verify_enums 不看它，任何求和恒等式都满足，"
               "而基于它算的比率恒等于 0 且不报错",
        patches=[("scripts/lakehouse/verify_constants.py",
                  '    "posts.share_count": "同 posts.like_count，从 post_shares 明细 bincount 回填",\n',
                  "")],
        cmd=[PY, "scripts/lakehouse/verify_constants.py", "-t", "posts"],
        expect=r"posts\.share_count.*不在任何清单里",
    ),
    Case(
        id="degenerate-col-stale-entry",
        cloud=True,
        guards="scripts/lakehouse/verify_constants.py（方向二：清单条目已过期）",
        defect="清单里某列已经不是退化列了（比如重灌真发生了），那条却还留着——"
               "豁免变成永久免疫，下一个人读到的是「这列还没修」",
        patches=[("scripts/lakehouse/verify_constants.py",
                  '    "events.created_at": "生成器挂在事件时刻 ev_time 上",',
                  '    "events.created_at": "生成器挂在事件时刻 ev_time 上",\n'
                  '    "posts.title": "负测注入的过期条目",')],
        cmd=[PY, "scripts/lakehouse/verify_constants.py", "-t", "posts"],
        expect=r"posts\.title.*已经不是常量了",
    ),
    Case(
        id="doc-sql-rotten",
        cloud=True,
        guards="scripts/lakehouse/verify_doc_sql.py（Athena EXPLAIN 过一遍文档）",
        defect="卡片里的示例 SQL 引用了不存在的列——文档不会被执行，错了没人会响",
        patches=[("knowledge/domains/attribution/channels.md",
                  "    channel_id,\n    channel_name,\n    platform\nFROM channels",
                  "    channel_id,\n    ghost_col_sql,\n    platform\nFROM channels")],
        cmd=[PY, "scripts/lakehouse/verify_doc_sql.py",
             "--only", "attribution/channels.md"],
        expect=r"条文档 SQL 跑不动",
    ),
    Case(
        id="csv-value-changed",
        cloud=True,
        guards="scripts/lakehouse/verify_load.py（CSV 真源 ⟷ Athena 现查）",
        defect="CSV 与湖里的数不一致——装载丢了行/串了值，行数对但求和不对",
        patches=[("data/csv/channels.csv", "13,直接访问", "130,直接访问")],
        cmd=[PY, "scripts/lakehouse/verify_load.py", "-t", "channels"],
        # 认到「channels 有几项指标不符」+ 汇总行；只认「处差异」的话，连"读不到表"
        # 之类跟装载无关的红也算过。
        expect=r"❌ channels\s+\d+ 项指标，\d+ 项不符[\s\S]*发现 \d+ 处差异",
        forbid=r"装载完整",
    ),
    Case(
        id="gov-probe-blind",
        cloud=True,
        guards="scripts/lakehouse/governance.py --verify（治理层实测探针）",
        defect="探针其实没在探——比如它用的是调用方凭证而不是 agent 角色。"
               "那时「读不到 user_messages」这类断言永远绿，而边界可能根本不在",
        # 这个用例不改文件：`--as-caller` 就是把探针换成 data lake admin 身份跑。
        # admin 本该读得到被排除的列和拒绝表，所以**必须全红**。全绿只有一种解释：
        # 这批断言什么也没验。基线是同一批探针以 agent 角色跑（必须绿）。
        patches=[],
        baseline=[PY, "scripts/lakehouse/governance.py", "--verify"],
        cmd=[PY, "scripts/lakehouse/governance.py", "--verify", "--as-caller"],
        expect=r"查询成功了，说明这道边界没生效",
    ),
    Case(
        id="gov-backend-not-assuming",
        cloud=True,
        guards="scripts/lakehouse/governance.py --verify-backend（后端接线）",
        defect="治理层全在，但后端没接上：角色建好了、LF 权限也发了，"
               "而 db.py 仍旧用进程自己的（admin）凭证查——**/health 看起来还是对的**",
        # 注入的是最像真实回归的那一种：AssumeRole 照做（所以 identity 字段仍显示
        # 那个角色），只是构造 Athena 客户端时没把 session 传下去。这条要是不红，
        # 「治理已验证」就完全建立在一个查错身份的后端上。
        patches=[("backend/db.py", "            region=region, session=session,",
                  "            region=region,")],
        cmd=[PY, "scripts/lakehouse/governance.py", "--verify-backend"],
        expect=r"后端读到了 users\.email —— 用的不是受限凭证",
    ),
]


# ---------------------------------------------------------------- 备份 / 还原

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Sandbox:
    """把要动的文件整份备份，退出时还原并核对 sha256。"""

    def __init__(self, rels: list[str]):
        self.rels = rels
        self.dir = Path(tempfile.mkdtemp(prefix="negtest-"))
        self.before: dict[str, str] = {}

    def __enter__(self) -> Sandbox:
        for rel in self.rels:
            src = ROOT / rel
            dst = self.dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            self.before[rel] = sha(src)
        return self

    def restore(self) -> list[str]:
        broken = []
        for rel in self.rels:
            src, dst = self.dir / rel, ROOT / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)   # delete 用例可能删掉了目录里最后一个文件
                shutil.copy2(src, dst)
                if sha(dst) != self.before[rel]:
                    broken.append(f"{rel}（还原后 sha256 不符）")
            except Exception as e:                      # noqa: BLE001
                broken.append(f"{rel}（{e}）")
        return broken

    def __exit__(self, *exc) -> None:
        global RESTORE_FAILED
        broken = self.restore()
        if broken:
            RESTORE_FAILED = True                      # 后面的用例一律不再跑，见 main()
            print(f"\n\033[31m还原失败\033[0m，备份在 {self.dir}，请手动恢复：")
            for b in broken:
                print(f"  - {b}")
            return                                     # 保留备份目录
        shutil.rmtree(self.dir, ignore_errors=True)


# 还原失败后工作树是脏的，任何后续用例的"绿"都不可信（而且基线缓存也失效了）。
RESTORE_FAILED = False


# ---------------------------------------------------------------- 执行

def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout + p.stderr


def apply_patches(case: Case) -> None:
    """唯一子串替换 + 整份删除。锚点不唯一就抛——负测的锚点漂移必须显式失败。"""
    for rel, old, new in case.patches:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"{rel} 里锚点出现 {n} 次（要求恰好 1 次）：{old[:60]!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")
    for rel in case.delete:
        (ROOT / rel).unlink()


# 基线（未注入时那次运行）按命令缓存：reconcile 家族有 8 个用例共用同一条
# `reconcile.py --strict`，一次云调用约 40 秒，不缓存就白跑 7 遍。
# 缓存成立的前提是**每个用例退出时工作树已还原**，所以还原一旦失败就停跑（RESTORE_FAILED）。
_BASELINE: dict[tuple[str, ...], tuple[int, str]] = {}


def baseline_result(cmd: list[str]) -> tuple[int, str]:
    key = tuple(cmd)
    if key not in _BASELINE:
        _BASELINE[key] = run(cmd)
    return _BASELINE[key]


def run_case(case: Case, fast: bool) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。"""
    base = None if fast else case.baseline_cmd()
    if base:
        rc, out = baseline_result(base)
        if rc != 0:
            return False, ("注入前就已经是红的（rc=%d），这个用例证明不了任何事。"
                           "先修好正向检查：\n%s" % (rc, tail(out)))

    files = case.touched()
    if not files:
        rc, out = run(case.cmd)
        return judge(case, rc, out)

    with Sandbox(files):
        apply_patches(case)
        rc, out = run(case.cmd)
    return judge(case, rc, out)


def judge(case: Case, rc: int, out: str) -> tuple[bool, str]:
    if rc == 0:
        return False, ("检查器在缺陷面前 exit 0 —— **假阴性**。\n" + tail(out))
    if not re.search(case.expect, out):
        return False, (f"红了，但不是因为这个缺陷（没匹配 /{case.expect}/）。\n"
                       "换个原因红了不算这个用例过。\n" + tail(out))
    if case.forbid and re.search(case.forbid, out):
        return False, (f"匹配到了不该出现的 /{case.forbid}/ —— 多报了，"
                       f"可能是假阳性回归。\n" + tail(out))
    return True, ""


def tail(out: str, n: int = 12) -> str:
    lines = [l for l in out.splitlines() if l.strip()]
    return "\n".join("        " + l for l in lines[-n:])


def main() -> int:
    ap = argparse.ArgumentParser(description="L8 负测：验证检查器该报时真会报")
    ap.add_argument("-c", "--case", action="append", default=[],
                    help="只跑这些用例 id（可重复）")
    ap.add_argument("--offline", action="store_true", help="只跑不连云的用例")
    ap.add_argument("--fast", action="store_true",
                    help="跳过「注入前应为绿」那一步（快一半，但一个本来就红的"
                         "检查器会让用例假通过）")
    ap.add_argument("--list", action="store_true", help="列出用例后退出")
    a = ap.parse_args()

    picked = [c for c in CASES
              if (not a.case or c.id in a.case) and (not a.offline or not c.cloud)]
    # 缺外部命令时**明说跳过**：假红会让人去查一个不存在的缺陷，静默少跑会让
    # 「22/22 全过」读起来像全都验过了。两种都比多打一行字贵。
    skipped = [c for c in picked if c.requires and not shutil.which(c.requires)]
    picked = [c for c in picked if c not in skipped]

    if a.list:
        for c in CASES:
            print(f"  {'☁️ ' if c.cloud else '  '} {c.id:<26} {c.guards}")
            print(f"       缺陷：{c.defect}")
        return 0
    if not picked:
        print(f"没有匹配的用例；已知 id：{', '.join(c.id for c in CASES)}")
        return 2

    unknown = set(a.case) - {c.id for c in CASES}
    if unknown:
        print(f"未知用例 id：{', '.join(sorted(unknown))}")
        return 2

    print(f"L8 负测：{len(picked)} 个用例"
          f"（{sum(1 for c in picked if c.cloud)} 个连云）"
          + ("，--fast：跳过注入前的绿检查" if a.fast else ""))
    print(f"解释器 {PY}\n")
    for c in skipped:
        print(f"  ⊘ {c.id:<26} 跳过：本机没有 {c.requires}（装了自动跑）")

    failed: list[str] = []
    for c in picked:
        if RESTORE_FAILED:
            print("\n\033[31m上一个用例还原失败，剩下的不跑了\033[0m"
                  "（脏工作树上的任何结论都不可信）。")
            return 1
        print(f"  ▶ {c.id:<26} ", end="", flush=True)
        try:
            ok, why = run_case(c, a.fast)
        except Exception as e:                          # noqa: BLE001
            ok, why = False, f"负测自身出错：{e}"
        if ok:
            print("\033[32mPASS\033[0m  （注入后正确报错）")
        else:
            print("\033[31mFAIL\033[0m")
            print(f"        守的是：{c.guards}")
            print(f"        缺陷：  {c.defect}")
            print(why)
            failed.append(c.id)

    print()
    if failed:
        print(f"\033[31m{len(failed)} 个用例失败\033[0m：{', '.join(failed)}")
        print("失败含义：这些检查器在对应缺陷面前不会报警——它们的绿灯说明不了什么。")
        return 1
    print(f"\033[32m{len(picked)} 个用例全部通过 ✅\033[0m"
          "  每个检查器都在对应缺陷面前变红并给出了正确的消息")
    print("提醒：负测只证明「该报时会报」。它不证明检查器覆盖面够——"
          "覆盖面缺口写在 docs/test-plan.md 的「没有自动化覆盖」一节。")
    return 0


if __name__ == "__main__":
    # Ctrl-C 也要还原：Sandbox 的 __exit__ 靠异常传播触发，
    # 默认 SIGTERM 不抛异常，会带着注入过的文件直接死掉。
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    raise SystemExit(main())
