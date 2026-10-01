"""Every readiness report in `corpus/`, replayed against the real booking code.

A report is a JSON file `corpus/readiness_*.json` with three parts:

- `turns`: the tester's evidence as sent - prompt, the reply the agent gave,
  the action trace. Not an expectation; often a record of the bug.
- `shop`: the business it was run against, and the moment it was run.
- `conversations`: the spec. Written by a person and reviewed before it is
  trusted - a tester's "expected" is a view, not a given. Each conversation
  plays in order against one customer; each step names an evidence turn
  (`turn`) or gives a message of our own (`say`, with `added_because`).

Each step's `expect` uses only the keys in `EXPECT_KEYS`. An unknown key
fails the run rather than being ignored, because an assertion that silently
does nothing is the fault this suite exists to catch. What the keys cannot
say goes in a named Python check (`check`, see `CHECKS`) rather than into a
growing schema.

On top of that, every step is held to `invariants` - what must be true of
any turn, including ones nobody has written an expectation for yet.

Adding a report: drop its JSON in `corpus/`, write its `conversations`, run
this file. Nothing here changes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import booking

REPORTS = sorted((Path(__file__).parent / "corpus").glob("readiness_*.json"))

# `analyzer_says` is what the analyzer read on the live turn, passed in the way
# the webhook passes it: {"wants_meeting": true}. It is a reading of the
# message, not a fact, and booking must not trust it further than that.
STEP_KEYS = {"turn", "say", "added_because", "expect", "check", "note", "analyzer_says"}
ANALYZER_KEYS = {"wants_meeting"}
CONVERSATION_KEYS = {"name", "steps", "contact_metadata", "note"}


def _load(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    report["_turns"] = {turn["id"]: turn for turn in report["turns"]}
    return report


def _cases():
    for path in REPORTS:
        report = _load(path)
        for index, conversation in enumerate(report["conversations"]):
            yield pytest.param(path, index, id=f"{path.stem}:{conversation['name']}")


# ------------------------------------------------------------- the clock
class _Clock:
    now: datetime | None = None


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = _Clock.now.astimezone(timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


# ------------------------------------------------------------- invariants
# Written from what a customer and the shop are owed, not from how the code
# decides it, so that a change to the code cannot quietly change them too.
_NO_GO = re.compile(
    r"\b(do not|don'?t|dont)\s+(book|schedule|reserve|confirm)\b|\bnot (yet|ready)\b|"
    r"\b(ask|check with|talk to)\s+my\s+(spouse|wife|husband|partner)\b|\bhold off\b",
    re.IGNORECASE,
)
# What a sentence claiming each action looks like to a person reading it.
_SAYS_BOOKED = re.compile(
    r"\b(is|are|been)\s+(now\s+)?(confirmed|booked|scheduled|reserved|set for)\b|\bscheduled for\b",
    re.IGNORECASE,
)
# A yes, as a person reading the message would take it.
_IS_YES = re.compile(
    r"^\W*(yes|yeah|yep|yup|ok|okay|sure|confirm\w*|go ahead|perfect|great|si|sí|haan|ji)\b",
    re.IGNORECASE,
)
_SAYS_CANCELLED = re.compile(r"\b(cancell?ed|removed|deleted)\b", re.IGNORECASE)
_SAYS_MOVED = re.compile(r"\b(moved|rescheduled)\b", re.IGNORECASE)


def _open_at(shop, start: datetime, end: datetime) -> bool:
    zone = ZoneInfo(shop.timezone)
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    hours = shop.agent_config["business_hours"].get(local_start.strftime("%A").lower())
    if not hours or local_start.date() != local_end.date():
        return False
    return hours["open"] <= local_start.strftime("%H:%M") and local_end.strftime("%H:%M") <= hours["close"]


async def invariants(db, shop, contact, message: str, turn, evidence: dict | None, live):
    now = _Clock.now
    length = timedelta(minutes=booking.duration_minutes(shop, "onsite"))
    where = booking.job_place(contact)

    # Told not to book: nothing is booked.
    if _NO_GO.search(message):
        assert not turn.booked, "booked after being told not to"

    # Nothing is written from the message that asks for it. Only a yes to what
    # was read back books, moves or cancels anything.
    if turn.performed:
        assert _IS_YES.match(message), f"{turn.performed} on {message!r}, which is not a yes"
    if turn.proposed:
        assert turn.performed is None and turn.reply, "a read-back that wrote something, or said nothing"

    # Nothing offered that has passed, that the shop is shut for, or to a job
    # outside the area.
    for slot in turn.offered:
        assert slot > now, f"offered {slot}, which has passed"
        assert _open_at(shop, slot, slot + length), f"offered {slot}, outside opening hours"
    if booking.in_area(shop, where) is False:
        assert turn.offered == [], f"offered times to a job in {where}"
        assert not turn.booked

    # Anything written is in the future, inside the hours, at an address in
    # the area.
    if turn.performed in ("booked", "moved"):
        made = turn.appointment
        starts, ends = booking._aware(made.starts_at), booking._aware(made.ends_at)
        assert starts > now and _open_at(shop, starts, ends), f"wrote {starts} outside hours or in the past"
        if made.kind == "onsite":
            assert made.location, "a site visit with nowhere to go"
            assert booking.in_area(shop, made.location) is not False, f"a visit at {made.location}"

    # One live appointment per customer, at most.
    confirmed = await db.scalar(
        select(func.count(Appointment.id)).where(
            Appointment.contact_id == contact.id, Appointment.status == APPOINTMENT_CONFIRMED
        )
    )
    assert confirmed <= 1, f"{confirmed} live appointments for one customer"

    # The reply they got then: if it said something happened that did not
    # happen now, the guard must refuse it.
    said = (evidence or {}).get("reply_then") or ""
    if said:
        claims = (
            (_SAYS_BOOKED.search(said) and live is None)
            or (_SAYS_CANCELLED.search(said) and not turn.cancelled)
            or (_SAYS_MOVED.search(said) and not turn.moved)
        )
        if claims:
            assert booking.unverified_claims(
                said, appointment=live, cancelled=turn.cancelled, moved=turn.moved
            ), f"the guard lets through: {said!r}"


# ------------------------------------------------------------- named checks
async def move_chains_to_the_original(db, shop, contact, turn, history):
    """A move cancels the old row and points the new one at it: one change, both halves."""
    assert turn.previous is not None, "no previous appointment recorded for the move"
    assert turn.appointment.replaces_id == turn.previous.id
    assert turn.previous.status == APPOINTMENT_CANCELLED


async def is_a_site_visit(db, shop, contact, turn, history):
    """Booked as the visit the shop does, not as a call nobody asked for."""
    assert turn.appointment.kind == "onsite", f"booked a {turn.appointment.kind}"


CHECKS = {
    "move_chains_to_the_original": move_chains_to_the_original,
    "is_a_site_visit": is_a_site_visit,
}


# ------------------------------------------------------------- the runner
async def _rows(db, contact) -> dict:
    async def count(status=None):
        query = select(func.count(Appointment.id)).where(Appointment.contact_id == contact.id)
        if status:
            query = query.where(Appointment.status == status)
        return await db.scalar(query)

    return {
        "confirmed": await count(APPOINTMENT_CONFIRMED),
        "cancelled": await count(APPOINTMENT_CANCELLED),
        "total": await count(),
    }


# One handler per expect key, and the keys a step may use are exactly these.
# A key that is accepted and never asserted is the silent pass this suite is
# here to catch - and it happened: "guard_blocks_reply_then" was written on
# five steps and read by nothing, until a run with the invariants switched
# off showed two reverted fixes going unnoticed. Declaring a key now means
# writing its handler.
class Seen:
    """What a step's expectations are checked against."""

    def __init__(self, db, shop, contact, turn, live, evidence, where):
        self.db, self.shop, self.contact, self.turn = db, shop, contact, turn
        self.live, self.evidence, self.where = live, evidence, where


