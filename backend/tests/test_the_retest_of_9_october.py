"""The confirmed problems from the Constrivo retest of 9 October.

    1. HIGH - "Cancel that Monday October 12 at 11 AM appointment. I do not
       want to reschedule, move it, or book anything else." was answered
       "that is the time it is already booked for" and six other times.
    2. HIGH - the dashboard said alerts were off while its own help text said
       an empty address sends to the owner; the two rules disagreed.
    3. MEDIUM - a Spanish customer was answered in English by the fixed
       replies whenever no model translated them in time.
    5. LOW - a yes after a refusal was answered off-topic, and "dog grooming"
       was first answered "what work do you need?".

Played through the simulator, as the tester did, where the tester used it.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import CRMContact, Organization
from app.services import analyzer, booking, languages, llm_service, outbox, scope, understanding

from .conftest import _session_for
from .test_the_requested_service_holds import ADDRESS, CONSTRIVO, EVERY_DAY, Model

MIAMI = ZoneInfo("America/New_York")


def next_monday():
    day = datetime.now(timezone.utc).astimezone(MIAMI).date() + timedelta(days=1)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


class FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}

    async def get(self, key):
        return self.keys.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key):
        self.keys.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    @asynccontextmanager
    async def connection():
        yield fake

    monkeypatch.setattr(outbox, "_connection", connection)
    return fake


class Simulator:
    """The Test agent page: state carried between turns the way the page carries it."""

    def __init__(self, tenant):
        self.tenant, self.state, self.history = tenant, {}, []

    async def say(self, text):
        answer = (
            await self.tenant.post(
                "/api/v1/agent/simulate",
                json={"message": text, "history": self.history, "booking_state": self.state},
            )
        ).json()
        self.state = answer.get("booking_state") or self.state
        self.history += [
            {"sender": "user", "content": text},
            {"sender": "agent", "content": answer.get("reply") or ""},
        ]
        return answer


async def constrivo_tenant(org_a, monkeypatch, behaviour="honest"):
    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.sales_prompt = CONSTRIVO
    organization.product_rules = CONSTRIVO
    organization.timezone = "America/New_York"
    organization.agent_config = {"business_hours": EVERY_DAY, "appointments": {"min_notice_minutes": 0}}
    await session.flush()
    model = Model("bathroom remodel", "dog grooming", behaviour)
    model.services = scope.offered_services(organization)
    monkeypatch.setattr(understanding, "structured", model.structured)

    async def plain(prompt):
        return "Thanks - noted."

    monkeypatch.setattr(llm_service, "_call_groq", plain)
    return Simulator(org_a)


async def booked_for_monday(chat):
    monday = next_monday()
    await chat.say("I need a bathroom remodel")
    await chat.say(f"The address is {ADDRESS}")
    read_back = await chat.say(f"Can you come {monday:%A %B} {monday.day} at 11 AM?")
    assert read_back["booking"]["performed"] is None
    assert "Reply YES" in read_back["reply"], read_back["reply"]
    booked = await chat.say("YES")
    assert booked["booking"]["performed"] == "booked", booked
    return monday


# ------------------------------------------------- 1. cancel, not a reschedule
@pytest.mark.parametrize(
    "cancel",
    [
        "Cancel that {day} at 11 AM appointment. I do not want to reschedule, move it, or book anything else.",
        "Please cancel my {day} 11 AM appointment, I don't want to move it or rebook.",
        "cancel the {day} 11am visit. don't reschedule and don't book anything else",
        "I want to cancel my appointment on {day} at 11 AM, not move it.",
        # A move word that is not refused at all: they will move it later,
        # themselves. Still a cancellation of the one they named.
        "Cancel my {day} 11 AM appointment please, I'll reschedule another time.",
    ],
)
async def test_a_cancellation_naming_its_own_time_is_a_cancellation(org_a, monkeypatch, cancel):
    chat = await constrivo_tenant(org_a, monkeypatch)
    monday = await booked_for_monday(chat)

    asked = await chat.say(cancel.format(day=f"{monday:%A %B} {monday.day}"))
    assert asked["reply"].startswith("To confirm: cancel your"), asked["reply"]
    assert not asked["booking"]["offered"], "replacement times were offered for a cancellation"
    assert "already booked for" not in asked["reply"]

    done = await chat.say("YES")
    assert done["booking"]["performed"] == "cancelled", done


async def test_a_spanish_cancellation_is_read_through_its_meaning(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    monday = await booked_for_monday(chat)
    real = analyzer.analyse

    async def with_meaning(history, message, stage):
        reading = await real(history, message, stage)
        if "Cancela" in message:
            reading["meaning"] = (
                f"Cancel my appointment on {monday:%A %B} {monday.day} at 11 AM. "
                "I do not want to reschedule it or book anything else."
            )
        return reading

    monkeypatch.setattr(analyzer, "analyse", with_meaning)
    asked = await chat.say(
        f"Cancela mi cita del lunes {monday.day} a las 11. No quiero reprogramarla ni reservar nada más."
    )
    assert "cancel your" in asked["reply"], asked["reply"]
    assert not asked["booking"]["offered"]


def test_a_refused_list_is_refused_whole():
    said = "Cancel it. I do not want to reschedule, move it, or book anything else."
    assert booking.wants_cancel(said) and not booking.wants_move(said)
    assert booking.wants_move("I don't want to cancel, just move it to Tuesday")


# ---------------------------------------------- 2. one answer about alerts
async def test_alerts_report_the_answer_the_agent_acts_on(org_a, monkeypatch):
    from app.services import notifications

    monkeypatch.setattr(notifications, "email_available", lambda: True)
    settings = (await org_a.get("/api/v1/notifications/settings")).json()
    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    assert settings["reachable"] is await notifications.can_reach(session, organization)
    assert settings["email_goes_to"] == await notifications.address_for(session, organization)
    assert settings["email"] == ""  # nothing typed in, and still a true answer about where it goes


# -------------------------------------- 3. Spanish stays Spanish when busy
async def test_a_fixed_reply_once_translated_stays_translated_when_models_are_busy(monkeypatch, redis):
    said = "Quiero hablar con una persona del equipo, por favor. Contéstame en español."
    question = "Would you like me to pass this conversation to the team? Reply YES and I'll hand it over."

    async def translates(prompt, timeout):
        return {"text": "¿Quieres que pase esta conversación al equipo? Responde SÍ y la transfiero."}

    monkeypatch.setattr(understanding, "structured", translates)
    first = await languages.in_customer_language(question, said)
    assert first.startswith("¿Quieres")

    async def busy(prompt, timeout):
        return None  # rate-limited: nothing back in time

    monkeypatch.setattr(understanding, "structured", busy)
    again = await languages.in_customer_language(question, "Hola, quiero hablar con alguien por favor")
    assert again == first, "a Spanish customer got English while the model was busy"


def test_the_language_is_told_without_a_model():
    assert languages.language_key("Quiero hablar con una persona, por favor") == "es"
    assert languages.language_key("Bonjour, je voudrais un devis") == "fr"
    assert languages.language_key("hi there") is None


# ------------------------------------------- 5. after a no, stay on the no
async def test_a_yes_after_a_refusal_restates_it(db_session):
    shop = Organization(name="Constrivo", sales_prompt=CONSTRIVO, product_rules=CONSTRIVO)
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(organization_id=shop.id, phone_number="+1305555", contact_metadata={}, qualification={})
    scope.remember_refusal(contact, "dog grooming")
    turn = await booking.handle_turn(db_session, shop, contact, "YES")
    assert turn.performed is None
    assert turn.reply and "dog grooming is not something we do" in turn.reply, turn.reply
    assert "email" not in turn.reply.lower()


async def test_unlisted_work_named_plainly_is_answered_with_what_the_business_does(db_session, monkeypatch):
    shop = Organization(name="Constrivo", sales_prompt=CONSTRIVO, product_rules=CONSTRIVO)
    shop.timezone = "UTC"
    shop.agent_config = {"business_hours": EVERY_DAY, "appointments": {"min_notice_minutes": 0}}
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(organization_id=shop.id, phone_number="+1305555", contact_metadata={}, qualification={})

    async def busy(prompt, timeout):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(understanding, "structured", busy)
    turn = await booking.handle_turn(db_session, shop, contact, "dog grooming tomorrow at 10am")
    said = (turn.reply or turn.plain_reply(shop) or "").lower()
    assert "what work you need" not in said, said
    assert "which of those" in said and "kitchen remodeling" in said
    assert not turn.offered


async def test_a_cancellation_that_is_really_not_yet_is_still_held(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    monday = await booked_for_monday(chat)
    held = await chat.say(
        f"I might cancel my {monday:%A} appointment but not yet - let me ask my wife first"
    )
    assert not held["reply"].startswith("To confirm: cancel"), held["reply"]
    assert held["booking"]["performed"] is None
