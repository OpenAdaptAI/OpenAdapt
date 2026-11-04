// Verifies an SSH signature (the SSHSIG format of `ssh-keygen -Y sign`,
// OpenSSH PROTOCOL.sshsig) in the browser or Node with WebCrypto. Used for a
// run authorization signed with a person's SSH key, the one GitHub publishes
// at github.com/<login>.keys. Only ssh-ed25519 keys and sha256/sha512 message
// hashes are accepted; anything else is refused.

const MAGIC = 'SSHSIG'
const utf8 = new TextEncoder()

function base64Bytes(text) {
    const bin = atob(text)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i)
    return out
}

function base64Of(bytes) {
    let bin = ''
    for (const b of bytes) bin += String.fromCharCode(b)
    return btoa(bin)
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

function sshString(bytes) {
    const len = new Uint8Array(4)
    new DataView(len.buffer).setUint32(0, bytes.length)
    return concat(len, bytes)
}

// Reads SSH wire fields (uint32 and length-prefixed strings) in order.
function reader(bytes) {
    let at = 0
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    return {
        uint32() {
            if (at + 4 > bytes.length) throw new Error('short')
            const v = view.getUint32(at)
            at += 4
            return v
        },
        string() {
            const len = this.uint32()
            if (at + len > bytes.length) throw new Error('short')
            const out = bytes.slice(at, at + len)
            at += len
            return out
        },
        raw(n) {
            if (at + n > bytes.length) throw new Error('short')
            const out = bytes.slice(at, at + n)
            at += n
            return out
        },
        done: () => at === bytes.length,
    }
}

const text = (bytes) => new TextDecoder().decode(bytes)

/** The raw 32-byte key from an ssh-ed25519 public key blob, or null. */
function ed25519FromBlob(blob) {
    try {
        const r = reader(blob)
        if (text(r.string()) !== 'ssh-ed25519') return null
        const key = r.string()
        return key.length === 32 && r.done() ? key : null
    } catch {
        return null
    }
}

/** OpenSSH's fingerprint of a public key blob: "SHA256:" + unpadded base64. */
export async function fingerprint(blob) {
    const digest = new Uint8Array(
        await globalThis.crypto.subtle.digest('SHA-256', blob)
    )
    return `SHA256:${base64Of(digest).replace(/=+$/, '')}`
}

/** The ssh-ed25519 key blobs in an authorized_keys-style list (github.com/<login>.keys). */
export function ed25519Keys(listText) {
    const keys = []
    for (const line of String(listText).split('\n')) {
        const [type, b64] = line.trim().split(/\s+/)
        if (type !== 'ssh-ed25519' || !b64) continue
        try {
            const blob = base64Bytes(b64)
            if (ed25519FromBlob(blob)) keys.push(blob)
        } catch {
            // not a key line
        }
    }
    return keys
}

/** Parse an armored SSHSIG, or return null when it isn't one. */
export function parseSshsig(armored) {
    const m = String(armored).match(
        /-----BEGIN SSH SIGNATURE-----([\s\S]*?)-----END SSH SIGNATURE-----/
    )
    if (!m) return null
    try {
        const r = reader(base64Bytes(m[1].replace(/\s+/g, '')))
        if (text(r.raw(6)) !== MAGIC) return null
        const version = r.uint32()
        const publicKey = r.string()
        const namespace = text(r.string())
        const reserved = r.string()
        const hashAlgorithm = text(r.string())
        const sigBlob = r.string()
        if (!r.done() || version !== 1) return null
        const s = reader(sigBlob)
        const sigType = text(s.string())
        const signature = s.string()
        if (!s.done()) return null
        return {
            publicKey,
            namespace,
            reserved,
            hashAlgorithm,
            sigType,
            signature,
        }
    } catch {
        return null
    }
}

/**
 * Check an SSH signature over `message` (bytes) for `namespace`, by one of
 * `allowedKeys` (ssh-ed25519 key blobs). Returns { ok, reason, fingerprint }.
 * Reasons: malformed, wrong_namespace, unsupported, unknown_key, bad_signature.
 */
export async function verifySshsig(armored, message, namespace, allowedKeys) {
    const sig = parseSshsig(armored)
    if (!sig) return { ok: false, reason: 'malformed' }
    const fp = await fingerprint(sig.publicKey)
    if (sig.namespace !== namespace)
        return { ok: false, reason: 'wrong_namespace', fingerprint: fp }
    const raw = ed25519FromBlob(sig.publicKey)
    const hash = { sha256: 'SHA-256', sha512: 'SHA-512' }[sig.hashAlgorithm]
    if (
        !raw ||
        sig.sigType !== 'ssh-ed25519' ||
        sig.signature.length !== 64 ||
        !hash
    )
        return { ok: false, reason: 'unsupported', fingerprint: fp }
    if (!allowedKeys.some((k) => equal(k, sig.publicKey)))
        return { ok: false, reason: 'unknown_key', fingerprint: fp }
    const digest = new Uint8Array(
        await globalThis.crypto.subtle.digest(hash, message)
    )
    const signed = concat(
        utf8.encode(MAGIC),
        sshString(utf8.encode(namespace)),
        sshString(sig.reserved),
        sshString(utf8.encode(sig.hashAlgorithm)),
        sshString(digest)
    )
    let ok = false
    try {
        const key = await globalThis.crypto.subtle.importKey(
            'raw',
            raw,
            { name: 'Ed25519' },
            false,
            ['verify']
        )
        ok = await globalThis.crypto.subtle.verify(
            { name: 'Ed25519' },
            key,
            sig.signature,
            signed
        )
    } catch {
        return { ok: false, reason: 'unsupported', fingerprint: fp }
    }
    return ok
        ? { ok: true, reason: null, fingerprint: fp }
        : { ok: false, reason: 'bad_signature', fingerprint: fp }
}
