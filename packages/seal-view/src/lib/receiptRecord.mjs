// Checks a record/run/v1 receipt (a DSSE envelope carrying an in-toto
// Statement) or its public card, in the browser or in Node, with WebCrypto.
// The same file as openadapt-web lib/receiptRecord.mjs, extended to the full
// record/run/v1 schema (confirmations, approval, payload and report.canonical
// subjects, evidence lines). A port of verify_receipt in OpenAdapt packages/types/openadapt_types/receipt.py;
// the shared vectors (tests/vectors/record-run-v1, copied from
// packages/types/tests/vectors/record-run-v1) pin both to the same answers.
// Fails closed: an unknown key, role or schema, any bad signature, a demo key
// next to a real one, or embedded content is a refusal. Trust comes only from
// the pinned registry; the outcome is copied, never upgraded.
import { base64Bytes, canonicalJson, duplicateKey, sha256Hex } from './localSeal.mjs'

export const STATEMENT_TYPE = 'https://in-toto.io/Statement/v1'
export const PREDICATE_TYPE = 'https://openadapt.ai/record/run/v1'
export const PAYLOAD_TYPE = 'application/vnd.in-toto+json'
export const CARD_SCHEMA = 'openadapt.receipt-card/v1'
export const OPENINGS_SCHEMA = 'openadapt.receipt-openings/v1'
export const REGISTRY_SCHEMA = 'openadapt.receipt-keys/v1'
export const PUBLIC_DISCLOSURE_VERSION = 'openadapt.public-disclosure/v1'
const COMMITMENT_DOMAIN = 'openadapt.receipt-commitment/v1'
const MAX_LINES = 256
const MAX_COUNT = 2 ** 53 - 1
const MAX_PAYLOAD_BYTES = 65536

const ROLES = ['this_computer', 'customer_key', 'openadapt_cloud', 'public_log', 'demo']
const ROLE_ORDER = ['this_computer', 'customer_key', 'openadapt_cloud', 'public_log']
const SIGNER_LINES = {
    this_computer: 'Signed by this computer',
    customer_key: "Signed by {org}'s key",
    openadapt_cloud: 'Countersigned by OpenAdapt Cloud',
    public_log: 'Logged publicly',
    demo: 'Demo key: checks only in this demo',
}
const REFUSAL_LINES = {
    unknown_signer: 'Not verified: unknown signer',
    unknown_role: 'Not verified: unknown signer role',
    unknown_schema: 'Not verified: unknown receipt format',
    unsigned: 'Not verified: no signature',
    revoked_key: 'Not verified: the signing key was revoked',
    bad_signature: 'Not verified: the receipt was changed after it was signed',
    mixed_demo: 'Not verified: a demo key signed alongside a real one',
    embedded_content: 'Not verified: the receipt holds embedded content',
    opening_mismatch: "Not verified: a disclosed value doesn't match the receipt",
    disclosure_not_allowed: 'Not verified: the card reveals a field it may not',
    inconsistent: 'Not verified: the receipt contradicts itself',
    malformed: 'Not verified: malformed receipt',
    verifier_unavailable: "Not verified: this browser can't check Ed25519 signatures",
}
export const refusalLine = (reason) => REFUSAL_LINES[reason] || 'Not verified'

const SHA256_HEX = /^[a-f0-9]{64}$/
const SALT_HEX = /^[a-f0-9]{64}$/
const HOUR = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z$/
const ISO_SECONDS = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/
const PROGRAM_ID = /^prog-[a-f0-9]{16}$/
const REASON = /^[a-z][a-z0-9_]{0,47}$/
const ORG = /^[A-Za-z0-9 ._-]{1,48}$/
const B64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/

const DISCLOSURE_FIELDS = {
    program_name: /^[A-Za-z0-9 ._-]{1,48}$/,
    flow_version: /^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}$/,
    github_login: /^[A-Za-z0-9-]{1,39}$/,
    run_id: /^[A-Za-z0-9._:-]{1,128}$/,
}
// openadapt.public-disclosure/v1: what a public card may reveal.
const PUBLIC_DISCLOSURE = { [PUBLIC_DISCLOSURE_VERSION]: ['program_name', 'flow_version', 'github_login'] }

