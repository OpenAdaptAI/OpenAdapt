// The offline Seal verifier against RFC 8032's own test vectors: section 7.1
// (Ed25519, TESTs 1-3) through the pure-JavaScript check and WebCrypto, and
// section 7.3 (Ed25519ph, "abc") through the prehashed check Rekor entries
// use. Then a Seal signed with TEST 1's secret key, checked end to end by
// verifyLocalSeal with WebCrypto Ed25519 and without it.
import { webcrypto } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { verifyEd25519, verifyPrehashed } from '../src/lib/ed25519ph.mjs'
import { signingPayload, verifyLocalSeal } from '../src/lib/localSeal.mjs'

const hex = (h) => Uint8Array.from(h.match(/../g) || [], (b) => parseInt(b, 16))
const b64 = (bytes) => Buffer.from(bytes).toString('base64')
const b64url = (bytes) => Buffer.from(bytes).toString('base64url')

// RFC 8032 section 7.1.
const ED25519 = [
    {
        name: 'TEST 1',
        secret: '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
        publicKey: 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
        message: '',
        signature:
            'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155' +
            '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b',
    },
    {
        name: 'TEST 2',
        publicKey: '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
        message: '72',
        signature:
            '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da' +
            '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00',
    },
    {
        name: 'TEST 3',
        publicKey: 'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
        message: 'af82',
        signature:
            '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac' +
            '18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a',
    },
]

// RFC 8032 section 7.3: Ed25519ph, the message "abc", prehashed with SHA-512.
const ED25519PH = {
    publicKey: 'ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf',
    message: '616263',
    signature:
        '98a70222f0b8121aa9d30f813d683f809e462b469c7ff87639499bb94e6dae41' +
        '31f85042463c2a355a2003d062adf5aaa10b8c61e636062aaad11c2a26083406',
}

const flip = (bytes, at) => {
    const out = bytes.slice()
    out[at] ^= 1
    return out
}

async function webcryptoVerify(publicKey, message, signature) {
    const key = await webcrypto.subtle.importKey('raw', publicKey, { name: 'Ed25519' }, false, ['verify'])
    return webcrypto.subtle.verify({ name: 'Ed25519' }, key, signature, message)
}

describe('RFC 8032 section 7.1, Ed25519', () => {
    for (const v of ED25519) {
        it(`${v.name}: accepted, and refused when any part changes`, async () => {
            const key = hex(v.publicKey)
            const msg = hex(v.message)
            const sig = hex(v.signature)
            expect(await verifyEd25519(key, msg, sig)).toBe(true)
            // WebCrypto agrees, so the vector itself is right.
            expect(await webcryptoVerify(key, msg, sig)).toBe(true)
            expect(await verifyEd25519(key, Uint8Array.from([...msg, 0]), sig)).toBe(false)
            expect(await verifyEd25519(key, msg, flip(sig, 0))).toBe(false)
            expect(await verifyEd25519(key, msg, flip(sig, 63))).toBe(false)
            expect(await verifyEd25519(flip(key, 0), msg, sig)).toBe(false)
        })
    }

    it('refuses a non-canonical S (S + L)', async () => {
        const v = ED25519[0]
        const sig = hex(v.signature)
        const L = 2n ** 252n + 27742317777372353535851937790883648493n
        let s = 0n
        for (let i = 63; i >= 32; i -= 1) s = (s << 8n) | BigInt(sig[i])
        let big = s + L
        const out = sig.slice()
        for (let i = 32; i < 64; i += 1) {
            out[i] = Number(big & 0xffn)
            big >>= 8n
        }
        expect(await verifyEd25519(hex(v.publicKey), hex(v.message), out)).toBe(false)
    })

    it('refuses keys and signatures of the wrong length', async () => {
        const v = ED25519[1]
        expect(await verifyEd25519(hex(v.publicKey).slice(1), hex(v.message), hex(v.signature))).toBe(false)
        expect(await verifyEd25519(hex(v.publicKey), hex(v.message), hex(v.signature).slice(1))).toBe(false)
    })
})

