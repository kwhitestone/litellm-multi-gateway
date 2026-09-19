"""
manage/routes.py — 自建管理页路由。

挂在 LiteLLM 的 FastAPI app 上（/manage/*），给非技术用户一个友好界面：
  - GET  /manage/            主页：创建 key 表单 + key 列表 + 登录审计（需登录）
  - GET  /manage/login       登录页（输 master key）
  - POST /manage/api/login   登录验证 → 签发 HttpOnly 会话 Cookie
  - POST /manage/logout      登出（清 Cookie + 审计）
  - POST /manage/api/new     创建 key（需登录）
  - POST /manage/api/delete  删除 key（需登录）
  - GET  /manage/backends    查看/编辑 backends.yaml（PG 存储，需登录）
  - POST /manage/api/backends 保存 backends.yaml（校验后入库，需登录，重启生效）
  - GET  /manage/test       上游测试页：直连各后端 / 经网关对比，完整展示返回（需登录）
  - POST /manage/api/test   发一条测试消息到指定后端+模型（需登录）

认证：会话 Cookie（HMAC 签名，滑动过期 12h），master key 只在登录时验一次，
之后全程不出现在 URL/响应里。登录有审计（stdout + postgres）。

后端→别名的映射逻辑直接 import 容器里的 gen_config.py，与 keys.sh 完全一致。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, audit, backends_store, backends_sync, secrets_store
from .backend_probe import build_direct_request

router = APIRouter(prefix="/manage")

# 模板目录：manage/templates/（相对本文件）
HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

# 后端配置：构建期烤进镜像的 /app/backends.yaml（本地测试可回退到仓库内）
_BACKENDS_PATH = Path(os.environ.get("BACKENDS_PATH", "/app/backends.yaml"))
if not _BACKENDS_PATH.exists():
    _BACKENDS_PATH = HERE.parent / "litellm" / "profiles" / "backends.yaml"

# LiteLLM 自身地址（同进程，localhost）+ master key
_PORT = os.environ.get("PORT", "4001")
LITELLM_BASE = os.environ.get("LITELLM_INTERNAL_BASE", f"http://127.0.0.1:{_PORT}")
MASTER_KEY = auth.MASTER_KEY  # 复用 auth 模块读的 GATEWAY_MASTER_KEY


def _backends() -> dict[str, Any]:
    with open(_BACKENDS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("backends", {})


def _backend_list() -> list[str]:
    return sorted(_backends().keys())


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（PaaS 常用 X-Forwarded-For）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "-"


# ---------- 别名解析（复用 gen_config.py） ----------

def _gen_config_path() -> Path:
    p = Path(os.environ.get("GEN_CONFIG_PATH", "/app/gen_config.py"))
    if p.exists():
        return p
    return HERE.parent / "litellm" / "profiles" / "gen_config.py"


def _load_gen_config():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_config", _gen_config_path())
    gc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gc)
    return gc


def _resolve_aliases(gc, backend_str: str) -> tuple[dict[str, str], list[str]]:
    """单/多后端 → LiteLLM key 的 {aliases, models}（与 keys.sh new 一致）。"""
    cfg = gc.load_backends(_BACKENDS_PATH)
    backends = [b.strip() for b in backend_str.split(",") if b.strip()]
    available = set(cfg["backends"])
    for b in backends:
        if b not in available:
            raise HTTPException(400, f"未知后端: {b}（可选: {', '.join(sorted(available))}）")

    if len(backends) == 1:
        return gc.resolve_mapping(backends[0], cfg), []

    cc = gc.claude_names(cfg)
    short: dict[str, str] = {}
    for b in backends:
        models = list(cfg["backends"][b]["models"].keys())
        short[b] = gc.model_name_for(b, models[0])
    aliases: dict[str, str] = {b: short[b] for b in backends}
    default_target = "claude-sonnet-5" if "claude" in backends else short[backends[0]]
    for c in cc:
        aliases[c] = default_target
    all_models = set(cc)
    for b in backends:
        for m in cfg["backends"][b]["models"]:
            all_models.add(gc.model_name_for(b, m))
    return aliases, sorted(all_models)


def _headroom_enabled() -> bool:
    """headroom 压缩是否在本实例启用（HEADROOM_API_BASE 配了即启用）。

    管理页据此决定是否显示「压缩」勾选框。
    """
    try:
        return _load_gen_config().headroom_settings() is not None
    except Exception:
        return False


def _headroom_name() -> str:
    try:
        return _load_gen_config().HEADROOM_GUARDRAIL_NAME
    except Exception:
        return "headroom-compression"


# headroom 按 key 开关走「全局默认压缩 + 逐 key 关闭」（B-lite）：
# 给 key 挂 guardrails=[...] 是企业版字段，无 license 时 /key/generate 直接 403；
# 而 disable_global_guardrails 写进 metadata 字典不过 _premium_user_check（企业版
# 门禁只扫顶层字段），所以反过来：config.yaml 里 default_on=true 全局压缩，
# 不想压的 key 标记 metadata.disable_global_guardrails=true。
_HEADROOM_DISABLE_FIELD = "disable_global_guardrails"


async def _key_metadata(key: str) -> dict[str, Any]:
    """读回该 key 当前的完整 metadata（/key/update 是整体替换，改前必须先读）。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{LITELLM_BASE}/key/info",
            params={"key": key},
            headers={"Authorization": f"Bearer {MASTER_KEY}"},
        )
    if r.status_code != 200:
        raise HTTPException(500, f"读取 key 信息失败: {r.text}")
    return (r.json().get("info") or {}).get("metadata") or {}


