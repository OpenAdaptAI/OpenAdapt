// Checks a Seal's anchor in the Rekor transparency log, offline, the way
// `openadapt-flow local-seal verify --require-anchor` does
// (packages/flow/openadapt_flow/seal_anchor.py, verify_anchor and
// verify_entry): the same order and the same refusal codes. From the stored
// anchor file it checks that the log entry is this Seal with this nonce, that
// the entry is signed by the Seal's pinned key (Ed25519ph, lib/ed25519ph.mjs),
// the log's signed entry timestamp, the RFC 6962 inclusion proof, and the
// signed checkpoint, against the pinned log key. Nothing is fetched.

import { verifyPrehashed } from './ed25519ph.mjs'
import { base64Bytes, canonicalJson } from './localSeal.mjs'

export const ANCHOR_SCHEMA = 'openadapt.local-seal-anchor/v1'

// The public-good Rekor log's key (https://rekor.sigstore.dev/api/v1/log/publicKey),
// the same key the engine builds in. Its SHA-256 (DER) is the log ID c0d23d6a…801d.
export const REKOR_PUBLIC_KEY_PEM = `-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE2G2Y+2tabdTV5BcGiBIx0a9fAFwr
kBbmLSGtks4L3qX6yYY0zufBnhC8Ur/iy55GhWP/9A/bY2LhC30M9+RYtw==
-----END PUBLIC KEY-----
`

const HEX64 = /^[a-f0-9]{64}$/
const ED25519_SPKI_PREFIX = '302a300506032b6570032100'
const subtle = () => globalThis.crypto.subtle
const utf8 = (s) => new TextEncoder().encode(s)

export const hexOf = (bytes) =>
    Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')

export function bytesOfHex(hex) {
    if (typeof hex !== 'string' || hex.length % 2 || /[^0-9a-f]/i.test(hex))
        throw new Error('hex')
    const out = new Uint8Array(hex.length / 2)
    for (let i = 0; i < out.length; i += 1)
        out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
    return out
}

function strictBase64(text) {
    if (
        typeof text !== 'string' ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(text) ||
        text.length % 4
    )
        throw new Error('base64')
    return base64Bytes(text)
}

const concat = (...parts) => {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
    let at = 0
    for (const p of parts) {
        out.set(p, at)
        at += p.length
    }
    return out
}

const equal = (a, b) => a.length === b.length && a.every((v, i) => v === b[i])
const sha256 = async (bytes) =>
    new Uint8Array(await subtle().digest('SHA-256', bytes))

function pemBody(pem) {
    return strictBase64(
        String(pem)
            .replace(/-----(BEGIN|END) PUBLIC KEY-----/g, '')
            .replace(/\s+/g, '')
    )
}

/** A pinned log key: { key: CryptoKey (ECDSA P-256), logId: hex SHA-256 of the DER }. */
// The public Rekor log's checkpoint origin name.
export const REKOR_ORIGIN = 'rekor.sigstore.dev'

export async function logKeyFromPem(pem, origin = REKOR_ORIGIN) {
    const der = pemBody(pem)
    const key = await subtle().importKey(
        'spki',
        der,
        { name: 'ECDSA', namedCurve: 'P-256' },
        false,
        ['verify']
    )
    return { key, logId: hexOf(await sha256(der)), origin }
}

// ECDSA signatures from the log are DER; WebCrypto wants r || s, 32 bytes each.
export function derToRaw(der) {
    if (der[0] !== 0x30) return null
    let at = 2
    if (der[1] & 0x80) at = 2 + (der[1] & 0x7f)
    const out = new Uint8Array(64)
    for (const half of [0, 1]) {
        if (der[at] !== 0x02) return null
        const len = der[at + 1]
        let int = der.slice(at + 2, at + 2 + len)
        at += 2 + len
        while (int.length > 32 && int[0] === 0) int = int.slice(1)
        if (int.length > 32) return null
        out.set(int, half * 32 + (32 - int.length))
    }
    return at === der.length ? out : null
}

async function logVerify(log, signature, payload) {
    const raw = derToRaw(signature)
    if (!raw) return false
    try {
        return await subtle().verify(
            { name: 'ECDSA', hash: 'SHA-256' },
            log.key,
            raw,
            payload
        )
    } catch {
        return false
    }
}

export const leafHash = (body) => sha256(concat(new Uint8Array([0]), body))
const node = (left, right) => sha256(concat(new Uint8Array([1]), left, right))

