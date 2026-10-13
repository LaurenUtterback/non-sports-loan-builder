"""Document extraction for the Deal Structuring tab.

The credit memo's extraction already captures the CERTAINTY side of a deal
(guaranteed qualifying_income, contract end, guarantee language). What structuring needs on
top of that is TIMING — the pay election, the season window, and every dated
lump payment — plus whatever the documents say about how the loan is expected to
be repaid.

Reuses the same subscription usage-token auth as extraction.py /
pa_extraction.py / loandocs_extraction.py via the shared ``_ask_claude`` helper.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, field_validator

from .models import DebtScheduleRow, LineItem, UploadedDoc
from .loandocs_extraction import _ask_claude, EXTRACTION_MODEL
from .extraction import LEGAL_NAME_RULE


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_number(v):
    """"$1,650,000" / "15%" / "N/A" -> 1650000.0 / 15.0 / None.

    Claude is told to return plain numbers and mostly does, but it formats them
    often enough that a hard failure here would 500 the whole extraction.
    """
    if v is None or isinstance(v, (int, float)):
        return v
    m = _NUMBER_RE.search(str(v).replace(",", ""))
    return float(m.group()) if m else None


class _Tolerant(BaseModel):
    """Base model that survives what the extraction actually returns.

    The prompt instructs Claude to use null for anything the documents do not
    state — which includes the str and list fields. Declaring those as plain
    `str` / `list` means one unstated field raises ValidationError and the
    request 500s. This coerces null back to the field's own default, so a
    partially-stated document degrades to blanks instead of failing.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _null_to_default(cls, v, info):
        if v is not None:
            return v
        field = cls.model_fields.get(info.field_name)
        if field is None:
            return v
        default = field.get_default(call_default_factory=True)
        # Optional fields keep their None; str/list fields get "" / [].
        return default if default is not None else v


class ExtractedIncomeEvent(_Tolerant):
    label: str = ""
    date: Optional[str] = None          # ISO yyyy-mm-dd
    amount: Optional[float] = None
    guaranteed: bool = True

    @field_validator("amount", mode="before")
    @classmethod
    def _clean_amount(cls, v):
        return _to_number(v)


class PFSRead(_Tolerant):
    """The PFS captured the way the CREDIT MEMO's extraction captures it.

    Field names deliberately match models.Extraction, so the whole block can be
    handed to the memo's own machinery (`Extraction(**pfs.model_dump())` ->
    structure.debt_service_from_memo): build_cash_flow decides which rows count
    and calc_debt_rollforward ages a stale statement forward — the model never
    sums debt service itself when these rows exist.

    All expenditure amounts are ANNUAL, as the PFS's Annual Expenditures
    section states them.
    """

    pfs_date: Optional[str] = None            # ISO — drives the roll-forward
    mortgage_payments: float = 0.0
    hoa_payments: float = 0.0
    student_loans: float = 0.0
    interest_principal_loans: float = 0.0
    insurance: float = 0.0
    alimony: float = 0.0
    auto_payments: float = 0.0
    other_expenses: list[LineItem] = []
    debt_schedule: list[DebtScheduleRow] = []

    @field_validator("mortgage_payments", "hoa_payments", "student_loans",
                     "interest_principal_loans", "insurance", "alimony",
                     "auto_payments", mode="before")
    @classmethod
    def _clean_money(cls, v):
        return _to_number(v) or 0.0

    @field_validator("other_expenses", mode="before")
    @classmethod
    def _clean_lines(cls, v):
        """"[{label: null, amount: "$5,000"}]" -> valid LineItems. A null field
        inside a row would otherwise fail the whole upload (the same lesson as
        _Tolerant itself, one level down)."""
        if not isinstance(v, list):
            return []
        out = []
        for row in v:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            amount = _to_number(row.get("amount")) or 0.0
            if label or amount:
                out.append({"label": label, "amount": amount})
        return out

    @field_validator("debt_schedule", mode="before")
    @classmethod
    def _clean_rows(cls, v):
        """Drop nulls per row so DebtScheduleRow's own defaults apply, and parse
        formatted numbers — a "$350,000" balance must not 500 the upload."""
        if not isinstance(v, list):
            return []
        out = []
        for row in v:
            if not isinstance(row, dict):
                continue
            clean = {k: val for k, val in row.items() if val is not None}
            for money_key in ("balance", "payment", "rate_pct"):
                if money_key in clean:
                    clean[money_key] = _to_number(clean[money_key]) or 0.0
            out.append(clean)
        return out


