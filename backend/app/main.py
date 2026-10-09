"""FastAPI application for the Credit Memo Builder.

Routes
------
GET  /api/health           - liveness check
POST /api/extract          - upload documents, get structured extraction back
POST /api/memo/html        - render memo as HTML
POST /api/memo/rollforward - preview the stale-PFS debt roll-forward
POST /api/memo/pdf         - render memo as PDF (download)
POST /api/memo/word        - render memo as Word .doc (download)
POST /api/binder/pdf       - merge executed PDFs into an indexed closing binder

Interactive API docs are auto-generated at /docs (Swagger) and /redoc.

Run locally:  uvicorn app.main:app --reload --port 8001
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from dotenv import load_dotenv

from .models import Extraction, MemoRequest, UploadedDoc
from . import calculations as calc
from . import extraction as extraction_service
from . import memo as memo_service
from .pa_models import PAExtraction, PARequest, PASendRequest, PATerms, BreakdownResult
from . import pa_extraction as pa_extraction_service
from . import pa_agreement as pa_agreement_service
from . import pa_breakdown as pa_breakdown_service
from . import esign_demand_signatures as esign_service
from .loandocs_models import LoanDocsRequest, SettlementSheetResult
from . import loandocs as loandocs_service
from . import loandocs_sheet as loandocs_sheet_service
from .loandocs_extraction import TeamContractExtraction, MemoDealExtraction
from . import loandocs_extraction as loandocs_extraction_service
from .binder_models import BinderRequest
from . import binder as binder_service
from .structure_models import (
    LeagueCadence, ProposedTerms, SelectRequest, StructureRequest,
    StructureResult, TermsRequest,
)
from . import structure as structure_service
from .structure_extraction import StructureExtraction
from . import structure_extraction as structure_extraction_service
from . import structure_summary as structure_summary_service
from .binder_extraction import BinderInfoExtraction, BinderSortResult
from . import binder_extraction as binder_extraction_service

# Load the project-root .env so the Claude usage token (CLAUDE_CODE_OAUTH_TOKEN)
# is available however the server is launched. This does NOT rely on uvicorn's
# --env-file flag, which silently does nothing unless python-dotenv is installed.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# Without this the app's own INFO records are dropped (uvicorn configures only
# its own loggers), and the upload manifest logged before every extraction —
# the only record of what a failed request actually contained — would vanish.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Credit Memo Builder API",
    description="Backend for South River Capital's athlete-loan credit memos.",
    version="1.0.0",
)

# Allow the Vite dev server (and configured production origins) to call the API.
_origins = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5174,http://127.0.0.1:5174",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "extraction_configured": extraction_service.usage_token() is not None,
        "pa_templates": pa_agreement_service.templates_present(),
        "pa_pdf": pa_agreement_service.pdf_available(),
    }


class ExtractRequest(MemoRequest):  # reuse base for OpenAPI clarity
    pass


@app.post("/api/extract", response_model=Extraction)
def extract(docs: list[UploadedDoc]) -> Extraction:
    """Run document extraction. Documents are base64-encoded by the frontend."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return extraction_service.extract_documents(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _filenames(req: MemoRequest) -> list[str]:
    # filenames are carried for the memo's document index; optional.
    return []


def _safe_name(name: str) -> str:
    """ASCII-safe borrower name for Content-Disposition (e.g. JosÃƒÆ’Ã‚Â© -> Jose)."""
    import unicodedata
    ascii_name = (
        unicodedata.normalize("NFKD", name or "Borrower")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    ascii_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in ascii_name)
    return ascii_name.strip("_") or "Borrower"


@app.post("/api/memo/html")
def memo_html(req: MemoRequest) -> Response:
    html = memo_service.render_html(req.terms, req.extraction, _filenames(req))
    return Response(content=html, media_type="text/html")


@app.post("/api/memo/rollforward")
def memo_rollforward(req: MemoRequest) -> dict:
    """Preview the stale-PFS roll-forward (rule 15) for the Step 2 review table.

    The math stays here rather than in the browser so the figures the user
    reviews are exactly the ones the memo will render.
    """
    from datetime import date as _date

    rf = calc.calc_debt_rollforward(req.extraction, _date.today())
    return {
        "applied": rf["applied"],
        "pfs_date": rf["pfs_date"].isoformat() if rf["pfs_date"] else None,
        "as_of": rf["as_of"].isoformat() if rf["as_of"] else None,
        "months": rf["months"],
        "total_paydown": rf["total_paydown"],
        "note": rf["note"],
        "warnings": rf["warnings"],
        "rows": [
            {**{k: v for k, v in r.items() if k != "maturity_date"},
             "maturity_date": r["maturity_date"].isoformat() if r["maturity_date"] else None}
            for r in rf["rows"]
        ],
    }


