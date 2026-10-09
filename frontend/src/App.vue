<script setup>
import { ref, reactive, computed } from 'vue'
import { extractDocuments, memoHtml, downloadPdf, downloadWord, rollforwardPreview } from './lib/api.js'
import PaBuilder from './PaBuilder.vue'
import LoanDocsBuilder from './LoanDocsBuilder.vue'
import ClosingBinderBuilder from './ClosingBinderBuilder.vue'
import StructureBuilder from './StructureBuilder.vue'

// which builder is showing: 'structure' (deal structuring), 'memo' (credit
// memo), 'pa' (participation agreement), 'loandocs' (closing document package)
// or 'binder' (closing binder).
// Structure comes FIRST and is the landing tab: the structure is decided from
// the borrower's cash flow before the memo underwrites it, not after.
const view = ref('structure')

const TAB_TAGS = {
  structure: 'Deal Structuring',
  memo: 'Credit Memorandum Builder',
  pa: 'Participation Agreement Builder',
  loandocs: 'Loan Documents Builder',
  binder: 'Closing Binder Builder',
}

// The Loan Documents tab's deal terms live here (populated by the tab on
// first mount) so they survive tab switches and the Closing Binder's
// "Pull deal info from Loan Documents" can read them.
const loanDocsTerms = reactive({})

// Memo terms another tab CARRIED into Step 2 (today: the Structure tab's
// sendToMemo), recorded as {field: carried value}. Lets runExtract tell a
// convenience prefill from a figure the underwriter actually set: a carried
// value left untouched must not block the memo's own Spotrac-primary salary
// prefill (Lauren, 2026-09-02 — a memo kept the contract figure because the
// Structure tab, which is documents-first by design, had seeded the field
// before the memo extraction ran).
const carriedTerms = reactive({})

// --- state -----------------------------------------------------------------
const files = ref([])
const extracting = ref(false)
const status = reactive({ type: '', msg: '' })
const extraction = ref(null)

const terms = reactive({
  name: '', dob: '', addr: '', phone: '', team: '', league: '', sport: '',
  ssn: '', dl: '', agent: '',
  loan: null, rate: null, fee: null, salary: null, contract_remaining: null,
  fund: '', mat: '', loan_type: 'New Loan',
  // College athlete (revenue share / NIL): swaps the memo's Section IV FAQ and
  // "Professional" phrasing to college language. Underwriter-set only — the
  // extraction never touches it.
  is_college: false,
})

const memoReady = ref(false)
const memoHtmlContent = ref('')
const genError = ref('')

// --- stale-PFS debt roll-forward (rule 15) ---------------------------------
// The preview comes from the backend so the review table and the memo can never
// disagree. Every edit below mutates `extraction`, which is what gets sent when
// the memo is generated — so an override here is what the memo renders.
const rollforward = ref(null)
const rfError = ref('')

// The page-1 summary liability each schedule row rolls up into. A manually
// added debt must pick one, or its paydown has no total to come out of.
const DEBT_CATEGORIES = [
  { value: 'mortgage_debt', label: 'Mortgage Debt (Schedule D)' },
  { value: 'notes_payable_others', label: 'Notes Payable to: others (Schedule F)' },
  { value: 'notes_payable_contract', label: 'Notes Payable: Contract Based (Schedule G)' },
]

async function refreshRollforward() {
  rfError.value = ''
  if (!extraction.value?.debt_schedule?.length) { rollforward.value = null; return }
  try {
    rollforward.value = await rollforwardPreview(buildTermsPayload(), extraction.value)
  } catch (err) {
    rollforward.value = null
    rfError.value = err.message
  }
}

function addDebt() {
  // Extraction misses a schedule row (or the PFS has no schedules at all) —
  // add it by hand so it still rolls forward.
  if (!extraction.value) extraction.value = {}
  if (!extraction.value.debt_schedule) extraction.value.debt_schedule = []
  extraction.value.debt_schedule.push({
    lender: '', category: 'notes_payable_others', balance: 0, payment: 0,
    payment_period: 'monthly', origination: '', maturity: '', rate_pct: 0,
    description: '', treatment: 'roll',
  })
  refreshRollforward()
}

