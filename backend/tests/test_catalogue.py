"""Reading a shop's WhatsApp catalogue, and knowing when there isn't one.

Learned by pointing this at a live paired session rather than by reading docs:
WhatsApp does not answer "no". Asking a personal account for its catalogue
returns nothing at all, until Baileys' own timeout fires about two minutes
later. The bridge bounds the wait; these cover what the API does with either
answer.

The other thing these hold down is the price. WhatsApp reports it as an integer
and does not say what scale it is on, so importing unseen is how an agent ends
up quoting a hundredth of the real price with complete confidence. Nothing is
written until a person has seen a preview of exactly what would be.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ChannelConfig, KnowledgeDocument
from app.services import catalogue


def _product(**overrides) -> dict:
    base = {
        "id": "p1",
        "retailerId": "AR-SPP-001",
        "name": "Aurora SoundPods Pro",
        "description": "Wireless earbuds with noise cancellation.",
        "price": 8900,
        "currency": "USD",
        "availability": "in stock",
        "url": "",
        "images": ["https://example.invalid/pods.jpg"],
    }
    base.update(overrides)
    return base


@pytest.fixture
async def qr_channel(org_a, db_session):
    """A tenant connected over WhatsApp Web, which is the only way to have one."""
    import uuid as _uuid

    channel = ChannelConfig(
        organization_id=_uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923097209908",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


@pytest.fixture
def bridge(monkeypatch):
    """Stand in for the bridge, so a test never needs a paired handset."""

    def answer(**kwargs):
        async def read(_channel):
            return catalogue.Catalogue(**kwargs)

        monkeypatch.setattr(catalogue, "read", read)

    return answer


# --------------------------------------------------------------- the price
def test_the_price_scale_is_a_choice_not_a_guess():
    """8900 is $89.00 or $8,900 depending on a convention WhatsApp does not
    state. Both are available, and neither is applied silently."""
    assert catalogue.money(8900, "USD", "cents") == "USD 89"
    assert catalogue.money(8900, "USD", "whole") == "USD 8,900"
    assert catalogue.money(89000, "USD", "thousandths") == "USD 89"


def test_a_product_keeps_its_price_on_the_same_line():
    """Same rule as a table row: separating the two invites the agent to pair
    the right price with the wrong item."""
    passage = catalogue.as_passage(_product(), "cents")

    assert passage.splitlines()[0].startswith("Aurora SoundPods Pro — USD 89")
    assert "SKU AR-SPP-001" in passage
    assert "in stock" in passage


def test_a_missing_price_does_not_produce_nonsense():
    assert catalogue.money(None, "USD") == ""
    assert "None" not in catalogue.as_passage(_product(price=None))


# ------------------------------------------------------------- the readiness
@pytest.mark.asyncio
async def test_a_shop_with_nothing_is_told_what_to_add(org_a):
    response = await org_a.get("/api/v1/knowledge/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "thin"
    assert "upload" in body["advice"].lower()
    # No paired account, so no claim is made about a catalogue either way.
    assert body["catalogue"]["checked"] is False


@pytest.mark.asyncio
async def test_a_described_shop_is_working_not_broken(org_a, db_session):
    """A good description and no files is a working agent. Reporting it as an
    error would be telling a client their product is broken when it is not."""
    from app.models import Organization
    import uuid as _uuid

    organization = await db_session.get(Organization, _uuid.UUID(org_a.organization_id))
    organization.product_rules = (
        "We sell handmade leather shoes, sizes 38 to 46, in black and tan. "
        "Delivery is next day in the city and free over 10,000. Returns within 14 days."
    )
    await db_session.flush()

    body = (await org_a.get("/api/v1/knowledge/readiness")).json()

    assert body["status"] == "described"
    assert "working from your description" in body["advice"]


@pytest.mark.asyncio
async def test_an_uploaded_file_is_the_strongest_answer(org_a):
    await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("prices.txt", b"Leather Chelsea Boot | $189 | in stock", "text/plain")},
    )

    body = (await org_a.get("/api/v1/knowledge/readiness")).json()

    assert body["status"] == "ready"
    assert body["documents"]["files"] == 1


@pytest.mark.asyncio
async def test_a_readable_catalogue_is_offered(org_a, qr_channel, bridge):
    bridge(business=True, products=[_product(), _product(id="p2", name="Halo Lamp")])

    body = (await org_a.get("/api/v1/knowledge/readiness")).json()

    assert body["status"] == "catalogue"
    assert body["catalogue"]["products"] == 2
    assert body["catalogue"]["business_account"] is True
    assert "import" in body["advice"].lower()


@pytest.mark.asyncio
async def test_a_personal_account_is_not_reported_as_a_failure(org_a, qr_channel, bridge):
    """The common case. It is an absence, not an error, and the shop should be
    pointed at uploading rather than told something went wrong."""
    bridge(business=False, products=[])

    body = (await org_a.get("/api/v1/knowledge/readiness")).json()

    assert body["catalogue"]["checked"] is True
    assert body["catalogue"]["products"] == 0
    assert body["status"] in ("thin", "described")
    assert "error" not in body["advice"].lower()


# --------------------------------------------------------------- the import
@pytest.mark.asyncio
async def test_nothing_is_written_until_a_preview_has_been_seen(org_a, qr_channel, bridge, db_session):
    bridge(business=True, products=[_product()])

    preview = await org_a.get("/api/v1/knowledge/catalogue/preview?scale=cents")

    assert preview.status_code == 200
    shown = preview.json()["products"]
    assert shown[0]["price"] == "USD 89"
    assert shown[0]["raw_price"] == 8900, "the raw number is shown so the scale can be judged"

    stored = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == qr_channel.organization_id
            )
        )
    ).scalars().all()
    assert stored == [], "a preview wrote to the knowledge base"


@pytest.mark.asyncio
async def test_importing_indexes_the_products(org_a, qr_channel, bridge):
    bridge(business=True, products=[_product(), _product(id="p2", name="Halo Lamp", price=5900)])

    response = await org_a._client.post(
        "/api/v1/knowledge/catalogue/import?scale=cents", headers=org_a.headers
    )

    assert response.status_code == 201
    assert response.json()["imported"] == 2

    hits = (await org_a.get("/api/v1/knowledge/search?q=noise cancelling earbuds")).json()
    assert any("89" in hit["content"] for hit in hits)


@pytest.mark.asyncio
async def test_reimporting_replaces_rather_than_accumulates(org_a, qr_channel, bridge, db_session):
    """A catalogue is a current statement of what a shop sells. Keeping last
    week's prices beside this week's gives the agent two answers to one
    question, and no way to choose."""
    bridge(business=True, products=[_product(price=8900)])
    await org_a._client.post("/api/v1/knowledge/catalogue/import", headers=org_a.headers)

    bridge(business=True, products=[_product(price=7900)])
    again = await org_a._client.post("/api/v1/knowledge/catalogue/import", headers=org_a.headers)

    assert again.json()["replaced"] == 1
    rows = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == qr_channel.organization_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert "79" in rows[0].content and "89" not in rows[0].content


@pytest.mark.asyncio
async def test_importing_without_a_catalogue_says_what_to_do_instead(
    org_a, qr_channel, bridge
):
    bridge(business=False, products=[])

    response = await org_a.get("/api/v1/knowledge/catalogue/preview")

    assert response.status_code == 404
    assert "upload" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_a_twilio_tenant_is_told_this_needs_whatsapp_web(org_a, db_session):
    """Twilio has no paired account to read a catalogue from."""
    import uuid as _uuid

    db_session.add(
        ChannelConfig(
            organization_id=_uuid.UUID(org_a.organization_id),
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="TWILIO",
            phone_number="+14155552671",
        )
    )
    await db_session.flush()

    response = await org_a.get("/api/v1/knowledge/catalogue/preview")

    assert response.status_code == 422
    assert "WhatsApp Web" in response.json()["detail"]
