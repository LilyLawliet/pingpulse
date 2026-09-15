"""The funnel, the reply times, and the ways both of them could lie.

Most of these are about shapes a chart is not allowed to have. A funnel that
widens at the bottom, a median reply time of zero for a shop that has never
replied, a quiet weekend that vanishes because empty days were skipped - each
one renders perfectly and tells the reader something untrue, which is worse
than a screen that fails to load.
"""

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from app.models import (
    SENDER_AGENT,
    SENDER_CUSTOMER,
    SENDER_OPERATOR,
    STAGE_OPERATOR,
    CRMContact,
    Message,
    Organization,
    StageEvent,
    TenantPipeline,
)
from app.services import analytics

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- helpers
_numbers = count(1)


async def add_contact(db, organization, *, stage="NEW_LEAD", created_at=None, **extra):
    contact = CRMContact(
        organization_id=organization.id,
        phone_number=f"+1555{next(_numbers):07d}",
        pipeline_stage=stage,
        created_at=created_at or NOW - timedelta(days=1),
        **extra,
    )
    db.add(contact)
    await db.flush()
    return contact


async def add_message(db, contact, sender, at, content="hello"):
    message = Message(
        organization_id=contact.organization_id,
        contact_id=contact.id,
        sender=sender,
        content=content,
        created_at=at,
    )
    db.add(message)
    await db.flush()
    return message


async def add_move(db, contact, to_stage, *, from_stage=None, at=None):
    event = StageEvent(
        organization_id=contact.organization_id,
        contact_id=contact.id,
        from_stage=from_stage,
        to_stage=to_stage,
        source="agent",
        at=at or NOW - timedelta(hours=1),
    )
    db.add(event)
    await db.flush()
    return event


def rung(result, key):
    for stage in result["stages"]:
        if stage["key"] == key:
            return stage
    raise AssertionError(f"{key} is not on the funnel")


# ------------------------------------------------------------------ funnel
@pytest.mark.asyncio
async def test_a_lead_deep_in_the_board_counts_at_every_stage_before_it(
    db_session, default_org
):
    """No history required to know a won lead was once a new lead.

    Contacts that predate stage tracking have no events at all. Reading only
    the events would show them at nothing, and a funnel with a full "Won" bar
    above four empty ones is the clearest possible way to look broken.
    """
    await add_contact(db_session, default_org, stage="WON")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert rung(result, "NEW_LEAD")["reached"] == 1
    assert rung(result, "QUALIFIED")["reached"] == 1
    assert rung(result, "WON")["reached"] == 1
    assert result["converted"] == 1


@pytest.mark.asyncio
async def test_the_funnel_never_widens(db_session, default_org):
    await add_contact(db_session, default_org, stage="NEW_LEAD")
    await add_contact(db_session, default_org, stage="NEW_LEAD")
    await add_contact(db_session, default_org, stage="QUALIFIED")
    await add_contact(db_session, default_org, stage="WON")

    result = await analytics.funnel(db_session, default_org.id, None, None)
    counts = [stage["reached"] for stage in result["stages"]]

    assert counts == sorted(counts, reverse=True), counts
    assert counts[0] == 4


@pytest.mark.asyncio
async def test_a_lost_lead_keeps_the_progress_it_made(db_session, default_org):
    """Lost is where it stopped, not how far it got.

    Without the event this lead would count only as having arrived, and every
    shop's funnel would understate the work that went into the deals it did
    not win - which is exactly the work they are trying to see.
    """
    contact = await add_contact(db_session, default_org, stage="LOST")
    await add_move(db_session, contact, "QUALIFIED", from_stage="CONTACTED")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert rung(result, "QUALIFIED")["reached"] == 1
    assert rung(result, "ESTIMATE_SENT")["reached"] == 0
    assert [exit_["count"] for exit_ in result["exits"] if exit_["key"] == "LOST"] == [1]


@pytest.mark.asyncio
async def test_exits_are_not_rungs(db_session, default_org):
    """A board's lost column must not appear as a step people travel to."""
    await add_contact(db_session, default_org, stage="LOST")

    result = await analytics.funnel(db_session, default_org.id, None, None)
    keys = [stage["key"] for stage in result["stages"]]

    assert "LOST" not in keys
    assert "UNQUALIFIED" not in keys
    # It still arrived, so the top of the funnel counts it.
    assert rung(result, "NEW_LEAD")["reached"] == 1


