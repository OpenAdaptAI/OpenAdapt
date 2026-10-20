// Checks an RFC 3161 timestamp token (seal.tsa.json, from Sigstore's
// Timestamp Authority at timestamp.sigstore.dev) in the browser or in Node,
// with WebCrypto, the way `openssl ts -verify` does for one pinned root:
//   1. the token is CMS SignedData over a TSTInfo;
//   2. its signed attributes name that TSTInfo and carry its digest;
//   3. the signer's ECDSA signature over those attributes is valid;
//   4. the signing certificate is issued and signed by the pinned Sigstore TSA
//      root, is for time stamping, and was valid at the time it attests.
// It returns the time the token attests (genTime, read from the token, never
// from the JSON beside it) and the digest it stamps (the message imprint), so
// the page can compare that digest with the Seal's own.

// The Sigstore TSA root (O=sigstore.dev, CN=sigstore-tsa-selfsigned), the same
// file as OpenAdapt's openadapt_flow/countersign/trust/sigstore-tsa-root.pem and
// the last certificate of https://timestamp.sigstore.dev/api/v1/timestamp/certchain.
export const SIGSTORE_TSA_ROOT_PEM = `-----BEGIN CERTIFICATE-----
MIIB9zCCAXygAwIBAgIUV7f0GLDOoEzIh8LXSW80OJiUp14wCgYIKoZIzj0EAwMw
OTEVMBMGA1UEChMMc2lnc3RvcmUuZGV2MSAwHgYDVQQDExdzaWdzdG9yZS10c2Et
c2VsZnNpZ25lZDAeFw0yNTA0MDgwNjU5NDNaFw0zNTA0MDYwNjU5NDNaMDkxFTAT
BgNVBAoTDHNpZ3N0b3JlLmRldjEgMB4GA1UEAxMXc2lnc3RvcmUtdHNhLXNlbGZz
aWduZWQwdjAQBgcqhkjOPQIBBgUrgQQAIgNiAAQUQNtfRT/ou3YATa6wB/kKTe70
cfJwyRIBovMnt8RcJph/COE82uyS6FmppLLL1VBPGcPfpQPYJNXzWwi8icwhKQ6W
/Qe2h3oebBb2FHpwNJDqo+TMaC/tdfkv/ElJB72jRTBDMA4GA1UdDwEB/wQEAwIB
BjASBgNVHRMBAf8ECDAGAQH/AgEAMB0GA1UdDgQWBBSY7AHvf7tR/9SVHm+KiJhT
B4nOvzAKBggqhkjOPQQDAwNpADBmAjEAwGEGrfGZR1cen1R8/DTVMI943LssZmJR
tDp/i7SfGHmGRP6gRbuj9vOK3b67Z0QQAjEAuT2H673LQEaHTcyQSZrkp4mX7Wwk
mF+sVbkYY5mXN+RMH13KUEHHOqASaemYWK/E
-----END CERTIFICATE-----`

export const TSA_SCHEMA = 'openadapt.record-timestamp/v1'

const OID = {
    signedData: '1.2.840.113549.1.7.2',
    tstInfo: '1.2.840.113549.1.9.16.1.4',
    contentType: '1.2.840.113549.1.9.3',
    messageDigest: '1.2.840.113549.1.9.4',
    sha256: '2.16.840.1.101.3.4.2.1',
    sha384: '2.16.840.1.101.3.4.2.2',
    sha512: '2.16.840.1.101.3.4.2.3',
    ecdsaSha256: '1.2.840.10045.4.3.2',
    ecdsaSha384: '1.2.840.10045.4.3.3',
    ecdsaSha512: '1.2.840.10045.4.3.4',
    ecPublicKey: '1.2.840.10045.2.1',
    p256: '1.2.840.10045.3.1.7',
    p384: '1.3.132.0.34',
    extKeyUsage: '2.5.29.37',
    timeStamping: '1.3.6.1.5.5.7.3.8',
}
const HASH = { [OID.sha256]: 'SHA-256', [OID.sha384]: 'SHA-384', [OID.sha512]: 'SHA-512' }
const SIG_HASH = { [OID.ecdsaSha256]: 'SHA-256', [OID.ecdsaSha384]: 'SHA-384', [OID.ecdsaSha512]: 'SHA-512' }
const CURVE = { [OID.p256]: ['P-256', 32], [OID.p384]: ['P-384', 48] }

class Bad extends Error {}
const fail = (why) => {
    throw new Bad(why)
}

