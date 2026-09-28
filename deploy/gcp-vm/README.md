# PingPulse — single-VM deployment on Google Compute Engine

Everything needed to run the full production stack on one Ubuntu VM: FastAPI,
PostgreSQL, Redis, the Celery worker, the dashboard, and a TLS front door.

| File | Purpose |
| --- | --- |
| `create-gcp-vm.sh` | Provisions the static IP, firewall rule and VM (run from your machine) |
| `setup.sh` | One-time VM bootstrap — Docker, firewall, swap, backups, boot unit |
| `.env.production.template` | Every environment variable, with placeholders |
| `docker-compose.prod.yml` | The stack |
| `Caddyfile` | HTTPS reverse proxy with automatic Let's Encrypt |
| `deploy.sh` | Build, back up, migrate, start, verify |
| `backup.sh` | Nightly database + media backup, optional upload to GCS |

---

## What this gives you

The three problems flagged in `ARCHITECTURE_RECOMMENDATION.md` as blockers for
Cloud Run **do not exist here**, because there is exactly one instance of each
service:

- **WebSocket fan-out** — one backend process holds every dashboard socket, so
  every broadcast reaches every client. No Redis relay needed.
- **Media on local disk** — a persistent disk, not an ephemeral one. Files
  survive restarts and are shared by the API and the worker through the same
  bind mount.
- **Migration races** — one API container, and migrations run as an explicit
  gated step in `deploy.sh` regardless.

You also get things the development stack does not have: real TLS on your own
domain (retiring ngrok), signature-verified webhooks, nightly backups, host
firewalling, capped logs, memory limits, and automatic restart after reboot.

## What it does not give you

One VM is one machine. Being straight about it:

- **No high availability.** If the VM fails, PingPulse is down until it comes
  back. Mitigated by automatic restart, healthchecks, nightly backups and disk
  snapshots — but not eliminated. Only multiple instances eliminate it.
- **Deploys have a short gap.** `deploy.sh` recreates containers; expect a few
  seconds where the API refuses connections. Twilio retries on failure, and the
  dashboard reconnects on its own, so in practice this is invisible — but it is
  not the zero-downtime revision switch Cloud Run gives you.
- **Scaling is manual.** Growth means resizing the VM (a stop, a machine-type
  change, a start) rather than an automatic scale-out.

For early clients at low message volume, these are the right trade. When they
stop being the right trade, the migration path is in the architecture report —
and everything here (the image, the env surface, the migration discipline)
carries over.

---

## 1. Create the VM

`create-gcp-vm.sh` does all three steps — static IP, firewall rule, instance —
and is idempotent, so a re-run after a partial failure finishes the rest.

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

cd deploy/gcp-vm
chmod +x create-gcp-vm.sh
./create-gcp-vm.sh --dry-run     # review the commands first
./create-gcp-vm.sh
```

It creates:

| Resource | Name | Detail |
| --- | --- | --- |
| Static external IP | `pingpulse-static-ip` | Premium tier, regional |
| VPC firewall rule | `allow-http-https` | `tcp:80,443` from `0.0.0.0/0`, tagged targets |
| Instance | `pingpulse-prod` | `e2-medium` (2 vCPU / 4 GB), Ubuntu 24.04 LTS, 30 GB `pd-standard`, tags `http-server,https-server` |

Every value is overridable from the environment:

```bash
ZONE=europe-west1-b DISK_TYPE=pd-balanced DISK_SIZE=50GB ./create-gcp-vm.sh
```

> **Port 80 must stay open permanently.** It is not only the HTTPS redirect —
> Let's Encrypt uses it for the HTTP-01 challenge on first issuance *and* on
> every renewal. Closing it later breaks renewal silently, about 60 days on.

> **Disk type.** `pd-standard` is HDD-backed and the cheapest option. For a
> PostgreSQL workload `pd-balanced` (SSD) is noticeably more responsive for a
> few dollars a month — worth it once real clients are on the system.

> **Region.** `me-central1` (Doha) is closest to the UAE and keeps latency low
> for Gulf traffic. Override `REGION` and `ZONE` if your customers are
> elsewhere — `europe-west1` and `us-central1` are the cheapest.

### Optional: scheduled disk snapshots

Cheap insurance against losing the machine itself.

```bash
gcloud compute resource-policies create snapshot-schedule pingpulse-daily \
  --region=me-central1 \
  --max-retention-days=14 \
  --daily-schedule \
  --start-time=02:00 \
  --on-source-disk-delete=keep-auto-snapshots

gcloud compute disks add-resource-policies pingpulse-prod \
  --zone=me-central1-a \
  --resource-policies=pingpulse-daily
```

## 2. Point DNS at the VM

Create an **A record** for your domain pointing at the static IP, **before**
deploying. Caddy proves domain ownership over HTTP to get its certificate; if
DNS does not resolve yet, issuance fails and the site stays on HTTPS-less
holding.

Verify from your machine:

```bash
dig +short pingpulse.example.com    # must return the static IP
```

## 3. Bootstrap the VM

```bash
gcloud compute ssh pingpulse-prod --zone=me-central1-a

