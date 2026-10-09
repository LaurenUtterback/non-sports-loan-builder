"""
Financial calculations for the credit memo.

This module is the authoritative home for every underwriting rule. The rules
were developed carefully and are locked in by tests/test_calculations.py using
the Alvarado reference deal. Do NOT change a rule here without updating the
tests to match the intended new behavior.

Underwriting rules encoded here
-------------------------------
1.  Taxes are ALWAYS 45% of gross income (salary + other income). Never pulled
    from documents.
2.  Ordinary living expenses are ALWAYS 10% of gross income.
3.  In the Guarantor Analysis cash flow, the Proposed Facility line is the loan
    PRINCIPAL only.
4.  On the Personal Financial Statement, the Proposed Facility is loan + interest
    (the full amount due). Interest comes from the amortization schedule, or
    falls back to a facility total stated in the documents.
5.  Net Worth = Total Assets - Total Liabilities. Always calculated, never copied
    from a stated figure in the documents.
6.  Total Liabilities includes the Proposed Facility (loan + interest).
7.  Alimony / child support is a cash-flow item ONLY (Available for Debt). It is
    never a PFS liability.
8.  Auto loan balances are never a separate PFS liability row (they live inside
    "Notes Payable to: others"). Monthly auto PAYMENTS still appear in the cash flow.
9.  Salary used everywhere is the GUARANTEED portion of compensation only:
    the guaranteed base salary PLUS any bonus that is guaranteed and paid
    every year of the contract (e.g. annual signing-bonus installments,
    guaranteed yearly roster bonuses). Non-guaranteed incentives, one-time
    bonuses, and endorsements stay excluded. When installments differ season
    to season, the amount used is the one SCHEDULED FOR THAT SPECIFIC
    current/upcoming season — never an average, never another season's.
    Reference example: $1,000,000 base + $9,000,000 guaranteed bonus
    scheduled for the season = $10,000,000 salary (never the base alone,
    never the $39,500,000 remaining contract value, and the bonus is not
    double-counted as other income). The total remaining contract value is
    captured separately (Extraction.contract_remaining), shown in Section
    VII, and serves as the guaranteed-earnings basis for rule 10.
10. LTC (Loan-to-Contract) = loan amount / guaranteed earnings, where
    guaranteed earnings is the TOTAL REMAINING contract value when the
    documents provide one (contract_remaining), else the guaranteed season
    salary. Section I's "advance against $X in guaranteed salary" states the
    same figure. The cash flow (rule 1) stays on the season salary.
11. Taxes are NEVER a PFS liability. Even when the Personal Financial Statement
    reports an estimated tax figure (e.g. "Taxes (Est of 35% of ...)"), it is
    excluded from Total Liabilities and from Net Worth.
12. Section VI (Uses of Funds) reproduces EVERY disbursement line provided in the
    documents (fees, payoffs, closing costs, insurance, interest reserve, ...).
    The "To be disbursed to Borrower" and "Net to be Disbursed to Borrower"
    subtotals are always recomputed from the lines, never copied. When the
    documents carry no breakdown, it falls back to gross loan less the
    origination fee from the deal terms.
13. The loan term in months (Section II Action Request) prefers the term stated
    in the documents (a term sheet's "Term: N months"); it falls back to the
    funding-to-maturity span used for the amortization schedule.
14. The memo phrases the borrower as "a Professional <sport> player", so the
    sport value is normalized to drop a leading "professional" — the memo must
    never render "Professional Professional ...".
15. STALE PFS ROLL-FORWARD ("Method A", Lauren's standing rule as of
    2026-08-05, applied to every memo). When the Personal Financial Statement
    is more than a month older than the memo date, each financed debt on its
    detail schedules is rolled forward as

        adjusted balance = reported balance - (monthly payment x months elapsed)

    where months elapsed counts whole payments from the month AFTER the
    statement date through the memo month (10/16/25 -> 7/28/26 = 9). The
    paydown is clamped so a balance never goes below zero and never runs past
    the loan's maturity date. Lines with no scheduled payment (credit cards and
    other revolving debt) and lines whose payment is NOT monthly (e.g. a
    contract note paid per game check) are left exactly as reported. Each
    schedule row's paydown is applied to the page-1 summary liability it rolls
    up into, so Total Liabilities and Net Worth reflect the adjusted figures.
    Known and accepted: the scheduled payment includes interest — and mortgage
    payments on the SureSports PFS form include taxes & insurance — so the
    method understates the true balance. That is deliberate.

    Whether a payment counts as monthly: Schedule D's column is literally
    "Monthly Payment", and Schedule F notes are monthly unless the form says
    otherwise; Schedule F/G's "Amount / Pay Period" column is frequently left
    blank. A CONTRACT-BASED note (Schedule G) is the exception — it is repaid
    out of game checks, so a blank period never counts as monthly there.

    Each debt carries a ``treatment`` the underwriter sets: "roll" (the default
    above), "hold" (carry exactly as the statement reports it), or "zero" (show
    it repaid in full — a payoff at closing, or a debt known to be settled).
    A ZERO-OUT does not depend on the statement being stale: the whole balance
    leaves its summary liability however fresh the PFS is. Debts may also be
    ADDED by hand when extraction misses one, or when the PFS carries no detail
    schedules at all; an added debt must name the summary liability it rolls
    into (``category``) or its paydown has no total to come out of, which is
    reported as a warning rather than applied somewhere arbitrary.
16. DDD INSURANCE FOLLOWS THE DISBURSEMENT (Lauren, 2026-08-26). The memo
    claims the DDD (death, disability & disgrace) policy ONLY when the
    documents' own disbursement breakdown carries the premium line (e.g.
    "DDD insurance ($17,000)"). When the disbursement does not reflect DDD
    insurance — or the documents carry no breakdown at all — the memo BODY's
    mentions drop out (both Tertiary repayment-source cards, the Section II
    insurance bullet, the Section III repayment cell, the injury FAQ's
    assigned-policy bullet) and the mitigants sentence loses its insurance
    clause. The Deal Summary & Policy Compliance coversheet KEEPS its
    insurance line either way (same-day refinement): the checklist row reads
    "If applicable" / "None in place" (status N/A; reworded from
    "Required" / "No Insurance" — Lauren, 2026-08-27) and the Collateral
    cell closes with "No Insurance" instead of "DDD policy".
    ``has_ddd_insurance`` decides from the Uses-of-Funds lines.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .models import DebtScheduleRow, Extraction, LineItem


# --- Underwriting constants ------------------------------------------------
# Rules 1 and 2. Named so structure.py can distribute the SAME rates across
# months without re-deriving them (its projection must tie out to this module's
# annual cash flow). Changing either changes the memo — see tests.

TAX_RATE = 0.45      # rule 1: taxes are always 45% of gross income
LIVING_RATE = 0.10   # rule 2: ordinary living expenses are always 10% of gross


# --- Row classifiers (mirror the JS regexes exactly) ----------------------

_FACILITY_RE = re.compile(r"proposed\s*facility", re.I)
_AUTO_RE_A = re.compile(r"\b(auto(mobile)?|vehicle|car)\b.*\b(loan|note|debt|financ)", re.I)
_AUTO_RE_B = re.compile(r"\b(auto|car)\s*loans?\b", re.I)
_ALIMONY_RE = re.compile(r"alimony|child\s*support", re.I)
_COMPUTED_RE_TAX = re.compile(r"\b(income\s*tax|federal.*tax|taxes)\b", re.I)
_COMPUTED_RE_LIVING = re.compile(r"ordinary\s*living|living\s*expense", re.I)


def _label(item) -> str:
    if item is None:
        return ""
    if isinstance(item, dict):
        return item.get("label") or ""
    return getattr(item, "label", "") or ""


def _amount(item) -> float:
    if item is None:
        return 0.0
    if isinstance(item, dict):
        return item.get("amount") or 0.0
    return getattr(item, "amount", 0.0) or 0.0


def is_facility_row(item) -> bool:
    return bool(_FACILITY_RE.search(_label(item)))


def is_auto_loan_row(item) -> bool:
    lbl = _label(item)
    return bool(_AUTO_RE_A.search(lbl) or _AUTO_RE_B.search(lbl))


def is_alimony_row(item) -> bool:
    return bool(_ALIMONY_RE.search(_label(item)))


def is_computed_row(item) -> bool:
    lbl = _label(item)
    return bool(_COMPUTED_RE_TAX.search(lbl) or _COMPUTED_RE_LIVING.search(lbl))


def is_tax_row(item) -> bool:
    return bool(_COMPUTED_RE_TAX.search(_label(item)))


def _sum(items) -> float:
    return sum(_amount(i) for i in (items or []))


# --- Amortization ----------------------------------------------------------

def calc_amort(principal: float, rate: float, fund_date: date, mat_date: date) -> dict:
    """Single-payment balloon facility. Interest accrues actual/365.

    Returns interest, balloon (principal + interest), month count, and a row
    schedule for display.
    """
    months = (mat_date.year - fund_date.year) * 12 + (mat_date.month - fund_date.month)
    days = (mat_date - fund_date).days
    interest = round(principal * (rate / 100 / 365) * days)
    balloon = principal + interest

    rows = [{
        "num": "",
        "date": f"Funding — {fund_date.strftime('%b %Y')}",
        "principal": None, "interest": None, "payment": None,
        "balance": principal, "is_fund": True,
    }]
    for i in range(1, months + 1):
        # advance i months from funding
        y = fund_date.year + (fund_date.month - 1 + i) // 12
        m = (fund_date.month - 1 + i) % 12 + 1
        d = date(y, m, min(fund_date.day, 28))
        is_balloon = i == months
        rows.append({
            "num": i,
            # integer fields, not the glibc-only "%-m/%-d" codes (ValueError on Windows)
            "date": f"{d.month}/{d.day}/{d.year}",
            "principal": principal if is_balloon else 0,
            "interest": interest if is_balloon else 0,
            "payment": balloon if is_balloon else 0,
            "balance": 0 if is_balloon else principal,
            "is_balloon": is_balloon,
        })
    return {"rows": rows, "interest": interest, "balloon": balloon, "months": months}


# --- Repayment schedule (display) -----------------------------------------

def calc_repayment_schedule(principal: float, rate: float,
                            fund_date: date, mat_date: date) -> dict:
    """Fallback repayment schedule for Section X when the documents don't carry one.

    Mirrors how South River's facilities actually repay: interest is paid every
    month (equal installments of principal * rate / 12) and the principal is
    repaid as a single balloon on the final payment. This is presentation only —
    it does NOT change ``calc_amort`` (the actual/365 interest used for the
    facility total on the PFS).

    Returns one row per monthly payment ({num, date, interest, principal, total,
    is_balloon}) plus column totals. Totals are the sum of the displayed rows so
    the table always foots.
    """
    months = (mat_date.year - fund_date.year) * 12 + (mat_date.month - fund_date.month)
    months = max(months, 1)
    monthly_interest = round((principal or 0) * (rate / 100) / 12)

    rows = []
    for i in range(1, months + 1):
        y = fund_date.year + (fund_date.month - 1 + i) // 12
        m = (fund_date.month - 1 + i) % 12 + 1
        d = date(y, m, min(fund_date.day, 28))
        is_balloon = i == months
        prin = principal if is_balloon else 0
        rows.append({
            "num": i,
            # integer day + %b/%y, never the glibc-only "%-d" (ValueError on Windows)
            "date": f"{d.day}-{d.strftime('%b')}-{d.strftime('%y')}",
            "interest": monthly_interest,
            "principal": prin,
            "total": monthly_interest + prin,
            "is_balloon": is_balloon,
        })

    total_interest = sum(r["interest"] for r in rows)
    total_principal = sum(r["principal"] for r in rows)
    return {
        "rows": rows,
        "total_interest": total_interest,
        "total_principal": total_principal,
        "total_payment": total_interest + total_principal,
        "months": months,
    }


# --- Facility total --------------------------------------------------------

def facility_total(ed: Optional[Extraction], amort: Optional[dict], loan: float) -> float:
    """Facility amount due = loan + interest.

    Prefer interest computed from the form's rate/dates. If those aren't set,
    fall back to a facility total stated in the uploaded documents.
    """
    if amort and amort.get("interest", 0) > 0:
        return (loan or 0) + amort["interest"]
    if ed and (ed.facility_total_due or 0) > 0:
        return ed.facility_total_due
    return loan or 0


def loan_term_months(ed: Optional[Extraction], amort: Optional[dict]) -> int:
    """Number of months the lender provides the loan, for the Section II
    Action Request.

    Prefers the term stated in the deal documents (a term sheet's
    "Term: N months"); falls back to the funding-to-maturity span computed for
    the amortization schedule. Returns 0 when neither is available.
    """
    if ed and (ed.loan_term_months or 0) > 0:
        return int(ed.loan_term_months)
    if amort and (amort.get("months") or 0) > 0:
        return int(amort["months"])
    return 0


# --- Rule 15: stale-PFS roll-forward --------------------------------------

# A PFS is "stale" once it is more than a month old at the memo date. Below this
# it is treated as current and every balance is used exactly as reported.
_STALE_AFTER_DAYS = 31

# Dates on these forms are typed by hand into Excel and arrive dirty: a missing
# separator ("08/072053"), or Excel's epoch showing through for an empty cell
# ("7/17/1905", "1/0/1900"). Anything resolving before this year is a placeholder.
_MIN_PLAUSIBLE_YEAR = 1950

_MONTHLY_RE = re.compile(r"\bmonth", re.I)

# Which page-1 summary liability a schedule row rolls up into. Checked in this
# order: "Notes Payable: Contract Based" must not be swallowed by the plainer
# "Notes Payable to: others" pattern.
_CATEGORY_MATCHERS = (
    ("notes_payable_contract", re.compile(r"contract", re.I)),
    ("mortgage_debt", re.compile(r"mortgage", re.I)),
    ("notes_payable_others", re.compile(r"notes?\s*payable", re.I)),
)

# How each category is named to a reader, and which PFS detail schedule it comes
# from. Used by the memo's Section IX debt table and by the paydown warnings, so
# neither shows a raw field name like "notes_payable_others".
CATEGORY_LABELS = {
    "mortgage_debt": ("D", "Mortgage Debt"),
    "notes_payable_others": ("F", "Notes Payable to: others"),
    "notes_payable_contract": ("G", "Notes Payable: Contract Based"),
}


def category_label(cat: str) -> str:
    """Reader-facing name for a summary-liability category ("" when unset)."""
    return CATEGORY_LABELS.get(cat or "", ("", ""))[1]


def category_schedule(cat: str) -> str:
    """The PFS detail schedule letter a category comes from ("" when unset)."""
    return CATEGORY_LABELS.get(cat or "", ("", ""))[0]


def summary_category(label: str) -> str:
    """Which summary-liability category a page-1 liability label belongs to.

    Public wrapper so the memo can list each scheduled debt under the summary
    line it rolls into, grouping them exactly the way the paydown is applied.
    """
    return _summary_category(label)


def _parse_loose_date(raw: str | None) -> Optional[date]:
    """Parse a PFS schedule date, tolerating the forms these sheets produce.

    Returns None for a blank, unparseable, or placeholder date rather than
    guessing — a wrong maturity would silently mis-cap a roll-forward.
    """
    s = (raw or "").strip()
    if not s:
        return None

    y = m = d = None
    if (mt := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)):
        y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
    elif (mt := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)):
        m, d, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
    elif (mt := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2})", s)):
        m, d, y = int(mt.group(1)), int(mt.group(2)), 2000 + int(mt.group(3))
    elif (mt := re.fullmatch(r"(\d{1,2})/(\d{2})(\d{4})", s)):
        # Missing separator, e.g. "08/072053" -> 08/07/2053.
        m, d, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
    elif (mt := re.fullmatch(r"(19|20)\d{2}", s)):
        # A bare Purchase Year (Schedule D) — treat as the start of that year.
        y, m, d = int(s), 1, 1
    else:
        return None

    if y < _MIN_PLAUSIBLE_YEAR:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _months_elapsed(start: date, end: date) -> int:
    """Whole scheduled payments from the month AFTER `start` through `end`'s
    month — Lauren's count (10/16/25 -> 7/28/26 = 9)."""
    return max(0, (end.year - start.year) * 12 + (end.month - start.month))


