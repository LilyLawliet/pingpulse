# PingPulse — WhatsApp Sales Agent

An AI sales agent for WhatsApp. Twilio delivers inbound customer messages to FastAPI,
which assembles a prompt from the business's own sales rules plus the live chat
history, generates a reply through Groq (falling back to Gemini), sends it back over
WhatsApp, and advances the lead through the pipeline — while streaming every step to a
React dashboard in real time.

**Everything lives on Drive D:** — source, virtualenv, Docker images' bind-mounted data,
build temp, and pip cache. Nothing is written to C:.

---

## 1. Architecture

```
   WhatsApp user
        │  (1) inbound message
        ▼
   ┌─────────┐   POST /api/v1/whatsapp/webhook   ┌──────────────────────┐
   │ Twilio  │ ────────────────────────────────► │  FastAPI (backend)   │
   │ Cloud   │ ◄──────────────────────────────── │  Python 3.13         │
   └─────────┘   (4) client.messages.create      └──────────┬───────────┘
                                                            │
              ┌─────────────────────────────────────────────┼──────────────┐
              │                      │                      │              │
              ▼                      ▼                      ▼              ▼
      ┌───────────────┐   ┌────────────────────┐  ┌──────────────┐  ┌────────────┐
      │ PostgreSQL 15 │   │ Groq  (primary)    │  │ Gemini       │  │ WebSocket  │
      │ organizations │   │ qwen/qwen3.8-27b   │  │ (fallback)   │  │ /ws/monitor│
      │ contacts      │   │ ~580-800 ms        │  │ 3.8-flash    │  │ broadcast  │
      │ messages      │   └────────────────────┘  └──────────────┘  └─────┬──────┘
      │ llm_logs      │                                                   │
      └───────────────┘                                                   ▼
                                                              ┌────────────────────┐
                                                              │ React + Tailwind   │
                                                              │ Nginx :3000        │
                                                              └────────────────────┘
```

### Request lifecycle

| Phase | What happens |
|-------|--------------|
| **1. Onboarding** | A business is registered via the dashboard: name, target audience tone, product rules, and base sales system prompt → `organizations`. |
| **2. Ingestion** | Twilio POSTs an `x-www-form-urlencoded` payload to `/api/v1/whatsapp/webhook`. The phone number is matched or auto-created in `contacts` (stage `LEAD`), recent history is loaded, and an `inbound_message` event is pushed to the dashboard. |
| **3. Generation** | The prompt is assembled as **Organization Prompt + Customer Metadata + Chat History + Latest Message**, sent to Groq, and — on any Groq failure (rate limit, timeout, outage) — retried on Gemini. Latency, provider, prompt and raw output are streamed live and written to `llm_logs`. |
| **4. Dispatch** | The reply is sent back through the Twilio REST API, stored in `messages`, and the contact's pipeline stage is re-evaluated (`LEAD → QUALIFIED → DEMO_BOOKED → CLOSED`). Stages only ever move forward. |

### Layers

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| Messaging | Twilio WhatsApp API | Inbound webhook + outbound REST send |
| Backend | FastAPI + asyncio (Python 3.13) | Webhooks, prompt assembly, LLM fallback, WebSocket fan-out |
| Intelligence | Groq (primary), Gemini (fallback) | Reply generation; automatic failover |
| Storage | PostgreSQL 15 + async SQLAlchemy | Organizations, contacts, messages, LLM execution logs |
| Real-time UI | React 18 + Tailwind + Vite, served by Nginx | Conversations, pipeline, business setup |

---

## 2. Layout

