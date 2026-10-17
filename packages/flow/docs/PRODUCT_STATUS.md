# Product capability and qualification

This is the authoritative map of the product surfaces, their accepted evidence,
and the qualification boundary for a customer workflow.
The machine-readable claim-to-evidence registry is [`claims.yaml`](../claims.yaml),
and its generated view is [`VERIFICATION.md`](VERIFICATION.md).

## Product release state

Flow has no static lifecycle label. Its exact release enters Production only
through an active signed, expiring, and revocable release admission. A missing,
expired, revoked, mismatched, or unverifiable admission produces **not actively
admitted**. It doesn't restore an older admission or fall back to another
label.

OpenAdapt shows Production only when all seven product targets have active
admissions for the exact deployed or default releases. Read the
[current admission-derived status](https://openadapt.ai/status.json) and the
[Production admission contract](https://docs.openadapt.ai/reference/production-lifecycle/).

A product release admission doesn't qualify a customer workflow. Standard and
Regulated actuation also require an active workflow admission for the exact
sealed bundle and its bound runtime, application, environment, input, action,
identity, effect, and policy contracts.

## Evidence vocabulary

The matrix reports the basis of each capability claim. It does not assign a
product lifecycle state.

| Evidence basis | Meaning here |
| --- | --- |
| **Required CI** | The capability runs in a required pull-request or exact-main check. |
| **Counted task acceptance** | A fixed task, environment, run count, oracle, and failure taxonomy passed an accepted qualification. |
| **Counted stand-in** | The integrated path and refusal contract passed against a bounded substitute. A deployment-specific acceptance record is still required. |
| **Opt-in or contract-only evidence** | The implementation exists, but the evidence is infrastructure-gated, mocked at an external boundary, or below a counted acceptance record. |
| **Separate admission** | Another product target or exact workflow supplies its own signed admission. |

## Integrated matrix

| Surface | Evidence basis | What is proven | Boundary that remains |
| --- | --- | --- | --- |
| Demonstration compiler and bundle | **Required CI** | Browser recording compiles into a parameterized, inspectable bundle in CI. | One demonstration can under-specify intent; production policies and effect bindings still require operator work. |
| Browser / Playwright recording and replay | **Required CI and counted attach trials** | Record, compile, replay, deterministic drift repair, reports, and refusal all run end to end against MockMed; a bounded OpenEMR result is published separately. The required browser suite also performs 3 real Chromium CDP-attach record-and-compile trials, checks source-time password exclusion, proves that recorder shutdown leaves the external browser running, and compiles actions across a live viewport and device-scale change. | This evidence doesn't cover arbitrary sites, long-term drift, or production reliability. Attach mode is Chromium-only, loopback-only, and requires a browser started with remote debugging. It refuses an action that overlaps a resize transition. It doesn't promote the Capture Chrome extension prototype or direct extension replay. |
| Healthy zero-model replay | **Required CI** | Repeated CI runs use the deterministic ladder with zero model calls. | Optional model grounding is a separate opt-in fallback; a changed app can still halt. |
| Deterministic re-resolution | **Required CI** | Theme, moved-control, and renamed-control fixtures resolve through non-model rungs and emit reviewable patches. | It covers bounded evidence-preserving drift, not arbitrary workflow or business-logic change. |
| AI-assisted repair | **Required CI contracts; deployment evidence required** | Local and remote VLM contracts, egress gates, refusal behavior, and retention boundaries are tested. | It is off by default. A model cannot authorize an action or prove identity or effect. The exact endpoint and task require workflow qualification. |
| Human teaching (`teach`) | **Required CI contracts; field evidence required** | Halt-to-correction-to-guarded-promotion and regression refusal run in default CI. | Evidence is controlled and synthetic. Broad authoring UX and field recovery time require deployment evidence. |
| Windows UIA replay | **Counted task acceptance** | Candidate `20260717-candidate-56759c8-v2` completed 3/3 exact WinForms trials with independently confirmed SQLite effects and 12 native UIA delivery receipts. Stale and ambiguous targets each refused 3/3; silent incorrect successes, over-halts, and model calls were zero. See [`benchmark/windows_uia/results.json`](../benchmark/windows_uia/results.json). | Acceptance covers the in-tree WinForms workflow and exact Windows VM. Each third-party application is qualified against its own controls, versions, identity rules, and effect oracle. |
| Desktop recording (`windows` / `macos` / `linux` / `rdp` / `citrix`) | **Required CI plus substrate acceptance** | `openadapt-capture` is the canonical native screen, mouse, keyboard, timing, window-scope, and media-capture component. Capture conversion, compile, and replay orchestration run in CI for every desktop selector, and the native substrate qualifications below prove the corresponding actuation paths. | Offline pixel capture cannot reconstruct structural accessibility evidence. A workflow that requires UIA, AX, or AT-SPI identity must retain a live structural observation or receive that evidence during qualification. Regulated profiles require declared secret handling and fail-closed privacy configuration. |
| Native macOS desktop actuation | **Counted task acceptance** | Candidate `b1b61a5` completed 3/3 TextEdit replace-and-save trials with exact file-byte effects and refused a two-window ambiguous selector without changing either file. See the [accepted evidence adjudication](../benchmark/macos_native/textedit_counted_3plus1_b1b61a5_20260717.adjudication.json). | Acceptance covers TextEdit on one macOS 15.7.3 Apple Silicon host and active user session. Customer applications require workflow-specific qualification. |
| Native macOS AX structured identity | **Counted task acceptance plus required CI** | The macOS backend implements the same structured-layer contract as the browser DOM, Windows UIA, and Linux AT-SPI backends: it records a stable AX locator, re-finds the UNIQUE element at replay, refuses ambiguous / truncated / scope-escaping enumeration instead of guessing, and returns structured text under a point. Headless unit CI covers record/locate/refuse; a live-AX TextEdit run produced real evidence ([AX identity adjudication](../benchmark/macos_native/ax_identity_20260720.adjudication.json)); the record→compile→replay conformance test asserts zero model calls on healthy replay. See [`tests/test_macos_structural.py`](../tests/test_macos_structural.py) and the [capability matrix](../tests/test_backend_capability_matrix.py). | The backend uses gated point-bound physical click after structural resolution rather than claiming AXPress everywhere. AX exposure varies by application; controls without durable AX identity use the visual ladder. |
| Native Linux desktop actuation | **Counted task acceptance plus required CI** | The required `linux-atspi-x11` job runs a real GTK3 application against AT-SPI inside an isolated Xvfb/session-D-Bus environment: 3 clean exact-file-effect trials, 3 ambiguous-target refusals, and 3 stale-target refusals. Unit CI covers the remaining window, traversal, capture, physical-input, and portal boundaries. | Acceptance is bounded to the in-tree GTK3 workflow and CI image. Each application and environment retains its own qualification. The built-in driver uses X11; Wayland requires a live operator-approved XDG portal session and refuses without one. |
| RDP | **Counted task acceptance plus required CI** | Candidate `82a658a` completed 3/3 real-network Aardwolf RDP trials into Windows 11, with a guest-tools file oracle, zero failures, zero silent incorrect successes, zero over-halts, and zero model calls. The public multi-window FreeRDP campaign adds a bounded 27-trial contract with independent SQLite, CSV, and Maildir oracles. The backend also rebaselines a changed framebuffer between actions, refuses a change during the exact-frame lease, refuses unsupported horizontal scroll before delivery, and classifies transport failures as uncertain delivery. See the [accepted batch](../benchmark/rdp/ACCEPTED_BATCH_82A658A.md) and [campaign contract](../benchmark/rdp_multiapp/README.md). | The accepted batch covers the tested 1280×800 transport/input task. The multi-window fixture uses synthetic applications. Target applications, identity/effect rules, session policies, and display conditions are qualified per deployment. A composite multi-monitor session remains deployment-qualified evidence, not part of the accepted 1280×800 batch. |
| Citrix / pixel-only remote display | **Required CI plus counted no-DOM stand-in** | `--backend citrix` binds an exact Citrix Workspace window, readiness marker, pixel-only ladder, governed run, durable resume, and report; required CI covers those orchestration and refusal contracts. The window driver recalculates capture scale after a resize or cross-monitor move and refuses DPI or geometry drift during input. The public real-ICA preflight adds distinct authority keys, executable and oracle attestations, a signed display and monitor-topology observation, explicit reliability metrics, one-use campaign state, crash recovery, and uncertain-dispatch handling. Separately, the retained no-DOM driver qualification passed 3 healthy effect-confirmed trials and 3 drift safe-halts with zero model calls, silent incorrect successes, or false completion, and records `code_readiness_accepted=true`. | The counted stand-in and preflight do not claim live ICA/HDX acceptance. A live result remains bound to the exact Workspace/server/application/display matrix, customer-approved executable, and independent effect oracle. Deployment-specific recipes, data, and thresholds stay outside the public repository. |
| Identity verification | **Required CI; exact workflow binding required** | Wrong-entity refusal and adversarial corpora run in CI. | An action without an identity contract has no entity check. Workflow admission must bind the exact armed actions and identity authority. |
| System-of-record effect verification | **Required CI; exact verifier binding required** | REST, FHIR, SQL, file, and document verifier contracts catch fault classes that screen-only verification misses. A deployment with multiple reviewed read boundaries selects and preflights the strongest evidence tier before input, retains that binding through durable resume, and never downgrades after an action. | Effects are not generally inferred; both authored effects and a configured verifier are required. A selected verifier that becomes unavailable halts or enters reconciliation. |
| Lint and certification policies | **Required CI** | The CLI reports coverage gaps and refuses bundles that violate a selected policy. | `replay` remains the permissive tutorial path. Governed deployment uses fail-closed `run` plus the required release and workflow admissions. |
| Durable pause, approval, and resume | **Required CI; authenticated operator route required** | Checkpoint, bundle-version binding, approval, stale-pause, and resume semantics are tested. | The engine records an asserted operator identity. Desktop, Cloud, or a customer-local identity route must authenticate that principal. |
| Typed business decisions | **Required CI; authenticated operator route required** | A typed qualification API adds or updates a finite decision node without manual manifest edits and invalidates stale certification. The graph runtime pauses at the certified choice, validates a supplied principal and role, retains a signed durable receipt, restores it after a crash, revalidates the live application, and permits only the certified successor branch. | The engine does not authenticate a user. Desktop, Cloud, or a customer-local identity route must supply an authenticated principal. A decision never replaces entity identity or effect verification. |
| Reviewed judgment cases | **Required CI contracts; reviewed workflow evidence required** | Qualification binds typed facts, local evidence hashes, reviewer provenance, and the exact decision contract to reviewed examples or counterfactuals. It preserves permanent human authority, requires reciprocal contrasts for an automatic-rule candidate, and refuses certification when a case still needs evidence. | The case layer does not synthesize executable policy from one or more examples. A reviewed automatic rule must be authored and qualified through the normal program path. |
| Qualified remote decision tasks | **Required CI contracts; negotiated peer schema required** | An explicitly negotiated V2 task binds optional reviewed entity wording to the exact qualification, bundle, step, policy, and pause. V1 stays byte-compatible, and an unavailable or unrecognized class renders as the signed neutral `record` or `item` fallback. | V2 requires `openadapt-types` 0.10.x and a consumer that negotiates the schema. Actual entity identifiers and live revalidation stay inside the customer-controlled runner. |
| On-prem / air-gapped deployment | **Release and clean-machine evidence; site acceptance required** | Local queue, fail-closed run gate, egress attestation, audit-chain verification, signed release verification, fresh-environment smoke/air-gap checks, atomic blue/green update, and rollback ship. | Site firewall, storage, keys, OS hardening, identity/effect integrations, and acceptance in the customer's environment remain deployment responsibilities. |
| Desktop GUI and tray | **Separate product target admission** | Desktop `v0.15.0` ships installable Windows, macOS, and Linux artifacts with checksums; its frozen engine lifecycle and install/launch/uninstall contracts run in release CI. | The published installer evidence covers the embedded browser lifecycle. Native and remote substrate selection is independently qualified through Flow and remains bound to the selected target configuration. |
| Hosted dashboard / control plane | **Separate product target admission** | The deployed service uses live Supabase, Stripe, and Modal dependencies for account and organization onboarding, checkout, exact-hash artifact ingest, attested browser workflow versions, structural reports, replacement activation, scheduling, entitlements, and metering. The reversible pre-payment contract passed 3/3 production trials. | The first genuine customer payment remains the acceptance event for the paid post-payment lifecycle. SLA, BAA, and compliance commitments apply only when included in reviewed written terms. |
| Hosted execution | **Separate product target and workflow admissions** | Production mode admits exact attested browser bundles, dispatches the configured runner, authenticates callbacks, and refuses mock fallback; development mock mode remains visibly synthetic. | Desktop, RDP, Citrix, and customer-controlled regulated execution are separately scoped deployment lanes rather than capabilities implied by the browser subscription. |
| Offline update and rollback | **Required release qualification and site rehearsal** | The operator-pulled path verifies signed archives, installs into a fresh blue/green environment, runs smoke and air-gap checks, atomically swaps the active release, and records rollback state. | Signer trust, artifact transport, OS/container policy, backup, disaster recovery, and a customer-site rehearsal remain deployment responsibilities. |

## Repair modes

"Self-healing" is shorthand for four materially different outcomes:

1. **Automatic deterministic re-resolution:** a lower non-model rung finds the
   same target from retained evidence and emits a patch. This is the path the
   bundled theme-drift demo exercises.
2. **AI-assisted repair:** an explicitly enabled grounding model proposes a
   target or state interpretation. Identity, risk, postcondition, and policy
   checks still apply; a model answer is not authorization.
3. **Human teaching:** an operator demonstrates a correction after a halt. The
   correction is induced as a guarded branch and promoted only if its regression
   gate passes.
4. **Unsupported drift:** evidence is insufficient, identity is ambiguous, a
   postcondition fails, or policy refuses the action. The correct outcome is a
   halt and report, not a repair.

## Evidence policy

- CI-backed capability claims are registered in [`claims.yaml`](../claims.yaml).
- Opt-in and field evidence cannot be promoted to a stronger tier merely because
  code exists.
- Benchmarks describe their task, environment, run count, success oracle,
  latency, model calls, cost assumptions, and caveats. They are bounded evidence,
  not general market proof.
- The weekly
  [`quickstart-lifecycle.yml`](../.github/workflows/quickstart-lifecycle.yml)
  installs the built wheel in a clean environment on Linux, macOS, and Windows;
  records, compiles, lints, certifies, replays, induces drift, inspects repair
  and report artifacts, uninstalls, and verifies the import is gone.
