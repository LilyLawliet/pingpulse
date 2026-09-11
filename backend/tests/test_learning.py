"""Learning from a shop's own words without learning from our own.

Two things are taken out of a shop's past replies — the facts it stated and the
way it writes — and each has one way of going badly wrong.

The facts can be laundered. If the agent's own answers are treated as source
material, a guess it made in March is extracted as a fact in September and
retrieved thereafter as though a person had confirmed it. Nothing downstream
can tell the difference, so it has to be prevented here.

The voice can drift. WhatsApp history says a message came from this number, not
who typed it; once the agent is answering, its output sits in the same history
as the owner's writing. A voice learned from that is the model imitating
itself, a little further from the shop with every round.

Both come down to the same question — did a person at this shop write this? —
and the tests below are almost entirely about the places where the answer is no.

The rest hold down two promises that matter to a client already in production:
a shop that never approves a voice keeps exactly the agent it has today, and
nothing reaches the knowledge base that a person did not look at first.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    SENDER_AGENT,
    SENDER_CUSTOMER,
    SENDER_OPERATOR,
    ChannelConfig,
    CRMContact,
    KnowledgeDocument,
    Message,
    Organization,
)
from app.services import learning


def _at(days_ago: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


def _when(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _chat(messages, jid="923001234567@s.whatsapp.net"):
    return {"jid": jid, "pushName": "Sara", "messages": messages}


@pytest.fixture
async def qr_channel(org_a, db_session):
    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
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
def whatsapp_history(monkeypatch):
    def load(chats):
        async def read(_channel):
            return chats

        from app.services import prospects

        monkeypatch.setattr(prospects, "read_history", read)

    return load


@pytest.fixture
def model_says(monkeypatch):
    """Pin the model's answer so the assertions are about our code, not Groq's."""

    def reply(text):
        async def call(_prompt):
            return text

        from app.services import llm_service

        monkeypatch.setattr(llm_service, "_call_groq", call)

    return reply


async def _contact(db_session, org_a, phone="+923001234567"):
    contact = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number=phone,
        pipeline_stage="LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


# ==================================================== what counts as human
@pytest.mark.asyncio
async def test_everything_the_phone_sent_before_the_agent_ran_is_the_shops_own(
    org_a, qr_channel, whatsapp_history, db_session
):
    """No agent has ever replied here, so the whole history is a person's."""
    whatsapp_history([
        _chat([
            {"fromMe": False, "text": "do you deliver to Karachi?", "at": _at(20)},
            {"fromMe": True, "text": "Yes, we deliver all over Pakistan.", "at": _at(20)},
        ])
    ])

    material = await learning.gather(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    assert material.cutoff is None
    assert [r.text for r in material.replies] == ["Yes, we deliver all over Pakistan."]
    assert material.skipped_after_cutoff == 0


@pytest.mark.asyncio
async def test_the_agents_own_replies_are_never_learned_from(
    org_a, qr_channel, whatsapp_history, db_session
):
    """The failure this whole feature is arranged around.

    The agent has been answering on this number since day 10. Everything the
    phone sent after that may be its output, and nothing in the history says
    which. Treating it as the shop's writing would be the model imitating
    itself; treating it as fact would be its guesses coming back as knowledge.
    """
    contact = await _contact(db_session, org_a)
    db_session.add(
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_AGENT,
            content="Certainly! Let me help you with that.",
            created_at=_when(10),
        )
    )
    await db_session.flush()

    whatsapp_history([
        _chat([
            {"fromMe": False, "text": "salam, delivery?", "at": _at(30)},
            {"fromMe": True, "text": "Walaikum salam, yes we deliver.", "at": _at(30)},
            {"fromMe": False, "text": "and returns?", "at": _at(5)},
            {"fromMe": True, "text": "Certainly! Our returns window is generous.", "at": _at(5)},
        ])
    ])

    material = await learning.gather(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    learned = [r.text for r in material.replies]
    assert learned == ["Walaikum salam, yes we deliver."]
    assert material.skipped_after_cutoff == 1
    assert not any("Certainly!" in text for text in learned), (
        "a reply the agent may have written was learned from"
    )


@pytest.mark.asyncio
async def test_the_cutoff_is_the_earliest_moment_the_agent_could_have_written(
    org_a, qr_channel, db_session
):
    """Two signals, and the safer one wins.

    Erring early loses some material. Erring late loses the voice itself, and
    quietly, because nothing about the result looks wrong.
    """
    qr_channel.session_connected_at = _when(12)
    contact = await _contact(db_session, org_a)
    db_session.add(
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_AGENT,
            content="hello",
            created_at=_when(40),
        )
    )
    await db_session.flush()

    cutoff = await learning.human_cutoff(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    assert cutoff is not None
    assert abs((cutoff - _when(40)).total_seconds()) < 5, "the later signal was used"


@pytest.mark.asyncio
async def test_a_dashboard_reply_is_human_whenever_it_was_written(
    org_a, qr_channel, whatsapp_history, db_session
):
    """Operator rows need no cutoff — they were labelled as a person's at the
    moment they were typed, which is why that label was built first."""
    contact = await _contact(db_session, org_a)
    db_session.add_all([
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_AGENT,
            content="I am the agent.",
            created_at=_when(30),
        ),
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_OPERATOR,
            content="Bhai stock aa gaya hai, kal bhej dete hain.",
            created_at=_when(2),
        ),
    ])
    await db_session.flush()
    whatsapp_history([])

    material = await learning.gather(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    assert [r.text for r in material.replies] == ["Bhai stock aa gaya hai, kal bhej dete hain."]
    assert material.from_dashboard == 1
    assert not any("I am the agent" in r.text for r in material.replies)


# ========================================================= privacy of others
def test_another_customers_details_do_not_survive_into_a_reply():
    """These were written to one person and get read back in front of another."""
    cleaned = learning.scrub(
        "Sure, send it to ali@example.com or call +92 300 1234567, order 88401923"
    )

    assert "ali@example.com" not in cleaned
    assert "1234567" not in cleaned
    assert "88401923" not in cleaned
    assert "Sure, send it to" in cleaned


def test_an_example_never_carries_a_figure():
    """An example is shown to customers it was not written for, so a price
    inside one becomes a number quoted for the wrong product."""
    replies = [
        learning.ShopReply("Yes ma'am, available in black and brown both.", _when(3), "phone"),
        learning.ShopReply("The black boots are 4500 with delivery included.", _when(4), "phone"),
    ]

    examples = learning.pick_examples(replies)

    assert examples == ["Yes ma'am, available in black and brown both."]


# ================================================================ the voice
@pytest.mark.asyncio
async def test_a_voice_is_not_invented_from_four_messages(
    org_a, qr_channel, whatsapp_history
):
    """It would be applied to every customer this shop has."""
    whatsapp_history([
        _chat([{"fromMe": True, "text": "Yes we have it in stock right now.", "at": _at(3)}])
    ])

    response = await org_a.post("/api/v1/learning/voice/preview")

    assert response.status_code == 422
    assert "5 are needed" in response.json()["detail"].replace("  ", " ")


@pytest.mark.asyncio
async def test_drafting_a_voice_does_not_apply_it(
    org_a, qr_channel, whatsapp_history, model_says, db_session
):
    """A shop sees the description of its own voice before it takes effect —
    it is the sort of thing they will want to correct."""
    whatsapp_history([
        _chat([
            {"fromMe": True, "text": f"Yes ji, that one is available in store.", "at": _at(i)}
            for i in range(3, 11)
        ])
    ])
    model_says('{"traits": ["Writes short, direct sentences.", "Opens with Yes ji."]}')

    draft = (await org_a.post("/api/v1/learning/voice/preview")).json()

    assert "Writes short, direct sentences." in draft["style"]
    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert organization.voice_style is None, "a draft was applied without approval"


@pytest.mark.asyncio
async def test_an_approved_voice_reaches_every_reply(org_a, db_session):
    from app.services.llm_service import build_prompt

    response = await org_a._client.put(
        "/api/v1/learning/voice",
        headers=org_a.headers,
        json={
            "style": "- Writes short, direct sentences.\n- Mixes English and Roman Urdu.",
            "examples": ["Yes ji, available hai, aaj hi bhej dete hain."],
        },
    )
    assert response.status_code == 200

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    prompt = build_prompt(organization, None, [], "kitna hai?")

    assert "HOW THIS SHOP WRITES" in prompt
    assert "Mixes English and Roman Urdu" in prompt
    assert "Yes ji, available hai" in prompt
    assert "FORM ONLY" in prompt, "the fence keeping style from carrying facts is missing"


@pytest.mark.asyncio
async def test_an_example_with_a_price_is_refused_with_the_reason(org_a):
    response = await org_a._client.put(
        "/api/v1/learning/voice",
        headers=org_a.headers,
        json={"style": "- Short sentences.", "examples": ["The boots are 4500 rupees."]},
    )

    assert response.status_code == 422
    assert "price" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_shop_that_never_approves_a_voice_keeps_the_agent_it_has(
    org_a, db_session
):
    """The promise made to every client already in production: this upgrade
    reaches them without changing a single reply until they ask it to."""
    from app.services.llm_service import build_prompt

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    prompt = build_prompt(organization, None, [], "hello")

    assert "HOW THIS SHOP WRITES" not in prompt


@pytest.mark.asyncio
async def test_clearing_the_voice_restores_the_default(org_a, db_session):
    from app.services.llm_service import build_prompt

    await org_a._client.put(
        "/api/v1/learning/voice",
        headers=org_a.headers,
        json={"style": "- All lowercase, no punctuation.", "examples": []},
    )
    response = await org_a.delete("/api/v1/learning/voice")
    assert response.status_code == 204

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    await db_session.refresh(organization)
    assert "HOW THIS SHOP WRITES" not in build_prompt(organization, None, [], "hi")


# ================================================================ the facts
@pytest.mark.asyncio
async def test_facts_are_taken_only_from_answers_a_person_gave(
    org_a, qr_channel, whatsapp_history, db_session
):
    """The agent's answers are not evidence. Extracting from one would turn a
    guess it made into a fact the knowledge base states with confidence."""
    contact = await _contact(db_session, org_a)
    db_session.add_all([
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_CUSTOMER,
            content="do you ship internationally?",
            created_at=_when(3),
        ),
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender=SENDER_AGENT,
            content="Absolutely, we ship worldwide!",
            created_at=_when(3),
        ),
    ])
    await db_session.flush()
    whatsapp_history([])

    material = await learning.gather(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    assert material.exchanges == [], "the agent's own answer became source material"


@pytest.mark.asyncio
async def test_a_question_and_the_shops_answer_are_paired(
    org_a, qr_channel, whatsapp_history, db_session
):
    whatsapp_history([
        _chat([
            {"fromMe": False, "text": "what are your timings", "at": _at(6)},
            {"fromMe": False, "text": "on sunday", "at": _at(6)},
            {"fromMe": True, "text": "We are open every day 11am to 9pm.", "at": _at(6)},
            {"fromMe": True, "text": "Sunday included.", "at": _at(6)},
        ])
    ])

    material = await learning.gather(
        db_session, uuid.UUID(org_a.organization_id), qr_channel
    )

    assert len(material.exchanges) == 1
    exchange = material.exchanges[0]
    assert exchange.asked == "what are your timings on sunday"
    assert exchange.answered == "We are open every day 11am to 9pm. Sunday included."


@pytest.mark.asyncio
async def test_previewing_facts_writes_nothing(
    org_a, qr_channel, whatsapp_history, model_says, db_session
):
    from sqlalchemy import select

    whatsapp_history([
        _chat([
            {"fromMe": False, "text": "delivery charges?", "at": _at(4)},
            {"fromMe": True, "text": "Delivery is free above two thousand.", "at": _at(4)},
        ])
    ])
    model_says('{"facts": [{"topic": "Delivery", "fact": "Delivery is free on orders over 2000."}]}')

    body = (await org_a.post("/api/v1/learning/facts/preview")).json()

    assert body["facts"][0]["topic"] == "Delivery"
    stored = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == uuid.UUID(org_a.organization_id)
            )
        )
    ).scalars().all()
    assert stored == [], "a preview wrote to the knowledge base"