// The ✕ takes the debt OFF THE MEMO (Lauren, 2026-09-02): its balance leaves
// the summary liability it rolls into (Total Liabilities and Net Worth move)
// and no line prints anywhere. The row is kept, marked "remove", so the strip
// under the table can say what happened and offer Restore — a misclick must
// never silently change the totals. Use the "Zero out (repaid)" treatment
// instead when the memo should disclose the debt as repaid in full.
function removeDebt(i) {
  extraction.value.debt_schedule[i].treatment = 'remove'
  refreshRollforward()
}

function restoreDebt(i) {
  extraction.value.debt_schedule[i].treatment = 'roll'
  refreshRollforward()
}

const removedDebts = computed(() =>
  (extraction.value?.debt_schedule || [])
    .map((d, i) => ({ d, i }))
    .filter((x) => x.d.treatment === 'remove'))

function categoryLabel(cat) {
  return DEBT_CATEGORIES.find((c) => c.value === cat)?.label || ''
}

const money = (n) => (n || n === 0)
  ? n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
  : ''

// --- derived ---------------------------------------------------------------
const canGenerate = computed(() => terms.loan && terms.salary)

// --- Spotrac salary / remaining (primary source since 2026-08-25) -----------
// Spotrac is the PRIMARY source for the Guaranteed salary and Guaranteed
// remaining prefills (the backend applies that in apply_spotrac_precedence);
// the documents are the backup and are shown here as the cross-check. The
// verdict is recomputed against whatever is in the field right now, so the
// line stays truthful while the user edits. Tolerance mirrors
// extraction.build_salary_check (0.1%, min $1).
const VERDICT_CLASS = {
  match: 'ok', mismatch: 'warn', spotrac_only: 'warn',
  docs_only: 'muted', unavailable: 'muted',
}

const sameFigure = (a, b) => Math.abs(a - b) <= Math.max(Math.max(a, b) * 0.001, 1)

const salaryVerify = computed(() => {
  const c = extraction.value?.salary_check
  if (!c) return null
  const cur = Number(terms.salary) || 0
  const spo = Number(c.spotrac_salary) || 0
  const doc = Number(c.doc_salary) || 0
  let verdict
  if (spo && cur) {
    verdict = sameFigure(spo, cur) ? 'match' : 'mismatch'
  } else if (spo) {
    verdict = 'spotrac_only'
  } else {
    verdict = c.verdict === 'unavailable' && !cur ? 'unavailable' : 'docs_only'
  }
  return { ...c, verdict, docsDiffer: !!(spo && doc && !sameFigure(spo, doc)) }
})

const salaryVerifyMsg = computed(() => {
  const v = salaryVerify.value
  if (!v) return ''
  const tag = v.season ? `Spotrac (${v.season} season)` : 'Spotrac'
  switch (v.verdict) {
    case 'match': return v.docsDiffer
      ? `✓ From ${tag} cap hit ${money(v.spotrac_salary)} (primary) — the documents read ${money(v.doc_salary)}.`
      : `✓ Matches ${tag} cap hit: ${money(v.spotrac_salary)}`
    case 'mismatch': return `⚠ ${tag} cap hit is ${money(v.spotrac_salary)} — Spotrac is the primary source; the documents are the backup.`
    case 'spotrac_only': return `✓ From ${tag} cap hit ${money(v.spotrac_salary)} — the documents produced no figure.`
    case 'docs_only': return `${tag}: no usable figure — using the documents as backup.`
    default: return 'Spotrac check could not be run — using the documents; verify the salary manually.'
  }
})

const showUseSpotrac = computed(() =>
  ['mismatch', 'spotrac_only'].includes(salaryVerify.value?.verdict))
const showUseDocSalary = computed(() =>
  salaryVerify.value?.verdict === 'match' && salaryVerify.value?.docsDiffer)

// The same treatment for the Guaranteed remaining (LTC basis) field.
const remainingVerify = computed(() => {
  const c = extraction.value?.salary_check
  if (!c) return null
  const spo = Number(c.spotrac_remaining) || 0
  const doc = Number(c.doc_remaining) || 0
  if (!spo && !doc) return null
  const cur = Number(terms.contract_remaining) || 0
  let verdict
  if (spo && cur) {
    verdict = sameFigure(spo, cur) ? 'match' : 'mismatch'
  } else if (spo) {
    verdict = 'spotrac_only'
  } else {
    verdict = 'docs_only'
  }
  return {
    spotrac_remaining: spo, doc_remaining: doc, verdict,
    season: c.season, docsDiffer: !!(spo && doc && !sameFigure(spo, doc)),
  }
})

