# openadapt-panel

A local web **control panel** for [OpenAdapt](https://openadapt.ai) — a browser
UI over the `openadapt` record → train → evaluate CLI, so the pipeline can be
driven without the terminal.

```bash
pip install "openadapt[panel]"
openadapt panel                     # opens http://127.0.0.1:8080
openadapt panel --port 9000 --no-open
```

## Run it like a desktop app

```bash
openadapt panel --install-shortcut  # adds an "OpenAdapt Panel" icon to your Desktop (Windows)
openadapt panel --app               # what the shortcut runs: tray app, no console window
```

Double-clicking the Desktop icon launches the panel via `pythonw` (no console),
opens your browser, and leaves an icon in the system tray with **Open Control
Panel** / **Quit**. It's still the same local web app — just tray-resident and
click-to-launch, with no native toolchain (pure Python via `pystray`/`pillow`).
The machine still needs Python + `openadapt[panel]` installed.

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
- **Frontend ships in the wheel** — the built React assets in
  `openadapt_panel/static/` are committed and bundled into the wheel, so the end
  user never needs Node.

## Developing the frontend

The UI is a React + Vite app under `frontend/`. Vite builds straight into
`openadapt_panel/static/` (`base: "/static/"`, matching the FastAPI mount); the
built `index.html` + `assets/` are committed so `pip install` / CI never touch
Node.

```bash
cd frontend
npm install
npm run build      # → openadapt_panel/static/  (commit the result)
npm run dev        # optional: Vite dev server (proxy /api to a running panel)
```

After changing anything under `frontend/src/`, run `npm run build` and commit the
regenerated `static/` output alongside the source.