```
D:\pingpulse\
├── docker-compose.yml          # 3 services, all volumes bound to D:
├── Dockerfile.backend          # python:3.13-slim + uvicorn
├── Dockerfile.frontend         # node:20 build -> nginx:1.27-alpine
├── .env                        # all credentials and configuration
├── README.md
├── backend/
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── pytest.ini
│   ├── alembic/
│   │   ├── env.py              # async env, reads DATABASE_URL from .env
│   │   └── versions/           # initial schema migration
│   ├── app/
│   │   ├── main.py             # entry point, /health, /ws/monitor, startup migration
│   │   ├── config.py           # pydantic-settings bound to .env
│   │   ├── database.py         # async engine + session factory
│   │   ├── models.py           # Organization, Contact, Message, LLMLog
│   │   ├── schemas.py          # Pydantic webhook + API schemas
│   │   ├── services/
│   │   │   ├── twilio_service.py   # outbound send + credential probe
│   │   │   ├── llm_service.py      # prompt builder + Groq/Gemini fallback
│   │   │   └── ws_manager.py       # WebSocket connection manager
│   │   └── api/
│   │       ├── webhook.py      # Twilio webhook + inbound pipeline
│   │       └── routes.py       # dashboard REST endpoints
│   └── tests/                  # 35 tests
├── frontend/
│   └── src/
│       ├── App.jsx
│       ├── api.js
│       ├── format.js                    # stage wording, phone/time formatting
│       ├── useMonitorSocket.js          # auto-reconnecting WS client
│       └── components/
│           ├── ConversationList.jsx     # who is talking to you
│           ├── ConversationThread.jsx   # the chat, with a typing indicator
│           ├── PipelineBoard.jsx        # funnel by stage
│           ├── MetricStrip.jsx          # headline numbers
│           ├── PulseLine.jsx            # live throughput trace
│           └── OrgSelector.jsx          # business switcher + setup sheet
├── scripts/
│   └── record_demo.py          # drives the real product and records the demo
├── demo/                       # rendered demo video, GIF and poster frame
├── docker/
│   ├── nginx.conf              # SPA + /api + /ws proxy
│   ├── postgres-data/          # ← PostgreSQL data volume (on D:)
│   └── backend-logs/           # ← backend log volume (on D:)
├── .venv/                      # local virtualenv (on D:)
├── .cache/pip/                 # pip cache (on D:)
└── .tmp/                       # build temp (on D:)
```

---

## 3. Environment setup

All configuration lives in a single `D:\pingpulse\.env`, read by `app/config.py`,
by Alembic, and by `docker-compose.yml`.

| Variable | Purpose |
|----------|---------|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio REST credentials |
| `TWILIO_WHATSAPP_NUMBER` | Sender number; `whatsapp:` prefix is added automatically |
| `GROQ_API_KEY` / `GROQ_MODEL` | Primary generation |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Fallback generation |
| `DATABASE_URL` | `postgresql+asyncpg://…`; Compose overrides the host to `db` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Database container credentials |
| `POSTGRES_HOST_PORT` | Host port for PostgreSQL (**5433** — 5432 is already in use on this machine) |
| `BACKEND_PORT` / `FRONTEND_PORT` | Published ports (8000 / 3000) |
| `CHAT_HISTORY_LIMIT` | Messages of history folded into each prompt (default 12) |
| `LLM_TIMEOUT_SECONDS` | Per-provider timeout before failover |
| `REPLY_DEADLINE_SECONDS` | The whole reply, both providers included (default 14) |
| `UNDERSTANDING_MODEL` | Groq model that reads documents and messages into structured form; empty uses `GROQ_MODEL`. A stronger model here pays for itself: a misread order is a wrong quote |
| `UNDERSTANDING_TIMEOUT_SECONDS` | Reading one message while the customer waits (default 6); past it the built-in reader is used |
| `EXTRACTION_TIMEOUT_SECONDS` | Reading one uploaded file (default 60) |
| `AUTO_MIGRATE_ON_STARTUP` | Run `alembic upgrade head` during FastAPI startup |
| `CORS_ORIGINS` | Comma-separated allowed origins |

### Local virtualenv (on Drive D:)

```powershell
D:\pingpulse\.venv\Scripts\python.exe -m pip install -r D:\pingpulse\backend\requirements.txt
```

Keep build artefacts off C: by pointing temp and cache at D: first:

```powershell
$env:TMP = "D:\pingpulse\.tmp"; $env:TEMP = "D:\pingpulse\.tmp"
$env:PIP_CACHE_DIR = "D:\pingpulse\.cache\pip"
```

---

## 4. Database migrations

Alembic reads `DATABASE_URL` from `.env` through `app.config`, so no URL is
hard-coded in `alembic.ini`.