const remainingVerifyMsg = computed(() => {
  const v = remainingVerify.value
  if (!v) return ''
  switch (v.verdict) {
    case 'match': return v.docsDiffer
      ? `✓ From Spotrac: ${money(v.spotrac_remaining)} remaining (primary) — the documents read ${money(v.doc_remaining)}.`
      : `✓ Matches Spotrac remaining value: ${money(v.spotrac_remaining)}`
    case 'mismatch': return `⚠ Spotrac remaining value is ${money(v.spotrac_remaining)} — Spotrac is the primary source; the documents are the backup.`
    case 'spotrac_only': return `✓ From Spotrac: ${money(v.spotrac_remaining)} remaining — the documents produced no figure.`
    default: return 'Spotrac produced no remaining value — using the documents as backup.'
  }
})

const showUseSpotracRemaining = computed(() =>
  ['mismatch', 'spotrac_only'].includes(remainingVerify.value?.verdict))
const showUseDocRemaining = computed(() =>
  remainingVerify.value?.verdict === 'match' && remainingVerify.value?.docsDiffer)

// --- handlers --------------------------------------------------------------
function onFiles(e) {
  // Accumulate across selections so the user can add documents one or several
  // at a time (e.g. from different folders); dedupe by name+size.
  const seen = new Set(files.value.map((f) => f.name + ':' + f.size))
  for (const f of Array.from(e.target.files)) {
    const key = f.name + ':' + f.size
    if (!seen.has(key)) { files.value.push(f); seen.add(key) }
  }
  // reset so picking the same file again (or adding more later) still fires change
  e.target.value = ''
}

function removeFile(i) {
  files.value.splice(i, 1)
}

async function runExtract() {
  if (!files.value.length) return
  extracting.value = true
  status.type = 'info'
  status.msg = `Analyzing ${files.value.length} document(s) with Claude…`
  try {
    const ed = await extractDocuments(files.value)
    extraction.value = ed
    // auto-fill empty fields from the extraction
    const map = {
      name: ed.borrower_name, dob: ed.dob, addr: ed.address, phone: ed.phone,
      team: ed.team, league: ed.league, sport: ed.sport, ssn: ed.ssn_masked,
      dl: ed.drivers_license, agent: ed.agent,
    }
    for (const [k, v] of Object.entries(map)) if (v && !terms[k]) terms[k] = v
    // Numeric deal terms: only fill blanks so a value the user already typed
    // is never overwritten. The backend already made Spotrac the primary
    // source for the guaranteed salary and remaining value (documents as
    // backup — Lauren, 2026-08-25), so ed.salary / ed.contract_remaining
    // carry the right figure whichever source produced it.
    // The one exception to blanks-only: a salary CARRIED over by the
    // Structure tab (documents-first by design) and left untouched is a
    // convenience prefill, not the underwriter's confirmation — this tab's
    // Spotrac-primary figure supersedes it. A value the underwriter edited
    // no longer matches the carried record and still wins (rule 10).
    const carriedUntouched = (k) => carriedTerms[k] != null
      && String(terms[k] ?? '') === String(carriedTerms[k])
    if (ed.salary && (!terms.salary || carriedUntouched('salary'))) {
      if (carriedTerms.salary != null) carriedTerms.salary = ed.salary
      terms.salary = ed.salary
    }
    if (ed.contract_remaining && !terms.contract_remaining) {
      terms.contract_remaining = ed.contract_remaining
    }
    if (ed.loan_amount && !terms.loan) terms.loan = ed.loan_amount
    if (ed.interest_rate_pct && !terms.rate) terms.rate = ed.interest_rate_pct
    if (ed.origination_fee_pct && !terms.fee) terms.fee = ed.origination_fee_pct
    await refreshRollforward()
    // Say what actually happened to the two Spotrac-primary fields: the
    // Spotrac figures landed, they were kept out by values already set, or
    // Spotrac produced nothing and the documents (the backup) filled in —
    // the silent fallback must be loud enough to notice.
    const check = ed.salary_check || {}
    const fromSpotrac = check.salary_source === 'spotrac'
      || check.remaining_source === 'spotrac'
    const spotracBlocked =
      (check.salary_source === 'spotrac'
        && Number(terms.salary) !== Number(ed.salary))
      || (check.remaining_source === 'spotrac'
        && Number(terms.contract_remaining) !== Number(ed.contract_remaining))
    if (fromSpotrac && !spotracBlocked) {
      status.type = 'ok'
      status.msg = '✓ Extracted — guaranteed salary / remaining prefilled from Spotrac (primary source); the documents\' figures are shown beneath the fields as backup'
    } else if (fromSpotrac) {
      status.type = 'info'
      status.msg = '✓ Extracted — Spotrac produced figures (the primary source), but the salary / remaining fields already carried values, which were kept. Use "Use Spotrac figure" beneath a field to take Spotrac\'s number.'
    } else if (ed.salary || ed.contract_remaining) {
      status.type = 'info'
      status.msg = '✓ Extracted — Spotrac produced no figure, so the guaranteed salary / remaining are the documents\' figures (the backup). Verify them at spotrac.com, then confirm deal terms and generate.'
    } else {
      status.type = 'ok'
      status.msg = '✓ Extracted — confirm deal terms and generate'
    }
  } catch (err) {
    status.type = 'err'
    status.msg = 'Extraction failed: ' + err.message
  }
  extracting.value = false
}

