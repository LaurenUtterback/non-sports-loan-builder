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
// value left untouched must not block this tab's own extraction prefill.
const carriedTerms = reactive({})

// --- state -----------------------------------------------------------------
const files = ref([])
const extracting = ref(false)
const status = reactive({ type: '', msg: '' })
const extraction = ref(null)

const terms = reactive({
  name: '', dob: '', addr: '', phone: '',
  employer: '', industry: '', occupation: '',
  ssn: '', dl: '', advisor: '',
  loan: null, rate: null, fee: null, income: null, contracted_income: null,
  collateral_value: null, collateral_desc: '',
  fund: '', mat: '', loan_type: 'New Loan',
  // Which borrower profile this deal is: swaps the memo's Section IV risk FAQ,
  // Section I's purpose language and Section XI's repayment sources.
  // Underwriter-set only — the extraction never touches it.
  profile: 'business',
})

const PROFILE_NOTES = {
  business: 'Business owner / executive: the risk FAQ covers customer concentration, income volatility and key-person risk. Qualifying income comes from the tax returns.',
  investment: 'Investment & real estate: the risk FAQ covers vacancy and tenant default, collateral valuation and refinancing risk at maturity. Rental income must be NET of operating costs and existing mortgage debt service.',
  consumer: 'General consumer / commercial: the risk FAQ covers income interruption and death or disability. Income is documented from returns, W-2s and pay stubs.',
}

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
const canGenerate = computed(() => terms.loan && terms.income)

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
      employer: ed.employer, industry: ed.industry, occupation: ed.occupation,
      ssn: ed.ssn_masked, dl: ed.drivers_license, advisor: ed.advisor,
      collateral_desc: ed.collateral_desc,
    }
    for (const [k, v] of Object.entries(map)) if (v && !terms[k]) terms[k] = v
    // Numeric deal terms: only fill blanks so a value the user already typed
    // is never overwritten. The one exception: an income figure CARRIED over
    // by the Structure tab and left untouched is a convenience prefill, not
    // the underwriter's confirmation, so this tab's own extraction supersedes
    // it. A value the underwriter edited no longer matches the carried record
    // and still wins (rule 10).
    const carriedUntouched = (k) => carriedTerms[k] != null
      && String(terms[k] ?? '') === String(carriedTerms[k])
    if (ed.qualifying_income && (!terms.income || carriedUntouched('income'))) {
      if (carriedTerms.income != null) carriedTerms.income = ed.qualifying_income
      terms.income = ed.qualifying_income
    }
    if (ed.contracted_income && !terms.contracted_income) {
      terms.contracted_income = ed.contracted_income
    }
    if (ed.collateral_value && !terms.collateral_value) {
      terms.collateral_value = ed.collateral_value
    }
    if (ed.loan_amount && !terms.loan) terms.loan = ed.loan_amount
    if (ed.interest_rate_pct && !terms.rate) terms.rate = ed.interest_rate_pct
    if (ed.origination_fee_pct && !terms.fee) terms.fee = ed.origination_fee_pct
    await refreshRollforward()
    // Say which figures actually landed, so a silent blank is loud enough
    // to notice before the memo is generated.
    if (ed.qualifying_income) {
      status.type = 'ok'
      const extras = []
      if (ed.contracted_income) extras.push('contracted income')
      if (ed.collateral_value) extras.push('pledged collateral')
      status.msg = '\u2713 Extracted \u2014 qualifying income prefilled from the documents'
        + (extras.length ? ', along with ' + extras.join(' and ') : '')
        + '. Read the Income Analysis note in Step 2, confirm the terms, then generate.'
    } else {
      status.type = 'info'
      status.msg = '\u2713 Extracted \u2014 but the documents produced NO qualifying income figure. Enter it in Step 2 yourself; the memo cannot compute LTI, DSCR or the cash flow without it.'
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
    income: Number(terms.income) || 0,
    contracted_income: Number(terms.contracted_income) || 0,
    collateral_value: Number(terms.collateral_value) || 0,
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
        <div class="tag">Non-Sports Loans &middot; {{ TAB_TAGS[view] }}</div>
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
      <label class="profile-pick">Borrower profile
        <select v-model="terms.profile">
          <option value="business">Business owner / executive</option>
          <option value="investment">Investment &amp; real estate</option>
          <option value="consumer">General consumer / commercial</option>
        </select>
      </label>
      <p class="hint profile-note">{{ PROFILE_NOTES[terms.profile] }}</p>
      <div class="grid">
        <label>Borrower name <input v-model="terms.name" /></label>
        <label>Business / employer <input v-model="terms.employer" /></label>
        <label>Industry <input v-model="terms.industry" /></label>
        <label>Role / title <input v-model="terms.occupation" /></label>
        <label>Qualifying income (annual)
          <input v-model.number="terms.income" type="number" />
          <span class="verify-note">Documented, recurring income only — net of
            operating costs on rental income, at the borrower's ownership share
            on business income. Drives the cash flow, LTI and DSCR.</span>
        </label>
        <label>Contracted income (LTI basis)
          <input v-model.number="terms.contracted_income" type="number" />
          <span class="verify-note">Income committed by contract or lease over
            the facility's term. Drives LTI and Section I. Most deals have none —
            leave blank to use the qualifying income.</span>
        </label>
        <label>Pledged collateral value
          <input v-model.number="terms.collateral_value" type="number" />
          <span class="verify-note">Third-party valuation where there is one.
            Blank means unsecured: the coversheet then reports LTV as N/A
            rather than as a pass.</span>
        </label>
        <label>Pledged collateral <input v-model="terms.collateral_desc"
            placeholder="e.g. mixed-use building, 12 Harbor Road" /></label>
        <label>Loan amount <input v-model.number="terms.loan" type="number" /></label>
        <label>Loan type
          <select v-model="terms.loan_type">
            <option>Refinance / Modification</option>
            <option>New Loan</option>
            <option>Asset-Secured Loan</option>
            <option>Bridge Loan</option>
          </select>
        </label>
        <label>Rate (% p.a.) <input v-model.number="terms.rate" type="number" step="0.01" /></label>
        <label>Origination fee (%) <input v-model.number="terms.fee" type="number" step="0.01" /></label>
        <label>Funding date <input v-model="terms.fund" type="date" /></label>
        <label>Maturity date <input v-model="terms.mat" type="date" /></label>
        <label>Tax ID (last 4) <input v-model="terms.ssn" placeholder="XXX-XX-1234" /></label>
        <label>Adviser (CPA / attorney) <input v-model="terms.advisor" /></label>
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
.profile-pick { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; color: #1a1a1a; margin-bottom: 6px; }
.profile-pick select { width: auto; min-width: 240px; }
.profile-note { margin: -2px 0 12px; max-width: 70ch; }
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
