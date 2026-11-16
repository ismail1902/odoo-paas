#!/usr/bin/env bash
# Odoo PaaS installer - Ubuntu 22.04 / 24.04 (root).
#
# Idempotent: safe to re-run after `git pull` to update the platform.
#
#   sudo ./install.sh                       install / update, autodetect host IP
#   sudo ./install.sh --public-host 1.2.3.4
#   sudo ./install.sh --admin-email me@example.com --admin-password 'secret'
#   sudo ./install.sh --rebuild-base        rebuild the Odoo base image too
#   sudo ./install.sh --odoo-versions "17.0 20.0"  build several versions
#   sudo ./install.sh --wkhtmltopdf-deb URL extra .deb for arm64 PDF reports
#   sudo ./install.sh --acme-email you@example.com  Let's Encrypt expiry contact
#
# What it does, in order:
#   1. apt prerequisites + Docker (via get.docker.com when missing)
#   2. copy the repository to /opt/odoo-paas (data stays in /var/lib/odoo-paas)
#   3. create/refresh .env (secrets are only generated once)
#   4. docker network odoo-paas + shared containers (PostgreSQL, Mailpit)
#   5. build the Odoo base image (odoo-paas/base:<version>)
#   6. Python venv + pip install -r requirements.txt
#   7. systemd service (odoo-paas) + firewall rules for the port ranges
#   8. seed the first admin user
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=/opt/odoo-paas
DATA_DIR=/var/lib/odoo-paas
ENV_FILE="$TARGET_DIR/.env"
ODOO_VERSION="${ODOO_VERSION:-17.0}"
PORT="${PORT:-8000}"
GEVENT_PORT_RANGE_START="${GEVENT_PORT_RANGE_START:-10000}"
GEVENT_PORT_RANGE_END="${GEVENT_PORT_RANGE_END:-10990}"
ACME_EMAIL="${ACME_EMAIL:-}"
BASE_IMAGE="odoo-paas/base:${ODOO_VERSION}"
PG_CONTAINER=odoo-paas-pg
MAILPIT_CONTAINER=odoo-paas-mailpit
NETWORK=odoo-paas
# Odoo >= 20 requires PostgreSQL >= 16; 16 also serves older series.
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16}"

PUBLIC_HOST="${PUBLIC_HOST:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
REBUILD_BASE=0
WKHTMLTOPDF_DEB="${WKHTMLTOPDF_DEB:-}"
ODOO_VERSIONS_ARG="${ODOO_VERSIONS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-host)    PUBLIC_HOST="$2"; shift 2 ;;
    --admin-email)    ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --rebuild-base)   REBUILD_BASE=1; shift ;;
    --odoo-versions)  ODOO_VERSIONS_ARG="$2"; shift 2 ;;
    --wkhtmltopdf-deb) WKHTMLTOPDF_DEB="$2"; shift 2 ;;
    --acme-email)     ACME_EMAIL="$2"; shift 2 ;;
    --odoo-version)   ODOO_VERSION="$2"; BASE_IMAGE="odoo-paas/base:$2"; shift 2 ;;
    -h|--help)        sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning: %s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo ./install.sh" >&2; exit 1; }

# ------------------------------------------------------------------ 1. packages
log "Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates rsync git python3 python3-venv python3-pip openssl nginx certbot >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || { echo "Docker daemon not reachable" >&2; exit 1; }
systemctl enable --now nginx >/dev/null 2>&1 || true
mkdir -p /var/www/letsencrypt
chown www-data:www-data /var/www/letsencrypt

# ------------------------------------------------------------------ 2. copy repo
log "Syncing platform to $TARGET_DIR"
mkdir -p "$TARGET_DIR" "$DATA_DIR"
rsync -a --delete \
  --exclude '.env' --exclude '.venv' --exclude 'var' --exclude '.git' --exclude '__pycache__' \
  "$REPO_DIR"/ "$TARGET_DIR"/

# ------------------------------------------------------------------ 3. .env
detect_host() {
  curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "127.0.0.1"
}

