# Stand-in re-run with the stored forms declared (#1811), 2026-11-03

The production contract (`benchmark/lifecycle_proof/verify.yaml`) with
`stored: upper` on street and city and `stored: capitalize_words` on the
names. The run tree was the harness of #1810 at 3e857b089 merged with
`fix/stored-form-openemr` at 3d8db5b31. The stand-in used the pinned
`baseline.sql` (sha256 `a1b14e00…`). No model calls.

| suite | right | faults passed | false alarms | clean saves confirmed |
|---|---|---|---|---|
| `faults.yaml` | 7/17 | 8 | 2 | 1 of 3 |
| held-out 1 to 5 (private) | 4, 6, 4, 5, 7 of 17 | 11, 9, 9, 10, 9 | 2, 2, 4, 2, 1 | 6 of 17 |

At c526ab686, without the stored forms, every suite refuted the clean save:
19 of the 20 clean cases were false alarms. Now 13 are. Twelve of them are
other users' legitimate writes in the same window, which the production
contract has no attribution for (the 666f0c0cc rows show the same). The
thirteenth (held-out 3, a US-format birth date) is a date-format question,
not casing. The held-out rows are on the private branch, under
`runs/stored-form-3d8db5b31/`.

Faults passed are the production contract's known gaps; this change doesn't
close any of them. It only stops a correct save being refuted for its
casing.
