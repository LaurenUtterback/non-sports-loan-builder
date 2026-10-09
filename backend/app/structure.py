"""Deal structuring — propose a repayment structure from the borrower's cash flow.

Every other builder in this app takes the loan's structure as GIVEN. This module
derives it. It projects the athlete's month-by-month cash flow from how their
league actually pays, then scores candidate structures against that projection.

The two variables that decide a structure
-----------------------------------------
1. TIMING   — when cash actually arrives (league pay cadence, bonus dates).
2. CERTAINTY— whether that cash is contractually locked or contingent.

    dated & certain + recurring  -> amortize on the pay cadence
    dated & certain + one event  -> bullet maturing just after the event
    undated / contingent         -> bullet + interest reserve (nothing to service)

Non-guaranteed income argues for amortizing FAST while the money flows (a cut or
injury ends it); guaranteed money can support a balloon.

Rules encoded here
------------------
S1. Taxes follow the CHECKS: 45% of each month's gross (calculations rule 1,
    distributed by pay date rather than annually). Bonus cash is taxed too.
S2. Ordinary living expenses are 10% of annual salary + other income, spread
    EVENLY over 12 months (calculations rule 2's annual total, but living costs
    do not stop in the offseason, so they are not tied to pay timing).
S3. The salary spread is the GUARANTEED season compensation only (rule 9), and
    it stops after ``contract_end`` — no season is projected past the contract.
S4. Coverage is reported two ways. ``coverage`` is the strict same-month test
    (payment vs that month's cash). ``cushion_coverage`` tests against the
    running surplus, because an athlete banks in-season money to live on in the
    offseason. A structure that fails same-month but passes on cushion is viable
    only if the borrower actually reserves — that is a credit judgment, so both
    numbers are always shown rather than collapsed into one verdict.
S5. A maturity date in a DRY month (no salary) is always flagged. The balloon
    should land where the cash is.
S6. Interest accrues actual/365, consistent with calculations.calc_amort.

The agent-commission knob defaults to 0.0 so the projection ties out EXACTLY to
the credit memo's annual cash flow. Setting it is an underwriter override that
deliberately diverges from the memo.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .calculations import LIVING_RATE, TAX_RATE
from .structure_models import (
    BonusEvent,
    CashFlowMonth,
    LeagueCadence,
    StructureCandidate,
    StructureInputs,
    StructurePayment,
    StructureResult,
)


# --- League pay cadences ---------------------------------------------------
#
# Defaults only. Pay elections vary by player and the contract always wins —
# every field is editable per deal in the UI.

LEAGUE_CADENCES: dict[str, LeagueCadence] = {
    "NFL": LeagueCadence(
        league="NFL",
        label="Weekly game checks across the regular season",
        season_start_month=9, season_start_day=7,
        season_end_month=1, season_end_day=4,
        pay_frequency="weekly",
        notes="Base salary is paid in ~18 weekly game checks, Sept-early Jan. "
              "Roughly seven dry months follow. Signing/roster/workout bonuses "
              "are paid separately on their own dates.",
    ),
    "NBA": LeagueCadence(
        league="NBA",
        label="Semi-monthly, Nov 15 - Apr 30 (default election)",
        season_start_month=11, season_start_day=15,
        season_end_month=4, season_end_day=30,
        pay_frequency="semimonthly",
        notes="Default is 24 semi-monthly payments across the season. Players "
              "may elect a stretched 12-month schedule - confirm the election, "
              "do not assume it.",
    ),
    "MLB": LeagueCadence(
        league="MLB",
        label="Semi-monthly across the regular season",
        season_start_month=4, season_start_day=1,
        season_end_month=9, season_end_day=30,
        pay_frequency="semimonthly",
        notes="Paid semi-monthly during the regular season only. Signing "
              "bonuses and deferred compensation are separate.",
    ),
    "NHL": LeagueCadence(
        league="NHL",
        label="Semi-monthly in season; signing bonus often July 1",
        season_start_month=10, season_start_day=15,
        season_end_month=4, season_end_day=15,
        pay_frequency="semimonthly",
        bonus_month=7, bonus_day=1,
        notes="Signing-bonus installments are frequently paid July 1 and are "
              "often the largest single cash event of the year - a natural "
              "balloon date. Escrow holdback reduces the net check.",
    ),
    "MLS": LeagueCadence(
        league="MLS",
        label="Monthly, year round",
        season_start_month=1, season_start_day=1,
        season_end_month=12, season_end_day=31,
        pay_frequency="monthly",
        year_round=True,
        notes="Year-round monthly salary supports a conventional amortizing "
              "schedule.",
    ),
}

# League name aliases seen in extractions.
_ALIASES = {
    "NATIONAL FOOTBALL LEAGUE": "NFL",
    "FOOTBALL": "NFL",
    "NATIONAL BASKETBALL ASSOCIATION": "NBA",
    "BASKETBALL": "NBA",
    "MAJOR LEAGUE BASEBALL": "MLB",
    "BASEBALL": "MLB",
    "NATIONAL HOCKEY LEAGUE": "NHL",
    "HOCKEY": "NHL",
    "ICE HOCKEY": "NHL",
    "MAJOR LEAGUE SOCCER": "MLS",
    "SOCCER": "MLS",
}

_DEFAULT_CADENCE = LeagueCadence(
    league="",
    label="Monthly, year round (no league cadence on file)",
    pay_frequency="monthly",
    year_round=True,
    notes="No league pay cadence on file - defaulting to level monthly income. "
          "Set the season window and pay frequency from the contract.",
)


def league_cadence(league: Optional[str]) -> LeagueCadence:
    """Default pay cadence for a league name. Never raises — unknown leagues
    fall back to level monthly income with a note."""
    key = (league or "").strip().upper()
    if not key:
        return _DEFAULT_CADENCE.model_copy()
    if key in LEAGUE_CADENCES:
        return LEAGUE_CADENCES[key].model_copy()
    if key in _ALIASES:
        return LEAGUE_CADENCES[_ALIASES[key]].model_copy()
    cad = _DEFAULT_CADENCE.model_copy()
    cad.league = league or ""
    return cad


# --- Season windows and pay dates ------------------------------------------

def _wraps_year(cad: LeagueCadence) -> bool:
    """True when the season runs across a calendar-year boundary (NFL, NBA, NHL)."""
    return (cad.season_end_month, cad.season_end_day) < (cad.season_start_month, cad.season_start_day)


def season_windows(cad: LeagueCadence, start: date, end: date) -> list[tuple[date, date]]:
    """Every (season_start, season_end) pair overlapping [start, end]."""
    windows: list[tuple[date, date]] = []
    for year in range(start.year - 1, end.year + 2):
        try:
            s = date(year, cad.season_start_month, cad.season_start_day)
        except ValueError:
            continue
        end_year = year + 1 if _wraps_year(cad) else year
        try:
            e = date(end_year, cad.season_end_month, cad.season_end_day)
        except ValueError:
            continue
        if e >= start and s <= end:
            windows.append((s, e))
    return windows


def _step_dates(s: date, e: date, freq: str) -> list[date]:
    """Pay dates inside one season window."""
    out: list[date] = []
    if freq == "weekly":
        d = s
        while d <= e:
            out.append(d)
            d += timedelta(days=7)
        return out

    if freq == "semimonthly":
        y, m = s.year, s.month
        while True:
            for day in (1, 15):
                try:
                    d = date(y, m, day)
                except ValueError:
                    continue
                if s <= d <= e:
                    out.append(d)
            if (y, m) >= (e.year, e.month):
                break
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    # monthly — pay on the season start's day-of-month, clamped to 28
    y, m = s.year, s.month
    while True:
        d = date(y, m, min(s.day, 28))
        if s <= d <= e:
            out.append(d)
        if (y, m) >= (e.year, e.month):
            break
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def pay_dates(cad: LeagueCadence, start: date, end: date,
              contract_end: Optional[date] = None) -> list[tuple[date, float]]:
    """Salary pay dates in [start, end] with each one's share of a season's salary.

    Returns (date, fraction_of_one_season_salary). Rule S3: nothing is projected
    past ``contract_end``.
    """
    out: list[tuple[date, float]] = []
    for s, e in season_windows(cad, start, end):
        if contract_end and s > contract_end:
            continue
        dates = _step_dates(s, e, cad.pay_frequency)
        if not dates:
            continue
        share = 1.0 / len(dates)
        for d in dates:
            if contract_end and d > contract_end:
                continue
            if start <= d <= end:
                out.append((d, share))
    return sorted(out)


# --- Cash flow projection --------------------------------------------------

def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, min(d.day, 28))


def project_cash_flow(inputs: StructureInputs,
                      cad: Optional[LeagueCadence] = None,
                      months: Optional[int] = None) -> list[CashFlowMonth]:
    """Month-by-month cash available for the proposed facility.

    Rules S1-S3. The window runs from the funding month through the loan's
    maturity (target term, or the expected exit, whichever is later) plus one
    month of tail.
    """
    cad = cad or resolve_cadence(inputs)
    start = inputs.funding_date or date.today()
    span = months or _projection_months(inputs)
    end = _add_months(start, span)

    # Bucket every cash event into its month.
    salary_by_month: dict[tuple[int, int], float] = {}
    for d, share in pay_dates(cad, start, end, inputs.contract_end):
        salary_by_month[_month_key(d)] = salary_by_month.get(_month_key(d), 0.0) + share * inputs.salary

    bonus_by_month: dict[tuple[int, int], float] = {}
    for b in inputs.bonus_events:
        if b.date and start <= b.date <= end:
            bonus_by_month[_month_key(b.date)] = bonus_by_month.get(_month_key(b.date), 0.0) + (b.amount or 0.0)

    # Other income is treated as level monthly unless a bonus event carries it.
    other_monthly = (inputs.other_income or 0.0) / 12.0
    # Rule S2 — living expenses spread evenly, off the memo's income basis.
    annual_income_basis = (inputs.salary or 0.0) + (inputs.other_income or 0.0)
    living_monthly = annual_income_basis * LIVING_RATE / 12.0
    other_debt_monthly = (inputs.other_debt_annual or 0.0) / 12.0

    rows: list[CashFlowMonth] = []
    cumulative = 0.0
    cursor = date(start.year, start.month, 1)
    for _ in range(span + 1):
        key = (cursor.year, cursor.month)
        sal = salary_by_month.get(key, 0.0)
        bon = bonus_by_month.get(key, 0.0)
        gross = sal + bon + other_monthly
        taxes = gross * TAX_RATE                       # rule S1
        agent = gross * (inputs.agent_pct or 0.0) / 100.0
        available = gross - taxes - agent - living_monthly - other_debt_monthly
        cumulative += available
        rows.append(CashFlowMonth(
            year=cursor.year, month=cursor.month,
            label=f"{cursor.strftime('%b')} {cursor.year}",
            in_season=sal > 0,
            gross=round(gross, 2), salary=round(sal, 2),
            bonus=round(bon, 2), other=round(other_monthly, 2),
            taxes=round(taxes, 2), living=round(living_monthly, 2),
            other_debt=round(other_debt_monthly, 2),
            available=round(available, 2), cumulative=round(cumulative, 2),
        ))
        cursor = _add_months(date(cursor.year, cursor.month, 1), 1)
    return rows


def _projection_months(inputs: StructureInputs) -> int:
    term = max(int(inputs.target_term_months or 0), 1)
    if inputs.expected_exit_date and inputs.funding_date:
        months_to_exit = ((inputs.expected_exit_date.year - inputs.funding_date.year) * 12
                          + inputs.expected_exit_date.month - inputs.funding_date.month)
        term = max(term, months_to_exit + 1)
    return term + 1


def resolve_cadence(inputs: StructureInputs) -> LeagueCadence:
    """The cadence actually used: an explicit override, else the league default."""
    return inputs.cadence or league_cadence(inputs.league)


# --- Interest --------------------------------------------------------------

def _interest(balance: float, rate: float, days: int) -> float:
    """Actual/365 interest — rule S6, same basis as calculations.calc_amort."""
    return balance * (rate / 100.0 / 365.0) * days


def _fmt(d: date) -> str:
    # integer day + %b/%y — never the glibc-only "%-d" (ValueError on Windows)
    return f"{d.day}-{d.strftime('%b')}-{d.strftime('%y')}"


# --- Candidate structures --------------------------------------------------
#
# Each builder returns an UNSCORED candidate; _score fills the coverage metrics.

def _maturity(inputs: StructureInputs) -> date:
    fund = inputs.funding_date or date.today()
    return _add_months(fund, max(int(inputs.target_term_months or 0), 1))


def _bullet_reserve(inputs: StructureInputs) -> StructureCandidate:
    """No payments until the exit event; interest funded from proceeds.

    The bottom-right cell of the matrix: there is no cash flow to service, so an
    amortizing schedule would just be paying the loan with its own proceeds.
    Maturity is set a 30-day buffer past the expected event.
    """
    fund = inputs.funding_date or date.today()
    if inputs.expected_exit_date:
        mat = inputs.expected_exit_date + timedelta(days=30)
    else:
        mat = _maturity(inputs)
    days = max((mat - fund).days, 1)
    interest = _interest(inputs.loan_amount, inputs.interest_rate, days)
    label = inputs.expected_exit_label or "expected exit"

    return StructureCandidate(
        key="bullet_reserve",
        name="Bullet + interest reserve",
        amortization_type="balloon",
        rationale=(
            f"No scheduled payments. Interest of ${interest:,.0f} is reserved out of "
            f"proceeds at funding and the facility retires in a single payment 30 "
            f"days after the {label}. Use when the repayment source is a one-time "
            f"event rather than recurring income — there is nothing to service "
            f"until it lands, so an amortizing schedule would fund payments out of "
            f"the loan itself."
        ),
        term_months=max(((mat.year - fund.year) * 12 + mat.month - fund.month), 1),
        maturity_date=mat,
        interest_reserve=round(interest, 2),
        payments=[StructurePayment(
            date=_fmt(mat), iso_date=mat,
            interest=round(interest, 2), principal=round(inputs.loan_amount, 2),
            total=round(inputs.loan_amount + interest, 2), is_balloon=True,
        )],
    )


def _interest_only_balloon(inputs: StructureInputs) -> StructureCandidate:
    """The house default today: monthly interest, principal as a balloon.

    Mirrors calculations.calc_repayment_schedule so this candidate is a true
    like-for-like baseline against what the tool already produces.
    """
    fund = inputs.funding_date or date.today()
    mat = _maturity(inputs)
    months = max((mat.year - fund.year) * 12 + mat.month - fund.month, 1)
    monthly_interest = (inputs.loan_amount or 0.0) * (inputs.interest_rate / 100.0) / 12.0

    payments: list[StructurePayment] = []
    for i in range(1, months + 1):
        d = _add_months(fund, i)
        is_balloon = i == months
        prin = inputs.loan_amount if is_balloon else 0.0
        payments.append(StructurePayment(
            date=_fmt(d), iso_date=d,
            interest=round(monthly_interest, 2), principal=round(prin, 2),
            total=round(monthly_interest + prin, 2), is_balloon=is_balloon,
        ))

    return StructureCandidate(
        key="interest_only_balloon",
        name="Interest-only + balloon",
        amortization_type="interest_only",
        rationale=(
            "Interest paid monthly, principal repaid in full at maturity. This is "
            "South River's current default structure and is shown as the baseline. "
            "It ignores pay timing — the monthly interest falls due in offseason "
            "months as readily as in-season ones."
        ),
        term_months=months,
        maturity_date=mat,
        payments=payments,
    )


def _seasonal_amortization(inputs: StructureInputs, cad: LeagueCadence) -> Optional[StructureCandidate]:
    """Principal amortized only across the months the checks actually land.

    One payment per in-season month (the last pay date in that month), so the
    schedule is servicable rather than following every weekly NFL game check.
    Interest accrues actual/365 on the outstanding balance. Anything not retired
    by maturity is a balloon on the final in-season payment.
    """
    if cad.year_round:
        return None    # nothing seasonal to shape around

    fund = inputs.funding_date or date.today()
    mat = _maturity(inputs)
    dates = pay_dates(cad, fund, mat, inputs.contract_end)
    if not dates:
        return None

    # collapse to one payment per in-season month
    by_month: dict[tuple[int, int], date] = {}
    for d, _share in dates:
        by_month[_month_key(d)] = max(by_month.get(_month_key(d), d), d)
    pay_days = sorted(by_month.values())
    if len(pay_days) < 2:
        return None

    principal_each = (inputs.loan_amount or 0.0) / len(pay_days)
    balance = inputs.loan_amount or 0.0
    last = fund
    payments: list[StructurePayment] = []
    for i, d in enumerate(pay_days, start=1):
        interest = _interest(balance, inputs.interest_rate, (d - last).days)
        is_last = i == len(pay_days)
        prin = balance if is_last else min(principal_each, balance)
        balance = round(balance - prin, 2)
        payments.append(StructurePayment(
            date=_fmt(d), iso_date=d,
            interest=round(interest, 2), principal=round(prin, 2),
            total=round(interest + prin, 2), is_balloon=is_last and prin > principal_each * 1.5,
        ))
        last = d

    final = pay_days[-1]
    return StructureCandidate(
        key="seasonal_amortization",
        name="Seasonal amortization",
        amortization_type="fully_amortized",
        rationale=(
            f"Payments fall only in the {len(pay_days)} months the borrower is "
            f"actually paid ({cad.label.lower()}), with nothing due in the "
            f"offseason. Principal amortizes across those months and interest "
            f"accrues actual/365 on the declining balance. This is the structure "
            f"that matches the cash flow — and it collects while the money is "
            f"flowing, which matters most when the income is not guaranteed."
        ),
        term_months=max((final.year - fund.year) * 12 + final.month - fund.month, 1),
        maturity_date=final,
        payments=payments,
    )


def _fully_amortized(inputs: StructureInputs, cad: LeagueCadence) -> Optional[StructureCandidate]:
    """Level monthly payments — only offered when income is year-round."""
    if not cad.year_round:
        return None

    fund = inputs.funding_date or date.today()
    mat = _maturity(inputs)
    months = max((mat.year - fund.year) * 12 + mat.month - fund.month, 1)
    principal = inputs.loan_amount or 0.0
    r = (inputs.interest_rate or 0.0) / 100.0 / 12.0
    if r > 0:
        pmt = principal * r / (1 - (1 + r) ** -months)
    else:
        pmt = principal / months

    balance = principal
    payments: list[StructurePayment] = []
    for i in range(1, months + 1):
        d = _add_months(fund, i)
        interest = balance * r
        prin = pmt - interest
        if i == months:
            prin = balance          # retire the remaining balance exactly
        balance = round(balance - prin, 2)
        payments.append(StructurePayment(
            date=_fmt(d), iso_date=d,
            interest=round(interest, 2), principal=round(prin, 2),
            total=round(interest + prin, 2),
        ))

    return StructureCandidate(
        key="fully_amortized",
        name="Fully amortized (level monthly)",
        amortization_type="fully_amortized",
        rationale=(
            "Equal monthly payments retiring the facility over the term. Viable "
            "here because the borrower is paid year round, so no payment lands in "
            "a month without income."
        ),
        term_months=months,
        maturity_date=mat,
        payments=payments,
    )


# --- Scoring ---------------------------------------------------------------

def _score(cand: StructureCandidate, cash_flow: list[CashFlowMonth],
           inputs: StructureInputs) -> StructureCandidate:
    """Fill coverage metrics and warnings. Rules S4 and S5."""
    by_month = {(m.year, m.month): m for m in cash_flow}

    min_cov = float("inf")
    min_cushion = float("inf")
    tightest = ""
    balance = 0.0          # running surplus net of payments made so far
    paid_idx = 0

    for p in cand.payments:
        if not p.iso_date or p.total <= 0:
            continue
        m = by_month.get((p.iso_date.year, p.iso_date.month))
        avail = m.available if m else 0.0
        cumulative = m.cumulative if m else 0.0
        p.month_available = round(avail, 2)
        p.coverage = round(avail / p.total, 3) if p.total else 0.0
        # cushion = everything banked through this month, less payments already made
        banked = cumulative - balance
        p.cushion_coverage = round(banked / p.total, 3) if p.total else 0.0
        balance += p.total
        if p.coverage < min_cov:
            min_cov = p.coverage
            tightest = m.label if m else p.date
        min_cushion = min(min_cushion, p.cushion_coverage)
        paid_idx += 1

    cand.total_interest = round(sum(p.interest for p in cand.payments), 2)
    cand.total_principal = round(sum(p.principal for p in cand.payments), 2)
    cand.total_paid = round(cand.total_interest + cand.total_principal, 2)
    cand.points_amount = round((inputs.loan_amount or 0.0)
                               * (inputs.origination_fee_pct or 0.0) / 100.0, 2)
    cand.min_coverage = 0.0 if min_cov == float("inf") else round(min_cov, 3)
    cand.min_cushion_coverage = 0.0 if min_cushion == float("inf") else round(min_cushion, 3)
    cand.tightest_month = tightest

    # rule S5 — a balloon landing in a month with no salary
    if cand.maturity_date:
        m = by_month.get((cand.maturity_date.year, cand.maturity_date.month))
        cand.matures_in_dry_month = bool(m and not m.in_season)

    req = inputs.min_coverage or 1.0
    if cand.key == "bullet_reserve":
        # Serviced from the exit event, not from the cash flow — the same-month
        # test is meaningless. Judge it on the event, and say so.
        cand.passes = True
        if not inputs.expected_exit_date:
            cand.warnings.append(
                "No expected exit date entered — maturity defaulted to the target "
                "term. A bullet is only as good as the date the event actually lands.")
        if cand.min_cushion_coverage < 1.0:
            cand.warnings.append(
                f"Projected cash flow alone does not retire the balloon "
                f"({cand.min_cushion_coverage:.2f}x banked at maturity). Repayment "
                f"depends entirely on the exit event.")
    else:
        cand.passes = cand.min_coverage >= req
        if cand.min_coverage < req:
            cand.warnings.append(
                f"Tightest month {cand.tightest_month} covers only "
                f"{cand.min_coverage:.2f}x the payment against a {req:.2f}x minimum.")
            if cand.min_cushion_coverage >= req:
                cand.warnings.append(
                    f"It does clear on banked cash ({cand.min_cushion_coverage:.2f}x) — "
                    f"viable only if the borrower actually reserves in-season income.")

    if cand.matures_in_dry_month:
        # A dry month is not automatically the wrong month. Maturing just after
        # the last check — while banked cash is still at its peak — is the RIGHT
        # side of the season; maturing deep in the offseason, after months of
        # drawdown, is not. Distinguish them by where the cumulative sits.
        peak = max((m.cumulative for m in cash_flow), default=0.0)
        at_mat = next((m.cumulative for m in cash_flow
                       if cand.maturity_date
                       and (m.year, m.month) == (cand.maturity_date.year, cand.maturity_date.month)), 0.0)
        if peak > 0 and at_mat >= peak * 0.95:
            cand.warnings.append(
                "Maturity falls in a month with no salary income, but immediately "
                "after the season — banked cash is still at its peak, so this is "
                "the right side of the offseason to land on.")
        else:
            cand.warnings.append(
                "Maturity falls in a month with no salary income, after the "
                "borrower has been drawing down the offseason. Move the balloon to "
                "a pay month, or to just after the last check, unless an exit "
                "event covers it.")

    if not inputs.salary_guaranteed and cand.key in ("interest_only_balloon", "bullet_reserve"):
        cand.warnings.append(
            "Income is not guaranteed. Deferring principal to a balloon leaves the "
            "full amount exposed to a cut or injury — amortizing while the money "
            "flows collects more of it.")

    return cand


# --- Top level -------------------------------------------------------------

def _recommend(cands: list[StructureCandidate], inputs: StructureInputs) -> None:
    """Mark one candidate recommended, following the timing/certainty matrix."""
    if not cands:
        return
    by_key = {c.key: c for c in cands}

    event_driven = bool(inputs.expected_exit_date) or (inputs.salary or 0) <= 0
    if event_driven and "bullet_reserve" in by_key:
        by_key["bullet_reserve"].recommended = True
        return

    # Recurring income: prefer the structure that matches the cadence, provided
    # it clears coverage. Non-guaranteed income argues for it even harder.
    for key in ("seasonal_amortization", "fully_amortized"):
        c = by_key.get(key)
        if c and c.passes:
            c.recommended = True
            return

    passing = [c for c in cands if c.passes and c.key != "bullet_reserve"]
    if passing:
        max(passing, key=lambda c: c.min_coverage).recommended = True
        return

    # Nothing clears the same-month test — recommend the best cushion coverage
    # and let the warnings carry the caveat.
    max(cands, key=lambda c: c.min_cushion_coverage).recommended = True


def _apply_no_contract(inputs: StructureInputs) -> StructureInputs:
    """Enforce no-team-contract mode server-side, never trusting the form.

    A stale salary left in a disabled field must not fund the projection —
    other income and dated bonus payments are all the cash there is. A
    PROPOSED (unexecuted) contract's expected signing date becomes the exit
    event when none was entered: the deal is repaid by the signing, so the
    bullet should mature just after it. The proposed VALUE is deliberately
    not touched here — it sizes the loan in propose_terms and is never income.
    """
    if not inputs.no_team_contract:
        return inputs
    update: dict = {"salary": 0.0, "salary_guaranteed": False}
    if inputs.proposed_contract_date and not inputs.expected_exit_date:
        update["expected_exit_date"] = inputs.proposed_contract_date
        if not inputs.expected_exit_label:
            update["expected_exit_label"] = "proposed contract signing"
    return inputs.model_copy(update=update)


def propose_structures(inputs: StructureInputs) -> StructureResult:
    """Project the cash flow and score every candidate structure against it."""
    inputs = _apply_no_contract(inputs)

    cad = resolve_cadence(inputs)
    cash_flow = project_cash_flow(inputs, cad)

    raw = [
        _seasonal_amortization(inputs, cad),
        _fully_amortized(inputs, cad),
        _interest_only_balloon(inputs),
        _bullet_reserve(inputs),
    ]
    cands = [_score(c, cash_flow, inputs) for c in raw if c is not None]
    _recommend(cands, inputs)

    annual_gross = (inputs.salary or 0.0) + (inputs.other_income or 0.0)
    annual_available = annual_gross * (1 - TAX_RATE - LIVING_RATE) - (inputs.other_debt_annual or 0.0)

    notes: list[str] = []
    if inputs.no_team_contract:
        notes.append(
            "No team contract — the projection runs on other income and dated "
            "payments only; no salary or salary cadence is used.")
        if inputs.proposed_contract_value:
            when = (f", expected to sign {inputs.proposed_contract_date:%b %d, %Y}"
                    if inputs.proposed_contract_date else "")
            notes.append(
                f"A PROPOSED contract for ${inputs.proposed_contract_value:,.0f} "
                f"guaranteed is on the table{when} — unexecuted, so it sizes the "
                f"loan and times the exit but is never projected as income.")
    if cad.notes and not inputs.no_team_contract:
        notes.append(f"{cad.league or 'Cadence'}: {cad.notes}")
    if not inputs.funding_date:
        notes.append("No funding date entered — the projection starts today.")
    if inputs.contract_end:
        notes.append(
            f"No salary is projected past the contract end ({inputs.contract_end:%b %d, %Y}).")
    if inputs.agent_pct:
        notes.append(
            f"An agent commission of {inputs.agent_pct:g}% is applied, so this projection "
            f"deliberately runs below the credit memo's cash flow.")
    dry = [m.label for m in cash_flow if not m.in_season and m.gross <= 0]
    if dry:
        notes.append(f"{len(dry)} month(s) in the projection carry no income at all.")

    return StructureResult(
        inputs_echo=inputs,
        cadence_used=cad,
        cash_flow=cash_flow,
        candidates=cands,
        annual_gross=round(annual_gross, 2),
        annual_available=round(annual_available, 2),
        notes=notes,
    )


def to_schedule_rows(cand: StructureCandidate) -> list[dict]:
    """Convert a selected candidate into loandocs ScheduleRow dicts.

    This is the push into the Loan Documents tab: the rows land in
    LoanDocTerms.repayment_schedule, which already outranks the computed
    Exhibit A schedule, so no document code changes.
    """
    return [
        {
            "date": p.date,
            "interest": round(p.interest, 2),
            "principal": round(p.principal, 2),
            "total": round(p.total, 2),
        }
        for p in cand.payments
    ]



# --- Proposing the terms ---------------------------------------------------
#
# House defaults, taken from South River's own recent closed deals rather than
# invented: 15% / 4.0% / 6mo is typical, 13.5% / 3.0% / 6mo on the largest.
# They are STARTING POINTS the underwriter edits,
# not a pricing model — nothing here derives a rate from risk.

HOUSE_RATE_PCT = 15.0
HOUSE_POINTS_PCT = 4.0
HOUSE_TERM_MONTHS = 6
_RATE_BASIS = ("South River's recent deals (15% / 4 pts / 6 months; 13.5% / 3 pts "
               "on the largest). A starting point to edit, not a priced rate.")


def _repayable_from_earnings(result: StructureResult, min_cov: float) -> bool:
    """Can any structure be retired out of the borrower's OWN earnings?

    Tested against BANKED cash (cushion coverage), not the tightest single
    month. South River's facilities are balloons retired from a season's
    accumulated earnings via the payroll sweep — an athlete banks in-season
    money precisely because the offseason has none. Judging capacity on the
    thinnest month instead would cap a $1.4m NFL salary at about $19,000,
    because January carries only one game check.

    bullet_reserve is excluded on purpose: it is repaid by an exit event rather
    than by earnings, so counting it would make capacity unbounded.
    """
    return any(c.min_cushion_coverage >= min_cov
               for c in result.candidates if c.key != "bullet_reserve")


def max_supportable_loan(inputs: StructureInputs, ceiling: float = 0.0) -> float:
    """The largest loan the borrower's projected CASH FLOW can service.

    Binary search: a size is supportable when some income-serviced structure
    still clears the minimum coverage. Returns 0.0 when even a token loan fails,
    which is the honest answer for a deal that has to be repaid by an event.
    """
    hi = ceiling if ceiling > 0 else max((inputs.salary or 0.0) * 2, 1_000_000.0)

    floor = max(hi * 0.001, 1000.0)
    min_cov = inputs.min_coverage or 1.0
    probe = inputs.model_copy(update={"loan_amount": floor})
    if not _repayable_from_earnings(propose_structures(probe), min_cov):
        return 0.0

    lo = floor
    if _repayable_from_earnings(propose_structures(
            inputs.model_copy(update={"loan_amount": hi})), min_cov):
        return float(round(hi, -3))

    for _ in range(18):
        mid = (lo + hi) / 2
        if _repayable_from_earnings(propose_structures(
                inputs.model_copy(update={"loan_amount": mid})), min_cov):
            lo = mid
        else:
            hi = mid
    return float(round(lo, -3))


def propose_terms(inputs: StructureInputs,
                  contract_remaining: Optional[float] = None):
    """Propose the loan amount, rate, points and term.

    The amount is the LOWER of two independent ceilings, and which one binds is
    reported rather than implied:

      * POLICY    — South River's Loan-to-Contract limit
                    (calculations.LTC_MAX_PCT) on guaranteed earnings, the same
                    basis rule 10 uses: total remaining contract value when
                    known, else the guaranteed season salary.
      * CASH FLOW — the largest loan an income-serviced structure can actually
                    carry at the minimum coverage.

    Rate, points and term are house defaults. This does not price risk, and the
    returned ``rate_basis`` says so.
    """
    from .calculations import LTC_MAX_PCT
    from .structure_models import ProposedTerms

    # No-team-contract mode: the same enforcement propose_structures applies
    # (zeroed salary; a proposed contract's signing date as the exit event),
    # and the LTC basis is the PROPOSED contract's guaranteed value — the only
    # earnings figure such a deal has, flagged below for credit approval.
    inputs = _apply_no_contract(inputs)
    if inputs.no_team_contract:
        basis = inputs.proposed_contract_value or 0.0
    else:
        basis = contract_remaining or inputs.salary or 0.0
    policy_cap = float(round(basis * LTC_MAX_PCT / 100.0, -3))
    capacity = max_supportable_loan(inputs, ceiling=max(policy_cap * 2, 1_000_000.0))

    warnings: list[str] = []
    event_driven = bool(inputs.expected_exit_date) or (inputs.salary or 0) <= 0
    if inputs.no_team_contract:
        if basis:
            warnings.append(
                f"Sized against a PROPOSED contract (${basis:,.0f} guaranteed) "
                f"that is NOT yet executed — nothing is contractually owed until "
                f"it is signed. Requires credit approval prior to funding.")
        else:
            warnings.append(
                "No team contract and no proposed contract value entered — the "
                "LTC policy has nothing to run on, so only the cash-flow ceiling "
                "applies.")

    if capacity <= 0:
        amount = policy_cap if event_driven else 0.0
        binding = "event" if event_driven else "cash flow"
    elif policy_cap and policy_cap <= capacity:
        amount, binding = policy_cap, "policy"
    else:
        amount, binding = capacity, "cash flow"

    can_repay = capacity > 0 and amount <= capacity
    if can_repay:
        note = (f"Yes — the projected cash flow services ${amount:,.0f} with the "
                f"structure recommended below. Capacity runs to ${capacity:,.0f} "
                f"before coverage breaks.")
    elif event_driven:
        note = ("Not from earnings. The contract's cash flow cannot retire a loan "
                "of this size, so repayment depends entirely on the exit event — "
                "structure it as a bullet and size it against the event, not the "
                "salary.")
        warnings.append("Repayment is event-dependent: nothing here is serviced by income.")
    else:
        note = ("No. No income-serviced structure clears the minimum coverage at "
                "any meaningful size — the contract cannot carry this facility.")
        warnings.append("The borrower's cash flow does not support a loan on these terms.")

    if binding == "policy":
        warnings.append(
            f"Held at the {LTC_MAX_PCT:g}% Loan-to-Contract policy limit on "
            f"${basis:,.0f} of guaranteed earnings; the cash flow alone would "
            f"carry ${capacity:,.0f}.")
    elif binding == "cash flow" and policy_cap and capacity < policy_cap:
        warnings.append(
            f"Cash flow binds before policy does: {LTC_MAX_PCT:g}% LTC would "
            f"allow ${policy_cap:,.0f}.")

    return ProposedTerms(
        loan_amount=amount,
        interest_rate=HOUSE_RATE_PCT,
        origination_fee_pct=HOUSE_POINTS_PCT,
        target_term_months=HOUSE_TERM_MONTHS,
        policy_cap=policy_cap,
        cash_capacity=capacity,
        binding_constraint=binding,
        guaranteed_earnings_basis=basis,
        can_repay=can_repay,
        repayment_note=note,
        rate_basis=_RATE_BASIS,
        warnings=warnings,
    )


# --- Debt service from the credit memo's PFS -------------------------------

def debt_service_from_memo(ed, as_of: Optional[date] = None) -> dict:
    """Annual non-facility debt service, using the MEMO's own PFS handling.

    Two reasons to go through calculations rather than ask the model again: the
    memo's cash flow already decides which rows count (mortgage, autos, alimony,
    student loans, HOA, ...), and `calc_debt_rollforward` already ages a stale
    statement forward (rule 15) — so a debt repaid since the statement date
    stops being counted here too, instead of inflating debt service off a
    document that may be a year old.
    """
    from . import calculations as calc

    if ed is None:
        return {"annual": 0.0, "note": "", "pfs_date": None, "dropped": []}

    flow = calc.build_cash_flow(ed, None, 0.0, 0.0)
    annual = sum(d["amt"] for d in flow["debt_items"])

    rf = calc.calc_debt_rollforward(ed, as_of or date.today())
    dropped: list[str] = []
    if rf.get("applied"):
        # A balance rolled (or zeroed) to nothing is repaid: its payments should
        # not go on being counted against the borrower.
        for row in rf.get("rows", []):
            if (row.get("rolled") and (row.get("adjusted") or 0) <= 0
                    and (row.get("payment") or 0) > 0):
                annual = max(0.0, annual - row["payment"] * 12)
                dropped.append(row.get("lender") or "a scheduled debt")

    note = rf.get("note") or ""
    if dropped:
        note = (note + " " if note else "") + (
            f"{len(dropped)} debt(s) are repaid as of the funding date and their "
            f"payments are excluded from debt service: {', '.join(dropped)}.")

    return {
        "annual": round(annual, 2),
        "note": note.strip(),
        "pfs_date": rf.get("pfs_date"),
        "dropped": dropped,
    }
