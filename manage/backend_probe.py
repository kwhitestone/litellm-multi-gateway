"""按后端协议构造管理页的最小直连测试请求。"""
from typing import Any


def build_direct_request(
    api_base: str, litellm_model: str, key: str, prompt: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    provider, separator, model = litellm_model.partition("/")
    if not separator or not model or provider not in {"anthropic", "openai"}:
        raise ValueError("直连测试仅支持配置了模型名的 anthropic 或 openai 后端")

    common = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if provider == "openai":
        return (
            f"{api_base.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            {**common, "max_completion_tokens": 64},
        )
    return (
        f"{api_base.rstrip('/')}/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "Content-Type": "application/json"},
        {**common, "max_tokens": 64},
    )
