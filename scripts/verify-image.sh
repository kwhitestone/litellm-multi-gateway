#!/usr/bin/env bash
# verify-image.sh — 验证单容器镜像（litellm-gateway-test）能否正常工作。
#
# 特点：
#   - 不碰 4001 端口（线上 litellm 在用），用 4002
#   - 不碰线上 db，起独立的临时 postgres（用完即删）
#   - 跑完自动清理（容器、网络、临时 db 卷），Ctrl+C 也会清
#
# 用法：
#   ./scripts/verify-image.sh           构建镜像 + 启动 + 验证 + 清理（全自动）
#   ./scripts/verify-image.sh --no-clean  验证完保留容器（方便手动进容器排查）
#   ./scripts/verify-image.sh --keep      同上
#
# 前置：provider key 从 .env 读。

set -euo pipefail
cd "$(dirname "$0")/.."

# ---- 配置（可改）----
IMAGE="litellm-gateway-test"
TEST_PORT="${TEST_PORT:-4002}"          # 验证用端口，避开线上的 4001
DB_CONTAINER="gw-verify-db"
APP_CONTAINER="gw-verify-app"
NETWORK="gw-verify-net"
MASTER_KEY="verify-master-key-12345"

NO_CLEAN=0
[[ "${1:-}" == "--no-clean" || "${1:-}" == "--keep" ]] && NO_CLEAN=1

# ---- 清理函数（正常退出 / Ctrl+C / 出错都调）----
cleanup() {
  if [[ "$NO_CLEAN" == "1" ]]; then return; fi
  echo
  echo "── 清理 ──"
  docker rm -f "$APP_CONTAINER" "$DB_CONTAINER" 2>/dev/null && echo "✓ 容器已移除" || true
  docker network rm "$NETWORK" 2>/dev/null && echo "✓ 网络已移除" || true
  docker volume rm gw-verify-pgdata 2>/dev/null && echo "✓ 临时 db 卷已删" || true
}
trap cleanup EXIT INT TERM

# ---- 读 .env 里的 provider key（构建/运行都需要）----
if [[ ! -f .env ]]; then
  echo "错误：找不到 .env（provider key 在里面）。请先 cp .env.example .env 并填好。" >&2
  exit 1
fi
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }
ARK_API_KEY="$(get_env ARK_API_KEY)"
Z_AI_API_KEY="$(get_env Z_AI_API_KEY)"
CLAUDE_CODE_KEY="$(get_env CLAUDE_CODE_KEY)"
CLAUDE_CODE_KEY_1="$(get_env CLAUDE_CODE_KEY_1)"
CLAUDE_CODE_KEY_2="$(get_env CLAUDE_CODE_KEY_2)"

[[ -z "$ARK_API_KEY" || -z "$Z_AI_API_KEY" || -z "$CLAUDE_CODE_KEY" ]] && {
  echo "错误：.env 缺 ARK_API_KEY / Z_AI_API_KEY / CLAUDE_CODE_KEY" >&2; exit 1; }

# ---- 1. 构建镜像 ----
echo "── 1/4 构建镜像 $IMAGE ──"
docker build -t "$IMAGE" . 2>&1 | tail -3

# ---- 2. 起独立 db + 测试镜像（隔离网络，不碰线上）----
echo
echo "── 2/4 起临时 db + 测试镜像（端口 $TEST_PORT，隔离网络）──"
docker network create "$NETWORK" 2>/dev/null || true

docker run -d --rm --name "$DB_CONTAINER" --network "$NETWORK" \
  -e POSTGRES_USER=litellm -e POSTGRES_PASSWORD=litellm -e POSTGRES_DB=litellm \
  -v gw-verify-pgdata:/var/lib/postgresql/data \
  postgres:16-alpine >/dev/null

echo "  等 db 就绪..."
for i in {1..30}; do
  docker exec "$DB_CONTAINER" pg_isready -U litellm >/dev/null 2>&1 && break
  sleep 1
done
echo "  ✓ db 就绪"

