"""Document extraction via the Anthropic API.

This is the security-critical reason the project moved to a backend: the
credential lives here in an environment variable and never reaches the browser.

Extraction authenticates STRICTLY with a Claude subscription usage token
(an OAuth token), never a pay-per-token API key. See `extract_documents`.

The extraction PROMPT is the second authoritative artifact in this project
(alongside calculations.py). Its rules about what qualifies as income
(documented, recurring, already received or contractually committed - never a
projection, a capital gain, or an income stream that ends before maturity),
capturing every liability and expenditure line, SSN redaction, and auto-loan
folding are load-bearing — keep them in sync with the rules documented in
calculations.py.

Section V's borrower/business narrative is written by this same call from the
uploaded documents (``background_narrative``). The athlete builder this one was
forked from instead researched that section from Wikipedia + Spotrac and
cross-checked the guaranteed salary against Spotrac in two further Claude
calls. Neither is carried over: no public source indexes a private borrower's
income the way Spotrac indexes a playing contract, so the documents are the
only source here and the underwriter is the only cross-check.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .models import Extraction, UploadedDoc
from .calculations import mask_ssn
from .doc_blocks import build_document_blocks

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


PROMPT = """You are a financial analyst at South River Capital preparing a credit memorandum for a NON-SPORTS loan. The borrower is a business owner or executive, an investment / real-estate borrower, or a general consumer or commercial borrower - never a professional athlete, and there is no team, league or playing contract anywhere in this deal. You have been given one or more documents which may include any combination of: Personal Financial Statement (PFS), personal and business tax returns (1040 with schedules, K-1s, 1120S/1065), business financial statements, pay stubs or W-2s, an employment or compensation agreement, leases or rent rolls, an appraisal or valuation, a credit report, a term sheet, or other deal documents.

Read ALL documents together and extract every piece of information needed for the credit memo. Use the most authoritative source for each field.

Return ONLY raw JSON, no markdown, no backticks:

{"borrower_name":null,"borrower_surname":null,"dob":null,"address":null,"phone":null,"employer":null,"industry":null,"occupation":null,"ssn_masked":null,"drivers_license":null,"advisor":null,"qualifying_income":0,"contracted_income":0,"other_income":0,"total_income":0,"federal_taxes":0,"mortgage_payments":0,"hoa_payments":0,"student_loans":0,"interest_principal_loans":0,"insurance":0,"alimony":0,"auto_payments":0,"living_expenses":0,"other_expenses":[],"total_expenditures":0,"assets":[],"total_assets":0,"liabilities":[],"total_liabilities":0,"net_worth":0,"collateral_value":0,"collateral_desc":null,"pfs_date":null,"debt_schedule":[],"facility_total_due":0,"loan_amount":0,"interest_rate_pct":0,"origination_fee_pct":0,"loan_term_months":0,"repayment_schedule":[],"uses_of_funds":null,"credit_notes":null,"income_notes":null,"background_narrative":null,"headline":null,"asset_class":null,"purpose":null,"exec_summary":null,"property_owner":null,"guarantors":null,"prepayment":null,"key_conditions":null,"collateral_notes":null,"collateral_coverage":[],"financials":[],"financial_notes":null,"risks":[],"required_documents":[],"repay_primary":null,"repay_secondary":null,"repay_tertiary":null}

