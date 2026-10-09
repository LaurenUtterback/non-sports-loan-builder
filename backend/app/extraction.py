"""Document extraction via the Anthropic API.

This is the security-critical reason the project moved to a backend: the
credential lives here in an environment variable and never reaches the browser.

Extraction authenticates STRICTLY with a Claude subscription usage token
(an OAuth token), never a pay-per-token API key. See `extract_documents`.

The extraction PROMPT is the second authoritative artifact in this project
(alongside calculations.py). Its rules about guaranteed-compensation-only
salary (guaranteed base PLUS guaranteed annual bonuses, nothing non-guaranteed),
capturing every liability and expenditure line, SSN redaction, and auto-loan
folding are load-bearing — keep them in sync with the rules documented in
calculations.py.

After extraction, public research on the athlete (Wikipedia + Spotrac,
gathered ONCE by research.py) feeds two more best-effort Claude calls: one
composes the Section V (Project Sponsorship) narrative so the section
describes the athlete, not just the facility (`_compose_sponsorship`), and
one reads the athlete's Spotrac page for the season cap hit and remaining
contract value (`_check_salary_against_spotrac`). Since 2026-08-25 (Lauren)
Spotrac is the PRIMARY source for the memo's Guaranteed salary / Guaranteed
remaining prefills — `apply_spotrac_precedence` — with the documents as the
backup and shown as the cross-check. Both steps stay best-effort: any failure
leaves the documents' figures in place and never breaks /api/extract.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .models import Extraction, UploadedDoc
from .calculations import mask_ssn
from .doc_blocks import build_document_blocks
from .research import gather_athlete_research

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-sonnet-4-6")

# OAuth ("usage") authentication, not an API key. A Claude subscription token
# authenticates via `Authorization: Bearer` plus this beta header, and the
# request must identify as Claude Code (the system prompt below) to be accepted.
_OAUTH_BETA_HEADER = "oauth-2025-04-20"
_CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
_TOKEN_PLACEHOLDER = "paste-your-token-here"


# The API occasionally answers a perfectly valid extraction with a 500. The SDK
# retries 5xx on its own, but its default of 2 has not been enough in practice —
# an upload that failed was succeeding on a manual retry moments later.
_MAX_RETRIES = 5

# The SDK honors the response's `x-should-retry: false` header, and Anthropic
# sets it on some 500s — so the whole _MAX_RETRIES budget can go unused (seen
# 2026-08-10: ONE attempt, then failure, on an 11-document upload whose manual
# retry succeeded). These app-level retries sit above the SDK and retry any
# 5xx regardless of that header, spaced further apart than the SDK would —
# "the same upload usually succeeds a few minutes later" is the observed
# pattern, so short exponential backoff is the wrong shape here.
_RETRY_DELAYS_S = (5, 15, 30)

# Above this the request is big enough to be worth warning about: large uploads
# are the pattern most associated with server-side 500s here.
_LARGE_REQUEST_MB = 12


def create_with_retry(client, where: str, **kwargs):
    """client.messages.create with app-level retries on server errors (>= 500).

    Retries by status code alone (duck-typed, no anthropic import needed) so a
    500 carrying `x-should-retry: false` is retried anyway. Anything below 500
    — auth, validation, request-too-large — raises immediately, exactly as
    before. After the last delay the final error propagates for
    describe_api_error to explain.
    """
    log = logging.getLogger(__name__)
    for attempt, delay in enumerate(_RETRY_DELAYS_S + (None,)):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - filtered by status code below
            status = getattr(exc, "status_code", None)
            if not status or status < 500 or delay is None:
                raise
            log.warning(
                "%s: attempt %d got a server error (%s, request %s) — "
                "retrying in %ds.", where, attempt + 1, status,
                getattr(exc, "request_id", None) or "unknown", delay)
            time.sleep(delay)


def usage_token() -> str | None:
    """Return the Claude subscription usage (OAuth) token, or None if unset.

    Treats the .env placeholder as unset so a real token is always required.
    """
    token = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if not token or token == _TOKEN_PLACEHOLDER:
        return None
    return token


def build_client(anthropic, token: str):
    """The Anthropic client every extraction in this app uses.

    Strictly usage auth, never an API key: any stray ANTHROPIC_API_KEY is
    dropped so the SDK cannot send both credentials (which the API rejects).
    """
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={"anthropic-beta": _OAUTH_BETA_HEADER},
        max_retries=_MAX_RETRIES,
    )


def _block_bytes(block: dict) -> float:
    """Roughly what this block costs in the request.

    A Word or Excel file is converted to text before it is sent, so its upload
    size says nothing about the request — an 8 MB .docx routinely becomes 20 KB
    of text. Sizing the BLOCK is what makes the warning below trustworthy.
    """
    if block.get("type") == "text":
        return len(block.get("text") or "")
    return len((block.get("source") or {}).get("data") or "") * 0.75


def log_request_manifest(where: str, docs, content: list[dict]) -> None:
    """Record exactly what was sent, so a failure can be diagnosed afterwards.

    Without this an API error names no file and the request is unreproducible —
    which is precisely the hole hit chasing a 500 on 2026-08-06.
    """
    sent_mb = sum(_block_bytes(b) for b in content) / 1_000_000
    lines = []
    for d, b in zip(docs, content):
        upload_kb = len(d.b64 or "") * 0.75 / 1024
        sent_kb = _block_bytes(b) / 1024
        size = (f"{upload_kb:.0f} KB" if b.get("type") != "text"
                else f"{upload_kb:.0f} KB -> {sent_kb:.0f} KB of text")
        lines.append(f"{d.filename or '(unnamed)'} [{b.get('type')}, {size}]")

    log = logging.getLogger(__name__)
    log.info("%s: %d document(s), ~%.1f MB sent: %s",
             where, len(docs), sent_mb, "; ".join(lines))
    if sent_mb > _LARGE_REQUEST_MB:
        log.warning("%s: request is ~%.1f MB — large requests are the usual cause "
                    "of server-side 500s; consider extracting fewer documents.",
                    where, sent_mb)


# The Messages API rejects request bodies over ~32 MB with a 413 that names
# nothing ("Request exceeds the maximum size"). ~30 MB of encoded content
# leaves headroom for the prompt and JSON framing. Hit 2026-08-11: an 11-file
# upload measuring ~26 MB raw encoded to ~35 MB and 413'd, and the user only
# learned "too large" — not which file to remove.
_REQUEST_BUDGET_MB = 30
_API_REQUEST_LIMIT_MB = 32


def _wire_bytes(block: dict) -> int:
    """What this block puts in the request body — base64 length, not raw."""
    if block.get("type") == "text":
        return len(block.get("text") or "")
    return len((block.get("source") or {}).get("data") or "")


def check_request_size(where: str, docs, content: list[dict]) -> None:
    """Make an over-budget request fit BEFORE the API round trip — shrink the
    largest blocks (image recompression, then text-layer swaps; see
    doc_blocks.shrink_blocks_to_fit), and refuse only what still cannot fit.

    Sized the way the 413 limit applies — base64 on the wire — and the error
    names the largest documents with how much must go, so the user acts on
    files, not on a bare "too large".
    """
    total_mb = sum(_wire_bytes(b) for b in content) / 1_000_000
    if total_mb <= _REQUEST_BUDGET_MB:
        return

    # Shrink instead of refusing (Lauren, 2026-08-14). Mutates content in
    # place; every change is logged so a degraded document is never silent.
    from .doc_blocks import shrink_blocks_to_fit

    log = logging.getLogger(__name__)
    notes = shrink_blocks_to_fit(docs, content, _REQUEST_BUDGET_MB * 1_000_000)
    if notes:
        log.info("%s: upload was ~%.1f MB encoded — shrunk to fit: %s",
                 where, total_mb, "; ".join(notes))
    total_mb = sum(_wire_bytes(b) for b in content) / 1_000_000
    if total_mb <= _REQUEST_BUDGET_MB:
        return

    sized = sorted(zip(docs, content), key=lambda dc: _wire_bytes(dc[1]),
                   reverse=True)
    biggest = "; ".join(
        f"{d.filename or '(unnamed)'} (~{_wire_bytes(b) / 1_000_000:.1f} MB encoded)"
        for d, b in sized[:3]
    )
    raise RuntimeError(
        f"The upload is too large for one request: ~{total_mb:.0f} MB once "
        f"encoded (even after automatic compression), over the API's "
        f"{_API_REQUEST_LIMIT_MB} MB limit — at least "
        f"~{total_mb - _REQUEST_BUDGET_MB:.0f} MB has to come out. Largest "
        f"documents: {biggest}. Remove the largest files and extract again "
        f"(large scanned bundles and brokerage/bank statements usually add "
        f"nothing the PFS and contract don't already state); anything removed "
        f"can be typed into Step 2 by hand."
    )


def describe_api_error(exc, where: str) -> str:
    """Turn an Anthropic APIError into something actionable for the user."""
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None) or "unknown"
    if status and status >= 500:
        return (
            f"Claude's API returned a server error ({status}) during {where}, and "
            f"kept failing through {len(_RETRY_DELAYS_S)} automatic retries spread "
            f"over ~{sum(_RETRY_DELAYS_S)} seconds. This is a fault on Anthropic's "
            f"side, not with your documents — the same upload usually succeeds a "
            f"few minutes later, so wait a moment and try again. If it keeps "
            f"failing, extract fewer documents at once (large requests fail this "
            f"way most often). Request ID {request_id}."
        )
    if status == 413 or "too large" in str(exc).lower():
        return (f"The upload is too large for one request during {where} — the "
                f"API caps a request at ~{_API_REQUEST_LIMIT_MB} MB once encoded. "
                f"Remove the largest documents (the backend log's manifest lists "
                f"each file's size) and try again. Request ID {request_id}.")
    return f"Claude API error during {where}: {exc} (request ID {request_id})"

# --- The borrower's name ----------------------------------------------------
# Lauren, 2026-10-01: a memo named the borrower by the two-part name the
# contract and the PFS used, while the passport and the driver's license in
# the same file carried a compound surname. A government ID governs the
# name on everything this app prints, so every uploader (memo, structure,
# participation agreement, loan documents, closing binder) shares this one
# rule and the four tabs cannot drift apart on it.
LEGAL_NAME_RULE = """CRITICAL — THE BORROWER'S NAME IS THEIR FULL LEGAL NAME. When any government-issued identity document is among the uploads — passport, driver's license, state ID, permanent resident card, visa — THAT document governs the name, over the contract, the PFS, a term sheet, the credit report, or the name the athlete plays under. Read its name fields together (a passport's "Surname / Nom / Apellidos" plus "Given names / Prénoms / Nombres"; a license's name lines) and return EVERY part they show, given names first, in normal reading order and normal capitalization: a passport reading Surname "RIVAS DELGADO" and Given names "MATEO" is "Mateo Rivas Delgado". Keep BOTH parts of a compound or double surname (paternal then maternal, as Hispanic, Portuguese and Brazilian names are registered) and keep any generational suffix (Jr., Sr., II, III, IV), punctuated as the ID punctuates it. The deal documents routinely shorten the name to the playing name: NEVER drop a surname or a suffix because the contract, the PFS, or the credit report omits it — the fullest form a legal identity document supports wins, and the shorter forms are not a conflict worth flagging. If NO identity document was uploaded, use the fullest name an executed legal document shows (the contract's signature block, a loan or security agreement, the PFS heading). Never invent a name part no document shows, never reorder the ID's parts (never "Rivas Delgado Mateo"), and never abbreviate to an initial what the ID spells out."""


PROMPT = """You are a financial analyst at South River Capital preparing a credit memorandum for a professional athlete loan. You have been given one or more documents which may include any combination of: Personal Financial Statement (PFS), player contract, credit report, pay stubs, or other deal documents.

Read ALL documents together and extract every piece of information needed for the credit memo. Use the most authoritative source for each field.

Return ONLY raw JSON, no markdown, no backticks:

{"borrower_name":null,"borrower_surname":null,"dob":null,"address":null,"phone":null,"team":null,"league":null,"sport":null,"ssn_masked":null,"drivers_license":null,"agent":null,"salary":0,"contract_remaining":0,"other_income":0,"total_income":0,"federal_taxes":0,"mortgage_payments":0,"hoa_payments":0,"student_loans":0,"interest_principal_loans":0,"insurance":0,"alimony":0,"auto_payments":0,"living_expenses":0,"other_expenses":[],"total_expenditures":0,"assets":[],"total_assets":0,"liabilities":[],"total_liabilities":0,"net_worth":0,"pfs_date":null,"debt_schedule":[],"facility_total_due":0,"loan_amount":0,"interest_rate_pct":0,"origination_fee_pct":0,"loan_term_months":0,"repayment_schedule":[],"uses_of_funds":null,"credit_notes":null,"contract_notes":null,"sponsorship_narrative":null}

Rules: assets and liabilities are arrays of {label, amount}. other_expenses is array of {label, amount}. Use 0 for missing numbers, null for missing strings. Include ALL non-zero line items. sport is the sport name ONLY (e.g. "Ice Hockey", "Basketball", "Football"); never prefix it with the word "professional" — the memo already phrases it as "a professional <sport> player". CRITICAL — the liabilities array must include EVERY liability line item EXACTLY as the uploaded Personal Financial Statement lists them, with their exact labels and amounts, omitting NOTHING: all Notes Payable categories (e.g., "Notes Payable: Contract Based", "Notes Payable to: others"), mortgage debt, credit card debt, taxes payable, and every other liability shown. Never merge, rename, or drop a liability row (the ONLY exception: auto loan balances fold into Notes Payable to: others as instructed below). CRITICAL — pfs_date is the date the Personal Financial Statement was PREPARED, as ISO yyyy-mm-dd (e.g. a "Completed on:" line, a date printed at the top of page 1, or the signature date): return it whenever a PFS is among the documents, else null. Do not use the credit report date, the contract date, or today's date - with ONE exception: when NO PFS is among the documents but debt_schedule was built from the credit report (see the no-PFS fallback below), return the credit report's own report/pull date as pfs_date, so those balances can be rolled forward from the date they were true. CRITICAL — debt_schedule is the PER-LOAN detail from the PFS's own detail schedules, which appear on a later page than the page-1 summary totals: Schedule D (Personal Residence / Real Estate Investments & Mortgage Debt), Schedule F (Notes Payable) and Schedule G (Contract Based Notes Payable). Return one object per financed debt listed in those schedules, omitting NOTHING, each {"lender","category","balance","payment","payment_period","origination","maturity","rate_pct","description"}, where:
- lender is the Lender / creditor name as shown ("Northgate Bank", "MidState Bank", "Credit Cards", "Sports Finance Fund, LP"). If a Schedule D row names no lender, use the property address.
- category is EXACTLY one of "mortgage_debt" (any Schedule D row), "notes_payable_others" (any Schedule F row) or "notes_payable_contract" (any Schedule G row) — this says which page-1 summary liability the row rolls up into, so never guess a different string.
- balance is the "Present Loan Balance" (Schedule D) or "Outstanding Amount" (Schedule F/G) as a positive number. Skip rows with no balance.
- payment is the "Monthly Payment" (Schedule D) or "Amount / Pay Period" (Schedule F/G) as a positive number; 0 if the row shows none (revolving lines such as credit cards typically show none).
- payment_period is how often that payment is made. Schedule D's column is headed "Monthly Payment", so return "monthly" for every Schedule D row that has a payment. Schedule F/G is headed "Amount / Pay Period", which is NOT necessarily monthly: return "monthly" when the document indicates a monthly payment, copy the period the document states when it states one (e.g. "per game check", "semi-monthly", "annual"), and return "" when the form shows a payment amount but names no period at all. Never invent a period.
CRITICAL — ROW ALIGNMENT. These schedules are tables, and the text layer of a scanned or Excel-exported PFS often reaches you with the columns out of order, so values can appear far from their own row. Every figure you return for a debt must come from the SAME TABLE ROW as that debt's lender: use the lender name and the Outstanding Amount as the anchor and read across that row for its payment, origination, maturity and rate. Do NOT pair a payment or a maturity date with a lender just because the two appear near each other in the text stream. If you cannot confidently tell which row a payment or maturity belongs to, return 0 / "" for it rather than attaching it to the wrong debt — a misplaced payment silently pays down the wrong loan in the memo. Revolving lines (a "Credit Cards" row, an unsecured note) normally show NO payment and NO maturity date on these forms: do not lend them a payment or maturity that belongs to a term loan on another row.
- origination is the "Date of Origination" (Schedule F/G) or the "Purchase Year" (Schedule D), copied as shown. maturity is the "Loan Maturity Date" / "Maturity Date", copied as shown. Copy these two VERBATIM even when they look wrong or malformed (a typo such as "08/072053", or a placeholder such as "7/17/1905" or "1/0/1900") — do NOT correct, reformat, or invent them, and return "" when the cell is blank. The credit memo sanitizes them.
- rate_pct is the schedule's stated Interest Rate as a percent number (13.5 for "13.5%"), 0 if blank. description is the "Reason for Debt" / collateral note (Schedule F/G) or the property address (Schedule D).
These rows are DETAIL, not additional liabilities: they must ALSO stay inside the page-1 summary amounts in the liabilities array (a Schedule D mortgage is already inside "Mortgage Debt"; a Schedule F auto note is already inside "Notes Payable to: others"). Never add a schedule row to liabilities as its own line. If the documents contain no such schedules, return [] - UNLESS no PFS is among the documents at all but a CREDIT REPORT is. NO-PFS FALLBACK: in that case build debt_schedule from the credit report's OPEN tradelines instead - one row per open account with a balance (auto loans, mortgages/HELOCs, credit cards, student loans, and other installment notes), omitting closed accounts and zero balances - where lender is the creditor name as reported, balance is the current balance, payment is the scheduled monthly payment as reported (0 for revolving credit-card lines - they do not amortize), payment_period is "monthly" for an installment account with a payment and "" otherwise, origination is the account-opened date as shown, maturity is "" unless the report states one, rate_pct 0 unless stated, description is the account type as the report words it, and category is "mortgage_debt" for mortgages/HELOCs and "notes_payable_others" for everything else. Keep these rows CONSISTENT with the liabilities array: with no PFS, build the liabilities from the same credit-report balances (mortgage debt as its own line; the rest inside "Notes Payable to: others" per the auto-loan folding rule; credit cards may be their own "Credit Card Debt" line), so each row's category matches a liability it is actually inside of. CRITICAL — capture the PROPOSED facility's deal terms exactly as the documents state them (a term sheet, approval, or commitment line such as "Loan Amount: $4,435,000", "Origination: 3%", "Rate: 13.5%", "Term: 6 months"): loan_amount is the loan / proposed facility PRINCIPAL as a numeric dollar amount (e.g. "$4.435M" -> 4435000, "$4,435,000" -> 4435000); interest_rate_pct is the facility's annual interest rate as a percent NUMBER (e.g. "13.5%" -> 13.5); origination_fee_pct is the origination/upfront/lender fee as a percent NUMBER (e.g. "3%" or "Origination: 3%" -> 3). Use 0 for any of these not stated in the documents. loan_term_months is the term/duration of the PROPOSED facility expressed in WHOLE months, as stated in the deal documents (e.g. a term sheet line such as "Term: 6 months", a stated loan period, or a "6 mo." note). If the term is stated in years, convert to months (1 year = 12 months). If the documents give only funding and maturity dates with no explicitly stated term, return 0 (the memo computes the span from the dates). Use 0 if no term is stated. facility_total_due is the proposed facility's TOTAL amount due (principal + interest) if any amortization schedule, payoff figure, or proposed-facility liability appears in the documents; otherwise 0. repayment_schedule is the proposed facility's repayment/amortization schedule WHEN the documents contain one (e.g., a payment/amortization table for the new loan): return an array with one object per scheduled payment, IN ORDER, each {"date","interest","principal","total"} where date is the payment date copied as shown (a short string such as "15-Jul-26"), interest and principal are that payment's dollar amounts (use 0 where a column is blank or zero — typically principal is 0 until the final balloon payment), and total is that payment's interest + principal. Copy the figures EXACTLY as the schedule lists them; do not recompute, round, or reorder. Capture ONLY the proposed facility's own schedule (not schedules for the borrower's other/existing debts). If the documents contain no repayment schedule for the proposed facility, return an empty array []. CRITICAL — uses_of_funds is the proposed facility's disbursement / sources-and-uses / settlement breakdown WHEN the documents contain one (e.g. a "Disbursement", "Uses of Funds", or settlement-statement table showing how the gross loan is applied): capture EVERY line provided, omitting NOTHING, as an object {"gross_loan_amount":0,"deductions":[{"label","amount"}],"additional_costs":[{"label","amount"}]}. gross_loan_amount is the full proposed facility (gross) amount. deductions are the fees and payoffs subtracted from the gross loan to reach the amount "to be disbursed to Borrower" — e.g. origination fee, underwriting fee, payoffs of existing loans (such as a "Bank Payments thru <date>" payoff line), and legal/closing costs. additional_costs are amounts funded from the loan and carved out of the to-Borrower figure to reach the NET disbursed — e.g. Death & Disgrace (DDD) insurance premium and Interest Reserve. Copy each label as shown and give every amount as a POSITIVE dollar magnitude (do not use negatives or parentheses). Do NOT include any subtotal/total lines themselves (e.g. "To be disbursed to Borrower", "Net to be Disbursed to Borrower") — those are recomputed. If the documents contain no disbursement breakdown, return null. Auto/vehicle loan balances must NOT appear as a separate row in the liabilities array — they are already included in "Notes Payable to: others"; if a document lists an auto loan balance not already within notes payable, fold it into the "Notes Payable to: others" amount instead of listing it separately. (Monthly auto payments still go in auto_payments for the cash flow.) CRITICAL — salary is the GUARANTEED compensation for the current/upcoming season: the guaranteed base salary PLUS every bonus that is guaranteed and paid every year of the contract (e.g. an annual signing-bonus installment, or a guaranteed yearly roster/reporting bonus). Settle this figure only after checking EVERY uploaded document for guarantee language — the contract's compensation paragraphs AND any separate guarantee addendum, rider, exhibit, or amendment (a guarantee is often documented apart from the salary paragraph, and may make only PART of the season salary guaranteed: e.g. a conditional salary guarantee addendum guaranteeing $1,500,000 of a $2,000,000 season salary means salary = 1500000), plus any term sheet or deal summary. When documents disagree, the executed contract and its addenda govern over any summary or term sheet. When the contract pays the athlete a guaranteed bonus each year on top of the base salary, ADD that bonus into salary — never report the base salary alone, and never ALSO count that bonus in other_income (no double counting). Signing-bonus installments and annual bonuses often DIFFER season to season: when the contract carries a year-by-year payment schedule, use the base salary AND the signing-bonus/bonus installment scheduled for THAT SPECIFIC current/upcoming season — never an average annual value and never another season's installment. WORKED EXAMPLE: the documents show a remaining contract value of $39,500,000, an annual professional-contract base salary of $1,000,000, and a guaranteed bonus (Bonus & Commission Income / signing-bonus installment) of $9,000,000 scheduled for the current season -> salary = 10000000 ($1,000,000 base + $9,000,000 guaranteed bonus for that specific season) and contract_remaining = 39500000. salary is NOT 1000000 (the base alone) and NOT 39500000 (the remaining/total contract value is never the salary). SECOND WORKED EXAMPLE (dated signing-bonus installments): a contract's SIGNING BONUS clause pays a total of $60,000,000 "payable in the installments and on the respective dates indicated below": July 15, 2022 $7,000,000; July 1, 2023 $9,000,000; July 1, 2024 $9,500,000; July 1, 2025 $9,000,000; July 1, 2026 $7,500,000; July 1, 2027 $6,750,000; July 1, 2028 $5,750,000; July 1, 2029 $5,500,000. If the season being underwritten is the one beginning in 2026, the installment added into salary is EXACTLY 7500000 (the July 1, 2026 payment) — with a $1,000,000 base salary for that season, salary = 8500000. It is NOT 60000000 (the total signing bonus), NOT an average installment, and NOT the 2025 or any other year's payment: match the installment whose payment date falls in the current/upcoming season being underwritten (per the funding date and deal documents). In that same example, contract_remaining counts the remaining installments from that season forward ($7,500,000 + $6,750,000 + $5,750,000 + $5,500,000 = $25,500,000) plus all remaining seasons' base salaries. CRITICAL — contract_remaining is the TOTAL REMAINING value of the player's contract in dollars: the sum of all compensation (base salaries plus scheduled bonuses/installments) for the current/upcoming season AND every remaining future season of the contract, as the documents state it. If the documents state a remaining or total contract value directly, use that figure. Use 0 if it cannot be determined. Still EXCLUDE everything that is not guaranteed or not paid annually: one-time bonuses, performance/incentive bonuses, non-guaranteed years, options, and endorsement income. If the contract distinguishes total compensation from guaranteed compensation, always use the guaranteed figure. In contract_notes, summarize the contract structure (total value, remaining value, term, guaranteed amount, current season base salary and any guaranteed bonus/installment for that season), state explicitly which portion is guaranteed, and show how the salary figure was composed (e.g. base + this season's bonus). CRITICAL - credit_notes is the memo's Credit paragraph and must summarize the CREDIT REPORT whenever one is among the documents, in 2-5 plain-prose sentences (no markdown), covering: (a) the credit score (the mid score when three bureau scores are shown); (b) any bankruptcies, judgments, tax liens, collections, charge-offs, repossessions, or foreclosures - or state explicitly that there are none; and (c) a PAYMENT-HISTORY review of the tradelines: check EVERY auto loan, mortgage/HELOC, and credit card account on the report for late or missed payments (30/60/90+ day delinquencies and past-due amounts). Name each derogatory finding specifically - the creditor, the account type, how late, and when (e.g. 'Two 30-day late payments on the auto loan with Ally, 03/2025 and 06/2025; all other tradelines paid as agreed.') - or, when the report shows every account current and paid as agreed, say exactly that ('All tradelines report paid as agreed; no late payments.'). Never soften or omit a delinquency the report shows, and never invent one. If NO credit report is among the documents, return null - never write 'reviewed' when nothing was. sponsorship_narrative is a brief factual narrative about the ATHLETE themselves (background, career path, current team and role) if the documents support one; null otherwise. CRITICAL — capture EVERY line item from the Annual Expenditures section of the PFS, omitting NOTHING: map mortgage payments to mortgage_payments, automobile payments to auto_payments, insurance premiums to insurance, alimony/child support to alimony, student loans to student_loans, interest & principal on loans to interest_principal_loans, HOA dues to hoa_payments, and EVERY other expenditure line item (with its exact label and amount) into the other_expenses array. Also capture insurance premiums and alimony/child support whenever they appear in ANY other document. ssn_masked must NEVER contain a full Social Security or Tax ID number — return ONLY the last 4 digits formatted exactly as "XXX-XX-1234". If a full SSN appears in any document, redact it to that format. Do not echo a full SSN anywhere in your output.

