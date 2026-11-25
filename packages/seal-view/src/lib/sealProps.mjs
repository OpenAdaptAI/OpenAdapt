// One run's Seal page props (components/SealPage.jsx), from that run's files
// as data, in the browser or in Node: the exact bytes of report.json, the
// exact text of seal.json (and of seal.anchor.json and seal.tsa.json when the
// run has them), the pinned Seal keys, and the program graph of the run's
// bundle. Nothing is fetched. openadapt.ai builds the same props at build
// time; the offline viewer (src/viewer) builds them in the page from the
// files embedded in it.
//
// Everything the page draws comes from these files: names of steps and fields,
// counts, outcomes, times and the signed fields. No typed values, screen text
// or patient data are passed to the page.
import { base64Bytes, canonicalJson, sha256Hex } from './localSeal.mjs'
import { programOf, sealRings } from './sealRings.mjs'
import { sealSummary } from './sealSummary.mjs'
import { tamperOptions } from './sealTamper.mjs'

const utf8 = (bytes) => new TextDecoder().decode(bytes)
const hexOf = async (text) => sha256Hex(new TextEncoder().encode(text))

function parseReceiptRecord(receiptText) {
    const envelope = JSON.parse(receiptText)
    const statement = JSON.parse(utf8(base64Bytes(envelope.payload)))
    const pred = statement.predicate || {}
    const reportSub = (statement.subject || []).find((s) => s?.name === 'report.json')
    const sigs = Array.isArray(envelope.signatures) ? envelope.signatures : []
    return {
        envelope,
        record: {
            outcome: pred.outcome ?? '',
            subject_sha256: reportSub?.digest?.sha256 ?? '',
            key_id: sigs.map((s) => s?.keyid).filter(Boolean).join(', '),
            signature: sigs[0]?.sig ?? '',
            started_hour: pred.started_hour ?? null,
            program_id: pred.program?.id ?? null,
            schema: 'record/run/v1',
        },
    }
}

/**
 * The iris's seed: SHA-256 of the report's digest followed by the Seal's own
 * digest (canonical JSON, signature included). A change to either file
 * changes the picture. The page computes the same over a changed copy.
 */
export const irisSeed = async (seal) => hexOf(seal.subject_sha256 + (await hexOf(canonicalJson(seal))))

/**
 * files: { report: Uint8Array, sealText?, receiptText?, receiptAnchorText?, anchorText?, tsaText?, tsrBytes?, auth? }
 *   (auth: { statement, sig, signers, anchorText? }, the run authorization's texts)
 * program: { spec, folds?, titles?, digest? } (programOf)
 * about: { id?, app, job, startedAt? } (what to call the run)
 * Returns { page, files }: the page's props, and the files as the check reads them.
 */
export async function sealPageProps({ files, keys, program, about = {}, authorization = null }) {
    const seal = files.sealText != null ? JSON.parse(files.sealText) : null
    const parsedReceipt = files.receiptText != null ? parseReceiptRecord(files.receiptText) : null
    const receiptRecord = parsedReceipt?.record ?? null
    if (!seal && !receiptRecord) throw new Error('the run has no seal or receipt')
    const primary = seal || receiptRecord
    const report = JSON.parse(utf8(files.report))
    const steps = programOf(program.spec, {
        folds: program.folds || null,
        titles: program.titles || null,
        digest: program.digest || null,
        report,
    })
    const rings = sealRings(report, steps)
    const summary = sealSummary(report, receiptRecord || seal, rings)
    const seed = seal
        ? await irisSeed(seal)
        : await hexOf(receiptRecord.subject_sha256 + (await hexOf(canonicalJson(parsedReceipt.envelope))))
    return {
        files,
        page: {
            id: about.id || seal?.run_id || report.run_id || '',
            tone: summary.tone,
            label: summary.label,
            headline: summary.headline,
            line: summary.line,
            job: about.job || report.workflow_name || 'Run',
            app: about.app || 'Test copy',
            startedAt: report.started_at ?? null,
            stopReason: summary.reason,
            stopNote: summary.note,
            rings,
            irisSeed: seed,
            media: rings.steps.map(() => ({ crop: null, frame: null })),
            seal: primary,
            receiptRecord,
            keys,
            files: {
                report: 'report.json',
                seal: files.sealText != null ? 'seal.json' : null,
                receipt: files.receiptText != null ? 'receipt.json' : null,
                anchor: files.anchorText != null ? 'seal.anchor.json' : null,
                receiptAnchor: files.receiptAnchorText != null ? 'receipt.anchor.json' : null,
                tsa: files.tsaText != null ? 'seal.tsa.json' : null,
                tsr: files.tsrBytes != null ? 'seal.tsr' : null,
            },
            anchor: null,
            authorization,
            authTiming: null,
            authorizedLine: null,
            tamper: tamperOptions(primary.outcome, report.execution_outcome),
            program: {
                spec: program.spec,
                folds: program.folds || null,
                titles: Object.fromEntries(steps.steps.map((s) => [s.id, s.name])),
            },
            runs: [],
        },
    }
}
