# Getting a halt to a phone

When a governed run cannot confirm something it halts and asks a person. This
document is about the *delivery* of that question: which paths exist, how much
each may carry, what a customer has to do to use one, and which designs were
considered and not built.

This path resolves an operational halt. A declared business-policy choice is a
different contract. It uses a finite `business_decision` program state, an
authorized role, an expiring signed request and receipt, and fresh live-state
revalidation. See [`BUSINESS_DECISIONS.md`](BUSINESS_DECISIONS.md). Neither path
can replace entity-identity or business-effect proof.

The engine already grades **evidence** with `VerificationTier` — weaker evidence
is recorded as weaker rather than concealed. This applies the same idea to
**delivery**. A decision taken without seeing what broke is a different decision
from one taken with it, and the system should be able to say which it got.

## The ladder

| Tier | Carries | Reachable from a phone |
| --- | --- | --- |
| `local_full` (1) | The whole local `presentation`: protected screenshot crops, the gated control label, the full halt detail | Only over the customer's own network, or through an HTTPS ingress they operate |
| `remote_closed_context` (2) | The signed PHI-free task **plus** `RemoteHaltContextV1` — what broke, which resolution rungs were tried and what each returned, what a continue re-proves. Closed enums, bounded integers and booleans only. **No string field. No image.** | Yes, from anywhere |
| `remote_identifiers` (3) | The signed task alone: opaque ids, digests, counts, expiry | Yes, from anywhere |
| `notification_only` (4) | A count and a fixed sentence | Yes, from anywhere |

Lower is stronger, matching `VerificationTier`, so the two read alike.

Three independent ceilings apply to a remote projection and the weakest wins:
the deployment's `human_decisions.remote.context_tier`, the profile named in
`deployment.runtime.profile`, and the profile the run was **actually executed
under**, read from its report. The third matters because a governed dispatch
carries its own execution profile, which the local `deployment.yaml` need not
name. None of them can widen another. `local_full` is not reachable as a remote
tier under any configuration — `effective_remote_tier` refuses it by name.

Today every profile permits `remote_closed_context`, because that tier adds only
closed enums, bounded integers and booleans — it widens what a remote operator
*knows* without widening what the envelope can *represent*. So
`max_remote_decision_tier` is not yet doing discriminating work, and it is worth
being clear about that rather than implying otherwise. It becomes load-bearing
the moment a tier exists that some profile must refuse — which is exactly where
a scrubbed tier would sit.

Every projection records the tier it **actually delivered**, not the one it
intended. An engine that could not build a context reports
`remote_identifiers`, which is the same rule the effect ladder applies to
evidence.

## The key claim, and why it is checkable

`RemoteHaltContextV1` has **no string-valued field**. Every value is drawn from
a vocabulary the engine already owns — `Rung`, `ActionKind`, the console's halt
categories, the ARIA/UIA role names, `RecheckKind` — or is a bounded integer or
a boolean. A relay that stores this object is *structurally incapable* of
representing a patient name, an MRN, an observed value, a path, or a workflow
label.

That is the same kind of guarantee `HumanDecisionTaskV1` already gives, and it
is checkable the same way: by reading a schema, not by trusting a detector. The
hosted control plane enforces it a third time in Postgres, in the same style as
`human_decision_task_contract_valid`.

The one thing this tier gives up is `target_label` — the target control's own
accessible name, which `halt_detail._safe_target_label` releases locally after
six independent proofs. It stays local. The phone therefore says *"OpenAdapt
could not find the button"* rather than *"the button labelled 'Open'"*, and
`target_label_withheld` tells the client that a name exists which it is not
being shown, so it renders the role noun rather than implying the control was
anonymous.

**That is the whole cost: one adjective.**

## What a customer has to do

Two paths, and they are not alternatives so much as different postures.

### Hosted relay — nothing to configure on the network

The runner makes **outbound HTTPS requests only**. No inbound port, no port
forward, no certificate, no reverse proxy, no static address; it works behind
NAT and on an ordinary practice broadband line.

1. Pair the desktop app to the hosted control plane (already a one-click
   `openadapt://connect` flow) and enable remote decisions in
   `deployment.yaml`:

   ```yaml
   human_decisions:
     remote:
       enabled: true
       tenant_id: <from the control plane>
       runner_id: <from the control plane>
       # context_tier defaults to remote_closed_context
   ```

2. Run the attended console with the lane on. Desktop does this for the
   operator; the equivalent command by hand is:

   ```
   OPENADAPT_RUNNER_TOKEN=oar_...  openadapt-flow console \
       --attend --allow-actions --remote-decisions --config deployment.yaml
   ```

3. Staff open `app.openadapt.ai` on their phone and sign in. It is a web page;
   there is no app to install.

That is the whole list. Nobody terminates TLS, because the only TLS involved is
the runner's outbound connection to a public host with an ordinary public
certificate.

### What actually runs, once step 2 is on

`console.decision_supervisor.DecisionSupervisor` owns the whole `runs` root
rather than one run, which is what makes the lane automatic instead of a library
somebody has to call:

- Every cycle it publishes **every** currently open attended pause, so a halt
  becomes answerable on a phone without anyone doing anything.
- When an answer comes back it re-scans and requires the relayed `task_id` *and*
  `capability_digest` to match a pause that is open **right now**. A decision
  that matches none is acknowledged `stale` and never executed. This matters the
  moment two runs are halted at once, which is the ordinary case: revalidation
  would catch a *wrong* answer, but not a *correct* answer applied to the wrong
  run.
- A relay whose deadline has passed, or whose deadline cannot be parsed, is
  acknowledged `expired`. An unreadable deadline is not a licence to act.
- It runs as a daemon thread inside the attended console, because that process
  already owns the deployment-bound action service, and because
  `execute_attended_action` takes a single-flight lease over the pause — so an
  answer from the phone and one from the local browser cannot both execute.

Every refusal at start-up is a hard exit, never a disabled feature. An operator
who passed `--remote-decisions` and silently got a loopback-only console would
believe a phone can answer a halt while nothing is listening for one. Missing
runner token, remote issuance not enabled, a read-only console, or a plaintext
control-plane origin each stop the console rather than degrade it.

### Runner-local portal — full fidelity, on the practice's own terms

`openadapt-desktop`'s decision portal serves `local_full`, including protected
screenshot crops. It is loopback-only by default and publishing it to a phone
requires an HTTPS origin the customer operates (`engine/portal/ingress.py`,
`customer_ingress` mode). That is correct for an organisation with an IT
department and wrong for a dental practice — which is precisely why the hosted
relay exists.

## Options considered and not built

### PHI scrubbing the presentation, then sending it over the hosted lane

**Rejected as a delivery path.** Not on principle — on the evidence.