def _with_headroom_flag(metadata: dict[str, Any], compress: bool) -> dict[str, Any]:
    """在既有 metadata 上增量改压缩开关，其余字段（tags/spend_logs_metadata…）原样保留。"""
    merged = dict(metadata)
    merged[_HEADROOM_DISABLE_FIELD] = not compress
    return merged


# ---------- 登录 / 登出 ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页。已登录则直接跳主页。"""
    if not MASTER_KEY:
        raise HTTPException(503, "GATEWAY_MASTER_KEY 未配置，管理页不可用")
    if auth.has_valid_session(request):
        return RedirectResponse(url="/manage/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/api/login")
async def do_login(request: Request):
    """登录验证：master key 正确 → 签发会话 Cookie + 审计。

    注意：必须把 Cookie 设到返回的 JSONResponse 上，而非注入的 response 参数——
    FastAPI 在返回显式 Response 时会丢弃注入 response 上设的 header。"""
    if not MASTER_KEY:
        raise HTTPException(503, "GATEWAY_MASTER_KEY 未配置")
    body = await request.json()
    submitted = (body.get("key") or "").strip()
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")

    if auth.check_master_key(submitted):
        resp = JSONResponse({"ok": True})
        auth.issue_session(resp)  # Cookie 设到真正返回的 response 上
        audit.log_login(success=True, ip=ip, user_agent=ua, note="login")
        return resp
    audit.log_login(success=False, ip=ip, user_agent=ua, note="wrong key")
    raise HTTPException(401, "管理密钥错误")


@router.post("/logout")
async def logout(request: Request):
    """登出：清 Cookie + 审计。"""
    was_logged_in = auth.has_valid_session(request)
    resp = RedirectResponse(url="/manage/login", status_code=302)
    auth.clear_session(resp)
    if was_logged_in:
        audit.log_login(success=True, ip=_client_ip(request),
                        user_agent=request.headers.get("user-agent", ""), note="logout")
    return resp


# ---------- 受保护路由（require_session + 滑动续命） ----------

