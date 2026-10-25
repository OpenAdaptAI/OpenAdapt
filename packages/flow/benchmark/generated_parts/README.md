# Generated parts against hand-built parts

Measures `openadapt-flow generate-parts`
([`docs/GENERATED_PARTS.md`](../../docs/GENERATED_PARTS.md)) against the parts
written by hand for the OpenEMR referral workflow.

## Duplicates: the generated rule against #1516's record check

`dup_backtest.py` runs both checks on #1516's own streams
(`benchmark/dup_check/measure.py` at `09d0298dc`, read from a checkout of that
branch). The long stream has 676 items: 440 new patients and 223 true
duplicates, 176 of them planted variants of the existing charts. The
generated rule is calibrated only on the 30 charts that exist before the
stream starts, with the generic error model in
`openadapt_flow/generate/identity.py`. It never sees the stream or its labels.
No app, no model calls; generation takes under half a second.

```bash
python -m generated_parts.dup_backtest --dup-check <checkout of #1516>
```

| Long stream, 676 items | generated rule | #1516 record check (hand-built) |
|---|---:|---:|
| missed duplicates, of 223 | 54 | 24 |
| false stops, of 440 new patients | 0 | 2 |
| false stops per 100 items | 0.0 | 0.3 |

Generation is worse at catching duplicates. The 54 misses:

- 24 are the two kinds #1516 also misses (a new name, new contact details
  and the same or a mistyped date of birth: nothing else to match on);
- 12 `pl_married_moved` (a married name and new contact details). The rule
  would need "same first name and same date of birth", and this system's own
  records hold six pairs of separate charts that share both, so calibration
  refuses it. #1516's hand-written rule accepts that risk;
- 12 `pl_both_typos_same_contact` (a typo in both names);
- 6 `r2_prior_name_moved_dob_typo` (a prior name, new contact details and a
  mistyped day).

The rule's own calibration estimates a 21% miss rate on made-up duplicates
of this system's records, and lists the error kinds it misses most (a changed
last name, or swapped names, together with new contact details). That text
goes to the person who approves it. The hand-built rule has a nickname table;
the generated one has none.

The rule's false-stop budget (estimated false stops per new record) changes
calibration, but not these results: at 0.005 and at 0.05 the stream gives 54
misses and 0 false stops.

### How the rule got here

Three versions were run on this stream. The second and third were changed
after seeing the first's result, so this stream is no longer a clean held-out
test for the generator:

| Version | Change | Missed | False stops |
|---|---|---:|---:|
| 1 | one error at a time | 78 | 0 |
| 2 | each error with contact details kept and changed; swapped names, an initial, a second surname, a prior name | 42 | 12 |
| 3 | admit a clause only if its estimated false stops per record, from per-comparison rates on this system's records, fit the budget | 54 | 0 |

Version 1 picked one clause that relied on the email: every made-up
duplicate kept its email. Version 2's loosest clauses ("same first name, last
name one letter apart") never fired on 30 records but did on 440 new
patients; version 3 estimates that risk from each comparison's rate instead
of waiting to see it.

The committed result is `results/duplicates.json`, including each rule in
words.
