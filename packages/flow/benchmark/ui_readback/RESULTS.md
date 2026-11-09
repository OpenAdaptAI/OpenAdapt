# Separate-session UI read-back: measured catch rates and false stops

Generated 2026-10-14 by `python -m benchmark.ui_readback.run`.
Platform: Darwin arm64 py3.12.13. Model calls: 0. Localhost only.

Every arm judges the same two effect contracts on the save (exactly one new
Triage encounter for the patient with no earlier row lost, and its note equal
to the input). Arms differ only in where the check reads. Ground truth never
comes from the arm being scored.

These are counts over a small, hand-written, deterministic fault list, not
rates you should expect in production. Repeats agree in every cell.

## GUI replay (MockMed, real browser)

Repeats per cell: 3. Ground truth: the fault server's store, read in process before and after each replay and judged by `benchmark/effect_e2e/ground_truth.judge`. The API arm reads that same store over HTTP, so on this harness its result is close to definitional.

| arm | wrong results caught | caught where the run alone showed success | correct saves the check stopped | correct saves the run stopped |
|---|---|---|---|---|
| no record check (screen postconditions only) | 1 of 8 | 0 of 7 | 0 of 3 | 1 of 3 |
| same-screen check (tier 4) | 1 of 8 | 0 of 7 | 0 of 3 | 1 of 3 |
| separate-session UI read-back (tier 2) | 8 of 8 | 7 of 7 | 0 of 3 | 1 of 3 |
| API read (tier 1) | 8 of 8 | 7 of 7 | 0 of 3 | 1 of 3 |

| scenario | ground truth | no record check | same-screen check | separate-session UI read-back | API read |
|---|---|---|---|---|---|
| `ok`: clean accepted write (control) | correct | passed | passed | passed | passed |
| `idempotent`: sent twice, de-duplicated by key | correct | passed | passed | passed | passed |
| `partial`: row saved, note dropped | partial | **MISSED** | **MISSED** | caught | caught |
| `optimistic`: UI paints success, server rejects (409) | absent | **MISSED** | **MISSED** | caught | caught |
| `duplicate`: save submitted twice, two rows | duplicate | **MISSED** | **MISSED** | caught | caught |
| `double`: save click delivered twice, two rows | duplicate | **MISSED** | **MISSED** | caught | caught |
| `stale`: last-write-wins removed another row | collateral_loss | **MISSED** | **MISSED** | caught | caught |
| `wrong_record`: the row landed on p2 | wrong_record | **MISSED** | **MISSED** | caught | caught |
| `no_persist`: 200, nothing persisted | absent | **MISSED** | **MISSED** | caught | caught |
| `session`: 401; app returns to sign-in | absent | caught | caught | caught | caught |
| `timeout`: committed, then the client aborted | correct | stopped (run) | stopped (run) | stopped (run) | stopped (run) |

Median time per replay on correct saves: no record check 7.59 s, same-screen check 8.39 s, separate-session UI read-back 10.34 s, API read 9.18 s.

## API write (effect_e2e record service, SQLite ground truth)

Repeats per cell: 5. Ground truth: a read-only connection to the SQLite file that bypasses the service, judged by its own classifier (`benchmark/effect_e2e`). Writes go through the replayer's API tier, as in `effect_e2e`; the record page is served from the same database.

| arm | wrong results caught | caught where the run alone showed success | correct saves the check stopped | correct saves the run stopped |
|---|---|---|---|---|
| same-screen check (tier 4) | 2 of 9 | 0 of 7 | 0 of 2 | 0 of 2 |
| separate-session UI read-back (tier 2) | 7 of 9 | 5 of 7 | 0 of 2 | 0 of 2 |
| API read (tier 1) | 8 of 9 | 6 of 7 | 0 of 2 | 0 of 2 |

| scenario | ground truth | same-screen check | separate-session UI read-back | API read |
|---|---|---|---|---|
| `ok`: clean accepted write (control) | correct | passed | passed | passed |
| `no_persist`: 2xx, nothing persisted | absent | **MISSED** | caught | caught |
| `partial`: row saved, note dropped | partial | **MISSED** | caught | caught |
| `duplicate`: the row landed twice | duplicate | **MISSED** | caught | caught |
| `wrong_record`: the row landed on p2 | wrong_record | **MISSED** | caught | caught |
| `stale`: last-write-wins removed another row | collateral_loss | **MISSED** | caught | caught |
| `collateral_unaudited`: target row correct, stray row on the billing table | collateral_write | **MISSED** | **MISSED** | **MISSED** |
| `page_cache`: nothing persisted; the record page renders the app's write echo | absent | **MISSED** | **MISSED** | caught |
| `optimistic`: server rejected (409) | absent | caught | caught | caught |
| `session`: session expired (401) | absent | caught | caught | caught |
| `timeout`: committed, then the client timed out | correct | passed | passed | passed |

Median time per replay on correct saves: same-screen check 0.01 s, separate-session UI read-back 1.40 s, API read 0.01 s.

### Display probes (correct saves; only the record page's display changes)

| probe | same-screen check | separate-session UI read-back | API read | when the read-back stopped |
|---|---|---|---|---|
| `baseline`: record page as declared | passed | passed | passed | - |
| `long_note_visible_text`: page shortens the note to 20 characters; reader reads the visible text | passed | stopped (check) | passed | after the save |
| `long_note_title_attribute`: page shortens the note; reader reads the full note from the title | passed | passed | passed | - |
| `irregular_whitespace`: input has repeated and trailing spaces; HTML collapses them | passed | passed | passed | - |
| `replica_lag_within_timeout`: page shows a new row 1 s late; the effect allows 3 s | passed | passed | passed | - |
| `replica_lag_beyond_timeout`: page shows a new row 1 s late; the effect allows 0.3 s | passed | stopped (check) | passed | after the save |
| `layout_redesign`: row and cell classes renamed; declared selectors no longer match | passed | stopped (check) | passed | after the save |
| `page_posts_audit_beacon`: record page POSTs a 'viewed' beacon on load | passed | stopped (check) | passed | before the save (nothing written) |
| `reader_password_wrong`: the read-back account's password is wrong | passed | stopped (check) | passed | before the save (nothing written) |

## Reproduce

```
python -m benchmark.ui_readback.run
```

Needs the `browser` extra and Chromium (`python -m playwright install chromium`).
