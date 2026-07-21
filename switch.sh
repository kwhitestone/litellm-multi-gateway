#!/usr/bin/env bash
# 切换 LiteLLM 后端 provider。预制配置在 litellm/profiles/*.yaml，一键切换 + 重启。
#
#   ./switch.sh            列出可选 profile + 当前在用哪个
#   ./switch.sh ark        切到 火山方舟 coding plan
#   ./switch.sh zai        切到 智谱 BigModel
set -euo pipefail
cd "$(dirname "$0")"

PROFILES="litellm/profiles"
ACTIVE="litellm/config.yaml"   # compose 实际挂载的；运行时由本脚本生成，不进 git

current() {
  if [ ! -f "$ACTIVE" ]; then
    echo "(未生成 — 先运行: ./switch.sh ark)"
    return
  fi
  grep -m1 '^# profile:' "$ACTIVE" 2>/dev/null | sed 's/^# profile: //' || echo "(未知)"
}

list() {
  echo "当前后端: $(current)"
  echo "可选 profile:"
  for f in "$PROFILES"/*.yaml; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .yaml)
    desc=$(grep -m1 '^# desc:' "$f" | sed 's/^# desc: //')
    printf "   %-8s %s\n" "$name" "$desc"
  done
  echo
  echo "用法: $0 <profile>"
}

case "${1:-}" in
  ""|list|-h|--help)
    list
    exit 0
    ;;
esac

target="$PROFILES/$1.yaml"
if [ ! -f "$target" ]; then
  echo "错误: 没有 profile '$1'" >&2
  echo >&2
  list >&2
  exit 1
fi

cp "$target" "$ACTIVE"
echo "✓ 已切换到: $1"

if docker compose ps litellm >/dev/null 2>&1 && [ -n "$(docker compose ps -q litellm 2>/dev/null)" ]; then
  echo "  重启 litellm..."
  docker compose restart litellm
  echo "  等 25s 让 litellm 重新就绪..."
  sleep 25
else
  echo "  (litellm 容器未运行，跳过重启。下次 docker compose up 时自动用新配置)"
fi

echo
echo "当前后端: $(current)"
echo
echo "提示："
case "$1" in
  ark)
    echo "  ark 纯文本 → 建议带 vision 插件看图："
    echo "    docker compose --profile vision up -d"
    echo "    Claude Code BASE_URL = http://127.0.0.1:4001" ;;
  zai)
    echo "  zai 原生多模态 → 不需要 vision 插件："
    echo "    docker compose up -d   （不带 --profile vision）"
    echo "    Claude Code BASE_URL = http://127.0.0.1:4000" ;;
esac
