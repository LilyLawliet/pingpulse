"""Alerts that reach somebody when the dashboard is shut.

The agent has always run headless, which is what it is for and also what
creates the problem these tests are about: the moments it cannot handle are
exactly the moments nobody is watching.

Most of what is checked here is restraint. An alerting channel fails by being
too loud far more often than by being too quiet - a person who gets four
buzzes about one angry customer turns the whole thing off, and then misses the
escalation that mattered. So: repeats collapse, unconfigured channels stay
silent rather than erroring, and nothing in here is ever allowed to be the
reason a customer's reply did not go out.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import (
    NOTIFY_KEYS,
    CRMContact,
    Notification,
    Organization,
    PushSubscription,
)
from app.services import notifications




# ------------------------------------------------------------------ settings
def test_a_shop_that_never_opened_the_settings_page_still_gets_the_urgent_ones():
    """A missing config is the defaults, not silence.

    A client who never found this page should still be told when somebody asks
    for a human. That is the one alert whose absence is a business problem
    rather than a preference.
    """
    fresh = Organization(name="New Shop", sales_prompt="Sell.")

    assert notifications.wants(fresh, "escalation") is True
    assert notifications.wants(fresh, "delivery_failure") is True
    # And the noisy ones stay off until somebody asks for them.
    assert notifications.wants(fresh, "new_lead") is False


def test_turning_everything_off_is_honoured():
    """An empty list is a decision, not an absence.

    Falling back to the defaults here would silently switch alerts back on for
    somebody who deliberately turned them all off, which is the kind of thing
    that gets a product uninstalled.
    """
    quiet = Organization(name="Quiet", sales_prompt="Sell.", notify_config={"events": []})

    assert all(notifications.wants(quiet, key) is False for key in NOTIFY_KEYS)


def test_settings_cannot_be_used_to_store_arbitrary_things():
    cleaned = notifications.clean_config(
        {"events": {"escalation": True, "nonsense": True, "booking": False},
         "email": "owner@shop.com"}
    )

    assert "nonsense" not in cleaned["events"]
    assert cleaned["events"]["escalation"] is True
    assert cleaned["events"]["booking"] is False
    assert cleaned["email"] == "owner@shop.com"


def test_an_event_added_later_is_not_silently_off():
    """The reason this is a map and not a list of the ones that are on.

    A shop that had ever opened the settings page would otherwise never
    receive an event added afterwards - they did not turn it off, it was not
    there to turn on - and the first one added this way was the disconnect
    alert, which is the most important thing this can tell anybody.
    """
    saved_before_it_existed = Organization(
        name="Early Adopter",
        sales_prompt="Sell.",
        notify_config={"events": {"escalation": True, "booking": False}, "email": ""},
    )

    assert notifications.wants(saved_before_it_existed, "whatsapp_down") is True
    assert notifications.wants(saved_before_it_existed, "booking") is False
    assert notifications.wants(saved_before_it_existed, "escalation") is True


def test_something_that_is_not_an_email_is_not_stored_as_one():
    """Better empty than a string that will fail silently at send time."""
    assert notifications.clean_config({"email": "just a name"})["email"] == ""
    assert notifications.clean_config({"email": "  "})["email"] == ""
    assert notifications.email_for(Organization(name="X", notify_config={"email": "nope"})) == ""


# -------------------------------------------------------------------- raising
@pytest.mark.asyncio
async def test_the_same_situation_only_tells_you_once(db_session, default_org):
    """Four angry messages in a row is one situation.

    Four buzzes is how a person learns to ignore the buzz, and the next one
    they ignore is the one that mattered.
    """
    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550001", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()

    first = await notifications.raise_alert(
        db_session, default_org, "escalation", "Someone needs a person", "help",
        contact_id=contact.id,
    )
    second = await notifications.raise_alert(
        db_session, default_org, "escalation", "Someone needs a person", "help again",
        contact_id=contact.id,
    )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_two_different_people_are_two_different_situations(db_session, default_org):
    """The cool-off must not silence a second customer.

    Collapsing per organization rather than per contact would mean one angry
    customer muting the next one for half an hour.
    """
    made = []
    for number in ("+15550001", "+15550002"):
        contact = CRMContact(
            organization_id=default_org.id, phone_number=number, pipeline_stage="NEW_LEAD"
        )
        db_session.add(contact)
        await db_session.flush()
        made.append(contact)

    one = await notifications.raise_alert(
        db_session, default_org, "escalation", "t", "b", contact_id=made[0].id
    )
    two = await notifications.raise_alert(
        db_session, default_org, "escalation", "t", "b", contact_id=made[1].id
    )

    assert one is not None and two is not None


@pytest.mark.asyncio
async def test_two_different_events_about_one_person_both_get_through(
    db_session, default_org
):
    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550001", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()

    escalation = await notifications.raise_alert(
        db_session, default_org, "escalation", "t", "b", contact_id=contact.id
    )
    failure = await notifications.raise_alert(
        db_session, default_org, "delivery_failure", "t", "b", contact_id=contact.id
    )

    assert escalation is not None and failure is not None


@pytest.mark.asyncio
async def test_the_cool_off_expires(db_session, default_org, monkeypatch):
    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550001", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()

    old = Notification(
        organization_id=default_org.id,
        contact_id=contact.id,
        event="escalation",
        title="t",
        body="b",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    db_session.add(old)
    await db_session.flush()

    again = await notifications.raise_alert(
        db_session, default_org, "escalation", "t", "b", contact_id=contact.id
    )

    assert again is not None


@pytest.mark.asyncio
async def test_an_event_nobody_asked_for_is_not_recorded(db_session, default_org):
    raised = await notifications.raise_alert(
        db_session, default_org, "new_lead", "A new lead", "someone said hello"
    )

    assert raised is None


@pytest.mark.asyncio
async def test_an_invented_event_is_ignored_rather_than_stored(db_session, default_org):
    assert await notifications.raise_alert(db_session, default_org, "wat", "t", "b") is None


@pytest.mark.asyncio
async def test_raising_an_alert_never_raises(db_session):
    """This is called mid-reply. It may not become the reason one fails."""

    class Broken:
        def add(self, _):
            raise RuntimeError("no session")

        async def flush(self):
            raise RuntimeError("no session")

        async def scalar(self, *_a, **_k):
            raise RuntimeError("no session")

    org = Organization(name="X", sales_prompt="Sell.")
    assert await notifications.raise_alert(Broken(), org, "escalation", "t", "b") is None
    assert await notifications.raise_alert(db_session, None, "escalation", "t", "b") is None


# ------------------------------------------------------------------ delivery
@pytest.mark.asyncio
async def test_nothing_is_sent_when_nothing_is_configured(db_session, default_org):
    """A deployment with no keys has one fewer channel, not a broken one."""
    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    result = await notifications.deliver(db_session, default_org, row)

    assert result == {"push": "not configured", "email": "not configured"}
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_a_configured_push_with_no_devices_says_so(
    db_session, default_org, monkeypatch
):
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    result = await notifications.deliver(db_session, default_org, row)

    assert result["push"] == "no devices"


@pytest.mark.asyncio
async def test_a_dead_subscription_is_dropped_rather_than_retried_forever(
    db_session, default_org, monkeypatch
):
    """A push service saying "gone" is the browser's last word on the matter.

    Keeping the row would mean every future alert waiting on a timeout for a
    device that no longer exists, which slows down the alerts that would have
    arrived.
    """
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    db_session.add(
        PushSubscription(
            organization_id=default_org.id,
            endpoint="https://push.example/gone",
            p256dh="k",
            auth="a",
        )
    )
    await db_session.flush()

    class Gone(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 410})()

    import app.services.notifications as module

    def explode(**_kwargs):
        raise Gone()

    fake = type("M", (), {"webpush": staticmethod(explode), "WebPushException": Gone})
    monkeypatch.setitem(__import__("sys").modules, "pywebpush", fake)

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    result = await module.deliver(db_session, default_org, row)
    await db_session.flush()

    assert result["push"] == "0 of 1"
    left = (await db_session.execute(__import__("sqlalchemy").select(PushSubscription))).scalars().all()
    assert left == []


@pytest.mark.asyncio
async def test_delivery_never_raises(db_session, default_org, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_from", "bot@example")
    default_org.notify_config = {"events": list(NOTIFY_KEYS), "email": "who@example.com"}

    async def explode(*_a, **_k):
        raise RuntimeError("the whole world is down")

    monkeypatch.setattr(notifications, "_send_push", explode)
    monkeypatch.setattr(notifications, "_send_email", explode)

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    result = await notifications.deliver(db_session, default_org, row)

    assert result["push"].startswith("failed")
    assert result["email"].startswith("failed")


# ----------------------------------------------------------------------- API
@pytest.mark.asyncio
async def test_the_settings_endpoint_says_what_the_server_can_actually_do(client, org_a):
    """A switch offered for a channel that cannot send is a broken promise."""
    response = await client.get("/api/v1/notifications/settings", headers=org_a.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["push_available"] is False
    assert body["email_available"] is False
    assert {event["key"] for event in body["events"]} == set(NOTIFY_KEYS)
    assert body["devices"] == 0


@pytest.mark.asyncio
async def test_settings_round_trip(client, org_a):
    saved = await client.put(
        "/api/v1/notifications/settings",
        headers=org_a.headers,
        json={"events": ["booking"], "email": "owner@shop.com"},
    )
    assert saved.status_code == 200, saved.text

    read = await client.get("/api/v1/notifications/settings", headers=org_a.headers)
    body = read.json()

    assert body["email"] == "owner@shop.com"
    assert [e["key"] for e in body["events"] if e["enabled"]] == ["booking"]


@pytest.mark.asyncio
async def test_subscribing_is_refused_when_push_is_not_set_up(client, org_a):
    """503 and a plain sentence, rather than a 500 from the crypto layer."""
    response = await client.post(
        "/api/v1/notifications/subscribe",
        headers=org_a.headers,
        json={"endpoint": "https://push.example/a", "keys": {"p256dh": "k", "auth": "a"}},
    )

    assert response.status_code == 503
    assert "not set up" in response.json()["detail"]


@pytest.mark.asyncio
async def test_resubscribing_replaces_rather_than_accumulates(
    client, org_a, monkeypatch
):
    """A browser reissues its subscription whenever the worker updates.

    Without this, every reload adds an endpoint and one escalation arrives a
    dozen times - which is the failure that makes somebody turn alerts off.
    """
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    payload = {"endpoint": "https://push.example/same", "keys": {"p256dh": "k", "auth": "a"}}
    first = await client.post(
        "/api/v1/notifications/subscribe", headers=org_a.headers, json=payload
    )
    second = await client.post(
        "/api/v1/notifications/subscribe", headers=org_a.headers, json=payload
    )

    assert first.status_code == 201
    assert second.json()["replaced"] is True

    settings_response = await client.get(
        "/api/v1/notifications/settings", headers=org_a.headers
    )
    assert settings_response.json()["devices"] == 1


@pytest.mark.asyncio
async def test_a_subscription_that_is_not_https_is_refused(client, org_a, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    response = await client.post(
        "/api/v1/notifications/subscribe",
        headers=org_a.headers,
        json={"endpoint": "http://push.example/a", "keys": {"p256dh": "k", "auth": "a"}},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_incomplete_subscription_is_refused(client, org_a, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    response = await client.post(
        "/api/v1/notifications/subscribe",
        headers=org_a.headers,
        json={"endpoint": "https://push.example/a", "keys": {}},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unsubscribing_something_we_never_had_is_not_an_error(client, org_a):
    """The browser is asking for a state it is already in."""
    response = await client.post(
        "/api/v1/notifications/unsubscribe",
        headers=org_a.headers,
        json={"endpoint": "https://push.example/never"},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_alerts_need_a_token(client):
    assert (await client.get("/api/v1/notifications/settings")).status_code == 401


@pytest.mark.asyncio
async def test_one_shop_cannot_see_anothers_alerts(client, org_a, org_b):
    await client.put(
        "/api/v1/notifications/settings",
        headers=org_a.headers,
        json={"events": ["booking"], "email": "alpha@example.com"},
    )

    theirs = await client.get("/api/v1/notifications/settings", headers=org_b.headers)

    assert theirs.json()["email"] == ""


@pytest.mark.asyncio
async def test_a_test_alert_ignores_the_cool_off_and_the_switches(client, org_a):
    """Somebody pressing "send a test" twice wants two answers.

    Applying the cool-off here would report success on a second press without
    sending anything, which is precisely the wrong answer to "is it working".
    """
    await client.put(
        "/api/v1/notifications/settings",
        headers=org_a.headers,
        json={"events": [], "email": ""},
    )

    first = await client.post("/api/v1/notifications/test", headers=org_a.headers)
    second = await client.post("/api/v1/notifications/test", headers=org_a.headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    # Both presses were really attempted - the cool-off did not swallow the
    # second and report the first one's success.
    assert first.json()["delivery"]["push"] == "not configured"
    assert second.json()["delivery"]["push"] == "not configured"

    # And neither of them reached anybody, so neither claims to have. This
    # shop has no address and no device: "sent: true" was the button that
    # exists to tell you the truth telling you the opposite.
    body = second.json()
    assert body["sent"] is False
    assert body["status"] == "nowhere"
    assert "nothing was set up" in body["summary"].lower()


# ---------------------------------------------------- the headless path
@pytest.mark.asyncio
async def test_an_escalation_raises_an_alert_with_nobody_watching(
    org_a, db_session, monkeypatch
):
    """The whole reason this subsystem exists.

    A customer demands a manager at nine at night. The agent correctly stops
    replying and waits for a person - and until now, the only way to find that
    out was to have had the dashboard open at that moment. Nothing about this
    path involves a connected browser, which is exactly the point.
    """
    import uuid as uuidlib

    from sqlalchemy import select

    from app.api import webhook
    from app.models import ChannelConfig
    from app.schemas import TwilioWebhookPayload

    channel = ChannelConfig(
        organization_id=uuidlib.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923097209908",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()

    queued: list = []
    monkeypatch.setattr(
        "app.tasks.queue_notification", lambda notification_id: queued.append(notification_id)
    )

    payload = TwilioWebhookPayload(
        From="whatsapp:+15550999",
        To="whatsapp:+923097209908",
        Body="This is unacceptable, let me speak to your manager",
        MessageSid="SM-angry",
    )
    result = await webhook.process_inbound_message(db_session, payload, channel)

    assert result["status"] == "escalated"

    rows = (
        await db_session.execute(
            select(Notification).where(Notification.event == "escalation")
        )
    ).scalars().all()

    assert len(rows) == 1
    assert "manager" in rows[0].body
    assert "waiting for you" in rows[0].body
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_a_broker_outage_does_not_cost_the_customer_their_reply(
    org_a, db_session, monkeypatch
):
    """Queuing is best-effort. Answering is not.

    If Redis is gone, the alert stays written and undelivered - recoverable.
    A raised exception here would instead turn a working agent into one that
    stops answering whenever the broker hiccups.
    """
    import uuid as uuidlib

    from sqlalchemy import select

    from app.api import webhook
    from app.models import ChannelConfig
    from app.schemas import TwilioWebhookPayload

    channel = ChannelConfig(
        organization_id=uuidlib.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923097209908",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()

    def broker_is_down(_notification_id):
        raise RuntimeError("redis is not listening")

    monkeypatch.setattr("app.tasks.queue_notification", broker_is_down)

    payload = TwilioWebhookPayload(
        From="whatsapp:+15550998",
        To="whatsapp:+923097209908",
        Body="I want a refund, this is a complaint",
        MessageSid="SM-refund",
    )
    result = await webhook.process_inbound_message(db_session, payload, channel)

    assert result["status"] == "escalated"
    rows = (await db_session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].sent_at is None


# ------------------------------------------------- the failure nothing sees
@pytest.mark.asyncio
async def test_a_logged_out_number_is_noticed(db_session, default_org):
    """The real one, found in production three days after it happened.

    Somebody logged the number out from their phone. WhatsApp killed the
    session, no messages arrived, and because every other alert in this module
    is triggered by an inbound message, nothing fired. The shop's agent was
    dead and the only symptom was quiet.
    """
    from app.models import Message

    # A shop that has clearly been using WhatsApp, and now has no channel.
    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550001", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        Message(
            organization_id=default_org.id,
            contact_id=contact.id,
            sender="user",
            content="hello",
        )
    )
    await db_session.flush()

    told = await notifications.watch_connections(db_session)

    assert told == 1
    from sqlalchemy import select

    row = (
        await db_session.execute(
            select(Notification).where(Notification.event == "whatsapp_down")
        )
    ).scalars().one()
    assert "no WhatsApp number is connected" in row.body
    assert "pair the number again" in row.body


@pytest.mark.asyncio
async def test_a_shop_that_has_never_set_up_is_not_told_it_is_broken(
    db_session, default_org
):
    """A tenant created an hour ago is not broken, it is new.

    Telling them their number has stopped working would be wrong, and would
    be the first thing they ever heard from us.
    """
    told = await notifications.watch_connections(db_session)

    assert told == 0


@pytest.mark.asyncio
async def test_a_half_paired_session_counts_as_down(db_session, default_org):
    """A channel row is not the same as a working connection.

    The row survives a logout; the session status is what changes. Checking
    only for the row's existence would report a dead number as healthy.
    """
    from app.models import ChannelConfig

    db_session.add(
        ChannelConfig(
            organization_id=default_org.id,
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="QR_SESSION",
            phone_number="+923097209908",
            session_status="LOGGED_OUT",
        )
    )
    await db_session.flush()

    told = await notifications.watch_connections(db_session)

    assert told == 1


@pytest.mark.asyncio
async def test_a_working_number_is_left_alone(db_session, default_org):
    from app.models import ChannelConfig

    db_session.add(
        ChannelConfig(
            organization_id=default_org.id,
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="QR_SESSION",
            phone_number="+923097209908",
            session_status="AUTHENTICATED",
        )
    )
    await db_session.flush()

    assert await notifications.watch_connections(db_session) == 0


@pytest.mark.asyncio
async def test_a_number_that_stays_down_is_mentioned_once_a_day(
    db_session, default_org
):
    """Not every five minutes.

    The first alert is the useful one. The rest exist only so it is not
    forgotten, and at loop frequency they would be the fastest way to get
    somebody to switch alerts off entirely.
    """
    from app.models import ChannelConfig

    db_session.add(
        ChannelConfig(
            organization_id=default_org.id,
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="QR_SESSION",
            phone_number="+923097209908",
            session_status="LOGGED_OUT",
        )
    )
    await db_session.flush()

    first = await notifications.watch_connections(db_session)
    second = await notifications.watch_connections(db_session)

    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_a_shop_that_turned_this_off_is_not_told(db_session, default_org):
    from app.models import ChannelConfig

    default_org.notify_config = {"events": ["escalation"], "email": ""}
    db_session.add(
        ChannelConfig(
            organization_id=default_org.id,
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="QR_SESSION",
            phone_number="+923097209908",
            session_status="LOGGED_OUT",
        )
    )
    await db_session.flush()

    assert await notifications.watch_connections(db_session) == 0


@pytest.mark.asyncio
async def test_the_watcher_never_raises(db_session):
    """It runs in a loop in the app process. It may not take the app with it."""

    class Broken:
        async def execute(self, *_a, **_k):
            raise RuntimeError("the database is gone")

    assert await notifications.watch_connections(Broken()) == 0


# ------------------------------------------------- reaching them without a click
@pytest.mark.asyncio
async def test_the_settings_offer_the_accounts_own_address(client):
    """An empty box is the difference between email working and never working.

    Somebody who will not read a browser prompt will not type an address in
    either. Their own account address is nearly always the right answer, so it
    is offered rather than waited for.
    """
    from tests.conftest import make_tenant

    shop = await make_tenant(client, "owner@realshop.co.uk", "Real Shop")
    response = await client.get("/api/v1/notifications/settings", headers=shop.headers)

    assert response.json()["suggested_email"] == "owner@realshop.co.uk"


@pytest.mark.asyncio
async def test_nothing_is_offered_when_the_account_has_no_real_address(client, org_a):
    """The fixtures, and every account created from an access token.

    Both end up with an address that exists to satisfy a column rather than to
    receive mail, and a suggestion is only useful if it would actually work.
    """
    response = await client.get("/api/v1/notifications/settings", headers=org_a.headers)

    assert response.json()["suggested_email"] == ""


@pytest.mark.asyncio
async def test_a_browser_that_already_said_yes_is_resubscribed_silently(
    client, org_a, monkeypatch
):
    """The half of "no click needed" that browsers actually permit.

    Permission cannot be requested without a gesture, but a browser that has
    already granted it can be resubscribed on every load - after a new tab, a
    cleared service worker, or a subscription the push service rotated. The
    server has to treat that repeat as a replacement rather than a second
    device, or one alert arrives twice.
    """
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    payload = {
        "endpoint": "https://push.example/quiet",
        "keys": {"p256dh": "k", "auth": "a"},
        "label": "Chrome on Windows",
    }
    for _ in range(4):
        response = await client.post(
            "/api/v1/notifications/subscribe", headers=org_a.headers, json=payload
        )
        assert response.status_code in (200, 201), response.text

    after = await client.get("/api/v1/notifications/settings", headers=org_a.headers)
    assert after.json()["devices"] == 1


@pytest.mark.asyncio
async def test_the_dashboard_can_tell_whether_anything_can_reach_them(client, org_a):
    """What the "Alerts off" warning in the header is reading.

    No devices and no address means every alert this system raises is written
    down and delivered to nobody, which looks identical to working.
    """
    before = (await client.get("/api/v1/notifications/settings", headers=org_a.headers)).json()
    assert before["devices"] == 0 and before["email"] == ""

    await client.put(
        "/api/v1/notifications/settings",
        headers=org_a.headers,
        json={"events": {"escalation": True}, "email": "owner@shop.com"},
    )

    after = (await client.get("/api/v1/notifications/settings", headers=org_a.headers)).json()
    assert after["email"] == "owner@shop.com"


def test_a_half_finished_mail_setup_is_not_offered(monkeypatch):
    """Host and address filled in, the app password still to come.

    Offering the switch anyway would mean every alert failing at send time,
    which is precisely what this module exists to never do.
    """
    monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(settings, "smtp_from", "shop@gmail.com")
    monkeypatch.setattr(settings, "smtp_user", "shop@gmail.com")
    monkeypatch.setattr(settings, "smtp_password", "")

    assert notifications.email_available() is False

    monkeypatch.setattr(settings, "smtp_password", "an app password")
    assert notifications.email_available() is True


def test_a_relay_that_needs_no_login_still_counts(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "localhost")
    monkeypatch.setattr(settings, "smtp_from", "bot@shop.local")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")

    assert notifications.email_available() is True


def test_a_placeholder_address_is_not_offered():
    """Accounts created from an access token get a made-up address.

    Offering it would have somebody save something like
    yaha@token.pingpulse.local and then wonder for a week why no alert ever
    arrived. Better to ask for an address than to suggest a dead one.
    """
    assert notifications.usable_address("yaha@token.pingpulse.local") == ""
    assert notifications.usable_address("owner@example.com") == ""
    assert notifications.usable_address("nobody@nowhere.invalid") == ""
    assert notifications.usable_address("not an address") == ""
    assert notifications.usable_address(None) == ""
    assert notifications.usable_address("  owner@realshop.co.uk ") == "owner@realshop.co.uk"


# ------------------------------------------------------- failure and retry
@pytest.mark.asyncio
async def test_a_failed_send_is_not_marked_as_finished(db_session, default_org, monkeypatch):
    """The bug the first real alert hit.

    Gmail's cold TLS handshake took longer than the timeout allowed, the send
    failed, and sent_at was stamped anyway - so the retry read "already sent"
    and returned without trying. One slow handshake lost the alert for good.
    """
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_from", "bot@example")
    default_org.notify_config = {"events": {"escalation": True}, "email": "who@example.com"}

    async def times_out(*_a, **_k):
        raise TimeoutError()

    monkeypatch.setattr(notifications, "_send_email", times_out)

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    result = await notifications.deliver(db_session, default_org, row)

    assert result["email"].startswith("failed")
    assert row.sent_at is None, "a failed send must stay open for a retry"


@pytest.mark.asyncio
async def test_a_retry_does_not_buzz_everybody_again(db_session, default_org, monkeypatch):
    """Push went out, email did not. Only email should be tried again.

    Without this, one transient email failure alongside a delivered push means
    the retry pushes a second time about something they were already told.
    """
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_from", "bot@example")
    default_org.notify_config = {"events": {"escalation": True}, "email": "who@example.com"}

    pushes = []

    async def push(*_a, **_k):
        pushes.append(1)
        return "1 of 1"

    emails = []

    async def email(*_a, **_k):
        emails.append(1)
        return "sent" if len(emails) > 1 else "failed: TimeoutError"

    monkeypatch.setattr(notifications, "_send_push", push)
    monkeypatch.setattr(notifications, "_send_email", email)

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    first = await notifications.deliver(db_session, default_org, row)
    assert first["push"] == "1 of 1" and first["email"].startswith("failed")
    assert row.sent_at is None

    second = await notifications.deliver(db_session, default_org, row)

    assert len(pushes) == 1, "the retry pushed again"
    assert second["push"] == "1 of 1"
    assert second["email"] == "sent"
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_an_unconfigured_channel_is_not_a_failure(db_session, default_org):
    """"not configured" and "no devices" are answers, not errors.

    Treating them as failures would retry every alert twice for a shop that
    has simply not set anything up.
    """
    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    db_session.add(row)
    await db_session.flush()

    await notifications.deliver(db_session, default_org, row)

    assert row.sent_at is not None


# ------------------------------------------------- handed over before it exists
def _run_task(task, argument):
    """Call a Celery task body without wrecking the session's event loop.

    The task calls `asyncio.run`, which closes the loop it made and leaves the
    thread with none. In a worker about to move on that is nothing; in a test
    session the next async test then fails with "no current event loop" for a
    reason that has nothing to do with it.
    """
    import asyncio

    try:
        return task(argument)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_a_row_the_worker_cannot_see_yet_is_not_treated_as_delivered(monkeypatch):
    """The narrowest gap in the whole chain, and the quietest.

    Every caller flushes the notification, hands the id to Celery and commits
    afterwards - sometimes with a websocket fan-out in between. The worker
    reads through its own connection, so until that commit lands the row does
    not exist. Returning "gone" made that a *success*: the task finished, the
    alert was never sent, and nothing anywhere said so.

    In the one production alert on record the worker picked the task up 186ms
    after the row was written. It won. Nothing made it likely to.
    """
    from app import tasks

    async def not_committed_yet(_id):
        raise tasks.NotYetVisible(_id)

    monkeypatch.setattr(tasks, "_run_notification", not_committed_yet)

    asked = {}

    class Retrying(Exception):
        pass

    def retry(**kwargs):
        asked.update(kwargs)
        return Retrying()

    monkeypatch.setattr(tasks.deliver_notification, "retry", retry)

    with pytest.raises(Retrying):
        _run_task(tasks.deliver_notification, "2b0a0e5e-0000-0000-0000-000000000001")

    assert asked, "a row that was not visible yet was quietly dropped"
    # Quickly, and more than twice. A committing transaction needs a moment,
    # not the two minutes a refusing mail server needs.
    assert asked["countdown"] == tasks.NOT_VISIBLE_COUNTDOWN
    assert asked["countdown"] <= 15
    assert asked["max_retries"] == tasks.NOT_VISIBLE_RETRIES


def test_a_refused_mail_server_still_waits_the_long_time(monkeypatch):
    """The two reasons to come back must not collapse into one countdown.

    Five seconds is right for a transaction landing and useless for a mail
    server that is rate-limiting; two minutes is the reverse.
    """
    from app import tasks

    async def mail_refused(_id):
        raise RuntimeError("could not deliver on: email")

    monkeypatch.setattr(tasks, "_run_notification", mail_refused)

    asked = {}

    class Retrying(Exception):
        pass

    def retry(**kwargs):
        asked.update(kwargs)
        return Retrying()

    monkeypatch.setattr(tasks.deliver_notification, "retry", retry)

    with pytest.raises(Retrying):
        _run_task(tasks.deliver_notification, "2b0a0e5e-0000-0000-0000-000000000002")

    assert asked["countdown"] == 120
    assert "max_retries" not in asked, "a mail failure borrowed the quick budget"


@pytest.mark.asyncio
async def test_the_watcher_commits_before_it_tells_the_worker(
    db_session, default_org, monkeypatch
):
    """Here the gap would stay open across every remaining tenant.

    The loop used to commit once at the end, so the first shop's alert was
    handed over and then sat uncommitted while every other organization was
    checked - a window measured in queries, not milliseconds.
    """
    from app.models import Message

    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550009", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        Message(
            organization_id=default_org.id,
            contact_id=contact.id,
            sender="user",
            content="hello",
        )
    )
    await db_session.flush()

    order = []
    real_commit = db_session.commit

    async def watched_commit():
        order.append("commit")
        await real_commit()

    monkeypatch.setattr(db_session, "commit", watched_commit)

    async def handed(_id):
        order.append("queued")

    monkeypatch.setattr(notifications, "_hand_to_worker", handed)

    assert await notifications.watch_connections(db_session) == 1
    assert order.index("commit") < order.index("queued"), (
        "the worker was told about a row that was not committed yet"
    )


@pytest.mark.asyncio
async def test_the_worker_itself_refuses_to_call_a_missing_row_finished(tmp_path, monkeypatch):
    """The line the bug was actually on.

    The test above proves the task retries when told the row is not visible.
    This proves the worker says so in the first place, against a real database
    where the row genuinely is not there.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import tasks
    from app.models import Base

    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/alerts.db"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    monkeypatch.setattr(settings, "database_url", url)

    with pytest.raises(tasks.NotYetVisible):
        await tasks._run_notification("2b0a0e5e-0000-0000-0000-000000000003")


