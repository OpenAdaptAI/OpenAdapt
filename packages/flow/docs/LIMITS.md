# Limits and guarantees

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
changes, OpenAdapt can re-resolve a target from retained evidence, ask an
explicitly enabled model for a proposal, or halt for an operator.

This page defines the boundary of those claims. It is written for people
deciding whether a workflow is suitable for evaluation or deployment. It is
not a compliance certification, a safety case for a specific workflow, an SLA,
or evidence that OpenAdapt is appropriate for unsupervised clinical use.

For the current evidence behind each maturity claim, see
[`VERIFICATION.md`](VERIFICATION.md). For the package-wide maturity map, see
[`PRODUCT_STATUS.md`](PRODUCT_STATUS.md).

## Product boundary

| Capability | Maturity | What the claim means | What it does not mean |
| --- | --- | --- | --- |
| Browser record, compile, and replay | **Beta** | The reference browser path runs end to end in automated and clean-environment tests. | It is not evidence for every site, browser extension, authentication flow, or long-running production workload. |
| Healthy replay with zero model calls | **Beta** | A run that resolves from retained evidence can execute without a language or vision model; the run report counts model calls. | Zero model calls does not mean zero network traffic. The target application, hosted control plane, remote backend, or effect verifier may still use the network. |
| Deterministic re-resolution | **Beta** | Bounded visual or structural drift can be resolved through non-model evidence and recorded as a reviewable change. | It is not general adaptation to a redesigned workflow, changed business rules, or missing evidence. |
| AI-assisted repair | **Experimental** | An explicitly enabled model can propose a target or interpret a changed screen. Existing runtime checks still apply. | A model proposal is not authorization, proof of identity, or proof that a business transaction succeeded. |
| Human teaching and resume | **Experimental** | A halt can produce evidence for an operator correction, guarded promotion, and durable resume. | Field recovery time, broad authoring UX, enterprise identity integration, and non-repudiation are not established. |
| Windows UI Automation | **Experimental** | The backend contract is tested and a gated Windows environment has exercised structural resolution. | Windows is not the default supported substrate and is not broadly validated across third-party desktop applications. |
| Native macOS automation | **Research** | The browser path runs on macOS. | There is no production-candidate native macOS accessibility backend in this repository. |
| RDP and Citrix-style pixel-only automation | **Research** | Adapter and pixel-only analog tests exercise the backend seam and refusal behavior. | Real RDP session diversity, ICA/HDX, DPI changes, lock screens, latency, credentials, and clinical applications are not validated. This is not a validated Citrix integration. |
| Managed browser execution | **Beta** | The hosted lane admits attested browser bundles; production mode requires a configured real runner and refuses silent mock fallback. | It does not extend the supported claim to Windows, RDP, Citrix, PHI-bearing shared-cloud execution, an SLA, or a regulated certification. |
| On-premises / customer-managed deployment | **Experimental** | Deployment configuration, local run gates, egress checks, and audit primitives are supplied. | OpenAdapt does not configure the customer's firewall, KMS, storage, identity provider, backups, retention, incident response, or legal compliance program. |

These labels describe product evidence, not risk acceptance. A Beta engine can
still produce an unsafe workflow if the demonstration, inferred checks, policy,
or deployment configuration is inadequate.

## What "deterministic" means

The compiled program fixes the action sequence, parameters, target evidence,
postconditions, and selected policy. The runtime applies a defined resolution
ladder to the live observations. It does not ask a general-purpose agent to
re-plan the task on every healthy run.

Deterministic does **not** mean:

- the application, OCR, network, or remote desktop will return identical
  observations;
- the same screen pixels prove the same database state;
- every run will take the same time or choose the same fallback rung;
- an action is idempotent or reversible; or
- a successful report proves the user's business intent was satisfied.

A "healthy" run is one that stays within the evidence and checks encoded in
the bundle. It is not a claim that the surrounding application is healthy.

