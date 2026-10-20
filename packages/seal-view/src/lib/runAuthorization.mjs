// Checks a run authorization (openadapt.run-authorization/v1) the way the
// engine does (packages/flow/openadapt_flow/run_authorization.py, verify and
// check_run; OpenAdapt #1401, with its security-review changes f8b11e84a):
// a statement a person signs with their SSH key (`ssh-keygen -Y sign -n
// openadapt-run-authorization`). The signing key must be the one the statement
// names (signer_fingerprint). Then it is matched to one run: the Seal names the
// statement's SHA-256, the Seal's scope item, key and the report's profile are
// in scope, and the report's program bundle is the one authorized. Its
// optional transparency-log anchor is checked with lib/sealAnchor.mjs and fixes
// when it was signed. Same refusal codes as the engine.
//
// Who signed is a separate question. The allowed-signers file stored next to
// the statement is not proof: whoever wrote the statement can write it. The
// signer is "@login" only when GitHub itself lists the signing key for that
// login, fetched live from the pinned host (checkGithubIdentity); otherwise
// the page says "identity not verified (self-asserted github:<login>)".

import { duplicateKey, sha256Hex } from './localSeal.mjs'
import { fingerprint, parseSshsig, verifySshsig } from './sshsig.mjs'

export const AUTHORIZATION_SCHEMA = 'openadapt.run-authorization/v1'
export const AUTHORIZATION_NAMESPACE = 'openadapt-run-authorization'
export const AUTHORIZATION_FILES = {
    statement: 'run-authorization.json',
    sig: 'run-authorization.json.sig',
    signers: 'run-authorization.allowed_signers',
    anchor: 'run-authorization.anchor.json',
}
const HEX64 = /^[a-f0-9]{64}$/
// Short labels (r3), never names or record numbers.
export const ITEM = /^[a-z]{1,2}[0-9]{1,3}$/
const SIGNER = /^SHA256:[A-Za-z0-9+/]{43}$/
const KEY_ID = /^seal-ed25519-[a-f0-9]{16}$/
const PROFILE = /^[a-z][a-z0-9_-]{0,31}$/
const HANDLE = /^github:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/

const only = (o, keys) =>
    o &&
    typeof o === 'object' &&
    !Array.isArray(o) &&
    Object.keys(o).every((k) => keys.includes(k)) &&
    keys.every((k) => k in o)

/** The statement, or null when it isn't a well-formed run authorization. */
export function parseAuthorization(text) {
    if (duplicateKey(text) !== null) return null
    let a
    try {
        a = JSON.parse(text)
    } catch {
        return null
    }
    const s = a?.scope
    const ok =
        only(a, [
            'authorizer',
            'bundle_content_digest',
            'issued_at',
            'nonce',
            'schema',
            'scope',
            'signer_fingerprint',
        ]) &&
        a.schema === AUTHORIZATION_SCHEMA &&
        HANDLE.test(a.authorizer) &&
        SIGNER.test(a.signer_fingerprint) &&
        HEX64.test(a.bundle_content_digest) &&
        ISO.test(a.issued_at) &&
        HEX64.test(a.nonce) &&
        s &&
        typeof s === 'object' &&
        !Array.isArray(s) &&
        Object.keys(s).every((k) => ['items', 'profile', 'seal_key_ids', 'reusable'].includes(k)) &&
        ['items', 'profile', 'seal_key_ids'].every((k) => k in s) &&
        (!('reusable' in s) || typeof s.reusable === 'boolean') &&
        Array.isArray(s.items) &&
        s.items.length > 0 &&
        s.items.every((i) => typeof i === 'string' && ITEM.test(i)) &&
        PROFILE.test(s.profile) &&
        Array.isArray(s.seal_key_ids) &&
        s.seal_key_ids.length > 0 &&
        s.seal_key_ids.every((k) => typeof k === 'string' && KEY_ID.test(k))
    return ok ? a : null
}

/** Whether a scope lets one item be run more than once (default: no). */
export const reusable = (statement) => statement?.scope?.reusable === true

/** The GitHub login in a "github:<login>" handle. */
export const loginOf = (handle) => String(handle).replace(/^github:/, '')

function base64Bytes(text) {
    const bin = atob(text)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i)
    return out
}

/**
 * The plain Ed25519 lines of an allowed-signers file, as the engine writes
 * them: `<principals> [namespaces="..."] ssh-ed25519 <base64>`. Any other line
 * (a certificate authority, other options) makes the whole file unusable.
 */
