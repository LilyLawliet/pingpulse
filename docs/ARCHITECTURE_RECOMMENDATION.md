# PingPulse — GCP Deployment & Desktop Packaging Recommendation

**Architectural analysis and setup strategy**
10 September 2026 · Analysis only — no code changed, nothing deployed

---

## 0. Executive summary

| Question | Recommendation | Confidence |
| --- | --- | --- |
| Desktop framework | **Tauri v2** | High |
| Backend compute | **Cloud Run** — after three refactors land | High |
| Interim compute | **Single Compute Engine VM** running today's `docker-compose` | High |
| Database | **Cloud SQL for PostgreSQL 16** + `pgvector` extension | High |
| Cache / broker | **Memorystore for Redis** (Basic tier to start) | Medium |
| Celery worker | **Separate always-on Cloud Run service**, not the API service | High |
| CI/CD | **GitHub Actions → Workload Identity Federation → Artifact Registry → Cloud Run** | High |
| Desktop updates | **Tauri Updater backed by GitHub Releases** | High |

**The headline finding:** the codebase is in good shape, but it is currently written on three
assumptions that hold for a single container and break silently the moment Cloud Run runs a
second instance. None are hard to fix. All three must be fixed *before* the first
production deploy, not after — because each one fails quietly rather than loudly, and the
symptom appears as "the dashboard sometimes misses messages" or "images randomly 404" rather
than as an error anyone can trace.

They are covered in section 2.2. If you would rather ship now and refactor later, section 2.3
gives a legitimate interim path that avoids all three.

---

## 1. Desktop: Tauri vs Electron

### 1.1 What we are actually packaging

The dashboard is a small, self-contained single-page app:

- React 18 + Vite 6 + Tailwind
- Exactly one runtime dependency beyond React: `lucide-react` (icons)
- No router, no SSR, no Node APIs, no native modules
- Talks to the backend over `fetch` and one WebSocket

This matters more than any general Tauri-vs-Electron argument. The app needs a *window with a
browser engine in it* and nothing else. It does not need a bundled Node runtime, filesystem
access, native menus, or OS integration. That profile suits Tauri almost perfectly and wastes
most of what Electron provides.

### 1.2 Comparison

| Criterion | Tauri v2 | Electron | Winner |
| --- | --- | --- | --- |
| Installer size | ~3–8 MB | ~80–150 MB | **Tauri** |
| Installed footprint | ~10–15 MB | ~200–300 MB | **Tauri** |
| Idle memory | ~40–90 MB | ~120–300 MB | **Tauri** |
| Rendering engine | System WebView2 (Windows) | Bundled Chromium | Electron (consistency) |
| Update download size | Small — often a few MB | Large, or delta with extra setup | **Tauri** |
| Security posture | Rust core, no Node in the renderer, explicit allowlist | Full Node in main process; needs careful `contextIsolation` | **Tauri** |
| WebSocket / telemetry | Standard browser `WebSocket` in the WebView — unchanged | Standard browser `WebSocket` — unchanged | Tie |
| Build toolchain | Needs Rust + MSVC build tools | Needs Node only | Electron |
| Ecosystem maturity | Younger, smaller | Very mature, huge ecosystem | Electron |
| Auto-update | Built in, signed manifests | `electron-updater`, well-trodden | Tie |

### 1.3 On WebSockets and telemetry specifically

This is worth addressing directly because it is often assumed to be a differentiator. **It is
not.** Both frameworks render your app in a browser engine, and in both cases
`new WebSocket(...)` is the ordinary browser API. `useMonitorSocket.js` needs no change to its
socket logic under either framework.

Neither framework has an advantage here. The real work is in URL configuration — see 1.5.

### 1.4 Recommendation: Tauri v2

For client distribution on Windows, Tauri is the clear choice:

1. **An 8 MB installer is a materially different sales experience** from a 150 MB one, especially
   for a client downloading over a phone tether or a slow office connection.
