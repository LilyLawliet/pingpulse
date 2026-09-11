"""Render the client demo guide to a PDF on Drive D:.

The guide is written as a web page first, because that is what the client is
actually sent. This prints the same page to A4 so it can be attached to an
email, handed over in a meeting, or read by someone who will not open a link.

Two things make the printed copy different from the screen copy, and both are
handled here rather than by hand:

  * the page is theme-aware, and a dark-mode laptop would otherwise print a
    black document. The light palette is forced by stamping the root element,
    which is exactly the switch the page's own tokens are built around;
  * screen layout breaks badly across pages — a step split from its number, a
    table row orphaned on its own. The print rules below keep each block whole.

    python scripts/make_demo_pdf.py
    python scripts/make_demo_pdf.py --html other.html --pdf other.pdf

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS = Path(r"D:\pingpulse\docs")
DEFAULT_HTML = DOCS / "PingPulse_Demo_Guide.html"
DEFAULT_PDF = DOCS / "PingPulse_Demo_Guide.pdf"

# Applied only to the printed copy. Kept here rather than in the page so the
# published web version stays exactly what the client sees in a browser.
PRINT_CSS = """
@page { size: A4; margin: 15mm 14mm 16mm; }

html, body { background: #FFFFFF !important; }

.wrap {
  max-width: none;
  padding-inline: 0;
  padding-block: 0 0;
}

.masthead { padding-block: 0 26px; }

/* A heading stranded at the foot of a page reads as a mistake. */
h2, h3 { break-after: avoid-page; }
section { break-inside: auto; padding-block: 30px 0; }

/* Each of these is one object; splitting it across a page break loses the
   relationship the layout is carrying. */
.steps > li,
.option,
.ticks li,
.gains li,
.package,
.note,
.bubble,
.bubble + .why { break-inside: avoid-page; }

.steps, .ticks { break-inside: auto; }

/* The screen version scrolls this sideways; on paper it simply fits. */
.scroll { overflow-x: visible; }
table { min-width: 0; break-inside: auto; }
tr { break-inside: avoid-page; }
thead { display: table-header-group; }

.close { break-inside: avoid-page; }
"""


def render(html: Path, pdf: Path) -> int:
    if not html.is_file():
        print(f"  no such file: {html}", file=sys.stderr)
        return 1

    pdf.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # file:// so the relative nothing-at-all resolves and the Google Fonts
        # link is still fetched over the network.
        page.goto(html.resolve().as_uri(), wait_until="networkidle")

        # Force the light palette. The page guards its dark tokens with
        # :root:not([data-theme="light"]), so this wins in every theme state.
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        page.add_style_tag(content=PRINT_CSS)

        # Web fonts decide line breaks, and line breaks decide page breaks, so
        # printing before they land produces a differently paginated document.
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(400)

        page.pdf(
            path=str(pdf),
            format="A4",
            print_background=True,
            margin={"top": "15mm", "right": "14mm", "bottom": "16mm", "left": "14mm"},
        )
        browser.close()

    size = pdf.stat().st_size
    print(f"  {pdf}")
    print(f"  {size / 1024:.0f} KB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    args = parser.parse_args()
    return render(args.html, args.pdf)


if __name__ == "__main__":
    sys.exit(main())
