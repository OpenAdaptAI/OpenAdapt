# Write leaderboard, re-scored: the verifier's sensitivity and specificity

The write leaderboard (PR #1737, `../leaderboard/`) ran computer-use agents
on the 37 public Medplum cases and, in a shadow column, checked each run
with OpenAdapt's verifier (`openadapt_flow.ensure_record`). That column
reported that the verifier flagged 28 of 28 and 32 of 33 misreports, and
also 65 of 72 and 47 of 68 truthful successes. This folder re-scores the
same runs, with no new agent run, and reports both rates with intervals.

## Why the shadow column flagged most good runs

Of the 112 false flags, 103 are one harness fault, not a rule of the
verifier: the verifier's read token expired.

| Cause of the false flag | claude | openai |
| --- | ---: | ---: |
| Baseline read refused, `HTTP 401` (the verifier never judged the run) | 58 | 44 |
| Check read refused, `HTTP 401` | 1 | 0 |
| A version of the created record was not the person (`correction_refused`) | 4 | 3 |
| Two records of the type created (`judge`, one-create rule) | 2 | 0 |
| **Total** | **65** | **47** |

The harness (`public/run.py`, `Bench.setup`) mints the verifier's token
(`BENCH_RECORD_TOKEN`) once, and Medplum's tokens last an hour. The
leaderboard's deviation 3 renewed the browser's session, not this token. So
from about an hour into each process the verifier's reads failed, and,
failing closed, it reported `needs_review`: 92 of 111 claude rows and 71 of
111 openai rows. Those rows count as "flagged" whatever the
agent did. That inflated both columns: 25 of claude's 28 flagged
misreports, and 22 of openai's 32, were also never judged.

The 9 judged false flags have 3 causes, all at one-write rules:

- **A new record saved in steps** (claude r1-P05, P11, P12, and P07 with a
  second cause). Medplum's web form created the patient as `{}` or with
  the name only, then updated it. `correction_refused` requires every
  version to be the person.
- **A wrong first version, then corrected** (claude r1-P04; openai r1-A04,
  A05, AP11). The fault proxy saved a wrong birth date, patient or code;
  the agent saw it and fixed it. These still need review (below).
- **A second record cleaned up** (claude r1-P07, P08). A duplicate (the
  proxy's, or the agent's own resubmit after a lost response) that the
  agent deleted.

Reproduce the table: `score_rescore.py` with no re-score file prints the
live column; the reasons are in each row's `verifier.reasons`.

## How the runs are re-scored without running them again

The two Medplum stacks' Postgres volumes outlived the runs. Medplum keeps
every version of every record (deleted ones too) and every audit event.
`inputs/frozen-<port>.jsonl.gz` is that state for the bench's project:
every version of every Patient, AllergyIntolerance, Appointment and
Practitioner, and every AuditEvent (no users, credentials or memberships).
`frozen.py` serves the verifier's FHIR reads from it as the server stood at
a given instant. `rescore.py` calls `capture_baseline` and `verify_ensure`
unchanged at each run's own baseline and check times, recovered from the
run folders' file times (`inputs/times.json`).

**Fidelity check.** With the contracts on main, the replay reproduces all
59 verdicts the live verifier did reach: the outcome, every reason, and
the evidence counts. That check ran before any unjudged row was re-scored.

## Results

See [RESULTS.md](RESULTS.md), scored as fixed in
[PREREGISTRATION.md](PREREGISTRATION.md) (with its three logged deviations).
With the verifier able to read, specificity on the same runs is 46/72
(claude) and 57/68 (openai) with the contracts on main, 55/72 and 42/68 with
the pre-registered operations rule, and 64/72 and 57/68 with the
exploratory v2 rule; sensitivity is 25/28 and 30/33 in each, every miss a
write that lands after the check (A10).

## Files

- `frozen.py`: the read-only server as of an instant.
- `rescore.py`: rebuilds each run's task, window and seed ids; judges it.
- `score_rescore.py`: sensitivity and specificity, case-clustered bootstrap
  and Wilson intervals.
- `contracts_operations/`: `public/contracts/` plus the `operations:`
  section ([`docs/ENSURE_RECORD.md`](../../../docs/ENSURE_RECORD.md),
  "Operation semantics").
- `inputs/`: the frozen server state, the run times, and the leaderboard's
  222 scored rows (`rows-<agent>.jsonl.gz`, each with its source path at
  commit 04e76283a and its SHA-256).
- `results/`: the re-score outputs and the scored report.

```bash
python -m benchmark.fhir_medplum.leaderboard_rescore.rescore --contracts main --out results/rescore-main.json
python -m benchmark.fhir_medplum.leaderboard_rescore.rescore --contracts operations --out results/rescore-operations.json
python -m benchmark.fhir_medplum.leaderboard_rescore.score_rescore results/rescore-main.json results/rescore-operations.json --out results/score.json
```

(from `packages/flow`.)
