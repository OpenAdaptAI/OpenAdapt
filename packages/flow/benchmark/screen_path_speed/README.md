# Screen-path speed: where an OpenEMR referral's time goes

This study asks whether the compiled OpenEMR referral program can run much
faster on the screen, without the API step. It profiles the run, changes
what can change without weakening a check, and measures before and after.
The scoreboard (`benchmark/scoreboard/`) is still the only place speed
figures are published. This folder holds the harness, the data and this
report.

Every patient is synthetic: the eight referrals of the October timing
study, phones 555-01xx, emails `example.invalid`. OpenAdapt isn't affiliated
with the OpenEMR project.

## Short answer

The screen path got about 13% faster: from 23.2 s to 20.1 s median wall time
per referral (N = 14 each, interleaved). It can't get dramatically faster
while every check stays on. Each of the 41 steps waits until the page has
been quiet for 100 ms, then proves the right element and the right value
through the browser. The two save steps alone take about 4.3 s: the
duplicate-patient check before the save, and the save followed by the
system-of-record read-back. A realistic floor for this program on the screen
is about 10 to 12 s wall time. The large speedup is the API step: 3.3 s on
the scoreboard.

## Setup

- App: OpenEMR 8.0.0.3, the pinned local fixture (`openemr_api_tier/spike_fixture.py`), a
  private copy on ports 9910 and 9911 in the shared podman VM.
- Program: the referral compiled from a fresh recording with network capture
  (`openemr_api_tier/feature_e2e.py record` and `compile`). It has 41 steps;
  the API step is off in every run here.
- Clock: the same clock as the scoreboard's `program` rows. It starts at a new browser
  context and covers sign-in and opening the form (`setup_s`), the engine
  (`engine_s`), and the harness's own SQL read-back (`readback_s`). The
  harness read-back includes a fixed 1.0 s settle. The engine also confirms
  each save itself, over OpenEMR's REST API, inside `engine_s`.
- Arms: `before` is commit `aebf7a388`, which is origin/main plus the two
  fixes below. On main as it stands, the program can't finish a referral at
  all. `after` is the branch tip. `after-quiet50` is the branch tip with the
  existing page-quiet setting lowered to 50 ms
  (`OPENADAPT_FLOW_PAGE_QUIET_MS=50`). That arm is a tradeoff, not a default.
- Protocol: `compare.py` runs blocks of 4 referrals per arm and rotates the
  arm order every block. Each block resets the database and seeds the
  existing chart Rosa Delgado. Each arm runs from its own checkout, and every
  row records the engine commit it ran. A row starts only when the 1-minute
  load is under 12. One `before` row ended at load 30.9, which caused its 32.6 s
  maximum.
- Median over correct saves; r6, the possible duplicate, is a safe stop and
  reported apart.

## Results

| Arm | Saves correct | Duplicate stopped | Wall s median (p25 to p75, min to max) | Engine s median |
|---|---|---|---|---|
| before (`aebf7a388`) | 14 of 14 | 2 of 2 at step_039 | 23.25 (22.70 to 24.54, 22.05 to 32.61) | 20.46 |
| after (`5aa8a6ce9`) | 14 of 14 | 2 of 2 at step_039 | 20.13 (19.57 to 20.35, 19.17 to 22.69) | 17.33 |
| after, 50 ms page quiet | 14 of 14 | 2 of 2 at step_039 | 18.01 (17.62 to 18.58, 17.14 to 20.62) | 15.29 |

All three arms ran the same checks at every step of every referral:
identity status and mode, input read-back, postconditions, submit binding,
and the save's 11 record-check effects. `summary.json` reports
`identical_across_arms_and_runs: true`.

Data: `results/openemr-ab-2026-11-18/` (`rows.jsonl`, `summary.json` and
one result per item, with per-step timings).

## Where the time goes (before)

Exclusive time per run, median of seven saves, from the profiler in
`benchmark/openemr_throughput/profiler.py` (`run.py --profile`; data in
`results/profile-before/`). The engine took 20.3 s.

| Source | Seconds | Share | What it is |
|---|---|---|---|
| Settle: quiet-window waits | 4.9 | 24% | After each action the page must be quiet for 100 ms; about 47 settles |
| Typing | 1.9 | 9% | Includes two frame descents per field to register and snapshot it |
| Clicks | 1.9 | 9% | The guarded click: re-binds the element and does a trial click |
| Input read-back | 1.8 | 9% | The field's value before and after typing, read through the frame chain |
| Identity (DOM) | 1.7 | 9% | The right-record check before each click |
| Screenshots | 1.4 | 7% | About 48 frames at about 30 ms |
| Target resolution | 1.4 | 7% | The structural lookup of each click target |
| Postconditions | 1.3 | 7% | Local-edit and dropdown checks after keys |
| Settle: page probes | 1.3 | 6% | 352 quiescence probes |
| OCR | 1.3 | 6% | The duplicate check's "No matches were found." and the save dialog's identity |
| Save read-back (REST) | 0.2 | 1% | The engine's record check after the save |
| Other | 0.8 | 4% | Engine bookkeeping |

By step kind (before, then after, per step): click 502 ms and 404 ms, type
406 ms and 275 ms, select-all key 226 ms and 180 ms, Enter 281 ms and
203 ms, scroll 490 ms and 394 ms. The two save steps took 4.7 s and 4.3 s.
Outside the engine: sign-in 1.4 s, harness read-back 1.3 s.

Most of the 1.8 s in typing, read-back, identity and lookups was one pattern: a frame
descent. OpenEMR's form sits two frames deep, and every DOM proof walked the
frame chain with about 14 to 28 separate browser calls. A run made about
3,970 Playwright calls.

## What changed

1. **One call per frame boundary (safe; the same proof).**
   `_frame_point` and `_focused_element_call` now read the frame element's
   tag, projection, selector, the selector's uniqueness and the child
   viewport in one call. A descent that lands on a non-frame element needs
   one call instead of three. When the document has no shadow root, a native
   `querySelectorAll` proves the selector is unique and names this element.
   Without shadow roots, that is what Playwright's `locator().count()` and
   handle comparison proved. With a shadow root, Playwright still decides. Calls per run fell
   from about 3,970 to 2,390. Real-Chromium tests compare the new descent with
   the old one over a grid of points on nested, transformed, bordered,
   occluded, pointer-transparent, duplicate-id and shadow-root pages
   (`tests/test_frame_descent_round_trips.py`).
2. **Settle waits until quiet is possible (safe).** After a busy probe, the
   page reports when its latest change becomes a full window old. No probe
   can answer quiet before then, so the settle sleeps until then instead of
   probing every 20 ms. The window, and every signal the probe checks, are
   unchanged (`tests/test_page_settle_wait_hint.py`).
3. **Fix: the dropdown check on a linear run.** The `field_value`
   postcondition read only a program run's parameters, so every linear run
   halted at its first dropdown commit (step_002 here). It failed closed, so
   nothing was saved wrong, but no referral could finish. It now uses the
   run's parameters (`tests/test_local_edit_classification.py`).
