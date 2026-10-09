"""Pydantic models for the Deal Structuring tab.

The Structure builder answers a question the other tabs assume away: given how
THIS borrower actually gets paid, what repayment structure should the loan have?

The other builders take the presented structure as given (LoanDocTerms defaults
``amortization_type`` to "balloon" and calculations.calc_amort can only express
a single-payment balloon). This module carries the inputs needed to project
month-by-month cash flow and the candidate structures scored against it.

Nothing here renders a document. A candidate the underwriter SELECTS is
converted to loandocs ScheduleRow rows and pushed into the Loan Documents tab
through its existing ``repayment_schedule`` override.
"""

from __future__ import annotations

# Aliased: IncomeEvent and StructurePayment both have a field NAMED ``date``,
# which shadows the type when Pydantic resolves the annotation strings.
from datetime import date as _date
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- Pattern pay cadence ----------------------------------------------------

PayFrequency = Literal["weekly", "semimonthly", "monthly"]


class IncomeCadence(BaseModel):
    """When money reaches the borrower over a year.

    These are DEFAULTS keyed off the income pattern. Every field is editable
    per deal — how a borrower is actually paid is a question of fact (a
    quarterly draw, a seasonal trade, project billing) and the documents
    always win over the default.
    """

    pattern: str = ""
    label: str = ""                        # human description shown in the UI
    season_start_month: int = 1
    season_start_day: int = 1
    season_end_month: int = 12
    season_end_day: int = 31
    pay_frequency: PayFrequency = "monthly"
    # True when qualifying_income is paid across the whole year rather than in-season only.
    year_round: bool = False
    # Month/day a recurring annual receipt customarily lands (a K-1 true-up).
    bonus_month: Optional[int] = None
    bonus_day: Optional[int] = None
    notes: str = ""


class IncomeEvent(BaseModel):
    """A dated lump receipt outside the regular income cadence.

    Owner distributions, an annual bonus, a milestone or progress billing, a
    maturing note receivable, or the expected proceeds of a sale or refinance
    not yet closed (mark ``guaranteed`` False).
    """

    label: str = ""
    date: Optional[_date] = None
    amount: float = 0.0
    guaranteed: bool = True


# --- Inputs ----------------------------------------------------------------

class StructureInputs(BaseModel):
    """Everything the projection needs. Pre-filled from the credit memo where
    the extraction already carries it; the timing fields are entered here."""

    # Who / what (mirrors Extraction so the memo can pre-fill)
    borrower_name: str = ""
    pattern: str = ""
    employer: str = ""

    # Income — documented, recurring annual qualifying income only.
    qualifying_income: float = 0.0
    other_income: float = 0.0
    # Annual non-facility debt service from the memo's cash flow (mortgage,
    # autos, alimony, ...). Spread evenly across the year.
    other_debt_annual: float = 0.0

    # Certainty
    income_horizon: Optional[_date] = None
    income_contracted: bool = True
    # No playing contract at all (free agent / income not from a employer).
    # ENFORCED SERVER-SIDE in propose_structures — qualifying_income is zeroed and the
    # projection runs on other income and dated payments only, whatever the
    # form still carried (the same belt-and-suspenders as loandocs'
    # no_contracted_income dropping the Payment Direction Letter in render_html).
    no_contracted_income: bool = False
    # PENDING proceeds the borrower expects but has not yet received
    # (only read in no-employer-contract mode). It sizes the loan — the LTI policy
    # cap runs on this value, flagged for credit approval — and its expected
    # signing date becomes the exit event. It is NEVER projected as income:
    # nothing is contractually owed until the contract is signed.
    pending_proceeds_value: float = 0.0
    pending_proceeds_date: Optional[_date] = None

    # Timing
    cadence: Optional[IncomeCadence] = None      # None → pattern default
    income_events: list[IncomeEvent] = Field(default_factory=list)

    # Deal terms being tested
    loan_amount: float = 0.0
    interest_rate: float = 0.0                   # % p.a.
    origination_fee_pct: float = 0.0             # points
    funding_date: Optional[_date] = None
    target_term_months: int = 12
    # For event-driven deals: when the repayment event is expected to land.
    expected_exit_date: Optional[_date] = None
    expected_exit_label: str = ""                # e.g. "Contract signing"

    # Underwriting knobs (editable — see structure.py for the defaults' basis)
    fee_off_gross_pct: float = 0.0                       # agent commission off gross
    min_coverage: float = 1.25                   # required cushion on a payment

    # The structure as presented to us, for the side-by-side.
    presented_type: str = ""
    presented_term_months: int = 0


