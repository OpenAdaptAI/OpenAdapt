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

## Pages

| Page | What it does | Seam |
|------|--------------|------|
| **Dashboard** | Read-only system/ecosystem status (Python, GPU, installed packages, API-key presence). Imports no sibling — `importlib.metadata`/`find_spec` only, mirroring `openadapt doctor`. | — |
| **Captures** | List recordings, generate + embed the HTML viewer, start a recording. | `openadapt_capture.Capture` / `create_html`; recording via CLI subprocess |
| **Train** | Start a run, live log + epoch/loss progress, stop cleanly. | `openadapt train start` subprocess; `training_log.json`; `STOP_TRAINING` sentinel |
| **Eval** | Mock or live/checkpoint benchmark, shows success rate / avg steps. | `openadapt_evals` (SmartMockAgent/ApiAgent/PolicyAgent, WAA adapters) |
| **Models** | Browse trained checkpoints on disk. | filesystem (`model_cache_dir`) |
| **Settings** | View effective config; edit API keys / dirs / device / ports; persisted to `.env`. | `.env` + `openadapt.config` schema |

Long-running ops (capture, train) run as background jobs and stream logs to the
UI over SSE; capture/train reuse the `openadapt` CLI as a subprocess so the panel
can't drift from the tested CLI seam. Eval runs in a worker thread (it needs
structured metrics back).

## Rules honored (from the meta-package)

- **Lazy sibling imports** — inside route handlers only, so the app boots
  headless.
- **System page never imports siblings** — `importlib` metadata only.
- **Loopback + token** — binds `127.0.0.1` and gates every `/api` route behind a
  per-session token handed to the browser via the launch URL.
- **Frontend ships in the wheel** — static assets are bundled so the end user
  never needs Node. (Currently a hand-written vanilla-JS SPA; a React build can
  replace it later, emitted to `openadapt_panel/static/` at package-build time.)