export function parseAllowedSigners(text) {
    const out = []
    for (const raw of String(text).split('\n')) {
        const line = raw.trim()
        if (!line || line.startsWith('#')) continue
        const m = line.match(
            /^(\S+)\s+(?:namespaces="([^"]*)"\s+)?ssh-ed25519\s+(\S+)(?:\s+.*)?$/
        )
        if (!m) return null
        let blob
        try {
            blob = base64Bytes(m[3])
        } catch {
            return null
        }
        out.push({
            principals: m[1].split(',').filter(Boolean),
            namespaces: m[2] ? m[2].split(',') : null,
            blob,
        })
    }
    return out
}

/**
 * Check the statement's text and its SSH signature, made by the key the
 * statement names. Returns { ok, reason, statement, sha256, signatureSha256,
 * fingerprint, login, publicKey, steps }. Reasons: auth_malformed,
 * auth_unsigned, auth_bad_signature, auth_signer_mismatch. Says nothing about
 * who holds the key: see checkGithubIdentity.
 */
export async function verifyAuthorization(statementText, sigText) {
    const steps = []
    const out = (ok, reason, extra = {}) => ({ ok, reason, steps, ...extra })
    const statement = parseAuthorization(statementText)
    if (!statement) {
        steps.push({ id: 'authShape', result: 'fail' })
        return out(false, 'auth_malformed')
    }
    const sha256 = await sha256Hex(new TextEncoder().encode(statementText))
    const base = { statement, sha256, login: loginOf(statement.authorizer) }
    if (!sigText) {
        steps.push({ id: 'authSignature', result: 'fail' })
        return out(false, 'auth_unsigned', base)
    }
    const signatureSha256 = await sha256Hex(new TextEncoder().encode(sigText))
    const parsed = parseSshsig(sigText)
    // The engine signs and accepts sha512 message hashes only.
    if (!parsed || parsed.hashAlgorithm !== 'sha512') {
        steps.push({ id: 'authSignature', result: 'fail' })
        return out(false, 'auth_bad_signature', { ...base, signatureSha256 })
    }
    const sig = await verifySshsig(
        sigText,
        new TextEncoder().encode(statementText),
        AUTHORIZATION_NAMESPACE,
        [parsed.publicKey]
    )
    const fp = await fingerprint(parsed.publicKey)
    const signed = { ...base, signatureSha256, fingerprint: fp, publicKey: parsed.publicKey }
    if (!sig.ok) {
        steps.push({ id: 'authSignature', result: 'fail' })
        return out(false, sig.reason === 'unsupported' ? 'unsupported' : 'auth_bad_signature', signed)
    }
    steps.push({ id: 'authSignature', result: 'pass' })
    if (fp !== statement.signer_fingerprint) {
        steps.push({ id: 'authKey', result: 'fail' })
        return out(false, 'auth_signer_mismatch', signed)
    }
    steps.push({ id: 'authKey', result: 'pass' })
    return out(true, null, signed)
}

/** GitHub's own list of a login's SSH keys: the pinned API host, HTTPS, no redirects. */
export const GITHUB_KEYS_API = (login) => `https://api.github.com/users/${encodeURIComponent(login)}/keys`

/**
 * Whether GitHub itself lists `publicKey` (the SSH key blob that signed) for
 * `login`, asked live. 'verified', 'not_listed', or 'unavailable' (network,
 * CORS, rate limit or format): only 'verified' may show the login as the signer.
 */
export async function checkGithubIdentity(login, publicKey, { fetchImpl = globalThis.fetch, timeoutMs = 6000 } = {}) {
    if (!HANDLE.test(`github:${login}`) || !publicKey) return 'unavailable'
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null
    const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null
    try {
        const r = await fetchImpl(GITHUB_KEYS_API(login), {
            redirect: 'error',
            credentials: 'omit',
            referrerPolicy: 'no-referrer',
            signal: ctrl?.signal,
            headers: { accept: 'application/vnd.github+json' },
        })
        if (!r.ok) return 'unavailable'
        const list = await r.json()
        if (!Array.isArray(list)) return 'unavailable'
        const equal = (a, b) => a.length === b.length && a.every((v, i) => v === b[i])
        for (const entry of list) {
            const m = /^ssh-ed25519\s+([A-Za-z0-9+/=]+)/.exec(String(entry?.key || ''))
            if (!m) continue
            let blob
            try {
                blob = base64Bytes(m[1])
            } catch {
                continue
            }
            if (equal(blob, publicKey)) return 'verified'
        }
        return 'not_listed'
    } catch {
        return 'unavailable'
    } finally {
        if (timer) clearTimeout(timer)
    }
}

