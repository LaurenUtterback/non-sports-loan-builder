"""Locating the venv's own interpreter.

This lived in research.py in the athlete builder, which this builder does not
have (there is no Spotrac to read), but the Playwright subprocess fallback in
memo.py still needs it — so it gets a module of its own rather than riding
along in whatever file happened to keep it.
"""

from __future__ import annotations

import sys
from pathlib import Path


def backend_python() -> str:
    """The venv's own python.exe — never sys.executable.

    The live server is a re-exec'd child whose sys.executable points at the
    BASE Python312 install (see run_local.py), which has no playwright; the
    venv interpreter next to the app does.
    """
    venv = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable
