import { useEffect, useState } from 'react'
import { Sparkles, X } from 'lucide-react'

/**
 * What has changed, in the words of someone running a shop.
 *
 * Not a changelog. A changelog is written for the people who wrote the code; an
 * operator wants to know what is different about their afternoon. Every entry
 * here is something they can see or do, which is also a useful filter on what
 * belongs: work that changes nothing for them — a backup that now leaves the
 * machine, a test suite in CI — is real and does not go on this list.
 */
const RELEASES = [
  {
    version: '1.5.6',
    date: '29 September 2026',
    items: [
      {
        title: 'When it doesn’t know, it says so and tells you',
        body:
          'A customer asking something none of your documents cover — "do you ship to '
          + 'Dubai?", "is this BPA-free?" — used to get a slow "could you tell me a little '
          + 'more?". Now the agent stops guessing, you get an alert with their question, '
          + 'and they are told it has gone to the team. That sentence is only sent if an '
          + 'alert address or device is really set up; otherwise they get the email or '
          + 'phone number written in your own documents. Every one of those alerts is a '
          + 'gap in your documents worth filling.',
      },
      {
        title: 'Replies come back faster',
        body:
          'The whole reply now has one deadline. The second AI provider is only tried if '
          + 'there is time left for it, and past the deadline you get the answer worked out '
          + 'from your documents rather than a longer wait. Reading the message and '
          + 'searching your documents both give up early and fall back to the fast path, '
          + 'which used to add over a minute on a slow connection.',
      },
      {
        title: 'You are told when the AI stops answering',
        body:
          'If neither AI provider responds, customers still get an answer built from your '
          + 'documents — and now you are told it is happening, once per cool-off rather '
          + 'than once per message. Before this, nothing said so: the replies quietly got '
          + 'worse and nobody knew why.',
      },
      {
        title: 'Booking can be tried in Test agent',
        body:
          'Ask for a time in Test agent and it offers real free times from your diary; '
          + 'reply with one and it books it — exactly as on WhatsApp, then undone, so '
          + 'nothing is kept. Without opening hours it tells you what a live customer '
          + 'would have been told, and whether anyone would have been alerted.',
      },
      {
        title: 'Price lists from every trade read correctly',
        body:
          '"Rs. 2,500" is one price, not the end of a sentence. Leader dots in a salon '
          + 'menu are not a full stop. Per-kilo and per-litre prices are rates, so half a '
          + 'kilo is half the price. "From $9,500" stays a starting price and is said as '
          + 'one. A line holding two services is read as two. Rules — "an extra Rs 2,000", '
          + '"Otherwise Rs 150" — are no longer read as products.',
      },
      {
        title: 'It follows how customers actually write',
        body:
          'Roman Urdu numbers ("do kg", "teen packets") count when a unit or product '
          + 'follows, so "do you have" is still a question. "aur" joins two things in one '
          + 'order. Ladies/women, guests/heads and one-letter typos still find the right '
          + 'product. "Show me what you have" now shows the things you have pictures of, '
          + 'nearest to whatever was mentioned, instead of asking them to say more. A '
          + 'place none of your documents mention goes to the team rather than being '
          + 'answered with another city’s rules.',
      },
    ],
  },
  {
    version: '1.5.5',
    date: '29 September 2026',
    items: [
      {
        title: 'A photo in your price list becomes the product’s photo',
        body:
          'Put a picture in each row of the price table in a Word file and upload it: the '
          + 'picture beside a product is now kept as that product’s photo, and the agent can '
          + 'send it when a customer asks to see the thing. The price is still read once, '
          + 'from the table. The upload tells you how many products got a photo. A photo '
          + 'column that had nothing typed under it used to shift every row’s prices one '
          + 'column left — that is fixed, so those price lists read correctly now.',
      },
      {
        title: 'Counting the pieces in a pack is priced in packs',
        body:
          '"20 gel pens" where a pack holds ten is two packs, and 25 is three — with the '
          + 'reason given, rather than a pack silently split. "3 packs of gel pens" still '
          + 'counts packs. A measure written per piece — "5 m per roll" in a set of five '
          + 'rolls — is no longer read as what the whole set holds.',
      },
      {
        title: 'It does not lose a count, or deny something you do sell',
        body:
          'A number in a product’s name used to cancel the customer’s count: an order for '
          + 'four coils of the 4 mm cable stopped being priced at all. And "standard gel '
          + 'pens" was answered with "we don’t have standard gel", from a shop whose pens '
          + 'are gel pens. Words about quality or size — regular, modern, thick — no longer '
          + 'make it say it does not stock something. Asking for a kind you genuinely do not '
          + 'have still gets a plain "we don’t have that", followed by the closest thing.',
      },
      {
        title: 'Delivery charged by place asks which city',
        body:
          'Where your rules charge one rate within your own city and another for the rest of '
          + 'the country, the agent uses the place the customer named. If they have not said, '
          + 'it gives both charges and asks — instead of picking one. An advance-payment rule '
          + '— "orders above PKR 15,000 need 50% advance" — is no longer read as 50% off.',
      },
    ],
  },
  {
    version: '1.5.4',
    date: '29 September 2026',
    items: [
      {
        title: 'It no longer says it is sending pictures it has not got',
        body:
          'On a real conversation the agent said "here are the photos" and nothing was sent. '
          + 'The list of products it was given carried a heading claiming photos were '
          + 'attached, whenever any product matched — including when none were attached, and '
          + 'when the products were rows of a price list, which never have photos. It is now '
          + 'told what is actually going out, it is stopped if it says otherwise, and if you '
          + 'have no product pictures at all it will not offer them. Asked for a photo it '
          + 'does not have, it says so and describes the thing instead.',
      },
      {
        title: 'It cannot promise an invoice or a price cut on your behalf',
        body:
          '"We will share a signed proforma invoice" and "the unit prices will be reduced" '
          + 'both reached a customer, and nothing in PingPulse issues a document or changes '
          + 'a price. Those sentences are now refused unless a person really was alerted. '
          + 'Your own terms — "invoices are due within 14 days" — are unaffected.',
      },
      {
        title: 'Mixed orders are read line by line',
        body:
          '"...and 10 MCBs. I am in Lahore ... 4mm cable" had priced ten cable coils. Each '
          + 'sentence of an order is now matched on its own, a product is not counted twice, '
          + 'and asking about 20 metres of something sold by the coil is answered with how '
          + 'it is sold instead of silently added to the total. Payment and returns questions '
          + 'are answered from your own terms, and a plain hello no longer drags up what the '
          + 'customer bought last time.',
      },
    ],
  },
  {
    version: '1.5.3',
    date: '29 September 2026',
    items: [
      {
        title: 'Your discount and delivery rules are applied, not guessed at',
        body:
          'Write "orders of PKR 500,000 or more receive 2% off" or "delivery is free for '
          + 'orders of PKR 250,000 or more within Lahore" and those rules are now worked out '
          + 'against the actual order, rather than left to the AI to notice. An order one '
          + 'rupee short is told it is one rupee short, and told what it would get. Where two '
          + 'discounts could apply the better one is used, since they do not add up, and the '
          + 'next one up is named with how far away it is. A sentence that is not definite — '
          + '"we may be able to do something on large orders" — is quoted to the customer '
          + 'rather than turned into a rule.',
      },
      {
        title: 'It can repeat your customer’s own figure back to them',
        body:
          '"I have an order worth PKR 499,999, do I get the discount?" used to take half a '
          + 'minute and come back with the wrong thing, because the check that stops invented '
          + 'prices treated the customer’s own number as one. Saying it back is not quoting '
          + 'a price, and it is allowed now.',
      },
      {
        title: 'Asking about delivery no longer drags in the last product',
        body:
          '"What about delivery in Lahore?" was pulling in whatever product had been '
          + 'discussed three turns earlier. Earlier messages are only used to pick the '
          + 'product when this message asks for a price or an amount without naming one. '
          + 'Test agent also now says why an AI reply was rejected, and shows the rules it '
          + 'applied under each answer.',
      },
    ],
  },
  {
    version: '1.5.2',
    date: '29 September 2026',
    items: [
      {
        title: 'It quotes from your price list, and does the sums',
        body:
          'Upload a price list and ask it what ten of something costs, and you now get ten '
          + 'of something costed. It reads every priced row of your table — by what the '
          + 'column headings mean, not where they sit — along with the sale unit each '
          + 'product comes in. Ask for 20 metres of a cable sold as a 100 m coil and it says '
          + 'so, and prices the coil, rather than inventing a per-metre rate. Price lists you '
          + 'uploaded before this are understood without uploading them again.',
      },
      {
        title: 'Why a real price list used to get you nothing',
        body:
          'The check that stops the agent inventing prices only recognised amounts with a '
          + 'currency written in front of them. A table headed "Unit Price (PKR)" writes each '
          + 'row as a bare number, so none of its prices counted — and no total ever could, '
          + 'because ten times a price is not written in any document. Correct answers were '
          + 'being thrown away and replaced with "could you tell me a little more about what '
          + 'you are looking for?". The check now follows the arithmetic instead of '
          + 'forbidding it, and an invented figure is still refused.',
      },
      {
        title: 'Test agent shows its working',
        body:
          'Under each reply it now lists the product it matched, the unit that product is '
          + 'sold in, and the sums the answer was built from — so you can catch a row read '
          + 'wrongly before a customer does. If neither AI provider answered it says '
          + '"answered without AI" rather than just "fallback", and uploading a file now '
          + 'reports how many priced products it found in it.',
      },
    ],
  },
  {
    version: '1.5.1',
    date: '28 September 2026',
    items: [
      {
        title: 'Your licence is the token and the date, and nothing else',
        body:
          'It used to count machines too: each computer that signed in took one of a '
          + 'fixed number of seats, and once they were gone the next one was refused. That '
          + 'never stopped a token being passed around — it only ever refused you. A second '
          + 'browser, a new laptop, or clearing your site data spent a seat that was never '
          + 'given back, and the refusal looked, on screen, exactly like a business that had '
          + 'never been set up. Seats are gone. Use your token on as many of your own '
          + 'machines as you like; it stops working on its expiry date and not before.',
      },
    ],
  },
  {
    version: '1.5.0',
    date: '28 September 2026',
    items: [
      {
        title: 'Type where you are, in your own words',
        body:
          'The timezone was a list of four hundred names spelt the way the timezone '
          + 'database spells them, which is not how anyone thinks of where their shop is. '
          + 'It is a box now: type Karachi, Lahore, UAE, Miami, Texas, Asia/Dubai or UTC+5 '
          + 'and it works out which zone you mean and saves it by itself — there is no Save '
          + 'button to forget. The time there is shown underneath, so a wrong one is obvious '
          + 'before it books anybody. A slip like "Karachy" offers Asia/Karachi rather than '
          + 'guessing, and something genuinely ambiguous is never guessed at all.',
      },
      {
        title: 'A step ticks the moment it saves',
        body:
          'Saving a step used to leave it saying it was not done for a second or two while '
          + 'everything was checked again from scratch. The tick, the banner, the count in '
          + 'the sidebar and the lock now all move as soon as the save is confirmed, and the '
          + 'check that follows settles it.',
      },
      {
        title: 'Delete a contact',
        body:
          'Details on a conversation now ends with Delete contact. It asks once more, names '
          + 'the person, and says exactly what goes: every message, their appointments and '
          + 'their place on the board, with no undo. If they had asked not to be messaged it '
          + 'says so first, because deleting them forgets that they asked.',
      },
    ],
  },
  {
    version: '1.4.9',
    date: '28 September 2026',
    items: [
      {
        title: 'A step is only ticked when it is actually done',
        body:
          'Prices and knowledge ticked itself the moment "What you sell" had anything in it '
          + 'at all — a single line and no price list counted as finished, while the panel '
          + 'underneath it said, correctly, that the agent had nothing to quote from. Two '
          + 'answers to one question, and the wrong one was the one that counted. The tick '
          + 'now comes from the same judgement the panel shows, so if you remove the last '
          + 'document the step goes back to not done — and if it is a required step, it '
          + 'says plainly that your agent will not start until it is finished.',
      },
    ],
  },
  {
    version: '1.4.8',
    date: '28 September 2026',
    items: [
      {
        title: 'Your name, and your access token, in the sidebar',
        body:
          'At the bottom of the sidebar is the name your licence was issued to. Open it and '
          + 'your access token is there — hidden until you press the eye, with a button to '
          + 'copy it, and the date it runs until. It is there for the day you need to sign in '
          + 'on another computer. Keep it to your own team: anyone holding it can open your '
          + 'dashboard.',
      },
      {
        title: 'Set up in any order',
        body:
          'The steps were locked behind the business description, which made an order into a '
          + 'rule it never was. Start with whichever you have to hand — putting the price '
          + 'list up before writing the description is fine. If your token arrived without a '
          + 'business attached, opening any step creates it, named after you, and Your '
          + 'business is where you rename it.',
      },
      {
        title: 'An uploaded price list now counts',
        body:
          'Prices and knowledge only ticked itself if you typed into "What you sell". A real '
          + 'document — the price list, the brochure — could be uploaded, read and searched '
          + 'correctly, and the step still said it was not done. It counts now, as soon as the '
          + 'upload has been read.',
      },
      {
        title: 'No inbox that turns into a lock',
        body:
          'Coming in to an unfinished setup drew the inbox first and replaced it with the '
          + 'lock a few seconds later, and Setup then sat on a spinner for several more. '
          + 'Nothing shut is drawn until the answer is in, and Setup opens on the step '
          + 'straight away. If we cannot be reached at all, it says so and keeps trying '
          + 'rather than locking anything.',
      },
      {
        title: 'Hours is what turns booking on',
        body:
          'It is worded plainly now: without your opening hours the agent cannot book '
          + 'anything and hands every booking request to a person instead. Where the hours '
          + 'panel needs your timezone, it links straight to the step that sets it.',
      },
    ],
  },
  {
    version: '1.4.7',
    date: '28 September 2026',
    items: [
      {
        title: 'Setup says what is required, and waits for it',
        body:
          'The steps are in three groups now: required, recommended and optional. The four '
          + 'required ones are what your agent cannot answer a customer properly without — '
          + 'your business, your prices, your timezone and your WhatsApp number — and WhatsApp '
          + 'is deliberately last, because connecting it is the moment real customers start '
          + 'getting answers. Until those four are done the inbox, board and analytics stay '
          + 'shut, and each one tells you exactly what is missing and takes you to it. You can '
          + 'still try the agent before any of that, from Test agent.',
      },
      {
        title: 'Your timezone is its own step, and it is one click',
        body:
          'It was a dropdown buried two thirds of the way down the business form, under the '
          + 'currency, and almost nobody set it — which meant every opening hour and every '
          + 'appointment was being read as UTC. It is a step of its own now, it offers the zone '
          + 'your computer is already in, and it prints the current time there so a wrong one is '
          + 'obvious before a customer finds it.',
      },
      {
        title: 'The calendar link has its own step too',
        body:
          'It used to sit at the bottom of the hours form, below the services list, where a '
          + 'business could take a booking and have nowhere to see it. It is on the setup list '
          + 'now, under Recommended, with the link ready to copy to a second phone.',
      },
      {
        title: 'It tells you when your phone has dropped',
        body:
          'A WhatsApp connection that goes offline now appears under Needs attention and at the '
          + 'top of Setup, in those words — rather than leaving you to notice that nothing has '
          + 'come in for a while. It does not lock you out of your own inbox while it is down.',
      },
    ],
  },
  {
    version: '1.4.6',
    date: '27 September 2026',
    items: [
      {
        title: 'A dashboard you can read',
        body:
          'Navigation moved into a sidebar down the left, so the inbox, board, analytics and '
          + 'setup are one click from anywhere instead of hidden behind a menu. Type is larger '
          + 'throughout, and there is a light theme as well as the dark one — the switch is at '
          + 'the bottom of the sidebar and it remembers which you picked.',
      },
      {
        title: 'Anything wrong is in one place',
        body:
          'Needs attention gathers the things that actually need you — somebody waiting, alerts '
          + 'going nowhere, a phone that has dropped — each one saying what is wrong and taking '
          + 'you to the fix. When nothing is wrong it is not there at all.',
      },
      {
        title: 'The QR code comes up in seconds',
        body:
          'Pairing a phone had been failing for days: abandoned attempts were quietly retrying '
          + 'in the background and crowding out new ones. They are cleared properly now, a code '
          + 'appears in about twenty seconds, and a phone that was already paired reconnects by '
          + 'itself instead of asking you to scan again. While you are scanning, the Twilio '
          + 'option is folded away behind a button rather than sitting alongside it.',
      },
    ],
  },
  {
    version: '1.4.3',
    date: '24 September 2026',
    items: [
      {
        title: 'Your appointments, in the calendar you already use',
        body:
          'In Hours and booking there is now a calendar link. Subscribe to it once from ' +
          'your phone and every appointment your agent takes shows up beside everything ' +
          'else in your day. There is no account to connect and nothing to install, and ' +
          'nothing on your phone can move or cancel a booking by accident — the feed only ' +
          'ever reads.',
      },
      {
        title: 'It tells you when it cannot book',
        body:
          'Booking needs your timezone and your opening hours, and until both are set it ' +
          'quietly does nothing. That is how a business ran for months taking no ' +
          'appointments at all with nothing anywhere saying so. The hours screen now states ' +
          'plainly whether your agent can book and what is stopping it, and until it can, ' +
          'booking requests come to you instead of a link that books nothing.',
      },
      {
        title: 'Asking for a person works now',
        body:
          'There were a lot of ways of asking for a human that it talked straight past — ' +
          'including the plain ones. It hears them now, stops, and tells you. It will still ' +
          'offer to help first, because "I can check that for you" is an honest offer and ' +
          'not a refusal to fetch somebody.',
      },
    ],
  },
  {
    version: '1.4.0',
    date: '22 September 2026',
    items: [
      {
        title: 'Setting up is a list, in order',
        body:
          'Settings was one long box you scrolled, with nothing saying which parts mattered. ' +
          'It is a page now: eight steps in the order they need doing, five required and ' +
          'three optional, each saying what it is for and what breaks if you skip it. The ' +
          'ticks come from the real system, so a business you set up on another machine ' +
          'already shows as done here.',
      },
      {
        title: 'It can read your opening hours off your own price list',
        body:
          'Upload the documents you already have and, if your hours are written in one of ' +
          'them, it offers to fill them in. It is a suggestion with the times shown to you, ' +
          'never a silent change — and several documents now add up instead of replacing ' +
          'each other.',
      },
      {
        title: 'A wrong setting can be undone',
        body:
          'Three things no document can tell us — what you never promise, how you handle ' +
          'pricing, and when to fetch a person — now start from sensible drafts for your ' +
          'trade, shown before they apply. Anything you change can be stepped back. And a ' +
          'setting that would quietly have done nothing is refused with a reason instead of ' +
          'being accepted and ignored.',
      },
    ],
  },
  {
    version: '1.3.9',
    date: '22 September 2026',
    items: [
      {
        title: 'It only offers times it can actually keep',
        body:
          'Appointments are offered from your real opening hours, in your timezone, and it ' +
          'will only book a slot it already offered. Every booking is written down as a ' +
          'record rather than living in the conversation.',
      },
      {
        title: 'One answer per message. One follow-up per follow-up.',
        body:
          'A customer once received forty-four identical nudges in a row, because restarts ' +
          'had been stacking copies of the same reminder to all release at once. A ' +
          'follow-up is now claimed before it is sent, so however many times it is handed ' +
          'over, one message goes out.',
      },
      {
        title: 'Nobody is messaged at three in the morning',
        body:
          'Follow-ups wait for a civilised hour. A reminder that arrives in the night is ' +
          'not a reminder, it is a reason to block the number.',
      },
      {
        title: 'It never says something happened unless it did',
        body:
          'No confirmed booking, no promised callback, no price it cannot point at. If it ' +
          'cannot check, it says so or it hands the conversation to you. That is enforced ' +
          'by the software rather than asked of the agent, which is the difference between ' +
          'a promise and a setting.',
      },
    ],
  },
  {
    version: '1.3.4',
    date: '16 September 2026',
    items: [
      {
        title: 'It can reach you with this closed',
        body:
          'Your agent has always kept working whether or not this page is open — that is ' +
          'the point of it. The catch was that the moments it cannot handle were exactly ' +
          'the moments nobody heard about: somebody demanding a manager at nine at night, ' +
          'a reply that never went out. Turn on alerts in your business settings and those ' +
          'reach your browser and your email instead of waiting for you to look.',
      },
      {
        title: 'You only have to say yes once',
        body:
          'Once you have allowed alerts on a device, it stays set up by itself — a new tab, ' +
          'a cleared browser or an update will not quietly stop them. Your browser will not ' +
          'let any website turn notifications on without you clicking, so the first yes is ' +
          'yours to give; email needs no permission at all, which is why the settings page ' +
          'offers to fill in your address for you.',
      },
      {
        title: 'It will not pester you',
        body:
          'The same thing happening twice in half an hour only tells you once. Four buzzes ' +
          'about one angry customer is how people learn to ignore the buzz, and the next one ' +
          'they ignore is the one that mattered. Only the urgent ones are on to begin with — ' +
          'the rest you switch on yourself.',
      },
      {
        title: 'Your board, as a board',
        body:
          'A new button in the top bar opens your pipeline with every stage side by side. ' +
          'Drag a lead from one column to the next, or use the small menu on the card if you ' +
          'are on a phone. The narrow list down the right is still there and unchanged.',
      },
      {
        title: 'What a job is worth',
        body:
          'Leads now have a value you can fill in, and the analytics screen adds what you ' +
          'have won, what is still open, and your average deal. Only what you type counts — ' +
          'the agent never guesses a number here, because a figure on a dashboard gets acted ' +
          'on and a guessed one is still a guess.',
      },
    ],
  },
  {
    version: '1.3.3',
    date: '16 September 2026',
    items: [
      {
        title: 'See how it is actually going',
        body:
          'A new button in the top bar. It shows how many leads arrived, how far each of ' +
          'them got and how many turned into work — over today, a week, a month, or all ' +
          'of it. The stages are your own, in your own words, so the numbers mean what ' +
          'your board says they mean.',
      },
      {
        title: 'Where people stop',
        body:
          'If forty people ask and six get as far as an estimate, this says where the ' +
          'other thirty-four dropped off, stage by stage. A lead you marked lost still ' +
          'counts for the distance it travelled before it went, so the work that went ' +
          'into it is not hidden.',
      },
      {
        title: 'How fast people get answered, and by whom',
        body:
          'Your typical reply time, your slowest ten per cent, and how much of the ' +
          'answering the agent did rather than you. Counted per conversation, so ' +
          'somebody firing off three messages in a row counts as waiting once — and a ' +
          'follow-up you sent unprompted does not count as answering anybody.',
      },
    ],
  },
  {
    version: '1.3.2',
    date: '11 September 2026',
    items: [
      {
        title: 'It can learn to sound like you',
        body:
          'In your business settings, ask it to read your voice. It looks at the messages ' +
          'you have written to customers yourself and describes how you write — short or ' +
          'warm, formal or casual, the language you use and whether you mix two, how you open ' +
          'and sign off. You read the description, change anything that is wrong, and only ' +
          'then does it take effect. Until you do, ' +
          'nothing about your agent changes.',
      },
      {
        title: 'It can learn what you have already told people',
        body:
          'Your delivery areas, your timings, how you take payment — you have answered ' +
          'these a hundred times in WhatsApp already. It reads those answers, turns them into ' +
          'plain facts, and shows you the list. Tick what is still true and it becomes part of ' +
          'what the agent knows.',
      },
      {
        title: 'It never learns from itself',
        body:
          'Only messages a person at your shop actually typed are used. Anything sent after ' +
          'the agent went live is set aside, because WhatsApp does not record who typed what ' +
          'and we will not teach it your voice out of its own replies.',
      },
    ],
  },
  {
    version: '1.3.1',
    date: '11 September 2026',
    items: [
      {
        title: 'Upload your price list',
        body:
          'Open your business settings and drop in a PDF, Word file or text file — your ' +
          'catalogue, your policies, whatever your customers ask about. The agent starts ' +
          'quoting from it straight away, and only from it: it will never invent a price.',
      },
      {
        title: 'Read your WhatsApp catalogue',
        body:
          'If your WhatsApp Business account already has products in it, we can read them ' +
          'directly — nothing to upload. You see the prices we read before anything is ' +
          'saved, so a decimal point in the wrong place never reaches a customer.',
      },
      {
        title: 'Your replies are marked as yours',
        body:
          'When you take over a conversation, your message now shows as yours rather than ' +
          'the agent’s. Worth having on its own, and it is what will let the agent learn ' +
          'to sound like you.',
      },
    ],
  },
  {
    version: '1.3.0',
    date: '11 September 2026',
    items: [
      {
        title: 'A new look',
        body:
          'The console is black, with the brand mark throughout. Easier on the eyes for the ' +
          'kind of screen that stays open all day.',
      },
      {
        title: 'Works on your phone',
        body:
          'The dashboard now fits a phone properly — chats, the open conversation and your ' +
          'pipeline, one at a time instead of three squeezed together.',
      },
      {
        title: 'One customer, one conversation',
        body:
          'When someone changes their WhatsApp account, they no longer appear twice. ' +
          'Conversations that had split back into one.',
      },
    ],
  },
  {
    version: '1.2.x',
    date: '10 September 2026',
    items: [
      {
        title: 'Nothing gets lost',
        body:
          'If WhatsApp is briefly unreachable, replies queue and go out by themselves. You ' +
          'can see which ones are waiting — nothing is shown as delivered until it is.',
      },
      {
        title: 'Follow-ups you control',
        body:
          'Nudge a quiet conversation in your own words, at a time you choose. If the ' +
          'customer writes back first, it cancels itself.',
      },
      {
        title: 'Updates arrive on their own',
        body:
          'The desktop app updates itself when you open it. No installer to run twice.',
      },
    ],
  },
]