def _performed(seen, wanted):
    """null | "booked" | "moved" | "cancelled". Required on every step."""
    assert seen.turn.performed == wanted, (
        f"{seen.where}: performed {seen.turn.performed!r}\n{seen.turn.prompt_block}"
    )


def _refusal(seen, wanted):
    """The Refusal reason, or null for none."""
    got = seen.turn.refusal.reason if seen.turn.refusal is not None else None
    assert got == wanted, f"{seen.where}: refusal {got!r}\n{seen.turn.prompt_block}"


def _offered(seen, wanted):
    """"none" | "some"."""
    assert wanted in ("none", "some"), f"{seen.where}: offered is 'none' or 'some'"
    assert bool(seen.turn.offered) == (wanted == "some"), f"{seen.where}: offered {seen.turn.offered}"


def _slot(seen, wanted):
    """"YYYY-MM-DD HH:MM" in the shop's zone, of this turn's appointment."""
    assert seen.turn.appointment is not None, f"{seen.where}: no appointment to have a slot"
    local = booking._aware(seen.turn.appointment.starts_at).astimezone(ZoneInfo(seen.shop.timezone))
    assert local.strftime("%Y-%m-%d %H:%M") == wanted, f"{seen.where}: slot {local}"


def _location_contains(seen, wanted):
    """A substring of this turn's appointment's location."""
    assert seen.turn.appointment is not None, f"{seen.where}: no appointment to have a location"
    assert wanted in (seen.turn.appointment.location or ""), (
        f"{seen.where}: location {seen.turn.appointment.location!r}"
    )


async def _rows_are(seen, wanted):
    """{"confirmed": n, "cancelled": n, "total": n} for this customer, after the step."""
    rows = await _rows(seen.db, seen.contact)
    unknown = set(wanted) - set(rows)
    assert not unknown, f"{seen.where}: rows has only {sorted(rows)}"
    for key, count in wanted.items():
        assert rows[key] == count, f"{seen.where}: {key} rows {rows[key]}, wanted {count}"


def _prompt_contains(seen, wanted):
    """Strings the agent's instructions for this turn must include."""
    for text in wanted:
        assert text in seen.turn.prompt_block, f"{seen.where}: {text!r} not in\n{seen.turn.prompt_block}"


def _proposed(seen, wanted):
    """"book" | "move" | "cancel": what was read back, waiting on a yes. null: nothing was."""
    if wanted is None:
        assert seen.turn.proposed is None, f"{seen.where}: read back {seen.turn.proposed} - nothing should be"
        return
    assert wanted in ("book", "move", "cancel"), f"{seen.where}: proposed is book, move, cancel or null"
    held = seen.turn.proposed or {}
    assert held.get("action") == wanted, f"{seen.where}: proposed {held.get('action')!r}\n{seen.turn.prompt_block}"
    assert seen.turn.reply and "Reply YES" in seen.turn.reply, f"{seen.where}: no read-back sent: {seen.turn.reply!r}"


def _guard_blocks_reply_then(seen, wanted):
    """true: the reply the tester received on this turn is refused by the guard now."""
    assert wanted is True, f"{seen.where}: guard_blocks_reply_then only takes true"
    said = (seen.evidence or {}).get("reply_then")
    assert said, f"{seen.where}: no reply_then in the evidence to check"
    assert booking.unverified_claims(
        said, appointment=seen.live, cancelled=seen.turn.cancelled, moved=seen.turn.moved
    ), f"{seen.where}: the guard lets through {said!r}"


EXPECT = {
    "performed": _performed,
    "refusal": _refusal,
    "offered": _offered,
    "slot": _slot,
    "location_contains": _location_contains,
    "rows": _rows_are,
    "prompt_contains": _prompt_contains,
    "guard_blocks_reply_then": _guard_blocks_reply_then,
    "proposed": _proposed,
}


