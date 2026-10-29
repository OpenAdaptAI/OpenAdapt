# Blind run 1: Immunization

Run on 2026-10-29 under `../BLIND.md` (generator frozen at `504eb7bab`) and
`../../stop_card/PREREG_REVIEW.md`, on live Medplum 5.2.8 (the #1640
fixture). The write type, task, demonstrations and fault suite came from an
independent author. The suite was frozen by sha256 (`c091ab44…`) before the
generator saw anything, and it stays private. Per-case results are in
openadapt-internal.

## Setup: the contract

| | |
|---|---|
| Generator output | no refusals; 1 proposal to confirm (identity) |
| Admitted by the generator | **no** |
| Person time | **78 s** (1.3 min) wall time, 2 lines edited: `PERSON_LOG.md` |
| Lines generated and kept | resource type, deployment, 5 field paths, `compare: date`, 3 `allow` values, 2 search parameters, the closed record, the change feed, `settle_s` |
| Lines the person wrote | identity: added `occurrence_date`; search: `occurrence_date: date` |

The generator proposed identity `[patient, vaccine_code]`, which is every
bound field that a reference or token search finds. The task's same-fact
rule also needs the same date. Confirming the proposal would have let a
2024 flu dose stand in for a 2025 one, a silent pass. The gate held: the
generator won't admit an unreviewed identity, and the person declined it.
The frozen generator has no input for a different rule, so the person
edited the contract and ran the generator's self-check on it. The clock
stopped at that self-check instead of at an admission. That is a deviation
from BLIND.md step 3, recorded in `PERSON_LOG.md`.

**Against the target (under 2 person minutes, gate refusing when unsure):**
78 s, but only because the person knew the contract format, and the
generator didn't admit the result. The setup cost dropped from 30 minutes
(run 1's hand-written Medplum Patient contract, `fhir_medplum/readpath/MINUTES.md`)
to about 1.3 minutes plus the reading. Identity for a type with no reviewed
template still needs a person.

## The blind fault suite

| | |
|---|---:|
| Cases | 24 |
| Right | 23 |
| **Silent passes** (a fault reported as `created_verified` or `found_existing`) | **0** |
| False stops | 1 |
| Errors | 0 |

The false stop is a look-alike case. The same-fact rule counts only
completed Immunizations ("an entered-in-error record asserts nothing"). An
`openadapt.ensure-record/v1` identity rule names task fields only, so it
can't leave out a record by status. The verifier counted a non-completed
look-alike as a live match and stopped. That is fail-closed, but a person's
time is spent on it. Fixing it means a status filter in the verifier's
contract, a verifier change with its own adversarial review. Not done here.

## Review: the stop card

16 stops reached a card. The card code chose each category, and each Undo
or Fix ran on the live fixture with a read-back.

| Category | Stops | Median words | Person s per stop (×0.5 / ×1 / ×2) |
|---|---:|---:|---|
| Fix | 6 | 24 | 36.3 / 42.6 / 55.0 |
| Undo | 8 | 23 | 36.2 / 42.3 / 54.5 |
| Open in the app | 2 | 18 | 184.6 / 339.1 / 648.2 |

| Machine step (Undo or Fix, with read-back) | Runs | Median | p90 | Failed read-backs |
|---|---:|---:|---:|---:|
| Undo | 8 | 0.021 s | 0.023 s | 0 |
| Fix | 6 | 0.021 s | 0.022 s | 0 |

Loopback HTTP to a local fixture: a deployed server adds network time.
At these sizes the person steps dominate whatever it adds.

**Median person seconds per caught harm (×1): 42.3**, against V1's 420
(×0.5: 36.2; ×2: 54.5). This is below the pre-registered bar of 210, so
by the pre-registered rule the card halves review cost on this suite.

**A category defect, reported as `PREREG_REVIEW.md` requires.** The false
stop's card was **Undo**. Its Undo deleted the agent's correct record,
which the stop then charges as a redo (223.8 s at ×1). For a stop whose
right answer was "the record is fine", the right category was Approve. The
cause is the same missing status filter: the card saw two live matches and
would keep neither. Under the pre-registration, **the card is not used in
run 3 until this is fixed and re-measured.** The fix belongs with the status
filter: once the look-alike isn't a match, the stop doesn't happen.

### Run 1, retrospectively

The model applied to run 1's B stops by class (`stop_card_model.json`).
This is labelled retrospective; run 1's verdict is unchanged. On the 10
caught harms, the card's total is 1,611 person-seconds against V1's 4,200.
Four of those harms (mixed, misleading referral) stay at Open in the app,
339 s each. Run 1's 4 false and correct stops are left out of both totals:
this suite produced no Approve card to measure.

## Not claimed

Person time is an agent's wall time; the card's seconds are modelled from
published operators and timed machine steps, not a timed person. One write
type, 24 cases.

## Files

- `ensure_immunization.yaml`: the contract the suite ran
- `generate_1.json`: the frozen generator's report, before the edit
- `PERSON_LOG.md`: the timed person actions
- `stop_card_model.json`: `python -m benchmark.stop_card.model` output
- `../teach/immunization/`: the recorded demonstrations and server facts