@app.post("/api/memo/pdf")
def memo_pdf(req: MemoRequest) -> Response:
    html = memo_service.render_html(req.terms, req.extraction, _filenames(req))
    try:
        pdf = memo_service.render_pdf(html)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    name = _safe_name(req.terms.name)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Credit_Memorandum_{name}.pdf"'},
    )


@app.post("/api/memo/word")
def memo_word(req: MemoRequest) -> Response:
    html = memo_service.render_html(req.terms, req.extraction, _filenames(req))
    doc = memo_service.render_word(html)
    name = _safe_name(req.terms.name)
    return Response(
        content=doc,
        media_type="application/msword",
        headers={"Content-Disposition": f'attachment; filename="Credit_Memorandum_{name}.doc"'},
    )


# --- Participation Agreement Builder ---------------------------------------

@app.post("/api/pa/extract", response_model=PAExtraction)
def pa_extract(docs: list[UploadedDoc]) -> PAExtraction:
    """Extract participation-deal fields from uploaded loan documents."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return pa_extraction_service.extract_documents(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/pa/breakdown", response_model=BreakdownResult)
def pa_breakdown(docs: list[UploadedDoc]) -> BreakdownResult:
    """Parse a Participant Breakdown .xlsx into deal info + per-participant terms."""
    import base64

    xlsx = next(
        (d for d in docs
         if d.filename.lower().endswith((".xlsx", ".xlsm"))
         or "spreadsheet" in d.mime or "excel" in d.mime),
        docs[0] if docs else None,
    )
    if xlsx is None:
        raise HTTPException(status_code=400, detail="No spreadsheet provided.")
    try:
        data = base64.b64decode(xlsx.b64)
        return BreakdownResult(**pa_breakdown_service.parse_breakdown(data))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface parse failures cleanly
        raise HTTPException(status_code=422, detail=f"Could not read the breakdown: {exc}") from exc


@app.get("/api/pa/defaults")
def pa_defaults() -> dict:
    """SRC signer + e-signature defaults for the send-for-signature step (from .env)."""
    out = pa_agreement_service.esign_defaults()
    esign = esign_service.status()
    out["esign_provider"] = esign["provider"]
    out["esign_ready"] = esign["ready"]
    out["esign_mode"] = esign["mode"]
    return out


@app.post("/api/pa/send")
def pa_send(req: PASendRequest) -> dict:
    """Render the agreement and send it out for signature via Demand Signatures."""
    lender_email = req.lender_signer_email.strip()
    participant_email = (req.terms.participant_email or "").strip()
    participant_name = (
        (req.terms.participant_signatory_name or "").strip()
        or (req.terms.participant_name or "").strip()
    )
    if "@" not in lender_email or "@" not in participant_email:
        raise HTTPException(status_code=400, detail="Both signer emails are needed before sending.")
    if not participant_name:
        raise HTTPException(status_code=400, detail="The participant signer needs a name.")
    try:
        pdf = pa_agreement_service.render_pdf(req.terms, req.agreement_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    subject = f"Please sign: Participation Agreement — {req.terms.borrower_name or 'South River Capital'}"
    try:
        return esign_service.send_for_signature(
            pdf,
            _pa_filename(req.terms, "pdf"),
            {"name": req.lender_signer_name.strip() or "James Plack", "email": lender_email},
            {"name": participant_name, "email": participant_email},
            subject,
            draft=req.draft,
        )
    except esign_service.EsignNotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except esign_service.EsignError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _pa_filename(terms: PATerms, ext: str) -> str:
    base = _safe_name(terms.borrower_name or "Participation")
    return f"Participation_Agreement_{base}.{ext}"


@app.post("/api/pa/docx")
def pa_docx(req: PARequest) -> Response:
    """Generate the filled Participation Agreement as a Word .docx."""
    data = pa_agreement_service.render_docx(req.terms, req.agreement_type)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{_pa_filename(req.terms, "docx")}"'},
    )


# --- Loan Documents Builder -------------------------------------------------

@app.get("/api/loandocs/defaults")
def loandocs_defaults() -> dict:
    """SRC bank/wire defaults for the Payment Direction Letter (from .env)."""
    return loandocs_service.bank_defaults()


@app.post("/api/loandocs/extract", response_model=TeamContractExtraction)
def loandocs_extract(docs: list[UploadedDoc]) -> TeamContractExtraction:
    """Extract Team & Contract fields (team name/address, league, contract
    title and date) from an uploaded player contract or other deal documents."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return loandocs_extraction_service.extract_documents(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/loandocs/memo", response_model=MemoDealExtraction)
