"""Compile the client overview Markdown into a print-ready A4 PDF.

`markdown` handles the parsing; headless Chromium does the typesetting and the
PDF write. WeasyPrint was the first choice and is installed, but its GTK native
libraries fail to load on this machine (`OSError 0x7e` on libgobject-2.0-0.dll),
and pdfkit needs a wkhtmltopdf binary that is not present either. Chromium is
already a dependency here, is what produced the existing client PDF, and gives
better control over page breaks and web fonts than any of them.

Screenshots referenced from the Markdown are inlined as base64 so the HTML
and the PDF are both self-contained — no broken image if the folder moves.
Any `[IMAGE PLACEHOLDER: ...]` line still renders as a labelled frame, for
artwork that does not exist yet.

    python scripts/build_client_docs.py

Reads and writes only under D:\\pingpulse\\docs.
"""

from __future__ import annotations

import asyncio
import base64
import html
import pathlib
import re
import sys

import markdown
from playwright.async_api import async_playwright

DOCS_DIR = pathlib.Path(r"D:\pingpulse\docs")

# One pipeline, several documents. They share the stylesheet on purpose: a
# client who reads two of these should not be able to tell they were built at
# different times.
#
#     python scripts/build_client_docs.py                     the overview
#     python scripts/build_client_docs.py PingPulse_Whats_New  a named one
DOCUMENTS = {
    "PingPulse_Client_Overview": {
        "title": "Client Overview<br>&amp; Upgrade",
        "lede": "An end-to-end, multi-tenant AI sales engine — built for any industry.",
        "meta": (
            "Document version 2.0 &nbsp;·&nbsp; 10 September 2026 &nbsp;·&nbsp; "
            "Supersedes all earlier architecture and guide documents"
        ),
    },
    "PingPulse_Alerts_And_Insight": {
        "title": "Knowing What<br>Your Agent Did",
        "lede": "Being told, the board, and the numbers — for when nobody is watching.",
        "meta": (
            "Release 1.3.3 &nbsp;·&nbsp; 17 September 2026 &nbsp;·&nbsp; "
            "Read alongside What Changed, and How to Use It"
        ),
    },
    "PingPulse_User_Guide": {
        "title": "The Guide",
        "lede": "Every screen, what it is for, and what to put in it.",
        "meta": (
            "Release 1.4.8 &nbsp;·&nbsp; 28 September 2026 &nbsp;·&nbsp; "
            "Screenshots from a demonstration account"
        ),
    },
    "PingPulse_Whats_New": {
        "title": "What Changed,<br>and How to Use It",
        "lede": "Everything added in 1.3.x, and the order to set it up in.",
        "meta": (
            "Release 1.3.2 &nbsp;·&nbsp; 11 September 2026 &nbsp;·&nbsp; "
            "Read alongside the Client Overview"
        ),
    },
    "PingPulse_Features_And_Setup": {
        "title": "Everything It Does,<br>and What You Do",
        "lede": "Every feature in plain language, and the twenty minutes of setup it needs from you.",
        "meta": (
            "Release 1.5.1 &nbsp;·&nbsp; 9 October 2026 &nbsp;·&nbsp; "
            "For a new client, before the first customer message"
        ),
    },
}
DEFAULT_DOC = "PingPulse_Client_Overview"

PLACEHOLDER = re.compile(r"^\[IMAGE PLACEHOLDER:\s*(.+?)\]$", re.MULTILINE)

# The product's own palette, so the document and the dashboard look related.
STYLE = """
@page {
  size: A4;
  margin: 18mm 16mm 16mm 16mm;
  @bottom-center { content: counter(page); }
}

:root {
  --ink: #16202b;
  --muted: #5b6b7a;
  --line: #dde5ea;
  --accent: #0f9c78;
  --accent-soft: #eefaf5;
  --navy: #0b1118;
}

* { box-sizing: border-box; }

body {
  font-family: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  color: var(--ink);
  font-size: 10.5pt;
  line-height: 1.62;
  margin: 0;
}

/* ---- cover ------------------------------------------------------------ */
.cover {
  background: var(--navy);
  color: #eef4f8;
  padding: 26mm 18mm 20mm 18mm;
  margin: -18mm -16mm 12mm -16mm;
}
.cover .mark {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 8.5pt;
  letter-spacing: .22em;
  text-transform: uppercase;
  color: #2fd8a8;
  margin-bottom: 10mm;
}
.cover h1 {
  font-size: 30pt;
  line-height: 1.12;
  margin: 0 0 5mm 0;
  color: #ffffff;
  border: 0;
  padding: 0;
}
.cover .lede { font-size: 12pt; color: #b9c8d4; margin: 0 0 8mm 0; }
.cover .meta {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 8.5pt;
  color: #7f93a3;
  border-top: 1px solid #24313d;
  padding-top: 4mm;
}

/* ---- headings --------------------------------------------------------- */
h1, h2, h3 { font-weight: 600; color: var(--navy); }
h2 {
  font-size: 15.5pt;
  margin: 10mm 0 3mm 0;
  padding-bottom: 2mm;
  border-bottom: 2px solid var(--accent);
  break-after: avoid;
}
h3 {
  font-size: 11.5pt;
  margin: 6mm 0 1.5mm 0;
  color: var(--accent);
  break-after: avoid;
}
p { margin: 0 0 3mm 0; }
strong { color: var(--navy); }

ul, ol { margin: 0 0 3mm 0; padding-left: 5mm; }
li { margin-bottom: 1.4mm; }

/* ---- tables ----------------------------------------------------------- */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 3mm 0 5mm 0;
  font-size: 9.5pt;
  break-inside: avoid;
}
th {
  text-align: left;
  background: var(--accent-soft);
  color: var(--navy);
  font-weight: 600;
  padding: 2.2mm 3mm;
  border-bottom: 1.5px solid var(--accent);
}
td { padding: 2.2mm 3mm; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }

/* ---- callouts --------------------------------------------------------- */
blockquote {
  margin: 4mm 0;
  padding: 3.5mm 5mm;
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  border-radius: 0 3px 3px 0;
  color: var(--ink);
  break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }

/* ---- screenshots ------------------------------------------------------ */
figure {
  margin: 4mm 0 6mm 0;
  break-inside: avoid;
}
figure img {
  width: 100%;
  display: block;
  border: 1px solid #c9d6df;
  border-radius: 4px;
}
figcaption {
  font-size: 8.5pt;
  color: var(--muted);
  margin-top: 1.8mm;
  padding-left: 2mm;
  border-left: 2px solid var(--accent);
  line-height: 1.45;
}

/* Fallback frame, only used if an image file is missing. */
.shot {
  border: 1.5px dashed #b7c6d2;
  border-radius: 5px;
  background: repeating-linear-gradient(45deg, #f7fafb 0 8px, #f1f6f8 8px 16px);
  padding: 16mm 8mm;
  margin: 4mm 0;
  text-align: center;
  break-inside: avoid;
}
.shot .tag {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 7.5pt;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--accent);
  display: block;
  margin-bottom: 2.5mm;
}
.shot .caption { font-size: 10pt; color: var(--muted); font-weight: 500; }

hr { border: 0; border-top: 1px solid var(--line); margin: 7mm 0; }

/* Keep a heading with the text under it. */
h2, h3 { page-break-after: avoid; }
"""

