"""Real-time WebSocket fan-out for the /ws/monitor dashboard channel.

Dashboard sockets are held in memory by whichever process accepted them, so a
broadcast only ever reached the process that made it. That was fine while every
event originated in the API — a webhook arrives, the reply is written, the same
process tells the tabs it is holding. It stopped being fine the moment work
moved to the Celery worker: a scheduled follow-up genuinely sent the message and
genuinely stored it, and the dashboard was never told, because the worker is a
different process holding no sockets at all. The message only appeared if
something else happened to force a re-read.

So a broadcast is now two things: the local fan-out it always was, and a publish
onto a Redis channel every process subscribes to. Each process stamps its own
`origin` on what it publishes and ignores its own messages coming back, which is
what keeps a tab from showing the same reply twice.

Publishing is best effort by design. If Redis is unavailable the local fan-out
has already happened, so the API keeps behaving exactly as it did before this
existed — one process, one set of tabs — and only cross-process events are lost.
An event is never worth failing a customer's reply for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import WebSocket

from app.config import settings

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

# The Redis channel every PingPulse process publishes to and subscribes to.
EVENTS_CHANNEL = "pingpulse:events"

# Identifies this process for its lifetime. An event carrying this origin came
# from here and has already been delivered locally; delivering it again when it
# arrives back over Redis would duplicate every message on screen.
ORIGIN = uuid.uuid4().hex


class ConnectionManager:
    """Tracks live dashboard sockets and sends each one its own business's events.

    Every socket belongs to one business, checked against the account's
    memberships when it connects, and an event goes only to the sockets of the
    business it names. It used to go to every socket on the server: a dashboard
    open on one shop received another shop's customer messages, and the inbox
    jumped to a conversation that was not even its own. An event that names no
    business goes to nobody.

    Broadcast never raises: a socket that fails mid-send is dropped so one dead
    dashboard tab can never break the webhook request that triggered the event.
    """

    def __init__(self) -> None:
        # Socket -> the business it is watching.
        self._connections: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = []
        self._history_limit = 100

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket, organization_id: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[websocket] = str(organization_id)
        logger.info("monitor client connected (total=%d)", len(self._connections))
        # Replay this business's recent events so a freshly opened tab is not
        # blank - marked as replays, so the tab shows them without acting on
        # them as if they had just happened.
        mine = [e for e in self._history if _business_of(e) == str(organization_id)][-25:]
        for event in mine:
            try:
                await websocket.send_text(json.dumps({**event, "replay": True}, default=str))
            except Exception:  # noqa: BLE001 - replay is best effort
                break

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.pop(websocket, None)
        logger.info("monitor client disconnected (total=%d)", len(self._connections))

    async def deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        """Send one already-formed event to the sockets this process holds.

        The half of a broadcast that does not involve Redis, so an event raised
        in another process takes exactly the same path out to the tabs as one
        raised here — including the replay history a new tab is given.
        """
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]

        message = json.dumps(event, default=str)
        business = _business_of(event)
        async with self._lock:
            targets = [ws for ws, org in self._connections.items() if business and org == business]

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_text(message)
            except Exception:  # noqa: BLE001 - a broken tab must not break the request
                dead.append(connection)

        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.pop(connection, None)
            logger.warning("dropped %d dead monitor socket(s)", len(dead))

        return event

    async def broadcast(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Raise an event: to this process's tabs, and to every other process."""
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": ORIGIN,
            "data": payload or {},
        }
        await self.deliver(event)
        await _publish(event)
        return event


def _business_of(event: dict[str, Any]) -> str | None:
    data = event.get("data") or {}
    found = data.get("organization_id") if isinstance(data, dict) else None
    return str(found) if found else None


manager = ConnectionManager()


async def _publish(event: dict[str, Any]) -> None:
    """Hand an event to the other processes. Never raises.

    A short-lived client rather than a shared one, for the same reason the
    outbox uses one: this is called both from the API's event loop and from
    Celery tasks that run `asyncio.run` per job, and a client bound to a loop
    that has since closed fails on its next use. Connecting on the container
    network costs well under a millisecond.
    """
    client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        await client.publish(EVENTS_CHANNEL, json.dumps(event, default=str))
    except Exception as exc:  # noqa: BLE001 - an event is not worth a failed reply
        logger.warning("could not publish %s to other processes: %s", event.get("type"), exc)
    finally:
        closer = getattr(client, "aclose", None) or client.close
        try:
            await closer()
        except Exception:  # noqa: BLE001 - closing must not mask the real error
            pass


async def run_event_bridge(stop: asyncio.Event) -> None:
    """Deliver events raised in other processes to this one's dashboards.

    Runs for the life of the API process. Redis being unreachable is a
    reconnect loop rather than a crash: the API still serves, and events raised
    locally still reach local tabs the whole time it is down.
    """
    while not stop.is_set():
        client = aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2
        )
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(EVENTS_CHANNEL)
            logger.info("listening for events from other processes (origin %s)", ORIGIN[:8])

            while not stop.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=5.0
                )
                if message is None:
                    continue  # idle tick, so `stop` is still checked promptly
                try:
                    event = json.loads(message["data"])
                except (ValueError, TypeError, KeyError):
                    continue
                if event.get("origin") == ORIGIN:
                    continue  # our own, already delivered on the way out
                await manager.deliver(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect rather than give up
            logger.warning("event bridge dropped, reconnecting: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        finally:
            closer = getattr(client, "aclose", None) or client.close
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass
