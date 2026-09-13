#!/usr/bin/env bash
# gw.sh - litellm-multi-gateway 容器控制脚本
#
# 两套模式：
#   compose  - docker compose 启停 db + litellm（本地开发，端口 4001）
#   image    - 单容器镜像启动，DATABASE_URL 指向 compose 的 db（端口 4002，不碰线上 4001）
#
# 用法：
#   ./gw.sh compose up        启动 db + litellm（4001）
#   ./gw.sh compose down      停止并清理
#   ./gw.sh compose restart   重启
#   ./gw.sh compose status    状态
#   ./gw.sh compose logs      跟踪日志
#
#   ./gw.sh image up          构建镜像 + 启动单容器（4002，复用 compose 的 db）
#   ./gw.sh image down        停止单容器
#   ./gw.sh image restart     重启单容器
#   ./gw.sh image status      状态
#   ./gw.sh image logs        跟踪日志
#   ./gw.sh image shell       进容器 shell
#
#   ./gw.sh up / down / restart / status / logs
#                             不指定模式时默认 compose

set -euo pipefail
cd "$(dirname "$0")"

# ---- 配置 ----
IMAGE="litellm-gateway"
COMPOSE_MODE="${COMPOSE_MODE:-compose}"
IMAGE_CONTAINER="litellm-gateway-test"
IMAGE_PORT="${IMAGE_PORT:-4002}"

# 读 .env
if [[ ! -f .env ]]; then
  echo "错误：找不到 .env，请先 cp .env.example .env 并填好。" >&2; exit 1
fi
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }

# ============================================================
# compose 模式
# ============================================================
compose_up() {
  echo "── compose up（db + litellm，端口 4001）──"
  docker compose up -d
  echo "✓ 已启动：http://127.0.0.1:4001"
}

compose_down() {
  echo "── compose down ──"
  docker compose down
  echo "✓ 已停止"
}

compose_restart() {
  docker compose restart
  echo "✓ 已重启"
}

compose_status() {
  docker compose ps
}

compose_logs() {
  docker compose logs -f --tail=50
}

# ============================================================
# image 模式（单容器，复用 compose 的 db）
# ============================================================
image_up() {
  # 确保 db 在跑
  if ! docker compose ps db --format json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('State')=='running' else 1)" 2>/dev/null; then
    echo "── db 未运行，先启动 compose db ──"
    docker compose up -d db
    echo "  等 db 就绪..."
    for i in {1..30}; do
      docker compose exec -T db pg_isready -U "$(get_env POSTGRES_USER || echo litellm)" >/dev/null 2>&1 && break
      sleep 1
    done
    echo "  ✓ db 就绪"
  fi

  # 如果镜像容器已存在，先清掉
  docker rm -f "$IMAGE_CONTAINER" 2>/dev/null || true

  # 构建镜像（如果不存在或 --build）
  if [[ ! "$(docker images -q $IMAGE 2>/dev/null)" ]] || [[ "${1:-}" == "--build" ]]; then
    echo "── 构建镜像 $IMAGE ──"
    docker build -t "$IMAGE" .
  else
    echo "── 镜像 $IMAGE 已存在（加 --build 强制重建）──"
  fi

  # 从 .env 读需要的变量
  local db_user db_pass db_name
  db_user="$(get_env POSTGRES_USER || echo litellm)"
  db_pass="$(get_env POSTGRES_PASSWORD || echo litellm)"
  db_name="$(get_env POSTGRES_DB || echo litellm)"

  echo "── 启动单容器（端口 $IMAGE_PORT，复用 compose db）──"
  docker run -d --name "$IMAGE_CONTAINER" \
    --network "$(docker compose config --format json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('networks',{}).keys() and list(json.load(open('/dev/stdin')).get('networks',{}).keys())[0] or 'litellm-multi-gateway_default')" 2>/dev/null || echo "litellm-multi-gateway_default")" \
    -p "127.0.0.1:${IMAGE_PORT}:4001" \
    -e DATABASE_URL="postgresql://${db_user}:${db_pass}@db:5432/${db_name}" \
    -e GATEWAY_MASTER_KEY="$(get_env GATEWAY_MASTER_KEY)" \
    -e ARK_API_KEY="$(get_env ARK_API_KEY)" \
    -e Z_AI_API_KEY="$(get_env Z_AI_API_KEY)" \
    -e CLAUDE_CODE_KEY="$(get_env CLAUDE_CODE_KEY)" \
    -e SUB_ARK_API_KEY="$(get_env SUB_ARK_API_KEY || true)" \
    -e UI_USERNAME="$(get_env UI_USERNAME || echo admin)" \
    -e UI_PASSWORD="$(get_env UI_PASSWORD || echo admin)" \
    -e VISION_API_KEY="$(get_env VISION_API_KEY || true)" \
    -e VISION_BASE_URL="$(get_env VISION_BASE_URL || true)" \
    -e VISION_MODEL="$(get_env VISION_MODEL || true)" \
    "$IMAGE"

  echo "── 等启动（含 prisma db push 迁移，首次约 40-60s）──"
  for i in {1..60}; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${IMAGE_PORT}/health/liveness" 2>/dev/null || echo 000)
    [[ "$code" == "200" ]] && { echo "✓ 健康检查通过（HTTP 200）"; break; }
    sleep 2; printf "."
  done
  if [[ "$code" != "200" ]]; then
    echo "✗ 健康检查失败（HTTP $code）" >&2
    docker logs "$IMAGE_CONTAINER" --tail 20 >&2
    echo "排查看日志：./gw.sh image logs" >&2
    exit 1
  fi
  echo "✓ 单容器已启动：http://127.0.0.1:${IMAGE_PORT}"
}

image_down() {
  echo "── 停止单容器 ──"
  docker rm -f "$IMAGE_CONTAINER" 2>/dev/null && echo "✓ 已停止" || echo "（容器不存在）"
}

image_restart() {
  image_down
  image_up
}

image_status() {
  docker ps -a --filter "name=$IMAGE_CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
}

image_logs() {
  docker logs -f --tail=50 "$IMAGE_CONTAINER"
}

image_shell() {
  docker exec -it "$IMAGE_CONTAINER" bash || docker exec -it "$IMAGE_CONTAINER" sh
}

# ============================================================
# 路由
# ============================================================
MODE="${1:-compose}"
ACTION="${2:-up}"

case "$MODE" in
  compose)
    case "$ACTION" in
      up)      compose_up ;;
      down)    compose_down ;;
      restart) compose_restart ;;
      status)  compose_status ;;
      logs)    compose_logs ;;
      *) echo "用法: ./gw.sh compose [up|down|restart|status|logs]"; exit 1 ;;
    esac ;;
  image)
    case "$ACTION" in
      up)      image_up "${3:-}" ;;
      down)    image_down ;;
      restart) image_restart ;;
      status)  image_status ;;
      logs)    image_logs ;;
      shell)   image_shell ;;
      *) echo "用法: ./gw.sh image [up [--build]|down|restart|status|logs|shell]"; exit 1 ;;
    esac ;;
  *)
    # 简写模式：不写 mode 默认 compose
    ACTION="$MODE"
    case "$ACTION" in
      up)      compose_up ;;
      down)    compose_down ;;
      restart) compose_restart ;;
      status)  compose_status ;;
      logs)    compose_logs ;;
      *) echo "用法: ./gw.sh [compose|image] [up|down|restart|status|logs]"; echo "  image 还支持: shell, up --build"; exit 1 ;;
    esac ;;
esac
