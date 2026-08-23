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

认证：会话 Cookie（HMAC 签名，滑动过期 12h），master key 只在登录时验一次，
之后全程不出现在 URL/响应里。登录有审计（stdout + postgres）。

后端→别名的映射逻辑直接 import 容器里的 gen_config.py，与 keys.sh 完全一致。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, audit, backends_store

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

    if len(update_body) == 1:  # 只有 key，没实际改动字段
        raise HTTPException(400, "至少要传 backend 或 max_budget/rpm/tpm")

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


# ---------- backends.yaml 编辑（PG 存储，重启生效） ----------

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
    })
    auth.renew_session(resp)
    return resp


@router.post("/api/backends", dependencies=[Depends(auth.require_session)])
async def save_backends(request: Request) -> JSONResponse:
    """保存 backends.yaml。body: {content: <yaml 全文>}
    校验（gen_config 试生成 config.yaml）通过才入库；重启容器后生效。"""
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
    resp = JSONResponse({"ok": True})
    auth.renew_session(resp)
    return resp


# ---------- 辅助：调 LiteLLM key 管理 API ----------

async def _list_key_hashes() -> list[str]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{LITELLM_BASE}/key/list", headers={"Authorization": f"Bearer {MASTER_KEY}"})
    if r.status_code != 200:
        return []
    return r.json().get("keys", [])


async def _fetch_keys() -> list[dict[str, Any]]:
    """返回 key 信息列表（alias/user/后端/hash/spend/budget/mappings），供模板渲染。"""
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
            bks: set[str] = set()
            for v in aliases.values():
                v = str(v)
                if v.startswith("claude_1-"):
                    bks.add("claude_1")
                elif v.startswith("claude_2-"):
                    bks.add("claude_2")
                elif v.startswith("ark-"):
                    bks.add("ark")
                elif v.startswith("zai-"):
                    bks.add("zai")
                elif v.startswith("claude"):
                    bks.add("claude")
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
            })
    return out
