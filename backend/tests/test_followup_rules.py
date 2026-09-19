"""When an automated nudge must not go out.

Two additions to rules that already covered replies, opt-out, stage and the
cap. A nudge at three in the morning is the same family of failure as an
appointment at one - a real message, sent at an hour nobody would have chosen.
And "still thinking about it?" to a customer with a site visit on Thursday
tells them the business has lost track of them.

Quiet hours defer; everything else refuses. The difference matters: a lead
lost to a clock is still a lead lost.
"""

from datetime import datetime, time, timedelta, timezone

import pytest

from app.models import Appointment, CRMContact, Organization
from app.services import agent_config
from app.tasks import ALREADY_BOOKED, QUIET, refuse_followup


def shop(zone="America/New_York", **config) -> Organization:
    organization = Organization(name="Beluga", sales_prompt="x")
    organization.timezone = zone
    organization.agent_config = config
    return organization


def armed_contact() -> CRMContact:
    """A contact whose follow-up sequence is live and eligible."""
    contact = CRMContact(phone_number="+15550001", sales_stage="QUALIFIED")
    contact.contact_metadata = {"followup_token": "tok"}
    contact.opt_out = False
    return contact


# ------------------------------------------------------------- quiet hours
@pytest.mark.parametrize(
    "local_hour,quiet",
    [(2, True), (5, True), (7, True), (8, False), (12, False), (20, False), (21, True), (23, True)],
)
def test_the_default_quiet_window_is_nine_at_night_to_eight(local_hour, quiet):
    organization = shop()
    zone = agent_config.zone_of(organization)
    at = datetime(2026, 10, 6, local_hour, 30, tzinfo=zone).astimezone(timezone.utc)

    assert agent_config.in_quiet_hours(organization, at) is quiet


def test_a_shop_can_set_its_own_quiet_hours():
    organization = shop(quiet_hours={"start": "18:00", "end": "10:00"})
    zone = agent_config.zone_of(organization)

    at_nine = datetime(2026, 10, 6, 9, 0, tzinfo=zone).astimezone(timezone.utc)
    at_noon = datetime(2026, 10, 6, 12, 0, tzinfo=zone).astimezone(timezone.utc)

    assert agent_config.in_quiet_hours(organization, at_nine) is True
    assert agent_config.in_quiet_hours(organization, at_noon) is False


def test_quiet_hours_are_read_in_the_shops_own_timezone():
    """Three in the morning in New York is eight in the morning in UTC.

    Reading the clock in the wrong zone is how a window meant to protect
    somebody's night lands in the middle of their working day.
    """
    new_york = shop("America/New_York")
    utc = shop("UTC")
    # 23:00 UTC is 7pm in New York: the same instant, and only one of them is
    # a time to leave somebody alone.
    moment = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)

    assert agent_config.in_quiet_hours(utc, moment) is True
    assert agent_config.in_quiet_hours(new_york, moment) is False


def test_a_held_message_is_released_at_the_end_of_the_window():
    organization = shop()
    zone = agent_config.zone_of(organization)
    at_three_am = datetime(2026, 10, 6, 3, 0, tzinfo=zone).astimezone(timezone.utc)

    released = agent_config.next_sendable_time(organization, at_three_am)

    assert released.astimezone(zone).hour == 8
    assert released.astimezone(zone).date() == datetime(2026, 10, 6).date()


def test_late_at_night_is_released_the_next_morning():
    """22:00 on Tuesday releases at 08:00 on Wednesday, not Tuesday."""
    organization = shop()
    zone = agent_config.zone_of(organization)
    at_ten_pm = datetime(2026, 10, 6, 22, 0, tzinfo=zone).astimezone(timezone.utc)

    released = agent_config.next_sendable_time(organization, at_ten_pm)

    assert released.astimezone(zone).hour == 8
    assert released.astimezone(zone).date() == datetime(2026, 10, 7).date()


def test_a_sendable_moment_is_returned_unchanged():
    """So a caller can tell whether anything was deferred by comparing."""
    organization = shop()
    zone = agent_config.zone_of(organization)
    midday = datetime(2026, 10, 6, 12, 0, tzinfo=zone).astimezone(timezone.utc)

    assert agent_config.next_sendable_time(organization, midday) == midday


# ------------------------------------------------- what the follow-up does
def test_a_nudge_in_the_night_is_held_not_dropped(monkeypatch):
    """QUIET is the one refusal the caller acts on rather than obeys."""
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: True)

    refusal = refuse_followup(
        armed_contact(), "tok", 1, manual=False, organization=organization
    )

    assert refusal == QUIET


def test_a_nudge_in_working_hours_goes_out(monkeypatch):
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)

    assert (
        refuse_followup(armed_contact(), "tok", 1, manual=False, organization=organization)
        is None
    )


# --------------------------------------------------- somebody already booked
def test_a_booked_customer_is_not_nudged(monkeypatch):
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)
    appointment = Appointment(
        starts_at=datetime.now(timezone.utc) + timedelta(days=2),
        ends_at=datetime.now(timezone.utc) + timedelta(days=2, hours=1),
        timezone_name="UTC",
        kind="onsite",
    )

    refusal = refuse_followup(
        armed_contact(),
        "tok",
        1,
        manual=False,
        organization=organization,
        appointment=appointment,
    )

    assert refusal == ALREADY_BOOKED


def test_an_operator_may_still_nudge_a_booked_customer(monkeypatch):
    """They have looked at the conversation and decided. The gate is for the
    automatic sequence, which has not."""
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)
    appointment = Appointment(
        starts_at=datetime.now(timezone.utc) + timedelta(days=2),
        ends_at=datetime.now(timezone.utc) + timedelta(days=2, hours=1),
        timezone_name="UTC",
        kind="onsite",
    )

    assert (
        refuse_followup(
            armed_contact(),
            "tok",
            1,
            manual=True,
            organization=organization,
            appointment=appointment,
        )
        is None
    )


def test_a_cancelled_appointment_does_not_hold_the_nudge_back(monkeypatch):
    """`upcoming_for` returns only confirmed rows, so a customer whose visit
    was cancelled is a lead again and may be followed up."""
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)

    assert (
        refuse_followup(
            armed_contact(), "tok", 1, manual=False, organization=organization, appointment=None
        )
        is None
    )


# ----------------------------------------------- the rules that came first
def test_an_opt_out_still_beats_everything(monkeypatch):
    """Including a nudge that was queued before they said STOP."""
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)
    contact = armed_contact()
    contact.opt_out = True

    refusal = refuse_followup(contact, "tok", 1, manual=False, organization=organization)

    assert refusal is not None and refusal != QUIET


def test_a_reply_still_cancels_the_sequence(monkeypatch):
    organization = shop()
    monkeypatch.setattr(agent_config, "in_quiet_hours", lambda *a, **k: False)
    contact = armed_contact()
    contact.contact_metadata = {"followup_token": "a newer token"}

    assert (
        refuse_followup(contact, "tok", 1, manual=False, organization=organization)
        is not None
    )


def test_the_new_gates_do_not_fire_without_the_information():
    """Called the old way - no organization, no appointment - nothing new
    refuses. A worker mid-deploy must not start dropping every nudge."""
    assert refuse_followup(armed_contact(), "tok", 1, manual=False) is None
