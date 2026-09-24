# Three documents for testing what an upload fills in

Upload these in **Setup → Prices and knowledge**. They are ordinary Word files,
the kind a client actually sends.

| File | What it states | What should happen |
|---|---|---|
| `beluga-handbook-WITH-hours.docx` | hours, services, areas | All three are read and offered. **Mon–Fri 09:00–18:00, Sat 10:00–14:00**, Sunday closed; 3 services; Miami-Dade, Broward, Palm Beach. |
| `beluga-price-list-SECOND-document.docx` | services, areas — **no hours** | Replaces the services and areas. The hours from the first file are **kept**, because this one says nothing about them. |
| `beluga-handbook-NO-hours.docx` | payment and warranty only | Nothing is filled in. It stays searchable and the agent quotes from it. |

## Several documents add up

Each upload replaces the fields it states and leaves alone the fields it does
not. A price list silent about opening hours is not a shop saying it has none,
so uploading a second file is never destructive.

Upload the first, then the second, and check **Hours and booking**: the hours
came from the handbook, the services and areas from the price list, and each
line says which file it came from.

## It fills the form in — saving is what switches things on

The hours are parsed out of prose, and prose read slightly wrong would have the
agent offering real appointments to real customers on the strength of a regular
expression. One look and one click is the whole cost.

If a document disagrees with something you have already saved, yours is kept
and an amber line says so, with a button to take the document's version
instead.

## Set your timezone first

**Setup → Your business → Where you are.** Pick from the list; your own
computer's zone is offered in one click. Every opening time is read against it,
so `09:00` with no timezone means 09:00 UTC — 4am in Miami, which is the fault
this was built to end.

Hours are still read from a document without one, so nothing is lost, but they
cannot be saved until a timezone is set.

## What is deliberately not read

**Never promise**, **pricing rules** and **words that should fetch a person**
stay empty no matter what you upload. Those are instructions to an agent, not
descriptions of a business — no customer handbook contains them, and a parser
reaching for them would be guessing at your policy and writing the guess into
what customers get told.

They are not left as three empty boxes either. **Start from a draft** above
them fills all three with what a careful business in your trade would write,
so the job is deleting the line that does not apply rather than authoring
policy from nothing. It is a convention of the trade, not a reading of your
documents, and it says so.

Where a business already has history, **the words that fetch a person** also
gets suggestions from it: the phrases that really did come up just before
somebody at the business stepped into a conversation. Each one shows how many
handovers it preceded, and how many ordinary chats it turned up in too. A
phrase that appears in both is never offered — a trigger that fires on
ordinary messages is how a business learns to stop reading its alerts.

Nothing from either is applied. Both fill the form in; saving is still yours.

## Getting back

**Check it first** shows the exact text your agent will be handed, before
anything is saved. **Undo last save** puts the previous version back, and
pressing it again brings your change forward — so trying a draft costs
nothing.

## The traps, on purpose

Every file contains "Delivery of fittings takes 2-3 working days" and the
handbook adds "about 45 minutes on site". Neither may be read as opening hours.
`beluga-price-list-SECOND-document.docx` has a **Payment** section directly
under its services, which must not be swallowed into the services list.

The same cases are locked down in
`backend/tests/test_opening_hours_from_documents.py`.
