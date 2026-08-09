"""
manage/auth.py — 管理页会话认证。

设计：
  - 登录页输 master key → 验证 → 签发 HttpOnly Cookie（HMAC 签名，含过期时间戳）
  - 滑动过期：每次合法请求自动续命 12 小时（更新 Cookie 过期时间）
  - master key 永不出现在 URL / 响应体里，只验一次后用 Cookie 维持会话
  - Cookie 值 = base64(payload).hmac，篡改任意一节都验不过

不依赖外部 session store：Cookie 自带过期时间 + HMAC 防伪，服务端无状态。
代价：登出靠客户端清 Cookie（服务端记审计，但无法吊销已签发 Cookie）——
对单人管理页够用；要吊销改 GATEWAY_MASTER_KEY（密钥变=所有旧 Cookie 失效）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Request, Response

MASTER_KEY = os.environ.get("GATEWAY_MASTER_KEY", "")
SESSION_TTL = int(os.environ.get("MANAGE_SESSION_TTL_HOURS", "12")) * 3600  # 滑动窗口秒数
COOKIE_NAME = "manage_session"
# 签名密钥 = master key 本身（变了=所有会话失效，正好是吊销语义）
_SIGNING_KEY = (MASTER_KEY or "unset").encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(expires_at: int, now: Optional[int] = None) -> str:
    """生成签名 Cookie 值。payload = {exp: 过期时间戳}。"""
    if now is None:
        now = int(time.time())
    payload = _b64(json.dumps({"exp": expires_at, "iat": now}).encode())
    sig = _b64(hmac.new(_SIGNING_KEY, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_session(cookie_val: str) -> bool:
    """验证 Cookie 签名 + 是否过期。"""
    if not cookie_val or "." not in cookie_val:
        return False
    payload_b64, _, sig_b64 = cookie_val.partition(".")
    expected_sig = _b64(hmac.new(_SIGNING_KEY, payload_b64.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig_b64, expected_sig):
        return False  # 签名不符（篡改或密钥已变）
    try:
        payload = json.loads(_unb64(payload_b64))
        return int(payload.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def issue_session(response: Response) -> None:
    """签发新会话 Cookie（过期 = 现在 + TTL），设到 response。"""
    expires_at = int(time.time()) + SESSION_TTL
    response.set_cookie(
        COOKIE_NAME, sign_session(expires_at),
        max_age=SESSION_TTL, httponly=True, samesite="lax",
        secure=os.environ.get("MANAGE_COOKIE_SECURE", "").lower() in ("1", "true"),
        path="/manage",
    )


def renew_session(response: Response) -> None:
    """滑动续命：合法请求时重设 Cookie 过期 = 现在 + TTL。"""
    issue_session(response)


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/manage")


def get_session(request: Request) -> Optional[str]:
    """读请求里的会话 Cookie 值（无则 None）。"""
    return request.cookies.get(COOKIE_NAME)


def require_session(request: Request):
    """FastAPI 依赖：未登录或会话过期 → 302 重定向到登录页（用 HTTPException + Location header）。

    用在所有需要登录的 /manage/* 路由上。"""
    from fastapi import HTTPException
    if not MASTER_KEY:
        raise HTTPException(503, "GATEWAY_MASTER_KEY 未配置，管理页不可用")
    cookie = get_session(request)
    if cookie and verify_session(cookie):
        return  # 合法
    # 未登录：用 302 + Location 重定向到登录页（FastAPI 依赖里只能抛 HTTPException，
    # 不能直接返回 Response；这里靠 headers 携带 Location 实现跳转）
    raise HTTPException(
        status_code=302,
        detail="未登录",
        headers={"Location": "/manage/login?next=" + request.url.path},
    )


def has_valid_session(request: Request) -> bool:
    """登录页用：判断当前是否已登录（已登录就不该再显示登录表单）。"""
    cookie = get_session(request)
    return bool(cookie and verify_session(cookie))


def check_master_key(submitted: str) -> bool:
    """登录提交的 key 是否正确（常量时间比较）。"""
    if not MASTER_KEY or not submitted:
        return False
    return hmac.compare_digest(submitted, MASTER_KEY)
