<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { loanDocsDefaults, loanDocsHtml, loanDocsDownloadPdf, loanDocsDownloadWord, loanDocsSettlement, loanDocsExtract, loanDocsReadMemo } from './lib/api.js'

// Deal terms/extraction the user already confirmed on the Credit Memo tab,
// plus the App-owned store this tab keeps its own terms in (so they survive
// tab switches and the Closing Binder tab can pull them).
const props = defineProps({
  memoTerms: { type: Object, default: () => ({}) },
  memoExtraction: { type: Object, default: null },
  termsStore: { type: Object, default: null },
})

// --- state -----------------------------------------------------------------
const TERM_DEFAULTS = {
  borrower_name: '', borrower_surname: '',
  borrower_street: '', borrower_city: '',
  borrower_state_abbr: '', borrower_zip: '', borrower_state: '',
  occupation: '', use_of_proceeds: 'Business related expenses',
  loan_amount: null, interest_rate: null, origination_fee_pct: null,
  amortization_type: 'balloon',
  has_insurance_policy: false,
  closing_date: '', maturity_date: '', loan_number: '',
  prepay_min_months: 'two', late_charge_pct: 10, exit_fee_pct: 10,
  default_rate_points: 5,
  no_team_contract: false, upcoming_season_year: '',
  team_name: '', team_street: '', team_city_state_zip: '',
  league: '', contract_title: '', contract_date: '',
  lender_signatory_title: 'CEO',
  account_name: '', bank_name: '', bank_account_no: '', bank_aba: '',
  bank_address_1: '', bank_address_2: '', bank_contact: '', bank_phone: '',
  // Exhibit A override rows + where they came from (workbook / credit memo
  // extraction / Structure tab). Null means the backend computes the schedule.
  repayment_schedule: null, schedule_source: '',
}
const terms = props.termsStore || reactive({ ...TERM_DEFAULTS })
// seed keys the store doesn't have yet (first mount); values already typed
// in a previous visit to this tab are kept
for (const [k, v] of Object.entries(TERM_DEFAULTS)) {
  if (!(k in terms)) terms[k] = v
}

const include = reactive({
  affidavit: true, note: true, lsa: true, guaranty: true,
  settlement: true, ucc: true, letter: true,
})

const DOC_LABELS = {
  affidavit: 'Business Entity Affidavit',
  note: 'Promissory Note',
  lsa: 'Loan and Security Agreement',
  guaranty: 'Guaranty',
  settlement: 'Memo of Settlement',
  ucc: 'UCC Financing Statement',
  letter: 'Payment Direction Letter',
}

// Memo of Settlement deduction rows (entered positive; printed in parens).
const settlementLines = ref([
  { label: 'Lender Origination Fee (Est)', amount: null },
  { label: 'SRC Legal/Closing Costs (Est)', amount: null },
])

// Lines carved out AFTER "To be disbursed to Borrower (Est)" — e.g. DDD
// Insurance. When any exist, the memo closes with a computed "Net to be
// disbursed to Borrower (Est)" row, mirroring the workbook.
const postLines = ref([])

// Exhibit A repayment rows pulled from the workbook's Sheet1, the memo
// extraction, or pushed over from the Structure tab (else the backend computes
// interest-monthly + balloon). These live in the SHARED terms store, not local
// refs, so a structure selected on the Structure tab lands here and survives
// tab switches like every other field on this tab.

// Amortization workbook (.xlsx) upload state
const sheetLoading = ref(false)
const sheetStatus = reactive({ type: '', msg: '' })

// Team & Contract document upload state (player contract, term sheet, ...)
const contractFiles = ref([])
const contractExtracting = ref(false)
const contractStatus = reactive({ type: '', msg: '' })

// Previously generated credit memo upload state (PDF is best)
const memoFiles = ref([])
const memoReading = ref(false)
const memoStatus = reactive({ type: '', msg: '' })

const previewHtml = ref('')
const ready = ref(false)
const genError = ref('')
const pullMsg = ref('')

