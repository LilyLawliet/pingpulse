# PingPulse — working notes

Multi-tenant WhatsApp AI sales agent. Live clients: **Yaha** (leather shoes, Pakistan)
and **Beluga Group** (bathroom remodeling, Miami).

## Where the dashboard is

PingPulse launched on **both desktop and the browser**. One build, `frontend/dist`,
served three ways:

| Where | Address |
|---|---|
| Production, browser | **https://pingpulse.duckdns.org/app/** |
| Production, desktop | the Tauri app, which updates itself on launch |
| Local development | `cd frontend && npm run dev` → `http://localhost:5173` |

The bare domain `https://pingpulse.duckdns.org/` answers with a status line and nothing
else — that is deliberate and does **not** mean no dashboard is served. It is at
`/app/`, mounted by the API itself (`backend/app/main.py`), proxied by Caddy.

`frontend/dist` is in `.gitignore`, so it never reaches the VM through git.
`scripts/make_release.py` is what ships it to both production copies, from the same
build, and verifies each as a client would. A backend-only deploy does not move it.

## Production

Single GCE VM. Project `pingpulse-508212`, zone `me-central1-b`, instance
`pingpulse-prod`. `gcloud` needs `--project` explicitly; the default account points
somewhere else.

Deploy: `scp` into `/opt/pingpulse/app/backend/...`, then
`cd /opt/pingpulse/app/deploy/gcp-vm && sudo ./deploy.sh` — that path, not
`/opt/pingpulse/app`. Alembic migrations run from `deploy.sh`, never at startup.
The `pingpulse-wa-qr` bridge is never restarted by a backend deploy.

Files with the same basename collide in `/tmp` when scp'd together — rename them
(`svc_`, `api_`) first.

## Rules that do not bend

- All code, migrations and UI live on **D:**. Never write to C: except the scratchpad.
- **Never tell a customer an action happened** unless the backend completed and
  verified it. Enforce this with deterministic backend state, not prompt wording.
- No OAuth, no SaaS billing, no unrelated integrations, no speculative features.
- A licence is an access token and the date it expires. Nothing counts machines:
  seats were removed in 1.5.1 because they refused the client rather than anyone
  else, and the refusal looked exactly like an unfinished setup.

## The UI is the user's

They design it themselves. Build with the existing tokens in `frontend/src/index.css`
and offer the result for restyling rather than inventing a look.