# 测试镜像：DATABASE_URL 指向临时 db；PORT=4002（容器内也用这个）；master key 自定义
# 不用 --rm：崩溃时保留容器以便看日志（cleanup 函数统一删）
docker run -d --name "$APP_CONTAINER" --network "$NETWORK" \
  -p "$TEST_PORT:4002" \
  -e DATABASE_URL="postgresql://litellm:litellm@$DB_CONTAINER:5432/litellm" \
  -e GATEWAY_MASTER_KEY="$MASTER_KEY" \
  -e ARK_API_KEY="$ARK_API_KEY" \
  -e Z_AI_API_KEY="$Z_AI_API_KEY" \
  -e CLAUDE_CODE_KEY="$CLAUDE_CODE_KEY" \
  -e CLAUDE_CODE_KEY_1="$CLAUDE_CODE_KEY_1" \
  -e CLAUDE_CODE_KEY_2="$CLAUDE_CODE_KEY_2" \
  -e PORT=4002 \
  "$IMAGE" >/dev/null

# ---- 3. 等启动 + 健康检查 ----
echo
echo "── 3/4 等 litellm 启动（含 prisma db push 迁移，首次约 40-60s）──"
BASE="http://127.0.0.1:$TEST_PORT"
code=000
for i in {1..90}; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health/liveness" 2>/dev/null || echo 000)
  [[ "$code" == "200" ]] && break
  sleep 2
  printf "."
done
echo
if [[ "$code" != "200" ]]; then
  echo "✗ 健康检查失败（HTTP $code）。容器日志：" >&2
  docker logs "$APP_CONTAINER" --tail 30 >&2 || true
  echo "（加 --keep 可保留容器排查：docker logs $APP_CONTAINER）" >&2
  exit 1
fi
echo "✓ 健康检查通过（HTTP 200）"

# ---- 4. 验证管理页 + 创建 key ----
echo
echo "── 4/4 验证管理页 ──"

# 管理页 HTML
mgmt_code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/manage/?k=$MASTER_KEY")
echo "  /manage/ → HTTP $mgmt_code"
if [[ "$mgmt_code" != "200" ]]; then
  echo "  ✗ 管理页没起来，容器日志：" >&2
  docker logs "$APP_CONTAINER" --tail 20 >&2
fi

# 通过管理页 API 创建一个测试 key（走 claude 后端）
echo "  创建测试 key（claude 后端）..."
resp=$(curl -s -X POST "$BASE/manage/api/new" \
  -H "Content-Type: application/json" -H "X-Management-Key: $MASTER_KEY" \
  -d '{"user":"verify-test","backend":"claude"}')
test_key=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('key',''))" 2>/dev/null || echo "")
if [[ -n "$test_key" ]]; then
  echo "  ✓ 创建成功：${test_key:0:25}..."
  # 确认 key 在 /key/list 里
  listed=$(curl -s "$BASE/key/list" -H "Authorization: Bearer $MASTER_KEY" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('YES' if d.get('keys') else 'NO')" 2>/dev/null || echo "?")
  echo "  /key/list 能查到：$listed"
  # 删掉测试 key
  curl -s -X POST "$BASE/manage/api/delete" \
    -H "Content-Type: application/json" -H "X-Management-Key: $MASTER_KEY" \
    -d "{\"key\":\"$test_key\"}" >/dev/null && echo "  ✓ 已删除测试 key"
else
  echo "  ✗ 创建失败：$resp" >&2
  docker logs "$APP_CONTAINER" --tail 20 >&2
fi

echo
echo "════════════════════════════════════════"
echo "✅ 验证完成（端口 $TEST_PORT，与线上 4001 完全隔离）"
echo "════════════════════════════════════════"
if [[ "$NO_CLEAN" == "1" ]]; then
  echo "镜像保留：docker logs $APP_CONTAINER / curl \"$BASE/manage/?k=$MASTER_KEY\""
  echo "手动清理：docker rm -f $APP_CONTAINER $DB_CONTAINER && docker network rm $NETWORK && docker volume rm gw-verify-pgdata"
  trap - EXIT INT TERM  # 不自动清理
fi
