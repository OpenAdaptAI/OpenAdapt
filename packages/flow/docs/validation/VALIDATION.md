# Adversarial validation — failure-mode matrix

Date: 2026-07-08 (initial audit and same-day fix); updated 2026-07-09
(postcondition-mining fix + live re-run); updated 2026-07-10 (identity
matcher rebuilt after the THIRD wrong-patient reopening — near-name
siblings — against a frozen held-out adversarial corpus; see the bounded
public summary in [IDENTITY_EVIDENCE.md](IDENTITY_EVIDENCE.md)). This document is the
result of deliberately trying to break compiled replay before anyone else
does. Every experiment ran with **zero model calls and $0 of API spend**:
compiled-replay only, no grounder, no agent arm. Failures found here are
the point of the exercise; nothing is softened.

## Fix update (2026-07-08, `feat/fix-wrong-actions`)

The initial audit found **6 wrong-actions (5 silent)** across two root
causes. Both are now fixed; the tables below carry **before → after**
columns and the characterization tests pin the new behavior.

1. **Identity-blind target resolution** → **pre-click identity check**
   (`openadapt_flow/runtime/identity.py`). The compiler now records each
   click target's *context band* — the OCR text of the click point's own
   text row, excluding the target's own crop (labels stay mutable/
   healable) and timestamp-bearing lines (volatile); bands shorter than
   12 squashed characters are too generic to discriminate and are not
   recorded. Before every click (including an anchored TYPE step's
   focusing click) the replayer re-reads the resolved point's own row and
   matches it token-wise, order-insensitively (OCR re-reads the same band
   in different segmentation orders between visits): matched tokens must
   cover >= 0.8 of the recorded band AND no contiguous run of uncovered
   recorded characters may exceed 4 — a wrong entity is a contiguous
   mismatch (a replaced name), so long shared row text cannot buy it a
   pass. Measured: true row 1.0 (still 1.0 under injected per-character
   OCR jitter, via a 0.7 whole-token similarity tier — REMOVED
   2026-07-10: that tier verified near-name siblings, the third
   wrong-patient reopening; see that fix update below); look-alike row
   sharing all non-name columns ~0.67 coverage with a 10-char uncovered
   name run. When a workflow parameter's demonstrated value is embedded
   in the recorded band (a parameterized *target*, e.g. the patient row),
   the run's value is substituted into the recorded band and the WHOLE
   substituted band must match — a row that merely mentions the run's
   value does not verify. On mismatch: safe-halt *before* the click, with
   the expected and observed band text in the error. On an unreadable
   band (OCR found nothing, retried at 2x resolution first): reversible
   steps proceed with the step flagged (`StepResult.identity.status ==
   "unreadable"`); steps marked irreversible at compile time refuse (risk
   is opt-in via `compile_recording(risk_overrides=...)` — never
   auto-assigned; see docs/LIMITS.md for what that means by default).
   This is a pre-action check against runtime values — the commit-8421d51
   rule (never bake a parameterized value's rendering into compiled
   *postconditions*) still holds untouched.
2. **Unverified typed input** → **typed-input verification**
   (`Replayer._verify_typed_input`). After every TYPE action, the browser
   backend reads the exact focused editable value; other substrates use OCR
   over the field region (around the focusing click; whole frame when focus
   was moved by keyboard; 2x-resolution retry). A pixel change alone is
   accepted only for steps explicitly declared `secret`, and only when the
   region gained no other readable text — the masked-field (password dots)
   shape — so an ordinary unreadable field or a dialog rendering over the
   field cannot false-verify while keystrokes fell elsewhere. When nothing
   changed at all: ONE refocus-and-retype retry (re-click the field, select-all so a
   false-negative first attempt is replaced rather than duplicated,
   retype), then safe-halt. When the region changed but the value is
   unreadable: immediate safe-halt WITHOUT retyping (retyping into an
   unknown render state could destroy pre-existing field content). The
   typed value is verified at *runtime*; nothing is baked into the
   bundle.

The identity matcher and typed-input verifier above describe their
**2026-07-09 hardened form** (adversarial review of the initial fix):
the initial char-coverage matcher verified a wrong entity when shared
row text dominated the band (coverage 0.89), verified generic bands
("Active High 3" vs "Active High 7" at 0.91), let ANY short embedded
param value disarm the band check entirely (wrong patient at 1.0 with
`priority="High"`), verified any row containing the run's value, spanned
2-3 table rows (one-row-off text bleed), and its order-sensitive scoring
false-aborted a correct OpenEMR modal target at 0.66 when page chrome
re-read in a different order. All six are pinned in
`tests/test_identity.py` / `tests/test_replayer.py`.

**After the fix: 0 wrong-actions across every case the suites pin** — the
six audit reproductions (tables below), the six review probes above, and
the perturbation/chaos/primitive matrices. Every previously-silent case
now ends in a safe-halt before the wrong action, or (steal-focus)
recovers and completes correctly. The claim is scoped to exactly those
pinned cases: the dangerous list in docs/LIMITS.md (zero-postcondition
steps, label-only and too-generic-band targets, unreadable bands on
default-compiled — reversible — steps) remains open and is NOT covered
by it. Residual gaps are listed honestly under "Failure modes ranked by
severity".

**False-abort cost of the fix: none measured** (initial fix; the
2026-07-09 hardening additionally FIXED a measured false abort — the
OpenEMR modal-band order-sensitivity above).

**Live false-abort re-check of the hardened thresholds (2026-07-09,
OpenEMR public demo, fake patients, $0, 0 model calls; public-demo
courtesy: 4 sessions total — 1 fresh record+compile, 3 paced replays).**
The fresh bundle armed **4 of 12** click steps with (row-refined)
identity context. Across 3 replays: **6/6 identity evaluations verified
at coverage 1.0** (the top-menu band and the patient-search-result row) —
**zero identity false-aborts from the tightened thresholds** — and **9/9
typed inputs verified** (username, masked password, parameterized note),
zero retries. All 3 replays then safe-halted at the SAME pre-existing,
identity-unrelated point: the Messages-card pencil click (step_013, an
UNARMED step — a tiny generic icon anchor with no row text). The
closed-loop scroll's anchor probe accepted a look-alike pencil above the
fold (the dashboard has one per card), the dashboard never scrolled, the
geometry click missed, and the step's postconditions caught it — an
honest halt in the documented P2 anchor/instance-state class (that day's
post-reset dashboard content differed from the 2026-07-08 regression,
where the same flow reached the note dialog 3/3). Nothing was written on
any run.

