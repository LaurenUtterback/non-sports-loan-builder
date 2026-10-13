"""Pydantic models — the typed contract between the Vue frontend, the FastAPI
backend, and the Anthropic extraction step.

FastAPI uses these to validate requests and to auto-generate the OpenAPI schema
at /docs, which is the single source of truth your frontend can read.
"""

from __future__ import annotations

from typing import Optional
from datetime import date

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    label: str
    amount: float = 0.0


class DisbursementItem(BaseModel):
    """One line in the Section VI disbursement / Uses-of-Funds waterfall."""
    label: str
    amount: float = 0.0


class UsesOfFunds(BaseModel):
    """The disbursement waterfall as the deal documents present it (Section VI).

    ``gross_loan_amount`` is the full proposed facility. ``deductions`` are the
    fees and payoffs taken out of the gross loan to reach the amount "to be
    disbursed to Borrower" (origination/underwriting fees, payoffs of existing
    loans, legal/closing costs, etc.). ``additional_costs`` are amounts funded
    from the loan and carved out of the to-Borrower figure to reach the NET
    disbursed (e.g. Death & Disgrace insurance premium, Interest Reserve).

    All amounts are positive dollar magnitudes; the subtotals (to-Borrower, net)
    are always recomputed from these lines, never copied from the documents.
    """
    gross_loan_amount: float = 0.0
    deductions: list[DisbursementItem] = Field(default_factory=list)
    additional_costs: list[DisbursementItem] = Field(default_factory=list)


class FinancialYear(BaseModel):
    """One column of the Financial Analysis table.

    McCormick's runs FY2023 / FY2024 / FY2025 / YTD 2026 against Revenue,
    EBITDA and Net Income, with the proposed debt service and DSCR computed
    beneath. ``period`` is the column heading exactly as the documents label
    it, so a YTD or stub period is not silently presented as a full year.
    """
    period: str = ""                  # "FY2024", "YTD 2026", "TTM 6/30/26"
    revenue: Optional[float] = None
    ebitda: Optional[float] = None
    net_income: Optional[float] = None


class RiskItem(BaseModel):
    """One Risk Considerations & Mitigants pair.

    Both examples write these as risk-then-mitigant rather than as a FAQ, and
    an unmitigated risk is still worth stating - ``mitigant`` may be empty, and
    the memo then prints the risk with the mitigant line marked outstanding
    rather than dropping the risk.
    """
    risk: str = ""                    # short label: "Dated appraisal"
    detail: str = ""                  # what the exposure actually is
    mitigant: str = ""                # what answers it; "" = none yet


class CollateralCoverageRow(BaseModel):
    """One line of the Collateral Coverage table - a value and the LTV it
    implies. McCormick stresses the 2021 income approach at 7.0% and 7.5% cap
    rates alongside the appraisal and a live sale-leaseback offer, so the
    reader sees the LTV under each, not just the flattering one."""
    basis: str = ""                   # "2021 MAI appraisal (Valbridge)"
    value: float = 0.0


class RepaymentRow(BaseModel):
    """One scheduled payment in the loan's repayment/amortization schedule.

    Captured from the uploaded documents when present (see extraction.py); the
    memo's Section X reproduces these rows verbatim. ``total`` is that payment's
    interest + principal and may be left 0 to be computed at render time.
    """
    date: str = ""                    # payment date as shown, e.g. "15-Jul-26"
    interest: float = 0.0
    principal: float = 0.0
    total: float = 0.0


class DebtScheduleRow(BaseModel):
    """One financed debt from the PFS's detail schedules — Schedule D (personal
    residence / investment real estate & mortgage debt), Schedule F (notes
    payable to others) or Schedule G (contract-based notes payable).

    These rows are what makes the stale-PFS roll-forward possible (rule 15 in
    calculations.py): page 1 of the PFS only gives summary totals, while these
    schedules carry the per-loan payment and maturity date.

    ``category`` says which page-1 summary liability the row rolls up into, so a
    computed paydown can be applied to the right total:
    "mortgage_debt" | "notes_payable_others" | "notes_payable_contract".

    ``payment_period`` is captured verbatim because Schedule F/G is headed
    "Amount / Pay Period", which is NOT always monthly (an NFL contract note may
    pay per game check). Only monthly payments are rolled forward.
    """
    lender: str = ""
    category: str = ""
    balance: float = 0.0              # Present Loan Balance / Outstanding Amount
    payment: float = 0.0              # Monthly Payment / Amount per pay period
    payment_period: str = ""          # "monthly", "semi-monthly", "per game check", ...
    origination: str = ""             # Date of Origination / Purchase Year, as shown
    maturity: str = ""                # Loan Maturity Date, as shown
    rate_pct: float = 0.0             # interest rate if the schedule states one
    description: str = ""             # Reason for Debt / property address
    # How the memo treats this debt. Set from the UI, never by extraction:
    #   "roll" (default) — reduce by payment x months elapsed (rule 15)
    #   "hold"           — carry at the balance the statement reports
    #   "zero"           — show as repaid in full; the whole balance comes out
    #                      of the summary liability (a payoff at closing, or a
    #                      debt the underwriter knows is settled)
    #   "remove"         — take the debt off the memo ENTIRELY (Lauren,
    #                      2026-09-02 — the ✕ in Step 2b): the balance leaves
    #                      its summary liability like a zero-out, but NO line
    #                      prints anywhere — no detail row, no footnote
    #                      mention, no credit-paragraph sentence. Use "zero"
    #                      instead when the memo should say "repaid in full".
    treatment: str = "roll"


