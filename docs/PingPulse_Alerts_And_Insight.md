# Knowing What Your Agent Did

**Release 1.3.3 — 17 September 2026**

Your agent answers customers whether or not anybody is watching it. That is the whole
point of it, and it is also the problem this release solves: the moments it *cannot*
handle are exactly the moments nobody is there to notice.

Until now, the only way to find out that a customer had demanded a manager at nine at
night was to have the dashboard open at nine at night. This document covers the three
ways you can now find out without it — being told, looking at the board, and looking at
the numbers — and, at the end, what each one is actually for.

Read it alongside *What Changed, and How to Use It*, which covers the 1.3 releases up to
this one.

---

## 1. Everything new, in one page

| What | What it means for you |
| --- | --- |
| **Alerts** | Seven things your agent can interrupt you about, by email and on your screen, with the dashboard shut. |
| **A watch on your number** | The one problem nothing else can see: a WhatsApp number that has quietly stopped working. Checked every five minutes. |
| **Alert receipts** | Every alert now says whether it reached you, and when it did not, why. |
| **Your board** | Your whole pipeline as columns you drag people across, with the value of each deal on the card. |
| **The numbers** | Where leads get stuck, how fast people get answered, what came in, where from, and what it is worth. |

Nothing here changes how your agent talks to customers. These are all ways of watching
it, not ways of changing it.

---

## 2. Alerts

### 2.1 What you can be told about

Seven things. Five are on before you touch anything; two are off, because they happen
often enough to become noise.

| | On by default |
| --- | --- |
| Your WhatsApp number has stopped working | **Yes** |
| Somebody asked for a person, or complained | **Yes** |
| Somebody wants to book a time | **Yes** |
| A message could not be delivered | **Yes** |
| Somebody asked to stop being messaged | **Yes** |
| A new person messaged for the first time | No |
| The agent could not answer something | No |

The defaults lean quiet on purpose. A channel that cries wolf gets switched off within a
week — and then the escalation that mattered is missed too. Start with the five, add the
other two if you find you want them.

### 2.2 Where it reaches you

Two channels, and they exist for each other.

**Email** needs nothing installed and no permission. It reaches you on a machine you have
never opened PingPulse on, on a phone in a pocket, on holiday. If you only set up one
thing, set up this one.

**Your browser** shows an alert on screen even with the dashboard closed, as long as the
browser is running. It is faster and more noticeable, and it costs one click per browser,
on each machine — browsers will not let anybody skip that click, and nor should they.

> A browser that has already been allowed stays allowed. You will not be asked again on
> that machine, and a new tab or a cleared cache does not undo it.

### 2.3 Setting it up

Open **your business settings** from the organisation selector at the top of the
dashboard, and scroll to **Tell me when something happens**.

1. **Tick what is worth interrupting you.** The five defaults are a reasonable place to
   start.
2. **Put an email address in.** Your own is offered as a suggestion — one press fills it
   in. Leave it empty for no email.
3. **Press *Alert this device*** if you want alerts on screen as well. Your browser will
   ask once; allow it.
4. **Press *Send a test*.** It sends a real alert through the real path and tells you
   exactly what happened to it. If it says it reached nobody, it will say why.

Step 4 is the one worth doing. The question you have is not "did my settings save" but
"will I hear it", and only a real send answers that.

### 2.4 Why you do not get buzzed four times

An angry customer sending four messages in a row is one thing happening. Repeats about
the same person and the same kind of event inside **thirty minutes** collapse into one
alert.

This holds across restarts and across the two channels — it is not a memory that gets
lost.

### 2.5 The alert that goes looking

Six of the seven are triggered by a message arriving. That leaves one hole, and it is the
worst one:

> **A WhatsApp number that has been logged out receives nothing.** No messages arrive, so
> nothing triggers anything, and the symptom is silence. A business can go days believing
> its agent is working.

So this one is gone looking for rather than reacted to. Every five minutes, PingPulse
checks whether your number can actually send and receive. If it cannot, you are told —
and told again once a day until it is fixed, because the first alert is the useful one
and the rest only exist so it is not forgotten.

Re-pairing the number stops it immediately.

---

## 3. When an alert does not arrive

An alert that fails is worse than no alert, because you believe you are covered and you
are not. Every alert PingPulse raises is now recorded with what became of it, and the
alert settings panel shows the last week of them.

| What you see | What it means | What to do |
| --- | --- | --- |
| **Reached your email** · *Reached 2 devices* | It got to you. | Nothing. |
| **Still trying** | It failed once and is being retried. | Wait a minute; it usually lands. |
| **Could not be delivered** | It ran out of chances. The reason is written beside it. | Check the address, or set up a second channel. |
| **Reached nobody** | It was sent and not one device took it. | Set up alerts on a browser you actually use. |
| **Nothing was set up to receive it** | There was nowhere to send it. | Add an email address, or allow alerts on this browser. |

The last two are the ones worth knowing about, because until this release they looked
exactly like success from the outside.