const OUTCOMES = ['VERIFIED', 'STOPPED', 'UNKNOWN', 'FAILED']
const SUBJECT_NAMES = ['report.json', 'report.canonical', 'bundle', 'evidence', 'payload']
const MEDIA_TYPES = ['application/json', 'application/cbor', 'text/plain', 'text/csv', 'application/pdf', 'application/octet-stream']
const EVIDENCE_KINDS = ['tx_receipt', 'write_receipt']
const SYSTEM_KINDS = ['ehr', 'payer_portal', 'bank', 'erp', 'crm', 'government_portal', 'email', 'other']
const READ_METHODS = ['portal_read_back', 'api', 'acknowledgment_message']
const INDEPENDENT_READS = ['portal_read_back', 'api']
const STATES = ['sent', 'acknowledged', 'recorded_as_intended', 'recorded_differently']
const C_STRENGTHS = ['read_back', 'acknowledgment', 'none']
const APPROVAL_MODES = ['each_item', 'problems_only', 'not_gated']
const APPROVER_KINDS = ['person', 'agent', 'none']
const REASONS_FOR_STATE = {
    sent: ['rejected_by_counterparty', 'ambiguous_key', 'receiver_unreachable', 'not_found', 'no_answer_by_deadline'],
    acknowledged: ['ambiguous_key', 'not_found', 'no_answer_by_deadline'],
    recorded_as_intended: ['conflicting_report'],
    recorded_differently: ['conflicting_report'],
}
const CONFIRMATION_KEY = /^(?:counterparty|observed_at|reason|field\.[0-9]{1,2})$/
const APPROVAL_KEY = /^(?:principal|scope|approved_at)$/
const CONFIRMATION_PATH = /^confirmations\.([0-9]{1,2})\.(counterparty|observed_at|reason|field\.[0-9]{1,2})$/
const APPROVAL_PATH = /^approval\.(principal|scope|approved_at)$/
const OPENED_VALUES = {
    counterparty: /^(?:ehr|payer_portal|bank|erp|crm|government_portal|email|other):cp-[a-f0-9]{16}$/,
    observed_at: ISO_SECONDS,
    reason: /^(?:rejected_by_counterparty|conflicting_report|ambiguous_key|receiver_unreachable|not_found|no_answer_by_deadline)$/,
    field: /^[a-z][a-z0-9_]{0,31}:[a-f0-9]{64}(?::differs)?$/,
    principal: /^(?:person|agent):pr-[a-f0-9]{16}$/,
    scope: SHA256_HEX,
    approved_at: ISO_SECONDS,
}
const TARGET_KINDS = ['gui', 'api']
const OPERATIONS = ['write', 'irreversible_write']
const CHECK_RESULTS = ['confirmed', 'refuted', 'not_checked']
const STRENGTHS = ['read_back', 'same_screen', 'none']
const NOT_CHECKED = ['effect_not_declared', 'approved_unverified', 'field_values', 'postconditions', 'uncertain']
const EVIDENCE_SCOPE_SCHEMA = 'openadapt.evidence-scope/v1'
const CHECK_LEVELS = ['unchecked', 'same_screen', 'reopened', 'separate_session', 'system_of_record', 'counterparty_confirmed']
const OUTCOME_WORDS = ['VERIFIED', 'SHOWN', 'CONFIRMED', 'ATTESTED', 'RECORDED']
const BASIS_LEVEL = {
    separate_session: 'separate_session',
    system_api: 'system_of_record',
    activity_log: 'system_of_record',
    db_frame: 'system_of_record',
    owned_connection: 'system_of_record',
}
const WIRE_CHANNELS = ['none', 'browser_network', 'http_proxy', 'api_client', 'db_connection']
const WIRE_PROTOCOLS = ['http', 'graphql', 'fhir', 'sql', 'websocket', 'grpc', 'smtp']
const NOT_COVERED = ['server_side_async_effects', 'outbound_effects', 'changes_after_read_back', 'fields_not_read', 'other_records', 'other_actors', 'read_back_time']
const ALWAYS_NOT_COVERED = ['server_side_async_effects', 'outbound_effects']
const MAX_RECHECK_S = 30 * 24 * 3600
const PROVENANCE_LABEL = /^(?!.*:\/\/)[A-Za-z0-9][A-Za-z0-9._:@+/ -]{0,127}$/
const EVIDENCE_SCOPE_OUTCOMES = {
    VERIFIED: ['VERIFIED'],
    CONFIRMED: ['VERIFIED'],
    SHOWN: ['UNKNOWN', 'STOPPED', 'FAILED'],
    ATTESTED: ['UNKNOWN', 'STOPPED', 'FAILED'],
    RECORDED: ['UNKNOWN', 'STOPPED', 'FAILED'],
}

class Refusal extends Error {
    constructor(reason, detail = '') {
        super(detail || reason)
        this.reason = reason
    }
}
const refuse = (reason, detail = '') => ({ ok: false, reason, detail, signerLines: [refusalLine(reason)], outcome: null, demo: false, disclosed: {} })

