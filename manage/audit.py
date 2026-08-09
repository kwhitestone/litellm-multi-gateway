"""
manage/audit.py — 管理页登录审计。

双重记录：
  1) stdout（docker logs 实时看，PaaS 自动采集）
  2) Postgres 表 manage_login_audit（历史可查，结构见 _ENSURE_TABLE_SQL）

表结构（自动建，幂等）：
  id           SERIAL PK
  ts           TIMESTAMPTZ（事件时间，UTC）
  ip           TEXT（来源 IP）
  success      BOOLEAN（登录成功/失败）
  user_agent   TEXT（浏览器标识）
  note         TEXT（备注，如 'session expired' / 'logout'）
"""
from __future__ import annotations

import os
import sys
from typing import Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS manage_login_audit (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip TEXT,
    success BOOLEAN NOT NULL,
    user_agent TEXT,
    note TEXT
);
"""

_table_ready = False


def _log_stdout(success: bool, ip: str, ua: str, note: str) -> None:
    status = "LOGIN_OK" if success else "LOGIN_FAIL"
    print(
        f"[manage_audit] {status} ip={ip or '-'} ua={ua[:60] or '-'} note={note or '-'}",
        file=sys.stderr, flush=True,
    )


def _ensure_table(cursor) -> None:
    global _table_ready
    if _table_ready:
        return
    try:
        cursor.execute(_ENSURE_TABLE_SQL)
        _table_ready = True
    except Exception:
        pass  # DB 不可用时审计降级为只写日志，不阻塞登录流程


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
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "INSERT INTO manage_login_audit (ip, success, user_agent, note) VALUES (%s,%s,%s,%s)",
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
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "SELECT ts, ip, success, note FROM manage_login_audit "
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
