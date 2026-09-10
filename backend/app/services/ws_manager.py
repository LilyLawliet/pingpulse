"""Real-time WebSocket fan-out for the /ws/monitor dashboard channel."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Event names the frontend listens for.
EVENT_INBOUND = "inbound_message"
EVENT_OUTBOUND = "outbound_message"
EVENT_THINKING = "ai_thinking"
EVENT_GENERATION = "ai_generation"
EVENT_STAGE = "stage_change"
EVENT_ERROR = "error"
# Fired once the transaction is committed, so dashboards know it is safe to
# re-read. Every other event is emitted mid-flight for liveness.
EVENT_SYNC = "sync"


class ConnectionManager:
    """Tracks live dashboard sockets and broadcasts JSON events to all of them.

    Broadcast never raises: a socket that fails mid-send is dropped so one dead
    dashboard tab can never break the webhook request that triggered the event.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = []
        self._history_limit = 100

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("monitor client connected (total=%d)", len(self._connections))
        # Replay recent events so a freshly opened tab is not blank.
        for event in list(self._history[-25:]):
            try:
                await websocket.send_text(json.dumps(event, default=str))
            except Exception:  # noqa: BLE001 - replay is best effort
                break

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("monitor client disconnected (total=%d)", len(self._connections))

    async def broadcast(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload or {},
        }
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]

        message = json.dumps(event, default=str)
        async with self._lock:
            targets = list(self._connections)

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_text(message)
            except Exception:  # noqa: BLE001 - a broken tab must not break the request
                dead.append(connection)

        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.discard(connection)
            logger.warning("dropped %d dead monitor socket(s)", len(dead))

        return event


manager = ConnectionManager()
