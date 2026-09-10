#!/usr/bin/env bash
#
# PingPulse — build, migrate and start (or update) the production stack.
#
# This is the supported way to bring the stack up. A bare `docker compose up`
# will start containers without applying migrations, because the API no longer
# migrates on start-up: a failed migration must stop the deploy, not leave a
# half-started service answering customers against the wrong schema.
#
# Sequence:
#   validate .env -> build images -> start db+redis -> back up -> migrate
#   -> start everything -> wait for /health -> report
#
#   ./deploy.sh              build and deploy
#   ./deploy.sh --no-build   restart with the current images
#   ./deploy.sh --pull       git pull first, then build and deploy
#
# Safe to re-run. Running it against an already-healthy stack is how you apply
# an update.

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.prod.yml"
DO_BUILD=1
DO_PULL=0

for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --pull)     DO_PULL=1 ;;
    -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
fail() { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ checks
log "Checking prerequisites"

command -v docker >/dev/null || fail "docker is not installed — run setup.sh first"
docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is missing"
[[ -f .env ]] || fail ".env not found. Copy .env.production.template to .env and fill it in."

# Refuse to deploy with placeholder secrets still in place. Catching this here
# is much cheaper than discovering it from a failed certificate or a rejected
# webhook signature an hour later.
if grep -q 'CHANGE_ME' .env; then
  echo
  echo "  .env still contains placeholder values:" >&2
  grep -n 'CHANGE_ME' .env | sed 's/^/    /' >&2
  fail "fill in every CHANGE_ME value before deploying"
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

for required in DOMAIN ACME_EMAIL PUBLIC_BASE_URL POSTGRES_PASSWORD SECRET_KEY \
                TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN GROQ_API_KEY GEMINI_API_KEY; do
  [[ -n "${!required:-}" ]] || fail "$required is empty in .env"
done

if [[ ${#SECRET_KEY} -lt 32 ]]; then
  fail "SECRET_KEY is too short — generate one with: openssl rand -hex 32"
fi

if [[ "${TWILIO_VALIDATE_SIGNATURE:-}" != "true" ]]; then
  printf '    \033[1;33m!!\033[0m TWILIO_VALIDATE_SIGNATURE is not true — the webhook is public and unverified\n'
fi

DATA_DIR="${DATA_DIR:-/opt/pingpulse/data}"
mkdir -p "${DATA_DIR}"/{postgres,redis,media,logs,backups,caddy/data,caddy/config}
ok "environment validated (domain: ${DOMAIN})"

# ------------------------------------------------------------------ source
if [[ $DO_PULL -eq 1 ]]; then
  log "Pulling the latest code"
  git -C ../.. pull --ff-only
  ok "at $(git -C ../.. rev-parse --short HEAD)"
fi

# ------------------------------------------------------------------ build
if [[ $DO_BUILD -eq 1 ]]; then
  log "Building images"
  $COMPOSE build
  ok "images built"
fi

# ------------------------------------------------------------------ data tier
log "Starting the database and cache"
$COMPOSE up -d db redis

printf '    waiting for postgres'
for attempt in $(seq 1 60); do
  if $COMPOSE exec -T db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    printf '\n'; ok "postgres accepting connections"; break
  fi
  printf '.'; sleep 2
  [[ $attempt -eq 60 ]] && { printf '\n'; fail "postgres did not become ready"; }
done

# ------------------------------------------------------------------ backup
# Taken before every migration, so a bad schema change is recoverable. Skipped
# on the very first deploy, when there is nothing to lose yet.
if $COMPOSE exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
     "select 1 from information_schema.tables where table_name='organizations'" 2>/dev/null | grep -q 1; then
  log "Backing up before migrating"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  if $COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
       -f "/backups/pre-deploy-${STAMP}.dump"; then
    ok "saved ${DATA_DIR}/backups/pre-deploy-${STAMP}.dump"
  else
    fail "pre-deploy backup failed — refusing to migrate"
  fi
else
  ok "fresh database, nothing to back up yet"
fi

# ------------------------------------------------------------------ migrate
log "Applying database migrations"
if $COMPOSE run --rm --no-deps \
     -e AUTO_MIGRATE_ON_STARTUP=false \
     backend alembic upgrade head; then
  ok "schema up to date"
else
  fail "migration failed — the previous version is still serving, nothing was switched"
fi

# ------------------------------------------------------------------ start
log "Starting the application"
# --remove-orphans stops containers from services that no longer exist in the
# compose file. Without it, dropping a service leaves its container running —
# which for a web frontend would mean the thing we just stopped serving is
# still up and still listening.
$COMPOSE up -d --remove-orphans
ok "containers started"

# ------------------------------------------------------------------ verify
# /health checks the database, both LLM providers, Twilio and the websocket
# layer — a real deployment gate rather than a liveness ping.
log "Waiting for the API to report healthy"
printf '    '
HEALTHY=0
for attempt in $(seq 1 45); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    HEALTHY=1; printf '\n'; break
  fi
  printf '.'; sleep 4
done
[[ $HEALTHY -eq 1 ]] || { printf '\n'; $COMPOSE logs --tail 40 backend; fail "API did not become healthy"; }

HEALTH_JSON="$(curl -fsS http://127.0.0.1:8000/health)"
echo "$HEALTH_JSON" | jq -r '.components | to_entries[] | "    \(.key): \(.value.status)  \(.value.detail)"' 2>/dev/null \
  || echo "    $HEALTH_JSON"

if echo "$HEALTH_JSON" | grep -q '"status":"error"'; then
  printf '    \033[1;33m!!\033[0m one or more components are unhealthy — check the detail above\n'
fi

# ------------------------------------------------------------------ done
log "Deployment complete"
$COMPOSE ps --format 'table {{.Name}}\t{{.Status}}'

cat <<NEXT

  Dashboard      https://${DOMAIN}
  Health         https://${DOMAIN}/health
  Twilio webhook https://${DOMAIN}/api/v1/whatsapp/webhook

  Logs           docker compose -f docker-compose.prod.yml logs -f backend
  Restart        ./deploy.sh --no-build
  Update         ./deploy.sh --pull

  If this is the first deploy, Caddy may take up to a minute to obtain the
  certificate. Watch it with:
      docker compose -f docker-compose.prod.yml logs -f caddy

NEXT
