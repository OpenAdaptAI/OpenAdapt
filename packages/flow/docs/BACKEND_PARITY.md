# Backend parity

This page shows which OpenAdapt capabilities work on which app surface, and
what evidence backs each claim. It was audited against the code and the
committed evidence at `e3117c14b` (2026-11-02). Each cell has one of four
verdicts:

| Verdict | Meaning |
| --- | --- |
| **QUALIFIED** | A run on the real surface (a real browser, a real remote session) against a third-party app, with committed results. The evidence path is given. |
| **TESTED** | Tests drive this capability on this surface, but not against a third-party app. "Real OS" means the run used a real operating system or protocol with an app we wrote (the WinForms "Patient Notes" app, a Tk kiosk, TextEdit with local files). Otherwise the tests use fakes or stand-ins. |
| **CODE ONLY** | Code handles this surface, but no test drives it there. |
| **MISSING** | No code path, or the code refuses the surface (the refusal is quoted). |

Fakes don't count as qualification. Five fail-open bugs that fakes hid were
found in October 2026, so a test with a fake backend, a fake UI Automation
module or a stand-in window is **TESTED**, never **QUALIFIED**.

## Surfaces

| Surface | `backend.kind` | How Flow sees the app | Code |
| --- | --- | --- | --- |
| Web | `web` | Playwright page: DOM and pixels | `backends/playwright_backend.py` |
| Windows | `windows` | UI Automation through the in-guest agent server (`agent_url`) | `backends/windows_backend.py`, `backends/win_agent/` |
| macOS | `macos` | Accessibility, one app window (`macos_app`) | `backends/macos_backend.py` |
| Linux | `linux` | AT-SPI, one exact window (`linux_app`, `linux_window_title`) | `backends/linux_backend.py` |
| RDP | `rdp` | Pixels: network RDP (`rdp_host`, Aardwolf or FreeRDP) or a local client window (`rdp_window`) | `backends/rdp_backend.py`, `backends/xfreerdp.py`, `backends/remote_display.py`, `backends/win32_window_client.py` |
| Citrix | `citrix` | Pixels: the local Citrix Workspace window | `backends/citrix_workspace.py` |

Parallels is not a surface. `backends/parallels_vm.py` starts, reverts and
reaches the Windows test VM; every action in that VM goes through the
Windows or RDP backend. `backends/factory.py` (`build_backend`) builds the six
kinds above and refuses a section that is missing its target.

A command picks the surface from, in order: the bundle's recorded surface, the
`backend:` section of a deployment file (`--config deploy.yaml`), then the
`--backend` flags. `--url` alone means web.

## The matrix

