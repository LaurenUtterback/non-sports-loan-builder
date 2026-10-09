"""The Structure tab's Spotrac cross-check (salary + team + league).

Lauren, 2026-08-14: the structuring extraction must pull the guaranteed season
salary, team and league from the documents AND check them against spotrac.com —
the same verification the credit memo tab runs, extended with team/league.

These lock in the contract: the salary verdict is computed by the memo's
build_salary_check (never the model), the check is harmless when Spotrac is
unreachable, and it is verification-only — Spotrac never overwrites a value the
documents produced.
"""

from app import structure_extraction as se
from app.extraction import build_salary_check
from app.models import SalaryCheck


# --- the model contract -------------------------------------------------------

def test_extraction_model_carries_the_check_and_spotrac_fields():
    ex = se.StructureExtraction(
        salary=1_650_000,
        salary_check=build_salary_check(1_650_000, 1_650_000, "https://x", "n"),
        spotrac_team="Example Team",
        spotrac_league="NFL",
    )
    assert isinstance(ex.salary_check, SalaryCheck)
    assert ex.salary_check.verdict == "match"
    assert ex.spotrac_team == "Example Team"


def test_old_payloads_and_nulls_still_validate():
    # The document prompt never returns these fields; _Tolerant also coerces an
    # explicit null back to the default (the 2026-08-10 500-on-upload lesson).
    ex = se.StructureExtraction(spotrac_team=None, spotrac_league=None,
                                salary_check=None)
    assert ex.salary_check is None
    assert ex.spotrac_team == "" and ex.spotrac_league == ""


# --- the check itself ----------------------------------------------------------

def test_verify_fills_check_team_and_league(monkeypatch):
    monkeypatch.setattr(se, "spotrac_lookup",
                        lambda name, league, sport: ("PAGE TEXT", "https://www.spotrac.com/nfl/x"))
    monkeypatch.setattr(se, "_ask_spotrac", lambda text, url, ex: {
        "spotrac_salary": 2_350_000, "season": "2026",
        "team": "Team B", "league": "NFL", "note": "Cap hit per Spotrac."})

    ex = se.StructureExtraction(borrower_name="Test Player",
                                salary=1_650_000, team="Team A")
    se._verify_with_spotrac(ex)

    assert ex.salary_check.verdict == "mismatch"       # docs vs Spotrac differ
    assert ex.salary_check.spotrac_salary == 2_350_000
    assert ex.salary_check.season == "2026"
    assert ex.spotrac_team == "Team B"
    assert ex.spotrac_league == "NFL"
    # verification-only: the documents' fields are never overwritten
    assert ex.team == "Team A" and ex.salary == 1_650_000


def test_no_page_yields_a_manual_note_without_calling_claude(monkeypatch):
    monkeypatch.setattr(se, "spotrac_lookup", lambda *a: (None, None))
    called = []
    monkeypatch.setattr(se, "_ask_spotrac",
                        lambda *a: called.append(1) or {})

    ex = se.StructureExtraction(borrower_name="Test Player", salary=1_000_000)
    se._verify_with_spotrac(ex)

    assert not called
    assert ex.salary_check.verdict == "docs_only"
    assert "spotrac.com" in ex.salary_check.note
    assert ex.spotrac_team == "" and ex.spotrac_league == ""


def test_no_borrower_name_skips_the_lookup_entirely(monkeypatch):
    def boom(*a):
        raise AssertionError("lookup must not run without a name")
    monkeypatch.setattr(se, "spotrac_lookup", boom)

    ex = se.StructureExtraction(salary=1_000_000)
    se._verify_with_spotrac(ex)
    assert ex.salary_check.verdict == "docs_only"


def test_failures_never_break_the_extract(monkeypatch):
    def boom(*a):
        raise RuntimeError("network down")
    monkeypatch.setattr(se, "spotrac_lookup", boom)

    ex = se.StructureExtraction(borrower_name="Test Player", salary=1_000_000)
    se._verify_with_spotrac(ex)                        # must not raise
    assert ex.salary_check.verdict == "docs_only"
    assert "spotrac.com" in ex.salary_check.note


def test_extract_documents_runs_the_check(monkeypatch):
    monkeypatch.setattr(se, "_ask_claude",
                        lambda docs, prompt, max_tokens: {"borrower_name": "T",
                                                          "salary": 1_000_000})
    seen = {}
    monkeypatch.setattr(se, "_verify_with_spotrac",
                        lambda ex: seen.setdefault("ex", ex))
    out = se.extract_documents([])
    assert seen["ex"] is out


# --- the prompt ------------------------------------------------------------------

def test_prompt_asks_for_the_cap_hit_team_and_league():
    # Same rule as the memo's check (Lauren, 2026-08-06): the Spotrac figure is
    # the season's CAP HIT, never the base salary alone — plus team and league.
    assert "CAP HIT" in se.SPOTRAC_CHECK_PROMPT
    assert "NEVER return the base salary alone" in se.SPOTRAC_CHECK_PROMPT
    assert "CURRENT team" in se.SPOTRAC_CHECK_PROMPT


def test_prompt_template_formats_cleanly():
    # The JSON example uses doubled braces; a stray single brace would make
    # .format() raise at extraction time, not at import time.
    text = se.SPOTRAC_CHECK_PROMPT.format(
        who="A", today="2026-08-14", doc_salary="1", url="u", text="t")
    assert '{"spotrac_salary":0,"season":null,"team":null,"league":null,"note":null}' in text
