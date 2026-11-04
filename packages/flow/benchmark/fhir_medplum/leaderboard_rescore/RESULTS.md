# Results: the write leaderboard's verifier, re-scored

Scored as fixed in [PREREGISTRATION.md](PREREGISTRATION.md), from
`results/score.json` (`score_rescore.py`). 111 runs per agent; 28
(claude) and 33 (openai) misreports; 72 and 68 truthful successes. Intervals
are the case-clustered bootstrap, then Wilson. The outcome vector is a share
of all 111 runs: completion (truthful successes confirmed), harm
(misreports confirmed), false refusal (truthful successes flagged), and
steps (median model calls per run).

**Fidelity gate: passed.** The main column equals the live verdict on all
59 runs the live verifier judged (claude 19, openai 40): outcome, reasons
and evidence counts.

### claude

| Column | Sensitivity (95% CI, %) | Specificity (95% CI, %) | Completion | Harm | False refusal | Steps |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| live (as published, token expired) | 28/28 (100–100; Wilson 88–100) | 7/72 (4–16; Wilson 5–19) | 6.3% | 0.0% | 58.6% | 36 |
| main, as registered | 28/28 (100–100; Wilson 88–100) | 14/72 (12–28; Wilson 12–30) | 12.6% | 0.0% | 52.3% | 36 |
| operations v1, as registered | 28/28 (100–100; Wilson 88–100) | 22/72 (22–39; Wilson 21–42) | 19.8% | 0.0% | 45.0% | 36 |
| main (deviations 1–2) | 25/28 (68–100; Wilson 73–96) | 46/72 (48–79; Wilson 52–74) | 41.4% | 2.7% | 23.4% | 36 |
| **operations v1, pre-registered rule** (deviations 1–2) | 25/28 (68–100; Wilson 73–96) | 55/72 (61–89; Wilson 65–85) | 49.5% | 2.7% | 15.3% | 36 |
| operations v2, exploratory (deviation 3) | 25/28 (68–100; Wilson 73–96) | 64/72 (77–97; Wilson 80–94) | 57.7% | 2.7% | 7.2% | 36 |

### openai

| Column | Sensitivity (95% CI, %) | Specificity (95% CI, %) | Completion | Harm | False refusal | Steps |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| live (as published, token expired) | 32/33 (91–100; Wilson 85–99) | 21/68 (24–38; Wilson 21–43) | 18.9% | 0.9% | 42.3% | 30 |
| main, as registered | 32/33 (91–100; Wilson 85–99) | 33/68 (37–60; Wilson 37–60) | 29.7% | 0.9% | 31.5% | 30 |
| operations v1, as registered | 32/33 (91–100; Wilson 85–99) | 27/68 (25–54; Wilson 29–52) | 24.3% | 0.9% | 36.9% | 30 |
| main (deviations 1–2) | 30/33 (73–100; Wilson 76–97) | 57/68 (67–97; Wilson 73–91) | 51.4% | 2.7% | 9.9% | 30 |
| **operations v1, pre-registered rule** (deviations 1–2) | 30/33 (73–100; Wilson 76–97) | 42/68 (41–81; Wilson 50–72) | 37.8% | 2.7% | 23.4% | 30 |
| operations v2, exploratory (deviation 3) | 30/33 (73–100; Wilson 76–97) | 57/68 (67–97; Wilson 73–91) | 51.4% | 2.7% | 9.9% | 30 |

## What the numbers say

The pre-registered rule is the v1 column. Most of the gain over the
as-registered columns (claude 14/72 to 46/72, openai 33/68 to 57/68 under
main) comes from deviation 1, made after seeing the first results; the
fidelity gate passes with or without it. An independent review checked
every agent write the cap hides: only the fault proxy's after-check creates
on case A10 (3 per agent), none an agent's save still in flight, so the cap
doesn't move a run toward confirming.

- **The published false-flag rate was mostly an expired token.** With the
  verifier able to read, specificity on the same runs and the same
  contracts goes from 7/72 to 46/72 (claude) and from 21/68 to 57/68
  (openai).
- **Sensitivity is 25/28 and 30/33.** Every miss is `late_collateral`
  (A10, 3 runs per agent): the fault proxy writes a stray record *after*
  the check, which no judge that checks at the end can see, by design
  (leaderboard REPORT.md). The published 28/28 and 32/33 counted runs the
  verifier never judged, and the as-registered replay "caught" A10 only
  through deviation 1's artifact.
- **The pre-registered operations rule (v1) helps claude and hurts openai.**
  `between: consistent` accepts a record saved in steps (claude's runs
  through Medplum's form), but refuses a corrected wrong *non-identity*
  value, which #1655's `person` rule accepts: in 15 openai runs the agent
  corrected a value the fault proxy had changed while the record was still
  the person, and v1 refuses them. The transient-duplicate rule cleared all 10 of claude's
  cleaned-up duplicates.
- **v2 (`between: either`) is never stricter than #1655 and keeps the
  partial-save gain**: 64/72 and 57/68, with the same sensitivity. It was
  chosen after seeing v1's result, so it is exploratory here (deviation 3);
  it needs a held-out fault set before it is adopted.
- **What is still flagged under v2** (claude 8, openai 11): 18 runs in
  which a version of the record named another person, patient or substance
  for a while before the agent corrected it (refused by design: a search
  the audit trail doesn't log can have shown it), and 1 run whose
  `_history` showed 2 versions where the audit trail showed 3 writes.

Per-row verdicts: `results/rescore-<column>.json`; every false flag's reasons
and every miss: `score.json`, `why`.

## Limits

- One application (Medplum 5.2.8 and its web app), the public cases, two
  agents; Fara-7B's rows were not frozen.
- The replay serves the verifier's reads from the server's own stored
  history; it was checked against every live verdict available (59), and
  the other 163 had no live verdict to check against.
- The operations rules were designed after seeing the 59 judged runs'
  failure classes (PREREGISTRATION.md, "What was seen").
- OpenAdapt wrote the cases, the proxy, the oracle, the harness and the
  verifier.
