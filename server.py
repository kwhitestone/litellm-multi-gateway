"""
server.py — 单容器入口。

import LiteLLM 原生 FastAPI app，挂上自建管理页 router，然后用 uvicorn 起服务。
单进程、单端口 —— 管理页 (/manage/*) 与 LiteLLM 所有端点 (/v1/*, /ui, /key/*) 共用同一 app。

关键：uvicorn 接收的是 app 对象（不是字符串 "litellm.proxy.proxy_server:app"），
这样 include_router 注入的路由才生效，不会被 uvicorn 重新 import 模块而丢失。
代价：num_workers 必须为 1（uvicorn 用 app 对象时不支持多 worker fork）。
对网关场景足够：LiteLLM 是 I/O 密集型，单 worker + asyncio 并发即可。
"""
from __future__ import annotations

import os
import subprocess
import sys

# 先把管理页依赖路径备好（hooks 目录 litellm 自己会加 PYTHONPATH，这里补 /app）
sys.path.insert(0, "/app")

SCHEMA_PATH = os.environ.get("PRISMA_SCHEMA_PATH", "/app/schema.prisma")


def _ensure_db_migrated() -> None:
    """首次部署（空 db）需要 prisma db push 建表。

    litellm 启动时不会自动跑 db push（除非 CLI 带 --use_prisma_db_push），
    空库会导致所有 key/审计表缺失、管理页创建 key 500。
    已有表的库再跑一次 db push 是幂等的（prisma 检测无变化跳过），所以每次启动都跑也安全。"""
    if not os.path.exists(SCHEMA_PATH):
        print(f"[server] schema.prisma 不存在({SCHEMA_PATH})，跳过 db 迁移", flush=True)
        return
    print("[server] 运行 prisma db push 建表/同步 schema...", flush=True)
    try:
        r = subprocess.run(
            ["prisma", "db", "push", f"--schema={SCHEMA_PATH}"],
            cwd="/app",
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            print("[server] ✓ prisma db push 完成", flush=True)
        else:
            # 非致命：可能 db 还没就绪，litellm 自己会重试连接
            print(f"[server] prisma db push 非零退出({r.returncode})，继续启动\n{r.stderr[-500:]}", flush=True)
    except Exception as exc:
        print(f"[server] prisma db push 异常: {exc!r}，继续启动", flush=True)


# 0. 首次部署迁移 db（在 litellm 连库前）
# 先把自建表（登录审计/backends 存储挪进独立 schema manage）从 public 清走，
# 否则 prisma db push 把它们当漂移表 DROP--有数据时会因 data-loss 警告整体失败，
# LiteLLM 升级的 schema 变更就永远应用不上。
if os.environ.get("DATABASE_URL"):
    try:
        from manage.pgschema import ensure as _ensure_manage_schema
        _ensure_manage_schema()
    except Exception as exc:
        print(f"[server] 自建表 schema 准备失败({exc!r})，继续启动", flush=True)
_ensure_db_migrated()

# 0.2 加密密钥注入（在 litellm 读 os.environ 之前）：
# manage.secrets 表里存了 Fernet 加密的 provider key，这里解密写 os.environ。
# 优先级最高（覆盖云端配置/环境变量的同名值），让"想藏的 key"不再出现在
# 云端配置/PaaS 环境变量里。DB 不可达/解密失败不阻塞启动，用原值兜底。
if os.environ.get("DATABASE_URL"):
    try:
        from manage.secrets_store import load_on_boot as _load_secrets
        _load_secrets()
    except Exception as exc:
        print(f"[server] 加密密钥注入失败({exc!r})，密钥用环境变量原值", flush=True)

# 0.5 backends.yaml 物化：PG 里存的是真相源（gateway_backends 表），拉下来覆盖
# /app/backends.yaml 并重新生成 config.yaml。首次部署把镜像内置版入库；DB 不可达
# 回退镜像内置版。必须在 import litellm 之前（config 在 import 时加载）。
import sys as _sys
if "/app" not in _sys.path:
    _sys.path.insert(0, "/app")
if os.environ.get("DATABASE_URL"):
    try:
        from manage.backends_store import load_on_boot
        load_on_boot()
    except Exception as exc:
        print(f"[server] backends 物化失败({exc!r})，用镜像内置版", flush=True)

# 0.6 config.yaml 无条件重新生成：guardrails 段（headroom 压缩）依赖 HEADROOM_*
# 环境变量，构建期跑 gen-config 时这些变量还不存在；load_on_boot 也只在 PG 里已有
# backends 行时才重生成。这里兜住首次部署 / 无 DATABASE_URL 的路径。
try:
    from manage.backends_store import regen_config
    regen_config()
except Exception as exc:
    print(f"[server] config.yaml 重新生成跳过({exc!r})", flush=True)

# 1. import LiteLLM 的 app（会触发 config.yaml 加载 + 路由注册）
from litellm.proxy.proxy_server import app  # noqa: E402

# 2. 挂自建管理页
from manage.routes import router as manage_router  # noqa: E402
app.include_router(manage_router)

# 3. 启动
if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "4001"))
    config_path = os.environ.get("LITELLM_CONFIG_PATH", "/app/config.yaml")

    print(f"[server] LiteLLM + manage 启动: host={host} port={port} config={config_path}", flush=True)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
    )