@pytest.mark.asyncio
async def test_imported_facts_become_something_the_agent_can_retrieve(
    org_a, db_session, offline_embeddings
):
    from app.services import retrieval

    response = await org_a.post(
        "/api/v1/learning/facts",
        json={"facts": [{"topic": "Delivery", "fact": "Delivery inside Lahore is next day."}]},
    )
    assert response.status_code == 201

    found = await retrieval.search(
        db_session, uuid.UUID(org_a.organization_id), "how long does delivery take"
    )
    assert any("next day" in chunk.content for chunk in found)


@pytest.mark.asyncio
async def test_reimporting_replaces_rather_than_accumulates(
    org_a, db_session, offline_embeddings
):
    """A shop that re-runs this after changing a policy means the new answer.
    Leaving the old one beside it gives the agent two answers and no way to
    choose between them."""
    from sqlalchemy import select

    await org_a.post(
        "/api/v1/learning/facts",
        json={"facts": [{"topic": "Delivery", "fact": "Delivery is free above 2000."}]},
    )
    response = await org_a.post(
        "/api/v1/learning/facts",
        json={"facts": [{"topic": "Delivery", "fact": "Delivery is free above 3000."}]},
    )

    assert response.json()["replaced"] == 1
    rows = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == uuid.UUID(org_a.organization_id),
                KnowledgeDocument.source == learning.LEARNED_SOURCE,
            )
        )
    ).scalars().all()
    assert [row.content for row in rows] == ["Delivery is free above 3000."]