// -- embedded content: screenshots, base64 blobs, URLs, long text ------------
const DATA_URI = /data:[a-z]+\//i
const URL_RE = /[a-z][a-z0-9+.-]*:\/\//i
const BASE64_RUN = /[A-Za-z0-9+/_-]{96,}={0,2}/
const IMAGE_MAGIC = ['iVBORw0KGgo', '/9j/', 'R0lGOD', 'UklGR', 'PHN2Zy']
function embedded(text) {
    const lower = text.toLowerCase()
    // Python counts code points; so does the spread.
    return [...text].length > 256 || DATA_URI.test(text) || URL_RE.test(text) || BASE64_RUN.test(text) || IMAGE_MAGIC.some((m) => text.startsWith(m)) || lower.includes('<svg') || lower.includes('<img')
}
export function findEmbeddedContent(value, path = '$') {
    if (typeof value === 'string') return embedded(value) ? path : null
    if (Array.isArray(value)) {
        for (let i = 0; i < value.length; i += 1) {
            const found = findEmbeddedContent(value[i], `${path}[${i}]`)
            if (found) return found
        }
        return null
    }
    if (value && typeof value === 'object') {
        for (const [k, v] of Object.entries(value)) {
            if (embedded(k)) return `${path}.[key]`
            const found = findEmbeddedContent(v, `${path}.${k}`)
            if (found) return found
        }
    }
    return null
}