""" + LEGAL_NAME_RULE + """

borrower_surname is the SURNAME ALONE, exactly as the identity document's own surname field prints it, in normal capitalization: "Rivas Delgado" for a passport whose Surname line reads "RIVAS DELGADO", "Reyes" for a license reading "REYES, DANIEL JR." (a generational suffix is part of the name but NOT part of the surname). It fills the LAST NAME box of the UCC-1 financing statement, where a compound surname split at the wrong word makes the filing defective, so never guess which word of a name is the surname: return null when no identity document is among the uploads."""


SPONSORSHIP_PROMPT = """You are a credit analyst at South River Capital writing Section V — "Project Sponsorship" — of a credit memorandum for a proposed loan to a professional athlete.

Borrower: {who}

Write the Project Sponsorship narrative about the ATHLETE and their professional playing career using the source material below. Cover, as available: who the athlete is and their current team and role; their path into the league (college/juniors, draft position or signing, prior teams); their career in the league (seasons, performance, notable achievements or distinctions); and their national/country team if they play on one (the country represented and tournaments such as the Olympics, World Championships, or World Cup, with any medals or results).

Rules: write 2 short paragraphs of plain prose — no markdown, bullets, headings, or citations. Professional credit-memo tone. State only facts present in the sources. CRITICAL — include NO financial information of any kind: no dollar figures, salaries, signing bonuses, contract values or lengths-with-values, career earnings, or cap figures (contract and compensation detail belongs in the Contract Analysis section, not here). If a source appears to describe a DIFFERENT person than the borrower (wrong sport, team, or league), silently ignore that source entirely — never mention that a source was disregarded, conflicting, or unavailable. No meta-commentary of any kind: do not discuss the sources, data availability, or what could not be verified; output nothing but the polished narrative itself. The narrative is EXCLUSIVELY about the athlete and their career: do NOT mention the proposed loan, credit facility, funds, use of proceeds, or the lender in any way. Never use the phrase "general business purposes". Return ONLY the narrative text.

SOURCES:

{sources}"""


