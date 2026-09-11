"""Turning a client's files into knowledge the agent can quote from.

Every shop already has its catalogue written down — as a PDF price list, or a
Word document of policies. Until now onboarding one meant somebody retyping it
into .txt files, which is the actual bottleneck in signing a client, not any
model capability.

Three things here are worth more than the extraction itself.

*Tables are kept as rows.* A price list is a table, and the fact that binds a
product to its price is the row they share. Flattened cell by cell the price
survives and the binding does not, and the agent quotes $89 for the wrong shoe —
a failure that reads as the model hallucinating when it was the parser.

*A scan is reported, not indexed.* A PDF of photographed pages has no text
layer, so extraction returns almost nothing and indexing "succeeds" with an
empty knowledge base. The client then asks why the agent knows nothing about
their products. Better to refuse and say why.

*Text is chunked before embedding.* One vector for a forty-page document points
at the average of everything in it and therefore at nothing in particular. The
splitter works on paragraph boundaries and never cuts a table row in half.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Roughly 250 words. Small enough that a hit is specific, large enough that a
# product and its surrounding detail usually stay together.
TARGET_CHARS = 1200
# Carried from the end of one chunk into the start of the next, so a fact
# sitting on a boundary is retrievable from either side.
OVERLAP_CHARS = 150
# Below this, a chunk is not worth its own row and is folded into the previous.
MIN_CHARS = 120

# A text layer this thin over this many pages means the pages are pictures.
SCAN_CHARS_PER_PAGE = 40

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown")


class UnsupportedDocument(ValueError):
    """The file is not a kind we can read."""


class UnreadableDocument(ValueError):
    """The file is the right kind but there is no text in it."""


@dataclass
class Extracted:
    """What came out of one file."""

    text: str
    kind: str
    pages: int = 0
    tables: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- tables
def _render_table(rows) -> str:
    """One line per row, cells joined — so a product keeps its price.

    The binding between a product and its price is the row. Emitting cells
    down the page loses it, and nothing downstream can put it back.
    """
    lines = []
    for row in rows or []:
        cells = [str(cell).strip().replace("\n", " ") for cell in row if cell is not None]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# ----------------------------------------------------------------------- pdf
def _from_pdf(data: bytes) -> Extracted:
    import pdfplumber

    parts: list[str] = []
    pages = 0
    tables = 0

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages += 1
            found = page.extract_tables() or []
            for table in found:
                rendered = _render_table(table)
                if rendered:
                    parts.append(rendered)
                    tables += 1
            # Tables are extracted separately above; this is the prose around
            # them. Some duplication between the two is harmless — retrieval
            # scores chunks, it does not mind seeing a price twice.
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text.strip())

    body = "\n\n".join(parts)
    result = Extracted(text=body, kind="pdf", pages=pages, tables=tables)

    if pages and len(body) < pages * SCAN_CHARS_PER_PAGE:
        raise UnreadableDocument(
            f"this PDF has {pages} page(s) but almost no text in it, which "
            "usually means the pages are scans or photographs. Text has to be "
            "selectable in the PDF for us to read it — export it from the "
            "original document, or send the price list as a Word file."
        )
    return result


# ---------------------------------------------------------------------- docx
def _from_docx(data: bytes) -> Extracted:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    tables = 0
    for table in document.tables:
        rendered = _render_table([[cell.text for cell in row.cells] for row in table.rows])
        if rendered:
            parts.append(rendered)
            tables += 1

    return Extracted(text="\n\n".join(parts), kind="docx", tables=tables)


# ---------------------------------------------------------------------- plain
def _from_text(data: bytes) -> Extracted:
    return Extracted(text=data.decode("utf-8", errors="replace").strip(), kind="text")


READERS = {
    ".pdf": _from_pdf,
    ".docx": _from_docx,
    ".txt": _from_text,
    ".md": _from_text,
    ".markdown": _from_text,
}


def extract(filename: str, data: bytes) -> Extracted:
    """Read one file. Raises rather than returning something empty."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    reader = READERS.get(suffix)
    if reader is None:
        raise UnsupportedDocument(
            f"{filename}: we can read {', '.join(SUPPORTED)} — not {suffix or 'that'}"
        )

    try:
        result = reader(data)
    except (UnreadableDocument, UnsupportedDocument):
        raise
    except Exception as exc:  # noqa: BLE001 - a corrupt file is the user's problem to see
        raise UnreadableDocument(f"{filename} could not be opened: {exc}") from exc

    if not result.text.strip():
        raise UnreadableDocument(f"{filename} contains no text we could read")
    return result


# -------------------------------------------------------------------- chunking
def _looks_like_table(block: str) -> bool:
    lines = block.splitlines()
    return bool(lines) and sum("|" in line for line in lines) >= max(1, len(lines) // 2)


def chunk(text: str, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split text into passages worth embedding one at a time.

    Paragraph boundaries first, so a chunk is a coherent passage rather than a
    fixed number of characters ending mid-sentence. A block of table rows is
    never split internally: half a price list is worse than two of them.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text or "") if b.strip()]
    if not blocks:
        return []

    chunks: list[str] = []
    current = ""

    for block in blocks:
        # An oversized table is emitted whole. It is better to embed one long
        # chunk than to cut a price away from the product it belongs to.
        if len(block) > target and _looks_like_table(block):
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block)
            continue

        # An oversized paragraph is split on sentence ends.
        if len(block) > target:
            for sentence in re.split(r"(?<=[.!?])\s+", block):
                if len(current) + len(sentence) + 1 > target and current:
                    chunks.append(current)
                    current = current[-overlap:] if overlap else ""
                current = f"{current} {sentence}".strip()
            continue

        if len(current) + len(block) + 2 > target and current:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        current = f"{current}\n\n{block}".strip()

    if current:
        chunks.append(current)

    # A trailing scrap belongs to the passage before it rather than on its own.
    if len(chunks) > 1 and len(chunks[-1]) < MIN_CHARS:
        chunks[-2] = f"{chunks[-2]}\n\n{chunks.pop()}"
    return chunks


def title_for(filename: str, index: int, total: int) -> str:
    """A human-readable name, numbered only when there is more than one part."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = stem.replace("-", " ").replace("_", " ").strip().title() or "Document"
    return stem if total == 1 else f"{stem} ({index + 1}/{total})"
