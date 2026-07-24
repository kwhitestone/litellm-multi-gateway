#!/usr/bin/env bash
# keys.sh - 管理客户端访问 key（绑 user，用量按 user 分开统计）
set -euo pipefail
cd "$(dirname "$0")"

BASE="http://127.0.0.1:4000"   # 直连 litellm（key 管理不经 vision）
# 新 key 默认可用的模型（profile 里出现的别名 + 实际模型名）
MODELS='["glm-5.2","claude-sonnet-5","claude-opus-4-8","claude-haiku-4-5-20251001"]'

cg() { curl -s --noproxy '*' "$@"; }

MASTER=$(grep -E '^ARK_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2-)
[ -z "$MASTER" ] && { echo "错误：.env 里没有 ARK_API_KEY（master key）" >&2; exit 1; }

show_help() {
  cat <<'EOF'
keys.sh - 管理客户端访问 key（绑 user，用量按 user 分开统计）

用法:
  ./keys.sh                    显示帮助 + 现有 key 列表
  ./keys.sh new <user> [alias] 给某个 user 创建新 key（明文只显示一次，请立即保存）
  ./keys.sh list               列出所有 key（alias / user / hash）
  ./keys.sh delete <hash>      删除 key（hash 从 list 拿）

例:
  ./keys.sh new cc             创建绑 user=cc 的 key
  ./keys.sh new hermes 手机     创建绑 user=hermes、alias=手机 的 key

客户端：BASE_URL=http://127.0.0.1:4001（带视觉）或 :4000（纯核心），token=<key>
EOF
}

list_keys() {
  hashes=$(cg "$BASE/key/list" -H "Authorization: Bearer $MASTER" | python3 -c "import json,sys;d=json.load(sys.stdin);print('\n'.join(d.get('keys',[])))" 2>/dev/null || true)
  if [ -z "$hashes" ]; then echo "(还没有 key，或 litellm 没起)"; return; fi
  n=$(echo "$hashes" | grep -c .)
  echo "key 列表（共 $n 个）："
  printf "  %-16s %-12s %s\n" "alias" "user" "hash(前16)"
  echo "  ──────────────── ──────────── ──────────────────"
  for h in $hashes; do
    cg "$BASE/key/info?key=$h" -H "Authorization: Bearer $MASTER" \
      | python3 -c "
import json,sys
try: d=json.load(sys.stdin); info=d.get('info',{})
except Exception: info={}
print('  %-16s %-12s %s' % (str(info.get('key_alias') or '-')[:16], str(info.get('user_id') or '-')[:12], sys.argv[1][:16]))
" "$h"
  done
}

case "${1:-help}" in
  ""|help|-h|--help)
    show_help
    echo
    list_keys
    ;;
  new)
    user="${2:?用法: $0 new <user> [alias]}"
    alias="${3:-$user-key}"
    cg -X POST "$BASE/key/generate" \
      -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
      -d "{\"user_id\":\"$user\",\"key_alias\":\"$alias\",\"models\":$MODELS}" \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
if 'key' not in d: print('创建失败:',d); sys.exit(1)
k=d['key']
print('✓ 创建成功')
print('  user :', d.get('user_id'))
print('  alias:', d.get('key_alias'))
print('  ┌──────────────────────────────────────────────────────────')
print('  │ 🔑 key 明文（只显示这一次，请立即保存）:')
print('  │', k)
print('  └──────────────────────────────────────────────────────────')
print()
print('客户端配置:')
print('  Claude Code : ANTHROPIC_BASE_URL=http://127.0.0.1:4001  ANTHROPIC_AUTH_TOKEN='+k)
print('  OpenAI 客户端: base_url=http://127.0.0.1:4001/v1  api_key='+k)
"
    ;;
  list)
    list_keys
    ;;
  delete)
    h="${2:?用法: $0 delete <hash>}"
    cg -X POST "$BASE/key/delete" \
      -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
      -d "{\"keys\":[\"$h\"]}" \
      | python3 -c "import json,sys;d=json.load(sys.stdin);print('✓ 删除:',d.get('message','') or d)"
    ;;
  *) echo "未知命令: $1" >&2; show_help >&2; exit 1 ;;
esac
