"""Tests that lock in the underwriting rules using the Alvarado reference deal.

These numbers were agreed with the business. If a change to calculations.py
breaks a test here, that is the test doing its job: either the change is wrong,
or the rule genuinely changed and the expected number must be updated
deliberately (with sign-off), not silently.

Run:  cd backend && pytest
"""

from datetime import date

import pytest

from app.models import (
    Extraction, LineItem, DealTerms, UsesOfFunds, DisbursementItem,
    DebtScheduleRow,
)
from app import calculations as calc
from app import memo as memo_service


# --- The Alvarado reference deal ------------------------------------------

ALVARADO_FACILITY_DUE = 2_703_754      # loan + interest, per the amort schedule
ALVARADO_LOAN = 2_499_000
ALVARADO_SALARY = 9_000_000            # guaranteed season salary


@pytest.fixture
def alvarado() -> Extraction:
    return Extraction(
        borrower_name="José Alvarado",
        salary=ALVARADO_SALARY,
        other_income=0,
        mortgage_payments=41_700,
        auto_payments=37_584,
        insurance=3_863,
        alimony=120_000,
        assets=[
            LineItem(label="Cash on Hand", amount=39_600),
            LineItem(label="Real Estate", amount=3_228_000),
            LineItem(label="Contract (Remaining)", amount=9_000_000),
        ],
        liabilities=[
            LineItem(label="Notes Payable: Contract Based", amount=5_454_402),
            LineItem(label="Notes Payable to: others", amount=303_095),
            LineItem(label="Mortgage Debt", amount=1_912_110),
        ],
        total_assets=12_267_600,
        facility_total_due=ALVARADO_FACILITY_DUE,
    )


# --- Balance sheet (PFS) ---------------------------------------------------

def test_total_liabilities_includes_facility_with_interest(alvarado):
    bs = calc.calc_balance_sheet(alvarado, ALVARADO_FACILITY_DUE)
    # 2,703,754 + 5,454,402 + 303,095 + 1,912,110
    assert bs["total_liab"] == 10_373_361


def test_net_worth_is_assets_minus_liabilities(alvarado):
    bs = calc.calc_balance_sheet(alvarado, ALVARADO_FACILITY_DUE)
    assert bs["assets_total"] == 12_267_600
    assert bs["net_worth"] == 1_894_239


def test_notes_payable_to_others_is_never_dropped(alvarado):
    bs = calc.calc_balance_sheet(alvarado, ALVARADO_FACILITY_DUE)
    labels = [l.label for l in bs["liab_items"]]
    assert "Notes Payable to: others" in labels


def test_alimony_is_not_a_pfs_liability():
    ed = Extraction(
        assets=[LineItem(label="Cash", amount=100_000)],
        total_assets=100_000,
        liabilities=[
            LineItem(label="Mortgage Debt", amount=50_000),
            LineItem(label="Alimony obligation", amount=84_000),  # misfiled
        ],
    )
    bs = calc.calc_balance_sheet(ed, 0)
    # alimony excluded -> only the mortgage counts
    assert bs["total_liab"] == 50_000


def test_negative_net_worth_renders_as_negative():
    # A facility underwritten against a small remaining contract can push Net
    # Worth below zero. _money() takes abs(), so the PFS Net Worth row must use
    # the signed formatter or a deficit would print as a positive figure.
    ed = Extraction(
        assets=[LineItem(label="Cash on Hand", amount=100_000)],
        total_assets=100_000,
        liabilities=[LineItem(label="Mortgage Debt", amount=2_000_000)],
    )
    terms = DealTerms(name="Deficit Borrower", loan=500_000, salary=100_000)
    html = memo_service.render_html(terms, ed, [])
    # with no rate/dates the facility falls back to the loan principal
    bs = calc.calc_balance_sheet(ed, 500_000)
    assert bs["net_worth"] == -2_400_000
    assert "($2,400,000)" in html
    # the bare positive form must not appear as the net-worth figure
    assert ">$2,400,000<" not in html


def test_auto_loan_balance_excluded_from_pfs():
    ed = Extraction(
        assets=[LineItem(label="Cash", amount=100_000)],
        total_assets=100_000,
        liabilities=[
            LineItem(label="Notes Payable to: others", amount=303_095),
            LineItem(label="Auto Loans", amount=60_000),  # already inside notes payable
        ],
    )
    bs = calc.calc_balance_sheet(ed, 0)
    assert bs["total_liab"] == 303_095


def test_taxes_are_not_a_pfs_liability():
    # rule 11: the PFS may report an estimated tax figure, but it is never a
    # liability and never reduces Net Worth.
    ed = Extraction(
        assets=[LineItem(label="Cash", amount=100_000)],
        total_assets=100_000,
        liabilities=[
            LineItem(label="Mortgage Debt", amount=50_000),
            LineItem(label="Taxes (Est of 35% of Gtd NHL Contract Remaining)",
                     amount=13_825_000),
        ],
    )
    bs = calc.calc_balance_sheet(ed, 0)
    labels = [l.label for l in bs["liab_items"]]
    assert not any("Taxes" in lbl for lbl in labels)
    # taxes excluded -> only the mortgage counts
    assert bs["total_liab"] == 50_000
    assert bs["net_worth"] == 50_000


# --- Uses of Funds (disbursement waterfall) -------------------------------

def test_uses_of_funds_full_breakdown_from_documents():
    # The example disbursement table: every line provided is carried through,
    # and the two subtotals foot to the lines.
    uof = UsesOfFunds(
        gross_loan_amount=4_435_000,
        deductions=[
            DisbursementItem(label="Lender Origination Fee (Est)", amount=133_050),
            DisbursementItem(label="SS Underwriting Fee (Est)", amount=133_050),
            DisbursementItem(label="BMO Payments thru 6/15/27 (Est)", amount=3_709_235),
            DisbursementItem(label="Legal/Closing Costs (Est)", amount=16_165),
        ],
        additional_costs=[
            DisbursementItem(label="Death & Disgrace Insurance (Est)", amount=110_875),
            DisbursementItem(label="Interest Reserve (Est)", amount=332_625),
        ],
    )
    r = calc.calc_uses_of_funds(uof, loan=4_435_000, fee_pct=3)
    assert r["gross"] == 4_435_000
    assert [d["label"] for d in r["deductions"]] == [
        "Lender Origination Fee (Est)", "SS Underwriting Fee (Est)",
        "BMO Payments thru 6/15/27 (Est)", "Legal/Closing Costs (Est)",
    ]
    assert r["to_borrower"] == 443_500       # gross − Σ deductions
    assert [a["label"] for a in r["additional_costs"]] == [
        "Death & Disgrace Insurance (Est)", "Interest Reserve (Est)",
    ]
    assert r["net_to_borrower"] == 0         # to_borrower − Σ additional_costs


def test_uses_of_funds_subtotals_are_recomputed_not_copied():
    # Even if a single deduction changes, the subtotals follow the lines.
    uof = UsesOfFunds(
        gross_loan_amount=1_000_000,
        deductions=[DisbursementItem(label="Origination Fee", amount=20_000)],
        additional_costs=[DisbursementItem(label="Interest Reserve", amount=80_000)],
    )
    r = calc.calc_uses_of_funds(uof, loan=1_000_000, fee_pct=2)
    assert r["to_borrower"] == 980_000
    assert r["net_to_borrower"] == 900_000


def test_uses_of_funds_drops_zero_lines():
    uof = UsesOfFunds(
        gross_loan_amount=500_000,
        deductions=[
            DisbursementItem(label="Origination Fee", amount=10_000),
            DisbursementItem(label="Empty line", amount=0),
        ],
    )
    r = calc.calc_uses_of_funds(uof, loan=500_000, fee_pct=2)
    assert [d["label"] for d in r["deductions"]] == ["Origination Fee"]
    assert r["to_borrower"] == 490_000


def test_uses_of_funds_falls_back_to_deal_terms():
    # No disbursement breakdown in the documents -> gross loan less the
    # origination fee from the deal terms, so Section VI is never empty.
    r = calc.calc_uses_of_funds(None, loan=2_499_000, fee_pct=2)
    assert r["gross"] == 2_499_000
    assert len(r["deductions"]) == 1
    assert r["deductions"][0]["label"] == "Origination Fee (2%)"
    assert r["deductions"][0]["amount"] == 49_980
    assert r["to_borrower"] == 2_449_020
    assert r["additional_costs"] == []


def test_render_html_contains_uses_of_funds_lines():
    ed = Extraction(
        uses_of_funds=UsesOfFunds(
            gross_loan_amount=4_435_000,
            deductions=[
                DisbursementItem(label="Lender Origination Fee (Est)", amount=133_050),
                DisbursementItem(label="BMO Payments thru 6/15/27 (Est)", amount=3_709_235),
            ],
            additional_costs=[
                DisbursementItem(label="Interest Reserve (Est)", amount=332_625),
            ],
        ),
    )
    terms = DealTerms(name="Test Borrower", loan=4_435_000, salary=9_000_000)
    html = memo_service.render_html(terms, ed, [])
    assert "Gross Loan Amount" in html
    assert "Lender Origination Fee (Est)" in html
    assert "BMO Payments thru 6/15/27 (Est)" in html
    assert "($3,709,235)" in html                       # deduction shown in parens
    assert "To be disbursed to Borrower (Est)" in html
    assert "Interest Reserve (Est)" in html
    assert "Net to be Disbursed to Borrower (Est)" in html


# --- DDD insurance follows the disbursement (rule 16) ----------------------
# Lauren, 2026-08-26: the memo and the Deal Summary & Policy Compliance
# coversheet claim a DDD policy ONLY when the documents' disbursement carries
# the premium line. Her reference example: a table ending at "To be disbursed
# to Borrower $1,339,000" has no policy; one continuing with
# "DDD insurance ($17,000)" down to a Net row does.

def _uof(*extra_labels):
    """The reference disbursement, plus any post-subtotal lines by label."""
    return UsesOfFunds(
        gross_loan_amount=1_400_000,
        deductions=[
            DisbursementItem(label="Lender Origination Fee", amount=56_000),
            DisbursementItem(label="Legal/Closing Costs", amount=5_000),
        ],
        additional_costs=[DisbursementItem(label=l, amount=17_000)
                          for l in extra_labels],
    )


def test_ddd_detected_in_the_disbursement_lines():
    assert calc.has_ddd_insurance(_uof("DDD insurance"))
    assert calc.has_ddd_insurance(_uof("Death & Disgrace Insurance (Est)"))
    # a premium deducted above the to-Borrower subtotal still counts
    assert calc.has_ddd_insurance(UsesOfFunds(
        gross_loan_amount=1_400_000,
        deductions=[DisbursementItem(label="D.D.D. Insurance Premium",
                                     amount=17_000)]))


def test_ddd_absent_when_the_disbursement_lacks_it():
    assert not calc.has_ddd_insurance(_uof())
    assert not calc.has_ddd_insurance(_uof("Interest Reserve (Est)"))
    assert not calc.has_ddd_insurance(None)   # no breakdown at all


def test_compliance_ddd_row_reads_none_in_place_without_the_policy():
    # The coversheet KEEPS its DDD row on every deal (Lauren, 2026-08-26);
    # without a premium line in the disbursement it reports "If applicable" /
    # "None in place" (reworded from "Required" / "No Insurance" — Lauren,
    # 2026-08-27: "Required" next to "None in place" read like a failed
    # requirement) as N/A — a statement of fact, not an exception — and the
    # mitigants sentence stops claiming the policy.
    cf = calc.build_cash_flow(Extraction(salary=10_000_000), None,
                              1_000_000, 10_000_000)
    comp = calc.calc_policy_compliance(ltc=10.0, cf=cf,
                                       mat_fmt="January 1, 2027",
                                       has_maturity=True, has_ddd=False)
    row = _comp_row(comp, "DDD insurance assigned to Lender")
    assert row["req"] == "If applicable"
    assert row["actual"] == "None in place"
    assert row["status"] == "na"
    assert all(e["label"] != "DDD insurance assigned to Lender"
               for e in comp["exceptions"])
    assert "DDD" not in comp["mitigants"]


def test_memo_reads_no_insurance_when_the_disbursement_lacks_ddd():
    ed = Extraction(uses_of_funds=_uof("Interest Reserve (Est)"))
    terms = DealTerms(name="Test Borrower", loan=1_400_000, salary=9_000_000)
    html = memo_service.render_html(terms, ed, [])
    # The coversheet keeps addressing insurance — honestly:
    assert "DDD insurance assigned to Lender" in html   # checklist row stays
    assert html.count("None in place") == 1             # its Actual
    assert html.count("No Insurance") == 1              # the Collateral cell
    # ...while the memo body stops claiming a policy:
    assert "DDD policy" not in html            # Collateral cell + Section III
    assert "DDD insurance policy" not in html  # both Tertiary cards
    assert "Lloyd" not in html                 # Section II bullet too
    assert "disability insurance policy" not in html   # injury FAQ bullet
    assert "Tertiary" not in html              # Sections I and XI lose the card


def test_memo_includes_ddd_when_the_disbursement_reflects_it():
    ed = Extraction(uses_of_funds=_uof("DDD insurance"))
    terms = DealTerms(name="Test Borrower", loan=1_400_000, salary=9_000_000)
    html = memo_service.render_html(terms, ed, [])
    assert "DDD policy" in html
    assert "DDD insurance assigned to Lender" in html
    assert "death &amp; disability insurance policy" in html
    assert html.count("Tertiary") == 2         # Sections I and XI
    assert "No Insurance" not in html
    assert "None in place" not in html


# --- Cash flow (Guarantor Analysis) ---------------------------------------

def test_taxes_are_45_percent_of_gross(alvarado):
    cf = calc.build_cash_flow(alvarado, None, ALVARADO_LOAN, ALVARADO_SALARY)
    assert cf["income"] == 9_000_000
    assert cf["taxes"] == 4_050_000          # 45%
    assert cf["living"] == 900_000           # 10%


def test_other_income_is_added_to_gross():
    ed = Extraction(salary=9_000_000, other_income=52_553)
    cf = calc.build_cash_flow(ed, None, ALVARADO_LOAN, 0)
    assert cf["income"] == 9_052_553
    assert cf["taxes"] == round(9_052_553 * 0.45)


def test_facility_in_cash_flow_is_principal_only(alvarado):
    cf = calc.build_cash_flow(alvarado, None, ALVARADO_LOAN, ALVARADO_SALARY)
    assert cf["proposed_ds"] == ALVARADO_LOAN  # NOT the interest-included figure


def test_alimony_always_appears_in_cash_flow(alvarado):
    cf = calc.build_cash_flow(alvarado, None, ALVARADO_LOAN, ALVARADO_SALARY)
    labels = [d["label"] for d in cf["debt_items"]]
    assert "Alimony / child support" in labels


def test_alimony_from_other_expenses_row():
    ed = Extraction(
        salary=1_000_000,
        other_expenses=[LineItem(label="Child Support", amount=96_000)],
    )
    cf = calc.build_cash_flow(ed, None, 0, 0)
    alimony = [d for d in cf["debt_items"] if d["label"] == "Alimony / child support"]
    assert alimony and alimony[0]["amt"] == 96_000


