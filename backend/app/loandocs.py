"""Loan Documents rendering.

Renders the closing-document package (Business Entity Affidavit, Promissory
Note, Loan & Security Agreement, Guaranty, Memo of Settlement, UCC-1 and
Payment Direction Letter) as HTML in the credit memo's design, then exports it
to PDF and Word through the same pipeline the memo uses (memo.render_pdf /
memo.render_word).

The Jinja template (templates/loan_documents.html.j2) is GENERATED from the
executed sports example by tools/build_loandocs_template.py — do not edit the
clause text by hand here; re-run the builder instead.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import calculations as calc
from .loandocs_models import LoanDocsInclude, LoanDocTerms

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = Path(__file__).parent / "logo.txt"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),  # template is trusted HTML
)

FOOTER_TEXT = "South River Capital — Loan Documents"

_DOC_TITLES = [
    ("affidavit", "Business Entity Affidavit"),
    ("note", "Promissory Note (with Exhibit A — Loan Repayments by Month)"),
    ("lsa", "Loan and Security Agreement (with Exhibit A — Definitions)"),
    ("guaranty", "Guaranty"),
    ("settlement", "Memo of Settlement"),
    ("ucc", "UCC Financing Statement (with Exhibit A)"),
    ("letter", "Payment Direction Letter"),
]

# .env-backed defaults for the Payment Direction Letter's receiving account.
# The values themselves live only in the (git-ignored) .env — never in code.
_BANK_ENV = {
    "account_name": "SRC_ACCOUNT_NAME",
    "bank_name": "SRC_BANK_NAME",
    "bank_account_no": "SRC_BANK_ACCOUNT_NO",
    "bank_aba": "SRC_BANK_ABA",
    "bank_address_1": "SRC_BANK_ADDRESS_1",
    "bank_address_2": "SRC_BANK_ADDRESS_2",
    "bank_contact": "SRC_BANK_CONTACT",
    "bank_phone": "SRC_BANK_PHONE",
}


def bank_defaults() -> dict:
    """The SRC_BANK_* values from the environment (for UI prefill)."""
    return {field: os.environ.get(env, "") for field, env in _BANK_ENV.items()}


def _money(n, cents: bool = False) -> str:
    if n is None:
        return "$________"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "$________"
    return f"${n:,.2f}" if cents else f"${round(n):,}"


def _fmt_num(n, blank: str = "___") -> str:
    """A rate/count without a trailing .0 (13.5 -> '13.5', 5.0 -> '5')."""
    if n is None:
        return blank
    try:
        return f"{float(n):g}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_short(d: date | None) -> str:
    return f"{d.month}/{d.day}/{d.year}" if d else "____________"


def _fmt_long(d: date | None) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else "________________"


# The sentence that opens the Note's Payment clause, per repayment structure.
# "balloon" is the sports template's wording and "fully_amortized" comes from
# an executed fully-amortized note (supplied by Lauren, 2026-07-08) — both
# verbatim.
PAYMENT_SENTENCES = {
    "balloon": ("No monthly payments are required; the full principal and "
                "interest are due as a balloon payment at maturity."),
    "interest_only": ("Interest only payments are due monthly, and the full "
                      "principal balance, together with any accrued and unpaid "
                      "interest, is due at maturity."),
    "fully_amortized": ("Monthly principal payments are required until the "
                        "owed amount has been paid in full."),
}


def _computed_rows(terms: LoanDocTerms) -> list[dict]:
    """Raw {date, interest, principal} floats per payment, by structure.

    Payment dates come from calc.calc_repayment_schedule so all three
    structures land on the same monthly anniversary dates the memo uses.
    """
    base = calc.calc_repayment_schedule(
        terms.loan_amount, terms.interest_rate,
        terms.closing_date, terms.maturity_date)
    dates = [r["date"] for r in base["rows"]]
    loan = terms.loan_amount
    kind = terms.amortization_type

    if kind == "fully_amortized":
        n = len(dates)
        r = terms.interest_rate / 100 / 12
        pmt = loan * r / (1 - (1 + r) ** -n) if r else loan / n
        rows, bal = [], float(loan)
        for i, d in enumerate(dates):
            interest = round(bal * r, 2)
            principal = round(pmt - interest, 2)
            if i == len(dates) - 1:  # retire the remaining balance exactly
                principal = round(bal, 2)
            bal = round(bal - principal, 2)
            rows.append({"date": d, "interest": interest, "principal": principal})
        return rows

    if kind == "interest_only":
        # the memo's Section X fallback: interest monthly, principal balloon
        return [{"date": r["date"], "interest": r["interest"],
                 "principal": r["principal"]} for r in base["rows"]]

    # balloon: nothing due until maturity, then principal + accrued interest.
    # Interest accrues actual/365 via calc_amort — the same engine the memo's
    # facility total uses — so the two tools never disagree on the balloon.
    amort = calc.calc_amort(loan, terms.interest_rate,
                            terms.closing_date, terms.maturity_date)
    rows = [{"date": d, "interest": 0, "principal": 0} for d in dates[:-1]]
    rows.append({"date": dates[-1], "interest": amort["interest"],
                 "principal": float(loan)})
    return rows


def _schedule(terms: LoanDocTerms) -> tuple[list[dict], dict]:
    """Exhibit A rows: supplied override first, else computed per the deal's
    repayment structure (balloon / interest only / fully amortized)."""
    if terms.repayment_schedule:
        rows = [
            {
                "num": i + 1,
                "date": r.date or "—",
                "interest": _money(r.interest, cents=True),
                "principal": _money(r.principal, cents=True),
                "total": _money(r.total if r.total is not None
                                else r.interest + r.principal, cents=True),
            }
            for i, r in enumerate(terms.repayment_schedule)
        ]
        ti = sum(r.interest for r in terms.repayment_schedule)
        tp = sum(r.principal for r in terms.repayment_schedule)
        tt = sum((r.total if r.total is not None else r.interest + r.principal)
                 for r in terms.repayment_schedule)
        totals = {"interest": _money(ti, cents=True),
                  "principal": _money(tp, cents=True),
                  "total": _money(tt, cents=True)}
        return rows, totals

    if terms.loan_amount and terms.interest_rate and terms.closing_date and terms.maturity_date:
        raw = _computed_rows(terms)
        rows = [
            {
                "num": i + 1,
                "date": r["date"],
                "interest": _money(r["interest"], cents=True),
                "principal": _money(r["principal"], cents=True),
                "total": _money(r["interest"] + r["principal"], cents=True),
            }
            for i, r in enumerate(raw)
        ]
        ti = sum(r["interest"] for r in raw)
        tp = sum(r["principal"] for r in raw)
        totals = {"interest": _money(ti, cents=True),
                  "principal": _money(tp, cents=True),
                  "total": _money(ti + tp, cents=True)}
        return rows, totals

    placeholder = {"num": "", "date": "Set the loan amount, rate, closing and "
                                      "maturity dates to compute the schedule.",
                   "interest": "—", "principal": "—", "total": "—"}
    return [placeholder], {"interest": "—", "principal": "—", "total": "—"}


def _settlement(terms: LoanDocTerms) -> list[dict]:
    """Memo of Settlement rows: Gross, each deduction in parens, computed
    'To be disbursed to Borrower (Est)'; when post-disbursement lines exist
    (e.g. DDD Insurance) they follow in parens down to a computed 'Net to be
    disbursed to Borrower (Est)'. Subtotals always recomputed, never copied."""
    gross = terms.loan_amount or 0
    rows = [{"label": "Gross Loan Amount",
             "amount": _money(gross, cents=True), "cls": "total"}]
    deducted = 0.0
    for line in terms.settlement_lines:
        if not line.amount:  # unfilled rows would print a noisy "($0.00)"
            continue
        amt = abs(line.amount)
        deducted += amt
        rows.append({"label": line.label or "—",
                     "amount": f"({_money(amt, cents=True)})", "cls": ""})
    disbursed = gross - deducted
    post = [line for line in terms.settlement_post_lines if line.amount]
    rows.append({"label": "To be disbursed to Borrower (Est)",
                 "amount": _money(disbursed, cents=True),
                 "cls": "total" if post else "grand"})
    if post:
        net = disbursed
        for line in post:
            amt = abs(line.amount)
            net -= amt
            rows.append({"label": line.label or "—",
                         "amount": f"({_money(amt, cents=True)})", "cls": ""})
        rows.append({"label": "Net to be disbursed to Borrower (Est)",
                     "amount": _money(net, cents=True), "cls": "grand"})
    return rows


