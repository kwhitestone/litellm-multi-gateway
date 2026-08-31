"""
manage/secrets_store.py - provider 密钥的加密存储。

目的：provider key（CLAUDE_CODE_KEY_1/2/3、ARK_API_KEY、Z_AI_API_KEY 等）不再放
云端配置 / PaaS 环境变量（那里所有能读配置面的人都能看到明文），改为存
manage.secrets 表（Fernet 加密），启动时解密注入进程环境变量。

威胁模型与边界：
  - 挡住：云端配置读者、PaaS 环境变量读者、DB 备份/拖库（拿到的是密文）。
  - 挡不住：能 exec 进容器 / 能读 /proc/1/environ 的人（解密后必须以明文进
    os.environ 才能被 config.yaml 的 os.environ/KEY 引用，这是物理上限，
    任何方案都一样）。
  - 解密材料：从 GATEWAY_MASTER_KEY 经 HKDF-SHA256 派生 Fernet key，不新增
    环境变量。代价：能同时读 env（拿派生材料）+ 连 DB（拿密文）的人可解密。
    要堵死这个口，把主密钥换成挂载文件（SECRETS_MASTER_KEY_FILE），env 里
    完全不出现解密材料。

表结构（pgschema.ensure 建）：manage.secrets(name PK, value_encrypted, updated_at)。

用法（server.py 启动早期 / entrypoint 调 load_on_boot）：
    from manage.secrets_store import load_on_boot
    load_on_boot()          # 解密全部 secrets 写 os.environ

CLI（scripts/secrets-cli.py 透传）：
    list / set NAME / rotate-key / verify
"""
from __future__ import annotations

import base64
import os
from typing import Optional

SCHEMA = "manage"
_TABLE = f'{SCHEMA}.secrets'

# 值太长多半是粘错（比如整段 yaml），DB 侧 TEXT 不限但这里早失败
MAX_VALUE_LEN = 4096

_fernet = None


# ---------- 加密 ----------

def _hkdf(master: bytes, info: str) -> bytes:
    """HKDF-SHA256(master, salt=固定域分隔, info) -> 32B Fernet key。"""
    import hashlib
    import hmac
    # extract：salt 用固定域分隔串（无保密性需求，只防跨用途重用）
    prk = hmac.new(b"litellm-mgw:secrets:v1", master, hashlib.sha256).digest()
    # expand：T(1) = HMAC(PRK, info || 0x01)，32B 即一轮
    return hmac.new(prk, (info + "\x01").encode(), hashlib.sha256).digest()


def _fernet_key() -> bytes:
    """派生材料：优先 SECRETS_MASTER_KEY_FILE（挂载文件），否则 GATEWAY_MASTER_KEY。"""
    key_file = os.environ.get("SECRETS_MASTER_KEY_FILE", "")
    if key_file and os.path.exists(key_file):
        with open(key_file, "rb") as f:
            master = f.read().strip()
        if not master:
            raise RuntimeError(f"SECRETS_MASTER_KEY_FILE({key_file}) 是空文件")
    else:
        master = os.environ.get("GATEWAY_MASTER_KEY", "").encode()
        if not master:
            raise RuntimeError("GATEWAY_MASTER_KEY 未配置，无法派生加密密钥")
    return base64.urlsafe_b64encode(_hkdf(master, "manage.secrets/fernet"))


def _get_fernet():
    """懒加载 Fernet（cryptography 镜像内有；导入失败时该功能整体降级）。"""
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_fernet_key())
    return _fernet


def encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


# ---------- DB ----------

def _db_url() -> str:
    """调用时读（不用模块级常量：entrypoint/server 场景 env 先就位，测试可 patch）。"""
    return os.environ.get("DATABASE_URL", "")


def _connect():
    url = _db_url()
    if not url:
        return None
    import psycopg2
    return psycopg2.connect(url)