def test_computed_rows_not_double_counted():
    ed = Extraction(
        salary=1_000_000,
        other_expenses=[
            LineItem(label="Income Taxes", amount=450_000),       # skipped (computed)
            LineItem(label="Ordinary Living Expenses", amount=100_000),  # skipped
            LineItem(label="Private school tuition", amount=60_000),     # kept
        ],
    )
    cf = calc.build_cash_flow(ed, None, 0, 0)
    labels = [d["label"] for d in cf["debt_items"]]
    assert "Private school tuition" in labels
    assert "Income Taxes" not in labels
    assert "Ordinary Living Expenses" not in labels


# --- Facility total --------------------------------------------------------

def test_facility_total_prefers_computed_interest():
    amort = {"interest": 203_754, "balloon": 0, "rows": [], "months": 12}
    assert calc.facility_total(None, amort, ALVARADO_LOAN) == ALVARADO_LOAN + 203_754


def test_facility_total_falls_back_to_documents(alvarado):
    # No amort computed -> use the documents' stated facility total
    assert calc.facility_total(alvarado, None, ALVARADO_LOAN) == ALVARADO_FACILITY_DUE


# --- Amortization ----------------------------------------------------------

def test_amort_interest_actual_365():
    amort = calc.calc_amort(2_499_762, 12.0, date(2026, 1, 1), date(2027, 1, 1))
    # 2,499,762 * 12% * 365/365 = 299,971 (rounded)
    assert amort["interest"] == round(2_499_762 * 0.12)
    assert amort["balloon"] == 2_499_762 + amort["interest"]
    assert amort["months"] == 12


# --- Repayment schedule (Section X display) --------------------------------

def test_repayment_schedule_interest_monthly_principal_balloon():
    # Interest paid every month; principal repaid as a single balloon at the end.
    sched = calc.calc_repayment_schedule(4_435_000, 15.0, date(2026, 6, 15), date(2026, 12, 15))
    monthly = round(4_435_000 * 0.15 / 12)   # 55,438

    assert sched["months"] == 6
    assert len(sched["rows"]) == 6
    assert monthly == 55_438
    # interest is paid in every period
    assert all(r["interest"] == monthly for r in sched["rows"])
    # principal appears only on the final (balloon) payment
    assert [r["principal"] for r in sched["rows"]] == [0, 0, 0, 0, 0, 4_435_000]
    assert sched["rows"][-1]["is_balloon"] is True
    # totals foot to the displayed rows
    assert sched["total_interest"] == monthly * 6
    assert sched["total_principal"] == 4_435_000
    assert sched["total_payment"] == monthly * 6 + 4_435_000
    # payments run on the 15th, July through December 2026
    assert sched["rows"][0]["date"] == "15-Jul-26"
    assert sched["rows"][-1]["date"] == "15-Dec-26"


def test_repayment_schedule_uses_documents_when_present(alvarado):
    # When the extraction carries a schedule, the memo reproduces THOSE rows
    # verbatim rather than computing its own.
    from app.models import RepaymentRow
    alvarado.repayment_schedule = [
        RepaymentRow(date="15-Jul-26", interest=55_438, principal=0, total=55_438),
        RepaymentRow(date="15-Aug-26", interest=55_438, principal=4_435_000, total=4_490_438),
    ]
    terms = DealTerms(
        name="José Alvarado", loan=ALVARADO_LOAN, rate=15, fee=2, salary=ALVARADO_SALARY,
        fund=date(2026, 6, 15), mat=date(2026, 8, 15),
    )
    html = memo_service.render_html(terms, alvarado, [])
    assert "15-Jul-26" in html and "15-Aug-26" in html
    assert "$4,490,438" in html      # a document row's total, copied through
    assert "$4,545,876" in html      # totals row = sum of the document's rows
    # the computed fallback (monthly interest on the 2,499,000 loan) must NOT run
    computed_monthly = round(ALVARADO_LOAN * 0.15 / 12)  # 31,238
    assert f"${computed_monthly:,}" not in html


# --- LTC -------------------------------------------------------------------

def test_confirmed_salary_overrides_extracted_salary_in_cash_flow(alvarado):
    """A salary corrected on the deal-terms form must reach the cash flow.

    Regression: the extracted figure used to win here, so an underwriter who
    changed the guaranteed salary saw Section VII move while Section VIII
    silently kept the stale number.
    """
    cf = calc.build_cash_flow(alvarado, None, ALVARADO_LOAN, 13_500_000)
    assert cf["salary_income"] == 13_500_000
    assert cf["income"] == 13_500_000
    assert cf["taxes"] == round(13_500_000 * 0.45)


def test_extracted_salary_still_used_when_form_leaves_it_blank(alvarado):
    cf = calc.build_cash_flow(alvarado, None, ALVARADO_LOAN, 0)
    assert cf["salary_income"] == ALVARADO_SALARY


def test_confirmed_contract_remaining_drives_ltc_and_section_vii(alvarado):
    """The form's guaranteed-remaining value wins over the extracted one.

    Every figure driven by the LTC basis must move together: the coversheet's
    Guaranteed Remaining, the LTC itself, Section I's "advance against" line
    and Section VII's Total Contract Remaining row.
    """
    alvarado.contract_remaining = 9_000_000
    terms = DealTerms(name="José Alvarado", loan=3_425_000, rate=15,
                      salary=13_500_000, contract_remaining=13_500_000)
    html = memo_service.render_html(terms, alvarado)
    assert "$13,500,000" in html
    assert "$3,425,000 loan &divide; $13,500,000 guaranteed earnings" in html
    # 3,425,000 / 13,500,000 = 25.4%
    assert "25.4%" in html
    assert "38.1%" not in html


def test_extracted_contract_remaining_used_when_form_leaves_it_blank(alvarado):
    alvarado.contract_remaining = 9_000_000
    terms = DealTerms(name="José Alvarado", loan=3_425_000, rate=15,
                      salary=9_000_000)
    html = memo_service.render_html(terms, alvarado)
    assert "$3,425,000 loan &divide; $9,000,000 guaranteed earnings" in html


def test_pfs_contract_asset_marks_to_confirmed_remaining(alvarado):
    """An explicitly confirmed remaining contract restates the PFS asset.

    Section IX must not report a contract asset that contradicts Section VII.
    """
    bs = calc.calc_balance_sheet(alvarado, 0, None, 13_500_000)
    assert bs["assets_total"] == 16_767_600      # 12,267,600 + 4,500,000
    assert bs["contract_mark"]["reported"] == 9_000_000
    assert bs["contract_mark"]["restated"] == 13_500_000


def test_pfs_contract_asset_untouched_when_values_agree(alvarado):
    """The ordinary deal: the form was pre-filled from the documents."""
    bs = calc.calc_balance_sheet(alvarado, 0, None, 9_000_000)
    assert bs["assets_total"] == 12_267_600
    assert bs["contract_mark"] is None


def test_pfs_contract_asset_untouched_when_not_confirmed(alvarado):
    bs = calc.calc_balance_sheet(alvarado, 0, None, 0)
    assert bs["assets_total"] == 12_267_600
    assert bs["contract_mark"] is None


def test_marked_contract_asset_is_disclosed_in_section_ix(alvarado):
    alvarado.contract_remaining = 9_000_000
    terms = DealTerms(name="José Alvarado", loan=3_425_000, rate=15,
                      salary=9_000_000, contract_remaining=13_500_000)
    html = memo_service.render_html(terms, alvarado)
    assert "restated from the $9,000,000" in html
    assert "confirmed at underwriting" in html


def test_ltc_is_loan_over_guaranteed_earnings():
    assert calc.calc_ltc(2_499_000, 9_000_000) == pytest.approx(27.77, abs=0.01)


