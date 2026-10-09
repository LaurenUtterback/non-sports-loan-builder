"""Oversized uploads are shrunk to fit, not refused (Lauren, 2026-08-14).

The Messages API hard-caps a request at ~32 MB. check_request_size used to
refuse an over-budget upload outright; now doc_blocks.shrink_blocks_to_fit
first recompresses the images inside the largest PDFs (a scanned bundle stays
a visual document), then swaps still-oversized PDFs for their text layer
(born-digital statements collapse to KBs; scans have no text layer and are
never swapped). Only what still cannot fit is refused, with the same
named-files error as before.
"""

import base64
import io
import os

from PIL import Image
from pypdf import PdfReader

from app import doc_blocks, extraction
from app.models import UploadedDoc


def _noisy_pdf(w: int = 2400, h: int = 1800) -> bytes:
    """A one-page PDF whose page is a large photographic-noise image — the
    JPEG-resistant worst case, like a high-DPI scan."""
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, format="PDF", quality=95)
    return buf.getvalue()


def _as_upload(name: str, raw: bytes):
    b64 = base64.b64encode(raw).decode("ascii")
    doc = UploadedDoc(filename=name, mime="application/pdf", b64=b64)
    block = {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": b64}}
    return doc, block


# --- pass 1: image recompression -----------------------------------------------

def test_recompression_shrinks_a_scanned_pdf_and_keeps_it_visual():
    raw = _noisy_pdf()
    doc, block = _as_upload("Supporting Documents.pdf", raw)

    notes = doc_blocks.shrink_blocks_to_fit([doc], [block], budget_bytes=1)

    assert any("recompressed" in n and "Supporting Documents.pdf" in n
               for n in notes)
    slim = base64.b64decode(block["source"]["data"])
    assert len(slim) < len(raw) * 0.7          # a real reduction, not noise
    assert block["type"] == "document"         # still sent as a visual PDF
    assert len(PdfReader(io.BytesIO(slim)).pages) == 1   # and still a valid one


def test_a_scan_with_no_text_layer_is_never_swapped_for_text():
    # budget_bytes=1 is unreachable, so pass 2 runs too — but a Pillow-written
    # image PDF has no text layer, so the block must stay a document.
    doc, block = _as_upload("DL scan.pdf", _noisy_pdf(1200, 900))
    doc_blocks.shrink_blocks_to_fit([doc], [block], budget_bytes=1)
    assert block["type"] == "document"


def test_within_budget_uploads_are_left_untouched():
    doc, block = _as_upload("PFS.pdf", _noisy_pdf(600, 400))
    before = block["source"]["data"]
    notes = doc_blocks.shrink_blocks_to_fit([doc], [block],
                                            budget_bytes=50_000_000)
    assert notes == [] and block["source"]["data"] == before


def test_unreadable_uploads_are_left_alone_without_raising():
    doc = UploadedDoc(filename="corrupt.pdf", mime="", b64="!!!not-base64!!!")
    block = {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": "!!!not-base64!!!"}}
    notes = doc_blocks.shrink_blocks_to_fit([doc], [block], budget_bytes=1)
    assert notes == [] and block["source"]["data"] == "!!!not-base64!!!"


# --- pass 2: text-layer swap -----------------------------------------------------

def test_text_layer_swap_when_recompression_is_not_enough(monkeypatch):
    monkeypatch.setattr(doc_blocks, "_recompress_pdf", lambda raw: None)
    monkeypatch.setattr(doc_blocks, "_pdf_text",
                        lambda raw: "STATEMENT LINE 1,234.56\n" * 40)

    doc, block = _as_upload("BS Ameriprise.pdf", b"%PDF-1.4 " + b"x" * 10_000)
    notes = doc_blocks.shrink_blocks_to_fit([doc], [block], budget_bytes=100)

    assert block["type"] == "text"
    assert "BS Ameriprise.pdf" in block["text"]      # labeled, like .docx text
    assert "STATEMENT LINE" in block["text"]
    assert any("text layer" in n for n in notes)


# --- the check_request_size wiring ----------------------------------------------

def _doc(name):
    return UploadedDoc(filename=name, mime="application/pdf", b64="")


def test_check_request_size_passes_once_shrunk_under_budget(monkeypatch):
    def fake_shrink(docs, blocks, budget):
        blocks[0]["source"]["data"] = "x" * 1_000
        return ["Supporting Documents.pdf: images recompressed"]
    monkeypatch.setattr(doc_blocks, "shrink_blocks_to_fit", fake_shrink)

    content = [{"type": "document", "source": {"data": "x" * 40_000_000}}]
    extraction.check_request_size(
        "extraction", [_doc("Supporting Documents.pdf")], content)  # must not raise


def test_refusal_says_compression_was_already_tried(monkeypatch):
    monkeypatch.setattr(doc_blocks, "shrink_blocks_to_fit",
                        lambda docs, blocks, budget: [])
    content = [{"type": "document", "source": {"data": "x" * 40_000_000}}]
    import pytest
    with pytest.raises(RuntimeError) as exc:
        extraction.check_request_size("extraction", [_doc("Bundle.pdf")], content)
    msg = str(exc.value)
    assert "even after automatic compression" in msg
    assert "Bundle.pdf" in msg and "too large" in msg and "Step 2" in msg
