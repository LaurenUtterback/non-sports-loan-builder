# Backend

FastAPI service: document extraction, underwriting calculations, memo rendering.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

Environment variables (see `../.env.example`):

- `CLAUDE_CODE_OAUTH_TOKEN` — required for `/api/extract`. A Claude subscription
  usage token (generate with `claude setup-token`), **not** an API key.
  Extraction draws on your Claude subscription usage. `ANTHROPIC_AUTH_TOKEN`
  works as an alias.
- `EXTRACTION_MODEL` — optional; defaults to `claude-sonnet-4-6`.
- `CORS_ORIGINS` — comma-separated allowed origins; defaults to the Vite dev server.

## PDF export (WeasyPrint)

`/api/memo/pdf` uses WeasyPrint to render a true PDF server-side. WeasyPrint
needs native libraries:

- **macOS:** `brew install pango`
- **Debian/Ubuntu:** `sudo apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`
- **Windows:** follow the WeasyPrint install docs (GTK runtime).

If WeasyPrint or its libraries aren't available, the PDF endpoint returns a clear
HTTP 501 with instructions, and Word + HTML export still work. You can also print
to PDF from the browser using the HTML preview.

## Tests

```bash
pytest
```

`tests/test_calculations.py` locks in the underwriting rules against the Alvarado
reference deal. Always run before pushing changes to `app/calculations.py` or
`app/memo.py`.

## API

Interactive docs at `http://localhost:8000/docs` once running. Endpoints:

| Method | Path             | Purpose                                  |
|--------|------------------|------------------------------------------|
| GET    | /api/health      | liveness + whether the usage token is set |
| POST   | /api/extract     | upload base64 docs → structured data     |
| POST   | /api/memo/html   | render memo as HTML                       |
| POST   | /api/memo/pdf    | render memo as PDF (download)             |
| POST   | /api/memo/word   | render memo as Word .doc (download)       |