2. **Every Windows 10/11 machine already has WebView2**, so there is no runtime to install.
3. **Background updates are small**, which makes "clients never re-download installers" realistic
   rather than aspirational.
4. **The Electron advantages do not apply to us.** Bundled-Chromium consistency matters when you
   support Windows, macOS and Linux against a complex UI. We ship a Tailwind dashboard to
   Windows clients. Node-in-main matters when the desktop app does local file or OS work. Ours
   does none.

**Where Tauri would cost you:** the build machine needs the Rust toolchain and MSVC build tools,
which is a one-time CI setup. And because it uses the system WebView, a client on an unusually
old or locked-down Windows build could see rendering differences. In practice WebView2 is
evergreen and this is rare — but it is the honest trade you are making.

### 1.5 The one real code change desktop packaging requires

The frontend currently assumes it is served from the same origin as the API:

- `frontend/src/api.js` → `const BASE = import.meta.env.VITE_API_BASE || '/api/v1'`
- `frontend/src/useMonitorSocket.js` → builds the socket URL from `window.location.host`

In the browser this works because nginx proxies `/api` and `/ws` to the backend on the same
origin. In a desktop shell the app loads from an internal protocol (`tauri://localhost`), so
both the relative API path and the derived WebSocket host point at the shell itself, not at
your backend. **The dashboard would load and then fail to fetch or connect.**

The fix is small and should be done deliberately, not discovered during packaging:

- Introduce a single configurable backend base URL, injected at build time (`VITE_API_BASE`) or
  read from a settings file at first run.
- Derive the WebSocket URL from that same base rather than from `window.location`.
- Add the desktop origin to `CORS_ORIGINS` on the backend, and stop relying on the `["*"]`
  fallback in `main.py`.

Letting a client point their own installer at their own backend URL also becomes a feature
rather than a rebuild, which matters once several clients are live.

### 1.6 Code signing — do not skip this

An unsigned `.exe` triggers a full-screen Microsoft SmartScreen warning that most non-technical
users will not click through, and some corporate machines will block outright. Budget for an
**OV or EV code-signing certificate** (roughly USD 200–600/year depending on vendor and type).
This applies equally to Tauri and Electron, and it is the single most common reason a polished
desktop build lands badly with clients.

---

## 2. GCP backend architecture

### 2.1 Compute: Cloud Run vs GKE vs Compute Engine

| Criterion | Cloud Run | GKE Autopilot | Compute Engine |
| --- | --- | --- | --- |
| Ops burden | Very low | High | Medium |
| Scale to zero | Yes | No | No |
| Zero-downtime deploys | Built in (revisions + traffic split) | Built in | Manual |
| WebSocket support | Yes, up to 60 min per connection | Yes, unrestricted | Yes, unrestricted |
| Long-running workers | Awkward — see 2.4 | Natural | Natural |
| Cost at low traffic | Lowest | Highest (control plane + nodes) | Low, but always-on |
| Fit for today's code | Needs three refactors | Works as-is | Works as-is |

**GKE is the wrong tool at this stage.** It solves problems you do not have — multi-service
orchestration, complex networking, node-level tuning — and charges a permanent ops tax for the
privilege. Revisit only if you outgrow Cloud Run's model, which on this workload is a long way
off.

**Cloud Run is the right destination.** Managed TLS, revision-based rollbacks, traffic splitting
for canary deploys, scale-to-zero on idle tenants, and a stable HTTPS URL that Twilio can post
to. It matches how this system actually behaves: bursty, webhook-driven, mostly idle.

### 2.2 Three things that must change before Cloud Run runs more than one instance

These are the specific findings from reading the code. Each is invisible in local Docker
because there is exactly one container.

#### (a) WebSocket fan-out is per-process — **this is the important one**

`backend/app/services/ws_manager.py` holds live dashboard sockets in an in-process
`set[WebSocket]`, plus a 100-event in-memory replay history.

