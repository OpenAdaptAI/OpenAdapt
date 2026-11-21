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

### Read-back: every field gets a verdict

The run keeps a ledger of every field it typed, selected or kept: automatic
fills, operator values, kept prefills, fields fixed on screen, popup inputs
and popup returns. After submit, the judge gives every ledger entry and every
bound fact one of three verdicts:

- `passed`: the summary control shows the intended value under the field's
  codec.
- `failed`: the summary shows a different value, or a value the layer cannot
  attribute to any expected fact.
- `not_checked`: the layer cannot read the field back. There is no read-back
  declared for it, its summary control is missing or ambiguous, the summary
  lookup failed, or it was entered only in a popup the record does not show.

A `not_checked` field is never counted as passed. The outcome is:

| Outcome | When | Receipt |
| --- | --- | --- |
| `verified_done` | every verdict `passed`, required outputs present, summary readable | signed |
| `readback_refuted` | any `failed` verdict, a missing required output, no contract, or an unreadable summary screen | none |
| `readback_incomplete` | nothing failed, but at least one field `not_checked` | none |

`readback_incomplete` means the quote was submitted and a person must check
the listed fields. The receipt and verdict models refuse a pass with an
unchecked field, and a verified receipt records `readback_not_checked_count`
as 0.

Inputs typed into a popup (for example a replacement-cost estimator) are
re-read from their controls just before the popup's apply button. A mismatch
stops the run before apply. A match does not count as a read-back of the
record: unless the summary shows the field, it stays `not_checked`.

The summary must match its declared landmark when one is configured. This is
the configured browser read-back only. It does not prove server-side
isolation, the absence of changes in other records, or atomic rollback of a
multi-page form.

### What the browser surface cannot do yet

Measured on four held-out synthetic portals
([RESULTS](../benchmark/form_tx_heldout_run/RESULTS.md)):

- It reads only the top document: a form inside an iframe shows no controls,
  and controls inside shadow DOM are invisible.
- It reads values back only from form controls (`input`, `select`,
  `textarea`, `output`, `[data-readonly-output]`). A summary or confirmation
  page that shows values as plain text cannot be read, so every field ends
  `not_checked`.
- A popup routine has one apply button; an estimator that needs Calculate and
  then Use This Value stops for a person.
- A type-ahead that fetches suggestions only after typing cannot be filled:
  the surface opens a listbox by clicking, finds none, and the run raises an
  error instead of stopping.

### Sign-in walls and radio groups

- A screen is a sign-in wall when it shows a configured auth-wall element, an
  editable password or secret field, lacks the configured signed-in landmark,
  or shows an error banner or dialog whose text matches
  `UIProfileSpec.session_expired_text_patterns` (English defaults such as
  "session has expired", "signed out", "please sign in again"). This check
  runs before any error is read as a validation failure, so an expired
  session stops as `auth_required`, and the operator can sign in and mark it
  fixed on screen. If the wall appears after the submit request went out, the
  outcome is `delivery_uncertain` and the run cannot resume, because the
  record may already exist.
- Native radios that share a `name` are one field. An unchecked radio reads
  as empty, its own value is in `radio_value`, and `options` lists the group.
  An anchor that matches several radios of one group resolves to the checked
  radio, or to the first when none is checked. A required group with no
  intake value stops once, not once per radio.

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

Rule proposals are a prototype API with no command and no persistence. On the
held-out portals they had no measured effect: rules were proposed after two
consistent operator answers, but none passed qualification, which replays a
dev case and needs `verified_done`. Do not describe them as learning from
stops.

Rule proposals need distinct run identities and the same pack digest. Repeated
submissions from one run cannot increase support. Admission requires an explicit
approval reference and a qualification check; consensus alone does not establish
that a mapping or policy is safe.

## Evidence scope

`benchmark/form_tx_heldout_run` measures the layer on four synthetic carrier
quote portals written by a separate agent that did not read this package,
with scoring pre-registered before any run
([PREREGISTRATION](../benchmark/form_tx_heldout_run/PREREGISTRATION.md),
[RESULTS](../benchmark/form_tx_heldout_run/RESULTS.md)). The packs were
written by the layer's author from one dev case per portal, after reading the
portal source. No real carrier, customer or record was used.

`benchmark/form_tx_portals` is an older synthetic fixture designed alongside
the layer. Its first result checked only some of the fields entered and is
invalidated. With the complete read-back, its seven underwriting cases end
`readback_incomplete`, because the estimator inputs are never shown on its
summary.