SALARY_CHECK_PROMPT = """You are a credit analyst at South River Capital reading TWO figures for a credit memorandum from Spotrac: a professional athlete's compensation for the current/upcoming season, and the total remaining value of the contract.

Borrower: {who}
Today's date: {today}
From the deal documents: guaranteed season salary ${doc_salary}; total remaining contract value ${doc_remaining}.

Below is the text of the athlete's Spotrac page. Using SPOTRAC ONLY (ignore the document figures above except to know which season is being underwritten), determine the athlete's compensation for the current/upcoming season as Spotrac's CAP HIT for that season. The cap hit is the base salary PLUS the prorated signing bonus and any roster, workout, or other bonus amounts Spotrac counts for that season — NEVER return the base salary alone when Spotrac lists a cap hit or those bonus components for the season. If the page shows no cap hit (a league or page without one), compose the closest equivalent: the season's base salary plus that season's bonus amounts as Spotrac lists them. Use only the season being underwritten — exclude other seasons, one-time or performance/incentive bonuses Spotrac does not count for the season, and endorsements.

Return ONLY raw JSON, no markdown, no backticks: {{"spotrac_salary":0,"spotrac_remaining":0,"season":null,"note":null}}
- spotrac_salary: that season cap hit (or composed equivalent) as a number. Return 0 if the page cannot support a figure (no contract data, contract expired, or the page describes a DIFFERENT person than the borrower — check name, sport, team).
- spotrac_remaining: the TOTAL REMAINING contract value — the current/upcoming season plus every future season, as Spotrac presents them (use Spotrac's own remaining/total figure when it covers exactly those seasons, otherwise sum the remaining seasons' amounts). Exclude seasons already completed. Return 0 if it cannot be determined.
- season: the season/year the figures belong to (e.g. "2026"), else null.
- note: 1-2 short plain-text sentences an underwriter can read: the figures' composition as Spotrac shows it (base salary + prorated signing bonus + other bonuses; which seasons make up the remaining value), and how much Spotrac marks as GUARANTEED — or why no figure could be determined. Never use markdown.

SPOTRAC ({url}):
{text}"""


