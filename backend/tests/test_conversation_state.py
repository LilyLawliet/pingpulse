"""Conversation-state grounding: no repeated greetings, no lost context.

These cover three failures seen in real generated replies:
  * every reply opened with "Hi {name}!"
  * "Walaikum Assalam" returned again when the customer had not greeted
  * "the black pair" resolved to a men's shoe in a conversation about heels
"""

from types import SimpleNamespace

from app.services.llm_service import build_prompt, describe_state

ORG = SimpleNamespace(
    name="Irsa's shoe shop",
    sales_prompt="Sell shoes.",
    target_tone="Warm and confident",
    product_rules="Kitten Heel PKR 8,900. Oxford Black PKR 12,900.",
)
CONTACT = SimpleNamespace(name="Hina", phone_number="+923337712004", pipeline_stage="LEAD")


def turn(sender, content):
    return SimpleNamespace(sender=sender, content=content)


# ------------------------------ greetings ---------------------------------
def test_first_contact_is_told_to_greet():
    state, rules = describe_state([], "Do you have heels?", "Hina")

    assert "first ever message" in state
    assert any("greeting" in rule.lower() and "Hina" in rule for rule in rules)


def test_continuation_is_told_not_to_greet():
    history = [turn("user", "Are the heels leather?"), turn("agent", "Hi Hina! Yes they are.")]

    state, rules = describe_state(history, "How much?", "Hina")

    assert "conversation already in progress" in state
    assert "already greeted" in state
    assert any("Do NOT greet" in rule for rule in rules)


def test_salaam_is_not_returned_when_customer_did_not_greet():
    history = [turn("user", "Salam, do you deliver?"), turn("agent", "Walaikum Assalam, Bilal! Yes.")]

    state, _ = describe_state(history, "What about formal shoes?", "Bilal")

    assert "did not greet you in this message" in state


def test_greeting_from_customer_is_noticed():
    state, _ = describe_state([], "Salam, do you deliver?", "Bilal")

    assert "The customer greeted you in this message." in state


def test_agent_greeting_detected_regardless_of_wording():
    for opener in ("Hi Hina! Yes.", "Hello there.", "Walaikum Assalam, Bilal!", "Hey — sure."):
        _, rules = describe_state(
            [turn("user", "q"), turn("agent", opener)], "and the price?", "X"
        )
        assert any("Do NOT greet" in rule for rule in rules)


# ------------------------------ context -----------------------------------
def test_continuation_carries_reference_resolution_rule():
    history = [
        turn("user", "Are the heels real leather?"),
        turn("agent", "Yes, genuine leather. The Kitten Heel is PKR 8,900."),
    ]

    _, rules = describe_state(history, "I want the black pair.", "Hina")

    joined = " ".join(rules)
    assert "the black pair" in joined
    assert "same category" in joined.lower()


def test_state_lists_what_the_customer_already_asked():
    history = [
        turn("user", "Are the heels real leather?"),
        turn("agent", "Yes."),
        turn("user", "Do you deliver?"),
        turn("agent", "We do."),
    ]

    state, _ = describe_state(history, "I want the black pair.", "Hina")

    assert "Are the heels real leather?" in state
    assert "Do you deliver?" in state


def test_state_quotes_the_agents_last_reply():
    history = [turn("user", "Heels?"), turn("agent", "The Kitten Heel is PKR 8,900.")]

    state, _ = describe_state(history, "I'll take it.", "Hina")

    assert "You last told them: The Kitten Heel is PKR 8,900." in state


def test_prompt_includes_the_state_block_and_rules():
    history = [turn("user", "Are the heels leather?"), turn("agent", "Hi Hina! Yes.")]

    prompt = build_prompt(ORG, CONTACT, history, "I want the black pair.")

    assert "=== CONVERSATION STATE ===" in prompt
    assert "HOW TO REPLY" in prompt
    assert "Do NOT greet" in prompt
    # State must be read before the rules that depend on it.
    assert prompt.index("=== CONVERSATION STATE ===") < prompt.index("HOW TO REPLY")


def test_first_message_prompt_does_not_forbid_greeting():
    prompt = build_prompt(ORG, CONTACT, [], "Do you have heels?")

    assert "Do NOT greet" not in prompt


def test_rules_always_forbid_inventing_products():
    for history in ([], [turn("user", "hi"), turn("agent", "Hello!")]):
        prompt = build_prompt(ORG, CONTACT, history, "anything cheaper?")
        assert "not in the business rules" in prompt
