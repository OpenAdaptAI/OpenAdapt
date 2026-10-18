# Backend parity

Every OpenAdapt capability should work the same way on every app surface, or
say exactly why it can't. This page lists each command and Python entry point
against the six surfaces and says which of these is true:

- **tested**: works, and a test drives it on that surface (file named).
- **works**: works through the shared backend factory; no test drives it on
  that surface yet.
- **refused**: stops before any action, with the reason quoted.
- **web only (demo)**: runs the bundled MockMed demo app by design.
- **n/a**: does not drive an app (offline over files, a server, or a report).

The six surfaces:

| Surface | `backend.kind` | How Flow sees the app |
| --- | --- | --- |
| Web | `web` | Playwright page: DOM and pixels |
| Windows | `windows` | UI Automation through the in-guest agent (`agent_url`) |
| macOS | `macos` | Accessibility, one app window (`macos_app`) |
| Linux | `linux` | AT-SPI, one exact window (`linux_app`, `linux_window_title`) |
| RDP | `rdp` | pixels: network RDP (`rdp_host`) or a local client window (`rdp_window`) |
| Citrix | `citrix` | pixels: the local Citrix Workspace window |

A command picks the surface from, in order: the bundle's recorded surface, the
`backend:` section of a deployment file (`--config deploy.yaml`), then the
`--backend` flags. `--url` alone means web. `backends/factory.py`
(`build_backend`) builds every surface and refuses a section that is missing
its target.

## Commands that run an app

| Command | Web | Windows | macOS | Linux | RDP | Citrix |
| --- | --- | --- | --- | --- | --- | --- |
| `record` | tested | tested (Capture; `--agent-url` refused) | tested (`--macos-app` scopes Capture) | works (whole desktop; `--linux-app` refused) | tested (local client window; `--rdp-host` refused) | tested |
| `replay` | tested | tested | works | works | works | tested |
| `run` | tested | tested | works | works | works | tested; refused without `rdp_readiness_text` |
| `resume` | tested | tested | works | works | works | tested; refused without `rdp_readiness_text` |
| `console --attend --allow-actions` | works | tested | works | works | works | works |
| `qualify run-case` | tested | works | works | works | works | works |
| `qualify campaign` | tested | refused unless `app.session` | refused unless `app.session` | refused unless `app.session` | tested (with `app.session`) | refused unless `app.session` |
| `connector run` | tested | works | works | works | tested | tested |
| `validate-hosted` | tested | tested | works | works | tested | works |
| `bench` | web only (demo) | web only (demo) | web only (demo) | web only (demo) | web only (demo) | web only (demo) |
| `demo`, `demo-record`, `tutorial` | web only (demo) | web only (demo) | web only (demo) | web only (demo) | web only (demo) | web only (demo) |
| `emit-mcp` (the generated server) | works | **web only (silent)** | **web only (silent)** | **web only (silent)** | **web only (silent)** | **web only (silent)** |
| `emit-skill` (the generated SKILL.md) | works | **tells the agent to pass `--url`** | same | same | same | same |
| `serve-execute` | works | **web only (silent)** | **web only (silent)** | **web only (silent)** | **web only (silent)** | **web only (silent)** |

Evidence:

- `record`: `tests/test_desktop_record.py`, `tests/test_backend_factory.py`,
  `tests/test_citrix_workspace_backend.py`. Linux recording captures the whole
  desktop; `--linux-app` is refused with "the current Capture component has no
  Linux window-scoping primitive".
