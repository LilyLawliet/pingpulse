"""The /ws/monitor connection manager and endpoint."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services.ws_manager import ConnectionManager


ORG = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeSocket:
    """Minimal WebSocket double recording what the manager sends it."""

    def __init__(self, fail_on_send: bool = False):
        self.accepted = False
        self.sent: list[str] = []
        self.fail_on_send = fail_on_send

    async def accept(self):
        self.accepted = True

    async def send_text(self, message: str):
        if self.fail_on_send:
            raise RuntimeError("socket is closed")
        self.sent.append(message)




async def test_connect_accepts_and_tracks_the_socket():
    manager = ConnectionManager()
    socket = FakeSocket()

    await manager.connect(socket, ORG)

    assert socket.accepted is True
    assert manager.connection_count == 1


async def test_disconnect_removes_the_socket():
    manager = ConnectionManager()
    socket = FakeSocket()

    await manager.connect(socket, ORG)
    await manager.disconnect(socket)

    assert manager.connection_count == 0


async def test_broadcast_reaches_every_connected_client():
    manager = ConnectionManager()
    first, second = FakeSocket(), FakeSocket()
    await manager.connect(first, ORG)
    await manager.connect(second, ORG)

    await manager.broadcast("inbound_message", {"content": "hello", "organization_id": ORG})

    for socket in (first, second):
        event = json.loads(socket.sent[-1])
        assert event["type"] == "inbound_message"
        assert event["data"]["content"] == "hello"
        assert "timestamp" in event


async def test_broadcast_drops_dead_sockets_without_raising():
    manager = ConnectionManager()
    healthy, dead = FakeSocket(), FakeSocket(fail_on_send=True)
    await manager.connect(healthy, ORG)
    await manager.connect(dead, ORG)

    await manager.broadcast("outbound_message", {"content": "still delivered", "organization_id": ORG})

    assert manager.connection_count == 1
    assert len(healthy.sent) == 1


async def test_new_client_receives_replayed_history():
    manager = ConnectionManager()
    await manager.broadcast("inbound_message", {"content": "earlier traffic", "organization_id": ORG})

    latecomer = FakeSocket()
    await manager.connect(latecomer, ORG)

    assert any("earlier traffic" in message for message in latecomer.sent)


async def test_broadcast_payload_is_json_serialisable():
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket, ORG)

    event = await manager.broadcast("ai_generation", {"latency_ms": 310, "provider": "groq", "organization_id": ORG})

    assert event["data"]["provider"] == "groq"
    json.loads(socket.sent[-1])


def test_ws_monitor_rejects_a_connection_with_no_token():
    """The live feed carries real customer messages, so it is not public.

    It used to accept any connection, which meant anyone who knew the URL could
    read conversations as they happened.
    """
    with TestClient(app) as raw:
        with pytest.raises(WebSocketDisconnect):
            with raw.websocket_connect("/ws/monitor") as socket:
                socket.receive_text()


def test_ws_monitor_endpoint_accepts_a_real_connection(monkeypatch):
    """End-to-end handshake through the ASGI app at /ws/monitor.

    The endpoint validates against the database, so the lookup is stubbed here
    rather than standing a schema up — the token rules themselves are covered
    by the access-token tests.
    """
    from app import main as main_module

    async def allow(_db, raw):
        if raw != "pp_live_valid":
            raise HTTPException(status_code=401, detail="nope")
        return object()

    async def watching(_db, _token, _asked):
        return ORG

    monkeypatch.setattr(main_module, "resolve_token", allow)
    monkeypatch.setattr(main_module, "_watched_business", watching)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/monitor?token=pp_live_valid") as websocket:
            websocket.send_text("ping")
            # The app-wide manager replays recent events to a new client, so
            # drain those before the pong we are actually asserting on.
            for _ in range(30):
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "pong":
                    break
            else:
                raise AssertionError("no pong received from /ws/monitor")


async def test_a_business_never_receives_another_business_s_events():
    """Every dashboard used to get every shop's customer messages."""
    manager = ConnectionManager()
    mine, theirs = FakeSocket(), FakeSocket()
    await manager.connect(mine, ORG)
    await manager.connect(theirs, OTHER)

    await manager.broadcast("inbound_message", {"content": "for ORG only", "organization_id": ORG})
    await manager.broadcast("inbound_message", {"content": "no business named"})

    assert [json.loads(m)["data"]["content"] for m in mine.sent] == ["for ORG only"]
    assert theirs.sent == []

    latecomer = FakeSocket()
    await manager.connect(latecomer, OTHER)
    assert latecomer.sent == [], "another business's history was replayed"


async def test_replayed_events_say_so():
    manager = ConnectionManager()
    await manager.broadcast("inbound_message", {"content": "earlier", "organization_id": ORG})
    latecomer = FakeSocket()
    await manager.connect(latecomer, ORG)
    assert json.loads(latecomer.sent[0])["replay"] is True


async def test_a_socket_may_only_watch_a_business_its_account_belongs_to():
    import uuid
    from types import SimpleNamespace

    from app import main as main_module

    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, active_organization_id=uuid.UUID(ORG))

    class DB:
        async def get(self, _model, _id):
            return user

        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: [uuid.UUID(ORG)])

    token = SimpleNamespace(user_id=user.id)
    watch = main_module._watched_business
    assert await watch(DB(), token, ORG) == ORG
    assert await watch(DB(), token, OTHER) == ORG, "a business the account is not in was watched"
    assert await watch(DB(), token, "not-a-uuid") == ORG
    assert await watch(DB(), SimpleNamespace(user_id=None), ORG) is None
