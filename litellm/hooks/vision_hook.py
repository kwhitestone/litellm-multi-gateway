"""
VisionPreRequestHook - litellm CustomLogger，在请求发往后端前改写 messages。

取代旧的独立 vision 容器：作为 litellm 进程内的 hook 运行，
按模型的 needs_vision 配置精确决定转图/透传。

做的事（in-place 改 messages，因为下游 handler 用原 messages 变量）：
  - needs_vision=true 的模型：图片块用视觉模型转成文字描述（纯文本后端需要）
  - needs_vision=false 的模型：图片块原图透传（原生多模态后端）
  - 所有模型：剥离 thinking/server_tool_use 及配对 tool_result（协议归一化）
  - 所有模型：剥掉 cache_control.ttl 避免 1h/5m 顺序冲突
  - 所有模型：移除 tool_result 内的 tool_reference block
    （上游 API/网关不一定支持此 block，会报 "Tool reference not found" 400）

挂两个 hook，因为 litellm 两条请求链的钩子点不同：
  1) async_pre_call_hook   —— proxy 层通用，覆盖 /v1/chat/completions、/v1/messages
                              等所有端点（proxy/common_request_processing.py 调用）。
                              在 router 路由**之前**跑，data["model"] 是 config 里的
                              model_name 别名（如 zai-glm-5.2）。
  2) async_pre_request_hook —— 只有 anthropic pass-through(/v1/messages) 一个调用点
                              (llms/anthropic/experimental_pass_through/messages/handler.py)，
                              chat/completions 链上根本没人调它——这就是它对
                              completions 无效的原因。在路由**之后**跑，model 是
                              litellm_params.model（如 anthropic/glm-5.2）。

所以 needs_vision 映射按「别名」和「litellm_params.model」两套 key 都建一份。
/v1/messages 会先后跑到两个 hook：第一遍转完图后已无 image/thinking 块，
第二遍是 no-op（图片=0 剥离块=0），无副作用。

注意：proxy 层有个短路优化（proxy/utils.py `has_pre_call_override`），只有叶子类
__dict__ 里真的定义了 async_pre_call_hook 才会进回调遍历——不能只靠继承基类。
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


# 视觉模型（默认智谱 glm-5v-turbo，可换任何 OpenAI 兼容视觉模型）
VISION_API_KEY = os.environ.get("VISION_API_KEY", "") or os.environ.get("Z_AI_API_KEY", "")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
VISION_MODEL = os.environ.get("VISION_MODEL", "glm-5v-turbo")
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
# 两个 hook 拿到的 model 形态不同（见模块 docstring）：
#   - async_pre_call_hook   -> model_name 别名，如 zai-glm-5.2
#   - async_pre_request_hook -> litellm_params.model，如 anthropic/glm-5.2
# 所以 per_model 里两种 key 都塞，任一命中即可。
# config.yaml 里两种标记：
#   - 头部全局 `# native_vision: true/false`（单后端 profile 用，回退默认）
#   - model_name 行内 `# needs_vision: true/false`（multi profile 用，精确到模型）
class _VisionConfig(NamedTuple):
    per_model: Dict[str, bool]  # model_name 别名 / litellm_params.model -> needs_vision
    global_default: bool        # 无 per-model 标记时的回退（头部 native_vision）

    def needs(self, model: str) -> bool:
        if not model:
            return self.global_default
        if model in self.per_model:
            return self.per_model[model]
        # 别名可能带 provider 前缀（openai/zai-glm-5.2）或反之，去掉一层前缀再试
        if "/" in model and model.split("/", 1)[1] in self.per_model:
            return self.per_model[model.split("/", 1)[1]]
        return self.global_default


def _parse_bool_after_colon(s: str) -> bool:
    """从 '... needs_vision: true  # 注释' 提取布尔值。按关键字定位，避免行内其它冒号干扰。"""
    for key in ("needs_vision:", "native_vision:"):
        if key in s:
            rest = s.split(key, 1)[1].strip().split()[0].lower()
            return rest.startswith("true")
    return False


_config_cache: Optional[_VisionConfig] = None


def _load_vision_config() -> _VisionConfig:
    """扫 config.yaml：头部 native_vision 作全局默认，model_list 里每条的 needs_vision 注释
    + model_name 别名 / litellm_params.model 字段建立 per-model 映射。

    结果进程内缓存：config.yaml 是只读挂载，进程生命周期内不会变，
    而这函数原先每个请求都重读一次文件（两个 hook 各一次）。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    global_default = False
    per_model: Dict[str, bool] = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        _log(f"config.yaml 未找到({CONFIG_PATH})，默认 needs_vision=false（透传）")
        _config_cache = _VisionConfig(per_model, global_default)
        return _config_cache

    in_model_list = False
    cur_nv: Optional[bool] = None      # 当前条目的 needs_vision（None=未标记）
    cur_model: Optional[str] = None    # 当前条目的 litellm_params.model 字段
    cur_alias: Optional[str] = None    # 当前条目的 model_name（proxy 层 hook 看到的）

    def _commit() -> None:
        # 别名和后端 model 都注册，两个 hook 各自的 model 形态都能命中
        if cur_nv is None:
            return
        for key in (cur_alias, cur_model):
            if key:
                per_model[key] = cur_nv

    for line in lines:
        stripped = line.strip()
        # 头部全局标记（model_list 之前）
        if not in_model_list and stripped.startswith("#") and "native_vision:" in stripped:
            global_default = _parse_bool_after_colon(stripped)
        if stripped == "model_list:":
            in_model_list = True
            continue
        # 退出 model_list 段：顶格非注释行（如 litellm_settings:）。用原始行缩进判断，
        # 不能用 stripped（litellm_params: 行 stripped 后也顶格，但它在 model_list 内）
        if in_model_list and stripped and not line.startswith((" ", "\t")) and not stripped.startswith("#"):
            _commit()   # 段结束前提交最后一条，否则最后一个模型丢标记
            cur_nv, cur_model, cur_alias = None, None, None
            in_model_list = False
        if not in_model_list:
            continue
        # 新条目开始：先把上一条提交
        if stripped.startswith("- model_name:"):
            _commit()
            cur_nv, cur_model, cur_alias = None, None, None
            # 行内 needs_vision 注释
            if "needs_vision:" in stripped:
                cur_nv = _parse_bool_after_colon(stripped)
            # model_name 别名（去掉行尾 # 注释）
            alias = stripped.split("model_name:", 1)[1].split("#")[0].strip().strip('"').strip("'")
            if alias:
                cur_alias = alias
        elif cur_model is None and "needs_vision:" in stripped and stripped.startswith("#"):
            # model_name 下一行的独立 needs_vision 注释
            cur_nv = _parse_bool_after_colon(stripped)
        # 提取 litellm_params.model 字段（flow 或多行都可能有 "model: xxx"）
        if "model:" in stripped and "model_name:" not in stripped:
            # 取 model: 后的值，去掉 provider 前缀的引号/逗号
            val = stripped.split("model:", 1)[1].split(",")[0].split("}")[0].strip().strip('"').strip("'")
            if val and not val.startswith("#"):
                cur_model = val
    # 提交最后一条（config 以 model_list 结尾、没有后续顶格段时）
    _commit()
    _config_cache = _VisionConfig(per_model, global_default)
    _log(f"vision config 已加载: {len(per_model)} 个 model key, global_default={global_default}")
    return _config_cache


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
        # 视觉模型(glm-5v-turbo 等)的 reasoning_content 也算在 max_tokens 里，
        # 给太小会导致思考占满、content 返回空串。
        "max_tokens": 3000,
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
        # 视觉模型(glm-5v-turbo 等)的 reasoning_content 也算在 max_tokens 里，
        # 给太小会导致思考占满、content 返回空串。
        "max_tokens": 3000,
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
            if ctx["needs_vision"]:   # 后端需要转图（纯文本端点如 ark）
                desc = await _describe(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
            else:   # 后端原生多模态，原图透传
                out.append(block)
        elif btype == "image_url" and ctx["n_img"] < MAX_IMAGES:
            ctx["n_img"] += 1
            if ctx["needs_vision"]:   # 后端需要转图（纯文本端点如 ark）
                desc = await _describe_openai(block)
                out.append({"type": "text", "text": f"[image, described by {VISION_MODEL}]\n{desc}"})
            else:   # 后端原生多模态，原图透传
                out.append(block)
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
        elif btype == "tool_reference":
            # tool_reference block is metadata in tool_result.content that
            # upstream API/gateway may not support -> 400 "Tool reference not found".
            # Remove all tool_reference blocks; they're just markers, not conversation logic.
            ctx["n_drop"] += 1
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

async def _transform(model: str, messages: List, container: Dict, where: str) -> Dict:
    """两个 hook 共用的改写逻辑。messages in-place 改（下游 handler 持有原 list 引用），
    container 是承载 system/tools 的 dict（pre_request 是 kwargs，pre_call 是 data）。"""
    needs_vision = _load_vision_config().needs(model)

    # 剥 cache_control ttl（对所有模型）。只走 messages/system/tools，
    # 不整体递归 container：pre_call 的 data 里挂着 metadata/logging_obj 等大对象。
    _strip_cache_ttl(messages)
    for key in ("system", "tools"):
        if key in container:
            _strip_cache_ttl(container[key])

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
            m["content"] = new_content  # in-place 改（下游 handler 用原 messages 变量）

    # system 也处理（Anthropic 格式的 system 是 block 数组；OpenAI 格式没这字段）
    system = container.get("system")
    if isinstance(system, list):
        container["system"] = await _walk(system, ctx)

    mode = "转文字" if needs_vision else "原图透传"
    _log(
        f"[{where}] model={model} needs_vision={needs_vision}({mode}) "
        f"图片={ctx['n_img']} 剥离块={ctx['n_drop']}"
    )
    return container


class _VisionPreRequestHook(CustomLogger):
    """按模型 needs_vision 决定转图/透传 + 协议归一化。

    两个 hook 都实现（调用路径不同，见模块 docstring）：
      - async_pre_call_hook    覆盖 /v1/chat/completions 等所有 proxy 端点
      - async_pre_request_hook 只覆盖 anthropic pass-through /v1/messages
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict,
        call_type: str,
    ) -> Optional[Dict]:
        """proxy 层通用钩子。data["model"] 是 config 的 model_name 别名（未路由）。
        返回 dict 会被 litellm 当作改写后的 data 使用。"""
        if not isinstance(data, dict):
            return None
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        return await _transform(data.get("model") or "", messages, data, f"pre_call/{call_type}")

    async def async_pre_request_hook(
        self, model: str, messages: List, kwargs: Dict
    ) -> Optional[Dict]:
        """anthropic pass-through 钩子。model 是 litellm_params.model（已路由）。
        /v1/messages 上 pre_call 已跑过一遍，这里通常是 no-op。"""
        if not isinstance(messages, list) or not messages:
            return None
        return await _transform(model, messages, kwargs, "pre_request")


# litellm config 的 callbacks 加载的是模块级属性，必须是实例（litellm 不自动实例化类）。
# config: callbacks: [hooks.vision_hook.VisionPreRequestHook]
VisionPreRequestHook = _VisionPreRequestHook()