def parse_json_reply(message, where: str) -> dict:
    """Join the reply's text blocks, strip any markdown fences, and parse JSON.

    A reply that was cut off at the response token limit (stop_reason
    "max_tokens") is reported as exactly that, with something the user can act
    on - not as the raw JSONDecodeError it causes. Seen 2026-08-20 on an
    11-document upload whose reply outgrew the then-3,000-token cap and
    surfaced in the UI as "Unterminated string starting at: line 155 ...".
    """
    raw = "".join(block.text for block in message.content if block.type == "text")
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean
        clean = clean.rsplit("```", 1)[0].strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        # The model sometimes wraps the JSON in prose ("Here is the ...: {...}"
        # - seen 2026-08-25 on the salary cross-check, which failed with
        # "Expecting value: line 1 column 1"). Parse the outermost {...} span
        # before giving up; a still-broken payload falls through to the
        # original error handling below.
        start, end = clean.find("{"), clean.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(clean[start:end + 1])
            except json.JSONDecodeError:
                pass
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"The {where} reply was cut off before it finished (it hit the "
                "response token limit). Retry the extraction; if it happens "
                "again, extract fewer documents at once - the largest document "
                "drives the longest replies."
            ) from exc
        raise RuntimeError(f"Could not parse extraction response: {exc}") from exc


