"""Claude-powered helpers for the Closing Binder.

Two jobs, both reusing the shared usage-token call from the Loan Documents
extractor:
- Step 1's deal-info reader: pull the four cover-page fields from whatever
  deal documents the user drops.
- Step 2's auto-sort: read the SIGNED closing package (and any insurance
  PDFs) and split it into the binder's standard sections with page ranges,
  ordered like SRC's executed example binder.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from .models import UploadedDoc
from .extraction import LEGAL_NAME_RULE
from .loandocs_extraction import _ask_claude


class BinderInfoExtraction(BaseModel):
    """The binder cover fields Claude pulls from the uploaded documents."""

    borrower_name: Optional[str] = None
    loan_amount: Optional[float] = None
    loan_number: Optional[str] = None
    closing_date: Optional[str] = None   # ISO yyyy-mm-dd for the date picker
    notes: Optional[str] = None


PROMPT = """You are an analyst at South River Capital LLC assembling the CLOSING BINDER for a loan to a professional athlete — a single PDF that compiles the executed deal documents behind a cover page and table of contents. You have been given one or more deal documents: possibly a South River credit memorandum, the closing documents (promissory note, loan and security agreement, memo of settlement, ...), a term sheet, or executed/scanned versions of these.

Extract the binder's COVER PAGE fields. Return ONLY raw JSON, no markdown, no backticks:

{"borrower_name":null,"loan_amount":0,"loan_number":null,"closing_date":null,"notes":null}

Rules:
- Use null for anything not stated in the documents (0 for loan_amount). Do not invent values.
- borrower_name: the borrower/athlete the loan is made to.
- loan_amount: the loan / proposed facility principal as a plain number with no "$" or commas (e.g. 785000).
- loan_number: the loan or account number if shown, digits as a string; many documents omit it.
- closing_date: the loan's closing date, formatted "YYYY-MM-DD". Prefer an explicitly stated Closing Date (the closing documents' cover or dating clauses); the date the documents were executed or a credit memo's proposed funding date is acceptable — say which you used in notes. Null if no date is stated; never guess.
- notes: one or two short sentences flagging anything missing, ambiguous, or conflicting (e.g. "No loan number in the documents", "Closing date taken from the memo's proposed funding date"). Null if nothing notable.

""" + LEGAL_NAME_RULE


def extract_binder_info(docs: list[UploadedDoc]) -> BinderInfoExtraction:
    """Binder cover fields from the uploaded deal documents."""
    return BinderInfoExtraction(**_ask_claude(docs, PROMPT, max_tokens=600))


# --- Auto-sort the signed closing package into binder sections ---------------

class SortPart(BaseModel):
    """A page range within one of the uploaded files (both 1-indexed)."""

    file_index: int
    page_from: int
    page_to: int


class SortSection(BaseModel):
    """One proposed binder section: its TOC title and the page ranges
    (possibly from several files) that make it up."""

    title: str
    parts: list[SortPart]


class BinderSortResult(BaseModel):
    sections: list[SortSection] = Field(default_factory=list)
    notes: Optional[str] = None


# The binder's section order, from SRC's executed example: closing documents
# in signing order, anything unrecognized after them, insurance always last.
_CATEGORY_ORDER = [
    ("affidavit", "Business Entity Affidavit"),
    ("note", "Promissory Note"),
    ("repayment_schedule", "Repayment Schedule"),
    ("lsa", "Loan and Security Agreement"),
    ("settlement", "Memo of Settlement"),
    ("ucc", "UCC"),
    ("direction_letter", "Direction Letter"),
]
_RANK = {cat: i for i, (cat, _) in enumerate(_CATEGORY_ORDER)}
# The Guaranty is filed together with the LSA, directly under the
# "Loan and Security Agreement" title page — one section, one TOC row
# (Lauren, 2026-07-13; the executed example has no separate Guaranty tab).
_MERGE_INTO = {"guaranty": "lsa"}
# Unrecognized ("other") sections go after the closing documents, in page
# order; insurance is always the last tab, all files merged into one section.


SORT_PROMPT_HEADER = """You are an analyst at South River Capital LLC assembling the CLOSING BINDER for a loan to a professional athlete. You have been given the uploaded PDF file(s) listed below, in upload order. Together they contain the SIGNED/EXECUTED closing package and possibly separate insurance documents.

Files:
"""

SORT_PROMPT_BODY = """
Every page of every file has been STAMPED by the system with a red label at the top: "FILE i - PAGE n / N", where i is the file's number in the list above and n is the page within that file. READ THE STAMP ON EACH PAGE and take file_index/first_page/last_page from the stamps — never by counting pages yourself.

Identify every distinct document and exactly which pages it spans. Return ONLY raw JSON, no markdown, no backticks:

{"documents":[{"file_index":1,"first_page":1,"last_page":2,"category":"package_cover","title":null}],"notes":null}

Rules:
- file_index is 1-based in the upload order listed above. first_page/last_page are 1-based page numbers WITHIN that file, inclusive, read from the stamps.
- Account for EVERY page of every file exactly once — no gaps, no overlaps. Attach a blank or unidentifiable page to the document it most likely belongs with and mention it in notes.
- CLASSIFY EACH PAGE BY WHAT IS PRINTED ON IT, never by an expected pattern. These are scans of a signed set: a page can be missing (double feed) or scanned twice, so the usual rhythm (cover, title sheet, document, title sheet, document, ...) CAN BREAK. A title sheet is a nearly blank page showing only a document's name (possibly with the borrower's name, a logo, or a kicker line). A document's own pages carry body text, tables, or signature blocks. Never call a page a title sheet because one is "due" — look at the page.
- South River's generated packages also print their own footer "PAGE x OF y" on every package page. If those printed numbers skip or repeat between consecutive scanned pages (e.g. PAGE 22 then PAGE 24), the scan is missing or repeating a package page: say exactly that in notes (e.g. "the printed footer skips from 22 to 24 — package page 23 is missing from the scan").
- category must be one of:
  "package_cover" — the closing package's own overall cover/summary page and its document-index pages, AND every standalone title/cover sheet: a page that is mostly blank and shows only a document's name (possibly with the borrower's name, a logo, or a kicker line) announcing the document that follows. A title sheet is ALWAYS its own "package_cover" entry — NEVER included in the following document's span, even though it belongs to that document — because the binder adds its own title pages and keeping them would print two covers per section. This applies equally when a file holds a SINGLE document whose first page is such a cover/title sheet: that first page is "package_cover" too.
  "affidavit" — Business Entity Affidavit (sworn statement).
  "note" — Promissory Note.
  "repayment_schedule" — the Note's repayment/payment schedule (Exhibit A, "Loan Repayments by Month"). Treat it as its OWN document even though it is the Note's exhibit — the binder format separates them.
  "lsa" — Loan and Security Agreement, INCLUDING its Exhibit A definitions.
  "guaranty" — Guaranty.
  "settlement" — Memo of Settlement.
  "ucc" — UCC Financing Statement, including its Exhibit A.
  "direction_letter" — the Payment Direction Letter: the Borrower's letter TO THE TEAM directing contract payments to the lender's account. Other letters and authorizations (bank-information releases, broker authorizations, wire instructions) are NOT the direction letter — classify those "other".
  "insurance" — insurance paperwork: quotes, applications, policies, binders, death/disability/disgrace coverage.
  "duplicate" — a page span that repeats a document that already appears elsewhere in the uploads (for example the same insurance application scanned at the end of the package AND uploaded as its own file). Keep the clearest copy under its real category and mark every other copy "duplicate", saying in notes what it duplicates; duplicates are left out of the binder.
  "other" — anything that is none of the above.
- title: null for the known categories; for "other" give a short title suitable for a table of contents row.
- notes: short sentences about anything ambiguous, missing, duplicated, or unidentifiable. Null if nothing notable."""


def _page_counts(docs: list[UploadedDoc]) -> list[int]:
    counts = []
    for d in docs:
        try:
            reader = PdfReader(io.BytesIO(base64.b64decode(d.b64)))
            if reader.is_encrypted:
                reader.decrypt("")
            counts.append(len(reader.pages))
        except Exception as exc:  # noqa: BLE001 - surface parse failures cleanly
            raise ValueError(
                f"'{d.filename or 'upload'}' could not be read as a PDF ({exc}). "
                "The binder takes PDF files only."
            ) from exc
    return counts


def _stamp_overlay(width: float, height: float, text: str):
    """A page-sized overlay page carrying just the stamp text, top center.

    Built with pypdf primitives (no extra PDF/canvas dependency) and round-
    tripped through a PdfWriter so the object graph is a clean, parsed page.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    size = 12.0
    est_width = 0.6 * size * len(text)          # rough Helvetica-Bold advance
    x = max(6.0, (width - est_width) / 2)
    y = height - size - 6.0
    safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    ops = (f"q\nBT\n/FSTAMP {size:.0f} Tf\n0.8 0 0 rg\n"
           f"1 0 0 1 {x:.2f} {y:.2f} Tm\n({safe}) Tj\nET\nQ\n")
    stream = DecodedStreamObject()
    stream.set_data(ops.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/FSTAMP"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica-Bold"),
            })
        })
    })
    buf = io.BytesIO()
    writer.write(buf)
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


def _stamped_pdf(data: bytes, file_no: int, count: int) -> bytes:
    """A copy of the PDF with "FILE i - PAGE n / N" stamped onto every page."""
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    # pages must belong to the writer before merge_page (pypdf 7 requires it)
    writer = PdfWriter(clone_from=reader)
    for i, page in enumerate(writer.pages, start=1):
        try:
            if (page.rotation or 0) % 360:
                page.transfer_rotation_to_content()
        except Exception:  # noqa: BLE001 - stamp upright pages, keep the rest
            pass
        box = page.mediabox
        overlay = _stamp_overlay(float(box.width), float(box.height),
                                 f"FILE {file_no} - PAGE {i} / {count}")
        page.merge_page(overlay)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _stamped_docs(docs: list[UploadedDoc],
                  page_counts: list[int]) -> list[UploadedDoc]:
    """Classification copies of the uploads for the sort's Claude call.

    Page-range identification across a long scan is exactly where the model
    drifts (a real signed package with a double-fed page put every section
    off by one), so the copy it reads carries the file/page number printed
    on every page. The BINDER still merges the original bytes — these copies
    never leave this module. A file that cannot be stamped is sent as-is.
    """
    out = []
    log = logging.getLogger(__name__)
    for i, (d, n) in enumerate(zip(docs, page_counts), start=1):
        try:
            data = _stamped_pdf(base64.b64decode(d.b64), i, n)
            out.append(UploadedDoc(filename=d.filename, mime="application/pdf",
                                   b64=base64.b64encode(data).decode()))
        except Exception as exc:  # noqa: BLE001 - never fail the sort over a stamp
            log.warning("binder sort: could not stamp %s (%s) — sending it unstamped",
                        d.filename or f"file {i}", exc)
            out.append(d)
    return out


# The audit's category enum: pass 1's categories minus "duplicate" (a single
# page cannot show it repeats a document elsewhere in the uploads).
_AUDIT_CATEGORIES = {cat for cat, _ in _CATEGORY_ORDER} | {
    "package_cover", "guaranty", "insurance", "other"}

AUDIT_PROMPT = """You are verifying the page classification of a signed athlete-loan closing package for South River Capital LLC. This PDF contains SELECTED SINGLE PAGES extracted from the stamped upload(s) — the pages are NOT consecutive, so never infer a page's identity from its neighbors or from any expected document order.

For EACH page: read its red stamp at the top ("FILE i - PAGE n / N") and classify the page from its own printed content ALONE. Return ONLY raw JSON, no markdown, no backticks:

{"pages":[{"file_index":1,"page":26,"category":"package_cover","title":null}]}

- file_index and page come from the page's stamp.
- category must be one of:
  "package_cover" — the package's own cover/index page, OR a title sheet: a nearly blank page showing only a document's name (possibly with the borrower's name, a logo, or a kicker line). A page with body text, tables, a notary block, or signature blocks is NEVER "package_cover".
  "affidavit" — Business Entity Affidavit (sworn statement).
  "note" — Promissory Note.
  "repayment_schedule" — the Note's repayment schedule (Exhibit A, "Loan Repayments by Month").
  "lsa" — Loan and Security Agreement, including its Exhibit A definitions.
  "guaranty" — Guaranty.
  "settlement" — Memo of Settlement.
  "ucc" — UCC Financing Statement, including its Exhibit A.
  "direction_letter" — the Payment Direction Letter: the Borrower's letter TO THE TEAM directing contract payments to the lender's account.
  "insurance" — insurance paperwork.
  "other" — anything else; give a short document title.
- title: null except for "other"."""


def _audit_page_selection(entries: list[dict], page_counts: list[int]) -> list[tuple[int, int]]:
    """The (file, page) list the audit re-reads one page at a time: every page
    called a cover/title sheet, and the first and last page of every document
    span — the places where a pattern-led misread changes the binder."""
    pages: set[tuple[int, int]] = set()
    for e in entries:
        try:
            fi, first, last = int(e.get("file_index")), int(e.get("first_page")), int(e.get("last_page"))
        except (TypeError, ValueError):
            continue
        if not 1 <= fi <= len(page_counts):
            continue
        n = page_counts[fi - 1]
        first, last = max(1, first), min(n, last)
        if first > last:
            continue
        cat = str(e.get("category") or "other")
        if cat == "duplicate":
            continue                      # dropped anyway
        if cat == "package_cover":
            pages.update((fi, p) for p in range(first, last + 1))
        else:
            pages.update({(fi, first), (fi, last)})
    return sorted(pages)[:40]             # bound the audit's size


def _audit_pdf(stamped: list[UploadedDoc], pages: list[tuple[int, int]]) -> UploadedDoc:
    """One PDF holding just the audited pages, taken from the stamped copies
    (each page identifies itself to the model through its stamp)."""
    readers: dict[int, PdfReader] = {}
    writer = PdfWriter()
    for fi, p in pages:
        reader = readers.get(fi)
        if reader is None:
            reader = readers[fi] = PdfReader(io.BytesIO(base64.b64decode(stamped[fi - 1].b64)))
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return UploadedDoc(filename="boundary_pages.pdf", mime="application/pdf",
                       b64=base64.b64encode(buf.getvalue()).decode())


def _apply_audit(entries: list[dict], audited: list[dict],
                 page_counts: list[int]) -> tuple[list[dict], int]:
    """Turn audit verdicts that contradict pass 1 into single-page entries,
    PREPENDED so they claim their page first — _organize's no-page-twice
    trimming then shaves that page off the original span deterministically."""
    implied: dict[tuple[int, int], str] = {}
    for e in entries:
        try:
            fi, first, last = int(e.get("file_index")), int(e.get("first_page")), int(e.get("last_page"))
        except (TypeError, ValueError):
            continue
        if not 1 <= fi <= len(page_counts):
            continue
        cat = str(e.get("category") or "other")
        for p in range(max(1, first), min(page_counts[fi - 1], last) + 1):
            implied.setdefault((fi, p), cat)

    corrections, seen = [], set()
    for a in audited:
        try:
            fi, p = int(a.get("file_index")), int(a.get("page"))
        except (TypeError, ValueError):
            continue
        cat = str(a.get("category") or "")
        if cat not in _AUDIT_CATEGORIES or (fi, p) in seen:
            continue
        seen.add((fi, p))
        before = implied.get((fi, p))
        if before is None or before == cat or before == "duplicate":
            continue
        # The audit settles exactly ONE question: title sheet or content.
        # Between two CONTENT categories pass 1 keeps the call — it saw the
        # whole file, and a page read alone cannot attribute itself (a fee
        # agreement's own Exhibit A schedule looks like "repayment_schedule",
        # a generic borrower affidavit looks like "affidavit").
        if before != "package_cover" and cat != "package_cover":
            continue
        corrections.append({"file_index": fi, "first_page": p, "last_page": p,
                            "category": cat, "title": a.get("title")})
    return corrections + entries, len(corrections)


def _organize(entries: list[dict], page_counts: list[int]) -> tuple[list[SortSection], list[str]]:
    """Claude's raw page-range list -> ordered binder sections + extra notes.

    Deterministic on purpose: canonical section order, all insurance ranges
    merged into one final section, package cover sheets dropped, and any
    pages Claude failed to assign reported so nothing goes missing silently.
    """
    notes: list[str] = []
    covered = [set() for _ in page_counts]
    by_category: dict[str, list[SortPart]] = {}  # known categories merge into
    # ONE section each (an LSA reported as body + exhibit stays one section)
    others: list[tuple] = []                     # ((file, first), section)
    insurance_parts: list[SortPart] = []
    cover_pages_dropped = 0                      # confirmed to the user below

    for e in entries:
        try:
            fi = int(e.get("file_index"))
            first = int(e.get("first_page"))
            last = int(e.get("last_page"))
        except (TypeError, ValueError):
            notes.append(f"Skipped an unreadable range entry: {e!r}.")
            continue
        cat = str(e.get("category") or "other")
        cat = _MERGE_INTO.get(cat, cat)
        if not 1 <= fi <= len(page_counts):
            notes.append(f"Skipped a range for a file that wasn't uploaded (file {fi}).")
            continue
        n = page_counts[fi - 1]
        first, last = max(1, first), min(n, last)
        if first > last:
            notes.append(f"Skipped an empty page range in file {fi}.")
            continue
        if cat in ("package_cover", "duplicate"):
            # dropped from the binder but the pages still count as accounted
            # for: the binder adds its own cover/title pages, and a duplicate
            # is deliberately kept once (under its real category) only
            covered[fi - 1].update(range(first, last + 1))
            if cat == "duplicate":
                notes.append(f"Left out file {fi} p.{first}-{last} — a duplicate "
                             "copy of a document already in the binder.")
            else:
                cover_pages_dropped += last - first + 1
            continue
        # No page may reach the binder twice: trim away pages another section
        # already claimed (the model does return overlaps despite the prompt).
        wanted = [p for p in range(first, last + 1) if p not in covered[fi - 1]]
        if not wanted:
            notes.append(f"Skipped file {fi} p.{first}-{last} — those pages are "
                         "already in another section.")
            continue
        if len(wanted) != last - first + 1:
            notes.append(f"Trimmed file {fi} p.{first}-{last} to drop pages "
                         "already in another section.")
        covered[fi - 1].update(wanted)
        runs, start, prev = [], wanted[0], wanted[0]
        for p in wanted[1:]:
            if p != prev + 1:
                runs.append((start, prev))
                start = p
            prev = p
        runs.append((start, prev))
        parts = [SortPart(file_index=fi, page_from=a, page_to=b) for a, b in runs]
        if cat == "insurance":
            insurance_parts.extend(parts)
        elif cat in _RANK:
            by_category.setdefault(cat, []).extend(parts)
        else:
            title = str(e.get("title") or "").strip() or "Document"
            others.append(((fi, parts[0].page_from), SortSection(title=title, parts=parts)))

    def _coalesced(parts: list[SortPart]) -> list[SortPart]:
        """Sorted, with contiguous ranges of the same file merged into one."""
        merged: list[SortPart] = []
        for p in sorted(parts, key=lambda p: (p.file_index, p.page_from)):
            last = merged[-1] if merged else None
            if last and last.file_index == p.file_index and p.page_from <= last.page_to + 1:
                last.page_to = max(last.page_to, p.page_to)
            else:
                merged.append(p)
        return merged

    if cover_pages_dropped:
        notes.insert(0, f"Removed {cover_pages_dropped} cover/title page(s) from "
                        "the uploads — the binder adds its own title page in "
                        "front of each document.")

    sections = []
    for cat, title in _CATEGORY_ORDER:
        parts = by_category.get(cat)
        if parts:
            sections.append(SortSection(title=title, parts=_coalesced(parts)))
    others.sort(key=lambda kv: kv[0])
    sections.extend(s for _, s in others)
    if insurance_parts:
        sections.append(SortSection(title="Insurance Documents",
                                    parts=_coalesced(insurance_parts)))

    for i, (count, got) in enumerate(zip(page_counts, covered)):
        missing = sorted(set(range(1, count + 1)) - got)
        if missing:
            runs, start = [], missing[0]
            for a, b in zip(missing, missing[1:] + [None]):
                if b != a + 1:
                    runs.append(str(start) if start == a else f"{start}-{a}")
                    start = b
            notes.append(f"Pages {', '.join(runs)} of file {i + 1} were not assigned "
                         "to any section — add them manually if they belong in the binder.")
    return sections, notes


