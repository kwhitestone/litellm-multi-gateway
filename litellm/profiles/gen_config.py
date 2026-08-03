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
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("错误：缺 pyyaml。装一下：pip install pyyaml")

HERE = Path(__file__).resolve().parent
DEFAULT_BACKENDS = HERE / "backends.yaml"
DEFAULT_OUT = HERE / "multi.yaml"


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
        lines.append(f"  # ===== {backend}（needs_vision={str(nv).lower()}）=====")
        for model, spec in b["models"].items():
            mn = model_name_for(backend, model)
            lm = spec["litellm_model"]
            api_base = b["api_base"]
            key_env = b["key_env"]
            lines.append(f"  - model_name: {mn}   # needs_vision: {str(nv).lower()}")
            lines.append(
                f"    litellm_params: {{ model: {lm}, api_base: {api_base}, "
                f"api_key: os.environ/{key_env} }}"
            )
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
