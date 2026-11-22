# Results: Set 4 v2 (sealed), arms A, B0 and B

Run 2026-11-22 on the Mac: headless Chromium, one process through the shared
CPU slots, because the lab laptop's WSL was hung. Each arm ran once, in the
registered order A, B0, B.

| | |
| --- | --- |
| Portals | sealed folder, freeze commit `eb7189f83` |
| B0 engine | `866aac8c1` |
| B engine | `1ddd69712` |
| Packs | committed at `a4e890c21`, before any scored case (D1) |
| Raw rows | `results/arm_{A,B0,B}.jsonl` and the full logs |
| Per-control logs | `results/controls_{B0,B}/` |
| Counts | `../form_tx_heldout_run/summarize.py --suite=set4v2` |

Everything is synthetic.

## Headline

**Verdict: rework. The halving test fails, so the deterministic-fix claim is
killed. Both hard gates hold.**

- **Clean-case false stops: B 12 of 14, B0 9 of 14.** B stopped more
  clean cases than B0, not half as many.
- **Hard gates:** zero false receipts and
  zero silent wrong submits, in B and in B0. Nothing ended `verified_done`
  in either arm.
- **B submitted 5 cases: 4 correct and 1 wrong.**
  - The wrong one is `lta-s03`, an evacuation limit the dev case never showed.
    It ended `readback_incomplete`, and the flag names the field as
    `unbound:Emergency medical evacuation limit`. The unbound-control rule did
    its job.
  - B0 submitted 8: 6 correct and 2 wrong, both flagged after submit.
- **Arm A (unguarded):** 13 of 30 correct, 16 wrong submitted with no flag,
  1 error.

## Criteria

| | Bar | B0 | B | |
| --- | --- | ---: | ---: | --- |
| Halving test: B false stops at most half of B0's | B ≤ 4 | 9 | 12 | **killed** |
| False receipts (hard gate) | 0 | 0 | 0 | held |
| Silent wrong submits (hard gate) | 0 | 0 | 0 | held |
| K1, trap cases stopped before submit | ≥75% | 9/16 | 13/16 (81%) | met for B |
| K2, clean false stops | ≤10% | 9/14 | 12/14 (86%) | not met |
| K3, verified share of submitted cases | ≥50% | 0/8 | 0/5 | not met |
| K4, wrong submitted | 0 | 2 | 1 | not met |
| Trap stopped, then correct | | 0 | 0 | |
| Harness errors | | 2 | 0 | |

B0's two harness errors come from the runner, not the engine. The scripted
operator received an `operator_script` action, `select`, that it does not
know.

**Verified share, both ways (secondary metric).** No case was verified in
either arm, so the share is 0 under today's meaning and 0 under the
check-level ladder. All five B submissions read the page they landed on
(`read_route: same_screen`). Under the ladder that read is SHOWN, not
VERIFIED, even where every field passed.

## False stops on clean cases, by type (B)

The type is the form-layer author's judgement, made from the rows and
control logs.

| Type | Count | Cases |
| --- | ---: | --- |
| T5, look-alike option (directory entry not buildable from the intake) | 5 | bfu-s01, s04, s06, s09; lta-s02 |
| T1, UI default versus prefill (default unproven from one demo) | 2 | hbm-s05, s08 |
| T3, identical Select rows (no intake key names the row) | 1 | hbm-s02 |
| Other, pack workaround for "add another traveller" (a popup that cannot exist) | 2 | lta-s01, s06 |
| Other, next control is a link, not a listed control | 1 | bfu-s02 |
| Other, commit behind an attestation screen (no-commit-control stop) | 1 | lta-s05 |

B0's 9 false stops are T5 (6) and missing traveller facts (3), which comes
from the same repeating-row limit.

B stops more clean cases than B0 for three reasons:
- **T1:** the compiler would not prove the deductible default from one
  demonstration. The B pack therefore halts on it, and B0's pack had
  declared it.
- **Repeating rows:** the B larkspan pack declared a stop for multi-traveller
  cases.
- **No-commit rule:** the new rule stops a screen with no commit control.

All three are fail-closed by design. They cost K2 and buy no false receipt.

## Demos per pack

Every pack used **one** demonstration plus the blank-form probe.

- No pack had a second demo.
- So the T1 defaults table could prove a default only where the dev intake
  differed from the blank value.
- The deductible's blank value equalled the dev intake, so it stayed
  unproven. This produced the two T1 stops.

## Wrong submits and not_checked, by type

**Wrong submits:**
- B: `lta-s03`, a control the demo skipped (T2). It was flagged on the
  right field.
- B0: `lta-s03` (T2) and `hbm-s06`, a deductible prefill mismatch caught by
  read-back (T1/T8).

**not_checked in B (14 fields):**
- combined summary rows (traveller name and date of birth) and attestation
  boxes;
- lookup-window search terms and results (`class_keyword`, `class_code`);
- one unbound control.

## What this cannot show

Three synthetic portals by one author, 30 cases, each pack built from one
demonstration. The B0/B comparison also differs in pack authors, not only in
engine and compiler.