It replaces a *structural* guarantee ("the envelope cannot represent protected
content", checkable by reading a schema) with a *statistical* one ("a
recall-limited detector caught everything"), into a multi-tenant service, where
a single miss is a breach. That substitution is the exact move this engine
exists to argue against.

The specific state of `openadapt-privacy` makes this concrete rather than
philosophical. Its own README says: *"Scrubbing is one control in a reviewed
egress process, not a guarantee that an artifact is free of protected data"*,
and its recall gate is 24 synthetic text identifiers with no held-out or
adversarial corpus. Its custom MRN and member-ID recognisers are anchored on
literal label prefixes (`MRN: `, `member ID `), so an identifier sitting in a
grid cell — the normal case in an EMR — is not detected. Image redaction is
routed to Presidio's OCR redactor, whose OCR engine is not pinned or documented
here, which has no confidence floor and no unreadable-region fallback, and which
**no test in that repository exercises on a real image**.

There is also a second cost that has nothing to do with recall: **scrubbing
frequently destroys the decision it was meant to deliver.** Many halts turn on
the identifiers themselves.

And there is a third point, which is the one that actually decides it. Read the
questions the engine asks:

| Category | The question the operator is asked |
| --- | --- |
| `identity` | Does the **live application** show the intended record? |
| `disambiguation`, `resolution` | Can you prepare one unambiguous target in the **live application**? |
| `human_required` | Have you completed the required human step in the **live application**? |
| `effect_*`, `placeholder_effect` | Is the destination record / verifier ready for OpenAdapt to check again? |
| `delivery_uncertain` | Is the live destination ready for OpenAdapt to reconcile before any retry? |
| `unmet_guard`, `postcondition`, `halt`, `operator_review` | Is the **live application** ready for OpenAdapt to verify and continue? |

Every one of them is answered by looking at the live application, and every
answer is then independently re-proved by the engine before anything continues —
`will_recheck` is exactly that list. The relayed screenshot is **triage
context**, not the source of the answer: it tells a person whether to walk over
now. So the value of putting a redacted frame into a multi-tenant service is
"the operator is slightly better briefed before walking to the machine", and the
cost is a statistical PHI guarantee. That trade is not worth taking, and the
closed context buys most of the briefing at no such cost.

*This is an analysis of the question text and the required action, verifiable by
reading `human_decisions._TASK_COPY` and `halt_detail._will_recheck`. It is not
field evidence about operator behaviour, and it should be revisited once real
pilots produce some.*

**Where scrubbing does belong**, and where it is already used: the notification
tier ("a decision needs review — open OpenAdapt"), the telemetry and audit path
where shape-not-content is genuinely sufficient, and as one of the six gates
`halt_detail._safe_target_label` applies before releasing a control label to the
**local** surface. All three are uses where a miss is contained. None of them
put a detector's output into a third party's database.

If a scrubbed tier is ever built it belongs between tiers 2 and 3, `regulated`
must refuse it, and it must not ship before image redaction has a measured
recall number on a held-out corpus. It is deliberately **not** defined in
`DecisionDeliveryTier` today, because defining it would imply it is available.

### LAN-only delivery

**Rejected for this customer, retained for the customer who has an IT
department.** The phone is usually on the practice's own wifi, so a
runner-served page needs no cloud at all — and the portal already does exactly
this.

What kills it as the dental answer is not the ingress config, it is the browser
security model. On plain `http://192.168.x.x` a page is not a secure context:
no service worker, no `crypto.subtle`, no installable PWA. So the LAN-plaintext
variant is simultaneously the *least* private option (protected screenshot crops
and a session bearer token in the clear on a shared-key wifi, which
`engine/portal/ingress.py` refuses by design) and the one that forecloses any
future end-to-end encryption.

The self-signed variant is worse in a way that is easy to miss: it does not just
produce a hostile interstitial, it **trains clinical staff to click through TLS
warnings**, and that click is indistinguishable from the one a real
man-in-the-middle would produce.

### Notification-only

**Rejected as the primary path, kept as tier 4.** "Something needs you, open the
desktop" is cheap and it is honest, but it converts the phone from a decision
surface into a pager. The practical failure is that the person cannot tell
whether to interrupt what they are doing — which is most of the value of putting
this on a phone at all. It stays in the ladder because it is the correct
fallback when a deployment caps context, and because it is what desktop's OS
notifications already are by construction.

## Written down, not built: an end-to-end encrypted tier for the frame

The closed context deliberately carries no pixels. If a screenshot crop is
eventually wanted on a phone from outside the practice's network, the right
shape is a **blind relay**, and the point of it is that it does not trade the
PHI property away — it makes "the hosted service cannot represent protected
content" true *by construction* (it holds ciphertext) rather than by omission
(it holds nothing).

Sketch, for the record:

- **Pairing.** Desktop shows a QR; the phone generates a keypair in-browser and
  the private key never leaves the device (non-extractable `CryptoKey` in
  IndexedDB). The QR carries a one-use pairing secret in the URL *fragment*, so
  it cannot land in a proxy log — `engine/portal/pairing.py` already implements
  this shape, including single-claim atomicity, five-minute expiry checked
  against both monotonic and wall clocks, and a confirmation code minted at
  claim time so a remote attacker's phone shows a code the operator cannot see.
  That module is the thing to reuse, not to redesign.
- **Sealing.** The runner encrypts the frame to the public keys of every
  currently approved device. Multiple approvers therefore means multiple
  recipients on one envelope, not a shared key.
- **The relay.** `DecisionRelay` already dials out and already carries an
  opaque, size-bounded body. The ciphertext rides beside the projection; the
  service stores and forwards it without a key.
- **Device loss and rotation.** Revoking a device removes it as a recipient of
  *future* envelopes. It cannot retroactively unseal an old one, so envelopes
  need a short server-side TTL, and rotation means re-pairing rather than
  re-keying — there is no key escrow, deliberately.
- **The receipt when the relay cannot read what it carried.** The relay can
  attest ciphertext digest, envelope size, recipient key ids, and timestamps.
  It cannot attest content. So the audit record must say exactly that: *this
  envelope, to these device keys, at this time* — and the decision receipt stays
  what it is today, a closed `(state, reason_code)` pair bound to the pause the
  engine re-verified. A relay that cannot read a payload must not be described
  as having confirmed anything about it.

The reason this is written down rather than built: the transport it needs now
exists, the pairing it needs already exists in desktop, and the analysis above
says the frame is triage context rather than the source of any answer. Building
the crypto before a pilot has told us the closed context is insufficient would
be building the expensive half of the answer first.

## What is not done yet

Stated plainly, because a half-wired lane that reads as finished is worse than
an honest gap.

- **Desktop does not pass `--remote-decisions` yet.** `engine/portal/service.py`
  spawns `openadapt-flow console --attend --allow-actions`; adding the flag is a
  small change, but the installer bundles a pinned frozen Flow, so the pin has
  to move to a release containing the flag first. Until then the lane is
  available from the CLI and not from the Desktop toggle.
- **The hosted side must be deployed.** The control plane has to accept the two
  new projection fields before the lane carries context; without that it still
  publishes and answers, at `remote_identifiers`.
- **`max_remote_decision_tier` is not yet discriminating** (see above). It
  becomes load-bearing when a tier exists that some profile must refuse.

## What is deliberately not claimed

`DecisionRelay` never says **delivered**. A successful POST proves the control
plane accepted a task; it does not prove a person received it, opened it, or
read it. The runner cannot observe that, so the vocabulary does not contain the
word. `publish` returns `published`, `already_published`, or `unknown`, and
`unknown` is never retried into a claim — the same discipline the engine applies
to an action whose delivery it could not confirm.
