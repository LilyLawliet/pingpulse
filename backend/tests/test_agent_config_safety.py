"""Reading the rules before they take effect, and putting them back after.

A wrong value in this config is quiet. Nothing raises, no message fails, and
the agent simply begins answering customers under a rule nobody meant - which
is then discovered from a customer rather than from the dashboard. That is the
failure these two endpoints exist for, and it is why the drafts and the
suggestions alongside them are safe to try at all.

`/preview` answers "what will it actually be told", which until now could only
be found out by saving and messaging the number.

`/undo` answers "put that back", without anyone having to remember what the
form said an hour ago.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Organization


async def _save(org, config, timezone=None):
    return await org._client.put(
        "/api/v1/agent-config",
        headers=org.headers,
        json={"agent_config": config, "timezone": timezone},
    )


async def _stored(db_session, org) -> dict:
    organization = await db_session.get(Organization, uuid.UUID(org.organization_id))
    await db_session.refresh(organization)
    return organization.agent_config or {}


# ================================================================= preview
@pytest.mark.asyncio
async def test_preview_shows_the_text_the_model_will_be_handed(org_a):
    response = await org_a.post(
        "/api/v1/agent-config/preview",
        json={
            "agent_config": {
                "services": ["Wet rooms", "Tiling"],
                "never_promise": "Same-day work.",
            }
        },
    )
    assert response.status_code == 200

    body = response.json()
    assert body["valid"] is True
    assert "Wet rooms" in body["prompt_block"]
    assert "Same-day work." in body["prompt_block"]


@pytest.mark.asyncio
async def test_preview_saves_nothing(org_a, db_session):
    """The whole point. A preview that wrote would be a save with a friendlier
    name on it."""
    await org_a.post(
        "/api/v1/agent-config/preview",
        json={"agent_config": {"services": ["Wet rooms"]}},
    )
    await db_session.commit()
    assert await _stored(db_session, org_a) == {}


@pytest.mark.asyncio
async def test_preview_reports_the_problems_a_save_would_raise(org_a):
    """Found while somebody is looking at the form, rather than as a 422 after
    they press the button."""
    body = (
        await org_a.post(
            "/api/v1/agent-config/preview",
            json={"agent_config": {"business_hours": {"monday": {"open": "9am", "close": "5pm"}}}},
        )
    ).json()

    assert body["valid"] is False
    assert body["problems"]
    assert any("HH:MM" in problem for problem in body["problems"])


@pytest.mark.asyncio
async def test_preview_names_what_would_change(org_a):
    await _save(org_a, {"never_promise": "Nothing at all."})

    body = (
        await org_a.post(
            "/api/v1/agent-config/preview",
            json={"agent_config": {"never_promise": "Same-day work."}},
        )
    ).json()

    assert body["changes"]["never_promise"]["from"] == "Nothing at all."
    assert body["changes"]["never_promise"]["to"] == "Same-day work."


@pytest.mark.asyncio
async def test_an_empty_config_previews_as_nothing_rather_than_a_heading(org_a):
    """A shop that has configured nothing keeps exactly the agent it has, and
    the preview has to say so rather than showing a bare section title."""
    body = (
        await org_a.post("/api/v1/agent-config/preview", json={"agent_config": {}})
    ).json()
    assert body["valid"] is True
    assert body["prompt_block"] == ""


@pytest.mark.asyncio
async def test_preview_refuses_something_that_is_not_a_config(org_a):
    response = await org_a.post(
        "/api/v1/agent-config/preview", json={"agent_config": "everything"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_preview_uses_a_timezone_that_has_not_been_saved_yet(org_a):
    """So somebody can see what picking a zone does to "we are closed" before
    committing to it."""
    body = (
        await org_a.post(
            "/api/v1/agent-config/preview",
            json={
                "agent_config": {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}},
                "timezone": "America/New_York",
            },
        )
    ).json()
    assert body["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_one_tenant_cannot_preview_against_anothers_saved_config(org_a, org_b):
    await _save(org_a, {"never_promise": "Alpha's rule."})

    body = (
        await org_b.post(
            "/api/v1/agent-config/preview",
            json={"agent_config": {"never_promise": "Beta's rule."}},
        )
    ).json()

    # The diff is against Beta's own saved config, which is empty.
    assert body["changes"]["never_promise"]["from"] is None


# ==================================================================== undo
@pytest.mark.asyncio
async def test_undo_puts_the_previous_value_back(org_a, db_session):
    await _save(org_a, {"never_promise": "Same-day work."})
    await _save(org_a, {"never_promise": "Absolutely anything."})

    response = await org_a.post("/api/v1/agent-config/undo")
    assert response.status_code == 200
    assert response.json()["agent_config"]["never_promise"] == "Same-day work."

    await db_session.commit()
    assert (await _stored(db_session, org_a))["never_promise"] == "Same-day work."


@pytest.mark.asyncio
async def test_undo_removes_a_field_that_was_not_there_before(org_a):
    """A field added by the last save has no previous value, and restoring it
    to null would leave a key the form then shows back as empty text."""
    await _save(org_a, {"never_promise": "Same-day work."})
    await _save(org_a, {"never_promise": "Same-day work.", "pricing_rules": "Ranges only."})

    body = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert "pricing_rules" not in body["agent_config"]
    assert body["agent_config"]["never_promise"] == "Same-day work."


@pytest.mark.asyncio
async def test_undo_twice_is_a_redo(org_a):
    """Rather than a second step backwards into history nobody asked for. A
    person who overshoots has to be able to come forward again."""
    await _save(org_a, {"never_promise": "First."})
    await _save(org_a, {"never_promise": "Second."})

    undone = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert undone["agent_config"]["never_promise"] == "First."

    redone = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert redone["agent_config"]["never_promise"] == "Second."


@pytest.mark.asyncio
async def test_undo_touches_only_the_fields_that_changed(org_a):
    """`undone` names what actually moved, so the page can say what it put
    back rather than "something changed". Saving replaces this config whole,
    so the snapshot is the state before the last save and nothing else."""
    await _save(org_a, {"never_promise": "Original.", "services": ["Tiling"]})
    await _save(org_a, {"never_promise": "Changed.", "services": ["Tiling"]})

    body = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert body["agent_config"]["never_promise"] == "Original."
    assert body["agent_config"]["services"] == ["Tiling"]
    assert body["undone"] == ["never_promise"]


@pytest.mark.asyncio
async def test_there_is_nothing_to_undo_on_a_new_tenant(org_a):
    response = await org_a.post("/api/v1/agent-config/undo")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_config_read_says_whether_there_is_anything_to_undo(org_a):
    """So the button is only offered where it would do something. An undo
    button that 404s is worse than none."""
    before = (await org_a.get("/api/v1/agent-config")).json()
    assert before["last_change"] is None

    await _save(org_a, {"never_promise": "Same-day work."})

    after = (await org_a.get("/api/v1/agent-config")).json()
    assert after["last_change"]["fields"] == ["never_promise"]
    assert after["last_change"]["was_undo"] is False


@pytest.mark.asyncio
async def test_undo_is_recorded_as_its_own_kind_of_change(org_a):
    await _save(org_a, {"never_promise": "Same-day work."})
    await org_a.post("/api/v1/agent-config/undo")

    found = (await org_a.get("/api/v1/agent-config")).json()
    assert found["last_change"]["was_undo"] is True


@pytest.mark.asyncio
async def test_one_tenant_cannot_undo_anothers_change(org_a, org_b):
    """Cross-tenant reads return nothing rather than somebody else's history."""
    await _save(org_a, {"never_promise": "Alpha's rule."})

    response = await org_b.post("/api/v1/agent-config/undo")
    assert response.status_code == 404

    still = (await org_a.get("/api/v1/agent-config")).json()
    assert still["agent_config"]["never_promise"] == "Alpha's rule."


