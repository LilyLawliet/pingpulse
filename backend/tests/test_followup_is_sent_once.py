"""One nudge is one message, however many times the queue delivers the task.

A client received the same check-in forty-four times in eight seconds, all to
one contact, all at 08:00 UTC — which is the end of the default quiet window.

The chain: tasks are acknowledged late, so a worker restart redelivers
whatever it was holding. Each redelivered task, finding itself inside quiet
hours, queued another copy for the moment the window ended. Several restarts
across one night left forty-four copies all timed for 08:00, and at 08:00 they
all came due.

Upstream care would have reduced the count and could not have made it zero: a
broker is allowed to deliver a task twice and no amount of scheduling
discipline changes that. What let it reach the customer was here. Nothing made
the *send* idempotent. `followup_attempts` was written after every send and
never read, so forty-four tasks each carrying attempt 1 passed every check in
turn, and the contact's metadata still read `followup_attempts: 1` afterwards.

So the rule these tests hold down is not "do not queue twice". It is: the
second delivery of the same nudge sends nothing.
"""

from __future__ import annotations

import uuid

import pytest

from app import tasks


def _contact(metadata=None, stage="QUALIFIED"):
    return type(
        "Contact",
        (),
        {
            "id": uuid.uuid4(),
            "contact_metadata": metadata if metadata is not None else {},
            "sales_stage": stage,
            "opt_out": False,
            "consent_at": None,
        },
    )()


# ================================================= the claim, on its own
def test_a_claim_names_the_attempt_not_just_the_sequence():
    """The token identifies a sequence, which is what cancellation needs and
    what the storm walked straight through: every one of those tasks carried
    the live token and attempt 1."""
    assert tasks.claim_key("abc", 1) != tasks.claim_key("abc", 2)


def test_an_unclaimed_nudge_is_not_already_sent():
    assert tasks.already_sent({}, "abc", 1) is False
    assert tasks.already_sent({"followup_sent": []}, "abc", 1) is False


def test_recording_a_claim_makes_it_already_sent():
    metadata = tasks.record_claim({}, "abc", 1)
    assert tasks.already_sent(metadata, "abc", 1) is True
    # A different attempt in the same sequence is still to come.
    assert tasks.already_sent(metadata, "abc", 2) is False


def test_a_new_sequence_is_unaffected_by_an_old_claim():
    """A customer who replies re-arms the sequence with a fresh token, and the
    first nudge of the new one must not be mistaken for the old one."""
    metadata = tasks.record_claim({}, "old-token", 1)
    assert tasks.already_sent(metadata, "new-token", 1) is False


def test_claims_do_not_grow_without_limit():
    """This rides in a JSON column read on every inbound message."""
    metadata: dict = {}
    for attempt in range(100):
        metadata = tasks.record_claim(metadata, "abc", attempt)
    assert len(metadata["followup_sent"]) == tasks.MAX_SENT_CLAIMS
    # The newest are the ones worth keeping.
    assert tasks.already_sent(metadata, "abc", 99) is True


def test_metadata_that_is_not_a_list_does_not_raise():
    """Rows written before this existed, and anything hand-edited."""
    assert tasks.already_sent({"followup_sent": "nonsense"}, "abc", 1) is False


# ============================================== the refusal it produces
def test_a_second_delivery_of_the_same_nudge_is_refused():
    """The case from the incident, in one assertion."""
    metadata = tasks.record_claim({"followup_token": "tok"}, "tok", 1)
    contact = _contact(metadata)

    assert (
        tasks.refuse_followup(contact, "tok", 1, manual=False)
        == tasks.ALREADY_SENT
    )


def test_the_next_nudge_in_the_sequence_is_still_allowed():
    """The guard must stop duplicates without stopping the sequence."""
    metadata = tasks.record_claim({"followup_token": "tok"}, "tok", 1)
    contact = _contact(metadata)

    assert tasks.refuse_followup(contact, "tok", 2, manual=False) is None


def test_cancellation_still_wins_over_everything():
    """A customer who replied gets nothing, claimed or not."""
    contact = _contact({"followup_token": "new"})
    assert tasks.refuse_followup(contact, "old", 1, manual=False) == tasks.CANCELLED


def test_a_manual_nudge_is_also_only_sent_once():
    """An operator pressing a button twice, or one redelivered task, is still
    one message to the customer."""
    metadata = tasks.record_claim({"followup_token": "tok"}, "tok", 1)
    contact = _contact(metadata, stage="NEW")

    assert tasks.refuse_followup(contact, "tok", 1, manual=True) == tasks.ALREADY_SENT


def test_forty_four_deliveries_of_one_nudge_produce_one_send():
    """The incident, replayed.

    Each task claims before sending, so the first wins and the other
    forty-three are refused. Without the claim every one of them passed,
    because they were identical and nothing recorded that one had gone.
    """
    metadata: dict = {"followup_token": "tok"}
    contact = _contact(metadata)

    sent = 0
    for _ in range(44):
        if tasks.refuse_followup(contact, "tok", 1, manual=False) is None:
            contact.contact_metadata = tasks.record_claim(
                dict(contact.contact_metadata), "tok", 1
            )
            sent += 1

    assert sent == 1


# ======================================== what quiet hours hands forward
def test_quiet_hours_defers_rather_than_drops(monkeypatch):
    """Unchanged behaviour, asserted so the fix above cannot quietly remove
    it: a nudge held overnight is still sent in the morning."""
    from types import SimpleNamespace

    organization = SimpleNamespace(
        id=uuid.uuid4(),
        timezone="UTC",
        agent_config={"quiet_hours": {"start": "21:00", "end": "08:00"}},
    )
    from datetime import datetime, timezone as tz

    from app.services import agent_config

    middle_of_the_night = datetime(2026, 9, 25, 2, 0, tzinfo=tz.utc)
    assert agent_config.in_quiet_hours(organization, middle_of_the_night)

    when = agent_config.next_sendable_time(organization, middle_of_the_night)
    assert when.hour == 8


def test_a_deferred_nudge_keeps_its_wording_and_its_kind():
    """The second bug found alongside the storm.

    The quiet-hours re-queue passed four positional arguments and dropped the
    other two, so a nudge an operator had written themselves came back the
    next morning as an automatic one - their wording replaced by the default
    check-in, and subject to the stage gate they had already decided against.

    Asserted against the source of the re-queue, because the failure is a
    silently defaulted argument: there is nothing to catch at runtime, and the
    symptom only appears the morning after a nudge written the night before.
    """
    import inspect

    # Named rather than positional. The task is bound, so `self` is present in
    # one view of the signature and absent in another, and an index here would
    # be asserting something about Celery rather than about this code.
    names = set(inspect.signature(tasks.schedule_customer_followup.__wrapped__).parameters)
    assert {"body", "manual"} <= names

    source = inspect.getsource(tasks._run_followup)
    requeue = source[source.index("if refusal == QUIET") :]
    assert '"body": body_override' in requeue
    assert '"manual": manual' in requeue
