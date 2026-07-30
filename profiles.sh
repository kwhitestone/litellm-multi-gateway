#!/usr/bin/env bash
# profiles.sh - 管理后端配置文件（new / list / delete）。
#
#   ./profiles.sh                      列出后端配置 + 当前运行的（multi）
#   ./profiles.sh new <name> [opts]    生成新后端配置（交互式或带参数）
#   ./profiles.sh delete <name>        删除后端配置
#
# litellm 直接挂载 litellm/profiles/multi.yaml 作 config（多后端共存，无需 switch）。
# ark/zai/claude.yaml 是各后端的配置定义；new 生成的新后端需手动合并进 multi.yaml。
set -euo pipefail
cd "$(dirname "$0")"

PROFILES="litellm/profiles"
ACTIVE="litellm/profiles/multi.yaml"
BASE="http://127.0.0.1:4001"

# ---------- 公共：当前 profile ----------
current() {
  if [ ! -f "$ACTIVE" ]; then
    echo "(multi.yaml 不存在)"
    return
  fi
  grep -m1 '^# profile:' "$ACTIVE" 2>/dev/null | sed 's/^# profile: //' || echo "(未知)"
}

# ---------- 子命令：list ----------
cmd_list() {
  echo "当前后端: $(current)"
  echo "可选 profile:"
  for f in "$PROFILES"/*.yaml; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .yaml)
    desc=$(grep -m1 '^# desc:' "$f" | sed 's/^# desc: //')
    printf "   %-12s %s\n" "$name" "${desc:-}"
  done
  echo
  echo "用法: $0 <new|delete> [args]  （无参数 = list）"
}

# ---------- 子命令：delete ----------
cmd_delete() {
  local name="${1:?用法: $0 delete <name>}"
  local target="$PROFILES/$name.yaml"
  if [ ! -f "$target" ]; then
    echo "错误: 没有 profile '$name'" >&2; exit 1
  fi
  # multi.yaml 是 litellm 的运行 config，不让删
  if [ "$name" = "multi" ]; then
    echo "错误: multi.yaml 是运行配置（litellm 直接挂载），不能删" >&2; exit 1
  fi
  read -rp "删除 $target ? [y/N] " ans
  case "$ans" in y|Y) ;; *) echo "取消"; exit 0 ;; esac
  rm -- "$target"
  echo "✓ 已删除: ${name}（若误删可用 git checkout 恢复）"
}

# ---------- 子命令：new ----------
cmd_new() {
  local name="" model="" base="" keyenv="" proto="anthropic" nativevision=""

  # 第一个非 -- 参数是 name，其余消费值
  while [ $# -gt 0 ]; do
    case "$1" in
      --model)         model="$2"; shift 2 ;;
      --base)          base="$2"; shift 2 ;;
      --key-env)       keyenv="$2"; shift 2 ;;
      --proto)         proto="$2"; shift 2 ;;
      --native-vision) nativevision="$2"; shift 2 ;;
      -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
      *) [ -z "$name" ] && name="$1" || { echo "未知参数: $1" >&2; exit 1; }; shift ;;
    esac
  done

  # 交互式提问（已通过 CLI 参数给定的就跳过）
  ask() {  # ask "提示语" "变量名" "默认值"
    local prompt="$1" var="$2" def="${3:-}"
    local cur="${!var:-}"
    if [ -n "$cur" ]; then echo "$prompt: $cur  (来自参数)" >&2; return; fi
    local val
    if [ -n "$def" ]; then
      read -rp "$prompt [$def]: " val; val="${val:-$def}"
    else
      read -rp "$prompt: " val
      [ -n "$val" ] || { echo "  必填，重来" >&2; exit 1; }
    fi
    printf -v "$var" '%s' "$val"
  }

  echo "=== 新建 provider profile ===" >&2
  ask "profile 名（文件名，如 zhipu2）" name
  ask "真实模型名（如 glm-4.7）" model
  ask "api_base（provider 的 Anthropic/OpenAI 兼容端点）" base
  ask "API key 的 env 变量名（.env 里要有）" keyenv
  ask "协议前缀 anthropic 或 openai" proto anthropic
  # native_vision：后端是否原生支持图片（y=true 原图透传，n=false 转文字）
  if [ -z "$nativevision" ]; then
    read -rp "后端原生支持图片？(y=原图透传 / n=转文字) [n]: " nv
    case "$nv" in y|Y|yes) nativevision=true ;; *) nativevision=false ;; esac
  else
    echo "后端原生支持图片？: $nativevision  (来自参数)" >&2
  fi
  case "$nativevision" in
    true|false) ;;
    *) echo "错误: --native-vision 只能是 true 或 false" >&2; exit 1 ;;
  esac

  case "$proto" in
    anthropic|openai) ;;
    *) echo "错误: --proto 只能是 anthropic 或 openai" >&2; exit 1 ;;
  esac

  local target="$PROFILES/$name.yaml"
  [ -f "$target" ] && { echo "错误: 已存在 ${target}（要覆盖请先 delete）" >&2; exit 1; }

  # Claude Code 默认会发的模型名，全部别名到真实模型。
  local -a CLAUDE_ALIASES=(
    claude-sonnet-5
    claude-opus-4-8
    claude-haiku-4-5-20251001
    claude-fable-5
    claude-opus-4-1
    claude-3-5-sonnet-20241022
    claude-3-5-haiku-20241022
  )

  {
    echo "# profile: ${name}"
    echo "# desc: 自定义 profile（${proto}/${model} + api_base=${base} + key=\${${keyenv}}）"
    echo "# native_vision: ${nativevision}   # true=原图透传(后端原生多模态) / false=转文字(纯文本后端)"
    echo "---"
    echo "model_list:"
    printf '  - model_name: %s\n' "$model"
    printf '    litellm_params:\n'
    printf '      model: %s/%s\n' "$proto" "$model"
    printf '      api_base: %s\n' "$base"
    printf '      api_key: os.environ/%s\n' "$keyenv"
    echo
    echo "  # Claude Code 默认模型名 -> 全部别名到 ${model}"
    local a
    for a in "${CLAUDE_ALIASES[@]}"; do
      printf '  - model_name: %s\n' "$a"
      printf '    litellm_params: { model: %s/%s, api_base: %s, api_key: os.environ/%s }\n' \
        "$proto" "$model" "$base" "$keyenv"
    done
    echo
    echo "litellm_settings:"
    echo "  callbacks: [hooks.vision_hook.VisionPreRequestHook]   # vision hook：按模型 needs_vision 转图/透传"
    echo "  drop_params: true"
    echo "  request_timeout: 600"
    echo "  num_retries: 2"
    echo
    echo "general_settings:"
    echo "  database_url: postgresql://litellm:litellm@db:5432/litellm"
    echo "  master_key: os.environ/ARK_API_KEY   # 网关访问密钥固定用 ARK_API_KEY，与后端 provider 解耦"
    echo "  store_model_in_db: true                # Admin UI Logs 页查看 request/response 详情（官方两行之一）"
    echo "  store_prompts_in_spend_logs: true      # 把请求 messages/响应存进 DB（官方两行之二）"
  } > "$target"

  echo
  echo "✓ 已生成: $target"
  echo
  echo "下一步:"
  echo "  1. 确认 .env 里有 ${keyenv}=<你的key>"
  echo "  2. 把该后端合并进 litellm/profiles/multi.yaml（multi 才会被 litellm 加载）"
  echo "  3. docker compose restart litellm   # 重载 multi.yaml"
  echo "  4. 客户端 BASE_URL = http://127.0.0.1:4001"
}

# ---------- 分发 ----------
case "${1:-list}" in
  ""|list|-h|--help) cmd_list ;;
  new)    shift; cmd_new "$@" ;;
  delete) shift; cmd_delete "$@" ;;
  *) echo "未知命令: $1" >&2; echo "用法: $0 <new|delete> [args]" >&2; exit 1 ;;
esac
