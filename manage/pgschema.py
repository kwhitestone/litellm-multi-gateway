"""
manage/pgschema.py - 自建表的 PG schema 管理。

自建表（manage_login_audit / gateway_backends）放独立 schema `manage`，
不与 LiteLLM 的表混在 public：
prisma db push 会把 public 里 schema.prisma 不认识的表当漂移处理（DROP），
一旦表里有数据就触发 data-loss 警告、db push 整体失败退出--
副作用是 LiteLLM 升级带来的 schema 变更永远应用不上。
独立 schema 对 prisma 完全不可见，两边互不干扰。

ensure() 幂等：建 schema + 建表 + 一次性把旧版本遗留在 public 的同名表
（本机制上线前建的）搬过来删掉，让 prisma db push 恢复干净通过。
"""
from __future__ import annotations

import os

DATABASE_URL = os.environ.get("DATABASE_URL", "")

SCHEMA = "manage"

# 表 DDL 单一真相源（audit.py / backends_store.py / secrets_store.py 的查询用 qualified 名引用）
_TABLES: dict[str, str] = {
    "manage_login_audit": f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.manage_login_audit (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ip TEXT,
            success BOOLEAN NOT NULL,
            user_agent TEXT,
            note TEXT
        )""",
    "gateway_backends": f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.gateway_backends (
            id INT PRIMARY KEY DEFAULT 1,
            content TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    "secrets": f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.secrets (
            name TEXT PRIMARY KEY,
            value_encrypted TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
}

_ready = False


def ensure() -> None:
    """建 schema/表（幂等，进程内只跑一次）。DATABASE_URL 未配置时静默跳过；
    DB 连不上抛异常（调用方决定是否致命--server.py 里非致命，audit 里降级）。"""
    global _ready
    if _ready or not DATABASE_URL:
        _ready = True
        return

    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
            for name, ddl in _TABLES.items():
                cur.execute(ddl)
                # 一次性迁移：旧版本把表建在 public（prisma 视为漂移要 DROP），
                # 数据搬到独立 schema 后删掉旧表，prisma db push 才能干净通过
                cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        f'INSERT INTO "{SCHEMA}"."{name}" '
                        f'SELECT * FROM public."{name}" ON CONFLICT DO NOTHING'
                    )
                    cur.execute(f'DROP TABLE public."{name}"')
        conn.commit()
        _ready = True
    finally:
        conn.close()
