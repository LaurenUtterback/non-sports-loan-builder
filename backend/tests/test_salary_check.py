"""The Spotrac salary / remaining-value check (Step 2's verification lines).

The verdict shown next to the Guaranteed salary field is computed
deterministically in build_salary_check, never by the model — these lock that
in, along with the check being harmless when Spotrac is unreachable.

Since 2026-08-25 (Lauren) Spotrac is the PRIMARY source for the memo's
Guaranteed salary and Guaranteed remaining prefills (apply_spotrac_precedence),
with the documents as the backup — decided on a deal whose uploaded contract
package was stale and the extraction picked the prior season's salary row.
The documents' figures stay on the check as the visible cross-check. The
Structure tab still attaches this check for verification only.
"""

from types import SimpleNamespace

import pytest

from app import extraction
from app.models import Extraction, SalaryCheck


# --- the verdict matrix ------------------------------------------------------

def test_equal_figures_match():
    check = extraction.build_salary_check(1_650_000, 1_650_000, "https://x", "n")
    assert check["verdict"] == "match"


def test_figures_within_tolerance_match():
    # 0.1% of $2,350,000 is ~$2,350 — a $500 rounding difference is the same figure.
    check = extraction.build_salary_check(2_350_000, 2_349_500, None, "")
    assert check["verdict"] == "match"


def test_figures_outside_tolerance_mismatch():
    # The real failure this exists to catch: documents show the guaranteed
    # portion, Spotrac (or the docs) show the full season salary.
    check = extraction.build_salary_check(1_650_000, 2_350_000, None, "")
    assert check["verdict"] == "mismatch"


def test_spotrac_only_when_documents_gave_nothing():
    check = extraction.build_salary_check(0, 1_500_000, None, "")
    assert check["verdict"] == "spotrac_only"


def test_docs_only_when_spotrac_gave_nothing():
    check = extraction.build_salary_check(1_500_000, 0, None, "")
    assert check["verdict"] == "docs_only"


def test_unavailable_when_neither_gave_a_figure():
    check = extraction.build_salary_check(0, 0, None, "")
    assert check["verdict"] == "unavailable"


def test_tiny_salaries_use_the_dollar_floor():
    # Below the floor the percentage tolerance would be fractions of a dollar.
    assert extraction.build_salary_check(500, 501, None, "")["verdict"] == "match"
    assert extraction.build_salary_check(500, 600, None, "")["verdict"] == "mismatch"


def test_negative_or_none_inputs_are_clamped():
    check = extraction.build_salary_check(-5, None, None, "")
    assert check["verdict"] == "unavailable"
    assert check["spotrac_salary"] == 0


def test_both_sources_ride_on_the_payload():
    check = extraction.build_salary_check(
        1_650_000, 2_350_000, None, "", doc_remaining=5_000_000,
        spotrac_remaining=9_000_000)
    assert check["doc_salary"] == 1_650_000
    assert check["doc_remaining"] == 5_000_000
    assert check["spotrac_remaining"] == 9_000_000
    # Sources describe what FILLED the extraction fields; build_salary_check
    # never decides that (apply_spotrac_precedence does, memo tab only).
    assert "salary_source" not in check


# --- Spotrac as the primary source (Lauren, 2026-08-25) ----------------------

def test_spotrac_figures_fill_the_extraction_fields():
    # The motivating shape: a stale contract package whose salary schedule
    # led the extraction to the PRIOR season's row, while Spotrac carries the
    # current season of the extension. Figures are synthetic.
    data = {
        "salary": 2_000_000, "contract_remaining": 2_000_000,
        "salary_check": extraction.build_salary_check(
            2_000_000, 18_500_000, None, "", doc_remaining=2_000_000,
            spotrac_remaining=80_000_000),
    }
    extraction.apply_spotrac_precedence(data)
    assert data["salary"] == 18_500_000
    assert data["contract_remaining"] == 80_000_000
    assert data["salary_check"]["salary_source"] == "spotrac"
    assert data["salary_check"]["remaining_source"] == "spotrac"
    # the documents' figures survive as the visible cross-check
    assert data["salary_check"]["doc_salary"] == 2_000_000
    assert data["salary_check"]["doc_remaining"] == 2_000_000


