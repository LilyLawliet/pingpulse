"""PingPulse FastAPI entry point: routers, /health, and the /ws/monitor socket."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import __version__
from app.api.auth import router as auth_router
from app.api.crm import router as crm_router
from app.api.knowledge import router as knowledge_router
from app.api.organizations import router as organizations_router
from app.api.routes import router as dashboard_router
from app.api.webhook import router as webhook_router
from app.config import settings
from app.database import SessionLocal, engine
from app.deps import resolve_token
from app.schemas import ComponentHealth, HealthResponse
from app.services import outbox, ws_manager
from app.services.llm_service import probe_gemini, probe_groq
from app.services.twilio_service import twilio_service
from app.services.ws_manager import manager

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("pingpulse")

# Twilio's client logs every request line and every response header at INFO,
# and httpx logs full request URLs — which for Gemini carries the API key in
# the query string. Both were being written to the log volume on the VM on
# every health check. Neither is worth a secret in a file the operator keeps.
for _noisy in ("twilio.http_client", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


async def run_migrations() -> None:
    """Apply `alembic upgrade head` in-process at startup.

    Alembic's runner is synchronous, so it is executed inside the engine's
    connection via run_sync rather than opening a second, sync engine.
    """
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    if not config_path.exists():
        logger.warning("alembic.ini not found at %s - skipping auto-migration", config_path)
        return

    def _upgrade(connection) -> None:
        config = Config(str(config_path))
        config.set_main_option("script_location", str(config_path.parent / "alembic"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async with engine.begin() as connection:
        await connection.run_sync(_upgrade)
    logger.info("database migrated to head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting %s v%s (%s)", settings.app_name, __version__, settings.app_env)
    if settings.auto_migrate_on_startup:
        try:
            await run_migrations()
        except Exception as exc:  # noqa: BLE001 - app still serves /health when DB is down
            logger.error("startup migration failed: %s", exc)

    # Replies that could not go out — most often because the WhatsApp Web
    # bridge was restarting — are parked in Redis and retried here. Started
    # after migrations so the drainer never touches a table that is mid-change.
    stop_drainer = asyncio.Event()
    drainer = asyncio.create_task(outbox.run_drainer(stop_drainer))

    # Dashboard sockets live in this process, but work happens in others — a
    # scheduled follow-up is sent by the Celery worker. Without this, that
    # message reached the customer and never appeared on screen.
    stop_bridge = asyncio.Event()
    bridge = asyncio.create_task(ws_manager.run_event_bridge(stop_bridge))

    yield

    stop_drainer.set()
    stop_bridge.set()
    for task in (drainer, bridge):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title="PingPulse - WhatsApp Sales Agent",
    description=(
        "Twilio WhatsApp ingestion, dynamic prompt assembly, Groq/Gemini generation "
        "with fallback, and a real-time monitoring WebSocket."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Stored attachments are served from our own domain so WhatsApp can fetch
# what a customer sent us and what we send back.
_media_root = Path(settings.media_dir)
_media_root.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(_media_root)), name="media")

# Desktop updates. The app polls /updates/latest.json and downloads the
# installer named there, so a client never runs an installer by hand twice.
#
# This is served from our own domain rather than a GitHub release because the
# repository is private: release assets on a private repository are not
# publicly downloadable, so the updater received a 404 and quietly did nothing.
# Public by design — an installer everyone is meant to run, signed with a key
# the app verifies before it will install anything.
_updates_root = Path(settings.updates_dir)
_updates_root.mkdir(parents=True, exist_ok=True)
app.mount("/updates", StaticFiles(directory=str(_updates_root)), name="updates")

# The dashboard, for operators who cannot run the desktop app — a client on a
# Mac, most immediately, since a macOS build needs a Mac to produce.
#
# This is the same build that ships inside the desktop app, byte for byte, so
# there is no second version to keep in step. It is served under /app rather
# than at the root so the bare domain still answers with nothing but a status
# line, and it is not a security boundary: an access token is what unlocks
# data, here exactly as in the desktop app, and every API call and the monitor
# socket check it the same way.
_web_root = Path(settings.web_dir)
if _web_root.is_dir() and (_web_root / "index.html").is_file():
    app.mount("/app", StaticFiles(directory=str(_web_root), html=True), name="dashboard")
    logger.info("serving the dashboard at /app from %s", _web_root)
else:
    logger.info("no web dashboard bundle at %s — /app is not served", _web_root)

app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(crm_router)
app.include_router(knowledge_router)
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {
        "service": settings.app_name,
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "monitor": "/ws/monitor",
    }


# ------------------------------- Health ----------------------------------
async def _check_database() -> ComponentHealth:
    started = time.perf_counter()
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return ComponentHealth(
            status="ok",
            detail="PostgreSQL reachable",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            status="error",
            detail=str(exc)[:300],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


async def _check_provider(probe, name: str) -> ComponentHealth:
    started = time.perf_counter()
    ok, detail = await probe()
    return ComponentHealth(
        status="ok" if ok else "degraded",
        detail=f"{name}: {detail}"[:300],
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


@app.get("/health", response_model=HealthResponse)
async def health(deep: bool = False):
    """Is this container serving? Optionally, are the providers reachable too?

    The default is deliberately cheap — a database round trip and nothing else.
    Docker probes this every fifteen seconds, and the deep version calls Groq,
    Gemini and Twilio over the internet, so leaving it on by default meant
    thousands of outbound API calls a day whose only purpose was to answer a
    liveness probe that a `SELECT 1` already answers.

    `/health?deep=1` still runs the full check, for when a client reports that
    replies have stopped and the question is which provider is at fault.

    The database is the only hard dependency: a provider outage degrades the
    service (503 is reserved for a database that is genuinely unreachable) but
    the container should not be recycled for it.
    """
    database = await _check_database()

    components = {
        "database": database,
        "websocket": ComponentHealth(
            status="ok", detail=f"{manager.connection_count} monitor client(s)"
        ),
    }

    if deep:
        components["groq"] = await _check_provider(probe_groq, "Groq")
        components["gemini"] = await _check_provider(probe_gemini, "Gemini")
        components["twilio"] = await _check_provider(twilio_service.probe, "Twilio")

    if database.status == "error":
        overall = "error"
    elif any(c.status in ("degraded", "error") for c in components.values()):
        overall = "degraded"
    else:
        overall = "ok"

    body = HealthResponse(
        status=overall,
        app=settings.app_name,
        version=__version__,
        checked_at=datetime.now(timezone.utc),
        components=components,
    )
    # 200 for ok/degraded keeps the Docker healthcheck green while a provider
    # key is misconfigured; only a dead database fails the container.
    return JSONResponse(
        status_code=503 if overall == "error" else 200,
        content=body.model_dump(mode="json"),
    )


# ----------------------------- WebSocket ---------------------------------
@app.websocket("/ws/monitor")
async def monitor_socket(websocket: WebSocket):
    """Live dashboard feed: inbound, thinking, generation, outbound, stage events.

    Authenticated with the same tokens as the REST API. This socket streams
    real customer messages as they arrive, so leaving it open — as it was —
    meant anyone who knew the URL could read live conversations.

    The token arrives as a query parameter because browsers cannot set headers
    on a websocket handshake. It is validated before the connection is
    accepted, and a bad token is closed with 1008 (policy violation) rather
    than accepted and then dropped.
    """
    raw = (websocket.query_params.get("token") or "").strip()
    if not raw:
        # Refused without touching the database — there is nothing to look up.
        await websocket.close(code=1008, reason="An access token is required")
        return

    async with SessionLocal() as db:
        try:
            await resolve_token(db, raw)
        except HTTPException:
            await websocket.close(code=1008, reason="Invalid or expired access token")
            return

    await manager.connect(websocket)
    try:
        while True:
            # Client messages are only used as a keepalive / ping channel.
            received = await websocket.receive_text()
            if received.strip().lower() in ("ping", '{"type":"ping"}'):
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("monitor socket error: %s", exc)
        await manager.disconnect(websocket)
