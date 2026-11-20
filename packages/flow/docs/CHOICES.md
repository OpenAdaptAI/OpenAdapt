# Dropdowns, popups and browser dialogs

Web apps with no API build their forms from a few parts: a native `<select>`,
a custom list that opens under a button, a searchable select from a widget
library, a type-ahead whose options load from the server, a second list that
reloads when the first one changes, a date picker, a browser `confirm()`
before the save, and a short notice after it. Flow uses one browser-choice
model for these controls. Runtime choices
require observable committed-value evidence; unsupported controls and
unmapped held-out labels refuse. The evidence below states the measured limits.

## The model

A choice is a click on an option of a list that the previous action opened.
At run time Flow chooses the option by what it says, never by where it was,
and then reads the control back.

Recording decides that a click is a choice from two behaviours, not from the
widget library:

- the clicked element has an option role (`option`, `treeitem`), or it is one
  of several same-shaped siblings (same tag, shared classes); and
- it, or a floating container around it, appeared after the previous press.

Then, once the screen settles, the control the previous press hit must show
the option's label (its text, or its value for a text field). Without that
read-back the click stays an ordinary click. A native `<select>` is a choice
when the person changes it.

The compiler folds the opening click, an optional search typed into the list,
and the option click into one `SELECT_OPTION` step with an `OptionChoice`
contract (`openadapt_flow/ir.py`). Search folding also requires the same causal
opener gesture and exact input session on the opening click, search and final
choice. An auxiliary search-field click must carry that same link. Unlinked
legacy searches and steps with independent effects or dialog authority stay
explicit; folding preserves the opening control's identity evidence.

The contract fields are:

| Field | Meaning |
| --- | --- |
| `kind` | `native_select` or `listed_option` |
| `label` | The demonstrated option, the default value |
| `value` | Exposed option value, or the observed committed value scoped to the demonstrated label |
| `query` | `label` when the person typed a search: replay types the wanted label |
| `option` | The option's shape: a role, or a tag and the classes it shares with its siblings |
| `readback` | `select`, `control_text` or `control_value` |
| `commit_field` | Independent unique named form field for a listed choice; the query input cannot verify itself |
| `shows` | For a list that writes something other than the label (a calendar writes a date): the exact text, and the choice stays literal |

When the control belongs to a named form field you passed to `--param`, the
choice becomes that parameter, with the demonstrated label as its default.
The label is then never asserted in any postcondition. Choice metadata and
browser-dialog messages follow the recorder's capture-time secret boundary:
text that matches a declared secret, or cannot be checked because that
boundary is opaque, stops recording before it is retained.

## At run time

`openadapt_flow/backends/choice.py` runs every choice the same way:

1. The control is the element at the point the runtime resolved for the step.
2. A native select is read from the DOM. Anything else is clicked open; its
   options are the visible elements with the recorded shape that were not on
   screen before the click. If the person searched, the wanted label is typed.
3. Flow waits until the option list stops changing (options that load from
   the server, a list that reloads after another choice). It waits up to
   `settle_s` seconds, 8 by default.
4. Exactly one enabled option must show the wanted label or carry it as its
   `value`. Flow resolves that unique enabled option by label or value again
   immediately before actuation; a change in position cannot redirect the click.
   Then it is clicked, with real pointer input, so lists that choose
   on press, on release or on click all work.
5. The control must show the chosen label, and the independent committed
   field must equal an exact expected value. That value comes from the option,
   a hidden native select's label/value census, or the recorded field value
   when replaying the demonstrated label. A new label without an observable
   mapping stops before option actuation. Query echo, a merely nonempty field
   change and nearby label text cannot establish the committed value.
6. The original control and field must remain connected and agree after the
   page settles. Replaced elements do not inherit the old proof by position.

Native DOM selection sends no extra focus action. It validates the selected
option again after the page handles `input`, before emitting `change`. Drift
at that boundary stops the operation with uncertain delivery: the first
event has already been sent, so the runtime cannot claim that nothing happened.

Otherwise the step halts with a code and nothing after it runs:

| Code | Meaning |
| --- | --- |
| `choice_missing` | No option shows the wanted value |
| `choice_ambiguous` | More than one option shows it |
| `choice_disabled` | The only match, its option group, or the select (including a disabled fieldset) is disabled |
| `choice_unsettled` | The option census is still changing at its deadline; no option is chosen |
| `choice_changed` | The unique enabled option disappeared, duplicated, or became disabled after the list settled |
| `choice_commit_unknown` | No independent unique field or exact committed-value mapping is available |
| `choice_search_missing` | The person searched, but no search box took focus |
| `choice_control_missing` | No control at the resolved point |
| `choice_unverified` | The control does not show the choice afterwards |

Codes never include option text, which can be patient data.

## Browser dialogs

`alert`, `confirm` and `prompt` block the page. Recording reads the person's
answer where the page reads it, from the call's return value, and attaches it
to the click that raised the dialog. The compiled step then answers only a
dialog of the recorded kind with the same message (values and digits
generalized, see
[MULTI_SURFACE.md](MULTI_SURFACE.md)). In a headed recording Flow's own dialog
listener answers nothing, so the dialog stays for the person to answer
instead of being dismissed by Playwright first. (Tested headless with the
answer coming from outside the recorder; no headed run is in the evidence.)

A recorded answer joins only a pending click from the same producer document.
A foreign document or missing ownership stops recording. New typed dialog
contracts also require a single browser document before input. A frame-tree or
navigation change invalidates the armed answer; ambiguous producer ownership
stops with `dialog_scope_unverified`. An idle recording
keeps a pending click for 5 seconds; that delay does not grant another
document the click's authority. New recordings also bind the dialog kind,
so an alert or prompt cannot borrow a confirm answer merely by showing the
same message.

A program compiled from such a recording (`dialogs_recorded` in
`workflow.json`) has the backend answer every dialog from the first action
on. A dialog that no step expects is dismissed, never accepted, and the run
stops with `dialog_unexpected`, checked after each step and before the next.
An unexpected `beforeunload` warning is dismissed as well: unsaved edits
stay on the original page and the run stops. Programs compiled before this
keep their previous behaviour. A `prompt()` that needs typed text stops
the recording; it is not a recordable action. A screenshot taken while a
dialog is open waits for it to be answered (up to 2 minutes for a person in
a headed recording).

## Other popups

- **Modal dialogs** in the page (`role=dialog`) are screens. Their buttons are
  recorded as clicks; a modal that shows up only at run time covers the next
  target and the step halts.
- **New tabs and windows** are surfaces ([MULTI_SURFACE.md](MULTI_SURFACE.md)).
- **Iframes**: a same-origin frame works like the page; a cross-origin frame
  stops the recording.
- **Toasts and notices** that come and go are not asserted when they hold a
  value that changes per run.

## Signed-in sessions after a manual 2FA login

Sign in by hand, once, in a Chromium profile kept for Flow, then let Flow
attach to that browser to record and to run. The password and the second
factor never pass through Flow, and the session cookies stay in that profile.
Setup is in [BROWSER_RECORDING.md](BROWSER_RECORDING.md#attach-an-existing-signed-in-browser),
and runs use the same browser (`run --browser-cdp-endpoint`, see
[Run in the same signed-in browser](BROWSER_RECORDING.md#run-in-the-same-signed-in-browser)). When the session has expired, the first step does not find its
screen and the run halts before any input; sign in again and rerun.

## Limits

- Governed and qualified runs that require a typed delivery receipt for
  every action refuse choice steps for now: a choice is proven by its
  read-back, not by a receipt. The runtime refuses these contexts before
  delivering any part of the choice action.
- Reading the selected label proves the input, not the absence of an
  auto-save or another business effect. Choice steps keep the existing
  option-commit risk policy: consequential context is irreversible, and an
  otherwise ambiguous option commit requires review. Qualification must
  establish any business effects independently.
- A segmented date field (separate day, month and year parts) is not
  measured.

- A list chosen with the keyboard only (arrow keys and Enter, no click on an
  option) is recorded as those keys, not as a choice.
- Calendar choices replay the demonstrated date only, and halt when the field
  does not show exactly that date. To vary the date, type it into the field
  when the picker allows typing.
- Multi-selects stop the recording.
- An option whose text differs from the wanted value only by extra text
  ("Cardiology - OPD 2") does not match; pass the full option text.
- Measured on the synthetic fixture in
  [benchmark/choice_widgets](../benchmark/choice_widgets/README.md) and on the
  real apps listed there.