4. **Fix: the benchmark's record check.** Since #2041 a REST read that
   doesn't declare itself complete is indeterminate, and the engine refused
   every save before the click. The shared OpenEMR oracle stays undeclared,
   as #2041 decided for a bounded live read. This fixture is owned and reset
   to a few charts before every block, though, so its harnesses
   (`run.py` and the scoreboard's `feature_e2e.py`) declare the read
   complete (`common.owned_fixture_oracle`), as #2041 did for other owned
   fixtures.
5. **Fix: the at-rest key, derived once per checkpoint store.** Encrypted
   checkpoints ran scrypt on every write and read: 131 times in one MockMed
   tutorial. A store now seals its containers under one salt, and derived keys are kept in
   memory, bounded, reused only for the same passphrase. The container format
   is unchanged (`tests/test_checkpoint_key_derivations.py`). Results below.

## What was measured and not changed

- **Page-quiet window, 100 ms to 50 ms** (the existing `fast` profile's
  setting): 2.1 s more off wall time, every check passing. It gives the app less time to
  start reacting before the next check, so it stays opt-in. The `fast`
  profile as a whole was no faster on one worker: its two OCR threads slowed
  the duplicate check's OCR by about 1 s, which cancelled the settle gain
  (`results/profile-before/ceiling-fast-profile.jsonl`).
- **The pre-save duplicate check and the save dialog's identity OCR**
  (about 1.3 s) were left alone. They are the record confirmation before the
  save.
- **The read-back after the save** (0.2 s, plus the server's own time to
  save, about 1 s) was left alone.
- **The guarded click** (about 100 ms per click) was left alone: it is the
  check that the click lands on the bound element.
- **Screenshots** (about 30 ms each): a raw capture would skip Playwright's caret
  hiding, which changes pixels compared with the recording.

## What would go further

- **Fuse click, select-all and type into one verified field-set** (a compiler
  change). A field now costs about 860 ms in three steps. One identity check,
  one fill, one read-back and one settle would cost about 350 ms. Over the
  form's 8 or 9 fields that saves about 4 s, to roughly 16 s wall time.
- **Sign in once per worklist**, which the worklist runner already does:
  minus 1.4 s per referral after the first.
- **Parallel workers** (`--workers`) raise throughput, not per-referral
  time.
- **The API step**, which the compiler already proposes for this form:
  0.6 s engine time and 3.3 s wall time on the scoreboard.

## MockMed tutorial: the at-rest key fix

`openadapt-flow tutorial` (record, compile and a governed run on MockMed),
with encryption at rest at its default, timed with `tutorial_timing.py`. That
script counts every scrypt run by wrapping `cryptography`'s `Scrypt.derive`.
The arms are `60ef46494` (before the fix) and `5aa8a6ce9` (the fix),
interleaved, N = 10 each, all exiting 0.

| Arm | scrypt runs per tutorial | Time in scrypt | Tutorial wall s median (p25 to p75) | Run steps s median |
|---|---|---|---|---|
| before | 131 (every run) | 24.2 s | 40.75 (40.40 to 42.59) | 6.66 |
| after | 1 (every run) | 0.18 s | 14.66 (12.14 to 16.16) | 3.04 |

One `after` run took 38.6 s, at load 9.6 to 12.2, with its run steps at
4.2 s; the median is unaffected. Data: `results/mockmed-tutorial-2026-11-18/`.

## Reproduce

```bash
export DOCKER_HOST=<podman socket> API_SPIKE_UI_PORT=9911 API_SPIKE_API_PORT=9910 \
  API_SPIKE_PROJECT=openadapt-openemr-perfscreen API_SPIKE_STATE=<state dir>
cd packages/flow
python -m benchmark.openemr_api_tier.spike_fixture create
python -m benchmark.openemr_api_tier.feature_e2e record --out REC
python -m benchmark.openemr_api_tier.feature_e2e compile --recording REC --out BUNDLE
GATE_LOAD1=12 python -m benchmark.screen_path_speed.compare --bundle BUNDLE --out OUT \
  --arm before=<checkout A> --arm after=<checkout B> \
  --arm after-quiet50=<checkout B>:OPENADAPT_FLOW_PAGE_QUIET_MS=50 --blocks 4
python -m benchmark.screen_path_speed.analyze OUT/rows.jsonl
python -m benchmark.screen_path_speed.run --bundle BUNDLE --out O --results R \
  --arm x --per-block 8 --profile       # the per-step profile
```

A first comparison attempt ran both arms on the branch's engine. `python -m`
puts the working directory ahead of `PYTHONPATH`. The rows showed it (both
arms had the same `engine_head`), and they were discarded. `compare.py` now
runs each arm from its own checkout.