Governed execution recomputes workflow semantics, snapshots verified sealed
assets, binds parameters and worklists, detects later plaintext asset
replacement at every consumer, and persists the same authorization across
durable resume.

## Repair has four distinct outcomes

"Self-healing" is shorthand and should not be read as unrestricted autonomy.
OpenAdapt distinguishes:

1. **Automatic deterministic re-resolution.** A non-model rung finds a target
   from retained structural, template, OCR, landmark, or geometric evidence.
2. **AI-assisted proposal.** An explicitly enabled model proposes a target or
   state interpretation. Identity, risk, postcondition, effect, and policy
   checks are not intentionally bypassed, but those checks have the limits
   described below.
3. **Human teaching.** An operator demonstrates or approves a correction after
   a halt. Promotion can be refused when the correction is underdetermined or
   weakens a guarded property.
4. **Unsupported drift.** Evidence is missing, ambiguous, or contradicted. The
   expected outcome is a halt and report, not a guessed continuation.

Target repair does not infer changed business intent. Adding a required step,
removing a business state, changing a transaction's meaning, or introducing a
new authorization rule requires a new or reviewed program.

## The main incorrect-success risks

OpenAdapt is designed to halt on many ambiguous conditions. The following
classes can still produce a green run unless the workflow and deployment add
the named control.

### 1. Identity protection applies only where it is armed

The compiler records whether each applicable action has usable identity
evidence. Reports expose identity-armed coverage and list unarmed steps with a
reason. An **unarmed** click has no pre-action identity check. Reporting the gap
does not close it.

For an armed step:

- structured browser text can distinguish characters that OCR may collapse;
- a definitive mismatch halts before the action;
- OCR can be unable to distinguish identifiers such as `O` from `0` or `l`
  from `1` on a pixel-only display;
- the pixel comparison tier rejects a wrong record on a pixel-only display by
  comparing the recorded vs live identifier crop (the pixels keep the `O`/`0`
  and `l`/`1` distinction OCR collapses). It has three outcomes — mismatch
  (localized glyph change → halt), abstain (render drift or uncertainty → fall
  through), and, when enabled, verify (match). The positive **verify** path is
  jitter-robust (the crops are sub-pixel aligned before comparison, so
  cross-render jitter of the same value no longer looks like a glyph change) and
  proven zero-false-accept on a synthetic cross-render battery, but its default
  is **off** (`runtime.identity.PIXEL_VERIFY_ENABLED`, see below).

Permissive `replay` may proceed on an unreadable reversible target and reports
that condition. Governed `run` requires an affirmative live verdict for every
identity-required admitted step; mismatch, unreadable, or abstain halts before
action, and program exception edges cannot convert that safety halt into
success. Unarmed steps remain outside this guarantee and may be refused by the
selected policy.

No identity tier can distinguish two entities whose available evidence is
identical. Structured text avoids OCR glyph collapse, but it is only as unique
as the identifiers exposed by the application. A deployment that requires a
specific internal record must include that discriminator or verify it through
the system of record.

That last distinction makes correct risk classification important. Risk is
inferred from action text and metadata and can be overridden, but inference is
heuristic. An icon-only save, a generic "OK", or an Enter key that submits a
form can be under-classified. A benign label containing a write-shaped word can
be over-classified and halt unnecessarily.

For consequential or entity-sensitive workflows, require a policy that refuses
unarmed actions, review the inferred risk and tags, and test identity against
real near-match and same-name records. Pure-pixel substrates should expect more
safe halts because OCR ambiguity cannot always be resolved. The adversarial
identity evidence and measured over-halt tradeoff are documented in
[`IDENTITY_EVIDENCE.md`](validation/IDENTITY_EVIDENCE.md) and the generated claim registry
in [`VERIFICATION.md`](VERIFICATION.md).

