"""Settings that would quietly do nothing are refused at the door.

Every reader of `agent_config` is deliberately forgiving: an unparseable
duration falls back to an hour, a broken timezone falls back to UTC, a day
with no hours is simply shut. That is right at reply time, where the
alternative is a customer left with silence.

It is wrong at save time, and this is the gap these tests close. A shop that
saved `{"monday": "9-5"}` got a 200, stored the string, and had an agent that
never offered an appointment again - while the settings page showed the value
back as though it had taken. Nothing anywhere said no.

The two worst shapes are here as their own tests because neither looks wrong:
a bare string where a list belongs escalates every conversation to a human,
and a day that closes before it opens is a day that can never be booked.
"""

import pytest

from app.services import agent_config


# ----------------------------------------------------------- what passes
def test_an_empty_config_is_valid():
    """A shop that has configured nothing is not a shop configured badly."""
    assert agent_config.validate({}) == []


def test_a_full_and_correct_config_is_valid():
    assert (
        agent_config.validate(
            {
                "business_hours": {
                    "monday": {"open": "09:00", "close": "17:00"},
                    "saturday": {"open": "9:00", "close": "13:00"},
                    "sunday": None,
                },
                "quiet_hours": {"start": "21:00", "end": "08:00"},
                "appointments": {
                    "enabled": True,
                    "duration_minutes": 60,
                    "buffer_minutes": 30,
                    "min_notice_minutes": 120,
                    "default_kind": "onsite",
                    "duration_by_kind": {"phone": 15, "onsite": 90},
                },
                "services": ["Bathroom remodeling"],
                "escalate_on": ["chargeback"],
                "never_promise": "a fixed completion date",
            }
        )
        == []
    )


def test_keys_this_module_does_not_know_are_left_alone():
    """Tenants keep their own notes in here, and some predate every key this
    validator knows. A save that throws away what it does not recognise is a
    worse failure than one that keeps it."""
    assert agent_config.validate({"something_a_tenant_invented": {"x": 1}}) == []


# ------------------------------------------------- shapes that used to crash
@pytest.mark.parametrize(
    "config",
    [
        {"business_hours": "9-5"},
        {"business_hours": {"monday": "9-5"}},
        {"business_hours": {"monday": ["9", "5"]}},
        {"quiet_hours": ["21:00", "08:00"]},
        {"quiet_hours": "21-8"},
        {"appointments": "yes"},
    ],
)
def test_a_shape_that_breaks_the_reply_path_is_refused(config):
    """Each of these raised AttributeError inside the reply path - on the
    customer's message, after it had already arrived."""
    assert agent_config.validate(config), f"{config} was accepted"


def test_the_refusal_says_what_to_type_instead():
    problems = agent_config.validate({"business_hours": {"monday": "9-5"}})
    assert any("09:00" in problem for problem in problems)


# --------------------------------------------------------- the quiet failures
def test_a_list_key_written_as_one_string_is_refused():
    """`escalate_on: "refund"` does not raise. It iterates character by
    character, so every message containing the letter "r" - "hello there" -
    is handed to a human, and nothing in the logs explains why."""
    problems = agent_config.validate({"escalate_on": "refund"})

    assert problems
    assert "escalate_on" in problems[0]


def test_the_string_escalate_on_really_did_fire_on_everything():
    """The behaviour the rule above exists to prevent, asserted directly so
    nobody relaxes the rule without seeing what it costs."""

    class Org:
        timezone = "UTC"
        agent_config = {"escalate_on": "refund"}

    assert agent_config.needs_escalation("hello there", Org()) == "r"
    assert agent_config.needs_escalation("hello there", None) is None


def test_a_misspelled_day_is_refused():
    """Ignored in silence before: a shop that set hours for "mon" had set
    their hours, as far as they knew, and had none."""
    problems = agent_config.validate(
        {"business_hours": {"mon": {"open": "09:00", "close": "17:00"}}}
    )

    assert problems
    assert "monday" in problems[0]


def test_a_day_that_closes_before_it_opens_is_refused():
    """Appointments are never offered across midnight - that is exactly how a
    customer was told 1am. Saving 17:00-09:00 produced a day that looked
    configured and could never be booked."""
    problems = agent_config.validate(
        {"business_hours": {"tuesday": {"open": "17:00", "close": "09:00"}}}
    )

    assert problems
    assert "midnight" in problems[0]


def test_a_missing_close_time_is_refused():
    problems = agent_config.validate({"business_hours": {"friday": {"open": "09:00"}}})

    assert any("close" in problem for problem in problems)


def test_a_day_said_to_be_shut_is_fine():
    """There has to be a way to say "we do not open on Sundays" that is not
    an error."""
    assert agent_config.validate({"business_hours": {"sunday": None}}) == []
    assert agent_config.validate({"business_hours": {"sunday": {}}}) == []


