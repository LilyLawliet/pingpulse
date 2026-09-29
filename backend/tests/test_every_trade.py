"""Every kind of business we sell to, asked the way its customers ask.

The documents in tests/corpus are written the way owners write them - a table,
"Item - Rs. 500" lines, a menu, a comma-separated export - and the questions
the way customers type: shorthand, typos, Roman Urdu, "per head for 60
people", "half kg". Each expected figure is the owner's own, worked out.

When a client's documents or customers surprise the reader, the fix goes in
the reader and the case goes here, so every other trade is checked against it
from then on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import offers, sales_policy

from .corpus.businesses import BUSINESSES


def _items(kind):
    return offers.read_items([(kind, BUSINESSES[kind])])


# ---------------------------------------------------------------- reading what they wrote
@pytest.mark.parametrize(
    "kind, name, price",
    [
        ("salon", "Haircut (women)", "2500"),   # "Rs." with a full stop
        ("salon", "Keratin treatment", "18000"),  # leader dots
        ("salon", "Pedicure", "2000"),          # two services on one line
        ("gym", "Monthly membership", "6000"),
        ("gym", "Admission fee (one time)", "3000"),
        ("electronics", "Samsung Galaxy A55 128GB", "104999"),  # comma-separated export
        ("dentist", "Filling", "150"),          # "Filling: from $150 per tooth"
        ("grocer", "Basmati rice (5 kg bag)", "2150"),
    ],
)
def test_every_price_is_read_however_it_is_written(kind, name, price):
    found = {i.name: i.price for i in _items(kind)}
    assert found.get(name) == Decimal(price), sorted(found)


@pytest.mark.parametrize(
    "kind, not_a_product",
    [("salon", "Home service"), ("grocer", "Otherwise"), ("bakery", "Minimum order")],
)
def test_a_rule_is_not_read_as_a_product(kind, not_a_product):
    assert not any(i.name.startswith(not_a_product) for i in _items(kind))


def test_a_starting_price_stays_a_starting_price():
    remodel = {i.name: i for i in _items("remodel")}
    assert remodel["Wet room conversion"].starting
    assert remodel["Full bathroom remodel"].starting, "'starts at' is a starting price too"
    assert not remodel["Vanity replacement"].starting
    reply = offers.quote("how much for a walk-in shower?", _items("remodel")).reply()
    assert "from USD 6,800" in reply


# ---------------------------------------------------------------- asking the way people ask
@pytest.mark.parametrize(
    "kind, message, total",
    [
        ("bakery", "2 kg chocolate fudge cake price", "4800"),
        ("bakery", "1.5kg red velvet?", "4200"),         # priced per kg: any amount
        ("bakery", "half kg vanilla cake", "950"),        # the rare word picks the cake
        ("bakery", "can I get 500g chocolate cake", "1200"),
        ("bakery", "2 dozen cupcakes", "3600"),
        ("grocer", "half kg onions", "70"),
        ("grocer", "10 kg basmati rice", "4300"),         # 5 kg bags
        ("grocer", "2 litres milk", "440"),
        ("catering", "silver menu for 60 people", "108000"),
        ("catering", "gold menu 120 guests total?", "312000"),  # guests are heads
        ("saas", "starter for a year?", "348"),           # 12 months
        ("gym", "5 PT sessions price", "12500"),          # PT is personal training
        ("dentist", "filling price for 3 teeth", "450"),  # teeth are tooths
        ("clothing", "2 kurtas how much", "6900"),
        ("electronics", "2 anker chargers", "6998"),
    ],
)
def test_the_amount_asked_for_is_worked_out(kind, message, total):
    result = offers.quote(message, _items(kind))
    assert result.options == [], [i.label for _, m in result.options for i in m]
    assert [line.total for line in result.lines] == [Decimal(total)], result.reply()


@pytest.mark.parametrize(
    "kind, message, name",
    [
        ("salon", "hair cut price for ladies", "Haircut (women)"),   # two words, a synonym
        ("salon", "kitne ka hai keratin?", "Keratin treatment"),      # Roman Urdu
        ("salon", "bridal makeup rate plz", "Bridal makeup package"),
        ("electronics", "redmi note 13 kitne ka", "Xiaomi Redmi Note 13 256GB"),
        ("electronics", "galaxy a55 price", "Samsung Galaxy A55 128GB"),
        ("dentist", "root canal $$?", "Root canal (molar)"),
        ("clothing", "dupatta ki price?", "Chiffon Dupatta"),
        ("remodel", "2 vanities installed", "Vanity replacement"),
    ],
)
def test_the_product_is_found_however_it_is_asked_for(kind, message, name):
    result = offers.quote(message, _items(kind))
    assert [line.item.name for line in result.lines] == [name], result.reply()


def test_both_means_both():
    result = offers.quote("mani pedi dono ka kitna?", _items("salon"))
    assert {line.item.name for line in result.lines} == {"Manicure", "Pedicure"}


def test_a_service_is_not_priced_per_unit():
    reply = offers.quote("how much is a cleaning", _items("dentist")).reply()
    assert "USD 95" in reply and "per unit" not in reply


def test_a_measure_word_is_not_a_kind_of_product():
    assert offers.quote("2 litres milk", _items("grocer")).lines[0].differs == ""


# ---------------------------------------------------------------- the rules around the goods
@pytest.mark.parametrize(
    "kind, message, expected",
    [
        ("bakery", "delivery charges?", "Rs 300 within Karachi"),
        ("clothing", "COD available?", "Cash on delivery available"),
        ("dentist", "do you accept insurance?", "Cigna"),
        ("salon", "advance for bridal?", "50% advance"),
        ("catering", "how much advance to book?", "30% advance"),
    ],
)
def test_the_terms_are_quoted_from_what_they_wrote(kind, message, expected):
    rules = offers.rules_for(message, [(kind, BUSINESSES[kind])])
    assert any(expected in rule for rule in rules), rules


@pytest.mark.parametrize(
    "kind, message, expected",
    [
        ("salon", "do u do home service", "Home service"),
        ("remodel", "is the estimate free?", "Free in-home estimate"),
        ("saas", "free trial?", "free trial"),
    ],
)
def test_with_no_model_the_answer_is_the_sentence_that_answers(kind, message, expected):
    class Chunk:
        content = BUSINESSES[kind]

    reply = sales_policy.deterministic_reply({}, [Chunk()], None, message=message)
    assert expected.lower() in reply.lower(), reply


# ---------------------------------------------------------------- typos and Roman Urdu
@pytest.mark.parametrize(
    "kind, message, name, total",
    [
        ("bakery", "do kg chocolat cake kitne ka?", "Chocolate fudge cake", "4800"),
        ("clothing", "teen kurte chahiye", "Khaddar Kurta", "10350"),
        ("catering", "gold menu 4 ppl", "Gold menu", "10400"),
        ("bakery", "vanila cake 1kg", "Plain vanilla sponge", "1900"),
        ("salon", "keratine price", "Keratin treatment", None),
    ],
)
def test_typos_and_roman_urdu_are_understood(kind, message, name, total):
    result = offers.quote(message, _items(kind))
    assert [line.item.name for line in result.lines] == [name], result.reply()
    assert result.lines[0].total == (Decimal(total) if total else None)
    assert result.lines[0].differs == "", "a typo is not a product we lack"


@pytest.mark.parametrize("message", ["do you have tomatoes?", "do you have dupatta"])
def test_do_in_english_is_not_two(message):
    kind = "grocer" if "tomato" in message else "clothing"
    assert offers.quote(message, _items(kind)).lines[0].quantity is None


def test_aur_joins_two_lines_of_an_order():
    result = offers.quote("do dupatte aur ek kurta", _items("clothing"))
    assert {(l.item.name, l.quantity) for l in result.lines} == {
        ("Chiffon Dupatta", 2), ("Khaddar Kurta", 1)
    }


# ---------------------------------------------------------------- "what do you have?"
@pytest.mark.parametrize(
    "kind, message, includes, excludes",
    [
        ("salon", "what services do you offer?", "Keratin treatment", None),
        ("bakery", "send me your menu", "Red velvet cake", None),
        ("grocer", "kya kya milta hai?", "Tomatoes", None),
        ("clothing", "What items do you have?", "Khaddar Kurta", None),
        ("gym", "what do you have", "Monthly membership", None),
    ],
)
def test_what_do_you_have_is_answered_from_the_list(kind, message, includes, excludes):
    reply = offers.quote(message, _items(kind)).reply()
    assert includes in reply, reply
    assert "What are you looking for?" in reply


def test_what_do_you_have_is_narrowed_by_the_rest_of_the_question():
    reply = offers.quote("what cakes do you have?", _items("bakery")).reply()
    assert "Chocolate fudge cake" in reply and "Red velvet cake" in reply
    assert "Brownies" not in reply and "Cupcakes" not in reply
    books = offers.quote("what books do you have?", _items("salon")).reply()
    assert books == "", "a salon has no books, and none are listed"


def test_a_generic_word_does_not_pick_an_unrelated_passage():
    class Chunk:
        content = "Unused items in their original packaging can be exchanged within 7 days of delivery."

    reply = sales_policy.deterministic_reply({}, [Chunk()], None, message="What items do you have?")
    assert "exchanged" not in reply


@pytest.mark.parametrize(
    "plural, singular",
    [
        # What this rule was written for.
        ("cakes", "cake"),
        ("services", "service"),
        ("sponges", "sponge"),
        # "-es" after a hiss is part of the plural.
        ("boxes", "box"),
        ("dishes", "dish"),
        ("glasses", "glass"),
        ("classes", "class"),
        # "-oes" needs a word in front of it. "shoes" is "shoe": stripping the
        # "es" left "sho", so a shoe shop could not answer "do you have shoes".
        ("shoes", "shoe"),
        ("toes", "toe"),
        ("tomatoes", "tomato"),
        ("potatoes", "potato"),
        ("heroes", "hero"),
        ("echoes", "echo"),
    ],
)
def test_a_plural_finds_what_the_business_wrote_in_the_singular(plural, singular):
    assert offers._stem(plural) == offers._stem(singular) == singular


def test_a_shoe_shop_answers_do_you_have_shoes():
    shop = offers.read_items([(
        "list",
        "Product | SKU | Details | Pack | Price\n"
        "Classic Leather Shoe | YH-100 | brown, mens | 1 pair | PKR 8,500\n",
    )])
    assert [line.item.sku for line in offers.quote("do you have shoes", shop).lines] == ["YH-100"]


def test_the_tail_of_a_rule_is_not_something_we_sell():
    """"...free for orders of PKR 5,000 or more" was read as a product.

    It reached a customer as "Here's some of what we have: ders of: PKR 5,000"
    once "what do you have?" started listing the price list.
    """
    policy = (
        "Delivery within Karachi is PKR 250 for orders below PKR 3,000, and free for "
        "orders of PKR 3,000 or more.\n"
        "Delivery to the rest of Pakistan is PKR 350 for orders below PKR 5,000, and "
        "free for orders of PKR 5,000 or more.\n"
        "Gift wrapping is PKR 150 per item and is not discounted.\n"
    )
    items = offers.read_items([("policy", policy)])
    assert [item.name for item in items] == ["Gift wrapping"]
    assert "ders" not in (offers.quote("What items do you have?", items).reply() or "")


def test_a_real_product_is_still_read_from_prose():
    shop = (
        "Wet room conversion - from USD 9,500\n"
        "Heated flooring - from USD 2,800\n"
        "Manicure - Rs 1,500 | Pedicure - Rs 2,000\n"
    )
    assert {item.name for item in offers.read_items([("shop", shop)])} == {
        "Wet room conversion", "Heated flooring", "Manicure", "Pedicure",
    }