def _money0(n: float) -> str:
    return f"${n:,.0f}"


def _sentence(text: str) -> str:
    """Capitalize a sentence built from a lender name or an article.

    Only the first character — .capitalize() would lower-case the rest and
    wreck names like "MidState Bank" and "Sports Finance Fund, LP".
    """
    return text[:1].upper() + text[1:] if text else text


def _long_date(d: date) -> str:
    # Integer fields, not "%-d" — that strftime code raises on Windows.
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _is_monthly(r: DebtScheduleRow) -> bool:
    """Whether a schedule row's payment is a monthly one.

    Schedule F/G is headed "Amount / Pay Period" and these forms routinely leave
    the period blank, so the default matters. Mortgages (Schedule D, column
    "Monthly Payment") and ordinary notes (Schedule F) are monthly unless the
    document says otherwise. CONTRACT-BASED notes (Schedule G) are not: they are
    repaid out of the athlete's game checks, so an unstated period never counts
    as monthly — which is how Lauren underwrote a real contract note by
    hand (rolled the mortgages and the auto note, held the contract note).
    """
    period = (r.payment_period or "").strip()
    if _MONTHLY_RE.search(period):
        return True
    if period:
        return False
    return r.category != "notes_payable_contract"


def _summary_category(label: str) -> str:
    for cat, rx in _CATEGORY_MATCHERS:
        if rx.search(label or ""):
            return cat
    return ""


