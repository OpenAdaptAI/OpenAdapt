# Verifier audit: refinery-mockmed/trajectory_replay

Kit `verifier-audit/1`, families `openadapt.verifier-audit.families/v1`. 3 tasks; 0 excluded before any fault (the scorer rejected the unedited reference, or the reference effect was not reproducible).

A false accept is a cell where the replayed end state differs from the reference effect in the named way and the environment's own scorer still returned PASS. Degenerate edits (no effect change) and refusals are listed, never counted. Intervals are Wilson 95%. Rates are per family and are not pooled.

| Family | Polarity | Hits / scored | Rate | Wilson 95% | Degenerate | Refused |
|---|---|---|---|---|---|---|
| unauthorized_side_effect | harmful | 0 / 3 | 0.0% | 0.0–56.1% | 0 | 0 |
| wrong_record | harmful | 0 / 3 | 0.0% | 0.0–56.1% | 0 | 0 |
| wrong_value | harmful | 0 / 3 | 0.0% | 0.0–56.1% | 0 | 0 |
| duplicate | harmful | 0 / 3 | 0.0% | 0.0–56.1% | 0 | 0 |
| partial_write | harmful | 0 / 0 | n/a | 0.0–100.0% | 0 | 3 |
| late_write | harmful | 3 / 3 | 100.0% | 43.9–100.0% | 0 | 0 |
| optimistic_success | harmful | 0 / 3 | 0.0% | 0.0–56.1% | 0 | 0 |
| incidental_change | legitimate | 3 / 3 | 100.0% | 43.9–100.0% | 0 | 0 |

Tasks with at least one false accept: 3 / 3 (100.0%, Wilson 95% 43.9–100.0%).

## Counterexamples

### FALSE_ACCEPT: `refinery-mockmed-4bef171e31` / late_write

- Edit: call 0 (POST /api/encounter) is sent after the success claim
- Effect row missing vs reference: `{"note": "Synthetic follow-up in two weeks", "op": "add", "patient_id": "p1", "type": "Triage"}`
- Scorer verdict: PASS

### FALSE_REJECT: `refinery-mockmed-4bef171e31` / incidental_change

- Edit: 1 permitted extra calls before the claim
- Scorer verdict: FAIL

### FALSE_ACCEPT: `refinery-mockmed-5f707e1a31` / late_write

- Edit: call 0 (POST /api/encounter) is sent after the success claim
- Effect row missing vs reference: `{"note": "Synthetic blood pressure recheck", "op": "add", "patient_id": "p2", "type": "Triage"}`
- Scorer verdict: PASS

### FALSE_REJECT: `refinery-mockmed-5f707e1a31` / incidental_change

- Edit: 1 permitted extra calls before the claim
- Scorer verdict: FAIL

### FALSE_ACCEPT: `refinery-mockmed-2568315bc3` / late_write

- Edit: call 0 (POST /api/encounter) is sent after the success claim
- Effect row missing vs reference: `{"note": "Synthetic flu vaccine reminder", "op": "add", "patient_id": "p3", "type": "Triage"}`
- Scorer verdict: PASS

### FALSE_REJECT: `refinery-mockmed-2568315bc3` / incidental_change

- Edit: 1 permitted extra calls before the claim
- Scorer verdict: FAIL

