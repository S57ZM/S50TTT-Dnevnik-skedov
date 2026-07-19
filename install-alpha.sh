#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALPHA_PORT="${ALPHA_PORT:-8024}"
BIND_IP="${BIND_IP:-0.0.0.0}"

info() { printf '\033[1;36m[ALPHA INFO]\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m[ALPHA OK]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[ALPHA NAPAKA]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker ni nameščen."
docker compose version >/dev/null 2>&1 || fail "Docker Compose ni na voljo."

cd "$APP_DIR"
mkdir -p data-alpha

if [[ ! -f .env.alpha ]]; then
  SECRET_KEY="$(openssl rand -hex 32)"
  ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 18)Aa1!"
  cat > .env.alpha <<EOF
PUID=$(id -u)
PGID=$(id -g)
ALPHA_PORT=$ALPHA_PORT
BIND_IP=$BIND_IP
TZ=Europe/Ljubljana
DATABASE_PATH=/app/data/skedi-alpha.db
SECRET_KEY=$SECRET_KEY
RELEASE_CHANNEL=alpha
ADMIN_USERNAME=S57ZM
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_NAME=Marko Zidar
ADMIN_CALLSIGN=S57ZM
EOF
  chmod 600 .env.alpha
  printf '%s\n' "$ADMIN_PASSWORD" > .initial-alpha-admin-password
  chmod 600 .initial-alpha-admin-password
  info "Ustvarjena sta ločena konfiguracija in prazna alpha baza."
else
  ADMIN_PASSWORD="$(cat .initial-alpha-admin-password 2>/dev/null || true)"
  info "Obstoječa alpha konfiguracija bo ohranjena."
fi

ALPHA_COMPOSE=(docker compose --env-file .env.alpha -f docker-compose.alpha.yml)

info "Gradim in zaganjam alpha portal ..."
"${ALPHA_COMPOSE[@]}" up -d --build

info "Čakam na odziv alpha portala ..."
for _ in $(seq 1 30); do
  if "${ALPHA_COMPOSE[@]}" exec -T skedi-alpha python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" >/dev/null 2>&1; then
    ok "Alpha portal deluje na portu $ALPHA_PORT."
    printf '\nLokalni naslov: http://192.168.1.57:%s\n' "$ALPHA_PORT"
    printf 'Uporabniško ime: S57ZM\n'
    if [[ -n "$ADMIN_PASSWORD" ]]; then
      printf 'Začetno alpha geslo: %s\n' "$ADMIN_PASSWORD"
    else
      printf 'Geslo: uporabi že nastavljeno alpha geslo.\n'
    fi
    printf '\nAlpha uporablja svojo bazo data-alpha/skedi-alpha.db.\n'
    exit 0
  fi
  sleep 2
done

"${ALPHA_COMPOSE[@]}" ps
"${ALPHA_COMPOSE[@]}" logs --tail=80 skedi-alpha
fail "Alpha portal se ni pravočasno odzval."
