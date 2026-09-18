#!/usr/bin/env python3
"""三条 arm 的单价 —— **从 AWS Price List API 取，不写在代码里**。

## 为什么不硬编码

金额那一栏和耗时那一栏的性质不一样。耗时是我们量出来的，价格是 AWS 定的，
写进代码就变成一个**没有出处、且会过期**的常量：读报告的人无法判断
「$5/TB 是当时的价还是作者记错了」，而它错了不会有任何东西报警。

所以这一层的规矩是：

1. 单价一律从 Price List API 取（`pricing` 端点只在 us-east-1 有，与被查区域无关）。
2. 取到的东西连**出处**一起存——`usagetype`、`unit`、`description`、取数时刻，
   都写进缓存文件。报告里的每一个金额因此可以反查到一条 AWS 价目行。
3. **取不到就报错，不给默认值。** 少一个价就少一条 arm 的成本，那时候该停下来，
   而不是拿一个猜的数把表填满。

## 三个实测踩出来的坑

**1. 分档价目有多条 `priceDimensions`，随手取一条会拿到错的档。** S3
`USW2-TimedStorage-ByteHrs` 就是：直接取第一条拿到的是「超过 500TB」那一档
（$0.021/GB-Mo），而本项目的用量在第一档（$0.023）。所以按 `beginRange == "0"`
选档，并且把「这一项有几档」记进缓存——分档存在本身是读数的人该知道的事。

**2. `MaxResults` 会截断，必须分页。** 不分页时 Redshift Serverless 的 RPU 价
和 Fargate 的 Linux x86 价都不在第一页里，症状是「这一项没有价」而不是报错。

**3. 单位要断言，不能只取数字。** `RPU-Hr`、`GB-Mo`、`Terabytes` 三种单位的换算
系数差好几个数量级。AWS 哪天把某项的单位从小时改成秒，只取 `pricePerUnit`
会得到一个量级错了的金额，而它长得像个正常数字。所以每一项都声明期望单位，
不符就抛。

用法：

    python3 scripts/bench/prices.py --show          # 打印当前价目（有缓存用缓存）
    python3 scripts/bench/prices.py --refresh       # 强制重取
    python3 scripts/bench/prices.py --selftest      # 换算逻辑自测，不连网
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "bench"
REGION = os.environ.get("AWS_REGION", "us-west-2")

#: Price List API 里的区域写法。它用的是「人读的名字」而不是区域代码。
LOCATIONS = {
    "us-west-2": "US West (Oregon)",
    "us-east-1": "US East (N. Virginia)",
}

#: 要取哪些价。键是本项目内部用的名字，值是 (ServiceCode, usagetype, 期望单位)。
#: `usagetype` 写全名而不是模糊匹配：模糊匹配会在 AWS 新增一个名字相近的项时
#: 悄悄换掉我们读的那一行（例如 `Fargate-ARM-vCPU-Hours` 与 `Fargate-vCPU-Hours`）。
WANT = {
    # Athena 按扫描字节计费。10MB 起步价不在价目里，是计费规则，写在 timing.py。
    "athena_per_tb_scanned": ("AmazonAthena", "USW2-DataScannedInTB", "Terabytes"),
    # Redshift Serverless 按 RPU-秒计费，60 秒起步价同样是规则不是价目。
    "redshift_per_rpu_hour": ("AmazonRedshift", "USW2-Redshift:ServerlessUsage", "RPU-Hr"),
    # Redshift 多出来的那份物化要占托管存储——这是它比另两条 arm 多付的固定成本。
    "redshift_storage_per_gb_month": ("AmazonRedshift", "USW2-RMS:Serverless", "GB-Mo"),
    # DuckDB 自己不收费，收费的是跑它的那个 Fargate 任务。x86 Linux，
    # 与 `scripts/bench/fargate.py` 推的 `linux/amd64` 镜像对应。
    "fargate_per_vcpu_hour": ("AmazonECS", "USW2-Fargate-vCPU-Hours:perCPU", "hours"),
    "fargate_per_gb_hour": ("AmazonECS", "USW2-Fargate-GB-Hours", "hours"),
    # DuckDB 直读 S3，GET 请求要单独付；Athena 的 $5/TB 里已经含了这部分。
    "s3_per_get_request": ("AmazonS3", "USW2-Requests-Tier2", "Requests"),
    "s3_storage_per_gb_month": ("AmazonS3", "USW2-TimedStorage-ByteHrs", "GB-Mo"),
}


def cache_path(region: str = REGION) -> Path:
    return CACHE_DIR / f"prices-{region}.json"


def _dimension(term: dict) -> tuple[dict, int]:
    """从一条 OnDemand term 里挑出第一档，并返回一共有几档。见 docstring 第 1 条。"""
    dims = list(term["priceDimensions"].values())
    first = [d for d in dims if str(d.get("beginRange", "0")) == "0"]
    if not first:
        # 没有 beginRange=0 这一档说明这一项的形状和我们的假设不一样，
        # 猜一个会得到量级错误的金额，所以直接抛。
        raise RuntimeError(
            f"价目项没有 beginRange=0 的档位，档位是："
            f"{[(d.get('beginRange'), d.get('endRange')) for d in dims]}")
    return first[0], len(dims)


def fetch(region: str = REGION) -> dict:
    """从 Price List API 取全部要用的单价。取不到就抛，不给默认值。"""
    import boto3

    loc = LOCATIONS.get(region)
    if not loc:
        raise RuntimeError(
            f"不知道区域 {region} 在 Price List API 里叫什么。"
            f"已知：{sorted(LOCATIONS)}。加一条映射即可，别改成模糊匹配。")
    # pricing 端点只在 us-east-1（和 ap-south-1）有，这与被查的区域无关。
    cli = boto3.client("pricing", region_name="us-east-1")

    by_service: dict[str, dict] = {}
    for svc in sorted({s for s, _, _ in WANT.values()}):
        rows: dict[str, dict] = {}
        for page in cli.get_paginator("get_products").paginate(
            ServiceCode=svc,
            Filters=[{"Type": "TERM_MATCH", "Field": "location", "Value": loc}],
        ):                                          # 见 docstring 第 2 条：必须分页
            for blob in page["PriceList"]:
                d = json.loads(blob)
                ut = d["product"]["attributes"].get("usagetype")
                if ut and ut not in rows:
                    rows[ut] = d
        by_service[svc] = rows

    out: dict[str, dict] = {}
    missing = []
    for key, (svc, ut, unit) in WANT.items():
        d = by_service[svc].get(ut)
        if d is None:
            missing.append(f"{key}: {svc} / {ut}")
            continue
        term = list(d["terms"]["OnDemand"].values())[0]
        dim, ntiers = _dimension(term)
        if dim["unit"] != unit:                     # 见 docstring 第 3 条
            raise RuntimeError(
                f"{key} 的单位变了：价目说 {dim['unit']!r}，代码按 {unit!r} 换算。"
                f"\n  换算系数会差好几个数量级，而算出来的金额看着像正常数。"
                f"\n  先确认新单位的含义，再改 WANT 里的期望单位。")
        out[key] = {
            "usd": float(dim["pricePerUnit"]["USD"]),
            "unit": dim["unit"],
            "service": svc,
            "usagetype": ut,
            "description": dim.get("description", ""),
            "tiers": ntiers,
        }
    if missing:
        raise RuntimeError(
            "这几项在价目里找不到，成本那一栏会缺口，所以不继续：\n  "
            + "\n  ".join(missing)
            + "\n  usagetype 是精确匹配的，AWS 改名会长成这个样子。")
    return {
        "region": region,
        "location": loc,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "AWS Price List API (get_products, us-east-1 endpoint)",
        "items": out,
    }


def load(region: str = REGION, refresh: bool = False) -> dict:
    """有缓存用缓存，否则取一次并写缓存。

    缓存不设过期时间是刻意的：**悄悄换掉报告里的单价比用旧价更坏**。
    要更新就显式 `--refresh`，那时缓存里的 `fetched_at` 会跟着变，
    读报告的人能看出这份金额是按哪天的价算的。
    """
    p = cache_path(region)
    if p.exists() and not refresh:
        return json.loads(p.read_text(encoding="utf-8"))
    data = fetch(region)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def usd(prices: dict, key: str) -> float:
    """取一项单价。缺了就抛——调用方拿到 0 会算出「这条 arm 免费」。"""
    it = prices["items"].get(key)
    if it is None:
        raise KeyError(f"价目里没有 {key!r}，有的是 {sorted(prices['items'])}")
    return float(it["usd"])


# ------------------------------------------------------------------ 计费规则
#
# 下面这些**不是价格，是规则**，所以写在代码里而不是从价目取：价目 API 给的是
# 单价，起步价 / 最小计费量这类规则不在里面。每一条都注明出处，因为它们对
# 「一条查询要多少钱」的影响比单价大得多。

#: Athena 每条查询的最小计费扫描量：10MB。一条只扫 1MB 的查询按 10MB 收。
#: 后果是**小查询的单价被拉平**——分区剪枝把扫描量从 10MB 压到 1MB 不省钱。
ATHENA_MIN_BYTES = 10 * 1024 * 1024

#: Redshift Serverless 的最小计费时长：60 秒。**这一条是三条 arm 里最容易读错的**：
#: 一条 0.9 秒的查询和一条 59 秒的查询收一样的钱，所以「Redshift 每条查询的成本」
#: 在小查询上完全由这个起步价决定，跟查询本身多重没关系。
#:
#: 2026-09-07 与账单侧对过一次，`redshift_cost` 是个**下界**，不是账单：一遍 8 条查询
#: 自身耗时 37.6s，模型按 max(37.6, 60) 算出 $0.048，而 `sys_serverless_usage` 记的是
#: 1440 RPU-秒 = $0.144，差 3 倍。差在这条下限的**单位**：它不是「一段活动收一次」，
#: 而是按**活动分钟**一段段收，跨了分钟边界就多收一段，活动之后紧邻的空闲分钟也照收
#: （那一遍占了 05:34/05:35 两分钟，加上 05:36 那个 compute=0 的分钟，共 3 段）。
#: 落到几个分钟上取决于查询在挂钟上的落点，从「自身耗时」推不出来，所以模型只能报下界。
#: 要账单侧真值走 `cost_cold.py --settle`，它直接读 `sys_serverless_usage`。
REDSHIFT_MIN_SECONDS = 60

#: 上面那次对账量到的比值：账单 / 模型 = 3.0×。写成常量是为了让引用它的说明文字
#: 与对账结果同源，改口径时不会两处不一致。样本只有一遍，当量级读。
REDSHIFT_BILLED_OVER_MODEL = 3.0

#: 本项目 workgroup 钉住的容量（base 8 / max 8）。RPU-秒 = RPU × 秒，
#: 而 RPU 数是我们自己钉的，所以它进成本公式而不是从引擎读。
REDSHIFT_RPU = int(os.environ.get("REDSHIFT_RPU", "8"))

#: Fargate 任务规格，与 `scripts/bench/fargate.py` 的任务定义一致。
FARGATE_VCPU = float(os.environ.get("BENCH_FARGATE_VCPU", "4"))
FARGATE_GB = float(os.environ.get("BENCH_FARGATE_GB", "16"))

#: Fargate 的最小计费时长：60 秒（按秒计费，每个任务至少收 1 分钟）。
#: **这条起步价的单位是「任务」，不是「查询」**，所以套几次取决于问的是几个任务：
#: 八条查询共用一个任务就只碰一次；八条各自单独来、各起一个任务就是八次。
#: 因此 `fargate_cost` 默认不套，由调用点按自己那一栏的前提显式传 `floor=True`。
FARGATE_MIN_SECONDS = 60

#: 起一个 DuckDB 任务到能跑第一条查询的时长：拉镜像 + 起容器。
#: 2026-09-07 从 ECS 任务的 `createdAt → startedAt` 量到 26.0 秒
#: （见 `data/bench/cost-cold-20260907T053407Z.jsonl` 与 `cost_cold.py --settle`）。
#: 这一项**只进「单条隔离」那一栏**：那一栏的前提是每条查询单独来一次，
#: 而单独来一次就得先起一个任务。八条连着跑只开机一次，那一栏由整个任务
#: 生命周期覆盖，不能再按条加，否则一次开机会被收八遍。
#: 写成常量而不是硬编码在算式里，是因为它是**实测值**、换镜像或换规格就会变，
#: 而它在单条成本里是压倒性的一项（26.0s 对执行的 0.1–2.7s）。
DUCKDB_BOOT_SECONDS = float(os.environ.get("BENCH_DUCKDB_BOOT_SECONDS", "26.0"))


def athena_cost(bytes_scanned: int, prices: dict) -> tuple[float, str]:
    """→ (美元, 口径说明)。不足 10MB 按 10MB 算。"""
    billed = max(int(bytes_scanned), ATHENA_MIN_BYTES)
    rate = usd(prices, "athena_per_tb_scanned")
    note = f"扫描 {bytes_scanned/1e6:.1f}MB → 计费 {billed/1e6:.1f}MB"
    if billed > bytes_scanned:
        note += "（不足 10MB 起步价，剪枝到更小不省钱）"
    return billed / 1024**4 * rate, note


def redshift_cost(seconds: float, prices: dict,
                  rpu: int = REDSHIFT_RPU) -> tuple[float, str]:
    """→ (美元, 口径说明)。不足 60 秒按 60 秒算。"""
    billed = max(float(seconds), float(REDSHIFT_MIN_SECONDS))
    rate = usd(prices, "redshift_per_rpu_hour")
    note = f"{seconds:.2f}s × {rpu} RPU → 计费 {billed:.0f}s"
    if billed > seconds:
        note += f"（不足 {REDSHIFT_MIN_SECONDS}s 起步价，这一条的成本与查询本身无关）"
    return billed / 3600 * rpu * rate, note


def fargate_cost(seconds: float, prices: dict, vcpu: float = FARGATE_VCPU,
                 gb: float = FARGATE_GB, floor: bool = False) -> tuple[float, str]:
    """→ (美元, 口径说明)。DuckDB 自己不收费，收费的是承载它的任务。

    **按任务存活时长算，不是按查询时长算。** 一个任务跑 20 条查询时，
    每条查询摊到的成本是任务时长 / 20，而不是各自的执行时长——这正是这条 arm
    与另两条最不一样的地方：它的成本与"跑了几条查询"无关，只与"开机多久"有关。

    `floor=True` 才套 60 秒起步价，因为那条线是**每个任务**一次。传进来的
    `seconds` 是一整个任务的生命周期时才该套；是某一条查询分到的那一段时就不该套，
    不然一个任务跑 8 条查询会被收 8 分钟。调用点必须自己知道手里的是哪一种。
    """
    billed = max(float(seconds), float(FARGATE_MIN_SECONDS)) if floor \
        else float(seconds)
    hrs = billed / 3600
    c = hrs * (vcpu * usd(prices, "fargate_per_vcpu_hour")
               + gb * usd(prices, "fargate_per_gb_hour"))
    note = f"任务存活 {seconds:.1f}s × {vcpu:g}vCPU/{gb:g}GB"
    if billed > seconds:
        note += (f"→ 计费 {billed:.0f}s（不足 {FARGATE_MIN_SECONDS}s 起步价；"
                 f"租不到比这更短的任务）")
    return c, note


def s3_get_cost(requests: int, prices: dict) -> tuple[float, str]:
    """DuckDB 直读 S3 时的 GET 请求费。Athena 的 $5/TB 里已经含了这部分，
    所以这一项只加在 DuckDB 那一栏——否则就是拿两把尺量。"""
    rate = usd(prices, "s3_per_get_request")
    return requests * rate, f"{requests} 次 GET"


# ---------------------------------------------------------------- 自测（无网）

def selftest() -> int:
    bad = 0

    def want(label, got, exp, tol=1e-12):
        nonlocal bad
        if abs(got - exp) > tol:
            bad += 1
            print(f"  FAIL {label}：算出 {got!r}，期望 {exp!r}")

    P = {"items": {
        "athena_per_tb_scanned": {"usd": 5.0},
        "redshift_per_rpu_hour": {"usd": 0.36},
        "fargate_per_vcpu_hour": {"usd": 0.04048},
        "fargate_per_gb_hour": {"usd": 0.004445},
        "s3_per_get_request": {"usd": 4e-7},
    }}

    # 1. Athena：整 TB 是干净的乘法
    want("1TB 扫描", athena_cost(1024**4, P)[0], 5.0)
    # 2. 起步价：1MB 和 10MB 同价，且 1MB 那条要说明自己被拉平了
    c1, n1 = athena_cost(1 * 1024**2, P)
    c10, _ = athena_cost(10 * 1024**2, P)
    want("不足 10MB 按 10MB", c1, c10)
    if "起步价" not in n1:
        bad += 1
        print("  FAIL 被起步价拉平的查询没有在口径说明里讲出来")
    # 2b. 超过起步价之后就不该再提起步价
    if "起步价" in athena_cost(500 * 1024**2, P)[1]:
        bad += 1
        print("  FAIL 500MB 的查询不该提起步价")

    # 3. Redshift：60 秒 × 8 RPU 的价
    want("60s × 8RPU", redshift_cost(60, P, rpu=8)[0], 60 / 3600 * 8 * 0.36)
    # 4. 最容易读错的一条：0.9s 和 59s 同价
    a, na = redshift_cost(0.9, P, rpu=8)
    b, _ = redshift_cost(59, P, rpu=8)
    want("0.9s 与 59s 同价（60s 起步）", a, b)
    if "起步价" not in na:
        bad += 1
        print("  FAIL 0.9s 那条没说明成本由起步价决定")
    # 4b. 真的超过 60s 之后要线性增长
    want("120s 是 60s 的两倍", redshift_cost(120, P, rpu=8)[0],
         2 * redshift_cost(60, P, rpu=8)[0])
    # 4c. RPU 翻倍成本翻倍
    want("16RPU 是 8RPU 的两倍", redshift_cost(60, P, rpu=16)[0],
         2 * redshift_cost(60, P, rpu=8)[0])

    # 5. Fargate：一小时 4vCPU/16GB
    want("1h 4vCPU/16GB", fargate_cost(3600, P, 4, 16)[0],
         4 * 0.04048 + 16 * 0.004445)
    # 5b. 默认不套起步价，线性到底。这一档对应「一条查询分到任务时长里的一段」，
    #     那一段没有自己的起步价——起步价是整个任务一次的事，见 FARGATE_MIN_SECONDS。
    want("Fargate 1s 是 2s 的一半", fargate_cost(1, P, 4, 16)[0],
         fargate_cost(2, P, 4, 16)[0] / 2)
    # 5c. floor=True 才套 60s，对应「一整个任务」这个单位
    want("Fargate 60s 起步价", fargate_cost(1, P, 4, 16, floor=True)[0],
         fargate_cost(FARGATE_MIN_SECONDS, P, 4, 16)[0])
    want("跑满后按实际时长", fargate_cost(120, P, 4, 16, floor=True)[0],
         fargate_cost(120, P, 4, 16)[0])
    want("起步价不改默认档", fargate_cost(1, P, 4, 16)[0],
         fargate_cost(3600, P, 4, 16)[0] / 3600)

    # 6. S3 GET
    want("10000 次 GET", s3_get_cost(10000, P)[0], 10000 * 4e-7)

    # 7. 缺价要抛，不能当 0
    try:
        usd({"items": {}}, "athena_per_tb_scanned")
        bad += 1
        print("  FAIL 缺价竟然没抛——调用方会拿到 0，算出「这条 arm 免费」")
    except KeyError:
        pass

    # 8. 分档：挑 beginRange=0 那一档，而不是随手第一条
    term = {"priceDimensions": {
        "b": {"beginRange": "512000", "unit": "GB-Mo",
              "pricePerUnit": {"USD": "0.021"}},
        "a": {"beginRange": "0", "unit": "GB-Mo",
              "pricePerUnit": {"USD": "0.023"}},
    }}
    dim, n = _dimension(term)
    if dim["pricePerUnit"]["USD"] != "0.023" or n != 2:
        bad += 1
        print(f"  FAIL 分档没挑第一档：拿到 {dim['pricePerUnit']} / {n} 档")
    # 8b. 没有第一档时要抛而不是猜
    try:
        _dimension({"priceDimensions": {"x": {"beginRange": "100", "unit": "GB-Mo",
                                              "pricePerUnit": {"USD": "1"}}}})
        bad += 1
        print("  FAIL 没有 beginRange=0 时竟然没抛")
    except RuntimeError:
        pass

    # 9. 单位断言在 WANT 里都填了（空字符串等于没断言）
    for k, (_, ut, unit) in WANT.items():
        if not unit or not ut:
            bad += 1
            print(f"  FAIL {k} 的 usagetype 或期望单位是空的")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  Athena 3 项（含 10MB 起步）、Redshift 5 项（含 60s 起步与 RPU 线性）、"
          f"Fargate 5 项（含 60s 起步价按任务收、默认档不套）、"
          f"S3 1 项、缺价即抛 1 项、分档 2 项、"
          f"单位断言 {len(WANT)} 项")
    print("换算逻辑自测通过 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="三条 arm 的单价（取自 AWS Price List API）")
    ap.add_argument("--show", action="store_true", help="打印当前价目")
    ap.add_argument("--refresh", action="store_true", help="强制重取并覆盖缓存")
    ap.add_argument("--selftest", action="store_true", help="换算逻辑自测，不连网")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    p = load(refresh=a.refresh)
    print(f"区域 {p['region']}（{p['location']}）  取数时刻 {p['fetched_at']}")
    print(f"出处 {p['source']}")
    print(f"缓存 {cache_path()}")
    print()
    w = max(len(k) for k in p["items"])
    for k, v in sorted(p["items"].items()):
        tier = f"  [{v['tiers']} 档，取第一档]" if v["tiers"] > 1 else ""
        print(f"  {k:<{w}}  {v['usd']:>12.10f} / {v['unit']:<11}"
              f"  {v['usagetype']}{tier}")
    print()
    print(f"计费规则（不在价目里，写在代码里）：")
    print(f"  Athena   每条查询最少按 {ATHENA_MIN_BYTES/1024**2:.0f}MB 计费")
    print(f"  Redshift 每条查询最少按 {REDSHIFT_MIN_SECONDS}s 计费，"
          f"容量钉住 {REDSHIFT_RPU} RPU")
    print(f"  Fargate  按任务存活时长，{FARGATE_VCPU:g}vCPU / {FARGATE_GB:g}GB，"
          f"每个任务最少按 {FARGATE_MIN_SECONDS}s 计费（单位是任务，不是查询）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