// US states for spelling out the abbreviation in the LSA text.
const STATE_NAMES = {
  AL: 'Alabama', AK: 'Alaska', AZ: 'Arizona', AR: 'Arkansas', CA: 'California',
  CO: 'Colorado', CT: 'Connecticut', DE: 'Delaware', FL: 'Florida', GA: 'Georgia',
  HI: 'Hawaii', ID: 'Idaho', IL: 'Illinois', IN: 'Indiana', IA: 'Iowa',
  KS: 'Kansas', KY: 'Kentucky', LA: 'Louisiana', ME: 'Maine', MD: 'Maryland',
  MA: 'Massachusetts', MI: 'Michigan', MN: 'Minnesota', MS: 'Mississippi',
  MO: 'Missouri', MT: 'Montana', NE: 'Nebraska', NV: 'Nevada', NH: 'New Hampshire',
  NJ: 'New Jersey', NM: 'New Mexico', NY: 'New York', NC: 'North Carolina',
  ND: 'North Dakota', OH: 'Ohio', OK: 'Oklahoma', OR: 'Oregon', PA: 'Pennsylvania',
  RI: 'Rhode Island', SC: 'South Carolina', SD: 'South Dakota', TN: 'Tennessee',
  TX: 'Texas', UT: 'Utah', VT: 'Vermont', VA: 'Virginia', WA: 'Washington',
  WV: 'West Virginia', WI: 'Wisconsin', WY: 'Wyoming', DC: 'District of Columbia',
}

onMounted(async () => {
  // Prefill the Payment Direction Letter's account block from the backend's
  // .env-configured SRC defaults (never hard-coded in this public repo).
  const d = await loanDocsDefaults()
  for (const [k, v] of Object.entries(d || {})) {
    if (v && k in terms && !terms[k]) terms[k] = v
  }
})

// --- Pull deal info from the Credit Memo tab --------------------------------
const hasMemo = computed(() => {
  const t = props.memoTerms || {}
  return !!(t.name || t.loan)
})

function titleCase(s) {
  return String(s || '').replace(/\w\S*/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase())
}

function pullFromMemo() {
  const t = props.memoTerms || {}
  const ed = props.memoExtraction || {}
  if (t.name) terms.borrower_name = t.name
  // The surname the credit memo's extraction read off the borrower's ID: the
  // UCC-1 files a compound surname whole ("Rivas Delgado"), and the full name
  // alone never says where one begins.
  if (ed.borrower_surname) terms.borrower_surname = ed.borrower_surname
  if (t.team && !terms.no_team_contract) terms.team_name = t.team
  if (t.league) terms.league = t.league
  if (t.loan) terms.loan_amount = t.loan
  if (t.rate) terms.interest_rate = t.rate
  if (t.fee) terms.origination_fee_pct = t.fee
  if (t.fund) terms.closing_date = t.fund
  if (t.mat) terms.maturity_date = t.mat
  if (t.sport) terms.occupation = `Professional ${titleCase(t.sport)} Player`

  // Best-effort split of the memo's one-line address into the UCC-1 cells:
  // "street, city, ST zip" (leaves fields untouched when it doesn't parse).
  const addr = String(t.addr || ed.address || '')
  const parts = addr.split(',').map((s) => s.trim()).filter(Boolean)
  if (parts.length >= 2) {
    terms.borrower_street = parts[0]
    const stZip = parts[parts.length - 1].match(/^([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)$/)
    if (stZip) {
      terms.borrower_state_abbr = stZip[1].toUpperCase()
      terms.borrower_zip = stZip[2]
      terms.borrower_city = parts.slice(1, -1).join(', ') || terms.borrower_city
      terms.borrower_state = STATE_NAMES[terms.borrower_state_abbr] || terms.borrower_state
    } else {
      terms.borrower_city = parts.slice(1).join(', ')
    }
  } else if (addr) {
    terms.borrower_street = addr
  }

  // Settlement deductions from the memo's Uses of Funds (Section VI) —
  // including the lines after "To be disbursed" (additional_costs, e.g. DDD
  // Insurance) so the Memo of Settlement shows the full waterfall.
  const uof = ed.uses_of_funds
  if (uof && Array.isArray(uof.deductions) && uof.deductions.length) {
    settlementLines.value = uof.deductions.map((d) => ({ label: d.label, amount: d.amount }))
    if (Array.isArray(uof.additional_costs) && uof.additional_costs.length) {
      postLines.value = uof.additional_costs.map((d) => ({ label: d.label, amount: d.amount }))
    }
  } else if (t.loan && t.fee) {
    const fee = settlementLines.value.find((l) => /origination/i.test(l.label))
    if (fee && fee.amount == null) fee.amount = Math.round(t.loan * t.fee / 100)
  }

  // Exhibit A repayment rows captured from the deal documents, when present —
  // but a schedule read from the uploaded workbook keeps priority.
  if (terms.schedule_source !== 'workbook') {
    if (Array.isArray(ed.repayment_schedule) && ed.repayment_schedule.length) {
      terms.repayment_schedule = ed.repayment_schedule.map((r) => ({
        date: r.date, interest: r.interest, principal: r.principal, total: r.total,
      }))
      terms.schedule_source = 'credit memo extraction'
    }
  }

  pullMsg.value = '✓ Pulled deal info from the Credit Memo tab — review the fields below'
  ready.value = false
}

// --- Read a previously generated credit memo -----------------------------------
function onMemoFiles(e) {
  const seen = new Set(memoFiles.value.map((f) => f.name + ':' + f.size))
  for (const f of Array.from(e.target.files)) {
    const key = f.name + ':' + f.size
    if (!seen.has(key)) { memoFiles.value.push(f); seen.add(key) }
  }
  e.target.value = ''
}

function removeMemoFile(i) { memoFiles.value.splice(i, 1) }

async function readCreditMemo() {
  if (!memoFiles.value.length) return
  memoReading.value = true
  memoStatus.type = 'info'
  memoStatus.msg = `Reading the credit memo with Claude…`
  try {
    const r = await loanDocsReadMemo(memoFiles.value)
    // Same behavior as the Participation Agreement tab's memo reader: only
    // fill fields that are still blank, never overwrite confirmed values.
    const map = {
      borrower_name: r.borrower_name,
      borrower_street: r.borrower_street,
      borrower_city: r.borrower_city,
      borrower_state_abbr: r.borrower_state_abbr,
      borrower_zip: r.borrower_zip,
      borrower_state: r.borrower_state,
      occupation: r.occupation,
      team_name: terms.no_team_contract ? '' : r.team_name,
      league: r.league,
      loan_amount: r.loan_amount,
      interest_rate: r.interest_rate_pct,
      origination_fee_pct: r.origination_fee_pct,
      maturity_date: r.maturity_date,
      loan_number: r.loan_number,
    }
    let filled = 0
    for (const [k, v] of Object.entries(map)) {
      if (v && !terms[k]) { terms[k] = v; filled++ }
    }
    memoStatus.type = filled ? 'ok' : 'err'
    memoStatus.msg = filled
      ? `✓ Filled ${filled} empty field(s) from the memo` + (r.notes ? ` — ${r.notes}` : '')
      : 'No empty fields to fill (values you typed are never overwritten).' + (r.notes ? ` ${r.notes}` : '')
    ready.value = false
  } catch (err) {
    memoStatus.type = 'err'
    memoStatus.msg = 'Could not read the memo: ' + err.message
  }
  memoReading.value = false
}

// No contract with a Team / employer: the Contract / Borrower's Employer
// definitions and the Payment Direction Letter's addressee switch to "the
// team that signs the Borrower in the upcoming <year> <league>" language, so
// the League and season year are still needed. Prefill the year from the
// closing date when it's blank.
function onNoContractToggle() {
  if (terms.no_team_contract && !terms.upcoming_season_year && terms.closing_date) {
    terms.upcoming_season_year = terms.closing_date.slice(0, 4)
  }
  ready.value = false
}

// --- Team & Contract document upload -------------------------------------------
function onContractFiles(e) {
  // accumulate like the Credit Memo tab, deduped by name+size
  const seen = new Set(contractFiles.value.map((f) => f.name + ':' + f.size))
  for (const f of Array.from(e.target.files)) {
    const key = f.name + ':' + f.size
    if (!seen.has(key)) { contractFiles.value.push(f); seen.add(key) }
  }
  e.target.value = ''
}

function removeContractFile(i) { contractFiles.value.splice(i, 1) }

// Normalize a date string ("March 1, 2025" or ISO) for the date picker.
function toISODate(s) {
  if (!s) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s
  const d = new Date(s)
  if (isNaN(d)) return ''
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

async function extractContract() {
  if (!contractFiles.value.length) return
  contractExtracting.value = true
  contractStatus.type = 'info'
  contractStatus.msg = `Analyzing ${contractFiles.value.length} document(s) with Claude…`
  try {
    const r = await loanDocsExtract(contractFiles.value)
    // The user uploaded the contract specifically for these fields, so
    // extracted values win; fields the extraction left empty are untouched.
    if (r.team_name) terms.team_name = r.team_name
    if (r.team_street) terms.team_street = r.team_street
    if (r.team_city_state_zip) terms.team_city_state_zip = r.team_city_state_zip
    if (r.league) terms.league = r.league
    if (r.contract_title) terms.contract_title = r.contract_title
    if (r.contract_date) terms.contract_date = toISODate(r.contract_date)
    // the player is deal-level info — fill only if still blank
    if (r.player_name && !terms.borrower_name) terms.borrower_name = r.player_name
    if (r.player_surname && !terms.borrower_surname) terms.borrower_surname = r.player_surname
    const got = ['team_name', 'team_street', 'team_city_state_zip', 'league',
                 'contract_title', 'contract_date'].filter((k) => r[k]).length
    contractStatus.type = got ? 'ok' : 'err'
    contractStatus.msg = got
      ? `✓ Filled ${got} field(s)` + (r.notes ? ` — ${r.notes}` : '')
      : 'Nothing usable found in the documents.' + (r.notes ? ` ${r.notes}` : '')
    ready.value = false
  } catch (err) {
    contractStatus.type = 'err'
    contractStatus.msg = 'Extraction failed: ' + err.message
  }
  contractExtracting.value = false
}

// --- amortization workbook upload --------------------------------------------
async function onSheetFile(e) {
  const files = Array.from(e.target.files)
  e.target.value = ''
  if (!files.length) return
  sheetLoading.value = true
  sheetStatus.type = 'info'
  sheetStatus.msg = `Reading ${files[0].name}…`
  try {
    const r = await loanDocsSettlement(files)
    const bits = []
    if (r.lines && r.lines.length) {
      settlementLines.value = r.lines.map((l) => ({ label: l.label, amount: l.amount }))
      bits.push(`${r.lines.length} settlement line(s) from "${r.settlement_sheet}"`)
    }
    if (r.post_lines && r.post_lines.length) {
      postLines.value = r.post_lines.map((l) => ({ label: l.label, amount: l.amount }))
      bits.push(`${r.post_lines.length} line(s) after disbursement`)
    } else if (r.lines && r.lines.length) {
      postLines.value = []
    }
    // the workbook's tab name says how the deal repays
    if (/amortiz/i.test(r.settlement_sheet || '')) {
      terms.amortization_type = 'fully_amortized'
      bits.push('repayment structure set to Fully Amortized')
    } else if (/balloon/i.test(r.settlement_sheet || '')) {
      terms.amortization_type = 'balloon'
      bits.push('repayment structure set to Balloon')
    }
    if (r.gross_loan_amount) {
      if (!terms.loan_amount) terms.loan_amount = r.gross_loan_amount
      else if (Number(terms.loan_amount) !== r.gross_loan_amount) {
        bits.push(`note: sheet's Gross Loan Amount (${fmtMoney(r.gross_loan_amount)}) differs from the Loan amount field (${fmtMoney(Number(terms.loan_amount))})`)
      }
    }
    if (r.schedule && r.schedule.length) {
      terms.repayment_schedule = r.schedule
      terms.schedule_source = 'workbook'
      bits.push(`${r.schedule.length} Exhibit A payment(s) from "${r.schedule_sheet}"`)
    }
    if (r.disbursed_check != null) {
      const ours = (r.gross_loan_amount || 0) - r.lines.reduce((s, l) => s + (l.amount || 0), 0)
      bits.push(Math.abs(ours - r.disbursed_check) < 0.01
        ? `disbursement foots to the sheet (${fmtMoney(r.disbursed_check)})`
        : `⚠ sheet shows ${fmtMoney(r.disbursed_check)} to be disbursed but its lines foot to ${fmtMoney(ours)}`)
      if (r.net_check != null) {
        const ourNet = ours - (r.post_lines || []).reduce((s, l) => s + (l.amount || 0), 0)
        bits.push(Math.abs(ourNet - r.net_check) < 0.01
          ? `net disbursement foots to the sheet (${fmtMoney(r.net_check)})`
          : `⚠ sheet shows ${fmtMoney(r.net_check)} net to be disbursed but its lines foot to ${fmtMoney(ourNet)}`)
      }
    }
    sheetStatus.type = 'ok'
    sheetStatus.msg = '✓ ' + bits.join(' · ')
    ready.value = false
  } catch (err) {
    sheetStatus.type = 'err'
    sheetStatus.msg = 'Could not read the workbook: ' + err.message
  }
  sheetLoading.value = false
}

// --- settlement rows ---------------------------------------------------------
function addLine() { settlementLines.value.push({ label: '', amount: null }) }
function removeLine(i) { settlementLines.value.splice(i, 1) }
function addPostLine() { postLines.value.push({ label: '', amount: null }) }
function removePostLine(i) { postLines.value.splice(i, 1) }

const disbursed = computed(() => {
  const gross = Number(terms.loan_amount) || 0
  const ded = settlementLines.value.reduce((s, l) => s + (Number(l.amount) || 0), 0)
  return gross - ded
})

const netDisbursed = computed(() =>
  disbursed.value - postLines.value.reduce((s, l) => s + (Number(l.amount) || 0), 0))

function fmtMoney(n) {
  if (n == null || isNaN(n)) return '—'
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// --- generate ---------------------------------------------------------------
function buildPayload() {
  return {
    terms: {
      ...terms,
      loan_amount: Number(terms.loan_amount) || null,
      interest_rate: Number(terms.interest_rate) || null,
      origination_fee_pct: Number(terms.origination_fee_pct) || null,
      late_charge_pct: Number(terms.late_charge_pct) || null,
      exit_fee_pct: Number(terms.exit_fee_pct) || null,
      default_rate_points: Number(terms.default_rate_points) || null,
      closing_date: terms.closing_date || null,
      maturity_date: terms.maturity_date || null,
      contract_date: terms.contract_date || null,
      settlement_lines: settlementLines.value
        .filter((l) => l.label || l.amount)
        .map((l) => ({ label: l.label, amount: Number(l.amount) || 0 })),
      settlement_post_lines: postLines.value
        .filter((l) => l.label || l.amount)
        .map((l) => ({ label: l.label, amount: Number(l.amount) || 0 })),
      repayment_schedule: terms.repayment_schedule,
    },
    include: { ...include },
  }
}

const canGenerate = computed(() => !!terms.borrower_name)

async function generate() {
  genError.value = ''
  try {
    const p = buildPayload()
    previewHtml.value = await loanDocsHtml(p.terms, p.include)
    ready.value = true
  } catch (err) {
    genError.value = err.message
  }
}

async function exportPdf() {
  try { const p = buildPayload(); await loanDocsDownloadPdf(p.terms, p.include) }
  catch (err) { genError.value = err.message }
}
async function exportWord() {
  try { const p = buildPayload(); await loanDocsDownloadWord(p.terms, p.include) }
  catch (err) { genError.value = err.message }
}
</script>

<template>
  <div>
    <!-- Step 1: deal info -->
    <section class="card">
      <h2><span class="step">1</span> Deal information</h2>
      <p class="hint">Build the credit memo first, then pull the deal terms straight into these fields.</p>
      <button :disabled="!hasMemo" @click="pullFromMemo">⬇ Pull deal info from Credit Memo</button>
      <p v-if="!hasMemo" class="hint">Nothing to pull yet — fill in the Credit Memo tab (or type the fields below).</p>
      <p v-if="pullMsg" class="status ok">{{ pullMsg }}</p>

      <p class="hint" style="margin-top:10px">…or upload a previously generated credit memo (PDF is best) and read the deal info from it. Only empty fields are filled.</p>
      <input type="file" multiple @change="onMemoFiles" :disabled="memoReading" />
      <ul v-if="memoFiles.length" class="filelist">
        <li v-for="(f, i) in memoFiles" :key="f.name + f.size">
          <span class="fname">{{ f.name }}</span>
          <button type="button" class="rm" @click="removeMemoFile(i)" title="Remove">✕</button>
        </li>
      </ul>
      <button type="button" :disabled="!memoFiles.length || memoReading" @click="readCreditMemo">
        {{ memoReading ? 'Reading…' : 'Read credit memo' }}
      </button>
      <p v-if="memoStatus.msg" :class="['status', memoStatus.type]">{{ memoStatus.msg }}</p>

      <div class="grid">
        <label>Borrower name <input v-model="terms.borrower_name" /></label>
        <label>Surname only (UCC-1) <input v-model="terms.borrower_surname" placeholder="e.g. Rivas Delgado" /></label>
        <label>Occupation <input v-model="terms.occupation" placeholder="Professional Baseball Player" /></label>
        <label>Street address <input v-model="terms.borrower_street" /></label>
        <label>City <input v-model="terms.borrower_city" /></label>
        <label>State (abbrev.) <input v-model="terms.borrower_state_abbr" placeholder="FL" /></label>
        <label>ZIP <input v-model="terms.borrower_zip" /></label>
        <label>State of residence (spelled out) <input v-model="terms.borrower_state" placeholder="Florida" /></label>
        <label>Loan number <input v-model="terms.loan_number" /></label>
        <label>Loan amount ($) <input v-model.number="terms.loan_amount" type="number" /></label>
        <label>Interest rate (% p.a.) <input v-model.number="terms.interest_rate" type="number" step="0.01" /></label>
        <label>Repayment structure
          <select v-model="terms.amortization_type">
            <option value="balloon">Balloon</option>
            <option value="interest_only">Interest Only</option>
            <option value="fully_amortized">Fully Amortized</option>
          </select>
        </label>
        <label>Insurance Policy
          <select v-model="terms.has_insurance_policy">
            <option :value="true">Yes</option>
            <option :value="false">No — requirement waived</option>
          </select>
        </label>
        <label>Origination / commitment fee (%) <input v-model.number="terms.origination_fee_pct" type="number" step="0.01" /></label>
        <label>Use of proceeds <input v-model="terms.use_of_proceeds" /></label>
        <label>Closing date <input v-model="terms.closing_date" type="date" /></label>
        <label>Maturity date <input v-model="terms.maturity_date" type="date" /></label>
      </div>
      <p class="hint">Borrower name is the FULL legal name as the passport or driver’s license spells it — every surname and any suffix (“Mateo Rivas Delgado”). “Surname only” is the ID’s surname field on its own: it fills the UCC-1’s LAST NAME box, which must carry a compound surname whole. Leave it blank and the last word of the name is used (a Jr./III moves to the suffix box either way).</p>

      <h3 class="grp">Note terms</h3>
      <div class="grid">
        <label>Prepayment minimum (months of interest) <input v-model="terms.prepay_min_months" placeholder="two" /></label>
        <label>Late charge (%) <input v-model.number="terms.late_charge_pct" type="number" step="0.01" /></label>
        <label>Exit fee (%) <input v-model.number="terms.exit_fee_pct" type="number" step="0.01" /></label>
        <label>Default rate (points above note rate) <input v-model.number="terms.default_rate_points" type="number" step="0.5" /></label>
      </div>

      <h3 class="grp">Team &amp; contract</h3>
      <label class="chk" style="margin:2px 0 8px">
        <input type="checkbox" v-model="terms.no_team_contract" @change="onNoContractToggle" />
        Athlete does not have a contract with a Team / Employer
      </label>
      <p v-if="terms.no_team_contract" class="hint">The cover page will show “None” for Team / Employer and Contract, and the Contract / Borrower’s Employer definitions and the Payment Direction Letter switch to “the team that signs the Borrower in the upcoming season” language — set the League and the season year below.</p>
      <template v-if="!terms.no_team_contract">
        <p class="hint">Upload the player's contract (or other deal documents) and Claude fills the team, address, league, and contract details below.</p>
        <input type="file" multiple @change="onContractFiles" :disabled="contractExtracting" />
        <ul v-if="contractFiles.length" class="filelist">
          <li v-for="(f, i) in contractFiles" :key="f.name + f.size">
            <span class="fname">{{ f.name }}</span>
            <button type="button" class="rm" @click="removeContractFile(i)" title="Remove">✕</button>
          </li>
        </ul>
        <button type="button" :disabled="!contractFiles.length || contractExtracting" @click="extractContract">
          {{ contractExtracting ? 'Analyzing…' : 'Extract team & contract with Claude' }}
        </button>
        <p v-if="contractStatus.msg" :class="['status', contractStatus.type]">{{ contractStatus.msg }}</p>
      </template>
      <div class="grid">
        <label>Team / Employer <input v-model="terms.team_name" :disabled="terms.no_team_contract" /></label>
        <label>League <input v-model="terms.league" placeholder="MLB" /></label>
        <label v-if="terms.no_team_contract">Upcoming season year <input v-model="terms.upcoming_season_year" placeholder="2026" /></label>
        <label>Team street address <input v-model="terms.team_street" :disabled="terms.no_team_contract" /></label>
        <label>Team city / state / zip <input v-model="terms.team_city_state_zip" :disabled="terms.no_team_contract" /></label>
        <label>Contract title <input v-model="terms.contract_title" :placeholder="(terms.league || 'MLB') + ' Professional Contract'" :disabled="terms.no_team_contract" /></label>
        <label>Contract date <input v-model="terms.contract_date" type="date" :disabled="terms.no_team_contract" /></label>
        <label>Lender signatory title <input v-model="terms.lender_signatory_title" /></label>
      </div>

      <h3 class="grp">Payment direction — receiving account</h3>
      <p class="hint">Prefilled from the server's .env (SRC_BANK_*). Edit here if a deal uses a different account.</p>
      <div class="grid">
        <label>Account name <input v-model="terms.account_name" /></label>
        <label>Bank name <input v-model="terms.bank_name" /></label>
        <label>Account no. <input v-model="terms.bank_account_no" /></label>
        <label>ABA routing no. <input v-model="terms.bank_aba" /></label>
        <label>Bank address (line 1) <input v-model="terms.bank_address_1" /></label>
        <label>Bank address (line 2) <input v-model="terms.bank_address_2" /></label>
        <label>Contact <input v-model="terms.bank_contact" /></label>
        <label>Telephone <input v-model="terms.bank_phone" /></label>
      </div>
    </section>

    <!-- Step 2: settlement + documents -->
    <section class="card">
      <h2><span class="step">2</span> Memo of Settlement &amp; documents</h2>
      <p class="hint">Upload the deal's amortization workbook (a "Balloon" or "Fully Amortized" .xlsx): the fee lines fill the Memo of Settlement, and Sheet1's repayment table becomes the Note's Exhibit A — Loan Repayments by Month.</p>
      <input type="file" accept=".xlsx,.xlsm" @change="onSheetFile" :disabled="sheetLoading" />
      <p v-if="sheetStatus.msg" :class="['status', sheetStatus.type]">{{ sheetStatus.msg }}</p>
      <p v-if="terms.repayment_schedule" class="hint">Exhibit A schedule: <strong>{{ terms.repayment_schedule.length }} payment(s)</strong> from the {{ terms.schedule_source }}.
        <button type="button" class="rm-link" @click="terms.repayment_schedule = null; terms.schedule_source = ''">✕ clear (compute from deal terms instead)</button></p>

      <h3 class="grp">Settlement deductions</h3>
      <p class="hint">Deductions from the gross loan (entered positive; printed in parentheses). "To be disbursed" is always recomputed.</p>
      <table class="lines">
        <tr v-for="(l, i) in settlementLines" :key="i">
          <td><input v-model="l.label" placeholder="e.g. Lender Origination Fee (Est)" /></td>
          <td style="width:160px"><input v-model.number="l.amount" type="number" placeholder="Amount" /></td>
          <td style="width:30px"><button type="button" class="rm" @click="removeLine(i)" title="Remove">✕</button></td>
        </tr>
      </table>
      <button type="button" class="ghost" @click="addLine">+ Add line</button>
      <p class="hint">To be disbursed to Borrower (Est): <strong>{{ fmtMoney(disbursed) }}</strong></p>

      <h3 class="grp">After disbursement</h3>
      <p class="hint">Amounts carved out of the disbursed figure (e.g. DDD Insurance). When any are listed, the Memo of Settlement adds them below "To be disbursed to Borrower (Est)" and closes with a computed "Net to be disbursed to Borrower (Est)" line.</p>
      <table class="lines" v-if="postLines.length">
        <tr v-for="(l, i) in postLines" :key="i">
          <td><input v-model="l.label" placeholder="e.g. DDD Insurance (Est)" /></td>
          <td style="width:160px"><input v-model.number="l.amount" type="number" placeholder="Amount" /></td>
          <td style="width:30px"><button type="button" class="rm" @click="removePostLine(i)" title="Remove">✕</button></td>
        </tr>
      </table>
      <button type="button" class="ghost" @click="addPostLine">+ Add line</button>
      <p class="hint" v-if="postLines.length">Net to be disbursed to Borrower (Est): <strong>{{ fmtMoney(netDisbursed) }}</strong></p>

      <h3 class="grp">Documents to include</h3>
      <div class="checks">
        <label v-for="(label, key) in DOC_LABELS" :key="key" class="chk">
          <input type="checkbox" v-model="include[key]" /> {{ label }}
        </label>
      </div>
    </section>

    <!-- Step 3: generate -->
    <section class="card">
      <h2><span class="step">3</span> Generate loan documents</h2>
      <p v-if="genError" class="status err">⚠ {{ genError }}</p>
      <button :disabled="!canGenerate" @click="generate">Generate loan documents</button>
      <template v-if="ready">
        <button class="ghost" @click="exportPdf">📕 PDF</button>
        <button class="ghost" @click="exportWord">📄 Word</button>
      </template>
    </section>

    <section v-if="ready" class="card">
      <h2>Preview</h2>
      <iframe :srcdoc="previewHtml" class="preview" title="Loan documents preview"></iframe>
    </section>
  </div>
</template>

<style scoped>
.grp { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--navy); margin: 16px 0 8px; }
table.lines { width: 100%; border-collapse: collapse; }
table.lines td { padding: 3px 4px 3px 0; }
table.lines input { width: 100%; }
.checks { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; }
.chk { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; color: #333; display: flex; }
.chk input { width: auto; }
.rm-link { background: transparent; color: #b00020; border: 0; padding: 0 2px; margin: 0 0 0 6px; font-size: 12px; cursor: pointer; }
@media (max-width: 640px) { .checks { grid-template-columns: 1fr; } }
</style>