**Pixel-verify enable bar.** The pixel tier's positive **verify** path is built
and validated but ships **disabled by default** (`PIXEL_VERIFY_ENABLED = False`).
On the self-contained jitter battery (`benchmark/pixel_identity_aligned`:
rendered MRNs plus the committed real-browser crops, re-rendered under sub-pixel
jitter, JPEG q≤10, 105–150% DPI, and theme inversion) the sub-pixel-aligned
whole-crop distance separates same-record matching renders (worst window ≈0.052)
from every different record — glyph-collapse siblings and wrong MRNs alike
(≈0.070) — with the verify gate (0.040) inside that gap, giving **zero
false-accept across the different-record trials with margin**. That evidence is
**synthetic**; a pixel false-accept is a silent wrong record, the worst possible
outcome, and no real RDP/Citrix/HDX identifier corpus has been captured yet
(that substrate is Research). The exact bar to flip the default on: reproduce
`false_accept == 0` with a comparable gap on a **real captured remote-display
identifier corpus**. Until then, verify is reachable only when a caller opts in
per risk class (`verify_pixel_identity(..., enable_verify=True)`); mismatch and
abstain remain always-on and can only ever add a halt.

### 2. A postcondition verifies only what it observes

The compiler mines visual and, where available, structural changes from one
demonstration. A step can have no meaningful observable change, or the mined
assertion can describe incidental screen state rather than the intended
outcome. In those cases replay can accept a no-op or halt on harmless change.

Common examples include:

- an action whose before and after screens are effectively identical;
- a native control whose popup exists outside the captured browser surface;
- a stable-looking tenant-specific row, menu, counter, or banner frozen into
  the demonstration;
- a typed value that the application reformats or masks; and
- a target that moved after an earlier action but before a later check.

Typed-input read-back and structural postconditions reduce this risk but remain
same-surface observations. A visible value does not prove it was durably stored.
Masked input can prove that the field changed, not what secret value it now
contains.

`lint` reports vacuous postconditions and other coverage gaps. `certify`
enforces the selected policy. Neither command understands the application's
business semantics, and the permissive `replay` command does not require
certification. Runnable is not the same as certified for a particular policy.

### 3. The screen is not the system of record

A success banner can coexist with a rejected, duplicated, partial, stale, or
later-rolled-back write. Visual verification and a screen-reading model cannot
detect a backend fault when the interface itself displays success.

OpenAdapt can verify a declared effect against a configured REST, FHIR, or
document-store verifier. When no such structured verifier is configured, a
GUI-only recording gets an auto-derived on-screen **read-back** oracle (below);
absent even that, the governed `run` gate admits a GUI write only after explicit
`--approve-unverified-writes` and only when the step has a non-vacuous screen
postcondition. The resulting authorization is bound to the exact sealed bundle,
effective runtime inputs, step, and effect-contract hashes. Replay records
`effect_approved_unverified=true`, never `effect_verified=true`; approval is
not independent confirmation. Direct API writes still require a verifier and
refuse this fallback.

The remaining boundary is material:

- the compiler does not infer a structured system-of-record binding (which
  API / DB / record / idempotency key) from a demonstration that never observed
  one; it auto-derives only the on-screen read-back below, or a flagged
  placeholder that halts;
- the operator must author and bind a structured effect to the correct record
  and run parameters;
- the verifier's permissions, query, freshness window, and independence must
  be validated in the real deployment;
- operator approval is risk acceptance, not independent confirmation; and
- a screen read-back is useful but is not independent effect verification.

A bounded fault-model study demonstrates why this matters: the screen-only
oracle silently accepted several transactional fault classes that the configured
effect path refused. See
[`FAULT_MODEL.md`](../benchmark/fault_model/FAULT_MODEL.md) and
[`EFFECT_VERIFIER.md`](design/EFFECT_VERIFIER.md). This is evidence for the
mechanism, not proof that a customer-specific effect binding is correct.

