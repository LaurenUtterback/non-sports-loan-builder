"""Pydantic models for the Participation Agreement Builder.

Two shapes:

* ``PAExtraction`` — what Claude pulls out of the dropped deal documents (loan &
  security agreement, term sheet, participation term sheet, …). Numbers come
  back as plain numbers; dates as "Month D, YYYY" strings.
* ``PATerms`` — the exact, editable strings that get injected into the
  ``participation_agreement.docx`` template. The frontend pre-fills these from
  the extraction (formatting amounts/percentages) and the user confirms them, so
  what they see in the form is what prints. The Lender is fixed in the template
  (South River Capital LLC / James Plack); the Participant defaults to Brookridge
  but stays editable.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


DEFAULT_PARTICIPANT = "Brookridge Opportunistic Credit Fund, LP"


class PAExtraction(BaseModel):
    """Structured data pulled from the uploaded deal documents by Claude.

    Field names match the JSON the extraction prompt asks for, so the prompt and
    this model must stay in sync (see ``pa_extraction.py``).
    """

    # From the borrower's loan documents
    borrower_name: Optional[str] = None
    agreement_date: Optional[str] = None         # participation agreement / closing date
    loan_agreement_date: Optional[str] = None    # date of the Loan & Security Agreement
    loan_amount: float = 0.0                      # total loan principal
    loan_number: Optional[str] = None
    interest_rate_apr: float = 0.0               # the participant's rate / loan APR (%)
    origination_fee_pct: float = 0.0             # origination fee (%)

    # Participation-specific terms (often set by South River separately; 0/None
    # when the uploaded documents don't state them).
    participant_name: Optional[str] = None
    participation_percentage: float = 0.0        # participant's % of the loan
    participant_loan_amount: float = 0.0         # participant's dollar share
    late_fee_share_pct: float = 0.0              # participant's share of late fees (%)
    servicing_fee_pct: float = 0.0
    participant_signatory_name: Optional[str] = None
    participant_signatory_title: Optional[str] = None

    notes: Optional[str] = None                  # anything notable / ambiguous


class PATerms(BaseModel):
    """The exact strings injected into the template (one per docxtpl placeholder).

    Defaults reproduce nothing deal-specific except the fixed/standard parties.
    """

    agreement_date: str = ""
    borrower_name: str = ""
    loan_principal: str = ""                      # recital/cert form, e.g. "$675,000"
    loan_agreement_date: str = ""
    loan_number: str = ""

    participant_name: str = DEFAULT_PARTICIPANT
    participant_signatory_name: str = ""
    participant_signatory_title: str = ""
    participant_address: str = ""
    participant_email: str = ""

    # Exhibit A — Key Terms (formatted display strings). Which of these appear
    # depends on the agreement type; unused ones are simply ignored by the
    # template that doesn't reference them.
    total_loan_amount: str = ""                   # brookridge — e.g. "$675,000.00"
    participation_percentage: str = ""            # both — e.g. "18.52%"
    participant_loan_amount: str = ""             # brookridge — e.g. "$125,000.00"
    purchase_price: str = ""                      # standard — e.g. "$100,000.00"
    origination_fee_pct: str = ""                 # both — e.g. "2.00%"
    origination_fee_amount: str = ""              # brookridge — e.g. "$2,500.00"
    app_admin_fees_pct: str = ""                  # standard — e.g. "0%"
    interest_rate_apr: str = ""                   # both — e.g. "12%"
    late_fee_share_pct: str = ""                  # both — share of late fees
    servicing_fee_pct: str = ""                   # both — e.g. "0%"


# Agreement forms the builder can produce.
AGREEMENT_TYPES = ("brookridge", "standard")


class PARequest(BaseModel):
    terms: PATerms
    agreement_type: str = "brookridge"            # "brookridge" or "standard"


class PASendRequest(PARequest):
    """Step 4: send the generated agreement out for signature (Demand Signatures)."""

    lender_signer_name: str = "James Plack"
    lender_signer_email: str = ""
    draft: bool = False           # create the envelope without emailing anyone (testing)


# --- Participant Breakdown (.xlsx) -----------------------------------------

class BreakdownParticipant(BaseModel):
    """One participant row from the breakdown. Percentage and dollar fields are
    display-ready strings rendered at the sheet's own precision (e.g. "18.52%",
    "$250,000.00"), so what prints matches the spreadsheet exactly."""
    name: str
    amount: str = ""                   # Participant $ (purchase price / loan share)
    participation_pct: str = ""        # Participant $ / Loan amount
    points_pct: str = ""               # origination (points) %
    points_amount: str = ""            # origination (points) $
    interest_rate: str = ""
    late_fee_share_pct: str = ""       # participation % * 50%
    email: str = ""


class BreakdownDeal(BaseModel):
    borrower_name: str = ""
    loan_number: str = ""
    loan_amount: float = 0.0


class BreakdownResult(BaseModel):
    deal: BreakdownDeal
    participants: list[BreakdownParticipant] = []
