"""Render the Builder User Guide HTML to PDF + Word via the app's own pipeline."""
import io
import sys
from pathlib import Path

# Derived from this file's location so the script works from any checkout.
ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT / "backend"))
from app.memo import render_pdf, render_word  # noqa: E402

FOOTER = "South River Capital — Builder User Guide"

html = (SCRATCH / "builder_user_guide.html").read_text(encoding="utf-8")
logo = (ROOT / "backend" / "app" / "logo.txt").read_text().strip()
html = html.replace("__LOGO__", logo)

pdf = render_pdf(html, footer_text=FOOTER)
(ROOT / "Builder User Guide.pdf").write_bytes(pdf)

doc = render_word(html, footer_text=FOOTER)
(ROOT / "Builder User Guide.doc").write_bytes(doc)

from pypdf import PdfReader  # noqa: E402
reader = PdfReader(io.BytesIO(pdf))
print(f"PDF pages: {len(reader.pages)}")
for i, page in enumerate(reader.pages, 1):
    text = page.extract_text() or ""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    print(f"  p{i}: {first[:70]}")
print(f"PDF bytes: {len(pdf):,}  DOC bytes: {len(doc):,}")