# Two figures this close are the same figure — Spotrac occasionally rounds a
# prorated bonus/installment slightly differently than the contract states it.
_MATCH_TOLERANCE_PCT = 0.1


def build_salary_check(doc_salary: float, spotrac_salary: float,
                       spotrac_url: str | None, note: str,
                       season: str | None = None, *,
                       doc_remaining: float = 0.0,
                       spotrac_remaining: float = 0.0) -> dict:
    """Assemble the SalaryCheck payload (models.SalaryCheck as a dict).

    The verdict is decided HERE, never by the model, so the UI's match/mismatch
    flag is deterministic: "match"/"mismatch" when both sources produced a
    figure, "spotrac_only"/"docs_only" when one did, "unavailable" when neither.
    Both sources' figures ride on the payload; which one FILLS the extraction
    fields is decided by apply_spotrac_precedence (the memo tab), not here —
    the Structure tab uses this same payload for verification only.
    """
    doc_salary = max(float(doc_salary or 0), 0.0)
    spotrac_salary = max(float(spotrac_salary or 0), 0.0)
    if doc_salary and spotrac_salary:
        tolerance = max(doc_salary, spotrac_salary) * _MATCH_TOLERANCE_PCT / 100
        verdict = ("match" if abs(doc_salary - spotrac_salary) <= max(tolerance, 1.0)
                   else "mismatch")
    elif spotrac_salary:
        verdict = "spotrac_only"
    elif doc_salary:
        verdict = "docs_only"
    else:
        verdict = "unavailable"
    return {
        "spotrac_salary": spotrac_salary,
        "spotrac_remaining": max(float(spotrac_remaining or 0), 0.0),
        "doc_salary": doc_salary,
        "doc_remaining": max(float(doc_remaining or 0), 0.0),
        "season": season,
        "spotrac_url": spotrac_url,
        "verdict": verdict,
        "note": note,
    }


