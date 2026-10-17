# Check a write that something else made

Prototype. The command and the receipt format may change.

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

The three inputs come from different people on purpose:

- `--params` holds the values the task named, such as which patient. They
  come from whoever assigned the task, so the agent can't pick what it is
  checked against.
- `--run-record` is what the agent reports. It must name `executor_kind`
  (`agent`, `script`, `human`, `flow_replay`, or `unknown`). The receipt
  keeps a SHA-256 digest of the file and reads nothing else from it.
- `--baseline` is the read taken before the agent started. Without it, a
  contract that counts new rows can't be judged, and the verdict is
  `INDETERMINATE`.

## Verdicts

| What the store shows | `effect_verdict` | `outcome` |
|---|---|---|
| The declared record, exactly once, through a system-of-record read | `CONFIRMED` | `verified` |
| Nothing that matches | `REFUTED` | `halted_before_effect` |
| A duplicate, a wrong value, or only some of the effects | `REFUTED` | `reconciliation_required` |
| The store could not be read, no baseline, or a separate-session read | `INDETERMINATE` or `CONFIRMED` | `reconciliation_required` |

Only a read through the system of record produces `verified`. A screen
read-back (`onscreen`) is refused before anything runs.

## The receipt

`share/effect-receipt.json` holds `receipt` and `evidence`. `receipt` has
every field of the Execute receipt (`openadapt.execute-evidence-receipt/v1`)
plus `executor_kind`, `executor_ref_sha256`, and `effect_verdict`, under the
schema `openadapt.effect-receipt/v1`. `evidence.readback` lists each effect's
contract hash, verdict, and counts; `receipt.evidence_digest` is its
SHA-256. Neither part holds record values, URLs, or the agent's own text.

The file is unsigned. An issuer adds `seal_signature` the same way OpenAdapt
Cloud signs Execute receipts. Check a signed file offline against keys you
pinned:

```bash
openadapt-flow verify-run --check signed.json --keys seal-keys.json
```

`openadapt seal verify` also checks the signature, but it does not yet show
`executor_kind`. Read the receipt before you rely on it.

The baseline file holds raw records. It is written with mode 0600 and never
goes into the receipt. Delete it when you no longer need it.
