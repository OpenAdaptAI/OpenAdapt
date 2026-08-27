# Create your first workflow

> **Current product path.** The canonical walkthrough is at
> [docs.openadapt.ai/get-started/first-workflow](https://docs.openadapt.ai/get-started/first-workflow/).

## Start in OpenAdapt Desktop

[Download OpenAdapt Desktop](https://openadapt.ai/download), open it, and grant
the screen and input permissions it requests. Desktop guides you through the
first recording, review, supervised run, and saved result.

Choose one small task in a real application. Use test data and keep the task
read-only. Opening a known test record and checking one value works well. Don't
start with a task that saves, submits, creates, deletes, or sends data.

## Record the task once

Select the application and describe the result you expect. Perform the task
once, then stop when that result is visible. Desktop compiles the recording
into an inspectable workflow.

Keep the demonstration direct. Exploratory clicks make the intended path less
clear.

## Review before the first run

Read every compiled step and detected input. Confirm that the task uses test
data and that each action is read-only. If an action can change state, has
unknown risk, or needs an identity, effect, or policy contract, open the
qualification review before OpenAdapt acts.

## Run it once while you watch

Keep the application visible for the complete run. Stop if OpenAdapt opens the
wrong record, leaves the demonstrated path, or reaches an unexpected screen.

The first run stays supervised. It doesn't qualify the workflow for a real
write or unattended use.

## Check the saved result

Confirm the expected value in the application. Then inspect the run saved in
Desktop. The report must show the expected test application and data, no write
or consequential action, and the evidence for every step.

If the report disagrees with what you saw, keep the recording and report. Fix
or record the workflow again before another run.

## Qualify wider use

Complete [workflow qualification](https://docs.openadapt.ai/guides/qualify-a-workflow/)
before any state-changing, unknown, consequential, irreversible, or unattended
use. Qualification binds the exact application and environment, action risks,
identity checks, independent effect checks, fault cases, and policy to one
workflow version.

## Optional command-line installation check

The synthetic quickstart checks the local launcher, engine, browser driver,
and effect verifier. It doesn't touch your application or qualify your
workflow.

Use Python 3.10, 3.11, or 3.12:

```bash
python -m pip install --upgrade openadapt
openadapt quickstart
```

A healthy run returns `VERIFIED` for the bundled MockMed task. It makes no
model or Cloud call. Inspect its local artifacts if you want to see the bundle
and report formats:

```bash
openadapt flow visualize openadapt-quickstart/bundle --out graph.html
openadapt flow lint openadapt-quickstart/bundle
```

The optional rejected-write simulation is an advanced effect-verification
check. It is documented in the [CLI reference](../cli.md#advanced-effect-verification-simulation),
after the real first-workflow path.