@router.get("/", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
async def manage_page(request: Request):
    """主页：创建表单 + key 列表 + 登录审计。"""
    keys_info = await _fetch_keys()
    logins = audit.recent_logins(8)
    base_url_hint = os.environ.get("PUBLIC_BASE_URL", f"http://127.0.0.1:{_PORT}")
    resp = templates.TemplateResponse(request, "manage.html", {
        "backends": _backend_list(),
        "keys": keys_info,
        "base_url": base_url_hint,
        "logins": logins,
        "headroom_enabled": _headroom_enabled(),
    })
    auth.renew_session(resp)  # 滑动续命：Cookie 设到真正返回的 response 上
    return resp


@router.post("/api/new", dependencies=[Depends(auth.require_session)])
async def new_key(request: Request) -> JSONResponse:
    """创建 key。body: {user, backend, alias?, max_budget?, rpm?, tpm?}"""
    body = await request.json()
    user = (body.get("user") or "").strip()
    backend = (body.get("backend") or "").strip()
    if not user or not backend:
        raise HTTPException(400, "user 和 backend 必填")
    alias = (body.get("alias") or f"{user}-key").strip()

    gc = _load_gen_config()
    aliases, models = _resolve_aliases(gc, backend)

    gen_body: dict[str, Any] = {
        "user_id": user, "key_alias": alias, "aliases": aliases, "models": models,
    }
    # headroom 压缩：全局 default_on=true，这里只在显式关闭时打 disable 标记。
    # 不传 headroom = 默认压缩（新 key 无需任何额外字段）。
    if body.get("headroom") is not None:
        gen_body["metadata"] = _with_headroom_flag({}, bool(body["headroom"]))
    if body.get("max_budget") not in (None, "", 0, "0"):
        try:
            gen_body["max_budget"] = float(body["max_budget"])
        except (TypeError, ValueError):
            raise HTTPException(400, "max_budget 必须是数字")
        gen_body.setdefault("budget_duration", body.get("budget_duration", "1mo"))
    for field, env in (("rpm_limit", "rpm"), ("tpm_limit", "tpm")):
        v = body.get(env)
        if v not in (None, "", 0, "0"):
            try:
                gen_body[field] = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{env} 必须是整数")

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{LITELLM_BASE}/key/generate",
            headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
            json=gen_body,
        )
    if r.status_code != 200 or "key" not in r.json():
        raise HTTPException(500, f"LiteLLM 创建失败: {r.text}")
    resp = JSONResponse(r.json())
    auth.renew_session(resp)
    return resp


@router.post("/api/delete", dependencies=[Depends(auth.require_session)])
async def delete_key(request: Request) -> JSONResponse:
    """删除 key。body: {key: <完整 hash 或明文>}"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key 必填")

    keys_to_delete = [key]
    if not key.startswith("sk-"):
        hashes = await _list_key_hashes()
        hits = [h for h in hashes if h == key or h.startswith(key)]
        if not hits:
            raise HTTPException(404, f"没找到匹配 '{key}' 的 key")
        keys_to_delete = [hits[0]]

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{LITELLM_BASE}/key/delete",
            headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
            json={"keys": keys_to_delete},
        )
    ct = r.headers.get("content-type", "")
    resp = JSONResponse(r.json() if ct.startswith("application/json") else {"raw": r.text})
    auth.renew_session(resp)
    return resp


@router.post("/api/update", dependencies=[Depends(auth.require_session)])
async def update_key(request: Request) -> JSONResponse:
    """改 key 的路由后端和/或预算。body: {key, backend?, max_budget?, rpm?, tpm?}

    - backend 非空：用 gen_config 重建 aliases/models（整体覆盖，与 keys.sh update 一致）
    - max_budget/rpm/tpm 非空：一并更新预算/速率
    至少要传 backend 或 max_budget/rpm/tpm 中的一个。秒级生效，不重启 litellm。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key 必填")
    # hash/前缀补全成完整 hash（LiteLLM /key/update 要完整 key）
    if not key.startswith("sk-"):
        hashes = await _list_key_hashes()
        hits = [h for h in hashes if h == key or h.startswith(key)]
        if not hits:
            raise HTTPException(404, f"没找到匹配 '{key}' 的 key")
        key = hits[0]

    update_body: dict[str, Any] = {"key": key}
    backend = (body.get("backend") or "").strip()
    if backend:
        gc = _load_gen_config()
        aliases, models = _resolve_aliases(gc, backend)
        update_body["aliases"] = aliases
        update_body["models"] = models
    # 预算/速率（传了就改；想清空预算传 max_budget:0）
    if body.get("max_budget") not in (None, "", 0, "0"):
        try:
            update_body["max_budget"] = float(body["max_budget"])
            update_body.setdefault("budget_duration", body.get("budget_duration", "1mo"))
        except (TypeError, ValueError):
            raise HTTPException(400, "max_budget 必须是数字")
    elif body.get("max_budget") in (0, "0"):
        update_body["max_budget"] = None  # 清空预算
    for field, env in (("rpm_limit", "rpm"), ("tpm_limit", "tpm")):
        v = body.get(env)
        if v not in (None, "", 0, "0"):
            try:
                update_body[field] = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{env} 必须是整数")
    # headroom 压缩开关（不传=不动）。/key/update 的 metadata 是整体替换不是 merge，
    # 所以先读回现有 metadata 再增量改，避免抹掉 tags/spend_logs_metadata 等既有字段。
    if body.get("headroom") is not None:
        update_body["metadata"] = _with_headroom_flag(
            await _key_metadata(key), bool(body["headroom"])
        )

    if len(update_body) == 1:  # 只有 key，没实际改动字段
        raise HTTPException(400, "至少要传 backend 或 max_budget/rpm/tpm/headroom")

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{LITELLM_BASE}/key/update",
            headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
            json=update_body,
        )
    if r.status_code != 200:
        raise HTTPException(500, f"LiteLLM 更新失败: {r.text}")
    resp = JSONResponse(r.json())
    auth.renew_session(resp)
    return resp


