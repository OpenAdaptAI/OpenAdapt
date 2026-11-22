# Set 4 v2 as a dev set: the B packs on the general GUI mechanisms

Run 2026-11-22 on the Mac: headless Chromium, one process, the scripted
operator unchanged. All 30 cases ran in each arm.

**This is a dev-set number, not a held-out one.** Set 4 v2 was unsealed
before this run. The `packs6` packs were written after reading the portal
source and the per-case portal settings, and after seeing which cases
stopped and why. The answer key was not read; only the scorer opened it.

| | |
| --- | --- |
| Engine | `ENGINE_SHA` (the `claude/form-tx-gui-wiring` branch) |
| Baseline | arm B, `packs5` unchanged, re-run on the same tree (`arm_B_rebase.jsonl`). It matches the registered `../results/arm_B.jsonl` on every case. |
| New arm | `packs6` with `--attester "scripted requester"` (`arm_gui.jsonl`, control logs in `controls_gui/`) |
| Without an attester | `packs6`, no grant (`arm_gui_noattester.jsonl`) |

Everything is synthetic.

## Headline

- **3 of B's 12 clean-case false stops clear: `lta-s01`, `lta-s05` and
  `lta-s06`.** Each case submits the correct record.
- **1 moves: `bfu-s02`.** It no longer stops on the hub's link. It now
  opens every section and stops in "Property and map" at the community
  directory pick (T5), like the other brackwater cases.
- **The other 8 are out of scope and unchanged:** T5 look-alike options
  (`bfu-s01`, `s04`, `s06`, `s09`, `lta-s02`), T1 defaults (`hbm-s05`,
  `s08`) and T3 identical rows (`hbm-s02`).
- **Hard gates hold:** no false receipt and no silent wrong submit. No case
  ended `verified_done` in either arm.
- **No harm added.** Wrong submits fell from 1 (`lta-s03`) to 0. Trap cases
  stopped before submit: 12 of 16 (B: 13 of 16). The one trap that no
  longer stops, `lta-s10`, now stops for the operator on the evacuation
  question, gets the answer and submits the correct record. That case is
  scored correct; in B it was a stop.

| | B (`packs5`) | GUI (`packs6`, attester) | GUI, no attester |
| --- | ---: | ---: | ---: |
| Clean false stops (of 14) | 12 | 9 | 13 |
| Wrong submits | 1 | 0 | 0 |
| False receipts | 0 | 0 | 0 |
| Silent wrong submits | 0 | 0 | 0 |
| Submitted, all correct | 4 of 5 | 9 of 9 | 0 |
| Traps stopped before submit (of 16) | 13 | 12 | — |
| Harness errors | 0 | 0 | 0 |

## What cleared each stop

| Case | B stopped on | Now |
| --- | --- | --- |
| `lta-s01` (2 travellers) | the popup workaround for "Add another traveller" | The repeating group adds traveller 2, fills both and checks each. |
| `lta-s06` (3 travellers) | the same | The same, with three instances. |
| `lta-s05` (2 travellers, details on file) | "No commit control": the "We have these details on file" card was not a screen of the pack | A declared step takes "Update my details" (no write) and the bindings check every value. Then the repeating group runs. This case needed a pack addition written after seeing it. |
| `bfu-s02` | "Next button missing": the hub's review control is a link | The hub opens all four sections in turn. It now stops at the community pick (T5), as the other brackwater cases do. |

## Traps the old stops caught by accident

Three traps were stopped only by the workarounds this removes. Each one is
still handled:

- **`lta-s04`**: the portal moves the return date by a day. The review
  shows the return date beside its label, and the snapshot did not pair the
  two, so B had no pre-submit check. The bridge now reads such a row before
  the commit and stops on it: "The review shows '07/15/2027' for
  'return_date'".
- **`lta-s07`**: stops on the state on file (Oregon) against the intake. It
  is reached through "Update my details", and the binding conflict stops it.
- **`lta-s10`**: the evacuation question no binding answers.
  `stop_on_blank_choice` stops on it, and the operator answers it. With the
  same rule, `lta-s03`, a wrong submit in B, now gets the operator's answer
  and submits correctly.

## The attestation grant

The review confirmation boxes (`lta`, `bfu`) are declared attestations. They
are ticked only on an `AttestationGrant`, never through an intake or derived
fact. In this run the grant is an explicit run input, `--attester "scripted
requester"`: the harness stands in for the person submitting, and each row
records it. With no attester, every case that reaches a review stops there,
which is the "no attester" column.

## What this cannot show

- Three synthetic portals by one author. The packs were edited after the
  set was unsealed.
- Most brackwater cases stop at the community pick, so the hub ran its
  first section to completion in every case. It ran all four only on the
  in-test synthetic hub (`tests/test_form_tx_gui_bridge.py`).
