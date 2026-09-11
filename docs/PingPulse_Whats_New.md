# What Changed, and How to Use It

**Release 1.3.2 — 11 September 2026**

This covers everything added across the 1.3 releases and, more usefully, the order to
set it up in. Read it alongside the Client Overview, which describes what the agent
does day to day; this one is about the parts you configure and the parts that learn.

---

## 1. Everything new, in one page

| What | What it means for you |
| --- | --- |
| **Upload your price list** | Drop in a PDF, Word file or text file. The agent quotes from it and only from it. |
| **Read your WhatsApp catalogue** | If your WhatsApp Business account already lists products, they can be imported directly — nothing to upload. |
| **Your replies are marked as yours** | When you take over a conversation, the message shows as yours rather than the agent's. |
| **Never answered** | Finds people who asked you about a product and never got a reply, so you can answer them one at a time. |
| **Learn how you write** | Reads the messages you wrote yourself and describes your style, for you to approve. The agent then writes that way. |
| **Learn what you have told customers** | Turns answers you have already given — delivery areas, timings, payment — into facts the agent knows. |
| **What's new** | The sparkle button in the header, which is where you found this. |
| **Works on a phone** | The dashboard fits a phone properly: chats, the open conversation and your pipeline, one at a time. |
| **A new look** | Black console with the brand mark throughout, easier on a screen that stays open all day. |

Two of these — learning your voice and learning your facts — **change nothing until you
approve a draft**. If you never open them, your agent behaves exactly as it does today.

---

## 2. Before anything else: the one thing that gates the rest

**WhatsApp hands over your past conversations only when a phone is first connected.**
Not on a reconnect of a session it already knows — at a fresh pairing.

This matters because three of the new features read that history:

- Never answered, to find people you missed
- Learn how you write, to see your own messages
- Learn what you have told customers, to find the answers you have given

If your number was paired some time ago, there is nothing for them to read yet, and each
of those screens will say so in plain words rather than showing you an empty list with no
explanation. Scan the QR again and the history arrives with it.

> **Nothing is lost by re-pairing.** Your conversations, contacts and pipeline live in
> PingPulse, not in the phone session. Re-pairing brings history *in*; it does not take
> anything away.

---

## 3. Setting it up, step by step

Everything below lives in one place: open **your business settings** from the
organisation selector at the top of the dashboard.

### Step 1 — Connect WhatsApp

In **Connection strategy**, choose how this number is connected.

- **WhatsApp Web (QR)** — scan the code with the phone that owns the business number.
  No per-message cost, and this is the option that brings history with it.
- **Twilio** — the official API. Enter your Account SID and Auth Token. Steadier, costs
  per message, and no conversation history is available.

Wait for the status to read **connected** before moving on.

### Step 2 — Tell it what you sell

The agent will never invent a price, a size or a stock level. That is deliberate, and it
means it is only as useful as what you give it here. There are three routes, and you can
use more than one.

**If your WhatsApp Business account has a catalogue** — it will say so, with a count of
the products it can see. Choose how the prices should be read, press **Preview**, and
check the first few rows against what you actually charge. WhatsApp reports a price as a
plain number without saying where the decimal point goes, so this one confirmation is the
difference between $89.00 and $8,900. When it looks right, press **Looks right — import**.

**If you have the information in files** — use **Choose files**, or drop them onto the
panel. PDF, Word, plain text and Markdown, up to 25 MB each. Price lists, policies,
anything customers ask about. Tables are kept intact, so a price stays attached to its row.

> A scanned PDF — a photograph of a page rather than a document with text in it — will be
> refused, and it will tell you that is why. There is nothing in the file for it to read.
> Send the original, or type the key facts into a text file.

**If you have neither** — that is still workable. What you sell and how you want it sold,
written in your own words in the settings, gets the agent a long way. It simply cannot
quote a specific price until something above gives it one.

### Step 3 — Check what it knows

The panel tells you what the agent currently has to work with and what would help most
next. Worth a glance before you let it answer real customers.

### Step 4 — Teach it how you write

Open **Learn from your own replies**.

1. Press **Read my voice**. It looks at the messages a person at your shop actually
   typed — not the agent's replies — and describes how you write: sentence length,
   whether you greet and how, the language you use and whether you mix two, formality,
   punctuation, emoji.
2. **Read the description and change anything wrong.** It is a description of your own
   voice, and you are the authority on it. Edit it freely.