With two Cloud Run instances: a Twilio webhook lands on **instance A**, which broadcasts to the
sockets *it* holds. A dashboard connected to **instance B** receives nothing. The operator sees
a conversation list that silently stops updating, with no error anywhere.

**Fix:** publish events to a Redis pub/sub channel and have every instance subscribe and relay
to its own local sockets. Memorystore is already in the recommended stack, so this adds no new
infrastructure — roughly a day of work in `ws_manager`, plus tests.

**Additionally**, enable **session affinity** on the Cloud Run service and set
`--min-instances=1`. Affinity keeps a reconnecting dashboard on the same instance; min-instances
stops scale-to-zero from tearing down every open socket during a quiet hour.

#### (b) Media is written to the local filesystem

`media_service` writes inbound WhatsApp media and catalogue images to `settings.media_dir`
(`/app/media`), served through a `StaticFiles` mount and referenced by `PUBLIC_BASE_URL`.

On Cloud Run the filesystem is an **in-memory tmpfs**: it counts against the instance's memory
limit, it is not shared between instances, and it is destroyed when the instance scales down.
A photo received on instance A returns 404 from instance B, and every stored image disappears
overnight.

**Fix:** write to a **Cloud Storage bucket** and serve media from public object URLs (or signed
URLs). Twilio must be able to fetch outbound `MediaUrl` values, so those objects need public
read or a signed URL with a sensible lifetime. This is contained to `media_service.py`.

#### (c) Migrations run on application startup

`main.py` runs Alembic migrations in the lifespan hook when `AUTO_MIGRATE_ON_STARTUP` is set.
With Cloud Run starting several instances concurrently on a new revision, several processes may
attempt the same migration simultaneously.

**Fix:** set `AUTO_MIGRATE_ON_STARTUP=false` in production and run migrations as an explicit
**Cloud Run Job** step in the deploy pipeline, before traffic shifts to the new revision. The
compose file already establishes the right principle — the worker is explicitly forbidden from
migrating — so this is extending an existing decision, not inventing one.

#### Two smaller items

- **Container port.** `Dockerfile.backend` hardcodes `--port 8000`. Cloud Run injects `$PORT`.
  Either set the container port to 8000 in the service config, or change the CMD to honour
  `${PORT:-8000}`. The second is more portable.
- **Twilio signature validation is off.** `twilio_validate_signature` defaults to `False`. On a
  public Cloud Run URL the webhook is internet-reachable and anyone can POST fabricated messages
  to it. **Turn this on before go-live.** It is the single highest-value security change in this
  document.

### 2.3 A legitimate interim option — worth considering seriously

Given 2.2, there is an honest alternative to doing the refactors first:

**Deploy today's `docker-compose` stack to a single Compute Engine VM** (e2-medium or
e2-standard-2), with Cloud SQL and Memorystore attached, or even the bundled Postgres/Redis
containers to start.

- Every problem in 2.2 disappears, because there is exactly one instance.
- Deployment is roughly an afternoon rather than a sprint.
- Cost is predictable: ~USD 25–50/month for the VM.
- Trade-off: no scale-to-zero, no automatic zero-downtime deploys, a single point of failure,
  and you will migrate later anyway.

**When this is the right call:** you need clients live in the next two weeks, and you expect
fewer than a handful of tenants at low message volume. A single VM comfortably serves that.

**When it is not:** you are onboarding many tenants, or you need the uptime story for
enterprise clients. Then do the refactors and go to Cloud Run directly — retrofitting
multi-instance correctness *after* clients depend on the system is materially harder.

My recommendation: **if the timeline allows two to three weeks, go straight to Cloud Run.** The
three fixes are well-understood and the code is well-structured for them.

### 2.4 The Celery worker — the part that does not fit Cloud Run cleanly

`app/tasks.py` runs a Celery worker (`--pool=solo`) that long-polls Redis for delayed follow-up
tasks. Cloud Run scales on *inbound HTTP requests*; a Celery worker serves none, so there is
nothing for Cloud Run to scale on.

