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
        ("1", "Contract Receivable (Guaranteed Salary)", cf["salary_income"], ""),
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


def _pfs_html(ed: Extraction | None, facility_due: float, salary: float,
              as_of: date | None = None, contract_remaining: float = 0) -> str:
    bs = calc.calc_balance_sheet(ed, facility_due, as_of, contract_remaining)
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
        mark = bs.get("contract_mark")
        if mark:
            # Say on the statement that this asset is not the figure the PFS
            # reports, the same way the roll-forward discloses the liabilities.
            out.append(
                f'<tr class="note"><td colspan="2"><em>{mark["label"]} is restated '
                f'from the {_money(mark["reported"])} reported on the Personal '
                f'Financial Statement to the {_money(mark["restated"])} remaining '
                f'contract value confirmed at underwriting.</em></td></tr>')
        if rf.get("applied"):
            # Rule 15 — say on the statement itself that these are not the
            # figures the PFS reports, so no reader mistakes them for verbatim.
            out.append(f'<tr class="note"><td colspan="2"><em>{_rollforward_footnote(rf)}'
                       f'</em></td></tr>')
    else:
        out.append(
            f'<tr><td>Contract Receivable</td><td class="num-col">{_money(salary)}</td></tr>'
            f'<tr class="total"><td>Total Assets</td><td class="num-col">{_money(salary)}</td></tr>'
            f'<tr><td>Liabilities</td><td></td></tr>'
            f'<tr><td><em>Proposed Facility (includes interest)</em></td>'
            f'<td class="num-col"><em>{_money(facility_due)}</em></td></tr>'
            f'<tr class="total"><td>Total Liabilities (incl. Proposed Facility)</td>'
            f'<td class="num-col">{_money(facility_due)}</td></tr>'
            f'<tr class="grand"><td>Net Worth (Total Assets &minus; Total Liabilities)</td>'
            f'<td class="num-col">{_signed(salary - facility_due)}</td></tr>'
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


_DUP_PROFESSIONAL_RE = re.compile(r"\bprofessional(?:\s+professional\b)+", re.I)


def _dedupe_professional(html: str) -> str:
    """Collapse any run of consecutive 'professional' words down to one.

    The memo phrases the borrower as "a Professional <sport> player", and the
    sport value is already normalized (calc.normalize_sport) so it can't carry
    its own "Professional". This is the final, source-agnostic guarantee: even
    if a narrative captured from the documents (sponsorship/contract notes)
    contains the doubled word, the rendered memo never reads
    "Professional Professional ...". Casing of the first word is preserved.
    """
    return _DUP_PROFESSIONAL_RE.sub(lambda m: m.group(0).split()[0], html)


def render_html(terms: DealTerms, ed: Extraction | None, filenames: list[str] | None = None) -> str:
    """Render the full credit memo as an HTML string."""
    filenames = filenames or []
    # Confirmed deal terms win; otherwise fall back to what was pulled from the
    # documents (same pattern as salary), so the memo reflects the term sheet.
    loan = terms.loan or (ed.loan_amount if ed else 0) or 0
    rate = terms.rate or (ed.interest_rate_pct if ed else 0) or 0
    fee = terms.fee or (ed.origination_fee_pct if ed else 0) or 0
    salary = terms.salary or (ed.salary if ed else 0) or 0
    sport = calc.normalize_sport(terms.sport)

    amort = None
    if terms.fund and terms.mat and loan and rate:
        amort = calc.calc_amort(loan, rate, terms.fund, terms.mat)

    cf = calc.build_cash_flow(ed, amort, loan, salary)
    facility_due = calc.facility_total(ed, amort, loan)
    # Section I's "advance against ..." figure and the LTC's guaranteed
    # earnings use the TOTAL REMAINING contract value when the documents
    # provide one (Lauren, 2026-07-06), falling back to the season salary.
    # The cash flow and Section VII stay on the season salary.
    # A remaining-contract value confirmed on the form wins over the extracted
    # one (same ``terms.x or ed.x`` precedence as every other term above), so
    # the underwriter can set the LTC basis when the documents are stale or
    # a new contract supersedes them.
    contract_remaining = (terms.contract_remaining
                          or (ed.contract_remaining if ed else 0) or 0)
    guar_basis = contract_remaining or salary
    ltc = calc.calc_ltc(loan, guar_basis)
    uof = calc.calc_uses_of_funds(ed.uses_of_funds if ed else None, loan, fee)
    # Rule 16: the memo claims a DDD policy ONLY when the documents' own
    # disbursement carries the premium line. When it doesn't, every DDD
    # mention drops out — coversheet row/collateral, mitigants, both Tertiary
    # repayment-source cards, the Section II bullet, Section III's repayment
    # cell, and the injury FAQ's assigned-policy bullet.
    has_ddd = calc.has_ddd_insurance(ed.uses_of_funds if ed else None)

    # Deal Summary & Policy Compliance coversheet (page 1). Runs on the same
    # LTC / cash flow figures the memo body reports, so the checklist can
    # never disagree with the sections behind it.
    credit_text = _credit_text(ed, date.today())
    compliance = calc.calc_policy_compliance(
        ltc=ltc, cf=cf, mat_fmt=_fmt_long(terms.mat),
        has_maturity=bool(terms.mat), has_ddd=has_ddd,
    )

    amort_for_tpl = amort or {"rows": [], "interest": 0, "balloon": 0, "months": 0}

    # Section X repayment schedule: prefer the schedule captured from the uploaded
    # documents; otherwise compute one (interest paid monthly, principal balloon)
    # from the deal terms. Empty when neither is available -> placeholder row.
    if ed and ed.repayment_schedule:
        rep_rows = [
            {
                "num": i + 1,
                "date": r.date or "—",
                "interest": r.interest,
                "principal": r.principal,
                "total": r.total or (r.interest + r.principal),
            }
            for i, r in enumerate(ed.repayment_schedule)
        ]
        rep_totals = {
            "total_interest": sum(r["interest"] for r in rep_rows),
            "total_principal": sum(r["principal"] for r in rep_rows),
            "total_payment": sum(r["total"] for r in rep_rows),
        }
    elif terms.fund and terms.mat and loan and rate:
        rep_sched = calc.calc_repayment_schedule(loan, rate, terms.fund, terms.mat)
        rep_rows = rep_sched["rows"]
        rep_totals = rep_sched
    else:
        rep_rows, rep_totals = [], {}

    context = {
        "logo": _LOGO_PATH.read_text().strip(),
        "name": terms.name or "[Borrower Name]",
        "team": terms.team or "[Team Name]",
        "league": terms.league or "[League]",
        "sport": sport or "[sport]",
        "addr": terms.addr or "[Address]",
        "phone": terms.phone or "[Phone]",
        "dob": terms.dob or "[DOB]",
        "ssn": calc.mask_ssn(terms.ssn) or "[XXX-XX-####]",
        "dl": terms.dl or "[DL#]",
        "agent": terms.agent or "",
        "rate": _fmt_pct(rate),
        "fee": _fmt_pct(fee),
        "fee_amt": _money(loan * fee / 100) if loan and fee else "[Fee]",
        "mat_fmt": _fmt_long(terms.mat),
        "md_fmt": _fmt_short(date.today()),
        "loan_type": terms.loan_type,
        "months": calc.loan_term_months(ed, amort),
        "loan_money": _money(loan),
        "salary_money": _money(salary),
        "guar_basis_money": _money(guar_basis),
        "contract_remaining_money": (_money(contract_remaining)
                                     if contract_remaining else ""),
        "interest_money": _money(amort_for_tpl["interest"]),
        "ltc": f"{ltc:.1f}",
        "sponsor_text": (ed.sponsorship_narrative if ed and ed.sponsorship_narrative
                         else f"{terms.name or '[Borrower Name]'} is a "
                              f"{'Collegiate' if terms.is_college else 'Professional'} "
                              f"{sport or '[sport]'} player for the "
                              f"{terms.team or '[Team Name]'} of the {terms.league or '[League]'}."),
        # Rule 15: when the PFS was rolled forward, the drafted explanation is
        # appended here — Lauren documents the roll-forward in this paragraph.
        "credit_text": credit_text,
        "compliance_html": _compliance_rows_html(compliance["rows"]),
        "exceptions_html": _exceptions_html(compliance),
        "has_ddd": has_ddd,
        # Rule 20: college mode swaps Section IV's FAQ and the "Professional"
        # phrasing for college language. Underwriter-set only.
        "is_college": bool(terms.is_college),
        # Section VII (Contract Analysis) only. Section V (Project Sponsorship)
        # deliberately does NOT show the contract notes — Lauren, 2026-07-06.
        "contract_notes": ed.contract_notes if ed else "",
        "cf_html": _cf_rows_html(cf),
        "uses_html": _uses_of_funds_html(uof),
        "pfs_html": _pfs_html(ed, facility_due, salary, date.today(),
                              contract_remaining),
        "repayment_html": _repayment_html(rep_rows, rep_totals),
        "doc_list_html": _doc_list_html(filenames),
    }

    html = _env.get_template("memo.html.j2").render(**context)
    return _dedupe_professional(html)


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

    from .research import _backend_python

    with tempfile.TemporaryDirectory() as td:
        in_path = Path(td) / "in.html"
        out_path = Path(td) / "out.pdf"
        in_path.write_text(html, encoding="utf-8")
        proc = subprocess.run(
            [_backend_python(), "-m", "app.memo", str(in_path), str(out_path),
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
