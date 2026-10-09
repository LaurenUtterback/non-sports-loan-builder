"""The Structure tab's "Athlete does not have a contract" mode.

Lauren, 2026-08-14. Mirrors loandocs' no_team_contract: the flag is ENFORCED
server-side (a stale salary left in a disabled form field must never fund the
projection), the projection runs on other income and dated payments only, and
the summary PDF's header presents the no-contract state instead of stale
contract fields.
"""

from datetime import date

from app import structure_summary
from app.structure import propose_structures, propose_terms
from app.structure_models import BonusEvent, StructureInputs


def _inputs(**over):
    base = dict(
        borrower_name="Test Player",
        no_team_contract=True,
        # A stale typed salary that the disabled field still carries — the
        # server must ignore it.
        salary=1_000_000.0,
        team="Old Team",
        league="NFL",
        other_income=240_000.0,
        loan_amount=100_000.0,
        interest_rate=15.0,
        funding_date=date(2026, 9, 1),
        target_term_months=12,
        bonus_events=[BonusEvent(label="Endorsement milestone",
                                 date=date(2027, 3, 1), amount=150_000.0)],
    )
    base.update(over)
    return StructureInputs(**base)


def test_old_payloads_default_to_having_a_contract():
    assert StructureInputs().no_team_contract is False


def test_salary_is_zeroed_server_side_not_trusted_to_the_form():
    result = propose_structures(_inputs())
    # annual gross = other income only; the $1M stale salary is ignored.
    assert result.annual_gross == 240_000.0
    assert result.inputs_echo.salary == 0.0
    assert result.inputs_echo.salary_guaranteed is False
    assert all(m.salary == 0 for m in result.cash_flow)


def test_structures_are_still_proposed_from_other_income_and_bonuses():
    result = propose_structures(_inputs())
    assert result.candidates                       # something to choose from
    # The dated endorsement payment is in the projection.
    assert any(m.bonus > 0 for m in result.cash_flow)


def test_the_no_contract_note_is_surfaced():
    notes = " ".join(propose_structures(_inputs()).notes)
    assert "No team contract" in notes


def test_with_a_contract_the_salary_still_counts():
    result = propose_structures(_inputs(no_team_contract=False))
    assert result.annual_gross == 1_240_000.0


# --- the PROPOSED (unexecuted) contract ------------------------------------------

def test_proposed_contract_sizes_the_loan_but_is_never_income():
    inputs = _inputs(proposed_contract_value=2_000_000.0,
                     proposed_contract_date=date(2027, 3, 15))
    terms = propose_terms(inputs, contract_remaining=5_000_000.0)
    # The LTC basis is the PROPOSED value — never the salary and never the
    # (executed) contract-remaining argument, which no-contract deals lack.
    assert terms.guaranteed_earnings_basis == 2_000_000.0
    assert terms.policy_cap == 600_000.0                  # 30% LTC
    assert any("PROPOSED" in w and "credit approval" in w for w in terms.warnings)
    # ... and the projection still counts only the other income.
    assert propose_structures(inputs).annual_gross == 240_000.0


def test_without_a_proposed_value_only_the_cash_flow_ceiling_applies():
    terms = propose_terms(_inputs())
    assert terms.policy_cap == 0.0
    assert terms.binding_constraint != "policy"
    assert any("cash-flow ceiling" in w for w in terms.warnings)


def test_expected_signing_becomes_the_exit_event():
    result = propose_structures(_inputs(proposed_contract_date=date(2027, 3, 15)))
    assert result.inputs_echo.expected_exit_date == date(2027, 3, 15)
    assert result.inputs_echo.expected_exit_label == "proposed contract signing"
    assert any("PROPOSED contract" in n for n in result.notes) is False  # no value entered


def test_an_entered_exit_event_is_not_overwritten():
    result = propose_structures(_inputs(
        proposed_contract_date=date(2027, 3, 15),
        expected_exit_date=date(2027, 6, 1),
        expected_exit_label="sale of the Scottsdale property"))
    assert result.inputs_echo.expected_exit_date == date(2027, 6, 1)
    assert result.inputs_echo.expected_exit_label == "sale of the Scottsdale property"


def test_the_proposed_contract_note_is_surfaced_with_the_value():
    notes = " ".join(propose_structures(_inputs(
        proposed_contract_value=2_000_000.0,
        proposed_contract_date=date(2027, 3, 15))).notes)
    assert "PROPOSED contract for $2,000,000" in notes
    assert "never projected as income" in notes


def test_summary_header_presents_the_no_contract_state():
    # build_context receives the route's ORIGINAL inputs — the stale typed
    # salary and old team must not render as if a contract backed them.
    inputs = _inputs()
    ctx = structure_summary.build_context(propose_structures(inputs), inputs)
    assert ctx["team"] == "None — no team contract"
    assert ctx["salary_money"] == "—"
    assert ctx["guaranteed"] is False
    assert ctx["cadence_source"] == "no team contract"
