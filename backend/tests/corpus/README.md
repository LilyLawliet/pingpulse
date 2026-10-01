# Readiness reports

Each `readiness_*.json` here is a tester's report, replayed against the real
booking code by `../test_readiness_replay.py` on every push. A failure there
is a fault somebody has already found once.

## Adding a report

1. **`turns`** — the tester's evidence, as sent: `id`, `prompt`, `reply_then`
   (what the agent answered), `trace_then` (what the Test agent said it would
   have done). This is a record, not a spec — it often *is* the bug.
2. **`shop`** — the business as it should be set up, and `now`, the moment the
   test was run (so "tomorrow" means what it meant to the tester).
3. **`conversations`** — the spec. Each one is played in order against a
   single customer. A step either names an evidence turn (`"turn": "T22"`) or
   is one of ours (`"say": "...", "added_because": "..."`).

A step's `expect` may use only these keys; any other key fails the run:

| key | means |
|---|---|
| `performed` | **required**: `null`, `"booked"`, `"moved"` or `"cancelled"` |
| `refusal` | why nothing happened: `"closed"`, `"past"`, `"needs_address"`, `"contact_invalid"`, `"outside_area"`, `"holding_off"`, `"not_found"`, `"unchanged"`, `"taken"`… or `null` |
| `offered` | `"none"` or `"some"` |
| `slot` | `"2026-10-05 11:00"`, the appointment's start in the shop's time |
| `location_contains` | text the appointment's address must contain |
| `rows` | `{"confirmed": 1, "cancelled": 1, "total": 2}` for that customer afterwards |
| `prompt_contains` | `["10:00 pm"]`: words the agent must have been told |
| `guard_blocks_reply_then` | `true`: the reply they got is one the guard now refuses |
| `proposed` | `"book"`, `"move"` or `"cancel"`: what was read back to them, waiting on a yes; `null`: nothing should be |

What the keys cannot say goes in `"check": "name"`, a Python function in
`CHECKS` in the runner. Do not add keys to say more; add a check.

**Nothing is written from the message that asks for it.** A request is read
back ("To confirm: site visit on Monday 5 October at 10:00 am at … Reply YES
to book it"), so a booking takes two steps: the request, expecting
`"proposed": "book"`, then `"say": "yes"`, expecting `"performed": "booked"`.

Every step is also held to the runner's invariants, whatever its `expect`
says: nothing booked after "don't book", nothing offered in the past or
outside hours or the areas served, no visit without an address, one live
appointment per customer, no false claim in the reply let through.

**A tester's "Expected" is a proposal.** They may write the `expect` blocks;
a person reviews them before they are merged, because once merged they are
what the code is held to.
