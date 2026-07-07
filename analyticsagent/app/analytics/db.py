"""只读数据库访问层。

所有 NL→SQL 产生的查询都经 run_query 执行，强制：单条语句、只读、超时、行数上限。
这是 agent 的 SQL 安全边界。
"""
from __future__ import annotations

import os
import re
import time
import asyncio
import datetime as _dt
import decimal
import uuid
from typing import Any

import logging

import psycopg

log = logging.getLogger("db")

# 连接参数**惰性**读取(调用时才读 env),而非 import 时冻结——彻底消除「db 早于
# runtime_config 被 import 时 PGHOST 还没就位」的冷启动竞态。
def _pg() -> dict:
    return {
        "host": os.getenv("PGHOST", "127.0.0.1"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "app_analytics"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", ""),
    }


CONNECT_TIMEOUT = int(os.getenv("PG_CONNECT_TIMEOUT", "15"))
MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "1000"))
STMT_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "15000"))


def _connect():
    """建连;失败时把目标 host/port 与真实错误打进日志(否则只被上层吞成模型文本)。"""
    p = _pg()
    try:
        return psycopg.connect(**p, connect_timeout=CONNECT_TIMEOUT)
    except Exception as e:
        log.error("DB connect failed host=%s port=%s db=%s user=%s: %s: %s",
                  p["host"], p["port"], p["dbname"], p["user"], type(e).__name__, e)
        raise

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|comment|merge|call|do|set|begin|commit)\b",
    re.IGNORECASE,
)


class SqlError(Exception):
    pass


def _strip(sql: str) -> str:
    # 去掉行/块注释与首尾空白、尾分号
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";").strip()


def validate(sql: str) -> str:
    s = _strip(sql)
    if not s:
        raise SqlError("空查询")
    if ";" in s:
        raise SqlError("只允许单条语句")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise SqlError("只允许 SELECT / WITH 查询")
    if _FORBIDDEN.search(s):
        raise SqlError("检测到非只读关键字，已拒绝")
    return s


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (_dt.timedelta,)):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, memoryview):
        return v.tobytes().decode("utf-8", "replace")
    return v


def _run_query_sync(sql: str) -> dict:
    clean = validate(sql)
    with _connect() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            # statement_timeout 用 set_config 参数化下发（STMT_TIMEOUT_MS 本就是内部 int
            # 常量、非用户输入，但参数化可彻底消除 SQL 字符串拼接的静态告警）。
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (str(int(STMT_TIMEOUT_MS)),))
            # 只计 execute+fetch 的纯 DB 耗时（不含建连/校验），用于前端把"SQL 真正
            # 执行时间"与"模型推理时间"拆开显示——这俩以前被混计在一个 stage 里。
            t0 = time.perf_counter()
            cur.execute(clean)
            cols = [d.name for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(MAX_ROWS + 1)
            exec_ms = round((time.perf_counter() - t0) * 1000, 1)
            truncated = len(fetched) > MAX_ROWS
            rows = [[_jsonable(c) for c in r] for r in fetched[:MAX_ROWS]]
    return {"columns": cols, "rows": rows, "rowcount": len(rows),
            "truncated": truncated, "exec_ms": exec_ms}


async def run_query(sql: str) -> dict:
    return await asyncio.to_thread(_run_query_sync, sql)


def _get_schema_sync(tables: list[str]) -> str:
    if not tables:
        return "（未指定表名）"
    safe = [t for t in tables if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t)]
    if not safe:
        return "（无合法表名）"
    out: list[str] = []
    with _connect() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            for t in safe:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (t,),
                )
                rows = cur.fetchall()
                if not rows:
                    out.append(f"## {t}\n  （表不存在）")
                    continue
                lines = [f"## {t}"]
                for name, dtype, nullable in rows:
                    null = "" if nullable == "YES" else " NOT NULL"
                    lines.append(f"  - {name}: {dtype}{null}")
                out.append("\n".join(lines))
    return "\n\n".join(out)


async def get_schema(tables: list[str]) -> str:
    return await asyncio.to_thread(_get_schema_sync, tables)


def ping() -> bool:
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False