#### On-screen read-back (the no-API default oracle) is a consistency signal, not transactional proof

For a GUI-only recording (Citrix / legacy EMR with no reachable read API), the
compiler auto-derives an on-screen read-back oracle from what the demonstration
already captured — the region where the saved value rendered and, when the
recording re-opened the record, the re-navigation to it. Two strengths, one
gate, measured in
[`benchmark/effect_readback/`](../benchmark/effect_readback/RESULTS.md):

- **Different-path read-back is the default.** Before re-reading, it re-opens
  the record by a path independent of the write flow, forcing a real fetch;
  this defeats the "the form still shows what I typed but nothing persisted"
  phantom/optimistic class. Its measured false-CONFIRM rate is 0, so it is
  auto-wired with no connector and HALTs on any non-CONFIRMED verdict.
- **Same-surface read-back is NOT a default.** Re-reading the write's own
  surface cannot see a phantom/optimistic/partial save (the note is still
  painted there); its measured false-CONFIRM rate is > 0. It is wired only when
  an operator explicitly sets `effects.kind: onscreen`, and is a halt-inducing
  consistency signal, never an automatic pass.

Even a different-path read-back is **same-application**, not an independent
system of record. A read-back CONFIRMED means "the expected value is on screen
when the record is re-opened" — a consistency signal, NOT full transactional
proof. It cannot catch a partial save the app re-renders optimistically, a
duplicate / double-submit, a lost update by a concurrent writer, or a value
served from a stale cache/BFF (the `duplicate` and `stale` blind spots in the
study CONFIRM because the value is present while a separate structural fault is
not). Where a read API exists, the **structured system-of-record oracle**
(`record_written` count, `forbid_collateral_loss`, idempotency key) remains the
transactional guarantee. The ultimate safety net is never the read-back: it is
the **identity gate** (right record) plus **halt-on-ambiguity** (never guess a
target), with read-back as additive assurance on top.

### 4. Model assistance can convert a halt into a false pass

Model use is off by default. When configured, a grounding model can propose a
target and a state verifier can re-evaluate some failed visual postconditions.
Only an affirmative answer is eligible to rescue the supported state checks;
uncertainty or service failure keeps the halt.

The model still reads the screen, can be wrong, and cannot prove a hidden
transactional effect. A model-assisted run is counted in the report and no
longer qualifies as a zero-model run. Dense lists, small identifiers, unusual
rendering, and in-progress states are known hard cases. Treat the model as an
availability aid, not a safety authority, and keep consequential actions behind
identity, effect, and policy controls that do not depend on the model's answer.

#### Configuring your own grounding model (bring your own)

The grounding model is operator-selectable. The `runtime.grounding_model`
section of a deployment config chooses which model backs the fallback rung:
`provider: anthropic` (the built-in Anthropic API path) or
`provider: openai_compatible` (any `{base_url}/chat/completions` vision
endpoint — OpenRouter, Azure OpenAI, a Bedrock/OpenAI proxy, or a self-hosted
vLLM / Ollama / LM Studio). The API key is supplied by reference: `api_key_env`
names an environment variable; the key is never stored in the config.

Two boundaries are load-bearing:

- **Configuring a model does not enable egress.** `runtime.grounding_model`
  only names *which* model would be used. Whether any screenshot leaves the box
  is still governed entirely by `runtime.allow_model_grounding`, which defaults
  to off. An enabled model with egress off stays dormant and the run is fully
  local. The hosted managed runner refuses any profile that enables
  model-grounding egress at all.