def test_ltc_zero_salary_safe():
    assert calc.calc_ltc(2_499_000, 0) == 0.0


# --- SSN masking -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("123-45-6789", "XXX-XX-6789"),
    ("123456789", "XXX-XX-6789"),
    ("6789", "XXX-XX-6789"),
    ("", ""),
    (None, ""),
])
def test_ssn_masking(raw, expected):
    assert calc.mask_ssn(raw) == expected


# --- Sport label ("professional <sport> player") --------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Professional Ice Hockey", "Ice Hockey"),
    ("professional basketball", "basketball"),
    ("PROFESSIONAL Football", "Football"),
    ("Ice Hockey", "Ice Hockey"),
    ("", ""),
    (None, ""),
])
def test_normalize_sport_strips_leading_professional(raw, expected):
    assert calc.normalize_sport(raw) == expected


def test_render_does_not_double_professional():
    # The extraction often returns "Professional Ice Hockey"; the memo must not
    # render "professional Professional ...".
    terms = DealTerms(
        name="Erik Lindqvist", team="New York Rangers", league="NHL",
        sport="Professional Ice Hockey", loan=4_435_000, salary=39_500_000,
    )
    html = memo_service.render_html(terms, None, [])
    assert "professional Professional" not in html
    assert "Professional Professional" not in html
    assert "a Professional Ice Hockey player" in html


@pytest.mark.parametrize("raw,expected", [
    ("a professional Professional player", "a professional player"),
    ("Professional Professional contract", "Professional contract"),
    ("professional   professional", "professional"),
    # single / non-consecutive occurrences are left untouched
    ("a professional player", "a professional player"),
    ("No professional team. Professional contract salary.",
     "No professional team. Professional contract salary."),
])
def test_dedupe_professional(raw, expected):
    assert memo_service._dedupe_professional(raw) == expected


def test_render_dedupes_professional_in_narrative():
    # A narrative captured from the documents that itself doubles the word is
    # still cleaned up in the rendered memo (source-agnostic safety net).
    ed = Extraction(
        sport="Ice Hockey",
        sponsorship_narrative=(
            "Erik Lindqvist is a professional Professional Ice Hockey player "
            "for the New York Rangers of the NHL."
        ),
    )
    terms = DealTerms(name="Erik Lindqvist", team="New York Rangers",
                      league="NHL", sport="Ice Hockey", loan=4_435_000,
                      salary=39_500_000)
    html = memo_service.render_html(terms, ed, [])
    assert "professional Professional" not in html
    assert "a professional Ice Hockey player" in html


# --- Loan term (Section II Action Request) --------------------------------

def test_loan_term_months_prefers_document_term():
    # The term stated in the documents wins over the funding-to-maturity span.
    ed = Extraction(loan_term_months=6)
    assert calc.loan_term_months(ed, {"months": 12}) == 6


def test_loan_term_months_falls_back_to_date_span():
    # No stated term -> use the amortization schedule's month count.
    ed = Extraction(loan_term_months=0)
    assert calc.loan_term_months(ed, {"months": 9}) == 9
    # also when there's no extraction at all
    assert calc.loan_term_months(None, {"months": 9}) == 9


def test_loan_term_months_zero_when_unknown():
    assert calc.loan_term_months(None, None) == 0
    assert calc.loan_term_months(Extraction(), {"months": 0}) == 0


def test_render_shows_document_term_without_dates():
    # Term sheet says 6 months; no funding/maturity dates entered. Section II
    # must still state the 6-month term sourced from the documents.
    ed = Extraction(loan_term_months=6)
    terms = DealTerms(name="Test Borrower", loan=4_435_000, rate=13.5, salary=9_000_000)
    html = memo_service.render_html(terms, ed, [])
    assert "<strong>6</strong>-month full balloon loan" in html


def test_render_document_term_overrides_date_span():
    # Even with dates that span 12 months, the stated 6-month term is used.
    ed = Extraction(loan_term_months=6)
    terms = DealTerms(
        name="Test Borrower", loan=4_435_000, rate=13.5, salary=9_000_000,
        fund=date(2026, 1, 1), mat=date(2027, 1, 1),
    )
    html = memo_service.render_html(terms, ed, [])
    assert "<strong>6</strong>-month full balloon loan" in html


# --- Rule 15: stale-PFS roll-forward (Method A) ---------------------------
#
# The reference set is a real SureSports PFS: statement dated 10/16/2025, read
# for a memo dated 8/5/2026 = 10 elapsed monthly payments. It deliberately
# includes the messy cases that form produces — a maturity date typed without a
# separator, an Excel epoch placeholder for an empty origination cell, a
# revolving line with no payment, and a contract note paid per game check.

PFS_DATE = "2025-10-16"
MEMO_DATE = date(2026, 8, 5)


@pytest.fixture
def stale_pfs() -> Extraction:
    return Extraction(
        pfs_date=PFS_DATE,
        total_assets=5_168_151,
        assets=[LineItem(label="Real Estate", amount=3_234_251),
                LineItem(label="Contract (Remaining)", amount=1_933_900)],
        liabilities=[
            LineItem(label="Mortgage Debt", amount=2_753_066),
            LineItem(label="Notes Payable to: others", amount=241_235),
            LineItem(label="Notes Payable: Contract Based", amount=1_544_835),
        ],
        debt_schedule=[
            DebtScheduleRow(
                lender="Primary residence", category="mortgage_debt",
                balance=618_815, payment=6_302, payment_period="monthly",
                origination="2023", maturity="08/072053",   # missing separator
            ),
            DebtScheduleRow(
                lender="second residence", category="mortgage_debt",
                balance=2_134_251, payment=18_195, payment_period="monthly",
                origination="2025", maturity="5/12/2055",
            ),
            DebtScheduleRow(
                lender="Northgate Bank", category="notes_payable_others",
                balance=100_000, payment=1_799, payment_period="monthly",
                origination="7/17/1905",                    # Excel epoch = blank
                maturity="5/15/2031", description="auto",
            ),
            DebtScheduleRow(
                lender="Credit Cards", category="notes_payable_others",
                balance=50_000, payment=0, description="credit card",
            ),
            DebtScheduleRow(
                lender="MidState Bank", category="notes_payable_others",
                balance=91_235, payment=1_250, payment_period="monthly",
                origination="10/8/2025", maturity="10/8/2030",
            ),
            DebtScheduleRow(
                # Schedule G leaves "Amount / Pay Period" blank, as this form
                # does — a contract note is repaid out of game checks, so an
                # unstated period must never be treated as monthly.
                lender="Sports Finance Fund, LP",
                category="notes_payable_contract",
                balance=1_544_835, payment=77_454, payment_period="",
                origination="3/22/2024", maturity="10/15/2027",
            ),
        ],
    )


def _row(rf, lender):
    return next(r for r in rf["rows"] if r["lender"] == lender)


def test_rollforward_counts_payments_from_month_after_statement(stale_pfs):
    # 10/16/25 -> 8/5/26: November through August = 10 scheduled payments.
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    assert rf["applied"] is True
    assert rf["months"] == 10
    # Lauren's worked example on a real memo: 10/16/25 -> 7/28/26 = 9.
    assert calc.calc_debt_rollforward(stale_pfs, date(2026, 7, 28))["months"] == 9


def test_rollforward_deducts_payment_times_months(stale_pfs):
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    assert _row(rf, "Primary residence")["adjusted"] == 555_795   # 618,815 - 63,020
    assert _row(rf, "second residence")["adjusted"] == 1_952_301      # 2,134,251 - 181,950
    assert _row(rf, "Northgate Bank")["adjusted"] == 82_010                 # 100,000 - 17,990
    assert _row(rf, "MidState Bank")["adjusted"] == 78_735             # 91,235 - 12,500
    assert rf["total_paydown"] == 275_460


def test_revolving_and_non_monthly_debt_is_carried_as_reported(stale_pfs):
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    cards = _row(rf, "Credit Cards")
    assert cards["rolled"] is False and cards["adjusted"] == 50_000
    assert "revolving" in cards["reason"]
    # Repaid out of game checks, not monthly — never rolled forward.
    fund_note = _row(rf, "Sports Finance Fund, LP")
    assert fund_note["rolled"] is False and fund_note["adjusted"] == 1_544_835
    assert "monthly" in fund_note["reason"]


def test_unstated_pay_period_is_monthly_except_on_contract_notes(stale_pfs):
    # These forms routinely leave Schedule F/G's "Amount / Pay Period" blank.
    # An ordinary note still rolls forward; a contract-based note never does.
    for r in stale_pfs.debt_schedule:
        r.payment_period = ""
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    assert _row(rf, "Northgate Bank")["adjusted"] == 82_010
    assert _row(rf, "Primary residence")["adjusted"] == 555_795
    assert _row(rf, "Sports Finance Fund, LP")["rolled"] is False


def test_explicit_non_monthly_period_beats_the_default():
    ed = Extraction(
        pfs_date=PFS_DATE,
        liabilities=[LineItem(label="Notes Payable to: others", amount=50_000)],
        debt_schedule=[DebtScheduleRow(
            lender="Seasonal note", category="notes_payable_others",
            balance=50_000, payment=5_000, payment_period="per game check")],
    )
    rf = calc.calc_debt_rollforward(ed, MEMO_DATE)
    assert _row(rf, "Seasonal note")["rolled"] is False


def test_row_held_by_the_user_is_carried_as_reported(stale_pfs):
    stale_pfs.debt_schedule[0].treatment = "hold"
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    held = _row(rf, "Primary residence")
    assert held["rolled"] is False and held["adjusted"] == 618_815
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    by_label = {l.label: l.amount for l in bs["liab_items"]}
    assert by_label["Mortgage Debt"] == 2_753_066 - 181_950  # only the 2nd rolled


def test_zero_out_removes_the_whole_balance(stale_pfs):
    # A debt being paid off at closing: the full balance leaves the summary.
    stale_pfs.debt_schedule[2].treatment = "zero"          # Northgate Bank, $100,000
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    auto_note = _row(rf, "Northgate Bank")
    assert auto_note["zeroed"] is True
    assert auto_note["adjusted"] == 0 and auto_note["paydown"] == 100_000

    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    by_label = {l.label: l.amount for l in bs["liab_items"]}
    # 241,235 less the MidState roll-forward (12,500) and all of Northgate Bank.
    assert by_label["Notes Payable to: others"] == 241_235 - 12_500 - 100_000
    # Sentence-cased, and a lender's own capitals survive it.
    assert "The Northgate Bank balance of $100,000 is shown as repaid in full" in rf["note"]


def test_lender_capitalisation_survives_sentence_casing(stale_pfs):
    stale_pfs.debt_schedule[4].treatment = "zero"          # MidState Bank
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    assert "The MidState Bank balance" in rf["note"]


def test_zero_out_applies_even_when_the_statement_is_current(stale_pfs):
    # A payoff at closing has nothing to do with how old the statement is.
    current = date(2025, 11, 5)                            # PFS is 3 weeks old
    stale_pfs.debt_schedule[2].treatment = "zero"
    rf = calc.calc_debt_rollforward(stale_pfs, current)
    assert rf["applied"] is True and rf["months"] == 0
    assert _row(rf, "Northgate Bank")["adjusted"] == 0
    # Nothing else moved — the roll-forward proper still needs a stale PFS.
    assert rf["total_paydown"] == 100_000
    assert _row(rf, "MidState Bank")["reason"] == "the statement is current"
    assert "rolled forward" not in rf["note"]


def test_manually_added_debt_rolls_into_the_category_it_is_given(stale_pfs):
    # Extraction missed a note; the underwriter adds it by hand.
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Missed note", category="notes_payable_others",
        balance=30_000, payment=500, payment_period="monthly",
        maturity="1/1/2032"))
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    by_label = {l.label: l.amount for l in bs["liab_items"]}
    # The bucket already carries Northgate Bank + MidState; the added note joins them.
    assert by_label["Notes Payable to: others"] == 210_745 - 5_000


def test_debt_with_no_category_changes_no_total_and_warns(stale_pfs):
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Uncategorised", category="", balance=30_000, payment=500,
        payment_period="monthly"))
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    by_label = {l.label: l.amount for l in bs["liab_items"]}
    assert by_label["Notes Payable to: others"] == 210_745        # unchanged
    assert any("had no matching liability line" in w
               for w in bs["rollforward"]["warnings"])


def test_rollforward_reduces_summary_liabilities_and_net_worth(stale_pfs):
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    by_label = {l.label: l.amount for l in bs["liab_items"]}
    assert by_label["Mortgage Debt"] == 2_508_096          # -244,970
    assert by_label["Notes Payable to: others"] == 210_745  # -30,490
    assert by_label["Notes Payable: Contract Based"] == 1_544_835  # untouched
    assert bs["reported_liab"] == 4_539_136
    assert bs["stated_liab"] == 4_263_676
    assert bs["net_worth"] == 5_168_151 - 4_263_676


def test_rollforward_never_mutates_the_extraction(stale_pfs):
    calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert stale_pfs.liabilities[0].amount == 2_753_066


def test_current_pfs_is_used_exactly_as_reported(stale_pfs):
    # Three weeks old — not stale, so nothing moves.
    rf = calc.calc_debt_rollforward(stale_pfs, date(2025, 11, 5))
    assert rf["applied"] is False
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=date(2025, 11, 5))
    assert bs["stated_liab"] == 4_539_136


def test_undated_pfs_is_not_rolled_forward(stale_pfs):
    stale_pfs.pfs_date = None
    rf = calc.calc_debt_rollforward(stale_pfs, MEMO_DATE)
    assert rf["applied"] is False
    assert rf["warnings"], "an undated PFS must be surfaced, not silently ignored"


def test_no_debt_schedule_leaves_balances_as_reported(stale_pfs):
    stale_pfs.debt_schedule = []
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert bs["stated_liab"] == 4_539_136
    assert bs["rollforward"]["applied"] is False


def test_paydown_stops_at_maturity_and_never_goes_negative():
    ed = Extraction(
        pfs_date=PFS_DATE,
        total_assets=100_000,
        liabilities=[LineItem(label="Notes Payable to: others", amount=15_000)],
        debt_schedule=[
            # Matures 4 months after the statement: only 4 payments count,
            # not the 10 that have elapsed.
            DebtScheduleRow(lender="Short note", category="notes_payable_others",
                            balance=10_000, payment=1_000, payment_period="monthly",
                            maturity="2/28/2026"),
            # Payments would exceed the balance — clamp at zero, never below.
            DebtScheduleRow(lender="Nearly paid", category="notes_payable_others",
                            balance=5_000, payment=1_000, payment_period="monthly",
                            maturity="12/1/2030"),
        ],
    )
    rf = calc.calc_debt_rollforward(ed, MEMO_DATE)
    assert _row(rf, "Short note")["months_applied"] == 4
    assert _row(rf, "Short note")["adjusted"] == 6_000
    assert _row(rf, "Nearly paid")["adjusted"] == 0


@pytest.mark.parametrize("raw,expected", [
    ("08/072053", date(2053, 8, 7)),    # separator missing (real PFS typo)
    ("5/12/2055", date(2055, 5, 12)),
    ("2026-01-31", date(2026, 1, 31)),
    ("10/8/30", date(2030, 10, 8)),
    ("2023", date(2023, 1, 1)),         # Schedule D "Purchase Year"
    ("7/17/1905", None),                # Excel epoch showing through a blank cell
    ("1/0/1900", None),
    ("", None),
    ("NA", None),
])
def test_schedule_dates_are_parsed_or_rejected(raw, expected):
    assert calc._parse_loose_date(raw) == expected


def test_render_html_documents_the_rollforward(stale_pfs, monkeypatch):
    monkeypatch.setattr(memo_service, "date", type("D", (date,), {
        "today": staticmethod(lambda: MEMO_DATE)}))
    terms = DealTerms(name="Test Borrower", loan=2_025_000, rate=13.5,
                      salary=1_624_396)
    html = memo_service.render_html(terms, stale_pfs, [])
    # The adjusted mortgage total reaches the statement...
    assert "$2,508,096" in html
    # ...and the Credit paragraph explains it the way Lauren writes it.
    assert "October 16, 2025" in html
    assert "assuming all payments were made as agreed" in html.lower()
    assert "$618,815 to $555,795" in html


MEMO_TERMS = DealTerms(name="Test Borrower", loan=2_025_000, rate=13.5,
                      salary=1_624_396)


@pytest.fixture
def at_memo_date(monkeypatch):
    monkeypatch.setattr(memo_service, "date", type("D", (date,), {
        "today": staticmethod(lambda: MEMO_DATE)}))


def test_every_scheduled_debt_is_listed_under_its_summary_liability(
        stale_pfs, at_memo_date):
    """Section IX lists each financed debt beneath the liability it rolls into,
    including the ones carried exactly as reported — those move no total and
    would otherwise appear nowhere on the memo."""
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    for lender in ("Primary residence", "second residence", "Northgate Bank",
                   "Credit Cards", "MidState Bank", "Sports Finance Fund, LP"):
        assert lender in html, f"{lender} is missing from the memo"
    # Each line carries the balance the memo uses, with the basis in fine print.
    assert "$555,795" in html
    assert "$618,815 reported, rolled forward 10 months" in html
    assert "no scheduled payment (revolving)" in html          # Credit Cards
    assert "not on a monthly schedule" in html                 # contract note
    # Listed under the right parent: the mortgages sit between Mortgage Debt
    # and the next summary line, not loose at the foot of the table.
    mortgage = html.index("Mortgage Debt")
    others = html.index("Notes Payable to: others")
    assert mortgage < html.index("Primary residence") < others
    assert others < html.index("Northgate Bank") < html.index("Notes Payable: Contract Based")


def test_a_hand_added_debt_that_moves_no_total_still_shows(stale_pfs, at_memo_date):
    # The case that sent an underwriter looking for it: a debt entered by hand
    # and held as reported changes no figure, but must still reach the memo.
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Hand-added note", category="notes_payable_others",
        balance=30_000, payment=500, payment_period="monthly",
        treatment="hold"))
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    assert "Hand-added note" in html
    assert "held at the reported balance" in html


def test_detail_lines_never_change_the_totals(stale_pfs, at_memo_date):
    """The debts are a breakdown of the summary liabilities, not additions to
    them — listing them must leave Total Liabilities and Net Worth untouched."""
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Hand-added note", category="notes_payable_others",
        balance=30_000, payment=500, payment_period="monthly", treatment="hold"))
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert bs["stated_liab"] == 4_263_676
    assert bs["net_worth"] == 5_168_151 - 4_263_676


def test_a_zeroed_note_leaves_the_statement_and_moves_both_totals(
        stale_pfs, at_memo_date):
    """Treatment "zero" on a note the facility pays off at closing (Lauren,
    2026-09-02): the summary line drains to $0 and stops printing, Total
    Liabilities and Net Worth move by the full balance, and the foot of the
    table documents the removal — never under the "no matching liability
    line" caveat, which is for true orphans."""
    before = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    note = next(r for r in stale_pfs.debt_schedule
                if r.category == "notes_payable_contract")
    note.treatment = "zero"
    after = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert after["total_liab"] == before["total_liab"] - 1_544_835
    assert after["net_worth"] == before["net_worth"] + 1_544_835
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    assert "Notes Payable: Contract Based" not in html
    assert "Repaid in full &mdash; removed from the summary totals above" in html
    assert "$1,544,835 reported, shown as repaid in full" in html
    assert "no matching liability line" not in html


def test_a_removed_debt_vanishes_from_the_memo_and_moves_both_totals(
        stale_pfs, at_memo_date):
    """Treatment "remove" — the ✕ in Step 2b (Lauren, 2026-09-02): the debt
    does not appear on the memo AT ALL. Its balance leaves the summary
    liability (Total Liabilities and Net Worth move by the full amount), and
    nothing prints — no summary line, no detail row, no repaid-in-full
    disclosure, no credit-paragraph sentence. The visible roll-forward
    footnote still counts only the rolls."""
    before = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    note = next(r for r in stale_pfs.debt_schedule
                if r.category == "notes_payable_contract")
    note.treatment = "remove"
    after = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert after["total_liab"] == before["total_liab"] - 1_544_835
    assert after["net_worth"] == before["net_worth"] + 1_544_835
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    assert "Notes Payable: Contract Based" not in html
    assert "Sports Finance Fund" not in html        # no trace anywhere
    assert "repaid in full" not in html             # not a zero-out disclosure
    assert "no matching liability line" not in html # not an orphan either
    assert "$275,460" in html                       # footnote: the rolls only


def test_removal_works_on_a_current_statement_and_adds_no_footnote(stale_pfs):
    # Removal is not a roll-forward: it must land when the statement is
    # CURRENT (nothing else moves) and must not fabricate a roll-forward note.
    stale_pfs.pfs_date = MEMO_DATE.isoformat()
    note = next(r for r in stale_pfs.debt_schedule
                if r.category == "notes_payable_contract")
    note.treatment = "remove"
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    # Reported lines sum 4,539,136; only the removal moves them.
    assert bs["stated_liab"] == 4_539_136 - 1_544_835
    assert bs["rollforward"]["applied"] is False


def test_a_debt_with_no_summary_line_is_shown_but_marked_excluded(
        stale_pfs, at_memo_date):
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Uncategorised", category="", balance=30_000, payment=500,
        payment_period="monthly"))
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    assert "Uncategorised" in html
    assert "not carried in the summary totals above" in html


def test_no_detail_lines_without_a_schedule(stale_pfs, at_memo_date):
    stale_pfs.debt_schedule = []
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    assert 'class="dsc"' not in html


def test_a_split_summary_line_lists_each_debt_once(stale_pfs, at_memo_date):
    """A statement can carry TWO lines of the same category — "Notes Payable
    to others (Secured)" and "(Unsecured)" (2026-09-02, a real memo) — and the
    breakdown printed under both listed every Schedule F debt twice. Each debt
    is listed once, under the first line of its category (where the paydown is
    applied); the second line prints with no breakdown."""
    stale_pfs.liabilities = [
        LineItem(label="Mortgage Debt", amount=2_753_066),
        LineItem(label="Notes Payable to others (Secured)", amount=141_235),
        LineItem(label="Notes Payable to others (Unsecured)", amount=100_000),
        LineItem(label="Notes Payable: Contract Based", amount=1_544_835),
    ]
    html = memo_service.render_html(MEMO_TERMS, stale_pfs, [])
    # Count DETAIL ROWS, not bare names — the Credit paragraph also narrates
    # each rolled debt by name.
    for lender in ("Northgate Bank", "Credit Cards", "MidState Bank"):
        assert html.count(f'class="dsc-lbl">{lender}') == 1, \
            f"{lender} listed more than once"
    # One detail line per scheduled debt — nothing duplicated, nothing orphaned.
    assert html.count('class="dsc"') == len(stale_pfs.debt_schedule)