if [[ ! -f "$ENV_FILE" ]]; then
  log "Creating $ENV_FILE"
  [[ -n "$PUBLIC_HOST" ]] || PUBLIC_HOST="$(detect_host)"
  SECRET_KEY="$(openssl rand -hex 32)"
  PG_PASSWORD="$(openssl rand -hex 16)"
  cat > "$ENV_FILE" <<EOF
APP_NAME=Odoo PaaS
SECRET_KEY=$SECRET_KEY

DATA_DIR=$DATA_DIR
HOST=0.0.0.0
PORT=$PORT
PUBLIC_HOST=$PUBLIC_HOST
GEVENT_PORT_RANGE_START=$GEVENT_PORT_RANGE_START
GEVENT_PORT_RANGE_END=$GEVENT_PORT_RANGE_END
ACME_EMAIL=$ACME_EMAIL

PG_CONTAINER=$PG_CONTAINER
PG_USER=odoo
PG_PASSWORD=$PG_PASSWORD

BASE_IMAGE=$BASE_IMAGE
ODOO_VERSION=$ODOO_VERSION
WKHTMLTOPDF_DEB=$WKHTMLTOPDF_DEB
MOCK_DOCKER=false
WORKER_ENABLED=true

ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF
  chmod 600 "$ENV_FILE"
else
  log "Keeping existing $ENV_FILE"
  PG_PASSWORD="$(grep -m1 '^PG_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
  [[ -n "$PUBLIC_HOST" ]] && sed -i "s|^PUBLIC_HOST=.*|PUBLIC_HOST=$PUBLIC_HOST|" "$ENV_FILE"
  if [[ -n "$ACME_EMAIL" ]]; then
    if grep -q '^ACME_EMAIL=' "$ENV_FILE"; then
      sed -i "s|^ACME_EMAIL=.*|ACME_EMAIL=$ACME_EMAIL|" "$ENV_FILE"
    else
      echo "ACME_EMAIL=$ACME_EMAIL" >> "$ENV_FILE"
    fi
  fi
fi

# ------------------------------------------------------------------ 4. containers
log "Preparing Docker network and shared containers"
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

if ! docker container inspect "$PG_CONTAINER" >/dev/null 2>&1; then
  log "Starting PostgreSQL ($PG_CONTAINER, $POSTGRES_IMAGE)"
  docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
    --network "$NETWORK" \
    -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -v odoo-paas-pgdata:/var/lib/postgresql/data \
    "$POSTGRES_IMAGE" >/dev/null
else
  docker start "$PG_CONTAINER" >/dev/null 2>&1 || true
  running_image="$(docker container inspect -f '{{.Config.Image}}' "$PG_CONTAINER")"
  if [[ "$running_image" != "$POSTGRES_IMAGE" ]]; then
    log "WARNING: $PG_CONTAINER runs $running_image, expected $POSTGRES_IMAGE."
    log "Odoo >= 20 needs PostgreSQL >= 16 - see docs/RUNBOOK.md (Upgrading PostgreSQL)."
  fi
fi

if ! docker container inspect "$MAILPIT_CONTAINER" >/dev/null 2>&1; then
  log "Starting Mailpit (catches all outgoing mail: http://HOST:8025)"
  docker run -d --name "$MAILPIT_CONTAINER" --restart unless-stopped \
    --network "$NETWORK" \
    -p 127.0.0.1:8025:8025 -p 127.0.0.1:1025:1025 \
    axllent/mailpit >/dev/null
else
  docker start "$MAILPIT_CONTAINER" >/dev/null 2>&1 || true
fi

# Self-heal: containers created by an older/partially failed run may not be
# attached to the platform network (name resolution between containers).
for name in "$PG_CONTAINER" "$MAILPIT_CONTAINER"; do
  docker network connect "$NETWORK" "$name" >/dev/null 2>&1 || true
done

# Wait for PostgreSQL, then keep the 'odoo' role password in sync with .env
# (idempotent; also makes password rotation a matter of editing .env).
for _ in $(seq 1 30); do
  docker exec "$PG_CONTAINER" pg_isready -U odoo >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$PG_CONTAINER" psql -U odoo -d postgres -q \
  -c "ALTER USER odoo WITH PASSWORD '$PG_PASSWORD'" >/dev/null 2>&1 \
  || warn "could not sync PostgreSQL password (container not ready?)"