# ---------- backends.yaml 编辑（PG 存储，保存即生效） ----------

@router.get("/backends", response_class=HTMLResponse,
            dependencies=[Depends(auth.require_session)])
async def backends_page(request: Request):
    """backends.yaml 编辑页。内容来自 PG（backends_store），DB 不可用展示当前文件。"""
    stored = backends_store.fetch()
    if stored is None:
        # DB 不可用：退回展示容器里实际在用的文件（管理页 routes 实时读它）
        content = _BACKENDS_PATH.read_text(encoding="utf-8")
        updated = None
        db_ok = False
    else:
        content = stored["content"]
        updated = stored["updated_at"]
        db_ok = True
    resp = templates.TemplateResponse(request, "backends.html", {
        "content": content,
        "updated": updated,
        "db_ok": db_ok,
        "sync": backends_sync.STATUS.as_dict(),
        "sync_interval": int(backends_sync.SYNC_INTERVAL_SECONDS),
    })
    auth.renew_session(resp)
    return resp


@router.get("/api/backends/status", dependencies=[Depends(auth.require_session)])
async def backends_sync_status() -> JSONResponse:
    """同步状态（给编辑页轮询刷横幅用）：错误详情 + 最近生效时间。"""
    return JSONResponse(backends_sync.STATUS.as_dict())


@router.post("/api/backends", dependencies=[Depends(auth.require_session)])
async def save_backends(request: Request) -> JSONResponse:
    """保存 backends.yaml。body: {content: <yaml 全文>}

    快路径：校验入库后，本实例立刻物化 + regen + 热重载，不等 10s 轮询。
    其余实例由各自的同步协程在一个周期内跟上（DB 的 updated_at 已跳变）。
    入库成功但热重载失败不算保存失败——配置已经在真相源里了，重启必然生效，
    所以返回 200 但带 reload_error，让页面提示「已保存，但路由仍是旧的」。
    """
    body = await request.json()
    content = body.get("content") or ""
    if not content.strip():
        raise HTTPException(400, "内容为空")
    try:
        backends_store.save(content)
    except ValueError as exc:
        raise HTTPException(400, f"校验失败: {exc}")
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))

    applied_at = None
    reload_error = None
    try:
        applied_at = await backends_sync.apply_now(content)
    except Exception as exc:
        reload_error = str(exc)

    resp = JSONResponse({
        "ok": True,
        "applied_at": applied_at,
        "reload_error": reload_error,
        "sync_interval": int(backends_sync.SYNC_INTERVAL_SECONDS),
    })
    auth.renew_session(resp)
    return resp


# ---------- 密钥管理（加密存储，重启后注入生效） ----------

@router.get("/secrets", response_class=HTMLResponse,
            dependencies=[Depends(auth.require_session)])
async def secrets_page(request: Request):
    """密钥管理页：列名/更新时间（不回显值），新增/删除。"""
    try:
        rows = [{"name": n, "updated": ts} for n, _enc, ts in secrets_store.list_all()]
        db_ok = True
    except Exception:
        rows, db_ok = [], False
    # 打开密钥页顺手快照一次 secrets 读审计（pg_stat_statements，未装则空）
    reads = audit.recent_secret_reads(10)
    resp = templates.TemplateResponse(request, "secrets.html", {
        "secrets": rows, "db_ok": db_ok, "secret_reads": reads})
    auth.renew_session(resp)
    return resp