def apply_spotrac_precedence(data: dict) -> None:
    """Make Spotrac the PRIMARY source for the guaranteed season salary and
    the remaining contract value, with the documents as the backup (Lauren,
    2026-08-25 — decided on a deal whose uploaded contract package was stale
    and the extraction picked the prior season's salary row).

    When the Spotrac check produced a figure it replaces the documents' value
    in ``data`` (what prefills Step 2 and the memo); when it produced none,
    the documents' figure stands. The check records which source filled each
    field (salary_source / remaining_source) and keeps the documents' own
    figures (doc_salary / doc_remaining) so the UI shows the executed
    contract as the cross-check. The underwriter's typed value still wins —
    rule 10's "confirmed terms win" is unchanged.
    """
    check = data.get("salary_check") or {}
    if check.get("spotrac_salary"):
        data["salary"] = check["spotrac_salary"]
        check["salary_source"] = "spotrac"
    else:
        check["salary_source"] = "docs"
    if check.get("spotrac_remaining"):
        data["contract_remaining"] = check["spotrac_remaining"]
        check["remaining_source"] = "spotrac"
    else:
        check["remaining_source"] = "docs"


def _check_salary_against_spotrac(client, data: dict, research: dict) -> dict:
    """Third Claude call: read the Spotrac page for the season's CAP HIT
    (base salary + prorated signing bonus + other counted bonuses — Lauren's
    choice 2026-08-06, never the base salary alone) and the total remaining
    contract value, and compare them with what the documents produced.
    Guarantee detail is reported in the note.

    Since 2026-08-25 the figures read here are the PRIMARY prefill for the
    memo's Guaranteed salary / Guaranteed remaining (apply_spotrac_precedence);
    the documents are the backup and stay on the check as the cross-check.
    Returns a SalaryCheck-shaped dict.
    """
    doc_salary = float(data.get("salary") or 0)
    doc_remaining = float(data.get("contract_remaining") or 0)
    text, url = research.get("spotrac_text"), research.get("spotrac_url")
    if not text:
        return build_salary_check(
            doc_salary, 0.0, url,
            "Spotrac page could not be retrieved — the documents' figures were "
            "used; verify the guaranteed salary manually at spotrac.com.",
            doc_remaining=doc_remaining)

    who = ", ".join(
        str(part) for part in (
            data.get("borrower_name"), data.get("sport"),
            data.get("team"), data.get("league"),
        ) if part
    )
    from datetime import date

    message = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=500,
        system=_CLAUDE_CODE_SYSTEM,
        messages=[{
            "role": "user",
            "content": SALARY_CHECK_PROMPT.format(
                who=who or "(name not extracted)",
                today=date.today().isoformat(),
                doc_salary=f"{doc_salary:,.0f}",
                doc_remaining=f"{doc_remaining:,.0f}",
                url=url,
                text=text,
            ),
        }],
    )
    parsed = parse_json_reply(message, "salary cross-check")
    return build_salary_check(
        doc_salary,
        float(parsed.get("spotrac_salary") or 0),
        url,
        str(parsed.get("note") or "").strip(),
        str(parsed.get("season") or "").strip() or None,
        doc_remaining=doc_remaining,
        spotrac_remaining=float(parsed.get("spotrac_remaining") or 0),
    )