**You do not have to go looking.** If anything in the past week failed to reach you, a red
**"2 alerts missed"** appears in the header, next to your connection status. Pressing it
opens this list. On a phone it collapses to a red number.

> Whatever the alert was about happened anyway. A missed alert means you were not told;
> it does not mean the agent stopped working.

---

## 4. Your board

Your pipeline as columns, left to right, in the order a deal actually moves: **new lead,
contacted, qualified, estimate scheduled, estimate sent, follow-up**, then **won, lost,
unqualified**.

- **Drag a card** to move somebody, or use the dropdown on the card — which is the half
  that works on a phone.
- **The value of the deal** shows on the card once you have set one.
- The columns are wider than the screen on purpose and scroll sideways. Squeezing nine
  columns into a laptop makes all nine unreadable.

Every move you make is recorded, which is what makes the funnel in the next section
possible.

---

## 5. The numbers

Open **How it is going** from the header. Everything reads over a window you choose:
**today, 7 days, 30 days, 90 days, or all time**. "Today" means today where your business
is, not in UTC.

**Money.** What is in your pipeline, what you have won, and what is still open. Deals
you marked lost are not counted as open pipeline. It tells you how many of your leads
have a value on them at all, because a figure built from four of forty leads is not a
forecast.

**Where leads get to.** How far people got, as a funnel. Two things worth knowing about
how it is built:

- It follows a **group of people over time**, not a snapshot of where everyone stands
  now. Somebody who came in last week and has since been won still counts as having
  passed through every stage on the way.
- A lead is counted at **the furthest point it reached**. Losing somebody is not a stage —
  a deal lost after an estimate counts as having reached the estimate.

**Messages.** What came in and what went out, over time. The bars group by hour, day or
week depending on how long a window you picked.

**How fast people get answered.** Your median and your slowest replies. The median is the
honest one — an average is dragged around by a single conversation somebody sat on
overnight.

**Where leads came from.** Which source each person arrived through, so you can tell which
advertising is producing conversations rather than clicks.

> **Nothing was backfilled.** The funnel and the reply times started counting when this
> was switched on. Early windows will look thin; that is the data being honest rather
> than the feature being broken.

---

## 6. What each of these is actually for

### An angry customer at nine at night

A customer demands a manager. The agent correctly hands the conversation over and stops
replying — which is right, and which also means nothing further happens until somebody
notices.

**Before:** you find out in the morning, and the customer has spent the night being
ignored.
**Now:** an email arrives within seconds. You answer it from your phone, or you decide it
can wait — but it is your decision rather than an accident.

### A number that quietly stopped working

This is not hypothetical. A live PingPulse number sat logged out for several days. Every
part of the system was healthy; the phone had simply dropped the session, and because a
logged-out number receives nothing, nothing anywhere had a reason to complain.

**Now:** you are told within five minutes, and told again every day until the number is
paired again.

This is the single most valuable alert in the list, and it is the reason the list exists.

### A quote that never went out

WhatsApp refuses a message — a number that is no longer valid, a media file it will not
take, a rate limit. The agent recorded a perfectly good reply that never arrived.

**Now:** *A message could not be delivered* tells you which customer, so you can reach
them another way rather than waiting on an answer to something they never saw.

### Deciding whether the agent is earning its keep

Open **How it is going**, pick *30 days*, and read three numbers: how many conversations
came in, how far they got, and how fast people were answered.

The useful comparison is not against another business. It is against the same three
numbers from before you had this — most owners already know, roughly, how long it used to
take them to answer somebody in the evening.

### Finding out where deals die

The funnel shows the drop between one stage and the next. A large fall between *estimate
sent* and *follow-up* is not a software problem — it is a sales one, and it is the single
most useful thing on that screen.

### Deciding where to spend on advertising

**Where leads came from** counts conversations, not clicks. A source producing a lot of
first messages and nothing beyond *contacted* is buying you attention from the wrong
people, which is worth knowing before you renew it.

---

## 7. What this does not do

Said plainly, because a tool that overstates itself is worse than one that admits a gap.

- **Alerts do not act.** They tell you something happened. The agent has already stopped
  replying in the cases that matter; the response is yours.
- **Browser alerts need a browser that is running.** Closed laptop, no alert. Email does
  not have this limitation, which is why it is the one to lean on.
- **A missed alert is not a missed event.** The escalation, the booking, the opt-out all
  happened and are all recorded. The alert is the messenger.
- **The numbers only know what they were told.** A deal with no value set is not in the
  money figures, and a lead whose stage was never moved sits where it was left.
- **Nothing here re-pairs your phone.** The watcher will tell you your number is down
  every day, accurately, indefinitely. Scanning the QR code again is the fix.

---

## 8. A short setup checklist

1. Open your business settings.
2. Scroll to **Tell me when something happens**.
3. Leave the five defaults ticked.
4. Put an email address in and save.
5. Press **Send a test** and read what it says.
6. If you want on-screen alerts, press **Alert this device** and allow it — on each
   machine you use.
7. Check the header occasionally. A red badge there means something did not reach you.

Five minutes, once. After that the only time you should think about any of this is when
it interrupts you — which is the point.