```powershell
cd D:\pingpulse\backend

D:\pingpulse\.venv\Scripts\alembic.exe upgrade head        # apply migrations
D:\pingpulse\.venv\Scripts\alembic.exe current             # show current revision
D:\pingpulse\.venv\Scripts\alembic.exe check               # detect model/schema drift
D:\pingpulse\.venv\Scripts\alembic.exe downgrade -1        # roll back one revision

# after changing models.py
D:\pingpulse\.venv\Scripts\alembic.exe revision --autogenerate -m "describe change"
```

The backend also migrates itself on startup when `AUTO_MIGRATE_ON_STARTUP=true`
(`app/main.py::run_migrations`), so `docker compose up` needs no separate step. To
migrate inside the running container explicitly:

```powershell
docker exec pingpulse-backend alembic upgrade head
```

### Schema

`organizations` (id, name, sales_prompt, target_tone, product_rules, created_at) ·
`contacts` (id, organization_id → organizations, phone_number **unique**, name,
pipeline_stage, created_at) · `messages` (id, contact_id → contacts, sender, content,
twilio_sid, created_at) · `llm_logs` (id, message_id → messages, provider, prompt_used,
raw_response, latency_ms, created_at). All primary keys are native PostgreSQL `uuid`.

---

## 5. Running with Docker

```powershell
cd D:\pingpulse
docker compose up --build          # build and start all three services
docker compose ps                  # show health of each service
docker compose logs -f backend     # follow backend logs
docker compose down                # stop (database data on D: is preserved)
docker compose down -v             # stop and drop volumes
```

The Compose project is named **pingpulse**, so containers are `pingpulse-db`,
`pingpulse-backend`, and `pingpulse-frontend`, on the `pingpulse-net` network.

### Storage — all bound to Drive D:

| Path in container | Host path |
|-------------------|-----------|
| `/var/lib/postgresql/data` | `D:\pingpulse\docker\postgres-data` |
| `/app/logs` | `D:\pingpulse\docker\backend-logs` |

### Health checks and start order

| Service | Check | Interval / retries |
|---------|-------|--------------------|
| `db` | `pg_isready -U pingpulse -d pingpulse` | 10s, 10 retries, 20s grace |
| `backend` | HTTP GET `/health` (stdlib `urllib`, no extra deps) | 15s, 5 retries, 40s grace |
| `frontend` | `wget http://127.0.0.1:3000/nginx-health` | 15s, 5 retries, 15s grace |

`backend` declares `depends_on: db: condition: service_healthy`, and `frontend`
depends on `backend` being healthy — so the stack starts strictly in order:
**db healthy → backend → backend healthy → frontend**.

---

## 6. The dashboard

PingPulse launched on both desktop and the browser, and the dashboard is one build
served three ways:

| Where | Address |
|---|---|
| **Production, in a browser** | <https://pingpulse.duckdns.org/app/> |
| **Production, desktop app** | the Tauri app, which updates itself |
| **Local development** | `cd frontend && npm run dev` → `http://localhost:5173` |

The same `frontend/dist` goes to all three, so there is no second version to keep in
step. `scripts/make_release.py` builds it once, ships it to both production copies and
then verifies each as a client would.

It is built for the business owner, not the operator, so it deliberately shows no model
names, latencies, prompts or infrastructure health — that detail stays on the API
(`/api/v1/logs`, `/health`) for whoever needs it.

Three panes, left to right: **Conversations** (everyone talking to you, with a live
typing indicator while the agent writes), the **thread** itself, and the **Pipeline**
board showing where each person sits in the funnel. Above them, four headline numbers
and a throughput trace that beats with live traffic. Stage names are shown in plain
English — `LEAD → QUALIFIED → DEMO_BOOKED → CLOSED` reads as *New lead → Interested →
Booked → Won*.

### Recording a demo

`scripts/record_demo.py` drives the real product end to end and records the browser:
three shoppers message in, the agent replies for real, and the funnel fills on camera.
Nothing is mocked — every reply in the video came from the running stack, quoting real
prices out of the business's own setup.

```powershell
docker compose up -d
$env:PLAYWRIGHT_BROWSERS_PATH = "D:\pingpulse\.cache\playwright"
D:\pingpulse\.venv\Scripts\python.exe -m pip install playwright
D:\pingpulse\.venv\Scripts\python.exe -m playwright install chromium
D:\pingpulse\.venv\Scripts\python.exe scripts\record_demo.py
```

