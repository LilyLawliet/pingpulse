"""The Tallybird test business reads the way its test script says it does.

docs/test-stores/Tallybird_Tests.md promises figures, hours and payment
details; this checks the document gives the agent exactly those, read by the
built-in reader, so the script can't drift from what the software does.
"""

from pathlib import Path

import pytest

from app.services import documents, offers, opening_hours, orders

PACK = Path(__file__).resolve().parents[2] / "docs" / "test-stores" / "Tallybird_POS_Business_Pack.docx"


@pytest.fixture(scope="module")
def prepared():
    text = documents.extract(PACK.name, PACK.read_bytes()).text
    texts = [(PACK.name, text)]
    return offers.Prepared(
        items=offers.read_items(texts), texts=texts, tiers=offers.read_tiers(texts), written=set()
    )


def test_fourteen_products_with_their_units(prepared):
    by_name = {item.name: item for item in prepared.items}
    assert len(prepared.items) == 14
    assert by_name["Growth plan"].sale_unit == "month" and by_name["Growth plan"].price == 6500
    assert by_name["Barcode label rolls"].pack == 10
    delivered = {item.name for item in prepared.items if orders.is_delivered(item)}
    assert delivered == {
        "Thermal receipt printer", "Barcode scanner", "Cash drawer", "Starter hardware kit",
        "Barcode label rolls",
    }


def test_hours_include_the_short_saturday(prepared):
    hours = opening_hours.parse(prepared.texts[0][1])
    assert hours["saturday"] == {"open": "11:00", "close": "15:00"}
    assert "sunday" not in hours


def test_payment_methods_and_the_details_to_send(prepared):
    assert set(orders.accepted_methods(prepared)) == {"Bank transfer", "JazzCash", "Card", "Cash on delivery"}
    assert "0300-1234567" in orders.how_to_pay(prepared, "JazzCash")[0]
    assert "PK36MEZN0001234567890123" in orders.how_to_pay(prepared, "Bank transfer")[0]
    assert "40,000" in orders.how_to_pay(prepared, "Cash on delivery")[0]


def test_the_scripted_sums(prepared):
    quote = offers.quote("25 label rolls", prepared.items)
    assert quote.lines[0].total == 7200
    applied = offers.apply_tiers(
        offers.Decimal(28300), prepared.tiers, "PKR", {"delivery"}, where="in Lahore"
    )
    assert applied.delivery.fee == 500
    two_kits = offers.apply_tiers(offers.Decimal(72000), prepared.tiers, "PKR", {"discount", "delivery"}, where="in Karachi")
    assert two_kits.saving == 3600 and two_kits.delivery.free


def test_when_will_it_arrive(prepared):
    found = offers.rules_for("when will it arrive in Karachi?", prepared.texts)
    assert any("working days" in sentence for sentence in found)
