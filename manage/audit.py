"""
manage/audit.py - 管理页登录审计。

双重记录：
  1) stdout（docker logs 实时看，PaaS 自动采集）
  2) Postgres 表 manage.manage_login_audit（历史可查，结构由 pgschema.ensure 建）

表在独立 schema `manage`（见 pgschema.py 说明：避免被 prisma db push 当漂移 DROP）。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from . import pgschema

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_TABLE = f'"{pgschema.SCHEMA}".manage_login_audit'


def _log_stdout(success: bool, ip: str, ua: str, note: str) -> None:
    status = "LOGIN_OK" if success else "LOGIN_FAIL"
    print(
        f"[manage_audit] {status} ip={ip or '-'} ua={ua[:60] or '-'} note={note or '-'}",
        file=sys.stderr, flush=True,
    )


def log_login(success: bool, ip: Optional[str] = None, user_agent: Optional[str] = None,
              note: str = "") -> None:
    """记一条登录审计：stdout 必写，postgres 尽力写（失败不阻塞）。"""
    ip = ip or "-"
    ua = (user_agent or "-")[:200]
    _log_stdout(success, ip, ua, note)

    if not DATABASE_URL:
        return
    try:
        # psycopg2 litellm 镜像自带；失败静默降级到只写日志
        pgschema.ensure()  # 幂等：schema/表不存在时补建
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_TABLE} (ip, success, user_agent, note) VALUES (%s,%s,%s,%s)",
                    (ip, success, ua, note),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def recent_logins(limit: int = 20) -> list[dict]:
    """查最近 N 条登录记录（管理页展示用）。DB 不可用返回空列表。"""
    if not DATABASE_URL:
        return []
    try:
        pgschema.ensure()
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ts, ip, success, note FROM {_TABLE} "
                    "ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
            return [
                {"ts": r[0].isoformat() if r[0] else "-", "ip": r[1] or "-",
                 "success": r[2], "note": r[3] or "-"}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []


def recent_secret_reads(limit: int = 20) -> list[dict]:
    """查最近 N 条 secrets 表读审计（管理页展示用）。

    数据来自 pgschema.audit_secrets_reads() 的轮询快照（pg_stat_statements），
    管理页登录时顺手快照一次；DB 不可用/扩展未装返回空列表。"""
    if not DATABASE_URL:
        return []
    try:
        pgschema.audit_secrets_reads()
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, db_user, client_addr, stat_calls "
                    "FROM manage.secrets_read_audit ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
            return [
                {"ts": r[0].isoformat() if r[0] else "-", "db_user": r[1] or "-",
                 "client_addr": r[2] or "-", "stat_calls": r[3]}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []
