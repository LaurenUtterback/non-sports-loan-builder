"""Closing Binder assembly tests.

The binder renders its cover/TOC/title pages through the real Playwright
pipeline and merges the uploads with pypdf, so most of these run the pipeline
end-to-end with tiny generated PDFs and verify the page math via the outline
bookmarks and the TOC's clickable link annotations.

Binder layout (modelled on an executed example binder): page 1 cover, page 2+
Table of Contents (every row a link), then per document an optional title
page followed by the document itself, untouched.
"""

import base64
import io

import pytest
from pypdf import PdfReader, PdfWriter

from app import binder
from app import binder_extraction
from app.binder_models import BinderDoc, BinderInfo, BinderPart
from app.models import UploadedDoc


def _pdf_b64(pages: int, width: float = 612) -> str:
    """A blank PDF; a custom width marks its pages so slicing is checkable."""
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=width, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return base64.b64encode(buf.getvalue()).decode()


def _docs():
    return [
        BinderDoc(title="Promissory Note", filename="note.pdf", b64=_pdf_b64(2)),
        # blank title -> must fall back to a cleaned-up filename
        BinderDoc(title="", filename="Loan_and_Security_Agreement.pdf", b64=_pdf_b64(3)),
    ]


def _outline_pages(reader: PdfReader) -> dict[str, int]:
    return {item.title: reader.get_destination_page_number(item)
            for item in reader.outline}


def _toc_links(reader: PdfReader, page_index: int = 1) -> list[int]:
    """Target page (0-indexed) of every /Link GoTo annotation on a page."""
    annots = reader.pages[page_index].get("/Annots")
    if annots is None:
        return []
    targets = []
    for a in annots.get_object():
        o = a.get_object()
        if o.get("/Subtype") != "/Link":
            continue
        action = o.get("/A")
        dest = action.get_object().get("/D") if action else o.get("/Dest")
        first = dest.get_object()[0].get_object()
        # the destination must REFERENCE the page object — a bare page number
        # is only valid for remote go-to and Adobe Reader won't follow it
        assert not isinstance(first, (int, float)), "TOC link uses a bare page number"
        targets.append(reader.get_page_number(first))
    return targets


def test_toc_entries_page_math():
    counts = [("A", 3), ("B", 1)]
    # cover + TOC = 2 lead pages; A = title 3 + doc 4-6, B = title 7 + doc 8
    with_tabs = binder._toc_entries(2, counts, tab_pages=True)
    assert [e["range_label"] for e in with_tabs] == ["3-6", "7-8"]
    assert [e["target"] for e in with_tabs] == [2, 6]
    # no title pages: A = 3-5, B = 6
    without = binder._toc_entries(2, counts, tab_pages=False)
    assert [e["range_label"] for e in without] == ["3-5", "6"]
    assert [e["target"] for e in without] == [2, 5]


def test_binder_with_title_pages_orders_pages_and_bookmarks():
    pdf = binder.build_binder(BinderInfo(borrower_name="Test Borrower"), _docs())
    r = PdfReader(io.BytesIO(pdf))
    # cover + 1 TOC page + (title + 2) + (title + 3)
    assert len(r.pages) == 2 + (1 + 2) + (1 + 3)
    marks = _outline_pages(r)
    assert marks["Cover"] == 0
    assert marks["Table of Contents"] == 1
    assert marks["Promissory Note"] == 2                    # its title page
    assert marks["Loan and Security Agreement"] == 2 + 1 + 2
    # the TOC lists both titles (the blank one from its filename) with ranges
    toc_text = r.pages[1].extract_text().lower()
    assert "table of contents" in toc_text
    assert "promissory note" in toc_text
    assert "loan and security agreement" in toc_text
    assert "3-5" in toc_text and "6-9" in toc_text


def test_binder_toc_rows_are_clickable_links():
    pdf = binder.build_binder(BinderInfo(borrower_name="Test Borrower"), _docs())
    r = PdfReader(io.BytesIO(pdf))
    # one link per document, jumping to that section's title page
    assert _toc_links(r) == [2, 5]
    # no links anywhere else in the front matter
    assert _toc_links(r, page_index=0) == []


def test_binder_without_title_pages():
    pdf = binder.build_binder(BinderInfo(), _docs(), tab_pages=False)
    r = PdfReader(io.BytesIO(pdf))
    assert len(r.pages) == 2 + 2 + 3
    marks = _outline_pages(r)
    assert marks["Promissory Note"] == 2
    assert marks["Loan and Security Agreement"] == 4
    assert _toc_links(r) == [2, 4]


def test_binder_sections_from_page_ranges_and_merged_parts():
    # one "signed package" split into two sections by page range, plus two
    # insurance files merged into a single section — widths mark the sources
    package = _pdf_b64(5, width=100)
    ins1, ins2 = _pdf_b64(1, width=200), _pdf_b64(2, width=300)
    docs = [
        BinderDoc(title="Promissory Note",
                  parts=[BinderPart(filename="pkg.pdf", b64=package, page_from=1, page_to=2)]),
        BinderDoc(title="Loan and Security Agreement",
                  parts=[BinderPart(filename="pkg.pdf", b64=package, page_from=3, page_to=5)]),
        BinderDoc(title="Insurance Documents",
                  parts=[BinderPart(filename="quote.pdf", b64=ins1),
                         BinderPart(filename="policy.pdf", b64=ins2)]),
    ]
    pdf = binder.build_binder(BinderInfo(borrower_name="Test Borrower"), docs, tab_pages=False)
    r = PdfReader(io.BytesIO(pdf))
    assert len(r.pages) == 2 + 2 + 3 + 3
    widths = [round(float(p.mediabox.width)) for p in r.pages[2:]]
    assert widths == [100, 100, 100, 100, 100, 200, 300, 300]
    marks = _outline_pages(r)
    assert marks["Loan and Security Agreement"] == 4
    assert marks["Insurance Documents"] == 7


def test_binder_rejects_bad_page_range():
    doc = BinderDoc(title="Note", parts=[
        BinderPart(filename="pkg.pdf", b64=_pdf_b64(3), page_from=2, page_to=9)])
    with pytest.raises(ValueError) as exc:
        binder.build_binder(BinderInfo(), [doc])
    assert "pkg.pdf" in str(exc.value) and "2-9" in str(exc.value)


def test_organize_orders_sections_and_merges_categories():
    entries = [
        {"file_index": 1, "first_page": 1, "last_page": 1, "category": "package_cover"},
        {"file_index": 1, "first_page": 10, "last_page": 12, "category": "ucc"},
        {"file_index": 1, "first_page": 2, "last_page": 3, "category": "note"},
        {"file_index": 1, "first_page": 4, "last_page": 7, "category": "lsa"},
        # the LSA's Exhibit A reported separately -> must merge into one section
        {"file_index": 1, "first_page": 8, "last_page": 9, "category": "lsa"},
        # the Guaranty files together with the LSA, under its title page
        {"file_index": 1, "first_page": 14, "last_page": 15, "category": "guaranty"},
        {"file_index": 3, "first_page": 1, "last_page": 2, "category": "insurance"},
        {"file_index": 1, "first_page": 13, "last_page": 13, "category": "other",
         "title": "Wire Confirmation"},
        {"file_index": 2, "first_page": 1, "last_page": 4, "category": "insurance"},
    ]
    sections, notes = binder_extraction._organize(entries, [15, 4, 2])
    assert [s.title for s in sections] == [
        "Promissory Note", "Loan and Security Agreement", "UCC",
        "Wire Confirmation", "Insurance Documents"]
    lsa = sections[1].parts
    # contiguous ranges of the same file coalesce (4-7 + 8-9 -> 4-9)
    assert [(p.page_from, p.page_to) for p in lsa] == [(4, 9), (14, 15)]
    ins = sections[-1].parts
    assert [(p.file_index, p.page_from, p.page_to) for p in ins] == [(2, 1, 4), (3, 1, 2)]
    # every page accounted for; the dropped cover is CONFIRMED, never silent
    assert notes == ["Removed 1 cover/title page(s) from the uploads — the binder "
                     "adds its own title page in front of each document."]


def test_organize_drops_duplicates_and_never_repeats_a_page():
    entries = [
        {"file_index": 1, "first_page": 1, "last_page": 4, "category": "note"},
        # overlaps the note -> trimmed to the pages nothing else claimed (5)
        {"file_index": 1, "first_page": 3, "last_page": 5, "category": "settlement"},
        # entirely inside pages already claimed -> skipped, not doubled
        {"file_index": 1, "first_page": 2, "last_page": 3, "category": "ucc"},
        # repeats a document uploaded separately -> left out of the binder
        {"file_index": 1, "first_page": 6, "last_page": 8, "category": "duplicate"},
        {"file_index": 2, "first_page": 1, "last_page": 3, "category": "insurance"},
    ]
    sections, notes = binder_extraction._organize(entries, [8, 3])
    assert [s.title for s in sections] == [
        "Promissory Note", "Memo of Settlement", "Insurance Documents"]
    assert [(p.page_from, p.page_to) for p in sections[1].parts] == [(5, 5)]
    # every page of file 1 is accounted for: claimed once, duplicate, or trimmed
    assert not any("were not assigned" in n for n in notes)
    assert any("duplicate" in n for n in notes)
    assert any("Trimmed file 1 p.3-5" in n for n in notes)
    assert any("Skipped file 1 p.2-3" in n for n in notes)


def test_sort_stamps_every_page_of_the_classification_copy():
    docs = [
        UploadedDoc(filename="pkg.pdf", mime="application/pdf", b64=_pdf_b64(3)),
        UploadedDoc(filename="ins.pdf", mime="application/pdf", b64=_pdf_b64(2)),
    ]
    counts = binder_extraction._page_counts(docs)
    stamped = binder_extraction._stamped_docs(docs, counts)
    r1 = PdfReader(io.BytesIO(base64.b64decode(stamped[0].b64)))
    assert len(r1.pages) == 3
    assert "FILE 1 - PAGE 2 / 3" in r1.pages[1].extract_text()
    r2 = PdfReader(io.BytesIO(base64.b64decode(stamped[1].b64)))
    assert "FILE 2 - PAGE 1 / 2" in r2.pages[0].extract_text()
    # the stamped copies are new documents; the binder merges the originals
    assert stamped[0].b64 != docs[0].b64


