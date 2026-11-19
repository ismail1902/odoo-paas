#!/usr/bin/env bash
# Odoo PaaS uninstaller.
#
# Default: stop/remove the application, systemd unit, nginx sites and platform
# containers, but keep PostgreSQL volumes, /var/lib/odoo-paas (state/builds/
# backups) and Let's Encrypt certificates.
#
#   sudo ./uninstall.sh --dry-run              show the plan, change nothing
#   sudo ./uninstall.sh                        remove app; keep data
#   sudo ./uninstall.sh --purge                remove app and all data
#   sudo ./uninstall.sh --purge --yes          non-interactive full removal
#   sudo ./uninstall.sh --purge --keep-images  full removal, keep base images
#   sudo ./uninstall.sh --delete-certificates  delete platform certs too
#
# Firewall rules and system packages (Docker, nginx, certbot) are not changed.
set -uo pipefail

TARGET_DIR=/opt/odoo-paas
DATA_DIR=/var/lib/odoo-paas
ENV_FILE="$TARGET_DIR/.env"
PURGE=0
ASSUME_YES=0
DRY_RUN=0
KEEP_IMAGES=0
DELETE_CERTS=0

log()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning: %s\033[0m\n' "$*" >&2; }

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

usage() {
  sed -n '2,15p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge|--delete-data) PURGE=1; shift ;;
    --yes|-y)              ASSUME_YES=1; shift ;;
    --dry-run)             DRY_RUN=1; shift ;;
    --keep-images)         KEEP_IMAGES=1; shift ;;
    --delete-certificates) DELETE_CERTS=1; shift ;;
    -h|--help)             usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo ./uninstall.sh" >&2; exit 1; }

env_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

PG_CONTAINER="${PG_CONTAINER:-$(env_value PG_CONTAINER)}"; PG_CONTAINER="${PG_CONTAINER:-odoo-paas-pg}"
PG_CONTAINER_15="${PG_CONTAINER_15:-$(env_value PG_CONTAINER_15)}"; PG_CONTAINER_15="${PG_CONTAINER_15:-odoo-paas-pg15}"
PG_CONTAINER_17="${PG_CONTAINER_17:-$(env_value PG_CONTAINER_17)}"; PG_CONTAINER_17="${PG_CONTAINER_17:-odoo-paas-pg17}"
PG_CONTAINER_18="${PG_CONTAINER_18:-$(env_value PG_CONTAINER_18)}"; PG_CONTAINER_18="${PG_CONTAINER_18:-odoo-paas-pg18}"
MAILPIT_CONTAINER="${MAILPIT_CONTAINER:-$(env_value MAILPIT_CONTAINER)}"; MAILPIT_CONTAINER="${MAILPIT_CONTAINER:-odoo-paas-mailpit}"
NETWORK="${DOCKER_NETWORK:-$(env_value DOCKER_NETWORK)}"; NETWORK="${NETWORK:-odoo-paas}"

HAS_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  HAS_DOCKER=1
fi

# Domains are read before any state/nginx files are removed.
DOMAINS=()
if [[ -f "$DATA_DIR/state.db" ]] && command -v python3 >/dev/null 2>&1; then
  mapfile -t DOMAINS < <(
    python3 - "$DATA_DIR/state.db" <<'PY'
import sqlite3
import sys

try:
    con = sqlite3.connect(sys.argv[1])
    domains = set()
    for (domain,) in con.execute(
        "select domain from branch where domain is not null and domain != ''"
    ):
        if domain.strip():
            domains.add(domain.strip())
    row = con.execute(
        "select value from setting where key = 'control_plane_domain'"
    ).fetchone()
    if row and (row[0] or "").strip():
        domains.add(row[0].strip())
    for domain in sorted(domains):
        print(domain)
except Exception:
    pass
PY
  )
fi