def calc_debt_rollforward(ed: Optional[Extraction], as_of: Optional[date]) -> dict:
    """Rule 15 — bring each financed debt on the PFS schedules to the memo date.

    Two things move a balance, and they are independent:

    * the ROLL-FORWARD proper (``treatment="roll"``), which needs a stale PFS —
      the balance drops by payment x months elapsed; and
    * a ZERO-OUT (``treatment="zero"``), which does not. A debt being paid off
      at closing, or one the underwriter knows is settled, comes out in full
      however fresh the statement is.

    Returns the per-row detail (so the memo and the UI can show reported vs
    adjusted), the paydown to apply to each page-1 summary liability, and a
    drafted sentence for the Credit paragraph. ``applied`` is False when nothing
    moved, in which case every balance stands exactly as reported.
    """
    if not ed or not as_of or not ed.debt_schedule:
        return {
            "applied": False, "pfs_date": None, "as_of": as_of, "months": 0,
            "rows": [], "paydown_by_category": {}, "total_paydown": 0.0,
            "note": "", "warnings": [],
        }

    pfs_date = _parse_loose_date(ed.pfs_date)
    stale = bool(pfs_date and (as_of - pfs_date).days > _STALE_AFTER_DAYS)
    months = _months_elapsed(pfs_date, as_of) if stale else 0

    rows: list[dict] = []
    paydown_by_category: dict[str, float] = {}
    # Balances REMOVED from the memo (treatment "remove" — the ✕ in Step 2b).
    # A separate channel from the paydown: it reduces the summary liability
    # the same way, but the memo's visible disclosure (footnote, credit
    # paragraph, detail rows) never counts or mentions it (Lauren, 2026-09-02).
    removed_by_category: dict[str, float] = {}
    warnings: list[str] = []

    def _take(cat: str, amount: float) -> None:
        paydown_by_category[cat] = paydown_by_category.get(cat, 0.0) + amount

    for r in ed.debt_schedule:
        row = {
            "lender": r.lender or r.description or "Unnamed debt",
            "category": r.category or "",
            "reported": r.balance or 0.0,
            "payment": r.payment or 0.0,
            "maturity": r.maturity or "",
            "maturity_date": _parse_loose_date(r.maturity),
            # "Reason for Debt" / property address — shown under the lender in
            # the memo's Section IX debt table.
            "description": r.description or "",
            "treatment": r.treatment or "roll",
            "months_applied": 0,
            "paydown": 0.0,
            "adjusted": r.balance or 0.0,
            "rolled": False,
            "zeroed": False,
            "removed": False,
            "reason": "",
        }

        if not row["reported"]:
            row["reason"] = "no balance reported"
        elif row["treatment"] == "remove":
            # Off the memo entirely: the balance leaves the summary liability,
            # and nothing prints — paydown stays 0 so the visible disclosure
            # never counts it.
            row.update(adjusted=0.0, removed=True,
                       reason="removed from the memo")
            cat = r.category or ""
            removed_by_category[cat] = (removed_by_category.get(cat, 0.0)
                                        + row["reported"])
        elif row["treatment"] == "zero":
            # Repaid in full: the whole balance leaves the summary liability.
            row.update(paydown=row["reported"], adjusted=0.0,
                       rolled=True, zeroed=True)
            _take(r.category or "", row["reported"])
        elif row["treatment"] == "hold":
            row["reason"] = "held at the reported balance"
        elif row["payment"] <= 0:
            row["reason"] = "no scheduled payment (revolving)"
        elif not _is_monthly(r):
            per = (r.payment_period or "").strip()
            row["reason"] = (f"payment is not monthly ({per})" if per
                             else "contract-based note, not on a monthly schedule")
        elif not pfs_date:
            row["reason"] = "the statement is undated"
        elif not stale:
            row["reason"] = "the statement is current"
        else:
            n = months
            mat = row["maturity_date"]
            if mat:
                n = min(n, _months_elapsed(pfs_date, mat))
            elif (r.maturity or "").strip():
                warnings.append(
                    f"{row['lender']}: maturity date \"{r.maturity}\" could not be "
                    f"read, so the roll-forward was not capped at maturity.")
            paydown = min(row["payment"] * n, row["reported"])
            if paydown > 0:
                row.update(months_applied=n, paydown=paydown,
                           adjusted=row["reported"] - paydown, rolled=True)
                _take(r.category or "", paydown)
            else:
                row["reason"] = "already past maturity at the statement date"

        rows.append(row)

    moved = [r for r in rows if r["paydown"] > 0]
    if not moved:
        if not pfs_date and any(r["reason"] == "the statement is undated" for r in rows):
            warnings.append("The PFS carries no readable statement date, so balances "
                            "are shown exactly as reported. Enter the statement date "
                            "to roll them forward.")
        return {
            "applied": False, "pfs_date": pfs_date, "as_of": as_of, "months": months,
            "rows": rows, "paydown_by_category": {}, "total_paydown": 0.0,
            "removed_by_category": removed_by_category,
            "note": "", "warnings": warnings,
        }

    rolled = [r for r in moved if not r["zeroed"]]
    zeroed = [r for r in moved if r["zeroed"]]
    held = [r for r in rows if r["paydown"] <= 0 and r["reported"]
            and not r["removed"] and r["reason"] != "no balance reported"]

    sentences = []
    if pfs_date:
        sentences.append(f"The Personal Financial Statement is dated "
                         f"{_long_date(pfs_date)}.")
    if rolled:
        parts = []
        for r in rolled:
            piece = (f"the {r['lender']} balance at {_money0(r['payment'])} per month "
                     f"reduces from {_money0(r['reported'])} to {_money0(r['adjusted'])}")
            if r["maturity_date"]:
                piece += f" (matures {_long_date(r['maturity_date'])})"
            parts.append(piece)
        sentences.append(
            f"Assuming all payments were made as agreed, the scheduled debt has "
            f"been rolled forward {months} month{'s' if months != 1 else ''} to "
            f"{_long_date(as_of)}: " + "; ".join(parts) + ".")
        sentences.append(
            f"The roll-forward totals "
            f"{_money0(sum(r['paydown'] for r in rolled))} of principal reduction.")
    if zeroed:
        sentences.append(_sentence(
            ", ".join(f"the {r['lender']} balance of {_money0(r['reported'])}"
                      for r in zeroed) +
            (" is" if len(zeroed) == 1 else " are") +
            " shown as repaid in full and carried at $0."))
    if held:
        sentences.append(_sentence(
            ", ".join(r["lender"] for r in held) +
            (" is" if len(held) == 1 else " are") +
            " carried at the balance reported on the statement (" +
            "; ".join(f"{r['lender']}: {r['reason']}" for r in held) + ")."))

    return {
        "applied": True,
        "pfs_date": pfs_date,
        "as_of": as_of,
        "months": months,
        "rows": rows,
        "paydown_by_category": paydown_by_category,
        "total_paydown": sum(r["paydown"] for r in moved),
        "removed_by_category": removed_by_category,
        "note": " ".join(sentences),
        "warnings": warnings,
    }