@router.post("/api/secrets/set", dependencies=[Depends(auth.require_session)])
async def secrets_set(request: Request) -> JSONResponse:
    """加密入库。body: {name, value}。值只进 DB（Fernet 密文），不回显。"""
    body = await request.json()
    try:
        secrets_store.set_secret(body.get("name") or "", body.get("value") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"写入失败: {exc}")
    audit.log_login(success=True, ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent", ""),
                    note=f"secrets:set:{(body.get('name') or '?')[:40]}")
    resp = JSONResponse({"ok": True})
    auth.renew_session(resp)
    return resp


@router.post("/api/secrets/delete", dependencies=[Depends(auth.require_session)])
async def secrets_delete(request: Request) -> JSONResponse:
    """删除。body: {name}。删除后该 key 回退环境变量/云端配置的原值。"""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name 必填")
    try:
        n = secrets_store.delete_secret(name)
    except Exception as exc:
        raise HTTPException(500, f"删除失败: {exc}")
    audit.log_login(success=True, ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent", ""),
                    note=f"secrets:delete:{name[:40]}")
    resp = JSONResponse({"ok": True, "deleted": n})
    auth.renew_session(resp)
    return resp


# ---------- 上游测试（直连 vs 经网关，完整展示返回） ----------

def _backend_test_targets() -> dict[str, dict[str, Any]]:
    """{backend: {models: [model_name...], api_base, key_env, native: {model_name: 原生模型名}}}，
    model_name 为 litellm 路由名（gen_config 命名），native 为直连时该发给上游的真实模型名
    （litellm_model 去 provider 前缀：anthropic/glm-5.3 -> glm-5.3）。"""
    gc = _load_gen_config()
    cfg = gc.load_backends(_BACKENDS_PATH)
    out: dict[str, dict[str, Any]] = {}
    for name, spec in cfg["backends"].items():
        native: dict[str, str] = {}
        for m, mspec in spec.get("models", {}).items():
            litellm_model = mspec.get("litellm_model", m)
            native[gc.model_name_for(name, m)] = litellm_model.split("/", 1)[-1]
        out[name] = {
            "models": sorted(native.keys()),
            "native": native,
            "api_base": spec.get("api_base", ""),
            "key_env": spec.get("key_env", ""),
        }
    return out


@router.get("/test", response_class=HTMLResponse,
            dependencies=[Depends(auth.require_session)])
async def test_page(request: Request):
    """上游测试页：对比直连和经网关的响应，帮助定位请求链路问题。"""
    resp = templates.TemplateResponse(request, "test.html", {
        "targets": _backend_test_targets(),
    })
    auth.renew_session(resp)
    return resp


