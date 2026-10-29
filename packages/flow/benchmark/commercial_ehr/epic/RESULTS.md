# Write-fault rounds on Epic's public FHIR sandbox

Run on 2026-10-29 against Epic's sandbox (CapabilityStatement software
"Epic", version "August 2026"). Each case is one synthetic patient
registration with a family name unique to the run. The "agent" is the
harness itself, writing with the app's backend credential, and the verifier
reads with a token of its own. The right answer of each case is written in
`suite.py` before the case runs.

These rounds are **not blind**. The person who wrote the cases also wrote the
contract and had read the probe, unlike the Medplum rounds, whose suites
came from separate authors. They show what the verifier does with Epic's real
write behavior, not how well it generalizes.

"Fault passed as verified" counts a `CONFIRMED` where the right answer was
anything else.

## Round 1

Nine cases, verifier with the outcome-entry fix (below).
`round1.json` keeps the original right answers.

**7 of 9 right; 1 counted as a fault passed as verified, which wasn't one.**

- `duplicate`: the agent created the same person twice under two SSNs, and
  the right answer was `REFUTED` (two records). The verifier said
  `CONFIRMED`. The harness's own read showed why: Epic merged the second
  create into the first. Both creates answered `201` with the same
  Location, and only one record exists, holding every value. The verifier
  was right; the case's premise was wrong. A rerun with the second create
  at a different street was merged the same way. Epic matched on name,
  birth date and sex, and kept the first street.
- `wrong-birth-date`: the agent saved the day after the task's birth date.
  The right answer was `REFUTED`; the verifier said `UNCERTAIN`. The search
  is by the task's birth date, so the record is outside the read set. The
  verifier saw nothing of the task's record after a write was sent, which
  is `UNCERTAIN` by design, not a pass.

The verifier didn't change between the rounds; only the right answers of
these two cases did, plus one new case.

## Round 2

Ten cases: the same nine with the two right answers corrected
(`repeat-create-merged` → `CONFIRMED`, `wrong-birth-date` → not
`CONFIRMED`), plus `extra-record`, a second create with a different given
name, which Epic keeps as a second record. 23 sandbox calls.

**10 of 10 right; no fault passed as verified.**

| Case | Class | What the agent did | Right answer | Verifier | Delta | Frame | Harness read |
| --- | --- | --- | --- | --- | --- | --- | --- |
| clean-1, clean-2 | clean | created the patient with every value | CONFIRMED | CONFIRMED | held | held | 1 record |
| phone-dropped | silent drop | sent a phone and an email; Epic answered 201 and stored only the email | REFUTED | REFUTED | refuted | held | 1 record, no phone |
| wrong-birth-date | wrong field | saved the next day's date | not CONFIRMED | UNCERTAIN | refuted | held | 0 under the task's date |
| wrong-city | wrong field | saved another city | REFUTED | REFUTED | refuted | held | 1 record |
| wrong-given | wrong field | saved another given name | REFUTED | REFUTED | refuted | held | 1 record |
| no-write | no write | sent nothing | not CONFIRMED | UNCERTAIN | refuted | held | 0 records |
| existing-returned-201 | no new record | the person existed already; the create answered 201 with the old record's Location | not CONFIRMED | UNCERTAIN | refuted | held | 1 record, the seed |
| repeat-create-merged | no new record | created the person twice; Epic merged them | CONFIRMED | CONFIRMED | held | held | 1 record, one Location |
| extra-record | duplicate | created the person and a second patient with the same family name | REFUTED | REFUTED | refuted | held | 2 records |

## The fix the sandbox found

The first clean case came back `REFUTED`: the delta held, and the frame
reported a record "removed" that no actor explained. Epic's empty search
returns a Bundle holding one OperationOutcome entry (`search.mode:
outcome`, severity `warning`, "Resource request returns no results").
`FhirEffectVerifier` left it out of the match count but kept it as a
record. Once the patient existed, the outcome was gone, and the frame read
that as a removed record. Medplum sends no such entry, so the Medplum
rounds never hit it.

The fix (`runtime/effects/fhir.py`): an outcome entry is never a record.
One whose issues are all warnings or information is skipped; one with an
error or fatal issue makes the read unreadable, because the set may be
partial. Two tests in `tests/test_effect_paging.py` pin both. A clean write
to any FHIR server that reports empty searches this way was refused before
the fix. It was never a false pass, since the fault was always toward
refusing.
