#!/usr/bin/env bash
# keys.sh - 管理客户端访问 key（绑 user，用量按 user 分开统计）
set -euo pipefail
cd "$(dirname "$0")"

BASE="http://127.0.0.1:4001"   # litellm（vision 已移入 litellm，端口统一 4001）

cg() { curl -s --noproxy '*' "$@"; }

# 单后端 -> 7 个 claude 名路由 {aliases, models}（new 单后端 + update 共用）
#   ark: 全->ark-glm-5.2   claude: identity(名即真名)   zai: 全->zai-glm-4.7
cc_aliases() {
  BACKEND="${1:?用法: cc_aliases <ark|claude|zai>}" python3 -c '
import json,os,sys
b=os.environ["BACKEND"]
CC=["claude-haiku-4-5-20251001","claude-sonnet-4-6","claude-sonnet-5",
    "claude-sonnet-4-8","claude-opus-4-8","claude-opus-5","claude-fable-5"]
T={"ark":"ark-glm-5.2","claude":None,"zai":"zai-glm-4.7"}
if b not in T: print("错误:未知后端 "+b,file=sys.stderr); sys.exit(1)
t=T[b]
print(json.dumps({"aliases":{c:(c if t is None else t) for c in CC},
                  "models":sorted(set(CC)|({t} if t else set()))}))
'
}

# 解析 key 输入：明文(sk-...)直接用；否则按 hash/前缀补全成完整 hash
resolve_key() {
  local q="$1"
  [[ "$q" == sk-* ]] && { echo "$q"; return; }
  cg "$BASE/key/list" -H "Authorization: Bearer $MASTER" | python3 -c "
import json,sys
d=json.load(sys.stdin); ks=d.get('keys',[]); q=sys.argv[1]
hits=[k for k in ks if k==q or k.startswith(q)]
print(hits[0] if hits else '')
" "$q"
}

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
  ./keys.sh update <key> --backend ark|claude|zai   动态改该 key 的 7 名路由（不重启 litellm）
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
    if [ "$(echo "$backend" | tr ',' '\n' | grep -c .)" -eq 1 ]; then
      # 单后端：7 个 claude 名全路由到该后端（claude=identity）
      am=$(cc_aliases "$backend") || exit 1
      body=$(KEY_USER="$user" KEY_ALIAS="$alias" AM="$am" python3 -c '
import json,os
d=json.loads(os.environ["AM"]); d["user_id"]=os.environ["KEY_USER"]; d["key_alias"]=os.environ["KEY_ALIAS"]
print(json.dumps(d))')
    else
      # 多后端：短名选后端 + 7 名默认指 claude-sonnet-5（若含 claude）
      body=$(BACKENDS="$backend" KEY_USER="$user" KEY_ALIAS="$alias" python3 -c '
import json,os,sys
backends=[b.strip() for b in os.environ["BACKENDS"].split(",") if b.strip()]
CC=["claude-haiku-4-5-20251001","claude-sonnet-4-6","claude-sonnet-5",
    "claude-sonnet-4-8","claude-opus-4-8","claude-opus-5","claude-fable-5"]
B={"ark":["ark-glm-5.2"],"claude":CC,"zai":["zai-glm-4.7"]}
for b in backends:
    if b not in B: print("错误:未知后端 "+b,file=sys.stderr); sys.exit(1)
short={"ark":"ark-glm-5.2","claude":"claude-sonnet-5","zai":"zai-glm-4.7"}
aliases={b:short[b] for b in backends}
ct="claude-sonnet-5" if "claude" in backends else short[backends[0]]
for c in CC: aliases[c]=ct
models=set(CC)
for b in backends:
    for m in B[b]: models.add(m)
print(json.dumps({"user_id":os.environ["KEY_USER"],"key_alias":os.environ["KEY_ALIAS"],
                  "models":sorted(models),"aliases":aliases}))
')
    fi
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
  update)
    shift
    key=""; backend=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --backend) backend="$2"; shift 2 ;;
        -h|--help) echo "用法: $0 update <key明文|hash|前缀> --backend ark|claude|zai  （动态改路由，不重启 litellm）"; exit 0 ;;
        *) [ -z "$key" ] && key="$1"; shift ;;
      esac
    done
    [ -n "$key" ] || { echo "用法: $0 update <key明文|hash|前缀> --backend ark|claude|zai"; exit 1; }
    if [ -z "$backend" ]; then read -rp "新后端（ark/claude/zai）: " backend; fi
    full=$(resolve_key "$key")
    [ -n "$full" ] || { echo "错误: 无法解析 key '$key'（明文 sk- / hash / 前缀）" >&2; exit 1; }
    [ "$full" != "$key" ] && echo "  匹配完整 hash: $full"
    # 重建完整 aliases + models（整体覆盖，cc_aliases 保证 7 名齐全）
    am=$(cc_aliases "$backend") || exit 1
    body=$(KEY="$full" AM="$am" python3 -c '
import json,os
d=json.loads(os.environ["AM"]); d["key"]=os.environ["KEY"]
print(json.dumps(d))')
    cg -X POST "$BASE/key/update" \
      -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
      -d "$body" \
      | python3 -c '
import json,sys
d=json.load(sys.stdin)
if isinstance(d,dict) and "error" in d:
    print("✗ 更新失败:", d["error"].get("message") or d["error"]); sys.exit(1)
print("✓ 路由已切换 ->", sys.argv[1], "（下个请求即生效，无需重启）")
for k,v in sorted((d.get("aliases") or {}).items()):
    print("   ", k, "->", v)
' "$backend"
    ;;
  *) echo "未知命令: $1" >&2; show_help >&2; exit 1 ;;
esac