// -- the closed record/run/v1 schema ----------------------------------------
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
function closed(v, fields, optional = [], where = '') {
    if (!isObj(v)) throw new Refusal('malformed', `${where} is an object`)
    for (const k of Object.keys(v)) if (!fields.includes(k) && !optional.includes(k)) throw new Refusal('malformed', `${where}.${k} is not a field`)
    for (const k of fields) if (!(k in v)) throw new Refusal('malformed', `${where}.${k} is missing`)
}
function int(v, lo, hi, where) {
    if (!Number.isInteger(v) || v < lo || v > hi) throw new Refusal('malformed', `${where} is an integer from ${lo} to ${hi}`)
}
function str(v, test, where) {
    const ok = typeof v === 'string' && (Array.isArray(test) ? test.includes(v) : test.test(v))
    if (!ok) throw new Refusal('malformed', `${where} is not valid`)
}
function checkCount(v, where) {
    closed(v, ['required', 'confirmed'], [], where)
    int(v.required, 0, MAX_COUNT, `${where}.required`)
    int(v.confirmed, 0, MAX_COUNT, `${where}.confirmed`)
}
// Validates the statement and returns it in its canonical wire form (the
// optional reason_code always present, as null when absent).
function validateStatement(s) {
    closed(s, ['_type', 'subject', 'predicateType', 'predicate'], [], '$')
    if (!Array.isArray(s.subject) || s.subject.length < 1 || s.subject.length > 4) throw new Refusal('malformed', 'subject has 1 to 4 entries')
    for (const [i, sub] of s.subject.entries()) {
        closed(sub, ['name', 'digest'], ['media_type'], `subject[${i}]`)
        str(sub.name, SUBJECT_NAMES, `subject[${i}].name`)
        if ('media_type' in sub) str(sub.media_type, MEDIA_TYPES, `subject[${i}].media_type`)
        closed(sub.digest, ['sha256'], [], `subject[${i}].digest`)
        str(sub.digest.sha256, SHA256_HEX, `subject[${i}].digest.sha256`)
    }
    const p = s.predicate
    closed(p, ['program', 'started_hour', 'outcome', 'steps', 'lines', 'checks', 'model_calls', 'disclosure'], ['reason_code', 'confirmations', 'approval', 'evidence_scope'], 'predicate')
    closed(p.program, ['id', 'version_sha256'], [], 'predicate.program')
    str(p.program.id, PROGRAM_ID, 'predicate.program.id')
    str(p.program.version_sha256, SHA256_HEX, 'predicate.program.version_sha256')
    str(p.started_hour, HOUR, 'predicate.started_hour')
    str(p.outcome, OUTCOMES, 'predicate.outcome')
    if (p.reason_code != null) str(p.reason_code, REASON, 'predicate.reason_code')
    closed(p.steps, ['total', 'ok'], [], 'predicate.steps')
    int(p.steps.total, 0, MAX_COUNT, 'predicate.steps.total')
    int(p.steps.ok, 0, MAX_COUNT, 'predicate.steps.ok')
    if (!Array.isArray(p.lines) || p.lines.length > MAX_LINES) throw new Refusal('malformed', 'predicate.lines')
    for (const [i, line] of p.lines.entries()) {
        const w = `predicate.lines[${i}]`
        closed(line, ['step', 'target_kind', 'operation', 'check', 'strength', 'not_checked'], ['evidence'], w)
        if ('evidence' in line) {
            closed(line.evidence, ['kind', 'sha256'], [], `${w}.evidence`)
            str(line.evidence.kind, EVIDENCE_KINDS, `${w}.evidence.kind`)
            str(line.evidence.sha256, SHA256_HEX, `${w}.evidence.sha256`)
        }
        int(line.step, 0, 10000, `${w}.step`)
        str(line.target_kind, TARGET_KINDS, `${w}.target_kind`)
        str(line.operation, OPERATIONS, `${w}.operation`)
        str(line.check, CHECK_RESULTS, `${w}.check`)
        str(line.strength, STRENGTHS, `${w}.strength`)
        if (!Array.isArray(line.not_checked) || line.not_checked.length > 5) throw new Refusal('malformed', `${w}.not_checked`)
        line.not_checked.forEach((c, j) => str(c, NOT_CHECKED, `${w}.not_checked[${j}]`))
    }
    const c = p.checks
    closed(c, ['identity', 'postconditions', 'effects', 'authorization', 'lines_confirmed', 'lines_refuted', 'lines_not_checked'], [], 'predicate.checks')
    for (const k of ['identity', 'postconditions', 'effects', 'authorization']) checkCount(c[k], `predicate.checks.${k}`)
    for (const k of ['lines_confirmed', 'lines_refuted', 'lines_not_checked']) int(c[k], 0, MAX_LINES, `predicate.checks.${k}`)
    int(p.model_calls, 0, MAX_COUNT, 'predicate.model_calls')
    closed(p.disclosure, ['version', 'commitments'], [], 'predicate.disclosure')
    str(p.disclosure.version, [PUBLIC_DISCLOSURE_VERSION], 'predicate.disclosure.version')
    if (!isObj(p.disclosure.commitments) || Object.keys(p.disclosure.commitments).length > 16) throw new Refusal('malformed', 'predicate.disclosure.commitments')
    for (const v of Object.values(p.disclosure.commitments)) if (typeof v !== 'string') throw new Refusal('malformed', 'a commitment is a string')
    if ('confirmations' in p) {
        if (!Array.isArray(p.confirmations) || p.confirmations.length > 64) throw new Refusal('malformed', 'predicate.confirmations')
        for (const [i, c] of p.confirmations.entries()) {
            const w = `predicate.confirmations[${i}]`
            closed(c, ['kind', 'system_kind', 'read_method', 'state', 'strength', 'differing_count', 'commitments'], ['for_step'], w)
            str(c.kind, ['counterparty_confirmation'], `${w}.kind`)
            if (c.for_step != null) int(c.for_step, 0, 10000, `${w}.for_step`)
            str(c.system_kind, SYSTEM_KINDS, `${w}.system_kind`)
            str(c.read_method, READ_METHODS, `${w}.read_method`)
            str(c.state, STATES, `${w}.state`)
            str(c.strength, C_STRENGTHS, `${w}.strength`)
            int(c.differing_count, 0, 64, `${w}.differing_count`)
            if (!isObj(c.commitments) || Object.keys(c.commitments).length < 2 || Object.keys(c.commitments).length > 34) throw new Refusal('malformed', `${w}.commitments`)
        }
    }
    if ('approval' in p) {
        closed(p.approval, ['mode', 'approver_kind', 'commitments'], [], 'predicate.approval')
        str(p.approval.mode, APPROVAL_MODES, 'predicate.approval.mode')
        str(p.approval.approver_kind, APPROVER_KINDS, 'predicate.approval.approver_kind')
        if (!isObj(p.approval.commitments) || Object.keys(p.approval.commitments).length > 3) throw new Refusal('malformed', 'predicate.approval.commitments')
    }
    // Python's canonical round trip refuses a null block, a null optional field
    // and a missing wire.protocols_parsed, so present means validated here too.
    if ('evidence_scope' in p) {
        const es = p.evidence_scope
        closed(es, ['schema', 'check_level', 'word', 'wire', 'not_covered'], ['basis', 'read_back_at', 'recheck_window_s', 'agent_used', 'verifier_adapter'], 'predicate.evidence_scope')
        str(es.schema, [EVIDENCE_SCOPE_SCHEMA], 'predicate.evidence_scope.schema')
        str(es.check_level, CHECK_LEVELS, 'predicate.evidence_scope.check_level')
        str(es.word, OUTCOME_WORDS, 'predicate.evidence_scope.word')
        if ('basis' in es) str(es.basis, Object.keys(BASIS_LEVEL), 'predicate.evidence_scope.basis')
        closed(es.wire, ['channel', 'protocols_parsed'], [], 'predicate.evidence_scope.wire')
        str(es.wire.channel, WIRE_CHANNELS, 'predicate.evidence_scope.wire.channel')
        const protos = es.wire.protocols_parsed
        if (!Array.isArray(protos) || protos.length > 7) throw new Refusal('malformed', 'predicate.evidence_scope.wire.protocols_parsed')
        protos.forEach((pr, j) => str(pr, WIRE_PROTOCOLS, `predicate.evidence_scope.wire.protocols_parsed[${j}]`))
        if (new Set(protos).size !== protos.length) throw new Refusal('malformed', 'wire.protocols_parsed repeats a protocol')
        if (es.wire.channel === 'none' && protos.length) throw new Refusal('malformed', 'a run that watched no wire parsed no protocol')
        if ('read_back_at' in es) str(es.read_back_at, ISO_SECONDS, 'predicate.evidence_scope.read_back_at')
        if ('recheck_window_s' in es) int(es.recheck_window_s, 0, MAX_RECHECK_S, 'predicate.evidence_scope.recheck_window_s')
        if ('agent_used' in es) str(es.agent_used, PROVENANCE_LABEL, 'predicate.evidence_scope.agent_used')
        if ('verifier_adapter' in es) str(es.verifier_adapter, PROVENANCE_LABEL, 'predicate.evidence_scope.verifier_adapter')
        if (!Array.isArray(es.not_covered) || es.not_covered.length < 2 || es.not_covered.length > 7) throw new Refusal('malformed', 'predicate.evidence_scope.not_covered')
        es.not_covered.forEach((nc, j) => str(nc, NOT_COVERED, `predicate.evidence_scope.not_covered[${j}]`))
        if (new Set(es.not_covered).size !== es.not_covered.length) throw new Refusal('malformed', 'not_covered repeats a code')
        if (!ALWAYS_NOT_COVERED.every((k) => es.not_covered.includes(k))) throw new Refusal('malformed', 'not_covered must list server_side_async_effects and outbound_effects')
        if (es.basis != null && BASIS_LEVEL[es.basis] !== es.check_level) throw new Refusal('malformed', `basis ${es.basis} does not reach ${es.check_level}`)
        if (es.word === 'VERIFIED') {
            if (!['separate_session', 'system_of_record'].includes(es.check_level) || es.basis == null) throw new Refusal('malformed', 'VERIFIED needs level 3 or 4 and a basis')
            if (es.read_back_at == null && !es.not_covered.includes('read_back_time')) throw new Refusal('malformed', 'VERIFIED needs read_back_at, or says it has none')
        }
        if (es.word === 'SHOWN' && !['same_screen', 'reopened'].includes(es.check_level)) throw new Refusal('malformed', 'SHOWN is a level 1 or 2 re-read of the acting screen')
        if (es.word === 'CONFIRMED' && es.check_level !== 'counterparty_confirmed') throw new Refusal('malformed', 'CONFIRMED is level 5, the counterparty acknowledgment')
        if (es.read_back_at != null && es.not_covered.includes('read_back_time')) throw new Refusal('malformed', 'a block with read_back_at covers the read-back time')
        if (es.recheck_window_s != null && es.read_back_at == null) throw new Refusal('malformed', 'a recheck window needs read_back_at')
    }
    return { ...s, predicate: { ...p, reason_code: p.reason_code ?? null } }
}

