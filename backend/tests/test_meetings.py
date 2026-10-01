"""Meetings with the business - a demo, a call - in the owner's real calendar.

Somebody selling software books demos. The prospect writes "can we book a
demo?", is offered times the owner is actually free - checked against the
owner's own Google or Outlook calendar, read from its private address - and
the booking is sent to that calendar as an invitation the moment it is made.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import CRMContact, Organization
from app.services import booking, busy_calendar, invites, notifications

from .conftest import confirmed

DUBAI = ZoneInfo("Asia/Dubai")
WEEKDAYS = {
    day: {"open": "09:00", "close": "18:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}


@pytest.fixture(autouse=True)
def _clean_cache():
    busy_calendar._cache.clear()
    yield
    busy_calendar._cache.clear()


@pytest.fixture
async def saas(db_session):
    organization = Organization(name="Ledgerly", sales_prompt="Accounting software for clinics.")
    organization.timezone = "Asia/Dubai"
    organization.agent_config = {
        "business_hours": WEEKDAYS,
        "appointments": {
            "min_notice_minutes": 60,
            "default_kind": "onsite",
            "duration_minutes": 60,
            "duration_by_kind": {"video": 30},
            "meeting_link": "https://meet.google.com/abc-defg-hij",
            "busy_calendar_url": "https://calendar.google.com/calendar/ical/x/private-y/basic.ics",
        },
        # The shop asks retail customers for an address before a visit. A
        # demo must not wait on that.
        "qualification_slots": [{"key": "address", "asks": "the property address"}],
    }
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(
        organization_id=organization.id, phone_number="971500000001", name="Dr Sara", qualification={}
    )
    db_session.add(contact)
    await db_session.flush()
    return organization, contact


def _ahead(weekday: int) -> date:
    today = datetime.now(DUBAI).date()
    return today + timedelta(days=(weekday - today.weekday()) % 7 + 7)


def _ics(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def _event(uid: str, start: datetime, end: datetime, extra: str = "") -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:{start.astimezone(timezone.utc).strftime(fmt)}\r\n"
        f"DTEND:{end.astimezone(timezone.utc).strftime(fmt)}\r\nSUMMARY:Private\r\n{extra}END:VEVENT\r\n"
    )


def _serve(monkeypatch, body: bytes | Exception):
    calls = []

    async def download(url):
        calls.append(url)
        if isinstance(body, Exception):
            raise body
        return body

    monkeypatch.setattr(busy_calendar, "_download", download)
    return calls


# ------------------------------------------------------------- reading
def test_busy_times_skip_free_cancelled_and_our_own_events():
    tuesday = _ahead(1)
    at = lambda h: datetime.combine(tuesday, time(h), tzinfo=DUBAI)  # noqa: E731
    body = _ics(
        _event("a@google.com", at(10), at(11)),
        _event("b@google.com", at(12), at(13), "TRANSP:TRANSPARENT\r\n"),
        _event("c@google.com", at(14), at(15), "STATUS:CANCELLED\r\n"),
        _event("invite-123@pingpulse", at(16), at(17)),
    )
    busy, _ = busy_calendar.parse(body, DUBAI)
    assert [(b.starts_at, b.ends_at) for b in busy] == [
        (at(10).astimezone(timezone.utc), at(11).astimezone(timezone.utc))
    ]


def test_a_weekly_meeting_blocks_every_week():
    monday = _ahead(0) - timedelta(days=7)
    start = datetime.combine(monday, time(9), tzinfo=DUBAI)
    body = _ics(_event("standup@google.com", start, start + timedelta(hours=1), "RRULE:FREQ=WEEKLY\r\n"))
    busy, _ = busy_calendar.parse(body, DUBAI)
    mondays = {b.starts_at.astimezone(DUBAI).date() for b in busy}
    assert _ahead(0) in mondays and _ahead(0) + timedelta(days=7) in mondays


def test_all_day_events_block_the_whole_day():
    friday = _ahead(4)
    body = _ics(
        "BEGIN:VEVENT\r\nUID:holiday@google.com\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART;VALUE=DATE:{friday.strftime('%Y%m%d')}\r\n"
        f"DTEND;VALUE=DATE:{(friday + timedelta(days=1)).strftime('%Y%m%d')}\r\nEND:VEVENT\r\n"
    )
    busy, _ = busy_calendar.parse(body, DUBAI)
    assert busy[0].starts_at.astimezone(DUBAI) == datetime.combine(friday, time(0), tzinfo=DUBAI)


@pytest.mark.parametrize(
    "url", ["http://example.com/cal.ics", "ftp://x/cal.ics", "not a url", "https://"]
)
def test_only_https_calendars_are_accepted(url):
    assert busy_calendar.problem_with(url)


def test_webcal_is_https():
    assert busy_calendar.normalise("webcal://p01.icloud.com/x") == "https://p01.icloud.com/x"
    assert busy_calendar.problem_with("webcal://p01.icloud.com/x") is None


def test_addresses_inside_the_network_are_refused():
    assert not busy_calendar._public("127.0.0.1")
    assert not busy_calendar._public("169.254.169.254")
    assert not busy_calendar._public("10.0.0.5")


# ------------------------------------------------------------- booking
@pytest.mark.asyncio
async def test_a_demo_is_offered_only_when_the_owner_is_free(saas, db_session, monkeypatch):
    organization, contact = saas
    tuesday = _ahead(1)
    busy_start = datetime.combine(tuesday, time(9), tzinfo=DUBAI)
    _serve(monkeypatch, _ics(_event("x@google.com", busy_start, busy_start + timedelta(hours=3))))

    turn = await booking.handle_turn(
        db_session, organization, contact, f"can we book a demo on {tuesday.day} {tuesday:%B}?"
    )
    assert turn.offered
    assert all(slot.astimezone(DUBAI).time() >= time(12) for slot in turn.offered), turn.offered
    assert "meeting" in turn.prompt_block and "30 minutes" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_busy_time_named_by_the_prospect_is_refused(saas, db_session, monkeypatch):
    organization, contact = saas
    tuesday = _ahead(1)
    at = datetime.combine(tuesday, time(15), tzinfo=DUBAI)
    _serve(monkeypatch, _ics(_event("x@google.com", at, at + timedelta(hours=1))))
    turn = await booking.handle_turn(
        db_session, organization, contact, f"book a demo {tuesday.day} {tuesday:%B} at 3pm"
    )
    assert turn.performed is None
    assert "isn't free" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_demo_is_booked_as_a_video_call_with_the_link_and_no_address_asked(
    saas, db_session, monkeypatch
):
    organization, contact = saas
    _serve(monkeypatch, _ics())
    thursday = _ahead(3)
    turn = await confirmed(
        db_session, organization, contact,
        f"I'd like to book a demo for my 3 clinics, {thursday.day} {thursday:%B} at 11am",
    )
    assert turn.performed == "booked", turn.prompt_block
    row = turn.appointment
    assert row.kind == "video"
    assert (row.ends_at - row.starts_at) == timedelta(minutes=30)
    assert row.location == "https://meet.google.com/abc-defg-hij"
    assert "3 clinics" in row.notes
    assert "join: https://meet.google.com/abc-defg-hij" in booking.describe(row)
    assert "calendar.google.com/calendar/render" in turn.prompt_block
    assert booking.alert_for(turn, "Dr Sara")[0] == "New meeting booked"


@pytest.mark.asyncio
async def test_picking_from_a_demo_offer_keeps_it_a_demo(saas, db_session, monkeypatch):
    organization, contact = saas
    _serve(monkeypatch, _ics())
    offer = await booking.handle_turn(db_session, organization, contact, "can we schedule a quick phone call?")
    assert offer.offered
    picked = await confirmed(db_session, organization, contact, "the first one")
    assert picked.performed == "booked"
    assert picked.appointment.kind == "phone"
    assert picked.appointment.starts_at == offer.offered[0]
    assert "quick phone call" in picked.appointment.notes


@pytest.mark.asyncio
async def test_an_unreadable_calendar_offers_nothing(saas, db_session, monkeypatch):
    organization, contact = saas
    _serve(monkeypatch, busy_calendar.Unreadable("the calendar answered 404"))
    turn = await booking.handle_turn(db_session, organization, contact, "can we book a demo?")
    assert turn.calendar_down
    assert turn.offered == [] and turn.performed is None
    assert "Do NOT offer" in turn.prompt_block

    # And a time can't be taken by hand either.
    refusal = await booking.is_free(
        db_session, organization,
        datetime.combine(_ahead(2), time(11), tzinfo=DUBAI),
        datetime.combine(_ahead(2), time(12), tzinfo=DUBAI),
    )
    assert refusal.reason == "calendar_unreadable"


@pytest.mark.asyncio
async def test_a_recent_copy_stands_in_when_the_calendar_blips(saas, db_session, monkeypatch):
    organization, contact = saas
    _serve(monkeypatch, _ics())
    await busy_calendar.read(organization)
    _serve(monkeypatch, busy_calendar.Unreadable("timeout"))
    copy = await busy_calendar.read(organization, fresh=True)
    assert copy is not None


# ------------------------------------------------------------- invitations
def test_the_invitation_is_a_request_and_the_withdrawal_a_cancel(saas):
    organization, contact = saas
    from types import SimpleNamespace
    import uuid

    row = SimpleNamespace(
        id=uuid.uuid4(), kind="video", status="confirmed", location="https://meet.google.com/x",
        notes="Meeting request: demo", starts_at=datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 8, 7, 30, tzinfo=timezone.utc),
    )
    request = invites.render(row, contact, "owner@ledgerly.io", "REQUEST")
    assert "METHOD:REQUEST" in request and "DTSTART:20261008T070000Z" in request
    assert f"UID:invite-{row.id}@pingpulse" in request
    assert "mailto:owner@ledgerly.io" in request
    withdrawn = invites.render(row, contact, "owner@ledgerly.io", "CANCEL")
    assert "METHOD:CANCEL" in withdrawn and "SEQUENCE:1" in withdrawn
    # Read back from the owner's calendar, our own invites are not busy time.
    assert busy_calendar.parse(request.encode(), DUBAI)[0] == []


@pytest.mark.asyncio
async def test_a_move_withdraws_the_old_event_and_invites_to_the_new(saas, db_session, monkeypatch):
    organization, contact = saas
    _serve(monkeypatch, _ics())
    sent = []

    async def send(message):
        sent.append(message)
        return "sent"

    async def address(db, org):
        return "owner@ledgerly.io"

    monkeypatch.setattr(invites, "_send", send)
    monkeypatch.setattr(notifications, "email_available", lambda: True)
    monkeypatch.setattr(notifications, "address_for", address)

    thursday, friday = _ahead(3), _ahead(4)
    booked = await confirmed(
        db_session, organization, contact, f"book a demo {thursday.day} {thursday:%B} at 11am"
    )
    moved = await confirmed(
        db_session, organization, contact, f"can we do {friday.day} {friday:%B} at 2pm instead"
    )
    assert moved.performed == "moved"
    await invites.send_for(db_session, organization, booked=moved.appointment, cancelled=moved.previous)

    methods = [part.get_param("method") for m in sent for part in m.walk() if part.get_content_type() == "text/calendar"]
    assert methods == ["CANCEL", "REQUEST"]
    assert str(booked.appointment.id) in sent[0].get_body(("calendar",)).get_content()


# ------------------------------------------------------------- settings
@pytest.mark.asyncio
async def test_connecting_a_calendar_reads_it_first_and_never_echoes_it(org_a, monkeypatch):
    friday = _ahead(4)
    at = datetime.combine(friday, time(10), tzinfo=DUBAI)
    _serve(monkeypatch, _ics(_event("x@google.com", at, at + timedelta(hours=1))))
    secret = "https://calendar.google.com/calendar/ical/me%40x.com/private-abc123/basic.ics"

    saved = await org_a.put("/api/v1/calendar/connection", json={"busy_calendar_url": secret})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["busy_calendar_set"] and body["busy_calendar_host"] == "calendar.google.com"
    assert body["check"]["ok"] and body["check"]["busy_next_14_days"] == 1
    assert "private-abc123" not in saved.text

    shown = (await org_a.get("/api/v1/calendar/connection")).json()
    assert "private-abc123" not in str(shown)

    meeting = await org_a.put(
        "/api/v1/calendar/connection",
        json={"meeting_kind": "video", "meeting_minutes": 45, "meeting_link": "https://zoom.us/j/1"},
    )
    assert meeting.json()["meeting_minutes"] == 45

    # The hours form saves the whole config back; the new settings must pass.
    config = (await org_a.get("/api/v1/agent-config")).json()
    again = await org_a.put(
        "/api/v1/agent-config",
        json={"agent_config": config["agent_config"], "timezone": config["timezone"] or "Asia/Dubai"},
    )
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_a_calendar_that_cannot_be_read_is_not_saved(org_a, monkeypatch):
    _serve(monkeypatch, busy_calendar.Unreadable("the calendar answered 404"))
    refused = await org_a.put(
        "/api/v1/calendar/connection", json={"busy_calendar_url": "https://example.com/nope.ics"}
    )
    assert refused.status_code == 422
    assert "404" in refused.text
    assert (await org_a.get("/api/v1/calendar/connection")).json()["busy_calendar_set"] is False

    plain = await org_a.put(
        "/api/v1/calendar/connection", json={"busy_calendar_url": "http://example.com/cal.ics"}
    )
    assert plain.status_code == 422


# --------------------------------------------- what counts as a meeting
@pytest.mark.parametrize(
    "text",
    [
        "do you do wholesale pricing?",
        "what is your wholesale rate for 500 units",
        "I am a distributor, what are your terms",
        "are you open to b2b orders",
        "do you have a partnership with ABC brand?",
        "can I get the wholesale price list",
        "we supply 3 teams, can you quote",
    ],
)
def test_the_trade_a_supplier_is_in_is_not_a_request_to_meet(text):
    """A wholesaler's customers say these all day. They are asking to buy."""
    assert not booking.is_meeting(text)


@pytest.mark.parametrize(
    "text",
    [
        "can we set up a call?",
        "can I get a demo",
        "lets meet up next week",
        "can we do a zoom call",
        "book a discovery call",
        "schedule a short video call",
        # the trade words do mean a meeting, attached to one
        "can we have a wholesale call",
        "set up a partnership chat",
        "send me a teams link",
        "can we do it over teams",
    ],
)
def test_asking_to_talk_is_a_meeting(text):
    assert booking.is_meeting(text)


@pytest.mark.parametrize(
    "text, move",
    [("please cancel my demo", False), ("can we cancel the meeting?", False),
     ("can I move my demo to another day?", True), ("change the call to friday", True)],
)
def test_demos_and_meetings_can_be_cancelled_and_moved_by_name(text, move):
    assert (booking.wants_move(text) if move else booking.wants_cancel(text)), text