class StructureExtraction(_Tolerant):
    """What Claude pulls from the deal documents for the structuring run."""

    # Who
    borrower_name: str = ""
    employer: str = ""
    pattern: str = ""

    # Certainty
    qualifying_income: Optional[float] = None            # GUARANTEED season compensation
    other_income: Optional[float] = None
    other_debt_annual: Optional[float] = None
    income_horizon: Optional[str] = None        # ISO
    income_contracted: Optional[bool] = None
    income_commitment_note: str = ""

    # Timing — the fields the memo extraction does not carry
    pay_frequency: Optional[str] = None       # weekly | semimonthly | monthly
    season_start: Optional[str] = None        # ISO, the season's first pay date
    season_end: Optional[str] = None          # ISO, the season's last pay date
    pay_election_note: str = ""
    income_events: list[ExtractedIncomeEvent] = []

    # Terms as presented
    loan_amount: Optional[float] = None
    interest_rate: Optional[float] = None
    origination_fee_pct: Optional[float] = None
    funding_date: Optional[str] = None        # ISO
    maturity_date: Optional[str] = None       # ISO
    presented_type: str = ""                  # e.g. "Full balloon", "Interest only"

    # Exit
    expected_exit_date: Optional[str] = None  # ISO
    expected_exit_label: str = ""

    notes: str = ""

    # The PFS as the memo reads it (see PFSRead). When present, the server
    # recomputes other_debt_annual from these rows through the memo's own
    # calculations — the model's summed figure above is only the fallback.
    pfs: Optional[PFSRead] = None
    debt_service_note: str = ""               # filled server-side, shown in the UI

    @field_validator("qualifying_income", "other_income", "other_debt_annual", "loan_amount",
                     "interest_rate", "origination_fee_pct", mode="before")
    @classmethod
    def _clean_numbers(cls, v):
        return _to_number(v)

    @field_validator("pay_frequency", mode="before")
    @classmethod
    def _normalize_frequency(cls, v):
        """Map what Claude writes onto the three cadences the engine supports.

        IncomeCadence.pay_frequency is a Literal, so an unrecognised value would
        fail the /propose call later — much further from the cause. Anything we
        can't place returns None, which falls back to the pattern default.
        """
        if not v:
            return None
        key = re.sub(r"[\s_-]", "", str(v)).lower()
        if key in ("weekly", "everyweek", "perweek", "weeklydraw"):
            return "weekly"
        if key in ("semimonthly", "twicemonthly", "twiceamonth", "bimonthly",
                   "biweekly", "everytwoweeks", "fortnightly", "24payments"):
            # Biweekly (26/yr) is not exactly semi-monthly (24/yr), but it is far
            # closer than weekly or monthly and keeps the projection sane.
            return "semimonthly"
        if key in ("monthly", "permonth", "everymonth", "12payments"):
            return "monthly"
        return None

    @field_validator("income_horizon", "funding_date", "maturity_date",
                     "expected_exit_date", mode="before")
    @classmethod
    def _only_real_dates(cls, v):
        """Keep a date only if it looks like one; drop anything else.

        These fields reach the UI's date pickers. A model that answers the
        income horizon with 12 (reading "horizon" as a number of months - the
        field was `contract_end` in the athlete builder, where the name itself
        said date) would otherwise fail validation and lose the ENTIRE
        extraction, including every figure it got right. Dropping one unusable
        value costs the user one field; refusing the reply costs them the
        upload.
        """
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return None
        text = str(v).strip()
        if not text or text.lower() in ("null", "none", "n/a", "na", "tbd"):
            return None
        return text if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None

    @field_validator("income_contracted", mode="before")
    @classmethod
    def _normalize_guaranteed(cls, v):
        if isinstance(v, str):
            key = v.strip().lower()
            if key in ("yes", "true", "y", "fully guaranteed", "guaranteed"):
                return True
            if key in ("no", "false", "n", "not guaranteed", "conditional", "partial"):
                return False
            return None
        return v