// A closed statement that contradicts itself is refused, never repaired.
function inconsistency(s) {
    const p = s.predicate
    const names = s.subject.map((x) => x.name)
    if (new Set(names).size !== names.length || !(names.includes('report.json') || names.includes('report.canonical'))) return 'subject names repeat, or the report is missing'
    for (const x of s.subject) if ((x.name === 'payload') !== ('media_type' in x)) return 'only the payload subject has a media type, and it always has one'
    if (p.approval) {
        const a = p.approval
        for (const [k, v] of Object.entries(a.commitments)) if (!APPROVAL_KEY.test(k) || !SHA256_HEX.test(v)) return `approval commitment ${k} is not a known field`
        if (a.mode === 'not_gated' && a.approver_kind !== 'none') return 'a not_gated run names no approver'
        if (a.approver_kind === 'none') {
            if (Object.keys(a.commitments).length) return 'an approval with no approver commits to nothing'
        } else if (!('principal' in a.commitments && 'scope' in a.commitments)) return "an approval names its approver's principal and scope"
    }
    for (const [path, value] of Object.entries(p.disclosure.commitments)) if (!DISCLOSURE_FIELDS[path] || !SHA256_HEX.test(value)) return `disclosure path ${path} is not a known field`
    if (p.steps.ok > p.steps.total) return 'steps.ok is more than steps.total'
    for (const k of ['identity', 'postconditions', 'effects', 'authorization']) if (p.checks[k].confirmed > p.checks[k].required) return `checks.${k} confirms more than it requires`
    const tally = { confirmed: 0, refuted: 0, not_checked: 0 }
    for (const line of p.lines) {
        tally[line.check] += 1
        if (line.check === 'confirmed' && line.strength === 'none') return 'a confirmed line has no strength'
        if (new Set(line.not_checked).size !== line.not_checked.length) return 'a line repeats a not_checked code'
    }
    if (p.checks.lines_confirmed !== tally.confirmed || p.checks.lines_refuted !== tally.refuted || p.checks.lines_not_checked !== tally.not_checked) return 'check counts disagree with the lines'
    if (p.outcome === 'VERIFIED' && tally.refuted) return 'a VERIFIED receipt has a refuted line'
    if (p.evidence_scope && !EVIDENCE_SCOPE_OUTCOMES[p.evidence_scope.word].includes(p.outcome)) return "the evidence scope's word disagrees with the outcome"
    const steps = p.lines.map((l) => l.step)
    for (let i = 1; i < steps.length; i += 1) if (steps[i] <= steps[i - 1]) return 'lines are not in step order, or repeat a step'
    for (const c of p.confirmations || []) {
        if (c.for_step != null && !steps.includes(c.for_step)) return 'a confirmation names a step with no line'
        if (c.state === 'sent' && c.strength !== 'none') return 'a sent handoff has no confirmation strength'
        if (c.state === 'acknowledged' && c.strength !== 'acknowledgment') return 'an acknowledgment has acknowledgment strength'
        if (c.state.startsWith('recorded_') && (!INDEPENDENT_READS.includes(c.read_method) || c.strength !== 'read_back')) return 'only a portal or API read-back can say how the record was recorded'
        if ((c.state === 'recorded_differently') !== (c.differing_count > 0)) return 'differing_count disagrees with the state'
        if (!('counterparty' in c.commitments && 'observed_at' in c.commitments)) return 'a confirmation commits to its counterparty and observed_at'
        for (const [k, v] of Object.entries(c.commitments)) if (!CONFIRMATION_KEY.test(k) || !SHA256_HEX.test(v)) return `confirmation commitment ${k} is not a known field`
        const fields = Object.keys(c.commitments).filter((k) => k.startsWith('field.')).length
        if (c.differing_count > fields) return 'more differing fields than observed fields'
    }
    return null
}

