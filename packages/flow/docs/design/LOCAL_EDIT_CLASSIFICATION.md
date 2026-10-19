# Classifying local edits without a person (proposal)

Status: proposal. Not merged; the founder decides.

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

## The proposal

The compiler classifies three local-edit patterns itself, and only when the
recording shows the action stayed local. Each classified step gets an
automatic outcome check that halts the run if, at run time, the action did
anything else.

| Pattern | Evidence the compiler needs (all of it) | Outcome check added to the step |
| --- | --- | --- |
| Select-all before typing: `Control+a` / `Meta+a` | The previous step clicked a single text field (structural role `textbox`, `searchbox` or `spinbutton`); the next step types into it; URL, title and page count unchanged across the key; no write request during the key (when the recording has network capture) | `local_edit`: after the key, the focused element is still that field, and URL, title and page count are unchanged. The next step's typed-input read-back then checks the field's whole value. |
| Confirm a typed option: `Enter` | The previous steps clicked a dropdown (role `combobox` or `listbox`) and typed an option's label (a parameter or a literal); URL, title and page count unchanged; no write request during the key (network capture **required**: `Enter` is a submission key) | `local_edit` as above, plus `field_value`: the focused dropdown shows the typed label (the run's parameter value). |
| In-form tab: a click on a tab | Role `tab`, or a `button` whose recorded click changed only the visible panel: URL, title and page count unchanged, no write request (network capture required), and the next step acts in the same frame | `local_edit` (URL, title and page count unchanged; focus need not stay). |

What stays the same:

- **The risk rule.** A step whose evidence is missing or mixed (a URL change,
  a write request in the window, no network capture for a submission key, a
  key that isn't one of these, a field role the compiler can't read) keeps
  `risk_review_required` and goes to a person, exactly as today.
- **Write-shaped context still wins.** A key or click whose retained text
  names a consequential write (`submit`, `save`, `delete`, ...) classifies
  irreversible before any of this runs.
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
check unless that policy exempts `local_edit` clicks (an open question below).

## Why this is safe enough

- The classification is backed by what the demonstration did, not by the
  label. A key that submitted the form in the recording (a POST, a URL change)
  is never classified.
- The outcome check runs every time. If the same key does something else at
  run time (the app changed, a different field had focus), the step fails its
  postcondition and the run halts at that step. The halt comes after the key
  was pressed, which is the residual risk: a key that, at run time only,
  commits something. For the three patterns that requires the app to have
  changed the field's behavior since the recording, and the first write it
  makes is still caught by the save's record check or by the next step's
  checks.
- Evidence is sealed in the bundle (the step's explanation and postconditions
  are part of the content digest), so a later edit to a classification
  changes the digest and needs a new admission.

## Open questions for the founder

1. Accept the residual risk above for these three patterns?
2. Should `clinical-write`'s `prohibit_unarmed_clicks` exempt a `local_edit`
   tab click (it writes nothing and stays in the form), or must a person or the
   compiler still arm it with an identity check? Today it must be armed.
3. Without network capture, `Control+a` is classified on the structural and
   URL evidence alone (select-all in a focused text field isn't a submission
   key). `Enter` and tab clicks need network capture. Is that the right line?

## Implementation sketch

- `openadapt_flow.risk.local_edit_classifications(steps, events, network)`:
  the three rules, returning `{step_id: (explanation, evidence)}`.
- `compile_recording` applies them after the per-step inference and before
  `risk_overrides`, and adds the outcome checks.
- Two postcondition kinds: `local_edit` (structural: focus and URL, title,
  page count) and `field_value` (the focused dropdown's label equals a run
  parameter). Both are evaluated by the runtime like the existing structural
  kinds, and both refuse rather than pass when the backend can't observe them
  (unlike `url_changed`, which passes unverified).
- Qualification's draft classification reads the same evidence, so a
  compiled `local_edit` step drafts as `reversible` instead of `unknown`.
- Tests: each pattern classifies with full evidence; each missing piece of
  evidence leaves the step for a person; each outcome check halts when the
  action did something else (focus moved, URL changed, wrong option).