@pytest.mark.asyncio
async def test_an_import_leaves_an_uploaded_price_list_alone(
    org_a, db_session, offline_embeddings
):
    """Only what this feature wrote is replaced. A shop's uploaded catalogue is
    not ours to clear."""
    from sqlalchemy import select

    from app.services import retrieval

    await retrieval.index_document(
        db_session,
        organization_id=uuid.UUID(org_a.organization_id),
        title="Price list",
        content="Black boots 4500.",
        source="price-list.pdf",
    )
    await db_session.flush()

    await org_a.post(
        "/api/v1/learning/facts",
        json={"facts": [{"topic": "Hours", "fact": "Open 11am to 9pm daily."}]},
    )

    rows = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == uuid.UUID(org_a.organization_id)
            )
        )
    ).scalars().all()
    assert {row.source for row in rows} == {"price-list.pdf", learning.LEARNED_SOURCE}


@pytest.mark.asyncio
async def test_nothing_is_written_without_a_person_choosing_it(org_a):
    response = await org_a.post("/api/v1/learning/facts", json={"facts": []})

    assert response.status_code == 422


# ================================================================== tenancy
@pytest.mark.asyncio
async def test_one_shops_voice_never_reaches_another(org_a, org_b, db_session):
    from app.services.llm_service import build_prompt

    await org_a._client.put(
        "/api/v1/learning/voice",
        headers=org_a.headers,
        json={"style": "- Writes in all lowercase.", "examples": []},
    )

    other = await db_session.get(Organization, uuid.UUID(org_b.organization_id))
    assert "all lowercase" not in build_prompt(other, None, [], "hello")


@pytest.mark.asyncio
async def test_learned_facts_are_not_retrievable_by_another_tenant(
    org_a, org_b, db_session, offline_embeddings
):
    from app.services import retrieval

    await org_a.post(
        "/api/v1/learning/facts",
        json={"facts": [{"topic": "Delivery", "fact": "Delivery inside Lahore is next day."}]},
    )

    found = await retrieval.search(
        db_session, uuid.UUID(org_b.organization_id), "delivery in Lahore"
    )
    assert found == []
