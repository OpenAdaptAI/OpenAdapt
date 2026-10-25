# Limits and guarantees

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
changes, OpenAdapt can re-resolve a target from retained evidence, ask an
explicitly enabled model for a proposal, or halt for an operator.

This page defines the boundary of those claims. It is written for people
deciding whether a workflow is suitable for evaluation or deployment. It is
not a compliance certification, a safety case for a specific workflow, an SLA,
or evidence that OpenAdapt is appropriate for unsupervised clinical use.

For the current evidence behind each capability claim, see
[`VERIFICATION.md`](VERIFICATION.md). For the package-wide evidence map, see
[`PRODUCT_STATUS.md`](PRODUCT_STATUS.md).

## Product boundary

| Capability | Evidence basis | What the claim means | What it does not mean |
| --- | --- | --- | --- |
| Browser record, compile, and replay | **Required CI and counted attach trials** | The launched-browser path runs end to end in automated and clean-environment tests. The attached-browser path reuses the same recorder and passes 3 real Chromium record-and-compile trials with secret exclusion and external-browser survival checks. A live resize case also compiles actions from two viewport and device-scale baselines. | It is not evidence for every site, browser extension, authentication flow, or long-running production workload. Attach mode requires a loopback CDP endpoint and one same-origin Chromium tab. An action that overlaps a resize is refused because its pre-action coordinate evidence is not exact. The custom Capture extension remains a prototype and is not a direct replay path. |
| Healthy replay with zero model calls | **Required CI** | A run that resolves from retained evidence can execute without a language or vision model; the run report counts model calls. | Zero model calls does not mean zero network traffic. The target application, hosted control plane, remote backend, or effect verifier may still use the network. |
| Deterministic re-resolution | **Required CI** | Bounded visual or structural drift can be resolved through non-model evidence and recorded as a reviewable change. | It is not general adaptation to a redesigned workflow, changed business rules, or missing evidence. |
| AI-assisted repair | **Required CI contracts; measured on synthetic apps; deployment evidence required** | An explicitly enabled model can propose a target or interpret a changed screen. Existing runtime checks still apply. Once a deployment names a repair agent, every stop that wrote nothing goes to it by default, and a person approves each fix unless a signed policy pre-approves its class. On MockMed's 22 drift modes, 6 of 21 stops that reached repair resumed and verified, with 0 wrong saves in 66 trials ([benchmark/auto_repair](../benchmark/auto_repair/README.md)). | A model proposal is not authorization, proof of identity, or proof that a business transaction succeeded. A stop after a possible write is never repaired automatically. The measured rate is for one agent on one synthetic app; it is not a field repair rate. |
| Human teaching and resume | **Required CI contracts; field evidence required** | A halt can produce evidence for an operator correction, guarded promotion, and durable resume. | Field recovery time, broad authoring UX, enterprise identity integration, and non-repudiation are not established. |
| Windows UI Automation | **Counted task acceptance** | The native UIA backend completed 3/3 exact WinForms effects and 3/3 refusals for both stale and ambiguous targets. | Acceptance is bound to the tested workflow and Windows environment; each application retains its own identity, effect, and version qualification. |
| Native macOS automation | **Counted task acceptance** | The AX/AppKit backend completed 3/3 exact TextEdit file effects and refused an ambiguous two-window target; structural record/locate/refuse behavior is covered separately. Known AX-only ` - Google Chrome` and ` - Google Chrome for Testing` window-title suffixes are accepted only for the corresponding exact CoreGraphics application owner; PID, unique-candidate, focused/main, and topmost-window checks remain mandatory. | Acceptance is bound to the tested host and application. Accessibility exposure and supported native actions vary by application. |
| Native Linux automation | **Counted task acceptance plus required CI** | Required current-main CI exercises AT-SPI against GTK3 with 3 exact effects, 3 ambiguity refusals, and 3 stale-target refusals. | The built-in path is X11. Wayland needs an operator-approved portal session, and each application retains its own qualification. |
| RDP automation | **Counted task acceptance plus required CI** | A real-network Aardwolf/Windows batch passed 3/3 with an independent guest-tools oracle; a separate real-FreeRDP batch covers record, compile, governed replay, and drift refusal. | The two records do not by themselves cover arbitrary applications, WAN conditions, display policy, or Citrix ICA/HDX. |
| Citrix / VDI pixel-window automation | **Required CI plus counted no-DOM stand-in** | The dedicated exact-Workspace-window backend, readiness gate, governed run, and durable resume passed 3 healthy effect-confirmed no-DOM trials plus 3 drift safe-halts. | The retained record explicitly has `ica_hdx_accepted=false`; live ICA/HDX acceptance stays bound to the exact deployment instead of being inferred from a stand-in or RDP. |
| Managed browser execution | **Separate product target and workflow admissions** | The hosted lane admits attested browser bundles; production mode requires a configured real runner and refuses silent mock fallback. | It does not extend the supported claim to Windows, RDP, Citrix, PHI-bearing shared-cloud execution, an SLA, or a regulated certification. |
| On-premises / customer-managed deployment | **Release and clean-machine evidence; site acceptance required** | Deployment configuration, local run gates, egress checks, and audit primitives are supplied. | OpenAdapt does not configure the customer's firewall, KMS, storage, identity provider, backups, retention, incident response, or legal compliance program. |
| Compose separately recorded bundles | **Required CI** | `openadapt-flow compose` writes a parent sequencer from named compiled children plus a handoff contract. Unit tests cover authoring validation, fail-closed missing-handoff HALT, and a two-child fixture whose second child is a local mock backend. | It does not merge two recordings into one backend, retarget a bundle onto a surface it was not recorded on, or introduce a process contract. Live Citrix, Production admission, and cross-application field campaigns are not claimed here. |