async def _expect(seen: Seen, expect: dict):
    unknown = set(expect) - set(EXPECT)
    assert not unknown, (
        f"{seen.where}: unknown expect keys {sorted(unknown)} - add a handler to EXPECT or use a check"
    )
    assert "performed" in expect, f"{seen.where}: every step must say what was performed (null for nothing)"
    for key, wanted in expect.items():
        result = EXPECT[key](seen, wanted)
        if hasattr(result, "__await__"):
            await result


@pytest.mark.asyncio
@pytest.mark.parametrize("path, index", list(_cases()))
async def test_conversation(path, index, db_session, monkeypatch):
    report = _load(path)
    conversation = report["conversations"][index]
    assert not set(conversation) - CONVERSATION_KEYS, f"unknown conversation keys in {conversation['name']}"

    _Clock.now = datetime.fromisoformat(report["shop"]["now"])
    monkeypatch.setattr(booking, "datetime", _Frozen)

    shop = Organization(name=report["business"], sales_prompt="")
    shop.timezone = report["shop"]["timezone"]
    shop.agent_config = json.loads(json.dumps(report["shop"]["agent_config"]))
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(
        organization_id=shop.id,
        phone_number="13055550000",
        name="Test customer",
        pipeline_stage="NEW_LEAD",
        qualification={},
        contact_metadata=dict(conversation.get("contact_metadata") or {}),
    )
    db_session.add(contact)
    await db_session.flush()

    history = []
    for number, step in enumerate(conversation["steps"], start=1):
        assert not set(step) - STEP_KEYS, f"unknown step keys {sorted(set(step) - STEP_KEYS)}"
        assert ("turn" in step) != ("say" in step), "a step names an evidence turn or says something, not both"
        if "say" in step:
            assert step.get("added_because"), "a step of our own says why it was added"
        evidence = report["_turns"].get(step["turn"]) if "turn" in step else None
        assert "turn" not in step or evidence, f"no evidence turn {step.get('turn')}"
        message = evidence["prompt"] if evidence else step["say"]
        where = f"{conversation['name']}, step {number} ({step.get('turn') or 'ours'})"
        assert "expect" in step or "check" in step, f"{where}: nothing is asserted"

        said = step.get("analyzer_says") or {}
        assert not set(said) - ANALYZER_KEYS, f"{where}: analyzer_says takes {sorted(ANALYZER_KEYS)}"
        turn = await booking.handle_turn(
            db_session, shop, contact, message, wants_meeting=bool(said.get("wants_meeting"))
        )
        live = await booking.upcoming_for(db_session, contact.id)
        history.append(turn)

        await invariants(db_session, shop, contact, message, turn, evidence, live)
        if "expect" in step:
            await _expect(Seen(db_session, shop, contact, turn, live, evidence, where), step["expect"])
        if "check" in step:
            assert step["check"] in CHECKS, f"{where}: no check called {step['check']!r}"
            await CHECKS[step["check"]](db_session, shop, contact, turn, history)


def test_every_evidence_turn_is_specified():
    """A turn in the evidence with no step is a finding nobody decided about."""
    for path in REPORTS:
        report = _load(path)
        used = {step.get("turn") for c in report["conversations"] for step in c["steps"]}
        missing = sorted(set(report["_turns"]) - used)
        assert not missing, f"{path.name}: no conversation plays {missing}"


@pytest.mark.asyncio
async def test_the_runner_refuses_what_it_does_not_know():
    """An expect key the runner does not read must fail, not pass."""
    seen = Seen(None, None, None, None, None, None, "probe")
    with pytest.raises(AssertionError, match="unknown expect keys"):
        await _expect(seen, {"performed": None, "booked_at": "x"})


def test_every_key_in_the_corpus_has_a_handler():
    for path in REPORTS:
        for conversation in _load(path)["conversations"]:
            for step in conversation["steps"]:
                assert set(step.get("expect", {})) <= set(EXPECT), (path.name, conversation["name"], step)
