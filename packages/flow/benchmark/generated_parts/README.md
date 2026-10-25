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
No app; generation takes about 0.25 s. The one model call is the generic
name-variant table (below), $0.24, made once.

```bash
python -m generated_parts.dup_backtest --dup-check <checkout of #1516>
```

### The objective, and the adoption rule

A miss (a second chart for one patient) is much worse than a false stop (a
person looks for a few seconds), so the rule is chosen to **miss as few
calibration duplicates as possible, subject to an estimated false-stop
budget** per new record. False stops are estimated per comparison from the
system's own records (Fellegi-Sunter u-probabilities), multiplied over a
clause's terms and by the table size.

**Adoption rule** (`identity.adopt`, enforced by `generate-parts build
--reference-duplicate-check`): a generated rule replaces the system's
hand-built check only if it misses no more duplicates on the same
calibration set. Otherwise the hand-built check stays, and the review says
why.

### The frontier (long stream)

| Budget (false stops per new record) | Calibration misses, generated | Test misses, of 223 | Test false stops, of 440 |
|---:|---:|---:|---:|
| 0.005 | 142 of 604 | 54 | 0 |
| 0.01 | 114 | 54 | 0 |
| 0.02 | 100 | 54 | 0 |
| 0.05 | 73 | 54 | 0 |
| 0.1 | 45 | 53 | 0 |
| #1516 record check (hand-built) | 3 of 604 | 24 | 2 |

With the name-variant table. Without it the rows are within a few misses of
these; with assumed labels (two existing charts sharing a first name and a
date of birth judged one person, not a person's labels) the 0.1 row gives
41 calibration misses and 52 test misses.

**On this system the adoption rule keeps the hand-built check at every
budget**: it misses 3 of 604 calibration duplicates where the generated rule
misses 45 or more. Generation is worse here.

### Why it is worse here

- **The calibration doesn't transfer.** Raising the budget cuts calibration
  misses from 142 to 45 and test misses not at all. Of the generated rule's
  test misses, 24 are the two kinds #1516 also can't catch, and the rest
  (married name and new contact details; typos in both names; a prior name
  with new contact details and a mistyped day) are the error kinds it still
  misses in calibration, at every budget.
- **This system's own records say the key clause is risky.** Catching a
  married name with new contact details needs "same first name and same date
  of birth". Six pairs of the 30 existing charts share both, so on this
  system's data that clause would stop 12 of 30 existing patients posed as
  new; #1516's hand-built rule accepts that (it has the same 12 calibration
  false stops) and is right on the stream (2 false stops in 440). Thirty
  records are too few to tell an artifact from a real risk; the generated
  rule takes the conservative reading.
- **Signals.** The generated rule now has what #1516 has: exact name and date,
  the medical record number, nicknames (a generic, model-made table of
  given-name variants, `generate/name_variants.json`, from common names only:
  no record leaves the system), initials, phonetic and one-letter matches,
  a date one part off or with day and month swapped, shared or hyphenated
  surname parts, a prior last name, swapped names, and contact tie-breakers.
  What it lacks is the hand-written judgment of which combinations to accept.

### How the rule got here

The rule changed four times on this stream, after seeing results, so this
stream is no longer a clean held-out test for the generator:

| Version | Change | Test missed | Test false stops |
|---|---|---:|---:|
| 1 | one error at a time | 78 | 0 |
| 2 | each error with contact details kept and changed; swapped names, an initial, a second surname, a prior name | 42 | 12 |
| 3 | estimated false stops per record from per-comparison rates | 54 | 0 |
| 4 | misses first within a budget; nicknames, MRN, typos in both names; adoption rule | 52-54 | 0 |

The committed result is `results/duplicates.json`: every arm, each rule in
words, and both checks' calibration scores.