def test_unapplied_paydown_is_recorded_per_category(stale_pfs):
    stale_pfs.debt_schedule.append(DebtScheduleRow(
        lender="Uncategorised", category="", balance=30_000, payment=500,
        payment_period="monthly"))
    bs = calc.calc_balance_sheet(stale_pfs, facility_due=0, as_of=MEMO_DATE)
    assert bs["rollforward"]["unapplied_by_category"] == {"": 5_000}


# --- Deal terms pulled from the documents ---------------------------------

def test_render_falls_back_to_extracted_deal_terms():
    # Loan amount / rate / fee left blank on the form -> the memo uses the
    # values pulled from the documents (the term sheet in the screenshot).
    ed = Extraction(
        loan_amount=4_435_000, interest_rate_pct=13.5, origination_fee_pct=3,
        salary=39_500_000,
    )
    terms = DealTerms(name="Erik Lindqvist")   # nothing typed yet
    html = memo_service.render_html(terms, ed, [])
    assert "$4,435,000" in html                       # loan amount
    assert "13.5%" in html                            # interest rate
    assert "3% ($133,050)" in html                    # origination fee % and $


def test_typed_deal_terms_override_extracted():
    # A value the user typed always wins over the document value.
    ed = Extraction(loan_amount=4_435_000, interest_rate_pct=13.5,
                    origination_fee_pct=3, salary=39_500_000)
    terms = DealTerms(name="Erik Lindqvist", loan=5_000_000, rate=12, fee=2,
                      salary=39_500_000)
    html = memo_service.render_html(terms, ed, [])
    assert "$5,000,000" in html
    assert "12%" in html
    assert "2% ($100,000)" in html
    assert "$4,435,000" not in html


# --- Section VII: total contract remaining ---------------------------------

def test_contract_remaining_renders_in_section_vii():
    terms = DealTerms(name="Test Borrower", loan=4_435_000, salary=10_000_000)
    ed = Extraction(salary=10_000_000, contract_remaining=39_500_000)
    html = memo_service.render_html(terms, ed, [])
    assert "Total Contract Remaining" in html
    assert "$39,500,000" in html


def test_contract_remaining_is_the_ltc_and_section_i_basis():
    # Rule 10: with a remaining contract value, Section I's "advance against"
    # figure and the LTC guaranteed earnings both use it, not the season salary.
    terms = DealTerms(name="Test Borrower", loan=4_435_000, salary=8_500_000)
    ed = Extraction(salary=8_500_000, contract_remaining=39_500_000)
    html = memo_service.render_html(terms, ed, [])
    assert "advance against <strong>$39,500,000</strong> in guaranteed salary" in html
    assert "$39,500,000 guaranteed earnings = <strong>11.2%</strong>" in html
    assert "52.2" not in html  # loan / season salary must no longer drive LTC


def test_ltc_falls_back_to_season_salary_without_contract_remaining():
    terms = DealTerms(name="Test Borrower", loan=4_435_000, salary=8_500_000)
    html = memo_service.render_html(terms, Extraction(salary=8_500_000), [])
    assert "advance against <strong>$8,500,000</strong> in guaranteed salary" in html
    assert "$8,500,000 guaranteed earnings = <strong>52.2%</strong>" in html


def test_contract_remaining_row_absent_when_not_extracted():
    terms = DealTerms(name="Test Borrower", loan=4_435_000, salary=10_000_000)
    html = memo_service.render_html(terms, Extraction(salary=10_000_000), [])
    assert "Total Contract Remaining" not in html


# --- Deal Summary & Policy Compliance coversheet ---------------------------

def _compliance(ed, loan, salary):
    """Run the coversheet checklist the way render_html assembles its inputs."""
    guar = (ed.contract_remaining if ed and ed.contract_remaining else 0) or salary
    cf = calc.build_cash_flow(ed, None, loan, salary)
    return calc.calc_policy_compliance(
        ltc=calc.calc_ltc(loan, guar), cf=cf,
        mat_fmt="January 1, 2027", has_maturity=True,
    )


def _comp_row(comp, label):
    return next(r for r in comp["rows"] if r["label"] == label)


# The checklist criteria Lauren retired 2026-08-25: they must not print on the
# coversheet, in the exceptions block, or anywhere else in the memo.
RETIRED_CRITERIA = (
    "Combined contract-note leverage",
    "Combined LTV — subject property",
    "Salary fully guaranteed",
    "Payroll direct-deposit sweep",
    "Lien on real estate recorded (if appl.)",
    "Minimum credit score (mid)",
    "No derogatories / late payments",
)


def test_compliance_omits_the_retired_criteria(alvarado):
    """Leverage/LTV/salary-guarantee/payroll-sweep/RE-lien/credit rows were
    removed from the coversheet at Lauren's direction (2026-08-25). Alvarado
    used to carry a combined-leverage exception; the deal is now clean."""
    comp = _compliance(alvarado, ALVARADO_LOAN, ALVARADO_SALARY)
    labels = {r["label"] for r in comp["rows"]}
    for retired in RETIRED_CRITERIA:
        assert retired not in labels, retired
    # LTC 27.8% is WITHIN the 30% policy limit (raised from 25%, Lauren
    # 2026-08-25) and net cash flow is positive -> no exceptions remain.
    assert _comp_row(comp, "Loan-to-Contract (LTC)")["status"] == "pass"
    assert _comp_row(comp, "Positive net cash flow after debt svc.")["status"] == "pass"
    assert comp["exceptions"] == []


def test_compliance_ltc_over_30_is_still_an_exception():
    # 35% LTC: over even the raised limit -> exception, echoed in the block.
    comp = _compliance(Extraction(salary=10_000_000), 3_500_000, 10_000_000)
    assert _comp_row(comp, "Loan-to-Contract (LTC)")["status"] == "exc"
    assert any(e["label"] == "Loan-to-Contract (LTC)" for e in comp["exceptions"])


def test_compliance_clean_deal_has_no_exceptions():
    comp = _compliance(Extraction(salary=10_000_000), 1_000_000, 10_000_000)
    assert comp["exceptions"] == []
    assert _comp_row(comp, "Loan-to-Contract (LTC)")["status"] == "pass"


def test_memo_without_credit_notes_never_asserts_clean_credit(alvarado):
    """Section IV must not claim 'No bankruptcies...' when extraction produced
    no credit_notes — the memo now says the report was not summarized."""
    alvarado.credit_notes = None
    terms = DealTerms(name="José Alvarado", loan=1_000_000, rate=15,
                      salary=9_000_000)
    html = memo_service.render_html(terms, alvarado)
    assert "No bankruptcies, no judgments, no tax liens on file" not in html
    assert "not summarized at extraction" in html


def test_prompt_requires_the_payment_history_review():
    """The extraction prompt must instruct a per-tradeline payment-history
    review of the credit report (auto loans, mortgages, credit cards)."""
    from app.extraction import PROMPT
    p = PROMPT.lower()
    assert "credit_notes" in p
    assert "payment-history" in p or "payment history" in p
    assert "auto loan" in p and "mortgage" in p and "credit card" in p
    assert "paid as agreed" in p
    assert "return null" in p          # no report -> null, never "reviewed"


def test_prompt_carries_the_no_pfs_credit_report_fallback():
    """With no PFS uploaded, the debt schedule (Step 2b) must come from the
    credit report's open tradelines, dated by the report's pull date.

    Regression: the 2026-08-20 credit_notes rule routed all credit-report
    content to the Credit paragraph, and a no-PFS deal's Step 2b went empty —
    the model had been filling it from the credit report informally.
    """
    from app.extraction import PROMPT
    p = PROMPT
    assert "NO-PFS FALLBACK" in p
    assert "OPEN tradelines" in p
    assert "report/pull date as pfs_date" in p
    # revolving lines must not be given an amortizing payment
    assert "0 for revolving credit-card lines" in p