def ensure_table() -> None:
    """建表（幂等）。load_on_boot / list_all 前都会调，独立于 pgschema.ensure。"""
    conn = _connect()
    if conn is None:
        raise RuntimeError("DATABASE_URL 未配置")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {_TABLE} (
                        name TEXT PRIMARY KEY,
                        value_encrypted TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )""")
    finally:
        conn.close()


# ---------- 对外操作 ----------

def load_on_boot() -> None:
    """启动时（import litellm 之前）解密全部 secrets 写 os.environ。

    DB/secrets 表不可用或解密失败：打日志继续启动（该 key 用环境变量/云端的
    原值兜底），不阻塞。密钥派生材料缺失时静默跳过（功能未启用）。
    """
    if not _db_url():
        return
    try:
        fernet = _get_fernet()  # 先验派生材料，缺了直接走静默跳过分支
    except RuntimeError as exc:
        print(f"[secrets] 跳过（{exc}）", flush=True)
        return
    try:
        ensure_table()
        rows = list_all()
    except Exception as exc:
        print(f"[secrets] 读取失败({exc!r})，密钥用环境变量原值", flush=True)
        return
    applied = 0
    for name, _enc, _ts in rows:
        try:
            os.environ[name] = decrypt(_enc)
            applied += 1
        except Exception as exc:
            print(f"[secrets] 解密 {name} 失败({exc!r})，用环境变量原值", flush=True)
    if rows:
        print(f"[secrets] 已从加密存储注入 {applied}/{len(rows)} 个密钥", flush=True)


def list_all() -> list[tuple[str, str, str]]:
    """[(name, value_encrypted, updated_at_iso)]，按 name 排序。先 ensure 表。"""
    ensure_table()
    conn = _connect()
    if conn is None:
        raise RuntimeError("DATABASE_URL 未配置")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT name, value_encrypted, updated_at FROM {_TABLE} ORDER BY name")
                return [(r[0], r[1], r[2].isoformat() if r[2] else "") for r in cur.fetchall()]
    finally:
        conn.close()


def set_secret(name: str, value: str) -> str:
    """加密入库（upsert）。返回加密后的 token（供 CLI verify）。"""
    name = _validate_name(name)
    if not value or len(value) > MAX_VALUE_LEN:
        raise ValueError(f"值不能为空且不超过 {MAX_VALUE_LEN} 字符")
    if any(c in value for c in "\n\r"):
        raise ValueError("值不能包含换行")
    ensure_table()
    enc = encrypt(value)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} (name, value_encrypted, updated_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (name) DO UPDATE SET "
                "value_encrypted = EXCLUDED.value_encrypted, updated_at = NOW()",
                (name, enc),
            )
    return enc


def delete_secret(name: str) -> int:
    """删除。返回删除行数（0=本来就没有）。"""
    ensure_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE name = %s", (name,))
            return cur.rowcount


def get_decrypted(name: str) -> Optional[str]:
    """解密取单条（CLI verify 用）。不存在返回 None。"""
    conn = _connect()
    if conn is None:
        raise RuntimeError("DATABASE_URL 未配置")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT value_encrypted FROM {_TABLE} WHERE name = %s", (name,))
                row = cur.fetchone()
                return decrypt(row[0]) if row else None
    finally:
        conn.close()


def rotate_all() -> int:
    """换派生密钥后重加密全部条目（旧 Fernet 解不开的条目会报错中止）。

    典型流程：改 GATEWAY_MASTER_KEY / SECRETS_MASTER_KEY_FILE 前先在旧材料下
    跑 export，换材料后跑 import。rotate_all 用于「同材料下怀疑泄露换 Fernet
    salt」的场景，较少用。
    """
    rows = list_all()
    reencrypted = 0
    for name, enc, _ts in rows:
        value = decrypt(enc)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {_TABLE} SET value_encrypted = %s, updated_at = NOW() WHERE name = %s",
                    (encrypt(value), name),
                )
        reencrypted += 1
    return reencrypted


def _validate_name(name: str) -> str:
    """密钥名 = 环境变量名：大写字母/数字/下划线，防 SQL/路径外的意外。"""
    name = (name or "").strip()
    if not name or not all(c.isupper() or c.isdigit() or c == "_" for c in name):
        raise ValueError("密钥名必须是环境变量风格：大写字母/数字/下划线（如 CLAUDE_CODE_KEY_3）")
    return name