@pytest.mark.asyncio
async def test_a_shop_with_no_leads_gets_zeros_and_no_rate(db_session, default_org):
    """Not a crash, and not a conversion rate of zero.

    Nought out of nought is not nought per cent; it is a question with no
    answer, and the screen has to be able to tell the difference to say
    "nothing yet" instead of "0%".
    """
    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert result["entered"] == 0
    assert result["converted"] == 0
    assert result["conversion_rate"] is None
    assert all(stage["reached"] == 0 for stage in result["stages"])
    assert all(stage["rate_from_previous"] is None for stage in result["stages"])


@pytest.mark.asyncio
async def test_a_board_made_entirely_of_exits_does_not_crash(db_session, default_org):
    """Nonsense, but a tenant can build it in the pipeline editor.

    A funnel with no rungs is not a reason to fail the whole screen - the
    traffic and reply panels next to it are still perfectly good answers.
    """
    for index, (key, outcome) in enumerate([("GONE", "lost"), ("NOPE", "unqualified")]):
        db_session.add(
            TenantPipeline(
                organization_id=default_org.id,
                key=key,
                label=key.title(),
                order_index=index,
                colour="rose",
                outcome=outcome,
                is_entry=index == 0,
            )
        )
    await db_session.flush()
    await add_contact(db_session, default_org, stage="GONE")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert result["stages"] == []
    assert result["entered"] == 1
    assert result["conversion_rate"] is None


@pytest.mark.asyncio
async def test_a_contact_on_a_stage_that_is_not_on_the_board_still_counts(
    db_session, default_org
):
    """Orphans are counted as arrived rather than dropped.

    A board can be rewritten under the contacts standing on it. Silently
    excluding them would make the top of the funnel smaller than the number of
    people the shop can see in its own inbox.
    """
    await add_contact(db_session, default_org, stage="SOME_OLD_KEY")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert result["entered"] == 1
    assert rung(result, "NEW_LEAD")["reached"] == 1


@pytest.mark.asyncio
async def test_the_window_is_a_cohort_of_arrivals(db_session, default_org):
    """Thirty days means the leads that arrived then, not the moves that did.

    Counting moves instead would let one old lead being dragged across the
    board today appear as a brand-new conversion, and a quiet month spent
    tidying the CRM would read as the best month on record.
    """
    old = await add_contact(
        db_session, default_org, stage="WON", created_at=NOW - timedelta(days=400)
    )
    await add_move(db_session, old, "WON", from_stage="FOLLOW_UP", at=NOW)
    await add_contact(
        db_session, default_org, stage="NEW_LEAD", created_at=NOW - timedelta(days=2)
    )

    recent = await analytics.funnel(
        db_session, default_org.id, NOW - timedelta(days=30), None
    )

    assert recent["entered"] == 1
    assert recent["converted"] == 0
    assert rung(recent, "WON")["reached"] == 0


@pytest.mark.asyncio
async def test_one_tenant_never_sees_another_tenants_funnel(db_session, default_org):
    other = Organization(name="Someone Else", sales_prompt="Sell.")
    db_session.add(other)
    await db_session.flush()

    theirs = await add_contact(db_session, other, stage="WON")
    await add_move(db_session, theirs, "WON", from_stage="FOLLOW_UP")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert result["entered"] == 0
    assert result["converted"] == 0


@pytest.mark.asyncio
async def test_drop_off_is_reported_against_the_stage_above(db_session, default_org):
    for _ in range(4):
        await add_contact(db_session, default_org, stage="NEW_LEAD")
    await add_contact(db_session, default_org, stage="QUALIFIED")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert rung(result, "NEW_LEAD")["dropped"] is None  # nothing above it
    assert rung(result, "CONTACTED")["reached"] == 1
    assert rung(result, "CONTACTED")["dropped"] == 4
    assert rung(result, "CONTACTED")["rate_from_previous"] == 0.2


