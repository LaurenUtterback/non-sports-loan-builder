"""Send the Participation Agreement out for signature via Demand Signatures.

Demand Signatures (https://demandsignatures.com) is South River's own e-signing
platform. Auth is a single org-scoped API key sent as a Bearer token — no OAuth
dance, no consent step, no private keys. Configuration comes from .env (the
repo is public):

    DEMAND_SIGNATURES_API_KEY   the org API key (``ds_live_...`` or ``ds_test_...``)
                                with the documents:read + documents:write scopes,
                                created under Organization -> API keys
    DEMAND_SIGNATURES_BASE_URL  optional — API base,
                                default ``https://demandsignatures.com/api``

Signature fields are placed automatically: ``find_sign_tabs`` locates every
"By:  ______ (SEAL)" line in the rendered PDF (agreement signature page and the
Exhibit B Participation Certificate) and assigns them alternately to the Lender
and the Participant — the templates always print the Lender block above the
Participant block. Both signers get the same signing_order and documents send
in parallel mode (Demand Signatures' default), so both are emailed at once —
matching how SRC's executed PAs were routed.

The send is four API calls: upload the PDF, add the two signers, place the
signature fields, send. Field positions are percentages of the page (0-100)
measured from the page's top-left corner to the field box's top-left corner.
"""

from __future__ import annotations

import io
import os
import re

import httpx
from pypdf import PdfReader

_DEFAULT_BASE_URL = "https://demandsignatures.com/api"
_KEY_PREFIXES = ("ds_live_", "ds_test_")


class EsignError(RuntimeError):
    """A Demand Signatures step failed — the message is safe to show in the UI."""


class EsignNotConfigured(EsignError):
    """The DEMAND_SIGNATURES_* environment values are missing or incomplete."""


# --- configuration -----------------------------------------------------------

def _cfg() -> dict:
    key = (os.environ.get("DEMAND_SIGNATURES_API_KEY") or "").strip()
    base = (os.environ.get("DEMAND_SIGNATURES_BASE_URL") or _DEFAULT_BASE_URL).strip()
    return {
        "api_key": key,
        "base_url": base.rstrip("/"),
        "mode": "test" if key.startswith("ds_test_") else "live",
    }


def status() -> dict:
    """For the UI: is one-click sending configured, and against which system?"""
    cfg = _cfg()
    return {
        "provider": "demandsignatures",
        "ready": cfg["api_key"].startswith(_KEY_PREFIXES),
        "mode": cfg["mode"],
    }


# --- signature-line detection --------------------------------------------------

_SIGN_LINE = re.compile(r"^By[:.]?\s+_{6,}")