These evidence entries do not assign a product lifecycle state or accept risk.
An actively admitted release can still produce an unsafe workflow if the
demonstration, inferred checks, policy, or deployment configuration is
inadequate.

## Composing separately recorded bundles

`compose` sequences already-compiled children through the governed `run`
path. A child starts only after every predecessor ends `VERIFIED`, or a halt
class named with `--allow-halt`. A handoff copies an effect-bound parameter
from a confirmed receipt. Missing evidence HALTs. Subflows and worklists
stay inside each child, on that child's surface.

## Parallel worklists

`--workers N` runs a worklist's items on N signed-in sessions at once
([`THROUGHPUT.md`](THROUGHPUT.md)). Every item keeps every check. The run
refuses N above the deployment's `worklist.max_workers`, fewer accounts than
workers, two accounts for the same user, two workers sharing one session, a
program with steps outside its loop, and rows not declared
`items_independent`. It can't tell on its own whether two rows touch the same
record; that declaration is yours. A halt on any worker stops new items on
all of them, and an item claimed before a crash is never run again
automatically: it is marked `uncertain` for a person to check. The pipelined
read-back window applies only to an item's last save and only to read-backs
that don't use the screen; durable runs, which include the Standard and
Regulated profiles, always read back inline. Parallel RDP runs use the
FreeRDP session pool and have unit tests only. Throughput figures come from
one machine and a local OpenEMR test copy, not from a production EMR.

## Short parameter values

The right-record check binds a typed value into its expected text only when
the value has at least 4 characters. An item whose bound value is shorter,
such as the first name "Bo", halts at the identity check instead of saving.
This is deliberate: a short value matches too much unrelated text to identify
a record.

## Save triage

`openadapt-flow triage` orders verified saves for review under
`problems_only` and proposes one batch approval for clean ones. A batch
approval records that a person agreed with saves the engine already made and
checked; it authorizes nothing, and a stopped save is never batched. The v0
rule and the learned score see only what the record check compared and the
item's history, so a wrong value in a field the record check doesn't read,
or a value the source document got wrong, can look clean. The audit sample
estimates how often that happens. The learned score's miss bound assumes new
saves resemble the labelled ones; it doesn't survive an application,
clinic or form change. The only evaluation is on synthetic data. See
[`DECISION_TRIAGE.md`](DECISION_TRIAGE.md).

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

