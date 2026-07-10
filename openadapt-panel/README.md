# openadapt-panel

A local web **control panel** for [OpenAdapt](https://openadapt.ai) — a browser
UI over the `openadapt` record → train → evaluate CLI, so the pipeline can be
driven without the terminal.

```bash
pip install "openadapt[panel]"
openadapt panel                     # opens http://127.0.0.1:8080
openadapt panel --port 9000 --no-open
```

## What it is

A FastAPI backend serving a single-page frontend. The backend is a **seam, not a
second implementation**: each route calls the exact sibling function the
corresponding CLI command already calls (`openadapt_ml.scripts.train.main`,
`openadapt_capture.Recorder`, …).

## Status — P0 (shell)

This release ships the **Dashboard / System** page only: a read-only view of
Python/platform, installed ecosystem packages + versions, GPU, and API-key
presence. It boots with **zero siblings installed** and imports no sibling
package (state is inspected via `importlib.metadata` / `find_spec`, mirroring
`openadapt doctor`).

Planned: P1 Captures + Eval · P2 Train + capture long-running ops · P3 Settings,
loopback token auth, packaging.

## Rules honored (from the meta-package)

- **Lazy sibling imports** — inside route handlers only, so the app boots
  headless.
- **System page never imports siblings** — `importlib` metadata only.
- **Loopback only** — binds `127.0.0.1`; per-session token auth lands in P3.
- **Frontend ships built** — the wheel bundles static assets so the end user
  never needs Node. (P0 uses a hand-written static page; a React build replaces
  it later, emitted to `openadapt_panel/static/` at package-build time.)
