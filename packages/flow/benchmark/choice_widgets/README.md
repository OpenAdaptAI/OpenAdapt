# Dropdowns and popups: before and after

The fixed task set behind [docs/CHOICES.md](../../docs/CHOICES.md). Every run is
scored from the app's own stored records, never from the run report.

## What is measured

**Synthetic app** (`app.py`, real headless Chromium, synthetic patients). One
page per widget family, each a registration with a patient name, the widget
and a Save button behind a browser `confirm()`, plus `register`: title (native
select), gender (ARIA listbox), blood group (`<div>` list that chooses on
press), department (Select2 with a search) that reloads the doctor list
(native select, loaded late), city (type-ahead from the server), visit date
(flatpickr), Save with `confirm()` and a notice, then a follow-up booked in a
modal dialog. Select2, Chosen, Tom Select, jQuery and flatpickr are the real
MIT or Apache-2.0 builds in `vendor/`.

`measure.py` records one demonstration per page with `record_interactive`
(real pointer and keyboard input), compiles it, and replays it in fresh
browsers:

| Case | Page change | A correct run |
| --- | --- | --- |
| `demo` | none | saves the demonstrated choice |
| `heldout` | none; the field's parameter names another option | saves that option |
| `reorder_late` | options reversed and 800 ms late | saves the demonstrated choice |
| `missing` | the option is gone | stops, saves nothing |
| `duplicate` | two options show the label | stops, saves nothing |
| `disabled` | the option is disabled | stops, saves nothing |
| `surprise_confirm` | an extra `confirm()` on choosing | stops (or the page asks nothing and the save is right) |
| `surprise_modal` | a notice dialog over the form before the save | stops, saves nothing |

Scores: `verified` (run succeeded, record right), `safe_halt` (run stopped,
nothing wrong stored), `wrong` (a wrong record, or success without the
record), `unbindable` (the program has no parameter for the field, so the
held-out job cannot be expressed), `record_refused`, `n/a`.

**Real apps** (`real_apps.py`, local containers pinned in `openemr/` and
`openmrs/`, synthetic patients). Flow attaches to a signed-in local Chromium
(the signed-in-session path), records, compiles, then runs each case in the
same browser, scored from the app's database (OpenEMR) or REST API (OpenMRS).

## Results

Before: `origin/main` 36b9a40f9. After: the implementation commits recorded
in each JSON's `label`; these measurements precede the extraction of recorder
helpers into `choice_capture.py` and the final semantic-lookup safety fixes. Load average during the runs: 8 to 26 (`results/log.txt`).

### Synthetic app, per widget (before → after)

| Widget | demo | heldout | reorder_late | missing | duplicate | disabled | surprise_confirm | surprise_modal |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| aria_listbox | safe_halt → verified | unbindable → verified | safe_halt → verified | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt |
| async_combobox | safe_halt → verified | unbindable → verified | safe_halt → verified | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt |
| cascade | record_refused → verified | record_refused → verified | record_refused → verified | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt |
| chosen | safe_halt → verified | unbindable → verified | safe_halt → verified | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt |
| datepicker | record_refused → verified | record_refused → n/a | record_refused → verified | record_refused → n/a | record_refused → n/a | record_refused → n/a | record_refused → verified | record_refused → safe_halt |
| div_dropdown | safe_halt → verified | unbindable → verified | safe_halt → verified | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt |
| native | record_refused → verified | record_refused → verified | record_refused → verified | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt |
| register | record_refused → verified | record_refused → verified | record_refused → verified | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt |
| select2 | safe_halt → verified | unbindable → verified | safe_halt → verified | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt | safe_halt → safe_halt |
| tom_select | record_refused → verified | record_refused → verified | record_refused → verified | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt | record_refused → safe_halt |

No case stored a wrong record in the retained before/after runs.

A later unreviewed run at `c3801377e` kept the conservative option-commit risk
policy and produced 24 verified cases, 40 safe stops and 16 compilation
refusals: the calendar and full-registration tasks were refused because a
potential save also filled a field. Input read-back does not establish that a
control lacks an auto-save. `--review-fixture-choices` records an explicit
operator risk decision for this synthetic app's local form edits, through the
compiler's existing `risk_overrides` data. It does not change the engine's
risk policy or qualify any real application. The raw output marks this flag
in each result.

- The final synthetic run at `d32bb3113` has 30 verified cases, 46 safe
  stops and 4 inapplicable date-picker cases, out of 80. All nine applicable
  held-out choices save the parameter value, including the type-ahead,
  Tom Select and registration pages. The baseline has 40 recording refusals,
  35 safe stops and 5 unbindable held-out jobs.
- The date picker replays the demonstrated date only (a literal choice checked
  against the exact date the field showed), so its held-out and option cases
  are `n/a`.
- `surprise_confirm` on the date picker page is `verified` because that page
  raises no extra question.

### Real apps

| App | Before | After |
| --- | --- | --- |
| OpenEMR 7.0.3, New Patient (title, sex, marital status, typed date of birth, Create, duplicate-check dialog in an iframe, reminders `alert()`) | Recording refused at the first native select | Recorded and compiled: three native-select choices bound to parameters and the `alert()` attached to the Create confirmation. In the demonstrated, held-out (Mrs./Female/Single) and by-value (`divorced`) runs every choice was made and read back; a missing option (`Prof.`) stops with `choice_missing`. The runs then stop at Create New Patient on an existing guard (`identity_uncounted`, the Search and Create buttons look like a repeated row), so no patient is saved by a run |
| OpenMRS 3.4.0, registration with a relationship (relative from a server type-ahead, relationship type from a select) | Recording refused (a click on a radio's label arrives as two clicks) | Recorded and compiled: the type-ahead and the relationship select compile to choices. Runs stop earlier, at the birth-date "No" tab, on the same existing repeated-row guard |

The two stops on real apps are the existing identity guard, unrelated to
dropdowns; they are fail-closed and stored nothing.

## Reproduce

From `packages/flow`, write fresh results outside the checkout:

```bash
python -m benchmark.choice_widgets.measure --review-fixture-choices --out /tmp/choice-results/fixture_after.json
podman compose -f benchmark/choice_widgets/openemr/compose.yml up -d
python -m benchmark.choice_widgets.real_apps --app openemr --out /tmp/choice-results/openemr_after.json
podman compose -f benchmark/choice_widgets/openmrs/compose.yml up -d
python -m benchmark.choice_widgets.real_apps --app openmrs --port 9662 --out /tmp/choice-results/openmrs_after.json
python -m benchmark.choice_widgets.summarize
```
