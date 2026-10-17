# Qualify a workflow

Before a workflow writes to a real system without a person watching, qualify
it: run it against the normal case and deliberate faults, and check that it
completes with the write confirmed by reading back the system of record, and
halts on every fault. The terms (qualify, certify, certification policy) are
defined in [QUALIFICATION_TERMS.md](QUALIFICATION_TERMS.md).

You need three things:

1. A compiled bundle. A task can be learned from one or more demonstrations:
   `openadapt-flow record`, then `openadapt-flow compile` (or `induce` for
   several recordings).
2. A test copy of the application you can reset between trials. Qualifying
   performs real writes.
3. A `verify.yaml` that says how to read the result back.

Then:

```bash
openadapt-flow qualify path/to/bundle --verify verify.yaml \
  --out runs/qual-1 --certify clinical-write
```

`qualify` checks `verify.yaml` and the bundle before it runs anything, and
lists every problem it finds (no reset, no read-back, a save step without an
identity check, inputs the record needs but the bundle does not have). It then
runs the six cases, stops at the first case that does not behave as required,
and, with `--certify`, signs the result under the policy.

## verify.yaml

This is the file for the local OpenEMR fixture
(`benchmark/openemr_local/verify.yaml`), shortened:

```yaml
version: 1

app:
  name: OpenEMR (local benchmark fixture)
  url: ${OPENEMR_UI_URL:-http://127.0.0.1:9301}
  version: "openemr@7c96c8eefe46"
  # Sign-in and fixture reset are app-specific; name a Python function.
  session: benchmark.openemr_local.qualify_hooks:open_session
  reset:
    python: benchmark.openemr_local.qualify_hooks:reset

# Where the write is read back from. Not the screen.
system_of_record:
  kind: rest
  base_url: ${OPENEMR_API_URL:-https://127.0.0.1:9300}
  records_path: /apis/default/api/patient?_limit=1000
  records_key: data
  session: benchmark.openemr_local.qualify_hooks:oracle_session

# The record the save must create, and which input fills each field.
record:
  name: patient
  match: [email, lname]     # how to find the new record
  duplicate_key: email      # an existing record with this value means "already done"
  fields:
    email: email            # field: input parameter name
    lname: lname
    fname: fname
    DOB: DOB
    state: {literal: MA}    # a fixed value

save_step: auto             # or a step id, e.g. s27

inputs:
  source: recorded          # or: file, with file: inputs.json

campaign:
  id: openemr-local-patient-registration-v2
  trials: 3
  policy: clinical-write
```

What each part does:

- **app**: where the application is, its version (recorded in the report),
  how to start a signed-in session, and how to reset it. `reset` takes exactly
  one of `command` (a shell command), `http` (`method`, `url`, `body`) or
  `python` (`module:function`). For a web app without sign-in you can leave
  out `session`; `qualify` opens a fresh browser at `url`.
- **system_of_record**: the read-back. `kind: rest` reads `records_path` under
  `base_url` and takes the list at `records_key`. Authentication is
  `auth: {bearer_env: NAME}`, `{header: X-Api-Key, value_env: NAME}` or
  `{basic_env: NAME}`, with the secret in the named environment variable.
  `kind: sql`, `fhir` and `graphql` take the same keys as the effects section
  of a deployment file ([EFFECT_KIT.md](EFFECT_KIT.md)). An on-screen check is
  refused: it cannot tell a real write from a screen that only looks right.
- **record**: what the save must write. `match` finds the record, `fields`
  says which input each field must equal, and `duplicate_key` is the field
  that makes a second write a duplicate. Leave `record` out when the compiler
  already bound the save's record checks from the demonstration.
- **save_step**: `auto` picks the single irreversible step. If there are none
  or several, `qualify` says so and asks you to name one.
- **inputs**: the test inputs. `recorded` reuses the demonstration's values;
  `file` loads a JSON object; `params` overrides single values; `unique`
  lists inputs to suffix per campaign, so reruns against a database that was
  not reset do not collide.
- **campaign**: the id written into the report, trials per case, the cases
  (all six by default) and the policy `--certify` uses when given no name.

`${NAME}` and `${NAME:-default}` read environment variables. A missing
variable without a default is an error.

## What you get

```
runs/qual-1/
  bundle/                     the bundle, with the qualification project
  cases/<case>/trial-NN/      run report, screenshots, oracle.json
  qualification/report.json   the signed result
  qualification/verdict.txt   the result, or why certification refused
  campaign-summary.json       one row per trial
  timing.json                 wall-clock time per trial and in total
  QUALIFICATION.md            a summary to read
```

With `--run-params inputs.json` it also resets the application and runs the
certified bundle once, as the runner would, into `runs/qual-1/certified-run/`.

## Limits

- Web only today. The ambiguity, wrong-identity and stale-identity faults are
  staged in the browser, on the save button the bundle's own selector finds.
  A fault driver for pixel surfaces (Windows, RDP, Citrix) is the next piece;
  until it exists, `qualify` refuses `app.target` other than `web`. The two
  effect faults do not depend on the surface.
- Sign-in, reset and API sessions are often app-specific. They are Python
  hooks named in `verify.yaml`, not configuration.
- The keys are local test keys. See
  [QUALIFICATION_TERMS.md](QUALIFICATION_TERMS.md#certify).
- A pass covers the environment it ran in and nothing more.

## Compiler fix-ups

`compile` now applies four fix-ups by default and lists each change in the
bundle's `compile_notes.json`:

- Enter after typing an option into a dropdown is dropped (web only; the
  typed text already selected the option).
- Ctrl+A on an empty field that changed no pixel is dropped.
- A click into a text field gets a check that the field is still where it was.
- A button with no label evidence gets a crop of itself as its identity check.

These used to be hand edits in a per-workflow script.