# ------------------------------------------------------------ reply times
@pytest.mark.asyncio
async def test_three_messages_in_a_row_are_one_wait(db_session, default_org):
    """A person typing in bursts is waiting once, not three times.

    Measuring per message would score the second and third lines at nearly
    zero seconds each and pull the median down to something no customer ever
    experienced.
    """
    contact = await add_contact(db_session, default_org)
    start = NOW
    await add_message(db_session, contact, SENDER_CUSTOMER, start)
    await add_message(db_session, contact, SENDER_CUSTOMER, start + timedelta(seconds=5))
    await add_message(db_session, contact, SENDER_CUSTOMER, start + timedelta(seconds=9))
    await add_message(db_session, contact, SENDER_AGENT, start + timedelta(seconds=60))

    volume = await analytics.traffic(
        db_session, default_org.id, None, NOW + timedelta(days=1), analytics.zone_for("UTC")
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["answered"] == 1
    assert replies["median_seconds"] == 60


@pytest.mark.asyncio
async def test_an_unprompted_message_is_not_an_answer(db_session, default_org):
    """A follow-up nudge went out because a timer fired, not because anyone asked.

    Counting it would credit the agent with answering a conversation nobody
    was having, and there is no wait to measure because nobody was waiting.
    """
    contact = await add_contact(db_session, default_org)
    await add_message(db_session, contact, SENDER_AGENT, NOW, content="Still there?")

    volume = await analytics.traffic(
        db_session, default_org.id, None, NOW + timedelta(days=1), analytics.zone_for("UTC")
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["answered"] == 0
    assert replies["median_seconds"] is None
    assert volume["totals"]["outbound"] == 1


@pytest.mark.asyncio
async def test_a_shop_that_has_never_replied_has_no_median(db_session, default_org):
    """None, not zero. Nought seconds is the best reply time there is."""
    contact = await add_contact(db_session, default_org)
    await add_message(db_session, contact, SENDER_CUSTOMER, NOW)

    volume = await analytics.traffic(
        db_session, default_org.id, None, NOW + timedelta(days=1), analytics.zone_for("UTC")
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["median_seconds"] is None
    assert replies["agent_share"] is None


@pytest.mark.asyncio
async def test_who_answered_is_recorded_separately(db_session, default_org):
    """Whether the agent is doing the work is the whole question a shop has."""
    contact = await add_contact(db_session, default_org)
    at = NOW
    for sender in (SENDER_AGENT, SENDER_AGENT, SENDER_OPERATOR):
        await add_message(db_session, contact, SENDER_CUSTOMER, at)
        await add_message(db_session, contact, sender, at + timedelta(seconds=10))
        at += timedelta(hours=1)

    volume = await analytics.traffic(
        db_session, default_org.id, None, NOW + timedelta(days=1), analytics.zone_for("UTC")
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["by"] == {"agent": 2, "operator": 1}
    assert replies["agent_share"] == pytest.approx(2 / 3, abs=1e-3)


@pytest.mark.asyncio
async def test_one_overnight_reply_does_not_move_the_median(db_session, default_org):
    """The reason these are medians rather than means.

    One message answered the next morning would drag a mean past every number
    around it, producing a figure that describes no reply ever actually sent.
    """
    contact = await add_contact(db_session, default_org)
    at = NOW
    for gap in (10, 12, 14, 11, 40_000):
        await add_message(db_session, contact, SENDER_CUSTOMER, at)
        await add_message(db_session, contact, SENDER_AGENT, at + timedelta(seconds=gap))
        at += timedelta(days=1)

    volume = await analytics.traffic(
        db_session,
        default_org.id,
        None,
        NOW + timedelta(days=30),
        analytics.zone_for("UTC"),
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["median_seconds"] == 12
    assert replies["p90_seconds"] == 40_000


@pytest.mark.asyncio
async def test_turns_are_not_carried_across_contacts(db_session, default_org):
    """Two conversations are two conversations.

    Rows arrive grouped by contact. Without resetting at the boundary, the
    last unanswered question of one person would be closed by the first reply
    to the next, inventing a wait that spans two different conversations.
    """
    one = await add_contact(db_session, default_org)
    two = await add_contact(db_session, default_org)
    await add_message(db_session, one, SENDER_CUSTOMER, NOW)
    await add_message(db_session, two, SENDER_AGENT, NOW + timedelta(hours=5))

    volume = await analytics.traffic(
        db_session, default_org.id, None, NOW + timedelta(days=1), analytics.zone_for("UTC")
    )
    replies = analytics.reply_times(volume["_rows"])

    assert replies["answered"] == 0


def test_naive_timestamps_do_not_blow_up():
    """SQLite hands back naive datetimes and PostgreSQL aware ones.

    Both mean UTC, and subtracting one from the other raises. This is the kind
    of thing that passes every test and then fails only in production.
    """
    naive = datetime(2026, 9, 15, 12, 0)
    aware = datetime(2026, 9, 15, 12, 0, 30, tzinfo=timezone.utc)
    rows = [("c1", SENDER_CUSTOMER, naive), ("c1", SENDER_AGENT, aware)]

    assert analytics.reply_times(rows)["median_seconds"] == 30


def test_a_reply_before_the_question_is_dropped_rather_than_negative():
    rows = [
        ("c1", SENDER_CUSTOMER, datetime(2026, 9, 15, 12, 0, 30, tzinfo=timezone.utc)),
        ("c1", SENDER_AGENT, datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)),
    ]

    assert analytics.reply_times(rows)["median_seconds"] is None


# ---------------------------------------------------------------- traffic
@pytest.mark.asyncio
async def test_quiet_days_are_drawn_rather_than_skipped(db_session, default_org):
    """The gaps are the information.

    A chart that omits empty days puts a busy Friday next to a busy Monday and
    makes the weekend disappear, which reads as steady traffic.
    """
    contact = await add_contact(
        db_session, default_org, created_at=NOW - timedelta(days=10)
    )
    await add_message(db_session, contact, SENDER_CUSTOMER, NOW - timedelta(days=6))
    await add_message(db_session, contact, SENDER_CUSTOMER, NOW - timedelta(days=2))

    volume = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(days=7),
        NOW,
        analytics.zone_for("UTC"),
    )

    assert volume["grain"] == "day"
    assert len(volume["points"]) >= 7
    assert any(point["inbound"] == 0 for point in volume["points"])
    assert volume["totals"]["inbound"] == 2


@pytest.mark.asyncio
async def test_days_start_when_the_shop_says_they_do(db_session, default_org):
    """A shop in Dubai wants a 2am local message on the day it happened there.

    Bucketing in UTC would file it under the previous day, and the busiest
    evening of the week would be split across two bars.
    """
    contact = await add_contact(
        db_session, default_org, created_at=NOW - timedelta(days=5)
    )
    late = datetime(2026, 9, 10, 22, 30, tzinfo=timezone.utc)  # 02:30 next day in Dubai
    await add_message(db_session, contact, SENDER_CUSTOMER, late)

    in_utc = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(days=7),
        NOW,
        analytics.zone_for("UTC"),
    )
    in_dubai = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(days=7),
        NOW,
        analytics.zone_for("Asia/Dubai"),
    )

    busy = lambda volume: [p["bucket"] for p in volume["points"] if p["inbound"]]
    assert busy(in_utc) == ["2026-09-10"]
    assert busy(in_dubai) == ["2026-09-11"]


def test_a_timezone_nobody_recognises_falls_back_to_utc():
    """A typo in the settings box must not take the dashboard down."""
    assert str(analytics.zone_for("Mars/Olympus")) == "UTC"
    assert str(analytics.zone_for("")) == "UTC"
    assert str(analytics.zone_for(None)) == "UTC"


@pytest.mark.asyncio
async def test_a_long_span_is_bucketed_by_week(db_session, default_org):
    """Three hundred daily bars on a laptop screen is a smear, not a chart."""
    volume = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(days=300),
        NOW,
        analytics.zone_for("UTC"),
    )

    assert volume["grain"] == "week"
    assert len(volume["points"]) <= analytics.MAX_BUCKETS


