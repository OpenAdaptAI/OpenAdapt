# Feeding a layered clinical-data platform (L1 integration)

Layered clinical research platforms separate **acquisition** (L1: get artifacts out of source
systems) from **standardization** (L2: parse artifacts into a research-grade
common data model) and **federation** (L3). The L1→L2 seam is deliberately
thin: *an on-disk file under a resolved extraction directory, plus manifest
metadata* (`{file_number}_{date}_{doctype}` filename or a CSV sidecar).

openadapt-flow targets the L1 role: instead of hand-building a bespoke
acquisition harness per site (per-EMR navigation code, per-screen detection,
custom recovery logic), a site operator **records the workflow once** and the
compiler produces the deterministic, self-healing script that acquires the
artifacts on every subsequent run — locally, with no per-run model calls on
the happy path.

## Current product path

- **Record → compile → replay → governed repair** end-to-end, vision-only (PNG in,
  clicks/keys out), validated against a mock EMR-like app including four
  drift scenarios (theme, layout move, label rename, unexpected modal) in CI.
- **Postconditions per step** derived from the recording (what actually
  changed on screen), so unattended runs verify progress instead of assuming
  it; semantic drift halts the run with an illustrated report rather than
  guessing.
- **Risk gate**: steps tagged irreversible refuse to act on low-confidence
  resolutions.
- **L1 artifact emission** (`openadapt_flow.emit.l1_artifact.emit_l1_artifact`):
  writes a workflow output file into an extraction directory under the
  canonical `{file_number}_{date}_{doctype}` name, appends a `manifest.csv`
  row, and drops a JSON provenance envelope (sha256, session, tool, version,
  captured_at) alongside — idempotent on identical re-emits, loud on content
  conflicts.

```python
from openadapt_flow.emit.l1_artifact import emit_l1_artifact

ref = emit_l1_artifact(
    downloaded_pdf,                # produced by a replayed workflow step
    extraction_dir,                # the root the L2 layer watches
    file_number="P1",
    date="2026-07-06",
    doctype="referral",
    session_id=run_report.workflow_name,
)
```

## Desktop and remote deployment

The compiler and governed runtime are shared across browser, Windows UIA,
native macOS, and RDP. The substrate driver supplies observations and action
delivery; OpenAdapt remains responsible for target uniqueness, identity,
policy, postconditions, independent effects, repair, and audit.

- **Windows UIA:** a fixed WinForms workflow passed 3/3 trials with
  independently confirmed SQLite effects. Native UIA actions produced 12
  delivery receipts, while stale and ambiguous targets each refused 3/3.
- **Native macOS:** a fixed TextEdit workflow passed 3/3 exact file-byte
  effect checks and refused a two-window ambiguous selector without modifying
  either file.
- **RDP:** real Aardwolf RDP into Windows 11 passed 3/3 trials for a fixed
  remote-input task, with exact file readback through an independent guest-tools
  oracle and no model calls. See [`backends/RDP.md`](backends/RDP.md).

These accepted tasks establish working substrate paths. A clinical deployment
qualifies the exact EMR, OS/session policy, display conditions, identity rules,
and system-of-record effect oracle before supervised production writes. Citrix
ICA/HDX follows the same adapter contract but requires qualification in the
customer's published application; RDP evidence is not treated as Citrix
evidence.

For artifact acquisition, downloaded files should remain the primary extraction
path. When data must be read from a screen, use structured/native value access
where available, with clipboard or OCR observations verified against the
workflow's declared postcondition or external effect.

## Trying it

```bash
pip install -e '.[dev]' && playwright install chromium
pytest -q                        # full suite incl. the drift/heal E2E matrix
openadapt-flow demo-record --out /tmp/rec
openadapt-flow compile /tmp/rec --out /tmp/bundle --name demo
openadapt-flow bench /tmp/bundle --n 3 --run-root /tmp/bench
```

Then read `/tmp/bench/BENCH.md` and the per-run `REPORT.md` — the same
artifacts a site operator would review after an unattended run.
