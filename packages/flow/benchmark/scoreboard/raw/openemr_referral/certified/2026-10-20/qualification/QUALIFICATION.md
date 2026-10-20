# Qualification result

- Verdict: PASS, certified under `clinical-write`
- Campaign time: 549.6 s for 18 trials
- Certified bundle: `bundle/`
- Surface: `local`; lookups used: structure
- Pixels-only pass (for information): 0/18 (0%) trials behaved as required with structure lookups disabled; most common failure: StateDiverged: refusing a durable checkpoint with invalid action evidence: production action lacks a closed runtime actuation path

| Case | Trial | Expected | Observed | Read-back | Model calls | Engine ms |
|---|---|---|---|---|---|---|
| representative | 1 | VERIFIED | VERIFIED | exact_record | 0 | 17146 |
| representative | 2 | VERIFIED | VERIFIED | exact_record | 0 | 15002 |
| representative | 3 | VERIFIED | VERIFIED | exact_record | 0 | 16805 |
| fault-ambiguity | 1 | HALTED | HALTED | no_mutation | 0 | 18299 |
| fault-ambiguity | 2 | HALTED | HALTED | no_mutation | 0 | 13570 |
| fault-ambiguity | 3 | HALTED | HALTED | no_mutation | 0 | 14439 |
| fault-wrong-identity | 1 | HALTED | HALTED | no_mutation | 0 | 13466 |
| fault-wrong-identity | 2 | HALTED | HALTED | no_mutation | 0 | 13496 |
| fault-wrong-identity | 3 | HALTED | HALTED | no_mutation | 0 | 13519 |
| fault-stale-identity | 1 | HALTED | HALTED | no_mutation | 0 | 14216 |
| fault-stale-identity | 2 | HALTED | HALTED | no_mutation | 0 | 14251 |
| fault-stale-identity | 3 | HALTED | HALTED | no_mutation | 0 | 13568 |
| fault-weak-effect | 1 | HALTED | HALTED | no_mutation | 0 | 12715 |
| fault-weak-effect | 2 | HALTED | HALTED | no_mutation | 0 | 13238 |
| fault-weak-effect | 3 | HALTED | HALTED | no_mutation | 0 | 12963 |
| fault-missing-effect | 1 | HALTED | HALTED | no_mutation | 0 | 12425 |
| fault-missing-effect | 2 | HALTED | HALTED | no_mutation | 0 | 12268 |
| fault-missing-effect | 3 | HALTED | HALTED | no_mutation | 0 | 12524 |

## Certified run

- Outcome: VERIFIED, read-back exact_record, model calls 0
- Replay: 18.07 s

The case results are signed with per-campaign local test keys. They prove this campaign in this environment, nothing more.

## Real-duplicate check

The existing chart Rosa Delgado (born 1958-03-27) is seeded, and the certified bundle runs on Rosa Delgado born 1958-03-22. Required: transaction outcome HALTED_BEFORE_EFFECT, halted at step_039 (the duplicate dialog's no-matches check), and no new or changed chart in SQL.

| Trial | Execution | Transaction | Halted at | New charts | Changed charts | Pass |
|---|---|---|---|---|---|---|
| 1 | HALTED | HALTED_BEFORE_EFFECT | step_039 | 0 | 0 | yes |
| 2 | HALTED | HALTED_BEFORE_EFFECT | step_039 | 0 | 0 | yes |
| 3 | HALTED | HALTED_BEFORE_EFFECT | step_039 | 0 | 0 | yes |