const SEEN_KEY = 'pingpulse.whatsnew'
export const LATEST = RELEASES[0].version

/** Whether this browser has already been shown the newest entry. */
export function hasUnseenUpgrades() {
  try {
    return localStorage.getItem(SEEN_KEY) !== LATEST
  } catch {
    // A private window with storage blocked should not lose the button, and
    // should not nag on every render either. Treat it as seen.
    return false
  }
}

export default function WhatsNew({ onClose }) {
  useEffect(() => {
    try {
      localStorage.setItem(SEEN_KEY, LATEST)
    } catch {
      // Nothing to do — the dot comes back next time, which is harmless.
    }
  }, [])

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center scrim p-4"
      // A click on the dimmed area outside closes it, as it does everywhere else.
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="max-h-[88vh] w-full max-w-lg overflow-auto rounded-2xl border border-edge bg-panel shadow-lift animate-pop">
        <header className="sticky top-0 flex items-center gap-2.5 border-b border-edge bg-panel px-5 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
            <Sparkles size={15} className="text-accent" />
          </span>
          <div>
            <h3 className="text-sm font-semibold text-ink">What&rsquo;s new</h3>
            <p className="mt-0.5 text-2xs text-dim">Recent upgrades to your agent</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded-lg p-1 text-faint transition-colors hover:bg-panel-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        <div className="space-y-6 px-5 py-5">
          {RELEASES.map((release) => (
            <section key={release.version}>
              <div className="mb-3 flex items-baseline gap-2">
                <h4 className="platinum font-mono text-xs font-bold">{release.version}</h4>
                <span className="text-2xs text-faint">{release.date}</span>
              </div>
              <ul className="space-y-3">
                {release.items.map((item) => (
                  <li key={item.title} className="border-l border-edge pl-3.5">
                    <p className="text-xs font-semibold text-ink">{item.title}</p>
                    <p className="mt-1 text-2xs leading-relaxed text-dim">{item.body}</p>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