// One DER element at `at`: { tag, start (of content), end, raw (whole element) }.
function read(bytes, at) {
    if (at + 2 > bytes.length) fail('truncated')
    const tag = bytes[at]
    if ((tag & 0x1f) === 0x1f) fail('long tag')
    let len = bytes[at + 1]
    let start = at + 2
    if (len & 0x80) {
        const n = len & 0x7f
        if (n === 0 || n > 4) fail('length')
        len = 0
        for (let i = 0; i < n; i += 1) len = len * 256 + bytes[start + i]
        start += n
    }
    const end = start + len
    if (end > bytes.length) fail('truncated')
    return { tag, start, end, bytes, raw: bytes.subarray(at, end), content: bytes.subarray(start, end) }
}
function children(el) {
    const out = []
    let at = el.start
    while (at < el.end) {
        const c = read(el.bytes, at)
        out.push(c)
        at = c.end
    }
    if (at !== el.end) fail('overrun')
    return out
}
const expect = (el, tag) => (el && el.tag === tag ? el : fail(`tag ${tag}`))
function oid(el) {
    expect(el, 0x06)
    const b = el.content
    const parts = [Math.floor(b[0] / 40), b[0] % 40]
    let v = 0
    for (let i = 1; i < b.length; i += 1) {
        v = v * 128 + (b[i] & 0x7f)
        if (!(b[i] & 0x80)) {
            parts.push(v)
            v = 0
        }
    }
    return parts.join('.')
}
const text = (el) => new TextDecoder().decode(el.content)
// GeneralizedTime (YYYYMMDDHHMMSS[.fff]Z) or UTCTime (YYMMDDHHMMSSZ), as an ISO string.
function time(el) {
    const t = text(el)
    const m =
        el.tag === 0x18
            ? /^(\d{4})(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\.\d+)?Z$/.exec(t)
            : el.tag === 0x17
              ? /^(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)()Z$/.exec(t)
              : null
    if (!m) fail('time')
    const year = el.tag === 0x17 ? (Number(m[1]) < 50 ? 2000 : 1900) + Number(m[1]) : Number(m[1])
    return `${String(year).padStart(4, '0')}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}${m[7] || ''}Z`
}
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
const equal = (a, b) => a.length === b.length && a.every((x, i) => x === b[i])

export function pemBytes(pem) {
    const b64 = String(pem).replace(/-----[^-]+-----/g, '').replace(/\s+/g, '')
    const bin = atob(b64)
    return Uint8Array.from(bin, (c) => c.charCodeAt(0))
}

// An X.509 certificate's parts this check needs.
function certificate(el) {
    const [tbs, sigAlg, sig] = children(expect(el, 0x30))
    const f = children(expect(tbs, 0x30))
    const off = f[0].tag === 0xa0 ? 1 : 0
    const issuer = f[off + 2]
    const validity = children(f[off + 3])
    const subject = f[off + 4]
    const spki = expect(f[off + 5], 0x30)
    const [alg] = children(spki)
    const [algOid, curve] = children(alg)
    let timestamping = false
    for (const x of f.slice(off + 6)) {
        if (x.tag !== 0xa3) continue
        for (const ext of children(children(x)[0])) {
            const parts = children(ext)
            if (oid(parts[0]) !== OID.extKeyUsage) continue
            const usages = children(read(parts.at(-1).content, 0))
            timestamping = usages.some((u) => oid(u) === OID.timeStamping)
        }
    }
    return {
        tbs: tbs.raw,
        sigAlg: oid(children(sigAlg)[0]),
        signature: expect(sig, 0x03).content.subarray(1),
        issuer: issuer.raw,
        subject: subject.raw,
        notBefore: time(validity[0]),
        notAfter: time(validity[1]),
        spki: spki.raw,
        keyAlg: oid(algOid),
        curve: curve && curve.tag === 0x06 ? oid(curve) : null,
        timestamping,
        serial: f[off].content,
    }
}

// ECDSA-Sig-Value (DER) to the raw r || s WebCrypto verifies.
function rawSignature(der, size) {
    const [r, s] = children(expect(read(der, 0), 0x30))
    const fix = (n) => {
        let b = n.content
        while (b.length > size && b[0] === 0) b = b.subarray(1)
        if (b.length > size) fail('signature')
        const out = new Uint8Array(size)
        out.set(b, size - b.length)
        return out
    }
    const out = new Uint8Array(size * 2)
    out.set(fix(r), 0)
    out.set(fix(s), size)
    return out
}

async function ecdsaVerify(cert, sigAlg, signature, data) {
    const hash = SIG_HASH[sigAlg]
    const curve = CURVE[cert.curve]
    if (!hash || !curve || cert.keyAlg !== OID.ecPublicKey) fail('algorithm')
    const subtle = globalThis.crypto.subtle
    const key = await subtle.importKey('spki', cert.spki, { name: 'ECDSA', namedCurve: curve[0] }, false, ['verify'])
    return subtle.verify({ name: 'ECDSA', hash }, key, rawSignature(signature, curve[1]), data)
}