# ------------------------------------------------------------- appointments
@pytest.mark.parametrize(
    "appointments,expected",
    [
        ({"duration_minutes": "soon"}, "number of minutes"),
        ({"duration_minutes": 0}, "between"),
        ({"duration_minutes": 4000}, "between"),
        ({"buffer_minutes": -30}, "between"),
        ({"min_notice_minutes": -1}, "between"),
        ({"enabled": "yes"}, "true or false"),
        ({"default_kind": "telepathy"}, "kind of appointment"),
        ({"duration_by_kind": [15, 90]}, "keyed by kind"),
        ({"duration_by_kind": {"carrier pigeon": 15}}, "kind of appointment"),
        ({"duration_by_kind": {"phone": "quick"}}, "number of minutes"),
        ({"duratoin_minutes": 60}, "no setting called"),
    ],
)
def test_an_appointment_setting_that_cannot_work_is_refused(appointments, expected):
    problems = agent_config.validate({"appointments": appointments})

    assert problems, f"{appointments} was accepted"
    assert any(expected in problem for problem in problems), problems


def test_a_boolean_is_not_a_number_of_minutes():
    """True is an int in Python, and 1 minute is not what anybody meant."""
    assert agent_config.validate({"appointments": {"duration_minutes": True}})


def test_a_negative_buffer_was_accepted_by_the_reader():
    """The reader clamps it to zero and carries on, so the shop that typed it
    never finds out their travel gap was ignored."""
    from app.services import booking

    class Org:
        timezone = "UTC"
        agent_config = {"appointments": {"buffer_minutes": -30}}

    assert booking.buffer_minutes(Org()) == 0
    assert agent_config.validate(Org.agent_config)


# ------------------------------------------------------- everything at once
def test_every_problem_is_reported_together():
    """A form that rejects one field per attempt is how somebody gives up
    halfway through setting their opening hours."""
    problems = agent_config.validate(
        {
            "business_hours": {"monday": "9-5", "funday": {"open": "09:00", "close": "17:00"}},
            "appointments": {"duration_minutes": "soon"},
            "escalate_on": "refund",
        }
    )

    assert len(problems) >= 4


# ---------------------------------------------------------------- the API
@pytest.mark.asyncio
async def test_the_endpoint_refuses_a_config_that_would_do_nothing(org_a):
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"business_hours": {"monday": "9 to 5"}}},
    )

    assert response.status_code == 422
    assert "09:00" in str(response.json()["detail"])


@pytest.mark.asyncio
async def test_a_refused_save_stores_nothing(org_a):
    """The half-written config must not land. A shop retrying after the error
    should be editing what they had, not what the failed attempt left."""
    await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"services": ["Roof repair"]}},
    )

    await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"services": ["Gutters"], "quiet_hours": "21-8"}},
    )

    read = (await org_a.get("/api/v1/agent-config")).json()
    assert read["agent_config"]["services"] == ["Roof repair"]
    assert "quiet_hours" not in read["agent_config"]


@pytest.mark.asyncio
async def test_a_good_config_still_saves(org_a):
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={
            "timezone": "America/New_York",
            "agent_config": {
                "business_hours": {"monday": {"open": "09:00", "close": "17:00"}},
                "appointments": {"duration_minutes": 90, "buffer_minutes": 30},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["agent_config"]["appointments"]["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_the_timezone_is_still_checked_alongside_it(org_a):
    response = await org_a._client.put(
        "/api/v1/agent-config", headers=org_a.headers, json={"timezone": "Mars/Olympus"}
    )

    assert response.status_code == 422


# ------------------------------------------------- hours need a place to be in
# The timezone column defaults to "UTC", so a tenant who never opened the
# setting is indistinguishable from one who chose it. Both live tenants are on
# that default and neither is anywhere near UTC - a Miami remodeller and a
# shoemaker in Pakistan. Hours read in the wrong zone are how a customer was
# offered 1am, so the question gets asked once, at the point it first matters.
@pytest.mark.asyncio
async def test_hours_without_a_timezone_are_refused(org_a):
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}}},
    )

    assert response.status_code == 422
    assert "timezone" in str(response.json()["detail"]).lower()


@pytest.mark.asyncio
async def test_quiet_hours_without_a_timezone_are_refused(org_a):
    """The same trap, and the one that is live right now: a quiet window of
    21:00-08:00 read as UTC is 17:00-04:00 in Miami, which permits a follow-up
    at four in the morning."""
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"quiet_hours": {"start": "21:00", "end": "08:00"}}},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_naming_the_timezone_in_the_same_breath_is_enough(org_a):
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={
            "timezone": "America/New_York",
            "agent_config": {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}},
        },
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_shop_that_really_is_on_utc_says_so_once(org_a):
    """Saying UTC deliberately must be possible, or a London shop can never
    set its hours at all."""
    first = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={
            "timezone": "UTC",
            "agent_config": {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}},
        },
    )
    assert first.status_code == 200


@pytest.mark.asyncio
async def test_an_organization_with_a_timezone_already_set_is_not_asked_again(org_a):
    await org_a._client.put(
        "/api/v1/agent-config", headers=org_a.headers, json={"timezone": "Asia/Karachi"}
    )

    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"business_hours": {"tuesday": {"open": "10:00", "close": "18:00"}}}},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_settings_that_have_nothing_to_do_with_time_are_not_blocked(org_a):
    """Only hours are refused. A shop listing its services has not been asked
    a question about clocks and must not be stopped by one."""
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"services": ["Roof repair"], "never_promise": "same-day work"}},
    )

    assert response.status_code == 200