def test_sort_prompt_reads_pages_not_patterns():
    body = binder_extraction.SORT_PROMPT_BODY
    assert "STAMPED" in body                      # ranges come from the stamps
    assert '"duplicate"' in body                  # repeated documents drop out
    assert "PAGE x OF y" in body                  # printed-footer skips reported
    assert "CLASSIFY EACH PAGE BY WHAT IS PRINTED ON IT" in body


def test_audit_selects_cover_pages_and_span_edges():
    entries = [
        {"file_index": 1, "first_page": 1, "last_page": 2, "category": "package_cover"},
        {"file_index": 1, "first_page": 3, "last_page": 7, "category": "lsa"},
        {"file_index": 2, "first_page": 1, "last_page": 3, "category": "duplicate"},
        {"file_index": 9, "first_page": 1, "last_page": 1, "category": "ucc"},
    ]
    pages = binder_extraction._audit_page_selection(entries, [7, 3])
    # covers page by page, spans by their edges; duplicates and bad files skipped
    assert pages == [(1, 1), (1, 2), (1, 3), (1, 7)]


def test_audit_corrections_reclaim_misfiled_boundary_pages():
    # The real-world failure shape: a scan missing one page broke the
    # title-sheet rhythm, so pass 1 swallowed the next document's title sheet
    # into the LSA span and dropped the Guaranty's first page as a title sheet.
    entries = [
        {"file_index": 1, "first_page": 1, "last_page": 11, "category": "lsa"},
        {"file_index": 1, "first_page": 12, "last_page": 12, "category": "package_cover"},
        {"file_index": 1, "first_page": 13, "last_page": 14, "category": "guaranty"},
    ]
    audited = [
        {"file_index": 1, "page": 11, "category": "package_cover"},  # title sheet
        {"file_index": 1, "page": 12, "category": "guaranty"},       # body page 1
        {"file_index": 1, "page": 1, "category": "lsa"},             # agrees: no-op
        {"file_index": 1, "page": 12, "category": "note"},           # dup verdict ignored
        {"file_index": 1, "page": 12, "category": "bogus"},          # unknown ignored
        # content-vs-content disagreement: pass 1 saw the whole file and keeps
        # the call — a lone page cannot attribute itself to a document
        {"file_index": 1, "page": 13, "category": "settlement"},
    ]
    fixed, corrected = binder_extraction._apply_audit(audited=audited,
                                                      entries=entries,
                                                      page_counts=[14])
    assert corrected == 2
    sections, notes = binder_extraction._organize(fixed, [14])
    # one LSA section: body trimmed to 1-10, guaranty (12-14) filed under it,
    # and the misread title sheet (11) dropped
    assert [s.title for s in sections] == ["Loan and Security Agreement"]
    assert [(p.page_from, p.page_to) for p in sections[0].parts] == [(1, 10), (12, 14)]
    assert not any("were not assigned" in n for n in notes)


def test_sort_survives_a_failed_audit(monkeypatch):
    calls = []

    def fake_ask(docs, prompt, max_tokens):
        calls.append(prompt)
        if len(calls) == 1:   # pass 1: the ranges
            return {"documents": [
                {"file_index": 1, "first_page": 1, "last_page": 1, "category": "package_cover"},
                {"file_index": 1, "first_page": 2, "last_page": 3, "category": "note"},
            ], "notes": None}
        raise RuntimeError("audit call went down")

    monkeypatch.setattr(binder_extraction, "_ask_claude", fake_ask)
    docs = [UploadedDoc(filename="pkg.pdf", mime="application/pdf", b64=_pdf_b64(3))]
    result = binder_extraction.sort_documents(docs)
    assert len(calls) == 2                       # the audit ran and failed
    assert [s.title for s in result.sections] == ["Promissory Note"]
    assert "double-check could not run" in result.notes


def test_organize_reports_unassigned_pages_and_bad_entries():
    entries = [
        {"file_index": 1, "first_page": 1, "last_page": 2, "category": "note"},
        {"file_index": 9, "first_page": 1, "last_page": 1, "category": "ucc"},
    ]
    sections, notes = binder_extraction._organize(entries, [5])
    assert [s.title for s in sections] == ["Promissory Note"]
    assert any("3-5" in n and "file 1" in n for n in notes)
    assert any("file 9" in n for n in notes)


def test_binder_rejects_non_pdf_with_filename():
    bad = BinderDoc(title="Contract", filename="contract.docx",
                    b64=base64.b64encode(b"this is not a pdf").decode())
    with pytest.raises(ValueError) as exc:
        binder.build_binder(BinderInfo(), [bad])
    assert "contract.docx" in str(exc.value)


def test_binder_requires_documents():
    with pytest.raises(ValueError):
        binder.build_binder(BinderInfo(), [])
