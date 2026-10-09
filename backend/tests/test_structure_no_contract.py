"""The Structure tab's "No contracted income" mode.

Mirrors loandocs' no_contracted_income: the flag is ENFORCED server-side (a
stale income figure left in a disabled form field must never fund the
projection), the projection runs on other income and dated receipts only, and
the summary PDF's header presents the no-contract state instead of stale
income fields.
"""

from datetime import date

import pytest

from app import structure_summary
from app.calculations import LTI_MAX_PCT
from app.structure import propose_structures, propose_terms
from app.structure_models import IncomeEvent, StructureInputs


def _inputs(**over):
    base = dict(
        borrower_name="Test Borrower",
        no_contracted_income=True,
        # A stale typed income figure that the disabled field still carries —
        # the server must ignore it.
        qualifying_income=1_000_000.0,
        employer="Old Employer LLC",
        pattern="SEASONAL",
        other_income=240_000.0,
        loan_amount=100_000.0,
        interest_rate=15.0,
        funding_date=date(2026, 4, 1),
        target_term_months=12,
        income_events=[IncomeEvent(label="Note receivable matures",
                                   date=date(2027, 3, 1), amount=150_000.0)],
    )
    base.update(over)
    return StructureInputs(**base)


def test_old_payloads_default_to_having_a_contract():
    assert StructureInputs().no_contracted_income is False


def test_income_is_zeroed_server_side_not_trusted_to_the_form():
    result = propose_structures(_inputs())
    # annual gross = other income only; the $1M stale figure is ignored.
    assert result.annual_gross == 240_000.0
    assert result.inputs_echo.qualifying_income == 0.0
    assert result.inputs_echo.income_contracted is False
    assert all(m.base_income == 0 for m in result.cash_flow)


def test_structures_are_still_proposed_from_other_income_and_events():
    result = propose_structures(_inputs())
    assert result.candidates                       # something to choose from
    # The dated receipt is in the projection.
    assert any(m.bonus > 0 for m in result.cash_flow)


def test_the_no_contract_note_is_surfaced():
    notes = " ".join(propose_structures(_inputs()).notes)
    assert "No contracted income" in notes


def test_with_contracted_income_the_income_still_counts():
    result = propose_structures(_inputs(no_contracted_income=False))
    assert result.annual_gross == 1_240_000.0


# --- PENDING (not yet received) proceeds ----------------------------------

def test_pending_proceeds_size_the_loan_but_are_never_income():
    inputs = _inputs(pending_proceeds_value=2_000_000.0,
                     pending_proceeds_date=date(2027, 3, 15))
    terms = propose_terms(inputs, contracted_income=5_000_000.0)
    # The LTI basis is the PENDING value — never the income figure and never
    # the contracted-income argument, which these deals lack.
    assert terms.income_basis == 2_000_000.0
    assert terms.policy_cap == pytest.approx(
        2_000_000.0 * LTI_MAX_PCT / 100, abs=1000)
    assert any("PENDING" in w and "credit approval" in w for w in terms.warnings)
    # ... and the projection still counts only the other income.
    assert propose_structures(inputs).annual_gross == 240_000.0


def test_without_a_pending_value_only_the_cash_flow_ceiling_applies():
    terms = propose_terms(_inputs())
    assert terms.policy_cap == 0.0
    assert terms.binding_constraint != "policy"
    assert any("cash-flow ceiling" in w for w in terms.warnings)


def test_the_expected_proceeds_date_becomes_the_exit_event():
    result = propose_structures(_inputs(pending_proceeds_date=date(2027, 3, 15)))
    assert result.inputs_echo.expected_exit_date == date(2027, 3, 15)
    assert result.inputs_echo.expected_exit_label == "pending proceeds"
    assert any("PENDING proceeds" in n for n in result.notes) is False  # no value entered


def test_an_entered_exit_event_is_not_overwritten():
    result = propose_structures(_inputs(
        pending_proceeds_date=date(2027, 3, 15),
        expected_exit_date=date(2027, 6, 1),
        expected_exit_label="sale of the Scottsdale property"))
    assert result.inputs_echo.expected_exit_date == date(2027, 6, 1)
    assert result.inputs_echo.expected_exit_label == "sale of the Scottsdale property"


def test_the_pending_proceeds_note_is_surfaced_with_the_value():
    notes = " ".join(propose_structures(_inputs(
        pending_proceeds_value=2_000_000.0,
        pending_proceeds_date=date(2027, 3, 15))).notes)
    assert "PENDING proceeds of $2,000,000" in notes
    assert "never projected as income" in notes


def test_summary_header_presents_the_no_contract_state():
    # build_context receives the route's ORIGINAL inputs — the stale typed
    # income and old employer must not render as if a contract backed them.
    inputs = _inputs()
    ctx = structure_summary.build_context(propose_structures(inputs), inputs)
    assert ctx["employer"] == "None — no contracted income"
    assert ctx["income_money"] == "—"
    assert ctx["guaranteed"] is False
    assert ctx["cadence_source"] == "no contracted income"