Clear the stage first so the console starts empty and fills on camera:

```powershell
docker exec pingpulse-db psql -U pingpulse -d pingpulse -c "DELETE FROM contacts;"
```

Playwright captures the viewport to `.webm`, then the script re-encodes it and deletes
the capture, leaving one file: **`demo/pingpulse-demo.mp4`**.

| Setting | Value | Why |
|---------|-------|-----|
| Container | `.mp4` | Plays in any deck, browser or player |
| Video codec | `libx264` | Universal hardware decode |
| Pixel format | `yuv420p` | Required by QuickTime, PowerPoint and mobile |
| Frame rate | `-vf fps=30 -r 30` | Constant 30 fps — resampled, so no stutter |
| Quality | `-preset slow -crf 18` | Visually lossless for UI text |
| Streaming | `-movflags +faststart` | Starts playing before it finishes downloading |

ffmpeg is resolved from `PATH`, then `$FFMPEG` (executable or its folder), then
`tools/ffmpeg.exe`, which is where this repo keeps a copy.

## 7. Multi-tenancy, CRM and knowledge

One account, many businesses. A **user** signs in, belongs to one or more
**organizations** through `organization_members`, and works inside whichever one
is active. Every tenant-owned table carries `organization_id`, and every query
that touches one is filtered by the organization resolved from the caller's
session — an id is never taken from the request body.

| Table | Holds |
|-------|-------|
| `users` | Accounts, password hashes, the active organization |
| `organizations` | Tenants, plus `default_currency` and `default_language` |
| `organization_members` | Who may act in which organization, and as what role |
| `crm_contacts` | Leads, tags, notes and remembered facts, per organization |
| `channel_configs` | The WhatsApp number each organization is reached on |
| `knowledge_documents` | Knowledge chunks and their embeddings, per organization |

Roles are `OWNER`, `ADMIN`, `AGENT`, `VIEWER`. Admin rights are needed to edit an
organization, manage members, or connect a channel; a viewer can read but not write.

### How isolation is enforced

`current_org` resolves the active organization **and re-checks membership on every
request**, so revoking someone's access takes effect immediately rather than when
their token expires. Handlers then filter on that id. Cross-tenant reads return
**404, not 403** — confirming that a record exists is itself a disclosure.

Phone numbers are unique **per organization**, not globally: the same shopper may
be a customer of two businesses on the platform, and neither sees the other's
conversation.

### Inbound routing

The number Twilio delivered to identifies the tenant, via `channel_configs`. If no
channel matches, routing falls back to `DEFAULT_ORGANIZATION_ID`, then to the only
organization when just one exists — so a single-tenant install keeps working
untouched while a multi-tenant one routes strictly by number.

### Currency and language

`default_currency` and `default_language` are injected into every prompt that
organization generates. A PKR/Urdu shop is told to quote Pakistani rupees and reply
in Urdu; a EUR/French one gets euros and French. The agent is explicitly told never
to convert between currencies.

### Knowledge retrieval (hybrid)

`POST /api/v1/knowledge/documents` embeds and stores a document against the active
organization. Search combines two signals — vector similarity (65%) for meaning and
keyword overlap (35%) for exact terms like product names — and `organization_id` is
a WHERE clause on the candidate query, so another tenant's rows are never loaded
into memory, let alone scored.

Embeddings come from Gemini (`gemini-embedding-001`). With no key or during an
outage, a deterministic hashed bag-of-words vector is used instead so indexing and
retrieval keep working; the model actually used is recorded on each document.

```powershell
# Prove isolation against the running stack
D:\pingpulse\.venv\Scripts\python.exe scripts\smoke_tenancy.py
```

## 8. The sales agent pipeline

Every inbound message runs the two-step pipeline from the architecture guide,
rather than handing the whole conversation to the model and hoping.

```
message -> analyzer -> customer memory -> scoped retrieval -> product match
        -> sales policy -> response -> memory / CRM update
```

