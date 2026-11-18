#!/usr/bin/env python3
"""把 /api/catalog 的响应固化成静态 web/catalog.json，供线上前端直接取。

## 为什么线上要用静态快照，而不是像本地那样打接口

线上前端由 CloudFront 托管:默认行为回源 S3,只有 `/ask` 这一条路径走 VPC origin →
内网 ALB → Fargate relay。而 relay(functions/ask-relay/server.mjs)只实现了
`/health` 与 `/ask`,`/api/catalog` 打过去落到 S3,拿到 404。

要让线上有实时接口,得在 relay 里用 JS 重写一遍 catalog.py(还要把 knowledge/domains/
与 schema_manifest.yaml 打进镜像)、给 task role 加 Glue + Athena + Lake Formation 权限、
再走 CodeBuild → ECR → ECS 换版本。代价不只是工作量:组装逻辑会变成 Python 和 JS
两份,而**没有任何测试盯着这两份别跑偏**——元数据静默失真正是我们一直在修的毛病。

所以走这条:部署期用**同一个** backend/catalog.py 生成快照。数据仍然是真从 Glue
查的,只是时间点固定在部署那一刻;`snapshot: true` 会让 UI 明说"部署期快照"而不是
假装实时。想升级成真实时,补 relay 路由即可,前端已经是 /api/catalog 优先。

## 那道闸

默认**拒绝写出降级快照**。Glue 挂了时 catalog.py 会退到 information_schema——
本地开发无所谓,推上线就等于把错的表清单和 0 行数固化给所有访客看,而且不报警。
宁可让部署失败。确实要发降级版本时显式加 --allow-degraded。

用法:
    python3 scripts/deploy/build_catalog_json.py            # 写 web/catalog.json
    python3 scripts/deploy/build_catalog_json.py --print    # 只看摘要不落盘
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# .env.local（gitignored）:与三个 bash 脚本同一份本地覆盖。经 deploy_web.sh 调用时
# 值已 export 进环境,这里解析是为了**单独跑**时行为一致。已有的环境变量优先。
_envf = ROOT / ".env.local"
if _envf.exists():
    for _line in _envf.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# 这些必须在 import catalog / db 之前设好:两个模块在导入期就读环境变量。
DEFAULTS = {
    "DB_BACKEND": "athena",
    "AWS_REGION": "us-west-2",
    "ATHENA_WORKGROUP": "analytics-agent-wg",
    "ICEBERG_NAMESPACE": "app_analytics",
}
for k, v in DEFAULTS.items():
    os.environ.setdefault(k, v)

# Glue catalog ID = <账号>:s3tablescatalog/<表桶名>。账号不硬编码,从当前凭证现算——
# 仓库里不放真实账号 ID,而克隆的人跑出来的本来就该是他们自己的账号。
# EXPECT_ACCOUNT 是可选守卫(通常在 .env.local):设了就核对,防多账号串号。
_expect = os.environ.get("EXPECT_ACCOUNT")
if _expect or not os.environ.get("GLUE_CATALOG_ID"):
    import boto3                                          # noqa: E402
    _acct = boto3.client("sts").get_caller_identity()["Account"]
    if _expect and _acct != _expect:
        raise SystemExit(f"✗ 当前凭证账号 {_acct},期望 {_expect}(.env.local 里钉的)")
    _bucket = os.environ.get("S3_TABLE_BUCKET", "analytics-agent-tables")
    os.environ.setdefault("GLUE_CATALOG_ID",
                          f"{_acct}:s3tablescatalog/{_bucket}")

sys.path.insert(0, str(ROOT / "backend"))

# 最低期望值:低于这些就说明查漏了,不该发布。
MIN_TABLES = 40

# 行数那道闸原来写的是 `MIN_ROWS = 50_000_000`——绑死在 v2 那批数据上。重新生成一次
# 数据(这批是 19 万行明细)它就红,而代码没问题。这跟 test_all.sh 把 GMV 写成
# 149685621.44 是同一个毛病:把**数据的规模**当成了**代码的正确性**。
#
# 它真正要挡的是"行数查漏了"。换到 Athena 之后这个风险还变大了:行数是 48 条独立
# 查询,`catalog._row_counts` 对单张失败是容忍的(少一个数字比整块消失好),所以
# 完全可能只回来 40 张的行数而接口照样 200。绑数量级看不出这种残缺,覆盖率能。
MIN_ROW_COVERAGE = 0.95


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "web" / "catalog.json"))
    ap.add_argument("--print", action="store_true", help="只打印摘要，不写文件")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="允许写出非 Glue 来源或规模不足的快照（默认拒绝）")
    args = ap.parse_args()

    try:
        import catalog
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 导入 backend/catalog.py 失败: {type(e).__name__}: {e}")
        print("  依赖装在 backend/.venv,用它跑:")
        print("  ./backend/.venv/bin/python scripts/deploy/build_catalog_json.py")
        return 1

    t0 = time.time()
    print(f"查 Glue catalog {os.environ['GLUE_CATALOG_ID']} "
          f"({os.environ['AWS_REGION']})…")
    try:
        data = catalog.build(refresh=True)
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 组装失败: {type(e).__name__}: {e}")
        return 1

    tot = data.get("totals") or {}
    tables, rows = tot.get("tables") or 0, tot.get("rows") or 0
    src = data.get("source")
    # 有几张表拿到了行数。UI 上"没有行数"和"0 行"长得几乎一样,所以这个数要打出来。
    with_rows = sum(1 for dm in data.get("domains") or []
                    for x in dm.get("tables") or [] if x.get("rows") is not None)
    tm = data.get("timings") or {}
    print(f"  来源={src} 引擎={data.get('engine')} "
          f"表={tables} 行={rows:,} 域={tot.get('domains')} "
          f"行数覆盖={with_rows}/{tables} "
          f"耗时 {time.time() - t0:.1f}s"
          + (f"(其中行数 {tm['row_counts_ms'] / 1000:.1f}s)"
             if tm.get("row_counts_ms") else ""))
    if data.get("warning"):
        print(f"  降级原因: {data['warning']}")

    problems = []
    if src != "glue":
        problems.append(f"元数据来源是 {src},不是 glue(Glue 查询失败后的降级路径)")
    # catalog.py 会把"表清单和行数来自两个不同的库"这种混搭写进 warning。
    # 快照会被固化给所有访客看,所以这里当硬错误处理而不只是打一行。
    if data.get("warning") and "两个不同的库" in data["warning"]:
        problems.append(f"元数据混搭: {data['warning']}")
    if tables < MIN_TABLES:
        problems.append(f"只查到 {tables} 张表,低于最低期望 {MIN_TABLES}")
    if tables and with_rows / tables < MIN_ROW_COVERAGE:
        problems.append(
            f"只有 {with_rows}/{tables} 张表拿到行数"
            f"(低于 {MIN_ROW_COVERAGE:.0%}),部分 count(*) 查询失败了")
    if rows <= 0:
        problems.append("base 层合计 0 行:行数整块查不到,不是空库就是查询全挂了")

    if problems:
        print("\n✗ 快照不合格,拒绝写出:")
        for p in problems:
            print(f"   - {p}")
        if not args.allow_degraded:
            print("\n  推上线等于把错的元数据固化给所有访客,且界面不会报警。")
            print("  先修数据源;确实要发降级版本再加 --allow-degraded。")
            return 1
        print("\n  --allow-degraded:仍然写出。UI 会显示降级来源。")

    # snapshot 让前端能诚实区分"部署期快照"与"实时接口",别把快照说成实时。
    data["snapshot"] = True
    data["generated_at_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"

    # catalog_id 掐掉账号前缀:这个文件会提交进公开仓,而且线上是 CloudFront 直出的
    # 公开静态文件(静态路径不过 Cognito,任何人都能 GET /catalog.json)。
    # 前端不读这个字段(只用 totals/domains/governance),留 catalog 名纯粹是溯源。
    if data.get("catalog_id") and ":" in data["catalog_id"]:
        data["catalog_id"] = data["catalog_id"].split(":", 1)[1]

    # 账号号码同理,但 catalog_id 那一行只掐掉了一处。治理层接上之后
    # `governance.role` 是个完整的 IAM ARN,里面就带着账号——手工掐一个字段的写法
    # 挡不住下一个带 ARN 的字段(比如 governance.error 里的报错原文)。所以在写盘
    # 前统一扫一遍:凡是 ARN 里的 12 位账号一律换成占位符。
    n_red = 0

    def redact(x):
        nonlocal n_red
        if isinstance(x, str):
            y = re.sub(r"(?<=arn:aws:)([a-z0-9-]*:[a-z0-9-]*:)\d{12}(?=:)",
                       r"\1<账号>", x)
            n_red += y != x
            return y
        if isinstance(x, dict):
            return {k: redact(v) for k, v in x.items()}
        if isinstance(x, list):
            return [redact(v) for v in x]
        return x

    data = redact(data)
    if n_red:
        print(f"  已把 {n_red} 处 ARN 里的账号号码换成占位符(这个文件是公开静态资源)")

    if args.print:
        print("\n（--print:未写文件）")
        return 0

    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✓ 已写 {out.relative_to(ROOT)}  "
          f"{out.stat().st_size / 1024:.1f} KB  ({data['generated_at_utc']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
