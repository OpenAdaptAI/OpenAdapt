# Product capability and qualification

This is the authoritative map of the product surfaces, their accepted evidence,
and the qualification boundary for a customer workflow.
The machine-readable claim-to-evidence registry is [`claims.yaml`](../claims.yaml),
and its generated view is [`VERIFICATION.md`](VERIFICATION.md).

## Status vocabulary

| Status | Meaning here |
| --- | --- |
| **Beta** | Runs end to end on the default CI or clean-machine lifecycle and is available for evaluation. No production SLA or general enterprise certification is implied. |
| **Scoped acceptance** | A fixed task, environment, run count, oracle, and failure taxonomy have passed an accepted qualification. This is stronger than a protocol spike and narrower than arbitrary-application support. |
| **Code-qualified** | The integrated product path and its refusal contract passed a counted end-to-end stand-in or protocol qualification. A transport- or application-specific acceptance record remains separately bound to the exact deployment. |
| **Experimental** | Real implementation exists, but evidence is opt-in, infrastructure-gated, mocked at an external boundary, or otherwise below a counted acceptance record. |
| **Research** | Protocol spike, design, or synthetic proof. Not a supported deployment surface. |
| **Deprecated** | Superseded path retained only for migration. Do not start new work on it. |
| **Archived** | Historical code with no active product role. |
| **Internal** | Maintainer tooling, not an OpenAdapt product surface. |

## Integrated matrix

| Surface | Status | What is proven | Boundary that remains |
| --- | --- | --- | --- |
| Demonstration compiler and bundle | **Beta** | Browser recording compiles into a parameterized, inspectable bundle in CI. | One demonstration can under-specify intent; production policies and effect bindings still require operator work. |
| Browser / Playwright recording and replay | **Beta** | Record, compile, replay, deterministic drift repair, reports, and refusal all run end to end against MockMed; a bounded OpenEMR result is published separately. | The reference path is not evidence for arbitrary sites, long-term drift, or production reliability. |
| Healthy zero-model replay | **Beta** | Repeated CI runs use the deterministic ladder with zero model calls. | Optional model grounding is a separate opt-in fallback; a changed app can still halt. |
| Deterministic re-resolution | **Beta** | Theme, moved-control, and renamed-control fixtures resolve through non-model rungs and emit reviewable patches. | It covers bounded evidence-preserving drift, not arbitrary workflow or business-logic change. |
| AI-assisted repair | **Experimental** | Local/remote VLM contracts, egress gates, refusal behavior, and retention boundaries are tested. | Off by default; model accuracy is not a safety guarantee and real deployment quality is unmeasured. |
| Human teaching (`teach`) | **Experimental** | Halt-to-correction-to-guarded-promotion and regression refusal run in default CI. | Evidence is controlled/synthetic; broad authoring UX and field recovery time are not established. |
| Windows UIA replay | **Scoped acceptance** | Candidate `20260717-candidate-56759c8-v2` completed 3/3 exact WinForms trials with independently confirmed SQLite effects and 12 native UIA delivery receipts. Stale and ambiguous targets each refused 3/3; silent incorrect successes, over-halts, and model calls were zero. See [`benchmark/windows_uia/results.json`](../benchmark/windows_uia/results.json). | Acceptance covers the in-tree WinForms workflow and exact Windows VM. Each third-party application is qualified against its own controls, versions, identity rules, and effect oracle. |
| Desktop recording (`windows` / `macos` / `linux` / `rdp` / `citrix`) | **Beta, capture-assisted** | `openadapt-capture` conversion, compile, and replay orchestration run in CI for every desktop selector, and the native substrate qualifications below prove the corresponding actuation paths. | Offline pixel capture cannot reconstruct structural accessibility evidence. Workflows that require UIA, AX, or AT-SPI identity use a live structural observer or are re-armed against the qualified application before release. Regulated profiles require declared secret handling and fail-closed privacy configuration. |
| Native macOS desktop actuation | **Scoped acceptance** | Candidate `b1b61a5` completed 3/3 TextEdit replace-and-save trials with exact file-byte effects and refused a two-window ambiguous selector without changing either file. See the [accepted evidence adjudication](../benchmark/macos_native/textedit_counted_3plus1_b1b61a5_20260717.adjudication.json). | Acceptance covers TextEdit on one macOS 15.7.3 Apple Silicon host and active user session. Customer applications require workflow-specific qualification. |
| Native macOS AX structured identity | **Scoped acceptance** | The macOS backend implements the same structured-layer contract as the browser DOM, Windows UIA, and Linux AT-SPI backends: it records a stable AX locator, re-finds the UNIQUE element at replay, refuses ambiguous / truncated / scope-escaping enumeration instead of guessing, and returns structured text under a point. Headless unit CI covers record/locate/refuse; a live-AX TextEdit run produced real evidence ([AX identity adjudication](../benchmark/macos_native/ax_identity_20260720.adjudication.json)); the record→compile→replay conformance test asserts zero model calls on healthy replay. See [`tests/test_macos_structural.py`](../tests/test_macos_structural.py) and the [capability matrix](../tests/test_backend_capability_matrix.py). | The backend uses gated point-bound physical click after structural resolution rather than claiming AXPress everywhere. AX exposure varies by application; controls without durable AX identity use the visual ladder. |
| Native Linux desktop actuation | **Scoped acceptance** | The required `linux-atspi-x11` job runs a real GTK3 application against AT-SPI inside an isolated Xvfb/session-D-Bus environment: 3 clean exact-file-effect trials, 3 ambiguous-target refusals, and 3 stale-target refusals. Unit CI covers the remaining window, traversal, capture, physical-input, and portal boundaries. | Acceptance is bounded to the in-tree GTK3 workflow and CI image. Each application and environment retains its own qualification. The built-in driver uses X11; Wayland requires a live operator-approved XDG portal session and refuses without one. |
| RDP | **Scoped acceptance** | Candidate `82a658a` completed 3/3 real-network Aardwolf RDP trials into Windows 11, with a guest-tools file oracle, zero failures, zero silent incorrect successes, zero over-halts, and zero model calls. The public multi-window FreeRDP campaign adds a bounded 27-trial contract with independent SQLite, CSV, and Maildir oracles. See the [accepted batch](../benchmark/rdp/ACCEPTED_BATCH_82A658A.md) and [campaign contract](../benchmark/rdp_multiapp/README.md). | The accepted batch covers the tested 1280×800 transport/input task. The multi-window fixture uses synthetic applications. Target applications, identity/effect rules, session policies, and display conditions are qualified per deployment. |
| Citrix / pixel-only remote display | **Code-qualified** | `--backend citrix` binds an exact Citrix Workspace window, readiness marker, pixel-only ladder, governed run, durable resume, and report; required CI covers those orchestration and refusal contracts. The public real-ICA preflight adds distinct authority keys, executable and oracle attestations, one-use campaign state, crash recovery, and uncertain-dispatch handling. Separately, the retained no-DOM driver qualification passed 3 healthy effect-confirmed trials and 3 drift safe-halts with zero model calls, silent incorrect successes, or false completion, and records `code_readiness_accepted=true`. | The counted stand-in and preflight do not claim live ICA/HDX acceptance. A live result remains bound to the exact Workspace/server/application matrix, customer-approved executable, and independent effect oracle. Deployment-specific recipes, data, and thresholds stay outside the public repository. |
| Identity verification | **Experimental, armed steps only** | Wrong-entity refusal and adversarial corpora run in CI. | Unarmed clicks have no identity check. Real compiled bundles currently arm only a subset of clicks. |
| System-of-record effect verification | **Experimental** | REST, FHIR, SQL, file, and document verifier contracts catch fault classes that screen-only verification misses. A deployment with multiple reviewed read boundaries selects and preflights the strongest evidence tier before input, retains that binding through durable resume, and never downgrades after an action. | Effects are not generally inferred; both authored effects and a configured verifier are required. A selected verifier that becomes unavailable halts or enters reconciliation. |
| Lint and certification policies | **Beta** | The CLI reports coverage gaps and refuses bundles that violate a selected policy. | Certification is opt-in; `replay` remains the permissive tutorial path. Use fail-closed `run` for a deployment. |
| Durable pause, approval, and resume | **Experimental** | Checkpoint, bundle-version binding, approval, stale-pause, and resume semantics are tested. | Operator identity is recorded, not integrated with an enterprise IdP; field operation is unmeasured. |
| Typed business decisions | **Experimental** | A typed qualification API adds or updates a finite decision node without manual manifest edits and invalidates stale certification. The graph runtime pauses at the certified choice, validates a supplied principal and role, retains a signed durable receipt, restores it after a crash, revalidates the live application, and permits only the certified successor branch. | The engine does not authenticate a user. Desktop, Cloud, or a customer-local identity route must supply an authenticated principal. A decision never replaces entity identity or effect verification. |
| Reviewed judgment cases | **Experimental** | Qualification binds typed facts, local evidence hashes, reviewer provenance, and the exact decision contract to reviewed examples or counterfactuals. It preserves permanent human authority, requires reciprocal contrasts for an automatic-rule candidate, and refuses certification when a case still needs evidence. | The case layer does not synthesize executable policy from one or more examples. A reviewed automatic rule must be authored and qualified through the normal program path. |
| Qualified remote decision tasks | **Experimental** | An explicitly negotiated V2 task binds optional reviewed entity wording to the exact qualification, bundle, step, policy, and pause. V1 stays byte-compatible, and an unavailable or unrecognized class renders as the signed neutral `record` or `item` fallback. | V2 requires `openadapt-types` 0.10.x and a consumer that negotiates the schema. Actual entity identifiers and live revalidation stay inside the customer-controlled runner. |
| On-prem / air-gapped deployment | **Beta foundation** | Local queue, fail-closed run gate, egress attestation, audit-chain verification, signed release verification, fresh-environment smoke/air-gap checks, atomic blue/green update, and rollback ship. | Site firewall, storage, keys, OS hardening, identity/effect integrations, and acceptance in the customer's environment remain deployment responsibilities. |
| Desktop GUI and tray | **Beta, separate repository** | Desktop `v0.9.0` ships installable Windows, macOS, and Linux artifacts with checksums; its frozen engine lifecycle and install/launch/uninstall contracts run in release CI. | The published installer evidence covers the embedded browser lifecycle. Native and remote substrate selection is independently qualified through Flow and remains bound to the selected target configuration. |
| Hosted dashboard / control plane | **Live beta, separate repository** | The deployed service uses live Supabase, Stripe, and Modal dependencies for account and organization onboarding, checkout, exact-hash artifact ingest, attested browser workflow versions, structural reports, replacement activation, scheduling, entitlements, and metering. The reversible pre-payment contract passed 3/3 production trials. | The first genuine customer payment remains the acceptance event for the paid post-payment lifecycle. SLA, BAA, and compliance commitments apply only when included in reviewed written terms. |
| Hosted execution | **Live beta — browser** | Production mode admits exact attested browser bundles, dispatches the configured runner, authenticates callbacks, and refuses mock fallback; development mock mode remains visibly synthetic. | Desktop, RDP, Citrix, and customer-controlled regulated execution are separately scoped deployment lanes rather than capabilities implied by the browser subscription. |
| Offline update and rollback | **Beta** | The operator-pulled path verifies signed archives, installs into a fresh blue/green environment, runs smoke and air-gap checks, atomically swaps the active release, and records rollback state. | Signer trust, artifact transport, OS/container policy, backup, disaster recovery, and a customer-site rehearsal remain deployment responsibilities. |

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
- The nightly
  [`quickstart-lifecycle.yml`](../.github/workflows/quickstart-lifecycle.yml)
  installs the built wheel in a clean environment on Linux, macOS, and Windows;
  records, compiles, lints, certifies, replays, induces drift, inspects repair
  and report artifacts, uninstalls, and verifies the import is gone.