| Capability | Web | Windows | macOS | Linux | RDP | Citrix |
| --- | --- | --- | --- | --- | --- | --- |
| [Record](#record) | QUALIFIED | TESTED (real OS, partial) | TESTED | TESTED (real OS, Capture only) | TESTED (real OS, scripted) | TESTED |
| [Compile](#compile) | QUALIFIED | TESTED | TESTED | CODE ONLY | QUALIFIED (narrow) | TESTED |
| [Replay and run](#replay-and-run) | QUALIFIED | TESTED (real OS) | TESTED (real OS) | CODE ONLY | QUALIFIED (narrow) | TESTED |
| [Step checks and element identity](#step-checks-and-element-identity) | QUALIFIED | TESTED (real OS) | TESTED (real OS) | TESTED | QUALIFIED (stops only) | TESTED |
| [Stop, hold and ask a person](#stop-hold-and-ask-a-person) | QUALIFIED | TESTED | TESTED (real OS, refusals) | TESTED | TESTED | TESTED |
| [Approvals](#approvals) | QUALIFIED (`problems_only`) | TESTED | CODE ONLY | CODE ONLY | CODE ONLY | CODE ONLY |
| [Self-repair and teach the fix](#self-repair-and-teach-the-fix) | TESTED (real browser) | MISSING | MISSING | MISSING | MISSING | MISSING |
| [Rollback and undo](#rollback-and-undo) | TESTED (real browser) | MISSING | MISSING | MISSING | MISSING | MISSING |
| [Write verifier](#write-verifier) | QUALIFIED | TESTED | TESTED | CODE ONLY | QUALIFIED | TESTED |
| [Shadow mode](#shadow-mode) | QUALIFIED (HTTP writes) | CODE ONLY (HTTP writes only) | CODE ONLY (HTTP writes only) | CODE ONLY (HTTP writes only) | CODE ONLY (HTTP writes only) | CODE ONLY (HTTP writes only) |
| [Agent guard](#agent-guard) | TESTED (real browser) | MISSING | MISSING | MISSING | MISSING | MISSING |
| [MCP and agent adapters](#mcp-and-agent-adapters) | TESTED (real browser) | TESTED | TESTED | TESTED | TESTED | TESTED |

In short: real third-party apps have run only in a browser (OpenEMR, Odoo,
Frappe Lending, openIMIS, Medplum) and over RDP (OpenEMR shown in Edge inside a
real Windows Server 2022 session). No third-party Windows desktop app has been
run on any surface, and no real Citrix session has been run at all.

The two QUALIFIED (narrow) RDP cells come from
[`benchmark/rdp_avc_real`](../benchmark/rdp_avc_real/README.md): the stock
Remote Desktop client into Windows Server 2022 with H.264/AVC444, driven by
`RemoteDisplayBackend` and `Win32WindowClient`. Across 105 runs, 55 saves were
confirmed in the database, with 0 wrong-record saves and 40 false stops. Three
limits apply:

- The third-party app is OpenEMR 7.0.3, a web app shown in Edge inside the
  session. The other app is the in-tree WinForms Patient Notes.
- Workflows were recorded with the scripted `Recorder` API
  (`harness.py`), not from a person's demonstration.
- Replays called `Replayer.run` directly. The run gate, execution profiles,
  approvals and hold/resume of `openadapt-flow run` were not part of these runs.

## Evidence by capability

Paths are relative to `packages/flow/` unless they start with `packages/` or
`.github/`. Line numbers are at `e3117c14b`.

### Record

- **Web, QUALIFIED.** Odoo 18: `benchmark/odoo_contacts/results/2026-10-28-click-teach/`
  (a Playwright-scripted demonstration, not a person). OpenEMR: the referral
  bundle behind `benchmark/lifecycle_proof/results/proof-15`.
- **Windows, TESTED (real OS, partial).** The post-merge `capture-windows` job
  (`.github/workflows/packages.yml`, `windows-latest`) runs the real UI
  Automation observer on a WinForms fixture
  (`packages/capture/tests/test_windows_uia_live.py`). Input capture is skipped
  there (`OPENADAPT_CI_NO_INPUT_INJECTION`), so recording a person's input on
  Windows has no automated test. Recording driven by an
  agent is tested with a fake backend
  (`packages/agent/tests/test_authoring_native.py`).
- **macOS, TESTED.** `tests/test_desktop_record.py`,
  `packages/agent/tests/test_authoring.py` (fake macOS backend). The macOS
  Capture CI job skips live recording.
- **Linux, TESTED (real OS, Capture only).** `.github/workflows/packages.yml`
  records a click and typed text in an Xvfb container, and
  `packages/capture/tests/test_linux_atspi_integration.py` reads one GTK
  button over AT-SPI. Flow's record, compile and run chain is not run on Linux.
  `--linux-app` is refused at record time: "the current Capture component has
  no Linux window-scoping primitive" (`__main__.py:207`).
- **RDP, TESTED (real OS, scripted).** `benchmark/rdp_avc_real` recorded both
  apps through the scripted `Recorder` over a real AVC444 session. No person's
  Capture recording has run over a real RDP session. Recording with
  `--rdp-host` (network RDP) is refused.
- **Citrix, TESTED.** `tests/test_citrix_workspace_backend.py`,
  `tests/test_jit_surface_pixels.py` (MockMed in a stand-in window).

### Compile

Compile reads files, so it runs anywhere. What differs by surface is the
identity it can capture.

- **Web, QUALIFIED.** Odoo (14 steps, 6 parameters; its own qualification
  refused the bundle under `clinical-write` for unarmed icon-only buttons) and
  the OpenEMR referral bundle (proof-15).
- **Windows, TESTED.** `adapters/capture.py` turns UI Automation evidence
  (`AutomationId`, or role and name) into a structural locator;
  `tests/test_capture_adapter.py`, `tests/test_desktop_recorder.py`. No test
  compiles a real Windows capture.
- **macOS, TESTED.** `tests/test_macos_structural.py` (Recorder, compiler and
  Replayer over a fake TextEdit).
- **Linux, CODE ONLY.** No test compiles a Linux recording.
- **RDP, QUALIFIED (narrow).** `benchmark/rdp_avc_real` (`harness.py`,
  `compile_recording`). Identity comes only from the OCR band or a marked crop
  (`compiler/compile.py`).
- **Citrix, TESTED.** `benchmark/citrix_workspace` (canvas stand-in, 6 of 6).

### Replay and run

Every non-web run goes through `_replay_desktop` (`__main__.py`), which passes
the same record check, approval gate and durable mode as a web run.

- **Web, QUALIFIED.** proof-15 on local OpenEMR 8.0.0.3: r1 to r3 VERIFIED with
  0 model calls. Also `benchmark/openemr_local`, `benchmark/frappe_lending`.
- **Windows, TESTED (real OS).** `benchmark/windows_uia/results.json`
  (2026-07-17): 3 of 3 accepted on the Patient Notes WinForms app in a
  Parallels Windows 11 ARM VM, with 0 silent wrong saves. `benchmark/desktop/results.json`
  (2026-07-10): the UI Automation arm passed 21 of 21; the pixel arm 6 of 21.
  Both predate later changes to `windows_backend.py` and `win_agent/`, and no
  CI job replays through `WindowsBackend` (`tests/e2e/test_parallels_desktop_e2e.py`
  is opt-in).
- **macOS, TESTED (real OS).** `benchmark/macos_native/textedit_counted_3plus1_b1b61a5_20260717.json`:
  3 of 3 byte-exact effects in TextEdit. The trials drove `MacOSBackend`
  directly rather than compile and run; the batch status is `failed`, and the
  adjudication file accepts the effects only.
- **Linux, CODE ONLY.** `tests/test_linux_backend.py` uses a fake client; no
  Replayer test uses `LinuxBackend`. KEY and SCROLL steps and coordinate clicks
  are refused unless `linux_allow_physical_input: true`
  (`linux_backend.py:907`, `:965`).
- **RDP, QUALIFIED (narrow).** `benchmark/rdp_avc_real/results/results.json`.
  Healthy OpenEMR runs saved 8 of 20; every run at 1440×900 stopped. The false
  stops are ranked by cause in its README. `benchmark/rdp_ladder` (Tk kiosk over
  FreeRDP, 6 of 6) is real-protocol, in-tree app. `benchmark/rdp/results.json`
  is a rejected batch (`accepted: false`, 0 of 3);
  `ACCEPTED_BATCH_82A658A.md` (3 of 3) covers transport and input only.
- **Citrix, TESTED.** `benchmark/citrix_ica_hdx` (`is_real_ica_hdx: false`) and
  `benchmark/citrix_workspace` (canvas stand-in). Runs are refused without a
  readiness marker: "governed Citrix execution requires a stable current-frame
  readiness marker before any action" (`__main__.py:726`).

### Step checks and element identity

`tests/test_backend_capability_matrix.py` pins which backends expose a
structured identity read and a structural action: web, Windows, macOS and
Linux do; RDP and Citrix don't.

- **Web, QUALIFIED.** proof-15: after the UI change, r5, r7 and r8 stopped
  before saving on an identity failure.
- **Windows, TESTED (real OS).** `benchmark/windows_uia`: stale and ambiguous
  targets each refused 3 of 3. The post-merge `flow-native-census` job reads a
  real WPF list over UI Automation on `windows-latest`. In
  `benchmark/desktop/results.json` the grid row had no `AutomationId`, so half
  of the clicks carried an identity check.
- **macOS, TESTED (real OS).** `benchmark/macos_native/ax_identity_20260720.json`
  (marked diagnostic, recorded on a dirty tree), and the same post-merge census
  job on an `NSTableView`.
- **Linux, TESTED.** `tests/test_linux_backend.py` (fake client).
  `scripts/qualify_linux_atspi.py` has no committed result.
- **RDP, QUALIFIED (stops only).** In `rdp_avc_real`, 10 of 10 look-alike runs
  stopped with 0 wrong saves. Pixel identity can only veto:
  `PIXEL_VERIFY_ENABLED = False` (`runtime/identity.py:1925`) until a real
  RDP or Citrix identifier corpus meets the bar in
  `benchmark/pixel_identity_aligned/EVIDENCE.md`. The standard gate refuses a
  consequential step with no identity check: "UNARMED -- would act with no
  verified target identity" (`run_gate.py:724`).
- **Citrix, TESTED.** `benchmark/citrix_ica_hdx` drift halts (stand-in),
  `tests/test_pixel_only_citrix_verified_refusal.py`.

### Stop, hold and ask a person

- **Web, QUALIFIED.** `benchmark/halt_cleanup/results/openemr.json`; proof-15 r4
  was killed with SIGKILL, held durably, and resumed after a person approved.
  `ask_user` has no real-app run.
- **Windows, TESTED.** `tests/test_console.py` (stubbed factory). The halts in
  `benchmark/windows_uia` are real-OS stops. `--hold-on-halt` (keep the halted
  screen open for a repair) is browser-only (`__main__.py`, its help text).
- **macOS, TESTED (real OS, refusals).** The TextEdit batch refused a
  two-window ambiguity without changing either file.
- **Linux, TESTED.** Ambiguity and stale-target refusals in
  `tests/test_linux_backend.py` (fake client).
- **RDP and Citrix, TESTED.** `tests/test_run_gate.py`,
  `tests/test_cli_new_commands.py` (resume). No hold, resume or attended
  console has run on a real remote session.

### Approvals

The approval gate, the item-approval queue and the phone web app don't depend
on the backend; the gate is built before the web or desktop branch
(`__main__.py`). The one backend-specific part is the check that a form did not
change while waiting: it reads the DOM on the web and OCR words elsewhere
(`approval_mode/form_state.py`).

- **Web, QUALIFIED (`problems_only` mode).** proof-15. No real-app run of the
  phone web app is committed.
- **Windows, TESTED.** `tests/test_approval_mode_cli.py` (`target_kind="windows"`,
  fake backend).
- **macOS, Linux, RDP and Citrix, CODE ONLY.**

### Self-repair and teach the fix

Self-repair is refused off the web: "only browser runs can be repaired for
now" (`cli_self_repair.py:206`). The repair code keys a fix on DOM identity
(`repair/self_repair.py`), and the MCP `teach_repair` tool works on the halted
run's own page, which only a browser run can hold open (`agent/repair.py`,
`--hold-on-halt`). Offline `teach` and `teach-branch` read the bundle and a new
recording, so they run anywhere, but no test uses a non-web bundle.

- **Web, TESTED (real browser).** `benchmark/auto_repair`: MockMed, 6 of 21
  resumed and verified. On openIMIS 0 of 2 probes passed, and proof-15's
  OpenEMR stop was refused as `not_repairable`. No repair has succeeded on a
  third-party app.
- **Windows, macOS, Linux, RDP and Citrix, MISSING.** `serve --teach` over MCP
  on RDP and Citrix records and passes its record check, but the canary is
  refused ("UNARMED"), so nothing is admitted
  (`tests/test_mcp_teach_surfaces.py`).

### Rollback and undo

- **Web, TESTED (real browser).** `benchmark/rollback/results/mockmed.json`
  rolled back after the save. On OpenEMR both rows are `not_rolled_back` (the
  run stopped before the save), and `benchmark/undo_proposal/results/openemr-new-patient.json`
  verified 0 of 2 undos. Rollback also refuses a durable run, which the
  Standard and Regulated profiles require (`__main__.py`,
  `_refuse_rollback_durable`).
- **Every other surface, MISSING.** "REFUSED before the run: rollback is on,
  and it doesn't run on a desktop or remote-display backend yet"
  (`__main__.py:2802`, `_refuse_rollback_unwired`).

### Write verifier

The SQL, FHIR and REST verifiers, `verify-run` and the whole-database frame
read the system of record and import no backend. They work the same on every
surface when the customer gives read-only access to the database or API. What
differs is the read-back from the app's own screens: `ui_readback` opens a
browser at a web record page, and `desktop_readback` reads Windows through UI
Automation and every other surface by OCR
(`runtime/effects/desktop_readback_sessions.py`).

- **Web, QUALIFIED.** proof-15 SQL record checks; `benchmark/openemr_live`
  (FHIR); `benchmark/fhir_medplum`.
- **Windows, TESTED.** `tests/test_desktop_readback.py`: the real `win_agent`
  server and `WindowsBackend` over a fake `uiautomation` module.
- **macOS, TESTED.** `tests/test_desktop_readback.py` (fake client, OCR).
- **Linux, CODE ONLY.**
- **RDP, QUALIFIED.** `rdp_avc_real`: `verify-run` agreed with the database on
  50 of 50 runs. Read-back confirmed 5 of 5 saves and returned
  indeterminate, never refuted, on 5 of 5 mismatches
  (`results/campaigns/pn-readback-2`, Patient Notes).
- **Citrix, TESTED.** `tests/test_pixel_only_citrix_verified_refusal.py`.

### Shadow mode

Shadow mode is a loopback HTTP proxy (`docs/SHADOW_MODE.md`). It sees only
writes an agent makes over HTTP, so it doesn't depend on the GUI surface. It
can't see a desktop client that writes over a database connection, ODBC or a
vendor protocol.

- **Web, QUALIFIED (HTTP writes).** `benchmark/fhir_medplum/public/SHADOW_RESULTS.md`:
  Medplum 5.2.8, 37 cases.
- **Other surfaces, CODE ONLY (HTTP writes only).**

### Agent guard

The guard works through the Chrome DevTools Protocol (`agent_guard/cdp.py`;
`docs/GUARD_AN_AGENT.md`).

- **Web, TESTED (real browser).** `benchmark/agent_guard/RESULTS.md`, MockMed
  only.
- **Every other surface, MISSING.**

### MCP and agent adapters

- **Web, TESTED (real browser).** `tests/e2e/test_mcp_teach_e2e.py` (runs in
  CI); `benchmark/first_tool` (Claude Code against the example app).
- **Desktop and remote surfaces, TESTED.** `emit-mcp` generates a server that
  runs `openadapt-flow run --config` for a desktop bundle, and `emit-skill`
  writes matching instructions (`emit/mcp_tool.py`, `emit/skill.py`;
  `tests/test_backend_parity_refusals.py`). `serve --teach` on RDP and Citrix:
  `tests/test_mcp_teach_surfaces.py` (admission refused, see above). On Windows,
  macOS and Linux `serve --teach` has no test. No native backend implements the
  `authoring_tree()` hook (`authoring.py`), so an agent sees no controls off
  the web and works from screenshots and OCR.

## Refusals

These stop before anything runs. Each is a gap listed in the next section.

| Command or feature | Surfaces refused | Message (file) |
| --- | --- | --- |
| `run --self-repair`, auto-repair | all but web | "only browser runs can be repaired for now" (`cli_self_repair.py`) |
| rollback | all but web | "rollback is on, and it doesn't run on a desktop or remote-display backend yet" (`__main__.py`) |
| `serve-execute` | all but web | "the reference Execute worker drives a browser only" (`execute/dispatch.py`) |
| `compile-agent` | all but web | "compile-agent runs web apps only so far" (`compile_agent/pipeline.py`) |
| `qualify campaign` | all but web | "qualify runs web campaigns today; the pixel-surface fault driver for Windows, RDP and Citrix is not built" (`qualify/campaign.py`) |
| `record --linux-app` | Linux | "the current Capture component has no Linux window-scoping primitive" (`__main__.py`) |
| Linux keys, scrolls and coordinate clicks | Linux, unless `linux_allow_physical_input` | "coordinate input is disabled for native Linux" (`backends/linux_backend.py`) |
| Any Citrix run without `rdp_readiness_text` | Citrix | "governed Citrix execution requires a stable current-frame readiness marker before any action" (`__main__.py`) |
| A consequential step with no identity check | any, in practice unqualified pixel bundles | "UNARMED -- would act with no verified target identity" (`run_gate.py`) |

## Gaps

Ordered by value to a customer whose apps are Windows desktop apps, often
over RDP or Citrix. Each has an issue labelled
[`backend-parity`](https://github.com/OpenAdaptAI/OpenAdapt/issues?q=label%3Abackend-parity).

| # | Gap | Issue |
| --- | --- | --- |
| 1 | Self-repair and teach the fix refuse Windows, RDP and Citrix runs | [#1694](https://github.com/OpenAdaptAI/OpenAdapt/issues/1694) |
| 2 | No third-party Windows desktop app has been run; CI never replays through `WindowsBackend` | [#1695](https://github.com/OpenAdaptAI/OpenAdapt/issues/1695) |
| 3 | No real Citrix session has been run, from outside or inside the session | [#1696](https://github.com/OpenAdaptAI/OpenAdapt/issues/1696) |
| 4 | The governed run path (person recording, run gate, profiles, approvals, hold and resume) has never run on a real RDP session | [#1697](https://github.com/OpenAdaptAI/OpenAdapt/issues/1697) |
| 5 | Pixel runs stop falsely too often, and pixel identity can only veto | [#1698](https://github.com/OpenAdaptAI/OpenAdapt/issues/1698) |
| 6 | Rollback is refused on desktop and remote backends | [#1699](https://github.com/OpenAdaptAI/OpenAdapt/issues/1699) |
| 7 | Shadow mode and the agent guard can't see a desktop client's writes | [#1700](https://github.com/OpenAdaptAI/OpenAdapt/issues/1700) |
| 8 | `qualify campaign` refuses Windows, RDP and Citrix | [#1701](https://github.com/OpenAdaptAI/OpenAdapt/issues/1701) |
| 9 | An agent sees no controls on Windows, macOS or Linux (`authoring_tree()`) | [#1702](https://github.com/OpenAdaptAI/OpenAdapt/issues/1702) |
| 10 | `serve-execute` and `compile-agent` refuse non-web surfaces | [#1703](https://github.com/OpenAdaptAI/OpenAdapt/issues/1703) |
| 11 | Linux compile and run have no real-app test; the GTK3 AT-SPI job never runs | [#1704](https://github.com/OpenAdaptAI/OpenAdapt/issues/1704) |
| 12 | macOS has no record, compile and run result on a real app | [#1705](https://github.com/OpenAdaptAI/OpenAdapt/issues/1705) |

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