- MockMed local benchmark, compiled arm end-to-end (fresh recording,
  arm-independent OCR verification of the final screen): **30/30 clean
  replays** and **3/3 `drift=theme` replays** (all heals + context
  refreshes verified), mean 6.8 s per clean run, 0 model calls.
- The full e2e matrix (baseline x3, params, viewports, theme/move/rename
  healing incl. healed-bundle re-replay, slow renders, CLI smoke) stayed
  green — the identity gate and typed-input verification changed no
  happy-path outcome.
- OpenEMR live regression (public demo, fake patients, 1 fresh recording +
  3 replays paced >= 35 s, $0; measured with the INITIAL matcher): the
  fresh bundle armed **7 of 12** click steps with identity context (the
  rest have no out-of-crop row text — login button, patient-chart link,
  pencil icon). Across 3 replays, **all 18 identity evaluations verified**
  at coverage 0.95–1.00 on real dense EMR rows (patient-result row, menu
  bar, dialog chrome) — **zero identity false-aborts** — and **9/9 typed
  inputs verified** (username, masked password, parameterized note) with
  zero retries needed. (Separately, live runs by the postcondition-mining
  work later reproduced an identity FALSE ABORT on the note-dialog
  textarea band under the initial ORDER-SENSITIVE matcher — 2/2 control
  replays, score ~0.66 — which the 2026-07-09 order-insensitive matcher
  fixes; that band shape is pinned in `tests/test_identity.py`.) All 3
  replays later aborted at the note-dialog step on a PRE-EXISTING, already
  documented defect: the mined `text_present ':01'` postcondition (the
  Track D "stray `:01` that slipped past the timestamp filter" —
  postcondition mining fragility, P2 below, deliberately out of scope of
  this fix — **fixed in the 2026-07-09 update that follows**). Same-day
  record→replay was enough for that timestamp fragment to leave the
  screen. The identity/typed-input layers behaved exactly as intended
  before the unrelated halt.

## Fix update (2026-07-09, `feat/fix-postcondition-mining`)

The audit's P2 "assertions overfit" and P3 "vacuous successes" findings —
and the `text_present ':01'` false-halt that aborted all three 2026-07-08
OpenEMR regression replays — are addressed by making postcondition mining
**select for stability, not novelty**:

1. **Volatility classifier** (`openadapt_flow/volatility.py`, shared by the
   compiler and the identity-context extractor). TEXT_PRESENT candidates,
   geometry landmarks and identity-band lines are rejected when they carry
   a clock time (including bare `':01'`-class OCR fragments, which slipped
   past the old pattern), a date NEAR the recording date (content
   chronology: log rows, "last updated" chrome), digit/punctuation-dominated
   text (counters, badges) or low-entropy noise. A date FAR from the
   recording date — a DOB in a patient banner — is deliberately **kept**:
   the old blanket timestamp filter ate identity banners because a DOB
   looks like a date, which is exactly the mechanism that left the MockMed
   wrong-patient rows without an identity-bearing assertion.
2. **Empirical stability check.** A candidate that appears in a step's
   after frame but is gone (or changed) by the NEXT step's before frame —
   two captures of the same screen, moments apart — is volatile by
   demonstration (toasts, spinners, ticking counters) and never asserted.
   The same rule drops REGION_STABLE postconditions whose region
   self-mutates between the two captures.
3. **Ranking prefers semantics over length.** "Longest new text" is gone;
   surviving candidates are ranked by alphabetic content with a proximity
   tiebreak toward the click target (text near the action is likelier to
   describe its effect than a distant data row).
4. **Structural fallback postconditions.** Steps that mined nothing visual
   (identical before/after frames — a new-tab click, an off-screen SPA
   route change) now assert URL_CHANGED / TITLE_CHANGED / NEW_TAB_OPENED
   when the recorder captured the backend's structural observations
   (Playwright exposes URL / title / page count). Nothing instance-specific
   is baked in — the replayer compares the step's END state to its own
   START state. Steps with no structural change either stay honestly
   vacuous, and on a backend that cannot observe the property the
   postcondition passes honestly-unverified rather than false-halting.
5. **Parameter hygiene.** Demo parameter values are excluded from geometry
   landmarks (on OpenEMR the save step's landmark used to be the recorded
   note text itself), and a compile-time lint fails compilation loudly if a
   demonstrated parameter value appears in any **text** postcondition or
   any landmark's OCR text outside the designated slots (`workflow.params`,
   a parameterized TYPE step's recorded example, anchor resolution/identity
   evidence — which re-anchors on the run's value at replay). Scoped
   precisely: the lint reads text evidence only. A later step's
   REGION_STABLE template can still embed the demo value's rendered
   *pixels*; that is a false-halt-direction gap (safe, costs availability),
   listed under known remaining.

Concrete before → after, same recordings:

- MockMed chart-open step: before `text_present 'No encounters yet.'`
  (patient-agnostic — the DOB banner was eaten by the timestamp filter);
  after `text_present 'JaneSample—MRNP1—DOB1980-01-01'` (the identity
  banner, DOB included).
