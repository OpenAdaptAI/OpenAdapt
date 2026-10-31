// Checks an offline Seal (openadapt.local-seal/v1) the way
// `openadapt-flow local-seal verify` does (packages/flow/openadapt_flow/
// offline_seal.py, verify_local_seal), in the browser or in Node, with
// WebCrypto. The order and the refusal codes are the engine's: malformed,
// subject_mismatch, unknown_key, revoked_key, key_mismatch, bad_signature.
// Trust comes only from the pinned key registry; the public key inside a Seal
// is compared with it, never trusted on its own. When the runtime's WebCrypto
// has no Ed25519, the signature is checked with the RFC 8032 equations in
// ./ed25519ph.mjs instead; only a runtime with neither (no SHA-512 at all)
// stops at `unsupported`, so the page can show the command instead of a
// result it didn't compute.
import { verifyEd25519 } from './ed25519ph.mjs'

export const LOCAL_SEAL_SCHEMA = 'openadapt.local-seal/v1'

const SHA256_HEX = /^[a-f0-9]{64}$/
const KEY_ID = /^seal-ed25519-[a-f0-9]{16}$/
const ISO_SECONDS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/
const B64 = /^[A-Za-z0-9+/]+={0,2}$/
const OUTCOME = /^[A-Z][A-Z_]{0,31}$/
const SUBJECT_KINDS = ['run-report', 'record', 'export', 'file']
// authorization_sha256 and scope_item are present only on a run sealed under a
// run authorization (absent, never null, otherwise).
const OPTIONAL_FIELDS = ['authorization_sha256', 'scope_item']
// A short label (r3), never a name or record number (OpenAdapt #1401 f8b11e84a).
const ITEM = /^[a-z]{1,2}[0-9]{1,3}$/
const FIELDS = [
    'algorithm',
    'issued_at',
    'issuer',
    'key_id',
    'outcome',
    'public_key',
    'run_id',
    'schema',
    'signature',
    'subject_kind',
    'subject_sha256',
]

