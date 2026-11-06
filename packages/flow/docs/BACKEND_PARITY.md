# Backend parity

This page shows which OpenAdapt capabilities are qualified or tested on which
app surface, and the evidence behind each. It was audited against the code and
the committed evidence at `e3117c14b` (2026-11-02). Each filled cell has one of
two verdicts:

| Verdict | Meaning |
| --- | --- |
| **QUALIFIED** | A run on the real surface (a real browser, a real remote session) against a third-party app, with committed results. The evidence path is given. |
| **TESTED** | Tests drive this capability on this surface. "Real OS" means the run used a real operating system or protocol with an app we wrote (the WinForms "Patient Notes" app, a Tk kiosk, TextEdit with local files). Otherwise the tests use fakes or stand-ins. |

QUALIFIED needs a real surface and a third-party app. A test with a fake
backend, a fake UI Automation module or a stand-in window is **TESTED**,
because fakes hid five fail-open bugs found in October 2026.

## Surfaces

| Surface | `backend.kind` | How Flow sees the app | Code |
| --- | --- | --- | --- |
| Web | `web` | Playwright page: DOM and pixels | `backends/playwright_backend.py` |
| Windows | `windows` | UI Automation through the in-guest agent server (`agent_url`) | `backends/windows_backend.py`, `backends/win_agent/` |
| macOS | `macos` | Accessibility, one app window (`macos_app`) | `backends/macos_backend.py` |
| Linux | `linux` | AT-SPI, one exact window (`linux_app`, `linux_window_title`) | `backends/linux_backend.py` |
| RDP | `rdp` | Pixels: network RDP (`rdp_host`, Aardwolf or FreeRDP) or a local client window (`rdp_window`) | `backends/rdp_backend.py`, `backends/xfreerdp.py`, `backends/remote_display.py`, `backends/win32_window_client.py` |
| Citrix | `citrix` | Pixels: the local Citrix Workspace window | `backends/citrix_workspace.py` |

`backends/parallels_vm.py` starts, reverts and reaches the Windows test VM;
actions in that VM go through the Windows or RDP backend.
`backends/factory.py` (`build_backend`) builds the six kinds above and checks
that each section names its target.

A command picks the surface from, in order: the bundle's recorded surface, the
`backend:` section of a deployment file (`--config deploy.yaml`), then the
`--backend` flags. `--url` alone means web.

## The matrix

