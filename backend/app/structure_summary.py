"""Render the structuring run as a one-look PDF for credit.

The Structure tab is an interactive tool; this is what gets SENT — a standalone
summary of the options considered, why one is recommended, and the cash flow
behind it. Same house design and the same Playwright pipeline as the credit
memorandum, so it sits alongside one without looking foreign.

Deliberately shows EVERY candidate, including the ones that fail. A structure
memo that only shows the answer hides the argument, and the argument is the
part credit needs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import memo as memo_service
from .structure_models import StructureInputs, StructureResult

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = Path(__file__).parent / "logo.txt"

# Autoescape ON, unlike the memo template: borrower names, extraction notes and
# the free-text exit label all reach this template as user/model input.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(default=True, default_for_string=True),
)

_FOOTER_TEXT = "South River Capital — Proposed Loan Structure"


def _money(v, dp: int = 0) -> str:
    """House convention: negatives in parentheses, as the memo renders them
    ("($649,402)"), never "$-649,402"."""
    if v is None:
        return "—"
    if v < 0:
        return f"(${abs(v):,.{dp}f})"
    return f"${v:,.{dp}f}"


def _fmt_date(d) -> str:
    return d.strftime("%B %d, %Y") if d else "—"


def build_context(result: StructureResult, inputs: StructureInputs) -> dict:
    """Flatten the scored result into template-ready strings."""
    flow = result.cash_flow
    scale = max([abs(m.available) for m in flow] or [1.0]) or 1.0

    rows = []
    for m in flow:
        # Bars are drawn from a centre line: positive right, negative left, so
        # the shape of the year reads at a glance.
        width = abs(m.available) / scale * 50.0
        rows.append({
            "label": m.label,
            "in_season": m.in_season,
            "available": m.available,
            "cumulative": m.cumulative,
            "gross_money": _money(m.gross),
            "available_money": _money(m.available),
            "cumulative_money": _money(m.cumulative),
            "bar_pct": round(width, 2),
            "bar_off": round(50.0 - width, 2) if m.available < 0 else 50.0,
        })

    cands = []
    for c in result.candidates:
        cands.append({
            "name": c.name,
            "rationale": c.rationale,
            "passes": c.passes,
            "recommended": c.recommended,
            "warnings": c.warnings,
            "payments": [{
                "date": p.date,
                "coverage": p.coverage,
                "month_available": p.month_available,
                "interest_money": _money(p.interest),
                "principal_money": _money(p.principal),
                "total_money": _money(p.total),
                "avail_money": _money(p.month_available),
            } for p in c.payments],
            "maturity_fmt": _fmt_date(c.maturity_date),
            "interest_money": _money(c.total_interest),
            "reserve_money": _money(c.interest_reserve),
            "interest_reserve": c.interest_reserve,
            "min_coverage": c.min_coverage,
            "min_cushion_coverage": c.min_cushion_coverage,
            "tightest_month": c.tightest_month,
        })

    recommended = next((c for c in cands if c["recommended"]), None)
    avail = result.annual_available
    leverage = (f"{inputs.loan_amount / avail:.1f}x" if avail > 0 else "—")
    cad = result.cadence_used

    # No team contract: propose_structures already ran on zeroed salary, but
    # THIS context is built from the route's original inputs — so the header
    # must not show a stale typed salary or team as if a contract backed them.
    no_contract = bool(getattr(inputs, "no_team_contract", False))

    return {
        "logo": _LOGO_PATH.read_text().strip(),
        "today": date.today().strftime("%B %d, %Y"),
        "borrower": inputs.borrower_name,
        "team": "None — no team contract" if no_contract else inputs.team,
        "league": "" if no_contract else inputs.league,
        "loan_money": _money(inputs.loan_amount),
        "rate": f"{inputs.interest_rate:g}",
        "points": f"{inputs.origination_fee_pct:g}" if inputs.origination_fee_pct else "",
        "funding": _fmt_date(inputs.funding_date),
        "salary_money": "—" if no_contract else _money(inputs.salary),
        "guaranteed": False if no_contract else inputs.salary_guaranteed,
        "presented": inputs.presented_type,
        "leverage": leverage,
        "annual_available_money": _money(avail),
        "cadence_label": "No salary cadence" if no_contract else (cad.label or "—"),
        "cadence_source": ("no team contract" if no_contract
                           else f"{cad.league} pay cadence" if cad.league
                           else "pay cadence on file"),
        "min_coverage": inputs.min_coverage or 1.0,
        "candidates": cands,
        "recommended": recommended,
        "cash_flow": rows,
        "notes": result.notes,
    }


def render_html(result: StructureResult, inputs: StructureInputs) -> str:
    return _env.get_template("structure_summary.html.j2").render(
        **build_context(result, inputs))


def render_pdf(html: str) -> bytes:
    """Same Chromium pipeline as the memo, with this document's footer."""
    return memo_service.render_pdf(html, footer_text=_FOOTER_TEXT)


def filename(inputs: StructureInputs) -> str:
    who = "".join(ch if ch.isalnum() else "_" for ch in (inputs.borrower_name or "Borrower"))
    return f"Proposed_Structure_{who.strip('_') or 'Borrower'}.pdf"
