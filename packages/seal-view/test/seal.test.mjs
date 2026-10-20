// The checks and the rings over a real quickstart run's files
// (tests/fixtures/seal_view at the repository root).
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { verifyEd25519 } from '../src/lib/ed25519ph.mjs'
import { base64Bytes, signingPayload, verifySealFile } from '../src/lib/localSeal.mjs'
import { checkReceipt } from '../src/lib/receiptCheck.mjs'
import { sealPageProps } from '../src/lib/sealProps.mjs'
import { programOf, sealRings } from '../src/lib/sealRings.mjs'
import { replayFrames } from '../src/lib/sealReplay.mjs'
import { applyTamper } from '../src/lib/sealTamper.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const fixtures = path.join(here, '..', '..', '..', 'tests', 'fixtures', 'seal_view')
const read = (...p) => fs.readFileSync(path.join(fixtures, ...p))
const keys = JSON.parse(read('keys.json'))
const spec = JSON.parse(read('program.json'))
const files = (run) => ({ report: new Uint8Array(read(run, 'report.json')), sealText: read(run, 'seal.json').toString('utf8') })

describe('the Seal check', () => {
    it('verifies the quickstart runs against the key that signed them', async () => {
        for (const run of ['run-1', 'run-broken']) {
            const r = await checkReceipt(files(run), keys)
            expect(r.ok, run).toBe(true)
            expect(r.steps.map((s) => s.result)).toEqual(['pass', 'pass', 'pass'])
        }
    })

    it('refuses a changed report, a changed Seal and an unknown key', async () => {
        const f = files('run-1')
        const option = { target: 'report', text: ['"model_calls": 0', '"model_calls": 1'] }
        expect((await checkReceipt(applyTamper(f, option), keys)).reason).toBe('subject_mismatch')
        const seal = { target: 'seal', field: 'outcome', from: 'VERIFIED', to: 'HALTED' }
        expect((await checkReceipt(applyTamper(f, seal), keys)).reason).toBe('bad_signature')
        expect((await checkReceipt(f, [])).reason).toBe('unknown_key')
    })

    it('checks Ed25519 without WebCrypto Ed25519, with the same answer', async () => {
        const seal = JSON.parse(files('run-1').sealText)
        const key = base64Bytes(seal.public_key)
        const sig = base64Bytes(seal.signature)
        expect(await verifyEd25519(key, signingPayload(seal), sig)).toBe(true)
        expect(await verifyEd25519(key, signingPayload({ ...seal, outcome: 'HALTED' }), sig)).toBe(false)
    })

    it('refuses a Seal file with a repeated field', async () => {
        const f = files('run-1')
        const text = f.sealText.replace('"outcome": "VERIFIED",', '"outcome": "VERIFIED",\n  "outcome": "HALTED",')
        expect((await verifySealFile(text, f.report, keys)).reason).toBe('malformed')
    })
})

describe('the rings', () => {
    it('read the saved run: six steps, the save read back twice', () => {
        const report = JSON.parse(read('run-1', 'report.json'))
        const rings = sealRings(report, programOf(spec, { report }))
        expect(rings.steps.map((s) => s.name)).toEqual([
            'Click Open',
            'Click New Encounter',
            'Click Triage',
            'Click the next field',
            'Type Note',
            'Click Save Encounter',
        ])
        expect(rings.counts).toMatchObject({ stepsPassed: 6, checks: 9, readbacks: 2, readbacksPassed: 2, identity: 5 })
        expect(rings.readbacks.map((r) => r.label)).toEqual(['The new record exists', 'Note matches'])
        expect(rings.anchor).toEqual({ step: 5, kind: 'save' })
    })

    it('read the stopped run: the save refuted, the Seal at the stop', () => {
        const report = JSON.parse(read('run-broken', 'report.json'))
        const rings = sealRings(report, programOf(spec, { report }))
        expect(rings.stoppedAt).toBe(5)
        expect(rings.counts).toMatchObject({ readbacks: 1, readbacksPassed: 0, readbacksFailed: 1 })
        expect(rings.anchor).toEqual({ step: 5, kind: 'stop' })
        expect(replayFrames(rings).at(-1).stage).toBe('stop')
    })

    it('refuse a report from another program', () => {
        const report = JSON.parse(read('run-1', 'report.json'))
        expect(() => sealRings(report, programOf(spec, { report, digest: '0'.repeat(64) }))).toThrow(/different program/)
    })
})

describe('the page props', () => {
    it('say what the run did, from its files', async () => {
        const { page } = await sealPageProps({
            files: files('run-broken'),
            keys,
            program: { spec },
            about: { app: 'MockMed', job: 'Add a triage note' },
        })
        expect(page).toMatchObject({
            tone: 'halt',
            label: 'Stopped for a person to check',
            stopReason: 'the save never reached the record',
            job: 'Add a triage note',
        })
        expect(page.irisSeed).toMatch(/^[a-f0-9]{64}$/)
        expect(page.tamper.map((t) => t.id)).toEqual(['report-outcome', 'report-model-calls', 'seal-outcome'])
    })
})