Automatic repair is bounded the same way. It takes only a stop that wrote
nothing (`HALTED_BEFORE_EFFECT`). A stop after a step the run gate counts as a
save has acted is `RECONCILIATION_REQUIRED`, even when the app saved nothing,
and goes to reconciliation and a person. On the recorded OpenEMR referral
stops of 2026-10-19 that was every stop (8 of 8): the "Create New Patient"
click before the duplicate-check dialog counts as a save. Measured on MockMed,
the default loop repaired a renamed or moved control (6 of 6 stops) and did
not repair a control moved into a closed menu, a new interstitial, a new
required choice, or a pixel postcondition that failed after a font change
(0 of 15). On openIMIS's relabelled Save, once a person reviewed the two
steps whose risk was unclassified, the stop reached repair 3 of 3, but no fix
passed the probe: the replay sees the Save disabled where the fix was cropped
from the enabled one. On silent-saves 1.1.0's UI-change blocks (#1380), 18
of the 28 items that stopped every time now end correctly: a page zoom no
longer fails a correct screen's check, and a repaired agent-compiled program
keeps its admission. A What's New screen dismissed by "Continue to tasks" still
stops every item: "Continue" can submit, so a person must classify that
control first. A compiled program is also reused only for the record check and
app environment it was admitted under ([JIT.md](JIT.md)).

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
- the recorded identifier crop shows the demonstration's screen. When the
  identity band contains a workflow parameter and the run's value differs from
  the demonstrated one (for example, a first name typed earlier in the run),
  the pixel tier doesn't compare the crops, because the right record renders
  different pixels too. The OCR parameter check decides instead: it puts the
  run's value into the recorded band and requires the whole band to match. A
  band that rests on a glyph-confusable identifier keeps the pixel comparison.

On a pixel-only display, the OCR tier also stops the correct record whenever
its own identifier contains a look-alike glyph (`O`/`0`, `l`/`1`, `B`/`8`).
A deployment can remove most of that cost by giving the runtime a complete
list of the identifiers that exist in the target system
(`runtime.known_ids_path`, see `openadapt_flow/runtime/known_ids.py`). The
abstain is lifted only when the first read and a second read of a fresh
screenshot agree, every other identity check passed, and exactly one listed
identifier falls in the look-alike class OCR read. If a look-alike twin is
listed, the run still stops. The list must come from the system of record,
not from the screen being checked, and must be complete: a list that misses
a real look-alike record lets that record verify.

The list is a configured export of the system of record. Before it is used,
an operator runs `openadapt-flow known-ids confirm EXPORT --source ... --by
...` and confirms its source and completeness. The confirmation records who
confirmed, when, the source, the row count and the export's SHA-256. Replay
halts with a named reason when the confirmation is missing
(`known_ids_unconfirmed`), the export's hash no longer matches
(`known_ids_changed`), or the export is older than
`runtime.known_ids_max_age_hours` (`known_ids_stale`, default 24). Measured
before and after numbers are in
[`benchmark/known_ids/KNOWN_IDS.md`](../benchmark/known_ids/KNOWN_IDS.md).

Before a step stops on a look-alike identifier, the replayer takes extra looks
that change nothing
([`openadapt_flow/runtime/identity_looks.py`](../openadapt_flow/runtime/identity_looks.py)):
the same read on two fresh screenshots, the 2x re-read, the known-ID list when
one is configured, and, where the surface allows it, a select-and-copy of the
identifier's text box (`runtime.identity_copy_look`). OCR looks can't settle
the record on their own, because a look-alike twin can read the same on every
frame and at every size. Only two looks can: the known-ID list, and a copied
identifier that equals a run parameter in the identity band. A look that names
another record stops the run. Each look is recorded on the step's identity
check.

