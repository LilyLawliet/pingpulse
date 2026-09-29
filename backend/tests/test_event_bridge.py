"""Events have to cross process boundaries, or work done elsewhere is invisible.

Dashboard sockets are held in memory by the process that accepted them. While
every event was raised by the API that was invisible: a webhook arrived and the
same process told the tabs it was holding. It stopped being true when work moved
to the Celery worker — a scheduled follow-up was genuinely sent and genuinely
stored, and the operator's screen showed nothing at all, because the worker
holds no sockets. The message was on the customer's phone and nowhere else.

So these cover the properties that make one process's event reach another's
tabs, and the ones that stop the cure being worse than the disease:

  * an event raised anywhere reaches the sockets held everywhere;
  * a process ignores its own events coming back, so nothing appears twice;
  * Redis being down costs cross-process events and nothing else — the API
    keeps serving and local tabs keep updating.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services import ws_manager
from app.services.ws_manager import ConnectionManager


class FakeSocket:
    """Minimal WebSocket double recording what the manager sends it."""

    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        self.sent.append(message)

    def events(self) -> list[dict]:
        return [json.loads(raw) for raw in self.sent]


ORG = "11111111-1111-1111-1111-111111111111"


def _event(event_type: str, origin: str, **data) -> dict:
    data.setdefault("organization_id", ORG)
    return {
        "type": event_type,
        "timestamp": "2026-09-11T00:00:00+00:00",
        "origin": origin,
        "data": data,
    }


# ------------------------------------------------------- raising an event
async def test_a_broadcast_is_published_for_the_other_processes(events_stay_local):
    """The half that was missing. Local fan-out alone is a single-process app."""
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket, ORG)

    await manager.broadcast("outbound_message", {"content": "hello", "organization_id": ORG})

    assert len(socket.events()) == 1, "the local tab still gets it directly"
    assert len(events_stay_local) == 1, "and the other processes are told"
    assert events_stay_local[0]["origin"] == ws_manager.ORIGIN
    assert events_stay_local[0]["data"]["content"] == "hello"


async def test_every_event_carries_the_process_that_raised_it(events_stay_local):
    """Without an origin there is no way to tell a loop-back from a real event."""
    manager = ConnectionManager()

    event = await manager.broadcast("sync", {"contact_id": "abc", "organization_id": ORG})

    assert event["origin"] == ws_manager.ORIGIN
    assert len(ws_manager.ORIGIN) == 32


# --------------------------------------------------- receiving one from afar
async def test_an_event_from_another_process_reaches_this_ones_sockets():
    """The whole point: the worker sends, this process's operator sees it."""
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket, ORG)

    await manager.deliver(
        _event("outbound_message", "some-other-process", content="Still interested?")
    )

    delivered = socket.events()
    assert len(delivered) == 1
    assert delivered[0]["data"]["content"] == "Still interested?"


async def test_a_remote_event_is_replayed_to_a_tab_that_opens_afterwards():
    """A follow-up that fired while the dashboard was closed is still news.

    Remote events go through the same history as local ones, so a tab opening
    a moment later is not blank about work that happened without it.
    """
    manager = ConnectionManager()
    await manager.deliver(_event("outbound_message", "worker", content="nudge"))

    latecomer = FakeSocket()
    await manager.connect(latecomer, ORG)

    assert [e["data"]["content"] for e in latecomer.events()] == ["nudge"]


# ------------------------------------------------------------- no duplicates
class _FakePubSub:
    def __init__(self, messages: list[dict], stop: asyncio.Event) -> None:
        self._messages = list(messages)
        self._stop = stop
        self.subscribed_to: str | None = None

    async def subscribe(self, channel: str) -> None:
        self.subscribed_to = channel

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        if self._messages:
            return {"data": json.dumps(self._messages.pop(0))}
        # Nothing left to feed it; end the loop rather than idle forever.
        self._stop.set()
        return None


class _FakeRedis:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


async def test_the_bridge_ignores_the_events_this_process_raised(monkeypatch):
    """A broadcast is delivered locally on the way out and comes back over Redis.

    Delivering it again would double every message on screen — the operator
    would see the agent say the same thing twice, which reads as a bug in the
    agent rather than in the plumbing.
    """
    stop = asyncio.Event()
    pubsub = _FakePubSub(
        [
            _event("outbound_message", ws_manager.ORIGIN, content="mine, already shown"),
            _event("outbound_message", "the-celery-worker", content="theirs, new"),
        ],
        stop,
    )
    fake = _FakeRedis(pubsub)
    monkeypatch.setattr(ws_manager.aioredis, "from_url", lambda *a, **k: fake)

    delivered: list[dict] = []

    async def record(event):
        delivered.append(event)

    monkeypatch.setattr(ws_manager.manager, "deliver", record)

    await asyncio.wait_for(ws_manager.run_event_bridge(stop), timeout=5)

    assert pubsub.subscribed_to == ws_manager.EVENTS_CHANNEL
    assert [e["data"]["content"] for e in delivered] == ["theirs, new"], (
        "our own event came back and would have been shown twice"
    )
    assert fake.closed, "the subscriber leaked its connection"


async def test_the_bridge_survives_rubbish_on_the_channel(monkeypatch):
    """Anything can publish to Redis. One bad payload must not end the loop."""
    stop = asyncio.Event()

    class Rubbish(_FakePubSub):
        async def get_message(self, ignore_subscribe_messages=True, timeout=None):
            if self._messages:
                item = self._messages.pop(0)
                return {"data": item if isinstance(item, str) else json.dumps(item)}
            self._stop.set()
            return None

    pubsub = Rubbish(
        ["not json at all", _event("sync", "elsewhere", contact_id="abc")], stop
    )
    monkeypatch.setattr(ws_manager.aioredis, "from_url", lambda *a, **k: _FakeRedis(pubsub))

    delivered: list[dict] = []

    async def record(event):
        delivered.append(event)

    monkeypatch.setattr(ws_manager.manager, "deliver", record)

    await asyncio.wait_for(ws_manager.run_event_bridge(stop), timeout=5)

    assert [e["type"] for e in delivered] == ["sync"]


# ------------------------------------------------------------- Redis is down
@pytest.mark.real_publish
async def test_redis_being_unreachable_does_not_break_a_broadcast(monkeypatch):
    """Degrade to what this was before the bridge existed, never to an error.

    An event is not worth failing a customer's reply for, so a publish that
    cannot connect is logged and swallowed — and the local tabs, which are the
    common case, are updated first and so are unaffected either way.
    """
    from app.config import settings

    # A port nothing is listening on, so the connection is refused rather than
    # hanging for the connect timeout.
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")

    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket, ORG)

    event = await asyncio.wait_for(
        manager.broadcast("outbound_message", {"content": "still shown", "organization_id": ORG}), timeout=10
    )

    assert event["type"] == "outbound_message"
    assert [e["data"]["content"] for e in socket.events()] == ["still shown"]
