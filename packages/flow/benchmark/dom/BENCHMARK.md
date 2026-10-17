# Benchmark: compiled vision replay vs. DOM-selector scripts

Date: 2026-07-10. The incumbent comparison. For "run the same browser workflow
N times", the incumbent is not a computer-use agent — it is a
Playwright/Selenium script: also $0 per run, also fast, no OCR anywhere.
This benchmark runs that incumbent head-to-head against the compiled
vision replay on the same task, the same frozen drift schedule, and the
same arm-independent success check, and reports whichever way it comes
out.

**Task** (MockMed, the bundled demo clinic app; fake data only): sign in as
`nurse.demo`, open the first referral task, create a New Encounter of type
Triage, enter a parameterized note (distinct per arm and slot), save.

**The DOM arms are steelmen — both of them.** Playwright's documented
best practices throughout: `get_by_label` for fields, `get_by_role` +
accessible name for buttons, an explicit final outcome assertion,
auto-waiting, standard timeouts, no retries, no sleeps, no brittle
CSS/XPath (the app exposes no `data-testid` contract). The task spec
("open the FIRST referral task") underdetermines one selector, so both
readings run as separate arms: **DOM (positional)** clicks the first
row's Open button — the literal reading — and **DOM (name-filtered)**
clicks the Open button in the row named `Jane Sample` — the
identity reading, hardcoding the demonstrated patient exactly as the
compiled arm's recorded identity band does. Every selector choice is
documented in `openadapt_flow/benchmark/dom_arm.py`.

## Verdict

**The wrong-action vector is spec underspecification, not "Playwright".** On the frozen schedule all arms tie (compiled 14/20; dom 14/20; dom_named 14/20): the drift that halts the compiled replay (notice/reqfield/modal-once) stops both DOM scripts too. Both DOM scripts are ~36x faster per clean run (p50 0.2s vs 7.2s). The perturbation matrix is where the arms separate, and they separate by HOW EACH ARM NAMES ITS TARGET, not by DOM vs vision. (a) **Positional selectors silently retarget under data drift.** The positional script ("first row" — the literal reading of the task spec) wrote to the WRONG PATIENT on 4 of 8 modes (grow, lookalike, missing, sort; 8 runs, every one with a healthy-looking final screen). A position-phrased spec cannot notice that the row's identity changed. (b) **The identity reading fixes it — and where a stable DOM exists, it also out-completes the compiled arm.** The name-filtered script (same code, one selector keyed to the demonstrated patient) completed CORRECTLY on grow, lookalike, move, sort, typelabel and failed closed on missing, rename, with zero wrong actions. The compiled arm was equally safe (wrong actions: 0) but never healed to the true row on these modes — every data-drift outcome was a halt: on data drift the name-filtered DOM arm finished the work the compiled arm safely declined. (c) **What demonstration buys: the identity came for free.** Nobody had to DECIDE that "first referral" really means Jane Sample — the demonstration captured the target's identity as a matter of course, while the DOM arms needed that judgment hand-written into a selector (and the positional variant shows what happens when it is not). The compiled arm's remaining browser-side edges on this data: demo-derived identity with no spec authoring, heal-through of label drift (`rename` broke both DOM scripts' Open selector — a human edit each — while the ladder healed through it), and fail-closed halts with an accurate report. Its costs are equally plain: ~36x slower per run, and an OCR judge with failure modes of its own. (One condition — `theme` — produced judge-disputed verdicts: an OCR-judge artifact affecting both arms equally, not an automation difference; see the measurement-validity section.) Boundary, stated plainly: this comparison exists ONLY on browser backends. On desktop, VDI/Citrix, or any pixels-without-DOM substrate there is no selector script to write — the criticism's own point — and wherever a stable, accessible DOM exists, an identity-keyed selector script is the honest incumbent to beat: as fast as the positional one and, on this matrix, as safe as the compiled arm.

![schedule success rate and perturbation outcome matrix](outcome_matrix.png)

## Head-to-head on the frozen 20-slot schedule

The hybrid benchmark's exact schedule: 20 slots,
6 drifted (30% — two each of `notice`,
`reqfield`, `modal-once`), identical condition per slot index for every
arm.

| | compiled replay | DOM (positional) | DOM (name-filtered) |
|---|---|---|---|
| runs | 20 | 20 | 20 |
| success rate | 70% (14/20) | 70% (14/20) | 70% (14/20) |
| success on clean slots | 14/14 | 14/14 | 14/14 |
| success on drifted slots | 0/6 | 0/6 | 0/6 |
| wall-clock p50 | 7.3 s | 0.2 s | 0.2 s |
| wall-clock p95 | 12.9 s | 30.2 s | 30.2 s |
| wrong-action events | 0 | 0 | 0 |
| maintenance events (needs human edit) | 0 | 6 | 6 |
| model cost | $0 | $0 | $0 |