@pytest.mark.asyncio
async def test_a_half_written_delivery_map_does_not_burn_every_retry(
    db_session, default_org, monkeypatch
):
    """A stored map missing a channel must not become a KeyError.

    The report line reads both keys. A row carrying only one raised on the way
    out, the task read that as a delivery failure, retried, raised again, and
    exhausted its budget without ever attempting a send.
    """
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_from", "bot@example")
    default_org.notify_config = {"events": {"escalation": True}, "email": "who@example.com"}

    async def email(*_a, **_k):
        return "sent"

    monkeypatch.setattr(notifications, "_send_email", email)

    row = Notification(
        organization_id=default_org.id, event="escalation", title="t", body="b"
    )
    row.delivery = {"email": "failed: TimeoutError"}
    db_session.add(row)
    await db_session.flush()

    result = await notifications.deliver(db_session, default_org, row)

    assert result["email"] == "sent"
    assert "push" in result, "the missing channel was never filled in"
    assert f"push={result['push']} email={result['email']}"


# ------------------------------------------------ a failure must not spread
@pytest.mark.asyncio
async def test_an_alert_nobody_got_does_not_silence_the_next_one(
    db_session, default_org
):
    """The cool-off counts buzzes, and a failed send was not a buzz.

    Nine at night, a customer demands a manager, the email times out and the
    retries run out. Ten minutes later they demand one again - and the first
    row, which reached nobody, suppressed it. Two escalations, no alerts, and
    the second failure caused by the first.
    """
    default_org.notify_config = {"events": {"escalation": True}}

    stale = Notification(
        organization_id=default_org.id,
        event="escalation",
        title="first",
        body="b",
    )
    db_session.add(stale)
    await db_session.flush()
    # Raised ten minutes ago, delivered to nobody, out of retries.
    stale.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale.sent_at = None
    await db_session.flush()

    again = await notifications.raise_alert(
        db_session, default_org, "escalation", "second", "b"
    )

    assert again is not None, "a failed alert silenced the one that followed it"