@router.post("/api/test", dependencies=[Depends(auth.require_session)])
async def api_test(request: Request) -> JSONResponse:
    """发一条最小测试消息。body: {backend, model, mode}
    mode=direct  按模型 provider 直连后端（不经 litellm，用 key_env 的真实 key）
    mode=gateway 经本网关 /v1/messages（用 master key，走完整 litellm 链路）
    返回完整上游响应（状态码/headers/body），错误也完整展示，方便排障。"""
    body = await request.json()
    backend = (body.get("backend") or "").strip()
    model = (body.get("model") or "").strip()
    mode = (body.get("mode") or "direct").strip()
    prompt = (body.get("prompt") or "hi").strip() or "hi"
    if backend not in _backends():
        raise HTTPException(400, f"未知后端: {backend}")

    started = time.monotonic()
    if mode == "gateway":
        payload = {"model": model or backend, "max_tokens": 64,
                   "messages": [{"role": "user", "content": prompt}]}
        r = await _relay_request(
            "POST", f"{LITELLM_BASE}/v1/messages",
            headers={"Authorization": f"Bearer {MASTER_KEY}",
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json=payload,
        )
    else:  # direct
        spec = _backends()[backend]
        key = os.environ.get(spec["key_env"], "")
        if not key:
            raise HTTPException(400, f"环境变量 {spec['key_env']} 未配置（.env 里加）")
        gc = _load_gen_config()
        selected = next((mspec for name, mspec in spec["models"].items()
                         if gc.model_name_for(backend, name) == model), None)
        if selected is None:
            raise HTTPException(400, "请选择该后端配置的模型")
        try:
            url, headers, payload = build_direct_request(
                spec["api_base"], selected["litellm_model"], key, prompt)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        r = await _relay_request(
            "POST", url, headers=headers, json=payload,
        )
    elapsed = round((time.monotonic() - started) * 1000)

    out: dict[str, Any] = {
        "ok": r.status_code == 200,
        "status": r.status_code,
        "elapsed_ms": elapsed,
        "headers": dict(r.headers),
        "body": _safe_body(r),
        "request": payload,
    }
    resp = JSONResponse(out)
    auth.renew_session(resp)
    return resp


async def _relay_request(method: str, url: str, **kw) -> "httpx.Response":
    """直连/经网关都走这里（60s 超时；上游 5xx 也要拿完整 body，不抛异常）。"""
    async with httpx.AsyncClient(timeout=60.0) as c:
        try:
            return await c.request(method, url, **kw)
        except httpx.HTTPError as exc:
            # 网络层失败（连不上/超时/DNS）：包成伪响应，页面照样完整展示
            return httpx.Response(
                0, request=httpx.Request(method, url),
                text=f"__network_error__\n{type(exc).__name__}: {exc}",
            )


def _safe_body(r: httpx.Response) -> str:
    """上游 body 原样返回；非文本 content-type 截断到 8KB 提示。"""
    ct = r.headers.get("content-type", "")
    text = r.text
    if not any(t in ct for t in ("json", "text", "event-stream")) and len(text) > 8192:
        return f"[binary content-type: {ct or 'unknown'}, {len(text)} bytes, 截断]"
    return text

async def _list_key_hashes() -> list[str]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{LITELLM_BASE}/key/list", headers={"Authorization": f"Bearer {MASTER_KEY}"})
    if r.status_code != 200:
        return []
    return r.json().get("keys", [])


async def _fetch_keys() -> list[dict[str, Any]]:
    """返回 key 信息列表（alias/user/后端/hash/spend/budget/mappings），供模板渲染。"""
    # model_name -> backend 反查表（按 backends.yaml 生成，任意后端自动识别，不再硬编码前缀）
    try:
        gc = _load_gen_config()
        cfg = gc.load_backends(_BACKENDS_PATH)
        model_to_backend = {
            gc.model_name_for(bname, m): bname
            for bname, bcfg in cfg["backends"].items()
            for m in bcfg["models"]
        }
    except Exception:
        model_to_backend = {}

    hashes = await _list_key_hashes()
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as c:
        for h in hashes:
            r = await c.get(
                f"{LITELLM_BASE}/key/info",
                params={"key": h},
                headers={"Authorization": f"Bearer {MASTER_KEY}"},
            )
            if r.status_code != 200:
                continue
            info = r.json().get("info", {}) or {}
            aliases = info.get("aliases") or {}
            bks: set[str] = {model_to_backend[str(v)] for v in aliases.values()
                             if str(v) in model_to_backend}
            # 模型映射展示：短名（多后端 key 的 model=xxx 选择器）+ claude 七名映射
            mappings: list[dict[str, str]] = []
            short_map = {k: v for k, v in aliases.items() if not k.startswith("claude-")}
            cc_map = {k: v for k, v in aliases.items() if k.startswith("claude-")}
            for k, v in sorted(short_map.items()):
                mappings.append({"from": k, "to": v})
            # claude 七名：若全是 identity（同名）或指向同一目标，摘要成一行；否则逐条列
            unique_targets = set(cc_map.values())
            if len(unique_targets) == 1 and len(cc_map) >= 7:
                mappings.append({"from": "claude 七名（默认）", "to": next(iter(unique_targets))})
            else:
                for k, v in sorted(cc_map.items()):
                    mappings.append({"from": k, "to": v})
            out.append({
                "alias": info.get("key_alias") or "-",
                "user": info.get("user_id") or "-",
                "backend": ", ".join(sorted(bks)) if bks else "?",
                "hash": h,
                "spend": info.get("spend") or 0,
                "max_budget": info.get("max_budget"),
                "mappings": mappings,
                # 该 key 是否压缩：全局默认开，只有打了 disable 标记的才是关
                "headroom": not (info.get("metadata") or {}).get(_HEADROOM_DISABLE_FIELD, False),
            })
    return out
