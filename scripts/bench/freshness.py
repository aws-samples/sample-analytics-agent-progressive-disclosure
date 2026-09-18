#!/usr/bin/env python3
"""三 arm 新鲜度对账：三份物化到底是不是同一代数据。

## 它拦的是哪个坑

2026-09-02 踩过一次：重新生成了 `s3://analytics-agent-raw/parquet/`，Redshift 从新
parquet 重灌了，而 Athena 与 DuckDB 共用的那份 S3 Tables / Iceberg 拷贝**没有跟着灌**。
于是三条 arm 读的是两代数据。

`correctness.py` 的表级闸门**结构上看不见这件事**：生成器 seed 固定，所以行数、数值
求和、时间边界、布尔计数在两代之间逐位相同，只有五个文本列的值不同。只有
`query_correctness.py --values` 抓到了。也就是说，再往表级闸门里加数据检查是没用的
——要拦的不是「某几列的值不对」，而是「两份物化不同代」这个类别。

所以这个脚本一条数据都不查。它比的是每个存储自己记的**代次**，三边各一份，
都能不扫数据就拿到。

## 三个存储各自能给什么

| 存储 | 谁在读 | 代次记录 | 从哪儿拿 |
|---|---|---|---|
| S3 parquet | 上游真源 | 对象 ETag / 字节数 / 个数 / 最后修改 | `ListObjectsV2` |
| S3 Tables Iceberg | `athena`、`duckdb` | 当前快照 id / 提交时间 / `total-records` | 直读 `metadata.json` |
| Redshift 托管存储 | `redshift` | COPY 结束时间 / `loaded_rows` / `source_file_bytes` | `sys_load_history` |

三处都是元数据读取：不跑 Athena 查询（不产生扫描费用），不扫 parquet，不 `COUNT(*)`。
一轮 35 张表大约四十次 API 调用。

## 两个坑，都是实测出来的，都会让判据失效

**一、别用 `s3tables get-table` 的 `modifiedAt` 当数据提交时间。**
`orders` 这张表 `modifiedAt` 是 2026-09-06T23:47，而它当前快照的时间戳是
2026-09-02T09:18 —— 中间那次前进没有人写数据，是 S3 Tables 的自动维护
（compaction 会产生新快照、推进表的元数据序号）。拿 `modifiedAt` 当「数据什么时候
更新的」会把维护动作读成一次装载，方向恰好是**漏报**：表看起来比实际更新。
所以这里一律读 `metadata.json` 里当前快照的 `timestamp-ms`。

**二、别用时间戳先后当陈旧判据。** 直觉上「Iceberg 快照早于 parquet 最后修改 →
Iceberg 陈旧」，实测在 35 张表里误报 32 张：那 32 张的 parquet 在 09-02 被重传过
（`LastModified` 前进），但内容没变，所以 09-01 灌进 Iceberg 的那一份仍然是对的。
S3 的 `LastModified` 在重传相同字节时也会前进，它记的是「什么时候写的」不是
「写的是什么」。

内容判据要用 **ETag**：它由内容派生，相同字节重传得到相同 ETag。
（多段上传的 ETag 是 `<md5>-<段数>` 而非内容 MD5，但对同一份内容加同样的分段方式
仍然稳定，做漂移检测够用。）

## 于是判据分三档，强度不同，报的时候不混在一起

**档一 · 无状态硬判据（不需要基线，任何时候都能跑）**

- `iceberg.total-records == redshift.loaded_rows == 装载快照行数`
  三方行数。Iceberg 侧来自快照 summary，Redshift 侧来自 COPY 记录，真源来自
  `data/loaded_row_counts.json`。任意两个不等 → 有一份是别的代次。
- `parquet 字节数 == redshift.source_file_bytes` 且 `个数 == source_file_count`
  这条**精确**证明 Redshift 读的就是现在这份 parquet。字节级，不是时间级。

**档二 · 基线漂移（需要 `--pin` 过一次）**

`data/bench/freshness_baseline.json` 钉住 golden 被验证通过的那一刻三个存储各自的
代次记录。之后任何一个存储自己动了、而别的没动，都会被点名。这一档才是真正覆盖
「两份物化不同代」的那一档：三边都记了代次，**至多只有一个能悄悄前进而不被发现**，
而事故恰恰是「只灌了一个」。

档一抓不到事故的原因就在这儿：行数和字节数在那次事故里全都是对的。

**档三 · 时间线线索（只出汇总，永不判失败）**

Iceberg 快照早于 parquet 最后修改时记一条。留着它是因为它偶尔确实指向真问题，
不让它判失败是因为上面坑二说的误报率——以及反方向也不成立：compaction 会把快照
时间推到 parquet 之后，看起来新鲜，其实数据一代都没变。所以它只出一行汇总，
逐条留在 trace 里：这一档几乎每张表都会响，itemize 出来会把上面两段淹掉，
而被淹掉的那两段才是要人去看的。

## 什么时候报「未知」而不是「通过」

两种表没有档一的输入，措辞必须分开，因为处置完全不同：

- **派生表**（parquet 前缀下没有对象）：`dwd_*` / `dws_*` / `mart_*` / `fin_*` /
  `growth_*` / `meta_snapshot` / `orders_backup_*` / `tmp_*` 这些是 Athena CTAS 在
  Iceberg 里直接建的，上游根本不是 parquet，Redshift 侧也不是 COPY
  （`sys_load_history` 只记 COPY，不记 CTAS）。这是**天然管不到**，它们的代次只能
  靠基线那一档看。
- **`sys_load_history` 滚掉**：有 parquet 源却查不到 COPY 记录。这是**本该有的信号丢了**。

两种都报「未知」，都不算通过。总结行也按这个分类报覆盖面——把没比过的表算进
「三方同代」是虚报，而虚报覆盖面恰好会让人以为这些表已经被盯着了。

用法：

    python3 scripts/bench/freshness.py                     # 对账（有基线就连基线一起比）
    python3 scripts/bench/freshness.py --pin               # 把当前三方代次钉成基线
    python3 scripts/bench/freshness.py --no-baseline       # 只跑无状态硬判据
    python3 scripts/bench/freshness.py -t orders -t users
    python3 scripts/bench/freshness.py --json
    python3 scripts/bench/freshness.py --selftest          # 判定逻辑自测，不连任何服务
    python3 scripts/bench/freshness.py --baseline <路径>   # 换一份基线（见下）

退出码：0 通过 / 1 硬判据不符 / 2 用法或前置条件问题（含基线缺失）。

## 怎么证明基线漂移那一档真的会响

这一档是唯一能覆盖「两份物化不同代」的判据，而它平时**永远是绿的**——一个永远
绿的检查和一个没接线的检查，从输出上分不出来。所以它必须能被主动点着看一次：

    cp data/bench/freshness_baseline.json /tmp/tampered.json
    # 改 /tmp/tampered.json 里某张表的 iceberg.snapshot_id 或 parquet.etag
    python3 scripts/bench/freshness.py --baseline /tmp/tampered.json -t <那张表>

期望是退出码 1，并且报出来的那一条**点到具体哪个存储**。实测过一次三个存储各篡
一张表、另留一张不动：三张各报一条、点名分别是 iceberg / parquet / redshift，
没动的那张仍是绿的。

这么做的直接收获是抓到了一个 bug：`--baseline` 原先只接在写基线那一支上，读的时候
被静默忽略，于是指一份篡改过的基线它会去读默认那份**然后报通过**。自测第 11 例
现在钉住这条接线（走 `main()`，不走 `check()`，因为坏的是接线不是判定）。

环境变量：`AWS_REGION`、`REDSHIFT_SECRET_ARN`、`S3_TABLE_BUCKET`、`ICEBERG_NAMESPACE`、
`RAW_BUCKET`、`BENCH_TRACE_S3`（在 Fargate 上跑必须设，否则 trace 随容器消失）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

# 这一行是为了它的副作用：`arms` 在模块层把 scripts/lakehouse、scripts/duckdb、
# scripts/redshift 插到 sys.path 上，下面 `import athena` / `import rsql` 靠的是它。
# 它本身只改 sys.path，不建任何连接（见 arms.py 开头），所以放在模块层是安全的。
import arms as _sys_path_side_effect  # noqa: E402,F401

TRACE_DIR = ROOT / "data" / "bench"
BASELINE = TRACE_DIR / "freshness_baseline.json"

#: parquet 前缀的约定写法。**只在 Redshift 侧查不到 COPY 记录时**用它兜底：
#: 能查到的时候一律用 `sys_load_history.data_source` 里那个前缀，因为那才是
#: Redshift 真正读过的位置。写死约定的风险是约定改了这里不知道，而不知道的表现是
#: 「parquet 侧 0 个对象」——那个信息看起来像桶空了，不像前缀猜错了。
PARQUET_PREFIX = "parquet/{table}/"


# ---------------------------------------------------------------- trace

TRACE_S3 = os.environ.get("BENCH_TRACE_S3", "").rstrip("/")


def upload_trace(path: Path) -> str:
    """把 trace 传到 `TRACE_S3`，返回落地 URI（没配就空串）。

    与 `correctness.py::upload_trace` 同口径：上传失败**不改结论**（判的是数据代次，
    不是管道通不通），但要吵一声。没有 import 那一份是因为 `correctness.py` 在模块
    层就把闸门的参数解析和判定全带进来了，为一个十行函数拉进来不值当。
    """
    if not TRACE_S3:
        return ""
    bucket, _, prefix = TRACE_S3.removeprefix("s3://").partition("/")
    key = f"{prefix.rstrip('/')}/{path.name}" if prefix else path.name
    try:
        import boto3
        boto3.client("s3", region_name=os.environ.get("AWS_REGION")).upload_file(
            str(path), bucket, key)
        return f"s3://{bucket}/{key}"
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️  trace 传不上 {TRACE_S3}：{e}\n"
              f"      对账结论不受影响，但这一轮的记录只在容器里。")
        return ""


# ---------------------------------------------------------------- 采集

def parquet_gen(s3, bucket: str, prefix: str) -> dict:
    """parquet 前缀的代次记录。

    `etag` 是**内容**指纹（排序后的 ETag 列表取 sha256），`last_modified` 是写入时间。
    两个都记、但判据只用前者：见模块 docstring 坑二，重传相同字节会推进后者。
    """
    etags: list[str] = []
    total = 0
    newest = None
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            etags.append(o["ETag"].strip('"'))
            total += o["Size"]
            if newest is None or o["LastModified"] > newest:
                newest = o["LastModified"]
    return {
        "uri": f"s3://{bucket}/{prefix}",
        "files": len(etags),
        "bytes": total,
        "etag": hashlib.sha256("".join(sorted(etags)).encode()).hexdigest()[:16]
                if etags else None,
        "last_modified": newest.isoformat() if newest else None,
    }


def iceberg_gen(s3t, s3, bucket_arn: str, namespace: str, table: str) -> dict:
    """Iceberg 表的代次记录：当前快照的 id、提交时间、行数、文件数。

    时间取 `metadata.json` 里当前快照的 `timestamp-ms`，**不是** `get_table` 的
    `modifiedAt`：后者会因 compaction 前进（见模块 docstring 坑一）。
    """
    d = s3t.get_table(tableBucketARN=bucket_arn, namespace=namespace, name=table)
    b, _, k = d["metadataLocation"].removeprefix("s3://").partition("/")
    meta = json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())
    cur = meta.get("current-snapshot-id")
    snap = next((s for s in meta.get("snapshots", [])
                 if s["snapshot-id"] == cur), None)
    summary = (snap or {}).get("summary", {})
    ms = (snap or {}).get("timestamp-ms")
    return {
        "snapshot_id": cur,
        "snapshots": len(meta.get("snapshots", [])),
        "committed_at": datetime.fromtimestamp(
            ms / 1000, timezone.utc).isoformat() if ms else None,
        "rows": int(summary["total-records"]) if "total-records" in summary else None,
        "files": int(summary["total-data-files"])
                 if "total-data-files" in summary else None,
        # Iceberg 自己重写过文件，这个字节数**与 parquet 源不可比**，只用于基线漂移
        "bytes": int(summary["total-files-size"])
                 if "total-files-size" in summary else None,
        # 维护动作推进的那个时间。记下来是为了让「快照没动但表动了」可辨认，不参与判据。
        "table_modified_at": d["modifiedAt"].isoformat() if d.get("modifiedAt") else None,
    }


def redshift_gens(client) -> dict[str, dict]:
    """一次查完所有表的 COPY 记录。表名 → 代次记录。

    一张表可能有多条（多次 COPY），取 `end_time` 最新那条。查不到的表不进这个 dict，
    调用方据此报「未知」而不是「通过」。
    """
    r = client.execute(
        "SELECT table_name, end_time, loaded_rows, loaded_bytes, "
        "       source_file_bytes, source_file_count, data_source, status "
        "FROM sys_load_history")
    out: dict[str, dict] = {}
    for name, end, rows, lbytes, sbytes, sfiles, src, status in r["rows"]:
        rec = {"loaded_at": end, "rows": rows, "stored_bytes": lbytes,
               "source_bytes": sbytes, "source_files": sfiles,
               "source": src, "status": status}
        if name not in out or str(end) > str(out[name]["loaded_at"]):
            out[name] = rec
    return out


# ---------------------------------------------------------------- 判定

def check(table: str, snap_rows: int | None, pq: dict, ice: dict,
          rs: dict | None, base: dict | None) -> dict:
    """判一张表。返回 `{hard, notes, hints, scope}`。

    三个清单分开是刻意的，它们的处置完全不同：

    - `hard` 判失败。
    - `notes` 是**这一轮没能判**的事（信号缺失、不适用）。不判失败，但必须显式说
      「未知」——信号过期时静默变绿正是这个脚本要修的那类毛病。
    - `hints` 是时间线线索，误报率高（见模块 docstring 坑二，实测 35 张里误报 32 张）。
      单独一档、只出汇总，因为它几乎每张表都会响，itemize 出来会把人训练成整段跳过。

    `scope` 说明这张表**实际比了多少**，给总结行用。声称「48 张表三方同代」而其中
    十几张根本没有第二份物化可比，是在虚报覆盖面。
    """
    hard: list[str] = []
    notes: list[str] = []
    hints: list[str] = []

    # 派生表：parquet 前缀下没有对象。这些表是 Athena CTAS 在 Iceberg 里直接建的，
    # 上游不是 S3 parquet，Redshift 侧也不是 COPY（`sys_load_history` 只记 COPY，
    # 不记 CTAS），所以档一的两条判据在它们身上**没有输入**，不是「信号滚掉了」。
    # 这两种情况要分开报：一种是这个脚本天然管不到，另一种是本该有的信号丢了。
    derived = pq["files"] == 0

    # 档一之一：三方行数
    trio = {"iceberg": ice.get("rows")}
    if rs:
        trio["redshift"] = rs.get("rows")
    if snap_rows is not None:
        trio["快照"] = snap_rows
    known = {k: v for k, v in trio.items() if v is not None}
    if len(set(known.values())) > 1:
        hard.append(f"{table} 行数三方不一致："
                    + "  ".join(f"{k}={v:,}" for k, v in known.items())
                    + "  → 至少有一份是别的代次")

    # 档一之二：parquet ↔ Redshift 字节级对齐
    if derived:
        scope = "lake_only"
        notes.append(f"{table} 派生表（{pq['uri']} 下无对象）：上游不是 parquet、"
                     f"Redshift 侧也不是 COPY，档一两条判据不适用。"
                     f"它的代次只能靠基线那一档看。")
    elif rs is None:
        scope = "partial"
        notes.append(f"{table} 有 parquet 源但 Redshift 侧查不到 COPY 记录"
                     f"（sys_load_history 保留窗口已过，或那次装载不是 COPY）："
                     f"字节对齐在这张表上是**未知**，不是通过")
    else:
        scope = "three_way"
        if pq["bytes"] != rs.get("source_bytes"):
            hard.append(f"{table} parquet 与 Redshift 读到的字节数不等："
                        f"现在 {pq['bytes']:,}，COPY 当时 {rs.get('source_bytes'):,}"
                        f"  → parquet 在 Redshift 装载之后改过，Redshift 还是旧代次")
        if pq["files"] != rs.get("source_files"):
            hard.append(f"{table} parquet 文件个数与 Redshift 读到的不等："
                        f"现在 {pq['files']}，COPY 当时 {rs.get('source_files')}")
        if rs.get("status") not in (None, "completed"):
            hard.append(f"{table} 上一次 COPY 的状态是 {rs['status']}，不是 completed")

    # 档二：基线漂移。三个存储各比自己的记录，谁动了点谁的名。
    if base:
        for store, now, keys in (
                ("parquet", pq, ("etag", "bytes", "files")),
                ("iceberg", ice, ("snapshot_id", "rows")),
                ("redshift", rs, ("loaded_at", "rows", "source_bytes"))):
            was = base.get(store) or {}
            # `now is None` 是**信号没了**，不是「值变了」。不挡住的话
            # `sys_load_history` 一滚掉，基线这一档就会报「loaded_at X → None」，
            # 把信号过期判成硬失败——上面已经按「未知」报过它了。
            if not was or now is None:
                continue
            moved = [k for k in keys
                     if k in was and was[k] != now.get(k)]
            if moved:
                hard.append(
                    f"{table} {store} 相对基线动过："
                    + "  ".join(f"{k} {was[k]} → {now.get(k)}" for k in moved)
                    + f"  → golden 是对着基线那一代验的，{store} 这条 arm 的结果不再受它保证")

    # 档三：时间线线索
    ic_t, pq_t = ice.get("committed_at"), pq.get("last_modified")
    if ic_t and pq_t and ic_t < pq_t:
        hints.append(f"{table}：Iceberg 快照 {ic_t[:19]} < parquet 最后修改 {pq_t[:19]}")
    return {"hard": hard, "notes": notes, "hints": hints, "scope": scope}


# ---------------------------------------------------------------- 主流程

def table_bucket_arn(region: str, table_bucket: str) -> str:
    """S3 Tables 表桶 ARN。口径与 `scripts/duckdb/conn.py::_arn()` 相同。"""
    if os.environ.get("S3_TABLES_ARN"):
        return os.environ["S3_TABLES_ARN"]
    import boto3
    acct = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    return f"arn:aws:s3tables:{region}:{acct}:bucket/{table_bucket}"


#: 采集的并发度。这一步是纯 IO 等待（每张表三次 AWS 元数据调用），串行跑 48 张表
#: 要 80 秒左右，那个量级做不了「每次跑闸门前先对账」的前置检查——太慢的检查会被
#: 加上跳过参数，然后就不再跑了。boto3 的 client 实例本身是线程安全的（resource 不是），
#: 所以三个 client 直接共享。并发度别调太高：ListObjectsV2 和 GetObject 都算 S3 请求，
#: 拧到几十条只会开始吃限流重试。
WORKERS = int(os.environ.get("FRESHNESS_WORKERS", "8"))


def collect(tables: list[str], region: str, tb_arn: str) -> dict[str, dict]:
    """采集三方代次。返回 表名 → {parquet, iceberg, redshift}。"""
    import boto3
    import athena as AT
    import rsql
    from concurrent.futures import ThreadPoolExecutor

    s3 = boto3.client("s3", region_name=region)
    s3t = boto3.client("s3tables", region_name=region)

    if not rsql.SECRET_ARN:
        print("  ⚠️  没有 REDSHIFT_SECRET_ARN：走 IAM 派生身份查 sys_load_history。"
              "\n      那个身份看不到别人的装载记录，查不到不等于没装载。")
    # Redshift 侧一次查完所有表，不进并发：它是一条 SQL，不是 per-table 调用。
    rs_all = redshift_gens(rsql.Client())

    def one(t: str) -> tuple[str, dict]:
        rs = rs_all.get(t)
        # 前缀优先用 Redshift 真读过的那个，见 PARQUET_PREFIX 的注释
        if rs and rs.get("source", "").startswith("s3://"):
            bkt, _, pfx = rs["source"].removeprefix("s3://").partition("/")
        else:
            bkt, pfx = AT.RAW_BUCKET, PARQUET_PREFIX.format(table=t)
        return t, {
            "parquet": parquet_gen(s3, bkt, pfx),
            "iceberg": iceberg_gen(s3t, s3, tb_arn, AT.NAMESPACE, t),
            "redshift": rs,
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # 结果按表名重排，不按完成顺序：输出顺序稳定，两轮之间才好 diff。
        return {t: g for t, g in sorted(ex.map(one, tables))}


def _short(p: Path) -> str:
    """打印用的路径：仓库内给相对路径，仓库外给绝对路径。

    `--baseline` 可以指到仓库外（反向验证就把篡改过的副本放在 /tmp），
    那时 `relative_to(ROOT)` 抛 ValueError——而它出现在**报告基线来源**这一行，
    崩在这里等于把「基线是哪一份」这个信息换成一条栈。
    """
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="三 arm 新鲜度对账（不查数据，只比代次）")
    ap.add_argument("-t", "--table", action="append", default=[])
    ap.add_argument("--pin", action="store_true",
                    help=f"把当前三方代次写成基线（{BASELINE.name}）。"
                         "只在 golden 刚验证通过时做。")
    ap.add_argument("--no-baseline", action="store_true",
                    help="跳过基线漂移这一档，只跑无状态硬判据")
    ap.add_argument("--baseline", type=Path, default=BASELINE,
                    help="换一份基线文件（默认 data/bench/freshness_baseline.json）。"
                         "用于对着历史某一次的 pin 比，或验证判定本身。")
    ap.add_argument("--json", action="store_true", help="把采集结果整份打出来")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="判定逻辑自测，不连服务")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    region = os.environ.get("AWS_REGION", "us-west-2")
    snap_path = ROOT / "data" / "loaded_row_counts.json"
    snap: dict[str, int] = {}
    if snap_path.exists():
        s = json.loads(snap_path.read_text(encoding="utf-8"))
        snap = {k: int(v) for k, v in s["tables"].items()}

    import boto3
    import athena as AT
    s3t = boto3.client("s3tables", region_name=region)
    tb_arn = table_bucket_arn(region, AT.TABLE_BUCKET)
    if a.table:
        tables = a.table
    else:
        tables = []
        for page in s3t.get_paginator("list_tables").paginate(
                tableBucketARN=tb_arn, namespace=AT.NAMESPACE):
            tables += [t["name"] for t in page["tables"]]
        tables.sort()

    print(f"表桶 {AT.TABLE_BUCKET} / 命名空间 {AT.NAMESPACE} / {len(tables)} 张表")
    print("采集三方代次（元数据读取，不扫数据、不跑 Athena 查询）…")
    t0 = time.time()
    gens = collect(tables, region, tb_arn)

    if a.json:
        print(json.dumps(gens, ensure_ascii=False, indent=2, default=str))

    if a.pin:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        a.baseline.write_text(json.dumps({
            "pinned_at": datetime.now(timezone.utc).isoformat(),
            "region": region,
            "note": "golden 验证通过时三个存储各自的代次。三边都记，"
                    "所以至多只有一个能悄悄前进而不被发现。"
                    "重新钉之前先确认 query_correctness.py --values 是绿的。",
            "tables": gens,
        }, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"\n基线已钉：{_short(a.baseline)}（{len(gens)} 张表）")
        return 0

    base: dict[str, dict] = {}
    if not a.no_baseline:
        # 一律走 `a.baseline`，不碰模块级 `BASELINE`。这里原来写死成后者，
        # 于是 `--baseline` 只对 `--pin` 生效、读的时候被静默忽略：给一份篡改过的
        # 基线，它照旧读默认那份、照旧报「通过」。**这正是加 `--baseline` 要验的东西
        # 自己坏了**，而它的坏法是报绿而不是报错——只有真去篡改一次才看得见。
        if a.baseline.exists():
            b = json.loads(a.baseline.read_text(encoding="utf-8"))
            base = b.get("tables", {})
            print(f"基线：{_short(a.baseline)}（钉于 {b.get('pinned_at','?')[:19]}）")
        else:
            print(f"\n没有基线（{_short(a.baseline)} 不存在）。")
            print("基线漂移那一档是唯一能覆盖「两份物化不同代」的判据——那次事故里行数和")
            print("字节数全是对的，只有基线能发现只灌了一个存储。先确认")
            print("`query_correctness.py --values` 是绿的，然后：")
            print("    python3 scripts/bench/freshness.py --pin")
            print("只想跑无状态那一档：加 --no-baseline")
            return 2
    print()

    trace = None
    if not a.no_trace:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tp = TRACE_DIR / f"freshness-{stamp}.jsonl"
        trace = tp.open("w", encoding="utf-8")
        trace.write(json.dumps({
            "kind": "run", "started_at": stamp, "region": region,
            "tables": len(tables), "baseline": bool(base),
            "namespace": AT.NAMESPACE, "table_bucket": AT.TABLE_BUCKET,
        }, ensure_ascii=False) + "\n")

    hard: list[str] = []
    notes: list[str] = []
    hints: list[str] = []
    scopes: dict[str, int] = {"three_way": 0, "partial": 0, "lake_only": 0}
    for t in tables:
        g = gens[t]
        r = check(t, snap.get(t), g["parquet"], g["iceberg"], g["redshift"],
                  base.get(t))
        hard += r["hard"]
        notes += r["notes"]
        hints += r["hints"]
        scopes[r["scope"]] += 1
        if trace:
            trace.write(json.dumps({"kind": "span", "table": t, **g, **r},
                                   ensure_ascii=False, default=str) + "\n")
        rows = g["iceberg"].get("rows")
        mark = {"three_way": "✅", "partial": "·", "lake_only": "·"}[r["scope"]]
        if r["hard"]:
            mark = "❌"
        note = ("三方同代" if r["scope"] == "three_way" else
                "派生表，档一不适用" if r["scope"] == "lake_only" else
                "Redshift 侧无 COPY 记录")
        print(f"  {mark} {t:<26} " + (f"{rows:,} 行  " if rows is not None else "")
              + (f"{len(r['hard'])} 项不符" if r["hard"] else note))

    elapsed = time.time() - t0
    if trace:
        trace.write(json.dumps({"kind": "summary", "tables": len(tables),
                                "hard": len(hard), "notes": len(notes),
                                "hints": len(hints), "scopes": scopes,
                                "wall_s": round(elapsed, 1)},
                               ensure_ascii=False) + "\n")
        trace.close()
        uploaded = upload_trace(tp)

    if notes:
        print(f"\n未能判定 {len(notes)} 项（不是通过，是没有输入）：")
        for n in notes[:12]:
            print(f"  - {n}")
        if len(notes) > 12:
            print(f"  …… 另有 {len(notes) - 12} 项，逐条在 trace 里")

    if hints:
        # 只出汇总。这一档实测误报率极高（35 张里 32 张），itemize 会淹掉上面两段。
        print(f"\n时间线线索 {len(hints)} 张表：Iceberg 快照早于 parquet 最后修改。"
              f"\n  **不判失败**：S3 的 LastModified 在重传相同字节时也会前进，"
              f"内容判据是 ETag（见脚本 docstring 坑二）。逐条在 trace 里。")

    print()
    if hard:
        print(f"新鲜度对账未通过 ❌  {len(hard)} 处\n")
        for h in hard[:40]:
            print(f"  - {h}")
        if len(hard) > 40:
            print(f"  …… 另有 {len(hard) - 40} 处")
        print("\n三份物化不同代时，正确性闸门的结果不成立：seed 固定，两代之间行数、"
              "数值求和、\n时间边界、布尔计数会逐位相同，差异只落在文本列上"
              "（2026-09-02 就是这么漏过去的）。")
        if trace:
            print(f"\n逐表代次在 {uploaded or tp.relative_to(ROOT)}")
        return 1

    # 覆盖面照实说：`lake_only` 和 `partial` 这两类没有第二份物化可比，把它们算进
    # 「三方同代」是虚报——而虚报覆盖面恰好会让人以为已经被盯着了。
    print(f"新鲜度对账通过 ✅  {len(tables)} 张表，其中 {scopes['three_way']} 张完成三方对账"
          + (f"，{scopes['lake_only']} 张只有 Iceberg 一份物化" if scopes["lake_only"] else "")
          + (f"，{scopes['partial']} 张缺 Redshift 装载记录" if scopes["partial"] else "")
          + ("；基线已比" if base else "；未比基线"))
    print(f"  墙钟 {elapsed:.1f}s，零扫描费用（{len(tables)} 张表约 {len(tables) * 3} 次元数据调用）")
    if not base:
        print("  ⚠️  没比基线：「只灌了一个存储、行数字节仍然对得上」这一类它抓不到——"
              "\n      2026-09-02 那次就是这个形态。跑一次 --pin 把基线钉上。")
    if trace:
        print(f"  trace：{tp.relative_to(ROOT)}"
              + (f" → {uploaded}" if uploaded else ""))
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    """判定逻辑自测：不连任何服务，只喂构造的代次 dict。

    要盯住三件事：

    1. **三个清单不能互串。** 时间线线索误报率高，串进硬判据就是长期红灯，而长期
       红灯的闸门等于没有闸门；反过来硬判据降级成线索则等于把闸门关了。
    2. **「没有输入」不能报成「通过」。** 信号缺失时静默变绿是这个脚本要修的那类毛病。
    3. **档一抓不到事故形态这件事本身要被测住。** 用例 8 特意断言「无基线时档一全绿」——
       那不是 bug，是必须记录下来的能力边界，否则以后有人会以为跑了这个脚本就够了。
    """
    bad = 0

    def PQ(**kw):
        d = {"uri": "s3://b/p/", "files": 1, "bytes": 100, "etag": "aa",
             "last_modified": "2026-09-01T00:00:00+00:00"}
        d.update(kw)
        return d

    def IC(**kw):
        d = {"snapshot_id": 1, "snapshots": 1,
             "committed_at": "2026-09-01T00:00:00+00:00", "rows": 10,
             "files": 1, "bytes": 90, "table_modified_at": None}
        d.update(kw)
        return d

    def RS(**kw):
        d = {"loaded_at": "2026-09-01T01:00:00", "rows": 10, "stored_bytes": 120,
             "source_bytes": 100, "source_files": 1, "source": "s3://b/p/",
             "status": "completed"}
        d.update(kw)
        return d

    def ck(name, got, want_sub, want_n=None):
        nonlocal bad
        if want_n is not None and len(got) != want_n:
            bad += 1
            print(f"  FAIL {name}：期望 {want_n} 条，实际 {len(got)}\n       {got}")
            return
        if want_sub and not any(want_sub in g for g in got):
            bad += 1
            print(f"  FAIL {name}：没有一条包含 {want_sub!r}\n       {got}")

    def scope_is(name, r, want):
        nonlocal bad
        if r["scope"] != want:
            bad += 1
            print(f"  FAIL {name}：scope 期望 {want}，实际 {r['scope']}")

    # 1. 全部同代 → 三个清单都空，且 scope 是完整的三方
    r = check("t", 10, PQ(), IC(), RS(), None)
    ck("三方同代 硬", r["hard"], "", 0)
    ck("三方同代 未判", r["notes"], "", 0)
    ck("三方同代 线索", r["hints"], "", 0)
    scope_is("三方同代", r, "three_way")

    # 2. Iceberg 行数与 Redshift 不等 → 硬判据一条
    r = check("t", 10, PQ(), IC(rows=9), RS(), None)
    ck("行数不一致", r["hard"], "行数三方不一致", 1)

    # 3. parquet 改过而 Redshift 没重灌 → 字节不等，硬判据
    r = check("t", 10, PQ(bytes=101), IC(), RS(), None)
    ck("字节不等", r["hard"], "字节数不等", 1)
    ck("字节不等指路", r["hard"], "Redshift 还是旧代次")

    # 4. 时间线只能进 hints。进 hard 就是长期红灯（实测 35 张里 32 张会响），
    #    进 notes 会跟「信号缺失」混在一段里，而那一段是要人去查的。
    r = check("t", 10, PQ(last_modified="2026-09-02T00:00:00+00:00"), IC(), RS(), None)
    ck("时间线 不进硬", r["hard"], "", 0)
    ck("时间线 不进未判", r["notes"], "", 0)
    ck("时间线 进线索", r["hints"], "<", 1)

    # 5. 有 parquet 源但 COPY 记录缺失 → 报未知，且必须与派生表分开
    r = check("t", 10, PQ(), IC(), None, None)
    ck("COPY 记录缺失 硬", r["hard"], "", 0)
    ck("COPY 记录缺失 未判", r["notes"], "**未知**", 1)
    scope_is("COPY 记录缺失", r, "partial")

    # 6. 派生表（parquet 前缀无对象）不能报成「信号滚掉了」——那是两回事：
    #    一种是这个脚本天然管不到，另一种是本该有的信号丢了。
    r = check("t", None, PQ(files=0, bytes=0, etag=None, last_modified=None),
              IC(), None, None)
    ck("派生表 硬", r["hard"], "", 0)
    ck("派生表 措辞", r["notes"], "派生表", 1)
    scope_is("派生表", r, "lake_only")
    if any("保留窗口" in x for x in r["notes"]):
        bad += 1
        print(f"  FAIL 派生表被报成了信号过期：{r['notes']}")

    # 7. 基线漂移：只有 iceberg 前进 → 点名 iceberg，不点别人
    b = {"parquet": PQ(), "iceberg": IC(), "redshift": RS()}
    r = check("t", 10, PQ(), IC(snapshot_id=2), RS(), b)
    ck("基线漂移点名", r["hard"], "iceberg 相对基线动过", 1)
    if any("parquet 相对基线" in x or "redshift 相对基线" in x for x in r["hard"]):
        bad += 1
        print(f"  FAIL 基线漂移点错了名：{r['hard']}")

    # 8. 事故形态复现：三边行数字节全对，只有 parquet 内容换了代（ETag 变、
    #    字节数巧合相同）。档一必须全绿，档二必须抓到——这正是 2026-09-02
    #    漏过去的那条路径，也是基线这一档存在的全部理由。
    r = check("t", 10, PQ(etag="bb"), IC(), RS(), b)
    ck("事故形态 被基线抓到", r["hard"], "parquet 相对基线动过", 1)
    r2 = check("t", 10, PQ(etag="bb"), IC(), RS(), None)
    if r2["hard"]:
        bad += 1
        print(f"  FAIL 无基线时不该报硬判据（说明档一被误当成能抓这一类）：{r2['hard']}")

    # 9. COPY 状态不是 completed → 硬判据
    r = check("t", 10, PQ(), IC(), RS(status="failed"), None)
    ck("COPY 未完成", r["hard"], "不是 completed", 1)

    # 10. 信号过期 + 有基线：不能报成「redshift 相对基线动过」。
    #     「值变了」和「值没了」是两回事，混起来的表现是 sys_load_history 一滚掉
    #     整张表就红，而红的原因指向数据不同代——完全跑偏。
    r = check("t", 10, PQ(), IC(), None, b)
    ck("信号过期不算漂移", r["hard"], "", 0)
    ck("信号过期仍报未知", r["notes"], "**未知**", 1)

    # 11. `--baseline` 真的被**读**路径用上了，而不是只对 `--pin` 生效。
    #     这一例走 `main()` 而不是 `check()`，因为坏的地方在接线不在判定：
    #     实测这个参数曾经只进了写基线那一支，读的时候被静默忽略——给一份篡改过的
    #     基线，它照旧读默认那份、照旧报「通过」。**报绿不报错**，所以上面十例
    #     一个都碰不到它，而且它坏掉的后果正是「漂移检测看起来验过了，其实没验」。
    #     只桩掉碰云的两处，判定、读文件、退出码都是真的。
    #     `main()` 的输出整段吞掉：它本来就会打一屏「未通过 ❌」，那是这一例
    #     期望的结果，但印在一次**通过**的自测中间会被读成自测失败。
    import contextlib
    import io
    import tempfile
    mod = globals()
    keep = (mod["collect"], mod["table_bucket_arn"], sys.argv)
    rc = None
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "b.json"
        bp.write_text(json.dumps({
            "pinned_at": "2026-01-01T00:00:00+00:00",
            "region": "us-west-2",
            # 基线里 parquet 的 ETag 与下面 collect() 交出来的不一样：
            # 参数接上了就必须红，没接上就会去读默认基线然后报绿。
            "tables": {"t": {"parquet": PQ(etag="OLDGEN"),
                             "iceberg": IC(), "redshift": RS()}},
        }, ensure_ascii=False, default=str), encoding="utf-8")
        try:
            mod["collect"] = lambda tables, region, tb_arn: {
                "t": {"parquet": PQ(), "iceberg": IC(), "redshift": RS()}}
            mod["table_bucket_arn"] = lambda region, tb: (
                "arn:aws:s3tables:us-west-2:000000000000:bucket/selftest")
            sys.argv = ["freshness.py", "-t", "t", "--no-trace",
                        "--baseline", str(bp)]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main()
        finally:
            mod["collect"], mod["table_bucket_arn"], sys.argv = keep
    if rc != 1:
        bad += 1
        print(f"  FAIL --baseline 指向一份对不上的基线，main() 返回 {rc}，期望 1"
              "——读路径没用上这个参数时它读的是默认基线，然后报通过")

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print("  三方同代 / 行数不一致 / 字节不等 / 时间线只进线索 / COPY 记录缺失 /"
          " 派生表不误报 / 基线点名 / 事故形态 / COPY 未完成 / 信号过期不算漂移 /"
          " --baseline 读路径接线")
    print("判定逻辑自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
