"""The model reads; the code checks. Both halves, with the model faked.

Each test hands the checks an answer a real model could give - including the
wrong ones: a price it made up, a product that is not in the file, a quantity
the customer never wrote - and asserts what survives.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.services import offers, understanding

from .corpus.businesses import BUSINESSES

BAKERY = BUSINESSES["bakery"]


@pytest.fixture
def model(monkeypatch):
    """A model that answers with whatever the test sets, and records prompts."""
    answers: dict[str, dict] = {}
    prompts: list[str] = []

    async def fake(prompt, timeout):
        prompts.append(prompt)
        for marker, answer in answers.items():
            if marker in prompt:
                return answer
        return None

    monkeypatch.setattr(understanding, "structured", fake)
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    return answers, prompts


# ---------------------------------------------------------------- reading a file
@pytest.mark.asyncio
async def test_what_the_model_reads_is_kept_only_if_the_file_says_it(model):
    answers, _ = model
    answers["to build its price list"] = {
        "items": [
            {"name": "Chocolate fudge cake", "price": 2400, "sold_as": "kg",
             "priced_per_measure": True},
            {"name": "Red velvet cake", "price": 2900, "sold_as": "kg"},          # made-up price
            {"name": "Lemon drizzle cake", "price": 1900, "sold_as": "kg"},       # not in the file
            {"name": "Cupcakes", "price": 1800, "sold_as": "dozen"},
        ],
        "rules": [
            {"topic": "delivery", "sentence": "Delivery: Rs 300 within Karachi for orders under Rs 5,000.",
             "max_order": 4999.99, "fee": 300, "place": "within Karachi"},
            {"topic": "discount", "sentence": "Orders over Rs 10,000 get 10% off.",   # not in the file
             "min_order": 10000, "percent": 10},
        ],
    }
    read = await understanding.read_document("bakery.txt", BAKERY, "PKR")

    assert read["read_by"] == "model"
    assert [i["name"] for i in read["items"]] == ["Chocolate fudge cake", "Cupcakes"]
    reasons = " ".join(d["why"] for d in read["dropped"])
    assert "2900 is not a price written in the file" in reasons
    assert "Lemon drizzle cake: the name is not in the file" in reasons
    assert len(read["rules"]) == 1 and read["rules"][0]["fee"] == "300"
    # "max_order" 4999.99 is not written in the sentence, so it is not applied.
    assert read["rules"][0]["max_order"] is None
    cake = understanding.as_items(read["items"], "bakery.txt")[0]
    assert cake.by_measure and cake.content == (Decimal("1"), "kg")


@pytest.mark.asyncio
async def test_no_model_means_the_reader(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    read = await understanding.read_document("bakery.txt", BAKERY, "PKR")
    assert read["read_by"] == "reader"
    assert any(i["name"] == "Red velvet cake" for i in read["items"])


@pytest.mark.asyncio
async def test_a_model_reading_that_survives_nothing_falls_back_to_the_reader(model):
    answers, _ = model
    answers["to build its price list"] = {"items": [{"name": "Invented thing", "price": 5}], "rules": []}
    read = await understanding.read_document("bakery.txt", BAKERY, "PKR")
    assert read["read_by"] == "reader" and read["items"]


# ---------------------------------------------------------------- reading a message
def _ids(kind="bakery"):
    items = offers.read_items([(kind, BUSINESSES[kind])])
    _, ids = understanding.product_lines(items)
    return items, ids


def test_an_id_the_list_does_not_have_is_not_quoted():
    _, ids = _ids()
    read = understanding.check_reading({"lines": [{"product": "P99", "quantity": 2}]}, ids, "2 cakes")
    assert read["lines"] == [] and "unknown product" in read["rejected"][0]


def test_a_quantity_the_customer_did_not_write_is_not_used():
    items, ids = _ids()
    key = next(k for k, i in ids.items() if i.name == "Brownies")
    read = understanding.check_reading(
        {"lines": [{"product": key, "quantity": 12, "counted_in": "unit"}]}, ids, "some brownies please"
    )
    (item, wanted), = read["lines"]
    assert item.name == "Brownies" and wanted.quantity is None


def test_numbers_in_words_and_roman_urdu_count():
    items, ids = _ids()
    key = next(k for k, i in ids.items() if i.name == "Chocolate fudge cake")
    read = understanding.check_reading(
        {"lines": [{"product": key, "quantity": 2, "counted_in": "kg"}]}, ids, "do kg chocolate cake"
    )
    (_, wanted), = read["lines"]
    assert wanted.measure == (Decimal(2), "kg")


def test_an_order_value_they_did_not_name_is_dropped():
    _, ids = _ids()
    read = understanding.check_reading({"order_value": 5000}, ids, "is delivery free?")
    assert read["order_value"] is None


@pytest.mark.asyncio
async def test_the_whole_turn_uses_the_model_s_reading(model, org_a):
    """Pieces in a pack, as the model reads them; packs, as the code counts them."""
    from app.models import Organization

    answers, prompts = model
    text = "Item | Sale Unit | Pack | Price (PKR)\nPastel gel pens | pack | 10 | 1,100\nA5 notebook | notebook | 1 | 1,250\n"
    items = offers.read_items([("list", text)])
    listing, ids = understanding.product_lines(items)
    pens = next(k for k, i in ids.items() if "pens" in i.name)
    answers["You read one customer message"] = {
        "lines": [{"product": pens, "quantity": 25, "counted_in": "pieces", "as_written": "25 pens"}],
        "topics": [],
    }
    prepared = offers.Prepared(items, [("list", text)], [], set())
    reading = await understanding.read_message("I'd like 25 of your pens", items)
    turn = offers.finish(prepared, Organization(name="Shop", sales_prompt="x"), "I'd like 25 of your pens", [], reading)
    (line,) = turn.quote.lines
    assert line.units == 3 and line.total == Decimal("3300")
    assert turn.quote.read_by == "model"
    assert "P1 |" in prompts[-1], "the model was not shown the catalogue with ids"


@pytest.mark.asyncio
async def test_what_the_model_says_is_not_stocked_is_said(model):
    answers, _ = model
    items, ids = _ids()
    answers["You read one customer message"] = {"lines": [], "not_stocked": ["gluten-free bread"]}
    reading = await understanding.read_message("do you have gluten-free bread?", items)
    prepared = offers.Prepared(items, [("b", BAKERY)], [], set())
    from app.models import Organization

    turn = offers.finish(prepared, Organization(name="B", sales_prompt="x"), "do you have gluten-free bread?", [], reading)
    assert "We don't have gluten-free bread." in turn.reply()


# ---------------------------------------------------------------- upload keeps it
@pytest.mark.asyncio
async def test_upload_keeps_the_checked_reading(model, org_a):
    answers, _ = model
    answers["to build its price list"] = {
        "items": [{"name": "Cupcakes", "price": 1800, "sold_as": "dozen"}],
        "rules": [],
    }
    response = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("bakery.txt", BAKERY.encode(), "text/plain")},
    )
    body = response.json()
    assert response.status_code == 201, body
    assert body["catalogue"]["read_by"] == "model" and body["products_found"] == 1

    listed = (await org_a.get("/api/v1/knowledge/catalogue")).json()
    assert listed[0]["source"] == "bakery.txt" and listed[0]["items"][0]["name"] == "Cupcakes"

    await org_a.delete("/api/v1/knowledge/sources", params={"source": "bakery.txt"})
    assert (await org_a.get("/api/v1/knowledge/catalogue")).json() == []


@pytest.mark.asyncio
async def test_the_owner_corrects_and_confirms_and_the_agent_quotes_the_correction(model, org_a):
    answers, _ = model
    answers["to build its price list"] = {"items": [{"name": "Cupcakes", "price": 1800, "sold_as": "dozen"}], "rules": []}
    await org_a._client.post(
        "/api/v1/knowledge/upload", headers=org_a.headers,
        files={"file": ("bakery.txt", BAKERY.encode(), "text/plain")},
    )
    (reading,) = (await org_a.get("/api/v1/knowledge/catalogue")).json()
    items = reading["items"]
    items[0]["price"] = "1950"
    corrected = await org_a.patch(f"/api/v1/knowledge/catalogue/{reading['id']}", json={"items": items})
    assert corrected.status_code == 200 and corrected.json()["status"] == "confirmed"

    bad = await org_a.patch(
        f"/api/v1/knowledge/catalogue/{reading['id']}", json={"items": [{"name": "Cupcakes", "price": "free"}]}
    )
    assert bad.status_code == 422

    answers["You read one customer message"] = {"lines": [{"product": "P1", "quantity": 2, "counted_in": "unit"}]}
    body = (await org_a.post("/api/v1/agent/simulate", json={"message": "2 dozen cupcakes"})).json()
    assert body["quote"]["lines"][0]["total"] == "PKR 3,900", body["quote"]


@pytest.mark.asyncio
async def test_another_shop_s_catalogue_cannot_be_read_or_changed(model, org_a, org_b):
    answers, _ = model
    answers["to build its price list"] = {"items": [{"name": "Cupcakes", "price": 1800}], "rules": []}
    await org_a._client.post(
        "/api/v1/knowledge/upload", headers=org_a.headers,
        files={"file": ("bakery.txt", BAKERY.encode(), "text/plain")},
    )
    (reading,) = (await org_a.get("/api/v1/knowledge/catalogue")).json()
    assert (await org_b.get("/api/v1/knowledge/catalogue")).json() == []
    stolen = await org_b.patch(f"/api/v1/knowledge/catalogue/{reading['id']}", json={"status": "confirmed"})
    assert stolen.status_code == 404


@pytest.mark.asyncio
async def test_a_place_the_terms_do_not_reach_goes_to_the_team(model):
    """"Rest of Pakistan" covers Lahore and not Dubai: language, read by the model."""
    from app.models import Organization

    answers, prompts = model
    text = BUSINESSES["bakery"]
    items = offers.read_items([("b", text)])
    tiers = offers.read_tiers([("b", text)])
    prepared = offers.Prepared(items, [("b", text)], tiers, set())
    answers["You read one customer message"] = {
        "lines": [], "topics": ["delivery"], "place": "Dubai", "place_covered": False,
    }
    reading = await understanding.read_message(
        "do you deliver to Dubai?", items, terms=offers.delivery_terms(prepared)
    )
    assert "within Karachi" in prompts[-1], "the model was not shown the delivery terms"
    turn = offers.finish(prepared, Organization(name="B", sales_prompt="x"), "do you deliver to Dubai?", [], reading)
    assert turn.quote.unknown_place == "Dubai"
