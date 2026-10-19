# Classifying local edits without a person

Status: approved by the founder on 2026-10-19, with a strict rule: the
compiler classifies a step only when the recording shows no mutating request
sent for it **and** a runtime check confirms only the expected local change.
Anything else goes to a person.

## The problem

A compiled program flags every special key and every unlabelled or
navigation-shaped click for review (`risk_review_required`), because its
risk can't be proven from the step alone. The run gate then refuses the
bundle until a person classifies each one. On the OpenEMR referral that is 12
steps (blocker section A in the referral admission list):

- 8 `Control+a` presses, each in a text field right before typing into it;
- 4 `Enter` presses, each right after typing an option's label into a
  dropdown;

plus, under `clinical-write`, the same keys fail `prohibit_vacuous_postconditions`
because a key has no outcome check (section B), and the Contact tab click is
unarmed (section C).

These are the same few patterns in every form-filling program, on every app.
A person approving each one, per bundle, is busywork that trains people to
approve without reading.

## The rule

The compiler classifies five local-edit patterns itself. Every pattern needs
all of this evidence from the recording:

- network capture (`record --capture-network`), with no request other than
  `GET`, `HEAD` or `OPTIONS` between the previous event and the next one;
- URL, title and page count recorded on both sides of the action, and
  unchanged;
- when the recorder snapshotted the record store around the step, the two
  snapshots equal.

A recording without network capture classifies nothing.

| Pattern | Step evidence | Outcome check added to the step |
| --- | --- | --- |
| `select_all`: `Control+a` / `Meta+a` | The previous step clicked a text field (role `textbox`, `searchbox` or `spinbutton`, with a selector); the next step types into it | `local_edit`: focus is still on that field; URL, title and page count unchanged. The next step's typed-input read-back then checks the field's whole value. |
| `confirm_option`: `Enter` in a dropdown | The steps before clicked a dropdown (role `combobox` or `listbox`) and typed a parameter's value | `local_edit` on the dropdown, plus `field_value`: the focused dropdown shows this run's value |
| `confirm_entry`: `Enter` in a text field | The steps before clicked a text field and typed a parameter's value | `local_edit` on the field, plus `field_value`: the focused field's value equals this run's value exactly |
| `in_form_tab`: a tab click | Role `tab`, or a `button` whose only mined check is a region change; the next acting step is in the same frame | `local_edit` (URL, title and page count unchanged; focus may move) |
| `dismiss_dialog`: an informational dialog's dismiss | A `button` or `link` labelled only "Continue", "Continue to ...", "Next", "Close", "OK", "Got it", "Dismiss", "Not now", "Maybe later" or "Skip for now" | `local_edit`, plus `text_absent` of the label: the dialog is gone |

`continue` and `next` are write words for the risk inference, so a dismiss
labelled with them starts irreversible. It is reconsidered only when those
are its only write words and its risk is still the per-step keyword
inference. A click that a later rule made irreversible (the click that
confirms a dialog, an icon-only click presumed to save) is never
reconsidered. Because a classified dismiss is reversible, effect mining no
longer attaches a placeholder `record_written` to it (#1377).

The runtime checks fail closed: a backend that can't read focus, the field
value, the URL, the title or the page count fails the step.

What stays the same:

- **The risk rule.** A step whose evidence is missing or mixed (a URL change,
  a write request in the window, no network capture for a submission key, a
  key that isn't one of these, a field role the compiler can't read) keeps
  `risk_review_required` and goes to a person, exactly as today.
- **Write-shaped context still wins.** A key or click whose retained text
  names a consequential write (`submit`, `save`, `delete`, ...) classifies
  irreversible before any of this runs. The one exception is a dismiss whose
  only write words are `continue` or `next`, with the full evidence above.
- **Explicit `risk_overrides` still win.**
- **D, the duplicate-prevention (idempotency) key on the save, is untouched.**
  That is a business decision.

What the run gate sees: the classified step is reversible, reviewed by the
compiler with recorded evidence (`risk_explanation` names the pattern and the
evidence), and has an outcome check, so `require_reviewed_action_risk`,
`prohibit_vacuous_postconditions` and `max_unverified_steps` pass for it. A
tab click classified this way is in-form navigation that writes nothing, so it
isn't consequential and isn't entity navigation; `prohibit_unarmed_clicks`
under `clinical-write` still applies to it, so it still needs an identity
check (decision 2 below).

## Why this is safe enough

- The classification is backed by what the demonstration did, not by the
  label. A step that sent a write request in the recording, changed the URL,
  title or page count, or changed the record store is never classified.
- The outcome check runs every time. If the same key does something else at
  run time (the app changed, a different field had focus), the step fails its
  postcondition and the run halts at that step. The halt comes after the key
  was pressed, which is the residual risk: a key that, at run time only,
  commits something. For these patterns that requires the app to have
  changed the field's behavior since the recording, and the first write it
  makes is still caught by the save's record check or by the next step's
  checks.
- Evidence is sealed in the bundle (the step's explanation and postconditions
  are part of the content digest), so a later edit to a classification
  changes the digest and needs a new admission.

## Decisions

1. The residual risk above is accepted for these patterns (founder,
   2026-10-19).
2. `clinical-write`'s `prohibit_unarmed_clicks` still applies to a classified
   tab click. On the OpenEMR referral, the Contact tab (`step_015`) still needs
   an identity check or a person.
3. Network capture is required for every pattern, `Control+a` included.

## Implementation

- `openadapt_flow.compiler.local_edit.local_edits(steps, events, network)`
  returns the classified steps with their explanation and checks.
- `compile_recording` applies them after the per-step risk inference and the
  confirmation-dialog pairing, and before `risk_overrides`, which still win.
  Effect mining runs after both.
- `openadapt_flow.risk.is_local_edit` and `is_sealed_dismiss` let the run
  gate's risk re-inference read a classified step from what the bundle seals:
  the explanation and its runtime checks.
- Postcondition kinds `local_edit` and `field_value`, evaluated by the
  replayer (`_local_edit_held`, `_field_value_shown`).
- Tests: `tests/test_local_edit_classification.py`. Each pattern has a
  positive case and a case where a write request was sent, which must
  refuse.

## OpenEMR referral, re-admitted on this branch

Run gate on the recorded referral with the API step admitted
(`--identity subject_name=lname`), compared with `main`:

| Policy | Violations on `main` | With local edits |
| --- | --- | --- |
| `permissive` | 12 | 1 |
| `clinical-write` | 32 | 9 |

What remains:

- `step_007` (`Control+a`): OpenEMR's background
  `POST /library/ajax/execute_background_services.php` landed in its window,
  so the strict rule leaves it for a person. Under `clinical-write` it also
  has no outcome check.
- `step_015` (the Contact tab): classified, but unarmed under
  `clinical-write`'s `prohibit_unarmed_clicks`.
- `step_039` (Create New Patient): unarmed, no identity contract and no
  record check on its GUI path.
- `step_039` and `step_040`: no duplicate-prevention (idempotency) key. That
  stays a person's choice.
- Encryption at rest, under the default and regulated profiles: seal the
  bundle.
