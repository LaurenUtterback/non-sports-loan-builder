# Credit Memo Builder

Generates credit memorandums for professional-athlete loans. Upload deal
documents, Claude extracts the data, confirm the terms, and download the memo as
HTML, PDF, or Word.

- **Backend:** Python + FastAPI (business logic, document extraction, rendering)
- **Frontend:** Vue 3 + Vite

## Why a backend

The original version called the Anthropic API directly from the browser, which
would expose an API key to anyone who opened the page. The backend keeps the key
server-side in an environment variable. It's also where the underwriting math
lives, with a test suite that locks in the rules.

## Quick start

You need Python 3.10+ and Node 18+.

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp ../.env.example ../.env                              # then edit ../.env and add your key
export $(grep -v '^#' ../.env | xargs)                  # load env vars (or use a tool like direnv)
uvicorn app.main:app --reload --port 8000
```

The API is now at http://localhost:8000, with interactive docs at
http://localhost:8000/docs.

> **PDF export** uses WeasyPrint, which needs system libraries (Pango/Cairo).
> See `backend/README.md`. If you skip it, everything else works and the PDF
> endpoint returns a clear message; you can still print to PDF from the browser.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api` to the backend, so no
extra configuration is needed.

### 3. Run the tests

```bash
cd backend && pytest
```

These verify the underwriting rules against the Alvarado reference deal. Run them
before pushing.

## Working with Claude Code

This repo includes a `CLAUDE.md` that Claude Code reads automatically. It maps
the codebase and lists the underwriting rules that must not regress. When asking
Claude Code to make changes, point it at `CLAUDE.md` first; it will know to run
`pytest` after touching the calculations.

## Collaborating with coworkers

1. **Use Git.** Initialize once (`git init`), then everyone clones the repo. See
   the suggested workflow below.
2. **Never commit secrets.** `.env` is git-ignored. Share keys out of band; each
   person keeps their own `.env`.
3. **Branch per change.** Create a branch (`git checkout -b fix-tax-rule`), make
   the change, run `pytest`, open a pull request for review, merge.
4. **Tests are the contract.** If you change a rule, change the matching test in
   the same commit so reviewers see the intended behavior change explicitly.
5. **The OpenAPI docs at `/docs`** are the source of truth for the API shape, so
   frontend and backend developers can work in parallel.

### Suggested Git workflow

```bash
git init
git add .
git commit -m "Initial import of Credit Memo Builder"
# create a repo on your host (GitHub/GitLab/etc.) and:
git remote add origin <your-repo-url>
git push -u origin main
```

Then for each piece of work: branch → commit → push → pull request → review →
merge. Keep `main` always passing `pytest`.

## Project structure

See `CLAUDE.md` for a full map and the list of underwriting rules.
