"""The fresh-process fallback for Playwright's false launch failure.

Four times in the athlete builder (2026-07-10, 07-22, 08-19, 08-25) a
long-lived backend began failing every Playwright launch with "Executable
doesn't exist at ..." while the exe sat on disk untouched and a fresh process
worked; the cause was MSIX filesystem virtualization hiding the browser
install from non-packaged processes. These lock in the fallback: on any
in-process failure the PDF render retries once in a fresh python process
before the user sees an error.

The athlete builder's Spotrac lookup had the same fallback. This builder has no
Spotrac lookup, so only the render path is covered here.
"""

import pytest

from app import memo, venv_python


def _boom(*args, **kwargs):
    raise RuntimeError("BrowserType.launch: Executable doesn't exist at ...")


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
    assert ".venv" in venv_python.backend_python()