The copy look has limits. It needs a surface where selecting the identifier
and copying it changes nothing, and a clipboard that reaches the machine Flow
runs on (RDP or Citrix clipboard redirection). It drags across only the one
text box that holds the identifier, and the run stops if the screen changes
anywhere else during the look. A canvas with no selectable text has no copy
look. A recording's own identifier is OCR text and can be a misread (the RDP
kiosk recorded `A1001` as `Alo01`), so in a workflow that always opens the
recorded record, the copy can stop a run but never confirm one. It confirms
only a record chosen by a run parameter. Governed and qualification runs never
take it. The check just before the click takes no input; it reuses the copy
only when the identifier's pixels are unchanged since the look. On a rendered
dense record list with the ID as a run parameter, false stops on the correct
row went from 232 to 24 of 288 with the copy look, and wrong-record accepts
stayed 0 of 288
([`benchmark/identity_looks/IDENTITY_LOOKS.md`](../benchmark/identity_looks/IDENTITY_LOOKS.md)).
When the recording itself read the ID with a look-alike glyph, the band is not
linked to the run parameter and the copy cannot confirm the record; 22 of the
24 remaining stops were that case.

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
heuristic. A generic "OK" or an Enter key that submits a form can be
under-classified. A benign label containing a write-shaped word can be
over-classified and halt unnecessarily.

Icon-only controls get two compile-time rules. A single OCR glyph (a
floppy-disk Save icon reads as "日") is not a label, so the click needs
qualification review instead of passing as a labelled non-write. And when a
demonstration entered data and no later step declares a write, every icon-only
click after the last entry is presumed to commit that record: it compiles
irreversible, and qualification requires its record check. An icon-only click
that only navigates after data entry is therefore over-classified until an
operator reclassifies it.

On a pixel surface an icon-only click carries no text that names its control.
When the recorder can move the pointer without clicking
(`Recorder(capture_hover=True)` on a backend with `hover`), it rests the
pointer on each click target first and keeps that frame. For a click with no
other identity evidence, the compiler stores what changed near the target, such
as the control's highlight or tooltip, as the step's hover evidence
([`openadapt_flow/runtime/hover_evidence.py`](../openadapt_flow/runtime/hover_evidence.py)).
At replay, on the pixel rungs, the runtime rests the pointer on the resolved
target, waits the same 0.8 s, and compares. A different response, or a
response where the recording saw none, stops the run before the click. A
missing response, or one that can't be compared (another render with no
readable text), is recorded on the step and changes nothing. A tooltip that
opens later than the wait looks like a different response and stops the run.
Controls that don't answer the pointer, such as most text fields, get no
evidence this way. The browser, remote-display (Citrix and RDP client window)
and FreeRDP backends can hover. The Aardwolf RDP transport and the native
Windows, macOS and Linux backends can't move the pointer without a button
yet, so they take no hover look. On an in-process kiosk with two identical
icons that differ only by tooltip, swapped between recording and replay, the
look-alike icon was clicked in 2 of 3 runs before and 0 of 3 after
([`benchmark/hover_identity`](../benchmark/hover_identity/README.md)).

A pixel step is checked on one frame and clicked a moment later. Consequential
identity-gated clicks were already bound to the exact checked frame. Every
other pixel click now gets a check-to-click guard (`Replayer._pixel_recheck`):
right before the click, the runtime takes a fresh frame and compares the
target region, the identity region and the hover response with the frame the
step was checked on. If one changed (a dialog opened, the list re-sorted), it
doesn't click. It re-resolves the target and re-runs the identity check on
the fresh frame, with no input, and clicks only when the next frame matches
that one. After two re-observations that still see a change, or when the
target or its identity can't be confirmed again, it refuses the click. Each
change is recorded on the step (`StepResult.check_to_click`). A change smaller
than 4 pixels, or under 24 levels per channel, doesn't count. Structured
targets are bound by element tokens instead and aren't rechecked.

A consequential click must be tied to a record before Flow delivers it. Inside
a table row, the row is that record. An icon-only Save that floats beside a
form (openIMIS's claim Save has no text, accessible name, or tooltip) has no
row, so the browser refuses it. When the browser recording shows how that
control relates to the fields the demonstration typed into, the compiler
records a **submit binding** (`Step.submit_binding`,
[`submit_binding.py`](../openadapt_flow/submit_binding.py)):

- form ownership: the control and the fields share one HTML form, or the
  fields share a form and the control sits outside every form, or nothing has
  a form;
- submit role: the form's submit button, another button of that form, or a
  button outside any form, plus the control's tag, type, role, and a digest of
  its icon geometry;
- position: the control follows every filled field in page order, sits in the
  same quadrant of their bounding box, and keeps the same tag path from the
  form or shared container down to it;
- uniqueness: it's the only rendered, unlabeled control after those fields.
  Controls in table rows and the adornments inside one field's own widget
  don't count.

A run recomputes the same observation for the element at the resolved point,
against the fields that run typed into, and clicks only when every recorded
value matches. The browser leases that observation until delivery, so a
second icon, a replaced field, or a moved control in between refuses the
click. The binding never relaxes another check: the resolution floor, the
identity band, and the delivery lease still apply.

The compiler doesn't bind the click when the demonstration was ambiguous, for
example when two unlabeled controls follow the filled fields, the control
belongs to another form, or the fields span two forms. It records why in
`Step.submit_binding_unbound_reason`, lint reports it as an error, and a run
refuses that click before any input. A pixel-only recording (RDP, Citrix)
carries no DOM structure, so it gets no binding, and a backend that can't read
the page structure refuses a bound click.

The binding ties the click to the entry the run made. It doesn't check that
the entry is right: the values typed come from the run's parameters, and only
a record check can confirm what the application saved.

A labelled save on a create-new form (Trac's **Create ticket**, NetBox's
**Create**) has the same problem: no existing record is on screen next to it,
so it compiled with no identity check, and a policy that refuses unarmed saves
refused it. Its record is the one the run is typing into the form. When the
recording shows the save is a button of the one form that owns every field
typed since the last save, the compiler gives it a **form identity**
(`Anchor.form_identity`; counts and tokens only, no values). Before the click,
a run requires that the button's form holds exactly the fields that run typed
into, and that each still holds the value the run left in it. A field the page
rewrote after typing, a field that is gone, or a button of another form halts
before the click (identity mode `form`). A backend that can't read the page
leaves the identity unreadable, which halts too. A field whose own script
reformats its value when it loses focus (a phone-number mask, for example)
also halts, because its value no longer matches what was typed.

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
whole-crop distance separates same-record matching renders (worst window
0.0615, p95 0.0547) from every different record — glyph-collapse siblings and
wrong MRNs alike (closest 0.0705) — with the verify gate (0.040) below both,
giving **zero false-accept across the different-record trials**. The margin
between the worst same-record window and the closest different record is
**0.0090**, which is narrow; the numbers are `distance_stats` in
[`benchmark/pixel_identity_aligned/evidence.json`](../benchmark/pixel_identity_aligned/evidence.json). That evidence is
**synthetic**; a pixel false-accept is a silent wrong record, the worst possible
outcome, and no real RDP/Citrix/HDX identifier corpus has been captured yet
(so the shipped pixel-verify default remains off). The exact bar to flip the default on: reproduce
`false_accept == 0` with a comparable gap on a **real captured remote-display
identifier corpus**. Until then, verify is reachable only when a caller opts in
per risk class (`verify_pixel_identity(..., enable_verify=True)`); mismatch and
abstain remain always-on and can only ever add a halt.

**Pixel tier and a different rasterizer.** Those thresholds hold only between
crops drawn by the same rasterizer. When ClearType was switched, the tier saw
glyph-sized differences at every edge and reported a different record for the
right one. It now abstains when the recorded and live crops differ in colour
fringing or anti-aliasing, and on a run replayed at another display scale. The
OCR tier then decides, and an identifier OCR cannot confirm, such as one with
1/l or 0/O, still halts. In the display-scaling sweep
([`benchmark/pixel_scale`](../benchmark/pixel_scale/README.md)) the RDP
fixture's own IDs (`MRN A1001`) halted at the identity check at every render
other than the recorded one, before and after the extra identity looks: that
workflow always opens the recorded record, whose ID the recording read as
`MRN Alo01`, and the kiosk has no selectable text.

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

A read-back taught by demonstration
([`EFFECT_KIT.md`](EFFECT_KIT.md#a-read-back-taught-by-demonstration-now-show-how-youd-check-it-saved))
has three more limits. It must reset client state (a reload, a new search, a
reopened window) before it reads. Flow recognizes a reload key, a closed and
reopened window, and a submitted search from the recording; any other reset
is the person's declaration, and Flow records it as declared rather than
checked. On a pixel-only display, a read that differs from the expected
value only in `O`/`0` or `l`/`1` is `INDETERMINATE`: the check halts instead
of guessing. It confirms nothing until a person admits it. Its assurance
grade is `screen_readback`, never `independent_readback`.

**Without a read that is independent of the acting session, the run cannot
be `VERIFIED`.** That read is either a system-of-record read (REST, FHIR, SQL,
file; tier 1) or the application's own screens read in a separate signed-in
session (tier 2): a record page in a separate browser (`ui_readback`), a
relaunched desktop app or a second RDP session (`fresh_session`, tested on
mocks only), or a look-up in a separate desktop, RDP or Citrix session
(`desktop_readback`, tier 2 with its own RDP logon). None of them goes around
the application, so none is a system-of-record read, and each has its own
limits ([`EFFECT_KIT.md`](EFFECT_KIT.md#separate-session-ui-read-back-ui_readback),
[`desktop_readback`](EFFECT_KIT.md#separate-session-desktop-read-back-desktop_readback)).
If a person can look the record up, the check can too. A pixel-only Citrix or
RDP deployment that declares no such check is the remaining case. Reopening the
record in the acting session (tier 3) is reported as its own check level,
`reopened` ("Reopened in the same session"), and is not `VERIFIED`: a
confirmed reopen ends `COMPLETED_UNVERIFIED`, and the agent result says
`not_sure_if_saved` with `checked_below_minimum`
([`READBACK_LADDER.md`](READBACK_LADDER.md)). A workflow's `minimum_readback`
can raise the floor to `system_of_record`, never lower it.
A save that only a later report can confirm (a claim acknowledgment, a
nightly export) ends `SAVED_AWAITING_CHECK` and becomes `VERIFIED` only when
that report accepts it ([`deferred_report`](EFFECT_KIT.md#checked-later-deferred_report)). The on-screen
oracle can still halt when the region is unreadable or wrong. If a
consequential write may already have been delivered and the check did not
confirm it, the production `transaction_outcome` is `RECONCILIATION_REQUIRED`,
never `VERIFIED`. Halt-on-doubt is unchanged. This is not live Citrix
Production evidence; a live ICA/HDX session is a deployment-specific
qualification. Wire the independent read before paying a sprint for a
`VERIFIED` outcome.

The reopen itself has limits. It earns the `reopened` level only when its
navigation contains a recognized state reset; without one it is
`same_screen`. A read-only guard refuses any reopen step that presses a
delete key, or `s`, Enter, Return or Delete with a modifier held (any
spelling: `control+s`, `Meta+S`, `ctrl+shift+s`, `alt+s`, `shift+delete`).
It refuses a click on a control labelled with a commit word (Save, Submit,
Delete, Sign, Send, ...), a click where OCR reads no label (an icon-only
button may be Save), and a click whose label can't be read at all. It reads
labels by OCR, so a control drawn with text that doesn't say what it does
passes, Enter alone is allowed (a search needs it), and typed text is not
checked. A whole-word match can also refuse a harmless control ("Post-op
notes"); the check then halts. A reference number is read from one screen
region and must be the only match there; two different numbers, or none, halt
the check. A search by a run input (this run's patient) can open an older
record with the same value, and Flow can't read the record before the save
without leaving the form, so such a check confirms only when the reopened
record shows this save's reference number and no other; an application that
shows no reference number halts there.

### 4. Model assistance can convert a halt into a false pass

Model use is off by default. When configured, a grounding model can propose a
target and a state verifier can re-evaluate some failed visual postconditions.
Only an affirmative answer is eligible to rescue the supported state checks;
uncertainty or service failure keeps the halt.

The state verifier never re-evaluates a step that can change a record: a step
marked irreversible, a step that reads as a write (Save, Submit), a step that
declares a system-of-record effect, or a step still waiting for risk review.
On those steps the screen check is the only on-screen confirmation of the
write, so a failed check stays failed, exactly as it would with no state
verifier, and the run stops unless the workflow has its own handler for that
step. The action was already sent, so a stopped run's transaction outcome is
`RECONCILIATION_REQUIRED`: a person checks the record before anything is
retried. Navigation and read-only steps can still be
rescued. A wrong answer there lets the run go on to the next step, which still
runs its own identity, risk, and screen checks before it acts.

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
arbitrary GUI workflow, and it does not buffer writes until a workflow's checks
have all passed.

What it does provide ([`AFTER_A_STOP.md`](AFTER_A_STOP.md)):

- Every run report has a change ledger: each step that could have changed the
  app, in order, with each declared effect marked confirmed, refuted or
  unknown. The halt message, `explain` and `REPORT.md` say what may need
  undoing.
- `lint` warns when a write comes before a right-record check or a halting
  precondition that the program could have done first. It suggests an order
  and never reorders the bundle.
- A bundle can declare an undo step for an effect. After a halt it is offered
  to a person, runs only after that person approves it, runs at most once,
  and counts as done only when an independent read of the system of record
  confirms its own declared effects.
- `undo propose` finds candidate undo steps for each saving step (another
  recording, captured traffic, or the field's old value) and tries each one
  on a declared test copy. It writes a candidate into the bundle, as a
  proposal, only when every record afterwards matches the read-back from
  before the save. A person approves a proposal once (`undo accept`), and
  each run still needs approval after the stop.

The limits of that:

- A step with no declared effect is `unknown` in the ledger. Flow can't say
  whether it changed anything.
- An undo exists only where someone declared it or approved a verified
  proposal. Flow never uses an undo no person approved.
- A proposal is verified once, on a test copy, with the bundle's parameters.
  It is evidence that the inverse path works on that copy, not a proof for
  every record in production.
- A proposal must restore every record exactly. An app that cancels by
  changing a status, or writes an audit row when a record is deleted, fails
  verification unless a person names those fields with `--ignore-field`.
- Flow builds API undo steps only from JSON requests. A browser form post, or
  a Delete control on the saved record's page, needs a recording of a person
  doing the undo.
- A proposal that finds its record (`locate`) or restores old values
  (`restore`) needs values the replayer keeps just before the save. They exist
  only for runs whose bundle already held the proposal, and only when the
  save's record check read the system of record before the save.
- An undo can fail or be refused. Then a person must repair the record by
  hand.
- No undo is offered for a workflow with a loop: the undo can't be bound to
  one iteration's record.
- The write-ordering check covers straight-line bundles only, and it can't
  tell whether a later check depends on an earlier write.
- Duplicate-write compensation (`effects.compensation`) is the only automatic
  repair, and only where a deployment has implemented and verified it.

Before resuming or teaching a halted consequential workflow, reconcile prior
effects, confirm the current entity and application state, and ensure the bundle
and approval still match the paused run. A report that accurately names the
halt does not prove earlier actions were harmless.

### 6. An agent that reads the app can be steered by it

When an agent does a task once so it can be compiled (teach), or proposes a
repair, it reads the live app. A record note, a banner, a tooltip, an image's
alt text or a server response can hold text written to steer it. Published
work on computer-use agents shows such text often works. OpenAdapt does not
rely on the agent ignoring it. The full analysis is in
[`THREAT_MODEL.md`](THREAT_MODEL.md).

What is enforced:

- Every teach path runs under a teach scope declared before the agent reads
  the app: `compile_agent`, `compile-agent`, teach over MCP and the airbag
  guard. The agent can type only declared values, can't follow a link or submit a form to
  an undeclared origin, and can't make the browser reach an undeclared origin.
  With a write adapter for the app, a write to another record, an undeclared
  field or an undeclared value is aborted before it is sent. After the run,
  Flow reads the system of record and refuses the draft unless it shows
  exactly the declared write. On MockMed, every injected instruction in the
  test suite (5 places by 4 goals, and each goal through each path) is
  blocked or caught this way, with a scripted agent that obeys all of them.
  The airbag guard can't refuse keystrokes in the agent's own browser; it
  blocks requests and saves instead.
- Self-repair refuses a proposed control that leads to another origin.
- A save approval shows who asked, the agent's own reason (as a quote), the
  screen text it saw, and any of that text that reads like an instruction to
  an agent.

What is not:

- Over MCP without `--teach-tasks`, the agent declares the record at
  `teach_start`; after reading the app, a hijacked agent can start again with
  another record. The person confirming the record check sees it.
- The airbag guard sees requests, not keystrokes; without a write adapter, a
  save's values are judged by the read-back after it lands.
- A self-repair bridge may type text that the screen shows, so injected text
  can become a typed value in a proposed repair. The person approving sees it.
- A steered agent can still read other records in the same app and send what
  it read to its model provider (scrubbed where scrubbing is on).
- Without a write adapter, and on every surface other than the web, a wrong
  write lands first and is caught by the read-back afterwards; it then needs
  reconciliation.
- Values chosen by clicking (not typing), drag and drop, and WebSocket
  traffic are checked only by the write adapter or the read-back, if at all.
- The instruction detector on approval forms is a heuristic display aid.
- No live model has been run against these fixtures yet; the live-model test
  is off by default because it costs money.

## Interaction and environment limits

Qualification is strongest when the demonstration retains exact observations
and exposes independently verifiable outcomes.

| Condition | Current boundary |
| --- | --- |
| Zoom, DPI, font, layout, viewport, or monitor changes | Browser attach recording accepts a stable viewport or device-scale change between actions, starts a new exact geometry epoch, and refuses an action that overlaps the transition. Native offline conversion can normalize a stable source-window move, resize, monitor change, or scale change into one fixed output viewport when every timeline row carries valid mapping metadata. It refuses a malformed timeline or a changed output viewport. RDP and Citrix live backends can rebaseline geometry between actions and refuse a change during the exact-frame lease. On remote-desktop replays, a session redrawn at another display scale is detected at the first step: the runtime fits one scale from the recorded labels and continues on a view resampled to the recorded geometry, with every check unchanged. In the [display-scaling sweep](../benchmark/pixel_scale/README.md) (in-process renderings of the RDP and canvas kiosks, 100 to 175%, ClearType on and off) this took healthy runs from 3 of 24 to 17 of 24 with no wrong write. A reflowed layout, a font change without a scale change, a scale change during the run, and a changed pinned FreeRDP session profile still halt. Browser zoom on the web path still halts at the first step ([`cosmetic_drift`](../benchmark/cosmetic_drift/COSMETIC_DRIFT.md)). Other rescale or reflow behavior remains workflow-specific. |
| Native select menus, file choosers, permission prompts, and secure desktops | OS or browser chrome may not appear in the captured surface and may be unrecordable or undrivable. Prefer an application-level or keyboard/API path. |
| New tabs and windows | A bundle recorded with `Recorder` can declare the popups and tabs a step opens, act in them after an explicit surface switch, and halt on any page it did not declare ([MULTI_SURFACE.md](MULTI_SURFACE.md)). Such bundles run governed, qualified, and durable. Resume re-binds surfaces by identity, and managed Cloud dispatch needs only the `surfaces:` map. A program (graph) resume is refused. `record --url` and attach mode (`--cdp`, only pages a followed page opened) capture popups and tabs, and `--surface NAME=KIND[:TARGET]` binds backend surfaces at replay. Native windows and JavaScript dialogs are screen states, not surfaces. A dialog is answered by the step that raises it (browser backend, scripted recorder), and a hidden native window is reached by an explicit focus step. |
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

The repository supplies integrity manifests, optional encryption at rest for
bundles, web recordings and the run directories of sealed bundles, destination
policy, local egress checks, and audit primitives. Encryption at rest does not
cover data in process memory, the target application's own storage, file names
and sizes, desktop recordings, or commands outside the list in
[`phi_at_rest.md`](phi_at_rest.md), which refuse sealed input; the key is a
passphrase in the environment, not a KMS. A content digest
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
