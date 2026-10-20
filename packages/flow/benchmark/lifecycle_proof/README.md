# One production configuration, its whole lifecycle

`run.py` runs the OpenEMR referral program on a local OpenEMR test copy,
using synthetic patients only. It runs one supported configuration through
admission, a batch, an interruption, an app change, a repair and continued
operation. Each phase writes its evidence to `--out`. A
[results](results/) directory holds a committed run.

## The configuration

| Part | Setting |
| --- | --- |
| Profile | Regulated |
| Authorization | Governed: the run gate on the certified, sealed bundle authorizes each run |
| Pin | The admitted content digest. A repaired version passes only through signed lineage. |
| Durable | Yes, with an encrypted checkpoint store |
| API tier | Where admissible. The referral's API step is refused by its own proposal on OpenEMR (other writes in the span, native validation), so the GUI save runs and the report says why. |
| Pipelined read-back | One window. Under a durable run, the deferred record check is held in the durable pause. |
| Repair | On. Every fix is re-qualified on the test copy and re-admitted with signed lineage. |
| Approval | `problems_only` |

This is the configuration the lifecycle soak (`benchmark/lifecycle`) runs at
scale.

## Phases

1. **admit**
   - Seal the certified referral bundle.
   - Qualify the sealed copy on this fixture: the six cases, certified under
     `clinical-write`.
   - Check the Regulated run gate.
   - Open the `problems_only` switch record.
2. **batch**: three referrals. Each item starts while the previous item's
   record check is still running.
3. **crash**
   - An item runs in a child process, which is killed (SIGKILL) while its
     record check runs.
   - The resume re-runs only the check.
   - The oracle counts one chart.
4. **drift**
   - The vendor relabels the save button ("Confirm Create New Patient"
     becomes "Save new patient").
   - The item stops before saving, and self-repair proposes a fix.
   - A person approves it once.
   - The fix is re-qualified on the test copy and re-admitted with lineage.
   - The item resumes and saves once.
5. **continue**: two more referrals on the repaired version. The pin to the
   original digest passes through the lineage.

The oracle reads the database directly and counts the charts for each
referral's email. The person is scripted (`ScriptedPerson`), and every touch
is in `ledger.jsonl`. The repair agent is a rule-based stand-in that calls no
model (`LabelAgent`). Pass `--repair-agent module:factory` to use another,
such as the Claude bridge agent in `benchmark/auto_repair`. The
`problems_only` switch status comes from a local stand-in for Cloud's signed
status route (`LocalStatusAuthority`); it signs "on" for the open switch.

## Reproduce

Before you start Docker, follow the shared-machine rules in
`~/oa/scratch/fixture-lease/README.md`: take a lease, keep at least 15 GiB
free, and keep the load under 40.

```bash
cd packages/flow
export PYTHONPATH=.:../types
python -m benchmark.lifecycle_proof.run up --out runs/proof --keys ~/oa/scratch/compose-proof/keys
python -m benchmark.lifecycle_proof.run all --out runs/proof --keys ~/oa/scratch/compose-proof/keys
```

`--keys` is a directory outside the repository. It holds the bundle key, the
checkpoint key, the local status key and the local admission key. Nothing
secret is written under `--out`.

## Limits

- **The test copy.** Re-qualification runs on the same fixture. Its data is
  dumped before and restored after, so the six cases' resets don't erase the
  batch's charts. A deployment would point verify.yaml at a separate copy.
- **The switch status.** It is signed locally. Cloud's status route and its
  switch thresholds are not part of this run.
