#!/usr/bin/env bash
#
# Ship the current code to the production VM.
#
# Replaces the three-command dance of tar, scp and ssh. Everything it does was
# already being done by hand; this makes it one command so a small fix is not a
# five-minute chore.
#
#   ./push.sh                 package, upload, deploy
#   ./push.sh --test          run the test suite first, and stop if it fails
#   ./push.sh --no-build      restart on the server without rebuilding images
#   ./push.sh --dry-run       show what would be sent, change nothing
#
# Run from D:\pingpulse in Git Bash. Nothing is written outside Drive D:.

set -euo pipefail

cd "$(dirname "$0")"

VM="pingpulse-prod"
ZONE="me-central1-b"
PROJECT="pingpulse-508212"
REMOTE="/opt/pingpulse/app"
ARCHIVE=".tmp/push.tgz"

GCLOUD="/d/google-cloud-sdk/bin/gcloud"
export CLOUDSDK_PYTHON="D:\\google-cloud-sdk\\platform\\bundledpython\\python.exe"
VENV_PY="/d/pingpulse/.venv/Scripts/python.exe"

RUN_TESTS=0
BUILD_ARG=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --test)     RUN_TESTS=1 ;;
    --no-build) BUILD_ARG="--no-build" ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
fail() { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$GCLOUD" >/dev/null 2>&1 || [ -x "$GCLOUD" ] || fail "gcloud not found at $GCLOUD"

# ------------------------------------------------------------------ tests
if [[ $RUN_TESTS -eq 1 ]]; then
  log "Running the test suite"
  if "$VENV_PY" -m pytest backend/tests -q 2>&1 | tail -3; then
    ok "tests passed"
  else
    fail "tests failed — nothing was deployed"
  fi
fi

# ---------------------------------------------------------------- package
# Only what the server actually builds from. node_modules, the virtualenv,
# demo videos and the Rust target directory are hundreds of megabytes and
# would make every deploy a long upload.
log "Packaging"
mkdir -p .tmp
tar -czf "$ARCHIVE" \
  --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' \
  backend/app backend/alembic backend/alembic.ini backend/requirements.txt backend/tests \
  frontend/src frontend/index.html frontend/package.json frontend/package-lock.json \
  frontend/vite.config.js frontend/tailwind.config.js frontend/postcss.config.js \
  docker/nginx.conf Dockerfile.backend Dockerfile.frontend \
  deploy/gcp-vm scripts

SIZE="$(du -k "$ARCHIVE" | cut -f1)"
ok "${SIZE} KB, $(tar -tzf "$ARCHIVE" | wc -l | tr -d ' ') files"

if [[ $DRY_RUN -eq 1 ]]; then
  log "Dry run — would deploy to ${VM} (${ZONE})"
  tar -tzf "$ARCHIVE" | head -20 | sed 's/^/    /'
  echo "    ..."
  exit 0
fi

# ----------------------------------------------------------------- upload
log "Uploading to ${VM}"
"$GCLOUD" compute scp "$ARCHIVE" "${VM}:push.tgz" \
  --zone="$ZONE" --project="$PROJECT" --quiet >/dev/null 2>&1 \
  || fail "upload failed — check: $GCLOUD compute ssh $VM --zone=$ZONE"
ok "uploaded"

# ----------------------------------------------------------------- deploy
# `sg docker` because the login shell's group membership only applies to a
# fresh login, and this is a one-shot non-interactive command.
log "Deploying"
"$GCLOUD" compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --quiet --command \
  "cd ${REMOTE} && tar -xzf ~/push.tgz && rm -f ~/push.tgz && chmod +x deploy/gcp-vm/*.sh \
   && cd deploy/gcp-vm && sg docker -c './deploy.sh ${BUILD_ARG}'" 2>&1 \
  | grep -E "migration|schema|healthy|ok |FAIL|error|Deployment complete|: ok|: error" \
  | sed 's/^/    /'

# ----------------------------------------------------------------- verify
log "Verifying from outside"
STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 https://pingpulse.duckdns.org/health || echo 000)"
if [[ "$STATUS" == "200" ]]; then
  ok "https://pingpulse.duckdns.org/health -> 200"
else
  fail "health check returned ${STATUS} — check the logs on the VM"
fi

rm -f "$ARCHIVE"
printf '\n\033[1;32mDeployed.\033[0m\n\n'
