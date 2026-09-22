# Two documents for testing what an upload does to opening hours

Upload these in **Setup → What your agent should know**. They are ordinary Word
files, the kind a client actually sends.

Both halves of the behaviour need testing, so there are two files. One states
opening hours; the other deliberately does not, while still containing numbers
that look like times — the trap a careless parser falls into.

| File | What should happen |
|---|---|
| `beluga-handbook-WITH-hours.docx` | Green banner. Hours are read and saved: **Mon–Fri 09:00–18:00, Sat 10:00–14:00**. Sunday stays closed. Booking switches on. |
| `beluga-handbook-NO-hours.docx` | Amber banner: no hours stated, booking stays off. |

## Before you start

Set the business timezone in **Setup**. Hours with no timezone are read as UTC,
so `09:00` for a Miami shop would mean 4am local — which is the fault this was
built to end. If the timezone is unset, the upload reads the hours, refuses to
apply them, and says so in amber. That is the intended answer, not a failure.

## What to check after each upload

**After the WITH-hours file:**

1. Setup → the hours step now shows Mon–Fri 09:00–18:00 and Sat 10:00–14:00.
2. Message the WhatsApp number asking to book. The agent should offer real
   times inside those hours, and nothing on a Sunday.
3. Ask for a Sunday. It should refuse rather than invent a slot.

**After the NO-hours file:**

1. Setup → the hours step is still empty.
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
