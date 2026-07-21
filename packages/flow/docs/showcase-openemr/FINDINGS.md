# OpenEMR spike — findings

Can a workflow recorded once against a real third-party application replay
vision-only, deterministically, with zero model calls? This spike ran the
full record → compile → replay loop against the official OpenEMR public
demo (`https://demo.openemr.io/openemr`, OpenEMR 8.0.0, published demo
credentials, fake patients only, instance resets daily at 08:00 UTC).
OpenEMR was chosen because it is nothing like MockMed: a dense,
frame-heavy, slow, LAMP-era EMR whose screens are being mutated all day by
other demo users.

Short answer: yes, after four capability fixes. The spike round scored
**4/5 replays end to end (18/18 steps)**, each run substituting a
different parameterized note value; the fifth run failed safely (a
resolution landed 12 px off an 18 px-tall icon, the click hit dead space,
and postconditions aborted the run without a wrong action). Every replay
was a fresh browser with no session state, driven purely by screenshots
and pixel coordinates.

A follow-up change removed the failure mode that broke run 5 —
closed-loop scrolling (see below) — and a fresh 5-run round against the
live demo then went **5/5 (18/18 steps each, zero model calls)**.

## The workflow

One clinical task, 18 compiled steps, recorded by
[`scripts/openemr_demo.py`](../../scripts/openemr_demo.py):

1. log in as the demo admin (username, password, sign-in — steps 000-004)
2. search the demo patient "Phil" in the top-bar demographics search
   (005-007)
3. open the chart of "Belford, Phil" from the Patient Finder results (008)
4. scroll the Medical Record Dashboard ~1600 px to the Messages card
   (009-012, four SCROLL steps)
5. open Patient Messages via the card's pencil icon (013)
6. Add → click the note textarea → type the note (parameterized) → Save
   as new message (014-017)

The note text is a workflow parameter. Each of the five replays supplied a
different value, and the run driver OCRs the final screen to confirm the
substituted text (not the recorded example) actually reached the message
list.

## What worked without changes

- **Iframes were a non-issue.** OpenEMR nests everything in frames (the
  login screen is the only frameless page; the post-login UI is a tab
  shell over six or more iframes, and the add-note dialog is an iframe
  inside a modal inside an iframe). The runtime never noticed: screenshots
  capture the composited page and `page.mouse` clicks land on whatever is
  under the pixel, frames included.
- **Login.** CSRF tokens, redirects, session cookies — all irrelevant to a
  vision-only replayer, because it does what a human does.
- **Template matching carried most steps.** In successful runs 8 of 9
  anchored clicks resolved on the first (local template) rung.
- **The postcondition system caught every real failure.** No run ever
  "succeeded" falsely, and no failure clicked through to a wrong side
  effect. Three distinct failure modes were all stopped at the failing
  step with an illustrated report naming it.

## What needed code changes

Four changes, each made because the live app broke an assumption MockMed
never tested. All are additive; the full unit suite (158 tests) passes.

1. **SCROLL action** (`ir.ActionKind.SCROLL`, `Backend.scroll(dx, dy)`,
   recorder/compiler/replayer support). The dashboard puts the Messages
   card ~1600 px below the fold. The wheel gesture dispatches at the
   current pointer position, so nested scroll containers (the dashboard is
   its own scrolling iframe) behave exactly as they do for a human. SCROLL
   steps compile with no postconditions: a scroll shifts the whole
   viewport, so a frame diff would assert mutable page content; the next
   anchored step's resolution verifies the scroll landed.

2. **Resolution retry until `Step.timeout_s`.** MockMed renders
   instantly; OpenEMR takes seconds, and `wait_settled` can return a
   settled-looking frame that is still loading. The replayer previously
   failed a step on the first ladder miss. It now retries the ladder with
   fresh settled frames until the step's timeout (structural errors and
   the risk gate never retry).

3. **REGION_STABLE postconditions tolerate small layout shifts.** All
   five first-round replays aborted on the same step: OpenEMR's calendar
   day view scrolls itself relative to the current time of day, so the
   recorded region's content had shifted ~12 px vertically between
   recording and replay — identical pixels, wrong position, phash
   distance 34 against a tolerance of 16
   ([failure-evidence/](failure-evidence/) has the two crops). The
   compiler now stores a crop of the expected region content in the
   bundle (`templates/<step_id>_expect.png`) and the replayer first
   searches for that content near the recorded region, falling back to
   the exact-position phash.

