# OpenAdapt Control Panel — Blueprint

> Direction (2026-07-10): continue with **modern OpenAdapt** (the record → train → evaluate
> pipeline) and give it a **local web control panel** so it can be used without the terminal.
> The Miso Replay record-and-replay product was shelved (archived on branch
> `feature/add-miso-replay`).

---

## 1. Goal

OpenAdapt today is CLI-only (`openadapt capture … / train … / eval …`). Powerful, but every
workflow requires the terminal, remembering flags, and reading JSON logs. **The control panel
makes the existing pipeline usable through a browser** — start a capture, kick off training,
watch progress, run an eval, all from buttons and forms.

**Non-goal:** reimplementing any capability. The panel is a *front-end over the existing CLI /
sibling packages*. If the CLI can't do it, the panel doesn't either (yet).

---

## 2. Form factor (decided)

**Local web app** — a FastAPI backend serving a React single-page app, opened in the user's
browser at `http://127.0.0.1:8080`.

Why this over the alternatives:
- **No native toolchain.** Pure Python + static web assets. Avoids the Rust + Visual Studio
  C++ Build Tools wall that blocked the desktop (Tauri) approach.
- **Reuses what exists.** `openadapt serve` already launches a web dashboard (from
  `openadapt-ml`) and `config.py` already has a "Server Settings" block (`server_port`,
  `server_host`, `server_auto_open`). The panel extends this pattern rather than inventing one.
- **Cross-platform, zero install friction** beyond `pip`.

---

## 3. Where it lives (packaging)

The meta-package rule is strict: **base install depends only on `click`**; all real
functionality is a sibling package pulled in as an extra. The panel follows suit.

- **New sibling package `openadapt-panel`** — the FastAPI app + built React assets.
- **New extra** in `pyproject.toml`: `panel = ["openadapt-panel>=0.1.0"]` (add to the `all`
  bundle).
- **New CLI command** `openadapt panel` in `openadapt/cli.py`, wired the same way as every
  other command: the sibling is **imported lazily inside the command body**, with an
  `except ImportError` that means only "not installed."

```bash
pip install "openadapt[panel]"
openadapt panel                 # opens http://127.0.0.1:8080 in the browser
openadapt panel --port 9000 --no-open
```

> The panel package builds its React frontend to static files at package-build time and ships
> them inside the wheel, so the end user never needs Node.

---

## 4. Architecture

```
┌── Browser (React SPA) ──┐   REST (actions) + WS/SSE (progress)   ┌── FastAPI (openadapt-panel) ──┐
│  Dashboard / Captures / │  ⇄ ─────────────────────────────────⇄ │  route handlers               │
│  Train / Eval / Models  │        127.0.0.1, token-authed         │  = thin wrappers that call    │
│  / Settings             │                                        │  the SAME sibling functions   │
└─────────────────────────┘                                        │  the CLI calls                │
                                                                    └───────────────┬───────────────┘
                                                                                    │ lazy import
                                       ┌────────────────────────────────────────────┼───────────────┐
                                       ▼                     ▼                        ▼               ▼
                                 openadapt-capture     openadapt-ml            openadapt-evals     config.settings
                                 (Recorder, Capture)   (scripts.train.main,    (ApiAgent, WAA…)
                                                        cloud.local.cmd_serve)
```

**Principle: the backend is a seam, not a second implementation.** Each route calls the exact
function the corresponding CLI command already calls (e.g. `openadapt_ml.scripts.train.main(...)`,
`openadapt_capture.Recorder(...)`). This keeps the panel honest and cheap to maintain.

### Long-running operations
Capture and training don't return quickly — they can't run inline in a request. The backend
runs them as **background tasks / subprocesses** and streams state to the UI:
- **Training** already writes `training_output/training_log.json` (status, epoch, loss). The
  panel polls or tails it and pushes updates over WebSocket/SSE. Stop = write the existing
  `STOP_TRAINING` sentinel file.
- **Capture** currently only stops via Ctrl+C (`capture stop` is a documented TODO). The panel
  needs a real stop signal → see prerequisites.

### Reuse the existing dashboard
For training visualization, embed / link the existing `openadapt serve` dashboard rather than
rebuilding charts.

---

## 5. Feature map (what each page wraps)

| Page | Wraps (CLI / sibling) | Notes |
|------|----------------------|-------|
| **Dashboard / System** | `doctor`, `version` | Installed packages, GPU, API-key presence. **Must use `importlib.metadata` / `find_spec`** — never import siblings (mirror `doctor`). |
| **Captures** | `capture list / view`, `openadapt_capture.Recorder`, `create_html` | List recordings, start/stop a recording, embed `viewer.html`. |
| **Train** | `train start / status / stop`, `openadapt_ml.scripts.train.main` | Pick capture + config, live progress from `training_log.json`, stop via `STOP_TRAINING`. |
| **Eval** | `eval run / mock`, `openadapt_evals` | Choose mock vs live (WAA), agent (api-claude / checkpoint), show success rate / avg steps. |
| **Models** | filesystem (`model_cache_dir`, checkpoints) | Browse trained checkpoints. |
| **Settings** | `openadapt.config.settings` + `.env` | Edit API keys, dirs, device, ports. Writes `.env`. |

---