COVER_TEMPLATE = """
<div class="cover">
  <div class="mark">PingPulse · WhatsApp AI Sales Agent</div>
  <h1>{title}</h1>
  <p class="lede">{lede}</p>
  <div class="meta">{meta}</div>
</div>
"""


def render_placeholders(text: str) -> str:
    """Swap the placeholder lines for a token Markdown will leave alone."""
    def frame(match: re.Match) -> str:
        caption = html.escape(match.group(1).strip())
        return (
            '<div class="shot">'
            '<span class="tag">Screenshot to be added</span>'
            f'<span class="caption">{caption}</span>'
            "</div>"
        )

    return PLACEHOLDER.sub(frame, text)


IMG_TAG = re.compile(r'<p><img alt="(?P<alt>[^"]*)" src="(?P<src>[^"]+)"\s*/?></p>')


def inline_images(body: str) -> tuple[str, int]:
    """Turn each Markdown image into a captioned, base64-embedded figure.

    Embedding rather than linking keeps the HTML and the PDF self-contained, so
    the document survives being emailed or moved out of this folder.
    """
    embedded = 0

    def figure(match: re.Match) -> str:
        nonlocal embedded
        src = match.group("src")
        alt = html.escape(match.group("alt"))
        path = (DOCS_DIR / src).resolve()
        if not path.is_file():
            print(f"  WARNING: missing image {src}", file=sys.stderr)
            return (
                '<div class="shot"><span class="tag">Screenshot to be added</span>'
                f'<span class="caption">{alt}</span></div>'
            )
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        embedded += 1
        return (
            f'<figure><img alt="{alt}" src="data:image/png;base64,{data}">'
            f"<figcaption>{alt}</figcaption></figure>"
        )

    return IMG_TAG.sub(figure, body), embedded


def build_html(md_text: str, cover: str) -> tuple[str, int]:
    body = markdown.markdown(
        render_placeholders(md_text),
        extensions=["tables", "attr_list", "sane_lists", "md_in_html"],
    )
    body, embedded = inline_images(body)

    # The Markdown title and the metadata line under it are re-set as the cover,
    # so drop the plain-text versions rather than printing them twice.
    body = re.sub(r"<h1>.*?</h1>", "", body, count=1, flags=re.S)
    body = re.sub(
        r"<p><strong>The definitive overview.*?</p>\s*(<hr\s*/?>)?",
        "",
        body,
        count=1,
        flags=re.S,
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PingPulse — Client Overview and Upgrade</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>{STYLE}</style>
</head>
<body>
{cover}
{body}
</body>
</html>"""
    return page, embedded


async def main() -> int:
    stem = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOC
    if stem not in DOCUMENTS:
        known = ", ".join(sorted(DOCUMENTS))
        print(f"unknown document {stem!r} - try one of: {known}", file=sys.stderr)
        return 2

    md_path = DOCS_DIR / f"{stem}.md"
    html_path = DOCS_DIR / f"{stem}.html"
    pdf_path = DOCS_DIR / f"{stem}.pdf"
    if not md_path.is_file():
        print(f"{md_path} is missing", file=sys.stderr)
        return 2

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    md_text = md_path.read_text(encoding="utf-8")
    placeholders = len(PLACEHOLDER.findall(md_text))

    page, embedded = build_html(md_text, COVER_TEMPLATE.format(**DOCUMENTS[stem]))
    html_path.write_text(page, encoding="utf-8")
    print(f"  markdown : {md_path.name} ({len(md_text.splitlines())} lines)")
    print(f"  images   : {embedded} embedded, {placeholders} placeholder frame(s)")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        tab = await browser.new_page()
        await tab.goto(html_path.as_uri(), wait_until="networkidle")
        await tab.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "18mm", "bottom": "16mm", "left": "16mm", "right": "16mm"},
        )
        await browser.close()

    size_kb = pdf_path.stat().st_size / 1024
    print(f"\n  markdown : {md_path}")
    print(f"  pdf      : {pdf_path}  ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
