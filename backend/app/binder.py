"""Closing Binder assembly.

Merges the deal's executed documents (uploaded as PDFs) into one binder that
follows SRC's closing-binder format (modelled on an executed example) in the
credit memo's visual design:

  page 1                cover — borrower, loan amount, "Closing Binder"
  page 2 (+overflow)    Table of Contents with dot leaders and page ranges;
                        every row is a CLICKABLE LINK to its section
  before each document  a title page (like the loan-docs cover sheets)
  the documents         byte-for-byte untouched

The cover/TOC/title pages render through the same Playwright/Chromium engine
as the memo, in a render pass that also MEASURES each TOC row's on-page
geometry so the rows can be turned into pypdf link annotations afterwards
(screen and print share the same 7in content width, so in-page offsets carry
over; the TOC is paginated here in Python — fixed rows per page, single-line
rows — so the page count is deterministic). The front matter renders WITHOUT
Chromium's "Page X of N" footer — N would count only the front matter, not
the merged documents. PDF outline bookmarks are added per section as well.
"""

from __future__ import annotations

import base64
import io
import math
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, NameObject

from . import memo
from .binder_models import BinderDoc, BinderInfo, BinderPart

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = Path(__file__).parent / "logo.txt"

# Autoescape is ON here (unlike the loandocs template): every context value is
# a plain string — document titles come straight from user input / filenames.
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)

FOOTER_TEXT = "South River Capital — Closing Binder"

# TOC rows per page. Conservative: rows are single-line (nowrap + ellipsis),
# so this bounds the TOC page count deterministically before rendering.
_TOC_ROWS_PER_PAGE = 18

# Letter geometry in PDF points, matching the render margins below.
_PAGE_W, _PAGE_H = 612.0, 792.0
_MARGIN_TOP, _MARGIN_LEFT = 50.4, 54.0     # .7in / .75in
_PX_TO_PT = 0.75                            # CSS px (96dpi) -> pt (72dpi)


def _money(n) -> str:
    if n is None:
        return "—"
    try:
        return f"${round(float(n)):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_long(d: date | None) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else "________________"


def _title_from_filename(name: str) -> str:
    """'Loan_and_Security_Agreement (executed).pdf' -> a readable title."""
    stem = re.sub(r"\.[^.]+$", "", name or "")
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip()


def _read_documents(docs: list[BinderDoc]) -> list[tuple[str, list]]:
    """Decode and parse every upload into (title, pages) per section, failing
    with the offending filename. A section is either a whole file or a list
    of parts (page ranges, possibly spanning several files)."""
    items: list[tuple[str, list]] = []
    readers: dict[str, PdfReader] = {}  # b64 -> parsed reader; the auto-sort
    # flow sends the same signed package once per section it was split into
    for i, d in enumerate(docs):
        parts = d.parts or [BinderPart(filename=d.filename, mime=d.mime, b64=d.b64)]
        title = (d.title.strip() or _title_from_filename(parts[0].filename)
                 or f"Document {i + 1}")
        pages: list = []
        for part in parts:
            label = part.filename or title
            try:
                reader = readers.get(part.b64)
                if reader is None:
                    reader = PdfReader(io.BytesIO(base64.b64decode(part.b64)))
                    if reader.is_encrypted:
                        reader.decrypt("")
                    readers[part.b64] = reader
                count = len(reader.pages)
            except Exception as exc:  # noqa: BLE001 - surface parse failures cleanly
                raise ValueError(
                    f"'{label}' could not be read as a PDF ({exc}). "
                    "The binder takes PDF files only — export or scan the document to PDF first."
                ) from exc
            first = part.page_from or 1
            last = part.page_to or count
            if not (1 <= first <= last <= count):
                raise ValueError(
                    f"'{label}': page range {first}-{last} is outside the file's 1-{count}.")
            pages.extend(reader.pages[first - 1:last])
        if not pages:
            raise ValueError(f"'{title}' has no pages.")
        items.append((title, pages))
    return items


def _toc_entries(lead_pages: int, counts: list[tuple[str, int]],
                 tab_pages: bool) -> list[dict]:
    """One entry per document: its TOC label, the 1-indexed page range shown
    ("3-4"; the range includes the title page, like the executed example),
    and the 0-indexed link/bookmark target (the title page when present).

    lead_pages is the cover + TOC page count only — the title pages are
    interleaved with the documents, so they are counted per entry here."""
    entries = []
    page = lead_pages  # 0-indexed count of pages emitted so far
    for title, count in counts:
        target = page  # title page if tab_pages, else the document itself
        first_shown = page + 1
        page += (1 if tab_pages else 0) + count
        label = str(first_shown) if page == first_shown else f"{first_shown}-{page}"
        entries.append({"title": title, "range_label": label, "target": target})
    return entries


def _front_html(info: BinderInfo, entries: list[dict], tab_pages: bool) -> str:
    toc_pages = [entries[i:i + _TOC_ROWS_PER_PAGE]
                 for i in range(0, len(entries), _TOC_ROWS_PER_PAGE)]
    context = {
        "logo": _LOGO_PATH.read_text().strip(),
        "borrower_name": info.borrower_name or "____________________",
        "loan_amount": _money(info.loan_amount) if info.loan_amount else "",
        "loan_number": info.loan_number,
        "closing_date": _fmt_long(info.closing_date),
        "toc_pages": toc_pages,
        "entries": entries,
        "tab_pages": tab_pages,
    }
    return _env.get_template("closing_binder.html.j2").render(**context)