**Step 1 — analyzer** (`services/analyzer.py`) returns strict JSON: intent,
sales stage, colour, fabric, budget, city, objection, whether they asked for
pictures, and the required next action. Any failure falls back to a
deterministic keyword reading, so an outage costs accuracy, never a reply.

**Customer memory** (`services/customer_memory.py`) is structured, not a
transcript. Each fact records its source and confidence, so something the
customer stated outranks something the model inferred. A withdrawn requirement
is marked inactive rather than deleted, and shown to the agent as "no longer
wanted" so it is not offered again.

**Retrieval** is scoped twice: to the organization, and to `doc_type="policy"`.
Products are surfaced separately as compact rows, because returning a full
product description as the answer to "how do I pay?" is exactly the failure
this split prevents.

**Product matching** (`services/product_search.py`) is attribute-first, not
embedding-first. Colour is a hard filter with whole-word matching — plain
substring matching silently turns every *emb-red-oidered* item red — and an
exact colour outranks a family match (red before maroon), which outranks
nothing at all.

**Sales policy** (`services/sales_policy.py`) carries the standing rules and the
per-turn directive for the current stage:

```
NEW -> DISCOVERY -> QUALIFIED -> PRESENTATION -> OBJECTION
    -> NEGOTIATION -> READY_TO_BUY -> CLOSED
```

`crm_contacts.sales_stage` holds that state; `pipeline_stage` is the operator
rollup shown on the CRM board.

### No generic handoffs

The agent is the shop. Any reply containing "our team will get back to you",
"we'll contact you shortly", "a representative will call" or similar is
**rejected and regenerated**, and a second offence fails the generation rather
than sending it. When both providers are down the customer still gets a real
answer — matched products with prices, or the retrieved fact read out — never a
promise that a human will follow up.

### Attachments, both directions

| Direction | Behaviour |
|-----------|-----------|
| Inbound | `MediaUrl0..N` are downloaded from Twilio (which requires auth and expires them) into the media volume on **D:**, and re-served from `PUBLIC_BASE_URL/media/...` |
| Outbound | Product photos are attached via Twilio's `media_url`. If a send with attachments fails, it retries once as text — the answer matters more than the picture |

`MEDIA_DIR` is bind-mounted to `D:\pingpulse\docker\media`. `PUBLIC_BASE_URL`
must be publicly reachable or WhatsApp cannot fetch what we host.

### Catalogue ingestion

`scripts/seed_nishat_linen.py` resets the tenant and loads the knowledge base:

```powershell
D:\pingpulse\.venv\Scripts\python.exe scripts\seed_nishat_linen.py --products 100
```

It keeps existing channel bindings, re-points them at the new organization,
loads the operational policies, and reads the live catalogue from the store's
public Shopify `products.json` feed — structured title, price, colour, fabric
and CDN images, which is both more reliable and lighter than scraping HTML.
Only public catalogue data is read; the store's robots.txt permits that while
prohibiting automated checkout, which nothing here goes near.

Live behaviour checks:

```powershell
D:\pingpulse\.venv\Scripts\python.exe scripts\test_nishat_agent.py
```

## 9. Endpoints

Base URL `http://localhost:8000` (also reachable through Nginx on `http://localhost:3000`).

### WhatsApp

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/whatsapp/webhook` | Twilio inbound webhook (form-encoded). Always returns `200` + empty TwiML so Twilio never retries and double-sends. |
| `GET` | `/api/v1/whatsapp/webhook` | Browser-friendly reachability probe |

### Dashboard

| Method | Path | Purpose |
|--------|------|---------|
| `POST` / `GET` | `/api/v1/organizations` | Create / list businesses |
| `GET` / `PATCH` | `/api/v1/organizations/{id}` | Fetch / update a business profile |
| `POST` / `GET` | `/api/v1/contacts` | Create / list contacts (filter by `organization_id`, `pipeline_stage`) |
| `PATCH` | `/api/v1/contacts/{id}` | Rename or move a lead between stages |
| `GET` | `/api/v1/contacts/{id}/messages` | Full conversation transcript |
| `GET` | `/api/v1/messages` | Recent messages across all contacts |
| `POST` | `/api/v1/messages/send` | Operator takeover — send a hand-written reply |
| `GET` | `/api/v1/logs` | LLM execution logs (provider, prompt, raw output, latency) |
| `GET` | `/api/v1/stats` | Counts, pipeline breakdown, per-provider average latency |

### System

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Live check of PostgreSQL, Groq, Gemini and Twilio |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/openapi.json` | OpenAPI schema |

