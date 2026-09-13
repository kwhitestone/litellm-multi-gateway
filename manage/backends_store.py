"""
manage/backends_store.py - backends.yaml 的 PG 存取。

设计（真相源 = Postgres，启动时物化成文件）：
  - 表 manage.gateway_backends 单行存 backends.yaml 全文（content），updated_at 记最后修改。
  - 所有现有消费者（LiteLLM 路由表 / vision_hook / 管理页 routes.py）读的都是文件，
    本模块只负责「文件从哪来」：server.py 启动时调 load_on_boot() 拉取并物化。
  - 管理页保存时调 validate() 用镜像里的 gen_config.py 做语法/映射校验，
    校验通过才入库（带病配置在保存阶段就被拦下，而不是重启起不来）。

表在独立 schema `manage`（见 pgschema.py 说明：避免被 prisma db push 当漂移 DROP）。
DB 不可用时的语义：读方向回退镜像内置版（启动不阻塞）；写方向直接报错。
与 audit.py 同款 psycopg2 直连模式（镜像已装 psycopg2-binary）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import pgschema

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_TABLE = f'"{pgschema.SCHEMA}".gateway_backends'

BAKED_BACKENDS = Path(os.environ.get("BACKENDS_PATH", "/app/backends.yaml"))
BAKED_CONFIG = Path(os.environ.get("LITELLM_CONFIG_PATH", "/app/config.yaml"))
GEN_CONFIG = Path(os.environ.get("GEN_CONFIG_PATH", "/app/gen_config.py"))


def _connect():
    """psycopg2 连接（延迟 import，与 audit.py 一致）。无 DATABASE_URL 返回 None。"""
    if not DATABASE_URL:
        return None
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _run_gen_config(backends_path: Path, out_path: Path) -> None:
    """调镜像里的 gen_config.py 生成 config.yaml。失败抛 RuntimeError（含 stderr）。"""
    import subprocess
    r = subprocess.run(
        ["python3", str(GEN_CONFIG), "gen-config",
         "--backends", str(backends_path), str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        # 语法错误 stderr 带 traceback，提取 yaml/py 抛错主体（RuntimeError 显示时再截尾）
        err = (r.stderr or r.stdout or "gen_config 无输出").strip().splitlines()
        raise RuntimeError("\n".join(err[-8:]))


def validate(content: str) -> str:
    """校验一段 backends.yaml 文本。返回规范化后的文本（原样），非法则抛 ValueError。

    两层校验：
      1. gen-config 试生成 config.yaml（YAML 语法 / backends 结构）
      2. 逐后端跑 resolve_mapping（mapping 指向不存在的模型 / 缺 "*" 兜底 /
         缺 claude 后端）-- gen-config 本身不走这步，keys.sh 建 key 时才会踩到，
         这里提前拦下，避免保存成功但建 key 全 500。"""
    import importlib.util
    import tempfile

    import yaml

    with tempfile.TemporaryDirectory() as td:
        b = Path(td) / "backends.yaml"
        c = Path(td) / "config.yaml"
        b.write_text(content, encoding="utf-8")
        try:
            _run_gen_config(b, c)
        except RuntimeError as exc:
            raise ValueError(str(exc))
        # gen_config 的报错用 sys.exit 抛 SystemExit；在子进程外同步执行以拿 mapping 校验
        try:
            cfg = yaml.safe_load(content) or {}
            spec = importlib.util.spec_from_file_location("gen_config", GEN_CONFIG)
            gc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gc)
            for backend in cfg.get("backends", {}):
                gc.resolve_mapping(backend, cfg)  # SystemExit = 校验失败
        except SystemExit as exc:
            raise ValueError(str(exc) or "mapping 校验失败")
        except Exception as exc:
            raise ValueError(f"配置解析失败: {exc}")
    return content


def load_on_boot() -> None:
    """server.py 启动时调（在 import litellm 之前）：
    1. 建 schema/表（幂等）
    2. 表里没行 -> 镜像内置 backends.yaml 作为初始值入库（config.yaml 构建期已生成，直接用）
    3. 表里有行 -> 覆盖 /app/backends.yaml 并重新生成 /app/config.yaml
    4. DB 不可达 -> 保持镜像内置版，打日志不阻塞启动
    """
    if not DATABASE_URL:
        return  # 没配 DB：纯镜像模式（config.yaml 构建期已生成）
    try:
        pgschema.ensure()
        conn = _connect()
        if conn is None:
            return
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT content FROM {_TABLE} WHERE id = 1")
                row = cur.fetchone()
                if row is None:
                    # 首次部署：镜像内置版入库。构建期已生成 config.yaml，无需重跑
                    baked = BAKED_BACKENDS.read_text(encoding="utf-8")
                    cur.execute(f"INSERT INTO {_TABLE} (id, content) VALUES (1, %s)", (baked,))
                    conn.commit()
                    print("[backends_store] 首次部署：镜像内置 backends.yaml 已入库", flush=True)
                    return
                content: str = row[0]
                BAKED_BACKENDS.write_text(content, encoding="utf-8")
                _run_gen_config(BAKED_BACKENDS, BAKED_CONFIG)
                conn.commit()
                print(f"[backends_store] 已从 PG 加载 backends.yaml"
                      f"（{len(content)} 字节）并重新生成 config.yaml", flush=True)
    except Exception as exc:
        print(f"[backends_store] PG 读取失败({exc!r})，回退镜像内置版", flush=True)


def regen_config() -> None:
    """用当前 /app/backends.yaml 重新生成 /app/config.yaml（幂等，失败不阻塞启动）。

    为什么启动时必须重跑一次：config.yaml 里的 guardrails 段（headroom 压缩）由
    gen_config 读 HEADROOM_* 环境变量决定，而构建期跑 gen-config 时这些变量还不存在。
    load_on_boot 只在「PG 里已有 backends 行」时重生成，首次部署 / 无 DATABASE_URL
    的场景会停留在构建期产物，headroom env 配了也不生效。这里兜住所有路径。
    调用点在 server.py，必须在 import litellm 之前（config 在 import 时加载）。
    """
    if not BAKED_BACKENDS.exists():
        return
    try:
        _run_gen_config(BAKED_BACKENDS, BAKED_CONFIG)
    except Exception as exc:
        print(f"[backends_store] config.yaml 重新生成失败({exc!r})，用现有版本", flush=True)


def fetch() -> Optional[dict]:
    """读当前存储的 backends.yaml。返回 {content, updated_at}，DB 不可用返回 None。"""
    try:
        pgschema.ensure()
        conn = _connect()
        if conn is None:
            return None
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT content, updated_at FROM {_TABLE} WHERE id = 1")
                row = cur.fetchone()
                if row is None:
                    # 库里有表但没行：展示镜像内置版（还没被 load_on_boot 初始化过）
                    return {"content": BAKED_BACKENDS.read_text(encoding="utf-8"),
                            "updated_at": None}
                return {"content": row[0], "updated_at": row[1].isoformat() if row[1] else None}
    except Exception:
        return None


def save(content: str) -> None:
    """校验 + 保存。gen_config 校验失败抛 ValueError；DB 写失败抛 RuntimeError。"""
    content = validate(content)
    try:
        pgschema.ensure()
        conn = _connect()
        if conn is None:
            raise RuntimeError("DATABASE_URL 未配置，无法保存")
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_TABLE} (id, content, updated_at) "
                    "VALUES (1, %s, NOW()) "
                    "ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, "
                    "updated_at = NOW()",
                    (content,),
                )
            conn.commit()
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"PG 写入失败: {exc}")
