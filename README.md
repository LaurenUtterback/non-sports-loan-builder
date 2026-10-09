# Non-Sports Loan Builder

Generates credit memorandums and closing packages for South River Capital's
non-sports loans — business owners and executives, investment and real-estate
borrowers, and general consumer or commercial borrowers. Upload deal documents,
Claude extracts the data, confirm the terms, and download the memo as HTML, PDF
or Word.

Five tabs: Deal Structuring, Credit Memo, Participation Agreement, Loan
Documents and Closing Binder.

- **Backend:** Python + FastAPI (business logic, document extraction, rendering)
- **Frontend:** Vue 3 + Vite

This is a fork of the athlete-loan `credit-memo-builder` and runs alongside it,
never on top of it: **backend :8001, frontend :5174** (the athlete builder keeps
:8000 and :5173). `CLAUDE.md` documents what changed in the fork and what was
deliberately kept.

## Why a backend

The original version called the Anthropic API directly from the browser, which
would expose a token to anyone who opened the page. The backend keeps it
server-side in an environment variable. It's also where the underwriting math
lives, with a test suite that locks in the rules.

## Quick start

You need Python 3.10+ and Node 18+.

### 1. Backend

```bash
cd backend
python -m venv .venv && .venv\Scripts\python.exe -m pip install -e ".[dev]"
copy ..\.env.example ..\.env      # then edit ..\.env and add your Claude usage token
.venv\Scripts\python.exe run_local.py
```

The app loads the project-root `.env` itself (python-dotenv), so no `--env-file`
flag is needed. Extraction authenticates with a Claude **subscription usage**
token (OAuth), never a pay-per-token API key — generate one with
`claude setup-token` and put it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5174**. Vite proxies `/api` to the backend on :8001.

Or just double-click `Start Builder.bat`, which starts both.

## Tests

```bash
cd backend
.venv\Scripts\python.exe -m pytest
```

`tests/test_calculations.py` locks in the underwriting rules. Run it before and
after touching `calculations.py` or `memo.py`.

## Policy thresholds are placeholders

`LTI_MAX_PCT` (50%), `LTV_MAX_PCT` (65%) and `DSCR_MIN` (1.25x) in
`calculations.py` are ordinary commercial-credit starting points, **not South
River policy**. Every memo coversheet prints a line saying so until the real
numbers are set.