- **PHI mode fails closed on egress.** When PHI mode is active
  (`OPENADAPT_FLOW_PHI_MODE`, or `OPENADAPT_FLOW_SCRUB=on`) a configured
  grounding endpoint is refused unless its host is on the admin-attested
  `runtime.phi_grounding_allowlist`; the run then stays fully local rather than
  egressing. Known public aggregators (`openrouter.ai`, `api.openai.com`,
  `api.anthropic.com`, `generativelanguage.googleapis.com`) stay blocked under
  PHI even when allowlisted, unless the operator sets
  `runtime.phi_egress_attested: true` to attest that a Business Associate
  Agreement (or equivalent) covers the destination. Ambiguity — an
  unresolvable host, an empty allowlist — always resolves to fully local.
  Non-PHI runs are unaffected: any configured endpoint works under the normal
  egress opt-in.

None of this changes the safety authority. A configured model, like the built-in
one, only *proposes* a point; the deterministic identity band and risk gate
still dispose before any click, and irreversible steps refuse a grounder
resolution and halt.

### 5. A halt is not a rollback

A run may halt after earlier steps have already changed the application or
system of record. OpenAdapt does not provide a general transaction spanning an
arbitrary GUI workflow. Compensation exists only where a deployment has
explicitly implemented and verified it.

Before resuming or teaching a halted consequential workflow, reconcile prior
effects, confirm the current entity and application state, and ensure the bundle
and approval still match the paused run. A report that accurately names the
halt does not prove earlier actions were harmless.

## Interaction and environment limits

The reference path is strongest when the target remains inside one captured
browser surface and the demonstration exposes observable outcomes.

| Condition | Current boundary |
| --- | --- |
| Zoom, DPI, font, layout, or viewport changes | Structural evidence can survive some reflow and visual rungs can survive some movement, but support is workflow-specific. Large rescale or reflow can halt. |
| Native select menus, file choosers, permission prompts, and secure desktops | OS or browser chrome may not appear in the captured surface and may be unrecordable or undrivable. Prefer an application-level or keyboard/API path. |
| New tabs and windows | Opening a tab can be observed structurally where the backend supports it; interaction inside additional windows and multi-window coordination are not a general supported path. |
| Drag and drop or gesture-heavy controls | Not a general supported primitive. Use a structured/API alternative or validate a purpose-built workflow. |
| Dates, locale-sensitive fields, and auto-formatting | The same keystrokes can produce different values across platforms or locales. Require an explicit read-back or system effect and test the deployment locale. |
| Slow or asynchronous applications | A state that outlasts configured waits can halt. Increasing a timeout does not establish correctness. |
| Transparent overlays or intercepted input | The action can be swallowed and detected only by a later check. If that check is vacuous, the no-op can be silent. |
| Parameterized entity selection | Replacing typed text does not automatically make recorded target evidence generalize to every entity. The resolved row still needs identity evidence for the runtime value. |
| Tenant, version, and dataset transfer | A demonstration can freeze tenant-specific state. Re-recording, recompilation, or reviewed program changes may be required. |

## Privacy, PHI, and sanitized artifacts

Compilation does **not** make a recording or bundle PHI-free. Recordings can
contain full screenshots, typed values, DOM or accessibility text, and
parameters. Bundles can retain target crops, labels, postconditions, identity
evidence, and example values. Machine-readable run reports and live frames can
contain data observed during execution.

OpenAdapt's outbound artifact path works on a copy:

1. transform supported text and still images under a named scrubber policy;
2. preserve and validate the recording or bundle's operational structure;
3. account for every file and refuse unsupported or unknown content;
4. rescan the derivative and expose remaining findings;
5. review it locally, including rendered images and text, before approval; and
6. freeze the exact approved archive and bind approval to its hash.

Changing the derivative invalidates approval. A changed executable bundle must
also pass separate runtime-semantics validation; privacy approval is not
executability proof. The safer authoring path is to review and approve a
sanitized recording derivative, then compile and validate the resulting bundle.

Scrubbing means **verified coverage under the configured detector, policy, and
review**, not a mathematical guarantee that no PHI remains. OCR and named-entity
recognition can miss unusual text, handwriting, images, or domain-specific
identifiers. Human review materially improves confidence but is still fallible;
regulated deployments must define reviewer identity, separation of duties,
acceptable residual risk, and incident handling.