Three options, in order of preference:

| Option | How | Verdict |
| --- | --- | --- |
| **Second Cloud Run service, always on** | Deploy the same image with the Celery command, `--min-instances=1`, `--no-cpu-throttling` | **Recommended.** Keeps one image and one pipeline. Costs one always-warm small instance. |
| **Cloud Tasks + HTTP endpoint** | Replace Celery with Cloud Tasks scheduling a POST to a `/internal/followup` route at the due time | Architecturally the best fit for serverless, and removes Redis-as-broker entirely — but it is a rewrite of the follow-up subsystem. Consider for v2. |
| **Small Compute Engine VM** | Run the worker on a VM | Works, but splits your deployment story across two paradigms. |

Note that `--no-cpu-throttling` is essential for option 1. Without it Cloud Run throttles CPU
outside request handling, and a Celery worker — which is *never* handling a request — would be
starved.

### 2.5 Database: Cloud SQL for PostgreSQL + pgvector

**Recommendation:** Cloud SQL for PostgreSQL 16, `db-g1-small` or `db-custom-1-3840` to start,
with automated backups, PITR, and a private IP reached over the Serverless VPC Connector.

**On pgvector — an important correction to the brief.** The system does **not** currently use
pgvector. `KnowledgeDocument.embedding` is a **JSON column**, and `retrieval.search()` loads
*every* document for the organisation into Python and scores cosine similarity in a loop:

```
statement = select(KnowledgeDocument).where(organization_id == ...)
documents = result.scalars().all()          # full scan
for document in documents:
    vector = cosine_similarity(query_vector, document.embedding)
```

At the current scale (a hundred-odd documents per tenant) this is genuinely fine and fast. It
will not stay fine. At a few thousand documents per tenant you are pulling megabytes of vectors
out of Postgres and into application memory on **every single customer message**.

Adopting pgvector is therefore a **migration, not a configuration flag**:

1. `CREATE EXTENSION vector;` on the Cloud SQL instance (supported).
2. Alembic migration: add a `vector(768)` column alongside the JSON one.
3. Backfill from the existing JSON embeddings.
4. Move scoring into SQL (`ORDER BY embedding <=> :query_vector LIMIT n`), keeping the
   organisation filter in the same `WHERE` clause so tenant isolation stays enforced at the
   query level.
5. Add an HNSW or IVFFlat index.
6. Drop the JSON column once verified.

**Priority: medium.** It is not a launch blocker at your current data volumes, but it is the
first thing that will bite as tenants grow their catalogues. Schedule it deliberately rather
than waiting for a latency complaint.

**Multi-tenant isolation** needs no change. Every tenant-scoped query already filters on
`organization_id` at the SQL level and cross-tenant reads return 404 — that design carries over
to Cloud SQL untouched. A single shared database with enforced row-level scoping is the right
model here; database-per-tenant would multiply cost and operational burden for no security gain
given the existing query discipline.

### 2.6 Cache and broker: Memorystore for Redis

**Recommendation:** Memorystore for Redis, Basic tier, 1 GB, on the same VPC.

It serves three jobs: the Celery broker, the Celery result backend, and — once (a) is fixed —
the WebSocket pub/sub fan-out.

- **Basic tier** has no replica and no automatic failover. For follow-up scheduling and socket
  fan-out that is an acceptable trade at this stage; losing Redis delays nudges and interrupts
  live dashboard updates, but does not lose customer data or block replies. The existing
  fail-fast broker options in `tasks.py` mean a Redis outage will not stall the reply path —
  that was already designed for.
- **Move to Standard tier** (with failover) when live dashboard updates become something clients
  actively rely on.
- Memorystore requires a **Serverless VPC Access Connector** for Cloud Run to reach it. Budget
  for that — it is easy to miss when estimating.

