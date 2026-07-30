#!/usr/bin/env bash
# profiles.sh - 显示 litellm 当前加载的配置（multi.yaml）。
#
#   ./profiles.sh                      显示当前 profile + 可用后端/模型
#
# litellm 直接挂载 litellm/profiles/multi.yaml 作 config（多后端共存）。
# 加/改后端直接编辑 multi.yaml，然后 docker compose restart litellm。
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
  echo "用法: $0  （无参数 = 显示当前配置）"
}

# ---------- 分发 ----------
case "${1:-list}" in
  ""|list|-h|--help) cmd_list ;;
  *) echo "未知命令: $1" >&2; echo "用法: $0 <new|delete> [args]" >&2; exit 1 ;;
esac