@pytest.mark.asyncio
async def test_a_single_day_is_bucketed_by_hour(db_session, default_org):
    volume = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(hours=12),
        NOW,
        analytics.zone_for("UTC"),
    )

    assert volume["grain"] == "hour"


@pytest.mark.asyncio
async def test_the_chart_and_the_total_under_it_agree(db_session, default_org):
    """They are read from the same rows, and they have to stay that way.

    Two numbers next to each other that disagree by one always look like a
    bug in the product, never like a rounding difference in a chart.
    """
    contact = await add_contact(db_session, default_org, created_at=NOW - timedelta(days=3))
    for offset in range(6):
        await add_message(
            db_session, contact, SENDER_CUSTOMER, NOW - timedelta(hours=offset * 7)
        )

    volume = await analytics.traffic(
        db_session,
        default_org.id,
        NOW - timedelta(days=7),
        NOW,
        analytics.zone_for("UTC"),
    )

    assert sum(point["inbound"] for point in volume["points"]) == volume["totals"]["inbound"]
    assert volume["totals"]["inbound"] == 6


# ------------------------------------------------------------ recording moves
@pytest.mark.asyncio
async def test_moving_a_lead_from_the_dashboard_is_written_down(client, org_a):
    contact = await client.post(
        "/api/v1/crm/contacts",
        headers=org_a.headers,
        json={"phone_number": "+15551230000", "name": "Dana"},
    )
    assert contact.status_code in (200, 201), contact.text
    contact_id = contact.json()["id"]

    moved = await client.patch(
        f"/api/v1/crm/contacts/{contact_id}",
        headers=org_a.headers,
        json={"pipeline_stage": "QUALIFIED"},
    )
    assert moved.status_code == 200, moved.text

    from sqlalchemy import select

    from tests.conftest import _session_for

    session = _session_for(client)
    events = (await session.execute(select(StageEvent))).scalars().all()

    assert len(events) == 1
    assert events[0].from_stage == "NEW_LEAD"
    assert events[0].to_stage == "QUALIFIED"
    assert events[0].source == STAGE_OPERATOR


