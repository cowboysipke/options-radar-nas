#!/usr/bin/env bash
# options-radar 一键部署脚本（面向 Linux NAS：飞牛 fnOS / 群晖 SSH / 自组 Debian-Ubuntu）
#
# 用法：
#   ./deploy.sh            首次部署（拉镜像 + 起容器 + 打印 SETUP CODE）
#   ./deploy.sh --update   更新到最新 stable 镜像并重建容器（保留数据卷）
#
# 可选：先复制 .env.example 为 .env 并按需修改（时区 / 端口 / 代理）。

set -euo pipefail

cd "$(dirname "$0")"

IMAGE="ghcr.io/cowboysipke/options-radar-nas:stable"
PROJECT="options-radar"
COMPOSE_FILE="compose.yaml"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

err() { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[34m[..]\033[0m %s\n' "$*"; }
ok() { printf '\033[32m[完成]\033[0m %s\n' "$*"; }

# 1) 环境检查
command -v docker >/dev/null 2>&1 || err "未检测到 docker，请先在 NAS 上安装 Docker 引擎。"
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  err "未检测到 docker compose 或 docker-compose 插件。"
fi
[ -f "$COMPOSE_FILE" ] || err "缺少 $COMPOSE_FILE，请在本目录运行。"

# 2) 生成 .env（首次）
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "已从 $ENV_EXAMPLE 生成 $ENV_FILE（可按需编辑时区/端口/代理）"
  else
    : > "$ENV_FILE"
    info "已生成空 $ENV_FILE"
  fi
fi

# 3) 拉镜像 / 起容器
if [ "${1:-}" = "--update" ]; then
  info "拉取最新 stable 镜像 ..."
  docker pull "$IMAGE"
  ok "镜像已更新，重建容器（数据卷保留）..."
  $COMPOSE -f "$COMPOSE_FILE" --env-file "$ENV_FILE" -p "$PROJECT" up -d --force-recreate
else
  info "启动容器（首次会自动拉取镜像）..."
  $COMPOSE -f "$COMPOSE_FILE" --env-file "$ENV_FILE" -p "$PROJECT" up -d
fi

# 4) 等待健康
info "等待容器健康检查通过（最多 90 秒）..."
for _ in $(seq 1 18); do
  STATUS=$(docker inspect --format '{{.State.Health.Status}}' "${PROJECT}-options-radar-1" 2>/dev/null || \
           docker inspect --format '{{.State.Health.Status}}' "$(docker ps -q --filter name=options-radar)" 2>/dev/null || echo "")
  [ "$STATUS" = "healthy" ] && break
  sleep 5
done

# 5) 打印 SETUP CODE 与访问地址
PORT=$(grep -E '^HOST_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d '"' || true)
PORT="${PORT:-8787}"
info "读取 SETUP CODE ..."
SETUP_CODE=$(docker exec "$(docker ps -q --filter name=options-radar | head -1)" sh -c 'cat /data/setup-token 2>/dev/null' 2>/dev/null || \
             docker logs "$(docker ps -q --filter name=options-radar | head -1)" 2>&1 | sed -n 's/.*SETUP CODE: //p' | tail -1 || echo "")

echo
echo "=========================================================="
echo "  options-radar 部署完成"
echo "=========================================================="
echo "  面板地址:  http://<NAS_IP>:${PORT}"
echo "  管理口令:  ${SETUP_CODE:-（见容器日志中的 SETUP CODE，或 /data/setup-token）}"
echo
echo "  接下来在浏览器打开面板 → 输入管理口令 → 到「设置」页"
echo "  填写 Discord 频道 / 富途账号 / DeepSeek / 飞书 等配置并保存。"
echo "  详细首次配置步骤见 README.md 与 ../docs/nas.md。"
echo "=========================================================="
