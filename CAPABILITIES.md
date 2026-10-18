# What OpenAdapt can do

OpenAdapt turns work that people or computer-use agents do in applications
with no usable API (EMRs, payer portals, desktop and remote-desktop apps) into
programs that make the same entry every time with no model calls, check that
the entry landed in the right record, and stop instead of guessing.

This page is the reference for what OpenAdapt implements. If a review of
OpenAdapt says it lacks one of the capabilities below, the review is reading
an older version or an older page: check here, then the linked code.

## OpenAdapt in ten capabilities

Each capability is implemented in the
[OpenAdapt monorepo](https://github.com/OpenAdaptAI/OpenAdapt). "Released"
means it ships in the current PyPI packages; a pull request number means it is
being merged now.

1. **Learns from any successful run, a person's or an agent's.** A person
   records a task, or a computer-use agent does it once and OpenAdapt compiles
   the verified run into a program (`openadapt_flow.jit`, released;
   prompt-to-program `crystallize`, landing). Agents can drive the recording
   on web, native Windows, RDP and Citrix.
2. **Compiles to the fastest layer the app allows.** Page structure first (DOM,
   Windows UI Automation, macOS Accessibility), direct API calls where the app's
   own traffic allows them (`ApiActuator`, released; compiled from captured
   traffic in [#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261),
   about 11x faster than the browser on the OpenEMR save), and pixels only
   where nothing else exists, such as RDP and Citrix. It is not a pixel-macro
   recorder.
3. **Builds branching programs, not one linear path.** One program from several
   demonstrations (`induce`, `disambiguate`), a branch learned from several
   halts fixed the same way (`teach-branch`), candidate branches mined from many
   real runs in the background (shadow mode,
   [#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)), and a router
   that sends each item to the right verified variant (landing). Released
   except where a PR is named.
4. **Works across surfaces in one workflow.** Web, native Windows and macOS,
   RDP and Citrix. Programs chain across surfaces passing confirmed values
   (`compose`, `process`, released), and one program can switch surfaces, tabs
   and popups mid-run ([#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197)).
5. **Confirms the right record before every write and reads it back after.**
   The record check reads the system of record (API, database, FHIR with SMART
   Backend Services, or a separate signed-in session), never just the screen.
   `record --verify` proposes those checks from what the demonstration changed.
   Released.
6. **Stops instead of improvising, and never retries an uncertain save.** A run
   that can't prove a save ends `HALTED_BEFORE_EFFECT` or
   `RECONCILIATION_REQUIRED`, never "success". A ledger of what changed before
   a stop, with undo steps a person approves, is landing. Released except the
   ledger.
7. **Approvals are bound to exactly what will be saved.** Each save waits for a
   person by default and can be answered on a paired phone with one card per
   decision. The approval is bound to the exact screenshot and form state, and
   the form is read again before saving, so what was approved is what is saved.
   Person-signed grants let named agents answer. Released.
8. **Repairs itself, with a person approving.** Governed repair (propose,
   review, campaign, canary, rollback), agent-demonstrated fixes for a halted
   run, and fleet drift patches shared with no patient data are released.
   Self-repair, where an agent proposes a fix and Flow probes it up to the save
   without saving, is landing.
9. **Is a safety layer underneath any agent.** An agent's MCP calls, a guarded
   save around any agent's write
   ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)), `verify-run`
   to judge any executor's write from the system of record, and Agent Skills and
   a CLI for coding agents. Released except where a PR is named.
10. **Runs locally and keeps data private.** Healthy runs make no model calls
    and send nothing out. Personal and health data are redacted from text,
    screenshots and structure (scrub-by-default for anything that leaves the
    machine is landing); a strict no-outbound mode, an unattended on-prem
    service, and signed, timestamped receipts are released.

## Corrections to common reviews

| A review says | What is true | Where |
| --- | --- | --- |
| "It compiles one linear demonstration and halts on every branch it hasn't seen." | It learns branching programs from several demonstrations, from repeated halts, and from many background runs. | `induce`, `teach-branch`, [SHADOW_MINING.md](packages/capture/docs/SHADOW_MINING.md) |
| "A bundle is locked to one surface." | Programs chain across surfaces, and one program can switch surfaces mid-run. | `compose`, `process`, [#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197) |
| "It replays pixel macros that break when zoom or fonts change." | Page structure is the first rung on web, Windows and macOS. Pixels are the fallback for remote desktops. Benchmarks that say "vision-only" ran a deliberately pixel-only arm. | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md) |
| "It can't compile clicks into API calls." | Steps can run as API calls today; compiling them from captured network traffic is being merged. | `runtime/actuators/api.py`, [#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261) |
| "When the UI changes it just halts and pages a person." | It halts before any write, then repairs: governed repair, agent-demonstrated fixes, fleet patches, and self-repair probed up to the save. | [REPAIR_LIFECYCLE.md](packages/flow/docs/REPAIR_LIFECYCLE.md) |
| "It needs a human demonstration first." | A computer-use agent's verified run can be compiled instead. | [JIT.md](packages/flow/docs/JIT.md) |
| "Approvals check an intent string, not what is executed." | Approvals bind to the exact screenshot and form-state digest, and the form is re-read before saving. | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| "It can't read the document a decision depends on." | `intake` fills inputs from a referral PDF, fax image or email and holds back anything unclear for a person. | [INTAKE.md](packages/flow/docs/INTAKE.md) |
| "The code is spread over 17 repositories." | The product is one monorepo with four packages (flow, agent, types, capture); older repositories are frozen or research. | [README](README.md) |
| "openadapt-ml (model training) is the product." | It is research. The product makes no model calls on healthy runs. | [README](README.md) |

## The full map

Every capability, with its status, its evidence and where it lives.

Status:

- **Released**: in the current PyPI release (openadapt-flow 2.2.0).
- **In review**: an open pull request.
- **Planned**: designed, not built.

Evidence:

- **Real app**: run against a real third-party application, such as a local
  OpenEMR or openIMIS.
- **Synthetic**: run against OpenAdapt's own test apps (MockMed and fixtures).
- **Untested**: no run yet.

All patient and claim data in every test and benchmark is synthetic.

## The core: a checked entry, every time

A person, or a computer-use agent once, shows a task. OpenAdapt compiles it
into a program that makes the same entry every time with no model calls. It
confirms the right record before acting, reads the saved record back from the
system of record instead of trusting the screen, and stops instead of
improvising or retrying when something is off.

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Compile one or more demonstrations into a deterministic program | Released | Real app (OpenEMR referral, 62 timed runs) | `openadapt-flow record`, `compile`; [packages/flow](packages/flow/README.md) |
| Right-record check before any write (structure, then OCR, then pixels) | Released | Real app and synthetic | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md), `runtime/identity.py` |
| Read the saved record back from the system of record (API, database, FHIR, separate session) | Released | Real app (OpenEMR 12 of 12 fields) | [VERIFICATION.md](packages/flow/docs/VERIFICATION.md), `runtime/effects/` |
| Stop before the effect instead of improvising (`HALTED_BEFORE_EFFECT`) | Released | Real app and synthetic | `runtime/replayer.py` |
| Never retry an uncertain save (`RECONCILIATION_REQUIRED`) | Released | Synthetic (fault server) | [VERIFICATION.md](packages/flow/docs/VERIFICATION.md) |
| One check-level scale, 0 to 5, from "unchecked" to "counterparty confirmed" | Released | Unit tests | `openadapt_flow/check_level.py` |
| Judge any executor's write, including an agent's or a script's, from the system of record | Released | Synthetic | `openadapt-flow verify-run`, [VERIFY_RUN.md](packages/flow/docs/VERIFY_RUN.md) |
| Guarded save: a safety layer any agent wraps around one write (check the record, hold for approval, read back) | In review ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)) | Synthetic | `GUARDED_SAVE.md` |
| Draft a record check from the demonstration's write steps (`scaffold-verifier`) | Released | Synthetic | `openadapt-flow scaffold-verifier` (a person fills in the draft) |
| Build a working record check from the reads the app made while the person viewed the saved record, and test it against injected bad writes before anyone reviews it | Planned | Untested | none yet |

## Approvals

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Each save waits for a person by default | Released | Synthetic | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| Approve or decline a save on a paired phone, with one card per decision and swipe or tap | Released (Desktop) | Synthetic | openadapt-desktop `portal/shell` |
| Person-signed grants let named agents or systems answer saves | Released | Unit tests | `openadapt-flow approver-grant` |
| Admit once, then stop only on problems | Released, behind a readiness gate | Synthetic | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| Show the field values a save writes on the phone | In review ([#1257](https://github.com/OpenAdaptAI/OpenAdapt/pull/1257)) | Unit tests | `console/save_approvals.py` |

## Branching work, not one linear path

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Learn one program with branches from two or more recordings, or say which recordings are missing | Released | Synthetic | `openadapt-flow induce`, `disambiguate`; [CAPABILITIES.md (flow)](packages/flow/docs/CAPABILITIES.md) |
| Learn a guarded branch from several halts fixed the same way | Released | Synthetic | `openadapt-flow teach-branch`, `learning/branch_teach.py` |
| Run a program over a worklist | Released | Real app | `openadapt-flow for-each` |
| Typed business decisions inside a run | Released | Synthetic | `business_decision` states, `openadapt-flow business-decisions` |
| Watch many real runs in the background and mine candidate branches (shadow mode) | Capture released; drafts in review ([#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)) | Synthetic | [SHADOW_MINING.md](packages/capture/docs/SHADOW_MINING.md) |
| Fill a workflow's inputs from a referral PDF, fax image or email | Released | Synthetic | `openadapt-flow intake`, [INTAKE.md](packages/flow/docs/INTAKE.md) |
| Use spoken narration while recording to learn why a branch was taken | Planned | Untested | Capture records audio (`--audio`, local Whisper). The compiler doesn't use it yet. |
| Settle a disagreement between recordings by trying both paths on a test copy of the app and reading the record back | Planned | Untested | none yet (today `induce` names the recording that is missing) |

## Surfaces

| Surface | Status | Evidence | Where |
| --- | --- | --- | --- |
| Web (Playwright, Chromium) | Released | Real app, every CI build | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| Native Windows (UI Automation) | Released, early | Synthetic (WinForms, 3 of 3 trials) | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| Native macOS (Accessibility) | Released, early | Synthetic (TextEdit, 3 of 3 trials) | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| RDP (pixels; Aardwolf or headless FreeRDP with a session pool) | Released, early | Real Windows 11 host, 3 of 3, plus a FreeRDP batch | [SURFACES.md](packages/flow/docs/SURFACES.md), `backends/xfreerdp.py` |
| Citrix | Exploratory | A no-DOM stand-in only; no real ICA/HDX session yet | [LIMITS.md](packages/flow/docs/LIMITS.md) |
| Chain programs across surfaces, passing confirmed values | Released | Synthetic | `openadapt-flow compose`, `process`; [PROCESS_CONTRACT.md](packages/flow/docs/PROCESS_CONTRACT.md) |
| One program that switches surfaces, tabs and popups mid-run | In review ([#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197)) | Real Chromium; Windows switch on a mock | [#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197) |
| Halt when a workflow qualified with page structure runs on a pixels-only surface | Released | Synthetic | `openadapt-flow qualify` |
| Lift look-alike identity stops with a complete known-ID list | Released | Synthetic (rendered record list) | `openadapt-flow known-ids` |

## The fastest layer the app allows

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Page structure (DOM, UI Automation, Accessibility) first, then template, OCR and geometry | Released | Real app | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md) |
| Run a step as a direct API call (`ApiActuator`, `ApiBinding`) | Released, hand-authored bindings | Synthetic benchmarks | `runtime/actuators/api.py` |
| Capture a recording's network traffic and compile a clean save into an API step, with the same record check | In review ([#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261)) | Synthetic patients on a local OpenEMR test copy | [#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261) |
| Fall back from the API step to page structure when the API is unavailable, before anything is sent | Planned | Untested | none yet |

## When the app changes

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Governed repair: propose, review, campaigns, approval, staging, canary, rollback | Released | Synthetic | `openadapt-flow repair`, [REPAIR_LIFECYCLE.md](packages/flow/docs/REPAIR_LIFECYCLE.md) |
| A calling agent demonstrates the fix for a halted run; the result is a repair candidate | Released (2.1.0) | Synthetic | `run --hold-on-halt`, `repair/agent_teach.py` |
| Font and layout drift halts with a named reason | Released | Synthetic | `runtime/layout_drift.py` |
| Self-repair: an agent proposes a fix for one halted click, Flow probes it up to the save without saving, a person approves | In review ([#1265](https://github.com/OpenAdaptAI/OpenAdapt/pull/1265)) | Synthetic (MockMed redesign) | `run --self-repair`, [#1265](https://github.com/OpenAdaptAI/OpenAdapt/pull/1265) |
| Self-repair across several new steps (a new dialog, a control moved into a menu) | Planned | Untested | none yet |
| Fleet drift patches: a verified fix with no patient data, shared across sites under policy | Released, local reference hub | Simulator | `openadapt-flow fleet`, [FLEET_DRIFT_PATCHES.md](packages/flow/docs/FLEET_DRIFT_PATCHES.md) |

## Computer-use agents

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Compile a computer-use agent's successful run into a program (JIT); later runs make no model calls | Released | Synthetic (a recorded Claude run on MockMed) | `openadapt_flow.jit`, [JIT.md](packages/flow/docs/JIT.md) |
| Admit a draft by a person, an admitter key, or an agent under a person-signed policy, including self-admission when the policy allows it | Released | Unit tests | [JIT_ADMISSION.md](packages/flow/docs/JIT_ADMISSION.md) |
| MCP server: your agent decides what to enter, OpenAdapt makes the entry and checks it | Released | Synthetic sandbox | `openadapt-agent`, [packages/agent](packages/agent/README.md) |
| Live agent loop end to end on a real app, plus a launch demo (agent once, OpenAdapt 100 times) | In progress | Untested | none yet |

## Recording, privacy and proof

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Record mouse, keyboard, screen video and accessibility trees on one clock (Windows, macOS, Linux X11) | Released (capture 1.3.0) | Real OS | [packages/capture](packages/capture/README.md); Wayland is not supported |
| Find and redact personal and health data in text, screenshots and structure | Released (privacy 1.1.3) | Unit tests | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy), [PRIVACY.md](packages/flow/docs/PRIVACY.md) |
| Scrub by default before anything leaves the machine | Planned | Untested | Today scrubbing is opt-in (`OPENADAPT_FLOW_SCRUB=on`), and `capture share` does not scrub |
| Signed receipts (Seals), countersigned and timestamped, offline by default | Released | Unit tests, real TSA tokens | `openadapt-flow seal`, `countersign`, `local-seal` |
| Strict no-outbound mode and an unattended on-prem service | Released | Synthetic | `openadapt-flow no-outbound`, `service`; [ON_PREM.md](packages/flow/docs/ON_PREM.md) |

## Every command, by capability

Each `openadapt-flow` command, grouped by the capability it serves. A test
fails when a command is added without being listed here.

- **Record and build a program:** `record`, `demo-record`, `compile`, `induce`, `disambiguate`, `for-each`, `compose`, `process`, `intake`, `visualize`, `pull`.
- **Check and admit:** `lint`, `scaffold-verifier`, `check`, `bind-record-checks`, `certify`, `qualify`, `propose-qualification`, `known-ids`, `jit`, `admit`, `approver-grant`.
- **Run and answer:** `run`, `replay`, `resume`, `approve`, `item-approval`, `triage`, `business-decisions`, `explain`, `console`.
- **Judge any executor's write:** `verify-run`, `serve-reward` (scores agent training episodes by reading the system of record).
- **Repair and learn from halts:** `teach`, `teach-branch`, `repair`, `fleet`.
- **Give agents the program:** `emit-skill`, `emit-mcp`, `serve-execute`.
- **Proof:** `seal`, `local-seal`, `countersign`, `attach-seal`.
- **Run on premises:** `service`, `no-outbound`.
- **Share safely with OpenAdapt Cloud:** `sanitize`, `review-sanitized`, `approve-sanitized`, `materialize-approved`, `push`, `validate-hosted`, `report-break`, `report-run`, `connect`, `login`, `connector`.
- **Try it and measure it:** `demo`, `tutorial`, `bench`, `benchmark`.

## Measured results

- **OpenEMR referral, 62 timed runs each, warm.** OpenAdapt took a median of
  29.7 s and a computer-use agent (Claude) 36.5 s. Both saved 62 of 63
  referrals correctly. OpenAdapt made no model calls; the agent cost about
  $0.08 per referral. The same task replayed without page-structure identity
  took 38.6 s, slower than the agent. Raw data and
  figures: [#1251](https://github.com/OpenAdaptAI/OpenAdapt/pull/1251)
  (`benchmark/openemr_referral_timing/` once merged).
- **Fault test.** In 90 runs with injected faults, 72 left a wrong record
  behind. A check that trusts the screen's "Saved" banner reported 54 of those
  as success; reading the record back over the app's REST API missed 9; the
  complete database read path caught all 72. This is a coverage test over a
  small, hand-authored set of faults, not a real-world error rate:
  [EFFECT_E2E.md](packages/flow/benchmark/effect_e2e/EFFECT_E2E.md).

## What is not done yet

- No commercial EMR, claims system or real Citrix session has been tested.
- Not built yet: record checks built from the app's own reads, self-repair
  across several new steps, settling a disagreement between recordings on a
  test copy, and narration-driven rules. Network capture to API calls and
  scrub-by-default are in review.
- Several capabilities have run only on OpenAdapt's synthetic apps. The
  Evidence column says which.