@pytest.mark.asyncio
async def test_saving_a_lead_without_moving_it_records_nothing(client, org_a):
    """A PATCH naming the stage it is already on is not a transition.

    Editing a phone number would otherwise write a move, and the history would
    fill up with steps nobody took.
    """
    created = await client.post(
        "/api/v1/crm/contacts",
        headers=org_a.headers,
        json={"phone_number": "+15551230001", "name": "Eli"},
    )
    contact_id = created.json()["id"]

    await client.patch(
        f"/api/v1/crm/contacts/{contact_id}",
        headers=org_a.headers,
        json={"name": "Eli B", "pipeline_stage": "NEW_LEAD"},
    )

    from sqlalchemy import select

    from tests.conftest import _session_for

    session = _session_for(client)
    assert (await session.execute(select(StageEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_failed_note_does_not_take_the_move_with_it(db_session, default_org):
    """The lead has already moved; losing the note is a gap in a chart.

    Rolling the move back to keep the history tidy would be a far worse trade
    than a missing row, so this swallows whatever went wrong.
    """
    contact = await add_contact(db_session, default_org)

    class Broken:
        def add(self, _):
            raise RuntimeError("the session is gone")

        async def flush(self):
            raise RuntimeError("the session is gone")

    await analytics.record_move(Broken(), contact, "QUALIFIED", from_stage="NEW_LEAD")
    await analytics.record_move(db_session, None, "QUALIFIED")
    await analytics.record_move(db_session, contact, "")


# -------------------------------------------------------------------- API
@pytest.mark.asyncio
async def test_the_analytics_endpoint_answers_for_a_brand_new_shop(client, org_a):
    """Day one, nothing configured, no contacts. It still has to render."""
    response = await client.get("/api/v1/analytics?window=30d", headers=org_a.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["funnel"]["entered"] == 0
    assert body["funnel"]["conversion_rate"] is None
    assert body["replies"]["median_seconds"] is None
    assert body["traffic"]["points"] != []
    assert body["sources"] == []
    assert body["movement"]["stage_changes"] == 0
    assert body["movement"]["tracked_from"] is None


@pytest.mark.asyncio
async def test_a_backwards_range_is_refused(client, org_a):
    response = await client.get(
        "/api/v1/analytics?since=2026-09-10T00:00:00Z&until=2026-09-01T00:00:00Z",
        headers=org_a.headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_window_nobody_has_heard_of_falls_back_to_all_time(client, org_a):
    """The screen asking is ours, so a typo should not blank the dashboard."""
    response = await client.get(
        "/api/v1/analytics?window=last-tuesday", headers=org_a.headers
    )

    assert response.status_code == 200
    assert response.json()["since"] is None


@pytest.mark.asyncio
async def test_analytics_needs_a_token(client):
    assert (await client.get("/api/v1/analytics")).status_code == 401


@pytest.mark.asyncio
async def test_analytics_are_scoped_to_the_caller(client, org_a, org_b):
    """Two shops on one database, and neither one's numbers leak."""
    await client.post(
        "/api/v1/crm/contacts",
        headers=org_a.headers,
        json={"phone_number": "+15559990000", "name": "Alpha lead"},
    )

    theirs = await client.get("/api/v1/analytics?window=all", headers=org_b.headers)

    assert theirs.status_code == 200
    assert theirs.json()["funnel"]["entered"] == 0
    assert theirs.json()["organization"] == "Beta Motors"

    ours = await client.get("/api/v1/analytics?window=all", headers=org_a.headers)
    assert ours.json()["funnel"]["entered"] == 1


@pytest.mark.asyncio
async def test_the_window_names_the_dashboard_uses_all_work(client, org_a):
    for window in ("today", "7d", "30d", "90d", "all"):
        response = await client.get(
            f"/api/v1/analytics?window={window}", headers=org_a.headers
        )
        assert response.status_code == 200, f"{window}: {response.text}"
        assert response.json()["window"] == window


# ------------------------------------------------------------------ windows
def test_today_means_the_shops_today():
    """A shop in Dubai opening this at nine in the morning.

    With midnight taken in UTC, "today" began at four the previous afternoon
    for them - so it quietly included most of yesterday evening, which for a
    lot of these businesses is when they are busiest. The number was wrong in
    the direction that flatters it, which is the worst direction.
    """
    dubai = analytics.zone_for("Asia/Dubai")

    local_midnight = analytics.window_start("today", zone=dubai)
    utc_midnight = analytics.window_start("today")

    assert local_midnight.astimezone(dubai).hour == 0
    assert local_midnight.astimezone(dubai).minute == 0
    # Four hours apart, in whichever direction the clock happens to fall.
    assert abs((local_midnight - utc_midnight).total_seconds()) in (0, 4 * 3600, 20 * 3600)


def test_the_rolling_windows_do_not_move_with_the_timezone():
    """Seven days is seven days. Only calendar boundaries need a zone."""
    here = analytics.window_start("7d", zone=analytics.zone_for("Pacific/Auckland"))
    there = analytics.window_start("7d", zone=analytics.zone_for("America/Anchorage"))

    assert abs((here - there).total_seconds()) < 2


def test_an_explicit_since_beats_everything():
    asked = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)

    assert analytics.window_start("today", asked, analytics.zone_for("Asia/Dubai")) == asked


def test_all_time_has_no_start():
    assert analytics.window_start("all") is None
    assert analytics.window_start(None) is None
    assert analytics.window_start("last-tuesday") is None


@pytest.mark.asyncio
async def test_the_funnel_speaks_the_tenants_own_words(db_session, default_org):
    """A renamed board must rename the chart, not orphan it.

    Keys are stable and labels are not, deliberately. A shop that translates
    its board or renames "Qualified" to "Surveyed" should see that word on the
    funnel, and the contacts standing in that column should still be counted.
    """
    board = [
        ("NEW_LEAD", "Enquiry", None),
        ("QUALIFIED", "Surveyed", None),
        ("WON", "Job booked", "won"),
        ("LOST", "Went elsewhere", "lost"),
    ]
    for index, (key, label, outcome) in enumerate(board):
        db_session.add(
            TenantPipeline(
                organization_id=default_org.id,
                key=key,
                label=label,
                order_index=index,
                colour="slate",
                outcome=outcome,
                is_entry=index == 0,
            )
        )
    await db_session.flush()
    await add_contact(db_session, default_org, stage="WON")
    await add_contact(db_session, default_org, stage="QUALIFIED")

    result = await analytics.funnel(db_session, default_org.id, None, None)

    assert [stage["label"] for stage in result["stages"]] == [
        "Enquiry",
        "Surveyed",
        "Job booked",
    ]
    assert rung(result, "QUALIFIED")["reached"] == 2
    assert result["converted"] == 1
    assert [exit_["label"] for exit_ in result["exits"]] == ["Went elsewhere"]