- OpenEMR fresh bundle (2026-07-09, live demo): every mined TEXT_PRESENT
  is chrome or card/table headers — the main menu bar, `Treatment
  Intervention Preferences`, `Last update` (the column header, where the
  old miner grabbed a message row's timestamp), `Showing 1 to 1 of 1
  entries…`. No timestamps, no `':01'` fragments, no note text, no other
  patients' rows. Per-step list below under Track D.
- Verification: full unit + e2e matrix green (387 tests as of the 2026-07-09
  validation snapshot, after the review hardening below and the merge with the
  `feat/fix-wrong-actions` review hardening, including the
  perturbation/chaos/primitives characterization suites, re-run whole; the
  suite has since grown to 864 collected — see README "Status"); OpenEMR live
  check under Track D below
  (2026-07-09 re-run).

**False-abort cost of this fix: none measured** — the MockMed baseline,
params, viewport, healing and slow-render scenarios all stayed green, and
the theme-drift heal showcase is unaffected.

**Exposed by this fix (pre-existing, disclosed):** with the `':01'` halt
gone, the live OpenEMR control replays now run 2 steps further and hit a
reproducible **identity-layer false abort** at the note-dialog textarea
click (step_014, 2/2 control replays): the recorded context band
(`'PPV + Show All <Back to Patient ShowActive'` — background chrome read
*through* a modal dialog, including an OCR-garbage token) never reproduces
under live OCR segmentation (`'+Add BacktoPatient Show All ShowActive'` —
same tokens, different segmentation/order), and the in-order coverage
matcher scores it ~0.66 < 0.8. Zero wrong actions — the halt is safe — but
it caps the control run at 14/17. This is the `feat/fix-wrong-actions`
identity band's matcher (order-sensitive `difflib` block coverage), not
postcondition mining; it was previously unmeasurable because every replay
aborted earlier on `':01'`. It was left un-touched by the mining fix
deliberately (the coverage semantics carried measured safety margins —
look-alike row = 0.70, threshold 0.8 — that an order-insensitive rewrite
would need to re-validate). **RESOLVED 2026-07-09**: the
`feat/fix-wrong-actions` review hardening did exactly that rewrite —
token-wise order-insensitive matching with a re-validated look-alike
margin (~0.67 coverage plus a 10-char uncovered run vs. the 0.8 AND
<=4-run thresholds) — and the permuted modal-band shape above is pinned
verified at 1.0 in `tests/test_identity.py`.

### Review hardening (2026-07-09, same branch)

Review of this fix produced verified classifier evasions — all fixed, all
pinned as unit tests in both directions (`tests/test_volatility.py`):

- **Month-name dates** classified as stable: `Jul 8, 2026`, `08 Jul 2026`,
  `Updated Jul 8`, `July 2026`, `Wednesday July 8` all evaded the
  numeric-only date pattern. Concrete risk: OpenEMR's post-login screen is
  a calendar — a mined `July 2026` header false-halts every replay the
  next month. Month-name fragments now feed the same near/far split as
  numeric dates; a month-day with no year (`Updated Jul 8`) recurs
  annually and is always volatile; a month-name DOB (`Jan 1, 1980`) is
  kept as identity data, exactly like the numeric form.
- **Relative-time phrases**: `3 min ago`, `2 hours ago`, `just now`, and a
  standalone `Yesterday`/`Today`/`Tomorrow` (message-list group headers)
  are volatile; embedded day-words in stable chrome ("Today's
  Appointments") are kept.
- **Counts and pagination**: `56 total entries`, `1 to 1 of 1`,
  `5 new messages` — and `Page 2 of 9`, previously pinned as *stable* by a
  unit test, reclassified: pagination position is navigation state that
  changes with data volume on shared instances, not identity.
- **Badge counters**: `Inbox (2)`, `Messages (14)` — a parenthesized bare
  integer is a live counter decorating an otherwise-stable label
  (strip-and-test: if removing it leaves the classification unchanged, the
  composite is rejected; the label alone remains minable).
- **European dot-clocks**: `Last updated 18.38`. Only unambiguous forms
  count (two-digit hour in valid range, an am/pm suffix, or a time-context
  word) so version/section numbers survive: `v2.0`, `v2.10 changelog` and
  `Version 2.10 release notes` are pinned stable.
- **Heal-time band refresh dropped DOB lines**: `_recontext` passed no
  reference date, so every date-bearing line was conservatively dropped
  from a healed anchor's refreshed band — including the DOB, the band's
  most discriminative identity evidence. It now anchors the near/far split
  on the heal date (`date.today()`), the exact analogue of the recording
  date at compile time.

The `Showing 1 to 1 of 1 entries (filtered from 56 total entries)` banner
the 2026-07-09 fresh bundle mined (step_007 below) is a case study in why
counts had to go: reviewer-measured, the per-line fuzzy matcher (0.8)
scores `0 to 0 of 0 entries` at **0.95** against it — the exact
empty-result state the assertion should catch PASSES — while a missing
`(filtered from …)` suffix scores 0.62 and false-halts. **Fuzzy matching
on digit-differing lines is a known remaining weakness: a one-digit count
difference scores above the 0.8 threshold**, beneath the matcher's
resolution. The fix here is upstream, not a matcher redesign: that banner
now classifies as `count` (pinned by test, spaced and OCR-squashed forms
both) and can never become an assertion.

Known remaining, documented here deliberately (not attempted):

- **Long-line anchors are OCR-segmentation-fragile at the resolution
  rung**: `find_text` fuzzy-matches whole OCR lines with no multi-line
  joining, so a long anchor `ocr_text` the engine re-segments differently
  can miss the OCR rung and degrade resolution to geometry. (The
  *assertion* side of this mechanism was fixed by `feat/fix-wrong-actions`:
  TEXT_PRESENT/ABSENT checks go through the segmentation-tolerant
  `vision.text_present` — whole-line ratio OR a contiguous >=0.8-of-target
  run across concatenated lines, merged/split re-reads exercised against
  the real engine in `tests/test_vision.py`.)
- **Structural checks pass as honestly-unverified on a transient None**
  (URL/title/page-count read None on either side), even on backends that
  are normally structural.
- **NEW_TAB_OPENED false-halts on named-window reuse**: re-targeting an
  existing named window navigates it without increasing the page count.
- **The persistence check has no coverage on the recording's final step**
  (no next before-frame exists): a final-step toast can still be asserted.
- **REGION_STABLE templates can embed rendered parameter pixels** (see the
  lint scope note above) — false-halt direction only.

## Identity-verification hardening (bounded public summary)

Adversarial review repeatedly found false-accept classes in the target-context
identity gate. The production response was structural: armed actions require
fresh identity evidence, ambiguous OCR-only identifiers abstain, and the runtime
halts when a plausible sibling cannot be excluded. The public mechanism and
conservative defaults are pinned by boundary tests.

The bounded aggregate evidence covers 6,900 synthetic string pairs plus 18
review probes: 0 measured false accepts, a 48.31% false-abort rate, and 100%
abort on deliberately indistinguishable pairs. A separate rendered/OCR
dense-surface exercise measured 26/360 false accepts before pixel-identity
hardening and 0/360 after it. See [IDENTITY_EVIDENCE.md](IDENTITY_EVIDENCE.md).

Those numbers are not a universal reliability claim: the operating point was
selected using the same synthetic evidence, the validation is not independent,
and prior zero measurements were reopened by new adversarial classes. The raw
corpora, grids, reviewer recipes, and deployment-derived tuning are maintained
privately; product qualification requires workflow-specific identity and effect
evidence.

## Outcome vocabulary

- **pass** — the run succeeded and did what the demonstration did.
- **safe-halt** — the run stopped with an accurate report and took no
  further actions. A *false abort* is a safe-halt whose cause was cosmetic
  (the task could have continued); it costs availability, not safety.
- **wrong-action** — the run executed an action on the wrong target or
  wrote incorrect state. *Silent* wrong-actions additionally reported
  success. **This is the critical class.**
- **crash** — an unhandled exception. (None occurred anywhere.)

## Headline numbers

Initial audit — **wrong-actions: 6**, of which **5 were silent** (wrong
state written AND the run reported success; 4 of the 5 silent modes
reproduced on every platform, while the `drift=grow` wrong-patient
outcome was observed on the recording platform and is
platform/rendering-dependent — see row 3). After the fix — **0**:

| # | case | before (audit) | after (fix) |
|---|---|---|---|
| 1 | `drift=lookalike` | **silent wrong-action** — saved to the look-alike patient | safe-halt before the click (identity coverage ~0.67 < 0.8, 10-char uncovered name run) |
| 2 | `drift=missing` | **silent wrong-action** — saved to the neighbouring patient | safe-halt before the click (coverage 0.00) |
| 3 | `drift=grow` | **silent wrong-action** — saved to the imposter at the recorded position | safe-halt before the click (coverage 0.00); on platforms where the global rung finds the true row first, a verified save to the CORRECT patient |
| 4 | chaos `delete-target-row` | **silent wrong-action** — saved to the patient that slid into place | safe-halt before the click (coverage 0.00) |
| 5 | chaos `steal-focus` | **silent wrong-action** — empty note saved, green report | **recovers**: refocus-and-retype retry lands the note; run completes with the CORRECT note (a failing retry safe-halts) |
| 6 | `sort-reorder` | wrong-action (caught) — wrong row clicked, state written, then halted | safe-halt before ANY click; app state untouched |

- **Safe-halt rate on the remaining perturbations/faults: 100%** — every
  non-silent failure halted with an accurate report; postconditions never
  produced a false success outside the wrong-action cases above; there were
  **zero crashes** across all tracks, before and after the fix.
- OpenEMR (live, public demo): 0 wrong-actions in 4 replays; the patient
  parameterization and cross-instance runs both ended in safe-halts whose
  mechanisms are findings in their own right (below).

All MockMed experiments are automated as characterization tests —
`tests/e2e/test_perturbation.py`, `tests/e2e/test_chaos.py`,
`tests/e2e/test_primitives.py` — so the failure modes are pinned: if one of
those tests ever fails, a documented failure mode changed (was fixed, or got
worse) and this document must be updated. OpenEMR experiments are driven by
`scripts/openemr_param_depth.py` (paced, fake patients only, not in CI).

Evidence pointers below refer to local, gitignored artifacts under
`runs/validation/` (full per-step screenshots, reports, healed bundles).
The pytest suites regenerate equivalent artifacts under their run dirs.

## Track A — perturbation/drift matrix (MockMed)

One recording (1280x800, default theme), replayed under one perturbation at
a time. Automated in `tests/e2e/test_perturbation.py`.

| perturbation | before → after | detail |
|---|---|---|
| baseline | pass | 8/8 anchors on the `template` rung, 0 heals |
| viewport 1440x900 | pass | layout is left-anchored; nothing moved |
| viewport 1024x768 | pass | same |
| viewport 900x360 (target below fold) | safe-halt (false abort) | halted at the note-field step: its REGION_STABLE region extends past the short viewport. The save button was off-screen and unreachable anyway — no scroll was demonstrated, and closed-loop scrolling only extends *recorded* scroll steps |
| device scale factor 2 | safe-halt (false abort) | halts at step_000: screenshots are 2x the coordinate space; template scale ladder tops out at 1.18x; OCR returns frame-pixel coordinates that no longer equal input pixels |
| CSS zoom 125% (`drift=zoom`) | safe-halt (false abort) | halts at step_000 for the same family of reasons |
| font size 16px→19px (`drift=font`) | safe-halt (false abort) | halts at step_000: REGION_STABLE phash cannot tolerate reflowed glyph metrics. Theme drift heals (README showcase); font drift yields 0% replayability |
| data growth (`drift=grow`, 4 rows added above target) | **wrong-action, silent → safe-halt** | before: local template matched the imposter row at the recorded position (≥0.985 despite different reason/priority text in-crop); encounter saved to `#patient/g1`, run reported success. After: identity band reads `Pat Placeholder Orthopedics intake Low` where `Jane Sample Knee pain referral High` was recorded (coverage 0.00) — halt before the click, nothing saved. Where the global rung finds the true row first (platform-dependent), the run instead saves to the CORRECT patient with identity verified. Audit evidence: `runs/validation/track-a/run-grow/` |
| look-alike row (`drift=lookalike`) | **wrong-action, silent → safe-halt** | before: a row with the same reason/priority directly above the target is pixel-identical inside the 160x64 crop (the NAME column is outside it); template rung confidence 1.0; saved to `#patient/p0`. After: the band's NAME text disagrees (`Taylor Duplicate ...`, coverage ~0.67 from the shared columns, below the 0.8 bar, with the replaced name a 10-char contiguous uncovered run) — halt before the click. Audit evidence: `runs/validation/track-a/run-lookalike/` |
| target row deleted (`drift=missing`) | **wrong-action, silent → safe-halt** | before: the neighbouring row occupies the recorded position and every rung that fires resolves to it; saved to `#patient/p2`. After: band reads `Alex Testcase Cardiology follow-up Medium` (coverage 0.00) — halt before the click, never click a look-alike. Audit evidence: `runs/validation/track-a/run-missing/` |
| empty list (`drift=empty`) | safe-halt | halts one step early: the sign-in step's postcondition asserts a data row's text as an "invariant" — before the 2026-07-09 mining fix `Cardiology follow-up`, after it a patient name from the task list (`Sam Specimen`): stability-selected mining rejects volatile fragments but cannot know a persistent data row from chrome, so this mechanism remains (disclosed under "known remaining") |
| slow renders 4s (`drift=slow`) | pass | postcondition polling + ladder retry absorb it (~20s run) |
| slow renders 12s (`drift=slow&slowms=12000`) | safe-halt | accurate report at the sign-in step (~5.5s postcondition window exceeded) |

The three scale-drift rows above (dsf, zoom, font size) are mapped out in
full — every zoom 80–200%, DPI 1–3x, font-size, and font-family point, plus
the realistic combinations — in [`benchmark/cosmetic_drift/COSMETIC_DRIFT.md`](../../benchmark/cosmetic_drift/COSMETIC_DRIFT.md)
(harness: `benchmark/cosmetic_drift/sweep.py`; pinned by
`tests/e2e/test_cosmetic_drift.py`). Headline: the envelope is "recorded render
only" for scale, font-family substitution is absorbed, and the whole sweep
fails **safe** — 0 wrong-actions in 21 points.

Why the silent wrong-patient saves got through, end to end (audit analysis
— items 1 and 2 still describe the template/postcondition layers; item 3
is where the fix landed):

1. The discriminative evidence for a table-row button (the patient name)
   sits **outside** the template crop, and a strict-looking 0.985 template
   threshold does not separate "same button, different row" (in-crop text
   differences moved the score by less than 1.5%).
2. ~~The compiler's timestamp filter — added for OpenEMR, correctly — drops
   the patient banner from the click step's postconditions because the DOB
   ("1980-01-01") matches a date pattern. The surviving "new text"
   postcondition is the patient-agnostic `No encounters yet.`~~ → fixed
   2026-07-09: the volatility classifier's near/far date split keeps a
   date FAR from the recording date (a DOB) as identity data — the chart
   step now asserts `JaneSample—MRNP1—DOB1980-01-01` itself.
3. ~~Nothing downstream re-verifies identity~~ → the pre-click identity
   check now re-verifies the target's row text against the recorded
   context band, and halts before clicking when it disagrees.

## Track B — mid-run fault injection (MockMed)

State sabotaged *between* steps of a live replay via a wrapping backend.
Automated in `tests/e2e/test_chaos.py`.

| fault (injection point) | before → after | detail |
|---|---|---|
| target row deleted after sign-in | **wrong-action, silent → safe-halt** | mid-run twin of `drift=missing`. Before: saved to `#patient/p2`, reported success. After: identity band mismatch (coverage 0.00) halts before the click; nothing saved. Audit evidence: `runs/validation/track-b/run-delete-target-row/` |
| opaque modal before save | safe-halt | all rungs fail; ladder retries to the step timeout; aborts naming the save step; **no click fired into the overlay** |
| invisible click-shield before save | safe-halt | vision sees an unchanged screen, resolves correctly, clicks into the shield; nothing happens; postconditions abort. One neutralized click; no state written |
| Triage/Consult buttons swapped | pass (healed) | labels differ, so lower rungs re-locate the true target; the saved encounter has the correct type. Identical-looking swaps are the look-alike case above — those go wrong |
| focus stolen between click and type | **wrong-action, silent → recovered** | before: keystrokes fall on `<body>`; the encounter is saved with an **empty note** and the run reports success (TYPE steps were never verified, and parameterized values are *by design* excluded from every postcondition). After: typed-input verification sees the field region unchanged, re-clicks the field, retypes once, confirms the text landed — the run completes with the CORRECT note. A retry that also fails safe-halts. Audit evidence: `runs/validation/track-b/run-steal-focus-before-type/` |
| app navigates away before save | safe-halt | postconditions fail on the wrong screen; nothing typed or saved |
| save button renamed mid-run | pass (healed) | geometry rung resolves via unchanged landmarks; anchor healed; saved correctly |

## Track C — interaction-primitive taxonomy

One tiny demo per primitive against
`openadapt_flow/mockmed/static/widgets.html` (one control per `?panel=`).
Automated in `tests/e2e/test_primitives.py`. Exploration evidence:
`runs/validation/track-c/summary.json`.

| primitive | verdict | evidence (one line) |
|---|---|---|
| button / link click | supported | entire MockMed + OpenEMR corpus |
| checkbox / radio | supported | replay reproduces `Consent yes, priority Urgent.` |
| DOM modal dialog (open/confirm) | supported | replay reproduces `Survey response recorded.` |
| typeahead, fixed value | supported | type prefix + click suggestion replays exactly |
| typeahead, **parameterized** value | **partial / hazard (still open)** | recorded suggestion anchor can't match; geometry clicks whatever sits at the first-suggestion **position** — correct here by coincidence, unverified by construction (the status text embeds the parameter, so the compiler excluded it from postconditions). The identity fix does NOT arm here: the suggestion's only discriminative text is its own label (excluded as mutable evidence), and its row carries no other text — `context_text` compiles to None. Re-verified after the fix: still picks `Bob Baker` by position |
| table pagination (as demonstrated) | supported | Next → pick on page 2 replays |
| sorting that reorders targets | **wrong-action (caught) → safe-halt before any click** | before: replay vs `?presort=desc` clicked the wrong row and wrote its pick into app state before the postcondition halted. After: the identity band of the resolved row (`Echocardiogram North`) disagrees with the recorded row (`Basic metabolic panel South`) and the run halts with app state untouched. Reorder is still *unsupported* (the true row is never found) — but now safely |
| keyboard-only flow (Tab/type/Enter) | supported | `Request submitted for Rivera on ward North.` replays |
| native `<select>`, mouse | unsupported | the dropdown popup is browser chrome — it never appears in page screenshots (predicted in FINDINGS.md, confirmed) |
| native `<select>`, arrow keys | **hazard** | inert in this harness (macOS headless): recording changed nothing — no visual change AND no structural change (URL/title/tab count all static), so even the 2026-07-09 structural fallback has nothing to assert; the steps compile with **zero postconditions** and replay is a **vacuous success**, disclosed |
| native `<select>`, type-prefix + Enter | supported (workaround) | `Species set to Dog.` replays — the keyboard fallback FINDINGS.md predicted |
| native date input, typed digits | **partial / hazard → platform-shaped, fail-closed** | segment-, locale-, AND renderer-dependent. macOS shape: typing `07082026` produced value `70820-02-06` *at record time*. Linux shape: the widget ignores the digits entirely. Exact browser value readback now SAFE-HALTS both at the type step because neither field value equals the intended digits; ordinary fields cannot use the secret-only masked-pixel fallback. No wrong date value is written; the calendar popup remains invisible browser chrome |
| iframe-heavy pages | supported | OpenEMR: 6+ nested iframes, modal-in-iframe-in-modal — vision-only replay never noticed (docs/showcase-openemr/FINDINGS.md) |
| new tab / `target=_blank` | **unsupported / silent → verified structurally (2026-07-09)** | the single-page frame never shows the new tab and before/after frames are identical, but the recorder now captures the backend's page count and the compiler mines a `NEW_TAB_OPENED` fallback postcondition — the step fails at replay if no tab actually opened. What happens INSIDE the new tab is still unobserved (single-window scope) |
| file upload | unsupported (structural) | the native file chooser is OS chrome; neither recordable nor drivable vision-only |
| drag-and-drop | unsupported (structural) | no DRAG in `ir.ActionKind`; the recorder has no drag API |
| conditional branching / loops | unsupported (structural) | `Workflow.steps` is a linear list; a demonstration cannot express if/else, and replay cannot skip or repeat steps |
| multi-window coordination | unsupported (structural) | `Backend` wraps exactly one page |

## Track D — parameterization depth (OpenEMR, live public demo)

Driver: `scripts/openemr_param_depth.py` (fresh browser per run, ≥30s
pacing, fake demo patients only; 8 demo sessions total including probes).
Question: the shipped benchmark parameterizes the note TEXT; what happens
when the PATIENT — a value that changes screen content — is a parameter?

| run | outcome | detail |
|---|---|---|
| `patient=Phil` (control) | pass | 18/18, 39.1s, 0 model calls, note verified on final screen |
| `patient=Susan` | safe-halt (false abort) | typed "Susan", results showed only "Underwood, Susan Ardmore"; the row click fell to the **geometry rung** (landmarks = column headers) and clicked the demonstrated **position** — which was Susan, the right patient. The chart opened, then the run aborted: step_008 asserts `No treatment intervention preferences recorded.`, a **Phil-dashboard state** baked in as an invariant. Evidence: `runs/validation/track-d/runs/run-susan-drift/` |
| cross-instance `/a/` | environment | `/a/` rejected its own published admin credentials that day (verified with DOM selectors — not a replay artifact). The replay executed the login correctly and safe-halted with an accurate report. Note: this probe was ad hoc — the committed script's `cross-instance` mode targets `/b/` only, so the `/a/` observation is not reproducible from the committed code |
| cross-instance `/b/` | safe-halt (false abort) | login **succeeded**, but the run halted at the login step's postconditions: `/b/` runs a different module set (no "Inventory" menu entry) and different calendar content, and both the menu-text assertion and the calendar REGION_STABLE were recorded on the main instance. Same version, different instance state → no transfer. Evidence: `runs/validation/track-d/runs/run-cross-instance-b/steps/step_004_after.png` |

Cross-VERSION drift was not testable: all public demo instances run
OpenEMR 8.0.0. Said plainly and skipped.

**The parameterization-depth answer (audit):** a parameter that changes
screen content is *position-bound and unverified*. Three interlocking
mechanisms (mechanism 1's "nothing verifies the identity of the clicked
row" is what the fix addresses: when the recorded row's context band embeds
the parameter's demo value, the pre-click check now requires the **run's**
value in the live band — position-resolved parameterized targets are no
longer click-blind, they must name the run's entity or the run halts):

1. **Anchors bind to the demonstrated value's pixels.** "Susan" cannot
   match a template/OCR anchor recorded on "Belford, Phil"; resolution
   degrades to geometry, which encodes *where* the demonstrated row was,
   not *which* row it is. With a single search result that is coincidentally
   correct; with multiple results it is the MockMed wrong-patient scenario
   on a real EMR — nothing verifies the identity of the clicked row.
2. **Parameterizing a value deletes its safety net.** The compiler
   (correctly) refuses to assert parameterized values — but that also
   stripped `Patient Messages for Belford, Phil` from the bundle the moment
   the patient became a parameter. The strongest identity check vanished
   *because* the value became variable.
3. **Assertions overfit to demonstrated state.** What remains asserts the
   demonstrated patient's dashboard (`No treatment intervention preferences
   recorded.`), another demo user's message fragment (`cleanly, no
   drainage.`), and a stray `:01` that slipped past the timestamp filter —
   all mutable, none identity-bearing. The same overfitting blocked the
   cross-instance replay at the login screen. → **Largely fixed 2026-07-09**
   (stability-selected mining; see the fresh-bundle table below): no
   timestamp/counter fragments, no other patients' content, and card/menu
   chrome preferred over data rows. Instance-stable state can still be
   mined (a module menu, a persistent data row; entry counts like
   `filtered from 56 total entries` were in this class until the review
   hardening rejected count phrases outright), so cross-instance transfer
   remains out of reach.

Also observed on the real app: the save step's geometry landmark is the
**recorded note text itself** — parameterized values leak into landmark
evidence, so healing quality silently degrades for any run whose parameter
differs from the demonstration (i.e., all of them). → **Fixed 2026-07-09**:
landmarks never embed a demo parameter value (or any volatile text), and a
compile-time lint fails the build if a demonstrated parameter value appears
in any text postcondition or any landmark's OCR text (REGION_STABLE
templates can still embed the value's rendered pixels — false-halt
direction; see the review-hardening known-remaining list).

### Track D re-run (2026-07-09, `feat/fix-postcondition-mining`)

One fresh record+compile and three paced replays against the live public
demo (fresh browser per session, >= 30 s apart, fake demo patients only,
$0, zero model calls; 4 demo sessions total). Every mined TEXT_PRESENT in
the fresh 17-step bundle, verbatim:

| step | intent | mined `text_present` |
|---|---|---|
| step_001 | type 'admin' | `admin` (the fixed login user — not a parameter) |
| step_004 | click 'Login' | `Calendar Finder Flow Recalls Messages Patient Fees Modules Procedures Admin Reports Miscellaneous Popups` (main menu chrome) |
| step_007 | press Enter (search) | `Showing1to1of1entries(filteredfrom56totalentries)` (results banner — historical: since the review hardening this string classifies as `count` and is rejected at compile time; a fresh compile mines a different candidate here) |
| step_008 | click 'Belford, Phil' | `TreatmentInterventionPreferences` (chart card title — the old miner asserted this card's Phil-specific *body* text) |
| step_012 | click pencil icon | `Last update` (column header — the old miner grabbed a message row's timestamp here; this is where `':01'` came from) |
| step_013 | click '+ Add' | `Calendar Finder Flow Recalls` (menu chrome) |
| step_016 | click 'Save as new message' | `Calendar Finder Flow Recalls Messages Patient Fees Modules Procedures Admin Reports Miscellaneous Popups` |

No clock fragments, no dates, no note text, no patient rows; the
parameter-leakage lint passed on the fresh bundle. Remaining vacuous steps:
the parameterized TYPE steps (by design — their value is verified at
runtime by typed-input verification) and the focusing click/scroll steps
listed with no `text_present`, which carry REGION_STABLE instead.

| run | outcome | detail |
|---|---|---|
| `patient=Phil` (control) | safe-halt (false abort) at step_014, **14/17** | **no postcondition failure anywhere** — steps 012 and 013, where every 2026-07-08 replay died on `text_present ':01'`, passed cleanly. The halt is the *identity-layer* band-order fragility described under "Exposed by this fix" above; 0 wrong actions, note not saved |
| `patient=Phil` (control, repeat) | safe-halt (false abort) at step_014, **14/17** | identical failure signature — reproducible, so it is filed as a real finding, not flake |
| `patient=Susan` (drift) | safe-halt at step_008, **8/17** | the parameterized-target position-bound limit documented above: the recorded row band carries Phil's phone/SSN/DOB (the demo value "Phil" itself is the target's own label, so param re-anchor mode does not arm) and the live band names Susan — the run refuses to click. Safe, correct, and still the P1 limitation |

Bottom line: the `':01'`-class postcondition false-halt is gone (0
postcondition failures in 3/3 live replays); the control run's remaining
blocker moved to the identity layer, was disclosed above, and has since
been fixed by the `feat/fix-wrong-actions` order-insensitive matcher
rework (the step_014 band shape is pinned verified in
`tests/test_identity.py`; the 14/17 runs above are the historical record
under the initial matcher).

## Failure modes ranked by severity

**P0 — silent wrong-state writes (5 reproductions). FIXED 2026-07-08.**
Row-level data drift (grow/look-alike/delete) redirected the whole tail of
a workflow to the wrong entity, and focus loss silently dropped typed
input; all mechanisms ended in a green report. Confidence was highest
(template rung, ~1.0) precisely when the click was wrongest, and the
irreversible-step risk gate never engaged because it keyed on *resolution
rung*, not on *target identity*. Fixed by the pre-click identity check
(the resolved point's own row text must match the recorded band —
order-insensitive token matching with an uncovered-residue cap — or, for
a parameterized target, the run-value-substituted band) and by typed-input
verification (read-back OCR of the typed value; diff-only acceptance
reserved for the masked no-new-text shape; one guarded refocus-and-retype
retry only when nothing changed, then halt). **Residual gaps, disclosed:**
(a) when the live band is *unreadable* (OCR finds nothing even at 2x —
e.g. an icon-only row), reversible steps proceed exactly as before with
the step flagged in the report (`identity: unreadable`), and only
compile-time-marked irreversible steps refuse — risk is opt-in via
`risk_overrides` and never auto-assigned, so in a default-compiled bundle
that refusal branch never runs (docs/LIMITS.md states this in the
dangerous list); (b) targets whose only discriminative text is their own
label (parameterized typeahead suggestions), and bands under 12 squashed
characters (too generic to discriminate), compile with no context band
and stay unverified; (c) ~~names within OCR-jitter similarity (>= 0.7
whole-token ratio, e.g. "Jane"/"Janet") are indistinguishable from
misreads and verify~~ — this gap was the mechanism of the THIRD
wrong-patient reopening and was FIXED 2026-07-10 (see that fix update:
near-name siblings now mismatch; only characteristic OCR char-class
confusions are treated as misreads — and after the FOURTH reopening
later that day, names equal only UNDER those confusion classes
(Neil/Nell) refuse too: when the only evidence is confusion
equivalence, the run aborts rather than verifies; the residual verify
classes are listed in docs/LIMITS.md "Known remaining");
(d) typed-input read-back can false-abort on widgets
that transform the value while typing (the native-date row in Track C),
and the refocus re-click / select-all retry assumptions are disclosed in
docs/LIMITS.md.

**P1 — parameterization is position-bound and self-disarming (Track D).
PARTIALLY ADDRESSED.** Changing a content-bearing parameter still removes
its own assertions and falls back to position resolution; but the click is
no longer blind — the identity check's param mode substitutes the RUN's
value into the recorded band and requires the WHOLE substituted band to
match before acting (the MockMed wrong-patient scenario on a real EMR now
halts instead of clicking, and since 2026-07-09 a row that merely mentions
the run's value no longer passes). Strictness cost, disclosed: when the
entity's own row text varies with the entity (a search result carries the
surname), the substituted band cannot match and the run halts even on the
correct row — verified re-anchoring for such rows is future work
(docs/LIMITS.md). ~~Landmark leakage of recorded parameter values
(healing-quality degradation) remains open~~ — fixed 2026-07-09: landmark
hygiene plus the compile-time parameter-leakage lint.

**P2 — cosmetic global drift zeroes availability (still open; out of scope of the 2026-07-08 fix).** Font +3px, 125% zoom,
or dsf 2 → false abort at step 000. Healing covers theme/move/rename but
nothing that rescales or reflows. Multi-scale matching stops at 1.18x, and
REGION_STABLE phashes break on reflow. Now bounded end to end in
[`benchmark/cosmetic_drift/COSMETIC_DRIFT.md`](../../benchmark/cosmetic_drift/COSMETIC_DRIFT.md):
the break is a hair-trigger at the FIRST scale deviation (the step_000
`region_stable` postcondition, not the resolver), font-FAMILY substitution to
a proportional face is fully absorbed, and — critically — the entire zoom /
DPI / font sweep fails safe (0 wrong-actions / 21 points). Candidate fixes
(DPI-aware coordinates; a scale-tolerant `region_stable`) are noted there.

**P2 — assertions overfit to instance/day state. LARGELY FIXED 2026-07-09.**
Mining now selects for stability instead of novelty: clock/near-date
fragments, counters and low-entropy noise are rejected (the `':01'` class
is structurally excluded — verified live), candidates must persist across
the recording's own frames, ranking prefers semantically-near alphabetic
text, and a DOB-class far date in an identity banner is deliberately KEPT
as identity evidence instead of being eaten by the old blanket timestamp
filter. The review hardening extended the reject set to month-name dates,
relative-time phrases, counts/pagination, badge counters and dot-clocks
(all reviewer-verified evasions; see above). **Still open within this
item:** text that is stable *within the recording* but instance-specific
(a module menu, a persistent data row like MockMed's `Sam Specimen`) can
still be mined — bundles still do not transfer between same-version
instances, and per-tenant re-recording remains the working assumption.

**P2 — identity band order-fragility on dialog clicks (exposed 2026-07-09,
pre-existing). FIXED 2026-07-09.** See "Exposed by this fix" above: the
recorded context band for a click inside a modal captures background
chrome whose OCR segmentation/order does not reproduce, and the initial
order-sensitive coverage matcher false-aborted on it (live OpenEMR control
runs capped at 14/17 — safe, 0 wrong actions, reproducible 2/2). Fixed by
the `feat/fix-wrong-actions` matcher rework: token-wise order-insensitive
matching with re-validated look-alike margins; the permuted modal-band
shape is pinned verified at 1.0 in `tests/test_identity.py`.

**P3 — vacuous successes. PARTIALLY FIXED 2026-07-09.** Steps that mined
nothing visual now fall back to structural postconditions (URL_CHANGED /
TITLE_CHANGED / NEW_TAB_OPENED) when the recorder captured structural
observations — the new-tab click is now verified. Still vacuous, disclosed:
actions with no visual AND no structural effect (the inert native
`<select>`), and any bundle recorded on a backend without structural
observations (native OS/RDP).

**P3 — verification blind spots inherent to pixels (still open).** The invisible
click-shield shows the runtime cannot distinguish "app ignored the click"
from "click never reached the app"; native browser chrome (select popups,
date pickers, file choosers) is invisible to screenshots.

## What held up

Reported with equal honesty:

- **No crashes, anywhere.** Every failure was a reported, per-step halt.
- **No false success without a wrong action first — scoped to UI drift.**
  Across the render/UI-drift matrix, when the replay acted on the *right*
  targets, postconditions never green-lit a failed outcome. This does **not**
  hold for the transactional-write class: the 2026-07-12 fault-model study
  (`benchmark/fault_model/`, and the dangerous list in docs/LIMITS.md) found
  phantom/partial-save successes on the *correct* action for 5 of 7
  transactional fault classes, where the screen reads success while the
  system of record is wrong. Vision postconditions cannot see that boundary.
- The halt machinery is sound when evidence dies loudly: opaque overlays,
  navigation hijacks, empty states, and 12s stalls all stopped the run at
  the right step with an accurate, named reason.
- Healing works as advertised for *labeled* drift: mid-run swaps and
  renames of text-bearing controls resolved to the correct targets.
- Timeout envelope behaved as designed (4s recovers; 12s halts).
- On the live OpenEMR demo, the compiled arm remains 0-model-call and
  passed its control run; the Susan run's wrong-free halt shows the
  postcondition system catching real cross-patient drift when an
  identity-bearing assertion happens to survive compilation.

## Reproduce

```bash
# MockMed tracks (A/B/C) — hermetic, run in CI:
pytest tests/e2e/test_perturbation.py tests/e2e/test_chaos.py \
       tests/e2e/test_primitives.py -q

# OpenEMR track D — live public demo, paced, fake patients only, $0:
.venv/bin/python scripts/openemr_param_depth.py record
.venv/bin/python scripts/openemr_param_depth.py replay
.venv/bin/python scripts/openemr_param_depth.py cross-instance
```

No `ANTHROPIC_API_KEY` is read by any of the above; the grounder rung is
disabled throughout.
