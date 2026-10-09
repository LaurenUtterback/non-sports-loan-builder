<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { paExtract, paBreakdown, paDownloadDocx, paDownloadPdf, paPreviewPdf, paDefaults, paSend } from './lib/api.js'

// Deal terms the user already confirmed on the Credit Memo tab (shared parent state).
const props = defineProps({
  memoTerms: { type: Object, default: () => ({}) },
  memoExtraction: { type: Object, default: null },
})

const BROOKRIDGE_PARTICIPANT = 'Brookridge Opportunistic Credit Fund, LP'
const BROOKRIDGE_SIGNATORY = 'Nate Bohn'   // Brookridge always signs via Nate Bohn

// Participants whose Signatory differs from the printed Participant Name.
// Everyone else signs in their own name (Signatory Name = Participant Name).
const PARTICIPANT_OVERRIDES = {
  'john howe': { participant: 'Howe Acres, LLC', signatory: 'John Howe' },
  'michael gooch': { participant: 'MG Separate Property Trust', signatory: 'Michael Gooch' },
  'john evans': { participant: 'Evo Capital, LLC', signatory: 'John Evans' },
}

// Today's date (yyyy-mm-dd) — the Agreement date defaults to the generation date.
const TODAY_ISO = (() => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})()

// --- state -----------------------------------------------------------------
const agreementType = ref('brookridge')           // 'brookridge' | 'standard'

const terms = reactive({
  // Deal (from the Credit Memo pull / Participant Breakdown)
  borrower_name: '',
  agreement_date: TODAY_ISO,      // defaults to the generation date; editable
  loan_agreement_date: '',
  loan_number: '',
  total_loan_amount: '',         // gross loan; feeds recital/cert principal
  loan_principal: '',            // derived from total (cents rule depends on type)
  interest_rate_apr: '',
  // Participation terms
  participation_percentage: '',
  participant_loan_amount: '',   // brookridge
  purchase_price: '',            // standard
  origination_fee_pct: '',
  origination_fee_amount: '',    // brookridge
  app_admin_fees_pct: '0%',      // standard
  late_fee_share_pct: '',
  servicing_fee_pct: '0%',
  // Participant & signatory
  participant_name: BROOKRIDGE_PARTICIPANT,
  participant_signatory_name: BROOKRIDGE_SIGNATORY,
  participant_signatory_title: '',
  participant_address: '',
  participant_email: '',
})

const previewUrl = ref('')
const generating = ref(false)
const genError = ref('')

// Participant Breakdown (.xlsx)
const breakdownFiles = ref([])
const breakdownLoading = ref(false)
const breakdownStatus = reactive({ type: '', msg: '' })
const breakdownDeal = ref(null)
const participantOptions = ref([])     // [{name, ...computed terms}]
const selectedParticipant = ref('')

// Upload a previously-generated Credit Memo (PDF/Word) and read deal info from it
const memoFiles = ref([])
const memoReading = ref(false)
const memoStatus = reactive({ type: '', msg: '' })

const isBrookridge = computed(() => agreementType.value === 'brookridge')

// Switching forms: keep the participant default sensible, and re-map the chosen
// breakdown participant's $ into the right field for the new form.
watch(agreementType, (now) => {
  if (now === 'brookridge') {
    if (!terms.participant_name) terms.participant_name = BROOKRIDGE_PARTICIPANT
    if (!terms.participant_signatory_name) terms.participant_signatory_name = BROOKRIDGE_SIGNATORY
  } else {
    if (terms.participant_name === BROOKRIDGE_PARTICIPANT) terms.participant_name = ''
    if (terms.participant_signatory_name === BROOKRIDGE_SIGNATORY) terms.participant_signatory_name = ''
  }
  if (selectedParticipant.value) applyParticipant()
  previewUrl.value = ''
})

// --- formatting helpers ----------------------------------------------------
function fmtMoney(n, cents = true) {
  if (n == null || isNaN(n) || n === 0) return ''
  return '$' + Number(n).toLocaleString('en-US', {
    minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0,
  })
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return ''
  return String(+Number(n).toFixed(2)) + '%'
}
function parseNum(s) {
  if (!s) return 0
  const v = parseFloat(String(s).replace(/[^0-9.\-]/g, ''))
  return isNaN(v) ? 0 : v
}
function fmtLongDate(s) {
  const m = String(s || '').match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!m) return s || ''
  return new Date(+m[1], +m[2] - 1, +m[3])
    .toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
}
// Normalize a date string to yyyy-mm-dd for the <input type=date> pickers.
function toISODate(s) {
  if (!s) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s
  const d = new Date(s)
  if (isNaN(d)) return ''
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

// --- Pull deal info from the Credit Memo tab ------------------------------
const hasMemo = computed(() => {
  const t = props.memoTerms || {}
  return !!(t.name || t.loan)
})

function pullFromMemo() {
  const t = props.memoTerms || {}
  if (t.name) terms.borrower_name = t.name
  if (t.loan) terms.total_loan_amount = fmtMoney(Number(t.loan), true)
  if (t.rate) terms.interest_rate_apr = fmtPct(Number(t.rate))
  if (t.fee) terms.origination_fee_pct = fmtPct(Number(t.fee))
  // Agreement date stays the generation date (today) — not the funding date.
}

// Upload a previously-generated Credit Memo and let Claude read the deal info.
// Fills deal-level fields only (never the participation split — that's the
// breakdown's job) and never overwrites a field that's already set.
function fill(key, value) { if (value && !terms[key]) terms[key] = value }
function onMemoFiles(e) { memoFiles.value = Array.from(e.target.files) }

async function readCreditMemo() {
  if (!memoFiles.value.length) return
  memoReading.value = true
  memoStatus.type = 'info'
  memoStatus.msg = 'Reading the credit memo with Claude…'
  try {
    const ed = await paExtract(memoFiles.value)
    fill('borrower_name', ed.borrower_name)
    fill('loan_agreement_date', toISODate(ed.loan_agreement_date))
    fill('loan_number', ed.loan_number)
    if (ed.loan_amount > 0) fill('total_loan_amount', fmtMoney(ed.loan_amount, true))
    if (ed.interest_rate_apr > 0) fill('interest_rate_apr', fmtPct(ed.interest_rate_apr))
    if (ed.origination_fee_pct > 0) fill('origination_fee_pct', fmtPct(ed.origination_fee_pct))
    memoStatus.type = 'ok'
    memoStatus.msg = '✓ Read the credit memo — deal info filled below. Add the breakdown for the participation split.'
  } catch (err) {
    memoStatus.type = 'err'
    memoStatus.msg = 'Could not read the credit memo: ' + err.message
  }
  memoReading.value = false
}

// Recompute the derived Key Terms from the entered amounts.
function recalc() {
  const total = parseNum(terms.total_loan_amount)
  if (isBrookridge.value) {
    const part = parseNum(terms.participant_loan_amount)
    const feePct = parseNum(terms.origination_fee_pct)
    if (total > 0 && part > 0) terms.participation_percentage = fmtPct((part / total) * 100)
    if (part > 0 && feePct > 0) terms.origination_fee_amount = fmtMoney((part * feePct) / 100, true)
  } else {
    const purchase = parseNum(terms.purchase_price)
    if (total > 0 && purchase > 0) terms.participation_percentage = fmtPct((purchase / total) * 100)
  }
}

// --- Participant Breakdown ------------------------------------------------
function onBreakdownFiles(e) { breakdownFiles.value = Array.from(e.target.files) }

async function loadBreakdown() {
  if (!breakdownFiles.value.length) return
  breakdownLoading.value = true
  breakdownStatus.type = 'info'
  breakdownStatus.msg = 'Reading breakdown…'
  try {
    const res = await paBreakdown(breakdownFiles.value)
    breakdownDeal.value = res.deal
    participantOptions.value = res.participants || []
    if (!participantOptions.value.length) throw new Error('No participants found in the sheet.')
    selectedParticipant.value = participantOptions.value[0].name
    applyParticipant()
    breakdownStatus.type = 'ok'
    breakdownStatus.msg = `✓ Loaded ${participantOptions.value.length} participant(s) for ${res.deal.borrower_name || 'this deal'} — pick one below.`
  } catch (err) {
    breakdownStatus.type = 'err'
    breakdownStatus.msg = 'Breakdown failed: ' + err.message
  }
  breakdownLoading.value = false
}

// Resolve a breakdown participant name -> printed Participant Name + Signatory.
// Brookridge keeps its fixed info; overrides (e.g. John Howe -> Howe Acres, LLC)
// name an entity with a distinct signer; everyone else signs in their own name.
function resolveParticipant(rawName) {
  const raw = (rawName || '').trim()
  const key = raw.toLowerCase()
  if (key.startsWith('brookridge')) {
    return { participant: BROOKRIDGE_PARTICIPANT, signatory: BROOKRIDGE_SIGNATORY }
  }
  return PARTICIPANT_OVERRIDES[key] || { participant: raw, signatory: raw }
}

// Fill the form from the chosen breakdown participant (authoritative — overwrites).
function applyParticipant() {
  const p = participantOptions.value.find((x) => x.name === selectedParticipant.value)
  if (!p) return
  const d = breakdownDeal.value || {}
  if (d.borrower_name) terms.borrower_name = d.borrower_name
  if (d.loan_number) terms.loan_number = String(d.loan_number)
  if (d.loan_amount) terms.total_loan_amount = fmtMoney(d.loan_amount, true)

  const resolved = resolveParticipant(p.name)
  terms.participant_name = resolved.participant
  terms.participant_signatory_name = resolved.signatory
  // These come from the breakdown already formatted at the sheet's precision.
  terms.participation_percentage = p.participation_pct
  terms.origination_fee_pct = p.points_pct
  terms.interest_rate_apr = p.interest_rate
  terms.late_fee_share_pct = p.late_fee_share_pct
  terms.participant_email = p.email || ''   // from breakdown Sheet 2; blank if none
  if (isBrookridge.value) {
    terms.participant_loan_amount = p.amount
    terms.origination_fee_amount = p.points_amount
  } else {
    terms.purchase_price = p.amount
  }
}

const canGenerate = computed(() =>
  terms.borrower_name && terms.total_loan_amount && terms.participation_percentage)

function syncBeforeSend() {
  const total = parseNum(terms.total_loan_amount)
  if (total > 0) {
    if (isBrookridge.value) {
      terms.total_loan_amount = fmtMoney(total, true)
      terms.loan_principal = fmtMoney(total, false)
    } else {
      terms.loan_principal = fmtMoney(total, true)
    }
  }
  // The date pickers hold yyyy-mm-dd; the agreement prints "Month D, YYYY".
  return {
    ...terms,
    agreement_date: fmtLongDate(terms.agreement_date),
    loan_agreement_date: fmtLongDate(terms.loan_agreement_date),
  }
}

async function generatePreview() {
  genError.value = ''
  generating.value = true
  try {
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = await paPreviewPdf(syncBeforeSend(), agreementType.value)
  } catch (err) { genError.value = err.message }
  generating.value = false
}
async function downloadDocx() {
  genError.value = ''
  try { await paDownloadDocx(syncBeforeSend(), agreementType.value) }
  catch (err) { genError.value = err.message }
}
async function downloadPdf() {
  genError.value = ''
  try { await paDownloadPdf(syncBeforeSend(), agreementType.value) }
  catch (err) { genError.value = err.message }
}

// --- Step 4: send out for signature -----------------------------------------
// The SRC signer identity and the e-signature site's name/URL come from .env
// via /api/pa/defaults — deployment details, never hard-coded (public repo).
const esign = reactive({
  signer_name: 'James Plack', signer_email: '', esign_name: '', esign_url: '',
  ready: false, mode: '',      // one-click Demand Signatures sending configured? test or live?
})
const copyStatus = ref('')
const sending = ref(false)
const sendStatus = reactive({ type: '', msg: '' })

onMounted(async () => {
  const d = await paDefaults()
  if (d.signer_name) esign.signer_name = d.signer_name
  if (d.signer_email) esign.signer_email = d.signer_email
  esign.esign_name = d.esign_name || ''
  esign.esign_url = d.esign_url || ''
  esign.ready = !!d.esign_ready
  esign.mode = d.esign_mode || ''
})

const canSend = computed(() =>
  canGenerate.value
  && String(esign.signer_email).includes('@')
  && String(terms.participant_email).includes('@'))

async function sendForSignature() {
  sendStatus.type = 'info'
  sendStatus.msg = 'Generating the PDF and sending it out for signature…'
  sending.value = true
  try {
    const r = await paSend(syncBeforeSend(), agreementType.value,
      { name: esign.signer_name, email: esign.signer_email })
    sendStatus.type = 'ok'
    sendStatus.msg = `✓ Sent — ${esign.signer_name} and ${terms.participant_signatory_name || terms.participant_name} `
      + `will each get a Demand Signatures email with the signature fields already placed.`
      + (r.mode === 'demo' ? ' (Demo mode: the documents carry a DEMONSTRATION watermark.)' : '')
  } catch (err) {
    sendStatus.type = 'err'
    sendStatus.msg = 'Could not send: ' + err.message
  }
  sending.value = false
}

const esignSiteLabel = computed(() => esign.esign_name || 'your e-signature site')

function signerLines() {
  return [
    `${esign.signer_name || 'South River Capital signer'} <${esign.signer_email || 'no email set'}>`,
    `${terms.participant_signatory_name || 'Participant signer'} <${terms.participant_email || 'no email set'}>`,
  ].join('\n')
}

// Synchronous copy inside the click gesture — never hangs, works everywhere.
function legacyCopy(text) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try { ok = document.execCommand('copy') } catch { ok = false }
  ta.remove()
  return ok
}