// Python's json.dumps(value, sort_keys=True, separators=(',', ':'),
// ensure_ascii=False) for the values a Seal holds: strings, null and objects.
export function canonicalJson(value) {
    if (value === null || typeof value !== 'object')
        return JSON.stringify(value)
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
    const keys = Object.keys(value).sort()
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`
}

export function signingPayload(seal) {
    const unsigned = Object.fromEntries(
        Object.entries(seal).filter(([k]) => k !== 'signature')
    )
    return new TextEncoder().encode(
        `${LOCAL_SEAL_SCHEMA}\0${canonicalJson(unsigned)}`
    )
}

export function base64Bytes(text) {
    const bin = atob(text)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i)
    return out
}

function hex(buffer) {
    return Array.from(new Uint8Array(buffer), (b) =>
        b.toString(16).padStart(2, '0')
    ).join('')
}

export async function sha256Hex(bytes) {
    return hex(await globalThis.crypto.subtle.digest('SHA-256', bytes))
}

// The fields and patterns of LocalSealV1 (extra fields forbidden).
export function wellFormed(seal) {
    if (!seal || typeof seal !== 'object' || Array.isArray(seal)) return false
    const keys = Object.keys(seal)
    if (keys.some((k) => !FIELDS.includes(k) && !OPTIONAL_FIELDS.includes(k)))
        return false
    if (
        'authorization_sha256' in seal &&
        !(
            typeof seal.authorization_sha256 === 'string' &&
            SHA256_HEX.test(seal.authorization_sha256)
        )
    )
        return false
    if (
        'scope_item' in seal &&
        !(typeof seal.scope_item === 'string' && ITEM.test(seal.scope_item))
    )
        return false
    const str = (k, re) => typeof seal[k] === 'string' && re.test(seal[k])
    return (
        seal.schema === LOCAL_SEAL_SCHEMA &&
        seal.issuer === 'local' &&
        seal.algorithm === 'ed25519' &&
        str('key_id', KEY_ID) &&
        str('public_key', B64) &&
        SUBJECT_KINDS.includes(seal.subject_kind) &&
        str('subject_sha256', SHA256_HEX) &&
        typeof seal.run_id === 'string' &&
        seal.run_id.length >= 1 &&
        seal.run_id.length <= 128 &&
        (seal.outcome == null || str('outcome', OUTCOME)) &&
        str('issued_at', ISO_SECONDS) &&
        str('signature', B64)
    )
}

// A pinned key is usable only if its key_id is the digest of its own bytes.
/** A key registry's entries: a JSON list, or an object with a `keys` list (both are the engine's). */
export const registryKeys = (registry) =>
    Array.isArray(registry) ? registry : registry?.keys || []

// A registry that lists one key_id twice is ambiguous: "active" in one entry
// must not outvote "revoked" in another (the engine's load_registry refuses it).
async function pinnedKey(registry, keyId) {
    const matches = registryKeys(registry).filter((k) => k.key_id === keyId)
    if (matches.length > 1) return { ambiguous: true }
    const entry = matches[0]
    if (!entry) return null
    const raw = base64Bytes(entry.public_key)
    if (raw.length !== 32) return null
    if (`seal-ed25519-${(await sha256Hex(raw)).slice(0, 16)}` !== entry.key_id)
        return null
    return { ...entry, raw }
}

export async function ed25519Supported() {
    try {
        await globalThis.crypto.subtle.importKey(
            'raw',
            new Uint8Array(32).fill(1),
            { name: 'Ed25519' },
            false,
            ['verify']
        )
        return true
    } catch {
        return false
    }
}

/**
 * Check a Seal over the exact bytes of its subject against a pinned registry.
 * Returns { ok, reason, digest, keyId, steps }, where steps lists each check
 * in order with its result: 'pass', 'fail' or 'skipped'.
 */
export async function verifyLocalSeal(seal, subjectBytes, registry) {
    const digest = await sha256Hex(subjectBytes)
    const steps = []
    const done = (ok, reason) => ({
        ok,
        reason,
        digest,
        keyId: seal?.key_id ?? null,
        steps,
    })
    if (!wellFormed(seal)) {
        steps.push({ id: 'shape', result: 'fail' })
        return done(false, 'malformed')
    }
    const match = seal.subject_sha256 === digest
    steps.push({ id: 'digest', result: match ? 'pass' : 'fail' })
    if (!match) return done(false, 'subject_mismatch')
    const key = await pinnedKey(registry, seal.key_id)
    if (!key) {
        steps.push({ id: 'key', result: 'fail' })
        return done(false, 'unknown_key')
    }
    if (key.ambiguous) {
        steps.push({ id: 'key', result: 'fail' })
        return done(false, 'ambiguous_key')
    }
    // issued_at is the signer's own claim; a revoked key can backdate it, so a
    // revoked key's Seals are refused whatever issued_at says.
    if (key.status === 'revoked') {
        steps.push({ id: 'key', result: 'fail' })
        return done(false, 'revoked_key')
    }
    if (seal.public_key !== key.public_key) {
        steps.push({ id: 'key', result: 'fail' })
        return done(false, 'key_mismatch')
    }
    steps.push({ id: 'key', result: 'pass' })
    let publicKey = null
    try {
        publicKey = await globalThis.crypto.subtle.importKey(
            'raw',
            key.raw,
            { name: 'Ed25519' },
            false,
            ['verify']
        )
    } catch {
        publicKey = null
    }
    let valid = false
    try {
        valid = publicKey
            ? await globalThis.crypto.subtle.verify(
                  { name: 'Ed25519' },
                  publicKey,
                  base64Bytes(seal.signature),
                  signingPayload(seal)
              )
            : await verifyEd25519(
                  key.raw,
                  signingPayload(seal),
                  base64Bytes(seal.signature)
              )
    } catch {
        if (!publicKey) {
            steps.push({ id: 'signature', result: 'skipped' })
            return done(false, 'unsupported')
        }
        valid = false
    }
    steps.push({ id: 'signature', result: valid ? 'pass' : 'fail' })
    return valid ? done(true, null) : done(false, 'bad_signature')
}

// The first key that appears twice in one JSON object, or null. JSON.parse
// keeps the last copy silently, so a Seal with a repeated field could show one
// value and be checked on another; the page refuses such a file outright.
export function duplicateKey(text) {
    const stack = []
    let i = 0
    while (i < text.length) {
        const ch = text[i]
        if (ch === '"') {
            let j = i + 1
            while (j < text.length && text[j] !== '"')
                j += text[j] === '\\' ? 2 : 1
            const top = stack.at(-1)
            if (top && top.keys && top.expectKey) {
                const key = JSON.parse(text.slice(i, j + 1))
                if (top.keys.has(key)) return key
                top.keys.add(key)
                top.expectKey = false
            }
            i = j + 1
            continue
        }
        if (ch === '{') stack.push({ keys: new Set(), expectKey: true })
        else if (ch === '[') stack.push({ keys: null })
        else if (ch === '}' || ch === ']') stack.pop()
        else if (ch === ',' && stack.at(-1)?.keys) stack.at(-1).expectKey = true
        i += 1
    }
    return null
}

/**
 * Check a Seal file's exact text over the exact bytes of the one subject file
 * shown on the page, with no fallback to any other file. Adds `subject`
 * ({ name, sha256 }) so the page can say which file it verified.
 */
export async function verifySealFile(
    sealText,
    subjectBytes,
    registry,
    subjectName = 'report.json'
) {
    const subject = { name: subjectName, sha256: await sha256Hex(subjectBytes) }
    const refuse = (duplicate) => ({
        ok: false,
        reason: 'malformed',
        digest: subject.sha256,
        keyId: null,
        steps: [{ id: 'shape', result: 'fail' }],
        subject,
        duplicate,
    })
    const duplicate = duplicateKey(sealText)
    if (duplicate !== null) return refuse(duplicate)
    let seal
    try {
        seal = JSON.parse(sealText)
    } catch {
        return refuse(null)
    }
    return {
        ...(await verifyLocalSeal(seal, subjectBytes, registry)),
        subject,
        seal,
    }
}