def _compose_sponsorship(client, data: dict, research: dict) -> str | None:
    """Second Claude call: write Section V from public research + doc notes.

    The Project Sponsorship section is athlete-centric — researched from
    Wikipedia and Spotrac (see research.py) rather than taken from the deal
    documents. Returns None when there is nothing to write from (no borrower
    name, or neither public source could be fetched), so the caller keeps the
    document-derived narrative instead.
    """
    name = (data.get("borrower_name") or "").strip()
    if not name:
        return None

    if not (research["wiki_text"] or research["spotrac_text"]):
        return None

    sources = []
    if research["wiki_text"]:
        sources.append(f"WIKIPEDIA ({research['wiki_url']}):\n{research['wiki_text']}")
    if research["spotrac_text"]:
        sources.append(f"SPOTRAC ({research['spotrac_url']}):\n{research['spotrac_text']}")
    if data.get("contract_notes"):
        sources.append(f"DEAL DOCUMENTS (contract notes):\n{data['contract_notes']}")

    who = ", ".join(
        str(part) for part in (
            name, data.get("sport"), data.get("team"), data.get("league"),
        ) if part
    )
    message = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=700,
        system=_CLAUDE_CODE_SYSTEM,
        messages=[{
            "role": "user",
            "content": SPONSORSHIP_PROMPT.format(who=who, sources="\n\n".join(sources)),
        }],
    )
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text or None


