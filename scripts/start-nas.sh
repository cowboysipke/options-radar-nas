#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "Supported NAS architectures: amd64, arm64" >&2; exit 2 ;;
esac

docker compose version >/dev/null
mkdir -p nas-data/browser-profile nas-data/evidence nas-data/strategies nas-data/backups secrets

if [ ! -s secrets/setup_token ]; then
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24 > secrets/setup_token
  else
    umask 077
    dd if=/dev/urandom bs=24 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n' > secrets/setup_token
  fi
fi

for name in deepseek_api_key feishu_app_secret ibkr_flex_token massive_api_key; do
  [ -e "secrets/$name" ] || { umask 077; : > "secrets/$name"; }
done
chmod 600 secrets/*

cat > .env.nas <<EOF
PUID=$(id -u)
PGID=$(id -g)
TZ=${TZ:-Asia/Shanghai}
NAS_BIND_IP=${NAS_BIND_IP:-127.0.0.1}
SETUP_PORT=${SETUP_PORT:-8787}
EOF

echo "NAS architecture: $ARCH"
echo "Setup token: $(cat secrets/setup_token)"
echo "Setup address: http://${NAS_BIND_IP:-127.0.0.1}:${SETUP_PORT:-8787}/"
if docker compose --env-file .env.nas ps --status running --services 2>/dev/null | grep -qx radar; then
  echo "Creating pre-upgrade backup..."
  docker compose --env-file .env.nas exec -T radar python -m options_radar.nas_runtime --backup
fi
docker compose --env-file .env.nas up -d --build
docker compose --env-file .env.nas ps

