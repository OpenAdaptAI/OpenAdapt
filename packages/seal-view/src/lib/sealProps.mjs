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
import { canonicalJson, sha256Hex } from './localSeal.mjs'
import { programOf, sealRings } from './sealRings.mjs'
import { sealSummary } from './sealSummary.mjs'
import { tamperOptions } from './sealTamper.mjs'

const utf8 = (bytes) => new TextDecoder().decode(bytes)
const hexOf = async (text) => sha256Hex(new TextEncoder().encode(text))

/**
 * The iris's seed: SHA-256 of the report's digest followed by the Seal's own
 * digest (canonical JSON, signature included). A change to either file
 * changes the picture. The page computes the same over a changed copy.
 */
export const irisSeed = async (seal) => hexOf(seal.subject_sha256 + (await hexOf(canonicalJson(seal))))

/**
 * files: { report: Uint8Array, sealText, anchorText?, tsaText?, tsrBytes?, auth? }
 *   (auth: { statement, sig, signers, anchorText? }, the run authorization's texts)
 * program: { spec, folds?, titles?, digest? } (programOf)
 * about: { id?, app, job, startedAt? } (what to call the run)
 * Returns { page, files }: the page's props, and the files as the check reads them.
 */
export async function sealPageProps({ files, keys, program, about = {}, authorization = null }) {
    const seal = JSON.parse(files.sealText)
    const report = JSON.parse(utf8(files.report))
    const steps = programOf(program.spec, {
        folds: program.folds || null,
        titles: program.titles || null,
        digest: program.digest || null,
        report,
    })
    const rings = sealRings(report, steps)
    const summary = sealSummary(report, seal, rings)
    return {
        files,
        page: {
            id: about.id || seal.run_id,
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
            irisSeed: await irisSeed(seal),
            media: rings.steps.map(() => ({ crop: null, frame: null })),
            seal,
            keys,
            files: {
                report: 'report.json',
                seal: 'seal.json',
                anchor: files.anchorText != null ? 'seal.anchor.json' : null,
                tsa: files.tsaText != null ? 'seal.tsa.json' : null,
                tsr: files.tsrBytes != null ? 'seal.tsr' : null,
            },
            anchor: null,
            authorization,
            authTiming: null,
            authorizedLine: null,
            tamper: tamperOptions(seal.outcome, report.execution_outcome),
            program: {
                spec: program.spec,
                folds: program.folds || null,
                titles: Object.fromEntries(steps.steps.map((s) => [s.id, s.name])),
            },
            runs: [],
        },
    }
}