def loandocs_memo(docs: list[UploadedDoc]) -> MemoDealExtraction:
    """Read a previously generated credit memorandum (PDF is best) and return
    the deal-level fields for the closing documents."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return loandocs_extraction_service.extract_memo(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/loandocs/settlement", response_model=SettlementSheetResult)
def loandocs_settlement(docs: list[UploadedDoc]) -> SettlementSheetResult:
    """Parse the deal's Balloon / Fully Amortized workbook into the Memo of
    Settlement fee lines and the Note's Exhibit A repayment schedule."""
    import base64

    xlsx = next(
        (d for d in docs
         if d.filename.lower().endswith((".xlsx", ".xlsm"))
         or "spreadsheet" in d.mime or "excel" in d.mime),
        docs[0] if docs else None,
    )
    if xlsx is None:
        raise HTTPException(status_code=400, detail="No spreadsheet provided.")
    try:
        data = base64.b64decode(xlsx.b64)
        return SettlementSheetResult(**loandocs_sheet_service.parse_settlement_workbook(data))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface parse failures cleanly
        raise HTTPException(status_code=422,
                            detail=f"Could not read the workbook: {exc}") from exc


def _loandocs_filename(req: LoanDocsRequest, ext: str) -> str:
    return f"Loan_Documents_{_safe_name(req.terms.borrower_name or 'Borrower')}.{ext}"


@app.post("/api/loandocs/html")
def loandocs_html(req: LoanDocsRequest) -> Response:
    html = loandocs_service.render_html(req.terms, req.include)
    return Response(content=html, media_type="text/html")


@app.post("/api/loandocs/pdf")
def loandocs_pdf(req: LoanDocsRequest) -> Response:
    html = loandocs_service.render_html(req.terms, req.include)
    try:
        pdf = memo_service.render_pdf(html, footer_text=loandocs_service.FOOTER_TEXT)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{_loandocs_filename(req, "pdf")}"'},
    )


@app.post("/api/loandocs/word")
def loandocs_word(req: LoanDocsRequest) -> Response:
    html = loandocs_service.render_html(req.terms, req.include)
    doc = memo_service.render_word(html, footer_text=loandocs_service.FOOTER_TEXT)
    return Response(
        content=doc,
        media_type="application/msword",
        headers={"Content-Disposition":
                 f'attachment; filename="{_loandocs_filename(req, "doc")}"'},
    )


# --- Closing Binder -----------------------------------------------------------

@app.post("/api/binder/extract", response_model=BinderInfoExtraction)
def binder_extract(docs: list[UploadedDoc]) -> BinderInfoExtraction:
    """Read uploaded deal documents (credit memo, loan documents, term sheet)
    and return the binder's cover-page fields."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return binder_extraction_service.extract_binder_info(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/binder/sort", response_model=BinderSortResult)
def binder_sort(docs: list[UploadedDoc]) -> BinderSortResult:
    """Read the uploaded signed closing package (and insurance PDFs) and
    split it into the binder's standard sections with page ranges."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return binder_extraction_service.sort_documents(docs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/binder/pdf")
def binder_pdf(req: BinderRequest) -> Response:
    """Merge the uploaded executed documents into one indexed closing binder."""
    if not req.documents:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        pdf = binder_service.build_binder(req.info, req.documents, req.tab_pages)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    name = _safe_name(req.info.borrower_name)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="Closing_Binder_{name}.pdf"'},
    )


@app.post("/api/pa/pdf")
def pa_pdf(req: PARequest) -> Response:
    """Generate the filled Participation Agreement as a PDF (via LibreOffice)."""
    try:
        data = pa_agreement_service.render_pdf(req.terms, req.agreement_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_pa_filename(req.terms, "pdf")}"'},
    )

