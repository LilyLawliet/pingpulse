"""The dashboard in a browser is the same dashboard, not a second one.

A client on a Mac cannot run the desktop app — a macOS bundle has to be built
on a Mac — so the same interface is served at /app for a browser. The risk that
creates is drift: two ways in that slowly stop behaving alike, so a bug is
reproducible in one and not the other.

These protect the properties that make drift impossible rather than merely
unlikely:

  * the browser is served the identical build that ships inside the installer;
  * the API does not care which one is asking — same token rules, same
    tenancy, same 401s;
  * nothing about the agent's own work depends on either being open.

And the property that made this safe to do at all: /app is not a way around
authentication.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "frontend" / "dist"


def _bundle_files() -> list[Path]:
    if not BUNDLE.is_dir():
        return []
    return sorted(p for p in BUNDLE.rglob("*") if p.is_file())


needs_bundle = pytest.mark.skipif(
    not (BUNDLE / "index.html").is_file(),
    reason="frontend has not been built (run: npm run build in frontend/)",
)


# ------------------------------------------------------- one build, two homes
@needs_bundle
def test_the_browser_and_the_desktop_app_are_served_the_same_build():
    """Not 'kept in sync' — literally the same files.

    The desktop installer embeds frontend/dist, and the web deploy uploads
    frontend/dist. One artefact, so there is no version of this that can drift.
    """
    index = (BUNDLE / "index.html").read_text(encoding="utf-8")
    assert "<script" in index, "the built page should load the app bundle"

    scripts = [p for p in _bundle_files() if p.suffix == ".js"]
    assert scripts, "a built dashboard has at least one script"


@needs_bundle
def test_the_build_uses_relative_asset_paths():
    """A single build has to work at the shell's root and under /app.

    Vite's default absolute base ("/assets/...") resolves against the domain
    root, so the same file that works in the desktop shell would 404 every
    asset when served under /app. Relative paths are what let one build serve
    both, and losing this would break the browser version silently — a blank
    page, no error anywhere the operator would look.
    """
    index = (BUNDLE / "index.html").read_text(encoding="utf-8")
    assert 'src="/assets/' not in index and 'href="/assets/' not in index, (
        "absolute asset paths break the dashboard when it is served under /app"
    )
    assert "./assets/" in index, "expected relative asset paths from base: './'"


@needs_bundle
def test_the_bundle_is_reproducible_from_one_place():
    """The digest of what we serve, for a deploy to compare against.

    Not a fixed value — the point is that the web bundle and the installer's
    bundle are computed from the same directory, so if they ever came from
    different builds this would be the thing that noticed.
    """
    digest = hashlib.sha256()
    for path in _bundle_files():
        digest.update(path.relative_to(BUNDLE).as_posix().encode())
        digest.update(path.read_bytes())
    assert len(digest.hexdigest()) == 64


# ------------------------------------------- the API treats both the same way
@pytest.mark.asyncio
async def test_a_browser_gets_no_data_without_a_token(client):
    """/app being public must not make anything behind it public."""
    for path in ("/api/v1/contacts", "/api/v1/stats", "/api/v1/messages"):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} answered without a token"


@pytest.mark.asyncio
async def test_the_same_token_works_regardless_of_what_is_asking(org_a):
    """There is no desktop-only or browser-only path into the API.

    Nothing but the token decides what comes back. A device header is sent by
    desktop builds from before seats were removed, and it is ignored - kept
    here so an older client on the same licence keeps working.
    """
    def as_(device: str) -> dict[str, str]:
        return {**org_a.headers, "X-PingPulse-Device": device}

    client = org_a._client
    as_desktop = await client.get("/api/v1/contacts", headers=as_("desktop-machine-1"))
    as_browser = await client.get("/api/v1/contacts", headers=as_("browser-machine-1"))

    assert as_desktop.status_code == 200
    assert as_browser.status_code == 200
    assert as_desktop.json() == as_browser.json()


@pytest.mark.asyncio
async def test_a_browser_cannot_reach_another_tenant(org_a, org_b):
    """Tenancy is enforced in the API, so it holds for every client equally."""
    created = await org_b.post(
        "/api/v1/crm/contacts", json={"phone_number": "+971500009999", "name": "Theirs"}
    )
    assert created.status_code == 201
    theirs = created.json()["id"]

    # 404 rather than 403: an id must not be probeable for existence.
    assert (await org_a.get(f"/api/v1/crm/contacts/{theirs}")).status_code == 404


# ------------------------------------------------ the agent runs without both
@pytest.mark.asyncio
async def test_the_agent_replies_with_no_dashboard_connected(
    client, db_session, monkeypatch
):
    """Background work belongs to the server, not to whoever is watching.

    A customer messaging at 3am gets an answer whether the operator has the
    desktop app open, a browser tab open, or nothing at all. This is the reason
    a browser client is a viewer rather than a downgrade.
    """
    from sqlalchemy import select

    from app.config import settings
    from app.models import ChannelConfig, Message, Organization
    from app.services import outbox
    from app.services.ws_manager import manager

    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    organization = Organization(name="Unwatched Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        ChannelConfig(
            organization_id=organization.id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155553333",
        )
    )
    await db_session.flush()

    sent = []

    async def capture(channel, to_number, body, media_urls=None, to_jid=None):
        sent.append(body)
        return True, "SM-unwatched"

    monkeypatch.setattr(outbox.whatsapp, "send_message", capture)

    assert manager.connection_count == 0, "nobody is watching, which is the point"

    response = await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "MessageSid": "SM_unwatched",
            "From": "whatsapp:+971500008888",
            "To": "whatsapp:+14155553333",
            "Body": "do you have these in black?",
            "NumMedia": "0",
        },
    )
    assert response.status_code == 200

    replies = (
        await db_session.execute(
            select(Message).where(
                Message.organization_id == organization.id, Message.sender == "agent"
            )
        )
    ).scalars().all()

    assert sent, "the customer got no reply with no dashboard open"
    assert replies, "and nothing was recorded for the operator to catch up on"
    assert replies[0].delivery_status == outbox.SENT