A clean design-time derivative does not sanitize live execution. A runtime can
reintroduce PHI through the target application, parameters, screenshots,
postconditions, model requests, effect queries, logs, and reports. Keep those
observations inside the declared execution boundary and apply storage,
retention, access, and egress controls there.

See [`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md) for the review and
exact-hash workflow, [`PRIVACY.md`](PRIVACY.md) for the field-level data map,
and [`ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) for component
boundaries.

## Deployment responsibilities

### Managed browser lane

Only upload the exact artifact admitted by the destination's policy. An
approved sanitized derivative does not authorize PHI-bearing runtime data in a
shared managed runner. Do not use the managed lane for PHI unless the written
service scope, deployment architecture, retention controls, and applicable
agreements explicitly cover the complete runtime data flow.

### Customer-managed and on-premises lanes

Running in a customer boundary changes where data and execution live; it does
not improve target resolution or verification by itself. The operator remains
responsible for network allowlists, firewall enforcement, secrets and KMS,
host hardening, storage encryption, backups, retention, access control,
monitoring, effect-verifier integration, update validation, and disaster
recovery.

The repository supplies integrity manifests, optional bundle encryption,
destination policy, local egress checks, and audit primitives. A content digest
proves byte identity, not publisher identity. A local hash chain is tamper
evident against ordinary changes, not tamper proof against an administrator who
can rewrite it. Local approvals record provenance but are not enterprise-IdP
signatures or non-repudiation.

OpenAdapt does not claim that this repository alone satisfies HIPAA, PHIPA,
PIPEDA, or another legal regime. It does not itself provide a BAA or independent
security certification. See [`ON_PREM.md`](ON_PREM.md) and
[`COMPLIANCE.md`](../deploy/on-prem/COMPLIANCE.md).

## Reading the benchmark evidence

The reproducible MockMed comparison shows that, on one simple browser task,
compiled replay completed the tested runs with lower latency and no model calls
on healthy runs than the tested computer-use agent. Both arms passed the same
external success check in that sample. The result supports the cost-and-latency
mechanism; it does not establish a production reliability advantage, long-term
maintenance cost, or safety on a real clinical system. Full task, sample,
oracle, model, pricing assumptions, and artifacts are in
[`benchmark/BENCHMARK.md`](../benchmark/BENCHMARK.md).

The OpenEMR public-demo comparison is a bounded field check against fake data
on a shared instance that resets. It is not exactly reproducible and is not
evidence of clinical production safety, real-patient operation, Citrix support,
or superior reliability. See
[`benchmark/openemr/BENCHMARK.md`](../benchmark/openemr/BENCHMARK.md).

When evaluating either result, include silent incorrect success and over-halt,
not only task success. Also account for demonstration and review time,
maintenance interventions, recovery time, effect-verifier integration, and the
cost of safe halts.

## Minimum deployment review

Before unattended execution of a consequential workflow, require evidence that:

- the exact bundle passes strict lint and the intended certification policy;
- every entity-sensitive and consequential action is identity-armed and tested
  against realistic near matches;
- risk classifications and tags have been reviewed by someone who understands
  the target application;
- every write declares the intended effect and a correctly scoped independent
  verifier is configured, or the lack of verification is explicitly accepted;
- every step has a meaningful postcondition and known same-surface limitations
  are accepted;
- the fail-closed `run --dry-run` gate admits the exact sealed bundle and
  compiler version intended for deployment;
- secrets, storage, screenshots, reports, checkpoints, logs, and retention are
  controlled inside the execution boundary;
- model egress is disabled or explicitly allowlisted and tested; and
- halt, reconciliation, approval, resume, rollback, and incident procedures
  have been exercised in the real environment.

Passing this review narrows known risks. It does not turn an application-level
automation into an independently certified transaction system.
