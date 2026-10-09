"""Locks in the Loan Documents builder.

Two guarantees:
1. The committed template (app/templates/loan_documents.html.j2) holds ONLY
   placeholders — none of the executed example's deal data may appear in it or
   in any rendered output. The repo is public, so even this test cannot carry
   those strings: the token list lives in the git-ignored
   tools/_loandocs_audit_tokens.json (present on the machine that builds the
   template, where the risk lives) and the tripwire tests skip without it.
2. Rendering with confirmed terms produces the whole package with the deal's
   figures in the right places, and the Memo of Settlement's "To be disbursed"
   is recomputed from the lines (never copied).
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.loandocs import _BANK_ENV, render_html
from app.loandocs_models import LoanDocsInclude, LoanDocTerms, SettlementLine

_TOKENS_FILE = (Path(__file__).parents[1] / "tools"
                / "_loandocs_audit_tokens.json")


@pytest.fixture(autouse=True)
def _scrub_bank_env(monkeypatch):
    # Any test module that imports app.main (test_structure.py does, for its
    # TestClient) runs load_dotenv() at pytest collection, putting the real
    # SRC_BANK_* values into the environment before these tests run. Scrub
    # them so the default render's wire block stays blank and the tripwire
    # keeps checking the template — not this machine's .env.
    for env in _BANK_ENV.values():
        monkeypatch.delenv(env, raising=False)


def _source_deal_tokens() -> list[str]:
    if not _TOKENS_FILE.exists():
        pytest.skip("tools/_loandocs_audit_tokens.json not present "
                    "(deal-data tripwire runs only where the source deal "
                    "documents live)")
    return json.loads(_TOKENS_FILE.read_text(encoding="utf-8"))


_TEMPLATE = (Path(__file__).parents[1] / "app" / "templates"
             / "loan_documents.html.j2")


def _terms() -> LoanDocTerms:
    return LoanDocTerms(
        borrower_name="Test Player",
        borrower_street="123 Main Street",
        borrower_city="Tampa", borrower_state_abbr="FL", borrower_zip="33601",
        borrower_state="Florida",
        occupation="Professional Baseball Player",
        loan_amount=2_000_000, interest_rate=12.5, origination_fee_pct=3,
        closing_date=date(2026, 7, 15), maturity_date=date(2026, 12, 1),
        team_name="Example Team", team_street="1 Stadium Way",
        team_city_state_zip="Tampa, FL 33601", league="MLB",
        contract_date=date(2025, 3, 1),
        settlement_lines=[
            SettlementLine(label="Lender Origination Fee (Est)", amount=60000),
            SettlementLine(label="SRC Legal/Closing Costs (Est)", amount=2075),
        ],
    )


def test_template_contains_no_deal_data():
    tpl = _TEMPLATE.read_text(encoding="utf-8")
    for token in _source_deal_tokens():
        assert token not in tpl, f"deal data leaked into template: {token!r}"


def test_render_contains_key_figures_and_no_source_deal():
    html = render_html(_terms(), LoanDocsInclude())
    for expected in [
        "Test Player", "$2,000,000", "12.5%", "$60,000",       # deal figures
        "December 1, 2026", "7/15/2026",                        # dates
        "PROMISSORY NOTE" if "PROMISSORY NOTE" in html else "Promissory Note",
        "Loan and Security Agreement", "Guaranty",
        "Memo of Settlement", "UCC FINANCING STATEMENT",
        "Payment Direction Letter",
        "MLB Professional Contract dated as of March 1, 2025",
    ]:
        assert expected in html
    # Settlement always recomputes: 2,000,000 - 60,000 - 2,075
    assert "$1,937,925.00" in html
    for token in _source_deal_tokens():
        assert token not in html, f"source deal data in render: {token!r}"


def test_settlement_post_lines_render_net_row():
    """Post-disbursement lines (e.g. DDD Insurance) print after the computed
    'To be disbursed to Borrower (Est)' subtotal, and the memo closes with a
    recomputed 'Net to be disbursed to Borrower (Est)' — mirroring the deal
    workbook's full fee block."""
    terms = _terms()
    terms.settlement_post_lines = [
        SettlementLine(label="DDD Insurance (Est)", amount=27125),
    ]
    html = render_html(terms, LoanDocsInclude())
    # 2,000,000 - 60,000 - 2,075 = 1,937,925 disbursed; - 27,125 = 1,910,800 net
    assert "To be disbursed to Borrower (Est)" in html
    assert "$1,937,925.00" in html
    assert "DDD Insurance (Est)" in html and "($27,125.00)" in html
    assert "Net to be disbursed to Borrower (Est)" in html
    assert "$1,910,800.00" in html
    # net is the grand (navy) row; the to-Borrower subtotal steps down a level
    net_i = html.index("Net to be disbursed to Borrower (Est)")
    assert 'class="grand"' in html[net_i - 200:net_i]
    # without post lines the memo ends at the to-Borrower row, as before
    plain = render_html(_terms(), LoanDocsInclude())
    assert "Net to be disbursed to Borrower (Est)" not in plain


