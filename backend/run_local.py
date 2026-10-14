r"""Local dev launcher for the Non-Sports Loan Builder backend.

Runs a single in-process Uvicorn server on THIS interpreter. Importing the app
object (instead of passing the ``"app.main:app"`` import string to the uvicorn
CLI) prevents Uvicorn from ever using a reload/worker subprocess. That matters
on this machine: launching ``python -m uvicorn app.main:app`` was starting the
actual server under the *base* Python interpreter, which lacks the venv-only
packages (playwright for PDF export, anthropic for extraction). Running this
file with the venv's python keeps the server in the venv.

Usage (from the backend/ directory, with the venv active or via its python):
    .\.venv\Scripts\python.exe run_local.py
"""

import uvicorn

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