def _apply_rollforward(liab_items: list[LineItem], rf: dict) -> list[LineItem]:
    """Reduce each page-1 summary liability by the paydown computed for the
    schedule rows that roll up into it. Returns NEW LineItems — the extraction's
    own rows are never mutated, so the reported figures stay recoverable."""
    remaining = dict(rf.get("paydown_by_category") or {})
    # Removed balances (treatment "remove") reduce the lines the same way the
    # visible paydown does — they are only excluded from the memo's DISCLOSURE.
    for _cat, _amt in (rf.get("removed_by_category") or {}).items():
        remaining[_cat] = remaining.get(_cat, 0.0) + _amt
    out: list[LineItem] = []
    for item in liab_items:
        cat = _summary_category(item.label)
        cut = min(remaining.get(cat, 0.0), item.amount or 0.0)
        if cut > 0:
            remaining[cat] -= cut
            out.append(LineItem(label=item.label, amount=item.amount - cut))
        else:
            out.append(LineItem(label=item.label, amount=item.amount))
    # What could NOT be applied, per category — either the row has no "Rolls
    # into" category, or the statement carries no summary line to take it out
    # of. Recorded on rf so the memo's debt table can say so on the row itself
    # instead of quietly showing an adjusted balance no total reflects.
    unapplied = {cat: left for cat, left in remaining.items() if left > 0.5}
    rf["unapplied_by_category"] = unapplied
    for cat, left in unapplied.items():
        where = (f'"{category_label(cat)}"' if category_label(cat)
                 else 'a debt with no "Rolls into" category set')
        rf.setdefault("warnings", []).append(
            f"{_money0(left)} of computed paydown for {where} had no matching "
            f"liability line on the statement and was not applied.")
    return out


