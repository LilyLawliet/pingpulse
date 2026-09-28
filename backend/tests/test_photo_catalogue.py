"""A price list with a photo on each row, from a kawaii stationery shop.

The photos are what the agent can send; the rows are what it quotes from.
Built from docs/test-stores/make_mikus_catalogue.py so the test and the file
a person uploads by hand are the same document.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import KnowledgeDocument
from app.services import documents, offers, product_search

MAKER = Path(__file__).resolve().parents[2] / "docs" / "test-stores" / "make_mikus_catalogue.py"


@pytest.fixture(scope="module")
def catalogue(tmp_path_factory) -> bytes:
    spec = importlib.util.spec_from_file_location("make_mikus_catalogue", MAKER)
    maker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maker)
    maker.OUT = tmp_path_factory.mktemp("mikus") / "mikus.docx"
    return maker.build().read_bytes()


@pytest.fixture
def items(catalogue):
    return offers.read_items([("mikus", documents.extract("mikus.docx", catalogue).text)])


def _item(items, sku):
    return next(i for i in items if i.sku == sku)


# ---------------------------------------------------------------- reading
def test_a_photo_column_does_not_shift_the_prices(items):
    """The empty-text photo column once pushed every price one column left."""
    assert len([i for i in items if i.sku]) == 15
    assert _item(items, "MK-NB-101").price == Decimal("1250")
    assert _item(items, "MK-PA-901").price == Decimal("1900")


def test_each_picture_is_tied_to_the_row_it_sits_on(catalogue):
    pictures = documents.extract("mikus.docx", catalogue).pictures
    assert len(pictures) == 12, "three rows have no photo"
    assert all(p.content_type == "image/png" for p in pictures)
    assert "MK-NB-101" in pictures[0].row


def test_a_measure_per_piece_is_not_what_the_set_holds(items):
    """"5 m per roll" in a set of 5 rolls is not a 5 m set."""
    tape = _item(items, "MK-TP-401")
    assert tape.content is None and tape.pack == 5


# ---------------------------------------------------------------- counting
@pytest.mark.parametrize(
    "message, sku, units, total",
    [
        ("I want 20 gel pens", "MK-PN-301", 2, "2200"),
        ("I want 25 gel pens", "MK-PN-301", 3, "3300"),
        ("2 sticker sheets please", "MK-ST-501", 1, "600"),
        ("1000 sheets of A4 paper", "MK-PA-901", 2, "3800"),
        ("3 packs of gel pens", "MK-PN-301", 3, "3300"),
        ("How much are 3 of the washi tape?", "MK-TP-401", 3, "2550"),
    ],
)
def test_pieces_inside_a_pack_are_counted_up_to_packs(items, message, sku, units, total):
    line = offers.quote(message, items).lines[0]
    assert line.item.sku == sku
    assert line.units == units and line.total == Decimal(total)


def test_the_a_in_are_is_not_the_number_one(items):
    assert offers.quote("How much are 3 of the washi tape?", items).lines[0].quantity == 3


def test_two_things_joined_by_and_the_are_both_read(items):
    result = offers.quote("I need 2 bunny pencil cases and the cat cafe book", items)
    assert {line.item.sku for line in result.lines} == {"MK-PC-701", "MK-BK-801"}


# ---------------------------------------------------------------- uploading
@pytest.mark.asyncio
async def test_uploaded_photos_become_products_the_agent_can_send(org_a, catalogue, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    response = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("Mikus.docx", catalogue, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["photos_found"] == 12
    assert body["products_found"] >= 15
    assert len(list(tmp_path.iterdir())) == 12

    from app.database import get_db
    from app.main import app

    async for db in app.dependency_overrides.get(get_db, get_db)():
        org_id = (
            await db.execute(
                select(KnowledgeDocument.organization_id).where(KnowledgeDocument.source == "Mikus.docx")
            )
        ).scalars().first()
        assert await product_search.has_photos(db, org_id)
        found = await product_search.find_products(db, org_id, "bunny pencil case")
        assert found and found[0].media_urls[0].startswith("https://shop.example/media/")

        # The price is quoted once, from the table, not again from the photo entry.
        turn = await offers.for_turn(db, _Org(org_id), "How much is the pencil case?")
        assert [line.item.sku for line in turn.quote.lines] == ["MK-PC-701"]
        break


class _Org:
    def __init__(self, id):
        self.id = id
        self.product_rules = ""
        self.default_currency = "PKR"


# ---------------------------------------------------------------- the shop's rules
@pytest.fixture
def tiers(catalogue):
    return offers.read_tiers([("mikus", documents.extract("mikus.docx", catalogue).text)])


def test_an_advance_payment_percentage_is_not_a_discount(tiers):
    """"Orders above PKR 15,000 need 50% advance" was applied as 50% off."""
    assert [t.percent for t in tiers if t.topic == "discount"] == [Decimal("10")]


def test_delivery_charged_by_place_asks_where_when_not_said(tiers):
    applied = offers.apply_tiers(Decimal("2200"), tiers, "PKR", {"delivery"})
    assert applied.delivery is None
    line = " ".join(applied.lines())
    assert "PKR 250 within Karachi" in line and "PKR 350 to the rest of Pakistan" in line


@pytest.mark.parametrize(
    "where, fee",
    [("I'm in Karachi", "250"), ("Please deliver to Lahore", "350"), ("Clifton, Karachi", "250")],
)
def test_the_place_the_customer_names_picks_the_charge(tiers, where, fee):
    applied = offers.apply_tiers(Decimal("2200"), tiers, "PKR", {"delivery"}, where=where)
    assert applied.delivery.fee == Decimal(fee)


def test_free_everywhere_needs_no_question(tiers):
    applied = offers.apply_tiers(Decimal("12000"), tiers, "PKR", {"delivery", "discount"})
    assert applied.delivery.free and applied.saving == Decimal("1200")


# ---------------------------------------------------------------- close, but not the same
def test_a_kind_the_shop_does_not_have_is_said_so(items):
    result = offers.quote("do you sell fountain pens?", items)
    assert result.lines[0].differs == "fountain pens"
    assert result.reply().startswith("We don't have fountain pens")
    assert "no such item" in result.prompt_block()


def test_a_colour_is_not_a_different_kind(items):
    assert offers.quote("pink bunny notebook price", items).lines[0].differs == ""


def test_a_number_in_the_product_s_name_is_not_a_quantity(items):
    line = offers.quote("Do you have the 2027 planner in blue?", items).lines[0]
    assert line.quantity is None and line.total is None


def test_things_named_together_in_an_order_are_one_each(items):
    result = offers.quote(
        "Hi! I need 2 Mochi lined notebooks, 25 gel pens, 1 Bunny Ears pencil case and "
        "The Little Cat Café, gift wrapped please. I'm in Karachi.",
        items,
    )
    assert result.subtotal == Decimal("9550")
    assert offers.quote("the planner and the pencil case, delivered to Lahore", items).subtotal == Decimal("5200")
    assert offers.quote("what's the difference between the planner and the pencil case?", items).subtotal is None


def test_delivered_to_a_place_is_a_delivery_question():
    assert "delivery" in offers.topics_in("the planner, delivered to Lahore")
    assert "delivery" not in offers.topics_in("it was delivered 10 days ago")
