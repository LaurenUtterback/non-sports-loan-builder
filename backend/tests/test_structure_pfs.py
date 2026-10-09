"""The Structure tab reads the PFS the way the credit memo does.

Lauren, 2026-08-14: the structuring extraction captures the PFS in the memo
extraction's own shape (pfs_date, annual expenditures, Schedule D/F/G rows) and
the server recomputes annual debt service through the memo's machinery —
build_cash_flow decides which rows count and calc_debt_rollforward ages a stale
statement forward (rule 15/17). The model's summed other_debt_annual is only a
fallback for documents whose PFS produced no usable lines.
"""

from app import structure_extraction as se


def _pfs(**over):
    base = {
        "pfs_date": "2025-10-16",
        "mortgage_payments": 60_000,
        "interest_principal_loans": 12_000,
        "other_expenses": [{"label": "Country club dues", "amount": 6_000}],
        "debt_schedule": [{
            "lender": "MidState Bank", "category": "notes_payable_others",
            "balance": 5_000, "payment": 1_000, "payment_period": "monthly",
        }],
    }
    base.update(over)
    return base


# --- the model contract -------------------------------------------------------

def test_pfs_block_tolerates_nulls_and_formatted_money():
    ex = se.StructureExtraction(pfs={
        "pfs_date": None,
        "mortgage_payments": "$60,000",
        "hoa_payments": None,
        "other_expenses": [{"label": None, "amount": "$5,000"},
                           {"label": None, "amount": None}],
        "debt_schedule": [{"lender": "Northgate Bank", "balance": "$350,000",
                           "payment": None, "maturity": None}],
    })
    assert ex.pfs.mortgage_payments == 60_000
    assert ex.pfs.hoa_payments == 0.0
    # a row with neither label nor amount is dropped, not a 500
    assert [x.amount for x in ex.pfs.other_expenses] == [5_000]
    assert ex.pfs.debt_schedule[0].balance == 350_000
    assert ex.pfs.debt_schedule[0].payment == 0.0
    assert ex.pfs.debt_schedule[0].maturity == ""


def test_old_payloads_without_pfs_still_validate():
    ex = se.StructureExtraction(pfs=None)
    assert ex.pfs is None and ex.debt_service_note == ""


# --- the computation ------------------------------------------------------------

def test_debt_service_is_computed_through_the_memo_machinery():
    ex = se.StructureExtraction(pfs=_pfs(pfs_date=None, debt_schedule=[]),
                                other_debt_annual=999.0)
    se._debt_service_from_pfs(ex)
    # 60,000 mortgage + 12,000 interest & principal + 6,000 other line —
    # the same rows build_cash_flow counts, replacing the model's own sum.
    assert ex.other_debt_annual == 78_000
    assert "credit memo" in ex.debt_service_note


def test_stale_pfs_drops_a_debt_repaid_by_funding():
    # Statement 2025-10-16, funding 2026-08-01: ~10 monthly payments have
    # elapsed, so the $5,000 MidState note (at $1,000/mo) rolls to zero and its
    # $12,000/yr of payments must stop being counted — rule 15's whole point.
    ex = se.StructureExtraction(pfs=_pfs(), funding_date="2026-08-01")
    se._debt_service_from_pfs(ex)
    assert ex.other_debt_annual == 78_000 - 12_000
    assert "MidState Bank" in ex.debt_service_note
    assert "excluded" in ex.debt_service_note


def test_fresh_pfs_keeps_every_payment():
    # Same statement read as of its own month: nothing rolls, nothing drops.
    ex = se.StructureExtraction(pfs=_pfs(), funding_date="2025-10-20")
    se._debt_service_from_pfs(ex)
    assert ex.other_debt_annual == 78_000


def test_no_pfs_keeps_the_models_fallback_sum():
    ex = se.StructureExtraction(pfs=None, other_debt_annual=45_000.0)
    se._debt_service_from_pfs(ex)
    assert ex.other_debt_annual == 45_000.0
    assert ex.debt_service_note == ""


def test_empty_pfs_keeps_the_models_fallback_sum():
    ex = se.StructureExtraction(pfs={}, other_debt_annual=45_000.0)
    se._debt_service_from_pfs(ex)
    assert ex.other_debt_annual == 45_000.0


def test_failures_never_break_the_extract(monkeypatch):
    from app import structure

    def boom(*a, **k):
        raise RuntimeError("computation exploded")
    monkeypatch.setattr(structure, "debt_service_from_memo", boom)

    ex = se.StructureExtraction(pfs=_pfs(), other_debt_annual=45_000.0)
    se._debt_service_from_pfs(ex)              # must not raise
    assert ex.other_debt_annual == 45_000.0    # the fallback stands


def test_extract_documents_runs_the_pfs_pass(monkeypatch):
    monkeypatch.setattr(se, "_ask_claude",
                        lambda docs, prompt, max_tokens: {"borrower_name": "T"})
    monkeypatch.setattr(se, "_verify_with_spotrac", lambda ex: None)
    seen = {}
    monkeypatch.setattr(se, "_debt_service_from_pfs",
                        lambda ex: seen.setdefault("ex", ex))
    out = se.extract_documents([])
    assert seen["ex"] is out


# --- the prompt ------------------------------------------------------------------

def test_prompt_captures_the_pfs_in_the_memos_shape():
    assert '"pfs"' in se.PROMPT
    assert "ROW ALIGNMENT" in se.PROMPT          # the scrambled-columns guard
    assert "notes_payable_contract" in se.PROMPT  # the category enum
    assert "FALLBACK" in se.PROMPT               # other_debt_annual demoted
