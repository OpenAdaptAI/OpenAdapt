# Form transaction prototype

`openadapt_flow.form_tx` is an experimental browser form reconciler. Its
Playwright backend observes and fills stateful forms, detects declared
prefill conflicts, and pauses for operator decisions. The backend protocol
allows other implementations; this package does not include a native desktop,
RDP, or Citrix form backend.

The reconciler makes no model calls. The browser still sends data to the
application it operates. Local execution does not by itself establish a
customer's data-handling policy or authorize a terminal action.

## Execution and verification

`InputDossier` carries source references and confidence values. Those are
caller assertions; the engine does not independently authenticate their source.
A required control needs a grounded fact or an explicit operator resolution,
even when the application has already filled it in. Unknown confirmation flags
never grant permission.

`FormPack` describes field bindings, screen anchors, popup routines, and a
summary contract. An exact CSS selector cannot bypass the anchor's other
semantic constraints. A human resolution must still refer to the same live
field, entity row, or modal. If the target changes while a decision is pending,
the engine refuses the decision and requires a new observation.

A verified completion requires a nonempty summary contract covering the
grounded bound facts and popup return values. Missing or partial read-back
contracts refuse completion and produce no verified receipt. The summary must
match its declared landmark, when one is configured. This establishes the
configured browser read-back only. It does not prove server-side isolation,
absence of changes in other records, or atomic rollback of a multi-page form.

`SignedFormTxReceipt` signs the local runner's statement. Its signature proves
integrity under that local key. It is not a server attestation, external witness,
or independent proof that every backend effect has been observed.

## Sessions, queue jobs, and operator decisions

`WarmSessionLease` requires positive authenticated-landmark evidence. A 200
response to a keepalive is insufficient; the lease checks the authenticated
screen afterward. Its request counter observes mutating HTTP methods. It cannot
prove that a server's GET handler has no side effects.

`FormTxQueueRunner` persists an uncertain claim before calling an executor.
Workers sharing a ledger directory cannot execute the same request concurrently.
An interrupted attempt stays held until its record is reconciled; automatic
retry is refused. A runner without a ledger directory has only process-local
state. The ledger contains local execution results and must be treated as
sensitive data, with the same retention policy as the operated records.

The Chrome side panel uses a local native host. The runtime adapter publishes
current halt state and consumes decisions bound to that run and state revision.
It does not authenticate an operator merely because a message supplies an
`actor_id`. The deployment must establish the operator identity and trusted
local process boundary.

## Pack compilation and rule proposals

`induce_form_pack` drafts bindings from structural snapshots. It does not infer
permission to submit, bind, issue, or otherwise finalize an operation from a
button label. A reviewed terminal-button pattern must be supplied explicitly.
A complete summary mapping is also required for its legacy `certified_ready`
field. That field describes static contract completeness, not live qualification
or production admission. Before use, validate the draft on an authorized
instance with planted faults and independently checked outcomes.

Rule proposals need distinct run identities and the same pack digest. Repeated
submissions from one run cannot increase support. Admission requires an explicit
approval reference and a qualification check; consensus alone does not establish
that a mapping or policy is safe.

## Evidence scope

`benchmark/form_tx_portals` serves purpose-built synthetic HTML and HTTP routes
inside headless Chromium. Its comparison arms are scripted policies, not
frontier browser agents. No real carrier, hospital system, or customer record
was used. The second portal was designed for this implementation and is not an
independently held-out application.

The original nine-completion result checked a subset of the fields entered.
Enforcing full bound-field coverage caused all nine cases to refuse. The
synthetic summaries and contracts were then expanded to expose those omitted
fields. Results from that correction must be reported separately from the
original measurement. Neither result establishes production reliability,
customer time savings, autonomous quoting, or hospital onboarding time.
