# Generated parts against hand-built parts

Measures `openadapt-flow generate-parts`
([`docs/GENERATED_PARTS.md`](../../docs/GENERATED_PARTS.md)) against the parts
written by hand for the OpenEMR referral workflow.

## Identity: a vetted template, and a generated per-site mapping

Calibrating a matching rule on one site's records can't learn which
near-matches are the same person: on #1516's stream a rule calibrated on the
30 existing charts misses 52 to 54 of 223 duplicates at every false-stop
budget, against the hand-built check's 24 (measured below). So identity is
built from two parts:

1. **A template, never generated.** `patient-identity/1` is #1516's record
   check (`openadapt_flow.runtime.checks.duplicate_record`): exact, close,
   prior-name and nickname clauses with fixed thresholds. It is reviewed,
   versioned code, owned like the safety policy, and named in the parts
   (`openadapt_flow/generate/identity_template.py`, `TEMPLATES`).
2. **A mapping, generated per site.** Which of this system's record fields
   feed each template slot (given and family name, a prior last name, date of
   birth and its stored format, MRN, phone, email, street), and which task
   parameter feeds each input. A field is chosen because its value is what
   the person typed in every demonstration, it is named like the slot, and
   its values look like the slot's (an email has an @); FHIR's coded lists
   are read by code (`telecom[system=email].value`, `name[use=old].family`).

The mapping is verified, never calibrated on the live table:

- **against the demonstrations**: each saved record, read through the
  mapping, must give back what was typed, input by input;
- **on a perturbation suite** built from the site's own records: generated
  duplicates (a typo in one or both names, a nickname, day and month
  swapped, a digit wrong in the date, a changed or hyphenated last name, an
  initial, swapped names), each with the contact details kept and changed;
  one case per slot that only that slot can catch (a referral under the
  prior last name with a mistyped day and new contact details; a new last
  name with a mistyped day and only the email, or only the phone, kept); and
  every existing record posed as a new patient.

**Adoption rule** (`identity_template.adopt_mapping`, enforced by
`generate-parts build --reference-identity`): a generated mapping is used
only if template plus mapping catches every duplicate on the suite that
template plus the reference mapping (the site's hand-written source adapter)
catches, and stops no more existing records. Fewer misses is not enough if
they are different ones. Otherwise the reference stays and the review says
why. The same rule governs a generated duplicate rule (`identity.adopt`,
`--reference-duplicate-check`).

Both yardsticks are built by the generator from synthetic duplicates, so
they lean toward generation: on its calibration set #1516's check missed 0
to 3 duplicates, and on the real stream 24. Passing the adoption rule is
necessary, not sufficient; the stream rows below are the evidence.

### Measured: the generated mapping against #1516's hand-written adapters

`identity_mapping_backtest.py` renders #1516's people (the existing charts
and every new chart the stream creates) in each site shape, generates the
mapping from the site's rows and three demonstrations, and runs the
template, from a checkout of #1516, with each mapping.

| Site shape | Demonstrations read back | Suite misses, of 580 | Suite stops of 30 existing | Adopted | Stream misses, of 223 | Stream false stops, of 440 |
|---|---|---:|---:|---|---:|---:|
| #1516's rows | 6 of 6 inputs | 5 (reference 5) | 12 (12) | generated | 24 (reference 24) | 2 (2) |
| OpenEMR REST rows | 6 of 6 | 5 (5) | 12 (12) | generated | 24 (24) | 2 (2) |
| FHIR Patient | 6 of 6 | 5 (5) | 12 (12) | generated | 24 (24) | 2 (2) |
| FHIR, read by first list item only | 5 of 5 (no email) | **30** (5) | 12 (12) | **reference** | 30 (24) | 2 (2) |

The generated mapping matches the hand-written adapter on all three
shapes, on the suite and on the 676-item stream. The fourth row is the
reason for the suite: read by the first list item only, a FHIR mapping loses
the email and the prior name. The demonstrations can't show that, the suite
does (30 misses against 5), and the adoption rule keeps the reference; on
the stream that mapping would have missed 30 of 223, against 24.

The suite's misses that the reference shares (5 of 580: nicknames the
template's own table doesn't list) and its 12 stops (the six pairs of
existing charts that share a first name and a date of birth, which the
template stops by design) are the template's, not the mapping's.

Two mapping bugs were found by tests, not by this table: grounded matching
once let the email slot take the street (fixed: the values must look like
the slot's), and the last name once took an old name's given name (fixed:
the field or the parameter must be named like the slot).

## A rule calibrated on one site's records (the approach this replaces)

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