/** The token's fields, without checking it. Throws on a malformed token. */
export function parseTimestampToken(der) {
    const top = children(expect(read(der, 0), 0x30))
    if (oid(top[0]) !== OID.signedData) fail('not signed data')
    const sd = children(expect(children(expect(top[1], 0xa0))[0], 0x30))
    const encap = children(expect(sd[2], 0x30))
    if (oid(encap[0]) !== OID.tstInfo) fail('not a timestamp')
    const eContent = expect(children(expect(encap[1], 0xa0))[0], 0x04).content
    let i = 3
    const certs = []
    if (sd[i] && sd[i].tag === 0xa0) {
        for (const c of children(sd[i])) certs.push(certificate(c))
        i += 1
    }
    if (sd[i] && sd[i].tag === 0xa1) i += 1
    const signers = children(expect(sd[i], 0x31))
    if (signers.length !== 1) fail('signers')
    const si = children(expect(signers[0], 0x30))
    const sid = si[1]
    const digestAlg = oid(children(si[2])[0])
    const attrs = expect(si[3], 0xa0)
    const sigAlg = oid(children(si[4])[0])
    const signature = expect(si[5], 0x04).content
    const attr = {}
    for (const a of children(attrs)) {
        const [t, values] = children(a)
        attr[oid(t)] = children(values)[0]
    }
    const tst = children(expect(read(eContent, 0), 0x30))
    const [imprintAlg, imprint] = children(expect(tst[2], 0x30))
    const serialOf = sid.tag === 0x30 ? children(sid)[1].content : null
    // The signed attributes are signed as a SET (tag 0x31), not as [0].
    const signedBytes = Uint8Array.from(attrs.raw)
    signedBytes[0] = 0x31
    return {
        eContent,
        certs,
        signerSerial: serialOf,
        digestAlg,
        sigAlg,
        signature,
        signedBytes,
        contentType: attr[OID.contentType] ? oid(attr[OID.contentType]) : null,
        messageDigest: attr[OID.messageDigest] ? expect(attr[OID.messageDigest], 0x04).content : null,
        policy: oid(tst[1]),
        imprintAlg: oid(children(imprintAlg)[0]),
        imprint: hex(expect(imprint, 0x04).content),
        genTime: time(tst[4]),
    }
}

/**
 * Check a token (DER bytes) against the pinned root. Returns
 * { ok, reason, genTime, imprint, imprintAlg }; reason is one of
 * tsa_malformed, tsa_content_mismatch, tsa_bad_signature, tsa_untrusted,
 * tsa_expired, tsa_not_timestamping.
 */
export async function verifyTimestampToken(der, rootPem = SIGSTORE_TSA_ROOT_PEM) {
    let t
    try {
        t = parseTimestampToken(der)
    } catch {
        return { ok: false, reason: 'tsa_malformed' }
    }
    const seen = { genTime: t.genTime, imprint: t.imprint, imprintAlg: HASH[t.imprintAlg] || t.imprintAlg }
    const done = (ok, reason) => ({ ok, reason, ...seen })
    try {
        const hash = HASH[t.digestAlg]
        if (!hash || t.contentType !== OID.tstInfo || !t.messageDigest) return done(false, 'tsa_content_mismatch')
        const digest = new Uint8Array(await globalThis.crypto.subtle.digest(hash, t.eContent))
        if (!equal(digest, t.messageDigest)) return done(false, 'tsa_content_mismatch')
        const root = certificate(read(pemBytes(rootPem), 0))
        const leaf =
            t.certs.find((c) => t.signerSerial && equal(c.serial, t.signerSerial)) ||
            (t.certs.length === 1 ? t.certs[0] : null)
        if (!leaf) return done(false, 'tsa_untrusted')
        if (!(await ecdsaVerify(leaf, t.sigAlg, t.signature, t.signedBytes))) return done(false, 'tsa_bad_signature')
        if (!equal(leaf.issuer, root.subject)) return done(false, 'tsa_untrusted')
        if (!(await ecdsaVerify(root, leaf.sigAlg, leaf.signature, leaf.tbs))) return done(false, 'tsa_untrusted')
        if (!leaf.timestamping) return done(false, 'tsa_not_timestamping')
        const at = Date.parse(t.genTime)
        for (const c of [leaf, root])
            if (!(Date.parse(c.notBefore) <= at && at <= Date.parse(c.notAfter))) return done(false, 'tsa_expired')
        return done(true, null)
    } catch (e) {
        return done(false, e instanceof Bad ? 'tsa_malformed' : 'tsa_bad_signature')
    }
}

/**
 * Check seal.tsa.json's text for one Seal: the token verifies, and the digest
 * it stamps is the Seal's own SHA-256 (sealHex, computed by the caller from
 * the Seal file). Adds `matches` and `reason` tsa_imprint_mismatch.
 */
export async function verifySealTimestamp(tsaText, sealHex) {
    let file
    try {
        file = JSON.parse(tsaText)
    } catch {
        return { ok: false, reason: 'tsa_malformed' }
    }
    if (!file || file.schema !== TSA_SCHEMA || typeof file.token !== 'string') return { ok: false, reason: 'tsa_malformed' }
    let der
    try {
        der = pemBytes(file.token)
    } catch {
        return { ok: false, reason: 'tsa_malformed' }
    }
    const r = await verifyTimestampToken(der)
    const matches = r.imprintAlg === 'SHA-256' && r.imprint === sealHex
    if (r.ok && !matches) return { ...r, ok: false, reason: 'tsa_imprint_mismatch', matches }
    return { ...r, matches }
}