**Redis Cloud** (the third-party managed option) is a reasonable alternative if you want to
avoid the VPC connector, but it means egress off Google's network and another vendor
relationship. Prefer Memorystore.

### 2.7 Webhook routing from Twilio and Meta

This part is straightforward, and the application already behaves correctly.

1. Deploy the API service with `--allow-unauthenticated`. The webhook must be publicly
   reachable; authentication is by **signature validation**, not by IAM.
2. Cloud Run provides a stable HTTPS URL with a managed certificate. Map a **custom domain**
   (e.g. `api.pingpulse.app`) so the webhook URL never changes if you recreate the service.
3. Point Twilio at `https://<your-domain>/api/v1/whatsapp/webhook` — either in the console or
   programmatically via the Senders API `webhook.callback_url` field, which is how the current
   ngrok setup already does it.
4. **Enable `TWILIO_VALIDATE_SIGNATURE=true`.** With signature validation on, Twilio computes the
   signature over the *full public URL* — so the service must see its real external URL. Cloud
   Run sets `X-Forwarded-Proto` correctly and uvicorn already runs with `--proxy-headers`, so
   this works, but it is worth testing explicitly because a mismatch here rejects every message.
5. The handler already returns `200` with empty TwiML on failure, which is correct — a non-2xx
   makes Twilio retry and would double-send replies.
6. `ngrok is no longer needed.` The tunnel exists purely to give a local machine a public URL.
   Cloud Run replaces it entirely, and you can drop the `tunnel` service and the `NGROK_*`
   configuration.

### 2.8 Secrets

Move every credential out of `.env` and into **Secret Manager**, mounted into Cloud Run as
environment variables:

- Twilio auth tokens, Groq keys, Gemini keys (including the numbered rotation keys the config
  already supports), the database password and the JWT signing secret.
- Grant the Cloud Run service account `roles/secretmanager.secretAccessor` only.
- Note that **per-tenant Twilio credentials already live in the `channel_configs` table**, which
  is the right place for them — but that means the database now holds client secrets. Ensure
  Cloud SQL encryption at rest (on by default) and consider application-level encryption of
  those columns before onboarding enterprise clients who will ask.

### 2.9 Indicative monthly cost

Rough order-of-magnitude for a low-traffic production deployment, `europe-west1` or
`me-central1`. **Estimates, not quotes** — real figures depend on region, traffic and committed
use discounts.

| Component | Configuration | Estimate (USD/month) |
| --- | --- | --- |
| Cloud Run — API | min-instances 1, 1 vCPU / 512 MB | 15–35 |
| Cloud Run — worker | min-instances 1, no CPU throttling | 15–30 |
| Cloud SQL PostgreSQL | db-g1-small, 20 GB SSD, backups | 30–50 |
| Memorystore Redis | Basic, 1 GB | 25–35 |
| Serverless VPC connector | Smallest instance | 8–12 |
| Cloud Storage (media) | Low tens of GB + egress | 2–8 |
| Artifact Registry | Image storage | 1–5 |
| **Total** | | **~95–175** |

The two always-on Cloud Run services are the largest controllable line. Scale-to-zero would cut
that substantially, but costs you WebSocket persistence and cold-start latency on the first
message — a poor trade for a product whose pitch is a two-second reply.

Note this is *infrastructure only*. LLM API usage is billed to each client's own keys under the
BYOK model, which keeps your largest variable cost off your own bill entirely.

---

## 3. CI/CD and update strategy

### 3.1 Backend: GitHub Actions → Cloud Run

**Authentication: use Workload Identity Federation, not a service-account JSON key.** WIF lets
the GitHub Actions runner exchange its OIDC token for short-lived GCP credentials, so there is
no long-lived secret in the repository. A leaked JSON key is a full compromise of the project;
this removes that class of risk entirely.

Pipeline shape on push to `main`:

1. **Test** — run `pytest` (253 tests today, ~15 s). Fail the pipeline here, not in production.
2. **Authenticate** to GCP via WIF.
3. **Build** the backend image, tagged with the commit SHA — never only `latest`, so any
   revision is reproducible and rollback is unambiguous.
