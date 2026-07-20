#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8023}"
BIND_IP="${BIND_IP:-0.0.0.0}"

info() { printf '\033[1;36m[INFO]\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[NAPAKA]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker ni nameščen."
docker compose version >/dev/null 2>&1 || fail "Docker Compose ni na voljo."

cd "$APP_DIR"
mkdir -p data backups backups-offsite
chmod 700 data backups backups-offsite

if [[ ! -f .env ]]; then
  SECRET_KEY="$(openssl rand -hex 32)"
  ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 18)Aa1!"
  cat > .env <<EOF
PUID=$(id -u)
PGID=$(id -g)
PORT=$PORT
BIND_IP=$BIND_IP
TZ=Europe/Ljubljana
DATABASE_PATH=/app/data/skedi.db
SECRET_KEY=$SECRET_KEY
SESSION_COOKIE_SECURE=1
SESSION_COOKIE_NAME=s50ttt_session
SESSION_HOURS=12
SESSION_ABSOLUTE_HOURS=24
TRUST_PROXY=1
TRUSTED_PROXY_NETWORKS=
TRUSTED_HOSTS=skedi.s57zm.eu,localhost,127.0.0.1
OFFSITE_BACKUP_ENABLED=0
OFFSITE_HOST_PATH=./backups-offsite
ADMIN_USERNAME=S57ZM
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_NAME=Marko Zidar
ADMIN_CALLSIGN=S57ZM
EOF
  chmod 600 .env
  printf '%s\n' "$ADMIN_PASSWORD" > .initial-admin-password
  chmod 600 .initial-admin-password
  info "Ustvarjena je nova varna konfiguracija."
else
  ADMIN_PASSWORD="$(cat .initial-admin-password 2>/dev/null || true)"
  info "Obstoječa konfiguracija .env bo ohranjena."
fi

info "Gradim in zaganjam portal ..."
docker compose up -d --build

info "Čakam na odziv portala ..."
for _ in $(seq 1 30); do
  if docker compose exec -T skedi python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" >/dev/null 2>&1; then
    ok "Portal deluje."
    printf '\nNaslov v lokalnem omrežju: http://192.168.1.57:%s\n' "$PORT"
    printf 'Uporabniško ime: S57ZM\n'
    if [[ -n "$ADMIN_PASSWORD" ]]; then
      printf 'Začetno geslo: %s\n' "$ADMIN_PASSWORD"
      printf '\nPortal bo pred nadaljevanjem zahteval spremembo začetnega gesla.\n'
    else
      printf 'Geslo: uporabi svoje že nastavljeno geslo.\n'
    fi
    printf '\nZa stanje uporabi: cd %q && docker compose ps\n' "$APP_DIR"
    exit 0
  fi
  sleep 2
done

docker compose ps
docker compose logs --tail=80 skedi
fail "Portal se ni pravočasno odzval. Zgornji izpis vsebuje dnevnike zagona."