BORROWER_PROFILES = ("business", "investment", "consumer")

PROFILE_LABELS = {
    "business": "Business owner / executive",
    "investment": "Investment & real estate",
    "consumer": "General consumer / commercial",
}


class Extraction(BaseModel):
    """Structured data pulled from uploaded documents by Claude.

    Field names match the JSON the extraction prompt asks the model to return,
    so the prompt and this model must stay in sync (see extraction.py).
    """
    borrower_name: Optional[str] = None
    # The surname exactly as a government ID prints it ("Rivas Delgado"), kept
    # apart from the full name because the UCC-1's LAST NAME box must carry a
    # compound surname whole - guessing the split from the name is what makes
    # a filing defective. None when no ID was uploaded.
    borrower_surname: Optional[str] = None
    dob: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    # Who pays the borrower, and what they do there. On a business-owner deal
    # ``employer`` is the operating company the borrower owns; on an executive
    # or consumer deal it is their employer; on an investment deal it is
    # whatever entity holds the income-producing assets.
    employer: Optional[str] = None
    industry: Optional[str] = None
    occupation: Optional[str] = None
    ssn_masked: Optional[str] = None
    drivers_license: Optional[str] = None
    # The borrower's professional adviser on the deal - CPA, attorney, wealth
    # manager or business manager (the non-sports counterpart of the athlete
    # builder's sports agent).
    advisor: Optional[str] = None

    # The income spine. ``qualifying_income`` is the ANNUAL income underwriting
    # will stand behind - documented, recurring, and net of anything the
    # underwriter would not lend against. ``contracted_income`` is the portion
    # committed by contract or lease over the facility's own term, which many
    # non-sports deals simply do not have (0 then); it is the LTI basis when
    # present, exactly as the remaining contract value was for an athlete.
    qualifying_income: float = 0.0
    contracted_income: float = 0.0
    other_income: float = 0.0
    total_income: float = 0.0
    federal_taxes: float = 0.0        # captured but NOT used (taxes computed at 45%)
    mortgage_payments: float = 0.0
    hoa_payments: float = 0.0
    student_loans: float = 0.0
    interest_principal_loans: float = 0.0
    insurance: float = 0.0
    alimony: float = 0.0
    auto_payments: float = 0.0
    living_expenses: float = 0.0      # captured but NOT used (living computed at 10%)
    other_expenses: list[LineItem] = Field(default_factory=list)
    total_expenditures: float = 0.0

    assets: list[LineItem] = Field(default_factory=list)
    total_assets: float = 0.0
    liabilities: list[LineItem] = Field(default_factory=list)
    total_liabilities: float = 0.0
    net_worth: float = 0.0            # captured but NOT used (recomputed)

    # Pledged collateral, when the facility is secured by a specific asset
    # (investment property, a securities account, equipment, a business
    # interest). ``collateral_value`` drives the LTV test; both are blank on an
    # unsecured deal carried by the personal guarantee alone.
    collateral_value: float = 0.0
    collateral_desc: Optional[str] = None

    # The date the PFS was prepared ("Completed on:"), ISO yyyy-mm-dd. Drives the
    # stale-PFS roll-forward (rule 15) together with debt_schedule below.
    pfs_date: Optional[str] = None
    # Per-loan detail from PFS Schedules D / F / G. Empty when the documents
    # carry no schedules, in which case balances are used exactly as reported.
    debt_schedule: list[DebtScheduleRow] = Field(default_factory=list)
    facility_total_due: float = 0.0
    # Proposed-facility deal terms as stated in the documents (term sheet, etc.).
    # The frontend pre-fills the deal-terms form from these; the memo also falls
    # back to them when the corresponding form field is left blank.
    loan_amount: float = 0.0          # loan / proposed facility principal
    interest_rate_pct: float = 0.0    # annual interest rate, percent (e.g. 13.5)
    origination_fee_pct: float = 0.0  # origination / upfront fee, percent (e.g. 3)
    loan_term_months: int = 0         # term of the proposed facility, in whole months

    # The loan's repayment schedule as it appears in the uploaded documents.
    # Empty when the documents contain no schedule (Section X then computes one).
    repayment_schedule: list[RepaymentRow] = Field(default_factory=list)

    # The disbursement / Uses-of-Funds breakdown from the documents (Section VI).
    # None when the documents carry no breakdown (Section VI then falls back to a
    # gross-loan/origination-fee table built from the deal terms).
    uses_of_funds: Optional[UsesOfFunds] = None

    # --- the memo's own sections (see memo.html.j2) ------------------------
    # A one-line deal headline, the way both examples title the memo:
    # "$4M First Lien Loan - McCormick Paints, Frederick, MD".
    headline: Optional[str] = None
    # "Commercial Real Estate", "Operating business", "Residential investment".
    asset_class: Optional[str] = None
    # Why the borrower wants the money, in a few words.
    purpose: Optional[str] = None
    # The Executive Summary: what is being asked for, on what terms, what
    # secures it, and what the credit actually rests on.
    exec_summary: Optional[str] = None
    # Loan Summary rows that are not already deal terms.
    property_owner: Optional[str] = None     # grantor / titled owner, if not the borrower
    guarantors: Optional[str] = None         # "None personal", or who guarantees
    prepayment: Optional[str] = None
    key_conditions: Optional[str] = None
    # Collateral Analysis narrative, and the coverage table's bases.
    collateral_notes: Optional[str] = None
    collateral_coverage: list[CollateralCoverageRow] = Field(default_factory=list)
    # Financial Analysis: one entry per reporting period, oldest first.
    financials: list[FinancialYear] = Field(default_factory=list)
    financial_notes: Optional[str] = None
    # Risk Considerations & Mitigants.
    risks: list[RiskItem] = Field(default_factory=list)
    # Required Documents - what must be in hand before closing.
    required_documents: list[str] = Field(default_factory=list)
    # The three repayment sources, in the examples' own order.
    repay_primary: Optional[str] = None
    repay_secondary: Optional[str] = None
    repay_tertiary: Optional[str] = None

    credit_notes: Optional[str] = None
    # Section VII's narrative on how the income was documented and composed
    # (the athlete builder's contract_notes).
    income_notes: Optional[str] = None
    # Section V's narrative on the borrower, their business and their industry
    # (the athlete builder's sponsorship_narrative). Written from the UPLOADED
    # DOCUMENTS only - this builder does no public research, because there is no
    # non-sports equivalent of Spotrac and a business owner's name is not a
    # reliable public index.
    background_narrative: Optional[str] = None


