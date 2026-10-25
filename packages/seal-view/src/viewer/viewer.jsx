// The offline Seal viewer: one HTML file that shows one run's Seal, with the
// run's files embedded in it (a <script type="application/json" id="seal-data">
// block, written by `openadapt seal view` and the quickstart). It verifies the
// Seal's Ed25519 signature over the embedded report in the page, draws the
// rings and the iris from the same bytes, plays the run over the step table
// and the program map, and lets the reader tamper with a copy. It makes no
// network requests: no fonts, no CDN, no live checks, so it works from file://
// with the network off.
//
// seal-data: { report_b64, seal_text, anchor_text?, tsa_text?, tsr_b64?, auth?, keys,
//              program: { spec, folds?, titles? },
//              about: { app, job, kicker? }, host: { trustWords?, unchanged?, footer? },
//              commands? }
import { createRoot } from 'react-dom/client'
import '@openadapt/program-graph/styles.css'
import SealPage, { sealPageMeta } from '../components/SealPage.jsx'
import { sealPageProps } from '../lib/sealProps.mjs'

const bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))

function Problem({ text }) {
    return (
        <main style={{ font: '16px/1.5 system-ui, sans-serif', color: '#eaf0f6', padding: '48px 24px', maxWidth: 640, margin: '0 auto' }}>
            <h1 style={{ fontSize: 22 }}>This Seal can&apos;t be shown</h1>
            <p>{text}</p>
            <p>
                Check it from a terminal instead: <code>openadapt seal verify RUN_FOLDER</code>
            </p>
        </main>
    )
}

async function main() {
    const root = createRoot(document.getElementById('root'))
    const node = document.getElementById('seal-data')
    let data
    try {
        data = JSON.parse(node?.textContent || 'null')
    } catch {
        data = null
    }
    if (!data || !data.report_b64 || !data.seal_text) {
        root.render(<Problem text="The page has no run in it. Write one with: openadapt seal view RUN_FOLDER" />)
        return
    }
    const files = {
        report: bytes(data.report_b64),
        sealText: data.seal_text,
        anchorText: data.anchor_text ?? null,
        tsaText: data.tsa_text ?? null,
        tsrBytes: data.tsr_b64 ? bytes(data.tsr_b64) : null,
        auth: data.auth ?? null,
    }
    let props
    try {
        props = await sealPageProps({
            files,
            keys: data.keys,
            program: data.program,
            about: data.about || {},
            authorization: data.authorization ?? null,
        })
    } catch (error) {
        root.render(<Problem text={`The run's files don't fit together: ${error.message}.`} />)
        return
    }
    const page = { ...props.page, commands: data.commands ?? null, commandsLabel: data.commands_label ?? null }
    document.title = sealPageMeta(page).title
    const extra = data.host || {}
    root.render(
        <SealPage
            seal={page}
            host={{
                // A fresh copy each time, so a tampered copy never touches the embedded files.
                loadFiles: async () => ({ ...files, report: files.report.slice() }),
                live: false,
                kicker: data.about?.kicker ?? null,
                trustWords: extra.trustWords,
                unchanged: extra.unchanged,
                footer: extra.footer,
                links: {
                    home: null,
                    map: null,
                    file: (name) => (extra.files?.includes(name) ? name : null),
                    keys: null,
                    docs: 'https://docs.openadapt.ai/reference/receipts-and-seals/',
                },
            }}
        />
    )
}

main()