PROMPT = """You are reading loan documents for a NON-SPORTS lender. The
borrower is a business owner or executive, an investment / real-estate
borrower, or a general consumer or commercial borrower - there is no team,
league or playing contract in this deal. Your job is to pull the facts needed
to decide the loan's REPAYMENT STRUCTURE: when the borrower actually receives
cash, and how certain that cash is.

Return RAW JSON only — no prose, no markdown fence. Use this exact shape:

{
  "borrower_name": "", "employer": "", "pattern": "", "industry": "",
  "qualifying_income": null, "other_income": null, "other_debt_annual": null,
  "income_horizon": null, "income_contracted": null, "income_commitment_note": "",
  "pay_frequency": null, "season_start": null, "season_end": null,
  "pay_election_note": "",
  "income_events": [{"label": "", "date": null, "amount": null, "guaranteed": true}],
  "pfs": {"pfs_date": null, "mortgage_payments": 0, "hoa_payments": 0,
          "student_loans": 0, "interest_principal_loans": 0, "insurance": 0,
          "alimony": 0, "auto_payments": 0,
          "other_expenses": [{"label": "", "amount": 0}],
          "debt_schedule": [{"lender": "", "category": "", "balance": 0,
                             "payment": 0, "payment_period": "", "origination": "",
                             "maturity": "", "rate_pct": 0, "description": ""}]},
  "loan_amount": null, "interest_rate": null, "origination_fee_pct": null,
  "funding_date": null, "maturity_date": null, "presented_type": "",
  "expected_exit_date": null, "expected_exit_label": "",
  "notes": ""
}

RULES — these are load-bearing:

1. QUALIFYING_INCOME is the borrower's ANNUAL income that underwriting can
   stand behind: documented, recurring, and already received or contractually
   committed. Compose it from whichever the documents support - salary and
   recurring guaranteed bonus from a W-2, pay stub or employment agreement;
   owner income from K-1 ordinary income, guaranteed payments, S-corp wages
   and distributions or Schedule C net profit, taken from TAX RETURNS over any
   internal statement and multiplied by the borrower's ownership percentage;
   net rental income (gross rents LESS operating expenses, taxes, insurance
   and existing mortgage debt service on the same property); and recurring
   interest, dividends and required distributions. EXCLUDE capital gains and
   sale proceeds, loan proceeds, gifts, one-time items, and anything that
   appears only in a projection or pro forma. When income varies year to year,
   use the MOST RECENT COMPLETE YEAR as filed - never an average, never the
   best year - and say the prior year and the trend in notes.

2. OTHER_INCOME is income the borrower receives that you did NOT already count
   in qualifying_income - and nothing else. The projection adds the two
   together, so any component repeated in both inflates the borrower's gross
   income and overstates how much loan the cash flow can carry. If every
   source you found is already inside qualifying_income, return 0. Never
   restate the rental, business or salary figure here because it is "also"
   other income.

3. INCOME_CONTRACTED is TRUE only when a named payor is CONTRACTUALLY OBLIGED
   to pay the income through the facility's maturity - signed leases running
   past maturity, an employment agreement's guaranteed (non-discretionary)
   pay, a contracted management or service fee. It is FALSE for at-will
   employment, month-to-month tenancies, discretionary bonuses, and ordinary
   business revenue, however reliable the history looks. If the obligation is
   conditional, partial or terminable on notice, set it FALSE and quote the
   condition in income_commitment_note. This flips which structures make
   sense, so do not round it up to true.

4. INCOME_EVENTS are dated lump receipts that fall OUTSIDE the regular income
   cadence: quarterly or annual owner distributions, an annual bonus, a
   milestone or progress billing, a lease-up payment, a maturing note
   receivable, expected sale or refinance proceeds. Give each its real expected
   date from the documents. NEVER include an amount already counted in
   qualifying_income - if quarterly distributions make up the annual figure,
   either spread them through qualifying_income or list them here, not both.
   Mark guaranteed=false for anything contingent (a sale not yet under
   contract, a bonus not yet declared). EVERY event must be a receipt the
   borrower expects IN THE FUTURE, on a real expected date you can name. A
   historical figure from a filed tax return is NOT an income event - a
   capital gain realised last year, a bonus already paid, a sale already
   closed. Those have been received and spent; projecting them forward invents
   cash the borrower will never see again. If you cannot name the date a
   receipt is expected, leave the event out and say so in notes.

5. INCOME_HORIZON is a DATE, never a duration: the ISO yyyy-mm-dd date on
   which the borrower's documented income runs out - the earliest lease expiry
   or agreement end the facility depends on, not the latest. It is NOT a
   number of months and NOT the loan's term. Return null when the income has
   no documented end date.

6. TIMING: pay_frequency is "weekly", "semimonthly" or "monthly" - only if the
   documents actually state how the borrower is paid. season_start /
   season_end are the first and last months of the borrower's INCOME-ACTIVE
   WINDOW in ISO form, for a business that earns seasonally (construction,
   landscaping, tourism, agriculture) - the tax returns' quarterly pattern or
   the business's own statements are the evidence. Leave them null for income
   earned evenly through the year. If the documents are silent on cadence,
   return null and say so in pay_election_note - a pattern default is applied
   instead. Do NOT invent a cadence. "pattern" is the income pattern in one
   word where the documents support one: PAYROLL, DISTRIBUTIONS, RENTS,
   SEASONAL or PROJECT. "industry" is the borrower's industry in a few words.

7. PFS: when a Personal Financial Statement is among the documents, fill "pfs"
   with the SAME fields the credit-memo extraction captures (else "pfs": null):
   * pfs_date is the date the PFS was PREPARED (a "Completed on:" line, a date
     printed on page 1, or the signature date), ISO yyyy-mm-dd. Never the
     credit report date, the contract date, or today's date.
   * Capture EVERY line of the Annual Expenditures section, mapped by line:
     mortgage payments -> mortgage_payments, automobile payments ->
     auto_payments, insurance premiums -> insurance, alimony/child support ->
     alimony, student loans -> student_loans, interest & principal on loans ->
     interest_principal_loans, HOA dues -> hoa_payments, and every other
     expenditure line (exact label and amount) into other_expenses. Amounts are
     ANNUAL. Do NOT include income taxes or ordinary living expenses — those
     are computed.
   * debt_schedule is the PER-LOAN detail from the PFS's own detail schedules,
     which appear on a later page than the page-1 summary totals: Schedule D
     (real estate & mortgage debt), Schedule F (notes payable) and Schedule G
     (contract-based notes payable). One object per financed debt, omitting
     nothing. category is EXACTLY "mortgage_debt" (any Schedule D row),
     "notes_payable_others" (F) or "notes_payable_contract" (G). balance and
     payment are positive numbers (payment 0 when the row shows none —
     revolving lines such as credit cards typically show none).
     payment_period: "monthly" when the document indicates monthly, the stated
     period copied verbatim when it names one ("quarterly", "annual"),
     "" when the form shows an amount but no period — NEVER invent a period.
     Copy origination and maturity VERBATIM even when they look malformed; ""
     when blank. ROW ALIGNMENT: these are tables, and the text layer often
     reaches you with columns out of order — every figure you return for a
     debt must come from the SAME TABLE ROW as that debt's lender (anchor on
     the lender name and the outstanding amount, and read across). If you
     cannot confidently tell which row a payment or maturity belongs to,
     return 0 / "" rather than attaching it to the wrong debt.
   other_debt_annual is your OWN summed total of the borrower's annual
   non-facility debt service (mortgage, autos, alimony/child support, student
   loans, other notes, HOA) — it is used only as a FALLBACK when "pfs" carries
   no usable lines; the server recomputes debt service from "pfs" the way the
   credit memo does, stale-statement roll-forward included.

8. EXIT: fill expected_exit_date / expected_exit_label ONLY when the documents
   indicate repayment depends on a specific EVENT rather than on income — a
   property sale, a refinance commitment, a business sale or recapitalization,
   a contract award being signed, a note receivable maturing. expected_exit_label is a SHORT NOUN PHRASE
   naming the event, at most 8 words ("contract extension signing", "sale of
   the Miami property") — never a sentence and never your reasoning; that
   belongs in notes. Set the label even if no date is stated. If repayment
   simply comes from the borrower's income, leave both null.

9. presented_type is how the loan is structured AS PRESENTED, in the documents'
   own words ("Full balloon", "Interest only with balloon", "Fully amortized").

10. Dates are ISO yyyy-mm-dd. Amounts are plain numbers, no symbols or commas.
   Percentages are numbers (15 for 15%). Use null for anything the documents do
   not state — NEVER guess, and never carry a figure over from a different
   season or a different loan.

11. NOTES is read by a person in a single glance. Give at most 5 short items,
   semicolon-separated, covering only what would change the structuring
   decision or what you were unsure about — a conflict between documents, a
   figure you had to interpret, a repayment source that is not the income.
   Do not restate figures that are already in the fields above, and do not
   summarize the deal.

12. BORROWER_NAME. """ + LEGAL_NAME_RULE + """
"""