function buildTermsPayload() {
  // normalize empty date strings to null for the backend
  return {
    ...terms,
    loan: Number(terms.loan) || 0,
    rate: Number(terms.rate) || 0,
    fee: Number(terms.fee) || 0,
    salary: Number(terms.salary) || 0,
    contract_remaining: Number(terms.contract_remaining) || 0,
    fund: terms.fund || null,
    mat: terms.mat || null,
  }
}

async function generate() {
  genError.value = ''
  try {
    memoHtmlContent.value = await memoHtml(buildTermsPayload(), extraction.value)
    memoReady.value = true
  } catch (err) {
    genError.value = err.message
  }
}

async function exportPdf() {
  try { await downloadPdf(buildTermsPayload(), extraction.value) }
  catch (err) { genError.value = err.message }
}
async function exportWord() {
  try { await downloadWord(buildTermsPayload(), extraction.value) }
  catch (err) { genError.value = err.message }
}
</script>

<template>
  <div class="wrap">
    <header class="masthead">
      <div>
        <div class="brand">South River Capital</div>
        <div class="tag">{{ TAB_TAGS[view] }}</div>
      </div>
      <nav class="tabs">
        <button :class="['tab', { active: view === 'structure' }]" @click="view = 'structure'">Structure</button>
        <button :class="['tab', { active: view === 'memo' }]" @click="view = 'memo'">Credit Memo</button>
        <button :class="['tab', { active: view === 'pa' }]" @click="view = 'pa'">Participation Agreement</button>
        <button :class="['tab', { active: view === 'loandocs' }]" @click="view = 'loandocs'">Loan Documents</button>
        <button :class="['tab', { active: view === 'binder' }]" @click="view = 'binder'">Closing Binder</button>
      </nav>
    </header>

    <!-- `files` is the SHARED deal-document list: documents dropped on the
         Structure tab are the same ones the Credit Memo extracts from, so a
         deal is uploaded once. `terms` is reactive, so the Structure tab fills
         the memo's deal terms directly (blanks only — typed values survive). -->
    <StructureBuilder v-if="view === 'structure'" :memo-terms="terms" :carried-terms="carriedTerms" :memo-extraction="extraction" :loan-docs-terms="loanDocsTerms" :deal-files="files" />
    <PaBuilder v-else-if="view === 'pa'" :memo-terms="terms" :memo-extraction="extraction" />
    <LoanDocsBuilder v-else-if="view === 'loandocs'" :memo-terms="terms" :memo-extraction="extraction" :terms-store="loanDocsTerms" />
    <ClosingBinderBuilder v-else-if="view === 'binder'" :loandocs-terms="loanDocsTerms" />

    <template v-else>
    <!-- Step 1: upload + extract -->
    <section class="card">
      <h2><span class="step">1</span> Upload deal documents</h2>
      <input type="file" multiple @change="onFiles" />
      <p class="hint">Select several at once, or add more one at a time — they accumulate.</p>
      <p v-if="files.length" class="hint">
        Documents uploaded on the <strong>Structure</strong> tab appear here automatically —
        this extraction is the fuller one (assets, liabilities, expenditures, uses of funds),
        so run it even if the terms are already filled in.
      </p>
      <ul v-if="files.length" class="filelist">
        <li v-for="(f, i) in files" :key="f.name + f.size">
          <span class="fname">{{ f.name }}</span>
          <button type="button" class="rm" @click="removeFile(i)" title="Remove">✕</button>
        </li>
      </ul>
      <p v-if="files.length" class="hint">{{ files.length }} file(s) selected</p>
      <button :disabled="!files.length || extracting" @click="runExtract">
        {{ extracting ? 'Analyzing…' : 'Extract with Claude' }}
      </button>
      <p v-if="status.msg" :class="['status', status.type]">{{ status.msg }}</p>
    </section>

    <!-- Step 2: confirm terms -->
    <section class="card">
      <h2><span class="step">2</span> Confirm deal terms</h2>
      <label class="chk">
        <input type="checkbox" v-model="terms.is_college" />
        College athlete (revenue share / NIL)
      </label>
      <p v-if="terms.is_college" class="hint college-note">
        🎓 College mode: the memo's Section IV risk FAQ swaps to college language —
        revenue-share payor risk (no league backstop), NIL counterparty risk, and a
        transfer / eligibility item — and the memo reads "Collegiate", not "Professional".
        Everything else (salary basis, LTC, cash flow) is unchanged.
      </p>
      <div class="grid">
        <label>Borrower name <input v-model="terms.name" /></label>
        <label>Team <input v-model="terms.team" /></label>
        <label>League <input v-model="terms.league" /></label>
        <label>Sport <input v-model="terms.sport" /></label>
        <label>Guaranteed salary
          <input v-model.number="terms.salary" type="number" />
          <span v-if="salaryVerify" :class="['verify', VERDICT_CLASS[salaryVerify.verdict]]">
            {{ salaryVerifyMsg }}
            <button v-if="showUseSpotrac" type="button" class="use"
                    @click="terms.salary = salaryVerify.spotrac_salary">Use Spotrac figure</button>
            <button v-if="showUseDocSalary" type="button" class="use"
                    @click="terms.salary = salaryVerify.doc_salary">Use contract figure</button>
            <a v-if="salaryVerify.spotrac_url" :href="salaryVerify.spotrac_url"
               target="_blank" rel="noopener">view&nbsp;↗</a>
          </span>
          <span v-if="salaryVerify?.note" class="verify-note">{{ salaryVerify.note }}</span>
        </label>
        <label>Guaranteed remaining (LTC basis)
          <input v-model.number="terms.contract_remaining" type="number" />
          <span v-if="remainingVerify" :class="['verify', VERDICT_CLASS[remainingVerify.verdict]]">
            {{ remainingVerifyMsg }}
            <button v-if="showUseSpotracRemaining" type="button" class="use"
                    @click="terms.contract_remaining = remainingVerify.spotrac_remaining">Use Spotrac figure</button>
            <button v-if="showUseDocRemaining" type="button" class="use"
                    @click="terms.contract_remaining = remainingVerify.doc_remaining">Use contract figure</button>
          </span>
          <span class="verify-note">Total remaining contract value — drives Guaranteed
            Remaining, LTC and Section I. Leave blank to use the guaranteed salary.</span>
        </label>
        <label>Loan amount <input v-model.number="terms.loan" type="number" /></label>
        <label>Loan type
          <select v-model="terms.loan_type">
            <option>Refinance / Modification</option>
            <option>New Loan</option>
            <option>Contract Advance</option>
            <option>Bridge Loan</option>
          </select>
        </label>
        <label>Rate (% p.a.) <input v-model.number="terms.rate" type="number" step="0.01" /></label>
        <label>Origination fee (%) <input v-model.number="terms.fee" type="number" step="0.01" /></label>
        <label>Funding date <input v-model="terms.fund" type="date" /></label>
        <label>Maturity date <input v-model="terms.mat" type="date" /></label>
        <label>Tax ID (last 4) <input v-model="terms.ssn" placeholder="XXX-XX-1234" /></label>
        <label>Agent <input v-model="terms.agent" /></label>
      </div>
    </section>

    <!-- Debt roll-forward: available whenever documents have been read, even
         when the PFS carried no detail schedules — debts can be added by hand -->
    <section v-if="extraction" class="card">
      <h2><span class="step">2b</span> Debt roll-forward</h2>
      <div class="rf-head">
        <label class="inline">PFS statement date
          <input v-model="extraction.pfs_date" type="date" @change="refreshRollforward" />
        </label>
        <p v-if="rollforward?.applied" class="hint">
          <strong>{{ money(rollforward.total_paydown) }}</strong> comes off the
          reported liabilities<span v-if="rollforward.months">
          &mdash; rolled forward {{ rollforward.months }} months to
          {{ rollforward.as_of }}, assuming payments were made as agreed</span>.
        </p>
        <p v-else class="hint">
          Balances are used exactly as reported &mdash; the statement is current,
          undated, or no debt is on a monthly schedule.
        </p>
      </div>

      <table v-if="extraction.debt_schedule?.length" class="rf">
        <thead>
          <tr>
            <th>Debt</th><th>Rolls into</th><th class="n">Reported</th>
            <th class="n">Payment</th><th>Maturity</th><th>Treatment</th>
            <th class="n">Adjusted</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(d, i) in extraction.debt_schedule" :key="i"
              v-show="d.treatment !== 'remove'"
              :class="{ held: !(rollforward?.rows?.[i]?.paydown > 0) }">
            <td>
              <input v-model="d.lender" class="mini wide" placeholder="Lender"
                     @change="refreshRollforward" />
            </td>
            <td>
              <select v-model="d.category" class="mini wide" @change="refreshRollforward">
                <option value="">— none —</option>
                <option v-for="c in DEBT_CATEGORIES" :key="c.value" :value="c.value">
                  {{ c.label }}
                </option>
              </select>
            </td>
            <td class="n">
              <input v-model.number="d.balance" type="number" class="mini"
                     @change="refreshRollforward" />
            </td>
            <td class="n">
              <input v-model.number="d.payment" type="number" class="mini"
                     @change="refreshRollforward" />
            </td>
            <td>
              <input v-model="d.maturity" class="mini wide" @change="refreshRollforward" />
            </td>
            <td>
              <select v-model="d.treatment" class="mini wide" @change="refreshRollforward">
                <option value="roll">Roll forward</option>
                <option value="hold">Hold as reported</option>
                <option value="zero">Zero out (repaid)</option>
              </select>
            </td>
            <td class="n">
              <strong v-if="rollforward?.rows?.[i]?.paydown > 0">
                {{ money(rollforward.rows[i].adjusted) }}
              </strong>
              <span v-else class="reason">{{ rollforward?.rows?.[i]?.reason || '—' }}</span>
            </td>
            <td>
              <button type="button" class="rm" @click="removeDebt(i)"
                      title="Remove from the memo">✕</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">
        No debt schedules were found in the uploaded documents. Add each financed
        debt from the PFS's Schedule D / F / G below so it can be rolled forward.
      </p>

      <button type="button" class="ghost" @click="addDebt">+ Add debt</button>

      <p v-for="r in removedDebts" :key="'rm' + r.i" class="status info">
        ✕ {{ r.d.lender || 'Debt' }} removed from the memo<template v-if="categoryLabel(r.d.category)">
        — {{ money(r.d.balance) }} comes out of "{{ categoryLabel(r.d.category) }}" and Total Liabilities; nothing about it will print</template><template v-else>
        — it had no "Rolls into" category, so the statement totals are unchanged</template>.
        <button type="button" class="use" @click="restoreDebt(r.i)">Restore</button>
      </p>
      <p v-for="(w, i) in rollforward?.warnings || []" :key="i" class="status err">⚠ {{ w }}</p>
      <p v-if="rfError" class="status err">⚠ {{ rfError }}</p>
      <p v-if="rollforward?.note" class="rf-note">{{ rollforward.note }}</p>
      <p class="hint">
        <strong>Roll forward</strong> reduces the balance by the payment for each
        month since the statement date. <strong>Hold</strong> carries it exactly as
        reported. <strong>Zero out</strong> shows it repaid in full &mdash; use it
        for a debt being paid off at closing. &ldquo;Rolls into&rdquo; is the
        Personal Financial Statement total the adjustment comes out of, so a debt
        with none set changes no total. Payments include interest (and mortgage
        payments on this form include taxes &amp; insurance), so the roll-forward
        understates the true balance.
      </p>
    </section>

    <!-- Step 3: generate -->
    <section class="card">
      <h2><span class="step">3</span> Generate memo</h2>
      <p v-if="genError" class="status err">⚠ {{ genError }}</p>
      <button :disabled="!canGenerate" @click="generate">Generate credit memo</button>
      <template v-if="memoReady">
        <button class="ghost" @click="exportPdf">📕 PDF</button>
        <button class="ghost" @click="exportWord">📄 Word</button>
      </template>
    </section>

    <section v-if="memoReady" class="card">
      <h2>Preview</h2>
      <iframe :srcdoc="memoHtmlContent" class="preview" title="Credit memo preview"></iframe>
    </section>
    </template>
  </div>