def find_sign_tabs(pdf_bytes: bytes) -> list[dict]:
    """Locate every "By:  ______" signature line in the rendered agreement.

    Returns [{page (1-based), x, y, page_width, page_height, party}] in PDF
    points (origin bottom-left). Lines are assigned alternately
    lender/participant in reading order — the templates always print the Lender
    block first, both on the signature page and on the Exhibit B certificate.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    lines: list[dict] = []
    for page_index, page in enumerate(reader.pages):
        found: list[tuple[float, float]] = []

        def visit(text, cm, tm, font_dict, font_size, _found=found):
            if _SIGN_LINE.match(text.strip()):
                _found.append((float(tm[4]), float(tm[5])))

        page.extract_text(visitor_text=visit)
        found.sort(key=lambda point: -point[1])  # top of the page first
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        for x, y in found:
            lines.append(
                {"page": page_index + 1, "x": x, "y": y,
                 "page_width": width, "page_height": height}
            )

    if not lines or len(lines) % 2:
        raise EsignError(
            f"Could not place the signature fields: expected pairs of 'By: ____' lines, "
            f"found {len(lines)}. Send manually instead."
        )
    for i, line in enumerate(lines):
        line["party"] = "lender" if i % 2 == 0 else "participant"
    return lines


# --- field placement -----------------------------------------------------------

# The box in PDF points (72/inch): shifted right past "By:  " onto the
# underscores, with its top 24pt above the underscore baseline so the drawn
# signature straddles the line.
_X_SHIFT = 30.0
_Y_SHIFT = 24.0
_BOX_WIDTH_PTS = 150.0
_BOX_HEIGHT_PTS = 36.0


def build_fields(tabs: list[dict]) -> list[dict]:
    """Field payloads for POST /documents/<id>/fields — pure so tests can check
    the point→percentage conversion without network.

    Each entry keeps the ``party`` key (popped before posting) so the caller
    can map it to the right recipient id.
    """
    fields = []
    for t in tabs:
        w, h = t["page_width"], t["page_height"]
        fields.append(
            {
                "party": t["party"],
                "type": "signature",
                "page": t["page"],
                "position_x": round((t["x"] + _X_SHIFT) / w * 100, 3),
                "position_y": round((h - t["y"] - _Y_SHIFT) / h * 100, 3),
                "width": round(_BOX_WIDTH_PTS / w * 100, 3),
                "height": round(_BOX_HEIGHT_PTS / h * 100, 3),
                "required": True,
            }
        )
    return fields


# --- sending -------------------------------------------------------------------

def _client(cfg: dict) -> httpx.Client:
    return httpx.Client(
        base_url=cfg["base_url"],
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=60,
    )


def _json_or_error(resp: httpx.Response, step: str) -> dict:
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("message") or resp.text[:300]
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = resp.text[:300]
        raise EsignError(f"Demand Signatures {step} failed ({resp.status_code}): {detail}")
    return resp.json()


def send_for_signature(
    pdf_bytes: bytes,
    filename: str,
    lender_signer: dict,
    participant_signer: dict,
    subject: str,
    message: str = "",
    draft: bool = False,
) -> dict:
    """Render-ready PDF in, sent document out. Raises EsignError with a UI-safe
    message. ``draft=True`` uploads and places everything but emails no one —
    the document stays a draft on demandsignatures.com.
    """
    tabs = find_sign_tabs(pdf_bytes)
    cfg = _cfg()
    if not cfg["api_key"].startswith(_KEY_PREFIXES):
        raise EsignNotConfigured(
            "Demand Signatures isn't connected yet — set DEMAND_SIGNATURES_API_KEY in .env "
            "(an org API key from demandsignatures.com with the documents scopes), "
            "then restart the backend."
        )
    fields = build_fields(tabs)
    title = filename.rsplit(".", 1)[0].replace("_", " ")

    try:
        with _client(cfg) as client:
            doc = _json_or_error(
                client.post(
                    "/documents/upload",
                    files={"file": (filename, pdf_bytes, "application/pdf")},
                    data={"title": title},
                ),
                "upload",
            )
            doc_id = str(doc.get("id") or "")
            if not doc_id:
                raise EsignError("Demand Signatures did not return a document id.")

            recipient_ids: dict[str, str] = {}
            for party, signer in (("lender", lender_signer), ("participant", participant_signer)):
                rec = _json_or_error(
                    client.post(
                        f"/documents/{doc_id}/recipients",
                        json={
                            "email": signer["email"],
                            "name": signer["name"],
                            "role": "signer",
                            "signing_order": 1,  # both at once — how SRC's executed PAs were routed
                        },
                    ),
                    "recipient setup",
                )
                recipient_ids[party] = str(rec.get("id") or "")

            for field in fields:
                payload = dict(field)
                payload["recipient_id"] = recipient_ids[payload.pop("party")]
                _json_or_error(
                    client.post(f"/documents/{doc_id}/fields", json=payload),
                    "field placement",
                )

            if draft:
                return {
                    "document_id": doc_id,
                    "status": "draft",
                    "mode": cfg["mode"],
                    "provider": "demandsignatures",
                }

            sent = _json_or_error(client.post(f"/documents/{doc_id}/send", json={}), "send")
            return {
                "document_id": doc_id,
                "status": sent.get("status", "pending"),
                "mode": cfg["mode"],
                "provider": "demandsignatures",
            }
    except httpx.HTTPError as exc:
        raise EsignError(f"Could not reach Demand Signatures: {exc}") from exc