// -- commitments ---------------------------------------------------------------
const utf8 = (s) => new TextEncoder().encode(s)
function concat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
    let at = 0
    for (const p of parts) {
        out.set(p, at)
        at += p.length
    }
    return out
}
const hexBytes = (h) => Uint8Array.from(h.match(/../g), (b) => parseInt(b, 16))
export async function commitment(path, saltHex, value) {
    const zero = new Uint8Array([0])
    return sha256Hex(concat(utf8(COMMITMENT_DOMAIN), zero, utf8(path), zero, hexBytes(saltHex), zero, utf8(canonicalJson(value))))
}
function valueProblem(path, value) {
    const m = CONFIRMATION_PATH.exec(path) || APPROVAL_PATH.exec(path)
    if (m) {
        const kind = (m[2] || m[1]).split('.')[0]
        return typeof value === 'string' && OPENED_VALUES[kind].test(value) ? null : `${path} does not match its field's pattern`
    }
    const pattern = DISCLOSURE_FIELDS[path]
    if (!pattern) return `${path} is not a disclosure field`
    if (typeof value !== 'string') return `${path} is not a string`
    if (embedded(value)) return `${path} holds embedded content`
    if (!pattern.test(value)) return `${path} does not match its field's pattern`
    return null
}
async function checkOpenings(openings, commitments, allowed) {
    if (!isObj(openings)) throw new Refusal('malformed', 'openings are an object')
    const found = findEmbeddedContent(openings)
    if (found) throw new Refusal('embedded_content', found)
    const disclosed = {}
    for (const [path, opening] of Object.entries(openings)) {
        if (allowed && !allowed.includes(path)) throw new Refusal('disclosure_not_allowed', path)
        if (!isObj(opening) || Object.keys(opening).length !== 2 || !('salt' in opening) || !('value' in opening)) throw new Refusal('malformed', `the opening for ${path}`)
        const problem = valueProblem(path, opening.value)
        if (problem) throw new Refusal('malformed', problem)
        if (!(typeof opening.salt === 'string' && SALT_HEX.test(opening.salt))) throw new Refusal('malformed', `the salt for ${path}`)
        if (commitments[path] !== (await commitment(path, opening.salt, opening.value))) throw new Refusal('opening_mismatch', path)
        disclosed[path] = opening.value
    }
    return disclosed
}

// -- the pinned registry ---------------------------------------------------------
/** A pinned openadapt.receipt-keys/v1 registry. Every problem refuses it whole. */
export async function parseRegistry(data) {
    if (!isObj(data) || data.schema !== REGISTRY_SCHEMA) throw new Refusal('unknown_schema', 'not an openadapt.receipt-keys/v1 registry')
    if (!Array.isArray(data.keys)) throw new Refusal('malformed', 'a registry has a keys list')
    const keys = []
    for (const e of data.keys) {
        if (!isObj(e)) throw new Refusal('malformed', 'a registry entry is an object')
        if (Object.keys(e).some((k) => !['key_id', 'public_key', 'role', 'status', 'revoked_at', 'org'].includes(k))) throw new Refusal('malformed', 'a registry entry has an unknown field')
        if (!ROLES.includes(e.role)) throw new Refusal('unknown_role', `unknown signer role ${e.role}`)
        if (typeof e.public_key !== 'string' || !B64.test(e.public_key)) throw new Refusal('malformed', 'a public key is base64')
        const raw = base64Bytes(e.public_key)
        if (raw.length !== 32 || e.key_id !== `seal-ed25519-${(await sha256Hex(raw)).slice(0, 16)}`) throw new Refusal('malformed', "a key id doesn't match its public key")
        const status = e.status ?? 'active'
        if (!['active', 'revoked'].includes(status) || (status === 'revoked' && !(typeof e.revoked_at === 'string' && ISO_SECONDS.test(e.revoked_at)))) throw new Refusal('malformed', 'a key is active, or revoked with revoked_at')
        if (e.role === 'customer_key' && !(typeof e.org === 'string' && ORG.test(e.org))) throw new Refusal('malformed', 'a customer key names its org')
        keys.push({ keyId: e.key_id, raw, role: e.role, status, revokedAt: status === 'revoked' ? e.revoked_at : null, org: e.role === 'customer_key' ? e.org : null })
    }
    if (new Set(keys.map((k) => k.keyId)).size !== keys.length) throw new Refusal('malformed', 'a registry lists one key id twice')
    return keys
}