@pytest.mark.asyncio
async def test_undo_refuses_to_restore_something_the_form_would_reject(
    org_a, db_session
):
    """A config stored before a validation rule existed must not be restored
    into a state the form now refuses, leaving somebody unable to save until
    they find the offending field themselves."""
    from app.services import agent_config

    await _save(org_a, {"never_promise": "Same-day work."})

    # A snapshot holding `escalate_on` as a bare string, which is the shape
    # that iterates character by character and turns "refund" into six
    # single-letter triggers.
    organization = await db_session.get(
        Organization, uuid.UUID(org_a.organization_id)
    )
    organization.agent_config = {
        **(organization.agent_config or {}),
        agent_config.PREVIOUS_KEY: {
            "config": {"escalate_on": "refund"},
            "at": None,
            "was_undo": False,
        },
    }
    await db_session.commit()

    response = await org_a.post("/api/v1/agent-config/undo")
    assert response.status_code == 422


# ============================================= the drafts stay undoable
@pytest.mark.asyncio
async def test_a_trade_draft_that_was_saved_can_be_undone(org_a):
    """The argument for the drafts existing at all: trying one costs nothing,
    because getting back is one press."""
    from app.services import trade_defaults

    draft = trade_defaults.BY_KEY["home_improvement"].as_dict()["draft"]

    await _save(org_a, {"never_promise": "Only what I wrote myself."})
    await _save(org_a, draft)

    body = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert body["agent_config"]["never_promise"] == "Only what I wrote myself."
    assert "pricing_rules" not in body["agent_config"]


@pytest.mark.asyncio
async def test_undo_keeps_a_document_read_since_the_last_save(org_a, db_session):
    """A document reading is not a settings change. Undoing a rule must not
    throw one away, because re-uploading would be the only way back."""
    from app.services import agent_config

    await _save(org_a, {"never_promise": "First."})
    await _save(org_a, {"never_promise": "Second."})

    organization = await db_session.get(
        Organization, uuid.UUID(org_a.organization_id)
    )
    organization.agent_config = {
        **(organization.agent_config or {}),
        agent_config.PROPOSED_KEY: {
            "fields": {"services": ["Wet rooms"]},
            "sources": {"services": "handbook.docx"},
        },
    }
    await db_session.commit()

    body = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert body["agent_config"]["never_promise"] == "First."
    assert body["agent_config"]["from_document"]["fields"]["services"] == ["Wet rooms"]


@pytest.mark.asyncio
async def test_undoing_a_confirmation_brings_the_suggestion_back(org_a, db_session):
    """The other direction. Saving the form consumes what a document offered;
    stepping back has to restore it, or the fields go blank with no way to
    refill them."""
    from app.services import agent_config

    organization = await db_session.get(
        Organization, uuid.UUID(org_a.organization_id)
    )
    organization.agent_config = {
        agent_config.PROPOSED_KEY: {
            "fields": {"services": ["Wet rooms"]},
            "sources": {"services": "handbook.docx"},
        }
    }
    await db_session.commit()

    # Confirming it: the server drops the suggestion, because the form is the
    # confirmation.
    await _save(org_a, {"services": ["Wet rooms"]})
    confirmed = (await org_a.get("/api/v1/agent-config")).json()
    assert "from_document" not in confirmed["agent_config"]

    body = (await org_a.post("/api/v1/agent-config/undo")).json()
    assert "services" not in body["agent_config"]
    assert body["agent_config"]["from_document"]["fields"]["services"] == ["Wet rooms"]
