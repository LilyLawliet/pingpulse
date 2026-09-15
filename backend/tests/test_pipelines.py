"""Boards that belong to the organization rather than to the code.

The board used to be a constant, which is right until the second industry
arrives: "Estimate sent" carries a whole workflow for a contractor and means
nothing to a salon.

Two properties keep per-tenant columns from becoming a source of lost contacts,
and they are what these hold down.

*There is always a board.* An organization with no rows of its own — one
created in the moment before seeding, or every organization in this suite,
which builds its schema with create_all and runs no migrations — gets the
defaults. A dashboard with no columns reads as broken rather than unconfigured.

*Nobody is left standing in a column that no longer exists.* Deleting a stage
somebody is in would take them off the board entirely, which looks exactly like
having lost them, so it is refused while anyone is there.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import DEFAULT_PIPELINE, CRMContact
from app.services import pipelines


def _contact(org_id, stage, phone="+15550301"):
    return CRMContact(
        organization_id=uuid.UUID(org_id),
        phone_number=phone,
        pipeline_stage=stage,
        sales_stage="NEW",
        tags=[],
    )


@pytest.mark.asyncio
async def test_an_organization_without_rows_still_has_a_board(org_a, db_session):
    stages = await pipelines.stages_for(db_session, uuid.UUID(org_a.organization_id))

    assert [s.key for s in stages] == [key for key, _l, _c, _o in DEFAULT_PIPELINE]
    assert stages[0].is_entry, "a new contact would have nowhere defined to land"


@pytest.mark.asyncio
async def test_the_default_board_is_the_nine_the_client_asked_for(org_a, db_session):
    stages = await pipelines.stages_for(db_session, uuid.UUID(org_a.organization_id))

    assert [s.label for s in stages] == [
        "New lead",
        "Contacted",
        "Qualified",
        "Estimate scheduled",
        "Estimate sent",
        "Follow-up",
        "Won",
        "Lost",
        "Unqualified",
    ]


@pytest.mark.asyncio
async def test_outcomes_are_marked_so_counting_never_reads_a_label(org_a, db_session):
    """A board can be renamed and translated freely. Analytics has to keep
    working across that, so which column means "sold" is recorded, not
    inferred from the word on it."""
    stages = await pipelines.stages_for(db_session, uuid.UUID(org_a.organization_id))
    outcomes = {s.key: s.outcome for s in stages if s.outcome}

    assert outcomes == {
        # An appointment, a job and a sale are three businesses' words for the
        # same milestone, so the column carries the meaning.
        "ESTIMATE_SCHEDULED": "booked",
        "WON": "won",
        "LOST": "lost",
        "UNQUALIFIED": "unqualified",
    }


@pytest.mark.asyncio
async def test_seeding_twice_does_not_double_the_board(org_a, db_session):
    org_id = uuid.UUID(org_a.organization_id)
    await pipelines.seed(db_session, org_id)
    await pipelines.seed(db_session, org_id)

    stages = await pipelines.stages_for(db_session, org_id)
    assert len(stages) == len(DEFAULT_PIPELINE)


@pytest.mark.asyncio
async def test_a_tenant_can_rename_and_reorder_their_own_board(org_a, db_session):
    response = await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={
            "stages": [
                {"key": "ENQUIRY", "label": "Enquiry", "colour": "sky"},
                {"key": "BOOKED_IN", "label": "Booked in", "colour": "violet"},
                {"key": "DONE", "label": "Done", "colour": "emerald", "outcome": "won"},
            ]
        },
    )

    assert response.status_code == 200
    stages = response.json()["stages"]
    assert [s["key"] for s in stages] == ["ENQUIRY", "BOOKED_IN", "DONE"]
    assert stages[0]["is_entry"] is True
    assert stages[2]["outcome"] == "won"


@pytest.mark.asyncio
async def test_a_stage_with_people_in_it_cannot_be_deleted(org_a, db_session):
    """Deleting it would leave them pointing at a column that is not on the
    board, which looks exactly like having lost them."""
    org_id = uuid.UUID(org_a.organization_id)
    await pipelines.seed(db_session, org_id)
    db_session.add(_contact(org_a.organization_id, "QUALIFIED"))
    await db_session.flush()

    response = await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={"stages": [{"key": "NEW_LEAD", "label": "New lead"}]},
    )

    assert response.status_code == 409
    assert "still in" in response.json()["detail"]

    # And the board it refused to change is untouched.
    stages = await pipelines.stages_for(db_session, org_id)
    assert len(stages) == len(DEFAULT_PIPELINE)


@pytest.mark.asyncio
async def test_an_empty_stage_can_be_removed(org_a, db_session):
    org_id = uuid.UUID(org_a.organization_id)
    await pipelines.seed(db_session, org_id)

    response = await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={
            "stages": [
                {"key": "NEW_LEAD", "label": "New lead"},
                {"key": "WON", "label": "Won", "outcome": "won"},
            ]
        },
    )

    assert response.status_code == 200
    assert len(response.json()["stages"]) == 2


@pytest.mark.asyncio
async def test_a_made_up_outcome_is_refused(org_a):
    response = await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={"stages": [{"key": "X", "label": "X", "outcome": "maybe"}]},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_board_cannot_be_emptied(org_a):
    response = await org_a._client.put(
        "/api/v1/pipeline", headers=org_a.headers, json={"stages": []}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_one_organizations_board_is_not_anothers(org_a, org_b, db_session):
    await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={"stages": [{"key": "ENQUIRY", "label": "Enquiry"}]},
    )

    theirs = (await org_b.get("/api/v1/pipeline")).json()["stages"]
    assert [s["key"] for s in theirs] != ["ENQUIRY"]
    assert len(theirs) == len(DEFAULT_PIPELINE)


@pytest.mark.asyncio
async def test_a_new_organization_is_given_its_own_board(org_a, db_session):
    """Through the real creation endpoint, not the fixture that builds an
    organization straight in the database - the seeding hangs off the route.

    Reading falls back to the defaults without rows, but a tenant cannot rename
    a column that has no row behind it, so the first customisation would
    otherwise have nothing to edit.
    """
    from sqlalchemy import select

    from app.models import TenantPipeline

    created = await org_a.post(
        "/api/v1/organizations",
        json={"name": "Fresh Shop", "sales_prompt": "Sell the thing."},
    )
    assert created.status_code in (200, 201), created.text

    rows = (
        await db_session.execute(
            select(TenantPipeline).where(
                TenantPipeline.organization_id == uuid.UUID(created.json()["id"])
            )
        )
    ).scalars().all()
    assert len(rows) == len(DEFAULT_PIPELINE)
