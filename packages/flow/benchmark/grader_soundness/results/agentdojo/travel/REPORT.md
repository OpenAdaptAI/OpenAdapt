# Verifier audit: agentdojo/v1.2.2/travel

Kit `verifier-audit/1`, families `openadapt.verifier-audit.families/v1`. 6 tasks; 14 excluded before any fault (the scorer rejected the unedited reference, or the reference effect was not reproducible).

A false accept is a cell where the replayed end state differs from the reference effect in the named way and the environment's own scorer still returned PASS. Degenerate edits (no effect change) and refusals are listed, never counted. Intervals are Wilson 95%. Rates are per family and are not pooled.

| Family | Polarity | Hits / scored | Rate | Wilson 95% | Degenerate | Refused |
|---|---|---|---|---|---|---|
| unauthorized_side_effect | harmful | 0 / 0 | n/a | 0.0–100.0% | 0 | 6 |
| wrong_record | harmful | 0 / 0 | n/a | 0.0–100.0% | 0 | 6 |
| wrong_value | harmful | 0 / 6 | 0.0% | 0.0–39.0% | 0 | 0 |
| duplicate | harmful | 0 / 5 | 0.0% | 0.0–43.5% | 1 | 0 |
| partial_write | harmful | 0 / 0 | n/a | 0.0–100.0% | 0 | 6 |
| late_write | harmful | 0 / 0 | n/a | 0.0–100.0% | 0 | 6 |
| optimistic_success | harmful | 0 / 6 | 0.0% | 0.0–39.0% | 0 | 0 |

Tasks with at least one false accept: 0 / 6 (0.0%, Wilson 95% 0.0–39.0%).

## Counterexamples

None.