# --- Projection ------------------------------------------------------------

class CashFlowMonth(BaseModel):
    """One month of the borrower's projected cash flow."""

    year: int = 0
    month: int = 0
    label: str = ""                    # "Sep 2026"
    in_season: bool = False
    gross: float = 0.0                 # income + events + other income
    base_income: float = 0.0
    bonus: float = 0.0
    other: float = 0.0
    taxes: float = 0.0
    living: float = 0.0
    other_debt: float = 0.0
    available: float = 0.0             # available for the proposed facility
    cumulative: float = 0.0            # running surplus carried forward


# --- Candidates ------------------------------------------------------------

class StructurePayment(BaseModel):
    """One payment in a candidate structure."""

    date: str = ""                     # "15-Sep-26", matches ScheduleRow
    iso_date: Optional[_date] = None
    interest: float = 0.0
    principal: float = 0.0
    total: float = 0.0
    is_balloon: bool = False
    # Cash available in the month this payment falls in, and the resulting
    # coverage. ``cushion`` uses the cumulative surplus (the borrower banks
    # in-season money); ``coverage`` is the stricter same-month test.
    month_available: float = 0.0
    coverage: float = 0.0
    cushion_coverage: float = 0.0


class StructureCandidate(BaseModel):
    """A proposed structure, scored against the projection."""

    key: str = ""                      # stable id for selection
    name: str = ""
    # Maps onto LoanDocTerms.amortization_type when pushed. Structures with no
    # existing equivalent map to the closest type and carry explicit rows.
    amortization_type: str = "balloon"
    rationale: str = ""
    term_months: int = 0
    maturity_date: Optional[_date] = None
    payments: list[StructurePayment] = Field(default_factory=list)

    total_interest: float = 0.0
    total_principal: float = 0.0
    total_paid: float = 0.0
    points_amount: float = 0.0
    interest_reserve: float = 0.0      # funded from proceeds, when used

    # Scoring
    min_coverage: float = 0.0          # tightest same-month coverage
    min_cushion_coverage: float = 0.0  # tightest cumulative-surplus coverage
    tightest_month: str = ""
    matures_in_dry_month: bool = False
    passes: bool = False
    warnings: list[str] = Field(default_factory=list)
    recommended: bool = False


class StructureResult(BaseModel):
    inputs_echo: StructureInputs
    cadence_used: IncomeCadence
    cash_flow: list[CashFlowMonth] = Field(default_factory=list)
    candidates: list[StructureCandidate] = Field(default_factory=list)
    annual_gross: float = 0.0
    annual_available: float = 0.0
    notes: list[str] = Field(default_factory=list)


class StructureRequest(BaseModel):
    inputs: StructureInputs


class SelectRequest(BaseModel):
    """Convert one candidate into Loan Documents Exhibit A rows."""

    inputs: StructureInputs
    candidate_key: str = ""


# --- Proposed terms --------------------------------------------------------

class ProposedTerms(BaseModel):
    """What the tool would lend, rather than what it was told to test.

    Answers the two questions credit actually asks first: how much can this
    contract carry, and will they be able to pay it off?
    """

    loan_amount: float = 0.0
    interest_rate: float = 0.0
    origination_fee_pct: float = 0.0
    target_term_months: int = 0

    # The two independent ceilings, so the binding one is visible rather than
    # implied. policy_cap is South River's LTI limit; cash_capacity is what the
    # projected cash flow can actually service.
    policy_cap: float = 0.0
    cash_capacity: float = 0.0
    binding_constraint: str = ""        # "policy" | "cash flow" | "event"
    income_basis: float = 0.0

    # Plain-English verdict on repayment.
    can_repay: bool = False
    repayment_note: str = ""
    rate_basis: str = ""                # where the rate/points defaults come from
    warnings: list[str] = Field(default_factory=list)


class TermsRequest(BaseModel):
    inputs: StructureInputs
    # Total remaining contract value when known — the LTI basis (rule 10).
    contracted_income: Optional[float] = None
