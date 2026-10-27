#!/usr/bin/env bash
# Install or update LSPay on a fresh Ubuntu 22.04/24.04 server.
#
#   curl -fsSL https://raw.githubusercontent.com/lycosdae/LSPay/main/deploy/install.sh | sudo bash -s -- [domain]
#
# With a domain (already pointing at this server) you get HTTPS automatically.
# Without one the site is served over plain HTTP on the server's IP: test use only.
# Running it again updates the code and keeps .env, the database and existing accounts.
set -euo pipefail

DOMAIN="${1:-}"
REPO="https://github.com/lycosdae/LSPay.git"
DIR="/opt/lspay"

if [[ $EUID -ne 0 ]]; then
  echo "請用 root 執行（指令前面加 sudo）" >&2
  exit 1
fi
if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "網域格式不正確：$DOMAIN" >&2
  exit 1
fi

echo "==> 安裝 Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
apt-get update -qq
apt-get install -y -qq git ufw openssl openssl >/dev/null

echo "==> 設定防火牆（只開 SSH、HTTP、HTTPS）"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> 下載程式"
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

rand() { openssl rand -hex "$1" | cut -c1-"$1"; }
IP="$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')"

FIRST_INSTALL=0
if [[ ! -f .env ]]; then
  FIRST_INSTALL=1
  echo "==> 產生設定檔 .env"
  DB_PASS="$(rand 32)"
  if [[ -n "$DOMAIN" ]]; then
    BASE_URL="https://$DOMAIN"; SITE_ADDRESS="$DOMAIN"; COOKIE_SECURE=true
  else
    BASE_URL="http://$IP"; SITE_ADDRESS=":80"; COOKIE_SECURE=false
  fi
  umask 077
  cat > .env <<ENV
DATABASE_URL=postgresql+psycopg://lspay:${DB_PASS}@db:5432/lspay
POSTGRES_PASSWORD=${DB_PASS}
BASE_URL=${BASE_URL}
SITE_ADDRESS=${SITE_ADDRESS}
ADMIN_PATH=console-$(rand 12)
SESSION_SECRET=$(rand 64)
COOKIE_SECURE=${COOKIE_SECURE}
RECEIVE_TIMEOUT_MINUTES=30
SWEEP_INTERVAL_SECONDS=60
CALLBACK_MAX_ATTEMPTS=6
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_WEBHOOK_SECRET=$(rand 32)
CALLBACK_IPS=${IP}
ENV
fi

COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml)

echo "==> 建置並啟動（第一次約需 2 到 5 分鐘）"
"${COMPOSE[@]}" up -d --build

echo "==> 等待服務啟動"
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

set -a
# shellcheck source=/dev/null
source .env
set +a

ADMIN_USER="admin"
ADMIN_PASS=""
if [[ $FIRST_INSTALL -eq 1 ]]; then
  ADMIN_PASS="$(rand 16)"
  "${COMPOSE[@]}" exec -T -e LSPAY_ADMIN_PASSWORD="$ADMIN_PASS" app python -m lspay.cli create-admin "$ADMIN_USER" >/dev/null
fi

echo
echo "=============================================="
echo " LSPay 已啟動"
# shellcheck disable=SC2153  # ADMIN_PATH comes from .env
echo " 後台網址：${BASE_URL}/${ADMIN_PATH}/"
echo " 商戶 API：${BASE_URL}/v1s/"
echo " 商戶後台：${BASE_URL}/merchant"
if [[ -n "$ADMIN_PASS" ]]; then
  echo " 管理員帳號：${ADMIN_USER}"
  echo " 管理員密碼：${ADMIN_PASS}"
  echo " （密碼只顯示這一次，登入後請到「用戶資料」修改）"
fi
echo " 設定檔：${DIR}/.env"
echo "=============================================="