| Capability | Web | Windows | macOS | Linux | RDP | Citrix |
| --- | --- | --- | --- | --- | --- | --- |
| [Record](#record) | QUALIFIED (scripted) | TESTED (real OS, scripted) | TESTED | TESTED (real OS, Capture) | QUALIFIED (narrow, scripted) | TESTED |
| [Compile](#compile) | QUALIFIED | TESTED (real OS) | TESTED | | QUALIFIED (narrow) | TESTED |
| [Replay and run](#replay-and-run) | QUALIFIED | TESTED (real OS) | TESTED (real OS) | | QUALIFIED (narrow) | TESTED |
| [Step checks and element identity](#step-checks-and-element-identity) | QUALIFIED | TESTED (real OS) | TESTED (real OS) | TESTED | QUALIFIED (look-alike stops) | TESTED |
| [Stop, hold and ask a person](#stop-hold-and-ask-a-person) | QUALIFIED | TESTED | TESTED (real OS, ambiguity stop) | TESTED | TESTED | TESTED |
| [Approvals](#approvals) | TESTED (real app) | TESTED | | | | |
| [Self-repair](#self-repair-and-teaching-the-fix) | TESTED (real browser) | | TESTED (real OS) | | TESTED | TESTED |
| [Teaching the fix](#self-repair-and-teaching-the-fix) | TESTED (real browser) | | | | | |
| [Rollback](#rollback-and-undo) | TESTED (real browser) | | | | | |
| [Undo (`undo propose`, `undo run`)](#rollback-and-undo) | TESTED (real browser) | | | | | |
| [Write verifier](#write-verifier) | QUALIFIED | TESTED | TESTED | | QUALIFIED | TESTED |
| [Shadow mode](#shadow-mode) | QUALIFIED (HTTP writes) | QUALIFIED (HTTP writes) | QUALIFIED (HTTP writes) | QUALIFIED (HTTP writes) | QUALIFIED (HTTP writes) | QUALIFIED (HTTP writes) |
| [Agent guard](#agent-guard) | TESTED (real browser) | TESTED (guarded save) | TESTED (guarded save) | TESTED (guarded save) | TESTED (guarded save) | TESTED (guarded save) |
| [MCP and agent adapters](#mcp-and-agent-adapters) | TESTED (real browser) | TESTED | TESTED | TESTED | TESTED | TESTED |

The third-party apps behind the QUALIFIED cells are OpenEMR, Odoo, Frappe
Lending, openIMIS and Medplum in a browser, and OpenEMR shown in Edge inside a
real Windows Server 2022 RDP session.

The two QUALIFIED (narrow) RDP cells come from
[`benchmark/rdp_avc_real`](../benchmark/rdp_avc_real/README.md): the stock
Remote Desktop client into Windows Server 2022 with H.264/AVC444, driven by
`RemoteDisplayBackend` and `Win32WindowClient`. Across 105 runs, 55 saves were
confirmed in the database, with 0 wrong-record saves and 40 false stops ranked
by cause. The scope of these runs:

- The apps are OpenEMR 7.0.3, a web app shown in Edge inside the session, and
  the in-tree WinForms Patient Notes.
- Workflows were recorded with the scripted `Recorder` API (`harness.py`).
- Replays called `Replayer.run` directly.

## Evidence by capability

Paths are relative to `packages/flow/` unless they start with `packages/` or
`.github/`.

### Record

A recording made by a script through the real recorder counts here.

- **Web, QUALIFIED (scripted).** Odoo 18: `benchmark/odoo_contacts/results/2026-10-28-click-teach/`
  (a Playwright-scripted demonstration). OpenEMR: the referral bundle behind
  `benchmark/lifecycle_proof/results/proof-15`.
- **Windows, TESTED (real OS, scripted).** `benchmark/windows_uia` recorded
  Patient Notes with the scripted `Recorder` on `WindowsBackend` in a Windows
  11 VM (`tests/e2e/test_parallels_desktop_e2e.py`, opt-in). The post-merge
  `capture-windows` job (`.github/workflows/packages.yml`, `windows-latest`)
  runs the real UI Automation observer on a WinForms fixture
  (`packages/capture/tests/test_windows_uia_live.py`). Recording driven by an
  agent is tested with a fake backend
  (`packages/agent/tests/test_authoring_native.py`).
- **macOS, TESTED.** `tests/test_desktop_record.py`,
  `packages/agent/tests/test_authoring.py` (fake macOS backend).
- **Linux, TESTED (real OS, Capture).** `.github/workflows/packages.yml`
  records a click and typed text in an Xvfb container, and
  `packages/capture/tests/test_linux_atspi_integration.py` reads one GTK
  button over AT-SPI (the Python 3.10 leg, after merge to main).
- **RDP, QUALIFIED (narrow, scripted).** `benchmark/rdp_avc_real` recorded both
  apps through the scripted `Recorder` over a real AVC444 session.
- **Citrix, TESTED.** `tests/test_citrix_workspace_backend.py`,
  `tests/test_jit_surface_pixels.py` (MockMed in a stand-in window).

### Agent control authoring

`AuthoringSession.observe()` lists actionable controls from Windows UI
Automation, macOS Accessibility and Linux AT-SPI. Native node references keep
the observed element identity inside the session. Before a node action, the
session rechecks its identity and window, resolves a unique structural
fingerprint, and dispatches through the backend's structural action. An
unreadable, stale or ambiguous target refuses without a coordinate retry.
RDP and Citrix observations remain screen-based.

The component controls in `tests/test_authoring.py`,
`tests/test_windows_backend.py`, `tests/test_win_agent_server.py`,
`tests/test_macos_structural.py` and `tests/test_linux_backend.py` use
intercepted native boundaries. They are **TESTED**, not live-OS qualification.

### Compile

Compile reads files, so it runs anywhere. What differs by surface is the
identity it can capture.

- **Web, QUALIFIED.** Odoo (14 steps, 6 parameters) and the OpenEMR referral
  bundle (proof-15).
- **Windows, TESTED (real OS).** `benchmark/windows_uia` compiled the scripted
  recording on a Windows 11 VM and replayed it on the structural rung
  (`tests/e2e/test_parallels_desktop_e2e.py`). `adapters/capture.py` turns UI
  Automation evidence from Capture (`AutomationId`, or role and name) into a
  structural locator; `tests/test_capture_adapter.py`,
  `tests/test_desktop_recorder.py`.
- **macOS, TESTED.** `tests/test_macos_structural.py` (Recorder, compiler and
  Replayer over a fake TextEdit).
- **RDP, QUALIFIED (narrow).** `benchmark/rdp_avc_real` (`harness.py`,
  `compile_recording`). Identity comes from the OCR band or a marked crop
  (`compiler/compile.py`).
- **Citrix, TESTED.** `benchmark/citrix_workspace` (canvas stand-in, 6 of 6).

### Replay and run

A single non-web run goes through `_replay_desktop` (`__main__.py`), which passes
the same record check, approval gate and durable mode as a web run.

- **Web, QUALIFIED.** proof-15 on local OpenEMR 8.0.0.3: r1 to r3 VERIFIED with
  0 model calls. Also `benchmark/openemr_local`, `benchmark/frappe_lending`.
- **Windows, TESTED (real OS).** `benchmark/windows_uia/results.json`
  (2026-07-17), on the Patient Notes WinForms app in a Parallels Windows 11 ARM
  VM: 3 of 3 on the final candidate, with 0 silent wrong saves, through
  `Replayer.run`. `benchmark/desktop/results.json` (2026-07-10): the UI
  Automation arm passed 21 of 21.
- **macOS, TESTED (real OS).** `benchmark/macos_native/textedit_counted_3plus1_b1b61a5_20260717.json`:
  3 of 3 byte-exact effects in TextEdit, driving `MacOSBackend` directly; the
  adjudication file accepts the three effects.
- **RDP, QUALIFIED (narrow).** `benchmark/rdp_avc_real/results/results.json`,
  with the false stops ranked by cause in its README. `benchmark/rdp_ladder`
  (Tk kiosk over FreeRDP, 6 of 6) is real-protocol, in-tree app.
  `ACCEPTED_BATCH_82A658A.md` (3 of 3) covers transport and input.
- **Citrix, TESTED.** `benchmark/citrix_ica_hdx` (stand-in) and
  `benchmark/citrix_workspace` (canvas stand-in). Each run starts from a
  readiness marker on the current frame (`rdp_readiness_text`, `__main__.py`).

### Step checks and element identity

`tests/test_backend_capability_matrix.py` pins which backends expose a
structured identity read and a structural action: web, Windows, macOS and
Linux.

- **Web, QUALIFIED.** proof-15: after the UI change, r5, r7 and r8 stopped
  before saving on an identity failure.
- **Windows, TESTED (real OS).** `benchmark/windows_uia`: stale and ambiguous
  targets each stopped 3 of 3 before any input. The post-merge
  `flow-native-census` job reads a real WPF list over UI Automation on
  `windows-latest`.
- **macOS, TESTED (real OS).** `benchmark/macos_native/ax_identity_20260720.json`
  (a diagnostic run), and the same post-merge census job on an `NSTableView`.
- **Linux, TESTED.** `tests/test_linux_backend.py` (fake client).
- **RDP, QUALIFIED (look-alike stops).** In `rdp_avc_real`, 10 of 10 look-alike
  runs stopped with 0 wrong saves. Pixel identity acts as a veto
  (`runtime/identity.py`); the bar for a positive pixel match is in
  `benchmark/pixel_identity_aligned/EVIDENCE.md`. The standard gate requires an
  identity check on every consequential step (`run_gate.py`).
- **Citrix, TESTED.** `benchmark/citrix_ica_hdx` drift halts (stand-in),
  `tests/test_pixel_only_citrix_verified_refusal.py`.

### Stop, hold and ask a person

- **Web, QUALIFIED.** `benchmark/halt_cleanup/results/openemr.json`; proof-15 r4
  was killed with SIGKILL, held durably, and resumed after a person approved.
- **Windows, TESTED.** `tests/test_console.py` (stubbed factory). The halts in
  `benchmark/windows_uia` are real-OS stops.
- **macOS, TESTED (real OS, ambiguity stop).** The TextEdit batch stopped on a
  two-window ambiguity without changing either file.
- **Linux, TESTED.** Ambiguity and stale-target stops in
  `tests/test_linux_backend.py` (fake client).
- **RDP and Citrix, TESTED.** `tests/test_run_gate.py`,
  `tests/test_cli_new_commands.py` (resume).

### Approvals

The approval gate, the item-approval queue and the phone web app are
backend-independent; the gate is built before the web or desktop branch
(`__main__.py`). The one backend-specific part is the check that a form stayed
the same while waiting: it reads the DOM on the web and OCR words elsewhere
(`approval_mode/form_state.py`).

- **Web, TESTED (real app).** proof-15 ran `problems_only` mode on OpenEMR,
  with a scripted person and a local stand-in for Cloud
  (`benchmark/lifecycle_proof/README.md`).
- **Windows, TESTED.** `tests/test_approval_mode_cli.py` (`target_kind="windows"`,
  fake backend).

### Self-repair and teaching the fix

Self-repair is qualified on browser runs
([`benchmark/auto_repair`](../benchmark/auto_repair/README.md)) and end to end
on native macOS AX ([#1715](https://github.com/OpenAdaptAI/OpenAdapt/pull/1715)).
Outside a browser it runs with `repair.session` in `deploy.yaml`, which starts
the app for each probe ([#1717](https://github.com/OpenAdaptAI/OpenAdapt/pull/1717)).
Element-identity admission for native and pixel backends is
[#1709](https://github.com/OpenAdaptAI/OpenAdapt/pull/1709): on a backend with
an accessibility tree, a new click is admitted by its element identity
(`element_identity_at`) and a record check; on pixels, by the record check;
either way a person approves. Teaching the fix (`run --hold-on-halt` and the
MCP `teach_repair` tool, which works on the halted run's own page) is tested on
browser runs (`agent/repair.py`). Offline `teach` and `teach-branch` read the
bundle and a new recording, and the governed `repair` lifecycle (register,
campaign, approve, stage, canary) works on evidence frames and run reports.

- **Web, TESTED (real browser).** `benchmark/auto_repair`: MockMed, 6 of 21
  repairable stops resumed and verified, 0 wrong saves. Teaching the fix:
  `tests/test_agent_teach_repair.py` (a rendered dialog page through the real
  replayer, teach loop, campaigns, person approval, staging and canary).
- **macOS, TESTED (real OS).** Qualified end to end on OpenAdapt's native Cocoa
  test app with SQLite through live AX
  ([#1715](https://github.com/OpenAdaptAI/OpenAdapt/pull/1715)): stop, admitted
  click, probe, replay and fault campaigns, one approval, resumed run
  `VERIFIED` ([`benchmark/repair_every_backend`](../benchmark/repair_every_backend/README.md)).
- **RDP and Citrix, TESTED.** `tests/test_repair_element_identity.py`.

### Rollback and undo

- **Web, TESTED (real browser).** `benchmark/rollback/results/mockmed.json`
  rolled back after the save. `benchmark/undo_proposal/results/mockloan.json`
  verified an undo in a real browser on a test copy
  ([AFTER_A_STOP.md](AFTER_A_STOP.md), [ROLLBACK.md](ROLLBACK.md)).

### Write verifier

The SQL, FHIR and REST verifiers, `verify-run` and the whole-database frame
read the system of record and import no backend. They work the same on every
surface when the customer gives read-only access to the database or API. The
read-back from the app's own screens differs by surface: `ui_readback` opens a
browser at a web record page, and `desktop_readback` reads Windows through UI
Automation and other surfaces by OCR
(`runtime/effects/desktop_readback_sessions.py`).

- **Web, QUALIFIED.** proof-15 SQL record checks; `benchmark/openemr_live`
  (FHIR); `benchmark/fhir_medplum`.
- **Windows, TESTED.** `tests/test_desktop_readback.py`: the real `win_agent`
  server and `WindowsBackend` over a fake `uiautomation` module.
- **macOS, TESTED.** `tests/test_desktop_readback.py` (fake client, OCR).
- **RDP, QUALIFIED.** `rdp_avc_real`: `verify-run` agreed with the database on
  50 of 50 runs. Read-back confirmed 5 of 5 saves and returned indeterminate
  on 5 of 5 mismatches (`results/campaigns/pn-readback-2`, Patient Notes).
- **Citrix, TESTED.** `tests/test_pixel_only_citrix_verified_refusal.py`.

### Shadow mode

Shadow mode is a loopback HTTP proxy (`docs/SHADOW_MODE.md`). It checks the
writes an agent makes over HTTP, whatever GUI surface the agent works on.

- **Every surface, QUALIFIED for HTTP writes.** `benchmark/fhir_medplum/public/SHADOW_RESULTS.md`:
  Medplum 5.2.8, 37 cases, with a scripted agent and a Claude agent writing
  FHIR over HTTP.

### Agent guard

There are two guards. `openadapt-flow guard` attaches to the agent's own
Chromium through the Chrome DevTools Protocol (`agent_guard/cdp.py`;
`docs/GUARD_AN_AGENT.md`). The guarded save (`guard_begin`, `guard_commit`,
`guard_confirm` over MCP; `guard.py`, `docs/GUARDED_SAVE.md`) takes the screen
text and a screenshot from the agent and reads the record back with
`verify-run`, so it works the same on every surface.

- **Web, TESTED (real browser).** `benchmark/agent_guard/RESULTS.md` (MockMed).
- **Every other surface, TESTED (guarded save).** `tests/test_guarded_save.py`.

### MCP and agent adapters

- **Web, TESTED (real browser).** `tests/e2e/test_mcp_teach_e2e.py` (runs in
  CI); `benchmark/first_tool` (Claude Code against the example app).
- **Desktop and remote surfaces, TESTED.** `emit-mcp` generates a server that
  runs `openadapt-flow run --config` for a desktop bundle, and `emit-skill`
  writes matching instructions (`emit/mcp_tool.py`, `emit/skill.py`;
  `tests/test_backend_parity_refusals.py`). `serve --teach` on RDP and Citrix
  records and passes its record check (`tests/test_mcp_teach_surfaces.py`).

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

## URL flags

`tests/test_backend_parity.py` fails when a command takes an app URL flag
(`--url`, `--target-url`, or any other `--...url` flag that names the app)
without `--backend` or `--config`, unless the command is listed here with the
reason. Service endpoints (`--tsa-url`, `--cloud-url`, `--execute-url`,
`--control-plane-url`, `--effects-base-url`, `--api-base-url`, `--agent-url`,
and `--log-url`, the transparency log) are not app URLs.

<!-- url-flag-refusals: the test reads this table. -->
| Command | URL flag | Why it has no backend-neutral alternative |
| --- | --- | --- |
| `openadapt-flow validate-hosted` | `--target-url` | The surface comes from the signed run report. A non-web report refuses a target URL ("Target URL is web-only and must be omitted"). |
| `openadapt-agent authoring connect` | `--url` | `--url` opens a fresh browser. Without it the command pins the local app window (one macOS or Linux window). Windows, RDP and Citrix are coach-only on the mailbox: the person drives and Capture records (`COACH_ONLY_BACKENDS` in `agent/mailbox.py`). |
| `openadapt-agent seal keys pin` | `--url` | The seal key server's address, not an app. |

