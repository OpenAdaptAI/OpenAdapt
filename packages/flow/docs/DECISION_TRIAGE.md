# Save triage

Save triage orders a list's verified saves for review. It puts the saves that
look clean in one batch that a person approves with one action, and sends
the rest to the per-item queue with the reasons they were flagged. It's in
`openadapt_flow/decision/` and the `openadapt-flow triage` command.

Triage doesn't approve, save, resume or certify anything. The engine has
already made and checked each save by the time triage sees it. A review
records whether a person agrees, as a label.

## When it applies

| Approval mode | What triage does |
| --- | --- |
| `each_item` | Nothing. Every save goes to the per-item queue in the order given, as before. |
| `not_gated` | Nothing, as above. |
| `problems_only` | Applies the policy below. |

Desktop's list queue and spot checks for `problems_only` aren't built yet
(see [`APPROVAL_MODES.md`](APPROVAL_MODES.md)). This page describes the
engine part they would call.

## Where each save goes

- **Stopped.** The record check refuted the save or couldn't read it, or the
  run didn't end `VERIFIED`. The engine already halted. Triage lists the
  save and never scores it; no rule, score or batch moves it.
- **Review each.** Flagged saves, with a reason code and plain text for each
  failed check, and the field or parameter name (never its value). A random
  sample of clean saves, 10% by default, is mixed in at random positions.
- **Batch.** The other clean saves, shown as "Approve N clean items" with the
  list. The person can pull any save back to the per-item queue before
  approving the rest.

## Policies

`rule_v0` is the default under `problems_only` and needs no labels. A save is
clean only when all of these hold:

- the record check confirmed the save on the first read, with no
  correction, and every read was of the system of record without the UI
  (REST, SQL, FHIR, GraphQL or a file export; Flow tier 1). A UI read,
  even in a separate session, or a missing read-back is never clean;
- every field it compared matched what the item entered (names, dates and
  record numbers are normalised first, so `07/14/1961` matches
  `1961-07-14`), and no entered field went unread;
- the run stayed on its recorded path (no repair, model call or rescue);
- the workflow has at least 20 earlier approved items, each number is
  within their range and 3 standard deviations of their mean, and each
  choice is one they used;
- every value read from a document (`intake`) and not confirmed by a person
  has confidence 0.9 or higher;
- recent declines are rare: at most one, and no more than 5% of the
  workflow's recent items. One slip among 20 or more items doesn't keep a
  steady workflow per item; a second decline, or one among fewer than 20
  items, does. `max_recent_declines=0, max_recent_decline_rate=0` restores
  the old "any decline" rule.

`learned` scores each save with a logistic regression over the same
features and opts in per workflow. It needs a calibration artifact for the
save's group (one workflow's save step), pinned by digest. It never clears a
save that failed one of the first three checks above. If the group has fewer
than 200 labels, or too few bad ones for the bound below, every save goes to
the per-item queue. That fallback stays `per_item` on purpose: choosing
`learned` asks for a measured miss rate, and an uncalibrated group has none.
`learned_fallback="rule_v0"` uses the v0 rule instead, for a team that
accepts an unmeasured rule on that workflow.

`off` sends every save to the per-item queue.

## Calibration

`openadapt-flow triage calibrate` fits one group's artifact from its label
log:

- The model is fitted out of fold, and its scores are calibrated with Platt
  scaling, or isotonic regression from 1,000 labels.
- `t_low` is the largest threshold at which the share of bad saves
  (`wrong_values` or `wrong_record`) scored below it is at most 5% with 90%
  confidence. It's a one-sided binomial bound, tested from the strictest
  threshold up. With zero misses, the bound needs 45 bad labels at 5% and
  460 at 0.5%. A group below that count isn't calibrated.
- Saves at or above `t_high` (0.5) are flagged. Saves between the two
  thresholds go to the per-item queue as uncertain.
- The artifact is sealed with a SHA-256 digest. Triage refuses an artifact
  whose digest isn't the pinned one.

The bound holds only while new saves look like the labelled ones. A new
application version, clinic or form breaks it. The fit uses the most recent
2,000 labels; refit on a schedule and after any such change.

## Labels and the audit sample

Each review appends one record to a label log (JSON lines, mode 0600). The
record holds the decision, the decline reason, the features, the queue the
save was in, and the chance it was reviewed on its own. It holds no value,
screenshot or record key. `openadapt_flow.decision.labels.to_judgment_case`
converts a record to a `JudgmentCaseV1` human-node case.

Labels are a person's decisions only. `ApprovalLabelV1.approver_identity()`
returns the `openadapt.approver-identity/v1` person behind a label. An
agent, rule or system decision on a save is recorded on the run report and
never written here.

Only the audit sample sees clean saves one at a time. Each bad save it finds
is weighted by the inverse of the audit rate in
`openadapt-flow triage estimate`, which estimates the share of bad saves
that reached a batch.

## What a provider sees

A provider gets a fixed list of 22 numbers and booleans, such as
`identity_fields_match` or `intake_min_confidence`. Values are compared on
this computer first. A request that carries a string, an unknown name or a
non-finite number can't be built. The only provider is the local logistic
regression; the protocol leaves room for others, and none exist.

## Commands

```bash
openadapt-flow triage plan --observations saves.jsonl --mode problems_only --out plan.json
openadapt-flow triage approve-batch plan.json --batch-sha256 SHA256 \
  --pull-back ITEM_ID --labels labels.jsonl --decided-by human --unauthenticated
openadapt-flow triage decide plan.json --item ITEM_ID --decline wrong_values \
  --labels labels.jsonl --decided-by human --approver-id USER --auth-method cloud_aal2
openadapt-flow triage calibrate --labels labels.jsonl --group GROUP_KEY --out artifact.json
openadapt-flow triage estimate --labels labels.jsonl
openadapt-flow triage eligibility --workflow WF --artifact artifact.json \
  --artifact-sha256 SHA256 --labels labels.jsonl
```

`plan` also takes `--run-dir` for finished runs. The report keeps each field
the record check compared (`effect_field_checks`), so a plan built from runs
compares fields the same way.

`eligibility` says whether a workflow has earned "only ask a person about
problems": an eligible, pinned calibration artifact, and audited clean saves
whose exact one-sided (Clopper-Pearson) miss-rate bound is under the target.
It exits 0 when eligible, 3 when not yet, and 2 with a reason when there's
no evidence. It never approves a save.

## Synthetic evaluation

`scripts/evaluate_decision_triage.py` generates a fake data-entry workflow
with injected faults, some the record check catches and some it can't (a
wrong value in a field it doesn't read, or a wrong value on the source
document). With seed 7, 8,000 training saves and 5,000 test saves in lists
of 50 ([`decision_triage_synthetic.json`](validation/decision_triage_synthetic.json)):

| Policy | Reviews per verified save | Bad saves in a batch | Share of bad saves missed |
| --- | --- | --- | --- |
| Per item (`each_item`) | 1.00 | 0 of 78 | 0% |
| `rule_v0` | 0.55 | 12 of 78 | 15% |
| `learned`, 5% bound | 1.00 | 0 of 78 | 0% |

A batch approval counts as one review. On this data, `rule_v0` puts 0.53%
bad saves in its batches, just over the 0.5% target. The learned bound is
too strict to batch more than 11 saves, because about a third of the bad
saves that pass the record check have no feature that separates them from
good saves. The best learned threshold under a 0.5% batch error, picked on
the test set, reaches 0.90 reviews per save. The audit estimated a 32% miss share against
a true 15%, from 238 audited saves.

The data and fault rates are invented. These numbers show how the parts
behave together; they say nothing about a real workflow.