describe('RFC 8032 section 7.3, Ed25519ph', () => {
    it('"abc": accepted over its SHA-512, refused over another digest', async () => {
        const digest = new Uint8Array(await webcrypto.subtle.digest('SHA-512', hex(ED25519PH.message)))
        const key = hex(ED25519PH.publicKey)
        const sig = hex(ED25519PH.signature)
        expect(await verifyPrehashed(key, digest, sig)).toBe(true)
        expect(await verifyPrehashed(key, flip(digest, 0), sig)).toBe(false)
        // A prehashed signature is not a pure Ed25519 signature over the message.
        expect(await verifyEd25519(key, hex(ED25519PH.message), sig)).toBe(false)
    })
})

describe('a Seal signed with TEST 1 secret key', () => {
    const v = ED25519[0]
    const publicKey = hex(v.publicKey)

    async function sealAndRegistry() {
        const privateKey = await webcrypto.subtle.importKey(
            'jwk',
            { kty: 'OKP', crv: 'Ed25519', d: b64url(hex(v.secret)), x: b64url(publicKey) },
            { name: 'Ed25519' },
            false,
            ['sign']
        )
        const keyDigest = new Uint8Array(await webcrypto.subtle.digest('SHA-256', publicKey))
        const keyId = `seal-ed25519-${Buffer.from(keyDigest).toString('hex').slice(0, 16)}`
        const subject = new TextEncoder().encode('{"execution_outcome": "VERIFIED"}\n')
        const subjectSha = Buffer.from(await webcrypto.subtle.digest('SHA-256', subject)).toString('hex')
        const unsigned = {
            algorithm: 'ed25519',
            issued_at: '2026-10-24T00:00:00Z',
            issuer: 'local',
            key_id: keyId,
            outcome: 'VERIFIED',
            public_key: b64(publicKey),
            run_id: 'rfc8032-test-1',
            schema: 'openadapt.local-seal/v1',
            subject_kind: 'run-report',
            subject_sha256: subjectSha,
        }
        const signature = new Uint8Array(
            await webcrypto.subtle.sign({ name: 'Ed25519' }, privateKey, signingPayload(unsigned))
        )
        const seal = { ...unsigned, signature: b64(signature) }
        const registry = [{ key_id: keyId, public_key: b64(publicKey), status: 'active' }]
        return { seal, subject, registry }
    }

    afterEach(() => vi.restoreAllMocks())

    it('verifies with WebCrypto Ed25519', async () => {
        const { seal, subject, registry } = await sealAndRegistry()
        const r = await verifyLocalSeal(seal, subject, registry)
        expect(r).toMatchObject({ ok: true, reason: null })
        expect(r.steps.map((s) => `${s.id}:${s.result}`)).toEqual(['digest:pass', 'key:pass', 'signature:pass'])
    })

    it('verifies with the RFC 8032 equations when WebCrypto has no Ed25519', async () => {
        const { seal, subject, registry } = await sealAndRegistry()
        const importKey = globalThis.crypto.subtle.importKey.bind(globalThis.crypto.subtle)
        const spy = vi.spyOn(globalThis.crypto.subtle, 'importKey').mockImplementation((format, data, alg, ...rest) => {
            if ((alg?.name || alg) === 'Ed25519') return Promise.reject(new Error('NotSupportedError'))
            return importKey(format, data, alg, ...rest)
        })
        const r = await verifyLocalSeal(seal, subject, registry)
        expect(spy).toHaveBeenCalled()
        expect(r).toMatchObject({ ok: true, reason: null })
        const changed = await verifyLocalSeal({ ...seal, outcome: 'HALTED' }, subject, registry)
        expect(changed).toMatchObject({ ok: false, reason: 'bad_signature' })
    })

    it('refuses the Seal over other bytes, and under a key the registry does not pin', async () => {
        const { seal, subject, registry } = await sealAndRegistry()
        expect((await verifyLocalSeal(seal, flip(subject, 0), registry)).reason).toBe('subject_mismatch')
        expect((await verifyLocalSeal(seal, subject, [])).reason).toBe('unknown_key')
    })
})
