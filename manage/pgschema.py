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

# 密文虽不可直接解密，但「谁在收集密文」值得留痕：secrets 表的 SELECT 也审计
# （PG 无原生 SELECT 触发器，用统计视图 + 轮询比对近似：见 audit_statements()）。
# pg_stat_statements 需库级开启（shared_preload_libraries）；没开时降级为空。
_AUDIT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_tables
                   WHERE schemaname = 'manage' AND tablename = 'secrets_read_audit') THEN
        CREATE TABLE manage.secrets_read_audit (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_user TEXT,
            client_addr TEXT,
            query_fragment TEXT,
            stat_calls BIGINT,
            stat_rows BIGINT
        );
    END IF;
END $$;
"""

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
            _ensure_secrets_read_audit(cur)
        conn.commit()
        _ready = True
    finally:
        conn.close()


def _ensure_secrets_read_audit(cur) -> None:
    """建 secrets 读审计表（幂等）。数据来源是 audit_secrets_reads() 的轮询快照，
    不是真触发器（PG 没有 SELECT 触发器）。"""
    cur.execute(_AUDIT_SQL)


def audit_secrets_reads() -> int:
    """把 pg_stat_statements 里命中 manage.secrets 的 SELECT 快照进审计表。

    返回新写入的行数；pg_stat_statements 不可用（未开 shared_preload_libraries
    或无权限）时返回 0 并打印原因。调用方：audit.py 定期/登录时触发。
    快照是累计值增量比对（stat_calls 增长才记一行），能看出「谁、何时、查了
    多少次」，但同一 db_user+query 的多次读聚成一行；查完即 RESET 的人不留痕
    （这是统计视图方案的本质局限，文档已写明）。"""
    import psycopg2
    if not DATABASE_URL:
        return 0
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'")
            if cur.fetchone()[0] == 0:
                print("[secrets-audit] pg_stat_statements 未安装，SELECT 审计降级为关闭", flush=True)
                return 0
            cur.execute("""
                SELECT userid::regrole, query, calls, rows
                FROM pg_stat_statements
                WHERE query ILIKE '%manage.secrets%' AND query ILIKE 'select%'
            """)
            written = 0
            for role, query, calls, rows in cur.fetchall():
                cur.execute(
                    "INSERT INTO manage.secrets_read_audit "
                    "(db_user, client_addr, query_fragment, stat_calls, stat_rows) "
                    "VALUES (%s, inet_client_addr()::text, %s, %s, %s)",
                    (str(role), query[:200], calls, rows),
                )
                written += 1
            conn.commit()
            return written
    except Exception as exc:
        print(f"[secrets-audit] 读审计快照失败({exc!r})", flush=True)
        return 0
    finally:
        conn.close()