async function copySigners() {
  const text = signerLines()
  let ok = legacyCopy(text)
  if (!ok && navigator.clipboard) {
    // Async clipboard API as backup — with a timeout so a permission prompt
    // that never resolves can't leave the button looking dead.
    ok = await Promise.race([
      navigator.clipboard.writeText(text).then(() => true, () => false),
      new Promise((resolve) => setTimeout(() => resolve(false), 1500)),
    ])
  }
  copyStatus.value = ok
    ? '✓ Signer list copied — paste it into the recipients screen.'
    : text  // last resort: show the list so it can be copied by hand
  setTimeout(() => { copyStatus.value = '' }, 8000)
}

function openEsignSite() {
  if (esign.esign_url) window.open(esign.esign_url, '_blank', 'noopener')
}
</script>

<template>
  <!-- Step 1: pick the form + load deal info -->
  <section class="card">
    <h2><span class="step">1</span> Choose the agreement &amp; load the breakdown</h2>
    <div class="grid">
      <label>Agreement type
        <select v-model="agreementType">
          <option value="brookridge">Brookridge Participation Agreement</option>
          <option value="standard">Participation Agreement (standard form)</option>
        </select>
      </label>
    </div>

    <h3 class="grp">Credit Memo</h3>
    <div v-if="hasMemo" class="pull">
      <button class="ghost" @click="pullFromMemo">⬇ Pull deal info from the Credit Memo tab</button>
      <span class="hint">Brings over the borrower, loan amount, interest rate and origination fee you entered on the Credit Memo tab.</span>
    </div>
    <p class="hint">
      Or, if the Credit Memo was generated previously, upload it (PDF works best) and Claude
      reads the deal terms. (The participation split still comes from the breakdown below.)
    </p>
    <input type="file" accept=".pdf,.doc,.docx,.htm,.html" @change="onMemoFiles" />
    <button :disabled="!memoFiles.length || memoReading" @click="readCreditMemo">
      {{ memoReading ? 'Reading…' : 'Read credit memo' }}
    </button>
    <p v-if="memoStatus.msg" :class="['status', memoStatus.type]">{{ memoStatus.msg }}</p>

    <h3 class="grp">Participant Breakdown (.xlsx)</h3>
    <p class="hint">
      Drop the deal's Participant Breakdown sheet to pull each participant's
      Participation %, Points % / $, interest rate and late-fee share. Pick a
      participant and the Key Terms below fill in automatically.
    </p>
    <input type="file" accept=".xlsx,.xlsm" @change="onBreakdownFiles" />
    <button :disabled="!breakdownFiles.length || breakdownLoading" @click="loadBreakdown">
      {{ breakdownLoading ? 'Reading…' : 'Load breakdown' }}
    </button>
    <p v-if="breakdownStatus.msg" :class="['status', breakdownStatus.type]">{{ breakdownStatus.msg }}</p>
    <div v-if="participantOptions.length" class="grid">
      <label>Participant (from breakdown)
        <select v-model="selectedParticipant" @change="applyParticipant">
          <option v-for="p in participantOptions" :key="p.name" :value="p.name">
            {{ p.name }} — {{ p.participation_pct }}, {{ p.amount }}
          </option>
        </select>
      </label>
    </div>
  </section>

  <!-- Step 2: confirm terms -->
  <section class="card">
    <h2><span class="step">2</span> Confirm the terms</h2>
    <p class="hint">
      Lender is fixed as <strong>South River Capital LLC</strong>. Everything below
      prints exactly as typed — review carefully before generating.
    </p>

    <h3 class="grp">Deal</h3>
    <div class="grid">
      <label>Borrower <input v-model="terms.borrower_name" /></label>
      <label>Loan number <input v-model="terms.loan_number" /></label>
      <label>Agreement date <input v-model="terms.agreement_date" type="date" /></label>
      <label>Loan &amp; Security Agreement date <input v-model="terms.loan_agreement_date" type="date" /></label>
      <label>Total loan amount <input v-model="terms.total_loan_amount" placeholder="$500,000.00" /></label>
      <label>Interest rate / APR <input v-model="terms.interest_rate_apr" placeholder="12%" /></label>
    </div>

    <h3 class="grp">
      Participation — Key Terms (Exhibit A)
      <button class="link" type="button" @click="recalc" title="Recompute % (and fee $) from the amounts">↻ Recalculate</button>
    </h3>
    <div class="grid">
      <!-- brookridge-specific -->
      <label v-if="isBrookridge">Participant's loan amount <input v-model="terms.participant_loan_amount" placeholder="$100,000.00" /></label>
      <!-- standard-specific -->
      <label v-else>Purchase price <input v-model="terms.purchase_price" placeholder="$100,000.00" /></label>

      <label>Participation % <input v-model="terms.participation_percentage" placeholder="20.00%" /></label>
      <label>Origination fee % <input v-model="terms.origination_fee_pct" placeholder="2.00%" /></label>

      <label v-if="isBrookridge">Origination fee $ <input v-model="terms.origination_fee_amount" placeholder="$10,000.00" /></label>
      <label v-else>Application &amp; administration fees <input v-model="terms.app_admin_fees_pct" placeholder="0%" /></label>

      <label>Participant's share of late fees <input v-model="terms.late_fee_share_pct" :placeholder="isBrookridge ? '10.00%' : '50.00% multiplied by the Participation Percentage'" /></label>
      <label>Servicing fee <input v-model="terms.servicing_fee_pct" placeholder="0%" /></label>
    </div>

    <h3 class="grp">Participant &amp; signatory</h3>
    <div class="grid">
      <label>Participant name <input v-model="terms.participant_name" :placeholder="isBrookridge ? '' : 'e.g. ABC Credit Fund, LP'" /></label>
      <label>Signatory name <input v-model="terms.participant_signatory_name" placeholder="e.g. Jane Smith" /></label>
      <label>Signatory title <input v-model="terms.participant_signatory_title" /></label>
      <label>Address <input v-model="terms.participant_address" /></label>
      <label>Email <input v-model="terms.participant_email" /></label>
    </div>
  </section>

  <!-- Step 3: generate -->
  <section class="card">
    <h2><span class="step">3</span> Generate the agreement</h2>
    <p v-if="genError" class="status err">⚠ {{ genError }}</p>
    <button :disabled="!canGenerate || generating" @click="generatePreview">
      {{ generating ? 'Generating…' : 'Generate & preview' }}
    </button>
    <button class="ghost" :disabled="!canGenerate" @click="downloadDocx">📄 Word (.docx)</button>
    <button class="ghost" :disabled="!canGenerate" @click="downloadPdf">📕 PDF</button>
  </section>

  <section v-if="previewUrl" class="card">
    <h2>Preview</h2>
    <iframe :src="previewUrl" class="preview" title="Participation agreement preview"></iframe>
  </section>

  <!-- Step 4: send out for signature -->
  <section class="card">
    <h2><span class="step">4</span> Send out for signature</h2>
    <p v-if="esign.ready" class="hint">
      One click sends the finished agreement to both signers through Demand Signatures, with the
      signature fields already placed on the signature page and the Exhibit&nbsp;B certificate.
      The participant's email comes from the breakdown (or type it in Step 2 — the two stay in sync).
    </p>
    <p v-else class="hint">
      Download the finished PDF, then open {{ esignSiteLabel }} and upload it there.
      The signers below fill in automatically — the participant's email comes from
      the breakdown's email sheet (or type it in Step 2; the two stay in sync).
    </p>
    <h3 class="grp">Signers</h3>
    <div class="grid">
      <label>South River signer <input v-model="esign.signer_name" /></label>
      <label>South River signer email <input v-model="esign.signer_email" placeholder="set SRC_SIGNER_EMAIL in .env" /></label>
      <label>Participant signer <input v-model="terms.participant_signatory_name" placeholder="e.g. Jane Smith" /></label>
      <label>Participant email <input v-model="terms.participant_email" placeholder="from the breakdown, or type it" /></label>
    </div>
    <template v-if="esign.ready">
      <button :disabled="!canSend || sending" @click="sendForSignature">
        {{ sending ? 'Sending…' : '✒️ Send for signature' }}
      </button>
      <button class="ghost" :disabled="!canGenerate" @click="downloadPdf">📕 PDF copy</button>
      <button class="ghost" @click="copySigners">📋 Copy signer list</button>
      <p v-if="!canSend && canGenerate" class="hint">Both signers need an email address before sending.</p>
      <p v-if="sendStatus.msg" :class="['status', sendStatus.type]">{{ sendStatus.msg }}</p>
    </template>
    <template v-else>
      <button class="ghost" :disabled="!canGenerate" @click="downloadPdf">📕 Download the final PDF</button>
      <button class="ghost" @click="copySigners">📋 Copy signer list</button>
      <button v-if="esign.esign_url" class="ghost" @click="openEsignSite">✒️ Open {{ esignSiteLabel }} ↗</button>
    </template>
    <p v-if="copyStatus" class="status ok">{{ copyStatus }}</p>
  </section>
</template>

<style scoped>
.grp {
  font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--navy); margin: 16px 0 8px; border-bottom: 1px solid #eee; padding-bottom: 4px;
  display: flex; align-items: center; justify-content: space-between;
}
.link {
  background: none; color: var(--gold); border: 0; font-size: 11px; cursor: pointer;
  margin: 0; padding: 0; text-transform: none; letter-spacing: 0;
}
.pull { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 4px 0 10px; }
.pull .hint { margin: 0; flex: 1; min-width: 220px; }
</style>