# --- Balance sheet (PFS) ---------------------------------------------------

# A PFS asset row carrying the contract's remaining value. Matched so a
# remaining-contract value confirmed at underwriting can restate it (below).
_CONTRACT_ASSET_RE = re.compile(r"contract", re.I)


def mark_contract_asset(assets, contract_remaining: float):
    """Restate the PFS "Contract (Remaining)" asset to the confirmed value.

    The PFS reports the contract asset as of its own date. When the underwriter
    confirms a DIFFERENT remaining contract value on the deal-terms form (a new
    contract, or a stale statement), Section IX would otherwise report an asset
    that contradicts Section VII's remaining-contract figure.

    Only an explicit override marks the asset: the form is pre-filled from the
    documents, so on an ordinary deal the two agree and nothing moves. Returns
    ``(assets, mark)``; ``mark`` is None when nothing changed, else the reported
    and restated figures for the Section IX footnote.
    """
    if not assets or not contract_remaining:
        return assets, None
    out, mark = [], None
    for a in assets:
        if mark is None and _CONTRACT_ASSET_RE.search(a.label or "")                 and a.amount and a.amount != contract_remaining:
            mark = {"label": a.label, "reported": a.amount,
                    "restated": contract_remaining}
            out.append(LineItem(label=a.label, amount=contract_remaining))
        else:
            out.append(a)
    return out, mark


def calc_balance_sheet(ed: Optional[Extraction], facility_due: float,
                       as_of: Optional[date] = None,
                       contract_remaining: float = 0) -> dict:
    """Net Worth = Total Assets - Total Liabilities, where liabilities include
    the proposed facility at loan + interest.

    Excludes from liabilities: the facility itself (added once below), auto-loan
    rows (folded into Notes Payable to: others), alimony/child support
    (a cash-flow item only), and tax rows (never a PFS liability, even when the
    Personal Financial Statement reports an estimated tax figure).

    ``as_of`` is the memo date. When given, and the PFS is more than a month
    older than it, the scheduled debts are rolled forward first (rule 15) so
    Total Liabilities and Net Worth run on the adjusted balances.
    """
    assets, contract_mark = mark_contract_asset(
        (ed.assets if ed else None), contract_remaining)
    assets_total = _sum(assets) or (ed.total_assets if ed else 0) or 0

    liab_items = [
        l for l in (ed.liabilities if ed else [])
        if not is_facility_row(l) and not is_auto_loan_row(l)
        and not is_alimony_row(l) and not is_tax_row(l)
    ]
    reported_liab = _sum(liab_items) or (ed.total_liabilities if ed else 0) or 0

    rf = calc_debt_rollforward(ed, as_of)
    # A removal must land even when nothing rolled (a current statement) —
    # it is not a roll-forward, just a balance taken off the memo.
    if rf["applied"] or rf.get("removed_by_category"):
        liab_items = _apply_rollforward(liab_items, rf)

    # Fall back to the stated total only when there are no line items at all —
    # a rolled-forward set of lines must never be overridden by the PFS total.
    stated_liab = _sum(liab_items) or (ed.total_liabilities if ed else 0) or 0
    total_liab = stated_liab + (facility_due or 0)
    return {
        "assets_total": assets_total,
        "assets_items": assets or [],
        "contract_mark": contract_mark,
        "stated_liab": stated_liab,
        "reported_liab": reported_liab,
        "total_liab": total_liab,
        "net_worth": assets_total - total_liab,
        "liab_items": liab_items,
        "rollforward": rf,
    }


# --- Uses of Funds (disbursement waterfall) -------------------------------

def calc_uses_of_funds(uof, loan: float, fee_pct: float) -> dict:
    """Build the Section VI disbursement waterfall.

    Prefers the disbursement breakdown captured from the uploaded documents so
    EVERY line provided (origination/underwriting fees, payoffs, closing costs,
    insurance, interest reserve, ...) appears on the memo. The two subtotals are
    ALWAYS recomputed from the line items, never copied from the documents
    (consistent with rule 5 — totals are calculated):

        to_borrower     = gross loan − Σ deductions
        net_to_borrower = to_borrower − Σ additional_costs

    Falls back to a gross-loan / origination-fee table built from the deal terms
    when the documents carry no breakdown, so Section VI is never empty.

    ``uof`` is a UsesOfFunds (or None). All input amounts are positive
    magnitudes; zero-amount lines are dropped.
    """
    if uof and (uof.gross_loan_amount or uof.deductions or uof.additional_costs):
        gross = uof.gross_loan_amount or (loan or 0)
        deductions = [{"label": _label(d), "amount": _amount(d)}
                      for d in uof.deductions if _amount(d)]
        additional = [{"label": _label(a), "amount": _amount(a)}
                      for a in uof.additional_costs if _amount(a)]
    else:
        gross = loan or 0
        fee_amt = round(gross * (fee_pct or 0) / 100)
        deductions = [{"label": f"Origination Fee ({fee_pct:g}%)", "amount": fee_amt}] if fee_amt else []
        additional = []

    to_borrower = gross - sum(d["amount"] for d in deductions)
    net_to_borrower = to_borrower - sum(a["amount"] for a in additional)
    return {
        "gross": gross,
        "deductions": deductions,
        "to_borrower": to_borrower,
        "additional_costs": additional,
        "net_to_borrower": net_to_borrower,
    }