## 6. Phase plan

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **P0 — Shell** | `openadapt-panel` package skeleton, `openadapt panel` command, FastAPI + built React SPA, **Dashboard/System page only** (read-only, `doctor` parity, no sibling execution). | `openadapt panel` opens a browser showing system status; works with zero siblings installed. |
| **P1 — Read + safe actions** | Captures (list/view), Eval (mock + run). These are the lowest-risk, mostly-synchronous flows. | Can view a capture and run a mock eval from the UI. |
| **P2 — Long-running ops** | Training start/status/stop with live progress (WS/SSE); capture record/stop. **Fix `capture stop`** (signal/file). Embed `serve` dashboard. | Start & monitor a real training run and a capture from the UI, stop cleanly. |
| **P3 — Polish & ship** | Settings editor (`.env`), loopback token auth, packaging (wheel bundles built assets), docs. | `pip install openadapt[panel]` → `openadapt panel` is a complete, documented experience. |

---

## 7. Constraints & rules to honor (from the meta-package)

These are load-bearing — violating them reintroduces the "#999 bug class" (green CI over broken
wiring):

1. **Lazy imports only.** The panel sibling must be imported inside the `openadapt panel`
   command body, never at module top in `cli.py`. Within the panel package, siblings
   (`openadapt-capture` etc.) must be imported inside route handlers, so the app boots headless.
2. **`version` / `doctor` / the System page never import siblings** — use `importlib.metadata`
   and `importlib.util.find_spec` to inspect without executing package code
   (`openadapt-capture` screenshots at import time and crashes headless).
3. **Don't mask wiring bugs.** `except ImportError` means "sibling not installed" — not "sibling
   installed but broken." A renamed/missing symbol must surface.
4. **Seam tests.** Extend the existing test philosophy: assert every panel route calls its
   sibling function with kwargs that exist in that function's signature (like
   `SERVE_NAMESPACE_ATTRS` in `tests/test_cli_smoke.py`). A route that wires nothing must not
   pass silently.
5. **Commit prefixes drive releases.** `feat:` on the meta-package = minor bump. Scope panel
   work so meta-package commits use the right prefix; the sibling package has its own release.

---

## 8. Prerequisites / things to fix first

- **`capture stop` is a TODO** (currently Ctrl+C only). The panel's capture flow needs a real
  stop mechanism (signal or sentinel file, mirroring `STOP_TRAINING`). Do this in
  `openadapt-capture` before P2.
- **⚠️ Root `.gitignore` bug:** line 31 is a bare `src`, which ignores **any** directory named
  `src` anywhere in the repo. This already silently dropped the Miso React UI from a commit.
  The panel's React frontend will live in a `src/` dir — **fix this pattern** (scope it, e.g.
  `/legacy/**/src/` or remove it) before adding the frontend, or its source will vanish from
  git the same way.
- **Security:** even on loopback, the panel triggers real system actions (recording, training,
  eval). Bind `127.0.0.1` only and require a per-session token (the `serve`/config server
  settings and the archived Miso engine both show the pattern).
- **Headless CI:** the panel package must import cleanly with no siblings and no display; its
  tests must skip sibling-dependent assertions when siblings aren't installed but run in CI
  where they are (same as the meta-package).

---

## 9. Current status & immediate next steps

**Status (updated):** built P0–P3 in-repo on `feature/control-panel-blueprint`. The
`openadapt-panel` package ships a FastAPI backend + vanilla-JS SPA with all six pages
(Dashboard, Captures, Train, Eval, Models, Settings), background jobs with SSE log streaming,
and per-session loopback token auth. `openadapt panel` command wired; `panel` extra added.
Open-question #10 resolved: **in-repo** (`openadapt-panel/`), to split out later.

**Done:**
- `.gitignore` `src` → `/src/` fix (prerequisite).
- `openadapt panel` command (find_spec distinguishes not-installed vs broken).
- Backend seam parity with the CLI: captures/eval call the same sibling functions; train &
  capture run as `openadapt` CLI subprocesses (train stop = `STOP_TRAINING` sentinel; capture
  stop = interrupt signal to the child's process group — sidesteps the `capture stop` TODO for
  the panel's purposes, though a graceful in-sibling stop is still worth adding).
- Tests: behavior tests (auth, routes, settings round-trip) run everywhere; seam contracts
  against sibling signatures skip locally and run in CI (which installs `./openadapt-panel[dev]`
  + siblings). All green locally (15 passed / 9 skipped; the skips are the sibling-dependent
  seam tests that run in CI).

**Remaining before "ship":**
1. **Push branch + open PR** (blocked: no GitHub creds/`gh` on the build box).
2. Verify live capture/train/eval on a machine with the siblings + GPU/display installed
   (not testable headless here).
3. Optional: replace the vanilla-JS SPA with a React build at package-build time; add a
   graceful `capture stop` in `openadapt-capture`; publish `openadapt-panel` to PyPI.

---

## 10. Open questions (revisit before P2)

- Does `openadapt-panel` live as a **separate sibling repo** (consistent with the ecosystem) or
  start **in-repo** and split out later? (Blueprint assumes separate sibling.)
- Reuse the `openadapt serve` dashboard wholesale for training, or absorb its views into the
  panel UI for a single consistent shell?
- Who is the primary user — the researcher (needs training/eval depth) or a less-technical
  operator (needs guardrails and presets)? This shifts how much the Settings/Train pages expose.
</content>
