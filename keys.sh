#!/usr/bin/env bash
# keys.sh - 管理客户端访问 key（绑 user，用量按 user 分开统计）
set -euo pipefail
cd "$(dirname "$0")"

BASE="http://127.0.0.1:4001"   # litellm（vision 已移入 litellm，端口统一 4001）
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
  ./keys.sh new <user> [alias] [--backend ark|claude|zai|逗号多选]
                 创建 key（默认后端 claude；--backend 决定该 key 走哪个后端）
  ./keys.sh list               列出所有 key（alias / user / hash）
  ./keys.sh delete <hash>      删除 key（hash 从 list 拿，支持前缀匹配）

例:
  ./keys.sh new cc             创建绑 user=cc 的 key
  ./keys.sh new hermes 手机     创建绑 user=hermes、alias=手机 的 key

客户端：BASE_URL=http://127.0.0.1:4001（Anthropic）或 http://127.0.0.1:4001/v1（OpenAI），token=<key>
EOF
}

list_keys() {
  hashes=$(cg "$BASE/key/list" -H "Authorization: Bearer $MASTER" | python3 -c "import json,sys;d=json.load(sys.stdin);print('\n'.join(d.get('keys',[])))" 2>/dev/null || true)
  if [ -z "$hashes" ]; then echo "(还没有 key，或 litellm 没起)"; return; fi
  n=$(echo "$hashes" | grep -c .)
  echo "key 列表（共 $n 个，delete 用完整 hash）："
  printf "  %-18s %-12s %s\n" "alias" "user" "hash"
  echo "  ────────────────── ──────────── ────────────────────────────────────"
  for h in $hashes; do
    cg "$BASE/key/info?key=$h" -H "Authorization: Bearer $MASTER" \
      | python3 -c "
import json,sys
try: d=json.load(sys.stdin); info=d.get('info',{})
except Exception: info={}
print('  %-18s %-12s %s' % (str(info.get('key_alias') or '-')[:18], str(info.get('user_id') or '-')[:12], sys.argv[1]))
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
    shift   # 去掉 "new"
    user=""; alias=""; backend=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --backend) backend="$2"; shift 2 ;;
        -h|--help) echo "用法: $0 new <user> [alias] [--backend ark|claude|zai|逗号多选]"; exit 0 ;;
        *) if [ -z "$user" ]; then user="$1"; elif [ -z "$alias" ]; then alias="$1"; fi; shift ;;
      esac
    done
    [ -n "$user" ] || { echo "用法: $0 new <user> [alias] [--backend ark|claude|zai|逗号多选]"; exit 1; }
    [ -z "$alias" ] && alias="$user-key"
    # 单后端 key：cc 默认名 alias 到该后端（cc 不改配置即可走）；多后端 key：短名选后端
    if [ -z "$backend" ]; then
      read -rp "后端（ark/claude/zai，逗号分隔多选）[claude]: " backend
      backend="${backend:-claude}"
    fi
    body=$(BACKENDS="$backend" KEY_USER="$user" KEY_ALIAS="$alias" python3 -c '
import json,sys,os
backends=[b.strip() for b in os.environ["BACKENDS"].split(",") if b.strip()]
CC=["claude-sonnet-5","claude-sonnet-4-6","claude-opus-4-8","claude-opus-5","claude-haiku-4-5-20251001","claude-fable-5"]
B={"ark":["ark-glm-5.2"], "claude":CC, "zai":["zai-glm-4.7"]}
for b in backends:
    if b not in B: print("错误:未知后端 "+b,file=sys.stderr); sys.exit(1)
models=set(); aliases={}
for b in backends:
    for m in B[b]: models.add(m)
if len(backends)==1:
    b=backends[0]
    if b!="claude":            # claude 后端的 model_name 就是 cc 默认名，无需 alias
        t=B[b][0]
        for c in CC: aliases[c]=t
        models.update(CC)
else:
    short={"ark":"ark-glm-5.2","claude":"claude-sonnet-5","zai":"zai-glm-4.7"}
    for b in backends: aliases[b]=short[b]
    ct="claude-sonnet-5" if "claude" in backends else short[backends[0]]
    for c in CC: aliases[c]=ct
    models.update(CC)
print(json.dumps({"user_id":os.environ["KEY_USER"],"key_alias":os.environ["KEY_ALIAS"],"models":sorted(models),"aliases":aliases}))
')
    [ -n "$body" ] || exit 1
    cg -X POST "$BASE/key/generate" \
      -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
      -d "$body" \
      | BACKEND="$backend" python3 -c "
import json,sys
d=json.load(sys.stdin)
if 'key' not in d: print('创建失败:',d); sys.exit(1)
k=d['key']
print('✓ 创建成功  后端:', sys.argv[1])
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
" "$backend"
    ;;
  list)
    list_keys
    ;;
  delete)
    q="${2:?用法: $0 delete <hash 或前缀>}"
    # litellm /key/delete 要完整 hash；用户可能只拿到前缀（list 截断显示），这里解析成完整 hash
    full=$(cg "$BASE/key/list" -H "Authorization: Bearer $MASTER" \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
ks=d.get('keys',[])
q=sys.argv[1]
hits=[k for k in ks if k==q or k.startswith(q)]
if not hits: sys.exit(1)
print(hits[0])   # 取第一个匹配
" "$q" 2>/dev/null || true)
    if [ -z "$full" ]; then
      echo "错误: 没找到匹配 '$q' 的 key（前缀也行）。./keys.sh list 看完整 hash。" >&2
      exit 1
    fi
    [ "$full" != "$q" ] && echo "  匹配到完整 hash: $full"
    cg -X POST "$BASE/key/delete" \
      -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
      -d "{\"keys\":[\"$full\"]}" \
      | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print('删除失败(非 JSON 响应):',e); sys.exit(1)
# litellm 成功返回 {'...':'...','deleted_keys':[...]}，失败返回 {'error':{...}}
if isinstance(d,dict) and 'error' in d:
    e=d['error']
    print('✗ 删除失败:', e.get('message') or e); sys.exit(1)
print('✓ 删除成功:', d.get('message') or d.get('deleted_keys') or d)
"
    ;;
  *) echo "未知命令: $1" >&2; show_help >&2; exit 1 ;;
esac