@pytest.mark.asyncio
async def test_an_alert_still_on_its_way_does_silence_the_next_one(
    db_session, default_org
):
    """The restraint has to survive the fix.

    An undelivered row a few seconds old is not a failure, it is in flight.
    Letting the next occurrence through would be the double buzz the cool-off
    exists to prevent.
    """
    default_org.notify_config = {"events": {"escalation": True}}

    in_flight = Notification(
        organization_id=default_org.id,
        event="escalation",
        title="first",
        body="b",
    )
    db_session.add(in_flight)
    await db_session.flush()

    again = await notifications.raise_alert(
        db_session, default_org, "escalation", "second", "b"
    )

    assert again is None, "buzzed twice about one thing"


@pytest.mark.asyncio
async def test_an_outage_alert_nobody_got_is_raised_again_tomorrow(
    db_session, default_org
):
    """Otherwise a failed daily alert buys the outage another day of quiet."""
    from app.models import Message

    contact = CRMContact(
        organization_id=default_org.id, phone_number="+15550011", pipeline_stage="NEW_LEAD"
    )
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        Message(
            organization_id=default_org.id,
            contact_id=contact.id,
            sender="user",
            content="hello",
        )
    )
    db_session.add(
        Notification(
            organization_id=default_org.id,
            event="whatsapp_down",
            title="told once",
            body="b",
        )
    )
    await db_session.flush()

    # An hour ago, and it reached nobody.
    from sqlalchemy import select as sa_select

    row = (
        await db_session.execute(
            sa_select(Notification).where(Notification.event == "whatsapp_down")
        )
    ).scalars().one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row.sent_at = None
    await db_session.flush()

    assert await notifications.watch_connections(db_session) == 1


