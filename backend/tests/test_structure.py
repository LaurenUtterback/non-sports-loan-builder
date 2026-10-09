"""Locks in the deal-structuring rules (structure.py, rules S1-S6).

The reference scenario is a SEASONAL trade - a contractor who earns April
through October and then runs five dry months on what was banked. Every
assertion about "no payment in a dry month" is the whole point of the tab, so
these are the tests that matter most.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import structure as st
from app.calculations import LIVING_RATE, TAX_RATE
from app.main import app
from app.loandocs_models import ScheduleRow
from app.structure_models import IncomeEvent, StructureInputs

client = TestClient(app)


def seasonal_inputs(**over) -> StructureInputs:
    base = dict(
        borrower_name="Reference Borrower",
        pattern="SEASONAL",
        qualifying_income=1_000_000.0,
        other_income=0.0,
        other_debt_annual=0.0,
        income_contracted=True,
        loan_amount=200_000.0,
        interest_rate=12.0,
        origination_fee_pct=3.0,
        funding_date=date(2026, 4, 1),
        target_term_months=12,
        min_coverage=1.25,
    )
    base.update(over)
    return StructureInputs(**base)


# --- Pattern cadences -------------------------------------------------------

def test_cadence_lookup_handles_aliases_and_unknowns():
    assert st.income_cadence("PAYROLL").pay_frequency == "semimonthly"
    assert st.income_cadence("k-1").pattern == "DISTRIBUTIONS"
    assert st.income_cadence("Real Estate").pattern == "RENTS"
    assert st.income_cadence("landscaping").pattern == "SEASONAL"
    # unknown pattern must not raise — it falls back to level monthly
    unknown = st.income_cadence("Interpretive Dance")
    assert unknown.year_round is True
    assert unknown.pay_frequency == "monthly"


def test_seasonal_pattern_pays_in_season_and_leaves_the_rest_dry():
    cad = st.income_cadence("SEASONAL")
    dates = st.pay_dates(cad, date(2026, 4, 1), date(2027, 4, 1))
    assert 6 <= len(dates) <= 8, "a seasonal trade earns across ~7 months"
    months = {(d.year, d.month) for d, _ in dates}
    # Apr-Oct 2026, and nothing from Nov through Mar
    assert (2026, 4) in months and (2026, 10) in months
    assert not any(m for m in months
                   if (m[0] == 2026 and m[1] >= 11) or (m[0] == 2027 and m[1] <= 3))


def test_income_stops_at_the_income_horizon():
    cad = st.income_cadence("PAYROLL")
    dates = st.pay_dates(cad, date(2026, 1, 1), date(2027, 12, 31),
                         income_horizon=date(2026, 6, 30))
    assert dates, "some dates should still fall inside the contract"
    assert max(d for d, _ in dates) <= date(2026, 6, 30)


# --- Projection (rules S1-S3) ---------------------------------------------

def test_projection_ties_out_to_the_memo_annual_cash_flow():
    """Rule S1/S2: the monthly projection must sum to the memo's annual figure."""
    inputs = StructureInputs(
        pattern="PAYROLL", qualifying_income=600_000.0, other_income=100_000.0,
        other_debt_annual=24_000.0, loan_amount=100_000.0,
        interest_rate=12.0, funding_date=date(2026, 1, 1), target_term_months=12,
    )
    flow = st.project_cash_flow(inputs)
    twelve = flow[:12]
    gross = 700_000.0
    expected = gross * (1 - TAX_RATE - LIVING_RATE) - 24_000.0
    assert sum(m.available for m in twelve) == pytest.approx(expected, rel=1e-6)
    assert sum(m.gross for m in twelve) == pytest.approx(gross, rel=1e-6)


def test_living_expenses_are_level_but_taxes_follow_the_checks():
    """Rule S2: living costs do not stop in the dry season; taxes do."""
    flow = st.project_cash_flow(seasonal_inputs())
    living = {round(m.living, 2) for m in flow}
    assert len(living) == 1, "living expenses are spread evenly"
    dry = [m for m in flow if not m.in_season]
    assert dry and all(m.taxes == 0 for m in dry), "no income, no withholding"
    assert all(m.available < 0 for m in dry), "dry months run a deficit"


def test_off_gross_fee_defaults_to_zero_so_the_projection_matches_the_memo():
    assert StructureInputs().fee_off_gross_pct == 0.0


# --- Candidate structures --------------------------------------------------

def test_seasonal_amortization_never_schedules_a_dry_month_payment():
    """The core promise of the tab."""
    result = st.propose_structures(seasonal_inputs())
    seasonal = next(c for c in result.candidates if c.key == "seasonal_amortization")
    in_season = {(m.year, m.month) for m in result.cash_flow if m.in_season}
    for p in seasonal.payments:
        assert (p.iso_date.year, p.iso_date.month) in in_season, \
            f"payment on {p.date} falls in a month with no income"


def test_seasonal_amortization_retires_the_principal_exactly():
    result = st.propose_structures(seasonal_inputs())
    seasonal = next(c for c in result.candidates if c.key == "seasonal_amortization")
    assert seasonal.total_principal == pytest.approx(200_000.0, abs=1.0)


def test_interest_only_balloon_is_offered_as_the_baseline():
    """The house default must always appear for the side-by-side."""
    result = st.propose_structures(seasonal_inputs())
    keys = [c.key for c in result.candidates]
    assert "interest_only_balloon" in keys
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert baseline.amortization_type == "interest_only"
    assert baseline.payments[-1].is_balloon


def test_bullet_reserve_has_one_payment_and_funds_its_own_interest():
    inputs = seasonal_inputs(expected_exit_date=date(2027, 3, 1),
                             expected_exit_label="sale of the Harbor Road lot")
    result = st.propose_structures(inputs)
    bullet = next(c for c in result.candidates if c.key == "bullet_reserve")
    assert len(bullet.payments) == 1
    assert bullet.interest_reserve > 0
    assert bullet.maturity_date == date(2027, 3, 31), "30-day buffer past the event"


def test_fully_amortized_only_offered_when_income_is_year_round():
    seasonal = [c.key for c in st.propose_structures(seasonal_inputs()).candidates]
    assert "fully_amortized" not in seasonal
    payroll = [c.key for c in
               st.propose_structures(seasonal_inputs(pattern="PAYROLL")).candidates]
    assert "fully_amortized" in payroll
    assert "seasonal_amortization" not in payroll


# --- Scoring (rules S4, S5) ------------------------------------------------

def test_dry_month_maturity_is_flagged():
    """Rule S5 — a balloon must not land where there is no cash."""
    # A January funding matures the following January: deep in the dry season,
    # after months of drawdown.
    result = st.propose_structures(seasonal_inputs(funding_date=date(2027, 1, 1)))
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert baseline.matures_in_dry_month
    assert any("drawing down the dry season" in w for w in baseline.warnings)


def test_maturity_just_after_the_season_is_not_treated_as_a_bad_dry_month():
    """Rule S5, the other half — a dry month at the cash PEAK is the right one.

    A May 1 funding maturing Nov 1 lands in a month with no income, but
    immediately after the last receipt of the season, with the whole season
    banked. That is deliberate structuring, not an error.
    """
    result = st.propose_structures(seasonal_inputs(
        funding_date=date(2026, 5, 1), target_term_months=6,
        income_horizon=date(2026, 10, 31),
    ))
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert baseline.maturity_date == date(2026, 11, 1)
    assert baseline.matures_in_dry_month
    assert any("right side of the dry season" in w for w in baseline.warnings)
    assert not any("drawing down" in w for w in baseline.warnings)


def test_offseason_payment_shows_negative_same_month_coverage():
    """Rule S4 — the two coverage numbers must not be collapsed into one.

    The house default bills interest every month, including the five dry
    months, where the borrower is running a deficit. Same-month coverage goes
    NEGATIVE there while banked-cash coverage stays healthy — that gap is the
    whole reason the tab exists, so it must survive in the output.
    """
    result = st.propose_structures(seasonal_inputs())
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert baseline.min_coverage < 0, "a dry-season payment is not covered by that month"
    assert baseline.min_cushion_coverage > 1, "but in-season banked cash does cover it"

    seasonal = next(c for c in result.candidates if c.key == "seasonal_amortization")
    assert seasonal.min_coverage > 0, "matching the cadence removes the deficit months"


def test_thin_coverage_fails_and_says_which_month():
    result = st.propose_structures(seasonal_inputs(loan_amount=800_000.0))
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert not baseline.passes
    assert baseline.tightest_month
    assert any("Tightest month" in w for w in baseline.warnings)


def test_non_guaranteed_income_warns_against_deferring_principal():
    result = st.propose_structures(seasonal_inputs(income_contracted=False))
    baseline = next(c for c in result.candidates if c.key == "interest_only_balloon")
    assert any("not guaranteed" in w for w in baseline.warnings)


# --- Recommendation --------------------------------------------------------

def test_event_driven_deal_recommends_the_bullet():
    """Waiting on a signature: nothing to service, so balloon + reserve."""
    inputs = seasonal_inputs(qualifying_income=0.0, expected_exit_date=date(2027, 3, 1),
                             expected_exit_label="sale of the Harbor Road lot")
    result = st.propose_structures(inputs)
    rec = next(c for c in result.candidates if c.recommended)
    assert rec.key == "bullet_reserve"


def test_recurring_seasonal_income_recommends_seasonal_amortization():
    result = st.propose_structures(seasonal_inputs(loan_amount=50_000.0))
    rec = next(c for c in result.candidates if c.recommended)
    assert rec.key == "seasonal_amortization"


def test_exactly_one_candidate_is_recommended():
    for inputs in (seasonal_inputs(), seasonal_inputs(pattern="PAYROLL"),
                   seasonal_inputs(loan_amount=900_000.0)):
        result = st.propose_structures(inputs)
        assert sum(1 for c in result.candidates if c.recommended) == 1


# --- Bonus events ----------------------------------------------------------

def test_an_income_event_shows_up_in_its_month():
    inputs = seasonal_inputs(income_events=[
        IncomeEvent(label="Q1 distribution", date=date(2027, 3, 15), amount=150_000.0),
    ])
    flow = st.project_cash_flow(inputs)
    march = next(m for m in flow if (m.year, m.month) == (2027, 3))
    assert march.bonus == pytest.approx(150_000.0)
    assert march.available > 0, "the bonus rescues an otherwise dry month"


# --- Push into Loan Documents ---------------------------------------------

def test_schedule_rows_match_the_loandocs_contract():
    """to_schedule_rows must produce valid ScheduleRow payloads."""
    result = st.propose_structures(seasonal_inputs())
    seasonal = next(c for c in result.candidates if c.key == "seasonal_amortization")
    rows = st.to_schedule_rows(seasonal)
    assert rows
    for r in rows:
        parsed = ScheduleRow(**r)          # raises if the shape is wrong
        assert parsed.date and parsed.total is not None
    # Windows-safe date formatting (never the glibc-only %-d)
    assert rows[0]["date"][0].isdigit()


# --- Routes ----------------------------------------------------------------

def test_propose_route_returns_candidates():
    res = client.post("/api/structure/propose",
                      json={"inputs": seasonal_inputs().model_dump(mode="json")})
    assert res.status_code == 200
    body = res.json()
    assert body["candidates"] and body["cash_flow"]
    assert body["cadence_used"]["pattern"] == "SEASONAL"


def test_propose_route_requires_a_loan_amount():
    res = client.post("/api/structure/propose",
                      json={"inputs": seasonal_inputs(loan_amount=0).model_dump(mode="json")})
    assert res.status_code == 400


