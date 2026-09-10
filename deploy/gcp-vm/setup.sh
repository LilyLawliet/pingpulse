#!/usr/bin/env bash
#
# PingPulse — one-time VM bootstrap for Ubuntu 22.04 / 24.04 on Google
# Compute Engine.
#
# Prepares a bare VM to run the production stack: Docker, firewall, swap, log
# rotation, unattended security updates, the data directory layout, and a
# systemd unit so the stack returns after a reboot.
#
# Idempotent — safe to re-run. It installs what is missing and leaves the rest
# alone.
#
#   curl -fsSL <raw-url>/setup.sh -o setup.sh   # or scp it up
#   chmod +x setup.sh
#   sudo ./setup.sh
#
# It does NOT start the stack. Run ./deploy.sh afterwards, once .env is filled
# in — that is a deliberate split, so the machine is ready before any secret
# is on it.

set -euo pipefail

APP_USER="${SUDO_USER:-$(whoami)}"
APP_DIR="/opt/pingpulse"
DATA_DIR="${APP_DIR}/data"
REPO_DIR="${APP_DIR}/app"
SWAP_SIZE="2G"

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!!\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./setup.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------- packages
log "Updating the system"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
  ca-certificates curl gnupg git ufw jq \
  unattended-upgrades postgresql-client
ok "base packages installed"

# ---------------------------------------------------------------- docker
if command -v docker >/dev/null 2>&1; then
  ok "docker already present ($(docker --version))"
else
  log "Installing Docker Engine"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  ok "docker installed ($(docker --version))"
fi

systemctl enable --now docker >/dev/null 2>&1 || true

# Let the login user drive docker without sudo. Takes effect on next login.
if ! id -nG "$APP_USER" | grep -qw docker; then
  usermod -aG docker "$APP_USER"
  warn "added '$APP_USER' to the docker group — log out and back in for it to apply"
fi

# ---------------------------------------------------------------- docker logs
# Container logs are the most common cause of a full boot disk on a small VM.
# The compose file caps each service, and this catches anything started outside it.
log "Capping Docker log growth"
mkdir -p /etc/docker
if [[ -f /etc/docker/daemon.json ]] && ! grep -q '"max-size"' /etc/docker/daemon.json; then
  warn "/etc/docker/daemon.json exists and sets no log cap — review it manually"
elif [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
JSON
  systemctl restart docker
  ok "global docker log rotation configured"
else
  ok "docker log rotation already configured"
fi

# ---------------------------------------------------------------- swap
# GCE images ship without swap. The frontend's Vite build is the memory spike
# that most often gets OOM-killed on a 4 GB machine.
if swapon --show | grep -q '/swapfile'; then
  ok "swap already active"
else
  log "Creating ${SWAP_SIZE} swap file"
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
  ok "swap enabled"
fi

# ---------------------------------------------------------------- firewall
# GCE also has its own VPC firewall — this is the second layer, on the host.
# Both must allow 80/443 for Let's Encrypt to reach Caddy.
log "Configuring the host firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   comment 'SSH' >/dev/null
ufw allow 80/tcp   comment 'HTTP - ACME challenge and redirect' >/dev/null
ufw allow 443/tcp  comment 'HTTPS' >/dev/null
ufw --force enable >/dev/null
ok "ufw active: 22, 80, 443 only"

# ---------------------------------------------------------------- auto updates
log "Enabling unattended security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "security patches will install automatically"

# ---------------------------------------------------------------- layout
log "Creating the data directory layout"
mkdir -p \
  "${DATA_DIR}/postgres" \
  "${DATA_DIR}/redis" \
  "${DATA_DIR}/media" \
  "${DATA_DIR}/logs" \
  "${DATA_DIR}/backups" \
  "${DATA_DIR}/caddy/data" \
  "${DATA_DIR}/caddy/config" \
  "${REPO_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
ok "${DATA_DIR} ready — this one directory holds all state"

# ---------------------------------------------------------------- systemd
# `restart: unless-stopped` already brings containers back when Docker starts.
# This unit additionally guarantees `compose up` runs after a reboot, so a
# service added later is picked up without anyone remembering to log in.
log "Installing the boot-time systemd unit"
cat > /etc/systemd/system/pingpulse.service <<UNIT
[Unit]
Description=PingPulse production stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=${APP_USER}
WorkingDirectory=${REPO_DIR}/deploy/gcp-vm
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pingpulse.service >/dev/null 2>&1 || true
ok "stack will start on boot"

# ---------------------------------------------------------------- backups
log "Scheduling nightly database backups"
CRON_LINE="0 3 * * * ${REPO_DIR}/deploy/gcp-vm/backup.sh >> ${DATA_DIR}/logs/backup.log 2>&1"
# Both halves of this fail on a fresh machine: `crontab -l` exits non-zero when
# the user has no crontab yet, and `grep -v` exits 1 when it filters an empty
# input. Under `set -euo pipefail` either one aborts the whole script, so the
# existing entries are gathered tolerantly into a temp file first.
CRON_TMP="$(mktemp)"
crontab -u "$APP_USER" -l 2>/dev/null | grep -v 'deploy/gcp-vm/backup.sh' > "$CRON_TMP" || true
echo "$CRON_LINE" >> "$CRON_TMP"
crontab -u "$APP_USER" "$CRON_TMP"
rm -f "$CRON_TMP"
ok "backup.sh will run at 03:00 daily"

# ---------------------------------------------------------------- done
cat <<NEXT

$(printf '\033[1;32m')VM bootstrap complete.$(printf '\033[0m')

  data directory : ${DATA_DIR}
  application    : ${REPO_DIR}
  firewall       : 22, 80, 443
  docker         : $(docker --version | cut -d, -f1)

Next steps
  1. Log out and back in, so docker group membership applies.

  2. Put the code in place:
       git clone <your-repo-url> ${REPO_DIR}

  3. Configure the environment:
       cd ${REPO_DIR}/deploy/gcp-vm
       cp .env.production.template .env
       nano .env          # fill every CHANGE_ME value
       chmod 600 .env

  4. Point DNS at this VM's static external IP BEFORE deploying —
     Let's Encrypt validates the domain, so the record must resolve first:
       $(curl -s -H 'Metadata-Flavor: Google' \
         http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo '<external ip>')

  5. Deploy:
       ./deploy.sh

NEXT
