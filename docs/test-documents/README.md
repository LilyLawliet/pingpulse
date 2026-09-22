# Two documents for testing what an upload does to opening hours

Upload these in **Setup → Prices and knowledge**. They are ordinary Word files,
the kind a client actually sends.

Both halves of the behaviour need testing, so there are two files. One states
opening hours; the other deliberately does not, while still containing numbers
that look like times — the trap a careless parser falls into.

| File | What should happen |
|---|---|
| `beluga-handbook-WITH-hours.docx` | The hours are read out and **offered**: **Mon–Fri 09:00–18:00, Sat 10:00–14:00**, Sunday closed. They appear already filled in at **Hours and booking**, marked as read from this document. |
| `beluga-handbook-NO-hours.docx` | Nothing is read. Booking stays off and the agent hands booking requests to a person. |

## The document does not switch booking on by itself

It fills the form in. Saving the hours is what switches booking on, and that is
deliberate: the hours are parsed out of prose, and prose read slightly wrong
would have the agent offering real appointments to real customers on the
strength of a regular expression. One look and one click is the whole cost.

## Set your timezone first

**Setup → Your business → Where you are.** Every opening time is read against
it, so `09:00` with no timezone means 09:00 UTC — 4am in Miami, which is the
fault this was built to end.

The hours are still read from the document without one, so nothing is lost, but
they cannot be saved until a timezone is set. The hours step says so in amber
rather than letting you save something that would book people overnight.

## What to check

**After the WITH-hours file:**

1. Go to **Hours and booking**. The grid is already filled: Mon–Fri
   09:00–18:00, Sat 10:00–14:00, Sunday empty. A green line names the file the
   hours came from.
2. Save. The step turns green and booking is on.
3. Message the WhatsApp number asking to book. The agent should offer real
   times inside those hours, and nothing on a Sunday.
4. Ask for a Sunday. It should refuse rather than invent a slot.

**After the NO-hours file:**

1. **Hours and booking** is still empty — no suggestion, nothing prefilled.
2. Ask to book. The agent must **not** offer a time and must **not** claim to
   be a person. It should say a team member will come back to you — and only
   when that alert has somewhere to go.
3. Check your alert inbox: an alert titled *"Someone wants to book and the
   agent cannot"* should have arrived. If the shop has no alert address or
   device configured, the agent is not allowed to promise a callback at all,
   because nothing would be behind the promise.

## The trap, on purpose

`beluga-handbook-NO-hours.docx` contains "Delivery of fittings takes 2-3
working days" and "about 45 minutes on site". Neither may be read as opening
hours. If a future change makes the parser greedier, this file is what catches
it.

The same cases are locked down in
`backend/tests/test_opening_hours_from_documents.py`.