/** The root an inclusion proof implies (RFC 9162 section 2.1.3.2), or null. */
export async function rootFromInclusion(index, size, leaf, proof) {
    let fn = BigInt(index)
    let sn = BigInt(size) - 1n
    if (fn < 0n || sn < 0n || fn > sn) return null
    let r = leaf
    for (const p of proof) {
        if (sn === 0n) return null
        if (fn & 1n || fn === sn) {
            r = await node(p, r)
            if (!(fn & 1n)) {
                while (!(fn & 1n) && fn !== 0n) {
                    fn >>= 1n
                    sn >>= 1n
                }
            }
        } else {
            r = await node(r, p)
        }
        fn >>= 1n
        sn >>= 1n
    }
    return sn === 0n ? r : null
}

/** Parse a signed note and check one signature against a pinned log key. */
export async function verifyCheckpoint(text, logKeys) {
    const cut = String(text).indexOf('\n\n')
    if (cut < 0) return null
    const body = text.slice(0, cut)
    const sigs = text.slice(cut + 2)
    const lines = body.split('\n')
    if (lines.length < 3 || !/^\d+$/.test(lines[1])) return null
    let root
    try {
        root = strictBase64(lines[2])
    } catch {
        return null
    }
    const signed = utf8(`${body}\n`)
    // The origin line names the log ("ORIGIN - TREE_ID" for Rekor v1), and so
    // must the signature line.
    const dash = lines[0].indexOf(' - ')
    const name = dash >= 0 ? lines[0].slice(0, dash) : lines[0]
    const tree = dash >= 0 ? lines[0].slice(dash + 3) : null
    if (tree !== null && !/^\d+$/.test(tree)) return null
    const treeId = tree === null ? null : BigInt(tree)
    for (const line of sigs.split('\n')) {
        if (!line.startsWith('— ')) continue
        const parts = line.slice(2).split(' ')
        if (parts.length < 2) continue
        const signer = parts.slice(0, -1).join(' ')
        let blob
        try {
            blob = strictBase64(parts.at(-1))
        } catch {
            continue
        }
        const hint = hexOf(blob.slice(0, 4))
        for (const log of logKeys) {
            if (name !== log.origin || signer !== log.origin) continue
            if (
                log.logId.slice(0, 8) === hint &&
                (await logVerify(log, blob.slice(4), signed))
            ) {
                return {
                    origin: lines[0],
                    size: BigInt(lines[1]),
                    root,
                    treeId,
                }
            }
        }
    }
    return null
}

export const sealSha256 = async (seal) =>
    hexOf(await sha256(utf8(canonicalJson(seal))))
// The statement an anchor logs: a record's digests and a nonce. A Seal's
// record is { seal_sha256 }; a run authorization's is
// { run_authorization_sha256, signature_sha256 }.
export const recordStatement = (fields, nonce) =>
    utf8(`${ANCHOR_SCHEMA}\0${canonicalJson({ nonce, ...fields })}`)
export const statement = (sealHex, nonce) =>
    recordStatement({ seal_sha256: sealHex }, nonce)

async function rawFromEd25519Pem(pemBytes) {
    try {
        const der = pemBody(new TextDecoder().decode(pemBytes))
        if (
            der.length !== 44 ||
            hexOf(der.slice(0, 12)) !== ED25519_SPKI_PREFIX
        )
            return null
        return der.slice(12)
    } catch {
        return null
    }
}

/**
 * Check that `anchor` (seal.anchor.json) anchors exactly `seal`, signed by the
 * pinned Seal key `publicRaw`, in a log whose key is in `logKeys`.
 * Returns { ok, reason, uuid, logIndex, integratedTime, logId, steps }.
 */
/** Check that `anchor` anchors exactly `seal`; see verifyRecordAnchor. */
export async function verifyAnchor(seal, anchor, publicRaw, logKeys) {
    return verifyRecordAnchor(
        { seal_sha256: await sealSha256(seal) },
        anchor,
        publicRaw,
        logKeys
    )
}

/**
 * Check that `anchor` anchors exactly the record whose digests are `fields`,
 * signed by the pinned key `publicRaw`, in a log whose key is in `logKeys`
 * (the engine's verify_record_anchor). Returns
 * { ok, reason, uuid, logIndex, integratedTime, steps }.
 */