4. **Push** to Artifact Registry.
5. **Migrate** — execute a Cloud Run Job running `alembic upgrade head`, and fail the deploy if
   it fails. This is the step that replaces `AUTO_MIGRATE_ON_STARTUP`.
6. **Deploy** the new revision to the API service.
7. **Deploy** the same image to the worker service.
8. **Smoke test** — poll `/health` on the new revision and confirm every component reports `ok`.
   The endpoint already checks database, Groq, Gemini, Twilio and the websocket layer, which
   makes it a genuinely useful deployment gate rather than a liveness ping.

**Zero-downtime and rollback.** Cloud Run keeps every revision. Deploy with `--no-traffic`, smoke
test the revision URL, then shift traffic. Rollback is a traffic split back to the previous
revision — seconds, no rebuild. Consider a canary (`--traffic <new>=10`) once you have multiple
paying tenants.

**Environments.** Use two Cloud Run services (`pingpulse-api-staging`, `pingpulse-api-prod`) with
separate Cloud SQL databases. Deploy `main` to staging automatically and promote to production
on a tag or manual approval. Migrations against a production database holding client
conversations deserve a deliberate human gate.

### 3.2 Desktop: Tauri Updater + GitHub Releases

Tauri's updater is built for exactly this and needs no third-party service.

**How it works:** the app checks a static JSON manifest at launch (and optionally on an
interval). If the manifest's version is newer than the running one, it downloads the bundle,
verifies its **signature against a public key compiled into the app**, and installs on next
restart. The signing key is what stops anyone who can serve that URL from pushing arbitrary code
to your clients — treat the private key with the same care as a code-signing certificate.

**Release pipeline** on a version tag (`v1.2.0`):

1. Build the frontend (`vite build`).
2. Build the Tauri bundle on a Windows runner, injecting `VITE_API_BASE` for the target backend.
3. Sign the update artifact with the Tauri private key (stored as a GitHub Actions secret).
4. Sign the installer with your code-signing certificate.
5. Publish installer, update bundle and `latest.json` manifest to a **GitHub Release**.
6. Point the app's updater endpoint at that release asset URL.

The client experience is then: open the dashboard, it updates itself quietly in the background,
next launch is the new version. No re-download, no instructions, no support ticket.

**Two practical notes.** Use a **public** repository for release assets, or a proxy — a private
repo's assets need an authenticated URL, which means shipping a token inside the app. And keep
`tauri.conf.json`'s version, the GitHub tag and the manifest in lockstep; a mismatch produces an
update loop where the app downloads the same version forever.

---

## 4. Implementation roadmap

Sequenced so that each phase is independently verifiable and nothing later depends on something
untested earlier.

### Phase 0 — Prerequisites (before touching GCP)

1. Create the GCP project; enable Cloud Run, Cloud SQL, Memorystore, Artifact Registry, Secret
   Manager, Cloud Build and Serverless VPC Access APIs.
2. Register the production domain and decide the API hostname.
3. Buy the **code-signing certificate** now — issuance (especially OV) can take days to weeks
   and will otherwise block your first desktop release.
4. Decide: interim VM (2.3) or straight to Cloud Run. Everything below assumes Cloud Run.

### Phase 1 — Make the code multi-instance safe *(do this first)*

5. **Redis pub/sub fan-out** in `ws_manager` — the highest-priority change in this document.
6. **Move media to Cloud Storage** in `media_service`.
7. **Disable startup migrations**; prepare the Alembic migration as a standalone job.
8. Make the container honour `$PORT`.
9. **Turn on Twilio signature validation** and test it end to end.
10. Add a configurable API base URL to the frontend and derive the WebSocket URL from it (1.5).
11. Run the full test suite. Add coverage for the new fan-out path — a broadcast that must
    cross two processes is exactly the kind of thing that only fails in production.