3. Below it are a few of your own lines, kept as examples. Remove any you would rather
   not have reused.
4. Press **Use this voice**.

From that moment every reply is written in that voice. If you do not like the result,
**Default voice** puts it back exactly as it was, immediately.

> **It needs at least five messages you wrote yourself.** Fewer than that and it will say
> so rather than invent a description of your style from three sentences.

**What it will not do:** take facts from your voice. Style comes from here; prices, stock
and promises come only from your catalogue and price list. An example that contains a
figure is refused for exactly this reason — an example written for one customer gets
shown to another, and a price must never travel that way.

### Step 5 — Teach it what you have already told customers

In the same panel, press **Find facts**.

You have already answered "do you deliver to X", "what time do you close" and "can I pay
cash" hundreds of times. This reads those answers and turns them into plain standalone
facts.

Every fact is shown with a tick beside it. **Untick anything that is no longer true**, then
press **Save these**. Only what you tick is saved.

Re-running it later replaces the previous set rather than adding to it, so when you change
your delivery charge you get the new answer and not both.

**What it will not do:** learn from the agent's own answers. Only exchanges a person
answered are used. A fact taken from the agent's reply would be its guess written down as
though you had confirmed it, and afterwards nothing could tell the two apart.

### Step 6 — Answer the people you never answered

The header shows an amber **"N waiting"** button when there is something to act on. Open it.

These are customers who asked about a product and never heard back — the shop was busy, it
was a Sunday, the message scrolled away. Each row shows their own words and what they asked
about.

**Write each reply yourself and send them one at a time.** There is deliberately no
select-all, and there will not be. WhatsApp treats a burst of identical messages as spam,
and it is your business number at risk. The counter reads "6 of 40 sent today" so the pace
is visible.

Anyone already in an active conversation with the agent is left off the list, so you never
end up with two conversations running side by side.

### Step 7 — Take over whenever you want

Type into any conversation and it goes out as **you**, shown as yours rather than the
agent's. The agent sees that a person has stepped in and follows your lead for the rest of
that conversation.

This is also the material Step 4 learns from, so taking over a few conversations in your
own words is the fastest way to give it a voice to read.

---

## 4. What it will not do

Worth knowing, because each of these is a decision rather than a gap.

- **Invent a price.** Every reply is checked against your real catalogue before it is sent.
- **Message people in bulk.** One conversation per action, with a daily cap that refuses
  rather than queues — queueing would only delay the same ban.
- **Learn from itself.** Only words a person at your shop wrote are used for voice or
  facts. WhatsApp does not record who typed a message, so anything sent after your agent
  went live is set aside rather than guessed at.
- **Carry one customer's details to another.** Email addresses, phone numbers and order
  references are stripped out of anything it keeps.
- **Change how it sounds without being asked.** Both learning features draft, show, and
  wait for you.

---

## 5. Questions we expect

**Will any of this change how my agent behaves today?**
No. Voice and facts both start switched off. Until you approve a draft, nothing is
different.

**I opened the learning panel and it says there is nothing to learn from.**
Your phone session predates the feature. WhatsApp only hands over history at a fresh
pairing — see section 2.

**A large number of my messages were "set aside". Why?**
Those were sent after your agent went live. WhatsApp records that a message came from your
number, not who typed it, so there is no way to tell which of them you wrote and which the
agent did. Using them would teach it your voice out of its own replies.

**Can I edit the voice description later?**
Yes, any time. Edit and press **Use this voice** again, or **Default voice** to clear it.

**What happens to facts that are out of date?**
Untick them and they are not saved. Re-running replaces the whole set, so the easiest fix
after a price change is to run it again.

**Do I need to reinstall anything?**
No. The desktop app updates itself on next launch. If you use the dashboard in a browser,
you already have it.

---

## 6. Where everything lives

| Where | What is there |
| --- | --- |
| Organisation selector → business settings | What you sell, how it should sell, currency, language |
| …→ **What your agent should know** | Upload files, import your WhatsApp catalogue, see what it knows |
| …→ **Learn from your own replies** | Voice and facts |
| …→ **Connection strategy** | WhatsApp Web or Twilio |
| Header → sparkle button | What's new |
| Header → amber **N waiting** | Customers who were never answered |
| Any conversation | Type to take over; your message is marked as yours |

---

*PingPulse — WhatsApp AI Sales Agent. Questions about anything here go to your account
contact.*