# ------------------------------------------------------ saying it in English
def _row(delivery, *, sent=False, age_minutes=0):
    from datetime import datetime as dt

    row = Notification(event="escalation", title="t", body="b")
    row.delivery = delivery
    row.created_at = dt.now(timezone.utc) - timedelta(minutes=age_minutes)
    row.sent_at = dt.now(timezone.utc) if sent else None
    return row


def test_nobody_is_ever_shown_a_python_exception_name():
    """"failed: TimeoutError" names a class to somebody who wants to know
    whether their phone will buzz tonight."""
    state = notifications.delivery_state(
        _row({"push": "no devices", "email": "failed: TimeoutError"}, age_minutes=60)
    )

    assert "TimeoutError" not in str(state)
    assert "failed:" not in str(state)
    assert state["status"] == "undelivered"
    assert state["summary"] == "Could not be delivered"
    assert "mail server did not answer" in state["detail"]


def test_an_alert_that_reached_nobody_does_not_read_as_sent():
    """The quietest way this feature can let somebody down.

    No device registered and no address given: nothing failed, so the record
    says delivered, and it looked exactly like success.
    """
    state = notifications.delivery_state(
        _row({"push": "no devices", "email": "no address"}, sent=True)
    )

    assert state["status"] == "nowhere"
    assert state["summary"] == "Nothing was set up to receive it"
    assert "no device has been set up" in state["detail"]
    assert "no email address has been given" in state["detail"]