def sort_documents(docs: list[UploadedDoc]) -> BinderSortResult:
    """Split the uploaded signed package (+ insurance PDFs) into ordered
    binder sections with page ranges.

    Two Claude passes: the ranges, then a page-by-page AUDIT of the boundary
    pages (title sheets and every span's first/last page) sent as isolated
    single pages. Scans of signed sets break the cover/title/document rhythm
    (a double-fed page put a real package's whole tail off by one, filing the
    executed Memo of Settlement as a title sheet), and pass 1 leans on that
    rhythm no matter how emphatically the prompt says not to — a page read
    alone has no rhythm to lean on. The audit is best-effort: if it fails,
    the pass-1 split stands.
    """
    page_counts = _page_counts(docs)
    listing = "".join(
        f"Document {i + 1}: {d.filename or f'upload {i + 1}'} ({n} pages)\n"
        for i, (d, n) in enumerate(zip(docs, page_counts)))
    stamped = _stamped_docs(docs, page_counts)
    data = _ask_claude(stamped, SORT_PROMPT_HEADER + listing + SORT_PROMPT_BODY,
                       max_tokens=4000)
    entries = data.get("documents") or []
    audit_notes: list[str] = []
    try:
        audit_pages = _audit_page_selection(entries, page_counts)
        if audit_pages:
            audit = _ask_claude([_audit_pdf(stamped, audit_pages)], AUDIT_PROMPT,
                                max_tokens=2000)
            entries, corrected = _apply_audit(entries, audit.get("pages") or [],
                                              page_counts)
            if corrected:
                audit_notes.append(
                    f"Re-read {len(audit_pages)} boundary page(s) one by one and "
                    f"corrected {corrected} of them.")
    except Exception as exc:  # noqa: BLE001 - the audit must never break the sort
        logging.getLogger(__name__).warning("binder sort: page audit failed: %s", exc)
        audit_notes.append("The page-by-page double-check could not run — review "
                           "the section boundaries before generating.")
    sections, extra = _organize(entries, page_counts)
    notes = " ".join(x for x in [str(data.get("notes") or "").strip() or None,
                                 *audit_notes, *extra] if x)
    return BinderSortResult(sections=sections, notes=notes or None)