`/health` returns `200` when healthy **or degraded**, and `503` only when PostgreSQL
is unreachable — a provider outage must not cause Docker to recycle a serving
container. Each provider check verifies that the *configured model* is actually
available on the key, not merely that the API answers.

```json
{
  "status": "ok",
  "components": {
    "database": { "status": "ok", "detail": "PostgreSQL reachable" },
    "groq":     { "status": "ok", "detail": "Groq: model 'qwen/qwen3.8-27b' available" },
    "gemini":   { "status": "ok", "detail": "Gemini: model 'gemini-3.8-flash' available" },
    "twilio":   { "status": "ok", "detail": "Twilio: account … status=active" },
    "websocket":{ "status": "ok", "detail": "0 monitor client(s)" }
  }
}
```

---

## 10. WebSocket monitoring

Connect to **`ws://localhost:8000/ws/monitor`** directly, or
**`ws://localhost:3000/ws/monitor`** through the Nginx proxy (the frontend uses a
relative URL, so it works in both dev and production).

The dashboard client auto-reconnects with 1s→15s exponential backoff and shows an
online/offline badge. Send `ping` at any time; the server replies `{"type":"pong"}`.
A newly connected client is replayed the last 25 events so a freshly opened tab is
never blank.

Every frame is `{ "type": …, "timestamp": ISO-8601, "data": { … } }`:

| `type` | Emitted when | Key `data` fields |
|--------|--------------|-------------------|
| `inbound_message` | A customer message arrives | `phone_number`, `content`, `pipeline_stage`, `new_contact` |
| `ai_thinking` | Prompt assembly begins | `step`, `detail` |
| `ai_generation` | A provider returns | `provider`, `model`, `latency_ms`, `fallback_used`, `prompt_used`, `raw_response` |
| `outbound_message` | The reply is dispatched | `content`, `provider`, `latency_ms`, `delivered`, `twilio_sid` |
| `stage_change` | A lead advances | `from`, `to`, `source` |
| `error` | Dispatch or processing fails | `stage`, `detail` |
| `sync` | The write is committed | `contact_id`, `pipeline_stage` |

Quick check from the shell — connect, ping, and print whatever arrives:

```powershell
D:\pingpulse\.venv\Scripts\python.exe -c @'
import asyncio, websockets
async def main():
    async with websockets.connect("ws://localhost:3000/ws/monitor") as ws:
        await ws.send("ping")
        for _ in range(10):
            print(await asyncio.wait_for(ws.recv(), timeout=30))
asyncio.run(main())
'@
```

…or simply open <http://localhost:3000> and watch the badge turn green.

---

## 11. Tests

```powershell
cd D:\pingpulse\backend
D:\pingpulse\.venv\Scripts\python.exe -m pytest -q
```

35 tests, no Docker or network required — they run against in-memory SQLite with
Twilio and both LLM providers stubbed.

| File | Covers |
|------|--------|
| `tests/test_webhook.py` | Twilio PascalCase form parsing, `whatsapp:` prefix stripping, unknown/missing fields, contact auto-creation and reuse, message + `llm_logs` persistence, dispatch-failure handling, `200`-on-garbage, forward-only stage progression |
| `tests/test_llm_prompt.py` | All four prompt sections present and **in blueprint order**, organization rules / contact metadata / history merged correctly, empty-history labelling, Groq success path, rate-limit failover to Gemini, both-providers-down safe reply |
| `tests/test_websocket.py` | Connect / disconnect tracking, broadcast fan-out, dead-socket eviction without raising, history replay, and a real handshake against `/ws/monitor` |

---

## 12. Operational notes

**Model names.** The models named in the original blueprint
(`llama-3.3-70b-versatile`, `gemini-1.5-pro`) return `404 — model not found` on these
API keys; both have been decommissioned. The stack is configured with current
equivalents, `qwen/qwen3.8-27b` (~580–800 ms, matching the blueprint's sub-second
target) and `gemini-3.8-flash`. Both are plain `.env` values — change `GROQ_MODEL` or
`GEMINI_MODEL` and restart. `/health` reports immediately if a configured model is
not available on the key.

