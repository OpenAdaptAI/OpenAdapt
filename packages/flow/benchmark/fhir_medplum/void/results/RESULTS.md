# Re-measure with `void`: results

Run on 2026-10-29 under `REMEASURE.md`, which was committed before the run.
The fixture was live Medplum 5.2.8 (the #1640 fixture, fresh volume). The
code was this branch merged with #1653 (`ca44ffce0`). Per-case results for
the frozen suite are in openadapt-internal.

## What decides whether the card goes into run 3

| Criterion (`REMEASURE.md`) | Result | Holds |
|---|---|---|
| Silent passes on the frozen suite: 0 | 0 | yes |
| Category defects: 0 | 0 | yes |
| Failed read-backs: 0 | 0 (13 timed Undo or Fix) | yes |
| Median person seconds per caught harm (×1) below 210 | 42.3 | yes |
| `void/cases.yaml` with the void line: 8 of 8 | 8 of 8 | yes |

All five hold.

## The frozen blind suite (sha256 `c091ab44…`, unchanged)

The contract is `ensure_immunization_void.yaml`: blind run 1's contract
plus `void: - status="entered-in-error"`.

| | Blind run 1 | With `void` |
|---|---:|---:|
| Cases | 24 | 24 |
| Right | 23 | **24** |
| Silent passes | 0 | **0** |
| False stops | 1 (c08) | **0** |
| Stops that reached a card | 16 | 15 |
| Category defects | 1 (c08: Undo on a correct record) | **0** |

The look-alike case c08 now ends `created_verified`. Its entered-in-error
look-alike is no longer the person, so there is no stop and no card. The
other 23 outcomes are the same as in run 1.

## The card

| Category | Stops | Median words | Person s per stop (×0.5 / ×1 / ×2) |
|---|---:|---:|---|
| Fix | 6 | 24 | 36.3 / 42.6 / 55.0 |
| Undo | 7 | 23 | 36.2 / 42.3 / 54.5 |
| Open in the app | 2 | 18 | 184.6 / 339.1 / 648.2 |

| Machine step (with read-back) | Runs | Median | p90 | Failed read-backs |
|---|---:|---:|---:|---:|
| Undo | 7 | 0.027 s | 0.032 s | 0 |
| Fix | 6 | 0.027 s | 0.030 s | 0 |

**Median person seconds per caught harm (×1): 42.3** (×0.5: 36.2; ×2:
54.5), against V1's 420. Every stop is a real fault. Each Fix stop is a
field-level difference on the one created record with no other writer. Each
Undo stop reverses only the agent's own writes. The two Open stops are the
cases where someone else wrote, or nothing was sent. Load average stayed
under 20 during the run.

## `void/cases.yaml` (sha256 `de27464c…`), with and without the line

| Case | Expected | With `void` | Without |
|---|---|---|---|
| v1 void look-alike, agent creates | `created_verified` | right | `needs_review` (false stop) |
| v2 only a void record, no write | `needs_review` | right | **`found_existing` (silent pass)** |
| v3 completed record exists | `found_existing` | right | right |
| v4 completed and void records exist | `found_existing` | right | `needs_review` (false stop) |
| v5 not-done look-alike, agent creates | `needs_review` | right | right |
| v6 agent creates a void record | `needs_review` | right | right |
| v7 agent voids the existing record, creates | `needs_review` | right | right |
| v8 another user voids it, agent creates | `needs_review` | right | right |

With the line: 8 of 8. Without it: 5 of 8, with 2 false stops and **one
silent pass**. Blind run 1's contract, which has no `void`, takes an
entered-in-error record as the person's existing dose (v2). The frozen
suite had no such case, so run 1 did not see this. The line removes a
silent pass as well as the false stop.

v5 still stops, as intended. `not-done` isn't on the void list, so a
not-done record still matches. The list names values to leave out, never
values to keep.

## Not claimed

The card's seconds are modelled from published operators and timed machine
steps, not timed with a person. One write type. 24 frozen cases plus 8
written for this change.

## Files

- `stop_card_model.json`: `python -m benchmark.stop_card.model` output
- `../ensure_immunization_void.yaml`: the contract the suite ran
- `../REVIEW_VOID.md`: the adversarial review of the `void` change