# Matches the DDD (death, disability & disgrace) premium however the settlement
# labels it: "DDD insurance", "D.D.D. Insurance Premium",
# "Death & Disgrace Insurance (Est)", "Disability insurance", ...
_DDD_RE = re.compile(r"\bddd\b|d\.\s*d\.\s*d|death|disab|disgrace", re.I)


def has_ddd_insurance(uof) -> bool:
    """Whether the deal carries a DDD policy — decided by the documents (rule 16).

    The premium shows up as a line in the disbursement breakdown (either side
    of the to-Borrower subtotal). When no line matches — or the documents carry
    no breakdown at all — the deal has no policy and the memo/coversheet must
    not claim one. ``uof`` is a UsesOfFunds (or None).
    """
    if not uof:
        return False
    return any(_DDD_RE.search(_label(item))
               for item in list(uof.deductions or []) + list(uof.additional_costs or []))


# --- Cash flow (Guarantor Analysis) ---------------------------------------

def build_cash_flow(ed: Optional[Extraction], amort: Optional[dict],
                    loan: float, form_salary: float) -> dict:
    # A salary confirmed on the deal-terms form WINS over the extracted figure,
    # matching how every other confirmed term is resolved in memo.render_html
    # (``terms.x or ed.x``). Before 2026-08-18 the extracted salary won here, so
    # an underwriter who corrected the guaranteed salary (or clicked "Use
    # Spotrac figure") moved Section VII but never this cash flow.
    salary_income = (form_salary or 0) or (ed.salary if ed and ed.salary else 0)
    other_income = (ed.other_income if ed else 0) or 0
    income = salary_income + other_income

    taxes = round(income * TAX_RATE)     # rule 1
    living = round(income * LIVING_RATE)  # rule 2
    avail = income - taxes - living

    proposed_ds = loan or 0              # rule 3: principal only in cash flow

    debt_items: list[dict] = []
    if ed:
        if ed.mortgage_payments:
            debt_items.append({"label": "Mortgage payments (incl. taxes & ins.)", "amt": ed.mortgage_payments})
        if ed.auto_payments:
            debt_items.append({"label": "Automobile payments", "amt": ed.auto_payments})
        if ed.insurance:
            debt_items.append({"label": "Insurance (home, health, vehicles)", "amt": ed.insurance})

        # rule 7 sourcing: alimony from the dedicated field, an other-expenses
        # row, or even a misfiled liabilities row — always surfaced here.
        alimony_amt = (
            ed.alimony
            or _sum([x for x in (ed.other_expenses or []) if is_alimony_row(x)])
            or _sum([x for x in (ed.liabilities or []) if is_alimony_row(x)])
        )
        if alimony_amt:
            debt_items.append({"label": "Alimony / child support", "amt": alimony_amt})

        if ed.student_loans:
            debt_items.append({"label": "Student loans", "amt": ed.student_loans})
        if ed.interest_principal_loans:
            debt_items.append({"label": "Interest & principal on loans", "amt": ed.interest_principal_loans})
        if ed.hoa_payments:
            debt_items.append({"label": "HOA payments", "amt": ed.hoa_payments})

        # Every remaining annual-expenditure item flows in, except the rows we
        # compute ourselves (taxes/living) and alimony (already added once).
        for x in (ed.other_expenses or []):
            if _amount(x) and not is_alimony_row(x) and not is_computed_row(x):
                debt_items.append({"label": _label(x), "amt": _amount(x)})

    other_debt = sum(d["amt"] for d in debt_items)
    total_ds = proposed_ds + other_debt

    return {
        "income": income,
        "salary_income": salary_income,
        "other_income": other_income,
        "taxes": taxes,
        "living": living,
        "avail": avail,
        "proposed_ds": proposed_ds,
        "debt_items": debt_items,
        "total_ds": total_ds,
        "net_cf": avail - total_ds,
    }


def calc_ltc(loan: float, guaranteed_salary: float) -> float:
    """Loan-to-Contract = loan / guaranteed earnings, as a percentage."""
    return (loan / guaranteed_salary * 100) if guaranteed_salary else 0.0


# --- Deal Summary & Policy Compliance (memo coversheet) ---------------------

# The Credit paragraph memo.py falls back to when extraction produced no
# credit_notes - an unreviewed report must never be reported as clean (the
# old default asserted "No bankruptcies, no judgments, no tax liens" whether
# or not anyone had looked).
CREDIT_NOT_SUMMARIZED = (
    "Credit report on file was not summarized at extraction - review it "
    "manually for the score, payment history (late or missed payments on "
    "auto loans, mortgages and credit cards), and derogatories.")

# Policy thresholds per South River Capital athlete-lending guidelines.
LTC_MAX_PCT = 30.0   # raised from 25% (Lauren, 2026-08-25)

