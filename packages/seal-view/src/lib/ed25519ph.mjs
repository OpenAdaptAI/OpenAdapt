// Ed25519ph verification (RFC 8032 section 5.1, prehashed, empty context), for
// the signature inside a Rekor hashedrekord entry: Rekor checks an Ed25519 key
// there as Ed25519ph over the artifact's SHA-512 digest, and WebCrypto offers
// pure Ed25519 only. A line-for-line port of the engine's verifier
// (packages/flow/openadapt_flow/ed25519ph.py), written from the RFC's
// equations. Verification only, over public data, so it need not be constant
// time. test/rfc8032.test.mjs checks it against the RFC 8032 section 7.3
// vector.

const P = 2n ** 255n - 19n
const L = 2n ** 252n + 27742317777372353535851937790883648493n
const PH_DOM = new Uint8Array([
    ...new TextEncoder().encode('SigEd25519 no Ed25519 collisions'),
    1,
    0,
])

const mod = (a) => {
    const r = a % P
    return r < 0n ? r + P : r
}

function pow(base, exp) {
    let result = 1n
    let b = mod(base)
    let e = exp
    while (e > 0n) {
        if (e & 1n) result = (result * b) % P
        b = (b * b) % P
        e >>= 1n
    }
    return result
}

const inv = (a) => pow(a, P - 2n)
const D = mod(-121665n * inv(121666n))
const SQRT_M1 = pow(2n, (P - 1n) / 4n)

function add(a, b) {
    const [x1, y1, z1, t1] = a
    const [x2, y2, z2, t2] = b
    const aa = mod((y1 - x1) * (y2 - x2))
    const bb = mod((y1 + x1) * (y2 + x2))
    const cc = mod(2n * t1 * t2 * D)
    const dd = mod(2n * z1 * z2)
    const e = bb - aa
    const f = dd - cc
    const g = dd + cc
    const h = bb + aa
    return [mod(e * f), mod(g * h), mod(f * g), mod(e * h)]
}

function mul(scalar, point) {
    let result = [0n, 1n, 1n, 0n]
    let p = point
    let s = scalar
    while (s > 0n) {
        if (s & 1n) result = add(result, p)
        p = add(p, p)
        s >>= 1n
    }
    return result
}

function recoverX(y, sign) {
    if (y >= P) return null
    const x2 = mod((y * y - 1n) * inv(D * y * y + 1n))
    if (x2 === 0n) return sign ? null : 0n
    let x = pow(x2, (P + 3n) / 8n)
    if (mod(x * x - x2) !== 0n) x = mod(x * SQRT_M1)
    if (mod(x * x - x2) !== 0n) return null
    if (Number(x & 1n) !== sign) x = P - x
    return x
}

const BY = mod(4n * inv(5n))
const BX = recoverX(BY, 0)
const B = [BX, BY, 1n, mod(BX * BY)]

function littleEndian(bytes) {
    let n = 0n
    for (let i = bytes.length - 1; i >= 0; i -= 1)
        n = (n << 8n) | BigInt(bytes[i])
    return n
}

function encode(point) {
    const [x, y, z] = point
    const zi = inv(z)
    const ax = mod(x * zi)
    const ay = mod(y * zi)
    let v = ay | ((ax & 1n) << 255n)
    const out = new Uint8Array(32)
    for (let i = 0; i < 32; i += 1) {
        out[i] = Number(v & 0xffn)
        v >>= 8n
    }
    return out
}

function decode(bytes) {
    if (bytes.length !== 32) return null
    const value = littleEndian(bytes)
    const y = value & ((1n << 255n) - 1n)
    const sign = Number(value >> 255n)
    const x = recoverX(y, sign)
    if (x === null) return null
    return [x, y, 1n, mod(x * y)]
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

/** Check an Ed25519ph signature over a 64-byte SHA-512 digest (empty context). */
export async function verifyPrehashed(publicKey, sha512Digest, signature) {
    if (sha512Digest.length !== 64 || signature.length !== 64) return false
    const pointA = decode(publicKey)
    const pointR = decode(signature.slice(0, 32))
    if (!pointA || !pointR) return false
    const s = littleEndian(signature.slice(32))
    if (s >= L) return false
    const h = new Uint8Array(
        await globalThis.crypto.subtle.digest(
            'SHA-512',
            concat(PH_DOM, signature.slice(0, 32), publicKey, sha512Digest)
        )
    )
    const k = littleEndian(h) % L
    return equal(encode(mul(s, B)), encode(add(pointR, mul(k, pointA))))
}

/**
 * Check a pure Ed25519 signature (RFC 8032 section 5.1.7) over a message. The
 * same equations as above with no prefix and the message itself: for a
 * browser whose WebCrypto has no Ed25519, so the check still runs in the page.
 */
export async function verifyEd25519(publicKey, message, signature) {
    if (publicKey.length !== 32 || signature.length !== 64) return false
    const pointA = decode(publicKey)
    const pointR = decode(signature.slice(0, 32))
    if (!pointA || !pointR) return false
    const s = littleEndian(signature.slice(32))
    if (s >= L) return false
    const h = new Uint8Array(
        await globalThis.crypto.subtle.digest(
            'SHA-512',
            concat(signature.slice(0, 32), publicKey, message)
        )
    )
    const k = littleEndian(h) % L
    return equal(encode(mul(s, B)), encode(add(pointR, mul(k, pointA))))
}