export async function verifyRecordAnchor(fields, anchor, publicRaw, logKeys) {
    const steps = []
    const out = (ok, reason, extra = {}) => ({ ok, reason, steps, ...extra })
    if (anchor == null) return out(false, 'anchor_missing')
    let nonce
    let entry
    let stored
    try {
        if (anchor.schema !== ANCHOR_SCHEMA) throw new Error('schema')
        nonce = String(anchor.nonce)
        entry = anchor.entry
        stored = Object.fromEntries(
            Object.keys(fields).map((k) => [k, anchor[k]])
        )
        if (
            !HEX64.test(nonce) ||
            !entry ||
            !Object.values(stored).every(
                (v) => typeof v === 'string' && HEX64.test(v)
            )
        )
            throw new Error('format')
    } catch {
        steps.push({ id: 'anchorEntry', result: 'fail' })
        return out(false, 'anchor_malformed')
    }
    const digest = hexOf(
        new Uint8Array(
            await subtle().digest('SHA-512', recordStatement(fields, nonce))
        )
    )
    if (
        Object.keys(fields).some((k) => stored[k] !== fields[k]) ||
        anchor.statement_sha512 !== digest
    ) {
        steps.push({ id: 'anchorEntry', result: 'fail' })
        return out(false, 'anchor_mismatch')
    }

    let uuid,
        bodyB64,
        bodyBytes,
        body,
        integratedTime,
        logId,
        logIndex,
        setSig,
        proof,
        hashed,
        sigContent,
        keyPem
    try {
        uuid = String(entry.uuid)
        bodyB64 = String(entry.body)
        bodyBytes = strictBase64(bodyB64)
        body = JSON.parse(new TextDecoder().decode(bodyBytes))
        integratedTime = entry.integratedTime
        logId = String(entry.logID)
        logIndex = entry.logIndex
        if (!Number.isInteger(integratedTime) || !Number.isInteger(logIndex))
            throw new Error('int')
        setSig = strictBase64(entry.verification.signedEntryTimestamp)
        proof = entry.verification.inclusionProof
        hashed = body.spec.data.hash
        sigContent = strictBase64(body.spec.signature.content)
        keyPem = strictBase64(body.spec.signature.publicKey.content)
    } catch {
        steps.push({ id: 'anchorEntry', result: 'fail' })
        return out(false, 'anchor_malformed')
    }
    const meta = { uuid, logIndex, integratedTime, logId }
    const refuse = (id, reason) => {
        steps.push({ id, result: 'fail' })
        return out(false, reason, meta)
    }
    if (body.kind !== 'hashedrekord' || body.apiVersion !== '0.0.1')
        return refuse('anchorEntry', 'anchor_malformed')
    if (hashed?.algorithm !== 'sha512' || hashed?.value !== digest)
        return refuse('anchorEntry', 'anchor_mismatch')
    steps.push({ id: 'anchorEntry', result: 'pass' })
    const entryKey = await rawFromEd25519Pem(keyPem)
    if (!entryKey || !equal(entryKey, publicRaw))
        return refuse('anchorKey', 'anchor_key_mismatch')
    if (!(await verifyPrehashed(publicRaw, bytesOfHex(digest), sigContent)))
        return refuse('anchorKey', 'anchor_bad_signature')
    steps.push({ id: 'anchorKey', result: 'pass' })
    const log = logKeys.find((k) => k.logId === logId)
    if (!log) return refuse('anchorTime', 'anchor_unknown_log')
    const setPayload = utf8(
        canonicalJson({ body: bodyB64, integratedTime, logID: logId, logIndex })
    )
    if (!(await logVerify(log, setSig, setPayload)))
        return refuse('anchorTime', 'anchor_bad_timestamp')
    steps.push({ id: 'anchorTime', result: 'pass' })
    const leaf = await leafHash(bodyBytes)
    if (!uuid.endsWith(hexOf(leaf)))
        return refuse('anchorInclusion', 'anchor_bad_inclusion')
    let root, claimedRoot, treeSize
    try {
        treeSize = BigInt(proof.treeSize)
        root = await rootFromInclusion(
            proof.logIndex,
            proof.treeSize,
            leaf,
            proof.hashes.map(bytesOfHex)
        )
        claimedRoot = bytesOfHex(proof.rootHash)
    } catch {
        return refuse('anchorInclusion', 'anchor_malformed')
    }
    if (!root || !equal(root, claimedRoot))
        return refuse('anchorInclusion', 'anchor_bad_inclusion')
    steps.push({ id: 'anchorInclusion', result: 'pass' })
    const checkpoint = await verifyCheckpoint(String(proof.checkpoint), [log])
    if (
        !checkpoint ||
        !equal(checkpoint.root, claimedRoot) ||
        checkpoint.size !== treeSize
    )
        return refuse('anchorCheckpoint', 'anchor_bad_checkpoint')
    // A Rekor v1 UUID starts with the tree ID the checkpoint names.
    if (
        uuid.length === 80 &&
        checkpoint.treeId !== null &&
        uuid.slice(0, 16) !== checkpoint.treeId.toString(16).padStart(16, '0')
    )
        return refuse('anchorCheckpoint', 'anchor_bad_checkpoint')
    steps.push({ id: 'anchorCheckpoint', result: 'pass' })
    return out(true, null, meta)
}
