"""The Demand Signatures send-for-signature step: tab detection, field
placement math, and the four-call send flow (upload → recipients → fields →
send) against a mocked API.

No test talks to demandsignatures.com — the flow test swaps the module's HTTP
client for one backed by ``httpx.MockTransport``.
"""

import json

import httpx
import pytest

from app import esign_demand_signatures as ds
from app import pa_agreement
from app.pa_models import PATerms


def _demo_terms() -> PATerms:
    return PATerms(
        borrower_name="Test Borrower",
        participant_name="Sample Participant, LLC",
        participant_signatory_name="Jane Sample",
        participation_percentage="10.00%",
        total_loan_amount="$1,000,000.00",
    )


@pytest.mark.parametrize("agreement_type", ["brookridge", "standard"])
def test_find_sign_tabs_places_two_pairs(agreement_type):
    """Both forms carry 4 'By: ____' lines: signature page + Exhibit B, lender first."""
    if not pa_agreement.pdf_available():
        pytest.skip("LibreOffice not available")
    pdf = pa_agreement.render_pdf(_demo_terms(), agreement_type)
    tabs = ds.find_sign_tabs(pdf)
    assert len(tabs) == 4
    assert [t["party"] for t in tabs] == ["lender", "participant", "lender", "participant"]
    assert len({t["page"] for t in tabs}) == 2
    for t in tabs:
        assert 0 < t["x"] < t["page_width"] and 0 < t["y"] < t["page_height"]
    by_page: dict = {}
    for t in tabs:
        by_page.setdefault(t["page"], []).append(t)
    for pair in by_page.values():
        lender = next(t for t in pair if t["party"] == "lender")
        participant = next(t for t in pair if t["party"] == "participant")
        assert lender["y"] > participant["y"]  # lender block prints above the participant's


def test_find_sign_tabs_rejects_odd_counts():
    from pypdf import PdfWriter
    import io

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(ds.EsignError):
        ds.find_sign_tabs(buf.getvalue())


_TABS = [
    {"page": 8, "x": 288.1, "y": 607.3, "page_width": 612.0, "page_height": 792.0,
     "party": "lender"},
    {"page": 8, "x": 288.1, "y": 434.8, "page_width": 612.0, "page_height": 792.0,
     "party": "participant"},
]


def test_build_fields_converts_points_to_percentages():
    """Demand Signatures positions are % of the page from the top-left corner;
    the box sits x+30 past "By:" with its top 24pt above the underscore line."""
    fields = ds.build_fields(_TABS)
    assert [f["party"] for f in fields] == ["lender", "participant"]
    lender = fields[0]
    assert lender["type"] == "signature"
    assert lender["page"] == 8
    assert lender["required"] is True
    assert lender["position_x"] == round((288.1 + 30) / 612 * 100, 3)
    assert lender["position_y"] == round((792 - 607.3 - 24) / 792 * 100, 3)
    for f in fields:
        for key in ("position_x", "position_y", "width", "height"):
            assert 0 <= f[key] <= 100