def test_compliance_flags_negative_cash_flow():
    # A big facility against a small salary drives net cash flow negative.
    comp = _compliance(Extraction(salary=1_000_000), 3_000_000, 1_000_000)
    assert _comp_row(comp, "Positive net cash flow after debt svc.")["status"] == "exc"
    assert any(e["label"] == "Positive net cash flow after debt svc."
               for e in comp["exceptions"])


def test_render_html_includes_compliance_coversheet(alvarado):
    terms = DealTerms(
        name="José Alvarado", team="Pelicans", league="NBA", sport="basketball",
        loan=ALVARADO_LOAN, rate=12, fee=2, salary=ALVARADO_SALARY,
        fund=date(2026, 1, 1), mat=date(2027, 1, 1),
    )
    html = memo_service.render_html(terms, alvarado, ["PFS.pdf"])
    assert "Deal Summary &amp; Policy Compliance" in html
    assert "Loan-to-Contract (LTC)" in html
    assert "Exceptions &amp; Mitigants" in html
    assert "Credit Approval — Exceptions Acknowledged" in html
    # Alvarado is clean under the current criteria (LTC 27.8% within 30%),
    # so the exceptions block reads "None".
    assert "all policy criteria are within guidelines" in html
    # The retired criteria (Lauren, 2026-08-25) must not print on the
    # checklist or anywhere else in the memo.
    for retired in RETIRED_CRITERIA:
        assert retired.replace("&", "&amp;") not in html, retired
    # The coversheet is page 1 and the memo body follows.
    assert html.index("Deal Summary &amp; Policy Compliance") < html.index("Credit Memorandum</h1>")


# --- Memo rendering smoke test --------------------------------------------

def test_render_html_contains_key_figures(alvarado):
    terms = DealTerms(
        name="José Alvarado", team="Pelicans", league="NBA", sport="basketball",
        loan=ALVARADO_LOAN, rate=12, fee=2, salary=ALVARADO_SALARY,
        fund=date(2026, 1, 1), mat=date(2027, 1, 1),
    )
    html = memo_service.render_html(terms, alvarado, ["PFS.pdf"])
    assert "José Alvarado" in html
    assert "Net Worth" in html
    assert "Proposed Facility" in html
    # the general-business-purposes verbiage must stay removed
    assert "general business purposes" not in html


# --- College-athlete mode (rule 20 — Lauren, 2026-09-14) --------------------
# terms.is_college swaps Section IV's FAQ and the "Professional" phrasing for
# college language. The pro contract-risk bullets ("no professional team has
# ever defaulted", the league backstop) are NOT true of a college payor —
# NIL collectives have missed payments and no governing body stands behind a
# school's revenue-share obligations — so college mode replaces them rather
# than restating them with "school" swapped in.

def _college_terms(**over):
    base = dict(name="Test Borrower", team="State University", league="NCAA",
                sport="basketball", loan=500_000, salary=2_000_000,
                is_college=True)
    base.update(over)
    return DealTerms(**base)


def test_memo_defaults_to_the_professional_faq():
    terms = DealTerms(name="Test Borrower", team="Test Team", league="NBA",
                      sport="basketball", loan=500_000, salary=2_000_000)
    html = memo_service.render_html(terms, Extraction(), [])
    assert "No professional team has ever defaulted" in html
    assert "The respective League can step in" in html
    assert "injured reserve" in html
    assert "Professional basketball player" in html
    assert "Team / Employer" in html
    assert "League contract receivable" in html
    # none of the college language leaks into a professional memo
    assert "House v. NCAA" not in html
    assert "loses eligibility" not in html
    assert "Collegiate" not in html


def test_college_mode_swaps_the_contract_risk_faq():
    html = memo_service.render_html(_college_terms(), Extraction(), [])
    # The pro claims are gone — neither is true of a college payor...
    assert "No professional team has ever defaulted" not in html
    assert "The respective League can step in" not in html
    assert "the Borrower's employer cannot fulfill" not in html
    # ...replaced by the honest college block, naming the payor:
    assert "the payor of the Borrower's compensation" in html
    assert "direct contractual obligation of State University" in html
    assert "House v. NCAA settlement (effective July 2025)" in html
    assert "no league-level backstop" in html
    assert "nonpayment disputes involving NIL collectives have occurred" in html


def test_college_mode_reads_college_throughout():
    html = memo_service.render_html(_college_terms(), Extraction(), [])
    # Sections I and V (fallback narrative) + Section XI's primary source
    assert "Collegiate basketball player" in html
    assert "Professional basketball player" not in html
    assert "Professional contract salary." not in html
    assert "revenue-sharing / NIL agreements" in html
    # page-2 header label — a school is never called an employer
    assert "School / Program" in html
    assert "Team / Employer" not in html
    # Section III collateral drops the word "League"
    assert "League contract receivable" not in html
    assert "Contract receivable — UCC-1 filing" in html
    # suspension and injury items swap; transfer/eligibility item is added
    assert "suspended by the program, the conference, or the NCAA" in html
    assert "injured reserve" not in html
    assert "transfers, is removed from the roster, or loses eligibility" in html
    assert "condition payment on continued enrollment" in html
    # the over-leverage FAQ is payor-agnostic and stays either way
    assert "What if the player over-leverages" in html


def test_college_injury_faq_still_follows_the_ddd_rule():
    # rule 19 is mode-independent: the assigned-policy bullet prints only when
    # the documents' disbursement carries the premium line.
    with_ddd = memo_service.render_html(
        _college_terms(), Extraction(uses_of_funds=_uof("DDD insurance")), [])
    without = memo_service.render_html(_college_terms(), Extraction(), [])
    assert "disability insurance policy naming only the Lender" in with_ddd
    assert "disability insurance policy" not in without


# --- Word export footer ----------------------------------------------------

def test_word_export_has_repeating_page_footer(alvarado):
    terms = DealTerms(
        name="José Alvarado", team="Pelicans", league="NBA", sport="basketball",
        loan=ALVARADO_LOAN, rate=12, fee=2, salary=ALVARADO_SALARY,
        fund=date(2026, 1, 1), mat=date(2027, 1, 1),
    )
    html = memo_service.render_html(terms, alvarado, ["PFS.pdf"])
    doc = memo_service.render_word(html).decode("utf-8")

    # Packaged as MHTML so the footer can live in its own part (never inline).
    assert doc.startswith("MIME-Version: 1.0")
    assert "multipart/related" in doc
    assert doc.count("Content-Location:") == 2          # main.htm + footer.htm
    assert "Content-Location: file:///C:/memo/footer.htm" in doc

    # The named section's bottom margin pulls in the separate footer part.
    assert "@page WordSection1" in doc
    assert 'mso-footer:url("footer.htm") f1' in doc
    assert "<div class=WordSection1>" in doc

    # The footer block (in its own part) carries live PAGE / NUMPAGES fields and
    # is NOT display:none (that dropped it entirely in some Word versions).
    assert "mso-element:footer" in doc
    assert "mso-element:footer;display:none" not in doc
    assert 'id=\'f1\'' in doc
    assert 'mso-field-code:" PAGE "' in doc
    assert 'mso-field-code:" NUMPAGES "' in doc
    assert "South River Capital — Credit Memorandum" in doc

    # the in-body (PDF/screen) footers are still suppressed so they don't show
    assert ".pg-footer{display:none !important;}" in doc
    # no stray UTF-8 replacement chars (em dash etc. encoded cleanly)
    assert "�" not in doc