class DealTerms(BaseModel):
    """The deal terms a user confirms before generating a memo."""
    name: str = ""
    dob: str = ""
    addr: str = ""
    phone: str = ""
    employer: str = ""
    industry: str = ""
    occupation: str = ""
    ssn: str = ""
    dl: str = ""
    advisor: str = ""

    loan: float = 0.0
    rate: float = 0.0                 # annual % (e.g. 12 for 12%)
    fee: float = 0.0
    income: float = 0.0               # annual qualifying income. Blank falls
                                      # back to Extraction.qualifying_income.
    contracted_income: float = 0.0    # income committed by contract or lease
                                      # over the facility term - the LTI basis
                                      # when present (rule 10). Blank falls back
                                      # to Extraction.contracted_income.
    collateral_value: float = 0.0     # value of the pledged collateral - the
                                      # LTV basis. 0 on an unsecured deal.
    collateral_desc: str = ""         # what is pledged, in the underwriter's
                                      # own words ("12 Harbor Road duplex").
    fund: Optional[date] = None       # funding date
    mat: Optional[date] = None        # maturity date
    loan_type: str = "New Loan"

    # --- the memo's framing (both examples lead with these) ----------------
    headline: str = ""                # blank -> composed from the terms
    asset_class: str = ""             # "Commercial Real Estate", ...
    purpose: str = ""                 # "Refinance / pay off existing bank loan"
    property_owner: str = ""          # titled owner, when not the borrower
    guarantors: str = ""              # blank -> "None personal" when unsecured
    prepayment: str = ""
    key_conditions: str = ""

    # How the facility pays. "io" = interest only with the principal due at
    # maturity (McCormick); "amortizing" = level payments retiring the
    # principal over the term (the Walgreens/Bouye template). This drives both
    # the Repayment Schedule and the Payments row of the Loan Summary.
    amortization: str = "io"

    # INVESTOR economics, which are NOT always what the borrower pays: on the
    # Walgreens deal the borrower paid 12% and 5 points while investors
    # received 10% and 1 point. Blank falls back to the borrower's rate/fee.
    investor_rate: float = 0.0
    investor_points: float = 0.0
    show_investor_returns: bool = True
    # Which of the three non-sports borrower profiles this deal is, which swaps
    # the memo's risk FAQ, repayment sources and purpose language (rule 20).
    # Set only by the underwriter, never by extraction.
    profile: str = "business"


class MemoRequest(BaseModel):
    terms: DealTerms
    extraction: Optional[Extraction] = None


class UploadedDoc(BaseModel):
    filename: str
    mime: str
    b64: str                          # base64-encoded file contents
