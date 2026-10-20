# One production configuration, its whole lifecycle

`run.py` runs the OpenEMR referral program on a local OpenEMR test copy,
using synthetic patients only. It runs one supported configuration through
admission, a batch, an interruption, an app change and continued operation.
Each phase writes its evidence to `--out`. The [results](results/) directory
holds a committed run (proof-15); see [Results](#results).

## The configuration

| Part | Setting |
| --- | --- |
| Profile | Regulated |
| Authorization | Governed: the run gate on the certified, encrypted bundle authorizes each run |
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
   - Encrypt the certified referral bundle file by file. A whole sealed set
     can't hold the approval gate's state yet (see Limits).
   - Qualify the encrypted copy on this fixture: the six cases, certified
     under `clinical-write`.
   - Check the Regulated run gate.
2. **switch**: open the `problems_only` switch record for the admitted
   digest. Its rung floor is computed from the passing cases (see below).
3. **batch**: three referrals. Each item starts while the previous item's
   record check is still running.
4. **crash**
   - An item runs in a child process, which is killed (SIGKILL) while its
     record check runs.
   - The resume re-runs only the check.
   - The oracle counts one chart.
   - After the restart the killed item reads as interrupted, so the
     `problems_only` switch turns off. A person approves the resume.
5. **drift**
   - The vendor relabels the save button ("Confirm Create New Patient"
     becomes "Save new patient").
   - The item stops before saving.
   - Self-repair runs. When it may repair the stop, it proposes a fix, a
     person approves it once, and the fix is re-qualified on the test copy
     and re-admitted with lineage. A relabeled save button is not such a
     stop (see Results).
6. **switch** again, for the repaired digest. A switch record names one
   bundle digest, so a re-admitted version needs its own record.
7. **continue**: two more referrals on the repaired version. The pin to the
   original digest passes through the lineage.

### The rung floor

For each step, the switch record's rung floor is the weakest resolution rung
used by any VERIFIED representative case of the qualification. A grounder
(model) resolution is never part of the floor. The dialogs those cases
dismissed become the record's allowed dialogs. Under `problems_only`, a run
that resolves a step below its floor, or meets a dialog outside that set,
goes to a person. Cloud computes the floor the same way on a real switch;
here `_passing_screen_facts` in `run.py` computes it locally.

The oracle reads the database directly and counts the charts for each
referral's email. The person is scripted (`ScriptedPerson`), and every touch
is in `ledger.jsonl`. The repair agent is a rule-based stand-in that calls no
model (`LabelAgent`). Pass `--repair-agent module:factory` to use another,
such as the Claude bridge agent in `benchmark/auto_repair`. The
`problems_only` switch status comes from a local stand-in for Cloud's signed
status route (`LocalStatusAuthority`); it signs "on" for the open switch.

## Results

proof-15 ran every phase on 2026-10-20 at commit `e58b279f9` of
`compose/lifecycle-proof`, against the pinned local OpenEMR release.

| Phase | What happened | Charts | Person touches |
| --- | --- | --- | --- |
| admit | All 6 qualification cases (one trial each) matched their expected outcomes: the representative case read `VERIFIED`, and the 5 fault cases stopped before any effect. The encrypted bundle `0c985b8b…` was certified under `clinical-write` and passed the Regulated run gate. | n/a | 0 |
| switch | The switch record was opened for that digest, with a structural rung floor on 15 steps and no allowed dialogs. | n/a | 0 |
| batch | r1, r2 and r3 each read `VERIFIED`, `no_person`, with 0 model calls. Each record check ran while the next item started. | 1 each | 0 |
| crash | r4 was killed (SIGKILL) inside its record check. The durable pause held the check (`readback_pending`, `delivery_uncertainty`). After a person approved the resume, only the check re-ran, and the run read `VERIFIED`. | 1 (at kill and after) | 1 |
| drift | After the relabel, r5 stopped before saving: the identity check failed at the save step, and the run read `REJECTED_POLICY`, `stop_before_saving`. Self-repair refused it as `not_repairable`. | 0 | 0 |
| continue | With the app still changed and the original bundle, r7 and r8 each stopped before saving, in the same way. | 0 each | 0 |

What this shows on the real application:

- **No double save.** The killed item has exactly one chart, and the save
  was never sent again.
- **No silent wrong save.** After the app change, every item stopped before
  saving and went to a person.
- **No repair hop.** The repair, re-qualification and re-admission hop did not
  run here. Self-repair never repairs a stop where the identity check didn't
  verify, because that stop could mean the wrong record. A relabeled control
  is indistinguishable from a different control, so a person re-teaches the
  step. That hop is covered by synthetic tests
  (`tests/test_repair_readmission.py`). Running it on OpenEMR needs an app
  change that the target ladder can't find at all, such as a moved or
  restyled button. That is a follow-up.

The earlier attempts are kept in `results/earlier-attempts/`. Each one found
something:

- **proof-12: a screen-hash race.** Admission failed closed on a repaint
  race. The stale-identity fault receipt hashes the screen once in the driver
  and again in the replayer, so a repaint between the two breaks the binding.
  A fix is planned in its own change.
- **proof-13: the batch worked, and the crash harness was wrong twice.**
  - The harness wrapped the record check. The `problems_only` gate correctly
    refused the wrapped check (`verifier_identity_changed`) and turned the
    switch off.
  - The harness's kill landed after the save was sent but before the durable
    pause was written. The runtime then started a durable run's check before
    its pause existed. That is fixed: the check now waits for the pause.
- **proof-14: one key for resume.** Resume failed closed because the harness
  used a separate checkpoint key. `resume` opens the bundle and the
  checkpoints with one key, as `openadapt-flow resume` does.

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

- **Massachusetts referrals only.** OpenEMR's state select shows a name and
  stores a code. The record check reads the entered name back through a value
  map in verify.yaml, and the map names only `Massachusetts: MA`. Every
  referral in this run is in Massachusetts. A referral in any other state
  fails its record check closed and goes to a person. A full map is
  [#1461](https://github.com/OpenAdaptAI/OpenAdapt/issues/1461).
- **The approval state outside a sealed set.** The bundle is encrypted file by
  file. A whole sealed set refuses an approval gate today, because Desktop and
  the console can't read sealed approval state yet.

- **The test copy.** Re-qualification runs on the same fixture. Its data is
  dumped before and restored after, so the six cases' resets don't erase the
  batch's charts. A deployment would point verify.yaml at a separate copy.
- **The switch status.** It is signed locally. Cloud's status route and its
  switch thresholds are not part of this run.
