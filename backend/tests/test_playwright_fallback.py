"""The fresh-process fallback for Playwright's false launch failure.

Three times now (2026-07-10, 07-22, 08-19) a long-lived backend began failing
every Playwright launch with "Executable doesn't exist at ..." while the exe
sat on disk untouched and a fresh process worked. These lock in the fallback:
on any in-process failure, the Spotrac lookup and the PDF render each retry
once in a fresh python process before the user sees an error.
"""

import pytest

from app import memo, research


def _boom(*args, **kwargs):
    raise RuntimeError("BrowserType.launch: Executable doesn't exist at ...")


def test_spotrac_lookup_falls_back_to_a_fresh_process(monkeypatch):
    monkeypatch.setattr(research, "wiki_lookup", lambda n, s: (None, None))
    monkeypatch.setattr(research, "spotrac_lookup", _boom)
    monkeypatch.setattr(research, "_spotrac_lookup_subprocess",
                        lambda n, l, s: ("PAGE TEXT", "https://spotrac.com/x"))
    out = research.gather_athlete_research("Test Player", "baseball", "MLB")
    assert out["spotrac_text"] == "PAGE TEXT"
    assert out["spotrac_url"] == "https://spotrac.com/x"


def test_spotrac_double_failure_still_never_raises(monkeypatch):
    # Research is best-effort: even both attempts failing must not break
    # /api/extract — the check line then says to verify manually.
    monkeypatch.setattr(research, "wiki_lookup", lambda n, s: (None, None))
    monkeypatch.setattr(research, "spotrac_lookup", _boom)
    monkeypatch.setattr(research, "_spotrac_lookup_subprocess", _boom)
    out = research.gather_athlete_research("Test Player", "baseball", "MLB")
    assert out["spotrac_text"] is None


def test_render_pdf_falls_back_to_a_fresh_process(monkeypatch):
    monkeypatch.setattr(memo, "_render_pdf_inprocess", _boom)
    monkeypatch.setattr(memo, "_render_pdf_subprocess",
                        lambda h, f, p: b"%PDF-from-subprocess")
    assert memo.render_pdf("<html></html>") == b"%PDF-from-subprocess"


def test_render_pdf_double_failure_keeps_the_familiar_message(monkeypatch):
    monkeypatch.setattr(memo, "_render_pdf_inprocess", _boom)
    monkeypatch.setattr(memo, "_render_pdf_subprocess", _boom)
    with pytest.raises(RuntimeError) as exc:
        memo.render_pdf("<html></html>")
    # The UI keys off this wording (generic catch-all) — keep it stable.
    assert "PDF rendering failed" in str(exc.value)
    assert "playwright install chromium" in str(exc.value)


def test_backend_python_prefers_the_venv():
    # The server can be a re-exec'd child of BASE Python312 (no playwright);
    # subprocess jobs must run on the venv interpreter instead.
    py = research._backend_python()
    assert ".venv" in py
