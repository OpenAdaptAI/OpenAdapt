# Check a write that something else made

Preview, aligned with RFC-0002 (Open Effect Receipt). The receipt format may
still change before the RFC is final.

An agent, a script, or a person says it saved a record. `verify-run` reads
the system of record and decides whether that is true. It uses the same
record checks a production `run` uses (`rest`, `sql`, `fhir`, `graphql`, and
the other kinds in [`EFFECT_KIT.md`](EFFECT_KIT.md)). It makes no AI model
calls.

```bash
# Before the agent starts: read the store so new rows can be told from old ones.
openadapt-flow verify-run --verify verify.yaml --params task.json \
  --capture-baseline before.json

# ... the agent does its work ...

openadapt-flow verify-run --verify verify.yaml --params task.json \
  --run-record agent-run.json --authorization-ref TICKET-1234 \
  --baseline before.json --out share/
```

`verify.yaml` has two parts, each in a shape Flow already reads:

```yaml
schema_version: openadapt.verify/v1
verifier:        # the same keys as the effects: section of deployment.yaml
  kind: rest
  base_url: http://localhost:8080
  records_path: /api/db
effects:         # the same list as effect_contract.yaml
  - kind: record_written
    match:
      patient_id: {param: patient_id}
      note: {param: note}
    expected_count: 1
    count_new_only: true
```

It is the same file `openadapt-flow qualify --verify` reads (see
[`QUALIFY_A_WORKFLOW.md`](QUALIFY_A_WORKFLOW.md)). verify-run ignores the
qualify-only sections (`app`, `save_step`, `inputs`, `campaign`, and
`verifier.session`), expands `record` to the same effect list qualify uses,
and expands `${NAME}` environment references the same way. So the receipt's
`workflow_digest` equals the `verify_config_sha256` qualify reports for the
same file. `max_execution_window_s` (default 3600) is the one key only
verify-run reads.

The three inputs come from different people on purpose:

- `--params` holds the values the task named, such as which patient. They
  come from whoever assigned the task, so the agent can't pick what it is
  checked against.
- `--run-record` is what the agent reports. It must name `executor_kind`
  (`agent`, `script`, `human`, `flow_replay`, or `unknown`). The receipt
  keeps a salted digest of the file and reads nothing else from it except
  `executor_id` and `executor_build`, which it also keeps only as digests.
- `--baseline` is the read taken before the agent started. Without it, a
  contract that counts new rows can't be judged, and the verdict is
  `INDETERMINATE`. A baseline works once: a second run with the same file
  is refused. One older than `max_execution_window_s` (default 3600) counts
  as no baseline.

To check the open record before the write, hold the write for a person, and
refuse a retry after an uncertain result, wrap the write in a guarded save
([`GUARDED_SAVE.md`](GUARDED_SAVE.md)). It reads this same file.

Optional flags:

- `--approver approver.json` names who approved the write, as
  `{kind, id, policy_ref}` with an opaque `id`. If the approver is the
  executor itself, the outcome is `rejected_policy` even when the write
  landed.
- `--remediates RECEIPT_ID` links this receipt to an earlier one it fixes.
- `--correlation-id ID` carries your own ticket or trace id.

Both link ids must be opaque (8 to 128 of `A-Za-z0-9._:-`). They are absent
unless given, and when present they are signed with the rest of the receipt.

## Verdicts

| What the store shows | `effect_verdict` | `outcome` |
|---|---|---|
| The declared record, exactly once, through a system-of-record read | `CONFIRMED` | `verified` |
| The same, but the executor approved its own write | `CONFIRMED` | `rejected_policy` |
| Nothing that matches | `REFUTED` | `halted_before_effect` |
| A duplicate, a wrong value, or only some of the effects | `REFUTED` | `reconciliation_required` |
| The store could not be read, no baseline, or a separate-session read | `INDETERMINATE` or `CONFIRMED` | `reconciliation_required` |

Only a read through the system of record produces `verified`. A screen
read-back (`onscreen`) is refused before anything runs.

`verification_tier` on the receipt says how the effects were checked
(RFC-0002, weakest effect wins):

- `independent_readback`: every effect was read from the system of record
  without the UI (Flow evidence tier 1).
- `screen_readback`: at least one effect was read through a UI session,
  including a separate signed-in session (Flow tier 2).
- `unchecked`: at least one effect was not read.

Only `independent_readback` can yield `verified`.

## The receipt

`share/effect-receipt.json` holds `receipt` and `evidence`. `receipt` has
every field of the Execute receipt (`openadapt.execute-evidence-receipt/v1`)
plus `executor_kind`, `executor_ref_sha256`, `effect_verdict`, and
`verification_tier` (and `remediates` and `correlation_id` when given),
under the schema `openadapt.effect-receipt/v1`. `evidence.readback` lists
each effect's contract hash, verdict, and counts. `receipt.evidence_digest`
is the SHA-256 of `evidence`. Neither part holds record values, URLs, or the
agent's own text.

Every digest whose input could hold customer data (the run record, the
authorization reference, the baseline, and each bound parameter inside an
effect's contract hash) is an HMAC-SHA256 keyed with a 256-bit salt drawn
for this run (`openadapt.run-salt/v1`). `evidence.salting.salt_commitment`
is the SHA-256 of the salt. The salt itself stays in a private file: the
baseline, or `share/effect-run.private.json` when there is no baseline. Each
REST read carries `X-OpenAdapt-Execution-Id` and a `traceparent` header so
the store's own logs can be matched to the run.

`evidence.authorization.approver` keeps a placeholder shape until OpenAdapt
types defines approver identity.

The file is unsigned. An issuer signs it with
`openadapt.effect-seal-signature/v1`, over its own domain, so a verifier
built for Execute Seals refuses an effect receipt instead of passing it.
Check a signed file offline against keys you pinned:

```bash
openadapt-flow verify-run --check signed.json --keys seal-keys.json
```

The check reports the signature (`verified`) and the outcome
(`verified_outcome`) separately. It exits 0 only when both hold. A genuine
signature over `halted_before_effect` still exits 1 unless you pass
`--accept-outcome halted_before_effect` (repeatable). An unsigned file is
never a pass.

The baseline and run files hold raw records or the salt. They are written
with mode 0600 and never go into the receipt. Delete them when you no longer
need them.