**Outbound WhatsApp requires a WhatsApp sender.** `+16602075318` is on the Twilio
account with SMS and voice capability, but it has **no WhatsApp channel**, so
outbound sends fail with `Twilio could not find a Channel with the specified From
address`. Ingestion, prompt assembly, generation, persistence and pipeline
progression all work; only the final hop is blocked. To finish it, either:

- use the Twilio WhatsApp **sandbox** — set `TWILIO_WHATSAPP_NUMBER=+14155238886`
  and have each test recipient join with the sandbox code; or
- complete **WhatsApp sender registration** for `+16602075318` in the Twilio Console.

A failed dispatch is never silent: the reply is still persisted, an `error` event is
broadcast to the dashboard, and the outbound row is stored with a null `twilio_sid`.

**Exposing the webhook — required, and the usual reason "nothing happens".**
Twilio runs in the cloud and cannot reach `localhost`. Until the sandbox's inbound
webhook points at a public URL for this machine, Twilio answers your WhatsApp messages
with its own placeholder ("You said: … Configure your WhatsApp Sandbox's Inbound URL")
and PingPulse never sees the traffic.

The stack ships a `tunnel` service (ngrok) that gives the backend a **permanent** public
HTTPS URL, so the Twilio Console is configured **once** and never touched again.

One-time setup:

1. Sign up free at <https://ngrok.com>.
2. Copy your authtoken from
   <https://dashboard.ngrok.com/get-started/your-authtoken>.
3. Claim your free reserved domain at <https://dashboard.ngrok.com/domains>
   (the free tier includes one, e.g. `pingpulse.ngrok-free.app`).
4. Put both in `.env`:

   ```ini
   NGROK_AUTHTOKEN=2abc...your-token
   NGROK_DOMAIN=https://pingpulse.ngrok-free.app
   COMPOSE_PROFILES=tunnel
   ```

5. `docker compose up -d` — the tunnel now starts with the stack.
6. In Twilio Console → *Messaging* → *Try it out* → *Send a WhatsApp message* →
   **Sandbox settings**, set **"When a message comes in"** to
   `https://pingpulse.ngrok-free.app/api/v1/whatsapp/webhook`, method **POST**, save.

That URL is reserved to your account, so it survives restarts and reboots — step 6 is
done once. Clear `COMPOSE_PROFILES` in `.env` to run the stack without exposing it.

**Inspecting live Twilio traffic.** The tunnel publishes ngrok's request inspector on
<http://localhost:4040> — every webhook Twilio sends, with full payload, response, and
a replay button. It is the fastest way to tell "Twilio never called us" apart from
"Twilio called us and we errored".

**Ad-hoc alternative.** `cloudflared` is vendored at `D:\pingpulse\tools\cloudflared.exe`
for throwaway tunnels that need no account:

```powershell
D:\pingpulse\tools\cloudflared.exe tunnel --url http://localhost:8000
```

It prints a random `https://<words>.trycloudflare.com` hostname that changes on every
restart, so the Twilio Console has to be re-pasted each time. Fine for a one-off check,
not for daily use.

**Reading after a write.** Every event except `sync` is broadcast mid-flight, before
the transaction commits, so the dashboard feels instant. A client that re-reads the API
on one of those events can miss the row it was told about. `sync` fires *after* the
commit and is the safe moment to re-read; the dashboard refreshes contacts and stats
there.

**Which business answers a new number.** All sandbox traffic arrives on one shared
Twilio number, so nothing in the payload identifies the business. `DEFAULT_ORGANIZATION_ID`
in `.env` decides which organization a first-time phone number is attached to; if it is
blank or does not resolve, the oldest organization wins. Existing contacts keep the
organization they were created with.

**Signature validation.** `TWILIO_VALIDATE_SIGNATURE` is `false` so that local and
tunnelled testing works. Turn it on before exposing the webhook publicly.

**Port 5432 was already taken** on this machine by an unrelated container, so
PostgreSQL is published on **5433**. Change `POSTGRES_HOST_PORT` if you prefer another.
