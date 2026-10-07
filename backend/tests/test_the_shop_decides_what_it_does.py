"""Whether a service is offered is read off the shop's own list, not guessed.

The model may say what the customer appears to want. It may not decide
whether the business does it. A client's retest found the agent accepting
dog grooming far enough into a conversation to confirm a time for it, and
the only thing that had ever said no was a model answering a prompt.

The three answers matter separately:

  True   - it matches something the shop lists
  False  - it matches nothing the shop lists, and names a trade
  None   - the list cannot settle it, so the model's reading is used

None is not a yes. It is the honest answer for "I want remodeling" said to
a remodeller, where there is nothing specific to match and refusing would be
worse than asking.
"""

from __future__ import annotations

import pytest

from app.models import Organization
from app.services import scope

CONSTRIVO = (
    "Residential and commercial construction and remodeling in the Miami / South "
    "Florida area (Dania Beach, FL). Services: general construction, home "
    "remodeling, kitchen remodeling, bathroom remodeling, commercial construction, "
    "custom home construction, home additions, exterior renovations, interior "
    "renovations, and impact windows, roofing and exterior construction. There are "
    "no fixed published prices."
)


def constrivo() -> Organization:
    return Organization(name="Constrivo Group", sales_prompt="x", product_rules=CONSTRIVO)


def test_the_services_are_read_out_of_what_the_business_wrote():
    found = scope.offered_services(constrivo())
    assert "kitchen remodeling" in found
    assert "roofing" in found
    assert "impact windows" in found
    # The sentence after the list is not a service.
    assert not any("published prices" in service for service in found)


def test_the_filled_in_field_wins_over_the_sentence():
    shop = Organization(
        name="Paws",
        sales_prompt="We look after pets.",
        product_rules="Services: kitchen remodeling, bathroom remodeling.",
    )
    shop.agent_config = {"services": ["pet grooming", "dog walking"]}
    assert scope.offered_services(shop) == ["pet grooming", "dog walking"]
    assert scope.supports(shop, "dog grooming") is True
    # The field the owner filled in is the list. The leftover sentence in the
    # product rules is not a second opinion - but it is still something the
    # business has written, so this is left open rather than refused.
    assert scope.supports(shop, "kitchen remodel") is None
    assert scope.supports(shop, "car gearbox repair") is False


@pytest.mark.parametrize(
    "job",
    ["dog grooming", "car gearbox repair", "haircut", "legal advice", "wedding photography"],
)
def test_a_trade_the_shop_does_not_list_is_refused_here(job):
    assert scope.supports(constrivo(), job) is False


@pytest.mark.parametrize(
    "job",
    [
        "kitchen remodel",
        "bathroom renovation",
        "interior renovation",
        "impact window installation",
        "commercial build-out",
    ],
)
def test_work_the_shop_lists_is_accepted_here(job):
    assert scope.supports(constrivo(), job) is True


def test_roofing_meets_a_roof():
    """"roofing" and "roof replacement" are one trade, and the shop does it.

    Stemming to whole words only, this came back False - a business turning
    away work it advertises, which is worse than the bug being fixed.
    """
    assert scope.supports(constrivo(), "roof replacement") is True


@pytest.mark.parametrize("job", ["dog grooming at home", "remodeling", "something residential"])
def test_a_word_half_the_services_share_settles_nothing(job):
    """"home" is in three of these services, so it cannot be what makes a job theirs."""
    assert scope.supports(constrivo(), job) is None


def test_a_business_that_has_listed_nothing_is_not_second_guessed():
    bare = Organization(name="Nothing Written Down", sales_prompt="We help people.")
    assert scope.offered_services(bare) == []
    assert scope.supports(bare, "dog grooming") is None


def test_an_unnamed_job_settles_nothing():
    assert scope.supports(constrivo(), "") is None
    assert scope.supports(constrivo(), None) is None


@pytest.mark.asyncio
async def test_the_check_overrules_a_model_that_says_yes(monkeypatch):
    """The model's own flag is not the verdict where the list has an answer."""
    from app.services import understanding

    async def structured(prompt, timeout):
        return {"job": "dog grooming", "service_fits": True, "place": None, "area_fits": None}

    monkeypatch.setattr(understanding, "structured", structured)
    verdict = await scope.check(constrivo(), "can you groom my dog?")
    assert verdict.job == "dog grooming"
    assert verdict.service_fits is False


@pytest.mark.asyncio
async def test_the_check_overrules_a_model_that_says_no(monkeypatch):
    """And the other way: a client is not told its own trade is not its own."""
    from app.services import understanding

    async def structured(prompt, timeout):
        return {"job": "kitchen remodel", "service_fits": False, "place": None, "area_fits": None}

    monkeypatch.setattr(understanding, "structured", structured)
    verdict = await scope.check(constrivo(), "I want my kitchen redone")
    assert verdict.service_fits is True


@pytest.mark.asyncio
async def test_the_model_is_kept_where_the_list_cannot_decide(monkeypatch):
    from app.services import understanding

    async def structured(prompt, timeout):
        return {"job": "remodeling", "service_fits": True, "place": None, "area_fits": None}

    monkeypatch.setattr(understanding, "structured", structured)
    verdict = await scope.check(constrivo(), "I want remodeling")
    assert verdict.service_fits is True


