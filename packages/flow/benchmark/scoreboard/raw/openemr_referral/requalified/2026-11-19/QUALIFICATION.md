# Qualification result

- Verdict: PASS, certified under `clinical-write`
- Campaign time: 653.6 s for 18 trials
- Certified bundle: `bundle/`
- Surface: `local`; lookups used: structure
- Pixels-only pass (for information): 0/18 (0%) trials behaved as required with structure lookups disabled; most common failure: StateDiverged: refusing a durable checkpoint with invalid action evidence: production action lacks a closed runtime actuation path

| Case | Trial | Expected | Observed | Read-back | Model calls | Engine ms |
|---|---|---|---|---|---|---|
| representative | 1 | VERIFIED | VERIFIED | exact_record | 0 | 16018 |
| representative | 2 | VERIFIED | VERIFIED | exact_record | 0 | 15475 |
| representative | 3 | VERIFIED | VERIFIED | exact_record | 0 | 15613 |
| fault-ambiguity | 1 | HALTED | HALTED | no_mutation | 0 | 15287 |
| fault-ambiguity | 2 | HALTED | HALTED | no_mutation | 0 | 14782 |
| fault-ambiguity | 3 | HALTED | HALTED | no_mutation | 0 | 14602 |
| fault-wrong-identity | 1 | HALTED | HALTED | no_mutation | 0 | 13834 |
| fault-wrong-identity | 2 | HALTED | HALTED | no_mutation | 0 | 13815 |
| fault-wrong-identity | 3 | HALTED | HALTED | no_mutation | 0 | 14108 |
| fault-stale-identity | 1 | HALTED | HALTED | no_mutation | 0 | 14396 |
| fault-stale-identity | 2 | HALTED | HALTED | no_mutation | 0 | 14101 |
| fault-stale-identity | 3 | HALTED | HALTED | no_mutation | 0 | 14111 |
| fault-weak-effect | 1 | HALTED | HALTED | no_mutation | 0 | 13194 |
| fault-weak-effect | 2 | HALTED | HALTED | no_mutation | 0 | 13116 |
| fault-weak-effect | 3 | HALTED | HALTED | no_mutation | 0 | 13052 |
| fault-missing-effect | 1 | HALTED | HALTED | no_mutation | 0 | 13268 |
| fault-missing-effect | 2 | HALTED | HALTED | no_mutation | 0 | 13258 |
| fault-missing-effect | 3 | HALTED | HALTED | no_mutation | 0 | 12793 |
| rule-existing-duplicate | 1 | HALTED | HALTED | no_mutation | 0 | 26948 |
| rule-existing-duplicate | 2 | HALTED | HALTED | no_mutation | 0 | 26176 |
| rule-existing-duplicate | 3 | HALTED | HALTED | no_mutation | 0 | 26224 |

The case results are signed with per-campaign local test keys. They prove this campaign in this environment, nothing more.
