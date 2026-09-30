"""The agent reading a business's document well enough to answer on day one.

Nobody teaches it. A document is uploaded; the agent writes down how customers
would ask for each passage, and reads every message for what it most likely
means. A garbled "yrly price??" then finds the passage about annual billing,
which shares not one word with it - and the passage, in the document's own
words, is still the only thing that reaches the reply.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import KnowledgeDocument, Organization
from app.services import analyzer, llm_service, retrieval, study, understanding

from .conftest import _session_for

ANNUAL = "Annual plans are billed at ten months, so two months are free."
DELIVERY = "Hardware ships in 3 to 5 working days to Karachi and 1 to 2 to Lahore."


@pytest.fixture(autouse=True)
def no_live_embeddings(monkeypatch):
    """Retrieval judged on words alone, not on whoever has API keys today.

    These tests are about what studying a passage adds to the search. With an
    embedding provider configured - as a working .env has - every search also
    made a live call to Gemini, and "yrly price??" already matched the annual
    billing passage at 0.67 on meaning alone. The test then failed on the very
    machine the software runs on, and passed only where no key was set.
    """
    async def flat(*args, **kwargs):
        return [], "none"

    monkeypatch.setattr("app.services.retrieval.embed", flat)


@pytest.fixture
def studies(monkeypatch):
    """The model's study of a document, as it would come back."""
    seen: list[str] = []

    async def structured(prompt, timeout):
        if "PASSAGES:" not in prompt:
            return None  # the price-list reading, not what is tested here
        seen.append(prompt)
        rows = []
        for n, passage in enumerate(prompt.split("PASSAGES:")[1].split("\n\n[")[0:], start=1):
            if "ten months" in passage:
                rows.append({"n": n, "questions": ["yrly price??", "saal ka kitna", "yrly price??", 7]})
            elif "working days" in passage:
                rows.append({"n": n, "questions": ["how long delivery khi", "kab tak aayega"]})
        return {"passages": rows}

    monkeypatch.setattr(understanding, "structured", structured)
    return seen


async def test_a_studied_passage_is_found_by_how_customers_ask(org_a, studies):
    session = _session_for(org_a._client)
    organization_id = uuid.UUID(org_a.organization_id)

    # Before: the customer's words and the document's share nothing.
    plain = await retrieval.index_document(session, organization_id, "Billing", ANNUAL)
    await session.commit()
    assert await retrieval.search(session, organization_id, "yrly price??") == []

    await session.delete(plain)
    await session.commit()

    asked = await study.questions_for([ANNUAL, DELIVERY])
    # Cleaned: no repeats, nothing that isn't text.
    assert asked == [["yrly price??", "saal ka kitna"], ["how long delivery khi", "kab tak aayega"]]
    for title, passage, questions in (("Billing", ANNUAL, asked[0]), ("Delivery", DELIVERY, asked[1])):
        await retrieval.index_document(session, organization_id, title, passage, asked_as=questions)
    await session.commit()

    found = await retrieval.search(session, organization_id, "yrly price??")
    assert found and found[0].title == "Billing"
    # What reaches the reply is the document's own words, never the questions.
    assert found[0].content == ANNUAL
    assert "yrly" not in retrieval.as_prompt_block(found)

    found = await retrieval.search(session, organization_id, "kab tak aayega")
    assert found and found[0].title == "Delivery"


async def test_no_ai_means_the_document_is_searched_as_before(monkeypatch):
    async def down(prompt, timeout):
        return None

    monkeypatch.setattr(understanding, "structured", down)
    assert await study.questions_for([ANNUAL, DELIVERY]) == [[], []]

    async def broken(prompt, timeout):
        return {"passages": "not a list"}

    monkeypatch.setattr(understanding, "structured", broken)
    assert await study.questions_for([ANNUAL]) == [[]]


async def test_an_upload_is_studied_and_says_so(org_a, studies):
    response = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("policies.txt", f"{ANNUAL}\n\n{DELIVERY}".encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["passages_studied"] >= 1

    hits = (await org_a.get("/api/v1/knowledge/search?q=saal ka kitna")).json()
    assert hits and "ten months" in hits[0]["content"]


async def test_documents_uploaded_before_are_studied_once(org_a, studies):
    session = _session_for(org_a._client)
    organization_id = uuid.UUID(org_a.organization_id)
    await retrieval.index_document(session, organization_id, "Billing", ANNUAL)
    await session.commit()

    assert await study.study_existing(session, organization_id) == 1
    await session.commit()
    row = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.organization_id == organization_id)
        )
    ).scalar_one()
    assert study.asked_as(row) == ["yrly price??", "saal ka kitna"]
    assert row.content == ANNUAL

    # Running it again finds nothing left to do.
    assert await study.study_existing(session, organization_id) == 0


async def test_several_readings_are_searched_separately(org_a):
    session = _session_for(org_a._client)
    organization_id = uuid.UUID(org_a.organization_id)
    await retrieval.index_document(session, organization_id, "Billing", ANNUAL)
    await retrieval.index_document(session, organization_id, "Delivery", DELIVERY)
    await session.commit()

    # The typed words find nothing; the analyzer's reading of them does.
    found = await retrieval.search_readings(
        session, organization_id, ["yrly??", "How are annual plans billed?", None, ""]
    )
    assert [chunk.title for chunk in found][:1] == ["Billing"]


def test_the_analyzer_reads_what_a_message_most_likely_means():
    read = analyzer._coerce(
        {"intent": "price_question", "meaning": "  How much is the Growth plan per year? "},
        "hw mch yrly 4 it",
        "NEW",
    )
    assert read["meaning"] == "How much is the Growth plan per year?"
    assert analyzer._coerce({"meaning": "null"}, "hi", "NEW")["meaning"] is None
    assert analyzer._coerce({"meaning": 12}, "hi", "NEW")["meaning"] is None
    assert analyzer.heuristic_analysis("hw mch")["meaning"] is None


def test_the_reply_is_given_the_reading_and_told_the_words_win():
    organization = Organization(name="Tallybird POS")
    prompt = llm_service.build_prompt(
        organization, None, [], "hw mch yrly 4 it", meaning="How much is the Growth plan per year?"
    )
    assert "Most likely meaning: How much is the Growth plan per year?" in prompt
    assert "go by their words" in prompt and "language they wrote in" in prompt

    # Nothing to add when the message was already clear.
    same = llm_service.build_prompt(organization, None, [], "What is the price?", meaning="what is the price?")
    assert "Most likely meaning" not in same
