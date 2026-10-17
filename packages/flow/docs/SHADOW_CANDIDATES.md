# Shadow-mined candidates

OpenAdapt Capture's shadow mode (off by default) can notice that a person
repeated the same cross-application task and, when the person clicks
**Review draft**, export the matching episodes. This page explains what Flow
does with that export. Capture's
[SHADOW_MINING.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/SHADOW_MINING.md)
covers how the episodes are mined and the privacy posture.

The code is `openadapt_flow/compiler/motif_induction.py`.

## What goes in

An `openadapt.shadow.episodes/v1` JSON document. Each episode is a list of
value-free symbols: an application, a control role, keyed digests in place of
labels and typed values, a coarse URL pattern, and short plain-text labels for
buttons and menu items. There are no screenshots, coordinates, or values.

The schema is strict. `induce_candidate` refuses a document with an unknown
field, a malformed digest, a plain-text label that contains digits or `@`, an
unknown step kind, or a status other than `candidate`.

## What comes out

```python
from openadapt_flow.compiler.motif_induction import induce_candidate, write_candidate

candidate = induce_candidate(export)          # dict or ShadowEpisodesExport
write_candidate(candidate, "shadow-candidates/abc")
```

`induce_candidate` turns each episode into a linear trace and runs the
existing multi-trace induction (`induce_program`) over them. Induction infers
parameters, loops, and optional steps, or refuses, exactly as it does for
recorded demonstrations. The result is a `MotifCandidate`:

| Field | Meaning |
|---|---|
| `status` | Always `candidate`. |
| `admitted`, `runnable` | Always `False`. The model rejects any other value. |
| `induction_certified` | Whether induction found the episodes structurally consistent. This is a statement about shape only. |
| `draft` | The induced program, or `None` when induction refused. |
| `record_next` | What to record or decide next. The first item is always "record this task once with OpenAdapt Capture". |
| `proposed_checks` | Verification checks the episodes suggest. |
| `admission_path` | The steps that turn the candidate into a program. |

`write_candidate` writes `candidate.json` and, when there is a draft,
`draft-workflow.json`. It never writes `workflow.json`, so no Flow command
treats the folder as a bundle, and it refuses a folder that already holds one.

## Why a candidate can't run

- **No anchors.** Shadow mining keeps no pixels, so every anchor in the draft
  is a placeholder (`shadow-unrecorded.png` at 0,0).
- **No values.** Parameter examples are digests such as `<email:5c91…>`.
- **Writes need review.** A click on a Submit-, Save-, or Send-like control,
  or a click right before a form submit, is marked `irreversible` with
  `risk_review_required`, so certification refuses it until a person reviews
  it.
- **Proposals aren't contracts.** A proposed check (`file_written`,
  `record_written`, `row_added`, `page_reached`) has `status="proposed"` and
  `admitted=False`, and is never attached to a step as an effect. The episodes
  that suggested a check can't approve it.

## How a person admits it

1. Record the task with Capture.
2. Compile each recording with `flow compile`.
3. Run `flow induce` over the recordings, using the draft only as a guide.
4. Confirm or reject each proposed check, then run `flow qualify`.

## Example

The test fixtures in `tests/fixtures/shadow/` come from Capture's miner on
synthetic streams:

- `report_mail`: save a spreadsheet, then mail it. Induction emits a draft
  with two parameters (recipient and subject) and proposes two checks (the
  file changes; a message is sent).
- `invoice_exact`: copy an invoice number from a spreadsheet cell into a web
  form. The cell differs every time, so induction refuses with
  `ambiguous_selection` ("which row?"), and the candidate has no draft.
- `invoice_variant`: the same task with an extra check box in one episode.
  Induction reads it as an optional step.
