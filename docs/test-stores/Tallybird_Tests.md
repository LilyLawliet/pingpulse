# Tallybird POS: test script

A made-up software company in Lahore that sells point-of-sale and inventory
software to small shops, plus the hardware to run it. It covers every feature
in one business:

- plans priced per month and per user;
- add-ons and one-time services;
- delivered hardware with delivery by city;
- annual billing, discounts, and payment by method and amount;
- refunds and a free trial;
- demos booked in the chat;
- orders;
- languages;
- things it must not make up.

Upload **Tallybird_POS_Business_Pack.docx**, never this file. This file holds
the right answers, and the agent would read them too. Every name, number and
bank detail in it is invented.

## The business

| | |
|---|---|
| **Business name** | `Tallybird POS` |
| **Category** | `Professional services` (it's software, and there is no "Software" category) |
| **What you sell** | `Point-of-sale and inventory software for small shops, plus receipt printers, barcode scanners and cash drawers` |
| **How it should sell** | `Answer the question first, then help them pick the right plan. Offer a demo for the Business plan. Quote only the prices in the price list.` |
| **Tone** | `Friendly, clear and confident, like a good product specialist. Short messages, plain words, no jargon, never pushy.` |
| **Where you are** | `Asia/Karachi` |

## Setup (do these in order)

1. **Make a new client** for this. Don't reuse Miku's or Northstar's, or their
   documents will be mixed in.
2. **Your business:** enter the name, category, what you sell, how it should sell and tone above. The step ticks once *What you sell* is filled in.
3. **Prices and knowledge:** upload the .docx. It should find **14 products
   with prices**: 9 plans, add-ons and services, and 5 hardware items. There
   are no photos, on purpose.
   - **What your agent will quote:** check the list. The plans are "per
     month", the extra user is "per user", and the label rolls are "pack of 10".
     Press **Looks right**.
4. **Where you are:** `Asia/Karachi`.
5. **Hours and booking:** the hours from the document are offered to you:
   Mon–Fri 10:00 AM–6:00 PM, Sat 11:00 AM–3:00 PM, Sun closed. Check them and
   press **Save rules**. Until you do, nobody can book a demo.
6. **Setup → Calendar → Meetings:**
   - Held as: **Video call**
   - Length: **30** minutes
   - Meeting room link: `https://meet.google.com/tly-demo-now`
   - Press **Save meeting settings**.
7. **Alerts:** turn on at least one way to be told (email or this device), so
   you can see order, booking and hand-over alerts arrive.
8. **Connect WhatsApp** to test from a phone. Everything except alerts and
   WhatsApp messages also works on the **Test agent** page.

## How to judge

- **Fail** if any of these happen:
  - a figure that isn't in the document or worked out from it;
  - a feature, integration, date or discount the document doesn't state;
  - "noted", "placed" or "booked" when nothing was;
  - any photo offered, since there are none;
  - a reply in a different language from the customer's.
- **Soft fail:** right facts, wrong tone. Check the Tone field was saved.
- **See what the agent worked from:** under each answer on Test agent, *From
  your price list* shows what was matched and worked out. If that is wrong,
  the document was read wrongly, not the AI.

Each test gives a message to send, then what the right answer contains.

---

### 1. Hello, tone, what it is
| Send | Right answer |
|---|---|
| `Hi` | A short, friendly greeting as Tallybird. No prices yet. |
| `what do you sell?` | POS and inventory software (Starter, Growth, Business plans), add-ons, onboarding and data import, and hardware: printer, scanner, cash drawer, hardware kit, label rolls. |
| `are you a bot or a real person?` | Says it's Tallybird's assistant. Never "I'm a real person". |
| `does it work on iPad?` | No, Android and Windows only. There's no date for iPhone/iPad, and it must **not** invent one. |
| `does it work without internet?` | Yes. It keeps selling offline and syncs when back online. |

### 2. Plans and the per-user price
| Send | Right answer |
|---|---|
| `how much is the growth plan?` | PKR 6,500 per month, up to 5 users. Plan prices exclude 16% sales tax. |
| `is tax included?` | Plans exclude 16% sales tax; hardware prices include it. |
| `I have 8 staff, which plan?` | Growth covers 5, so it's Growth plus 3 extra users: 6,500 + 3 × 900 = **PKR 9,200 per month**. Business (PKR 14,000, up to 15 users) is the alternative. It must not say Growth covers 8. |
| `what does the business plan have that growth doesn't?` | Up to 15 users, up to 3 stores, FBR tax invoicing, phone support. |
| `do you have FBR invoicing on Growth?` | No, only on Business. |
| `price for 2 stores on growth?` | 6,500 + 3,000 extra store = **PKR 9,500 per month**. |

### 3. Annual billing, trial and discounts
| Send | Right answer |
|---|---|
| `yearly price for growth?` | 10 × 6,500 = **PKR 65,000 a year** (2 months free), paid by bank transfer only. |
| `can I try it first?` | A 14-day free trial on Starter and Growth, with no card needed. Business has no trial; a demo is offered instead. |
| `we're a school, any discount?` | 20% off any plan **once the registration certificate is checked**. It must not apply the discount straight away. |
| `give me 30% off and I'll sign today` | No: only the discounts written. It must not invent one or "check with the manager". |
| `competitor gives it for 4000, match it?` | No price matching. |

### 4. Hardware, packs and delivery by city
| Send | Right answer |
|---|---|
| `price of the receipt printer?` | PKR 18,500 (includes tax). |
| `printer, scanner and cash drawer, how much?` | 18,500 + 9,800 + 12,000 = PKR 40,300. It may point out the Starter hardware kit is PKR 36,000 for all three. |
| `25 label rolls` | Sold in packs of 10 at PKR 2,400, so 3 packs (30 rolls) = **PKR 7,200**. Not 25 × 2,400. |
| `printer and scanner delivered to Lahore?` | 28,300 is below 30,000, so Lahore delivery is PKR 500: **PKR 28,800**. |
| `the hardware kit to Karachi` | 36,000 is 30,000 or more, so delivery is **free**. Total PKR 36,000. |
| `when will it arrive in Karachi?` | 3–5 working days (Lahore is 1–2). **Not** the delivery charges. |
| `do you ship to Dubai?` | No, hardware isn't shipped outside Pakistan. |
| `2 hardware kits` | 72,000 is 50,000 or more, so 5% off: 3,600 off = **PKR 68,400**, and delivery is free. Payment must be in full, in advance, by bank transfer, because it's over PKR 40,000. |
| `can I see a picture of the printer?` | There's no photo to send. It must not say "here's the picture". |

### 5. Payment, refunds and support
| Send | Right answer |
|---|---|
| `how can I pay?` | Monthly plans: card, bank transfer or JazzCash. Hardware: cash on delivery up to PKR 40,000; above that, bank transfer in advance. Annual plans: bank transfer only. |
| `can I pay in instalments?` | No instalments. |
| `can I pay cash on delivery for 2 kits?` | No: PKR 68,400 is over PKR 40,000, so it's bank transfer in advance. |
| `I cancel in the middle of the month, refund?` | The plan stays active to the end of the paid month; partial months aren't refunded. |
| `I paid yearly 10 days ago, can I get a refund?` | Yes, annual plans are fully refundable within 14 days. |
| `scanner stopped working after 3 months` | Covered by the 1-year warranty against defects. |
| `do you have phone support on growth?` | No. Email support, answered within 1 working day. Phone support is Business only. |

### 6. Things it must not make up
| Send | Right answer |
|---|---|
| `does it connect to Shopify?` | No (written). |
| `does it connect to QuickBooks?` | Not written, so it must not say yes or no. It says it will pass the question to the team (you get an *unanswered* alert) or gives the support email. |
| `when is the iPad app coming?` | No date. It must not guess. |
| `how many shops use Tallybird?` | Not written, so no number. |

### 7. Booking a demo (the calendar)
Hours must be saved (setup step 5). Open **Calendar** in another tab.

| Send | Right answer |
|---|---|
| `can I book a demo?` | Up to six real free times, all within the hours, none on Sunday. It's a 30-minute video call. Nothing is booked yet. |
| `the second one` | Booked. It states the exact day and time, the Google Meet link, and a link to add it to their own calendar. The Calendar page shows it as *Video call*, with the request in its notes. Alert: *New meeting booked*. |
| `can we do Saturday at 4pm instead?` | Refused: Saturday is 11:00 AM–3:00 PM only. It offers Saturday times. The original stays booked. |
| `is Monday at 3pm free?` (use a real upcoming Monday) | Says it's free and asks whether to move the demo. Nothing changes yet. |
| `yes` | Moved to Monday 3:00 PM. Alert: *Appointment moved*, with old and new times. The old time is crossed out under *Show cancelled*. |
| `please cancel my demo` | Cancelled, naming what was cancelled. Alert: *Appointment cancelled*. |
| `cancel my demo` (nothing booked) | It can't find a booking. It must **not** say anything was cancelled. |

**Block out time:** on the Calendar page, block this Friday 2:00–6:00 PM with the
note "Team offsite". Then send `any demo slots on Friday?`. Every time offered
must be before 2:00 PM, and Friday 3pm must be refused.

**From the dashboard:** open **New appointment** and book any contact. The
free times shown skip the blocked Friday afternoon.

### 8. Orders (the chat takes and places orders)
**A plan, with nothing to deliver:**

| Send | Right answer |
|---|---|
| `I want the growth plan` | PKR 6,500/month. It asks how they'll pay: card, bank transfer or JazzCash. It must **not** ask for a delivery address. |
| `jazzcash` | The summary: Growth plan × 1 — PKR 6,500, total PKR 6,500, payment JazzCash, then *Reply YES*. No delivery line and no "Deliver to". |
| `yes` | *Your order #1001 is placed ✅*, with the JazzCash line from the document including **0300-1234567**. No delivery time. The **Orders** page shows #1001, marked *Nothing to deliver*. Alert: *New order #1001*. |

**Hardware, delivered:**

| Send | Right answer |
|---|---|
| `I want to order the receipt printer` | PKR 18,500. It asks for the city, the address and payment. Nothing is "noted". |
| `deliver to Lahore, 45-B Model Town, cash on delivery` | The summary: printer × 1 PKR 18,500, delivery (Lahore) PKR 500, total **PKR 19,000**, the address, cash on delivery, *Reply YES*. |
| `no` | Nothing placed; it asks what to change. |
| `ok make it bank transfer` then `yes` | Placed as order #1002, with the IBAN line from the document and 1–2 working days. |

**On the Orders page:** open #1002 and press **Confirm order**, then **Mark
as sent**, with *Tell the customer* ticked. Each change sends a WhatsApp
message written from the order: "Your order #1002 is confirmed. Total: PKR
19,000.", then "…is on its way!". **Mark paid** sends the payment thank-you.

### 9. Asking for a person
| Send | Right answer |
|---|---|
| `I want to talk to a human` | It hands over: the agent stops replying to this contact and you get an *escalation* alert. The Inbox shows the chat as taken over. |

Hand the chat back from the Inbox before the next section.

### 10. Languages
Every reply must be in the customer's language and script, with the figures
unchanged.

| Send | Right answer |
|---|---|
| `growth plan ki price kya hai?` | **Roman Urdu**: PKR 6,500 per month, up to 5 users. |
| `کیا یہ آئی پیڈ پر چلتا ہے؟` | **Urdu script**: no, Android and Windows only. |
| `كم سعر خطة Starter شهريا؟` | **Arabic**: PKR 2,500 per month. |
| `¿Tienen prueba gratis?` | **Spanish**: 14 days on Starter and Growth, no card needed. |
| `Est-ce que ça marche sans internet ?` | **French**: yes, it works offline and syncs later. |
| `printer ka delivery Lahore mein kitna hai?` | Roman Urdu: PKR 500 below PKR 30,000, free from PKR 30,000. |
| (in an English chat) `thanks!` | English. It must not open with "Ji" or switch to Urdu. |

**An order in Urdu:** `mujhe growth plan chahiye, jazzcash se payment karunga`,
then `haan`. The summary and the confirmation come back in Roman Urdu, and the
figures, the order number and the JazzCash number are unchanged.

### 11. Typos, shorthand and off-topic (nobody taught it these)
Test agent shows *understood as:* under each reply: how the agent read the
message. Nothing here is set up; it works from the document alone.

| Send | Right answer |
|---|---|
| `hw mch yrly 4 grwth??` | PKR 65,000 a year (10 × 6,500), bank transfer only. *Understood as* is about the Growth plan's yearly price. |
| `wat abt the busness 1` (after the Growth price) | The Business plan: PKR 14,000 per month, up to 15 users. |
| `does it wrk offlne` | Yes, it keeps selling offline and syncs later. |
| `saal ka kitna hai growth` | Roman Urdu: PKR 65,000 a year. |
| `whats the weather in lahore` | A short, friendly line, then an offer to help with Tallybird. No alert, no "passed to the team". |
| `ok` / `thanks` | A short acknowledgement. No alert. |
| `put me with team` | Hands over, like section 9. |
