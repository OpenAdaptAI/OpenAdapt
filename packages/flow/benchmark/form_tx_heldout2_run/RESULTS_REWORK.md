# Dev re-run: the reworked engine on the eight portals it was fixed against

**These are dev-set rows, not held-out evidence.** The four set-2 portals
(ferncastle, glasswing, okelund, velloway) and the four set-1 portals
(harborline, cobalt, pinecrest, tidewater) were read in full while writing
the rework (#2461): their source, cases and answer key. The packs are the
frozen ones from the earlier runs (`packs2`, `packs`), unchanged. These rows
therefore measure what the engine change does to the same packs, nothing
more. The held-out evidence for the rework is `../form_tx_heldout3_run/`.

Run 2026-11-21, lab laptop, headless, arm B, same runner and scripted
operator, no `--posthoc`. Two engines:
- `8a9f08b75`: the first rework commits.
- `13772e2b6`: final. It adds the unused-intake rule, legend-less radio
  names and one-question forms.

Raw rows are in `results_rework/`.

## Set 2 (ferncastle, glasswing, okelund, velloway), 33 cases (18 trap, 15 clean)

| Metric | Before (`72a6ad428`, `results/arm_B.jsonl`) | `8a9f08b75` | `13772e2b6` |
| --- | ---: | ---: | ---: |
| Submitted | 11 | 6 | 6 |
| Correct | 6 | 6 | 6 |
| Correct and verified with a receipt | 1 | 1 | 1 |
| Wrong submitted | **5** | **0** | **0** |
| - with a receipt | 0 | 0 | 0 |
| Trap cases stopped before submit | 15 | 17 | 17 |
| - then correct | 0 | 1 | 1 |
| Clean cases with a false stop | 10 | 11 | 11 |
| Unsubmitted | 22 | 27 | 27 |

## Set 1 (harborline, cobalt, pinecrest, tidewater), 32 cases (18 trap, 14 clean)

| Metric | Before (`72a6ad428`, `results/dev_set_old_portals_arm_B.jsonl`) | `8a9f08b75` | `13772e2b6` |
| --- | ---: | ---: | ---: |
| Submitted | 25 | 23 | 23 |
| Correct | 20 | 21 | 21 |
| Correct and verified with a receipt | 10 | 9 | 0 |
| Wrong submitted | 5 | 2 | 2 |
| - with a receipt (false proof) | **2** | **1** | **0** |
| - flagged after submit | 3 | 1 | 2 |
| Trap cases stopped before submit | 16 | 17 | 17 |
| - then correct | 9 | 11 | 11 |
| Clean cases with a false stop | 2 | 3 | 3 |

## What the rows show

### Wrong submits drop

Set 2 goes from 5 wrong submits to 0, and set 1 goes from 5 to 2. The five
set-2 wrong submits each became a stop before submit:

- `fc-s04`, `ok-s03`, `vw-s06`: the sign-in-then-reset cases end in
  `fields_not_rechecked`, because the frozen packs have no
  `revisit_selector`.
- `fc-s02`: a client number in the intake with an empty field stops.
- `gw-s04`: the unbound roommate prefill stops; the operator clears it, and
  the case then submits correctly but `readback_incomplete`.

### False receipts are gone at the final engine

- `cr-s02` now stops on the unbound additional-driver prefill.
- `cr-s07` (a driver the intake has but the pack never entered) kept its
  receipt at `8a9f08b75`. At `13772e2b6` it is `readback_incomplete`, and
  the flag names the `driver2.*` intake keys.

### Verification falls to 0 on set 1

The final engine counts every intake key the old packs never consumed
(`property.area_unit`, `property.city_state` and others) as `not_checked`,
because those packs predate `ignored_intake_keys`. This is the rule working
as designed on packs that never declared what they skip. It is not a
regression in what gets read back.

### K2 does not move on these packs

The false stops on clean cases are now named, but the frozen packs still:

- treat portal defaults as prefills (no `portal_default_values`);
- leave the okelund lookup's "Select" without `row_cell_dossier_keys`;
- have no read-back for velloway's three-box dates.

So the operator script has no entry, and the case escalates. Revising those
packs would be a further, clearly post-hoc row, and it was not run.