# Generational suffixes belong in the UCC-1's own MIDDLE NAME / SUFFIX box,
# never in the surname box - "Jr." as a debtor's last name is as defective a
# filing as half a compound surname.
_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}


def _is_suffix(token: str) -> bool:
    return token.rstrip(".,").lower() in _SUFFIX_TOKENS


def _split_name(name: str, surname: str = "") -> tuple[str, str, str]:
    """(last, first, middle/suffix) for the UCC-1 debtor name boxes.

    UCC-1 sufficiency turns on the debtor's name as their driver's license
    shows it, so the surname has to go in box 1b WHOLE: a borrower whose
    license reads RIVAS DELGADO / MATEO is filed under "Rivas Delgado", never
    under "Delgado" with "Mateo Rivas" as the first name (Lauren, 2026-10-01).
    Nothing in the name itself says where a compound surname begins, so
    `surname` - the ID's own surname field, read by the extraction and
    editable on the tab - decides when it is known. Without it the last word
    is the surname, as before; a generational suffix moves to its own box
    either way.
    """
    tokens = (name or "").strip().split()
    if not tokens:
        return "", "", ""
    suffixes = [t for t in tokens if _is_suffix(t)]
    rest = [t for t in tokens if not _is_suffix(t)]
    surname_tokens = (surname or "").strip().split()
    if surname_tokens:
        last = " ".join(surname_tokens)
        # Take the surname off the end of the full name, leaving the given
        # names - tolerating a name typed without it ("Mateo" / "Rivas Delgado").
        tail = {t.lower().rstrip(".,") for t in surname_tokens}
        given = list(rest)
        while given and given[-1].lower().rstrip(".,") in tail:
            given.pop()
    elif len(rest) >= 2:
        last, given = rest[-1], rest[:-1]
    else:
        last, given = (rest[0] if rest else ""), []
    middle = " ".join(given[1:] + suffixes)
    return last, (given[0] if given else ""), middle