Rules: assets and liabilities are arrays of {label, amount}. other_expenses is array of {label, amount}. Use 0 for missing numbers, null for missing strings. Include ALL non-zero line items. employer is who pays the borrower: the operating company the borrower OWNS on a business-owner deal, their employer on an executive or salaried deal, or the entity that holds the income-producing assets on an investment deal (null if the documents name none). industry is the borrower's industry or sector in a few words as the documents describe it ("commercial HVAC contracting", "multifamily real estate"); never guess it from the borrower's name. occupation is the borrower's role or title ("owner and president", "managing partner", "regional sales director"). advisor is the borrower's professional adviser on the deal if the documents name one - CPA, attorney, wealth manager or business manager - as a name, with the role in parentheses if given. CRITICAL — the liabilities array must include EVERY liability line item EXACTLY as the uploaded Personal Financial Statement lists them, with their exact labels and amounts, omitting NOTHING: all Notes Payable categories (e.g., "Notes Payable: Contract Based", "Notes Payable to: others"), mortgage debt, credit card debt, taxes payable, and every other liability shown. Never merge, rename, or drop a liability row (the ONLY exception: auto loan balances fold into Notes Payable to: others as instructed below). CRITICAL — pfs_date is the date the Personal Financial Statement was PREPARED, as ISO yyyy-mm-dd (e.g. a "Completed on:" line, a date printed at the top of page 1, or the signature date): return it whenever a PFS is among the documents, else null. Do not use the credit report date, the contract date, or today's date - with ONE exception: when NO PFS is among the documents but debt_schedule was built from the credit report (see the no-PFS fallback below), return the credit report's own report/pull date as pfs_date, so those balances can be rolled forward from the date they were true. CRITICAL — debt_schedule is the PER-LOAN detail from the PFS's own detail schedules, which appear on a later page than the page-1 summary totals: Schedule D (Personal Residence / Real Estate Investments & Mortgage Debt), Schedule F (Notes Payable) and Schedule G (Contract Based Notes Payable). Return one object per financed debt listed in those schedules, omitting NOTHING, each {"lender","category","balance","payment","payment_period","origination","maturity","rate_pct","description"}, where:
- lender is the Lender / creditor name as shown ("Northgate Bank", "MidState Bank", "Credit Cards", "Sports Finance Fund, LP"). If a Schedule D row names no lender, use the property address.
- category is EXACTLY one of "mortgage_debt" (any Schedule D row), "notes_payable_others" (any Schedule F row) or "notes_payable_contract" (any Schedule G row) — this says which page-1 summary liability the row rolls up into, so never guess a different string.
- balance is the "Present Loan Balance" (Schedule D) or "Outstanding Amount" (Schedule F/G) as a positive number. Skip rows with no balance.
- payment is the "Monthly Payment" (Schedule D) or "Amount / Pay Period" (Schedule F/G) as a positive number; 0 if the row shows none (revolving lines such as credit cards typically show none).
- payment_period is how often that payment is made. Schedule D's column is headed "Monthly Payment", so return "monthly" for every Schedule D row that has a payment. Schedule F/G is headed "Amount / Pay Period", which is NOT necessarily monthly: return "monthly" when the document indicates a monthly payment, copy the period the document states when it states one (e.g. "per game check", "semi-monthly", "annual"), and return "" when the form shows a payment amount but names no period at all. Never invent a period.
CRITICAL — ROW ALIGNMENT. These schedules are tables, and the text layer of a scanned or Excel-exported PFS often reaches you with the columns out of order, so values can appear far from their own row. Every figure you return for a debt must come from the SAME TABLE ROW as that debt's lender: use the lender name and the Outstanding Amount as the anchor and read across that row for its payment, origination, maturity and rate. Do NOT pair a payment or a maturity date with a lender just because the two appear near each other in the text stream. If you cannot confidently tell which row a payment or maturity belongs to, return 0 / "" for it rather than attaching it to the wrong debt — a misplaced payment silently pays down the wrong loan in the memo. Revolving lines (a "Credit Cards" row, an unsecured note) normally show NO payment and NO maturity date on these forms: do not lend them a payment or maturity that belongs to a term loan on another row.
- origination is the "Date of Origination" (Schedule F/G) or the "Purchase Year" (Schedule D), copied as shown. maturity is the "Loan Maturity Date" / "Maturity Date", copied as shown. Copy these two VERBATIM even when they look wrong or malformed (a typo such as "08/072053", or a placeholder such as "7/17/1905" or "1/0/1900") — do NOT correct, reformat, or invent them, and return "" when the cell is blank. The credit memo sanitizes them.
- rate_pct is the schedule's stated Interest Rate as a percent number (13.5 for "13.5%"), 0 if blank. description is the "Reason for Debt" / collateral note (Schedule F/G) or the property address (Schedule D).
These rows are DETAIL, not additional liabilities: they must ALSO stay inside the page-1 summary amounts in the liabilities array (a Schedule D mortgage is already inside "Mortgage Debt"; a Schedule F auto note is already inside "Notes Payable to: others"). Never add a schedule row to liabilities as its own line. If the documents contain no such schedules, return [] - UNLESS no PFS is among the documents at all but a CREDIT REPORT is. NO-PFS FALLBACK: in that case build debt_schedule from the credit report's OPEN tradelines instead - one row per open account with a balance (auto loans, mortgages/HELOCs, credit cards, student loans, and other installment notes), omitting closed accounts and zero balances - where lender is the creditor name as reported, balance is the current balance, payment is the scheduled monthly payment as reported (0 for revolving credit-card lines - they do not amortize), payment_period is "monthly" for an installment account with a payment and "" otherwise, origination is the account-opened date as shown, maturity is "" unless the report states one, rate_pct 0 unless stated, description is the account type as the report words it, and category is "mortgage_debt" for mortgages/HELOCs and "notes_payable_others" for everything else. Keep these rows CONSISTENT with the liabilities array: with no PFS, build the liabilities from the same credit-report balances (mortgage debt as its own line; the rest inside "Notes Payable to: others" per the auto-loan folding rule; credit cards may be their own "Credit Card Debt" line), so each row's category matches a liability it is actually inside of. CRITICAL — capture the PROPOSED facility's deal terms exactly as the documents state them (a term sheet, approval, or commitment line such as "Loan Amount: $4,435,000", "Origination: 3%", "Rate: 13.5%", "Term: 6 months"): loan_amount is the loan / proposed facility PRINCIPAL as a numeric dollar amount (e.g. "$4.435M" -> 4435000, "$4,435,000" -> 4435000); interest_rate_pct is the facility's annual interest rate as a percent NUMBER (e.g. "13.5%" -> 13.5); origination_fee_pct is the origination/upfront/lender fee as a percent NUMBER (e.g. "3%" or "Origination: 3%" -> 3). Use 0 for any of these not stated in the documents. loan_term_months is the term/duration of the PROPOSED facility expressed in WHOLE months, as stated in the deal documents (e.g. a term sheet line such as "Term: 6 months", a stated loan period, or a "6 mo." note). If the term is stated in years, convert to months (1 year = 12 months). If the documents give only funding and maturity dates with no explicitly stated term, return 0 (the memo computes the span from the dates). Use 0 if no term is stated. facility_total_due is the proposed facility's TOTAL amount due (principal + interest) if any amortization schedule, payoff figure, or proposed-facility liability appears in the documents; otherwise 0. repayment_schedule is the proposed facility's repayment/amortization schedule WHEN the documents contain one (e.g., a payment/amortization table for the new loan): return an array with one object per scheduled payment, IN ORDER, each {"date","interest","principal","total"} where date is the payment date copied as shown (a short string such as "15-Jul-26"), interest and principal are that payment's dollar amounts (use 0 where a column is blank or zero — typically principal is 0 until the final balloon payment), and total is that payment's interest + principal. Copy the figures EXACTLY as the schedule lists them; do not recompute, round, or reorder. Capture ONLY the proposed facility's own schedule (not schedules for the borrower's other/existing debts). If the documents contain no repayment schedule for the proposed facility, return an empty array []. CRITICAL — uses_of_funds is the proposed facility's disbursement / sources-and-uses / settlement breakdown WHEN the documents contain one (e.g. a "Disbursement", "Uses of Funds", or settlement-statement table showing how the gross loan is applied): capture EVERY line provided, omitting NOTHING, as an object {"gross_loan_amount":0,"deductions":[{"label","amount"}],"additional_costs":[{"label","amount"}]}. gross_loan_amount is the full proposed facility (gross) amount. deductions are the fees and payoffs subtracted from the gross loan to reach the amount "to be disbursed to Borrower" — e.g. origination fee, underwriting fee, payoffs of existing loans (such as a "Bank Payments thru <date>" payoff line), and legal/closing costs. additional_costs are amounts funded from the loan and carved out of the to-Borrower figure to reach the NET disbursed — e.g. Death & Disgrace (DDD) insurance premium and Interest Reserve. Copy each label as shown and give every amount as a POSITIVE dollar magnitude (do not use negatives or parentheses). Do NOT include any subtotal/total lines themselves (e.g. "To be disbursed to Borrower", "Net to be Disbursed to Borrower") — those are recomputed. If the documents contain no disbursement breakdown, return null. Auto/vehicle loan balances must NOT appear as a separate row in the liabilities array — they are already included in "Notes Payable to: others"; if a document lists an auto loan balance not already within notes payable, fold it into the "Notes Payable to: others" amount instead of listing it separately. (Monthly auto payments still go in auto_payments for the cash flow.) CRITICAL — qualifying_income is the borrower's ANNUAL income that underwriting can stand behind: documented, recurring, and already received or contractually committed - never projected, hoped-for, or one-time. Settle it only after reading EVERY uploaded document, and compose it from whichever of these the documents actually support:
- WAGES AND SALARY. Base salary and any recurring guaranteed bonus from a W-2, pay stub, offer letter or employment agreement. A discretionary or performance bonus is NOT included unless the agreement obliges the employer to pay it; when a bonus is included, say in income_notes which document obliges it.
- BUSINESS INCOME for an owner. The income the borrower can actually draw from the business: K-1 ordinary business income and guaranteed payments, S-corp wages plus distributions, or a sole proprietor's Schedule C net profit, taken from TAX RETURNS in preference to any internal statement or projection. Multiply by the borrower's OWNERSHIP PERCENTAGE when the entity is not wholly theirs, and state that percentage in income_notes. Do NOT add back depreciation, amortization or any other non-cash item into this figure - if the documents support an add-back, describe it in income_notes and leave the decision to the underwriter.
- NET RENTAL INCOME. Gross scheduled rents LESS operating expenses, taxes, insurance and existing mortgage debt service on the same property, from a Schedule E, lease, or rent roll. Never report gross rents as income.
- INVESTMENT INCOME. Recurring interest, dividends and required distributions from a Schedule B, 1099 or statement. EXCLUDE realized and unrealized capital gains and unrealized appreciation.
- RETIREMENT, PENSION, ANNUITY, TRUST, ROYALTY or SUPPORT income that documents show as ongoing.
EXCLUDE from qualifying_income: capital gains and asset-sale proceeds, loan proceeds (including this facility), gifts and one-time transfers, forgiven debt, any income the documents show ENDING before the facility matures, and any figure that appears only in a projection, pro forma or borrower-prepared summary with no return or statement behind it. WHEN INCOME VARIES YEAR TO YEAR, report the MOST RECENT COMPLETE YEAR as documented and state the prior year(s) and the trend in income_notes - never silently average the years, and never pick the highest year. When documents disagree, a filed tax return governs over a business-prepared statement, which governs over a borrower-prepared summary or term sheet. qualifying_income is the ANNUAL total of the components above, and whatever you count there must NOT also be counted in other_income (no double counting). Use 0 only when no document supports any income figure at all. CRITICAL — contracted_income is a SUBSET of qualifying_income, never an addition to it: the portion of the income you already counted there that is COMMITTED BY CONTRACT OR LEASE over the facility's own term - the only thing in a non-sports deal that plays the role a guaranteed playing contract plays for an athlete. Count it ONLY where an executed document obliges a named payor to pay a stated amount over a stated period: signed lease rents running through the facility's maturity, an employment or compensation agreement's guaranteed (non-discretionary) pay, a contracted management or service fee, or a note receivable's scheduled payments to the borrower. Sum the committed amounts falling between the funding date and maturity, and name the documents and payors in income_notes. Most non-sports deals have NO such commitment: return 0 then rather than reusing qualifying_income, and never count a month-to-month tenancy, an at-will salary, a discretionary bonus, or expected business revenue as contracted. CRITICAL — collateral_value and collateral_desc describe the asset PLEDGED to this facility, when the documents identify one (a term sheet's collateral paragraph, a mortgage or deed of trust, a security or pledge agreement, an appraisal obtained for this loan). collateral_value is its value as the most authoritative document states it - a third-party appraisal or broker valuation over a statement value, and a statement value over the borrower's own estimate - as a positive number; collateral_desc identifies it in a few words, copying the documents' own description ("mixed-use building, 12 Harbor Road", "pledged brokerage account at Fidelity", "100% of the membership interests in the operating company"). Return 0 and null when the documents pledge nothing specific and the facility rests on the personal guarantee: never put the borrower's total assets, their residence, or an unpledged property here, and never carry over a PFS value for an asset the deal documents do not pledge. In income_notes, state the FINAL composition only. Write it as settled fact for an approver to read — it is printed verbatim in the memo's Income Analysis section. Never show your working: no "REVISED", no restating a figure you first considered, no self-correction, no deliberation about what to include, no "to avoid double-counting" asides. Decide, then report the decision. Summarize how qualifying_income was composed and documented: each component and its amount, the document each came from, the ownership percentage used for any business income, what was excluded and why, the prior year's figures and the trend where income varies, any contracted commitment counted in contracted_income with its payor and end date, and any add-back the documents would support but you did not take. State plainly anything an underwriter must verify. CRITICAL - credit_notes is the memo's Credit paragraph and must summarize the CREDIT REPORT whenever one is among the documents, in 2-5 plain-prose sentences (no markdown), covering: (a) the credit score (the mid score when three bureau scores are shown); (b) any bankruptcies, judgments, tax liens, collections, charge-offs, repossessions, or foreclosures - or state explicitly that there are none; and (c) a PAYMENT-HISTORY review of the tradelines: check EVERY auto loan, mortgage/HELOC, and credit card account on the report for late or missed payments (30/60/90+ day delinquencies and past-due amounts). Name each derogatory finding specifically - the creditor, the account type, how late, and when (e.g. 'Two 30-day late payments on the auto loan with Ally, 03/2025 and 06/2025; all other tradelines paid as agreed.') - or, when the report shows every account current and paid as agreed, say exactly that ('All tradelines report paid as agreed; no late payments.'). Never soften or omit a delinquency the report shows, and never invent one. If NO credit report is among the documents, return null - never write 'reviewed' when nothing was. background_narrative is Section V of the memo - 2 short paragraphs of plain prose (no markdown, bullets or headings) about the BORROWER and, where they own one, their business: who they are, their role and tenure, the business's line of work, how long it has operated, its scale as the documents show it (revenue, employees, locations, properties held), its customer or tenant base, and the industry it sells into. Write it from the UPLOADED DOCUMENTS ONLY - this is not a research task and there are no outside sources. State only what a document supports, name no figure the documents do not carry, and include NO information about the proposed loan, its proceeds, or the lender. Return null when the documents support no narrative at all; never pad it with generalities about the industry. CRITICAL — THE MEMO'S OWN SECTIONS. The credit memorandum this feeds opens with the deal and closes with a recommendation, so capture the following from the documents, each null or [] when the documents do not support it. Never invent any of it.
- headline: a one-line title for the deal, in the form "$4M First Lien Loan - McCormick Paints, Frederick, MD": the amount, what secures it, the borrower, and the location or business where the documents give one.
- asset_class: what kind of credit this is, in a few words - "Commercial Real Estate", "Operating business", "Residential investment", "Consumer".
- purpose: what the money is for, in a few words as the documents put it ("Refinance of existing bank loan", "Working capital and equipment").
- exec_summary: 2 or 3 short paragraphs of plain prose, the memo's Executive Summary: who the borrower is and what they are asking for; the term, rate, points and repayment structure; what secures it and at what loan-to-value; and - this is the part that matters most - WHAT THE CREDIT ACTUALLY RESTS ON. Say plainly when it rests on the collateral rather than on cash flow, or on the guarantee rather than on either. State the weaknesses in the same breath as the strengths: a memo that reads like a pitch is useless to an approver.
- property_owner: the entity that holds title to the collateral when it is NOT the borrower (a separate property-owning affiliate). Null when the borrower owns it directly.
- guarantors: who guarantees the facility, copied as the documents put it - "None personal", or the names and the form of guarantee.
- prepayment: the prepayment terms if stated. key_conditions: the conditions of closing the documents name, as one short line.
- collateral_notes: the Collateral Analysis narrative - what the asset is, its size, condition, location and use; how it was valued, by whom, on what date and on what basis; and anything that qualifies the value (an appraisal addressed to another lender, a dated valuation, a flood zone, an environmental concern). Null when nothing is pledged.
- collateral_coverage: one entry per DISTINCT basis of value the documents support, each {"basis","value"} - an appraisal (name the appraiser and date), a current offer, a tax assessment, a stressed re-capitalisation the documents themselves compute. Do NOT invent a stress case; capture only bases the documents state.
- financials: one entry per reporting period, OLDEST FIRST, each {"period","revenue","ebitda","net_income"}. "period" is the column heading as the documents label it ("FY2024", "YTD 2026", "TTM 6/30/26") - never present a stub or year-to-date period as a full year. Use null for a figure the documents do not state: the memo prints [TBD] there, which is the honest answer and better than a guess. Return [] when there are no statements at all.
- financial_notes: one or two sentences on where the figures came from and what is missing or unaudited.
- risks: one entry per material risk, each {"risk","detail","mitigant"}. "risk" is a short label ("Dated appraisal", "Customer concentration", "Interest only / refinance risk"); "detail" is what the exposure actually is; "mitigant" is what answers it. LEAVE mitigant EMPTY when the documents offer none - the memo prints the risk with the mitigant marked outstanding, and inventing a comfort nobody has is the single most dangerous thing you could do here. Include the risks a reader would hold against the deal, not only the ones the documents volunteer.
- required_documents: what must be in hand before closing, one short line each (valuations and reliance letters, environmental reports, entity documents and resolutions, title, insurance, payoff letters, financial statements).
- repay_primary / repay_secondary / repay_tertiary: the three repayment sources in order, one sentence each - how the facility is serviced, what repays it if that fails, and what the Lender falls back on last.
 CRITICAL — capture EVERY line item from the Annual Expenditures section of the PFS, omitting NOTHING: map mortgage payments to mortgage_payments, automobile payments to auto_payments, insurance premiums to insurance, alimony/child support to alimony, student loans to student_loans, interest & principal on loans to interest_principal_loans, HOA dues to hoa_payments, and EVERY other expenditure line item (with its exact label and amount) into the other_expenses array. Also capture insurance premiums and alimony/child support whenever they appear in ANY other document. ssn_masked must NEVER contain a full Social Security or Tax ID number — return ONLY the last 4 digits formatted exactly as "XXX-XX-1234". If a full SSN appears in any document, redact it to that format. Do not echo a full SSN anywhere in your output.

""" + LEGAL_NAME_RULE + """

borrower_surname is the SURNAME ALONE, exactly as the identity document's own surname field prints it, in normal capitalization: "Rivas Delgado" for a passport whose Surname line reads "RIVAS DELGADO", "Reyes" for a license reading "REYES, DANIEL JR." (a generational suffix is part of the name but NOT part of the surname). It fills the LAST NAME box of the UCC-1 financing statement, where a compound surname split at the wrong word makes the filing defective, so never guess which word of a name is the surname: return null when no identity document is among the uploads."""


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

    return Extraction(**data)