- `replay`, `run`: non-web runs go through `_replay_desktop`
  (`__main__.py`). `tests/test_backend_factory.py` (windows),
  `tests/test_citrix_workspace_backend.py`, `tests/test_run_gate.py` (Citrix
  readiness refusal: "governed Citrix execution requires a stable
  current-frame readiness marker before any action").
- `resume`: `tests/test_cli_new_commands.py`.
- `console`: `tests/test_console.py` (windows, factory stubbed).
- `qualify campaign`: `qualify/session.py` has a built-in session for web
  only. Other kinds refuse with "app.target ... needs app.session
  (module:function returning (backend, close)); only web has a built-in
  session". `tests/test_qualify_declarative.py`,
  `tests/test_lookup_capabilities.py`.
- `connector run`: `tests/test_connector.py`. A non-web job that carries a URL
  is refused: "native/remote byoc dispatch carries a browser target boundary".
- `validate-hosted`: `tests/test_runtime_validation.py`. A target URL for a
  non-web run is refused: "Target URL is web-only and must be omitted".

## Commands that compile or check files

These read recordings, bundles, run directories or reports. They do not drive
an app, so the surface does not matter: **n/a** on all six.

`compile`, `induce`, `for-each`, `compose`, `process` (their `run` hands each
child to `run`), `verify-run`, `scaffold-verifier`, `lint`, `explain`,
`visualize`, `seal`, `bind-record-checks`, `certify`, `qualify` (all verbs
except `campaign` and `run-case`), `propose-qualification`, `disambiguate`,
`teach`, `teach-branch`, `repair` (all verbs; they work on evidence frames and
the repair store), `admit`, `check`, `known-ids`, `triage`, `intake`, `pull`,
`push`, `report-break`, `report-run`, `sanitize`, `review-sanitized`,
`materialize-approved`, `approve-sanitized`, `connect`, `login`,
`local-seal`, `attach-seal`, `countersign`, `no-outbound`, `service`,
`approve`, `item-approval`, `approver-grant`, `jit` (admission verbs),
`fleet`, `business-decisions`, `serve-reward` (reads an oracle; it never
drives an app).

## Python entry points

| Entry point | Web | Windows | macOS | Linux | RDP | Citrix |
| --- | --- | --- | --- | --- | --- | --- |
| `jit.compile_agent(app=WebApp(...))` | tested (`tests/e2e/test_jit_cua_e2e.py`) | n/a (use `SurfaceApp`) | n/a | n/a | n/a | n/a |
| `jit.compile_agent(app=SurfaceApp(...))` ([#1283](https://github.com/OpenAdaptAI/OpenAdapt/pull/1283)) | n/a (use `WebApp`) | works | works | works (needs `linux_allow_physical_input`) | tested | tested |
| `AuthoringSession` (agent drives a recording) | tested | tested | tested | works | tested | tested |
| `runtime.effects.ui_readback` | tested | see below | see below | see below | see below | see below |
| `effects.kind: desktop_readback` ([#1280](https://github.com/OpenAdaptAI/OpenAdapt/pull/1280)) | refused: "a web record page uses effects.kind: ui_readback" | tested (UI Automation, WinForms stand-in) | works (OCR; Accessibility tree not read yet) | works (OCR; AT-SPI tree not read yet) | tested (network RDP, OCR) | works (OCR) |
| `openadapt-agent serve --authoring` (pinning a window) | tested | tested | tested (frontmost window only) | **refused** | tested | tested |
| `openadapt-agent` run tools | tested | works (surface from `--config`) | works | works | works | works |

- `SurfaceApp`: `tests/test_jit_surface_app.py` (every kind: validation,
  origin binding, Citrix readiness refusal) and
  `tests/test_jit_surface_pixels.py` (records MockMed through the real
  `RemoteDisplayBackend` and `CitrixWorkspaceBackend`, pixels only). On RDP
  and Citrix the canary is refused by the standard run gate: "Identity
  coverage: ... UNARMED -- would act with no verified target identity". A
  pixel click has no verified record identity until the bundle is qualified;
  `run --profile standard` refuses the same bundle the same way.
- `AuthoringSession`: `packages/agent/tests/test_authoring_native.py`
  (windows, rdp, citrix), `packages/agent/tests/test_authoring.py` (macOS).
- `desktop_readback` (#1280): `tests/test_desktop_readback.py` covers Windows
  (the real win_agent server and `WindowsBackend` over a WinForms stand-in) and
  network RDP (`FreeRDPBackend` and RapidOCR over the RDP kiosk). It refuses a
  read-back session that is, or may be, the one that types (same agent, same
  RDP host and user, or the same window), at load and again after flag
  overrides. Its proposal step (#1290) and deferred reports (#1297) work on
  every surface; the server-answer check (#1298) is browser-only, because no
  other backend sees HTTP.
- `ui_readback` opens its own browser at an `http(s)` record page. It works
  with any replay surface when the app also has a web record page. A desktop
  app without one needs a reopen read-back instead (see
  [VERIFICATION.md](VERIFICATION.md)).
- `openadapt-agent serve --authoring` on Linux: there is no Linux pin flag,
  so it stops with "stdio --authoring needs a locally pinned window".
  [#1287](https://github.com/OpenAdaptAI/OpenAdapt/pull/1287) adds
  `--authoring --config deploy.yaml --authoring-from-config` for every
  surface except network RDP, which it refuses (each run's own session would
  disconnect the recording's).
- An agent clicks at window coordinates. Native Linux refuses coordinate
  input unless the section sets `linux_allow_physical_input: true`
  ("coordinate input is disabled for native Linux"), so agent-driven
  recording on Linux needs it. Replay of a recorded AT-SPI target does not.

## URL flags

`tests/test_backend_parity.py` fails when a command takes an app URL flag
(`--url`, `--target-url`, or any other `--...url` flag that names the app)
without `--backend` or `--config`, unless the command is listed here with the
reason. Service endpoints (`--tsa-url`, `--cloud-url`, `--execute-url`,
`--control-plane-url`, `--effects-base-url`, `--api-base-url`, `--agent-url`)
are not app URLs.

<!-- url-flag-refusals: the test reads this table. -->
| Command | URL flag | Why it has no backend-neutral alternative |
| --- | --- | --- |
| `openadapt-flow validate-hosted` | `--target-url` | The surface comes from the signed run report. A non-web report refuses a target URL ("Target URL is web-only and must be omitted"). |
| `openadapt-agent authoring connect` | `--url` | `--url` opens a fresh browser. Without it the command pins the local app window (one macOS or Linux window). Windows, RDP and Citrix are coach-only on the mailbox: the person drives and Capture records (`COACH_ONLY_BACKENDS` in `agent/mailbox.py`). |
| `openadapt-agent seal keys pin` | `--url` | The seal key server's address, not an app. |

## Open gaps

| Gap | Where | Status |
| --- | --- | --- |
| `emit-mcp` server drives Playwright for every bundle | `emit/mcp_tool.py` | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) |
| `emit-skill` tells the agent to pass `--url` for a desktop bundle | `emit/skill.py` | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) |
| `serve-execute` runs every bundle in a browser | `execute/dispatch.py` | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) (refuses non-web) |
| `bench` runs a desktop bundle against the web demo | `__main__.py` (`_cmd_bench`) | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) (refuses non-web) |
| `console --attend` drives Citrix without the readiness marker | `runtime/durable/attended_service.py` | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) |
| `resume --backend X` on a bundle recorded on Y is not refused | `__main__.py` (`_cmd_resume`) | [#1285](https://github.com/OpenAdaptAI/OpenAdapt/pull/1285) |
| Agent authoring can't pin a Linux window or a named macOS app | `agent/authoring.py`, `agent/cli.py` | [#1287](https://github.com/OpenAdaptAI/OpenAdapt/pull/1287) (`serve --authoring --config deploy.yaml --authoring-from-config`) |
| `AuthoringSession.observe()` lists no elements on Windows, macOS or Linux: web reads the DOM, and no native backend implements the `authoring_tree()` hook, so a desktop agent falls back to OCR text | `backends/windows_backend.py`, `macos_backend.py`, `linux_backend.py` | follow-up |
| JIT canary on RDP or Citrix needs qualified pixel identity | `jit/` | follow-up ([#1283](https://github.com/OpenAdaptAI/OpenAdapt/pull/1283) refuses it with the gate's reason) |
| `route` on a desktop surface: no test | [#1264](https://github.com/OpenAdaptAI/OpenAdapt/pull/1264) | [#1288](https://github.com/OpenAdaptAI/OpenAdapt/pull/1288) |
| `induce --probe-on` on a desktop test copy: no test | [#1276](https://github.com/OpenAdaptAI/OpenAdapt/pull/1276) | [#1289](https://github.com/OpenAdaptAI/OpenAdapt/pull/1289) |
| `run --self-repair` refuses non-web: "only browser runs can be repaired for now" | [#1265](https://github.com/OpenAdaptAI/OpenAdapt/pull/1265) | refused with a reason; desktop support after it merges |
| `compile-agent` takes `--backend`/`--config` but refuses every non-web target (exit 2, before anything runs); its observe step and save gate read the DOM | [#1277](https://github.com/OpenAdaptAI/OpenAdapt/pull/1277) | refused with a reason; desktop support (via `jit.SurfaceApp`) after it merges |
| The GTK3 AT-SPI qualification job is in `packages/flow/.github/`, which GitHub does not run in the monorepo | `.github/workflows/` | follow-up |
