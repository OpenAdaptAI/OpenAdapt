# @openadapt/seal-view

One run's Seal as a React component, and an offline viewer built from it. Give
it a run's `report.json`, its `seal.json` and the Seal keys you trust, and it
checks the Ed25519 signature over the exact report in the browser, draws the
Seal from what the report recorded, plays the run back over the step table and
the program map, and lets a reader change a copy of either file to watch the
check fail.

openadapt.ai's Seal pages and `openadapt quickstart` use the same code.

## What it shows

- **The Seal.** An arc per program step, a tick per screen check, a dot per
  read of the saved record, a short arc per right-record check, and an iris
  drawn from the report's SHA-256 and the Seal's own digest. A change to
  either file changes the iris.
- **The check.** The report's SHA-256 computed in the browser against the one
  the Seal signed, then the signature against the pinned key. A failure names
  its reason and code (`subject_mismatch`, `bad_signature`, `unknown_key` and
  the others `openadapt-flow local-seal verify` uses).
- **The replay.** One frame index drives the ring, the step line, the step
  table, the read-back table and the full program workbench
  ([`@openadapt/program-graph`](../program-graph/README.md)): sections,
  inspector, evidence lanes, stop rules and a tour. While the inspector shows
  the step the Seal certifies (the save, or the stop), a card at its top says
  so and goes back up to the Seal.
- **Tamper with it.** Change the outcome or the AI call count in a copy of the
  report, or the outcome in a copy of the Seal, and check the copy.
- **Witnesses.** A Sigstore Rekor entry (`seal.anchor.json`) and an RFC 3161
  timestamp (`seal.tsa.json` naming its token file `seal.tsr`) are checked
  offline and shown only when the run has them.

## Use it

```jsx
import { SealPage, sealPageProps } from '@openadapt/seal-view'
import '@openadapt/program-graph/styles.css'
import '@openadapt/seal-view/styles.css'

const files = { report: reportBytes, sealText }  // exact bytes and text
const { page } = await sealPageProps({ files, keys, program: { spec } })

<SealPage seal={page} host={{ loadFiles: async () => files, live: false }} />
```

`spec` is the program graph of the run's bundle (`openadapt-flow visualize
--format json`). `host` holds the parts that differ between hosts: `Link`,
`links`, `map` (`kicker`, `title`, `resolveMedia` for the workbench),
`copyHref`, `kicker`, `footer`, `trustWords`, `live` (Rekor's live entry
and GitHub's key list, default on) and `loadFiles` (default: fetch the paths in
`seal.files`). `sealPageMeta(page)` gives the title and description for the
host's `<head>`.

## The offline viewer

`node scripts/build.mjs` writes `dist/seal-viewer.html` and copies it to
`openadapt/seal_view/viewer.html`, which the launcher ships. It is one file:
the viewer and the program map built with Preact's React-compatible runtime,
the styles inlined, and a `<script type="application/json" id="seal-data">`
slot that `openadapt seal view RUN_DIR` fills with the run's files. Its
Content-Security-Policy allows no requests, so it works from `file://` with
the network off and uploads nothing.

Size, measured on 2026-10-24: 212,474 bytes (68,660 gzipped). The budget is
262,144 bytes (`VIEWER_BUDGET` in `scripts/build.mjs`, and
`tests/snapshots/seal_viewer_size.json` at the repository root);
`node scripts/build.mjs --check` fails over it or when the launcher's copy is
not this build.

## Develop

```bash
npm ci --ignore-scripts
npm test
node scripts/build.mjs
```

The browser tests are in the launcher's suite: `tests/test_seal_view.py` opens
a real quickstart run's Seal pages from `file://` in Chromium and records
every request.

## License

MIT. Extracted from openadapt.ai's Seal page.