def test_every_device_refusing_is_not_a_success():
    """"0 of 4" does not start with "failed", so it counted as delivered -
    four registered devices and not one of them took it."""
    state = notifications.delivery_state(
        _row({"push": "0 of 4", "email": "no address"}, sent=True)
    )

    assert state["status"] == "undelivered"
    assert "none of your 4 devices accepted it" in state["detail"]


def test_having_nowhere_to_send_it_is_not_the_same_as_being_refused():
    """The two used to share a sentence, and it argued with itself:
    "Nothing was set up to receive it - none of your 3 devices accepted it"."""
    refused = notifications.delivery_state(
        _row({"push": "0 of 3", "email": "no address"}, sent=True)
    )
    nowhere = notifications.delivery_state(
        _row({"push": "no devices", "email": "no address"}, sent=True)
    )

    assert refused["summary"] == "Reached nobody"
    assert nowhere["summary"] == "Nothing was set up to receive it"
    assert refused["summary"] != nowhere["summary"]
    assert "nothing was set up" not in refused["summary"].lower()


def test_one_channel_getting_through_is_enough():
    state = notifications.delivery_state(
        _row({"push": "no devices", "email": "sent"}, sent=True)
    )

    assert state["status"] == "delivered"
    assert state["summary"] == "Reached your email"
    assert state["detail"] is None


