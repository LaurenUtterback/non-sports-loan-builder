"""Document extraction for the Deal Structuring tab.

The credit memo's extraction already captures the CERTAINTY side of a deal
(guaranteed salary, contract end, guarantee language). What structuring needs on
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

from .models import DebtScheduleRow, LineItem, SalaryCheck, UploadedDoc
from .loandocs_extraction import _ask_claude, EXTRACTION_MODEL
# The Spotrac cross-check reuses the credit memo's proven pieces: the verdict
# math (build_salary_check — computed HERE, never by the model) and the same
# usage-token auth as every other extractor.
from .extraction import (
    build_salary_check, usage_token, build_client, create_with_retry,
    parse_json_reply, LEGAL_NAME_RULE, _CLAUDE_CODE_SYSTEM,
)
from .research import spotrac_lookup


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


class ExtractedBonus(_Tolerant):
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
    team: str = ""
    league: str = ""

    # Certainty
    salary: Optional[float] = None            # GUARANTEED season compensation
    other_income: Optional[float] = None
    other_debt_annual: Optional[float] = None
    contract_end: Optional[str] = None        # ISO
    salary_guaranteed: Optional[bool] = None
    salary_guarantee_note: str = ""

    # Timing — the fields the memo extraction does not carry
    pay_frequency: Optional[str] = None       # weekly | semimonthly | monthly
    season_start: Optional[str] = None        # ISO, the season's first pay date
    season_end: Optional[str] = None          # ISO, the season's last pay date
    pay_election_note: str = ""
    bonus_events: list[ExtractedBonus] = []

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

    # Spotrac cross-check (filled server-side AFTER the document extraction,
    # never by the document prompt). Verification only — the documents stay
    # authoritative; the UI shows these under the salary / team / league fields
    # and fills a field from them only when the documents produced nothing.
    salary_check: Optional[SalaryCheck] = None
    spotrac_team: str = ""
    spotrac_league: str = ""

    @field_validator("salary", "other_income", "other_debt_annual", "loan_amount",
                     "interest_rate", "origination_fee_pct", mode="before")
    @classmethod
    def _clean_numbers(cls, v):
        return _to_number(v)

    @field_validator("pay_frequency", mode="before")
    @classmethod
    def _normalize_frequency(cls, v):
        """Map what Claude writes onto the three cadences the engine supports.

        LeagueCadence.pay_frequency is a Literal, so an unrecognised value would
        fail the /propose call later — much further from the cause. Anything we
        can't place returns None, which falls back to the league default.
        """
        if not v:
            return None
        key = re.sub(r"[\s_-]", "", str(v)).lower()
        if key in ("weekly", "everyweek", "pergame", "gamecheck", "gamechecks"):
            return "weekly"
        if key in ("semimonthly", "twicemonthly", "twiceamonth", "bimonthly",
                   "biweekly", "everytwoweeks", "fortnightly", "24payments"):
            # Biweekly (26/yr) is not exactly semi-monthly (24/yr), but it is far
            # closer than weekly or monthly and keeps the projection sane.
            return "semimonthly"
        if key in ("monthly", "permonth", "everymonth", "12payments"):
            return "monthly"
        return None

    @field_validator("salary_guaranteed", mode="before")
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


PROMPT = """You are reading loan documents for a professional athlete lender.
Your job is to pull the facts needed to decide the loan's REPAYMENT STRUCTURE:
when the borrower actually receives cash, and how certain that cash is.

Return RAW JSON only — no prose, no markdown fence. Use this exact shape:

{
  "borrower_name": "", "team": "", "league": "",
  "salary": null, "other_income": null, "other_debt_annual": null,
  "contract_end": null, "salary_guaranteed": null, "salary_guarantee_note": "",
  "pay_frequency": null, "season_start": null, "season_end": null,
  "pay_election_note": "",
  "bonus_events": [{"label": "", "date": null, "amount": null, "guaranteed": true}],
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

1. SALARY is the GUARANTEED portion of the season's compensation ONLY: the
   guaranteed base salary PLUS any bonus that is guaranteed and paid every year
   of the contract (annual signing-bonus installments, guaranteed yearly roster
   bonuses). Exclude non-guaranteed incentives, one-time bonuses and
   endorsements. When installments differ season to season, use the one
   SCHEDULED FOR THE SEASON BEING UNDERWRITTEN — never an average, never
   another season's. If the contract states a Paragraph 5 salary but an
   addendum guarantees only part of it, salary is the GUARANTEED part.

2. salary_guaranteed is TRUE only for a clean, unconditional guarantee. If the
   guarantee is conditional, partial, voidable (failure to report, refusal to
   play, non-football injury, retirement, breach), or a one-way/limited SPC,
   set it FALSE and quote the condition in salary_guarantee_note. This flips
   which structures make sense, so do not round it up to true.

3. BONUS_EVENTS are dated lump payments that fall OUTSIDE the regular salary
   cadence: signing-bonus installments, roster bonuses, workout bonuses,
   endorsement milestones, deferred payments. Give each its real payment date
   from the schedule. NEVER include an amount here that you already counted in
   salary — if a guaranteed annual installment is inside salary, leave it out
   of bonus_events. Mark guaranteed=false for anything contingent.

4. TIMING: pay_frequency is "weekly", "semimonthly" or "monthly" — only if the
   documents actually state how the player is paid. season_start / season_end
   are the first and last SCHEDULED PAY DATES of the season, in ISO form, again
   only if stated. If the documents are silent, return null and say so in
   pay_election_note — a league default will be applied instead. Do NOT invent
   a cadence. If the player has ELECTED a pay schedule (e.g. an NBA player
   electing payment over 12 months instead of the season), record that election
   in pay_election_note; it matters more than the league default.

5. PFS: when a Personal Financial Statement is among the documents, fill "pfs"
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
     period copied verbatim when it names one ("per game check", "annual"),
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

6. EXIT: fill expected_exit_date / expected_exit_label ONLY when the documents
   indicate repayment depends on a specific EVENT rather than on income — a
   contract extension or new contract being signed, an endorsement closing, a
   property sale, a refinance. expected_exit_label is a SHORT NOUN PHRASE
   naming the event, at most 8 words ("contract extension signing", "sale of
   the Miami property") — never a sentence and never your reasoning; that
   belongs in notes. Set the label even if no date is stated. If repayment
   simply comes from salary, leave both null.

7. presented_type is how the loan is structured AS PRESENTED, in the documents'
   own words ("Full balloon", "Interest only with balloon", "Fully amortized").

8. Dates are ISO yyyy-mm-dd. Amounts are plain numbers, no symbols or commas.
   Percentages are numbers (15 for 15%). Use null for anything the documents do
   not state — NEVER guess, and never carry a figure over from a different
   season or a different loan.

9. NOTES is read by a person in a single glance. Give at most 5 short items,
   semicolon-separated, covering only what would change the structuring
   decision or what you were unsure about — a conflict between documents, a
   figure you had to interpret, a repayment source that is not the salary.
   Do not restate figures that are already in the fields above, and do not
   summarize the deal.

10. BORROWER_NAME. """ + LEGAL_NAME_RULE + """
"""


SPOTRAC_CHECK_PROMPT = """You are a credit analyst at South River Capital verifying deal facts for a loan-structuring run: a professional athlete's season compensation, current team, and league, as Spotrac presents them.

Borrower: {who}
Today's date: {today}
From the deal documents: guaranteed season salary ${doc_salary}.

Below is the text of the athlete's Spotrac page. Using SPOTRAC ONLY (ignore the document figure above except to know which season is being underwritten), determine:
1. The athlete's compensation for the current/upcoming season as Spotrac's CAP HIT for that season. The cap hit is the base salary PLUS the prorated signing bonus and any roster, workout, or other bonus amounts Spotrac counts for that season — NEVER return the base salary alone when Spotrac lists a cap hit or those bonus components for the season. If the page shows no cap hit (a league or page without one), compose the closest equivalent: the season's base salary plus that season's bonus amounts as Spotrac lists them. Use only the season being underwritten — exclude other seasons, one-time or performance/incentive bonuses Spotrac does not count for the season, and endorsements.
2. The athlete's CURRENT team as Spotrac lists it — the team the athlete is signed/rostered with now, never a former team from a transaction history.
3. The league the athlete plays in (NFL, NBA, MLB, NHL, MLS, ...).

Return ONLY raw JSON, no markdown, no backticks: {{"spotrac_salary":0,"season":null,"team":null,"league":null,"note":null}}
- spotrac_salary: that season cap hit (or composed equivalent) as a number. Return 0 if the page cannot support a figure (no contract data, contract expired, or the page describes a DIFFERENT person than the borrower — check name, sport, team).
- season: the season/year the figure belongs to (e.g. "2026"), else null.
- team / league: exactly as Spotrac lists them, else null. Return null for BOTH if the page describes a different person than the borrower.
- note: 1-2 short plain-text sentences an underwriter can read: the figure's composition as Spotrac shows it (base salary + prorated signing bonus + other bonuses), and how much of the season's compensation Spotrac marks as GUARANTEED — or why no figure could be determined. Never use markdown.

SPOTRAC ({url}):
{text}"""


def _ask_spotrac(text: str, url: str | None, ex: StructureExtraction) -> dict:
    """Second Claude call: read the fetched Spotrac page for the season cap
    hit, current team and league. Raises on any failure — the caller treats
    every failure as 'check could not be run', never as a broken extract."""
    token = usage_token()
    if not token:
        raise RuntimeError("No Claude usage token is set.")
    import anthropic

    client = build_client(anthropic, token)
    who = ", ".join(p for p in (ex.borrower_name, ex.team, ex.league) if p)
    message = create_with_retry(
        client, "structure Spotrac check",
        model=EXTRACTION_MODEL,
        max_tokens=500,
        system=_CLAUDE_CODE_SYSTEM,
        messages=[{
            "role": "user",
            "content": SPOTRAC_CHECK_PROMPT.format(
                who=who or "(name not extracted)",
                today=date.today().isoformat(),
                doc_salary=f"{float(ex.salary or 0):,.0f}",
                url=url,
                text=text,
            ),
        }],
    )
    return parse_json_reply(message, "structure Spotrac check")


def _verify_with_spotrac(ex: StructureExtraction) -> None:
    """Cross-check the extracted guaranteed salary, team and league against the
    athlete's Spotrac page (Lauren, 2026-08-14 — the same check the credit memo
    tab runs, extended with team/league).

    Mutates ``ex`` in place: ``salary_check`` (verdict computed by
    build_salary_check, never the model) plus ``spotrac_team`` /
    ``spotrac_league`` as Spotrac lists them. Best-effort by contract — any
    failure leaves a 'verify manually' note and must NEVER break the extract.
    """
    manual_note = ("The Spotrac cross-check could not be run — verify the "
                   "guaranteed salary, team and league manually at spotrac.com.")
    try:
        name = (ex.borrower_name or "").strip()
        text = url = None
        if name:
            text, url = spotrac_lookup(name, ex.league or None, None)
        if not text:
            ex.salary_check = SalaryCheck(**build_salary_check(
                ex.salary or 0, 0.0, url,
                "Spotrac page could not be retrieved — verify the guaranteed "
                "salary, team and league manually at spotrac.com."))
            return
        parsed = _ask_spotrac(text, url, ex)
        ex.salary_check = SalaryCheck(**build_salary_check(
            ex.salary or 0,
            float(parsed.get("spotrac_salary") or 0),
            url,
            str(parsed.get("note") or "").strip(),
            str(parsed.get("season") or "").strip() or None,
        ))
        ex.spotrac_team = str(parsed.get("team") or "").strip()
        ex.spotrac_league = str(parsed.get("league") or "").strip()
    except Exception as exc:  # noqa: BLE001 - verification must never break extraction
        logging.getLogger(__name__).warning("Structure Spotrac check failed: %s", exc)
        ex.salary_check = SalaryCheck(**build_salary_check(
            ex.salary or 0, 0.0, None, manual_note))


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
    PFS through the memo's debt-service handling and cross-check salary /
    team / league against Spotrac (both best-effort)."""
    # 8000, not 4000: the structure reply nests the full captured PFS, so
    # the same document-heavy deal that truncated the memo extraction on
    # 2026-08-20 would truncate here too.
    ex = StructureExtraction(**_ask_claude(docs, PROMPT, max_tokens=8000))
    _debt_service_from_pfs(ex)
    _verify_with_spotrac(ex)
    return ex