Read the maintenance row with its asymmetry in view (details in the
section below): a DOM maintenance event means a human edits the script;
a compiled drift halt is not counted there but is not free either — it
takes a fresh one-minute demonstration or an agent fallback. Neither
number is "zero cost"; they are different currencies.

Per-condition outcomes on the schedule:

| condition | compiled replay | DOM (positional) | DOM (name-filtered) |
|---|---|---|---|
| `clean` | 14/14 success | 14/14 success | 14/14 success |
| `notice` | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error |
| `reqfield` | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error |
| `modal-once` | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error | 0/2 success, 2 halt/error |

## Head-to-head on the perturbation drift modes

The validation suite's drift matrix (PR #12/#13) plus `sort` and
`typelabel`; every mode is flag-gated in the MockMed app and deterministic.
One fresh browser per run.

| drift mode | compiled replay | DOM (positional) | DOM (name-filtered) |
|---|---|---|---|
| `lookalike` | halt/error at `step_005` (4.5s) x2 | **WRONG ACTION** (wrote to the wrong target; final=#patient/p0, 0.2s) x2 | success (0.2s) x2 |
| `missing` | halt/error at `step_005` (4.6s) x2 | **WRONG ACTION** (wrote to the wrong target; final=#patient/p2, 0.2s) x2 | halt/error at `open referral by patient name` (30.1s) — needs human edit x2 |
| `grow` | halt/error at `step_005` (4.6s) x2 | **WRONG ACTION** (wrote to the wrong target; final=#patient/g1, 0.2s) x2 | success (0.2s) x2 |
| `sort` | halt/error at `step_005` (5.1s) x2 | **WRONG ACTION** (wrote to the wrong target; final=#patient/p2, 0.2s) x2 | success (0.2s) x2 |
| `theme` | failed verification (16.1s) — but the run COMPLETED; see the measurement-validity note; success (14.4s, 8 heals) | success (0.2s) x2 | failed verification (0.2s) — but the run COMPLETED; see the measurement-validity note; success (0.2s) |
| `rename` | success (9.7s, 2 heals); success (9.6s, 2 heals) | halt/error at `open first referral` (30.1s) — needs human edit x2 | halt/error at `open referral by patient name` (30.1s) — needs human edit x2 |
| `move` | success (8.3s, 2 heals) x2 | success (0.2s) x2 | success (0.2s) x2 |
| `typelabel` | success (7.9s, 1 heal); success (8.0s, 1 heal) | success (0.2s) x2 | success (0.2s) x2 |

## Wrong-action events, all arms

- **dom** on `lookalike`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p0`
- **dom** on `lookalike`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p0`
- **dom** on `missing`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p2`
- **dom** on `missing`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p2`
- **dom** on `grow`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/g1`
- **dom** on `grow`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/g1`
- **dom** on `sort`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p2`
- **dom** on `sort`: wrote this run's note with the save evidence on screen but right_patient=False, wrong_type_row=False, final state `#patient/p2`

Totals: compiled replay 0, DOM (positional) 8, DOM (name-filtered) 0 (schedule + perturbation runs).

## Measurement validity — where the judge itself is the weak link

The shared judge is OCR on a screenshot, and OCR can miss low-contrast text (the dark `theme` palette is the known offender; whether a given dark banner is read is deterministic per frame but depends on the note's glyphs — which is why two runs of the same condition can split). The following runs REPORTED FULL COMPLETION — every step executed, structural audit trail consistent with success — yet failed the OCR verification, with no wrong action detected. They are counted as failures in every number above (the judge's verdict stands, identically for every arm), but check the saved final screenshot (the disputed finals are committed alongside this report) before quoting any of them as an automation failure — on audit these are judge false negatives, not automation failures:

- compiled on `theme` (perturbation slot 28, right_patient=True): `finals/perturbation_compiled_028.png`
- dom_named on `theme` (perturbation slot 28, final state `#patient/p1`, right_patient=True): `finals/perturbation_dom_named_028.png`

## Maintenance asymmetry, stated honestly

A DOM script that drift breaks **loudly** (selector timeout, failed
outcome assertion) stays broken until a human edits the script — every
such run is counted above as a maintenance event (DOM total:
8). A DOM script that drift breaks
**silently** (wrong-action rows) is worse: it needs the same human edit
plus someone noticing the bad writes first, and every run until then
mutates the wrong record.

The compiled bundle is never hand-edited: cosmetic drift is absorbed by
the resolution ladder (heals), and non-absorbable drift ends in a safe
halt with an accurate report. That is not free either — a persistently
halting bundle needs a fresh one-minute demonstration, or an agent
fallback (see the hybrid benchmark) — but it fails closed, and the
recovery path does not involve reading someone else's selector code.

## Variant analysis — the selector variants, measured and unmeasured

The one genuinely ambiguous step is opening the referral, and the two
readings of it are both benchmarked as arms above ("first row" vs "the
row named `Jane Sample`"). On hardcoding: an earlier draft of this
report dismissed the name-filtered variant because "the patient becomes
a hardcoded constant" — that framing was asymmetric and is retracted.
**The compiled arm hardcodes the same constant**: its recorded identity
band embeds "Jane Sample Knee pain referral High" and every replay
checks the live row against it before clicking. Both identity-keyed
approaches encode the demonstrated patient; they differ in HOW the
identity got captured (demonstration vs hand-authored selector) and in
failure semantics (fail-closed halt vs fail-closed timeout), not in
whether the patient is encoded. The positional variant is the one that
encodes no identity at all — and the wrong-action column shows what
that costs.

Unmeasured variants, for completeness:

- Keying to the app's DOM id (`#open-p1`) would behave like the
  name-filtered arm on this matrix (`p1` IS the patient identity, as a
  database key instead of a display name) — id-in-selector is what
  Playwright's guidance steers away from, and display names are the
  more maintainable spelling of the same choice.
- Nothing in the selector toolbox fixes `notice`, `reqfield`, or
  `modal-once` without a human adding new steps — the same conditions
  that halt the compiled replay.

## Methodology

- **Record + compile once** (compiled arm only): the demo is recorded via
  the Playwright demo driver and compiled into a vision-anchored bundle;
  one-time, excluded from per-run latency, same as every other benchmark
  here. The DOM arms need no demonstration — a human wrote them from the
  task spec instead (~the same one-off effort, different skill).
- **Identical environments.** Every run of every arm gets a fresh
  chromium (1280x800, deviceScaleFactor=1) against the same locally
  served MockMed app; drift is injected via `?drift=` query flags, so
  conditions are exactly reproducible.
- **Different interfaces, deliberately.** The compiled arm is
  vision-only (screenshots in, pixel clicks out). The DOM arms drive the
  page through selectors — that IS the comparison. The two DOM arms
  differ in exactly ONE selector (positional vs name-filtered referral
  row), isolating spec phrasing from everything else.
- **Same success criterion, implemented once.** `verify_final_state` on
  the final screenshot: OCR must find the `Encounter saved` banner AND a
  `Triage — <note>` row AND the right patient's name
  (`Jane Sample`), and this run's note must not sit in a wrong-type
  row. Neither arm's self-report is used. This is the hybrid benchmark's
  own check (`verify_hybrid_final`), reused — not a reimplementation.
- **Wrong actions measured for both arms.** The final-state identity
  check flags saves that landed on the wrong patient or wrong encounter
  type, whichever arm produced them.
- **Wall-clock** is measured around the replay / script only (browser
  and server startup excluded for both arms). DOM failures burn
  Playwright's standard 30 s auto-wait timeout before erroring; that
  cost is included, because an unattended script pays it too.
- **$0 and deterministic.** Neither arm makes a model call. MockMed's
  drift hooks are deterministic; OCR on identical frames is
  deterministic. (One known nondeterminism: under `grow`, which template
  rung fires first in the compiled arm is rendering-dependent — both
  safe outcomes are reported as measured.)

## Caveats — read before quoting these numbers

- **This arm only exists on browser backends.** That is not a footnote;
  it is the boundary of the whole comparison, and it cuts both ways. On
  desktop apps, VDI/Citrix, or anything rendered as pixels without an
  inspectable DOM, there is no selector script to write — the incumbent
  comparison is unavailable there, and the vision ladder is the only one
  of the two that runs at all. Conversely, wherever a stable DOM exists,
  the numbers above are the honest baseline the ladder has to beat.
- **MockMed is our own app**, small and clean; its accessibility (proper
  labels, roles) is BETTER than much real-world markup, which flatters
  the DOM arm's selector stability. Real EMRs bury controls in iframes
  and div-soup; both arms would degrade, plausibly at different rates.
- **The drift menu is ours too.** The schedule's three conditions were
  chosen (by the hybrid benchmark) because they halt the compiled arm;
  the perturbation modes come from the validation suite. Neither set was
  chosen to flatter or sabotage the DOM arm — it never ran against any
  of them before this benchmark — but a different drift mix would move
  the totals.
- **n = 1-2 per perturbation cell.** The hooks are deterministic, so
  these are existence results by design, not rates.
- Single machine (macOS-15.7.3-arm64-arm-64bit); local server; no network.

## Reproduce

```
.venv/bin/python -m openadapt_flow.benchmark.dom_arm --out benchmark/dom --n-per-perturbation 2
```

(`--n-per-perturbation 2` matches the committed results.) No API key
needed; nothing here spends money.