def test_both_channels_are_named_when_both_worked():
    state = notifications.delivery_state(
        _row({"push": "2 of 2", "email": "sent"}, sent=True)
    )

    assert state["status"] == "delivered"
    assert state["summary"] == "Reached 2 devices and your email"


def test_one_device_is_not_called_1_devices():
    state = notifications.delivery_state(_row({"push": "1 of 1", "email": "no address"}, sent=True))

    assert state["summary"] == "Reached 1 device"


def test_something_still_on_its_way_is_not_reported_as_lost():
    """A minute-old failure is a retry pending, not a lost alert. Calling it
    "could not be delivered" would have somebody chasing a phantom."""
    state = notifications.delivery_state(
        _row({"push": "no devices", "email": "failed: SMTPException"}, age_minutes=1)
    )

    assert state["status"] == "trying"
    assert state["summary"] == "Still trying"


@pytest.mark.asyncio
async def test_the_dashboard_is_told_how_many_alerts_were_lost(client, org_a, db_session):
    """The badge has to ride along with a call the dashboard already makes."""
    from sqlalchemy import select as sa_select

    from app.models import Organization

    organization = (
        await db_session.execute(
            sa_select(Organization).where(Organization.id == org_a.organization_id)
        )
    ).scalars().one()

    lost = Notification(
        organization_id=organization.id, event="escalation", title="t", body="b"
    )
    db_session.add(lost)
    await db_session.flush()
    lost.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    lost.sent_at = None
    lost.delivery = {"push": "no devices", "email": "failed: TimeoutError"}
    await db_session.commit()

    # And one that finished having reached nobody, which has a sent_at and so
    # was invisible to a query asking for rows without one.
    refused = Notification(
        organization_id=organization.id, event="escalation", title="t2", body="b"
    )
    db_session.add(refused)
    await db_session.flush()
    refused.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    refused.sent_at = datetime.now(timezone.utc) - timedelta(hours=3)
    refused.delivery = {"push": "0 of 2", "email": "no address"}
    await db_session.commit()

    response = await client.get("/api/v1/notifications/settings", headers=org_a.headers)

    assert response.status_code == 200
    # Two, matching what the list shows in red. A badge that disagrees with
    # the list beneath it teaches people to trust neither.
    assert response.json()["undelivered"] == 2
