# Realism audit: assumptions the tests do not check

Date: 2026-10-20. Code: `origin/main` at `0669c4b1f`.

## Why this audit

A sign-in spike found that window-scoped recording re-finds its target window
by owner and title every frame. Chrome changes its title on almost every
navigation, so the recorder lost the window. No test caught it, because the
capture tests use a fake platform whose window title never changes. That fix
is in progress separately and is not repeated here.

This audit asks the same question of every critical component: what does the
code assume about real apps, and do the tests use fakes that hide those
assumptions? The safety rule is that the runtime fails closed: it stops
rather than guessing. A gap that **fails open**, doing the wrong thing and
reporting success, is the worst kind and is ranked first.

## How to read the table

- **Rank** orders the gaps by fail-open first, then by likelihood in real use
  times impact.
- **L × I** is likelihood and impact: H (high), M (medium) or L (low).
- **Fails** is OPEN (wrong action or wrong verdict, reported as success) or
  CLOSED (the run stops; this costs availability, not correctness).
- **Test** names the realistic test that now pins the gap. An *xfail* test is
  marked `xfail(strict=True)`. It runs in CI and is expected to fail until the
  gap is fixed; when a fix lands, the test passes, strict mode fails the
  build, and the marker must be removed.

Paths are relative to `packages/flow/openadapt_flow/` unless they start with
`packages/`.

## Ranked gaps

| Rank | Component | Assumption in the code | Real case that breaks it | Current test (and the fake that hides it) | Fails | L × I | Realistic test |
|---|---|---|---|---|---|---|---|
| 1 | Replay: record identity | A row whose visible text matches the recording is the recorded row. Identity is verified per row and never checked for uniqueness on screen (`runtime/identity.py`). | Two worklist rows read "John Smith Follow-up Dr Lee" and swap order between recording and replay. Replay opens and signs the other patient's chart. | Homonyms in `validation/dense_surface.py` always differ in date of birth or record number. | **OPEN, reproduced** | M × H | [`test_realism_browser.py`](../../packages/flow/tests/test_realism_browser.py) `::test_identical_rows_that_swap_order_never_sign_the_other_chart` (real Chromium, xfail). Issue [#1469](https://github.com/OpenAdaptAI/OpenAdapt/issues/1469) |
| 2 | Durable resume | A save that was delivered cleanly but then failed its record check wrote nothing. The pause carries `delivery_uncertainty` only when the delivery itself raised (`runtime/durable/controller.py:621-641`). The resume guard checks only that field (`runtime/durable/resume.py:1050-1067`). | The system of record is slow and the read-back times out after Save was clicked. The operator approves the default "resume", and Save is clicked again, creating a duplicate record. | `tests/test_durable_runtime.py::test_resume_continues_from_last_checkpoint` asserts the re-delivery as correct. `FakeBackend` writes nothing. | **OPEN, reproduced** | H × H | [`test_realism_durable_resume.py`](../../packages/flow/tests/test_realism_durable_resume.py) (the backend writes a row per press; xfail). Issue [#1470](https://github.com/OpenAdaptAI/OpenAdapt/issues/1470) |
| 3 | Capture: input attribution | Every key and click during a window-scoped recording belongs to the target window. `_geometry_for_action` checks identity, visibility and bounds, never focus (`packages/capture/openadapt_capture/window_capture.py:597-628`). The active-window poller is off in window mode (`recorder.py:3149`). | Mid-recording, the user switches to a chat app or a password prompt and types. Those keys are stored as target actions against the target's frame, including the other app's text. | `FakePlatform` (`packages/capture/tests/test_window_capture.py`) has no notion of focus. | OPEN | H × H | A focus probe on the capture scope; a fake platform that reports another focused process must refuse the key. Owned by the window-identity session (see [Coordination](#coordination)). Issue [#1471](https://github.com/OpenAdaptAI/OpenAdapt/issues/1471) |
| 4 | Read-back values | A non-exact read-back match means the expected value is present. `compare_value(..., exact=False)` is a substring test (`runtime/effects/readback_values.py:214`), used for every on-screen check (`runtime/effects/onscreen.py:603-610`). | The expected amount is 250 and the screen shows "$1,250.00". The expected quantity is 5 and the screen shows "Qty 15". A credit "$100.00 CR" reads as 100.00. Each confirms the write. | `test_onscreen_verifier.py` confirms embedded labels and never tests a neighbouring number. | **OPEN, reproduced** | H × H | [`test_realism_readback_values.py`](../../packages/flow/tests/test_realism_readback_values.py) (Hypothesis over integers and formats; xfail). Issue [#1472](https://github.com/OpenAdaptAI/OpenAdapt/issues/1472) |
| 5 | Teach: parameter inference | A value typed whole in one step is the parameter, even if the same value also appears inside longer text in another step (`jit/lift.py`). | The agent types the patient's name into its field and into a note. The note keeps the demonstrated name on every later run. The record check passes because the literal is constant. | `tests/test_jit.py` asserted the old behaviour. | OPEN → **fixed** | H × H | Hypothesis property in [`test_jit.py`](../../packages/flow/tests/test_jit.py) (fix PR [#1465](https://github.com/OpenAdaptAI/OpenAdapt/pull/1465)) |
| 6 | Effect checks: baselines | A record's identity is its raw `id`, or `str()` of each field (`runtime/effects/effect.py` `stable_id`). The baseline is stored with `model_dump(mode="json")` (`verify_run.py:709`). | Postgres, MSSQL and Oracle return UUID, Decimal and datetime values. After the JSON round trip they no longer compare equal, so every existing row counts as new. `count_new_only` then confirms a write that never happened. | `test_verify_run*.py` and `test_effect_sql.py` use SQLite ints and strings. | OPEN → **fixed** | M × H | Hypothesis round trip in `tests/test_stable_id_json_baseline.py` (fix PR, see below) |
| 7 | Teach: canaries | Canary values are different from run 1's. Nothing checks it (`compile_agent/pipeline.py:1010-1041`). | A canary run with run 1's values passes even when the program types a constant, which makes canaries blind to ranks 5 and 8. | Every test happens to use different values. | OPEN | H × H | Property test: admitting with canary values equal to run 1's must refuse. Issue [#1473](https://github.com/OpenAdaptAI/OpenAdapt/issues/1473). |
| 8 | Teach: parameter inference | A value typed whole exactly once belongs to the field it was typed in, with no record-field check. Record binding matches any field whose value equals the input (`jit/lift.py:164-171`, `record_sor.py:272-291`). | `quantity="1"` is set with a stepper, and "1" is typed into refills. Refills becomes the quantity parameter. | Synthetic workflows with long, unique values. | OPEN after the write | M × H | Hypothesis workflows with short values that collide. Issue [#1474](https://github.com/OpenAdaptAI/OpenAdapt/issues/1474). |
| 9 | Read-back values | A fuzzy text match whose longest common run covers 90% of the value confirms it (`runtime/effects/onscreen.py:262-277`). | "follow-up scheduled for 2026-10-29" confirms "…2026-10-21". "john smithson" confirms "John Smith". | No test with a difference at the end. | **OPEN, reproduced** | M × M-H | [`test_realism_readback_values.py`](../../packages/flow/tests/test_realism_readback_values.py) (xfail). Issue [#1472](https://github.com/OpenAdaptAI/OpenAdapt/issues/1472) |
| 10 | UI read-back | A "not found" page proves a record is absent. The browser read-back returns before its identity check (`runtime/effects/ui_readback.py:1089-1104`). The desktop read-back already guards this (`desktop_readback.py:1310`). | A void or delete workflow is verified by absence, but a wrong URL, an access-denied page or a filtered list also shows "not found". | `test_ui_readback_verifier.py` has no such case. | OPEN | M × H | Port the desktop test. Issue [#1475](https://github.com/OpenAdaptAI/OpenAdapt/issues/1475). |
| 11 | Effect checks: paging | The first page of a FHIR or REST search holds every match (`runtime/effects/fhir.py:245`, `rest.py:100-150`). | Servers page at 20 to 50 results. A duplicate on page 2 still confirms `expected_count=1`. | `test_effect_fhir.py` has no paging. | OPEN | M × H | Fake FHIR server with `_count=2`. Issue [#1476](https://github.com/OpenAdaptAI/OpenAdapt/issues/1476). |
| 12 | Capture: several windows | When several windows match, the largest is the target (`packages/capture/openadapt_capture/window_capture.py:2050`, `window_capture_linux.py:553`). macOS raises `WindowCaptureAmbiguousError` instead. | Two browser or EMR windows are open on different patients. The bigger one is recorded. | `test_window_capture_linux.py::test_resolver_binds_process_and_root_pixel_coordinates` asserts that the largest wins. | OPEN at bind time | M × H | [`test_realism_capture.py`](../../packages/capture/tests/test_realism_capture.py) (xfail). Issue [#1477](https://github.com/OpenAdaptAI/OpenAdapt/issues/1477) |
| 13 | Replay: typed-input check | Typing into a field produces the field's old value plus the typed text, and that counts as landed (`runtime/replayer.py:16545-16555`). Nothing clears the field first. | A search box keeps its last query, or a restored draft fills the field. The step saves "SmithJones". | No test with a prefilled field. In a real browser, the click's region check halts this case today. | CLOSED in the tested shape; OPEN when the fill arrives after the click check | M × H | [`test_realism_browser.py`](../../packages/flow/tests/test_realism_browser.py) `::test_a_field_autofilled_on_focus_never_saves_a_concatenation` (passes; pins the halt) |
| 14 | Replay: browser dialogs | A dialog the recording never saw is auto-dismissed by Playwright and not reported (`backends/playwright_backend.py:1137-1140, 3200-3232`). | A `confirm()` before Save returns false, so the save silently doesn't happen. | `test_multi_surface_dialog.py` covers recorded dialogs only. | CLOSED when the step has a visible postcondition (tested); OPEN when it has none | H × M | [`test_realism_browser.py`](../../packages/flow/tests/test_realism_browser.py) `::test_an_unrecorded_confirm_dialog_never_ends_in_success` (passes; pins the halt) |
| 15 | Replay: pixel typed-input check | OCR read-back of a typed value passes at 80% of the value, and any pixel change passes for values under 4 characters (`runtime/replayer.py:16598-16604`, `runtime/identity.py:631-638`). | Keystrokes are dropped over RDP or Citrix, or sent to a guest with a different keyboard layout. MRN `12345678901` arrives as `123456789xy`. | Synthetic OCR fakes. | OPEN | M × H | Fake OCR read-back that is wrong in the last 2 characters must halt. Issue [#1478](https://github.com/OpenAdaptAI/OpenAdapt/issues/1478). |
| 16 | Replay: retry after no change | When a field shows no change, the replayer re-clicks, selects all and retypes. The first delivery may have gone to another window (`runtime/replayer.py:16670-16760`). Windows-agent typing has no foreground check (`backends/win_agent/server.py:419-458`). | A notification steals focus. The patient name lands in a chat box, the retry fills the right field, and the step passes. | Fake backends never change focus. | OPEN | M × H | Fake desktop backend that moves focus on the first keystroke must halt. Issue [#1478](https://github.com/OpenAdaptAI/OpenAdapt/issues/1478). |
| 17 | Replay: occlusion | A structurally present but covered or off-viewport element returns the same value as a missing element, so resolution falls through to the pixel rungs (`backends/playwright_backend.py:2027-2048`, `runtime/resolver.py:767`). | A chat bubble or sticky banner covers the button's centre, and the template click lands on the overlay. | `test_structural_rung.py` has no partial occlusion. | OPEN | M × M-H | Real page with a fixed overlay on the button's centre. Issue [#1479](https://github.com/OpenAdaptAI/OpenAdapt/issues/1479). |
| 18 | Durable: deliveries outside a fence | A first, customer-local leg writes its checkpoint after delivery. A crash in between leaves no pause, so the only path is a fresh run from step 0. The local CLI builds no idempotency ledger. | The process is killed after Save. The re-run repeats every save. | Crashes are simulated with monkeypatch; no test kills a real process. | OPEN | M × H | Subprocess killed with SIGKILL at a random write, then re-run. Issue [#1480](https://github.com/OpenAdaptAI/OpenAdapt/issues/1480). |
| 19 | Capture: clocks | Input events use `time.time()`, and frames use a `perf_counter` offset (`input_observer/*.py`, `recorder.py:2030`). | An NTP step, or sleep and wake on macOS, makes action timestamps drift from frame timestamps. | The observer tests inject a fake clock. | OPEN (alignment) | M × M | Monkeypatch `time.time` to jump mid-recording. Not filed yet. |
| 20 | Capture: Windows | `IsWindowVisible` means the window is on screen. The region grab is what the window shows (`window_capture.py:2004, 2075-2096`). | A minimized window, or one on another virtual desktop. A toast or overlay over the window is recorded as the target's pixels. | No unit tests for the Windows resolver or grab. | OPEN | M × H | A resolver over injected Win32 attributes. Issue [#1477](https://github.com/OpenAdaptAI/OpenAdapt/issues/1477). |
| 21 | Capture: title binding | The title selector re-matches every frame. Linux, macOS and remote-display replay also re-find by exact title. | Chrome, LibreOffice and EMR windows retitle per record. | Static-title fakes. | CLOSED (stops) | H × L | Owned by the window-identity session. |
| 22 | Replay: settle timeout | Outside production profiles, a page that never settles proceeds with the latest frame (`vision/settle.py:304-313`). | Long-poll or server-sent-event requests keep the page "busy" forever. | `test_page_settle.py` covers a blocked settle, not the proceed path. | OPEN (demo profile only) | H × M | Real page with an open EventSource. Not filed yet. |

### Lower-ranked gaps worth knowing

- **Scrolling:** a scroll step wheels at the last pointer position. Over a
  number input, that can change its value (`runtime/replayer.py:17193`).
- **macOS click mapping:** y is mapped with the x scale (`backends/macos_backend.py:811`).
  This is up to about 9 points off at the bottom of a window with slightly
  different x and y scales.
- **Windows non-ASCII typing:** text goes through the clipboard and the
  clipboard is never restored, so accented names stay on it.
- **Single-surface workflows:** an unexpected new tab or popup is not checked.
- **Late duplicate writes:** record polling stops at the first CONFIRMED, so
  a second row written a moment later is missed.
- **List walks:** identical rows on the same page are merged, and a row
  inserted between page reads is not detected.
- **macOS:** trackpad scroll magnitudes are lost because only line deltas
  are read. Windows low-level hooks can be removed without notice, and a
  macOS permission revoked mid-recording is not detected.
- **Durable runs on macOS:** `fsync` without `F_FULLFSYNC` is not durable
  across power loss.

## What already fails closed, and was checked

- **Structural ambiguity:** several DOM matches are refused at the structural,
  template and OCR rungs.
- **Stale or re-rendered elements:** refused by the actuation token.
- **Frames:** the iframe chain is re-proven before delivery.
- **Browser input:** Playwright input is scoped to the page, so OS focus
  changes, password-manager bubbles and permission prompts cannot redirect
  keystrokes.
- **Window identity in capture:** bound to window id, process id and process
  start time. Bounds are compared exactly before and after each grab.
- **Approvals:** bound to the exact pause digest, run, run directory and
  bundle version. A rejected pause is terminal.
- **Checkpoint writes:** atomic (temporary file, fsync, replace).
- **OCR glyph confusion and ambiguous dates:** become INDETERMINATE.

## Coordination

The window-identity fix (rank 21) belongs to the session fixing the recorder
title bug. Ranks 3 and 12 touch the same file,
`packages/capture/openadapt_capture/window_capture.py`, and have been raised
with that session rather than changed here.

## Test infrastructure notes

- CI installs Chromium for the flow shards (`.github/workflows/packages.yml`),
  so the browser tests here run on every flow change.
- Several browser test modules call `pytest.skip` when Chromium fails to
  launch. In CI that skip should be a failure, or a broken browser install
  silently drops those tests.
- The macOS, Windows, Linux, RDP and Citrix replay backends are tested only
  against fake clients. None of the fakes change focus, titles, keyboard
  layout or codec noise. A realistic test for those needs a real OS runner.
