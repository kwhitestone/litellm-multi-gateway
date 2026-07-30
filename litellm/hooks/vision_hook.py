"""
VisionPreRequestHook - litellm CustomLogger，在 pass-through 请求发往后端前改写 messages。

取代旧的独立 vision 容器：作为 litellm 进程内的 pre-request hook 运行，
此时已知道真实后端模型，能按模型的 needs_vision 配置精确决定转图/透传。

做的事（in-place 改 messages，因为 pass-through handler 用原 messages 变量）：
  - needs_vision=true 的模型：图片块用视觉模型转成文字描述（纯文本后端需要）
  - needs_vision=false 的模型：图片块原图透传（原生多模态后端）
  - 所有模型：剥离 thinking/server_tool_use 及配对 tool_result（协议归一化）
  - 所有模型：剥掉 cache_control.ttl 避免 1h/5m 顺序冲突
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
from litellm.integrations.custom_logger import CustomLogger
from litellm._logging import verbose_logger


def _log(msg: str) -> None:
    print(f"[vision_hook] {msg}", file=sys.stderr, flush=True)


# 视觉模型（默认智谱 glm-4.6v，可换任何 OpenAI 兼容视觉模型）
VISION_API_KEY = os.environ.get("VISION_API_KEY", "") or os.environ.get("Z_AI_API_KEY", "")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
VISION_MODEL = os.environ.get("VISION_MODEL", "glm-4.6v")
MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES_PER_REQUEST", "20"))
DESC_PROMPT = os.environ.get(
    "VISION_DESC_PROMPT",
    "你是给编程助手看图的助手。请精确描述这张图：逐字引用可见的文字/标签，"
    "说明布局与结构、UI 元素、报错信息；颜色只在有意义时提。用图中主要文字的语言回答。",
)

CONFIG_PATH = os.environ.get("LITELLM_CONFIG_PATH", "/app/config.yaml")

_cache: dict[str, str] = {}


# ---------- 读 config.yaml：哪些 model_name 需要 vision 转图 ----------
# config.yaml 里每个 model_name 后跟注释 `# needs_vision: true/false`。
# hook 拿到的 model 是 litellm_params.model（带 provider 前缀，如 anthropic/glm-5.2）。
# 所以按 model 字段值建映射，精确知道每个后端模型是否需要转图。
# config.yaml 里两种标记：
#   - 头部全局 `# native_vision: true/false`（单后端 profile 用，回退默认）
#   - model_name 行内 `# needs_vision: true/false`（multi profile 用，精确到模型）
class _VisionConfig(NamedTuple):
    per_model: Dict[str, bool]  # litellm_params.model 字段值 -> needs_vision
    global_default: bool        # 无 per-model 标记时的回退（头部 native_vision）

    def needs(self, model: str) -> bool:
        if model and model in self.per_model:
            return self.per_model[model]
        return self.global_default


def _parse_bool_after_colon(s: str) -> bool:
    """从 'xxx: true   # 注释' 提取布尔值。"""
    rest = s.split(":", 1)[1].strip().split()[0].lower()
    return rest.startswith("true")


def _load_vision_config() -> _VisionConfig:
    """扫 config.yaml：头部 native_vision 作全局默认，model_list 里每条的 needs_vision 注释
    + litellm_params.model 字段建立 per-model 映射。"""
    global_default = False
    per_model: Dict[str, bool] = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        _log(f"config.yaml 未找到({CONFIG_PATH})，默认 needs_vision=false（透传）")
        return _VisionConfig(per_model, global_default)

    in_model_list = False
    cur_nv: Optional[bool] = None      # 当前条目的 needs_vision（None=未标记）
    cur_model: Optional[str] = None    # 当前条目的 litellm_params.model 字段
    for line in lines:
        stripped = line.strip()
        # 头部全局标记（model_list 之前）
        if not in_model_list and stripped.startswith("#") and "native_vision:" in stripped:
            global_default = _parse_bool_after_colon(stripped)
        if stripped == "model_list:":
            in_model_list = True
            continue
        if in_model_list and stripped and not stripped.startswith(("- ", " ", "\t", "#")):
            in_model_list = False  # 退出 model_list 段
        if not in_model_list:
            continue
        # 新条目开始：先把上一条提交
        if stripped.startswith("- model_name:"):
            if cur_model and cur_nv is not None:
                per_model[cur_model] = cur_nv
            cur_nv, cur_model = None, None
            # 行内 needs_vision 注释
            if "needs_vision:" in stripped:
                cur_nv = _parse_bool_after_colon(stripped)
        elif cur_model is None and "needs_vision:" in stripped and stripped.startswith("#"):
            # model_name 下一行的独立 needs_vision 注释
            cur_nv = _parse_bool_after_colon(stripped)
        # 提取 litellm_params.model 字段（flow 或多行都可能有 "model: xxx"）
        if "model:" in stripped and "model_name:" not in stripped:
            # 取 model: 后的值，去掉 provider 前缀的引号/逗号
            val = stripped.split("model:", 1)[1].split(",")[0].split("}")[0].strip().strip('"').strip("'")
            if val and not val.startswith("#"):
                cur_model = val
    # 提交最后一条
    if cur_model and cur_nv is not None:
        per_model[cur_model] = cur_nv
    return _VisionConfig(per_model, global_default)


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
    """处理 OpenAI 格式的 image_url 块。"""
    if not VISION_API_KEY:
        return "[image skipped: VISION_API_KEY not set]"
    url = ((block.get("image_url") or {}).get("url")) or ""
    if not url:
        return "[image: empty]"
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        media_type = "image/png"
        if ";" in header and "/" in header:
            media_type = header.split(":")[1].split(";")[0]
        return await _describe({"source": {"media_type": media_type, "data": b64, "type": "base64"}})
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
        if btype == "image" and ctx["n_img"] < MAX_IMAGES:
            ctx["n_img"] += 1
            if ctx["needs_vision"]:
                out.append(block)  # 原图透传
            else:
                desc = await _describe(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
        elif btype == "image_url" and ctx["n_img"] < MAX_IMAGES:
            ctx["n_img"] += 1
            if ctx["needs_vision"]:
                out.append(block)  # 原图透传
            else:
                desc = await _describe_openai(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
        elif btype in ("thinking", "redacted_thinking"):
            ctx["n_drop"] += 1
        elif btype == "server_tool_use":
            ctx["n_drop"] += 1
        elif btype == "tool_result" and block.get("tool_use_id") in ctx["server_ids"]:
            ctx["n_drop"] += 1
        elif btype == "tool_result" and isinstance(block.get("content"), list):
            nb = dict(block)
            nb["content"] = await _walk(block["content"], ctx)
            out.append(nb)
        else:
            out.append(block)
    return out


def _strip_cache_ttl(obj: Any) -> None:
    """剥掉所有 cache_control.ttl，避免 1h/5m 顺序冲突。"""
    if isinstance(obj, dict):
        cc = obj.get("cache_control")
        if isinstance(cc, dict) and "ttl" in cc:
            cc.pop("ttl", None)
        for v in obj.values():
            _strip_cache_ttl(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_cache_ttl(item)


# ---------- CustomLogger hook ----------

class _VisionPreRequestHook(CustomLogger):
    """litellm pre-request hook：按模型 needs_vision 决定转图/透传 + 协议归一化。"""

    async def async_pre_request_hook(
        self, model: str, messages: List, kwargs: Dict
    ) -> Optional[Dict]:
        if not isinstance(messages, list) or not messages:
            return None

        needs_vision = _load_vision_config().needs(model)

        # 剥 cache_control ttl（对所有模型）
        _strip_cache_ttl(kwargs)  # 含 tools/system 等
        _strip_cache_ttl(messages)

        # 收集 server_tool_use id（用于剥离配对 tool_result）
        server_ids = set()
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("id"):
                        server_ids.add(b["id"])

        ctx = {"n_img": 0, "n_drop": 0, "server_ids": server_ids, "needs_vision": needs_vision}
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                new_content = await _walk(m["content"], ctx)
                if not new_content:
                    new_content = [{"type": "text", "text": ""}]
                m["content"] = new_content  # in-place 改（pass-through handler 用原 messages 变量）

        # system 也处理
        system = kwargs.get("system")
        if isinstance(system, list):
            kwargs["system"] = await _walk(system, ctx)

        mode = "原图透传" if needs_vision else "转文字"
        _log(f"model={model} needs_vision={needs_vision}({mode}) 图片={ctx['n_img']} 剥离块={ctx['n_drop']}")
        return kwargs  # 返回 kwargs（messages 已 in-place 改）


# litellm config 的 callbacks 加载的是模块级属性，必须是实例（litellm 不自动实例化类）。
# config: callbacks: [hooks.vision_hook.VisionPreRequestHook]
VisionPreRequestHook = _VisionPreRequestHook()