# Measures every .toc-row's offset within its .page's content box (CSS px).
# Screen pages have the same 7in content width as the printed pages, so these
# offsets are valid in the PDF too.
_MEASURE_JS = """
() => {
  const pages = Array.from(document.querySelectorAll('.page'));
  return Array.from(document.querySelectorAll('.toc-row')).map((el) => {
    const r = el.getBoundingClientRect();
    const pg = el.closest('.page');
    const pr = pg.getBoundingClientRect();
    const cs = getComputedStyle(pg);
    return {
      page: pages.indexOf(pg),
      x: r.left - pr.left - parseFloat(cs.paddingLeft),
      y: r.top - pr.top - parseFloat(cs.paddingTop),
      w: r.width, h: r.height,
    };
  });
}
"""


def _render_front(html: str) -> tuple[bytes, list[dict]]:
    """Render the front matter to PDF and measure the TOC rows in one pass.

    Same engine and page setup as memo.render_pdf, but page.evaluate() runs
    before page.pdf() to capture the row geometry for the link annotations.
    """
    try:
        from playwright.sync_api import sync_playwright  # imported lazily; heavy dep
    except ImportError as exc:
        raise RuntimeError(
            "PDF export needs Playwright. Install it with: "
            "pip install playwright  then  python -m playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                rows = page.evaluate(_MEASURE_JS)
                pdf = page.pdf(
                    format="Letter",
                    print_background=True,
                    display_header_footer=True,
                    header_template=memo._PDF_HEADER_TEMPLATE,
                    footer_template=memo._pdf_footer_template(FOOTER_TEXT, page_numbers=False),
                    margin={"top": "0.7in", "bottom": "0.6in", "left": "0.75in", "right": "0.75in"},
                )
            finally:
                browser.close()
    except Exception as exc:  # browser missing, launch failure, render error
        raise RuntimeError(
            f"PDF rendering failed: {exc}. If the browser is missing, install it "
            "with: python -m playwright install chromium"
        ) from exc
    return pdf, rows


def _link_rect(row: dict) -> tuple[float, float, float, float]:
    """A measured CSS-px row box -> a PDF-points rectangle (origin bottom-left)."""
    x0 = _MARGIN_LEFT + row["x"] * _PX_TO_PT
    x1 = x0 + row["w"] * _PX_TO_PT
    y_top = _MARGIN_TOP + row["y"] * _PX_TO_PT
    return (x0, _PAGE_H - y_top - row["h"] * _PX_TO_PT, x1, _PAGE_H - y_top)


def build_binder(info: BinderInfo, docs: list[BinderDoc], tab_pages: bool = True) -> bytes:
    if not docs:
        raise ValueError("No documents provided.")
    items = _read_documents(docs)
    counts = [(title, len(pages)) for title, pages in items]

    # Front matter is deterministic: 1 cover page + the TOC pages (fixed rows
    # per page) + one title page per document when enabled.
    toc_page_count = math.ceil(len(items) / _TOC_ROWS_PER_PAGE)
    front_pages = 1 + toc_page_count + (len(items) if tab_pages else 0)
    entries = _toc_entries(1 + toc_page_count, counts, tab_pages)

    front_pdf, rows = _render_front(_front_html(info, entries, tab_pages))
    front = PdfReader(io.BytesIO(front_pdf))
    if len(front.pages) != front_pages or len(rows) != len(entries):
        raise RuntimeError(
            "Binder front matter did not paginate as expected "
            f"({len(front.pages)} pages vs {front_pages} planned) — "
            "a document title may be overflowing the Table of Contents."
        )

    writer = PdfWriter()
    for p in front.pages[: 1 + toc_page_count]:
        writer.add_page(p)
    writer.add_outline_item("Cover", 0)
    writer.add_outline_item("Table of Contents", 1)
    for i, ((title, pages), entry) in enumerate(zip(items, entries)):
        if tab_pages:
            writer.add_page(front.pages[1 + toc_page_count + i])
        for p in pages:
            writer.add_page(p)
        # after the pages exist in the writer — a bookmark added before its
        # target page resolves to a dead destination
        writer.add_outline_item(title, entry["target"])

    # The clickable Table of Contents: one link annotation per measured row,
    # jumping to that section's title page (or first page, without tabs).
    # row["page"] indexes the front matter's .page divs (0 = cover), which is
    # exactly the writer's page order for the cover + TOC pages.
    for row, entry in zip(rows, entries):
        writer.add_annotation(
            page_number=row["page"],
            annotation=Link(rect=_link_rect(row), target_page_index=entry["target"]),
        )
        # pypdf serializes target_page_index as a bare page NUMBER in /Dest,
        # which is only valid for remote go-to links — Adobe Reader won't
        # follow it inside the same document. Patch the freshly added
        # annotation to reference the page object itself.
        annot = writer.pages[row["page"]]["/Annots"][-1].get_object()
        annot[NameObject("/Dest")] = ArrayObject([
            writer.pages[entry["target"]].indirect_reference, NameObject("/Fit")])

    writer.add_metadata({
        "/Title": f"Closing Binder — {info.borrower_name or 'Borrower'}",
        "/Author": "South River Capital, LLC",
    })
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