def test_documents_are_the_backup_when_spotrac_gave_nothing():
    data = {
        "salary": 1_650_000, "contract_remaining": 5_000_000,
        "salary_check": extraction.build_salary_check(
            1_650_000, 0, None, "no page", doc_remaining=5_000_000),
    }
    extraction.apply_spotrac_precedence(data)
    assert data["salary"] == 1_650_000
    assert data["contract_remaining"] == 5_000_000
    # a payload validated through the model reports the docs as the source
    ed = Extraction(**data)
    assert ed.salary_check.salary_source == "docs"
    assert ed.salary_check.remaining_source == "docs"


def test_precedence_is_per_field():
    # Spotrac produced a season figure but no remaining value: the salary
    # flips to Spotrac, the remaining stays on the documents.
    data = {
        "salary": 1_650_000, "contract_remaining": 5_000_000,
        "salary_check": extraction.build_salary_check(
            1_650_000, 1_700_000, None, "", doc_remaining=5_000_000),
    }
    extraction.apply_spotrac_precedence(data)
    assert data["salary"] == 1_700_000
    assert data["contract_remaining"] == 5_000_000
    assert data["salary_check"]["salary_source"] == "spotrac"
    assert data["salary_check"]["remaining_source"] == "docs"


# --- the check itself --------------------------------------------------------

class _FakeClient:
    """Stands in for the Anthropic client; returns a canned reply."""

    def __init__(self, reply: str):
        self._reply = reply
        self.last_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._reply)])


def test_no_spotrac_page_yields_a_manual_check_note_without_calling_claude():
    # research.py could not fetch the page: the check must still return a
    # record (so the UI says the check did not run) and must not touch the API.
    out = extraction._check_salary_against_spotrac(
        client=None,  # would explode if used
        data={"salary": 1_650_000},
        research={"spotrac_text": None, "spotrac_url": None},
    )
    assert out["verdict"] == "docs_only"
    assert "spotrac.com" in out["note"]


def test_spotrac_reply_is_parsed_and_compared():
    client = _FakeClient(
        '```json\n{"spotrac_salary": 2350000, "spotrac_remaining": 9000000, '
        '"season": "2026", "note": "Base salary per Spotrac."}\n```')
    out = extraction._check_salary_against_spotrac(
        client,
        data={"borrower_name": "Test Player", "salary": 1_650_000,
              "contract_remaining": 1_650_000},
        research={"spotrac_text": "Contract details ...",
                  "spotrac_url": "https://www.spotrac.com/nfl/player/x"},
    )
    assert out["verdict"] == "mismatch"
    assert out["spotrac_salary"] == 2_350_000
    assert out["spotrac_remaining"] == 9_000_000
    assert out["doc_salary"] == 1_650_000
    assert out["doc_remaining"] == 1_650_000
    assert out["season"] == "2026"
    assert out["spotrac_url"] == "https://www.spotrac.com/nfl/player/x"
    # the page text must actually be in the prompt that was sent
    sent = client.last_kwargs["messages"][0]["content"]
    assert "Contract details" in sent and "Test Player" in sent


def test_prompt_asks_for_the_cap_hit_not_the_base_salary():
    # Lauren, 2026-08-06: the Spotrac figure is the season's CAP HIT (base +
    # prorated signing bonus + counted bonuses), never the base salary alone.
    assert "CAP HIT" in extraction.SALARY_CHECK_PROMPT
    assert "NEVER return the base salary alone" in extraction.SALARY_CHECK_PROMPT


def test_prompt_template_formats_cleanly():
    # The JSON example inside the prompt uses doubled braces; a stray single
    # brace would make .format() raise at extraction time, not at import time.
    text = extraction.SALARY_CHECK_PROMPT.format(
        who="A", today="2026-08-06", doc_salary="1", doc_remaining="2",
        url="u", text="t")
    assert ('{"spotrac_salary":0,"spotrac_remaining":0,"season":null,'
            '"note":null}') in text


def test_prompt_asks_for_the_remaining_contract_value():
    # Lauren, 2026-08-25: Spotrac also supplies the Guaranteed remaining
    # (LTC basis) — the prompt must read the total remaining value.
    p = extraction.SALARY_CHECK_PROMPT
    assert "spotrac_remaining" in p
    assert "TOTAL REMAINING" in p
    assert "Exclude seasons already completed" in p


# --- the model contract ------------------------------------------------------

def test_extraction_model_carries_the_check():
    ed = Extraction(salary=1.0, salary_check=extraction.build_salary_check(
        1.0, 1.0, "https://www.spotrac.com/x", "note"))
    assert isinstance(ed.salary_check, SalaryCheck)
    assert ed.salary_check.verdict == "match"
    # and old payloads without the field still validate
    assert Extraction(salary=1.0).salary_check is None