CONTAINERS=()
add_container() {
  [[ "$HAS_DOCKER" == "1" ]] || return 0
  docker container inspect "$1" >/dev/null 2>&1 && CONTAINERS+=("$1")
}
if [[ "$HAS_DOCKER" == "1" ]]; then
  add_container "$PG_CONTAINER"
  add_container "$PG_CONTAINER_15"
  add_container "$PG_CONTAINER_17"
  add_container "$PG_CONTAINER_18"
  add_container "$MAILPIT_CONTAINER"
  while IFS= read -r name; do
    [[ -n "$name" ]] && CONTAINERS+=("$name")
  done < <(docker ps -a --filter 'name=odoo-paas-branch-' --format '{{.Names}}')
  if ((${#CONTAINERS[@]})); then
    mapfile -t CONTAINERS < <(printf '%s\n' "${CONTAINERS[@]}" | sort -u)
  fi
fi

VOLUMES=(
  odoo-paas-pgdata
  odoo-paas-pgdata16
  odoo-paas-pgdata-old
  odoo-paas-pg15data
  odoo-paas-pg17data
  odoo-paas-pg18data
)
if [[ "$HAS_DOCKER" == "1" ]]; then
  for container in "$PG_CONTAINER" "$PG_CONTAINER_15" "$PG_CONTAINER_17" "$PG_CONTAINER_18"; do
    docker container inspect "$container" >/dev/null 2>&1 || continue
    while IFS= read -r volume; do
      [[ -n "$volume" ]] && VOLUMES+=("$volume")
    done < <(docker container inspect -f \
      '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' \
      "$container")
  done
  if [[ "$PURGE" == "1" ]]; then
    while IFS= read -r volume; do
      [[ -n "$volume" ]] && VOLUMES+=("$volume")
    done < <(docker volume ls --format '{{.Name}}' | grep -E '^odoo-paas-pg.*data' || true)
  fi
  mapfile -t VOLUMES < <(printf '%s\n' "${VOLUMES[@]}" | sed '/^$/d' | sort -u)
  EXISTING_VOLUMES=()
  for volume in "${VOLUMES[@]}"; do
    docker volume inspect "$volume" >/dev/null 2>&1 && EXISTING_VOLUMES+=("$volume")
  done
  if ((${#EXISTING_VOLUMES[@]})); then
    VOLUMES=("${EXISTING_VOLUMES[@]}")
  else
    VOLUMES=()
  fi
fi

IMAGES=()
if [[ "$HAS_DOCKER" == "1" ]]; then
  mapfile -t IMAGES < <(
    docker images --format '{{.Repository}}:{{.Tag}}' |
      awk -F: '$1 == "odoo-paas/base" { print }' | sort -u
  )
fi

CONFIGS=()
shopt -s nullglob
CONFIGS=(/etc/nginx/conf.d/odoo-paas-branch-*.conf)
shopt -u nullglob
if [[ -e /etc/nginx/conf.d/odoo-paas-control-plane.conf ]]; then
  CONFIGS+=(/etc/nginx/conf.d/odoo-paas-control-plane.conf)
fi

if ((${#DOMAINS[@]} == 0)) && ((${#CONFIGS[@]})); then
  mapfile -t DOMAINS < <(
    for config in "${CONFIGS[@]}"; do
      awk '/^[[:space:]]*server_name[[:space:]]/ {
        for (i = 2; i <= NF; i++) {
          gsub(/;/, "", $i)
          if ($i != "_") print $i
        }
      }' "$config"
    done | sort -u
  )
fi

log "Odoo PaaS uninstall plan"
printf '  Remove systemd service : odoo-paas\n'
printf '  Remove nginx sites     : %s\n' "${#CONFIGS[@]}"
printf '  Remove containers      : %s\n' "${#CONTAINERS[@]}"
printf '  Remove code            : %s\n' "$TARGET_DIR"
if [[ "$PURGE" == "1" ]]; then
  printf '  DELETE data directory  : %s\n' "$DATA_DIR"
  printf '  DELETE DB volumes      : %s\n' "${#VOLUMES[@]}"
  if [[ "$KEEP_IMAGES" == "0" ]]; then
    printf '  DELETE base images     : %s\n' "${#IMAGES[@]}"
  else
    printf '  Keep base images       : %s\n' "${#IMAGES[@]}"
  fi
else
  printf '  Keep data directory    : %s\n' "$DATA_DIR"
  printf '  Keep DB volumes        : %s\n' "${#VOLUMES[@]}"
fi
if [[ "$DELETE_CERTS" == "1" ]]; then
  printf '  DELETE certificates    : %s\n' "${#DOMAINS[@]}"
else
  printf '  Keep certificates      : %s\n' "${#DOMAINS[@]}"
fi

if [[ "$DRY_RUN" == "0" && "$ASSUME_YES" == "0" ]]; then
  echo
  if [[ "$PURGE" == "1" ]]; then
    echo "WARNING: --purge deletes PostgreSQL volumes, backups, builds and state."
  fi
  echo "Type 'uninstall' to remove the application, or Ctrl-C to abort."
  read -r -p "Confirm: " answer
  [[ "$answer" == "uninstall" ]] || { echo "Aborted."; exit 1; }
fi

log "Stopping transient restart timers"
if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    run systemctl stop "$unit" >/dev/null 2>&1 || true
  done < <(systemctl list-units --all --plain --no-legend 'odoo-paas-rebind-*' 2>/dev/null | awk '{print $1}')
fi

log "Stopping and removing systemd service"
if command -v systemctl >/dev/null 2>&1; then
  run systemctl disable --now odoo-paas >/dev/null 2>&1 || true
  run rm -f /etc/systemd/system/odoo-paas.service
  run systemctl daemon-reload
  run systemctl reset-failed odoo-paas >/dev/null 2>&1 || true
fi

log "Removing nginx sites"
for config in "${CONFIGS[@]}"; do
  run rm -f "$config"
done
if command -v nginx >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: nginx -t && systemctl reload nginx"
  elif nginx -t >/dev/null 2>&1; then
    systemctl reload nginx >/dev/null 2>&1 || warn "could not reload nginx"
  else
    warn "nginx config test failed after removing Odoo PaaS sites; nginx was not reloaded"
  fi
fi

if [[ "$HAS_DOCKER" == "1" ]]; then
  log "Removing platform containers"
  for container in "${CONTAINERS[@]}"; do
    run docker rm -f "$container" >/dev/null 2>&1 || warn "could not remove container $container"
  done
else
  warn "Docker is unavailable; skipping containers, volumes and images"
fi

log "Removing $TARGET_DIR"
run rm -rf "$TARGET_DIR"

if [[ "$PURGE" == "1" ]]; then
  if [[ "$HAS_DOCKER" == "1" ]]; then
    log "Removing PostgreSQL volumes"
    for volume in "${VOLUMES[@]}"; do
      if docker volume inspect "$volume" >/dev/null 2>&1; then
        run docker volume rm "$volume" >/dev/null 2>&1 || warn "could not remove volume $volume"
      fi
    done
  fi
  log "Removing $DATA_DIR"
  run rm -rf "$DATA_DIR"
  if [[ "$HAS_DOCKER" == "1" && "$KEEP_IMAGES" == "0" ]]; then
    log "Removing Odoo base images"
    for image in "${IMAGES[@]}"; do
      run docker rmi "$image" >/dev/null 2>&1 || warn "could not remove image $image"
    done
  fi
else
  log "Keeping PostgreSQL volumes and $DATA_DIR"
fi

if [[ "$DELETE_CERTS" == "1" ]]; then
  if command -v certbot >/dev/null 2>&1; then
    log "Deleting platform certificates"
    for domain in "${DOMAINS[@]}"; do
      run certbot delete --cert-name "$domain" --non-interactive >/dev/null 2>&1 \
        || warn "could not delete certificate $domain"
    done
  else
    warn "certbot is not installed; certificates were not deleted"
  fi
elif ((${#DOMAINS[@]})); then
  log "Keeping certificates for: ${DOMAINS[*]}"
fi

if [[ "$HAS_DOCKER" == "1" ]]; then
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    log "Removing Docker network $NETWORK"
    run docker network rm "$NETWORK" >/dev/null 2>&1 \
      || warn "could not remove network $NETWORK (containers may still use it)"
  fi
fi

echo
if [[ "$DRY_RUN" == "1" ]]; then
  log "Dry run complete - nothing was changed"
elif [[ "$PURGE" == "1" ]]; then
  log "Odoo PaaS removed (data, volumes and selected artifacts deleted)"
else
  log "Odoo PaaS application removed"
  echo "Kept: PostgreSQL volumes, $DATA_DIR, and Let's Encrypt certificates."
  echo "Re-run install.sh to reinstall; branch domains may need to be re-saved."
fi
echo "Firewall rules and system packages (Docker, nginx, certbot) were left untouched."