def test_select_route_returns_pushable_rows():
    res = client.post("/api/structure/select", json={
        "inputs": seasonal_inputs().model_dump(mode="json"),
        "candidate_key": "seasonal_amortization",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["amortization_type"] in ("balloon", "interest_only", "fully_amortized")
    assert body["repayment_schedule"]


def test_select_route_404s_on_an_unknown_candidate():
    res = client.post("/api/structure/select", json={
        "inputs": seasonal_inputs().model_dump(mode="json"),
        "candidate_key": "does_not_exist",
    })
    assert res.status_code == 404


def test_cadences_route_lists_the_patterns():
    res = client.get("/api/structure/cadences")
    assert res.status_code == 200
    assert {c["pattern"] for c in res.json()} >= {
        "PAYROLL", "DISTRIBUTIONS", "RENTS", "SEASONAL", "PROJECT"}


# --- Extraction tolerance --------------------------------------------------
#
# The prompt tells Claude to use null for anything the documents do not state.
# The model must therefore survive nulls in EVERY field — including the str and
# list ones. Getting this wrong 500s the whole upload on any document that
# happens to be missing a field (the bug reported 2026-08-10; the reference
# memo hid it by having every field populated).

from app.structure_extraction import ExtractedIncomeEvent, StructureExtraction


def test_extraction_survives_null_in_every_field():
    payload = {k: None for k in StructureExtraction.model_fields}
    ed = StructureExtraction(**payload)          # must not raise
    assert ed.borrower_name == ""                # str -> ""
    assert ed.income_events == []                 # list -> []
    assert ed.qualifying_income is None                     # Optional stays None


def test_extraction_accepts_formatted_money_and_percents():
    ed = StructureExtraction(qualifying_income="$1,650,000", loan_amount="3,000,000",
                             interest_rate="15%", origination_fee_pct="4.0 %")
    assert ed.qualifying_income == 1_650_000
    assert ed.loan_amount == 3_000_000
    assert ed.interest_rate == 15
    assert ed.origination_fee_pct == 4.0


def test_unparseable_number_degrades_to_none_rather_than_raising():
    assert StructureExtraction(qualifying_income="not stated").qualifying_income is None


def test_income_event_amounts_are_cleaned_too():
    b = ExtractedIncomeEvent(label="Q3 draw", date="2026-07-01", amount="$550,000")
    assert b.amount == 550_000
    assert ExtractedIncomeEvent(label=None).label == ""


def test_pay_frequency_normalizes_onto_the_supported_cadences():
    """IncomeCadence.pay_frequency is a Literal — an unmapped value would fail
    the /propose call later, far from the cause."""
    cases = {
        "semi-monthly": "semimonthly", "Semi Monthly": "semimonthly",
        "bi-weekly": "semimonthly", "twice a month": "semimonthly",
        "weekly": "weekly", "weekly draw": "weekly",
        "Monthly": "monthly", "every month": "monthly",
        "whenever he feels like it": None, "": None,
    }
    for raw, expected in cases.items():
        assert StructureExtraction(pay_frequency=raw).pay_frequency == expected, raw
    # and whatever survives must be a value the cadence model accepts
    from app.structure_models import IncomeCadence
    for v in ("weekly", "semimonthly", "monthly"):
        assert IncomeCadence(pay_frequency=v).pay_frequency == v


def test_guarantee_accepts_yes_no_strings():
    assert StructureExtraction(income_contracted="Yes").income_contracted is True
    assert StructureExtraction(income_contracted="conditional").income_contracted is False
    assert StructureExtraction(income_contracted=None).income_contracted is None


def test_extract_route_never_returns_a_bare_500(monkeypatch):
    """An unexpected shape must come back as a readable 422, not a 500."""
    from app import structure_extraction as se

    def boom(_docs):
        raise ValueError("unexpected field 'qualifying_income' was a dict")
    monkeypatch.setattr(se, "extract_documents", boom)

    res = client.post("/api/structure/extract", json=[
        {"filename": "x.pdf", "mime": "application/pdf", "b64": "eA=="},
    ])
    assert res.status_code == 422
    assert "unexpected field" in res.json()["detail"]


def test_extract_route_still_400s_with_no_documents():
    assert client.post("/api/structure/extract", json=[]).status_code == 400


# --- Sendable summary ------------------------------------------------------
#
# What actually goes to credit. It must show EVERY option, including the ones
# that fail — a structure memo that shows only the answer hides the argument.

from app import structure_summary as ss


def test_summary_shows_every_option_including_the_failures():
    inputs = seasonal_inputs(loan_amount=300_000.0, qualifying_income=165_000.0,
                             other_debt_annual=35_000.0, funding_date=date(2026, 5, 1),
                             target_term_months=6, income_contracted=False)
    result = st.propose_structures(inputs)
    html = ss.render_html(result, inputs)
    assert [c for c in result.candidates if not c.passes], "scenario should fail some"
    for c in result.candidates:
        assert c.name in html, f"{c.name} missing from the summary"
    assert "Fails" in html and "Recommended" in html
    # the reasoning, not just the verdict
    assert "Options Considered" in html and "Projected Cash Flow" in html


def test_summary_uses_the_house_negative_format():
    """Negatives render as ($39,340), the way the memo does — never $-39,340."""
    assert ss._money(-39_340) == "($39,340)"
    assert ss._money(321_634) == "$321,634"
    assert ss._money(None) == "—"

    inputs = seasonal_inputs(funding_date=date(2026, 5, 1))
    html = ss.render_html(st.propose_structures(inputs), inputs)
    assert "$-" not in html, "a negative leaked through unformatted"


def test_summary_states_the_leverage_and_the_guarantee_caveat():
    inputs = seasonal_inputs(loan_amount=300_000.0, qualifying_income=165_000.0,
                             other_debt_annual=35_000.0, income_contracted=False)
    ctx = ss.build_context(st.propose_structures(inputs), inputs)
    assert ctx["leverage"].endswith("x")
    assert ctx["guaranteed"] is False        # template prints "(not fully guaranteed)"
    assert ctx["recommended"] is not None


def test_summary_survives_a_deal_with_almost_nothing_filled_in():
    """No term sheet is uploaded on these deals, so most fields are blank."""
    inputs = StructureInputs(loan_amount=50_000.0)
    html = ss.render_html(st.propose_structures(inputs), inputs)
    assert "Proposed Loan Structure" in html


def test_summary_filename_is_safe():
    assert ss.filename(StructureInputs(borrower_name="José E. Vásquez Jr.")) \
        .startswith("Proposed_Structure_")
    assert ss.filename(StructureInputs()) == "Proposed_Structure_Borrower.pdf"


def test_summary_html_route_renders():
    res = client.post("/api/structure/summary/html",
                      json={"inputs": seasonal_inputs().model_dump(mode="json")})
    assert res.status_code == 200
    assert "Proposed Loan Structure" in res.text


def test_summary_pdf_route_requires_a_loan_amount():
    res = client.post("/api/structure/summary/pdf",
                      json={"inputs": seasonal_inputs(loan_amount=0).model_dump(mode="json")})
    assert res.status_code == 400


# --- Proposing the terms ---------------------------------------------------
#
# Lauren/Jim, 2026-08-10: the tool should propose the loan itself rather than be
# handed one, and say whether the borrower can actually pay it off.

def test_capacity_is_measured_against_banked_cash_not_the_thinnest_month():
    """South River's facilities are balloons retired from a whole season's
    accumulated income. Testing the tightest single month instead caps a
    seven-figure seasonal income at a tiny fraction of itself, because the dry
    months carry no receipts at all."""
    inputs = seasonal_inputs(qualifying_income=1_650_000.0, other_debt_annual=350_000.0,
                             funding_date=date(2026, 5, 1), target_term_months=6,
                             income_horizon=date(2026, 10, 31), loan_amount=1.0)
    cap = st.max_supportable_loan(inputs)
    assert cap > 300_000, f"capacity {cap:,.0f} is implausibly low for a $1.65m income"


def test_terms_are_held_at_the_lti_policy_limit_when_policy_binds():
    from app.calculations import LTI_MAX_PCT
    inputs = seasonal_inputs(qualifying_income=1_650_000.0, other_debt_annual=350_000.0,
                             funding_date=date(2026, 5, 1), target_term_months=6,
                             income_horizon=date(2026, 10, 31), loan_amount=1.0)
    t = st.propose_terms(inputs)
    assert t.policy_cap == pytest.approx(1_650_000.0 * LTI_MAX_PCT / 100, abs=1000)
    assert t.loan_amount <= t.policy_cap
    if t.cash_capacity >= t.policy_cap:
        assert t.binding_constraint == "policy"
        assert any("policy limit" in w for w in t.warnings)


def test_cash_flow_can_bind_before_policy_does():
    """Long-dated contracted income, thin near-term cash: policy allows far more.

    Funded at the start of the dry season, six months of which carry no
    receipts at all, while the leases behind the income run for years.
    """
    inputs = seasonal_inputs(qualifying_income=1_000_000.0, other_debt_annual=200_000.0,
                             income_contracted=True, funding_date=date(2026, 11, 1),
                             target_term_months=6, loan_amount=1.0)
    t = st.propose_terms(inputs, contracted_income=3_950_000.0)
    assert t.policy_cap > t.cash_capacity
    assert t.binding_constraint == "cash flow"
    assert t.loan_amount == t.cash_capacity


def test_terms_answer_the_can_they_repay_question_in_words():
    t = st.propose_terms(seasonal_inputs(loan_amount=1.0, funding_date=date(2026, 5, 1)))
    assert t.repayment_note
    assert isinstance(t.can_repay, bool)


def test_rate_and_points_are_house_defaults_and_say_so():
    """Nothing here prices risk — the basis must be stated, not implied."""
    t = st.propose_terms(seasonal_inputs(loan_amount=1.0))
    assert t.interest_rate == st.HOUSE_RATE_PCT
    assert t.origination_fee_pct == st.HOUSE_POINTS_PCT
    assert t.target_term_months == st.HOUSE_TERM_MONTHS
    assert "not a priced rate" in t.rate_basis


def test_event_driven_deal_with_no_income_is_not_called_repayable_from_earnings():
    t = st.propose_terms(seasonal_inputs(qualifying_income=0.0, loan_amount=1.0,
                                         expected_exit_date=date(2027, 3, 1)))
    assert t.binding_constraint == "event"
    assert "event" in t.repayment_note.lower()


def test_terms_route():
    res = client.post("/api/structure/terms", json={
        "inputs": seasonal_inputs(loan_amount=1.0).model_dump(mode="json"),
        "contracted_income": 3_950_000.0,
    })
    assert res.status_code == 200
    assert res.json()["loan_amount"] > 0


# --- PFS handling borrowed from the credit memo ----------------------------

def test_debt_service_uses_the_memos_own_rows():
    """Same rows the memo's cash flow counts — not a second opinion from the model."""
    from app.models import Extraction
    ed = Extraction(mortgage_payments=300_000, auto_payments=30_000,
                    interest_principal_loans=20_000)
    ds = st.debt_service_from_memo(ed, date(2026, 8, 1))
    assert ds["annual"] == pytest.approx(350_000.0)


def test_debt_service_handles_no_extraction():
    assert st.debt_service_from_memo(None)["annual"] == 0.0


def test_debt_service_route():
    res = client.post("/api/structure/debt-service", json={
        "terms": {"name": "X", "fund": "2026-08-01"},
        "extraction": {"mortgage_payments": 120000},
    })
    assert res.status_code == 200
    assert res.json()["annual"] == pytest.approx(120000.0)