4. **Global template matches must not contradict landmarks (unlabeled
   anchors only).** The dashboard has one visually identical pencil icon
   per card — a dozen clones. Once the Messages card's content changed
   (the very note the recording had saved), the local template search
   missed and the global rung matched a different card's pencil: the
   replay clicked it, landed on Patient Reminders, and the postcondition
   aborted the run. An icon-only anchor now rejects a global template
   match when every locatable landmark places the target more than 40 px
   away, falling through to the geometry rung — which resolved the right
   pencil in every subsequent run. Labeled anchors are exempt (their
   templates carry the label, and rename/move drift healing relies on
   global acceptance).

   A fifth, smaller fix fell out of run analysis: parameterized TYPE steps
   no longer emit a REGION_STABLE postcondition at all — the changed
   region is the typed value's own pixels, and asserting its rendering is
   the pixel-level equivalent of asserting the excluded text (one run
   failed exactly this way when its note text was shorter than the
   recorded example).

5. **Closed-loop scrolling** (follow-up, after the spike round). The spike
   replayed SCROLL steps open-loop — four fixed 400 px gestures — and that
   is what ultimately broke run 5: earlier replays had grown the dashboard,
   so the recorded scroll distance left the post-scroll viewport ~12 px
   displaced. A SCROLL step's execution semantics are now "scroll until
   the NEXT anchored step's anchor resolves, starting from the recorded
   delta": the step probes the next anchor on the current settled frame
   (no-op when a previous scroll already brought it into view), then
   repeats scroll-by-recorded-delta → settle → probe until the anchor
   resolves, bounded by ~2.5x the step's recorded distance. Consecutive
   SCROLL steps hand the loop to each other; exhausting the budget with no
   further SCROLL step fails the run loudly, naming the anchor that never
   appeared. Steps with no later anchored step keep the fixed recorded
   delta. Probes never call the grounder, so replays stay model-free.

## Replay results, spike round (open-loop scrolling, 5 fresh-browser runs)

| run | outcome | steps | rungs | heals | note verified on final screen | wall time |
|----:|---------|------:|-------|------:|------------------------------|----------:|
| 1 | success | 18/18 | template 8, geometry 1 | 1 | yes | 29.8 s |
| 2 | success | 18/18 | template 8, geometry 1 | 1 | yes | 29.0 s |
| 3 | success | 18/18 | template 8, geometry 1 | 1 | no (OCR miss; note visibly present in `final.png`) | 28.9 s |
| 4 | success | 18/18 | template 8, geometry 1 | 1 | no (same) | 32.4 s |
| 5 | failure at step_013 | 13/18 | template 5 | 0 | — | 26.5 s |

Success rate 4/5. Zero model calls in every run. The geometry resolution
in each successful run is the pencil-icon step (013): the landmark guard
rejects the ambiguous global template match and the landmark offsets
resolve the true target, healing the anchor from the live frame.

Run 5's failure: the geometry estimate came in at (814, 356) against a
true target of roughly (813, 368) — 12 px high, just above the 18 px-tall
icon. Cause: the dashboard's total content height had grown (each earlier
replay appends a message to the card), shifting the post-scroll viewport
by about the same 12 px, and OCR bounding-box jitter on the landmark text
did not fully track it. The click hit the card border, nothing happened,
and both postconditions failed the step.

Note on the two "OCR miss" rows: the replayed note is plainly visible in
the saved `final.png` of runs 3 and 4, but rapidocr dropped the table line
containing it, so the out-of-band verification (squashed-text containment
over the whole frame) could not confirm it. This is a measurement
limitation of the verification script, not a replay failure — and a fair
sample of rapidocr's line coverage on dense 13-14 px table text at
1280x800.

## Replay results, closed-loop round (current, 5 fresh-browser runs)

Re-run the same day with closed-loop scrolling (change 5 above), same
bundle, five new parameterized note values, fresh browser per run:

| run | outcome | steps | rungs | heals | note verified on final screen | wall time |
|----:|---------|------:|-------|------:|------------------------------|----------:|
| 1 | success | 18/18 | template 8, geometry 1 | 1 | yes | 37.7 s |
| 2 | success | 18/18 | template 8, geometry 1 | 1 | yes | 36.0 s |
| 3 | success | 18/18 | template 8, geometry 1 | 1 | no (OCR miss; note visibly present in `final.png`) | 37.3 s |
| 4 | success | 18/18 | template 8, geometry 1 | 1 | yes | 36.5 s |
| 5 | success | 18/18 | template 8, geometry 1 | 1 | no (same) | 37.1 s |

**Success rate 5/5. Zero model calls in every run.** By this point the
demo instance carried nine-plus replay-appended messages (this round ran
on top of the spike round's mutations — more content growth than broke
the open-loop run 5), and the scroll loop absorbed the displacement in
every run: each SCROLL step probes the pencil anchor and stops scrolling
only when it actually resolves, so the pencil-icon step's geometry
resolution starts from a viewport the anchor is verifiably inside. The
pencil step still resolves via the geometry rung (the card's mutated
content keeps defeating its template) — that residual dependence on
OCR-box precision for small icons remains real, it just no longer
compounds with scroll displacement.

Two costs, reported honestly: wall time rose from ~29 s to ~37 s per run
(each scroll step settles and runs a ladder probe per gesture — a fair
trade for removing the round's only failure mode), and the out-of-band
note verification again OCR-missed two runs whose notes are plainly
visible in the saved final screens (the same rapidocr line-coverage
limitation as the spike round).

## What is still rough

- **Shared mutable demo state is the dominant noise source.** The public
  demo is writable by anyone and resets daily. Replays mutate it too:
  every successful run appends a message, which grows the dashboard,
  which shifts the post-scroll layout, which is what ultimately broke the
  spike round's run 5. Closed-loop scrolling now absorbs the scroll-axis
  component of this drift; against a per-tenant instance (the realistic
  deployment) the rest shrinks to the app's own dynamic content.
- **rapidocr on dense EMR text is mediocre.** Labels compile with missing
  or mangled characters ("ername" for Username, "Searchbyanydemogre" for
  the search placeholder), whole table lines are sometimes dropped, and
  OCR box centers jitter by ~10 px between visits to the same screen.
  Template + landmark evidence compensates; OCR alone would not carry
  this app.
- **Geometry precision vs. small targets.** Landmark dx/dy offsets are
  exact at record time, but locating the landmark by OCR reintroduces
  box-center error of the same order as a small icon's size. Fine for
  buttons; marginal for 20 px icons.
- **Horizontal scroll side effect.** At 1280x800 the OpenEMR shell is
  1346 px wide; focusing the top-bar search box scrolls the document 25 px
  right. It is deterministic (record and replay both do it via the same
  focus), but it means anchors recorded after that step live in a shifted
  coordinate frame — worth knowing when reading the frames.
- **Selects were dodged, not solved.** The add-note dialog's two native
  `<select>`s keep their defaults in this workflow. Native dropdown
  popups do not appear in page screenshots, so choosing a non-default
  option vision-only will need a keyboard-based fallback (click, type
  prefix, Enter). Not built because nothing here needed it.
- **The demo resets daily.** The committed bundle should replay against a
  freshly reset instance (that is the state it was recorded in), but
  intraday state from other visitors is unpredictable. Treat the recorded
  artifacts as a snapshot, not a CI fixture.

## Layout

```
recording/          meta.json, events.jsonl, frames/ (18 events, before/after)
bundle/             workflow.json, workflow.py, templates/ (anchor + expect crops)
runs/run-1/         full artifacts: report.json, REPORT.md, steps/, heals/, final.png
runs/run-2..5/      report.json, REPORT.md, final.png (step frames trimmed for size)
runs/summary.json   per-run outcomes for the closed-loop round
failure-evidence/   the round-1 calendar-region crops (recorded vs replay)
```

To reproduce (writes to this directory; needs network access to the
public demo):

```bash
.venv/bin/python scripts/openemr_demo.py record   # record + compile
.venv/bin/python scripts/openemr_demo.py replay   # 5 fresh-browser replays
```

Fake demo patients only. Do not point the script at a real OpenEMR
install.