/**
 * With reusable false, one Seal per (authorization, item): the first by
 * issued_at, then by seal SHA-256, keeps the item; the rest are refused
 * (auth_item_reused). runs: [{ seal, sealSha256, statement }]. Returns the
 * set of sealSha256 values refused.
 */
export function reusedItems(runs) {
    const groups = new Map()
    for (const r of runs) {
        if (!r.seal?.authorization_sha256 || !r.statement || reusable(r.statement)) continue
        const key = `${r.seal.authorization_sha256}\u0000${r.seal.scope_item}`
        if (!groups.has(key)) groups.set(key, [])
        groups.get(key).push(r)
    }
    const refused = new Set()
    for (const list of groups.values()) {
        list.sort((a, b) => (a.seal.issued_at < b.seal.issued_at ? -1 : a.seal.issued_at > b.seal.issued_at ? 1 : a.sealSha256 < b.sealSha256 ? -1 : 1))
        for (const r of list.slice(1)) refused.add(r.sealSha256)
    }
    return refused
}

/**
 * Whether a verified Seal and its report fit a verified authorization (the
 * engine's check_run): null, or auth_not_referenced, auth_reference_mismatch,
 * auth_scope_mismatch, auth_bundle_mismatch.
 */
export function checkRun(seal, report, auth) {
    if (!auth?.ok || !auth.statement) return auth?.reason || 'auth_malformed'
    if (seal.authorization_sha256 == null) return 'auth_not_referenced'
    if (seal.authorization_sha256 !== auth.sha256)
        return 'auth_reference_mismatch'
    const scope = auth.statement.scope
    if (
        !scope.items.includes(seal.scope_item) ||
        !scope.seal_key_ids.includes(seal.key_id) ||
        report?.execution_profile !== scope.profile
    )
        return 'auth_scope_mismatch'
    if (report?.bundle_content_digest !== auth.statement.bundle_content_digest)
        return 'auth_bundle_mismatch'
    return null
}

/** A report's started_at in whole epoch seconds, or null without a timezone (the engine's _started_epoch). */
export function startedEpoch(value) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value))
        return null
    const ms = Date.parse(value)
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}

/**
 * When the authorization was signed relative to the run (the engine's
 * authorization_timing, after its security review): before_run only when both
 * the authorization and the Seal are in the log and authorization time <
 * the report's started_at <= the Seal's log time. Both anchored in any other
 * order: after_run (a started_at later than the Seal's log time included).
 * Either anchor missing, or no started_at: not_anchored. A run re-sealed under
 * an authorization signed later is never "before_run".
 */
export function authorizationTiming(authAnchorTime, startedAt, sealAnchorTime) {
    const started = startedEpoch(startedAt)
    if (!Number.isInteger(authAnchorTime) || !Number.isInteger(sealAnchorTime) || started === null) return 'not_anchored'
    return authAnchorTime < started && started <= sealAnchorTime ? 'before_run' : 'after_run'
}

/** Secondary, never used for wording: the authorization's log time against the Seal's. */
export function authorizationVsSeal(authAnchorTime, sealAnchorTime) {
    if (!Number.isInteger(authAnchorTime) || !Number.isInteger(sealAnchorTime))
        return null
    return authAnchorTime <= sealAnchorTime ? 'before_seal' : 'after_seal'
}

/**
 * The line for who authorized the run. "@login" only when GitHub's own live
 * list showed the signing key for that login; otherwise the login is only a
 * claim. Without both log times it says nothing about order.
 */
export function authorizedLine(login, timing, authAnchorTime, identity = 'unverified') {
    const verified = identity === 'verified'
    const who = verified ? `@${login}` : 'a key'
    const claim = verified ? '' : ` · identity not verified (self-asserted github:${login})`
    if (timing === 'before_run') return `Authorized by ${who} before this run${claim}`
    if (timing === 'after_run')
        return `Authorization signed by ${who} on ${new Date(authAnchorTime * 1000).toISOString().slice(0, 10)}, after this run${claim}`
    return `Authorized by ${who}${claim}`
}