# --------------------------------------------------------------- any trade
# Nothing above is about construction. The list is whatever the business
# wrote down, so the same code has to work for a shoemaker, a software
# seller, a printer and a wholesaler - including refusing the remodeller's
# dog grooming for all of them, and accepting each one's own trade.
OTHER_TRADES = [
    (
        "handmade shoes",
        "We sell handmade leather shoes, boots, sandals and belts, made to order in Lahore.",
        ["leather boots", "belt repair"],
        ["dog grooming", "roof replacement"],
    ),
    (
        "point of sale software",
        "We provide point of sale software, inventory management, barcode scanning and "
        "sales reporting for small retailers.",
        ["inventory software", "barcode scanner"],
        ["dog grooming", "kitchen remodel"],
    ),
    (
        "stationery",
        "Services: custom stationery printing, wedding invitations, notebooks, greeting cards.",
        ["wedding invitations", "notebook printing"],
        ["car repair", "dog grooming"],
    ),
    (
        "trade supply",
        "We supply electrical cable, conduit, junction boxes and switchgear to trade customers.",
        ["electrical cable", "switchgear"],
        ["haircut", "dog grooming"],
    ),
    (
        "bathrooms",
        "We offer bathroom remodeling, shower replacement, tiling and vanity installation "
        "across Miami.",
        ["shower replacement", "tiling"],
        ["dog grooming", "legal advice"],
    ),
]


@pytest.mark.parametrize(
    "trade,description,theirs,not_theirs",
    OTHER_TRADES,
    ids=[row[0] for row in OTHER_TRADES],
)
def test_any_trade_decides_for_itself(trade, description, theirs, not_theirs):
    shop = Organization(name=trade, sales_prompt=description, product_rules="")
    assert scope.offered_services(shop), f"{trade}: nothing read out of its own words"
    for job in theirs:
        assert scope.supports(shop, job) is True, f"{trade} was told it does not do {job}"
    for job in not_theirs:
        assert scope.supports(shop, job) is False, f"{trade} accepted {job}"


# ------------------------------------------------- a list is in one language
# Found in production, on a screenshot meant to be evidence: a Miami
# remodeller told a Spanish-speaking customer that "remodelación de cocina"
# was not something it did, having listed "kitchen remodeling". The list
# matched nothing because the list is in English, and no words in common was
# read as a different trade.
#
# So the list only decides for requests written the way the list is. Where it
# cannot, the model's reading is used - the same as for a business that has
# listed nothing. That cuts both ways: grooming asked for in Spanish is not
# refused here either. A list in one language cannot be the authority on
# requests in another, and being wrong in that direction costs a client
# their own customers.
SPANISH = [
    ("remodelación de cocina", "Hola, quiero remodelar mi cocina. Pueden ayudarme?"),
    ("remodelación de baño", "Buenas, necesito remodelar mi baño completo"),
    ("peluquería canina", "Hola, pueden bañar y cortar el pelo a mi perro?"),
]


@pytest.mark.parametrize("job,said", SPANISH, ids=[row[0] for row in SPANISH])
def test_a_request_in_another_language_is_not_refused_off_an_english_list(job, said):
    assert scope.supports(constrivo(), job, said) is None


def test_the_customers_own_sentence_decides_the_language():
    """Not the two or three words the model labelled the job with.

    "remodelación de cocina" on its own is too short to tell from English -
    it scores as English and was refused. The sentence it came from is not.
    """
    shop = constrivo()
    said = "Hola, quiero remodelar mi cocina. Pueden ayudarme?"
    assert scope.supports(shop, "remodelación de cocina", said) is None


@pytest.mark.parametrize(
    "job,said",
    [("dog grooming", "Can you groom my dog?"), ("car gearbox repair", "Can you fix my gearbox?")],
)
def test_the_list_still_decides_in_its_own_language(job, said):
    assert scope.supports(constrivo(), job, said) is False


# Grammar is not a trade. A message made only of modal verbs, pronouns and
# words for arranging a time names no work in any industry, and reading one
# as a request stops a booking: "can you come at 3am tomorrow?" left `can`
# behind and the customer was asked what work they needed, one message after
# they had said. Checked against every trade above, because a stopword list
# that only suits a builder is the thing this file exists to prevent.
ARRANGING_ONLY = [
    "can you come at 3am tomorrow?",
    "what times do you have?",
    "could we do it next week?",
    "would Tuesday at 2pm work for you?",
    "how about tomorrow morning?",
    "is that ok?",
    "yes please, book me in",
]


@pytest.mark.parametrize("said", ARRANGING_ONLY, ids=[s[:22] for s in ARRANGING_ONLY])
@pytest.mark.parametrize(
    "trade,description",
    [(row[0], row[1]) for row in OTHER_TRADES] + [("remodelling", None)],
    ids=[row[0] for row in OTHER_TRADES] + ["remodelling"],
)
def test_arranging_a_time_is_never_read_as_naming_work(trade, description, said):
    shop = constrivo() if description is None else Organization(
        name=trade, sales_prompt=description, product_rules=""
    )
    assert scope.names_unmatched_work(shop, said) is False, (
        f"{trade}: {said!r} was read as asking for work the shop does not do"
    )