# copy setup.sh up, or clone the repo first and run it from there
chmod +x setup.sh
sudo ./setup.sh
```

Then log out and back in, so docker group membership takes effect.

## 4. Configure and deploy

```bash
git clone <your-repo-url> /opt/pingpulse/app
cd /opt/pingpulse/app/deploy/gcp-vm

cp .env.production.template .env
nano .env            # fill in every CHANGE_ME
chmod 600 .env

chmod +x deploy.sh backup.sh
./deploy.sh
```

`deploy.sh` refuses to start if any placeholder remains, backs the database up
before migrating, fails the deploy if the migration fails, and waits for
`/health` to report every component healthy before declaring success.

Generate the two secrets it asks for with:

```bash
openssl rand -base64 32     # POSTGRES_PASSWORD
openssl rand -hex 32        # SECRET_KEY
```

## 5. Point Twilio at it

Set the webhook to:

```
https://YOUR_DOMAIN/api/v1/whatsapp/webhook
```

Either in the Twilio Console, or programmatically via the Senders API
`webhook.callback_url` field — the same field the ngrok setup used.

Then send a real WhatsApp message and confirm end to end:

- a reply arrives on the phone,
- the conversation appears live on `https://YOUR_DOMAIN`,
- the CRM stage advances,
- any product image is fetchable by Twilio.

> **Signature validation.** `TWILIO_VALIDATE_SIGNATURE=true` means Twilio's
> signature is checked against the full public URL. If every message starts
> failing with a 403, the usual cause is `PUBLIC_BASE_URL` not exactly matching
> the URL Twilio is calling — check scheme, host and trailing slash.

## 6. Seed a tenant

```bash
cd /opt/pingpulse/app/deploy/gcp-vm
docker compose -f docker-compose.prod.yml exec backend python - < ../../scripts/seed_retail_demo.py
```

---

## Day-to-day

| Task | Command |
| --- | --- |
| Deploy an update | `./deploy.sh --pull` |
| Restart without rebuilding | `./deploy.sh --no-build` |
| Follow API logs | `docker compose -f docker-compose.prod.yml logs -f backend` |
| Follow worker logs | `docker compose -f docker-compose.prod.yml logs -f worker` |
| Certificate trouble | `docker compose -f docker-compose.prod.yml logs -f caddy` |
| Service status | `docker compose -f docker-compose.prod.yml ps` |
| Health detail | `curl -s https://YOUR_DOMAIN/health \| jq` |
| Manual backup | `./backup.sh` |
| Database shell | `docker compose -f docker-compose.prod.yml exec db psql -U pingpulse -d pingpulse` |
| Disk usage | `df -h /opt/pingpulse/data` |

### Restoring from a backup

```bash
cd /opt/pingpulse/app/deploy/gcp-vm
docker compose -f docker-compose.prod.yml stop backend worker

docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U pingpulse -d pingpulse --clean --if-exists \
  /backups/db-20260910-030000.dump

docker compose -f docker-compose.prod.yml start backend worker
```

Media restores by extracting the matching archive over `${DATA_DIR}/media`.

**Test this before you need it.** An untested backup is a hypothesis.

---

## Notes

**Version skew.** Production runs `pgvector/pgvector:pg16`; development runs
`postgres:15`. This is a fresh database, so adopting 16 now costs nothing and
means the `vector` extension is already present for the vector-search migration
described in the architecture report. Align the development stack to 16 when
convenient — `pg_dump`/`pg_restore` across the two works fine in the meantime.

**Enabling pgvector**, once the code is ready for it:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The extension ships in the image; nothing else needs installing.

**Nothing is reachable from the internet except Caddy.** Postgres and Redis
publish no ports at all. The API is bound to `127.0.0.1:8000` so it can be
reached over an SSH tunnel for debugging, but not from outside.

**Container memory limits** are set for a 4 GB machine. On `e2-standard-2`
(8 GB) you can raise `backend` and `db` — Postgres in particular benefits from
more cache.

**Cost.** Roughly $25–35/month for `e2-medium` plus disk, snapshots and egress;
around $50 for `e2-standard-2`. The $300 trial credit covers the full 90 days
with room to spare. LLM usage is billed to each client's own keys under the
BYOK model, so it never appears on this bill.

## Checking the agent before and after a deploy

`scripts/replay_eval.py` replays scripted conversations through a client's Test
agent endpoint, with the real AI and nothing sent, and scores every reply.

    python scripts/replay_eval.py evals/*.json --backend https://pingpulse.duckdns.org --token <client token>

It prints each failing reply and why, how many replies came without the AI,
and the median and slowest reply time. Run it against a staging client before
a deploy and against each client after they upload new documents. Add a suite
under `evals/` whenever a client finds a failure.
