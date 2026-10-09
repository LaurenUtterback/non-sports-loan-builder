"""Document extraction for the Loan Documents builder's Team & Contract fields.

Reuses the same Claude subscription usage-token (OAuth) authentication as the
credit-memo extraction (see ``extraction.py``) — never a pay-per-token API key.
The prompt is tuned to pull the details the closing package needs about the
athlete's employer and playing contract: the team's name and mailing address
(the Payment Direction Letter is mailed there), the league, and how the
contract should be referenced (title + "as of" date) in the Loan & Security
Agreement's Contract definition.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from pydantic import BaseModel

from .models import UploadedDoc
# Reuse the exact, proven auth pieces from the credit-memo extraction.
from .doc_blocks import build_document_blocks
from .extraction import (
    usage_token, build_client, create_with_retry, log_request_manifest,
    check_request_size, describe_api_error, parse_json_reply,
    LEGAL_NAME_RULE, _OAUTH_BETA_HEADER, _CLAUDE_CODE_SYSTEM,
)

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-sonnet-4-6")


class TeamContractExtraction(BaseModel):
    """What Claude pulls out of the uploaded contract / deal documents."""

    player_name: Optional[str] = None
    # The ID's own surname field, when an identity document is among the
    # uploads - it fills the UCC-1's LAST NAME box whole (see loandocs.py).
    player_surname: Optional[str] = None
    team_name: Optional[str] = None
    team_street: Optional[str] = None
    team_city_state_zip: Optional[str] = None
    league: Optional[str] = None
    contract_title: Optional[str] = None
    contract_date: Optional[str] = None   # ISO yyyy-mm-dd for the date picker
    notes: Optional[str] = None


PROMPT = """You are an analyst at South River Capital LLC assembling the CLOSING DOCUMENTS for a loan to a professional athlete. You have been given one or more deal documents, which may include the athlete's professional playing contract (or a summary/addendum of it), a term sheet, a South River credit memorandum, or other loan documents.

Extract the TEAM and CONTRACT details below. They are used two ways: (1) the Loan & Security Agreement defines the "Contract" as: the <contract_title> between <player> and <team> dated <contract date>; and (2) a Payment Direction Letter is MAILED to the team's front office at the address you extract.

Return ONLY raw JSON, no markdown, no backticks:

{"player_name":null,"player_surname":null,"team_name":null,"team_street":null,"team_city_state_zip":null,"league":null,"contract_title":null,"contract_date":null,"notes":null}

Rules:
- Use null for anything not stated in the documents. Do not invent values.
- player_name: the athlete who is party to the contract, named as the rule at the bottom of these instructions requires.
- player_surname: the SURNAME ALONE, exactly as a government ID's own surname field prints it, in normal capitalization ("Rivas Delgado" for a passport whose Surname line reads "RIVAS DELGADO"; a generational suffix is NOT part of the surname). It fills the LAST NAME box of the UCC-1 financing statement, where a compound surname split at the wrong word makes the filing defective - so never guess which word of a name is the surname: null unless an identity document is among the documents.
- team_name: the full club/team name employing the athlete, e.g. "Baltimore Orioles".
- team_street: the street line of the club's mailing address AS STATED in the documents (e.g. "333 West Camden Street"). team_city_state_zip: the rest of that address on one line (e.g. "Baltimore, MD 21201" or "Vancouver, BC V6B 4Y8, Canada"). The letter is physically mailed there, so only use an address that appears in the documents; if none does, return null for both and say so in notes.
- league: the league's usual abbreviation (MLB, NBA, NFL, NHL, MLS, WNBA, ...).
- contract_title: a short legal reference for the contract, preferring the document's own title prefixed with the league when helpful — e.g. "MLB Uniform Player's Contract", "NBA Standard Player Contract", or "MLB Professional Contract" when the papers just call it a player contract. Null if you cannot tell.
- contract_date: the date the contract was made / its "as of" date, formatted "YYYY-MM-DD". If the documents show several contract-related dates (signing date vs. effective date), prefer the "dated as of" date and mention the others in notes. Null if not stated.
- notes: one or two short sentences flagging anything missing, ambiguous, or conflicting (e.g. "Team address not shown in the documents", "Contract is an extension dated ..."). Null if nothing notable.