### Phase 2 — GCP infrastructure

12. Provision Cloud SQL; enable `pgvector` (even if unused initially, so it is ready).
13. Provision Memorystore and the Serverless VPC Access Connector.
14. Create the media bucket with appropriate public-read or signed-URL policy.
15. Load all secrets into Secret Manager; create a least-privilege service account.
16. Deploy the API service manually once and confirm `/health` reports every component `ok`.
17. Deploy the worker service with `--min-instances=1 --no-cpu-throttling`.
18. Map the custom domain; verify the managed certificate.

### Phase 3 — Cut over the webhook

19. Point Twilio at the Cloud Run URL.
20. Send real messages through the live path. Verify: reply delivered, media fetchable by
    Twilio, dashboard updates in real time, CRM stage advances.
21. **Explicitly test with two instances running** (`--min-instances=2`) — send a burst and
    confirm the dashboard misses nothing. This is the test that proves Phase 1 worked, and it
    is the one most likely to be skipped.
22. Decommission ngrok.

### Phase 4 — Desktop packaging

23. Scaffold Tauri v2 around the existing Vite build.
24. Configure `VITE_API_BASE` for the production backend; add the desktop origin to CORS.
25. Build an unsigned local `.exe`; verify login, live socket updates, media rendering.
26. Sign the installer; verify SmartScreen does not warn.
27. Configure the updater, generate the signing keypair, publish `v0.1.0` as a GitHub Release.
28. Install `v0.1.0` on a clean machine, publish `v0.1.1`, and confirm it self-updates.

### Phase 5 — CI/CD

29. Configure Workload Identity Federation.
30. Build the backend workflow (3.1), deploying to **staging** first.
31. Add the migration job step and the `/health` smoke gate.
32. Add production deploy behind a manual approval.
33. Add the tag-triggered Tauri release workflow.

### Phase 6 — Hardening, then scale

34. Uptime checks on `/health`; alerting on error rate and latency.
35. Log-based metrics for LLM failures and rate limits — you already log provider fallbacks.
36. Load-test the webhook path at realistic burst rates.
37. **Then** schedule the pgvector migration (2.5), before any tenant's catalogue grows large.
38. Document the per-client onboarding runbook: keys, seeding, channel binding, desktop build.

---

## 5. Risk register

| Risk | Impact | Likelihood | Mitigation |
| --- | --- | --- | --- |
| WebSocket fan-out not fixed before multi-instance | Dashboard silently misses events | **High if skipped** | Phase 1.5; test at `--min-instances=2` |
| Media on ephemeral storage | Images 404, data loss | **High if skipped** | Move to GCS in Phase 1 |
| Twilio signature validation left off | Anyone can post fake messages | Medium | One config change; do it in Phase 1 |
| Concurrent startup migrations | Schema corruption on deploy | Medium | Migration job in pipeline |
| Code-signing certificate delay | Desktop launch slips | Medium | Order in Phase 0 |
| Celery worker CPU-throttled | Follow-ups never fire | Medium | `--no-cpu-throttling` |
| Vector search full scan at volume | Reply latency climbs per tenant | Low now, rising | pgvector migration, Phase 6 |
| Memorystore Basic has no failover | Live updates interrupted | Low | Accept now; Standard tier later |

---

## 6. What I would do

1. **Fix the three multi-instance issues first.** They are perhaps a week of focused work, and
   they are much cheaper now than after clients depend on the system.
2. **Go straight to Cloud Run** if the timeline allows. Use the single VM only if you must be
   live within days.
3. **Choose Tauri**, and order the code-signing certificate this week.
4. **Turn on Twilio signature validation before anything is public.**
5. **Leave pgvector until after launch** — it is a real issue, but it is a *scaling* issue, and
   shipping is worth more right now than a search optimisation nobody can yet feel.

---

*Analysis prepared for PingPulse. No code was changed and nothing was deployed in producing
this report.*
