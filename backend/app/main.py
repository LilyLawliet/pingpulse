"""PingPulse FastAPI entry point: routers, /health, and the /ws/monitor socket."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
from app.schemas import ComponentHealth, HealthResponse
from app.services.llm_service import probe_gemini, probe_groq
from app.services.twilio_service import twilio_service
from app.services.ws_manager import manager

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("pingpulse")


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
    yield
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
async def health():
    """Live check of PostgreSQL, Groq, Gemini and the Twilio client.

    The database is the only hard dependency: a provider outage degrades the
    service (503 is reserved for a DB that is genuinely unreachable) but the
    container should not be recycled for it.
    """
    database = await _check_database()
    groq = await _check_provider(probe_groq, "Groq")
    gemini = await _check_provider(probe_gemini, "Gemini")
    twilio = await _check_provider(twilio_service.probe, "Twilio")

    components = {
        "database": database,
        "groq": groq,
        "gemini": gemini,
        "twilio": twilio,
        "websocket": ComponentHealth(
            status="ok", detail=f"{manager.connection_count} monitor client(s)"
        ),
    }

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
    """Live dashboard feed: inbound, thinking, generation, outbound, stage events."""
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
