#!/usr/bin/env bash
#
# PingPulse — provision the Google Compute Engine VM that runs the stack.
#
# Creates, in order:
#   1. a static external IP        (pingpulse-static-ip)
#   2. a VPC firewall rule         (allow-http-https, tcp:80,443 from 0.0.0.0/0)
#   3. an e2-medium Ubuntu 24.04 LTS instance with that IP attached
#
# Run from your own machine, not from the VM. Requires the gcloud CLI, already
# authenticated:
#     gcloud auth login
#     gcloud config set project YOUR_PROJECT_ID
#
#   ./create-gcp-vm.sh                 create everything
#   ./create-gcp-vm.sh --dry-run       print the commands without running them
#
# Idempotent: anything that already exists is reported and skipped, so a
# re-run after a partial failure completes the rest rather than erroring.

set -euo pipefail

# --------------------------------------------------------------- settings
# Override any of these from the environment, e.g.
#     ZONE=europe-west1-b DISK_SIZE=50GB ./create-gcp-vm.sh
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-me-central1}"
ZONE="${ZONE:-me-central1-a}"

INSTANCE_NAME="${INSTANCE_NAME:-pingpulse-prod}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-medium}"          # 2 vCPU, 4 GB RAM
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2404-lts-amd64}"
IMAGE_PROJECT="${IMAGE_PROJECT:-ubuntu-os-cloud}"

DISK_SIZE="${DISK_SIZE:-30GB}"
DISK_TYPE="${DISK_TYPE:-pd-standard}"

STATIC_IP_NAME="${STATIC_IP_NAME:-pingpulse-static-ip}"
FIREWALL_RULE_NAME="${FIREWALL_RULE_NAME:-allow-http-https}"
NETWORK_TAGS="${NETWORK_TAGS:-http-server,https-server}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
skip() { printf '    \033[1;33m--\033[0m %s\n' "$*"; }
fail() { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    \033[2m$ %s\033[0m\n' "$*"
  else
    "$@"
  fi
}

# --------------------------------------------------------------- checks
command -v gcloud >/dev/null || fail "gcloud CLI not found — install the Google Cloud SDK"
[[ -n "$PROJECT_ID" ]] || fail "no project set — run: gcloud config set project YOUR_PROJECT_ID"

log "Configuration"
cat <<CFG
    project        ${PROJECT_ID}
    region / zone  ${REGION} / ${ZONE}
    instance       ${INSTANCE_NAME}  (${MACHINE_TYPE}, 2 vCPU / 4 GB)
    image          ${IMAGE_FAMILY}
    boot disk      ${DISK_SIZE} ${DISK_TYPE}
    static IP      ${STATIC_IP_NAME}
    firewall       ${FIREWALL_RULE_NAME}  tcp:80,443 from 0.0.0.0/0
    tags           ${NETWORK_TAGS}
CFG
[[ $DRY_RUN -eq 1 ]] && printf '\n    \033[1;33mdry run — nothing will be created\033[0m\n'

# --------------------------------------------------------------- 1. static IP
log "1/3  Static external IP address"
if gcloud compute addresses describe "$STATIC_IP_NAME" \
     --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "${STATIC_IP_NAME} already exists"
else
  run gcloud compute addresses create "$STATIC_IP_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --network-tier=PREMIUM \
    --description="Static IP for the PingPulse production VM"
  ok "reserved ${STATIC_IP_NAME}"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  STATIC_IP="$(gcloud compute addresses describe "$STATIC_IP_NAME" \
    --region="$REGION" --project="$PROJECT_ID" --format='value(address)')"
  ok "address: ${STATIC_IP}"
else
  STATIC_IP="<reserved-ip>"
fi

# --------------------------------------------------------------- 2. firewall
log "2/3  VPC firewall rule"
if gcloud compute firewall-rules describe "$FIREWALL_RULE_NAME" \
     --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "${FIREWALL_RULE_NAME} already exists"
else
  # Port 80 is not optional and is not only for the redirect: Let's Encrypt
  # uses it for the HTTP-01 challenge on first issuance AND on every renewal.
  # Closing it later silently breaks certificate renewal ~60 days on.
  run gcloud compute firewall-rules create "$FIREWALL_RULE_NAME" \
    --project="$PROJECT_ID" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=http-server,https-server \
    --description="PingPulse: HTTP for ACME challenges and redirect, HTTPS for traffic"
  ok "created ${FIREWALL_RULE_NAME} (tcp:80,443 from 0.0.0.0/0)"
fi

# --------------------------------------------------------------- 3. instance
log "3/3  Compute Engine instance"
if gcloud compute instances describe "$INSTANCE_NAME" \
     --zone="$ZONE" --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "${INSTANCE_NAME} already exists"
else
  run gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --image-family="$IMAGE_FAMILY" \
    --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="$DISK_SIZE" \
    --boot-disk-type="$DISK_TYPE" \
    --boot-disk-device-name="$INSTANCE_NAME" \
    --tags="$NETWORK_TAGS" \
    --address="$STATIC_IP" \
    --metadata=enable-oslogin=TRUE \
    --scopes=https://www.googleapis.com/auth/devstorage.read_write,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write \
    --labels=app=pingpulse,env=production \
    --description="PingPulse production stack"
  ok "created ${INSTANCE_NAME}"
fi

# --------------------------------------------------------------- next steps
if [[ $DRY_RUN -eq 1 ]]; then
  printf '\n    \033[1;33mdry run complete — nothing was created\033[0m\n\n'
  exit 0
fi

log "Done"
gcloud compute instances list --filter="name=${INSTANCE_NAME}" --project="$PROJECT_ID" \
  --format="table(name,zone.basename(),machineType.basename(),status,EXTERNAL_IP)"

cat <<NEXT

Next steps

  1. Point DNS at the VM. Create an A record for your domain:

         your-domain.com.    A    ${STATIC_IP}

     Do this BEFORE deploying. Let's Encrypt validates the domain over HTTP,
     so the record must already resolve or certificate issuance fails.

     Check it with:  dig +short your-domain.com

  2. Connect:

         gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE} --project=${PROJECT_ID}

  3. Bootstrap the VM, then deploy:

         sudo ./setup.sh
         # log out and back in so docker group membership applies
         git clone <your-repo-url> /opt/pingpulse/app
         cd /opt/pingpulse/app/deploy/gcp-vm
         cp .env.production.template .env && nano .env && chmod 600 .env
         ./deploy.sh

Notes

  * The boot disk is ${DISK_SIZE} ${DISK_TYPE}. pd-standard is HDD-backed and
    the cheapest option; if database responsiveness matters more than a few
    dollars a month, pd-balanced (SSD) is a noticeably better fit for
    PostgreSQL. Re-run with:  DISK_TYPE=pd-balanced ./create-gcp-vm.sh

  * ${DISK_SIZE} holds Ubuntu, the Docker images and the data directory
    comfortably at first. Watch it with  df -h /opt/pingpulse/data  and grow
    it before it fills:
        gcloud compute disks resize ${INSTANCE_NAME} --zone=${ZONE} --size=50GB

  * The static IP is billed while reserved but not attached. If you delete the
    instance and do not need the address, release it:
        gcloud compute addresses delete ${STATIC_IP_NAME} --region=${REGION}

NEXT