def extract_documents(docs: list[UploadedDoc]) -> Extraction:
    """Send documents to Claude and parse the structured response.

    Authenticates STRICTLY with a Claude subscription usage token (OAuth),
    never an API key. Generate the token with `claude setup-token` and put it
    in .env as CLAUDE_CODE_OAUTH_TOKEN (ANTHROPIC_AUTH_TOKEN also works).

    Raises RuntimeError if no usage token is set or the response can't be parsed.
    """
    token = usage_token()
    if not token:
        raise RuntimeError(
            "No Claude usage token is set. Run `claude setup-token` and put the "
            "token in .env as CLAUDE_CODE_OAUTH_TOKEN. Extraction uses your Claude "
            "subscription usage, not a pay-per-token API key."
        )

    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The 'anthropic' package is not installed. Run: pip install -e '.[dev]'"
        ) from exc

    client = build_client(anthropic, token)

    # Blocks are built from each file's own magic bytes, never the browser's
    # reported MIME — see doc_blocks. Word/Excel arrive as extracted text.
    content: list[dict] = build_document_blocks(docs)
    log_request_manifest("extraction", docs, content)
    check_request_size("extraction", docs, content)
    content.append({"type": "text", "text": PROMPT})

    try:
        message = create_with_retry(
            client, "extraction",
            model=EXTRACTION_MODEL,
            # The reply must carry every PFS line item, debt-schedule row,
            # repayment row and uses-of-funds line - a document-heavy deal
            # outgrew 3,000 tokens on 2026-08-20 (truncated JSON), so the
            # budget is sized well past the largest reply seen.
            max_tokens=8000,
            system=_CLAUDE_CODE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as exc:
        raise RuntimeError(
            "Claude rejected the usage token. Regenerate it with `claude setup-token`, "
            "update CLAUDE_CODE_OAUTH_TOKEN in .env, and restart the backend."
        ) from exc
    except anthropic.APIError as exc:
        logging.getLogger(__name__).error("Extraction failed: %s", exc)
        raise RuntimeError(describe_api_error(exc, "extraction")) from exc

    data = parse_json_reply(message, "extraction")

    # Belt-and-suspenders: re-mask the SSN server-side regardless of model output.
    if data.get("ssn_masked"):
        data["ssn_masked"] = mask_ssn(data["ssn_masked"])

    # Public research (Wikipedia + Spotrac) is fetched ONCE and feeds two
    # best-effort steps: the Section V narrative and the guaranteed-salary
    # cross-check. gather_athlete_research never raises (per-source failures
    # are swallowed inside), and neither step may ever break /api/extract.
    name = (data.get("borrower_name") or "").strip()
    research = gather_athlete_research(name, data.get("sport"), data.get("league"))

    # Section V (Project Sponsorship) is athlete-centric: researched from
    # Wikipedia + Spotrac and composed by a second Claude call. Any failure
    # keeps whatever narrative the documents provided.
    try:
        narrative = _compose_sponsorship(client, data, research)
        if narrative:
            data["sponsorship_narrative"] = narrative
    except Exception as exc:  # noqa: BLE001 - research must never break extraction
        logging.getLogger(__name__).warning("Sponsorship research failed: %s", exc)

    # Spotrac read of the guaranteed salary and remaining contract value —
    # the PRIMARY prefill for those two Step 2 fields since 2026-08-25, with
    # the documents as the backup (and shown as the cross-check).
    try:
        data["salary_check"] = _check_salary_against_spotrac(client, data, research)
    except Exception as exc:  # noqa: BLE001 - research must never break extraction
        logging.getLogger(__name__).warning("Salary cross-check failed: %s", exc)
        data["salary_check"] = build_salary_check(
            data.get("salary") or 0, 0.0, research.get("spotrac_url"),
            "The Spotrac check could not be run — the documents' figures were "
            "used; verify the guaranteed salary manually at spotrac.com.",
            doc_remaining=data.get("contract_remaining") or 0)
    apply_spotrac_precedence(data)

    return Extraction(**data)
