"""The /ws/monitor connection manager and endpoint."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services.ws_manager import ConnectionManager


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

    await manager.connect(socket)

    assert socket.accepted is True
    assert manager.connection_count == 1


async def test_disconnect_removes_the_socket():
    manager = ConnectionManager()
    socket = FakeSocket()

    await manager.connect(socket)
    await manager.disconnect(socket)

    assert manager.connection_count == 0


async def test_broadcast_reaches_every_connected_client():
    manager = ConnectionManager()
    first, second = FakeSocket(), FakeSocket()
    await manager.connect(first)
    await manager.connect(second)

    await manager.broadcast("inbound_message", {"content": "hello"})

    for socket in (first, second):
        event = json.loads(socket.sent[-1])
        assert event["type"] == "inbound_message"
        assert event["data"]["content"] == "hello"
        assert "timestamp" in event


async def test_broadcast_drops_dead_sockets_without_raising():
    manager = ConnectionManager()
    healthy, dead = FakeSocket(), FakeSocket(fail_on_send=True)
    await manager.connect(healthy)
    await manager.connect(dead)

    await manager.broadcast("outbound_message", {"content": "still delivered"})

    assert manager.connection_count == 1
    assert len(healthy.sent) == 1


async def test_new_client_receives_replayed_history():
    manager = ConnectionManager()
    await manager.broadcast("inbound_message", {"content": "earlier traffic"})

    latecomer = FakeSocket()
    await manager.connect(latecomer)

    assert any("earlier traffic" in message for message in latecomer.sent)


async def test_broadcast_payload_is_json_serialisable():
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket)

    event = await manager.broadcast("ai_generation", {"latency_ms": 310, "provider": "groq"})

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

    monkeypatch.setattr(main_module, "resolve_token", allow)

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
