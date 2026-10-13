"""Memo rendering.

Produces the credit memo as HTML (via the Jinja2 template that mirrors the
original design pixel-for-pixel), and exports it to PDF and Word.

- HTML  : Jinja2 template in templates/memo.html.j2
- PDF   : WeasyPrint renders the print-styled HTML to a true PDF server-side
- Word  : HTML-wrapped .doc that Microsoft Word opens with formatting intact
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import DealTerms, Extraction
from . import calculations as calc

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = Path(__file__).parent / "logo.txt"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),  # template is trusted HTML
)

# PDF page furniture. Chromium draws these in the page MARGIN (not the body), so
# the footer can never overlap content, and its <span class="pageNumber"> /
# <span class="totalPages"> tokens are filled with the real, correct page numbers
# for however many physical pages the deal happens to span. The in-body
# ``.pg-footer`` is hidden in print (see @media print in memo.html.j2) so it does
# not double up. An empty header template suppresses Chromium's default
# date/title header.
_PDF_HEADER_TEMPLATE = "<div></div>"
_DEFAULT_FOOTER_TEXT = "South River Capital — Credit Memorandum"

# Neither of Lauren's example memos carries the Deal Summary & Policy
# Compliance checklist: both lead with the deal and close with a
# recommendation and signature lines. Rather than delete the checklist, it
# moves to a final APPENDIX page - the memo reads like the examples, and the
# compliance view survives for the file. Set this False to drop it entirely.
_SHOW_COMPLIANCE = True


def _pdf_footer_template(footer_text: str, page_numbers: bool = True) -> str:
    label = footer_text.replace("—", "&mdash;")
    pages = (
        '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>'
        if page_numbers else "<span></span>"
    )
    return (
        '<div style="width:100%;box-sizing:border-box;padding:0 0.75in;margin:0;'
        'font-family:Arial,Helvetica,sans-serif;font-size:8px;line-height:1;'
        'letter-spacing:0.08em;color:#8a8a8a;text-transform:uppercase;'
        'display:flex;justify-content:space-between;align-items:center;">'
        f"<span>{label}</span>"
        f"{pages}"
        "</div>"
    )


def _money(n) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n == 0:
        return "$0"
    return "$" + f"{abs(round(n)):,}"


def _money2(n) -> str:
    """A figure WITH cents: "$36,666.67".

    Lauren's examples print cents wherever a payment is stated - McCormick's
    $36,666.67 monthly interest and $916.67 per $100,000, the Bouye template's
    $88,785.95 instalment. A memo that rounds a payment to the dollar disagrees
    with the note the borrower signs, so payments, the repayment schedule and
    investor returns all use this; headline amounts and the Financial Analysis
    stay on whole dollars, as the examples have them.
    """
    if n is None:
        return "\u2014"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "\u2014"
    return "$" + f"{n:,.2f}"


def _signed(n) -> str:
    if n is None:
        return "—"
    return f"({_money(abs(n))})" if n < 0 else _money(n)


def _fmt_pct(n) -> str:
    """Format a rate/fee percent without a trailing .0 (3.0 -> '3', 13.5 -> '13.5'),
    so the memo matches how the term sheet states it."""
    try:
        return f"{float(n):g}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_long(d: date | None) -> str:
    # Build the no-leading-zero day from the date's integer fields instead of the
    # glibc/BSD-only "%-d" strftime code, which raises ValueError on Windows.
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else "[Maturity Date]"


def _fmt_short(d: date | None) -> str:
    return d.strftime("%m/%d/%Y") if d else "[Date]"


def _cf_rows_html(cf: dict) -> str:
    rows = [
        ("1", "Qualifying Income (Annual)", cf["base_income"], ""),
    ]
    if cf["other_income"]:
        rows.append(("2", "Other Income (Annual Income Section)", cf["other_income"], ""))
    rows += [
        ("", "Gross Cash Flow", cf["income"], "total"),
        ("3", "Less: Taxes @ 45%", -cf["taxes"], ""),
        ("4", "Less: Ordinary Living Expenses @ 10%", -cf["living"], ""),
        ("", "Total Deductions", -(cf["taxes"] + cf["living"]), "total"),
        ("", "Available for Debt", cf["avail"], "total"),
        ("5", "Less: Proposed Facility", -cf["proposed_ds"], ""),
    ]
    for di in cf["debt_items"]:
        rows.append(("", "Less: " + di["label"], -di["amt"], ""))
    rows += [
        ("", "Total Debt", -cf["total_ds"], "total"),
        ("", "Net Cash Flow", cf["net_cf"], "grand"),
    ]
    return "".join(
        f'<tr class="{cls}"><td>{n}</td><td>{label}</td>'
        f'<td class="num-col">{_signed(amt)}</td></tr>'
        for n, label, amt, cls in rows
    )


def _rollforward_footnote(rf: dict) -> str:
    """The line under the PFS table saying these are not the reported figures.

    A roll-forward and a zero-out can happen together or on their own, so the
    sentence is assembled from whichever actually applied.
    """
    rolled = [r for r in rf["rows"] if r["paydown"] > 0 and not r["zeroed"]]
    zeroed = [r for r in rf["rows"] if r["zeroed"]]
    bits = []
    if rolled:
        bits.append(
            f'rolled forward {rf["months"]} months from the '
            f'{_fmt_short(rf["pfs_date"])} Personal Financial Statement to '
            f'{_fmt_short(rf["as_of"])} at the scheduled monthly payments')
    if zeroed:
        bits.append(f'reduced by {len(zeroed)} '
                    f'{"obligation" if len(zeroed) == 1 else "obligations"} '
                    f'shown as repaid in full')
    # The "as agreed" qualifier belongs to the roll-forward, so it trails the
    # whole clause rather than sitting mid-sentence when a zero-out joins it.
    qualifier = ', assuming payments were made as agreed' if rolled else ''
    return (f'Liability balances are {" and ".join(bits)} '
            f'(&minus;{_money(rf["total_paydown"])} in total){qualifier}. '
            f'Revolving and non-monthly obligations are carried as reported.')


def _debt_row_html(r: dict) -> str:
    """One scheduled debt as an indented detail line under a summary liability.

    The amount is the balance this memorandum carries; the fine print names the
    schedule it came from and, when the carried figure differs from the
    statement, the reported balance it was brought forward from.
    """
    bits = []
    if r["description"] and r["description"].lower() != r["lender"].lower():
        bits.append(r["description"])
    if sch := calc.category_schedule(r["category"]):
        bits.append(f"Schedule {sch}")
    if r["zeroed"]:
        bits.append(f'{_money(r["reported"])} reported, shown as repaid in full')
    elif r["rolled"]:
        n = r["months_applied"]
        bits.append(f'{_money(r["reported"])} reported, rolled forward '
                    f'{n} month{"" if n == 1 else "s"}')
    elif r["treatment"] == "hold":
        bits.append("held at the reported balance")
    elif r["reason"]:
        bits.append(f'as reported &mdash; {r["reason"]}')
    return (f'<tr class="dsc"><td class="dsc-lbl">{r["lender"]}'
            f'<span class="dsc-note">{" &middot; ".join(bits)}</span></td>'
            f'<td class="num-col">{_money(r["adjusted"])}</td></tr>')


def _debt_detail_rows(rf: dict, category: str) -> str:
    """The scheduled debts that roll up into one page-1 summary liability,
    listed beneath it (rule 15).

    Every debt on the schedule appears — including the ones carried exactly as
    reported, which move no total and would otherwise show up nowhere on the
    memo. They are a BREAKDOWN of the summary line above them, never added into
    a total.
    """
    if not category:
        return ""
    return "".join(_debt_row_html(r) for r in (rf.get("rows") or [])
                   if r["category"] == category and not r.get("removed"))


def _orphan_debt_rows(rf: dict, liab_items: list, reported_items: list) -> str:
    """Scheduled debts with no summary liability to sit under — a row whose
    "Rolls into" category is unset, or one the statement carries no line for.

    Listed at the foot of the liabilities block under an explicit caveat: these
    balances are NOT inside Total Liabilities, so nothing is double counted and
    nothing is silently dropped either.
    """
    covered = {calc.summary_category(l.label) for l in liab_items if l.amount}
    # The statement's own lines, before any adjustment — a category that WAS
    # on the statement but no longer prints was consumed by a zero-out (or a
    # roll-forward that reached $0): a deliberate removal to document, never
    # an orphan (2026-09-02 — a note paid off at closing was zeroed out and
    # its audit line printed under the "no matching liability line" caveat).
    on_statement = {calc.summary_category(l.label) for l in reported_items
                    if l.amount}
    # A removed debt (treatment "remove" — the ✕) prints NOWHERE, by design.
    leftovers = [r for r in (rf.get("rows") or [])
                 if r["category"] not in covered and not r.get("removed")]
    removed = [r for r in leftovers if r["category"] in on_statement]
    orphans = [r for r in leftovers if r["category"] not in on_statement]
    out = []
    if removed:
        out.append(
            '<tr class="dsc"><td class="dsc-lbl" colspan="2"><em>Repaid in full '
            '&mdash; removed from the summary totals above:</em></td></tr>'
            + "".join(_debt_row_html(r) for r in removed))
    if orphans:
        out.append(
            '<tr class="dsc"><td class="dsc-lbl" colspan="2"><em>Scheduled debt not '
            'carried in the summary totals above &mdash; no matching liability line '
            'on the statement:</em></td></tr>'
            + "".join(_debt_row_html(r) for r in orphans))
    return "".join(out)


def _pfs_html(ed: Extraction | None, facility_due: float,
              fallback_asset: float = 0, fallback_label: str = "",
              as_of: date | None = None) -> str:
    bs = calc.calc_balance_sheet(ed, facility_due, as_of)
    rf = bs.get("rollforward") or {}
    out = []
    if ed and ed.total_assets:
        for a in bs["assets_items"]:
            if a.amount:
                out.append(f'<tr><td>{a.label}</td><td class="num-col">{_money(a.amount)}</td></tr>')
        out.append(f'<tr class="total"><td>Total Assets</td><td class="num-col">{_money(bs["assets_total"])}</td></tr>')
        out.append('<tr><td>Liabilities</td><td></td></tr>')
        if facility_due:
            out.append(f'<tr><td><em>Proposed Facility (includes interest)</em></td>'
                       f'<td class="num-col"><em>{_money(facility_due)}</em></td></tr>')
        detailed: set[str] = set()
        for l in bs["liab_items"]:
            if l.amount:
                out.append(f'<tr><td>{l.label}</td><td class="num-col">{_money(l.amount)}</td></tr>')
                # Rule 15 — each financed debt that rolls into this line is
                # listed under it, so a debt added in Step 2b always shows.
                # Under the FIRST line of its category only: a statement can
                # carry two lines the category matches ("Notes Payable to
                # others (Secured)" and "(Unsecured)", 2026-09-02), and the
                # breakdown printed under both listed every debt twice. The
                # first line is also where _apply_rollforward takes the
                # paydown from, so the breakdown sits with the money.
                cat = calc.summary_category(l.label)
                if cat not in detailed:
                    detailed.add(cat)
                    out.append(_debt_detail_rows(rf, cat))
        out.append(_orphan_debt_rows(rf, bs["liab_items"],
                                     (ed.liabilities if ed else None) or []))
        out.append(f'<tr class="total"><td>Total Liabilities (incl. Proposed Facility)</td>'
                   f'<td class="num-col">{_money(bs["total_liab"])}</td></tr>')
        out.append(f'<tr class="grand"><td>Net Worth (Total Assets &minus; Total Liabilities)</td>'
                   f'<td class="num-col">{_signed(bs["net_worth"])}</td></tr>')
        if rf.get("applied"):
            # Rule 15 — say on the statement itself that these are not the
            # figures the PFS reports, so no reader mistakes them for verbatim.
            out.append(f'<tr class="note"><td colspan="2"><em>{_rollforward_footnote(rf)}'
                       f'</em></td></tr>')
    else:
        # No PFS among the documents. The statement is stubbed from the one
        # asset the deal itself evidences, named so no reader mistakes it for
        # a balance sheet: the pledged collateral, or the qualifying income
        # where nothing is pledged.
        out.append(
            f'<tr><td>{fallback_label or "Qualifying Income (Annual)"}</td>'
            f'<td class="num-col">{_money(fallback_asset)}</td></tr>'
            f'<tr class="total"><td>Total Assets</td><td class="num-col">{_money(fallback_asset)}</td></tr>'
            f'<tr><td>Liabilities</td><td></td></tr>'
            f'<tr><td><em>Proposed Facility (includes interest)</em></td>'
            f'<td class="num-col"><em>{_money(facility_due)}</em></td></tr>'
            f'<tr class="total"><td>Total Liabilities (incl. Proposed Facility)</td>'
            f'<td class="num-col">{_money(facility_due)}</td></tr>'
            f'<tr class="grand"><td>Net Worth (Total Assets &minus; Total Liabilities)</td>'
            f'<td class="num-col">{_signed(fallback_asset - facility_due)}</td></tr>'
            f'<tr class="note"><td colspan="2"><em>No Personal Financial '
            f'Statement was among the documents: this statement shows only the '
            f'asset the deal documents evidence and the proposed facility. '
            f'Obtain a PFS before funding.</em></td></tr>'
        )
    return "".join(out)


def _credit_text(ed: Extraction | None, as_of: date | None) -> str:
    """Section IV credit paragraph, with the rule 15 roll-forward appended.

    The drafted roll-forward sentence states the reported figure, the payment
    and window, the "assuming payments were made as agreed" qualifier, and each
    maturity date — the way Lauren writes it by hand.
    """
    # No credit_notes -> the not-summarized sentinel, never a clean-credit
    # assertion nobody verified.
    base = (ed.credit_notes if ed and ed.credit_notes
            else calc.CREDIT_NOT_SUMMARIZED)
    rf = calc.calc_debt_rollforward(ed, as_of)
    if rf.get("note"):
        base = f"{base.rstrip()} {rf['note']}"
    for w in rf.get("warnings") or []:
        base = f"{base.rstrip()} {w}"
    return base


def _uses_of_funds_html(uof: dict) -> str:
    """Render Section VI's disbursement waterfall from calc.calc_uses_of_funds.

    Gross Loan Amount, each deduction shown as a parenthesized reduction, the
    "To be disbursed to Borrower" subtotal, then (when the documents provide
    them) the additional costs carved out of that figure and the final "Net to
    be Disbursed to Borrower". Mirrors South River's standard disbursement table.
    """
    out = [
        f'<tr class="total"><td>Gross Loan Amount</td>'
        f'<td class="num-col">{_money(uof["gross"])}</td></tr>'
    ]
    for d in uof["deductions"]:
        out.append(f'<tr><td>{d["label"]}</td>'
                   f'<td class="num-col">({_money(d["amount"])})</td></tr>')
    out.append(
        f'<tr class="total"><td><em>To be disbursed to Borrower (Est)</em></td>'
        f'<td class="num-col"><em>{_money(uof["to_borrower"])}</em></td></tr>'
    )
    if uof["additional_costs"]:
        for a in uof["additional_costs"]:
            out.append(f'<tr><td>{a["label"]}</td>'
                       f'<td class="num-col">{_money(a["amount"])}</td></tr>')
        out.append(
            f'<tr class="grand"><td>Net to be Disbursed to Borrower (Est)</td>'
            f'<td class="num-col">{_money(uof["net_to_borrower"])}</td></tr>'
        )
    return "".join(out)


def _repayment_html(rows: list[dict], totals: dict) -> str:
    """Render Section X's repayment table — one row per scheduled payment
    (Payment | Date | Interest | Principal | Total) followed by a totals row.

    ``rows`` come from the uploaded documents when present, otherwise from
    calc.calc_repayment_schedule. An empty schedule yields a placeholder row.
    """
    if not rows:
        return ('<tr><td colspan="5" style="text-align:center;color:#777">'
                'Repayment schedule unavailable — include it in the uploaded '
                'documents, or set the loan amount, rate, and funding/maturity '
                'dates.</td></tr>')
    out = []
    for r in rows:
        out.append(
            f'<tr><td>{r["num"]}</td><td>{r["date"]}</td>'
            f'<td class="num-col">{_money(r["interest"])}</td>'
            f'<td class="num-col">{_money(r["principal"])}</td>'
            f'<td class="num-col">{_money(r["total"])}</td></tr>'
        )
    out.append(
        f'<tr class="total"><td></td><td></td>'
        f'<td class="num-col">{_money(totals.get("total_interest", 0))}</td>'
        f'<td class="num-col">{_money(totals.get("total_principal", 0))}</td>'
        f'<td class="num-col">{_money(totals.get("total_payment", 0))}</td></tr>'
    )
    return "".join(out)


def _doc_list_html(filenames: list[str]) -> str:
    if filenames:
        return "".join(
            f'<tr><td class="k" style="width:40px;text-align:center">{chr(65 + i)}</td>'
            f'<td>{fn}</td></tr>'
            for i, fn in enumerate(filenames)
        )
    return (
        '<tr><td class="k" style="width:40px;text-align:center">A</td><td>League Contract and Pay Stub</td></tr>'
        '<tr><td class="k" style="text-align:center">B</td><td>Driver\'s License</td></tr>'
        '<tr><td class="k" style="text-align:center">C</td><td>Credit Report / LexisNexis Report</td></tr>'
        '<tr><td class="k" style="text-align:center">D</td><td>Personal Financial Statement (PFS)</td></tr>'
    )


def _compliance_rows_html(rows: list[dict]) -> str:
    """Coversheet checklist rows — Criteria | Requirement | Actual | Status.

    Status renders all three boxes (Pass / Exc. / N/A) with the computed one
    filled in, mirroring the paper template's check-one-of-three layout.
    """
    def badges(status: str) -> str:
        return "".join(
            f'<span class="st{" on" if status == key else ""}">{lab}</span>'
            for key, lab in (("pass", "Pass"), ("exc", "Exc."), ("na", "N/A"))
        )
    return "".join(
        f'<tr><td>{r["label"]}</td><td>{r["req"]}</td><td>{r["actual"]}</td>'
        f'<td class="st-cell">{badges(r["status"])}</td></tr>'
        for r in rows
    )


def _exceptions_html(comp: dict) -> str:
    """The coversheet's Exceptions & Mitigants block.

    Lists every computed exception with the standard mitigants; reads "None"
    when all criteria are within guidelines.
    """
    if not comp["exceptions"]:
        return ('<p style="margin:0">None &mdash; all policy criteria are '
                'within guidelines.</p>')
    n = len(comp["exceptions"])
    items = "".join(
        f'<li><strong>{e["label"]}</strong> &mdash; {e["detail"]}</li>'
        for e in comp["exceptions"]
    )
    noun = "exception requires" if n == 1 else f"{n} exceptions require"
    return (
        f'<ul class="tight" style="margin:0 0 5pt">{items}</ul>'
        f'<p style="margin:0"><strong>Mitigants:</strong> {comp["mitigants"]}. '
        f'The {noun} credit approval prior to funding.</p>'
    )


def _money_or_tbd(v) -> str:
    """A figure, or the examples' own bracketed placeholder when it is absent.

    McCormick prints "[TBD]" in its Financial Analysis rather than leaving the
    cell blank or inventing a number, which is the honest presentation: the
    reader can see the question was asked and not yet answered.
    """
    return _money(v) if v is not None else "[TBD]"


def _risk_rows(ed: Extraction | None, *, has_collateral: bool, is_io: bool,
               has_insurance_policy: bool, profile: str) -> list[dict]:
    """The Risk Considerations & Mitigants pairs.

    The documents' own risks win. With none captured, the structural risks this
    facility carries are stated from the deal's own shape - an interest-only
    balloon has refinancing risk whether or not anyone wrote it down.
    """
    captured = [
        {"risk": r.risk or "Risk", "detail": r.detail or "",
         "mitigant": r.mitigant or ""}
        for r in (ed.risks if ed else []) or [] if (r.risk or r.detail)
    ]
    if captured:
        return captured

    out: list[dict] = []
    if is_io:
        out.append({
            "risk": "Interest only / refinance risk",
            "detail": "No principal amortizes, so the full balance is due at maturity.",
            "mitigant": ("Exit is a refinance or sale of the pledged collateral, "
                         "with the equity cushion shown in Collateral Coverage above."
                         if has_collateral else
                         "Repayment depends on a refinance or on the Borrower's own "
                         "liquidity; there is no collateral to sell."),
        })
    if not has_collateral:
        out.append({
            "risk": "Unsecured",
            "detail": "No specific asset is pledged; recourse is to the Borrower personally.",
            "mitigant": ("Full personal guarantee with confession of judgment, and a "
                         "UCC-1 filing against the Borrower's business assets."),
        })
    if profile == "business":
        out.append({
            "risk": "Customer concentration and earnings volatility",
            "detail": ("Owner income moves with the business, and a closely held "
                       "business's revenue is often concentrated in few customers."),
            "mitigant": ("Underwritten against the most recent complete filed year "
                         "rather than projections; the personal guarantee reaches "
                         "assets outside the business."),
        })
        out.append({
            "risk": "Key-person risk",
            "detail": "The income underwritten depends on the Borrower's continued involvement.",
            "mitigant": ("Life / disability insurance assigned to Lender."
                         if has_insurance_policy else
                         "No policy is assigned; the guarantee and the Borrower's "
                         "estate are the support."),
        })
    elif profile == "investment":
        out.append({
            "risk": "Vacancy and tenant default",
            "detail": "Rental income stops if tenants leave or stop paying.",
            "mitigant": ("Income is underwritten NET of operating costs, taxes, "
                         "insurance and existing mortgage debt service; leases and "
                         "the rent roll are confirmed at underwriting."),
        })
        out.append({
            "risk": "Valuation",
            "detail": "A decline in collateral value erodes the Lender's cushion.",
            "mitigant": ("A third-party valuation is a condition of closing, and the "
                         "coverage table above shows the LTV under stressed values."),
        })
    else:
        out.append({
            "risk": "Income interruption",
            "detail": "The facility is carried by personal income, which can stop.",
            "mitigant": ("Income is documented from returns, W-2s and pay stubs "
                         "rather than projected; liquid assets on the PFS are the "
                         "first support."),
        })
    out.append({
        "risk": "Additional leverage",
        "detail": "New borrowing would rank against the same cash flow.",
        "mitigant": ("Lender holds a first right of refusal on all additional debt; "
                     "failure to comply is an event of default, and credit is pulled "
                     "periodically."),
    })
    return out


def render_html(terms: DealTerms, ed: Extraction | None, filenames: list[str] | None = None) -> str:
    """Render the full credit memo as an HTML string.

    The section order follows Lauren's two example memos (McCormick Paints and
    the Walgreens/Bouye template) rather than the athlete builder's numbered
    Sections I-XII. See memo.html.j2.
    """
    filenames = filenames or []
    loan = terms.loan or (ed.loan_amount if ed else 0) or 0
    rate = terms.rate or (ed.interest_rate_pct if ed else 0) or 0
    fee = terms.fee or (ed.origination_fee_pct if ed else 0) or 0
    income = terms.income or (ed.qualifying_income if ed else 0) or 0
    profile = calc.normalize_profile(terms.profile)
    is_io = (terms.amortization or "io") != "amortizing"

    amort = None
    if terms.fund and terms.mat and loan and rate:
        amort = calc.calc_amort(loan, rate, terms.fund, terms.mat)

    cf = calc.build_cash_flow(ed, amort, loan, income)
    facility_due = calc.facility_total(ed, amort, loan)

    contracted_income = (terms.contracted_income
                         or (ed.contracted_income if ed else 0) or 0)
    income_basis = contracted_income or income
    lti = calc.calc_lti(loan, income_basis)

    collateral_value = (terms.collateral_value
                        or (ed.collateral_value if ed else 0) or 0)
    collateral_desc = (terms.collateral_desc
                       or (ed.collateral_desc if ed else "") or "")
    ltv = calc.calc_ltv(loan, collateral_value)
    dscr = calc.calc_dscr(cf)

    uof = calc.calc_uses_of_funds(ed.uses_of_funds if ed else None, loan, fee)
    has_ins = calc.has_insurance(ed.uses_of_funds if ed else None)

    months = calc.loan_term_months(ed, amort)

    # --- repayment schedule ------------------------------------------------
    # The documents' own schedule wins. Otherwise it is computed from the
    # terms, on the convention the facility actually uses: interest-only pays
    # rate/12 monthly with the principal at maturity (McCormick), an
    # amortizing facility pays a level actual/360 instalment (the Bouye
    # template). A long interest-only schedule is banded by year, the way
    # McCormick presents its 36 months as three rows plus a total.
    rep_rows: list[dict] = []
    rep_totals: dict = {}
    bands: list[dict] = []
    if ed and ed.repayment_schedule:
        rep_rows = [
            {"num": i + 1, "date": r.date or "\u2014", "interest": r.interest,
             "principal": r.principal,
             "total": r.total or (r.interest + r.principal)}
            for i, r in enumerate(ed.repayment_schedule)
        ]
        rep_totals = {
            "total_interest": sum(r["interest"] for r in rep_rows),
            "total_principal": sum(r["principal"] for r in rep_rows),
            "total_payment": sum(r["total"] for r in rep_rows),
        }
    elif terms.fund and terms.mat and loan and rate:
        sched = (calc.calc_repayment_schedule(loan, rate, terms.fund, terms.mat)
                 if is_io else
                 calc.calc_amortizing_schedule(loan, rate, terms.fund, terms.mat))
        rep_rows, rep_totals = sched["rows"], sched
        if len(rep_rows) > 14:
            bands = calc.annualize_schedule(rep_rows, terms.fund)

    band_ctx = [{"label": b["label"],
                 "interest_money": _money2(b["interest"]),
                 "principal_money": _money2(b["principal"]),
                 "total_money": _money2(b["total"])} for b in bands]
    totals_ctx = {
        "interest_money": _money2(rep_totals.get("total_interest", 0)),
        "principal_money": _money2(rep_totals.get("total_principal", 0)),
        "total_money": _money2(rep_totals.get("total_payment", 0)),
    }

    monthly_payment = 0.0
    if rep_rows:
        monthly_payment = (rep_totals.get("level_payment")
                           or rep_rows[0].get("interest") or 0)

    # --- financial analysis -------------------------------------------------
    sched_for_ds = rep_totals if not is_io else None
    debt_service = calc.annual_debt_service(loan, rate, terms.amortization or "io",
                                            sched_for_ds)
    fin = calc.calc_financial_analysis(ed.financials if ed else None, debt_service)
    fin_ctx = {
        "periods": [{
            "period": p["period"] or "[period]",
            "revenue_money": _money_or_tbd(p["revenue"]),
            "ebitda_money": _money_or_tbd(p["ebitda"]),
            "net_income_money": _money_or_tbd(p["net_income"]),
            "debt_service_money": _money(p["debt_service"]),
            "dscr_fmt": f'{p["dscr"]:.2f}x' if p["dscr"] is not None else "[TBD]",
        } for p in fin["periods"]],
        "breakeven": [{"label": b["label"], "money": _money(b["ebitda"])}
                      for b in fin["breakeven"]],
    }

    # --- collateral coverage ------------------------------------------------
    cov_rows = list((ed.collateral_coverage if ed else []) or [])
    if not cov_rows and collateral_value:
        from .models import CollateralCoverageRow
        cov_rows = [CollateralCoverageRow(
            basis=(ed.collateral_desc if ed and ed.collateral_desc
                   else "Valuation on file"), value=collateral_value)]
    coverage = calc.calc_collateral_coverage(cov_rows, loan, collateral_value)
    coverage_ctx = {
        "rows": [{"basis": r["basis"], "value_money": _money(r["value"]),
                  "ltv": f'{r["ltv"]:.1f}' if r["ltv"] is not None else ""}
                 for r in coverage["rows"]],
        "equity_money": (_money(coverage["equity_cushion"])
                         if coverage["equity_cushion"] else ""),
    }

    # --- investor returns ---------------------------------------------------
    inv_rate = terms.investor_rate or rate
    inv_points = terms.investor_points or fee
    inv = calc.calc_investor_returns(loan, inv_rate, inv_points, months)

    def _inv_block(b):
        return {"points_money": _money2(b["points"]), "monthly_money": _money2(b["monthly"]),
                "annual_money": _money2(b["annual"]), "total_money": _money2(b["total"])}

    investor_ctx = {
        "rate": _fmt_pct(inv_rate), "points": _fmt_pct(inv_points),
        "unit_money": _money(inv["unit"]),
        "per_unit": _inv_block(inv["per_unit"]), "full": _inv_block(inv["full"]),
    }

    # --- narratives ---------------------------------------------------------
    has_pfs = bool(ed and (ed.total_assets or ed.assets or ed.liabilities))
    credit_text = _credit_text(ed, date.today())
    compliance = calc.calc_policy_compliance(
        lti=lti, ltv=ltv, dscr=dscr, cf=cf, mat_fmt=_fmt_long(terms.mat),
        has_maturity=bool(terms.mat), has_collateral=bool(collateral_value),
        has_insurance_policy=has_ins,
    )

    name = terms.name or "[Borrower Name]"
    employer = terms.employer or (ed.employer if ed else "") or ""
    asset_class = terms.asset_class or (ed.asset_class if ed else "") or ""
    purpose = terms.purpose or (ed.purpose if ed else "") or ""

    headline = terms.headline or (ed.headline if ed else "") or (
        f"{_money(loan)} {'First Lien ' if collateral_value else ''}"
        f"{'Interest-Only ' if is_io else ''}Loan \u2014 {name}"
        + (f", {employer}" if employer else ""))

    collateral_cell = collateral_desc or (
        "Pledged collateral" if collateral_value
        else "Unsecured \u2014 personal guarantee; UCC-1 on business assets")

    payments_desc = (
        f"Interest only, {_money2(monthly_payment)} per month; principal due at maturity"
        if is_io else
        f"Level payments of {_money2(monthly_payment)} per month, fully amortizing")

    exec_summary = (ed.exec_summary if ed and ed.exec_summary else
                    f"{name} is seeking a {_money(loan)} loan"
                    + (f" to {purpose.lower()}" if purpose else "")
                    + f". The facility has a {months}-month term at {_fmt_pct(rate)}%"
                    + (f" with {_fmt_pct(fee)} point(s)" if fee else "")
                    + (", interest only, with the full principal due at maturity."
                       if is_io else ", fully amortizing over the term.")
                    + (f" It is secured by {collateral_desc or 'the pledged collateral'}, "
                       f"valued at {_money(collateral_value)}, a {ltv:.1f}% loan-to-value."
                       if collateral_value else
                       " It is unsecured, resting on the Borrower's personal guarantee.")
                    + (f" Documented qualifying income is {_money(income)}."
                       if income else
                       " [No qualifying income figure was captured \u2014 enter it before"
                       " circulating this memo.]"))

    recommendation = (
        f"Approve a {_money(loan)}, {months}-month"
        + (", interest-only" if is_io else ", fully amortizing")
        + f" loan at {_fmt_pct(rate)}%"
        + (f" and {_fmt_pct(fee)} point(s)" if fee else "")
        + f" to {name}"
        + (f", secured by {collateral_desc or 'the pledged collateral'}"
           f" ({ltv:.1f}% LTV on {_money(collateral_value)})" if collateral_value else
           ", on an unsecured basis supported by the personal guarantee")
        + ". Approval is subject to the required documents above"
        + (f", in particular {terms.key_conditions}" if terms.key_conditions else "")
        + ".")

    context = {
        # 7 fixed pages (title/exec, loan summary, collateral+borrower,
        # financials, risks+repayment sources, sources+schedule,
        # investor+documents+recommendation), plus the two conditional ones.
        "page_count": 7 + (1 if has_pfs else 0) + (1 if _SHOW_COMPLIANCE else 0),
        "logo": _LOGO_PATH.read_text().strip(),
        "tbd": "[TBD]",
        "name": name,
        "headline": headline,
        "asset_class": asset_class,
        "purpose": purpose,
        "md_fmt": _fmt_short(date.today()),
        "md_long": _fmt_long(date.today()),
        "mat_fmt": _fmt_long(terms.mat),
        "months": months,
        "is_io": is_io,
        "rate": _fmt_pct(rate),
        "fee": _fmt_pct(fee),
        "fee_amt": _money(loan * fee / 100) if loan and fee else "[Fee]",
        "loan_money": _money(loan),
        "lti": f"{lti:.1f}",
        "income_basis_money": _money(income_basis),
        "contracted_money": _money(contracted_income) if contracted_income else "",
        "ltv": f"{ltv:.1f}" if collateral_value else "",
        "dscr": f"{dscr:.2f}" if dscr else "",
        "exec_summary": exec_summary,
        "property_owner": terms.property_owner or (ed.property_owner if ed else "") or "",
        "guarantors": (terms.guarantors or (ed.guarantors if ed else "")
                       or f"{name} \u2014 full personal guarantee"),
        "payments_desc": payments_desc,
        "prepayment": terms.prepayment or (ed.prepayment if ed else "") or "",
        "key_conditions": terms.key_conditions or (ed.key_conditions if ed else "") or "",
        "collateral_cell": collateral_cell,
        "collateral_desc": collateral_desc,
        "collateral_money": _money(collateral_value) if collateral_value else "",
        "collateral_basis": (ed.collateral_coverage[0].basis
                             if ed and ed.collateral_coverage else ""),
        "collateral_notes": ed.collateral_notes if ed else "",
        "coverage": coverage_ctx,
        "background_text": (
            ed.background_narrative if ed and ed.background_narrative
            else f"{name} is {terms.occupation or '[role]'} at "
                 f"{employer or '[Business / Employer]'}"
                 f"{(' (' + terms.industry + ')') if terms.industry else ''}."),
        "financial_notes": ed.financial_notes if ed else "",
        "fin": fin_ctx,
        "debt_service_money": _money(debt_service),
        "has_pfs": has_pfs,
        "cf_html": _cf_rows_html(cf),
        "pfs_html": _pfs_html(ed, facility_due,
                              collateral_value or income,
                              (collateral_desc or "Pledged Collateral")
                              if collateral_value else "Qualifying Income (Annual)",
                              date.today()),
        "credit_text": credit_text,
        "risks": _risk_rows(ed, has_collateral=bool(collateral_value), is_io=is_io,
                            has_insurance_policy=has_ins, profile=profile),
        "repay_primary": (ed.repay_primary if ed and ed.repay_primary else
                          {"business": "Operating cash flow of the Borrower's business.",
                           "investment": "Net income from the Borrower's income-producing assets.",
                           "consumer": "The Borrower's documented personal income."}[profile]),
        "repay_secondary": (ed.repay_secondary if ed and ed.repay_secondary else
                            ("Sale or refinance of the pledged collateral."
                             if collateral_value else
                             "Enforcement of the full personal guarantee.")),
        "repay_tertiary": (ed.repay_tertiary if ed and ed.repay_tertiary else
                           ("Foreclosure on the Lender's first-lien position and sale "
                            f"of the collateral; at {ltv:.1f}% LTV a forced sale need "
                            f"recover only about {ltv:.0f}% of appraised value to repay "
                            "the loan in full." if collateral_value else
                            "Liquidation of the Borrower's other personal assets, as "
                            "shown on the Personal Financial Statement.")),
        "uses_html": _uses_of_funds_html(uof),
        "repayment_intro": (
            f"The loan is interest only. The Borrower pays {_money2(monthly_payment)} each "
            f"month, and the final payment is that interest plus the full "
            f"{_money(loan)} principal." if is_io else
            f"The loan fully amortizes over {months} months in level payments of "
            f"{_money2(monthly_payment)}, interest accrued actual/360 on the declining "
            f"balance."),
        "repayment_bands": band_ctx,
        "repayment_totals": totals_ctx,
        "repayment_html": _repayment_html(rep_rows, rep_totals),
        "show_investor": bool(terms.show_investor_returns and loan and inv_rate),
        "investor": investor_ctx,
        "investor_differs": (abs(inv_rate - rate) > 1e-9
                             or abs(inv_points - fee) > 1e-9),
        "required_documents": list((ed.required_documents if ed else []) or []),
        "doc_list_html": _doc_list_html(filenames),
        "recommendation": recommendation,
        "show_compliance": _SHOW_COMPLIANCE,
        "compliance_html": _compliance_rows_html(compliance["rows"]),
        "exceptions_html": _exceptions_html(compliance),
        "provisional": compliance["provisional"],
        "profile": profile,
        "profile_label": calc.profile_label(profile),
        "income_notes": ed.income_notes if ed else "",
    }

    return _env.get_template("memo.html.j2").render(**context)


def _render_pdf_inprocess(html: str, footer_text: str,
                          page_numbers: bool) -> bytes:
    """Render the memo HTML to PDF using headless Chromium via Playwright.

    Chromium's print engine honors the template's own ``@page`` rules and
    ``@media print`` CSS, so the PDF matches the on-screen preview. Playwright
    ships its own browser (installed once with ``python -m playwright install
    chromium``), so no system GTK/Pango/Cairo libraries are required — which is
    why this replaced WeasyPrint, whose native deps aren't available on Windows.
    """
    from playwright.sync_api import sync_playwright  # imported lazily; heavy dep

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            # Letter size with explicit margins so Chromium reserves the bottom
            # margin for the native footer (display_header_footer). print_background
            # keeps the navy/gold styling. The footer template carries the real
            # "Page X of N" so numbering is always correct, and lives in the margin
            # so it never prints over the body. Margins match the template's @page.
            pdf = page.pdf(
                format="Letter",
                print_background=True,
                display_header_footer=True,
                header_template=_PDF_HEADER_TEMPLATE,
                footer_template=_pdf_footer_template(footer_text, page_numbers),
                margin={"top": "0.7in", "bottom": "0.6in", "left": "0.75in", "right": "0.75in"},
            )
        finally:
            browser.close()
    return pdf


def _render_pdf_subprocess(html: str, footer_text: str,
                           page_numbers: bool) -> bytes:
    """The same render, in a FRESH python process (the __main__ block below).

    Kept as belt-and-suspenders against Playwright launch failures. The
    original trigger — a false "Executable doesn't exist" (2026-07-10, 07-22,
    08-19, 08-25) — was ROOT-CAUSED on 2026-08-25: the browsers had been
    installed from inside a packaged (MSIX) app, so Windows virtualized them
    into that app's AppData sandbox, invisible to backends started at logon /
    by the keep-alive task (and to their children, which is why this retry
    could not help on 08-25). Fixed by copying them to the real
    %LOCALAPPDATA%/ms-playwright from a non-packaged process. If the error
    ever returns after a `playwright install`, check WHERE the install ran —
    never install browsers from inside a packaged app. Runs on the venv's
    python (the server itself may be a re-exec'd child of base Python312,
    which has no playwright).
    """
    import subprocess
    import tempfile

    from .venv_python import backend_python

    with tempfile.TemporaryDirectory() as td:
        in_path = Path(td) / "in.html"
        out_path = Path(td) / "out.pdf"
        in_path.write_text(html, encoding="utf-8")
        proc = subprocess.run(
            [backend_python(), "-m", "app.memo", str(in_path), str(out_path),
             footer_text, "1" if page_numbers else "0"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError((proc.stderr or "").strip()[-400:]
                               or f"subprocess exit {proc.returncode}")
        return out_path.read_bytes()


def render_pdf(html: str, footer_text: str = _DEFAULT_FOOTER_TEXT,
               page_numbers: bool = True) -> bytes:
    """Render to PDF in-process; on any failure retry once in a fresh process."""
    try:
        return _render_pdf_inprocess(html, footer_text, page_numbers)
    except Exception as exc:  # browser missing, launch failure, render error
        import logging
        logging.getLogger(__name__).warning(
            "In-process PDF render failed (%s) — retrying in a fresh process", exc)
        try:
            return _render_pdf_subprocess(html, footer_text, page_numbers)
        except Exception as exc2:
            raise RuntimeError(
                f"PDF rendering failed: {exc2}. If the browser is missing, install it "
                "with: python -m playwright install chromium"
            ) from exc2


_OFFICE_NS = (
    " xmlns:o='urn:schemas-microsoft-com:office:office'"
    " xmlns:w='urn:schemas-microsoft-com:office:word'"
    " xmlns='http://www.w3.org/TR/REC-html40'"
)

# Shared style for the footer paragraph (used in both the main and footer parts).
_MSO_FOOTER_STYLE = (
    "p.MsoFooter,li.MsoFooter,div.MsoFooter{margin:0;font-family:Arial,Helvetica,sans-serif;"
    "font-size:6.8pt;letter-spacing:.1em;color:#8a8a8a;text-transform:uppercase;"
    "border-top:.75pt solid #cdd3da;padding-top:4pt;tab-stops:right 7.3in;}"
)

# The repeating footer: brand on the left, live PAGE / NUMPAGES fields on the right.
def _mso_footer_paragraph(footer_text: str) -> str:
    return (
        f"<p class=MsoFooter>{footer_text}"
        "<span style='mso-tab-count:1'></span>"
        "Page <span style='mso-field-code:\" PAGE \"'></span> of "
        "<span style='mso-field-code:\" NUMPAGES \"'></span></p>"
    )


def render_word(html: str, footer_text: str = _DEFAULT_FOOTER_TEXT) -> bytes:
    """Package the memo as an MHTML (Web Archive) ``.doc`` so Microsoft Word
    shows a real repeating page footer at the bottom of every page.

    Word can't use the footer the PDF relies on. A single-file HTML ``.doc`` is a
    dead end here: an inline ``mso-element:footer`` div either leaks into the body
    (a blank "Page  of " above the real footer, because the fields only evaluate
    in the footer region) or, when hidden with ``display:none``, is dropped
    entirely. So we mirror what Word itself does on "Save as Single File Web
    Page": ship the footer in a SEPARATE MHTML part referenced by
    ``@page WordSection1`` via ``mso-footer:url(...)``. Living outside the body,
    it never renders inline, and its live PAGE / NUMPAGES fields give correct
    per-page numbers. The PDF path is unaffected.
    """
    meta = (
        "<head><meta name=ProgId content=Word.Document>"
        "<meta name=Generator content=\"Microsoft Word\">"
        "<!--[if gte mso 9]><xml><w:WordDocument>"
        "<w:View>Print</w:View><w:Zoom>100</w:Zoom></w:WordDocument></xml><![endif]-->"
    )

    # Named Word section whose bottom margin holds the footer (in footer.htm),
    # and a rule that suppresses the template's in-body .pg-footer rows.
    word_style = (
        "<style>"
        "@page WordSection1{size:8.5in 11.0in;margin:0.6in 0.6in 0.6in 0.6in;"
        "mso-header-margin:0.5in;mso-footer-margin:0.3in;"
        "mso-footer:url(\"footer.htm\") f1;mso-paper-source:0;}"
        "div.WordSection1{page:WordSection1;}"
        # Word's HTML engine ignores CSS var(), so tr.grand's navy background
        # (background:var(--navy)) is dropped while its color:#fff survives —
        # white text on a white page. Repaint the grand rows (Net Cash Flow,
        # Net Worth, Net to be Disbursed) for Word only: bold black text on the
        # same light grey the .total rows use. This block comes after the
        # template's <style>, so the cascade makes it win. PDF/HTML unaffected.
        "tbody tr.grand td{background:#eef1f4;color:#000;border-color:#cdd3da;}"
        + _MSO_FOOTER_STYLE +
        ".pg-footer{display:none !important;}"
        "</style>"
    )

    main = (
        html.replace('<html lang="en">', f'<html lang="en"{_OFFICE_NS}>', 1)
            .replace("<head>", meta, 1)
            .replace("</head>", word_style + "</head>", 1)
    )
    main = re.sub(r"(<body[^>]*>)", r"\1<div class=WordSection1>", main, count=1)
    main = main.replace("</body>", "</div></body>", 1)

    # The footer lives in its own part so it never appears in the body flow.
    footer_part = (
        f"<html{_OFFICE_NS}><head>"
        "<meta http-equiv=Content-Type content=\"text/html; charset=utf-8\">"
        "<style>" + _MSO_FOOTER_STYLE + "</style></head><body>"
        "<div style='mso-element:footer' id='f1'>" + _mso_footer_paragraph(footer_text) + "</div>"
        "</body></html>"
    )

    # Assemble the MHTML container. footer.htm resolves relative to main.htm, so
    # the @page mso-footer:url("footer.htm") points at the second part.
    boundary = "----=_NextPart_SouthRiverCreditMemo"
    mhtml = (
        "MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/related; boundary="{boundary}"; type="text/html"\r\n'
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Location: file:///C:/memo/main.htm\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "\r\n"
        f"{main}\r\n"
        f"--{boundary}\r\n"
        "Content-Location: file:///C:/memo/footer.htm\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "\r\n"
        f"{footer_part}\r\n"
        f"--{boundary}--\r\n"
    )
    return mhtml.encode("utf-8")


if __name__ == "__main__":
    # Subprocess entry for _render_pdf_subprocess:
    #   python -m app.memo <in.html> <out.pdf> <footer_text> <1|0>
    import sys as _sys
    _in, _out, _footer, _nums = _sys.argv[1:5]
    _pdf = _render_pdf_inprocess(Path(_in).read_text(encoding="utf-8"),
                                 _footer, _nums == "1")
    Path(_out).write_bytes(_pdf)