def _debt_service_from_pfs(ex: StructureExtraction) -> None:
    """Recompute other_debt_annual from the captured PFS the way the CREDIT
    MEMO reads it (Lauren, 2026-08-14) — through calculations, never a second
    ask to the model: build_cash_flow decides which rows count and
    calc_debt_rollforward ages a stale statement forward, so a debt repaid
    since the statement date stops being counted here too.

    Mutates ``ex``: ``other_debt_annual`` (only when the PFS produced usable
    lines — the model's own summed figure stands as the fallback otherwise)
    and ``debt_service_note`` so the adjustment is visible, never silent.
    Best-effort by contract: any failure keeps the fallback and never breaks
    the extract.
    """
    if ex.pfs is None:
        return
    try:
        from .models import Extraction
        from .structure import debt_service_from_memo

        as_of = None
        if ex.funding_date:
            try:
                as_of = date.fromisoformat(str(ex.funding_date))
            except ValueError:
                as_of = None

        memo_ed = Extraction(**ex.pfs.model_dump())
        ds = debt_service_from_memo(memo_ed, as_of)
        if not (ds["annual"] or ds["dropped"]):
            return                      # no usable lines — the fallback stands
        ex.other_debt_annual = ds["annual"]
        note = ("Other debt service was computed from the PFS's annual "
                "expenditures the way the credit memo reads it.")
        if ds["note"]:
            note += " " + ds["note"]
        ex.debt_service_note = note
    except Exception as exc:  # noqa: BLE001 - must never break the extract
        logging.getLogger(__name__).warning(
            "Structure PFS debt-service computation failed: %s", exc)


def extract_documents(docs: list[UploadedDoc]) -> StructureExtraction:
    """Pull the structuring fields from uploaded deal documents, then run the
    PFS through the memo's debt-service handling (best-effort).

    The athlete builder also cross-checked the season salary, team and league
    against Spotrac here. Nothing indexes a private borrower's income that
    way, so the documents are the only source and the underwriter is the only
    cross-check.
    """
    # 8000, not 4000: the structure reply nests the full captured PFS, so
    # the same document-heavy deal that truncated the memo extraction on
    # 2026-08-20 would truncate here too.
    ex = StructureExtraction(**_ask_claude(docs, PROMPT, max_tokens=8000))
    _debt_service_from_pfs(ex)
    return ex