// DSSE's pre-authentication encoding of the raw payload bytes, used as-is.
export function pae(payloadType, payload) {
    const type = utf8(payloadType)
    return concat(utf8(`DSSEv1 ${type.length} `), type, utf8(` ${payload.length} `), payload)
}

export const isReceiptEnvelope = (v) => isObj(v) && 'payloadType' in v && 'payload' in v
export const isReceiptCard = (v) => isObj(v) && v.schema === CARD_SCHEMA

function parseStrict(text) {
    if (duplicateKey(text) !== null) throw new Refusal('malformed', 'duplicate key')
    try {
        return JSON.parse(text)
    } catch {
        throw new Refusal('malformed', 'the payload is not JSON')
    }
}

async function verifySignature(raw, sig, data) {
    let key
    try {
        key = await globalThis.crypto.subtle.importKey('raw', raw, { name: 'Ed25519' }, false, ['verify'])
    } catch {
        throw new Refusal('verifier_unavailable')
    }
    return globalThis.crypto.subtle.verify({ name: 'Ed25519' }, key, sig, data)
}

/**
 * Verify a receipt (document: the DSSE envelope, receipt.json) or its public
 * card (openadapt.receipt-card/v1) against a pinned registry (the raw
 * openadapt.receipt-keys/v1 JSON). openings: the private sidecar, for an
 * envelope only. Returns { ok, reason, signerLines, outcome, demo, disclosed }.
 */
