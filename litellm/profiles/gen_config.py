#!/usr/bin/env python3
"""
gen_config.py — backends.yaml 的解析器 + multi.yaml 生成器 + 映射解析器。

被 keys.sh 调用，也可直接跑：
  python3 gen_config.py gen-config [--backends PATH] [--out PATH]
      读 backends.yaml，生成 multi.yaml（默认同目录）
  python3 gen_config.py aliases <backend> [--backends PATH]
      打印该后端的 {aliases, models} JSON（keys.sh new/update 用）
  python3 gen_config.py multi-aliases <backend,backend,...> [--backends PATH]
      打印多后端 key 的 {aliases, models} JSON（keys.sh new 多后端分支用）

backends.yaml 的 mapping 语法见该文件头部注释。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("错误：缺 pyyaml。装一下：pip install pyyaml")

HERE = Path(__file__).resolve().parent
DEFAULT_BACKENDS = HERE / "backends.yaml"
DEFAULT_OUT = HERE / "multi.yaml"

# ---- headroom 提示词压缩（LiteLLM 内置 guardrail，纯环境变量开关）----
# HEADROOM_API_BASE 有值即启用：生成 guardrails 段，LiteLLM 在 pre_call 把 messages
# 发到 {api_base}/v1/compress 压缩后再转发上游。为空/未设置则完全不生成该段
# （HeadroomGuardrail.__init__ 缺 api_base 会抛 ValueError 直接起不来，所以必须由这里控制）。
#
# 「按 key 开关」走 default_on=true + 逐 key 关闭（B-lite）：给 key 挂 guardrails=[...]
# 是企业版字段，无 LITELLM_LICENSE 时 /key/generate 直接 403，所以反过来做——
# 全局默认压缩，不想压的 key 写 metadata.disable_global_guardrails=true。
HEADROOM_GUARDRAIL_NAME = "headroom-compression"
# 关 key 级压缩的 metadata 字段名（LiteLLM 官方字段，见 custom_guardrail.py
# get_disable_global_guardrail / litellm_pre_call_utils.py add_key_level_controls）
HEADROOM_DISABLE_FIELD = "disable_global_guardrails"


def headroom_settings(env: dict | None = None) -> dict | None:
    """从环境变量解析 headroom 配置。未启用返回 None。

    HEADROOM_API_BASE        压缩服务地址（唯一开关，空=停用）
    HEADROOM_API_KEY         可选 Bearer token。这里不读值，只生成 os.environ/ 引用，
                             密钥不落进 config.yaml（与 provider key 同款处理）
    HEADROOM_DEFAULT_ON      默认 true=所有请求都压缩，逐 key 用 metadata
                             disable_global_guardrails=true 关（B-lite 语义）。
                             显式设 false 回到旧语义（只有挂了该 guardrail 的 key 压缩），
                             但挂 guardrails 需要企业版 license，无 license 会 403。
    HEADROOM_UNREACHABLE     fail_open（默认，压缩服务挂了放行未压缩请求）| fail_closed（报错）
    """
    env = os.environ if env is None else env
    api_base = (env.get("HEADROOM_API_BASE") or "").strip()
    if not api_base:
        return None
    fallback = (env.get("HEADROOM_UNREACHABLE") or "fail_open").strip().lower()
    if fallback not in ("fail_open", "fail_closed"):
        sys.exit(f"错误：HEADROOM_UNREACHABLE 只能是 fail_open / fail_closed，收到 {fallback!r}")
    return {
        "api_base": api_base,
        # 有 HEADROOM_API_KEY 才写 api_key，且写成 os.environ/ 引用而非明文
        "api_key": "os.environ/HEADROOM_API_KEY" if (env.get("HEADROOM_API_KEY") or "").strip() else None,
        # 默认 true（B-lite）：不设该变量 = 全局压缩，逐 key 关。显式 false/0/no 才关全局。
        "default_on": (env.get("HEADROOM_DEFAULT_ON") or "true").strip().lower() not in ("0", "false", "no"),
        "unreachable_fallback": fallback,
    }


def load_backends(path: Path = DEFAULT_BACKENDS) -> dict:
    if not path.exists():
        sys.exit(f"错误：找不到 {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def claude_names(cfg: dict) -> list[str]:
    """CC 七名列表 = claude 后端的 models key（单一真相源，不再硬编码）。"""
    claude = cfg["backends"].get("claude")
    if not claude:
        sys.exit("错误：backends.yaml 里没有 claude 后端，无法确定 claude 名列表")
    return list(claude["models"].keys())


def model_name_for(backend: str, model: str) -> str:
    """非 claude 后端拼 <backend>-<model>；claude 后端用模型名本身。"""
    if backend == "claude":
        return model
    return f"{backend}-{model}"


def _match_pattern(name: str, pattern: str) -> bool:
    """精确名 或 prefix-* 通配（* 只能在末尾）。"""
    if pattern == "*":
        return True
    if pattern == name:
        return True
    if pattern.endswith("-*"):
        prefix = pattern[:-1]  # 去掉末尾 *，保留连字符：claude-haiku-*
        return name.startswith(prefix)
    return False


def resolve_mapping(backend: str, cfg: dict) -> dict[str, str]:
    """返回 {claude名: model_name}。claude 后端无 mapping=identity。"""
    cc = claude_names(cfg)
    b = cfg["backends"].get(backend)
    if not b:
        sys.exit(f"错误：未知后端 {backend!r}，可选：{', '.join(cfg['backends'])}")

    mapping = b.get("mapping")
    if not mapping:
        # identity：claude 名映射到同名 model_name
        return {c: model_name_for(backend, c) for c in cc}

    # 有 mapping：按特异性解析（精确名 > prefix-* > *）。低特异性先记，高特异性覆盖。
    def specificity(pat: str) -> int:
        if pat == "*":
            return 0
        if pat.endswith("-*"):
            return 1
        return 2  # 精确名

    rules = sorted(mapping.items(), key=lambda kv: specificity(kv[0]))
    result: dict[str, str] = {}
    for name in cc:
        hit = None
        for pat, target in rules:  # 升序遍历，后写的（更高特异性）覆盖
            if _match_pattern(name, pat):
                hit = target
        if hit is None:
            sys.exit(f"错误：后端 {backend} 的 mapping 没有匹配 {name!r} 的规则（缺 \"*\" 默认？）")
        if hit not in b["models"]:
            sys.exit(f"错误：后端 {backend} 的 mapping 把 {name!r} 指向 {hit!r}，但 models 里没有它")
        result[name] = model_name_for(backend, hit)
    return result


def gen_multi_yaml(cfg: dict) -> str:
    """从 backends.cfg 生成 multi.yaml 文本。"""
    lines = [
        "# profile: multi",
        "# desc: 多后端共存，按 key aliases 路由到不同后端",
        "#",
        "# ⚠️ 自动生成，勿手改。改 litellm/profiles/backends.yaml 后跑：./keys.sh gen-config",
        "#",
        "---",
        "model_list:",
    ]
    for backend, b in cfg["backends"].items():
        nv = b.get("needs_vision", False)
        st = b.get("strip_thinking", False)
        lines.append(f"  # ===== {backend}（needs_vision={str(nv).lower()}）=====")
        for model, spec in b["models"].items():
            mn = model_name_for(backend, model)
            lm = spec["litellm_model"]
            api_base = b["api_base"]
            key_env = b["key_env"]
            lines.append(
                f"  - model_name: {mn}   # needs_vision: {str(nv).lower()} "
                f"strip_thinking: {str(st).lower()}"
            )
            lines.append(
                f"    litellm_params: {{ model: {lm}, api_base: {api_base}, "
                f"api_key: os.environ/{key_env} }}"
            )
        lines.append("")

    # headroom 压缩：HEADROOM_API_BASE 配了才生成（见 headroom_settings 注释）
    hr = headroom_settings()
    if hr:
        lines.append("# ===== headroom 提示词压缩（由 HEADROOM_API_BASE 环境变量启用）=====")
        lines.append("guardrails:")
        lines.append(f"  - guardrail_name: {HEADROOM_GUARDRAIL_NAME}")
        lines.append("    litellm_params:")
        lines.append("      guardrail: headroom")
        lines.append("      mode: pre_call")
        lines.append(f"      api_base: {hr['api_base']}")
        if hr["api_key"]:
            lines.append(f"      api_key: {hr['api_key']}")
        lines.append(f"      default_on: {'true' if hr['default_on'] else 'false'}")
        lines.append(f"      unreachable_fallback: {hr['unreachable_fallback']}")
        lines.append("")

    # litellm_settings / general_settings 原样输出
    for section in ("litellm_settings", "general_settings"):
        if section in cfg:
            lines.append(f"{section}:")
            for k, v in cfg[section].items():
                lines.append(f"  {k}: {_scalar(v)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _scalar(v) -> str:
    """把 yaml 值序列化成行内标量：list/dict 用 flow style，其余原样。"""
    if isinstance(v, (list, dict)):
        return yaml.dump(v, default_flow_style=True).strip()
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return str(v)


def _split_opts(args: list[str]) -> tuple[list[str], Path]:
    """从 args 里抽出 --backends PATH（可选），返回 (位置参数, backends路径)。"""
    positional = []
    backends = DEFAULT_BACKENDS
    i = 0
    while i < len(args):
        if args[i] == "--backends" and i + 1 < len(args):
            backends = Path(args[i + 1])
            i += 2
        elif args[i].startswith("--backends="):
            backends = Path(args[i].split("=", 1)[1])
            i += 1
        else:
            positional.append(args[i])
            i += 1
    return positional, backends


def cmd_gen_config(args: list[str]) -> None:
    positional, backends = _split_opts(args)
    out = Path(positional[0]) if positional else DEFAULT_OUT
    cfg = load_backends(backends)
    text = gen_multi_yaml(cfg)
    out.write_text(text, encoding="utf-8")
    print(f"✓ 已生成 {out}（{len(cfg['backends'])} 个后端）")


def cmd_aliases(args: list[str]) -> None:
    """单后端：打印 {aliases, models} JSON。"""
    positional, backends = _split_opts(args)
    backend = positional[0]
    cfg = load_backends(backends)
    aliases = resolve_mapping(backend, cfg)
    cc = claude_names(cfg)
    b_models = {model_name_for(backend, m) for m in cfg["backends"][backend]["models"]}
    models = sorted(set(cc) | b_models)
    print(json.dumps({"aliases": aliases, "models": models}))


def cmd_multi_aliases(args: list[str]) -> None:
    """多后端：短名选后端 + 7 名默认指 claude-sonnet-5（若含 claude）。"""
    positional, backends = _split_opts(args)
    backend_list = [x.strip() for x in positional[0].split(",") if x.strip()]
    cfg = load_backends(backends)
    available = set(cfg["backends"])
    for b in backend_list:
        if b not in available:
            sys.exit(f"错误：未知后端 {b!r}，可选：{', '.join(available)}")

    cc = claude_names(cfg)
    short = {}
    for b in backend_list:
        models = list(cfg["backends"][b]["models"].keys())
        short[b] = model_name_for(b, models[0])

    aliases = {b: short[b] for b in backend_list}
    default_target = "claude-sonnet-5" if "claude" in backend_list else short[backend_list[0]]
    for c in cc:
        aliases[c] = default_target

    all_models = set(cc)
    for b in backend_list:
        for m in cfg["backends"][b]["models"]:
            all_models.add(model_name_for(b, m))
    print(json.dumps({"aliases": aliases, "models": sorted(all_models)}))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    rest = sys.argv[2:]
    if cmd == "gen-config":
        cmd_gen_config(rest)
    elif cmd == "aliases":
        cmd_aliases(rest)
    elif cmd == "multi-aliases":
        cmd_multi_aliases(rest)
    else:
        sys.exit(f"未知命令 {cmd!r}。可用：gen-config / aliases / multi-aliases")


if __name__ == "__main__":
    main()
