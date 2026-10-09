# CLAUDE.md — project guide for Claude Code

This file is read automatically by Claude Code. It explains how the project is
laid out, where the important logic lives, and the rules that must not regress.

## What this is

A web app that builds credit memorandums for professional-athlete loans at South
River Capital. A user uploads deal documents (contract, PFS, credit report),
Claude extracts the structured data, the user confirms deal terms, and the app
generates a formatted credit memo that can be downloaded as HTML, PDF, or Word.

It was ported from a single React component into a Python (FastAPI) backend and
a Vue 3 frontend. The port moved the Anthropic API call server-side so the API
key is never exposed in the browser.

## Layout

```
backend/                 FastAPI + the authoritative business logic
  app/
    calculations.py      ALL underwriting math and rules (the crown jewels)
    models.py            Pydantic models — the API contract
    extraction.py        Anthropic document extraction (holds the prompt)
    doc_blocks.py        uploads -> Anthropic content blocks (all 3 uploaders)
    research.py          Wikipedia + Spotrac research on the athlete (Section V)
    memo.py              Renders the memo (Jinja2 HTML, PDF, Word)
    main.py              FastAPI routes
    templates/memo.html.j2   the memo design (HTML/CSS)
    logo.txt             base64 logo data URI
  tests/
    test_calculations.py LOCKS IN the rules using the Alvarado reference deal
frontend/                Vue 3 + Vite UI
  src/
    App.vue              the whole flow (upload → terms → generate → export)
    lib/api.js           the only place that talks to the backend
```

## The rules that must not regress

These are encoded in `tests/test_calculations.py`. Run `pytest` before and after
any change to `calculations.py` or `memo.py`. If a test breaks, either the change
is wrong or a rule genuinely changed — in which case update the test deliberately,
never silently.

1. Taxes are ALWAYS 45% of gross income (salary + other income). Never from docs.
2. Ordinary living expenses are ALWAYS 10% of gross income.
3. In the Guarantor Analysis cash flow, the Proposed Facility line is the loan
   PRINCIPAL only.
4. On the PFS, the Proposed Facility is loan + interest (full amount due).
   Interest comes from the amortization schedule; if rate/dates aren't set, it
   falls back to a facility total stated in the documents.
5. Net Worth = Total Assets − Total Liabilities. Always calculated, never copied.
6. Total Liabilities includes the Proposed Facility (loan + interest).
7. Alimony / child support is a cash-flow item ONLY. Never a PFS liability.
8. Auto loan balances are never a separate PFS liability (they live inside
   "Notes Payable to: others"). Monthly auto PAYMENTS still appear in the cash flow.
9. Salary used everywhere is the GUARANTEED portion of compensation only —
   the guaranteed base salary PLUS any bonus that is guaranteed and paid every
   year of the contract (annual signing-bonus installments, guaranteed yearly
   roster bonuses). Non-guaranteed incentives, one-time bonuses, and
   endorsements stay excluded. When bonus/signing-bonus installments differ by
   season, the amount added is the one scheduled for that SPECIFIC season —
   never an average, never another season's installment. Lauren's reference
   example (baked into the extraction prompt as a worked example): remaining
   contract value $39,500,000, base salary $1,000,000, guaranteed bonus
   scheduled for the season $9,000,000 → salary = $10,000,000 (base + bonus;
   never the base alone, never the remaining contract value, and the bonus is
   never double-counted in other_income). A second worked example in the
   prompt (from a real dated-installment schedule Lauren supplied 2026-07-06:
   $60M signing bonus paid July 2022–2029 in installments ranging $5.5M–$9.5M)
   shows how to pick the installment whose payment date falls in the season
   being underwritten. The total remaining contract value
   is captured separately (`contract_remaining`) and rendered in Section VII
   (sentence + "Total Contract Remaining" table row). (Changed 2026-07-06:
   annual guaranteed bonuses used to be excluded; now they are added into
   salary.)
