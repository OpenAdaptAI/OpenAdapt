// The shared record/run/v1 vectors (packages/types/tests/vectors/record-run-v1),
// checked by the viewer's verifier: the same answers as the Python verifier.
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from 'vitest'
import { verifyReceipt } from '../src/lib/receiptRecord.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const dir = join(here, '..', '..', 'types', 'tests', 'vectors', 'record-run-v1')
const read = (name) => JSON.parse(readFileSync(join(dir, name), 'utf8'))
const registry = read('registry.json')
const names = readdirSync(dir).filter((n) => n.endsWith('.json') && !n.startsWith('registry') && n !== 'jcs.json' && !n.startsWith('chain-'))

test('the vector set is there', () => {
    expect(names.length).toBeGreaterThanOrEqual(30)
})

for (const name of names) {
    test(`vector ${name}`, async () => {
        const v = read(name)
        if ('received' in v) return // the handoff-bytes check is the receiver's, not the viewer's
        const got = await verifyReceipt(v.document, registry, { openings: v.openings ?? null })
        const e = v.expected
        expect(got.ok, got.detail).toBe(e.ok)
        expect(got.reason).toBe(e.reason)
        expect(got.signerLines).toEqual(e.signer_lines)
        if (e.ok) {
            expect(got.outcome).toBe(e.outcome)
            expect(got.demo).toBe(e.demo)
            expect(got.disclosed).toEqual(e.disclosed)
            expect(got.confirmationLines).toEqual(e.confirmation_lines ?? [])
            expect(got.independent).toEqual(e.independent ?? [])
            if (e.approval_line) expect(got.approvalLine).toBe(e.approval_line)
        }
    })
}
