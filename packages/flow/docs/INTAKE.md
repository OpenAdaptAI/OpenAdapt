# Intake: fill parameters from a referral document

`openadapt-flow intake` reads a referral PDF, a fax or scan image, or an `.eml`
email and proposes a value for each of the workflow's parameters. It is an
optional step before `run`. The run itself is unchanged and makes no model
calls.

```bash
openadapt-flow intake ./new-referral referral.pdf --out params.json
openadapt-flow run ./new-referral --params-file params.json
```

## What it does

1. Reads text: the PDF text layer (with `pypdf` when installed, otherwise a
   built-in reader for simple PDFs), RapidOCR for images, and the plain-text
   body plus PDF and image attachments for email. Scanned PDFs have no text
   layer; export the page as an image to OCR it.
2. Matches each parameter against document labels. `insurance_no` matches
   `Insurance No:` and `Insurance Number:`. Add more labels with
   `--alias insurance_no="Member ID"`.
3. Validates each value against the parameter's type (`date`, `number`,
   `boolean`, `enum`, `string`) and normalizes it (dates become `YYYY-MM-DD`).
4. Holds back any field that is missing, invalid, ambiguous (two different
   values), or below the confidence threshold (default 0.9). On a terminal it
   asks you to confirm or correct each one. Otherwise it prints the pending
   fields, writes nothing, and exits with code 2. Pass `--accept NAME` after
   you have checked a value.

Confidence is a fixed score from the kind of match, not a probability. An
exact label match scores 0.95; a match on only the last word of the name (for
example `Name:` for `patient_name`) scores 0.6.

## Let the calling agent read it

When labels vary too much for matching, the agent that called the CLI can read
the document instead:

```bash
OPENADAPT_FLOW_SCRUB=on openadapt-flow intake ./new-referral referral.pdf --agent-request > request.json
# The agent answers request.json with a line number and label per field.
openadapt-flow intake ./new-referral referral.pdf --agent-response answer.json
```

The request carries the document after openadapt-privacy scrubbing, so the
agent sees placeholders instead of identifiers. It answers with line numbers,
and the values are read from the original text on your machine. Agent mode
refuses to run when scrubbing is off or openadapt-privacy is not installed
(`pip install 'openadapt-flow[privacy]'`). Scrubbing is only as good as the
underlying recognizer; review the request before you send it anywhere.

## Python

```python
from openadapt_flow.intake import extract_fields, read_document
from openadapt_flow.ir import Workflow

wf = Workflow.load("new-referral")
result = extract_fields(read_document("referral.pdf"), wf.param_specs)
for name in result.needs_confirmation():
    result.confirm(name, input(f"{name}: "))
params = result.to_params()
```