# Spelled-out league names for the no-team-contract wording ("the team that
# signs the Borrower in the upcoming 2026 National Football League"). Unknown
# leagues fall through to whatever was typed in the League field.
_LEAGUE_FULL = {
    "NFL": "National Football League",
    "MLB": "Major League Baseball",
    "NBA": "National Basketball Association",
    "NHL": "National Hockey League",
    "MLS": "Major League Soccer",
    "WNBA": "Women's National Basketball Association",
}


def render_html(terms: LoanDocTerms, include: LoanDocsInclude) -> str:
    fee_amount = terms.origination_fee_amount
    if fee_amount is None and terms.loan_amount and terms.origination_fee_pct:
        fee_amount = terms.loan_amount * terms.origination_fee_pct / 100

    contract_title = terms.contract_title or (
        f"{terms.league} Professional Contract" if terms.league
        else "Professional Contract")

    last, first, middle_suffix = _split_name(terms.borrower_name,
                                             terms.borrower_surname)
    city_state_zip = ", ".join(x for x in [terms.borrower_city] if x)
    tail = " ".join(x for x in [terms.borrower_state_abbr, terms.borrower_zip] if x)
    city_state_zip = ", ".join(x for x in [city_state_zip, tail] if x) or "____________"

    bank = bank_defaults()
    for field in _BANK_ENV:
        override = getattr(terms, field)
        if override:
            bank[field] = override

    rows, totals = _schedule(terms)

    context = {
        "logo": _LOGO_PATH.read_text().strip(),
        "include": include,
        "doc_index": [title for key, title in _DOC_TITLES
                      if getattr(include, key)],
        "borrower_name": terms.borrower_name or "____________________",
        "borrower_first_name": first,
        "borrower_last_name": last,
        # The UCC-1 cell keeps its height when there is no middle name/suffix.
        "borrower_middle_suffix": middle_suffix or "&nbsp;",
        "borrower_street": terms.borrower_street or "____________________",
        "borrower_city": terms.borrower_city or "________",
        "borrower_state_abbr": terms.borrower_state_abbr or "____",
        "borrower_zip": terms.borrower_zip or "______",
        "borrower_city_state_zip": city_state_zip,
        "borrower_state": terms.borrower_state or "____________",
        "occupation": terms.occupation or "Professional Athlete",
        "use_of_proceeds": terms.use_of_proceeds or "Business related expenses",
        "loan_amount": _money(terms.loan_amount),
        "loan_amount_full": _money(terms.loan_amount, cents=True),
        "origination_fee_amount": _money(fee_amount),
        "interest_rate": _fmt_num(terms.interest_rate),
        "prepay_min_months": terms.prepay_min_months or "___",
        "payment_structure_sentence": PAYMENT_SENTENCES.get(
            terms.amortization_type, PAYMENT_SENTENCES["balloon"]),
        "has_insurance_policy": terms.has_insurance_policy,
        "late_charge_pct": _fmt_num(terms.late_charge_pct),
        "exit_fee_pct": _fmt_num(terms.exit_fee_pct),
        "default_rate_points": _fmt_num(terms.default_rate_points),
        "closing_date": _fmt_short(terms.closing_date),
        "closing_year": terms.closing_date.year if terms.closing_date else "20___",
        "maturity_date_long": _fmt_long(terms.maturity_date),
        "loan_number": terms.loan_number,
        "no_team_contract": terms.no_team_contract,
        "league_full": _LEAGUE_FULL.get((terms.league or "").strip().upper(),
                                        (terms.league or "").strip())
                       or "________________",
        "upcoming_season_year": terms.upcoming_season_year.strip() or (
            str(terms.closing_date.year) if terms.closing_date else "20___"),
        "team_name": terms.team_name or "____________________",
        "team_street": terms.team_street or "____________________",
        "team_city_state_zip": terms.team_city_state_zip or "____________________",
        "contract_title": contract_title,
        "contract_date_long": _fmt_long(terms.contract_date),
        "lender_signatory_title": terms.lender_signatory_title or "________",
        "schedule": rows,
        "schedule_totals": totals,
        "settlement": _settlement(terms),
        **bank,
    }
    for field in _BANK_ENV:
        context[field] = bank[field] or "________________"

    return _env.get_template("loan_documents.html.j2").render(**context)
