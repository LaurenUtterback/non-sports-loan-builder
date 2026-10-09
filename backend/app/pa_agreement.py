"""Generate the Participation Agreement.

* Word (.docx): fill ``templates/participation_agreement.docx`` with the
  confirmed terms using docxtpl (pure Python — no Word/Office required).
* PDF: convert that exact filled .docx with LibreOffice headless, so the PDF is
  a faithful rendering of the Word document (single source of truth).

LibreOffice is located via ``SOFFICE_PATH`` (env) or a few well-known locations,
including a no-admin copy extracted under %LOCALAPPDATA%\\CreditMemoBuilder. If
it isn't found, .docx still works and the PDF path raises a clear message.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from docxtpl import DocxTemplate

from .pa_models import PATerms

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATES = {
    "brookridge": _TEMPLATE_DIR / "participation_agreement_brookridge.docx",
    "standard": _TEMPLATE_DIR / "participation_agreement_standard.docx",
}
DEFAULT_TYPE = "brookridge"


def template_path(agreement_type: str | None) -> Path:
    """Resolve an agreement type to its template file (falls back to brookridge)."""
    return _TEMPLATES.get((agreement_type or DEFAULT_TYPE).lower(), _TEMPLATES[DEFAULT_TYPE])


def templates_present() -> dict:
    return {k: v.exists() for k, v in _TEMPLATES.items()}


# "Send out for signature" prefills. The SRC signer's email and the
# e-signature site's name/URL are deployment details that live ONLY in .env
# (the repo is public) — never hard-code them.
_ESIGN_ENV = {
    "signer_name": "SRC_SIGNER_NAME",
    "signer_email": "SRC_SIGNER_EMAIL",
    "esign_name": "ESIGN_NAME",
    "esign_url": "ESIGN_URL",
}


def esign_defaults() -> dict:
    """SRC signer + e-signature site defaults from the environment (UI prefill)."""
    out = {field: os.environ.get(env, "") for field, env in _ESIGN_ENV.items()}
    # The agreement itself already prints the SRC signatory's name.
    out["signer_name"] = out["signer_name"] or "James Plack"
    return out

_SOFFICE_CANDIDATES = [
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "CreditMemoBuilder", "libreoffice", "program", "soffice.exe",
    ),
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def find_soffice() -> str | None:
    """Return a path to the LibreOffice ``soffice`` binary, or None if not found."""
    env = os.environ.get("SOFFICE_PATH")
    if env and Path(env).exists():
        return env
    for cand in _SOFFICE_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    return shutil.which("soffice") or shutil.which("soffice.exe")


def pdf_available() -> bool:
    return find_soffice() is not None


def render_docx(terms: PATerms, agreement_type: str | None = DEFAULT_TYPE) -> bytes:
    """Fill the chosen template with ``terms`` and return the .docx bytes."""
    tpl_path = template_path(agreement_type)
    if not tpl_path.exists():
        raise RuntimeError(
            f"Template missing: {tpl_path}. Rebuild it with tools/build_pa_template.py."
        )
    tpl = DocxTemplate(str(tpl_path))
    tpl.render(terms.model_dump())
    import io

    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def render_pdf(terms: PATerms, agreement_type: str | None = DEFAULT_TYPE) -> bytes:
    """Fill the chosen template and convert the resulting .docx to PDF via LibreOffice."""
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "PDF export needs LibreOffice, which isn't installed. The Word (.docx) "
            "download works without it; you can also open the .docx in Word and "
            "use Save As → PDF. To enable one-click PDF, run setup_libreoffice "
            "(see the project README)."
        )

    docx_bytes = render_docx(terms, agreement_type)
    with tempfile.TemporaryDirectory(prefix="pa_pdf_") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "agreement.docx"
        src.write_bytes(docx_bytes)
        # Per-call user profile dir avoids the "LibreOffice already running" lock
        # when a desktop LibreOffice (or a prior conversion) is open.
        profile = tmp_path / "lo_profile"
        profile_uri = "file:///" + str(profile).replace("\\", "/")

        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(tmp_path),
            str(src),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("LibreOffice PDF conversion timed out.") from exc

        out_pdf = tmp_path / "agreement.pdf"
        if not out_pdf.exists():
            raise RuntimeError(
                "LibreOffice did not produce a PDF. "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
        return out_pdf.read_bytes()
