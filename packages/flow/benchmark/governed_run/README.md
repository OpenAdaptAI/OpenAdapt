# Governed run authorization probe

Synthetic deterministic policy-handoff evaluation; not a production reliability benchmark.

Trials per condition: **3**. Oracle: backend action log plus the persisted `Replayer` result.

| Condition | Correct action | Silent wrong action | Safe halt | Over-halt |
| --- | ---: | ---: | ---: | ---: |
| `permissive_unreadable` | 0/3 | 3/3 | 0/3 | 0/3 |
| `governed_unreadable` | 0/3 | 0/3 | 3/3 | 0/3 |
| `governed_verified` | 3/3 | 0/3 | 0/3 | 0/3 |

Caveat: This isolates runtime authorization semantics; it does not measure OCR accuracy, application reliability, or production error rates.
