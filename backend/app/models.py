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


class SalaryCheck(BaseModel):
    """Spotrac read of the guaranteed season salary and remaining contract
    value (shown in Step 2).

    Since 2026-08-25 (Lauren) Spotrac is the PRIMARY source for the memo's
    Guaranteed salary and Guaranteed remaining prefills; the documents are the
    backup when Spotrac produced no figure, and their own figures are kept
    here (``doc_salary`` / ``doc_remaining``) so Step 2 shows the executed
    contract as the cross-check. ``spotrac_salary`` is Spotrac's CAP HIT for
    the season being underwritten (base salary + prorated signing bonus +
    other counted bonuses; never the base salary alone), with the guarantee
    detail carried in ``note``. ``spotrac_remaining`` is the total remaining
    contract value from that season forward. ``salary_source`` /
    ``remaining_source`` record which source actually filled
    Extraction.salary / Extraction.contract_remaining ("spotrac" | "docs" —
    set by extraction.apply_spotrac_precedence; the Structure tab attaches
    this check for verification only and leaves both at "docs").
    ``verdict`` is computed server-side (extraction.build_salary_check),
    never by the model:
    "match" | "mismatch" | "docs_only" | "spotrac_only" | "unavailable".
    """
    spotrac_salary: float = 0.0
    spotrac_remaining: float = 0.0    # Spotrac's total remaining contract value
    doc_salary: float = 0.0           # the documents' season salary (backup)
    doc_remaining: float = 0.0        # the documents' remaining value (backup)
    salary_source: str = "docs"       # which source filled Extraction.salary
    remaining_source: str = "docs"    # which filled Extraction.contract_remaining
    season: Optional[str] = None      # the season the figure belongs to, e.g. "2026"
    spotrac_url: Optional[str] = None
    verdict: str = "unavailable"
    note: str = ""                    # one-line plain-text explanation for the underwriter


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
    team: Optional[str] = None
    league: Optional[str] = None
    sport: Optional[str] = None
    ssn_masked: Optional[str] = None
    drivers_license: Optional[str] = None
    agent: Optional[str] = None

    salary: float = 0.0               # GUARANTEED season compensation (base + this season's guaranteed bonus/installment)
    contract_remaining: float = 0.0   # total remaining contract value (all remaining seasons)
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

    credit_notes: Optional[str] = None
    contract_notes: Optional[str] = None
    sponsorship_narrative: Optional[str] = None

    # Spotrac cross-check of `salary`, attached after extraction (best-effort,
    # UI-only — see SalaryCheck). None only on extractions from before the check.
    salary_check: Optional[SalaryCheck] = None


class DealTerms(BaseModel):
    """The deal terms a user confirms before generating a memo."""
    name: str = ""
    dob: str = ""
    addr: str = ""
    phone: str = ""
    team: str = ""
    league: str = ""
    sport: str = ""
    ssn: str = ""
    dl: str = ""
    agent: str = ""

    loan: float = 0.0
    rate: float = 0.0                 # annual % (e.g. 12 for 12%)
    fee: float = 0.0
    salary: float = 0.0               # guaranteed season salary (base + guaranteed annual bonuses)
    contract_remaining: float = 0.0   # total remaining contract value — the LTC /
                                      # "Guaranteed Remaining" basis (rule 10).
                                      # Blank falls back to Extraction.contract_remaining.
    fund: Optional[date] = None       # funding date
    mat: Optional[date] = None        # maturity date
    loan_type: str = "New Loan"
    is_college: bool = False          # college athlete (revenue share / NIL):
                                      # Section IV's FAQ and the "Professional"
                                      # phrasing swap to college language.
                                      # Set only by the underwriter, never by
                                      # extraction.


class MemoRequest(BaseModel):
    terms: DealTerms
    extraction: Optional[Extraction] = None


class UploadedDoc(BaseModel):
    filename: str
    mime: str
    b64: str                          # base64-encoded file contents
