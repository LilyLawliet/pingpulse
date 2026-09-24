"""A starting draft of the three fields no document can fill in.

Opening hours, services and areas are facts, and `document_facts` reads them
out of a shop's own handbook. What the agent must never promise, how it may
talk about price, and which words should fetch a person are not facts. They
are decisions, they live in somebody's head rather than in any file, and the
setup form asks for them as three empty boxes.

An empty box is the wrong question. It asks a person to *author* policy from
nothing, when the thing they are actually good at is *correcting* a draft -
reading "never promise same-day work" and knowing instantly that they do in
fact offer it on Tuesdays. So each trade gets a draft written the way a
careful person in that trade would write it, and setup becomes deleting the
line that does not apply.

This is not extraction and it does not pretend to be. Nothing here was read
off the shop's documents or inferred from its conversations; it is a
convention of the trade, offered as text the person can see in full before it
governs anything. That distinction is the whole safety argument:

  * It is **shown, never stored.** These endpoints derive and return. The
    config changes when somebody presses save on a form they were looking at,
    same as if they had typed every word.
  * It is **legible.** Three short sentences a person reads in ten seconds,
    not a model's summary of an industry.
  * It is **conservative in the right direction.** A draft that is too strict
    makes the agent decline something it could have sold, and a person notices
    that and loosens it. A draft that is too permissive makes it promise
    something the shop cannot do, and nobody finds out until a customer is
    angry. Every line here errs strict.

Adding a trade is adding a row. It needs no migration, because none of it is
stored against an organization - the shop stores the words it approved, not
the name of the trade it picked them from.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Trade:
    """One trade, and the draft policy a careful shop in it would write."""

    key: str
    label: str
    # What a person recognises themselves in. Shown under the label so picking
    # does not come down to guessing what "professional services" covers.
    examples: str
    never_promise: str = ""
    pricing_rules: str = ""
    escalate_on: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "examples": self.examples,
            "draft": {
                "never_promise": self.never_promise,
                "pricing_rules": self.pricing_rules,
                "escalate_on": list(self.escalate_on),
            },
        }


# The trades, in the order they are offered. Ordered by how often a WhatsApp
# sales agent is actually one of them rather than alphabetically, because the
# list is read top to bottom and the common cases should be found first.
#
# `escalate_on` entries are matched as substrings against the customer's own
# lowercased words by `agent_config.needs_escalation`, so they are written as
# fragments that survive being embedded in a sentence - "water damage" rather
# than "we have water damage". Single common words are avoided: a trigger that
# fires on every third message teaches a shop to ignore the alerts, and then
# the one that mattered is ignored too.
TRADES: tuple[Trade, ...] = (
    Trade(
        key="home_improvement",
        label="Home improvement and trades",
        examples="Bathroom and kitchen remodeling, plumbing, electrical, roofing, flooring",
        never_promise=(
            "A firm price before somebody has seen the space. "
            "Same-day or next-day start dates. "
            "That existing tiling, pipework or wiring can be reused. "
            "A completion date on a job that has not been surveyed."
        ),
        pricing_rules=(
            "Quote ranges, never a single figure, until a site visit has happened. "
            "Always say whether the callout or survey fee is refundable against the work. "
            "Materials and labour are quoted separately."
        ),
        escalate_on=(
            "water damage", "leak", "flooding", "burst", "no hot water",
            "gas", "electric shock", "insurance claim", "warranty claim",
            "unfinished", "still not finished", "damaged my",
        ),
    ),
    Trade(
        key="retail",
        label="Retail and ecommerce",
        examples="Clothing, footwear, jewellery, electronics, homeware",
        never_promise=(
            "That an item is in stock without checking. "
            "A delivery date faster than the courier's stated range. "
            "That a size or colour can be swapped after dispatch. "
            "Restocking of a sold-out item."
        ),
        pricing_rules=(
            "Quote the listed price only. Discounts and codes come from a person. "
            "Always state delivery cost alongside the item price rather than at checkout. "
            "Say plainly when a price excludes duty or shipping."
        ),
        escalate_on=(
            "never arrived", "wrong item", "wrong size", "faulty", "broken",
            "return", "exchange", "chargeback", "damaged in transit",
            "still waiting", "tracking",
        ),
    ),
    Trade(
        key="beauty",
        label="Beauty and wellness",
        examples="Salon, barber, spa, nails, aesthetics, massage",
        never_promise=(
            "A specific result from a treatment. "
            "That a named stylist or therapist is free without checking the diary. "
            "That a treatment is safe during pregnancy or on a given skin condition. "
            "Correcting somebody else's work sight unseen."
        ),
        pricing_rules=(
            "Quote from the price list only, and say that longer or thicker hair may cost more. "
            "Course and package prices come from a person. "
            "State the deposit and the cancellation window with every booking."
        ),
        escalate_on=(
            "reaction", "allergic", "burn", "burnt", "infection", "swelling",
            "pregnant", "ruined", "patch test",
        ),
    ),
    Trade(
        key="health",
        label="Clinics and health practices",
        examples="Dental, physiotherapy, optical, veterinary, private GP",
        never_promise=(
            "A diagnosis, or any opinion on what a symptom means. "
            "That a treatment will work, or how long recovery takes. "
            "That a specific clinician will be the one seen. "
            "That a treatment is covered by an insurer."
        ),
        pricing_rules=(
            "Quote consultation fees only. Treatment prices follow an assessment. "
            "Never estimate what an insurer will pay. "
            "State whether the consultation fee comes off the treatment cost."
        ),
        escalate_on=(
            "emergency", "severe pain", "bleeding", "chest pain",
            "can't breathe", "cannot breathe", "accident", "urgent",
            "prescription", "results", "insurance",
        ),
    ),
    Trade(
        key="automotive",
        label="Motor trade",
        examples="Garage, bodyshop, MOT and servicing, car sales, valeting",
        never_promise=(
            "A repair price before the vehicle has been looked at. "
            "That a fault is what the customer thinks it is. "
            "A collection time that depends on a part arriving. "
            "That a used vehicle has no history the checks have not covered."
        ),
        pricing_rules=(
            "Diagnostic fee first, quote after. Never estimate a repair from a description. "
            "Parts and labour are quoted separately. "
            "Say whether the diagnostic fee comes off the repair."
        ),
        escalate_on=(
            "accident", "broke down", "breakdown", "not safe", "unsafe",
            "failed mot", "still faulty", "came back", "warranty",
            "finance", "part exchange",
        ),
    ),
    Trade(
        key="property",
        label="Property",
        examples="Estate and letting agency, property management, short lets",
        never_promise=(
            "That a viewing slot is held before it is confirmed. "
            "That an offer will be accepted or passed on the same day. "
            "Rental approval, or any view on whether somebody will pass referencing. "
            "That a property is still available without checking."
        ),
        pricing_rules=(
            "Quote the listed asking price or rent only. Never speculate on what a seller would take. "
            "State every fee - admin, deposit, holding - with the rent, not after. "
            "Negotiation happens with a person."
        ),
        escalate_on=(
            "offer", "eviction", "deposit", "solicitor", "survey",
            "mortgage", "chain", "repairs needed", "mould", "damp",
            "no heating", "landlord",
        ),
    ),
    Trade(
        key="food",
        label="Food and hospitality",
        examples="Restaurant, café, catering, bakery, takeaway",
        never_promise=(
            "That a dish is free from an allergen. "
            "A table at a time the diary has not confirmed. "
            "A delivery time during a service rush. "
            "That a special or seasonal item is available today."
        ),
        pricing_rules=(
            "Menu prices only. Event and catering quotes come from a person. "
            "State the minimum spend and any service charge up front. "
            "Say when a price is per head rather than total."
        ),
        escalate_on=(
            "allergy", "allergic", "intolerance", "gluten", "nut",
            "food poisoning", "ill", "sick", "hair in", "cold food",
            "never arrived", "large party",
        ),
    ),
    Trade(
        key="professional",
        label="Professional services",
        examples="Accountancy, legal, consultancy, agency, tuition",
        never_promise=(
            "Advice on a specific situation. That is what the consultation is for. "
            "An outcome, a timescale for one, or any view on the merits of a case. "
            "A fixed fee before scope is agreed. "
            "That a deadline can still be met."
        ),
        pricing_rules=(
            "Quote the consultation fee and the rate. Project fees follow a scope call. "
            "Never estimate a total from a description of the work. "
            "Say plainly whether the rate excludes tax and disbursements."
        ),
        escalate_on=(
            "deadline", "court", "hmrc", "irs", "tax bill", "penalty",
            "investigation", "dispute", "complaint", "negligence",
            "urgent", "notice",
        ),
    ),
    Trade(
        key="general",
        label="Something else",
        examples="A sensible starting point for any business that takes enquiries",
        never_promise=(
            "A price that has not been confirmed by a person. "
            "A date or time that is not in the diary. "
            "That something is in stock, available or possible without checking. "
            "That somebody will call back within a stated number of minutes."
        ),
        pricing_rules=(
            "Quote only prices that are written down. Anything else goes to a person. "
            "Give ranges rather than single figures where the work varies. "
            "State what a price excludes."
        ),
        escalate_on=(
            "refund", "cancel my", "complaint", "still waiting",
            "no one replied", "nobody replied", "urgent", "wrong",
        ),
    ),
)

BY_KEY = {trade.key: trade for trade in TRADES}

# The fields a trade draft is allowed to fill. Deliberately the complement of
# `agent_config.DOCUMENT_FIELDS`: a document states facts, a trade suggests
# policy, and nothing is filled by both. Where they overlapped, the later
# source would silently win and nobody could say which had.
DRAFT_FIELDS = ("never_promise", "pricing_rules", "escalate_on")


def listing() -> list[dict]:
    """Every trade and its draft, for a person choosing between them."""
    return [trade.as_dict() for trade in TRADES]


def for_trade(key: str | None) -> Trade | None:
    """One trade by key, or None. Unknown keys are not an error.

    A client sending a key this build does not have is asking for a draft, and
    the right answer is no draft rather than a 400 - the form behind it works
    perfectly well with three empty boxes, which is what it had before.
    """
    if not key:
        return None
    return BY_KEY.get(str(key).strip().lower())
