# Identity repair with person approval

Status: proposal. Internal. Nothing here is built.

## Problem

When a run can't confirm that it's acting on the right record, it stops before
saving, and automatic repair doesn't run. `_repairable` in
`openadapt_flow/repair/self_repair.py` refuses any halt whose identity status
isn't `verified` ("a wrong-record halt is never repaired"). That rule is
correct: a failed identity check might mean the wrong patient.

It also means that a harmless app change can stop every item. In the OpenEMR
lifecycle proof (draft PR OpenAdaptAI/OpenAdapt#1463), relabeling the save
button made the save step's identity check fail. Items r5, r7 and r8 all
stopped before saving, and self-repair returned `not_repairable` each time.
Nothing wrong was saved, but nothing was saved at all until a person fixed the
bundle.

This design lets a model propose the fix and lets re-qualification test it,
while a person decides whether the record is the right one. The model never
makes that decision.

## 1. Trigger

The path starts only when all of these are true:

- The run ended `HALTED_BEFORE_EFFECT` and `route_stop`
  (`repair/auto_repair.py`) returned `repair`. A run that may have written
  goes to reconciliation as it does today.
- The halted step's identity status is `mismatch`, `unreadable` or `abstain`.
- No input reached a consequential step and no effect was observed. These are
  the same checks `_repairable` already makes before it looks at identity.
- No person declined the item.

Healthy runs don't change. The agent is never called, and the run records
`status="not_needed"` with zero model calls and zero cost.

## 2. Proposal

Reuse the self-repair path. Don't add a second one.

- `SelfRepair.run(bundle_dir, run_dir)` in `repair/self_repair.py` drives the
  halt, propose, patch, probe and campaign steps.
- The agent implements `RepairAgent.propose(RepairRequest) -> RepairProposal`.
  `RepairRequest` already carries the scrubbed live screen, the recorded crop
  and label, and the masked `record_check`.
- `AutoRepair` and `RepairLedger` (`repair/auto_repair.py`) route the stop and
  write one ledger row per stop. The CLI entry points are
  `cmd_run_self_repair` and `resolve_auto_repair` in `cli_self_repair.py`.

The change is a new identity mode for `_repairable`. It's off by default and
turned on in deploy.yaml under `repair`. In that mode an identity halt becomes
repairable, but the result can't resume without the person decision in
section 4. The agent gets one new piece of information: which identity fields
failed (field names only, never values). It's never asked whether the record
is right.

The repair may change where a step finds its target or reads its identity. It
may not change which fields must match. `fixed_contract_refusal` in
`repair/readmission.py` already refuses a change to `record_check_digest`,
`identity_armed`, `required_identity_step_ids` or `identity_contract_sha256`.

## 3. Verification

The proposed bundle goes through the checks that exist today, in order:

1. The self-repair probe replays the patched bundle in a fresh session and
   stops before the first save. It passes only if the deterministic ladder
   found the target, every identity check verified, and the form matches.
2. The campaigns in `RepairStore` (`repair/lifecycle.py`) run, including the
   operator's fault probes. Each fault probe must stop before the save.
3. `requalify(parent_bundle, candidate_bundle, out_dir, Requalification)` in
   `repair/readmission.py` runs the parent's qualification campaign on a test
   copy of the app (the store copy), with the parent's cases and policy.
   `admit` then writes the signed lineage that lets a `--pin-digest` run
   accept the new version.

A failure at any step ends the attempt. The candidate isn't staged or shown to
anyone as approvable, and the item stays stopped for a person, as it does
today. The ledger row records why.

One known risk needs its own fault probe. A model could make the check pass by
reading identity from a place where the expected values appear but that isn't
the record, such as a search box that holds the typed name. Re-qualification
must include a case where the open record is a different patient whose name is
typed on the screen, and the repaired check must stop on it.

## 4. Person decision

After verification passes, one request goes to a person. The card shows:

- From the source document: name, date of birth, MRN.
- From the screen: the same three fields as read from the record header, plus
  a screenshot of the header. The screenshot comes from the run, not from the
  region the repaired check reads, so a bad read can't hide a mismatch.
- Three actions: **Approve**, **Reject** and **Open** (the full screen and
  the repair diff).

The answer goes through the existing path: `decide_save_approval` in
`console/save_approvals.py` with the task digest and signature, recorded by
`ItemApprovalStore` and `RepairStore` under the person's name. The resumed run
is a new governed run under `run_authorization.py`, bound to the approved
bundle digest. The repair and the decision are recorded in the run report's
`self_repair` block (`ir.SelfRepairEvidence`). Carrying the identity decision
(both sets of fields as salted hashes, the approver, the time) into the Seal
is new work; see phase 3.
`SelfRepair` already refuses an answer whose approver isn't a person, and the
agent can't review its own patch (`_refuse_self_review`).

Policy:

- **Regulated:** identity repairs always need a person. Not configurable.
- **Standard:** the default is also a person. A deployment can set a policy
  for low-risk steps, but it can't skip the person when the check failed with
  `mismatch`.

Showing names, dates of birth and MRNs on a phone is a privacy decision. The
card must be `no-store`, values must never appear in the notification text,
and a deployment must be able to turn the phone off and require Desktop.

## 5. Mobile surface

| Option | What it costs | Works offline | Notes |
| --- | --- | --- | --- |
| Push from Desktop or the tray | Small. Desktop already notifies. | No | A notification can't safely show PHI, so it can only link elsewhere. |
| Phone-optimized web page | Small. The decision portal and the swipe cards (openadapt-desktop#212) exist. Add an identity card. | No | Same pairing, digest and single-use rules as save approvals. |
| Native app | Large. App store review, signing, push keys, a second client to secure. | Partly | No decision here needs it. |

Recommendation: a phone web page for the decision, with a Desktop or tray
notification that says only "1 identity check waiting" and links to it. Don't
build a native app. The card should reuse the `SaveField` presentation fields
from branch `flow/save-approval-fields`, which isn't merged yet.

## 6. Engagement without rubber-stamping

A person who approves the same card many times a day will stop looking. This
is automation bias. Rewarding speed or volume makes it worse, because it pays
people to approve without reading, and an identity error is the one mistake
this product can't undo. So the game rewards catching errors, and only that.

- **Planted mismatches.** Some cards are known-wrong pairs built from test
  records: a matching name with a different date of birth, two transposed MRN
  digits, a similar name. Approving a plant does nothing, and the person is
  told at once. The catch rate per reviewer and per period, with the number of
  plants, is written to the Seal as evidence that review was real.
- **Streaks count catches, not approvals.** Approving fast earns nothing.
- **Lighter review is earned.** A reviewer with a high catch rate on enough
  plants can answer low-risk Standard items with one tap. Everyone else
  confirms by typing the last digits of the MRN. Under Regulated, review never
  gets lighter. A missed plant resets the level.
- **Team view.** Time saved by repairs and the count of wrong saves, which
  must stay at zero. No leaderboard for speed or volume.

Plants must be unpredictable in timing and look like real cards. The rate
needs to be high enough to measure (for example, 1 in 20) but low enough not
to waste time.

## 7. Open questions

- Does `identity_contract_sha256` cover where identity is read, or only which
  fields must match? If it covers the location, every identity repair needs a
  new admission, and this design shrinks to a faster way to ask for one.
- Should a `mismatch` halt ever enter this path, or only `unreadable` and
  `abstain`? A mismatch is the case most likely to be a real wrong record.
- What catch rate, over how many plants, earns lighter review? We need data
  before we pick a number.
- Who may see PHI on a phone, and does a deployment need a site-hosted relay?
- Does one approval release a whole worklist group (as `WorklistQueue` does
  for standard changes), or one item at a time? For identity, the safe default
  is one item at a time.

## Phased plan

1. **Measure.** Count identity stops by cause on the lifecycle proof and the
   stop-causes benchmark. No code changes.
2. **Engine.** Add the identity mode to `_repairable`, the person-required
   rule, and the "typed name on screen" fault probe. Run it on the MockMed
   fixture, then on the OpenEMR test copy.
3. **Card.** Add the identity card to the phone portal, with Approve, Reject
   and Open, and record the decision in the Seal.
4. **Plants.** Add planted mismatches and the catch rate in the Seal. Enable
   lighter review only after we have catch-rate data.
5. **Team view.** Show time saved and wrong saves.