# ------------------------------------------------------------------ 5. base image
BUILD_VERSIONS="${ODOO_VERSIONS_ARG:-$ODOO_VERSION}"
for version in $BUILD_VERSIONS; do
  image="odoo-paas/base:$version"
  # Odoo >= 20 needs Python >= 3.12 -> official python image (same Debian base).
  base_os="debian:bookworm-slim"
  if [[ "${version%%.*}" -ge 20 ]]; then
    base_os="python:3.12-slim-bookworm"
  fi
  if [[ "$REBUILD_BASE" == "1" ]] || ! docker image inspect "$image" >/dev/null 2>&1; then
    log "Building $image (downloads Odoo $version - takes a few minutes)"
    docker build --build-arg "BASE_IMAGE=$base_os" \
      --build-arg "ODOO_VERSION=$version" \
      --build-arg "WKHTMLTOPDF_DEB=$WKHTMLTOPDF_DEB" \
      -t "$image" "$TARGET_DIR/docker/odoo-base"
  else
    log "Base image $image already present (use --rebuild-base to refresh)"
  fi
done

# ------------------------------------------------------------------ 6. venv
log "Installing platform dependencies"
[[ -d "$TARGET_DIR/.venv" ]] || python3 -m venv "$TARGET_DIR/.venv"
"$TARGET_DIR/.venv/bin/pip" install -q --upgrade pip
"$TARGET_DIR/.venv/bin/pip" install -q -r "$TARGET_DIR/requirements.txt"

# git repos are chowned to the container user (1000) but read by root - allow it
git config --global --add safe.directory '*'

# ------------------------------------------------------------------ 7. systemd + firewall
log "Installing systemd service"
install -m 644 "$TARGET_DIR/systemd/odoo-paas.service" /etc/systemd/system/odoo-paas.service
systemctl daemon-reload
systemctl enable odoo-paas >/dev/null 2>&1 || true
systemctl restart odoo-paas

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  log "Opening firewall ports"
  ufw allow "$PORT"/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 9000:9990/tcp >/dev/null
fi

# ------------------------------------------------------------------ 8. admin user
source_env() { grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true; }
ADMIN_EMAIL="${ADMIN_EMAIL:-$(source_env ADMIN_EMAIL)}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(source_env ADMIN_PASSWORD)}"
cd "$TARGET_DIR"
"$TARGET_DIR/.venv/bin/python" -m app.manage init >/dev/null

if [[ -n "$ADMIN_EMAIL" && -n "$ADMIN_PASSWORD" ]]; then
  "$TARGET_DIR/.venv/bin/python" -m app.manage create-admin \
    --email "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD" --role admin | tail -n1
  DISPLAY_PASSWORD="$ADMIN_PASSWORD"
else
  if "$TARGET_DIR/.venv/bin/python" - <<'EOF'
import sys
from sqlmodel import select
from app.db import init_db, session_scope
from app.models import User
init_db()
with session_scope() as s:
    sys.exit(0 if s.exec(select(User)).first() else 1)
EOF
  then
    DISPLAY_PASSWORD="(unchanged)"
  else
    DISPLAY_PASSWORD="$(openssl rand -base64 12)"
    "$TARGET_DIR/.venv/bin/python" -m app.manage create-admin \
      --email "admin@$(hostname -f 2>/dev/null || echo localhost)" \
      --password "$DISPLAY_PASSWORD" --role admin | tail -n1
    ADMIN_EMAIL="admin@$(hostname -f 2>/dev/null || echo localhost)"
  fi
fi

echo
log "Done"
HOST_SHOWN="$(source_env PUBLIC_HOST)"
cat <<EOF
  Control plane : http://${HOST_SHOWN:-<server-ip>}:$PORT
  Login         : ${ADMIN_EMAIL:-see .env}  /  ${DISPLAY_PASSWORD:-see .env}
  Mailpit (mail): http://127.0.0.1:8025  (SSH tunnel: ssh -L 8025:127.0.0.1:8025 root@server)
  Data          : $DATA_DIR
  Service       : systemctl status odoo-paas
  Diagnostics   : $TARGET_DIR/.venv/bin/python -m app.manage doctor
EOF
