#!/usr/bin/env python3
"""Redshift Data API 客户端 —— 建表 / COPY / 查询 的统一入口。

## 为什么走 Data API 而不是 psycopg 直连

Data API 是 HTTPS + IAM 的 AWS API，不是数据库连接。带来三件事：
- AgentCore Runtime **不需要进 VPC**，也不需要连接池
- 凭证不落地：要么用 Secrets Manager 托管的密钥，要么用 IAM 临时凭证
  （serverless 下库用户直接由 IAM 身份派生，形如 `IAM:alice`）
- workgroup 保持 `publiclyAccessible=false`，没有任何入站端口

代价是它**异步**：ExecuteStatement 提交 → DescribeStatement 轮询 → GetStatementResult 取结果。
本模块把这三步包成同步调用。

## 认证两种模式

- `--secret`（或 REDSHIFT_SECRET_ARN）：用管理员密钥。建表/COPY/GRANT 这类运维操作用它。
- 不给 secret：走 IAM 临时凭证，库用户由调用者的 IAM 身份派生。agent 运行时用它。

## 每次调用默认是**独立会话**，`SET` 不会留下来

这一条是实测出来的，而且踩过：`SET enable_result_cache_for_session TO off` 单独发一次，
紧接着 `SHOW enable_result_cache_for_session` 回来还是 `on`。ExecuteStatement 之间不
共享会话，所以任何会话级设置发完就没了。这比设不上更坏——基准脚本会记录
「结果缓存已关」，而缓存其实开着，于是重复查询的耗时凭空好看一截，且日志说它是干净的。

要让 `SET` 留住必须显式要一个会话：第一次调用带 `SessionKeepAliveSeconds`，
从 DescribeStatement 拿回 `SessionId`，后续调用**只带 `SessionId`**
（带了它就不能再带 WorkgroupName / Database / SecretArn，互斥）。`Client(keepalive=N)`
就是这件事，`scripts/bench/arms.py` 计时前依赖它。

会话是有代价的：它在 Redshift 侧占一个连接，`keepalive` 秒内不释放。所以默认不开，
只有需要跨语句保持状态的场景（计时、临时表）才开。
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time

import boto3

# us-west-2 而不是 v2 时期的 ap-northeast-1：这一条不是跟随项目搬家，是硬约束。
# Parquet 的 COPY 走 Spectrum，而 Spectrum 不接受 REGION 参数（见 load_from_s3.py
# 的坑 2），桶必须与 Redshift 同区。数据在 s3://analytics-agent-raw（us-west-2），
# 所以这一条 arm 只能在 us-west-2。
REGION = os.environ.get("AWS_REGION", "us-west-2")
WORKGROUP = os.environ.get("REDSHIFT_WORKGROUP", "analytics-agent-wg")
DATABASE = os.environ.get("REDSHIFT_DATABASE", "app_analytics")
SECRET_ARN = os.environ.get("REDSHIFT_SECRET_ARN", "")

# 轮询节奏。日常用宽一点（少打 Data API），**计时基准要调窄**：这个间隔整体
# 加到客户端墙上时钟上。实测：一条引擎自报 109ms 的查询，默认 0.4s 起步下
# 墙上时钟量到 1293ms——那个数里七成是这里的 sleep，不是 Redshift。
# 见 scripts/bench/timing.py。
POLL_INITIAL = float(os.environ.get("REDSHIFT_POLL_INITIAL", "0.4"))
POLL_MAX = float(os.environ.get("REDSHIFT_POLL_MAX", "3.0"))


class RedshiftError(RuntimeError):
    pass


class Client:
    def __init__(self, workgroup: str = WORKGROUP, database: str = DATABASE,
                 secret_arn: str = SECRET_ARN, region: str = REGION,
                 keepalive: int = 0):
        """`keepalive > 0` 时所有语句跑在同一个会话里，会话级 `SET` 因此能留住。

        见模块 docstring：不开会话的话 `SET` 发完就没了，而且不报错。
        """
        self.workgroup, self.database, self.secret_arn = workgroup, database, secret_arn
        self.keepalive = keepalive
        self.session_id = ""
        self._c = boto3.client("redshift-data", region_name=region)

    def _kw(self) -> dict:
        """本次调用的定位参数。会话建起来之后**只能**带 SessionId，其余互斥。"""
        if self.session_id:
            return {"SessionId": self.session_id}
        kw = {"WorkgroupName": self.workgroup, "Database": self.database}
        if self.secret_arn:
            kw["SecretArn"] = self.secret_arn
        if self.keepalive:
            kw["SessionKeepAliveSeconds"] = int(self.keepalive)
        return kw

    def execute(self, sql: str, timeout: float = 1800.0, fetch: bool = True) -> dict:
        """执行一条 SQL，同步等到结束。fetch=True 且有结果集时返回 columns/rows。"""
        sid = self._c.execute_statement(Sql=sql, **self._kw())["Id"]
        return self._wait(sid, sql, timeout, fetch)

    def execute_many(self, sqls: list[str], timeout: float = 3600.0) -> None:
        """按顺序执行多条（BatchExecuteStatement，同一事务提交）。"""
        sid = self._c.batch_execute_statement(Sqls=sqls, **self._kw())["Id"]
        self._wait(sid, sqls[0] if sqls else "", timeout, fetch=False)

    def session_setting(self, name: str) -> str:
        """回读一个会话级设置的**实际生效值**。

        存在的理由是别把「我发了 SET」当成「设置生效了」。不开会话时这两件事不等价，
        而不等价的那一侧不报错。
        """
        r = self.execute(f"SHOW {name}")
        rows = r.get("rows") or []
        return str(rows[0][0]) if rows and rows[0] else ""

    def _wait(self, sid: str, sql: str, timeout: float, fetch: bool) -> dict:
        t0, delay = time.time(), POLL_INITIAL
        while True:
            d = self._c.describe_statement(Id=sid)
            st = d["Status"]
            if st in ("FINISHED", "FAILED", "ABORTED"):
                break
            if time.time() - t0 > timeout:
                raise RedshiftError(f"超时 {timeout}s：{sql[:120]}")
            time.sleep(delay)
            delay = min(delay * 1.6, POLL_MAX)
        # 会话 id 只在第一次调用的响应里出现，之后要靠它继续。放在状态检查之前记，
        # 因为一条语句失败不代表会话没建起来。
        if self.keepalive and not self.session_id and d.get("SessionId"):
            self.session_id = d["SessionId"]
        if st != "FINISHED":
            raise RedshiftError(f"{st}: {d.get('Error', '(无错误信息)')}\nSQL: {sql[:400]}")
        out = {"elapsed_ms": d.get("Duration", 0) // 1_000_000,
               "rows_affected": d.get("ResultRows", -1),
               "session_id": self.session_id}
        if fetch and d.get("HasResultSet"):
            out.update(self._results(sid))
        return out

    def _results(self, sid: str) -> dict:
        cols: list[str] = []
        types: list[str] = []
        rows: list[list] = []
        tok = None
        while True:
            kw = {"Id": sid}
            if tok:
                kw["NextToken"] = tok
            r = self._c.get_statement_result(**kw)
            if not cols:
                cols = [c["name"] for c in r["ColumnMetadata"]]
                types = [str(c.get("typeName", "")).lower() for c in r["ColumnMetadata"]]
            for rec in r["Records"]:
                rows.append([_scalar(f, types[i] if i < len(types) else "")
                             for i, f in enumerate(rec)])
            tok = r.get("NextToken")
            if not tok:
                break
        return {"columns": cols, "rows": rows, "rowcount": len(rows),
                "column_types": types}


# Data API 把这些类型统统装在 stringValue 里，必须按列元数据还原成 Python 数值。
_NUMERIC_TYPES = {"numeric", "decimal"}
_TS_TYPES = {"timestamp", "timestamptz", "timestamp without time zone",
             "timestamp with time zone"}


def _scalar(field: dict, type_name: str = ""):
    """把 Data API 的字段还原成与 psycopg 路径**同类型**的 Python 值。

    这是必须做的 parity 修复，不是锦上添花：Data API 对 DECIMAL/NUMERIC 一律回
    `stringValue`（如 `"55679342.35"`），而 psycopg 回 `Decimal` 并在 db.py 里转成
    float。不还原的话，上层拿到的是字符串——eval 的数值提取直接漏掉，表现为
    「SQL 完全正确但判定失败」，极难排查（我们在 21 题里踩掉了 5 题）。

    时间戳同理：Data API 回 `'2026-01-24 00:00:00.0'`（空格分隔、带 .0），
    psycopg 回 datetime 后被格式成 isoformat。这里统一成 isoformat 的 `T` 形式。
    """
    if field.get("isNull"):
        return None
    if "stringValue" in field:
        s = field["stringValue"]
        if type_name in _NUMERIC_TYPES:
            try:
                return float(s)
            except (TypeError, ValueError):
                return s
        if type_name in _TS_TYPES and isinstance(s, str):
            t = s.replace(" ", "T", 1)
            if t.endswith(".0"):
                t = t[:-2]
            return t
        return s
    for k in ("longValue", "doubleValue", "booleanValue", "blobValue"):
        if k in field:
            return field[k]
    return None


def split_statements(sql: str) -> list[str]:
    """按分号拆分 SQL，但**尊重字符串字面量、标识符引号和注释**。

    朴素的 `sql.split(";")` 会在这类语句上炸掉：

        COMMENT ON TABLE t IS '粒度=dt;归因=last_touch';

    注释文本里的 ASCII 分号被当成语句结束符，字符串被截断，报
    "Unterminated string literal"。这里做一遍字符级扫描：单引号内（含 '' 转义）、
    双引号标识符内、`--` 行注释和 `/* */` 块注释内的分号都不算分隔符。
    """
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    in_s = in_d = in_line = in_block = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line:
            cur.append(ch)
            if ch == "\n":
                in_line = False
        elif in_block:
            cur.append(ch)
            if ch == "*" and nxt == "/":
                cur.append(nxt)
                i += 1
                in_block = False
        elif in_s:
            cur.append(ch)
            if ch == "'":
                if nxt == "'":          # '' 是转义的单引号，不结束字面量
                    cur.append(nxt)
                    i += 1
                else:
                    in_s = False
        elif in_d:
            cur.append(ch)
            if ch == '"':
                in_d = False
        elif ch == "-" and nxt == "-":
            cur.append(ch)
            in_line = True
        elif ch == "/" and nxt == "*":
            cur.append(ch)
            in_block = True
        elif ch == "'":
            cur.append(ch)
            in_s = True
        elif ch == '"':
            cur.append(ch)
            in_d = True
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))

    # 丢掉纯注释/空白的片段
    res = []
    for s in out:
        body = re.sub(r"--[^\n]*", "", s)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL).strip()
        if body:
            res.append(s.strip())
    return res


# ---------------------------------------------------------------- CLI

def _print_table(res: dict) -> None:
    cols, rows = res.get("columns", []), res.get("rows", [])
    if not cols:
        print(f"(无结果集)  耗时 {res.get('elapsed_ms', 0)}ms  "
              f"影响行数 {res.get('rows_affected')}")
        return
    w = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
         for i, c in enumerate(cols)]
    print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * w[i] for i in range(len(cols))))
    for r in rows[:200]:
        print("  ".join(str(v).ljust(w[i]) for i, v in enumerate(r)))
    print(f"\n{len(rows)} 行  耗时 {res.get('elapsed_ms', 0)}ms")


def main() -> int:
    ap = argparse.ArgumentParser(description="Redshift Data API 客户端")
    ap.add_argument("sql", nargs="?", help="要执行的 SQL；省略则从 --file 读")
    ap.add_argument("--file", help="从文件读 SQL（按 ; 拆成多条顺序执行）")
    ap.add_argument("--workgroup", default=WORKGROUP)
    ap.add_argument("--database", default=DATABASE)
    ap.add_argument("--secret", default=SECRET_ARN)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--tolerate", help="错误信息匹配该正则时继续执行（用于幂等语句，"
                                       "如 'already exists|does not exist'）。"
                                       "不匹配的错误仍然中断。")
    a = ap.parse_args()

    c = Client(a.workgroup, a.database, a.secret)
    if a.file:
        stmts = split_statements(open(a.file, encoding="utf-8").read())
        print(f"{len(stmts)} 条语句")
        for i, s in enumerate(stmts, 1):
            head = " ".join(s.split())[:70]
            try:
                r = c.execute(s, timeout=a.timeout, fetch=False)
                print(f"  [{i}/{len(stmts)}] ok   {head}  ({r.get('elapsed_ms',0)}ms)")
            except RedshiftError as e:
                if a.tolerate and re.search(a.tolerate, str(e), re.IGNORECASE):
                    print(f"  [{i}/{len(stmts)}] skip {head}  （已容忍：匹配 --tolerate）")
                    continue
                print(f"  [{i}/{len(stmts)}] FAIL {head}\n      {e}")
                return 1
        return 0

    if not a.sql:
        ap.error("需要 sql 或 --file")
    res = c.execute(a.sql, timeout=a.timeout)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_table(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
