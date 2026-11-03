# Write-fault rounds on Epic's public FHIR sandbox

Run on 2026-10-29 against Epic's sandbox (CapabilityStatement software
"Epic", version "August 2026"). Each case is one synthetic patient
registration with a family name unique to the run. The "agent" is the
harness itself, writing with the app's backend credential, and the verifier
reads with a token of its own. The right answer of each case is written in
`suite.py` before the case runs.

Rounds 1 to 3 are **not blind**: the person who wrote their cases also wrote
the contract and had read the probe. The blind round's cases came from a
separate agent (below).

| Round | Cases | Right | Faults passed as verified | Blind |
| --- | --- | --- | --- | --- |
| 1 | 9 | 7 | 0 (one counted, then shown right) | no |
| 2 | 10 | 10 | 0 | no |
| Blind | 13 | 12 | 0 | **yes** |
| 3, after the contract fix | 10 | 10 | 0 | no |
| Blind file rerun, after the fix | 13 | 13 | 0 | no (the file had been run) |

"Fault passed as verified" counts a `CONFIRMED` where the right answer was
anything else.

## The read path these results cover

Everything the verifier read went through Epic's FHIR R4 API with its own
SMART backend token (scope `system/Patient.read`):

| Read | Used for |
| --- | --- |
| `GET Patient?family=…&birthdate=…`, every page, before and after the write | the delta (one new record holding the task's values) and the frame (nothing else in that set moved) |
| `GET metadata` | the server's `Date` header, for the window |

What the sandbox doesn't offer, so these results don't cover it:

- **No AuditEvent** (the endpoint answers 404) and **no `_history`** (404),
  and records carry no `meta.versionId` or `meta.lastUpdated`. Nothing says
  who wrote a record or whether it changed during the window, other than
  comparing values.
- **Provenance only through `_revinclude=Provenance:target`** on a search,
  naming the latest author and time. The probe read it, but the verifier
  doesn't use it, so authorship isn't checked.
- **No change sweep:** `_lastUpdated` is refused on Patient and ignored on
  Observation, so writes outside the search set (another patient, another
  resource type) are not seen.

Wording for any mention outside this repository: "checked against Epic's
public FHIR sandbox (open.epic) through FHIR". No logo, no "certified" or
"validated by Epic", and no timing or load figures; the sandbox terms rule
those out (FINDINGS.md).

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

## Blind round

A separate agent wrote 13 cases from a brief alone: the task, the meaning of
each outcome, Epic's measured behavior, and the suite format. It had no
access to the verifier, the contract or these rounds. The file was frozen
before the run (sha256
`ff402d6af689832f0d4ca3e51a6e8f6dcfdcf07f591689620054f1b7d278c08d`), the
check and contract at commit `c93069d0d`. It ran once. The cases and
per-case results stay private; `blind_summary.json` has the counts.

Classes: clean 4, wrong field 2, missing field 1, silent drop 1, no write 1,
duplicate 1, no new record 2, other 1. Right answers: `CONFIRMED` 4,
`REFUTED` 5, `UNCERTAIN` 2, either refusal 2.

**12 of 13 right; no fault passed as verified.** The miss was a false
refusal of a clean save: a patient with the same family name and birth date
already existed, and the agent registered the task's person correctly. The
record count held (one new record), but each value check matched every
record with the task's family name, the old one included, and read
"conflicting". The defect was in the contract, not the verifier. The
contract now declares one `exact_new_set` effect, whose new record must hold
every value, so records that were there before aren't judged. Losing one
still refutes. Round 3 reran the ten built-in cases on the new contract
(10 of 10), and the blind file got 13 of 13 on a rerun, which is no
longer blind.

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
partial. An OperationOutcome with no `search.mode` is treated the same way.
Tests in `tests/test_effect_paging.py` pin each case, and `ensure_record`'s
reader now applies the same severity check. [REVIEW.md](REVIEW.md) has the
adversarial review. A clean write
to any FHIR server that reports empty searches this way was refused before
the fix. It was never a false pass, since the fault was always toward
refusing.