""" + LEGAL_NAME_RULE


class MemoDealExtraction(BaseModel):
    """Deal-level fields read from a previously generated credit memorandum."""

    borrower_name: Optional[str] = None
    borrower_street: Optional[str] = None
    borrower_city: Optional[str] = None
    borrower_state_abbr: Optional[str] = None
    borrower_zip: Optional[str] = None
    borrower_state: Optional[str] = None   # spelled out, e.g. "Florida"
    occupation: Optional[str] = None
    team_name: Optional[str] = None
    league: Optional[str] = None
    loan_amount: Optional[float] = None
    interest_rate_pct: Optional[float] = None
    origination_fee_pct: Optional[float] = None
    maturity_date: Optional[str] = None    # ISO yyyy-mm-dd
    loan_number: Optional[str] = None
    notes: Optional[str] = None


MEMO_PROMPT = """You have been given a South River Capital CREDIT MEMORANDUM previously generated for a professional-athlete loan (usually a PDF). Read it and extract the deal-level fields needed to prepare the loan CLOSING DOCUMENTS. Return ONLY raw JSON, no markdown, no backticks:

{"borrower_name":null,"borrower_street":null,"borrower_city":null,"borrower_state_abbr":null,"borrower_zip":null,"borrower_state":null,"occupation":null,"team_name":null,"league":null,"loan_amount":0,"interest_rate_pct":0,"origination_fee_pct":0,"maturity_date":null,"loan_number":null,"notes":null}

Rules:
- Numbers must be plain numbers with no "$", commas, or "%": loan_amount 3300000; interest_rate_pct 12.5; origination_fee_pct 3 means 3%. Use 0 when a number is not stated. Use null for missing strings. Do not invent values.
- borrower_name: the borrower/athlete the memo is about, copied EXACTLY as the memo prints it - every surname (a compound surname such as "Rivas Delgado" is two words of one name) and any generational suffix (Jr., Sr., II, III). Never shorten the name to the one the athlete plays under.
- The memo's "Address (Season)" line is one string — split it: borrower_street (street line), borrower_city, borrower_state_abbr (2-letter), borrower_zip. borrower_state is the state spelled out from that abbreviation (e.g. FL -> "Florida").
- occupation: phrase it "Professional <Sport> Player" from how the memo describes the borrower (e.g. "a Professional baseball player" -> "Professional Baseball Player").
- team_name: the team/employer named in the memo. league: the league (the memo's "Lending Area" or the league named alongside the team).
- loan_amount: the proposed facility / loan amount. interest_rate_pct: the stated interest rate. origination_fee_pct: the upfront/origination fee percentage.
- maturity_date: the proposed maturity date, formatted "YYYY-MM-DD". Null if not stated. (The memo usually has no funding/closing date — do not guess one.)
- loan_number: the loan or account number if shown (digits as a string); memos usually omit it.
- notes: one or two short sentences flagging anything missing, ambiguous, or conflicting. Null if nothing notable."""


def _ask_claude(docs: list[UploadedDoc], prompt: str, max_tokens: int) -> dict:
    """Send documents + a prompt to Claude and parse the raw-JSON reply.

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
    log_request_manifest("loan-documents extraction", docs, content)
    check_request_size("loan-documents extraction", docs, content)
    content.append({"type": "text", "text": prompt})

    try:
        message = create_with_retry(
            client, "loan-documents extraction",
            model=EXTRACTION_MODEL,
            max_tokens=max_tokens,
            system=_CLAUDE_CODE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as exc:
        raise RuntimeError(
            "Claude rejected the usage token. Regenerate it with `claude setup-token`, "
            "update CLAUDE_CODE_OAUTH_TOKEN in .env, and restart the backend."
        ) from exc
    except anthropic.APIError as exc:
        logging.getLogger(__name__).error("Loan-documents extraction failed: %s", exc)
        raise RuntimeError(describe_api_error(exc, "extraction")) from exc

    return parse_json_reply(message, "extraction")


def extract_documents(docs: list[UploadedDoc]) -> TeamContractExtraction:
    """Team & Contract fields from an uploaded player contract / deal docs."""
    return TeamContractExtraction(**_ask_claude(docs, PROMPT, max_tokens=1000))


def extract_memo(docs: list[UploadedDoc]) -> MemoDealExtraction:
    """Deal-level fields from a previously generated credit memorandum."""
    return MemoDealExtraction(**_ask_claude(docs, MEMO_PROMPT, max_tokens=1000))