# The standing mitigants every SRC athlete loan carries; printed under any
# exception so the approver signs off with the protections in view.
STANDARD_MITIGANTS = (
    "payroll direct-deposit sweep at the source, UCC-1 first-lien on the "
    "contract receivable, full personal guarantee with confession of "
    "judgment, and DDD insurance assigned to Lender"
)
# The same mitigants on a deal without a DDD policy (rule 16 — the documents'
# disbursement carries no premium line): the insurance clause drops out.
STANDARD_MITIGANTS_NO_DDD = (
    "payroll direct-deposit sweep at the source, UCC-1 first-lien on the "
    "contract receivable, and full personal guarantee with confession of "
    "judgment"
)


def calc_policy_compliance(*, ltc: float, cf: dict,
                           mat_fmt: str, has_maturity: bool,
                           has_ddd: bool = True) -> dict:
    """Build the Deal Summary & Policy Compliance coversheet checklist.

    Each row is {label, req, actual, status} with status "pass" | "exc" | "na".
    Quantitative tests (LTC, net cash flow) are COMPUTED from the same figures
    the memo body reports, so the coversheet and the memo can never disagree.
    Structural requirements every SRC deal satisfies through the loan
    documents (UCC-1, clean UCC search, personal guarantee, DDD insurance,
    work authorization, no-new-debt covenant) show as conditions of closing.

    ``has_ddd`` follows the documents' disbursement (rule 16 —
    has_ddd_insurance). The "DDD insurance assigned to Lender" row always
    prints (Lauren, 2026-08-26); when False it reads "If applicable" /
    "None in place" with status N/A, and the mitigants sentence loses its
    insurance clause,
    so the coversheet never claims a policy the deal does not carry.

    RETIRED CRITERIA (Lauren, 2026-08-25 — removed from the coversheet, the
    exceptions block, and any compliance listing going forward): combined
    contract-note leverage, combined LTV on the subject property, salary
    fully guaranteed, payroll direct-deposit sweep, lien on real estate
    recorded, minimum credit score, and no derogatories / late payments.
    Do not re-add them without her direction.

    Every "exc" row is echoed in ``exceptions`` (with the standard mitigants),
    matching the sheet's rule that any exception requires credit approval
    prior to funding.

    ``cf`` is build_cash_flow's output.
    """
    rows: list[dict] = []

    def add(label: str, req: str, actual: str, status: str):
        rows.append({"label": label, "req": req, "actual": actual,
                     "status": status})

    # 1. Loan-to-Contract — same figure as Section III Formulas/Advances.
    add("Loan-to-Contract (LTC)", f"&le; {LTC_MAX_PCT:g}%", f"{ltc:.1f}%",
        "pass" if ltc <= LTC_MAX_PCT else "exc")

    # 2. Structure.
    add("Maturity within guaranteed term", "Required",
        f"Balloon at maturity {mat_fmt}" if has_maturity else "Maturity not set",
        "pass" if has_maturity else "na")

    # 3-7. Structural requirements satisfied through the loan documents —
    # closing conditions of every SRC athlete loan, per Section II.
    for label, actual in (
        ("UCC-1 on contract receivable", "Condition of closing — first-lien filing"),
        ("Clean UCC search — no competing liens", "Pre-funding search — condition of closing"),
        ("Personal guarantee w/ conf. of judgment", "Condition of closing — full personal guarantee"),
    ):
        add(label, "Required", actual, "pass")

    # The DDD row ALWAYS prints (Lauren, 2026-08-26); what it reports follows
    # the documents' disbursement (rule 16): Required / policy as a condition
    # of closing when the premium line is there, "If applicable" /
    # "None in place" (N/A) when not — "Required" next to "None in place"
    # read like a failed requirement (reworded — Lauren, 2026-08-27).
    if has_ddd:
        add("DDD insurance assigned to Lender", "Required",
            "Condition of closing — policy naming Lender", "pass")
    else:
        add("DDD insurance assigned to Lender", "If applicable",
            "None in place", "na")

    add("US work authorization current", "Required",
        "Verified at closing — condition of closing", "pass")

    # 8. Ongoing covenant in the loan documents (Section IV).
    add("No new contract debt w/o consent", "Ongoing",
        "Covenant in loan documents — quarterly credit pulls", "pass")

    # 9. Net cash flow after debt service — Section VIII's bottom line.
    net_cf = cf.get("net_cf", 0)
    add("Positive net cash flow after debt svc.", "Required",
        (f"{_money0(net_cf)}" if net_cf >= 0 else f"({_money0(abs(net_cf))})"),
        "pass" if net_cf > 0 else "exc")

    exceptions = [{"label": r["label"], "detail": f'{r["actual"]} vs. {r["req"]}'}
                  for r in rows if r["status"] == "exc"]
    return {"rows": rows, "exceptions": exceptions,
            "mitigants": (STANDARD_MITIGANTS if has_ddd
                          else STANDARD_MITIGANTS_NO_DDD)}


# --- SSN masking -----------------------------------------------------------

def mask_ssn(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))[-4:]
    return f"XXX-XX-{digits}" if digits else ""


# --- Sport label -----------------------------------------------------------

_PRO_PREFIX_RE = re.compile(r"^\s*professional\s+", re.I)


def normalize_sport(sport) -> str:
    """Strip a leading 'Professional' from the sport name.

    The memo phrases this as "a Professional <sport> player", so a sport value
    of "Professional Ice Hockey" would render the word twice. Dropping the
    prefix here guarantees the memo never says "Professional Professional ...".
    """
    return _PRO_PREFIX_RE.sub("", str(sport or "")).strip()