</template>

<style>
:root { --navy: #0f2a43; --gold: #b9952b; }
body { margin: 0; background: #eceae3; font-family: system-ui, sans-serif; color: #1a1a1a; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 20px; }
.masthead { border-bottom: 3px solid var(--navy); padding-bottom: 12px; margin-bottom: 18px; display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.brand { font-size: 20px; font-weight: 700; color: var(--navy); letter-spacing: .04em; text-transform: uppercase; }
.tag { font-size: 11px; letter-spacing: .25em; color: var(--gold); text-transform: uppercase; margin-top: 4px; }
.tabs { display: flex; gap: 4px; }
.tab { background: #fff; color: var(--navy); border: 1px solid #cdd3da; border-bottom: 0; border-radius: 8px 8px 0 0; padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer; margin: 0 0 -1px; }
.tab.active { background: var(--navy); color: #fff; border-color: var(--navy); }
.card { background: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 16px 18px; margin-bottom: 14px; }
.card h2 { font-size: 14px; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }
.step { background: var(--navy); color: #fff; width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; }
.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.chk { flex-direction: row; align-items: center; gap: 6px; font-size: 13px; color: #1a1a1a; margin-bottom: 10px; }
.chk input { width: auto; }
.college-note { margin: -4px 0 10px; }
label { display: flex; flex-direction: column; font-size: 12px; gap: 4px; color: #555; }
input, select { padding: 6px 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; background: #fff; }
.rf-head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.rf-head .hint { margin: 0 0 4px; }
label.inline { flex-direction: column; }
table.rf { width: 100%; border-collapse: collapse; font-size: 12px; }
table.rf th { text-align: left; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: #777; border-bottom: 1px solid #ddd; padding: 4px 6px; }
table.rf td { padding: 4px 6px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
table.rf td.n, table.rf th.n { text-align: right; }
table.rf td.c, table.rf th.c { text-align: center; }
table.rf tr.held { color: #888; }
.cat { display: block; font-size: 10px; color: #999; text-transform: capitalize; }
.reason { font-size: 11px; font-style: italic; color: #999; }
input.mini { padding: 3px 5px; font-size: 12px; width: 90px; text-align: right; }
input.mini.wide { text-align: left; width: 100px; }
.rf-note { background: #f7f6f2; border-left: 3px solid var(--gold); padding: 8px 10px; font-size: 12px; line-height: 1.5; color: #333; }
.verify { font-size: 11px; line-height: 1.45; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.verify.ok { color: #0f6e56; }
.verify.warn { color: #9a6b00; }
.verify.muted { color: #888; }
.verify a { color: inherit; }
.verify .use { background: none; border: 0; padding: 0; margin: 0; color: #0c447c; text-decoration: underline; cursor: pointer; font-size: 11px; font-weight: 600; }
.verify-note { font-size: 11px; line-height: 1.45; color: #888; font-style: italic; }
button { background: var(--navy); color: #fff; border: 0; border-radius: 6px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; margin-right: 8px; margin-top: 8px; }
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: #fff; color: var(--navy); border: 1px solid var(--navy); }
.hint { font-size: 12px; color: #888; }
.filelist { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.filelist li { display: flex; align-items: center; justify-content: space-between; gap: 10px; background: #f4f6f8; border: 1px solid #e0e4e9; border-radius: 6px; padding: 5px 10px; font-size: 12px; }
.filelist .fname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filelist .rm { background: transparent; color: #b00020; border: 0; padding: 0 4px; margin: 0; font-size: 13px; line-height: 1; cursor: pointer; }
.status { font-size: 12px; padding: 8px 10px; border-radius: 6px; margin-top: 10px; }
.status.info { background: #e6f1fb; color: #0c447c; }
.status.ok { background: #e8f5ee; color: #0f6e56; }
.status.err { background: #fdecea; color: #b00020; }
.preview { width: 100%; height: 720px; border: 1px solid #ddd; border-radius: 6px; }
@media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
</style>