def test_include_flags_drop_documents():
    include = LoanDocsInclude(guaranty=False, ucc=False)
    html = render_html(_terms(), include)
    assert "<h1>Guaranty</h1>" not in html
    assert "UCC FINANCING STATEMENT" not in html
    assert "Loan and Security Agreement" in html
    # the cover index reflects what's in the package
    assert "Guaranty" not in html.split("Documents in this Package")[1].split("</section>")[0]


def test_prepay_and_default_terms_render():
    html = render_html(_terms(), LoanDocsInclude())
    assert "minimum of two months of interest" in html
    assert "LATE Charge equal to 10%" in html
    assert "EXIT Fee equal to 10%" in html
    assert "is 5 percentage points above" in html


def test_no_team_contract_swaps_wording_and_blanks_cover():
    """no_team_contract: the cover shows "None" for Team / Employer and
    Contract, and the Contract / Borrower's Employer definitions and the
    Payment Direction Letter's addressee switch to the league-based wording
    Lauren supplied 2026-07-09 (year falls back to the closing year)."""
    terms = _terms()
    terms.no_team_contract = True
    terms.league = "NFL"
    html = render_html(terms, LoanDocsInclude())
    assert ('<td class="k">Team / Employer</td><td>None</td>') in html
    assert ('<td class="k">Contract</td><td>None</td>') in html
    # LSA Exhibit A — Borrower's Employer (closing 2026-07-15 -> year 2026)
    assert ("means the team that signs the Borrower in the upcoming 2026 "
            "National Football League.") in html
    # Contract definition — appears in BOTH the LSA and UCC Exhibit A
    contract_def = ("Contract means the NFL Professional Contract between "
                    "Test Player and The National Football League that signs "
                    "Mr. Test Player to a 2026 Player’s Contract in the "
                    "upcoming 2026 season")
    assert html.count(contract_def) == 2
    # Payment Direction Letter stays in the package, addressed to the league
    assert "Payment Direction Letter" in html
    cover = html.split("Documents in this Package")[1].split("</section>")[0]
    assert "Payment Direction Letter" in cover
    assert ("The National Football League that signs Mr. Test Player in the "
            "upcoming 2026 National Football League.") in html
    assert "TBD (&ldquo;Team&rdquo;)" in html
    # an explicit season year wins over the closing-date fallback
    terms.upcoming_season_year = "2027"
    html2 = render_html(terms, LoanDocsInclude())
    assert "to a 2027 Player’s Contract in the upcoming 2027 season" in html2
    # the flag defaults off: team wording renders normally
    normal = render_html(_terms(), LoanDocsInclude())
    assert "Payment Direction Letter" in normal
    assert "that signs the Borrower in the upcoming" not in normal
    assert "TBD (&ldquo;Team&rdquo;)" not in normal
    assert ('<td class="k">Team / Employer</td><td>Example Team</td>') in normal


def test_each_document_gets_a_cover_page():
    """Every included document is preceded by its own cover page (masthead +
    centered title + borrower) — plus a "Repayment Schedule" cover before the
    Note's Exhibit A — and excluding a document drops its cover(s) too
    (Lauren, 2026-07-10)."""
    html = render_html(_terms(), LoanDocsInclude())
    assert html.count('class="page docpage doc-cover"') == 8
    assert html.count('<p class="cover-borrower">Test Player</p>') == 8
    for title in ["Business Entity Affidavit", "Promissory Note",
                  "Repayment Schedule",
                  "Loan and Security Agreement", "Guaranty",
                  "Memo of Settlement", "UCC Financing Statement",
                  "Payment Direction Letter"]:
        assert f"<h1>{title}</h1>" in html.split('class="cover-mid"', 1)[1]
    # the Repayment Schedule cover sits between the Note's notary block and
    # its Exhibit A table, which starts a fresh page without its own break
    assert ("<h1>Repayment Schedule</h1>"
            in html.split("NOTARY PUBLIC", 1)[1].split("Loan Repayments by Month")[0])
    assert 'class="exhibit exhibit-page"' in html
    fewer = render_html(_terms(), LoanDocsInclude(guaranty=False, ucc=False))
    assert fewer.count('class="page docpage doc-cover"') == 6
    # dropping the Note drops the Repayment Schedule cover with it
    no_note = render_html(_terms(), LoanDocsInclude(note=False))
    assert "<h1>Repayment Schedule</h1>" not in no_note


def test_signature_and_notary_blocks_share_a_keep_wrapper():
    """Each signature block and its notary acknowledgment are wrapped in a
    .keep container (break-inside: avoid) so they print on ONE page — the
    Affidavit, Note, LSA and Guaranty each have one (Lauren, 2026-07-10)."""
    html = render_html(_terms(), LoanDocsInclude())
    assert html.count('<div class="keep">') == 4
    assert ".keep{break-inside:avoid;page-break-inside:avoid;}" in html
    # the Note's wrapper (2nd) holds both the signature grid and the notary
    # acknowledgment — split segments: [1]=affidavit, [2]=note, [3]=lsa, ...
    note_seg = html.split('<div class="keep">')[2]
    ack = note_seg.index("Acknowledgment")
    notary = note_seg.index("NOTARY PUBLIC")
    sig = note_seg.index("BORROWER:")
    assert sig < ack < notary < note_seg.index("Exhibit A")


def test_insurance_policy_definition_waived_by_default():
    """No Insurance Policy (default): the sports template's waived wording,
    verbatim — including its stray quote mark."""
    html = render_html(_terms(), LoanDocsInclude())
    assert ("acceptable to Lender” as this loan’s death &amp; disgrace "
            "insurance. However, the requirement to obtain and maintain such "
            "insurance has been waived.") in html
    assert "in a form and substance acceptable to the Lender" not in html


def test_insurance_policy_definition_when_policy_exists():
    """Insurance Policy = Yes: the policy wording Lauren supplied 2026-07-08."""
    terms = _terms()
    terms.has_insurance_policy = True
    html = render_html(terms, LoanDocsInclude())
    assert ("Insurance Policy</u> means one or more policies of insurance, "
            "in a form and substance acceptable to the Lender, issued by "
            "insurers acceptable to the Lender.") in html
    assert "has been waived" not in html.split("Insurance Policy</u> means")[1].split("</p>")[0]


def test_insurance_rep_follows_dropdown():
    """LSA rep 4.1(i) swaps with the same dropdown (Lauren, 2026-07-08)."""
    waived = render_html(_terms(), LoanDocsInclude())
    assert ("The requirement to maintain an Insurance Policy has been "
            "waived.") in waived
    assert "will not in any way be affected by, or terminate or lapse" not in waived

    terms = _terms()
    terms.has_insurance_policy = True
    insured = render_html(terms, LoanDocsInclude())
    assert ("The Insurance Policy is in full force and effect, is "
            "underwritten by financially sound and reputable insurers and is "
            "otherwise in compliance with all criteria set forth below in "
            "this Agreement. The Insurance Policy will remain in full force "
            "and effect and will not in any way be affected by, or terminate "
            "or lapse by reason of any of the transactions contemplated "
            "hereby.") in insured
    assert "The requirement to maintain an Insurance Policy has been waived" not in insured


def test_balloon_structure_is_default():
    """Balloon: the source template's Payment sentence, $0 rows, then one
    payment of principal + actual/365 accrued interest (calc_amort's figure,
    so the Note and the memo's facility total can never disagree)."""
    html = render_html(_terms(), LoanDocsInclude())
    assert ("No monthly payments are required; the full principal and "
            "interest are due as a balloon payment at maturity.") in html
    # 2026-07-15 -> 2026-12-01 is 139 days: 2,000,000 * 12.5%/365 * 139
    assert "$95,205.00" in html            # accrued interest, final row
    assert "$2,095,205.00" in html         # balloon total
    assert html.count("$0.00</td>") >= 8   # the earlier months pay nothing


def test_interest_only_structure():
    terms = _terms()
    terms.amortization_type = "interest_only"
    html = render_html(terms, LoanDocsInclude())
    assert "Interest only payments are due monthly" in html
    assert "$20,833.00" in html            # 2,000,000 * 12.5% / 12, monthly
    assert "$2,020,833.00" in html         # final row: principal + last interest


def test_fully_amortized_structure():
    terms = _terms()
    terms.amortization_type = "fully_amortized"
    html = render_html(terms, LoanDocsInclude())
    # an executed fully-amortized note's Payment wording, verbatim
    assert ("Monthly principal payments are required until the owed amount "
            "has been paid in full.") in html
    # 5 level payments on 2,000,000 @ 12.5%/12: principal column foots exactly
    assert "$2,000,000.00" in html
    assert "No monthly payments are required" not in html