def _mocked_send(monkeypatch, *, draft=False, send_status=201):
    """Run send_for_signature against a MockTransport; return (result, calls)."""
    monkeypatch.setenv("DEMAND_SIGNATURES_API_KEY", "ds_live_abc123.secret")
    monkeypatch.delenv("DEMAND_SIGNATURES_BASE_URL", raising=False)
    monkeypatch.setattr(ds, "find_sign_tabs", lambda pdf: list(_TABS))

    calls: list[dict] = []
    recipient_ids = iter(["r-lender", "r-participant"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content
        record = {"path": path, "auth": request.headers.get("Authorization")}
        if path.endswith("/upload"):
            record["multipart"] = b"application/pdf" in body and b"%PDF-fake" in body
            calls.append(record)
            return httpx.Response(201, json={"id": "doc-1", "page_count": 9})
        record["json"] = json.loads(body)
        calls.append(record)
        if path.endswith("/recipients"):
            return httpx.Response(201, json={"id": next(recipient_ids)})
        if path.endswith("/fields"):
            return httpx.Response(201, json={"id": "field-x"})
        if path.endswith("/send"):
            return httpx.Response(send_status, json={"status": "pending"})
        raise AssertionError(f"unexpected call: {path}")

    def fake_client(cfg):
        return httpx.Client(
            base_url=cfg["base_url"],
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(ds, "_client", fake_client)
    result = ds.send_for_signature(
        b"%PDF-fake",
        "Participation_Agreement_Test_Borrower.pdf",
        {"name": "Jim Plack", "email": "lender@example.com"},
        {"name": "Jane Sample", "email": "jane@example.com"},
        "Please sign: Participation Agreement — Test Borrower",
        draft=draft,
    )
    return result, calls


def test_send_full_flow(monkeypatch):
    result, calls = _mocked_send(monkeypatch)

    assert [c["path"] for c in calls] == [
        "/api/documents/upload",
        "/api/documents/doc-1/recipients",
        "/api/documents/doc-1/recipients",
        "/api/documents/doc-1/fields",
        "/api/documents/doc-1/fields",
        "/api/documents/doc-1/send",
    ]
    assert all(c["auth"] == "Bearer ds_live_abc123.secret" for c in calls)
    assert calls[0]["multipart"]

    lender_rec, participant_rec = calls[1]["json"], calls[2]["json"]
    assert lender_rec == {
        "email": "lender@example.com", "name": "Jim Plack",
        "role": "signer", "signing_order": 1,
    }
    assert participant_rec["email"] == "jane@example.com"
    assert participant_rec["signing_order"] == 1  # both emailed at once

    lender_field, participant_field = calls[3]["json"], calls[4]["json"]
    assert lender_field["recipient_id"] == "r-lender"
    assert participant_field["recipient_id"] == "r-participant"
    for f in (lender_field, participant_field):
        assert f["type"] == "signature"
        assert "party" not in f  # internal key never reaches the API

    assert result == {
        "document_id": "doc-1", "status": "pending",
        "mode": "live", "provider": "demandsignatures",
    }


def test_send_draft_uploads_but_never_sends(monkeypatch):
    result, calls = _mocked_send(monkeypatch, draft=True)
    assert not any(c["path"].endswith("/send") for c in calls)
    assert result["status"] == "draft"


def test_send_surfaces_api_errors(monkeypatch):
    monkeypatch.setenv("DEMAND_SIGNATURES_API_KEY", "ds_live_abc123.secret")
    monkeypatch.setattr(ds, "find_sign_tabs", lambda pdf: list(_TABS))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "No file provided.", "errors": {}})

    monkeypatch.setattr(
        ds, "_client",
        lambda cfg: httpx.Client(base_url=cfg["base_url"],
                                 transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ds.EsignError, match="upload failed \\(400\\): No file provided."):
        ds.send_for_signature(
            b"%PDF-fake", "PA.pdf",
            {"name": "a", "email": "a@b.c"}, {"name": "d", "email": "d@e.f"},
            "subject",
        )


def test_send_unconfigured(monkeypatch):
    monkeypatch.delenv("DEMAND_SIGNATURES_API_KEY", raising=False)
    monkeypatch.setattr(ds, "find_sign_tabs", lambda pdf: list(_TABS))
    with pytest.raises(ds.EsignNotConfigured):
        ds.send_for_signature(
            b"%PDF-fake", "PA.pdf",
            {"name": "a", "email": "a@b.c"}, {"name": "d", "email": "d@e.f"},
            "subject",
        )


def test_status_modes(monkeypatch):
    monkeypatch.delenv("DEMAND_SIGNATURES_API_KEY", raising=False)
    assert ds.status() == {"provider": "demandsignatures", "ready": False, "mode": "live"}

    monkeypatch.setenv("DEMAND_SIGNATURES_API_KEY", "ds_test_k.secret")
    assert ds.status() == {"provider": "demandsignatures", "ready": True, "mode": "test"}

    monkeypatch.setenv("DEMAND_SIGNATURES_API_KEY", "ds_live_k.secret")
    assert ds.status() == {"provider": "demandsignatures", "ready": True, "mode": "live"}

    monkeypatch.setenv("DEMAND_SIGNATURES_API_KEY", "not-a-real-key")
    assert ds.status()["ready"] is False
