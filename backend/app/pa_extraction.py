"""Document extraction for the Participation Agreement Builder.

Reuses the same Claude subscription usage-token (OAuth) authentication as the
credit-memo extraction (see ``extraction.py``) — never a pay-per-token API key.
The prompt here is tuned for participation deals: it reads the loan & security
agreement / term sheet (and an optional participation term sheet) and returns the
fields needed to fill the Participation Agreement and its Key Terms exhibit.
"""

from __future__ import annotations

import json
import logging
import os

from .models import UploadedDoc
from .pa_models import PAExtraction
# Reuse the exact, proven auth pieces from the credit-memo extraction.
from .doc_blocks import build_document_blocks
from .extraction import (
    LEGAL_NAME_RULE,
    usage_token, build_client, create_with_retry, log_request_manifest,
    check_request_size, describe_api_error, parse_json_reply,
    _OAUTH_BETA_HEADER, _CLAUDE_CODE_SYSTEM,
)

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-sonnet-4-6")

PROMPT = """You are an analyst at South River Capital LLC preparing a loan PARTICIPATION AGREEMENT. South River ("Lender") originates a loan to a borrower and sells an undivided fractional participation interest in that loan to a Participant (by default "Brookridge Opportunistic Credit Fund, LP").

You have been given one or more deal documents, which may include any combination of: a South River credit memorandum prepared for the loan, a Loan and Security Agreement, a promissory note, a term sheet, a commitment letter, a closing/settlement statement, or a participation term sheet. A credit memorandum is an excellent source for the borrower, the total loan amount, the interest rate, the origination fee, and the funding/closing date.

Read ALL documents together and extract the fields below. Use the most authoritative source for each. Return ONLY raw JSON, no markdown, no backticks:

{"borrower_name":null,"agreement_date":null,"loan_agreement_date":null,"loan_amount":0,"loan_number":null,"interest_rate_apr":0,"origination_fee_pct":0,"participant_name":null,"participation_percentage":0,"participant_loan_amount":0,"late_fee_share_pct":0,"servicing_fee_pct":0,"participant_signatory_name":null,"participant_signatory_title":null,"notes":null}

Rules:
- Numbers must be plain numbers with no "$", commas, or "%": loan_amount 675000 (not "$675,000"); interest_rate_apr 12 (not "12%"); origination_fee_pct 2 means 2%. Use 0 when a number is not stated. Use null for missing strings.
- borrower_name: the borrower on the loan (the athlete/obligor).
- loan_amount: the TOTAL principal amount of the loan (the whole facility), not the participant's share.
- loan_agreement_date: the date OF the Loan and Security Agreement (the loan documents), formatted "Month D, YYYY" (e.g. "October 6, 2025").
- agreement_date: the date the participation agreement is made / the closing or funding date if stated, formatted "Month D, YYYY". If no such date appears, return null.
- loan_number: the loan or account number if shown (digits as a string).
- interest_rate_apr: the loan's stated interest rate / APR as a number.
- origination_fee_pct: the origination fee percentage if stated.
- The PARTICIPATION-SPECIFIC terms are usually set by South River separately and are OFTEN NOT in the borrower's loan documents. Only fill them if a document (e.g. a participation term sheet) explicitly states them; otherwise leave them 0 / null: participant_name, participation_percentage (participant's % of the loan), participant_loan_amount (the participant's dollar share / purchase price), late_fee_share_pct (participant's share of late fees), servicing_fee_pct, participant_signatory_name, participant_signatory_title.
- participant_name: only set if a participant OTHER than the default is clearly named in the documents; otherwise null.
- notes: one or two short sentences flagging anything ambiguous, conflicting, or worth the analyst's attention (e.g. multiple candidate dates, rate expressed as a range). Keep it brief; null if nothing notable.
- Do not invent values. If a figure is not in the documents, return 0 or null and (if relevant) mention it in notes.

""" + LEGAL_NAME_RULE


def extract_documents(docs: list[UploadedDoc]) -> PAExtraction:
    """Send the uploaded deal documents to Claude and parse the structured response.

    Authenticates strictly with the Claude subscription usage token (OAuth).
    Raises RuntimeError if no token is set or the response can't be parsed.
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

    # Magic-byte sniffing, not the browser's MIME — see doc_blocks.
    content: list[dict] = build_document_blocks(docs)
    log_request_manifest("participation-agreement extraction", docs, content)
    check_request_size("participation-agreement extraction", docs, content)
    content.append({"type": "text", "text": PROMPT})

    try:
        message = create_with_retry(
            client, "participation-agreement extraction",
            model=EXTRACTION_MODEL,
            max_tokens=2000,
            system=_CLAUDE_CODE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as exc:
        raise RuntimeError(
            "Claude rejected the usage token. Regenerate it with `claude setup-token`, "
            "update CLAUDE_CODE_OAUTH_TOKEN in .env, and restart the backend."
        ) from exc
    except anthropic.APIError as exc:
        logging.getLogger(__name__).error("PA extraction failed: %s", exc)
        raise RuntimeError(describe_api_error(exc, "extraction")) from exc

    data = parse_json_reply(message, "extraction")

    return PAExtraction(**data)