# --- Deal structuring ------------------------------------------------------

@app.get("/api/structure/cadences", response_model=list[LeagueCadence])
def structure_cadences() -> list[LeagueCadence]:
    """The league pay-cadence defaults, for the tab's league picker."""
    return list(structure_service.LEAGUE_CADENCES.values())


@app.get("/api/structure/cadence/{league}", response_model=LeagueCadence)
def structure_cadence(league: str) -> LeagueCadence:
    """The default cadence for one league (unknown leagues get level monthly)."""
    return structure_service.league_cadence(league)


@app.post("/api/structure/extract", response_model=StructureExtraction)
def structure_extract(docs: list[UploadedDoc]) -> StructureExtraction:
    """Read deal documents for the structuring inputs (timing + certainty)."""
    if not docs:
        raise HTTPException(status_code=400, detail="No documents provided.")
    try:
        return structure_extraction_service.extract_documents(docs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Anything else (a shape we didn't anticipate back from the model) is
        # reported with its cause rather than as a bare 500, which tells the
        # user nothing and hides the reason in a server window.
        raise HTTPException(
            status_code=422,
            detail=f"Could not understand the extraction result: {exc}",
        ) from exc


@app.post("/api/structure/propose", response_model=StructureResult)
def structure_propose(req: StructureRequest) -> StructureResult:
    """Project the borrower's cash flow and score every candidate structure."""
    if (req.inputs.loan_amount or 0) <= 0:
        raise HTTPException(status_code=400, detail="A loan amount is required.")
    return structure_service.propose_structures(req.inputs)


@app.post("/api/structure/terms", response_model=ProposedTerms)
def structure_terms(req: TermsRequest) -> ProposedTerms:
    """Propose the loan amount, rate, points and term from the contract.

    The amount is the lower of South River's Loan-to-Contract policy limit and
    what the borrower's own earnings can actually retire; which one binds is
    reported. Rate/points/term are house defaults, not a priced rate.
    """
    return structure_service.propose_terms(req.inputs, req.contract_remaining)


@app.post("/api/structure/debt-service")
def structure_debt_service(req: MemoRequest) -> dict:
    """Annual non-facility debt service using the CREDIT MEMO's PFS handling.

    Goes through calculations so the Structure tab counts the same rows the memo
    counts, and so a stale statement is rolled forward the same way (rule 15) —
    a debt repaid since the statement date stops being counted here too.
    """
    as_of = req.terms.fund if getattr(req.terms, "fund", None) else None
    return structure_service.debt_service_from_memo(req.extraction, as_of)


@app.post("/api/structure/summary/html")
def structure_summary_html(req: StructureRequest) -> Response:
    """Preview the structure summary (options + cash flow) as HTML."""
    result = structure_service.propose_structures(req.inputs)
    html = structure_summary_service.render_html(result, req.inputs)
    return Response(content=html, media_type="text/html")


@app.post("/api/structure/summary/pdf")
def structure_summary_pdf(req: StructureRequest) -> Response:
    """The sendable version: every option considered, the recommendation and
    the cash flow behind it, in the credit memorandum's house design."""
    if (req.inputs.loan_amount or 0) <= 0:
        raise HTTPException(status_code=400, detail="A loan amount is required.")
    result = structure_service.propose_structures(req.inputs)
    html = structure_summary_service.render_html(result, req.inputs)
    try:
        pdf = structure_summary_service.render_pdf(html)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    name = structure_summary_service.filename(req.inputs)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/structure/select")
def structure_select(req: SelectRequest) -> dict:
    """Convert the chosen candidate into Loan Documents Exhibit A rows.

    Returns the rows plus the amortization_type and maturity date so the Loan
    Documents tab can apply the whole structure, not just the schedule.
    """
    result = structure_service.propose_structures(req.inputs)
    match = next((c for c in result.candidates if c.key == req.candidate_key), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"No candidate structure '{req.candidate_key}' for these inputs.",
        )
    return {
        "candidate_key": match.key,
        "name": match.name,
        "amortization_type": match.amortization_type,
        "maturity_date": match.maturity_date.isoformat() if match.maturity_date else None,
        "interest_reserve": match.interest_reserve,
        "repayment_schedule": structure_service.to_schedule_rows(match),
    }
