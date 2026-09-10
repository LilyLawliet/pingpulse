"""Build the client-facing PDF: what PingPulse does, in plain language.

Renders an A4 document in headless Chromium and prints it to PDF, embedding a
live screenshot of the dashboard so the client sees the real product.

    python scripts/make_client_pdf.py
"""

import asyncio
import base64
import pathlib

from playwright.async_api import async_playwright

OUT_DIR = pathlib.Path(r"D:\pingpulse\demo")
PDF_PATH = OUT_DIR / "PingPulse-for-clients.pdf"
DASHBOARD = "http://localhost:3000"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PingPulse — for clients</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
  :root {
    --paper: #ffffff;
    --band: #f2f8f6;
    --ink: #0e1a22;
    --muted: #5c6e79;
    --accent: #0e9e79;
    --accent-tint: #e4f5ef;
    --rule: #dce6e6;
  }

  @page { size: A4; margin: 14mm 14mm 16mm; }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    font-family: 'IBM Plex Sans', system-ui, sans-serif;
    color: var(--ink);
    background: var(--paper);
    font-size: 10.5pt;
    line-height: 1.55;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }

  h1, h2, h3 { text-wrap: balance; margin: 0; }
  h1 { font-size: 30pt; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; }
  h2 { font-size: 15pt; font-weight: 600; letter-spacing: -0.01em; }
  h3 { font-size: 11pt; font-weight: 600; }
  p { margin: 0; }

  .page { page-break-after: always; }
  .page:last-child { page-break-after: auto; }

  .eyebrow {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 7.5pt;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--accent);
  }

  /* ---------- masthead ---------- */
  .masthead { display: flex; align-items: center; gap: 10px; margin-bottom: 26mm; }
  .mark {
    width: 26px; height: 26px; border-radius: 7px;
    background: var(--accent); color: #fff;
    display: grid; place-items: center;
    font-weight: 700; font-size: 12pt;
  }
  .wordmark { font-weight: 700; font-size: 12pt; letter-spacing: -0.01em; }
  .masthead .role { margin-left: auto; font-size: 8.5pt; color: var(--muted); }

  .lede { font-size: 12.5pt; line-height: 1.5; color: var(--muted); max-width: 62ch; margin-top: 14px; }

  /* ---------- stats ---------- */
  .stats { display: flex; gap: 0; margin: 22px 0 0; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
  .stat { flex: 1; padding: 14px 16px 14px 0; }
  .stat + .stat { padding-left: 16px; border-left: 1px solid var(--rule); }
  .stat b { display: block; font-family: 'IBM Plex Mono', monospace; font-size: 17pt; font-weight: 600; letter-spacing: -0.02em; }
  .stat span { font-size: 8.5pt; color: var(--muted); }

  section { margin-top: 22px; }
  .section-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
  .section-head h2 { flex: 1; }
  .body-copy { max-width: 66ch; color: var(--muted); }
  .body-copy strong { color: var(--ink); font-weight: 600; }

  /* ---------- steps ---------- */
  .steps { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-top: 12px; }
  .step { border: 1px solid var(--rule); border-radius: 9px; padding: 13px 15px; }
  .step .n {
    font-family: 'IBM Plex Mono', monospace; font-size: 8pt; font-weight: 600;
    color: var(--accent); display: block; margin-bottom: 5px;
  }
  .step h3 { margin-bottom: 3px; }
  .step p { font-size: 9.5pt; color: var(--muted); }

  /* ---------- chat ---------- */
  .chat { background: var(--band); border-radius: 11px; padding: 15px; margin-top: 12px; }
  .bubble { max-width: 74%; padding: 8px 11px; border-radius: 11px; font-size: 9.5pt; margin-bottom: 8px; line-height: 1.45; }
  .from-them { background: #fff; border: 1px solid var(--rule); border-top-left-radius: 3px; }
  .from-agent { background: var(--accent-tint); border: 1px solid #c6e8dc; border-top-right-radius: 3px; margin-left: auto; }
  .who {
    display: block; font-family: 'IBM Plex Mono', monospace; font-size: 6.5pt;
    letter-spacing: 0.1em; text-transform: uppercase; color: var(--accent); margin-bottom: 3px;
  }
  .note { font-size: 8.5pt; color: var(--muted); margin-top: 9px; padding-left: 11px; border-left: 2px solid var(--accent); }

  /* ---------- lists ---------- */
  .checks { margin: 10px 0 0; padding: 0; list-style: none; }
  .checks li { position: relative; padding-left: 20px; margin-bottom: 7px; font-size: 10pt; color: var(--muted); }
  .checks li::before {
    content: '\\2713'; position: absolute; left: 0; top: -1px;
    color: var(--accent); font-weight: 700;
  }
  .checks b { color: var(--ink); font-weight: 600; }

  figure { margin: 12px 0 0; }
  figure img { width: 100%; border: 1px solid var(--rule); border-radius: 9px; display: block; }
  figcaption { font-size: 8.5pt; color: var(--muted); margin-top: 7px; }

  .callout { background: var(--accent-tint); border-radius: 10px; padding: 15px 17px; margin-top: 16px; }
  .callout h3 { margin-bottom: 5px; }
  .callout p { font-size: 9.5pt; color: #2c5a4c; }

  footer { margin-top: 22px; padding-top: 11px; border-top: 1px solid var(--rule); font-size: 8.5pt; color: var(--muted); display: flex; }
  footer .right { margin-left: auto; font-family: 'IBM Plex Mono', monospace; }
</style>
</head>
<body>

<!-- ============================ PAGE 1 ============================ -->
<div class="page">
  <div class="masthead">
    <span class="mark">P</span>
    <span class="wordmark">PingPulse</span>
    <span class="role">An AI sales agent for WhatsApp</span>
  </div>

  <span class="eyebrow">What it is</span>
  <h1>Every WhatsApp message<br>answered in a second.</h1>
  <p class="lede">
    PingPulse replies to your customers on WhatsApp the moment they message — with your
    prices, your rules and your tone of voice. It qualifies the lead, answers the awkward
    questions, and hands you people who are ready to buy.
  </p>

  <div class="stats">
    <div class="stat"><b>~1 sec</b><span>Average reply time</span></div>
    <div class="stat"><b>24 / 7</b><span>Including nights and weekends</span></div>
    <div class="stat"><b>0</b><span>Messages left unanswered</span></div>
  </div>

  <section>
    <div class="section-head"><span class="eyebrow">The problem</span></div>
    <h2>Customers message when you are busy. Then they buy elsewhere.</h2>
    <p class="body-copy" style="margin-top:8px">
      Most enquiries arrive outside the moments you are free to answer — mid-service, mid-commute,
      late at night. A reply an hour later reaches someone who has already asked two competitors.
      The enquiry was never the problem. <strong>The wait was.</strong>
    </p>
  </section>

  <section>
    <div class="section-head"><span class="eyebrow">How it works</span></div>
    <h2>Four steps, and none of them are yours.</h2>
    <div class="steps">
      <div class="step">
        <span class="n">01</span>
        <h3>You describe your business, once</h3>
        <p>Your products and prices, how you want to sound, and what the agent should never say.</p>
      </div>
      <div class="step">
        <span class="n">02</span>
        <h3>A customer messages your WhatsApp</h3>
        <p>Same number they already use. Nothing for them to install, download or sign up for.</p>
      </div>
      <div class="step">
        <span class="n">03</span>
        <h3>The agent answers straight away</h3>
        <p>It reads the whole conversation, checks your price list, and replies in about a second.</p>
      </div>
      <div class="step">
        <span class="n">04</span>
        <h3>You see it happen live</h3>
        <p>Every conversation on one screen, with each customer sorted into where they are in the pipeline.</p>
      </div>
    </div>
  </section>

  <footer>
    <span>PingPulse — AI sales agent for WhatsApp</span>
    <span class="right">1 / 3</span>
  </footer>
</div>

<!-- ============================ PAGE 2 ============================ -->
<div class="page">
  <section style="margin-top:0">
    <div class="section-head"><span class="eyebrow">In practice</span></div>
    <h2>It answers like someone who works there.</h2>
    <p class="body-copy" style="margin-top:8px">
      A real exchange, generated by the agent — not written by hand. The customer never says
      which shoe she means. The agent knows, because it read the conversation.
    </p>

    <div class="chat">
      <div class="bubble from-them">Are the heels real leather?</div>
      <div class="bubble from-agent">
        <span class="who">AI agent</span>
        Yes, all our women's heels are genuine leather. The Kitten Heel in black or nude is
        PKR 8,900 if you like. Which size do you wear?
      </div>
      <div class="bubble from-them">I love the black pair, how much are they?</div>
      <div class="bubble from-agent">
        <span class="who">AI agent</span>
        The black Kitten Heel is PKR 8,900. Since that's under the free delivery threshold,
        shipping adds PKR 350. Which size do you need so I can hold a pair for you?
      </div>
      <div class="bubble from-them">Perfect, I want to buy them.</div>
      <div class="bubble from-agent">
        <span class="who">AI agent</span>
        Wonderful! I can hold those black Kitten Heels for you for 48 hours. Just let me know
        your size and delivery address so I can process the order.
      </div>
      <p class="note">
        "The black pair" was never named. The agent resolved it to the Kitten Heel it had
        just quoted, kept the price consistent, and worked out the delivery charge itself.
      </p>
    </div>
  </section>

  <section>
    <div class="section-head"><span class="eyebrow">Memory</span></div>
    <h2>It remembers your customers.</h2>
    <p class="body-copy" style="margin-top:8px">
      When someone mentions their size, their city or what they are shopping for, the agent keeps it.
      Not just for that chat — for the next one too, weeks later. Nobody is asked the same question twice.
    </p>
    <ul class="checks">
      <li><b>Their size</b> — so an order can be confirmed without going back and forth</li>
      <li><b>Their city</b> — so delivery time and cost are right the first time</li>
      <li><b>What they were looking at</b> — so a returning customer picks up where they left off</li>
    </ul>
  </section>

  <section>
    <div class="section-head"><span class="eyebrow">Safety</span></div>
    <h2>It will not make things up.</h2>
    <p class="body-copy" style="margin-top:8px">
      The risk with an AI answering customers is that it invents a price or promises something you
      cannot honour. PingPulse is built so that cannot happen quietly.
    </p>
    <ul class="checks">
      <li>Every price it quotes is <b>checked against your list</b> before the message is sent</li>
      <li>If a product is not something you sell, it <b>says so</b> instead of improvising</li>
      <li>If it is ever unsure which item you mean, it <b>asks</b> rather than guessing</li>
      <li>If something goes wrong behind the scenes, the customer gets a <b>polite holding reply</b>, never a broken one</li>
    </ul>
  </section>

  <footer>
    <span>PingPulse — AI sales agent for WhatsApp</span>
    <span class="right">2 / 3</span>
  </footer>
</div>

<!-- ============================ PAGE 3 ============================ -->
<div class="page">
  <section style="margin-top:0">
    <div class="section-head"><span class="eyebrow">Your dashboard</span></div>
    <h2>Everything happening, on one screen.</h2>
    <p class="body-copy" style="margin-top:8px">
      Open it and watch conversations arrive in real time. Read any thread, see who is close to
      buying, and step in yourself whenever you want to.
    </p>
    <figure>
      <img src="{screenshot}" alt="The PingPulse dashboard showing live WhatsApp conversations and the sales pipeline">
      <figcaption>
        Live conversations on the left, the full thread in the middle, and your sales pipeline on
        the right — new leads through to won.
      </figcaption>
    </figure>
  </section>

  <section>
    <div class="section-head"><span class="eyebrow">Getting started</span></div>
    <h2>What we need from you.</h2>
    <ul class="checks">
      <li><b>Your product and price list</b> — however you already keep it</li>
      <li><b>A WhatsApp business number</b> — we handle the connection</li>
      <li><b>A sense of how you speak to customers</b> — formal, warm, playful; the agent matches it</li>
      <li><b>About an hour of your time</b> — then it runs on its own</li>
    </ul>
  </section>

  <div class="callout">
    <h3>You stay in control</h3>
    <p>
      Change a price, adjust the tone, or add a rule whenever you like — it takes effect on the
      very next message. Every conversation is yours to read, and you can take over any chat
      yourself at any point.
    </p>
  </div>

  <footer>
    <span>PingPulse — AI sales agent for WhatsApp</span>
    <span class="right">3 / 3</span>
  </footer>
</div>

</body>
</html>
"""


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Capture the running dashboard so the client sees the real product.
        shot_page = await browser.new_page(viewport={"width": 1600, "height": 900})
        try:
            await shot_page.goto(DASHBOARD, wait_until="networkidle")
            await shot_page.wait_for_timeout(3000)
            # Open a conversation — an empty thread pane sells nothing.
            rows = shot_page.locator("section:has-text('Conversations') button")
            if await rows.count():
                await rows.nth(0).click()
                await shot_page.wait_for_timeout(2000)
            shot = await shot_page.screenshot(type="png")
            encoded = "data:image/png;base64," + base64.b64encode(shot).decode()
        except Exception as exc:  # noqa: BLE001
            print(f"could not screenshot the dashboard ({exc}); leaving it out")
            encoded = ""
        await shot_page.close()

        page = await browser.new_page()
        await page.set_content(PAGE.replace("{screenshot}", encoded), wait_until="networkidle")
        await page.wait_for_timeout(1500)  # let the webfonts settle
        await page.pdf(
            path=str(PDF_PATH),
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        await browser.close()

    print(f"{PDF_PATH}  ({PDF_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    asyncio.run(main())