10. LTC (Loan-to-Contract) = loan amount / guaranteed earnings, where
    guaranteed earnings is the TOTAL REMAINING contract value when extracted
    (`contract_remaining`), else the guaranteed season salary. Section I's
    "advance against $X in guaranteed salary" figure uses the same basis
    (Lauren, 2026-07-06). The cash flow still runs on the season salary.
    CONFIRMED TERMS WIN (2026-08-18). Both the guaranteed salary and the
    remaining contract value are deal-terms fields, resolved the same way as
    every other term in `render_html` (`terms.x or ed.x`). Two defects were
    fixed to make that true: `build_cash_flow` preferred the EXTRACTED salary,
    so a corrected salary (or the "Use Spotrac figure" button) moved Section VII
    while Section VIII silently kept the stale figure; and `contract_remaining`
    had no field in Step 2 at all, so the LTC / "Guaranteed Remaining" basis
    could not be changed from the UI. Step 2 now carries "Guaranteed remaining
    (LTC basis)", pre-filled from Spotrac since 2026-08-25 with the documents
    as backup (see "Spotrac is PRIMARY" below).
    When a CONFIRMED remaining contract value differs from the contract asset
    the PFS reports, Section IX restates that asset to the confirmed figure and
    footnotes it ("restated from the $X reported ... to the $Y ... confirmed at
    underwriting"), so Section IX can never report a contract asset that
    contradicts Section VII. Pre-fill means the two agree on an ordinary deal
    and nothing moves; only an explicit override marks the asset.
    `calc.mark_contract_asset`, surfaced through `calc_balance_sheet`
    (`assets_items` / `contract_mark`) so Total Assets and Net Worth run on the
    restated figure.
11. The memo must NOT contain the phrase "general business purposes".
12. SSN/Tax ID is only ever stored/shown as the last 4 digits (XXX-XX-1234).
13. Taxes are NEVER a PFS liability. Even when the PFS reports an estimated tax
    figure (e.g. "Taxes (Est of 35% of ...)"), it is excluded from Total
    Liabilities and from Net Worth.
14. Section VI (Uses of Funds) reproduces EVERY disbursement line provided in the
    documents (fees, payoffs, closing costs, insurance, interest reserve, ...).
    The "To be disbursed to Borrower" and "Net to be Disbursed to Borrower"
    subtotals are always recomputed from the lines, never copied. With no
    breakdown in the documents it falls back to gross loan less the origination
    fee from the deal terms. Captured by `uses_of_funds` (extraction.py) and
    rendered by `calc_uses_of_funds` / `_uses_of_funds_html`.
15. The loan term in months (Section II Action Request) prefers the term stated
    in the documents (a term sheet's "Term: N months"), falling back to the
    funding-to-maturity span. Captured by `loan_term_months` (extraction.py) and
    resolved by `calc.loan_term_months`.
16. The memo phrases the borrower as "a Professional <sport> player", so the
    sport value is normalized (`calc.normalize_sport`) to drop a leading
    "professional". As a source-agnostic backstop, `_dedupe_professional` (memo.py)
    collapses any consecutive duplicate "professional" in the rendered HTML
    (e.g. from a captured narrative). The memo must never render
    "Professional Professional ...".

17. STALE-PFS ROLL-FORWARD ("Method A" — Lauren's standing rule, 2026-08-05,
    applied to every memo). When the PFS is more than a month older than the
    memo date, each financed debt on its detail schedules is rolled forward:
    `adjusted = reported − (monthly payment × months elapsed)`, where months
    elapsed counts whole payments from the month AFTER the statement date
    through the memo month (10/16/25 → 7/28/26 = 9). Clamped at $0 and never
    past maturity. Left as reported: lines with no payment (credit cards and
    other revolving debt), explicitly non-monthly payments, and CONTRACT-BASED
    notes (Schedule G) whose pay period is blank — those are repaid from game
    checks, not monthly. Each row's paydown is applied to the page-1 summary
    liability it rolls up into (`category`), so Total Liabilities and Net Worth
    run on adjusted figures. Rejected alternative: straight-line origination →
    maturity (it moves a 30-year mortgage far less). Known and accepted:
    payments include interest, and mortgage payments on the SureSports form
    include taxes & insurance, so the method understates true balances.
    `calc_debt_rollforward` / `_apply_rollforward` in calculations.py, fed by
    the `pfs_date` + `debt_schedule` extraction fields (Schedules D/F/G). The
    memo footnotes the PFS table and appends a drafted explanation to the
    Credit paragraph (`_credit_text`). Section IX also LISTS every scheduled
    debt as an indented detail line under the summary liability it rolls into
    (`_debt_detail_rows` / `_debt_row_html` in memo.py), showing the carried
    balance with the schedule, the reported balance and the basis in fine
    print — including the debts carried exactly as reported, which move no
    total and so reached the memo nowhere before 2026-08-18. These lines are a
    BREAKDOWN of the summary line above them and never change a total; a debt
    whose category matches no liability line on the statement is listed at the
    foot of the block under an explicit "not carried in the summary totals
    above" caveat (`_orphan_debt_rows`) rather than being silently dropped.
    KNOWN GAP: a hand-added debt the PFS summary genuinely omits is still shown
    as detail only, so it does NOT raise Total Liabilities — the detail lines
    then will not sum to the summary line. Adding an "additive" treatment is
    the open question. Each debt has a `treatment`: "roll"
    (default), "hold" (carry as reported), "zero" (repaid in full — the whole
    balance leaves its summary liability, and unlike a roll-forward this does
    NOT require a stale PFS) or "remove" (Lauren, 2026-09-02 — the ✕ in Step
    2b): the debt comes OFF THE MEMO ENTIRELY — its balance leaves the
    summary liability like a zero-out, but NOTHING prints (no summary line
    once drained, no detail row, no footnote mention, no credit-paragraph
    sentence). The removal rides `removed_by_category`, a channel separate
    from the visible paydown, so the disclosed roll-forward totals never
    count it, and it lands even on a CURRENT statement. The ✕ therefore no
    longer deletes the row: it marks it "remove", the table hides it, and a
    strip under the table names what was removed with a Restore button — a
    misclick must never silently change Total Liabilities. Removal is
    UNDISCLOSED on the memo by design (Lauren: "it will not appear on the
    Credit Memo"); use "zero" when the memo should say "repaid in full".
    Step 2b of the UI shows reported vs adjusted per
    debt, every field editable, previewed by `POST /api/memo/rollforward` so the
    review table and the memo can never disagree. The step appears whenever
    documents have been read — including when NO schedules were found — because
    debts can be added by hand; an added debt must pick the summary liability it
    rolls into (`category`) or its paydown is warned about, never silently
    applied elsewhere. NOTE: the PFS text layer often
    reaches Claude with columns scrambled — the prompt's ROW ALIGNMENT block
    guards against pairing a payment with the wrong lender, and Step 2b exists
    so the underwriter can catch what slips through.

18. DEAL SUMMARY & POLICY COMPLIANCE COVERSHEET (added 2026-08-10, from
    Lauren's paper template). The memo's page 1 is an auto-filled coversheet:
    a deal-summary block (borrower, team/league, loan, term, collateral,
    guaranteed remaining, repayment, LTC) and a 9-row policy checklist, each
    row Pass / Exc. / N/A. `calc_policy_compliance` (calculations.py) computes
    it from the SAME LTC / cash flow figures the memo body reports, so the
    coversheet can never disagree with the sections behind it. Quantitative
    tests: LTC ≤ 30% (raised from 25% — Lauren, 2026-08-25) and positive net
    cash flow (Section VIII's bottom line). Structural requirements every deal
    satisfies through the loan documents (UCC-1, clean UCC search, personal
    guarantee, DDD insurance, work authorization, no-new-debt covenant) show
    as conditions of closing. RETIRED CRITERIA (Lauren, 2026-08-25): the
    combined contract-note leverage, combined LTV, salary-fully-guaranteed,
    payroll direct-deposit sweep, real-estate lien, minimum credit score, and
    no-derogatories/late-payments rows were REMOVED from the coversheet, the
    exceptions block, and the Deal Summary (whose Repayment cell now reads
    just "Balloon at maturity") — do not re-add them without her direction
    (locked by `test_compliance_omits_the_retired_criteria`). Every "Exc." row
    is echoed in the Exceptions & Mitigants block with the standard mitigants
    and "requires credit approval prior to funding"; a credit-approval
    signature line closes the page. Rendered by `_compliance_rows_html` /
    `_exceptions_html` (memo.py) into the coversheet page of memo.html.j2
    (screen footers are now "of 7"). Locked by the `test_compliance_*` tests.

19. DDD INSURANCE FOLLOWS THE DISBURSEMENT (Lauren, 2026-08-26). The memo
    claims a DDD (death, disability & disgrace) policy ONLY when the
    documents' own disbursement breakdown carries the premium line (e.g.
    "DDD insurance ($17,000)" — either side of the to-Borrower subtotal).
    When the disbursement does not reflect DDD insurance — or the documents
    carry no breakdown at all — the MEMO BODY's mentions drop out (both
    Tertiary repayment-source cards in Sections I and XI, the Section II
    Lloyd's-of-London bullet, Section III's repayment cell, the injury FAQ's
    assigned-policy bullet) and the mitigants sentence loses its insurance
    clause (`STANDARD_MITIGANTS_NO_DDD`). The COVERSHEET keeps addressing
    insurance either way (Lauren's same-day refinement): the "DDD insurance
    assigned to Lender" checklist row always prints — Requirement
    "If applicable", Actual "None in place" (reworded from "Required" /
    "No Insurance", Lauren 2026-08-27: "Required" next to "None in place"
    read like a failed requirement), status N/A (a statement of fact, NOT an
    exception) when there is no premium line; Requirement stays "Required"
    when the policy is there — and the Collateral cell closes with
    "· No Insurance" instead of "· DDD policy" (deliberately NOT harmonized
    with the row's rewording). Decided by `calc.has_ddd_insurance` (regex
    over the Uses-of-Funds labels: DDD / D.D.D. / death / disab / disgrace),
    threaded through `calc_policy_compliance(has_ddd=...)` and the template's
    `has_ddd` flag (rule 16 in calculations.py's own numbering). Locked by
    the `test_ddd_*` / `test_memo_*_ddd_*` / `test_memo_reads_no_insurance_*`
    / `test_compliance_ddd_row_reads_none_in_place_without_the_policy` tests.

20. COLLEGE-ATHLETE MODE (Lauren, 2026-09-14). `DealTerms.is_college` — a
    checkbox at the top of the memo tab's Step 2 ("College athlete (revenue
    share / NIL)"), default off, never set by extraction — swaps the memo's
    pro-league language for college language. Section IV's FAQ: the suspension
    item names the program / conference / NCAA; the contract-risk item DROPS
    the "no professional team has ever defaulted" and league-backstop bullets
    (neither is true of a college payor — NIL collectives have missed
    payments, and no governing body stands behind a school's revenue-share
    obligations) in favor of the House-settlement revenue-share framing
    (naming {{ team }} as the payor), the no-league-backstop statement, and
    NIL counterparty risk; a transfer / roster / eligibility item is ADDED
    (payment conditionality is the real college contract risk); the injury
    item stops citing injured reserve and defers to the agreement's own terms
    (its has_ddd bullet still follows rule 19). Elsewhere: "Professional
    <sport> player" reads "Collegiate" (Section I and the Section V fallback
    narrative in memo.py), Section XI's primary source reads "Compensation
    under the Borrower's revenue-sharing / NIL agreements.", the page-2
    header label reads "School / Program" instead of "Team / Employer"
    (athlete employment status is litigated — never call the school an
    employer), and Section III's collateral cell drops the word "League".
    Everything else — coversheet, guarantee assertions, salary basis, LTC,
    cash flow — is UNCHANGED in college mode. Locked by the
    `test_college_*` tests and `test_memo_defaults_to_the_professional_faq`.

21. THE BORROWER'S FULL LEGAL NAME (Lauren, 2026-10-01). Every document this
    app prints names the borrower the way their government ID spells it —
    EVERY surname and any generational suffix. A compound surname (paternal
    then maternal: a passport reading Surname "RIVAS DELGADO", Given names
    "MATEO" is "Mateo Rivas Delgado") and a Jr./Sr./III stay in the name even
    when the contract, the PFS, the credit report, or the name the athlete
    plays under drops them; the ID governs and the shorter forms are not a
    conflict worth flagging. With no ID uploaded, the fullest name an executed
    legal document shows wins. The rule is ONE string —
    `extraction.LEGAL_NAME_RULE` — appended to all five uploader prompts
    (credit memo, structure, participation agreement, loan documents, closing
    binder) so the four tabs cannot drift apart on it;
    `tests/test_legal_name.py` asserts every prompt carries it. Two
    consequences elsewhere:
    * UCC-1. `Extraction.borrower_surname` and
      `TeamContractExtraction.player_surname` capture the ID's SURNAME FIELD
      on its own, and `LoanDocTerms.borrower_surname` (its own box on the Loan
      Documents tab, prefilled from either) fills the financing statement's
      LAST NAME cell. UCC-1 sufficiency turns on the debtor's name as the
      driver's license shows it, and nothing inside a name string says where a
      compound surname begins — filed under "Delgado", the statement is
      defective. Blank falls back to the last word of the name, as before; a
      generational suffix always moves to the MIDDLE NAME / SUFFIX cell, where
      it belonged all along (`loandocs._split_name`).
    * Research. Spotrac and Wikipedia index the COMMON name, so `research.py`
      also searches the name minus its trailing surname (never minus the
      FIRST one — strangers really are named that, and the league check would
      wave one through), matches results on either half of the surname with
      the given name breaking ties, and trims the player page on whichever
      form it finds. Without that a compound-surname borrower silently loses
      the Spotrac salary prefill and the Section V narrative.


The Alvarado reference deal: $12,267,600 assets, $10,373,361 total liabilities,
$1,894,239 net worth, facility (incl. interest) $2,703,754, LTC 27.8%.

## Uploaded files -> content blocks (`doc_blocks.py`)

All three uploaders (`extraction.py`, `pa_extraction.py`, `loandocs_extraction.py`)
build their message content with `build_document_blocks()`. Do NOT hand a file's
declared MIME to the API as a document block's `media_type` — that was a real
bug (fixed 2026-08-05): the API accepts only `application/pdf` there, and
Windows reports `application/octet-stream` or an empty MIME for files pulled off
the Z: share, so a genuine PDF could fail the whole extraction with a raw 400
(`...document.source.base64.media_type: Input should be 'application/pdf'`).

The file's MAGIC BYTES decide, never the declared MIME: PDF -> document block;
PNG/JPEG/GIF/WEBP -> image block; .docx/.xlsx -> converted to text here
(python-docx / openpyxl, `data_only=True`, capped at 400 rows/sheet) and sent as
a text block labelled with the filename; .txt/.csv/.md/.json -> inlined text.
Anything else (including legacy .doc/.xls) raises a RuntimeError NAMING THE FILE,
which the routes turn into a 502 whose detail the UI shows — the user must always
learn which document to convert. Locked by `tests/test_doc_blocks.py`.

## The Credit paragraph (Section IV) and the credit report

Since 2026-08-20 the extraction PROMPT requires `credit_notes` to summarize
the uploaded credit report: the (mid) score, any bankruptcies / judgments /
liens / collections / charge-offs / repossessions / foreclosures, and a
PER-TRADELINE payment-history review — every auto loan, mortgage/HELOC and
credit card checked for late or missed payments, each finding named with the
creditor, how late, and when. A clean report must be stated affirmatively
("All tradelines report paid as agreed; no late payments."). When NO credit
report is uploaded, `credit_notes` is null and `_credit_text` (memo.py) falls
back to `calc.CREDIT_NOT_SUMMARIZED` — an honest "review it manually" line,
NEVER the old "Credit report reviewed. No bankruptcies..." default, which
asserted a clean report nobody had checked. Locked by
`test_memo_without_credit_notes_never_asserts_clean_credit`. (The coversheet's
credit-score and derogatories rows, which used to key off this paragraph, were
retired 2026-08-25 — see rule 18; the Credit paragraph itself is unchanged.)

## No-PFS deals: the credit report stands in

When NO Personal Financial Statement is uploaded but a credit report is, the
extraction PROMPT builds `debt_schedule` from the credit report's open
tradelines (auto loans, mortgages/HELOCs, credit cards, student loans,
installment notes; revolving cards get payment 0 — they don't amortize) and
returns the report's pull date as `pfs_date`, the ONE exception to the
"never the credit report date" rule — so Step 2b populates and the
roll-forward ages the balances from the date they were true. The liabilities
array is built from the same balances so each row's category matches a
liability it sits inside. Added 2026-08-20 after a no-PFS deal's Step 2b went
empty: the model had been filling it from the credit report informally, and
the credit_notes rule (below) stopped that. Locked by
`test_prompt_carries_the_no_pfs_credit_report_fallback`.

## When an extraction fails

All three uploaders share `build_client()` / `log_request_manifest()` /
`describe_api_error()` from `extraction.py`:
- **Retries**: the client is built with `max_retries=5`, above the SDK's default
  of 2 — that default was not enough; uploads that failed with a 500 succeeded
  on a manual retry moments later.
- **Manifest**: every extraction INFO-logs what it is about to send (filenames,
  block type, size) BEFORE the call, so a failure is diagnosable afterwards.
  Chasing a 500 on 2026-08-06 there was no such record and the request could not
  be reproduced. `main.py` calls `logging.basicConfig` for this — without it
  uvicorn configures only its own loggers and the app's INFO records vanish.
  Sizes are measured PER BLOCK, not per upload: a Word/Excel file is converted
  to text first (a real case: an 8,568 KB .docx became 111 KB of text), so
  upload size would point the next investigation at the wrong file.
- **Errors**: a 5xx is reported as Anthropic-side, with the request ID and the
  advice to retry or split the upload — never as a bare status code. Locked by
  `tests/test_extraction_diagnostics.py`.
- **Truncated replies are named, not dumped** (2026-08-20 — an 11-document
  upload's reply outgrew the memo extraction's then-3,000-token response cap
  and the UI showed the raw JSONDecodeError, "Unterminated string starting
  at: line 155 ..."): every uploader parses the model's reply through
  `extraction.parse_json_reply`, which reports a stop_reason of "max_tokens"
  as "the reply was cut off ... extract fewer documents at once" instead of a
  parse error. The memo extraction's response budget is 8,000 tokens and the
  structure tab's (whose reply nests the full captured PFS) matches it; both
  are sized well past the largest reply seen. Locked by
  `tests/test_extraction_diagnostics.py`.
- **Oversized uploads are shrunk, not refused** (Lauren, 2026-08-14 — hit on a
  real ~35 MB deal upload): the API hard-caps a request at ~32 MB encoded, and
  `check_request_size` sizes the request base64-on-the-wire BEFORE the call.
  When over the ~30 MB budget, `doc_blocks.shrink_blocks_to_fit` shrinks the
  largest blocks IN PLACE, only as much as needed: pass 1 re-encodes the images
  inside PDFs (downscale to 1600 px / JPEG q60 — kept only if actually smaller,
  because a CCITT fax-compressed scan can GROW as JPEG); pass 2 swaps a
  still-oversized PDF for its text layer, labeled in the block so the model
  knows (scans have no text layer and are never swapped; capped at 150k chars).
  A ~39 MB three-bundle fixture shrinks to ~11 MB in ~1.3 s with every document
  still visual. Every change is INFO-logged so a degraded document is never
  silent; what still cannot fit gets the same named-files refusal, now saying
  "even after automatic compression". All four uploaders share this via
  `check_request_size`. Locked by `tests/test_upload_shrink.py`.

## The extraction prompt

`extraction.py` holds the prompt sent to Claude. Its instructions about
guaranteed-compensation-only salary (base + guaranteed annual bonuses),
capturing every liability/expenditure line verbatim, auto-loan folding, and SSN
redaction are load-bearing. Keep them consistent with the rules above.

It also holds `LEGAL_NAME_RULE` (rule 21), the one copy of the borrower-name
rule that every other uploader imports and appends to its own prompt. Change it
there, never per tab.

## Spotrac is PRIMARY for Guaranteed salary & remaining (Step 2)

SPOTRAC FIRST, CONTRACT AS BACKUP (Lauren, 2026-08-25 — decided on a deal
whose uploaded contract package was stale and the extraction picked the prior
season's salary row): the memo's Guaranteed salary and Guaranteed remaining
(LTC basis) prefills come from the athlete's Spotrac page; the figures the
documents produced are the backup, used when Spotrac gives nothing and always
shown as the cross-check. The extraction prompt still settles the documents'
salary from ALL uploaded documents (compensation paragraphs, guarantee
addenda/riders/exhibits, term sheets; the executed contract governs on
conflict) — that figure now rides on the check as `doc_salary` /
`doc_remaining` instead of filling the fields when Spotrac produced a figure.

- `research.py`'s Spotrac fetch is done ONCE per extraction and feeds two
  consumers: the Section V narrative and
  `extraction._check_salary_against_spotrac`, a third Claude call that reads
  the page for the season's CAP HIT (Lauren, 2026-08-06: base salary +
  prorated signing bonus + other bonuses Spotrac counts for the season —
  NEVER the base salary alone; with no cap hit on the page it composes base +
  that season's bonuses) and, since 2026-08-25, the TOTAL REMAINING contract
  value (current/upcoming season + all future seasons; completed seasons
  excluded). How much Spotrac marks as GUARANTEED is reported in the check's
  note, not netted out of the figure.
- `extraction.apply_spotrac_precedence` makes the swap after the check runs:
  a non-zero Spotrac figure replaces `Extraction.salary` /
  `Extraction.contract_remaining`, and `salary_source` / `remaining_source`
  record which source filled each field (per-field — Spotrac can win one and
  the documents the other). Rule 10's "confirmed terms win" is unchanged: a
  value the underwriter types still beats every prefill.
- The verdict ("match" | "mismatch" | "docs_only" | "spotrac_only" |
  "unavailable") is computed by `extraction.build_salary_check`
  (tolerance 0.1%, min $1), NEVER by the model. Carried on
  `Extraction.salary_check` (models.SalaryCheck). Locked by
  `tests/test_salary_check.py`.
- Best-effort: neither the fetch nor the check may ever break /api/extract —
  any failure leaves the documents' figures in place (the backup).
- App.vue recomputes the verdict live against whatever is in each field
  (`salaryVerify` / `remainingVerify`, mirroring the same tolerance), shows
  ✓/⚠ + Spotrac's note and a link, and offers "Use Spotrac figure" when the
  field diverges from Spotrac and "Use contract figure" when the Spotrac
  prefill differs from the documents — either way the underwriter decides,
  the app only types the number.
- The STRUCTURE tab still treats the documents as primary: it attaches the
  same check for verification lines only (structure_extraction never calls
  apply_spotrac_precedence). Flip it there only if Lauren asks.
- CARRIED PREFILLS MUST NOT DEFEAT THE RULE (Lauren, 2026-09-02 — a memo
  kept the contract's salary because the Structure tab, documents-first by
  design, had seeded the memo's Step 2 salary via sendToMemo before the memo
  extraction ran, and the blanks-only fill then skipped the Spotrac figure).
  sendToMemo records what it carries in App.vue's `carriedTerms`
  ({field: value}); runExtract treats a carried salary still matching that
  record as a blank, so the Spotrac-primary figure supersedes it. A value
  the underwriter EDITED no longer matches the record and still wins
  (rule 10, confirmed terms win). The extraction status line now also says
  what actually happened: prefilled from Spotrac / Spotrac figures kept out
  by values already set (with the Use-Spotrac-figure hint) / Spotrac
  produced no figure so the documents (the backup) filled in — the fallback
  is never silent.

## Section V — Project Sponsorship research

Section V describes the ATHLETE and their playing career ONLY (background,
league career, national/country team play when applicable) — it must never
mention the loan, facility, funds, or use of proceeds, and it must contain NO
financial information at all (no salaries, bonuses, contract values, or career
earnings — that detail lives in Section VII Contract Analysis). Lauren,
2026-07-06. After document
extraction, `extraction.py`
`_compose_sponsorship` makes a second Claude call that writes the narrative
from public sources gathered by `research.py`:
- Wikipedia via httpx (needs the descriptive User-Agent — Wikimedia 403s
  browser-imitating UAs).
- Spotrac via the same Playwright headless Chromium used for PDF export, with a
  real-browser UA (Spotrac's CloudFront 403s plain HTTP clients and the default
  HeadlessChrome UA). The API's web_search server tool is NOT available to
  subscription OAuth tokens — don't try to switch to it.
Research is best-effort: any failure keeps the document-derived
`sponsorship_narrative` (and memo.py's one-line fallback below that). It must
never make /api/extract fail.

Playwright browser install — READ THIS before running `playwright install`:
install the browsers from a PLAIN terminal (or a scheduled task), NEVER from
inside an MSIX-packaged app (the Claude desktop app is one). A packaged
process's writes to %LOCALAPPDATA% are silently virtualized into that app's
sandbox (`AppData\Local\Packages\<pkg>\LocalCache\Local\...`), where only
that app's own descendants can see them — every backend started at logon or by
the keep-alive task then fails Playwright launches with a false "Executable
doesn't exist", killing BOTH the Spotrac verification (the salary/team/league
cross-checks) and PDF export. This bit four times (2026-07-10 → 08-25) before
being root-caused; see `memo._render_pdf_subprocess`'s docstring for the story.

## Running locally

Easiest: double-click **`Start Builder.bat`** in the repo root — it launches the
backend and frontend in two minimized windows and opens http://localhost:5173.
Keep those windows open while using the app; there is no auto-start on reboot.

Manual:
Backend:  see `backend/README.md` (uvicorn on :8000)
Frontend: `cd frontend && npm install && npm run dev` (Vite on :5173, proxies /api)

Note: the app is at **http://localhost:5173** (Vite binds the `localhost`/IPv6
name, not `127.0.0.1` — `127.0.0.1:5173` can refuse the connection).

## Conventions

- Business logic lives in the backend, not the frontend. The frontend only
  collects input, calls the API, and displays results.
- All network calls go through `frontend/src/lib/api.js`.
- Add a test for any new rule or any bug you fix in calculations.
- Never commit `.env` or any API key.

## Participation Agreement Builder

A second tool lives in the same app (tab switch in `App.vue`, `view === 'pa'`).
It generates South River's loan **Participation Agreement** — Lender is always
fixed (South River Capital LLC / James Plack). A dropdown chooses the form:
  * **Brookridge Participation Agreement** — Participant defaults to Brookridge
    Opportunistic Credit Fund, LP; Brookridge's clause set; Key Terms include
    Total Loan Amount, Participant's Loan Amount, Origination Fee $.
  * **Participation Agreement (standard form)** — Participant blank by default;
    adds the Lead-Lender disclaimer, Participant rep (c), an Arbitration section,
    and different default-interest language; Key Terms use Purchase Price,
    Application & Administration Fees, and late fees as "% × Participation %".
Drop the deal documents, Claude extracts the loan terms, the user confirms every
field, and it produces the filled agreement as Word **and** PDF (agreement +
Exhibit A Key Terms + Exhibit B Participation Certificate). The chosen form is
sent as `agreement_type` ("brookridge" | "standard") on the /api/pa requests.

Backend pieces:
- `app/pa_models.py` — `PAExtraction` (what Claude pulls from docs), `PATerms`
  (the exact strings injected into the template, one per placeholder), and the
  `Breakdown*` models.
- `app/pa_extraction.py` — reuses the SAME Claude usage-token (OAuth) auth as
  `extraction.py`; only the prompt differs (loan/participation deal fields).
- `app/pa_breakdown.py` — parses the deal's **Participant Breakdown .xlsx**
  (openpyxl) into deal info + per-participant terms. It RECOMPUTES the sheet's
  formulas itself (Participation % = Participant $ / Loan amount; Points $ =
  Points % × Participant $; Late-fee share = Participation % × 50%) so it works
  even without Excel's cached values. Emails come from the lookup sheet.
- `app/pa_agreement.py` — fills the template with **docxtpl** (pure Python) and
  converts the .docx to PDF with **LibreOffice headless** (`find_soffice()`).
- Routes: `POST /api/pa/extract`, `/api/pa/breakdown`, `/api/pa/docx`, `/api/pa/pdf`,
  `/api/pa/send`, `GET /api/pa/defaults`.
- Frontend: `frontend/src/PaBuilder.vue` (uses the app's existing global CSS).
  Drop the breakdown .xlsx → pick a participant → the Key Terms (participation %,
  points %/$, interest, late-fee share, amount, email) auto-fill (mapped per
  form: brookridge → participant_loan_amount + origination_fee_amount; standard
  → purchase_price). "Recalculate" applies the same formulas to manual entry.
  `App.vue` passes the Credit Memo's `terms` to `PaBuilder` as `:memo-terms`, and a
  **"Pull deal info from Credit Memo"** button copies borrower, loan amount,
  interest rate, origination fee, and the funding date (→ agreement_date) over —
  so a typical flow is: build the memo → pull → drop the breakdown → generate.
- Step 4 "Send out for signature" (bottom of the PA tab): the signer rows
  prefill — the SRC signer from `GET /api/pa/defaults`, the participant
  signatory/email from the same `terms` fields the breakdown already fills.
  With Demand Signatures configured (`esign_ready` from the defaults
  endpoint) the primary button is a real one-click send: `POST /api/pa/send`
  renders the PDF server-side and `app/esign_demand_signatures.py` uploads
  and sends it. Without it, the step falls back to the manual flow:
  final-PDF download, a copy-signer-list button (synchronous execCommand
  first; the async clipboard API only as a timeout-guarded backup, because a
  hung permission prompt would otherwise leave the button dead), and a
  button opening the e-signature site where SRC sends documents manually.
- `app/esign_demand_signatures.py`: Demand Signatures is South River's own
  e-signing platform (https://demandsignatures.com); auth is one org-scoped
  Bearer API key — no OAuth, no consent step. The send is four API calls:
  `POST /documents/upload` (multipart PDF) → `POST .../recipients` (both
  signers, same signing_order so both are emailed at once, matching SRC's
  executed PAs) → `POST .../fields` → `POST .../send`. `find_sign_tabs()`
  locates every "By: ______" line in the rendered PDF with pypdf and assigns
  them alternately lender/participant (the templates always print the Lender
  block above the Participant block — signature page AND Exhibit B
  certificate, 4 lines total, locked by
  `tests/test_esign_demand_signatures.py`). Field positions are percentages
  of the page (0–100) from the top-left corner; `build_fields()` converts
  from PDF points keeping the tuned box placement (x+30 past "By:", top =
  page_height − y − 24). `PASendRequest.draft=True` uploads and places
  everything but emails no one — the document stays a draft on
  demandsignatures.com (safe testing). Config is env-only (public repo —
  never hard-code): `DEMAND_SIGNATURES_API_KEY` (`ds_live_...` /
  `ds_test_...`, scopes documents:read + documents:write), optional
  `DEMAND_SIGNATURES_BASE_URL` (default `https://demandsignatures.com/api`).
  The SRC signer identity and the manual signing site's name/URL stay in
  `.env` too (`SRC_SIGNER_NAME`, `SRC_SIGNER_EMAIL`, `ESIGN_NAME`,
  `ESIGN_URL`, read by `pa_agreement.esign_defaults()`).

Templates — `app/templates/participation_agreement_{brookridge,standard}.docx`:
- Carry the SRC logo (blue compass — decoded from `app/logo.txt`) left-aligned
  in a FIRST-page-only Word header (matching the credit memo's masthead), added
  by the build script; body layout and pages 2+ are untouched.
- Hold ONLY `{{ placeholders }}`, never deal data. Built by
  `tools/build_pa_template.py` (config-driven; one config per form) from
  `tools/_pa_struct_{brookridge,standard}.json` — faithful paragraph-level
  captures (text, auto-number strings, alignment, bold spans) of the source
  docs. Re-run after editing: `.venv\Scripts\python.exe tools\build_pa_template.py`.
- BROOKRIDGE: reproduced verbatim, including its numbering and internal
  cross-references ("Section 6.2", "9.3", …) and their quirks — do NOT "fix" them.
- STANDARD: clause WORDING is verbatim, but the source's numbering was internally
  inconsistent (auto-numbers 1)/a)/i) that didn't match its own "Section 3.2(b)"/
  "6.1"/"9.3" cross-references, plus a duplicated Section 9). Per the user's
  decision it is NORMALIZED to the decimal scheme the clauses cross-reference
  (8 Notices, 9 Arbitration, 10 Miscellaneous) with the one stale reference
  corrected ("9.3" → "10.3"). The numbering is encoded as an explicit `numbered`
  map in the STANDARD config, not taken from the source's auto-numbers.
- `pa_agreement.template_path(agreement_type)` selects the file (default brookridge).
- `tools/_*` is git-ignored: it can contain REAL deal data (borrower, amounts,
  loan #) captured from the sources, and this repo is public.

PDF export (LibreOffice):
- Located at runtime via `SOFFICE_PATH` env, then known install paths, then a
  no-admin copy under `%LOCALAPPDATA%\CreditMemoBuilder\libreoffice\program\
  soffice.exe`. The .docx download always works; only PDF needs LibreOffice.
- On this machine LibreOffice was extracted (no admin) with
  `msiexec /a <LibreOffice.msi> /qn TARGETDIR=%LOCALAPPDATA%\CreditMemoBuilder\
  libreoffice`. Word COM automation is NOT used (SaveAs hangs in headless/
  non-interactive sessions).

## Loan Documents Builder

A third tool (tab in `App.vue`, `view === 'loandocs'`) that generates South
River's athlete-loan CLOSING PACKAGE in the credit memo's visual design:
Business Entity Affidavit, Promissory Note (+ Exhibit A repayment schedule),
Loan and Security Agreement (+ Exhibit A definitions), Guaranty, Memo of
Settlement, UCC-1 Financing Statement (+ Exhibit A), and the Payment Direction
Letter to the team. Each document can be included/excluded per package.

Backend pieces:
- `app/loandocs_models.py` - `LoanDocTerms` (one field per placeholder),
  `SettlementLine`, `ScheduleRow`, `LoanDocsInclude`, `LoanDocsRequest`.
- `app/loandocs.py` - builds the Jinja context and renders
  `templates/loan_documents.html.j2`. PDF/Word go through the SAME pipeline as
  the memo (`memo.render_pdf` / `memo.render_word`, whose footer text is now a
  parameter). The Note's Exhibit A schedule uses the memo's
  `calc.calc_repayment_schedule` unless rows are supplied (pulled from the
  memo extraction). The Memo of Settlement's "To be disbursed to Borrower" is
  ALWAYS recomputed from the deduction lines, never copied.
- Routes: `POST /api/loandocs/html|pdf|word`, `GET /api/loandocs/defaults`.
- The Payment Direction Letter's receiving account (bank name, account no.,
  ABA, contact) comes from `SRC_BANK_*` / `SRC_ACCOUNT_NAME` in `.env` - the
  values must NEVER be hard-coded (public repo). `loandocs.bank_defaults()`
  reads them; the UI prefills from `/api/loandocs/defaults` and any field can
  be overridden per deal.
- Frontend: `frontend/src/LoanDocsBuilder.vue` - "Pull deal info from Credit
  Memo" copies borrower/team/league/loan/rate/fee/dates, derives Occupation
  from the sport, parses the memo's one-line address into the UCC-1's
  street/city/state/zip cells, seeds the settlement deductions from the memo's
  Uses of Funds, and carries the extraction's repayment rows into Exhibit A.
  "Surname only (UCC-1)" is the ID's surname field, carried from the memo
  extraction or read off the deal documents and editable here - it decides the
  financing statement's LAST NAME cell (rule 21).

Template - `app/templates/loan_documents.html.j2`:
- GENERATED by `tools/build_loandocs_template.py` from
  `tools/_loandocs_struct.json`, a Word-COM capture (read-only; SaveAs hangs)
  of the executed example
  `Z:\SRC Shared\Servicing Tools\2. Executed Templates\Closing Documents\Loan
  documents Sports Template.docx`. COM text is authoritative (the source fills
  deal data via Word FIELDS that python-docx can't see); runs/page breaks come
  from python-docx, aligned by difflib. Re-run after editing:
  `.venv\Scripts\python.exe tools\build_loandocs_template.py` (re-capture with
  `tools\capture_loandocs.py` only if the source doc itself changes).
- Holds ONLY `{{ placeholders }}`. The build hard-fails if any deal-data token
  (borrower, amounts, dates, bank numbers) survives; `tests/test_loandocs.py`
  re-checks the committed file and every render.
- Clause wording is verbatim INCLUDING the source's numbering quirks (the
  LSA's MISCELLANEOUS/CONSENTS sections are numbered 7.3-7.19) - do not "fix"
  them. ONE deliberate correction: LSA 7.17 had paste-corrupted text
  ("...common la alifornia derpliance with y such Obligor..."), restored to
  the intended "...common law for disclosure...".
- `tools/` stays git-ignored: the struct json contains the real deal data
  captured from the executed example.

Amortization workbook upload (added 2026-07-08, second pass):
- `app/loandocs_sheet.py` + `POST /api/loandocs/settlement`: drop the deal's
  "Balloon *.xlsx" / "Fully Amortized *.xlsx" on the Loan Documents tab
  (Step 2) and it fills the Memo of Settlement and the Note's Exhibit A.
- The fee block is found by its LABELS, never coordinates (it moves between
  workbooks: G/H on Balloon, K/L on Fully Amortized): "Gross Loan Amount" ->
  fee/payoff lines below it -> "To be disbursed to Borrower (Est)". Some
  workbooks continue with lines carved out of the to-Borrower figure (e.g.
  DDD Insurance) down to "Net to be disbursed to Borrower (Est)" — those are
  returned as `post_lines` / `LoanDocTerms.settlement_post_lines` and the
  rendered Memo of Settlement then closes with a recomputed Net row (added
  2026-07-10). The post section is kept ONLY when the Net terminator row is
  found, so stray cells below a Net-less block are never swept in. Blank rows
  inside the block are skipped. The sheet's own disbursed/net figures are
  returned ONLY as cross-checks (the UI compares and warns); the rendered
  memo always recomputes both subtotals from the lines.
- The Exhibit A schedule comes from the "Sheet1" tab: header row of
  Payment Number | Payment Date | Principal | Interest | Total (Payment),
  rows until the first row without a payment number (the totals row). The
  main sheet's full amortization grid has a similar header but also
  "Beginning Balance" — that's the exclusion test. Rows are reflected
  VERBATIM (dates included), rounded to cents.
- A workbook-loaded schedule takes priority over rows pulled from the credit
  memo extraction; the UI has a "clear" link to fall back to the computed
  interest-monthly + balloon schedule. Locked by `tests/test_loandocs_sheet.py`.

Team & Contract extraction (added 2026-07-08, third pass):
- `app/loandocs_extraction.py` + `POST /api/loandocs/extract`: upload the
  player's contract (or any deal documents) in the Loan Documents tab's
  "Team & contract" group and Claude fills team name, team street address,
  team city/state/zip, league, contract title, and contract date. Same
  subscription usage-token auth as extraction.py / pa_extraction.py.
- Extracted values OVERWRITE the team/contract fields (the user uploaded the
  contract specifically for them); player_name fills Borrower name only when
  blank. The team address is only taken when it appears IN the documents (the
  Payment Direction Letter is mailed there) - the prompt forbids inventing it
  and surfaces a note instead, shown in the status line.
- contract_date returns ISO yyyy-mm-dd for the date picker.

Repayment structure dropdown (added 2026-07-08, fourth pass):
- `LoanDocTerms.amortization_type`: "balloon" (default) | "interest_only" |
  "fully_amortized". Drives BOTH the Note clause (d) opening sentence (the
  template's `{{ payment_structure_sentence }}`; the balloon wording is the
  source template's, verbatim - see `PAYMENT_SENTENCES` in loandocs.py) and
  the computed Exhibit A schedule (`_computed_rows`): balloon = $0 rows then
  principal + actual/365 interest via calc_amort (same engine as the memo's
  facility total); interest_only = the memo's Section X fallback (interest
  monthly, principal balloon); fully_amortized = level monthly payments with
  the last row retiring the remaining balance exactly.
- A schedule from the uploaded workbook / memo extraction still outranks the
  computed one. Uploading a workbook also sets the dropdown from the fee-block
  tab name (Balloon / Fully Amortized).

Insurance Policy dropdown (added 2026-07-08, fifth pass):
- `LoanDocTerms.has_insurance_policy` (default False) + a Yes/No dropdown in
  the tab. Swaps the LSA Exhibit A "Insurance Policy" definition via a Jinja
  conditional in the template ({% if has_insurance_policy %}):
  * Yes: "Insurance Policy means one or more policies of insurance, in a form
    and substance acceptable to the Lender, issued by insurers acceptable to
    the Lender." (wording supplied by Lauren 2026-07-08)
  * No (default): the sports template's waived wording VERBATIM, including
    its stray closing quote after "acceptable to Lender”" - do not tidy it.
- Note: the LSA rep 4.1(i) also says the insurance requirement "has been
  waived" and is NOT yet conditional - flagged to Lauren, awaiting direction.

- LSA rep 4.1(i) now follows the SAME has_insurance_policy dropdown (Lauren,
  2026-07-08): Yes = "The Insurance Policy is in full force and effect ...
  will not in any way be affected by, or terminate or lapse by reason of any
  of the transactions contemplated hereby."; No = the waived rep. Both texts
  verbatim as she supplied them (the No wording drops the source's trailing
  double period).

Credit memo upload (added 2026-07-08, sixth pass):
- `POST /api/loandocs/memo` + an uploader in the Loan Documents tab's Step 1
  (beside the live "Pull deal info" button), mirroring the PA tab's memo
  reader: drop a previously generated credit memorandum (PDF is best) and
  Claude returns the deal-level fields (`MemoDealExtraction` in
  loandocs_extraction.py; the shared `_ask_claude` helper serves both
  prompts). The memo's one-line "Address (Season)" is split into
  street/city/state/zip, the state is spelled out for the LSA text, and the
  occupation is phrased "Professional <Sport> Player".
- Like the PA reader, it fills ONLY empty fields - typed values are never
  overwritten. Memos usually lack a funding/closing date and loan number; the
  prompt says not to guess, and anything notable comes back in `notes`
  (shown in the status line).

No-team-contract checkbox (added 2026-07-09):
- `LoanDocTerms.no_team_contract` (default False) + a checkbox at the top of
  the tab's "Team & contract" group ("Athlete does not have a contract with a
  Team / employer"). When checked: the team/contract fields are disabled and
  the contract uploader is hidden; the cover page shows "None" for
  Team / Employer and Contract (a Jinja conditional in the build script's
  COVER block); and the Payment Direction Letter is dropped from the package
  — enforced server-side in `render_html` regardless of the include flag
  (the UI also unchecks and disables its checkbox, restoring it when
  unchecked). "Pull deal info" and the memo reader skip team/league while
  checked. Locked by `test_no_team_contract_drops_letter_and_blanks_cover`.
- The LSA/UCC clauses that reference the Contract (rep 4.1(e), section 5.12,
  the collateral items and the Contract / Borrower's Employer definitions)
  still render with blank underscores in this mode - swapping or omitting
  them needs verbatim no-contract wording from Lauren (same process as the
  insurance dropdown), flagged 2026-07-09.

## Closing Binder

A fourth tool (tab in `App.vue`, `view === 'binder'`) that merges the deal's
EXECUTED documents — uploaded as PDFs, usually scans of the signed set — into
one closing-binder PDF. Its structure follows an executed example binder the
user supplied (kept on the Z: drive — never name the deal here; this repo is
public), re-dressed in the credit memo's visual design:
- page 1: a cover page — borrower name, loan amount, "Closing Binder" (plus
  masthead with closing date / loan number);
- page 2 (+overflow): a TABLE OF CONTENTS with dot leaders and page ranges
  ("5-11"), headed "(Click on a section title to jump directly to the page)"
  — every row is a real /Link annotation that jumps to its section;
- optionally (default on) a title page in front of each document, in the
  loan-docs cover-sheet style; the TOC range includes it, and links/bookmarks
  target it (like the example);
- PDF outline bookmarks ("Cover", "Table of Contents", one per document).

Unlike the other builders it generates no clause text: the uploaded documents
pass through byte-for-byte.

Backend pieces:
- `app/binder_models.py` — `BinderDoc` (title + base64 PDF), `BinderInfo`
  (cover fields), `BinderRequest`.
- `app/binder.py` — `build_binder()`. The front matter renders from
  `templates/closing_binder.html.j2` through the same Playwright engine as
  the memo, in a custom pass (`_render_front`) that ALSO measures each
  `.toc-row`'s geometry via page.evaluate() before page.pdf(); pypdf then
  stitches everything together and turns the measured rows into link
  annotations. Quirks that must not regress:
  * Screen/print geometry sharing: the measured row offsets are valid in the
    PDF because the screen `.page` has the same 7in content width and .7in
    top offset as the printed page. If the template's margins/padding change,
    update `_MARGIN_TOP`/`_MARGIN_LEFT`/`_link_rect`.
  * Front-matter pagination is DETERMINISTIC: 1 cover + ceil(n/18) TOC pages
    (`_TOC_ROWS_PER_PAGE`, rows are single-line nowrap+ellipsis) + n title
    pages. `build_binder` raises if the render disagrees.
  * The front matter renders with `page_numbers=False` (a `memo.render_pdf` /
    `_pdf_footer_template` parameter) — Chromium's "Page X of N" would count
    only the front matter, not the merged documents.
  * pypdf bookmarks must be added AFTER their target page exists in the
    writer, or the destination is dead (resolves to page None). Links are
    written as direct `/Dest` arrays (pypdf `annotations.Link` with
    `target_page_index`), not /A GoTo actions.
  * This template's Jinja env has autoescape ON (unlike loandocs) because
    document titles are free-text user input.
- `app/binder_extraction.py` + `POST /api/binder/extract` — Step 1's
  document reader: drop any deal documents (credit memo, loan documents,
  term sheet; PDFs are best) and Claude returns the cover fields
  (`BinderInfoExtraction`: borrower_name, loan_amount, loan_number,
  closing_date ISO, notes). Same usage-token auth via the shared
  `_ask_claude` in loandocs_extraction.py. The prompt prefers an explicit
  Closing Date and says in `notes` which date it used; it never guesses a
  loan number.
- `POST /api/binder/sort` (same module) — Step 2's auto-sort: upload the
  SIGNED closing package (and separate insurance PDFs) and Claude returns
  every document's page range, which `_organize()` post-processes
  DETERMINISTICALLY into `BinderSortResult.sections`:
  * canonical order (affidavit, note, repayment schedule, LSA, settlement,
    UCC, direction letter), "other" sections after those, insurance ALWAYS
    last;
  * all ranges of the SAME known category merge into ONE section (Claude
    tends to report the LSA body and its Exhibit A separately), and all
    insurance files/ranges merge into one "Insurance Documents" section;
  * the GUARANTY is filed together with the LSA, directly under the
    "Loan and Security Agreement" title page — one section, one TOC row
    (`_MERGE_INTO`; the user's choice 2026-07-13, and the executed example
    has no separate Guaranty tab). The prompt still has Claude label
    "guaranty" ranges; the merge is a backend rule;
  * category "package_cover" (the package's own cover/index and per-document
    title sheets) is DROPPED — the binder adds its own cover and title
    pages; the prompt is emphatic that a title sheet is never part of the
    following document's span (Claude initially lumped them in);
  * pages Claude failed to assign are reported in `notes` (computed in
    Python from the page counts, not trusted from the model) so nothing
    disappears silently.
  Sections can therefore be PAGE RANGES of one file and can span several
  files — `BinderDoc.parts` (list of `BinderPart` with optional
  page_from/page_to, 1-indexed inclusive) carries that to `build_binder`,
  which validates ranges and parses each distinct upload only once. The
  single-file b64 form still works (the manual flow).
- Route: `POST /api/binder/pdf` (400 no docs, 422 unreadable/non-PDF upload
  naming the file, 501 renderer unavailable). PDF only — there is no Word
  export of a merged scan set.
- Tests: `tests/test_binder.py` (page math via the outline bookmarks, TOC
  link targets, filename fallback titles, non-PDF rejection).

Frontend: `frontend/src/ClosingBinderBuilder.vue`. Step 1 has BOTH the live
"Pull deal info from Loan Documents" button and a document uploader ("Read
deal documents") that fills ONLY empty fields — typed values are never
overwritten (PA/loandocs precedent), with the extraction's notes shown in
the status line. The pull reads the LOAN DOCUMENTS tab (not the memo — the
closing documents carry the binder's exact fields: borrower, loan amount,
loan number, closing date). To make that possible the Loan Documents tab's
`terms` now live in an App.vue-owned store (`loanDocsTerms`, passed down as
`terms-store`; the tab seeds missing keys from its `TERM_DEFAULTS` on first
mount) — which also means that tab's typed values now SURVIVE tab switches,
unlike the other tabs' local state.

Step 2's section rows are `{ uid, title, parts: [{file, from, to}] }` — a
manual add creates one whole-file part; "✨ Sort & organize with Claude"
sends every distinct file in the list to `/api/binder/sort` and REPLACES the
rows with the organized sections (source shown as "file.pdf p.3–8", multiple
parts joined with "+"; notes in the status line). Rows stay editable,
reorderable and removable afterwards. `binderPdf` base64-encodes each
distinct file only once however many sections it was split into. Step 2's PDF uploader
accumulates files (non-PDFs are skipped with a notice), each row has an
editable title (defaulting to the cleaned-up filename), ↑/↓ reordering and
remove; generation previews the binder inline and the download button reuses
the same blob.

## Deal Structuring

The FIRST tab and the app's landing view (`view === 'structure'`, App.vue's
default) — the structure is decided from the borrower's cash flow BEFORE the
memo underwrites it, not after. It inverts what the other builders assume:
instead of taking the presented loan structure as given, it DERIVES one from how
the borrower actually gets paid, and scores candidates against a month-by-month
cash flow projection.

The two variables that decide a structure are TIMING (when cash arrives — the
league pay cadence) and CERTAINTY (whether it is contractually locked):

    dated & certain + recurring  -> amortize on the pay cadence
    dated & certain + one event  -> bullet maturing just after the event
    undated / contingent         -> bullet + interest reserve

Non-guaranteed income argues for amortizing FAST while the money flows; a cut
or injury ends it. Guaranteed money can support a balloon.

The tab is driven by DOCUMENT UPLOAD, not manual entry (the user's explicit
requirement, 2026-08-10): drop the contract / term sheet / PFS on Step 1 and
`POST /api/structure/extract` fills everything below. Every field stays editable
afterwards, and "Pull from the Credit Memo tab" remains as a secondary path.

Backend pieces:
- `app/structure_models.py` — `LeagueCadence`, `BonusEvent`, `StructureInputs`,
  `CashFlowMonth`, `StructurePayment`, `StructureCandidate`, `StructureResult`.
  NOTE: `BonusEvent` and `StructurePayment` both have a field named `date`,
  which shadows the type when Pydantic resolves annotations — the module
  imports `date as _date` for that reason. Do not "tidy" it back.
- `app/structure.py` — `LEAGUE_CADENCES` (NFL/NBA/MLB/NHL/MLS defaults plus
  aliases; unknown leagues fall back to level monthly, never raising),
  `pay_dates()`, `project_cash_flow()`, the four candidate builders, `_score()`,
  `propose_structures()`, `to_schedule_rows()`.
- `app/structure_extraction.py` + `POST /api/structure/extract` — reads the deal
  documents for the structuring inputs, reusing the shared `_ask_claude` helper
  (same subscription usage-token auth as the other extractors). Its prompt
  carries the memo's guaranteed-compensation-only salary rule (rule 9) and is
  emphatic about three things the model gets wrong otherwise: a CONDITIONAL
  guarantee is `salary_guaranteed: false` (it flips which structures make
  sense); a guaranteed installment already inside `salary` must NOT be repeated
  in `bonus_events`; and an unstated pay cadence returns null so the league
  default applies, never an invented one. `expected_exit_label` is capped at a
  short noun phrase and `notes` at ~5 items — the model returns an essay in both
  otherwise.
  TOLERANCE (fixed 2026-08-10, was a 500 on upload): the prompt tells Claude to
  use null for anything the documents do not state, so EVERY field can come back
  null — including the str and list ones. `StructureExtraction` therefore extends
  `_Tolerant`, which coerces null to the field's own default ("" / []), parses
  formatted money and percents ("$1,650,000", "15%"), normalizes `pay_frequency`
  onto the three cadences the engine supports (an unmapped value would otherwise
  fail the later /propose call, far from the cause), and accepts yes/no strings
  for `salary_guaranteed`. Do not re-declare these as plain `str`/`list`: one
  unstated field then fails the whole upload. The route also catches non-
  RuntimeError exceptions as a 422 naming the cause rather than a bare 500.
- SPOTRAC CROSS-CHECK (Lauren, 2026-08-14): after the document extraction,
  `structure_extraction._verify_with_spotrac` fetches the athlete's Spotrac
  page (research.spotrac_lookup — the same Playwright fetch the memo uses) and
  a second Claude call (`_ask_spotrac`, SPOTRAC_CHECK_PROMPT) reads it for the
  season CAP HIT (same rule as the memo's check: base + prorated/counted
  bonuses, never base alone), the CURRENT team, and the league. The salary
  verdict comes from the memo's `extraction.build_salary_check` — computed
  server-side, never by the model — and lands on
  `StructureExtraction.salary_check` (models.SalaryCheck), with
  `spotrac_team` / `spotrac_league` alongside. Best-effort by contract: any
  failure leaves a "verify manually" note and never breaks /structure/extract.
  StructureBuilder.vue shows live-recomputed verification lines under the
  salary / team / league fields (mirroring App.vue's salaryVerify; team/league
  match is normalized containment, so "the Denver Broncos Football Club" ==
  "Denver Broncos"), offers "Use Spotrac" buttons (taking Spotrac's league
  also reloads the cadence default), and fills a field from Spotrac ONLY when
  the documents produced nothing — labeled in the status line. The documents
  stay authoritative. Locked by `tests/test_structure_spotrac.py`.
- Routes: `GET /api/structure/cadences`, `GET /api/structure/cadence/{league}`,
  `POST /api/structure/extract`, `POST /api/structure/propose`,
  `POST /api/structure/select`.
- Tests: `tests/test_structure.py`.

Rules encoded (S1-S6, in structure.py's docstring):
S1. Taxes follow the CHECKS — 45% of each month's gross (rule 1 distributed by
    pay date rather than annually). Bonus cash is taxed too.
S2. Living expenses are 10% of annual salary + other income spread EVENLY over
    12 months — rule 2's annual total, but living costs do not stop in the
    offseason, so they are not tied to pay timing.
S3. Salary is the guaranteed season compensation (rule 9) and STOPS at
    `contract_end` — no season is projected past the contract.
S4. Coverage is reported TWO ways and never collapsed: `coverage` (strict, same
    month) and `cushion_coverage` (against banked cash, since athletes bank
    in-season money). Same-month coverage goes NEGATIVE in offseason months —
    that is correct and load-bearing, not a bug to clamp at zero.
S5. A maturity in a dry month is flagged, but the warning DISTINGUISHES a dry
    month at the cumulative-cash peak (just after the last check — deliberate
    good structuring) from one deep in the offseason after drawdown.
S6. Interest accrues actual/365, same basis as `calc_amort`.

`calculations.TAX_RATE` / `LIVING_RATE` were extracted as named constants so
this module distributes the SAME rates the memo uses; the monthly projection
must tie out to the memo's annual cash flow (locked by
`test_projection_ties_out_to_the_memo_annual_cash_flow`). The `agent_pct` knob
therefore defaults to 0.0 — setting it deliberately diverges from the memo.

Frontend: `frontend/src/StructureBuilder.vue`. Six steps: upload -> confirm
deal & borrower -> terms being tested -> pay cadence (editable, seeded from the
league default) -> propose -> review & apply.

A deal is UPLOADED ONCE. App.vue's `files` list is passed down as `:deal-files`
and is the SAME list the Credit Memo tab extracts from, so documents dropped on
the Structure tab are already waiting on the memo tab (the array is mutated in
place — reassigning it would break the parent's reference). The memo still runs
its OWN, fuller extraction over those files: the structuring prompt deliberately
does not pull assets, liabilities, annual expenditures or uses of funds, so the
two are not interchangeable and both tabs say so. `sendToMemo()` additionally
carries the deal terms into the memo's Step 2 grid (borrower, team, league,
salary, loan, rate, fee, funding, maturity), fired automatically after
extraction and re-runnable from a button. It fills BLANKS ONLY — an
underwriter's correction on the memo tab is never overwritten (the same
precedent as the PA / Loan Docs / Binder readers).

Selection is the gate: the tab proposes, the underwriter picks one, and only
then does `POST /api/structure/select` return `repayment_schedule` rows +
`amortization_type` for the Loan Documents tab. That lands in
`LoanDocTerms.repayment_schedule`, which ALREADY outranks the computed Exhibit A
schedule — no document code changes. To make the push possible, the Loan
Documents tab's `pulledSchedule` / `scheduleSource` local refs were MOVED into
the shared App-owned store as `repayment_schedule` / `schedule_source` (the same
mechanism that already let the Closing Binder read that tab).

Validated against a real NFL deal: the engine reproduces the season's 18 game
checks across Sep–Jan, fails both amortizing structures, and recommends the
bullet — matching the memo's own conclusion.

NO-TEAM-CONTRACT MODE (Lauren, 2026-08-14 — mirrors loandocs'
no_team_contract): `StructureInputs.no_team_contract` + a checkbox at the top
of Step 2 ("Athlete does not have a contract with a Team / employer"). When
checked: team/league/salary/contract-end/guarantee fields are disabled, the
Spotrac verification lines are suppressed, extraction and Spotrac fills skip
the contract fields, sendToMemo() never carries team/league/salary, the pay
cadence controls are hidden (income = other income spread evenly + dated bonus
events, which stay editable — they ARE the income), and "Propose terms" is
disabled (its amount is capped against guaranteed earnings). ENFORCED
SERVER-SIDE in `propose_structures` — salary is zeroed and salary_guaranteed
forced False whatever the form carried, a note is added to the result, and
`structure_summary.build_context` presents the no-contract header (the summary
routes pass the ORIGINAL inputs, so a stale typed salary must not render).
Applying a structure also sets the Loan Documents store's `no_team_contract`,
which already blanks the cover and drops the Payment Direction Letter there.
Locked by `tests/test_structure_no_contract.py`.

PROPOSED (UNEXECUTED) CONTRACT within no-contract mode (Lauren, 2026-08-14):
`StructureInputs.proposed_contract_value` / `proposed_contract_date`, entered
in Step 2 when the no-contract box is checked. The proposed contract does two
things and deliberately NOT a third: (1) it SIZES the loan — "Propose terms"
re-enables and the LTC policy cap runs on the proposed value (the executed
contract-remaining figure is explicitly ignored in this mode), with a warning
that it is sized against an unexecuted contract and requires credit approval;
(2) its expected signing date becomes the EXIT EVENT when none was entered
(`_apply_no_contract`, shared by propose_structures and propose_terms — label
"proposed contract signing"), so the bullet matures just after the signing;
(3) it is NEVER projected as income — nothing is contractually owed until it
is signed, and the projection still runs on other income + dated payments
only. The result notes carry the proposed-contract line (rendered on the
summary PDF too). Locked by the `test_proposed_*` tests in
`tests/test_structure_no_contract.py`.

### Proposing the terms (Lauren/Jim, 2026-08-10)

These deals have NO term sheet to upload, so the tool proposes the loan itself
rather than being handed one — `structure.propose_terms()`, `POST
/api/structure/terms`, and "✨ Propose terms from the contract" on Step 3.

The AMOUNT is the lower of two independent ceilings, and the binding one is
named rather than implied:
  * POLICY    — `calculations.LTC_MAX_PCT` (30%) on guaranteed earnings, the
                same basis rule 10 uses: total remaining contract value when
                known, else the guaranteed season salary.
  * CASH FLOW — `max_supportable_loan()`, a binary search for the largest loan
                still repayable from the borrower's own earnings.

Capacity is judged on BANKED cash (`min_cushion_coverage`), NOT the tightest
single month. South River's facilities are balloons retired from a season's
accumulated earnings through the payroll sweep, and an athlete banks in-season
money precisely because the offseason has none. Judging on the thinnest month
caps a seven-figure NFL salary at a tiny fraction of itself — January carries
one game check — which is arithmetically true and commercially meaningless.
Locked by `test_capacity_is_measured_against_banked_cash_not_the_thinnest_month`.
`bullet_reserve` is excluded from the capacity test: it is repaid by an event,
not by earnings, so counting it would make capacity unbounded.

RATE, POINTS and TERM are house defaults taken from recent closed deals
(15%/4%/6mo typical; 13.5%/3%/6mo on the largest), NOT a pricing model.
Nothing derives a rate from risk, and `rate_basis` says so on screen. Do not
add a computed rate without Jim's pricing rules — an authoritative-looking
invented rate gets quoted.

`propose_terms` also answers "can they pay it off?" in words (`repayment_note`),
which was the actual request.

### PFS handled the way the memo handles it

`structure.debt_service_from_memo()` + `POST /api/structure/debt-service`.
Annual non-facility debt service goes through `calculations`, not a second ask
to the model, for two reasons: `build_cash_flow` already decides which rows
count, and `calc_debt_rollforward` (rule 15) already ages a stale statement
forward. A debt whose balance rolls to zero by the funding date has its
payments dropped from debt service too — otherwise an old PFS inflates the
borrower's obligations with debts they have since repaid. The roll-forward note
is surfaced in the tab so the adjustment is visible, never silent.

The UPLOAD path does this too (Lauren, 2026-08-14), not just "Pull from the
Credit Memo tab": the structuring prompt captures the PFS in the MEMO
extraction's own shape — `StructureExtraction.pfs` (`PFSRead`: `pfs_date`, the
Annual Expenditures fields, `debt_schedule` Schedule D/F/G rows with the same
category enum and ROW ALIGNMENT guard as the memo prompt) — and
`structure_extraction._debt_service_from_pfs` hands that block to
`Extraction(**pfs.model_dump())` -> `debt_service_from_memo`, using the
extracted funding date as the roll-forward as-of. The computed figure
overwrites `other_debt_annual`; the model's own summed value is kept ONLY as a
fallback when the PFS produced no usable lines (the prompt says so explicitly).
`debt_service_note` carries the roll-forward narrative to the UI (the same
`pfsNote` line the pull path uses). Best-effort: any failure keeps the fallback
and never breaks /structure/extract. `PFSRead`'s validators clean nulls and
formatted money INSIDE nested rows (a `{"label": null}` line item or a
"$350,000" balance must not 500 the upload — the same lesson as `_Tolerant`,
one level down). Locked by `tests/test_structure_pfs.py`.