export async function verifyReceipt(document, registry, { openings = null } = {}) {
    try {
        const keys = await parseRegistry(registry)
        let cardOpenings = null
        let allowed = null
        let envelope = document
        if (isReceiptCard(document)) {
            const k = Object.keys(document).sort().join(',')
            if (k !== 'disclosure_version,envelope,openings,schema') throw new Refusal('malformed', 'a card has exactly four fields')
            allowed = PUBLIC_DISCLOSURE[document.disclosure_version]
            if (!allowed) throw new Refusal('unknown_schema', 'unknown public disclosure version')
            cardOpenings = document.openings
            if (!isObj(cardOpenings)) throw new Refusal('malformed', "a card's openings are an object")
            envelope = document.envelope
            if (openings != null) throw new Refusal('malformed', 'a card carries its own openings')
        }
        if (!isObj(envelope) || Object.keys(envelope).sort().join(',') !== 'payload,payloadType,signatures') throw new Refusal('malformed', 'a DSSE envelope has payloadType, payload, signatures')
        if (envelope.payloadType !== PAYLOAD_TYPE) throw new Refusal('unknown_schema', 'the payload is not an in-toto statement')
        if (typeof envelope.payload !== 'string' || !B64.test(envelope.payload)) throw new Refusal('malformed', 'the payload is not base64')
        const payload = base64Bytes(envelope.payload)
        if (payload.length > MAX_PAYLOAD_BYTES) throw new Refusal('malformed', 'the payload is too large')
        const signatures = envelope.signatures
        if (!Array.isArray(signatures) || !signatures.length) throw new Refusal('unsigned')
        const byId = new Map(keys.map((k) => [k.keyId, k]))
        const signed = pae(PAYLOAD_TYPE, payload)
        const checks = []
        const seen = new Set()
        for (const sig of signatures) {
            if (!isObj(sig) || Object.keys(sig).sort().join(',') !== 'keyid,sig') throw new Refusal('malformed', 'a signature has keyid and sig')
            const kid = sig.keyid
            if (typeof kid !== 'string' || seen.has(kid)) throw new Refusal('malformed', "a signature's keyid is missing or repeated")
            seen.add(kid)
            const key = byId.get(kid)
            if (!key) throw new Refusal('unknown_signer', `${kid} is not in the pinned registry`)
            if (typeof sig.sig !== 'string' || !B64.test(sig.sig)) throw new Refusal('bad_signature', `${kid}'s signature`)
            if (!(await verifySignature(key.raw, base64Bytes(sig.sig), signed))) throw new Refusal('bad_signature', `${kid}'s signature`)
            if (key.status === 'revoked') throw new Refusal('revoked_key', `${kid} was revoked at ${key.revokedAt}`)
            checks.push({ keyId: kid, role: key.role, line: SIGNER_LINES[key.role].replace('{org}', key.org || 'the customer') })
        }
        const roles = new Set(checks.map((c) => c.role))
        const demo = roles.has('demo')
        if (demo && roles.size !== 1) throw new Refusal('mixed_demo')
        let text
        try {
            text = new TextDecoder('utf-8', { fatal: true }).decode(payload)
        } catch {
            throw new Refusal('malformed', 'the payload is not UTF-8')
        }
        const raw = parseStrict(text)
        if (!isObj(raw)) throw new Refusal('malformed', 'the payload is not an object')
        if (raw._type !== STATEMENT_TYPE || raw.predicateType !== PREDICATE_TYPE) throw new Refusal('unknown_schema', 'not a record/run/v1 statement')
        const rest = Object.fromEntries(Object.entries(raw).filter(([k]) => k !== '_type' && k !== 'predicateType'))
        const found = findEmbeddedContent(rest)
        if (found) throw new Refusal('embedded_content', found)
        const statement = validateStatement(raw)
        if (canonicalJson(statement) !== text) throw new Refusal('malformed', 'the payload is not canonical JSON')
        const problem = inconsistency(statement)
        if (problem) throw new Refusal('inconsistent', problem)
        const commitments = { ...statement.predicate.disclosure.commitments }
        for (const [i, c] of (statement.predicate.confirmations || []).entries()) for (const [k, v] of Object.entries(c.commitments)) commitments[`confirmations.${i}.${k}`] = v
        if (statement.predicate.approval) for (const [k, v] of Object.entries(statement.predicate.approval.commitments)) commitments[`approval.${k}`] = v
        let disclosed = {}
        let isPrivate = false
        if (cardOpenings) disclosed = await checkOpenings(cardOpenings, commitments, allowed)
        else if (openings != null) {
            if (!isObj(openings) || Object.keys(openings).sort().join(',') !== 'openings,payload_sha256,schema') throw new Refusal('malformed', 'a private openings file has three fields')
            if (openings.schema !== OPENINGS_SCHEMA) throw new Refusal('unknown_schema', 'not an openadapt.receipt-openings/v1 file')
            if (openings.payload_sha256 !== (await sha256Hex(payload))) throw new Refusal('opening_mismatch', 'the openings belong to another receipt')
            disclosed = await checkOpenings(openings.openings, commitments, null)
            isPrivate = true
            for (const [i, c] of (statement.predicate.confirmations || []).entries()) {
                const reason = disclosed[`confirmations.${i}.reason`]
                if (reason != null && !REASONS_FOR_STATE[c.state].includes(reason)) throw new Refusal('inconsistent', `a ${c.state} confirmation can't have reason ${reason}`)
            }
        }
        const ordered = [...checks].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role))
        const signerLines = [...new Set(ordered.map((c) => c.line))]
        const confirmations = statement.predicate.confirmations || []
        return {
            ok: true,
            reason: null,
            detail: '',
            signerLines,
            confirmationLines: confirmations.map((c, i) => confirmationLine(c, disclosed[`confirmations.${i}.observed_at`])),
            independent: confirmations.map((c) => INDEPENDENT_READS.includes(c.read_method)),
            approvalLine: approvalLine(statement.predicate.approval),
            outcome: statement.predicate.outcome,
            demo,
            disclosed,
            private: isPrivate,
            statement,
        }
    } catch (e) {
        if (e instanceof Refusal) return refuse(e.reason, e.message)
        return refuse('malformed', String(e?.message || e))
    }
}

/** The approval line, as every verifier prints it. */
export function approvalLine(a) {
    if (!a) return 'No approval recorded'
    if (a.mode === 'not_gated') return 'Not gated: no approval was required for these writes'
    if (a.approver_kind === 'none') return `Approval required for ${a.mode === 'each_item' ? 'each item' : 'items that fail a check'}; no item was approved`
    return `Approved by ${a.approver_kind === 'person' ? 'a person' : 'an agent delegate'}: ${a.mode === 'each_item' ? 'each item' : 'items that failed a check'}`
}

/** One counterparty confirmation's line; never merged with the signer lines. */
export function confirmationLine(c, observedAt) {
    const at = observedAt ? ` at ${observedAt}` : ''
    if (c.state === 'recorded_as_intended') return `Confirmed in the other party's system${at}`
    if (c.state === 'recorded_differently') return `Recorded differently in the other party's system${at}: ${c.differing_count} ${c.differing_count === 1 ? 'field differs' : 'fields differ'}`
    if (c.state === 'acknowledged') return `Acknowledged by the other party${at}`
    return `Sent to the other party${at}, not confirmed`
}
