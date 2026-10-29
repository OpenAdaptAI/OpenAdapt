# What OpenAdapt can do

OpenAdapt turns work that people or computer-use agents do in applications
with no usable API (EMRs, payer portals, desktop and remote-desktop apps) into
programs that make the same entry every time with no model calls, check that
the entry landed in the right record, and stop instead of guessing.

This page indexes what OpenAdapt implements, with the status, the evidence and
the code for each capability:

- [Ten capabilities](#openadapt-in-ten-capabilities) and [often missed](#often-missed)
- [The core: a checked entry](#the-core-a-checked-entry-every-time),
  [approvals](#approvals), [branching work](#branching-work-not-one-linear-path)
- [Surfaces](#surfaces), [the fastest layer](#the-fastest-layer-the-app-allows),
  [when the app changes](#when-the-app-changes)
- [Computer-use agents](#computer-use-agents),
  [recording, privacy and proof](#recording-privacy-and-proof)
- [Every command](#every-command-by-capability) and
  [measured results](#measured-results)

## OpenAdapt in ten capabilities

Each capability is implemented in the
[OpenAdapt monorepo](https://github.com/OpenAdaptAI/OpenAdapt). "Released"
means it ships in the current PyPI packages; "on main" means merged into the
monorepo and installable from source.

1. **Learns from any successful run, a person's or an agent's.** A person
   records a task, or a computer-use agent does it once and OpenAdapt compiles
   the verified run into a program (`openadapt_flow.jit`, released;
   prompt-to-program `compile-agent`, on main; the agent can act through code
   instead of screenshots, [#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305)). Agents can drive the recording
   through the web, native Windows, RDP and Citrix backends. An agent connected over MCP can
   teach itself a new workflow and then call it as a tool
   (`openadapt-agent serve --teach`,
   [#1335](https://github.com/OpenAdaptAI/OpenAdapt/pull/1335)).
2. **Compiles to the fastest layer the app allows.** Page structure first (DOM,
   Windows UI Automation, macOS Accessibility), direct API calls where the app's
   own traffic allows them (`ApiActuator`, released; compiled from captured
   traffic in [#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261)),
   and pixels only
   where nothing else exists, such as a remote desktop. It is not a pixel-macro
   recorder.
3. **Builds branching programs, not one linear path.** One program from several
   demonstrations (`induce`, `disambiguate`), a branch learned from several
   halts fixed the same way (`teach-branch`), candidate branches mined from many
   real runs in the background (shadow mode,
   [#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)), and a router
   that sends each item to the right verified variant. Shadow mining and the
   router are on main; the rest is released.
4. **Works across surfaces in one workflow.** Backends for web, native Windows
   and macOS, Linux, RDP and Citrix. Programs chain across surfaces passing confirmed values
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
   a stop, with undo steps a person approves, is on main. The rest is
   released.
7. **Approvals are bound to exactly what will be saved.** Each save waits for a
   person by default and can be answered on a paired phone with one card per
   decision. The approval is bound to the exact screenshot and form state, and
   the form is read again before saving, so what was approved is what is saved.
   Person-signed grants let named agents answer. Released.
8. **Repairs itself, with a person approving.** Governed repair (propose,
   review, campaign, canary, rollback), agent-demonstrated fixes for a halted
   run, and fleet drift patches shared with no patient data are released.
   Self-repair, where an agent proposes a fix and Flow probes it up to the save
   without saving, is on main, and so are fixes of up to three new steps.
9. **Is a safety layer underneath any agent.** An agent's MCP calls, a task
   ledger that keeps a long job's rules, items, questions and checked saves on
   the server ([#1332](https://github.com/OpenAdaptAI/OpenAdapt/pull/1332)), a guarded save around any agent's write
   ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)), a guard for
   an agent you already run that holds its browser's saves with no change to
   the agent ([#1357](https://github.com/OpenAdaptAI/OpenAdapt/pull/1357)), `verify-run`
   to judge any executor's write from the system of record, and Agent Skills and
   a CLI for coding agents. Released except where a PR is named.
10. **Runs locally and keeps data private.** Healthy runs make no model calls
    and send nothing out. Personal and health data are redacted from text,
    screenshots and structure (scrub-by-default for anything that leaves the
    machine is on main); a strict no-outbound mode, an unattended on-prem
    service, and signed, timestamped receipts are released.

## Often missed

| What OpenAdapt does | Where |
| --- | --- |
| Learns branching programs from several demonstrations, from repeated halts, and from many background runs. | `induce`, `teach-branch`, [SHADOW_MINING.md](packages/capture/docs/SHADOW_MINING.md), [LEARN_FROM_MANY_RUNS.md](packages/flow/docs/LEARN_FROM_MANY_RUNS.md) |
| Chains programs across surfaces, and one program can switch surfaces, tabs and popups mid-run. | `compose`, `process`, [#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197) |
| Uses page structure first on web, Windows, macOS and Linux, with pixels as the fallback for remote desktops. Benchmarks labelled "vision-only" ran a deliberately pixel-only arm. | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md) |
| Replaces a single HTML form submission with the request the app itself sends, compiled from the network traffic captured while recording, then reads the record back. JSON or GraphQL requests, multi-step wizards and governed runs still use the screen steps. | `api`, [API_TIER.md](packages/flow/docs/API_TIER.md) |
| Builds the record check itself. After a save, the app usually reads the record back to show it; `scaffold-verifier` finds that read in the captured traffic or a read-only database, builds the check from it and tries to break it before a person reviews it. No API or SQL needs to be written by hand. | [EFFECT_KIT.md](packages/flow/docs/EFFECT_KIT.md#a-record-check-built-from-the-apps-own-reads-scaffold-verifier) |
| Checks a save on desktop, RDP and Citrix apps with no API at all, by looking the record up in the app's own screens in a separate session. | [EFFECT_KIT.md](packages/flow/docs/EFFECT_KIT.md#separate-session-desktop-read-back-desktop_readback) |
| Keeps pixel-path runs working across display scaling from 100% to 175% and font smoothing on or off, and stops rather than saves when the record changed. | [benchmark/pixel_scale](packages/flow/benchmark/pixel_scale/README.md) |
| Lists every change made before a stop, and reverses them with undo steps a person approves. | `undo`, [AFTER_A_STOP.md](packages/flow/docs/AFTER_A_STOP.md) |
| Repairs itself when the app changes: governed repair, agent-demonstrated fixes, fleet patches, and self-repair probed up to the save. | [REPAIR_LIFECYCLE.md](packages/flow/docs/REPAIR_LIFECYCLE.md) |
| Repairs a change that needs new steps, such as a new required dialog or a control moved into a menu: up to three new steps, each with its own record check, probed up to the save before one person approves. | [SELF_REPAIR.md](packages/flow/docs/SELF_REPAIR.md), [#1325](https://github.com/OpenAdaptAI/OpenAdapt/pull/1325) |
| Compiles a computer-use agent's verified run, with no human demonstration needed. | [JIT.md](packages/flow/docs/JIT.md) |
| The agent that does a task once can act through code, not a screenshot-and-click loop: it reads the page or accessibility tree, acts by role and name, keeps notes with a sandboxed read-only shell, and asks for a screenshot only when it needs one. It is arm 7 of the scoreboard. | `CODE_AS_ACTION.md` in [#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305) |
| Binds each approval to the exact screenshot and form state, and re-reads the form before saving. | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| Has a written threat model for prompt injection against the agent that teaches or repairs a program, with tests. | [THREAT_MODEL.md](packages/flow/docs/THREAT_MODEL.md) |
| Fills inputs from a referral PDF, fax image or email, and holds back anything unclear for a person. | `intake`, [INTAKE.md](packages/flow/docs/INTAKE.md) |
| Ships as one monorepo with separately installable packages (flow, agent, types, capture, privacy, desktop and tray), each released from the monorepo. | [README](README.md) |
| Makes no model calls on healthy runs. Model training (openadapt-ml) is separate research. | [README](README.md) |

## The full map

Every capability, with its status, its evidence and where it lives.

Status:

- **Released**: in the current PyPI release (openadapt-flow 2.2.0).
- **On main**: merged into the monorepo; installable from source.

Evidence:

- **Real app**: run against a real third-party application, such as a local
  OpenEMR or openIMIS.
- **Synthetic**: run against OpenAdapt's own test apps (MockMed and fixtures).

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
| Classify common local edits without a person (select-all, Enter in a field or dropdown, an in-form tab, an informational dialog's dismiss), only when the recording's network capture shows no write and a runtime check confirms only the expected change | On main ([#1366](https://github.com/OpenAdaptAI/OpenAdapt/pull/1366)) | Synthetic; OpenEMR referral recording re-admitted offline (violations 12 to 1 under `permissive`) | [LOCAL_EDIT_CLASSIFICATION.md](packages/flow/docs/design/LOCAL_EDIT_CLASSIFICATION.md), `compiler/local_edit.py` |
| Right-record check before any write (structure, then OCR, then pixels) | Released | Real app and synthetic | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md), `runtime/identity.py` |
| Read the saved record back from the system of record (API, database, FHIR, separate session) | Released | Real app (OpenEMR 12 of 12 fields) | [VERIFICATION.md](packages/flow/docs/VERIFICATION.md), `runtime/effects/` |
| Read values out of an app as a workflow step (READ), with a records format, a signed review queue and export to CSV or a database | On main ([#1229](https://github.com/OpenAdaptAI/OpenAdapt/pull/1229), [#1215](https://github.com/OpenAdaptAI/OpenAdapt/pull/1215), [#1218](https://github.com/OpenAdaptAI/OpenAdapt/pull/1218)–[#1224](https://github.com/OpenAdaptAI/OpenAdapt/pull/1224)) | Synthetic (mock desktop apps) | `openadapt_flow/interchange/` |
| Stop before the effect instead of improvising (`HALTED_BEFORE_EFFECT`) | Released | Real app and synthetic | `runtime/replayer.py` |
| Never retry an uncertain save (`RECONCILIATION_REQUIRED`) | Released | Synthetic (fault server) | [VERIFICATION.md](packages/flow/docs/VERIFICATION.md) |
| One check-level scale, 0 to 5, from "unchecked" to "counterparty confirmed" | Released | Unit tests | `openadapt_flow/check_level.py` |
| Judge any executor's write, including an agent's or a script's, from the system of record | Released | Synthetic | `openadapt-flow verify-run`, [VERIFY_RUN.md](packages/flow/docs/VERIFY_RUN.md) |
| Prove that nothing outside the declared change moved (frame conditions), and attribute concurrent edits by other users through the app's audit log or a modified-by field; the receipt signs which parts were checked | On main | Synthetic (MockMed, SQLite audit log) | `verify-run`, [VERIFICATION_COVERAGE.md](packages/flow/docs/VERIFICATION_COVERAGE.md) |
| Admit a record check only after a fault suite written to a store copy shows it catches every fault class it claims, including writes outside its read (another table, a side row for the saved record), and sign each class's result into the receipt | On main | Synthetic (in-memory REST store, SQLite copy); OpenEMR stand-in built from the pinned fixture's dump | `openadapt_flow/fault_gate.py`, `scaffold-verifier --fault-gate`, `verify-run` `admission:` |
| Check that the saved record matches its source document, not only the parameters: intake keeps each value's line, a second reading must agree before a guarded save, and a round trip compares the read-back with the document | On main | Synthetic (MockMed, text referral) | `openadapt_flow/provenance.py`, `intake`, `verify-run --provenance`, guarded save |
| Check one write from Python, without the CLI: one API (`openadapt_flow.verifier`: `begin_write`, `verify_write`; 2.4.1's `capture` form still works, deprecated) with one outcome (`CONFIRMED`, `REFUTED`, `UNCERTAIN`), the receipt, which parts were checked, and the result's scope (read path, stores read, time window, what it doesn't cover) | On main | Synthetic (SQLite; MockMed's fault server over real HTTP) | `openadapt_flow/verifier.py`, [VERIFIER_API.md](packages/flow/docs/VERIFIER_API.md) |
| Never send a write twice while its outcome is unknown: an append-only ledger on local disk (pluggable backend), keyed by the write's identity, that every adapter, the MCP tools and the CLI consult before sending; a read-back against the attempt's own baseline resolves it, and a refuted write waits for a person (`openadapt verify ledger list`, `reconcile`, `release`) | On main | Synthetic (MockMed's fault server: a worker killed between the send and the read-back, killed during a timed-out send, killed before the send, a late write, and two worker processes started together on one write) | `openadapt_flow/write_ledger.py`, [VERIFIER_API.md](packages/flow/docs/VERIFIER_API.md#the-uncertain-write-ledger) |
| Guarded save: a safety layer any agent wraps around one write (check the record, hold for approval, read back) | On main ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)) | Synthetic | `GUARDED_SAVE.md` |
| A person's approval in Desktop is signed with a key kept behind Touch ID on a Mac: one-prompt setup in the app, a prompt for every answer, and Flow never sees the key | On main ([#1500](https://github.com/OpenAdaptAI/OpenAdapt/pull/1500)) | Unit tests; opt-in test on a real Secure Enclave | `packages/desktop/openadapt_desktop/approver_keychain.py`, [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md#the-approver-key) |
| The approver root Desktop trusts can be pinned by an administrator, in a file this user can't write (macOS, Linux, Windows); Desktop checks the owner, mode or ACL and refuses links. A Mac without one uses the root it set up behind Touch ID, read with Keychain prompts off; the two disagreeing is refused | On main | Unit tests per attack on real files; Windows ACL rules unit-tested, real-ACL test on Windows CI; opt-in test against the real login Keychain | `openadapt-desktop approver-pin`, `packages/desktop/openadapt_desktop/approver_anchor.py`, [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md#the-approver-key) |
| Review stops: every stop as one card with the evidence side by side (the item next to each possible match, field by field), answered with one key or tap on the computer or a paired phone, signed with the approver key, with batches for similar stops. The queue presents and collects; a changed or expired card changes nothing | On main ([#1519](https://github.com/OpenAdaptAI/OpenAdapt/pull/1519)) | Unit tests; a keystroke-level estimate on 20 soak stops (26.7 s to 8.2 s to decide, a model, not a person) | `packages/desktop/openadapt_desktop/stop_queue.py`, [benchmark/stop_queue](packages/desktop/benchmark/stop_queue/README.md) |
| Draft a record check from the demonstration's write steps (`scaffold-verifier`) | Released | Synthetic | `openadapt-flow scaffold-verifier` (a person fills in the draft) |
| Build a working record check from the reads the app made while the person viewed the saved record, and test it against injected bad writes before anyone reviews it | On main ([#1284](https://github.com/OpenAdaptAI/OpenAdapt/pull/1284)) | Synthetic (MockMed, OpenEMR fixture) | `openadapt-flow scaffold-verifier` |
| Check a save on desktop, RDP or Citrix by looking the record up in a separate read-only session | On main ([#1280](https://github.com/OpenAdaptAI/OpenAdapt/pull/1280)) | Synthetic (WinForms, RDP and Citrix stand-ins) | `runtime/effects/` |
| Learn the record check from the recording, when the person looks the record up after saving | On main ([#1290](https://github.com/OpenAdaptAI/OpenAdapt/pull/1290)) | Synthetic | `record --verify` |
| A record check's duplicate key can be several fields together, such as last name, first name and birth date: the check fails when more than one record carries all of them, counting records that were already there | On main ([#1393](https://github.com/OpenAdaptAI/OpenAdapt/issues/1393)) | Synthetic (OpenEMR-shaped patient rows) | verify.yaml `record.duplicate_key: [lname, fname, DOB]`, `confirm_params(idempotency_key=[...])`, `Effect.composite_key` |
| Checked later: a save confirmed by a later report or acknowledgment, never counted as done until then | On main ([#1297](https://github.com/OpenAdaptAI/OpenAdapt/pull/1297)) | Synthetic | `SAVED_AWAITING_CHECK` |
| Keep the server's own answer to a save as lower-level evidence on the receipt | On main ([#1298](https://github.com/OpenAdaptAI/OpenAdapt/pull/1298)) | Synthetic | receipts |
| A ledger of what changed before a stop, a write-ordering lint, and undo steps a person approves | On main ([#1263](https://github.com/OpenAdaptAI/OpenAdapt/pull/1263)) | Synthetic (MockMed) | change ledger |
| Propose an undo for each saving step (another recording, captured traffic, or the old value), keep it only if a test copy shows every record restored, and offer it after a stop once a person approves it | On main ([#1346](https://github.com/OpenAdaptAI/OpenAdapt/pull/1346)) | Synthetic (MockMed, MockLoan, live browser) | `openadapt-flow undo propose`, `undo accept`; [AFTER_A_STOP.md](packages/flow/docs/AFTER_A_STOP.md) |

## Approvals

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Each save waits for a person by default | Released | Synthetic | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| Approve or decline a save on a paired phone, with one card per decision and swipe or tap | Released (Desktop) | Synthetic | openadapt-desktop `portal/shell` |
| Person-signed grants let named agents or systems answer saves | Released | Unit tests | `openadapt-flow approver-grant` |
| Admit once, then stop only on problems | On main ([#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)), behind a readiness gate | Synthetic | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md) |
| Show the field values a save writes on the phone | On main ([#1257](https://github.com/OpenAdaptAI/OpenAdapt/pull/1257)) | Unit tests | `console/save_approvals.py` |
| Show why an agent asked for a save: who asked, its own reason (as a quote), the screen text it saw, and any of that text that reads like an instruction to an agent | On main ([#1359](https://github.com/OpenAdaptAI/OpenAdapt/pull/1359)) | Unit tests, synthetic | `ApprovalProvenance` in `approval_mode/item_approval.py`; the phone card in openadapt-desktop `portal/shell` |

## Branching work, not one linear path

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Learn one program with branches from two or more recordings, or say which recordings are missing | Released | Synthetic | `openadapt-flow induce`, `disambiguate`; [CAPABILITIES.md (flow)](packages/flow/docs/CAPABILITIES.md) |
| Learn a guarded branch from several halts fixed the same way | Released | Synthetic | `openadapt-flow teach-branch`, `learning/branch_teach.py` |
| Run a program over a worklist | Released | Real app | `openadapt-flow for-each` |
| Learn a worklist loop from recordings that search a record, open it, work it and go back to the list, even when the last row skips the return or rows differ (a field some rows lack, a write some rows get behind a screen check a person accepts). Each pass selects the row's own record by its values, verifies it, and checks that row's write. Certified and governed runs refuse it until qualification covers it | On main ([#1435](https://github.com/OpenAdaptAI/OpenAdapt/pull/1435)) | Synthetic (held-out rows run through the interpreter against an in-memory app) | `openadapt-flow induce`; `Step.target_param` |
| Score whether a program generalizes by running held-out items: each item runs through the replayer with its own inputs and record check, and an independent check the program never sees says whether the intended record got exactly the intended change (generalized, halted safely, silent wrong, wrong effect, unconfirmed write); a loop program runs each item as a one-row worklist. `induce --held-out` stays a structural shape check that runs nothing | On main ([#1527](https://github.com/OpenAdaptAI/OpenAdapt/pull/1527)) | Synthetic (an induced worklist loop scored on held-out rows against an in-memory chart app) | `openadapt_flow.validation.behavioral_generalization` |
| Typed business decisions inside a run | Released | Synthetic | `business_decision` states, `openadapt-flow business-decisions` |
| Watch many real runs in the background and mine candidate branches (shadow mode) | On main ([#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)) | Synthetic | [SHADOW_MINING.md](packages/capture/docs/SHADOW_MINING.md), [LEARN_FROM_MANY_RUNS.md](packages/flow/docs/LEARN_FROM_MANY_RUNS.md) |
| Fill a workflow's inputs from a referral PDF, fax image or email | Released | Synthetic | `openadapt-flow intake`, [INTAKE.md](packages/flow/docs/INTAKE.md) |
| Use spoken narration while recording to propose the rule behind a branch | On main ([#1291](https://github.com/OpenAdaptAI/OpenAdapt/pull/1291)) | Synthetic (generated audio) | `openadapt-capture[audio,privacy]` |
| Settle a disagreement between recordings by trying both paths on a test copy of the app and reading the record back | On main ([#1276](https://github.com/OpenAdaptAI/OpenAdapt/pull/1276), [#1289](https://github.com/OpenAdaptAI/OpenAdapt/pull/1289)) | Synthetic (MockMed; desktop and remote test copies) | `induce --probe-on` |
| Route each item to the right verified variant of a program | On main ([#1264](https://github.com/OpenAdaptAI/OpenAdapt/pull/1264)) | Synthetic | `variants`, `route` |

## Surfaces

| Surface | Status | Evidence | Where |
| --- | --- | --- | --- |
| Web (Playwright, Chromium) | Released | Real app, every CI build | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| Native Windows (UI Automation) | Released | Synthetic (WinForms, 3 of 3 trials) | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| Native macOS (Accessibility) | Released | Synthetic (TextEdit, 3 of 3 trials) | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| RDP (pixels; Aardwolf or headless FreeRDP with a session pool) | Released | Real Windows 11 host, 3 of 3, plus a FreeRDP batch; through the stock Remote Desktop client on a real H.264/AVC444 session, 105 runs on two apps: 55 saves confirmed in the database, 0 wrong-record saves, 40 false stops ranked by cause | [SURFACES.md](packages/flow/docs/SURFACES.md), `backends/xfreerdp.py`, [benchmark/rdp_avc_real](packages/flow/benchmark/rdp_avc_real/README.md) |
| Citrix and other virtual desktops, with OpenAdapt installed inside the session (`in_session`): the same structure-first path as a local desktop | Released | Same evidence as the Windows path | [SURFACES.md](packages/flow/docs/SURFACES.md) |
| Citrix from outside the session, pixels only (`remote_display`), the fallback when nothing can be installed inside | Exploratory | A Citrix stand-in with no DOM: 3 healthy runs, 3 stops on drift | [LIMITS.md](packages/flow/docs/LIMITS.md) |
| Chain programs across surfaces, passing confirmed values | Released | Synthetic | `openadapt-flow compose`, `process`; [PROCESS_CONTRACT.md](packages/flow/docs/PROCESS_CONTRACT.md) |
| One program that switches surfaces, tabs and popups mid-run | On main ([#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197)) | Real Chromium; Windows switch on a mock | [#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197) |
| Halt when a workflow qualified with page structure runs on a pixels-only surface | Released | Synthetic | `openadapt-flow qualify` |
| Stop before acting when another shown row has the same identity; when the rows carry an app-owned record id (a `data-*` id, the row link), act on the row with the recorded id; on a surface that cannot count the rows, stop before any step that leads into a write | On main ([#1504](https://github.com/OpenAdaptAI/OpenAdapt/pull/1504)) | Real browser (headless Chromium worklist with identical rows) | `runtime/identity_census.py`; `PlaywrightBackend.identity_candidates_at` |
| Lift look-alike identity stops with a complete known-ID list | Released | Synthetic (rendered record list) | `openadapt-flow known-ids` |
| Take extra looks before stopping on a look-alike record ID: fresh frames, the known-ID list, and a select-and-copy of the ID where the surface allows it | On main ([#1333](https://github.com/OpenAdaptAI/OpenAdapt/pull/1333)) | Synthetic (dense record list in a browser) | `runtime.identity_copy_look` in deployment.yaml; [IDENTITY_LOOKS.md](packages/flow/benchmark/identity_looks/IDENTITY_LOOKS.md) |
| Check an icon-only click target's hover response (highlight, tooltip) before clicking on a pixel surface, and re-check the target and identity regions on a fresh frame right before every pixel click | On main ([#1334](https://github.com/OpenAdaptAI/OpenAdapt/pull/1334)) | Synthetic (in-process kiosk) | `Recorder(capture_hover=True)`; [hover_identity](packages/flow/benchmark/hover_identity/README.md) |

## The fastest layer the app allows

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Page structure (DOM, UI Automation, Accessibility) first, then template, OCR and geometry | Released | Real app | [RESOLUTION_LADDER.md](packages/flow/docs/RESOLUTION_LADDER.md) |
| Run a step as a direct API call (`ApiActuator`, `ApiBinding`) | Released, hand-authored bindings | Synthetic benchmarks | `runtime/actuators/api.py` |
| Capture a recording's network traffic and compile a clean save into an API step, with the same record check | On main ([#1269](https://github.com/OpenAdaptAI/OpenAdapt/pull/1269)) | Synthetic patients on a local OpenEMR test copy | network capture, `ApiBinding` |
| Fall back from the API step to page structure when the API is unavailable, before anything is sent | On main ([#1269](https://github.com/OpenAdaptAI/OpenAdapt/pull/1269)) | Synthetic | [#1269](https://github.com/OpenAdaptAI/OpenAdapt/pull/1269) |
| Approve an API step's exact request under per-item approval: one digest binds the request bytes, the record and the record check, and a changed request is not sent | On main ([#1355](https://github.com/OpenAdaptAI/OpenAdapt/pull/1355)) | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#under-an-approval-mode) |
| Run an API step in a durable run: checkpoint the span after the save is sent, never resend or redo a save that may have landed, and reconcile it by reading the record back | On main ([#1358](https://github.com/OpenAdaptAI/OpenAdapt/pull/1358)) | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#in-a-durable-run) |
| Run an admitted API step under a governed run authorization and the Standard or Regulated profile: the authorization names the API step by digest, the person who admits it names the right-record parameter, and the outcome reads `VERIFIED` | On main ([#1361](https://github.com/OpenAdaptAI/OpenAdapt/pull/1361)) | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#under-governance) |
| Run an admitted API step under a managed (Cloud) dispatch whose authorization names it: the API step rows are inside the dispatch digest Cloud signs, and each send still needs Cloud's delivery permit | On main | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#under-governance) |
| Run an admitted API step under a signed production qualification admission when its qualified effect contract names the API path, and let a qualification case run the path its contract names | On main | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#under-governance) |
| Defer an API save's read-back with pipelined read-back, as for a GUI save, and keep self-repair's probes and resumed run on the repaired GUI step | On main ([#1362](https://github.com/OpenAdaptAI/OpenAdapt/pull/1362)) | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#with-parallel-workers-pipelined-read-back-and-self-repair) |
| Defer the last record check under durable and governed runs: the durable pause holds the check, so a crash re-runs only the check and never resends the save | On main | Synthetic | [THROUGHPUT.md](packages/flow/docs/THROUGHPUT.md#under-durable-and-governed-runs) |
| Run an admitted API step inside a multi-surface program, bound to its one browser surface and that surface's origin; its folded steps record the surface | On main | Synthetic | [API_TIER.md](packages/flow/docs/API_TIER.md#in-a-multi-surface-program) |
| Say, for every feature under every run profile, whether it works or is refused and why, checked against the runtime in CI | On main ([#1364](https://github.com/OpenAdaptAI/OpenAdapt/pull/1364)) | Synthetic | [COMPATIBILITY.md](packages/flow/docs/COMPATIBILITY.md) |
| Run a worklist on several workers at once, one signed-in session each, each item claimed once, with a stop on any worker stopping all | On main ([#1296](https://github.com/OpenAdaptAI/OpenAdapt/pull/1296)) | Synthetic patients on a local OpenEMR test copy | `--workers`, [THROUGHPUT.md](packages/flow/docs/THROUGHPUT.md) |
| Read a save back while the next item runs (pipelined read-back, REST, FHIR, GraphQL or SQL record checks) | On main ([#1296](https://github.com/OpenAdaptAI/OpenAdapt/pull/1296)) | Synthetic | `--readback-window`, [THROUGHPUT.md](packages/flow/docs/THROUGHPUT.md) |
| Read a paged REST or FHIR record list whole (FHIR `next` links and `total`, REST `records_next_path`), or not at all: a page elsewhere, a failed page, a redirect or a count that doesn't match the total is unreadable | On main ([#1538](https://github.com/OpenAdaptAI/OpenAdapt/pull/1538)) | Synthetic (local FHIR and REST servers) | `effects: kind: rest` `records_next_path`, `kind: fhir`; [EFFECT_KIT.md](packages/flow/docs/EFFECT_KIT.md) |
| A faster speed profile that keeps every check (one insert per plain text field, shorter page-quiet wait) | On main ([#1296](https://github.com/OpenAdaptAI/OpenAdapt/pull/1296)) | Synthetic | `runtime.speed: fast`, [THROUGHPUT.md](packages/flow/docs/THROUGHPUT.md) |
| Record where each step's time goes in every run report | On main ([#1296](https://github.com/OpenAdaptAI/OpenAdapt/pull/1296)) | Real app and synthetic | `timings` in `report.json` |

## When the app changes

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Governed repair: propose, review, campaigns, approval, staging, canary, rollback | Released | Synthetic | `openadapt-flow repair`, [REPAIR_LIFECYCLE.md](packages/flow/docs/REPAIR_LIFECYCLE.md) |
| A calling agent demonstrates the fix for a halted run; the result is a repair candidate | Released (2.1.0) | Synthetic | `run --hold-on-halt`, `repair/agent_teach.py` |
| Font and layout drift halts with a named reason | Released | Synthetic | `runtime/layout_drift.py` |
| Self-repair: an agent proposes a fix for one halted click, Flow probes it up to the save without saving, a person approves | On main ([#1265](https://github.com/OpenAdaptAI/OpenAdapt/pull/1265)) | Synthetic (MockMed redesign) | `run --self-repair`, [#1265](https://github.com/OpenAdaptAI/OpenAdapt/pull/1265) |
| A scroll that never finds the next click's target halts on that click, so self-repair can take a relabelled control behind a scroll; the refusal for unreviewed-risk steps names them | On main ([#1337](https://github.com/OpenAdaptAI/OpenAdapt/pull/1337)) | Synthetic; openIMIS drift row checked offline on its recorded frames | `runtime/replayer.py`, `qualify set-risk` |
| Self-repair across several new steps: a bridge of up to three actions for a new required dialog or a control moved into a menu, each new step armed with a record check and probed up to the save, one person approving | On main ([#1325](https://github.com/OpenAdaptAI/OpenAdapt/pull/1325)) | Synthetic (MockMed menu and provider drifts, scripted agent; [benchmark/auto_repair](packages/flow/benchmark/auto_repair/README.md)) | `run --self-repair`, [SELF_REPAIR.md](packages/flow/docs/SELF_REPAIR.md) |
| Keep pixel-path runs working across display scaling and font smoothing changes | On main ([#1286](https://github.com/OpenAdaptAI/OpenAdapt/pull/1286)) | Synthetic (in-process RDP and canvas renderings: 17 of 24 healthy runs across 100–175%, up from 3; 0 wrong saves) | [benchmark/pixel_scale](packages/flow/benchmark/pixel_scale/README.md) |
| Find icons again on Citrix, RDP and VNC after a display-scale, zoom, font, theme or codec change, with a local model that counts look-alikes and stops on a tie (`--local-grounding`, off by default) | On main ([#1596](https://github.com/OpenAdaptAI/OpenAdapt/pull/1596)) | Synthetic (Chromium-drawn clinic screen, pixels only: icons found under 21 of 26 conditions, template matching 8; 0 wrong targets in 65; whole runs unchanged, 4 of 39 in both arms, because the layout, identity and census checks stop first). Held out on real AVC444 RDP frames: 0 wrong picks, 6 right, 75 abstentions in 81 | [benchmark/pixel_grounding](packages/flow/benchmark/pixel_grounding/README.md), `runtime/local_grounder.py` |
| Pre-approved repairs: a signed policy lists low-risk changes that apply without a person, a test-record run before each queue, and one approval for every item halted the same way | On main ([#1294](https://github.com/OpenAdaptAI/OpenAdapt/pull/1294)) | Synthetic | standard changes |
| Repair a pinned bundle: the fix runs the pinned bundle's own qualification campaign on a test copy, keeps every write, effect, identity rule and record check, and is re-admitted with signed lineage (at most three repairs, no revoked key) that the pin verifies | On main | Synthetic | [SELF_REPAIR.md](packages/flow/docs/SELF_REPAIR.md#a-pinned-bundle-re-qualify-and-re-admit) |
| Repair by default: once a repair agent is configured, every stop that wrote nothing goes to it without anyone starting it; a stop after a possible write goes to reconciliation; every stop is a row in a ledger with the funnel from stop to resumed item | On main ([#1360](https://github.com/OpenAdaptAI/OpenAdapt/pull/1360)) | Synthetic (MockMed, 22 drift modes, real Claude agent: 6 of 21 repairable stops resumed and verified, 0 wrong saves in 66 runs); routing over recorded OpenEMR stops; live openIMIS relabel: 3 of 3 reached repair and stopped with no wrong save; silent-saves 1.1.0 UI-change blocks: 18 of 28 items that always stopped now correct, 0 wrong | `run` (on by default, `--no-auto-repair`), `repair:` in `deploy.yaml`, [benchmark/auto_repair](packages/flow/benchmark/auto_repair/README.md) |
| Repair a relabeled save button: prove from the recorded identity hash that only the label changed (same control id, kind and form), re-qualify the fix on the new release, and have a person admit the certified copy afresh, since a save button's identity is part of the record check | On main | Real app (local OpenEMR, synthetic data): after the save button was relabeled, the stopped item re-ran and saved once after 8.3 operator minutes and 763 s of re-qualification; 896 s from stop to done ([#1513](https://github.com/OpenAdaptAI/OpenAdapt/pull/1513)) | `openadapt_flow/repair/relabel.py`, [SELF_REPAIR.md](packages/flow/docs/SELF_REPAIR.md#a-relabeled-control-proof-that-only-the-label-changed) |
| A duplicate check that reads the app's match list (`no_duplicate_candidate`): stop only on a listed record with the item's last name or date of birth, fail closed on an unreadable or unloaded list | On main | Real app (local OpenEMR, synthetic data): false stops per 100 items 70.4 before, 16.4 after; 0 silent wrongs in 30 saves ([#1513](https://github.com/OpenAdaptAI/OpenAdapt/pull/1513)) | [DUPLICATE_CHECK.md](packages/flow/docs/DUPLICATE_CHECK.md) |
| A switched-off `problems_only` gate asks a person before the save instead of stopping the item | On main | Synthetic; real app in [#1513](https://github.com/OpenAdaptAI/OpenAdapt/pull/1513) | [APPROVAL_MODES.md](packages/flow/docs/APPROVAL_MODES.md#when-the-mode-turns-itself-off) |
| Fleet drift patches: a verified fix with no patient data, shared across sites under policy | Released, local reference hub | Simulator | `openadapt-flow fleet`, [FLEET_DRIFT_PATCHES.md](packages/flow/docs/FLEET_DRIFT_PATCHES.md) |

## Computer-use agents

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Compile a computer-use agent's successful run into a program (JIT); later runs make no model calls | Released | Synthetic (a recorded Claude run on MockMed) | `openadapt_flow.jit`, [JIT.md](packages/flow/docs/JIT.md) |
| Reuse a compiled program only for the record check and app environment it was admitted under (a changed record check or a new app version at the same origin is refused) | On main ([#1360](https://github.com/OpenAdaptAI/OpenAdapt/pull/1360)) | Unit tests | `TaskSignature(record_check=, environment=)`, `WebApp(environment_id=)`, [JIT.md](packages/flow/docs/JIT.md) |
| Code as action: the run-1 agent acts through one terminal tool (structure first, actions by role and name, a sandboxed read-only shell, screenshots on request); every action is a structured step checked against the compiled program | On main ([#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305)) | Real app (local OpenEMR) and synthetic | `ClaudeAgent(vision="code")`, `CODE_AS_ACTION.md` ([#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305)) |
| Admit a draft by a person, an admitter key, or an agent under a person-signed policy, including self-admission when the policy allows it | Released | Unit tests | [JIT_ADMISSION.md](packages/flow/docs/JIT_ADMISSION.md) |
| MCP server: your agent decides what to enter, OpenAdapt makes the entry and checks it | Released | Synthetic sandbox | `openadapt-agent`, [packages/agent](packages/agent/README.md) |
| Teach over MCP: an agent does a new task once through OpenAdapt; the run is read back, compiled and admitted (a person confirms the record check; the agent admits only under a person-signed policy), and becomes a tool it calls with no model calls. Web, and Windows, macOS, Linux, RDP and Citrix with a person approving each save | On main ([#1335](https://github.com/OpenAdaptAI/OpenAdapt/pull/1335)) | Synthetic (MockMed over MCP, and through the real RDP and Citrix backends) | `openadapt-agent serve --teach`, [packages/agent](packages/agent/README.md#teach-a-new-workflow-from-your-agent) |
| Turn a read-only browser task into a tool for the coding agent you already use: `openadapt setup` registers the MCP server and a skill in Claude Code, Cursor, Codex and Gemini CLI (idempotent, `--dry-run`, `--undo`); the agent does the task once through `teach_start`/`teach_act`/`teach_finish`, the task must end in a download, the file is checked (columns, a column between two inputs or equal to one, fewest rows), the compiled steps are replayed once with no model, and the agent gets a `taught_<name>` tool that downloads a checked file with new inputs. The browser sends no POST, PUT, PATCH or DELETE and stays on one site; a compiled run stops when a control is missing, matches more than one element, or is relabelled | On main ([#1502](https://github.com/OpenAdaptAI/OpenAdapt/pull/1502)) | Real headless Chromium on the local example portal (scripted MCP client, in CI); a real Claude Code agent taught the export from the guide's prompt and called the tool with new dates (2026-10-24, one run) | `openadapt setup`, `openadapt-agent serve --teach-browser`, [FIRST_TOOL.md](packages/flow/docs/FIRST_TOOL.md) |
| Verify any agent's own writes: `openadapt verify` reads the system of record before and after a write, against a contract the operator owns (REST, FHIR, SQL, GraphQL), and returns a verified receipt (`openadapt.effect-receipt/v1`) or a refusal (`REFUTED`, `UNCERTAIN`), never an unverified success. Python SDK (`openadapt.verify`, which is `openadapt_flow.verifier`: `begin_write`, `verify_write`, `verified_write`) wraps a write in any agent framework; the MCP server (`openadapt verify serve`) offers `verify_begin` and `verify_check` to any MCP client | On main ([#1589](https://github.com/OpenAdaptAI/OpenAdapt/pull/1589)) | Synthetic (MockMed's fault server over real HTTP: correct write, wrong record, not saved, saved twice, unreadable store) | `openadapt verify`, [VERIFY_WRITES.md](packages/flow/docs/VERIFY_WRITES.md) |
| Teach a browser write as a verified tool: with write contracts (`serve --teach-browser --contracts DIR`), the agent teaches a save once; run 1 is verified in the system of record before the `taught_<name>` tool exists, and every call makes the write and returns a verified receipt or a refusal. Sign-in values come from the server's environment and never reach the agent or the program. Controls in named iframes are found through their frame | On main ([#1589](https://github.com/OpenAdaptAI/OpenAdapt/pull/1589)) | Synthetic (the example app's sign-in and order form in headless Chromium over MCP: verified save, a save the app drops, a save it keeps twice) | `openadapt-agent serve --teach-browser --contracts`, [VERIFY_WRITES.md](packages/flow/docs/VERIFY_WRITES.md#teach-a-write-as-a-tool) |
| Guard an agent you already run: attach to its own Chromium over CDP (one added line), hold each save until the record on screen is the one the task names, block it and tell the agent why when it isn't, read the save back; a run that reads back correctly becomes a draft program with a record check from scaffold-verifier, which a person confirms and admits, then a `taught_*` tool with no model calls. `--observe-only` never blocks | On main ([#1357](https://github.com/OpenAdaptAI/OpenAdapt/pull/1357), stacked on [#1335](https://github.com/OpenAdaptAI/OpenAdapt/pull/1335)) | Synthetic (MockMed, scripted Playwright agent, `effect_e2e` faults) | `openadapt-flow guard`, [GUARD_AN_AGENT.md](packages/flow/docs/GUARD_AN_AGENT.md) |
| Task ledger per job: rules locked when the job opens and enforced on every run, item status set only from OpenAdapt's own results, and yes-or-no questions a person answers on the phone or in Desktop, including when a run stops | On main ([#1332](https://github.com/OpenAdaptAI/OpenAdapt/pull/1332)) | Synthetic (30-item MockMed worklist, scripted agent, no model calls) | MCP tools `open_job`, `get_job`, `add_items`, `record_evidence`, `ask_user`, `get_answer`, `change_constraint`, `close_job`; [Track a long job](packages/agent/README.md#track-a-long-job) |
| Limit a teaching agent to the declared task on every teach path (`compile_agent`, `compile-agent`, teach over MCP, the airbag guard): typed text must be a declared value, links and browser requests to other origins are refused, writes are checked before they are sent, and the record is read back before a draft exists. Injected instructions in record notes, banners, tooltips, alt text and server responses are blocked or caught on MockMed through each path (scripted agent; a live-model mode is off by default). Self-repair refuses a control that leaves the app | On main ([#1359](https://github.com/OpenAdaptAI/OpenAdapt/pull/1359), [#1386](https://github.com/OpenAdaptAI/OpenAdapt/pull/1386)) | Synthetic | `openadapt_flow.teach_scope`, [THREAT_MODEL.md](packages/flow/docs/THREAT_MODEL.md) |
| Compile an agent's first verified run from a prompt (`compile-agent`), and a code-as-action authoring agent | On main ([#1277](https://github.com/OpenAdaptAI/OpenAdapt/pull/1277), [#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305)); the agent harness behind it runs on every backend, on main ([#1283](https://github.com/OpenAdaptAI/OpenAdapt/pull/1283)) | Synthetic | `compile-agent` |
| Score harness optimizers (Harness², Meta-Harness) on GUI tasks with the record read-back as the reward, with full trace export | On main ([#1299](https://github.com/OpenAdaptAI/OpenAdapt/pull/1299), [#1300](https://github.com/OpenAdaptAI/OpenAdapt/pull/1300), [#1301](https://github.com/OpenAdaptAI/OpenAdapt/pull/1301)) | Synthetic (scripted proposer) | `integrations/harness2/` |
| One scoreboard comparing the agent alone with OpenAdapt across tasks (correctness by read-back, cost, time, items per hour) | On main ([#1303](https://github.com/OpenAdaptAI/OpenAdapt/pull/1303)) | Synthetic and local OpenEMR and openIMIS test copies | `benchmark/scoreboard/` |
| Pull a skill from the registry (`pull app/task@version`) and run it only after qualifying it on your own instance: `pull` marks it and expires any certification it carries, `skill bind` records your addresses and the names of the variables that hold credentials (never their values), `skill qualify` runs its bundled record check there, and `run`, `replay`, `emit-skill` and `emit-mcp` refuse it until then, and on any other instance. The index and its archives carry a keyless Sigstore attestation from the registry release workflow, checked offline against a pinned identity, and expire and can't be rolled back; a shipped record check runs no code and reads only your binding | On main | Unit tests | `openadapt-flow skill`, [SKILLS_REGISTRY.md](packages/flow/docs/design/SKILLS_REGISTRY.md) |

## Recording, privacy and proof

| Capability | Status | Evidence | Where |
| --- | --- | --- | --- |
| Record mouse, keyboard, screen video and accessibility trees on one clock (Windows, macOS, Linux X11) | Released (capture 1.3.0) | Real OS | [packages/capture](packages/capture/README.md) |
| Find and redact personal and health data in text, screenshots and structure | Released (privacy 1.1.3) | Unit tests | [packages/privacy](packages/privacy/README.md), [PRIVACY.md](packages/flow/docs/PRIVACY.md) |
| Scrub by default before anything leaves the machine | On main ([#1262](https://github.com/OpenAdaptAI/OpenAdapt/pull/1262)) | Unit tests | `openadapt-privacy` |
| Start and stop Desktop recordings from the menu bar or system tray, and see Desktop's state and how many cloud runs need a person | Released (tray 0.3.7) | Unit tests; CI runs the tray client against Desktop's socket server | [packages/tray](packages/tray/README.md) |
| Show in the menu bar when a recording started by `openadapt-flow record` or `capture record` is ready and when it ends, and start and stop `openadapt-flow record` from the macOS menu bar, without OpenAdapt Desktop | On main ([#1481](https://github.com/OpenAdaptAI/OpenAdapt/pull/1481)) | Unit tests with a stand-in control client; run once on one Mac with a real `openadapt-flow record --backend macos` window recording (ready 6.6 s after start, stop reported as saved) | [packages/tray](packages/tray/README.md#recording-without-openadapt-desktop) |
| Each package installs on its own within a size budget, checked in CI (privacy without desktop, capture without plotting) | On main ([#1270](https://github.com/OpenAdaptAI/OpenAdapt/pull/1270), [#1271](https://github.com/OpenAdaptAI/OpenAdapt/pull/1271), [#1279](https://github.com/OpenAdaptAI/OpenAdapt/pull/1279)) | CI | `.github/workflows/packages.yml` |
| See a compiled program as a map (every step, exact edge, loop and stop rule, with each step's checks) and play a recorded run over it; one React package that Desktop, app.openadapt.ai and openadapt.ai share | On main ([packages/program-graph](packages/program-graph/README.md)) | Real compiled OpenEMR referral program and a recorded scoreboard run, both on synthetic data | `@openadapt/program-graph`; `openadapt-flow visualize --format json` |
| Named sections for every compiled program ("Who: title, first name, last name", "Contact", "Create New Patient"), found without a model from the save, the recorded form structure, the fields and any narration; a person can rename, merge or split them on the review screen | On main | Unit tests; the certified OpenEMR referral bundle and a real recording of it on a local test copy with made-up patients | `openadapt-flow sections`; [SECTIONS.md](packages/flow/docs/SECTIONS.md) |
| Encrypt PHI at rest: every bundle file that can hold patient data under one sealed index (unknown files refused), web recordings from the first frame, and the run directories of sealed bundles; compile, induce, scaffold-verifier, qualify, admit, run, resume and undo read them in memory and refuse without the key | On main | Synthetic (no-plaintext audit-hook test over record, compile and a VERIFIED run; per-frame cost measured) | `openadapt-flow seal`, `record`; [phi_at_rest.md](packages/flow/docs/phi_at_rest.md) |
| Signed receipts (Seals), countersigned and timestamped, offline by default | Released | Unit tests, real TSA tokens | `openadapt-flow seal`, `countersign`, `local-seal` |
| Offline Seals signed with a key the customer holds: a key file, the OS keychain, or a PKCS#11 token or HSM; pinned keys with rotation and revocation; no fallback to a generated key | On main ([#1392](https://github.com/OpenAdaptAI/OpenAdapt/pull/1392)) | Unit tests with synthetic data (keychain and PKCS#11 through stand-ins) | `openadapt-flow local-seal --key`, `local-seal key create`, `pin`, `revoke`; `openadapt seal verify FOLDER` checks every sealed run in a folder; [OFFLINE_SEALS.md](packages/flow/docs/OFFLINE_SEALS.md#customer-held-keys) |
| Anchor an offline Seal in the public Sigstore Rekor transparency log (opt-in; sends one digest and the public key), and verify the anchor offline from the stored inclusion proof | On main ([#1394](https://github.com/OpenAdaptAI/OpenAdapt/pull/1394)) | Unit tests against a simulated Rekor log; real public Rekor entries and Sigstore TSA tokens for the landing demo's referral runs, verified offline in tests; Ed25519ph matches the RFC 8032 test vector | `openadapt-flow local-seal anchor`, `local-seal verify --require-anchor`; [OFFLINE_SEALS.md](packages/flow/docs/OFFLINE_SEALS.md#optional-anchor-a-seal-in-a-public-transparency-log) |
| Run authorizations signed with the authorizer's GitHub SSH key (`ssh-keygen -Y sign`, after a summary of what it allows), naming the signing key and bound to the bundle digest, items, profile and Seal keys, and referenced by each Seal; the identity checked against github.com (`--live`) or pinned allowed signers; a pseudonymous operator profile counted from anchored Seals only | On main ([#1401](https://github.com/OpenAdaptAI/OpenAdapt/pull/1401)) | Unit tests with synthetic data; signatures checked against OpenSSH's `ssh-keygen` both ways | `openadapt-flow local-seal authorize`, `local-seal run --authorization`, `local-seal profile`; [OFFLINE_SEALS.md](packages/flow/docs/OFFLINE_SEALS.md#run-authorizations-who-authorized-the-runs) |
| Run reports redacted before they are sealed: session and CSRF tokens, OAuth grants and credential headers become `<redacted>` and are listed in `redactions`; a Seal refuses a report that still holds one; per-app policy | On main ([#1407](https://github.com/OpenAdaptAI/OpenAdapt/pull/1407)) | Unit tests with generated tokens; 7,773 local reports scanned, only OpenEMR `token_main` matched | `openadapt-flow redact-report`, `local-seal run --allow-unredacted`; [REPORT_REDACTION.md](packages/flow/docs/REPORT_REDACTION.md) |
| Strict no-outbound mode and an unattended on-prem service | Released | Synthetic | `openadapt-flow no-outbound`, `service`; [ON_PREM.md](packages/flow/docs/ON_PREM.md) |

## Every command, by capability

Each `openadapt-flow` command, grouped by the capability it serves. Each
command has one file under
[`packages/flow/docs/commands/`](packages/flow/docs/commands/): to add a
command, add `<group>/<verb>.md` and run `python scripts/build_command_map.py`.
A test fails when a command has no file or this list is out of date.

<!-- BEGIN generated by scripts/build_command_map.py -->

**Record and build a program:**

- `compile-agent`: With `admit TASK_DIR`, a person confirms and admits a waiting draft, such as one an agent taught over MCP (`openadapt-agent serve --teach`).
- `compile`
- `compose`
- `demo-record`
- `disambiguate`
- `for-each`
- `induce`
- `intake`
- `narration`
- `process`
- `pull`
- `record`
- `route`
- `sections`: Show, rename, merge or split the named sections a program map folds
- `skill`: Bind a pulled skill to your instance, qualify it there, and check whether it may run.
- `variants`
- `visualize`

**Check and admit:**

- `admit`
- `api`
- `approver-grant`
- `bind-record-checks`
- `certify`
- `check`
- `jit`
- `known-ids`
- `lint`
- `propose-qualification`
- `qualify`
- `scaffold-verifier`

**Run and answer:**

- `approve`
- `business-decisions`
- `console`
- `deferred`
- `explain`
- `item-approval`
- `replay`
- `resume`
- `run`
- `triage`

**Judge any executor's write:**

- `serve-reward`: Scores agent training episodes by reading the system of record.
- `verify-run`

**Repair and learn from halts:**

- `fleet`
- `repair`
- `teach-branch`
- `teach`
- `undo`: The undo steps a person approves after a stop; `undo propose` finds and verifies them on a test copy, `undo accept` is a person's one-time approval of a proposal.

**Give agents the program:**

- `emit-mcp`
- `emit-skill`
- `serve-execute`

**Proof:**

- `attach-seal`
- `countersign`
- `local-seal`: Offline Seals: customer-held keys, pinned verification, Rekor anchoring, run authorizations, operator profiles.
- `redact-report`: Strip session tokens from an existing run's report so it can be sealed again; the original stays local
- `seal`

**Run on premises:**

- `no-outbound`
- `service`

**Share safely with OpenAdapt Cloud:**

- `approve-sanitized`
- `connect`
- `connector`
- `login`
- `materialize-approved`
- `push`
- `report-break`
- `report-run`
- `review-sanitized`
- `sanitize`
- `validate-hosted`

**Try it and measure it:**

- `bench`
- `benchmark`
- `demo`
- `silent-saves`: Scores any GUI agent on injected bad saves, from the source checkout.
- `tutorial`

**Guard an agent you already run:**

- `guard`: Attaches to the agent's own Chromium over CDP, holds each save until the right record is on screen, reads it back, and hands a verified run to the teach dir.

<!-- END generated by scripts/build_command_map.py -->

## Measured results

- **OpenEMR referral, 62 timed runs each, warm.** OpenAdapt took a median of
  29.7 s and a computer-use agent (Claude) 36.5 s. Both saved 62 of 63
  referrals correctly. OpenAdapt made no model calls; the agent cost about
  $0.08 per referral. Raw data and
  figures: [benchmark/openemr_referral_timing](packages/flow/benchmark/openemr_referral_timing/).
- **Guard an agent you already run, MockMed.** A scripted Playwright agent
  in its own browser, 90 runs at a high shared load (median load average 29).
  Of the 8 `effect_e2e` faults that leave a wrong record, the agent alone
  reported 6 as saved; with the guard, the read-back caught 7 and 1 (a stray
  write to a table the REST read doesn't cover) got through; the compiled
  program caught all 8. The guard blocked the saves it can see: a save on the
  wrong patient and a second submit. Same-load pairs: the guarded agent took
  6.3 s (6 actions, each a model call for a real agent), the compiled tool
  4.4 s with no model calls:
  [GUARD_AN_AGENT.md](packages/flow/docs/GUARD_AN_AGENT.md#measured-on-mockmed).
- **Fault test.** We injected 8 kinds of bad save, 9 runs each, alongside
  clean runs: 72 runs left a wrong record behind. A check that trusts the screen's "Saved" banner reported 54 of those
  as success; reading the record back over the app's REST API missed 9; the
  complete database read path caught all 72. The fault set and method are in
  [EFFECT_E2E.md](packages/flow/benchmark/effect_e2e/EFFECT_E2E.md).
- **OpenEMR referral: agent, screen program and API step.** Median wall time
  per referral, from a fresh browser through sign-in, the entry and the read-back:
  agent alone 36.5 s (62 of 63 correct, the 14 to 15 Oct study); compiled program
  on the screen 22.5 s (18 of 18); the same program with the API step 3.3 s
  (18 of 18). The program rows ran on 19 Oct, interleaved on one copy, with
  the approval gate off; the benchmark harness admitted the API step:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#openemr-agent-screen-program-and-api-step).
- **An agent's claims versus the read-back, on OpenEMR.** With faults injected
  in the fixture's database (partial, wrong value, duplicate, not persisted,
  another chart deleted, another chart edited; 5 items each), Claude Sonnet 5.5
  computer use claimed success on all 30 bad saves. `verify-run` caught 25 and
  confirmed 5 of 5 clean saves with no false alarms:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#the-agents-claims-versus-the-read-back).
- **An agent's claims versus the read-back.** Claude Sonnet 5.5 computer use,
  working alone on a test app whose server broke the save in 6 ways (5 items
  each), reported all 30 bad saves as done. `verify-run` flagged all 30 and
  confirmed all 10 clean saves, with no false alarms. The app shows "saved"
  before the server answers, so this measures what the screen can't show, not
  the agent's care:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#the-agents-claims-versus-the-read-back).
- **openIMIS claims.** On a test copy of openIMIS, the open-source insurance
  and claims system that runs Nepal's national health insurance scheme, the
  compiled program entered 149 synthetic claims with no model calls: 148 read
  back correct and none were saved wrong:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#openimis).

The systems each result ran on are listed in
[LIMITS.md](packages/flow/docs/LIMITS.md#validated-on).
