// The Sigstore witnesses of a published OpenEMR run's Seal (test/fixtures/openemr-r3):
// its Rekor entry and its RFC 3161 timestamp, checked offline over the Seal file.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { checkReceiptAnchor, checkWitnesses } from '../src/lib/sealWitnesses.mjs'

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'openemr-r3')
const text = (name) => fs.readFileSync(path.join(dir, name), 'utf8')
const keys = JSON.parse(text('site-keys.json'))
const files = () => ({
    sealText: text('seal.json'),
    anchorText: text('seal.anchor.json'),
    tsaText: text('seal.tsa.json'),
    tsrBytes: new Uint8Array(fs.readFileSync(path.join(dir, 'seal.tsr'))),
})

describe('the witnesses', () => {
    it('pass for the published Seal', async () => {
        const { rekor, tsa } = await checkWitnesses(files(), keys)
        expect(rekor).toMatchObject({ ok: true })
        expect(tsa).toMatchObject({ ok: true, matches: true })
    })

    it('fail for a changed Seal', async () => {
        const f = files()
        const seal = JSON.parse(f.sealText)
        const changed = { ...f, sealText: JSON.stringify({ ...seal, outcome: seal.outcome === 'VERIFIED' ? 'HALTED' : 'VERIFIED' }) }
        const { rekor, tsa } = await checkWitnesses(changed, keys)
        expect(rekor.ok).toBe(false)
        expect(tsa).toMatchObject({ ok: false, reason: 'tsa_imprint_mismatch' })
    })

    it('fail when the token file is not the one the record names', async () => {
        const f = files()
        f.tsrBytes[f.tsrBytes.length - 1] ^= 1
        expect((await checkWitnesses(f, keys)).tsa).toMatchObject({ ok: false, reason: 'tsa_token_mismatch' })
        expect((await checkWitnesses({ ...files(), tsrBytes: null }, keys)).tsa.reason).toBe('tsa_token_missing')
    })

    it('rejects malformed or mismatched receipt.anchor.json (#2479)', async () => {
        const vecDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'types', 'tests', 'vectors', 'record-run-v1')
        const vec = JSON.parse(fs.readFileSync(path.join(vecDir, 'cloud-countersign.json'), 'utf8'))
        const registry = JSON.parse(fs.readFileSync(path.join(vecDir, 'registry.json'), 'utf8'))
        const bad = await checkReceiptAnchor(vec.document, '{}', registry)
        expect(bad.ok).toBe(false)
        expect(bad.reason).toBe('anchor_malformed')
        const wrongKey = await checkReceiptAnchor(vec.document, text('seal.anchor.json'), { schema: 'openadapt.receipt-keys/v1', keys: [] })
        expect(wrongKey.ok).toBe(false)
        expect(wrongKey.reason).toBe('anchor_key_mismatch')
    })
})
