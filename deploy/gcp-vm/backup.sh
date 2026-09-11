#!/usr/bin/env bash
#
# PingPulse — nightly backup of the database and stored media.
#
# Installed as a cron job by setup.sh (03:00 daily). Safe to run by hand.
#
# A single VM has no high availability, so backups are what actually protects
# the data: they turn "the VM is gone" from a catastrophe into an afternoon.
# Restore instructions are in README.md.
#
#   ./backup.sh

set -euo pipefail

cd "$(dirname "$0")"

[[ -f .env ]] || { echo "backup: .env not found" >&2; exit 1; }
# shellcheck disable=SC1091
set -a; source .env; set +a

DATA_DIR="${DATA_DIR:-/opt/pingpulse/data}"
BACKUP_DIR="${DATA_DIR}/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
COMPOSE="docker compose -f docker-compose.prod.yml"

mkdir -p "$BACKUP_DIR"
echo "[$(date -Is)] backup starting"

# ------------------------------------------------------------------ database
# Custom format (-Fc) rather than plain SQL: compressed, and restorable
# selectively with pg_restore.
DB_FILE="db-${STAMP}.dump"
if $COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "/backups/${DB_FILE}"; then
  SIZE="$(du -h "${BACKUP_DIR}/${DB_FILE}" | cut -f1)"
  echo "[$(date -Is)] database dumped: ${DB_FILE} (${SIZE})"
else
  echo "[$(date -Is)] ERROR: pg_dump failed" >&2
  exit 1
fi

# ------------------------------------------------------------------ media
# Customer photos and catalogue images. Not in the database, and not
# reproducible if lost.
MEDIA_FILE="media-${STAMP}.tar.gz"
if [[ -d "${DATA_DIR}/media" ]] && [[ -n "$(ls -A "${DATA_DIR}/media" 2>/dev/null)" ]]; then
  tar -czf "${BACKUP_DIR}/${MEDIA_FILE}" -C "${DATA_DIR}" media
  echo "[$(date -Is)] media archived: ${MEDIA_FILE} ($(du -h "${BACKUP_DIR}/${MEDIA_FILE}" | cut -f1))"
else
  MEDIA_FILE=""
  echo "[$(date -Is)] no media to archive"
fi

# --------------------------------------------------------- whatsapp sessions
# The Baileys credentials for every paired handset. Small, and the one thing on
# this disk that cannot be regenerated from anything else: the database can be
# restored, media can be re-sent, but losing these means every client rescans a
# QR code — which means telephoning each of them to say their WhatsApp needs
# reconnecting. Backed up for that reason alone.
#
# They are secrets. The bucket they are copied to has public access prevention
# enforced, and this archive should be treated like the .env beside it.
SESSION_FILE="wa-sessions-${STAMP}.tar.gz"
if [[ -d "${DATA_DIR}/wa_sessions" ]] && [[ -n "$(ls -A "${DATA_DIR}/wa_sessions" 2>/dev/null)" ]]; then
  tar -czf "${BACKUP_DIR}/${SESSION_FILE}" -C "${DATA_DIR}" wa_sessions
  echo "[$(date -Is)] sessions archived: ${SESSION_FILE} ($(du -h "${BACKUP_DIR}/${SESSION_FILE}" | cut -f1))"
else
  SESSION_FILE=""
  echo "[$(date -Is)] no paired sessions to archive"
fi

# ------------------------------------------------------------------ offsite
# On-VM backups do not survive the VM. If a bucket is configured, copy them
# off the machine — this is the difference between a backup and a real one.
if [[ -n "${BACKUP_GCS_BUCKET:-}" ]]; then
  if command -v gsutil >/dev/null 2>&1; then
    # An upload that fails must fail the run. A backup script that reports
    # success while nothing left the machine is worse than one that is missing,
    # because nobody goes looking for it.
    gsutil -q cp "${BACKUP_DIR}/${DB_FILE}" "gs://${BACKUP_GCS_BUCKET}/db/${DB_FILE}" || {
      echo "[$(date -Is)] ERROR: database backup did not reach the bucket" >&2
      exit 1
    }
    [[ -n "$MEDIA_FILE" ]] && \
      gsutil -q cp "${BACKUP_DIR}/${MEDIA_FILE}" "gs://${BACKUP_GCS_BUCKET}/media/${MEDIA_FILE}"
    [[ -n "$SESSION_FILE" ]] && \
      gsutil -q cp "${BACKUP_DIR}/${SESSION_FILE}" "gs://${BACKUP_GCS_BUCKET}/wa-sessions/${SESSION_FILE}"
    echo "[$(date -Is)] uploaded to gs://${BACKUP_GCS_BUCKET}"
  else
    echo "[$(date -Is)] WARNING: BACKUP_GCS_BUCKET is set but gsutil is not installed" >&2
  fi
else
  echo "[$(date -Is)] WARNING: no BACKUP_GCS_BUCKET set — backups exist only on this VM" >&2
fi

# ------------------------------------------------------------------ retention
DELETED="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -o -name '*.tar.gz' \
  | wc -l)"
find "$BACKUP_DIR" -maxdepth 1 \( -name 'db-*.dump' -o -name 'media-*.tar.gz' -o -name 'wa-sessions-*.tar.gz' -o -name 'pre-deploy-*.dump' \) \
  -mtime "+${RETENTION_DAYS}" -print -delete | sed 's/^/[pruned] /'

echo "[$(date -Is)] backup complete — ${DELETED} archive(s) on disk, keeping ${RETENTION_DAYS} days"
echo "[$(date -Is)] disk: $(df -h "${DATA_DIR}" | awk 'NR==2 {print $4" free of "$2}')"
