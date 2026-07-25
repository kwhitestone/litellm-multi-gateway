"""
vision 前置层：常驻在客户端与 LiteLLM 之间，按当前 profile 决定图片处理方式。

  Claude Code -> vision(:4001) -> litellm(:4000) -> 你的 provider(ark/zai/claude/…)

做的事：
  - 读 config.yaml 头部的 `# native_vision:` 标记，决定图片处理方式：
      true  (zai/claude 等原生多模态后端) -> 原图透传，不转文字
      false (ark 等纯文本后端)            -> 用视觉模型把图片转成文字描述
  - 归一化纯文本端点不认的块：thinking / redacted_thinking / server_tool_use 及其配对 tool_result
    （这部分与视觉无关，所有 profile 都做）
  - 其余原样转发到 LiteLLM，SSE 流式响应原样回传（tool_use 等不受影响）

客户端固定指向 :4001，切 profile 时 vision 读最新的 config.yaml，无需重启即可切换行为。
视觉模型可换（默认智谱 glm-4.6v），改 VISION_BASE_URL / VISION_MODEL 即可（OpenAI 兼容接口都行）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


def _log(msg: str) -> None:
    print(f"[vision] {msg}", file=sys.stderr, flush=True)


# 下游 LiteLLM。容器内用服务名 litellm；宿主机直连改成 http://127.0.0.1:4000
DOWNSTREAM = os.environ.get("DOWNSTREAM_URL", "http://litellm:4000").rstrip("/")
# 转发时带的鉴权（= LiteLLM 的 master_key，默认与上游 provider key 同一个）
DOWNSTREAM_KEY = os.environ.get("DOWNSTREAM_KEY", "") or os.environ.get("ARK_API_KEY", "")

# 视觉模型（默认智谱 glm-4.6v，可换任何 OpenAI 兼容的视觉模型）
VISION_API_KEY = os.environ.get("VISION_API_KEY", "") or os.environ.get("Z_AI_API_KEY", "")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
VISION_MODEL = os.environ.get("VISION_MODEL", "glm-4.6v")
MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES_PER_REQUEST", "20"))
DESC_PROMPT = os.environ.get(
    "VISION_DESC_PROMPT",
    "你是给编程助手看图的助手。请精确描述这张图：逐字引用可见的文字/标签，"
    "说明布局与结构、UI 元素、报错信息；颜色只在有意义时提。用图中主要文字的语言回答。",
)

REQ_HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive",
           "authorization", "x-api-key"}
RESP_HOP = {"content-length", "transfer-encoding", "connection"}

_cache: dict[str, str] = {}


# ---------- 当前后端是否原生支持视觉 ----------
# 读 litellm 挂载进来的 config.yaml（由 profiles.sh switch 生成）头部注释
#   # native_vision: true   -> 后端能看图，图片原图透传
#   # native_vision: false  -> 纯文本后端，图片转文字（默认，向后兼容）
# 每次请求重读，支持切 profile 后无需重启 vision 即时生效。
CONFIG_PATH = os.environ.get("LITELLM_CONFIG_PATH", "/app/config.yaml")


def _native_vision() -> bool:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("#"):
                    break  # 注释段结束（遇到 --- 或 yaml 正文）
                if "native_vision:" in line:
                    return line.split("native_vision:", 1)[1].strip().lower().startswith("true")
    except FileNotFoundError:
        _log(f"config.yaml 未找到({CONFIG_PATH})，native_vision 默认 false")
    except Exception as exc:
        _log(f"读 native_vision 失败，默认 false: {exc!r}")
    return False


# ---------- 图片 -> 文字 ----------

async def _describe(block: dict) -> str:
    if not VISION_API_KEY:
        return "[image skipped: VISION_API_KEY not set]"
    src = block.get("source") or {}
    media_type = src.get("media_type", "image/png")
    b64 = src.get("data") or ""
    if not b64:
        if src.get("type") == "url" and src.get("url"):
            return f"[image url: {src.get('url')}] (url 图片未预描述)"
        return "[image: empty]"
    key = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:16]
    if key in _cache:
        return _cache[key]
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": DESC_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
            ],
        }],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"}
    _log(f"调用 {VISION_MODEL} 描述图片({key}, base64 长度={len(b64)})...")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
            r = await c.post(f"{VISION_BASE_URL}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        _log(f"视觉模型调用失败: {exc!r}")
        return f"[image describe failed: {exc}]"
    text = (text or "").strip()
    _cache[key] = text
    return text


async def _describe_openai(block: dict) -> str:
    """处理 OpenAI 格式的 image_url 块：{image_url:{url:"data:..." 或 "https://..."}}。"""
    if not VISION_API_KEY:
        return "[image skipped: VISION_API_KEY not set]"
    url = ((block.get("image_url") or {}).get("url")) or ""
    if not url:
        return "[image: empty]"
    # data URL: data:image/png;base64,xxxx
    if url.startswith("data:"):
        # 解析出 media_type 和 base64，复用缓存逻辑
        header, _, b64 = url.partition(",")
        media_type = "image/png"
        if ";" in header and "/" in header:
            media_type = header.split(":")[1].split(";")[0]
        # 复用 _describe 的缓存（按 base64 内容哈希）
        return await _describe({"source": {"media_type": media_type, "data": b64, "type": "base64"}})
    # http(s) URL：视觉模型多数支持直接传 URL
    key = "url:" + url[:64]
    if key in _cache:
        return _cache[key]
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": DESC_PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"}
    _log(f"调用 {VISION_MODEL} 描述图片(url, {url[:60]}...)...")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
            r = await c.post(f"{VISION_BASE_URL}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        _log(f"视觉模型调用失败(url): {exc!r}")
        return f"[image describe failed: {exc}]"
    text = (text or "").strip()
    _cache[key] = text
    return text


async def _walk(blocks: list, ctx: dict) -> list:
    out: list = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        # Anthropic 格式: {type:"image", source:{...}}
        if btype == "image" and ctx["n_img"] < MAX_IMAGES:
            ctx["n_img"] += 1
            if ctx["native_vision"]:
                out.append(block)  # 后端原生支持视觉 -> 原图透传
            else:
                desc = await _describe(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
        # OpenAI 格式: {type:"image_url", image_url:{url:"data:..." 或 "https://..."}}
        elif btype == "image_url" and ctx["n_img"] < MAX_IMAGES:
            ctx["n_img"] += 1
            if ctx["native_vision"]:
                out.append(block)  # 后端原生支持视觉 -> 原图透传
            else:
                desc = await _describe_openai(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
        elif btype in ("thinking", "redacted_thinking"):
            ctx["n_drop"] += 1  # 纯文本端点不接受推理块作为输入
        elif btype == "server_tool_use":
            ctx["n_drop"] += 1  # web_search 等服务端工具，下游没有
        elif btype == "tool_result" and block.get("tool_use_id") in ctx["server_ids"]:
            ctx["n_drop"] += 1  # 对应已删除的 server_tool_use
        elif btype == "tool_result" and isinstance(block.get("content"), list):
            nb = dict(block)
            nb["content"] = await _walk(block["content"], ctx)
            out.append(nb)
        else:
            out.append(block)
    return out


async def _transform(data: dict[str, Any]) -> dict[str, Any]:
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return data

    server_ids = set()
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("id"):
                    server_ids.add(b["id"])

    ctx = {"n_img": 0, "n_drop": 0, "server_ids": server_ids, "native_vision": _native_vision()}
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            m["content"] = await _walk(m["content"], ctx)
            if not m["content"]:
                m["content"] = [{"type": "text", "text": ""}]
    sysv = data.get("system")
    if isinstance(sysv, list):
        data["system"] = await _walk(sysv, ctx)

    mode = "原图透传" if ctx["native_vision"] else "转文字"
    _log(f"图片 {ctx['n_img']} 张（{mode}）；剥离 {ctx['n_drop']} 个非文本块"
         f"(thinking/server_tool_use/server_tool_result)")
    return data


# ---------- 透传 ----------

async def handle(request: Request):
    raw = await request.body()
    path = request.url.path

    if request.method == "POST" and (
        path.startswith("/v1/messages") or path.startswith("/v1/chat/completions")
    ):
        try:
            data = json.loads(raw) if raw else None
            if isinstance(data, dict):
                data = await _transform(data)
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as exc:
            _log(f"transform 抛异常，透传原请求: {exc!r}")

    # 优先保留客户端自带的 Authorization（虚拟 key），让 litellm 按 key 识别 user/统计用量；
    # 客户端没带时才用 DOWNSTREAM_KEY（master key）兜底。
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in REQ_HOP}
    client_auth = request.headers.get("authorization") or request.headers.get("x-api-key")
    if client_auth:
        fwd_headers["authorization"] = client_auth
    elif DOWNSTREAM_KEY:
        fwd_headers["authorization"] = f"Bearer {DOWNSTREAM_KEY}"
    if request.method in ("POST", "PUT", "PATCH"):
        fwd_headers["content-type"] = "application/json"

    url = DOWNSTREAM + path
    if request.url.query:
        url += "?" + request.url.query

    client = request.app.state.client
    req = client.build_request(request.method, url, content=raw or None, headers=fwd_headers)
    upstream = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESP_HOP}
    return StreamingResponse(gen(), status_code=upstream.status_code, headers=resp_headers)


@asynccontextmanager
async def lifespan(app):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    _log(f"started | downstream={DOWNSTREAM} | vision={VISION_MODEL} | "
         f"VISION_API_KEY={'set' if VISION_API_KEY else 'MISSING'}")
    yield
    await app.state.client.aclose()


routes = [Route("/{path:path}", handle, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])]
app = Starlette(routes=routes, lifespan=lifespan)
