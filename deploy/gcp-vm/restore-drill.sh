#!/usr/bin/env bash
#
# PingPulse — prove the backups actually restore.
#
# A backup nobody has restored is a hypothesis. This takes the most recent dump
# *from the bucket* — not the copy on this disk, because the copy on this disk
# is the one that dies with the machine — restores it into a scratch database,
# counts what came back, and compares it against production.
#
#   ./restore-drill.sh
#
# It never touches the live database. The restore target is a separate database
# created for the run and dropped at the end; production is only ever read from,
# and only to count rows.
#
# Run it after any change to backup.sh, and on a calendar reminder otherwise.
# The number it prints at the end — how long a restore takes — is the number to
# quote when someone asks how bad a lost VM would be.

set -euo pipefail

cd "$(dirname "$0")"

[[ -f .env ]] || { echo "restore-drill: .env not found" >&2; exit 1; }
# shellcheck disable=SC1091
set -a; source .env; set +a

DRILL_DB="restore_drill_$(date +%s)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

psql_live() { docker exec pingpulse-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c "$1"; }
psql_drill() { docker exec pingpulse-db psql -U "$POSTGRES_USER" -d "$DRILL_DB" -t -A -c "$1"; }

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
bad() { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------- fetch from the bucket
say "Fetching the newest dump from the bucket"
[[ -n "${BACKUP_GCS_BUCKET:-}" ]] || bad "BACKUP_GCS_BUCKET is not set — there is nothing off this machine to restore from"

LATEST="$(gsutil ls "gs://${BACKUP_GCS_BUCKET}/db/*.dump" | sort | tail -1)"
[[ -n "$LATEST" ]] || bad "no dumps in gs://${BACKUP_GCS_BUCKET}/db/"
gsutil -q cp "$LATEST" "${WORK}/drill.dump"
ok "$(basename "$LATEST") ($(du -h "${WORK}/drill.dump" | cut -f1))"

# ------------------------------------------------------------------- restore
say "Restoring into ${DRILL_DB}"
STARTED="$(date +%s)"

docker cp "${WORK}/drill.dump" "pingpulse-db:/tmp/drill.dump" >/dev/null
docker exec pingpulse-db createdb -U "$POSTGRES_USER" "$DRILL_DB"
# Cleanup runs whatever happens next, so a failed restore does not leave a
# half-populated database lying around with the production one.
trap 'docker exec pingpulse-db dropdb -U "$POSTGRES_USER" --if-exists "$DRILL_DB" >/dev/null 2>&1 || true; docker exec pingpulse-db rm -f /tmp/drill.dump >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

docker exec pingpulse-db pg_restore -U "$POSTGRES_USER" -d "$DRILL_DB" --no-owner /tmp/drill.dump
ELAPSED=$(( $(date +%s) - STARTED ))
ok "restored in ${ELAPSED}s"

# ------------------------------------------------------------------- compare
say "Checking what came back"
FAILED=0
for TABLE in organizations crm_contacts messages access_tokens knowledge_documents channel_configs; do
  LIVE="$(psql_live "select count(*) from ${TABLE};" | tr -d '[:space:]')"
  BACK="$(psql_drill "select count(*) from ${TABLE};" | tr -d '[:space:]')"
  if [[ "$LIVE" == "$BACK" ]]; then
    printf '    %-22s %6s rows  ok\n' "$TABLE" "$BACK"
  else
    printf '    \033[1;33m%-22s %6s rows restored, %s live\033[0m\n' "$TABLE" "$BACK" "$LIVE"
    # Not fatal on its own: the dump was taken earlier, so rows written since
    # then are legitimately missing. Fewer is explainable; more is not.
    [[ "$BACK" -gt "$LIVE" ]] && FAILED=1
  fi
done

# A restore that produces empty tables is the failure this drill exists to
# catch — it is what a corrupt or truncated dump looks like.
ORGS="$(psql_drill "select count(*) from organizations;" | tr -d '[:space:]')"
[[ "$ORGS" -gt 0 ]] || bad "the restored database has no organizations — the dump is not usable"

# And the token table specifically, because a restore that loses it locks every
# client out of a system that is otherwise running fine.
TOKENS="$(psql_drill "select count(*) from access_tokens where is_active;" | tr -d '[:space:]')"
[[ "$TOKENS" -gt 0 ]] || bad "no active access tokens restored — clients could not sign in"
ok "${TOKENS} active token(s) came back"

[[ "$FAILED" -eq 0 ]] || bad "a table restored with more rows than production holds — that is not a stale dump"

say "Drill passed"
echo "    A lost VM costs roughly ${ELAPSED}s of restore, plus rebuild time."
echo "    Source: ${LATEST}"
