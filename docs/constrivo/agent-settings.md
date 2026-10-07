# Constrivo Group — what to put in the agent settings

Three fields, and they do different jobs. Put the right text in each: the
backend reads them for different decisions, and the commonest configuration
fault is writing what the business does into the field meant for how the
agent should talk.

---

## 1. Product rules — *what the business does and where*

This is the field the scope checks read. The services list in it is what
decides whether a request is work Constrivo does, and the state named in it
is what decides whether a property is in the area served. If this field is
empty, neither check can run.

Paste exactly this:

```
Residential and commercial construction and remodeling in Miami and South
Florida. Office at 120 N Compass Way, Dania Beach, Florida 33004. Services:
general construction, home remodeling, kitchen remodeling, bathroom
remodeling, commercial construction, custom home construction, home
additions, interior renovations, exterior renovations, impact windows,
roofing, exterior construction. Constrivo Group handles permitting, local
municipal zoning and environmental reviews, and builds to Miami-Dade HVHZ
(High-Velocity Hurricane Zone) standards. There are no published prices: every
project is priced through a free consultation and a detailed line-item
estimate, with fixed-budget guardrails and no surprise change orders. No
completion dates or typical durations are published; the timeline comes with
the estimate. Financing is available through GreenSky, which sets its own
terms; no rates, loan sizes, repayment lengths or eligibility criteria are
published. Completed projects carry a warranty of between two and five years,
confirmed per project. Customers get a digital portal with daily photo
updates.
```

Three things in that text are load-bearing:

- **"Services: ... ."** — the list between "Services:" and the full stop is
  read as the services Constrivo offers. Keep the commas, keep the full stop,
  and add any new service to this list.
- **"Florida"** — the state has to appear in words. It is compared against
  the state in a customer's address, which is how a property in Seattle is
  refused. "FL" alone is not enough.
- **"no published prices" / "no completion dates"** — these are what let the
  agent say so plainly instead of inventing a number.

---

## 2. Sales prompt — *how the agent should talk*

This field is for tone and behaviour only. Nothing here is treated as fact
about the business.

```
Greet the customer by name and answer their question directly. Ask one thing
at a time; this is a chat, not a form. Work out what the project is, whether
it is residential or commercial, where it is, roughly how big it is, when
they want it done, whether they have plans or photos, and whether they are
interested in financing. Mention GreenSky financing where it fits, without
quoting terms. Close by offering to book a free consultation and taking the
best phone number or email. If you do not know something, say the project
specialist will confirm it.
```

Note what is **not** in it: no prices, no timelines, no services list. Those
live in the product rules, and repeating them here only creates a second
version to go out of date.

---

## 3. Services and service areas — *the structured lists*

Fill these in if the settings screen offers them. They take priority over the
sentence in the product rules, and they are clearer to maintain.

**Services**

```
general construction
home remodeling
kitchen remodeling
bathroom remodeling
commercial construction
custom home construction
home additions
interior renovations
exterior renovations
impact windows
roofing
exterior construction
```

**Service areas**

```
Miami
Miami-Dade County
Broward County
South Florida
Dania Beach
Fort Lauderdale
Key Biscayne
Brickell
Coconut Grove
```

Filling in service areas makes the area check exact rather than a comparison
of states, so a request from Orlando — Florida, but not South Florida — is
handled correctly too.

---

## 4. Opening hours — *needed before anything can be booked*

Constrivo Group does not publish opening hours on its website, and the agent
cannot offer a time without them. They are currently set to Monday to Friday,
09:00–20:00, America/New_York. Confirm with the client that this is right,
because every time the agent offers comes from it.

---

## One thing to check with the client before switching

The product rules in place today say:

> Published timelines: kitchens 4–12 weeks, bathrooms 2–6 weeks, custom homes
> 10–14 months.

**Those figures are not on the website.** Nothing on constrivogroup.com states
a duration for any service — the services page describes scope only. The agent
has been quoting "typically 4–12 weeks" to customers on the strength of this
line alone.

Either they are real, and came from a brochure or from the client directly, in
which case put them back and say where they come from; or they were written
as a plausible-sounding filler, in which case the agent has been giving
customers timeframes the company never published. The text above leaves them
out, because a timeline quoted to a customer is the kind of thing that gets
held against a builder.

Worth one question to the client before this goes in.

---

## What to remove from the current knowledge base

The document in place now contains agent scripting, which is read back to
customers word for word. Lines beginning `OPEN:`, `DISCOVER:` and
`Who it is for:` have all been quoted to a customer in testing — one reply
began `OPEN: 'Absolutely — Constrivo Group handles...'`.

Replace it with `knowledge-base.md` from this folder, which has none of that
in it.
