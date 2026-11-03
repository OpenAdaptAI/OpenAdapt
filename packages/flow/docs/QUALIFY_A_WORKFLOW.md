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
runs them again with structure lookups turned off (the pixels-only pass),
and, with `--certify`, signs the result under the policy.

The pixels-only pass decides the qualification when `app.surface` is
`remote_display` or `unknown`: the workflow will run on a remote display
viewed from outside the session, where Flow sees only pixels. Then the six
cases run only once, with structure off. For any other surface the signed
cases use structure and the pixels-only pass rate is printed for
information. See
[Lookup capabilities](SURFACES.md#lookup-capabilities-structure-or-pixels).

## verify.yaml

This is the file for the local OpenEMR fixture
(`benchmark/openemr_local/verify.yaml`), shortened:

```yaml
schema_version: openadapt.verify/v1
environment_id: openemr-local-fixture-7c96c8eefe46   # optional
minimum_effect_strength: independent_system_of_record

app:
  name: OpenEMR (local benchmark fixture)
  url: ${OPENEMR_UI_URL:-http://127.0.0.1:9301}
  version: "openemr@7c96c8eefe46"
  # Where the workflow will run in production: local (default),
  # remote_in_session, remote_display (pixels only) or unknown.
  surface: local
  # Sign-in and fixture reset are app-specific; name a Python function.
  session: benchmark.openemr_local.qualify_hooks:open_session
  reset:
    python: benchmark.openemr_local.qualify_hooks:reset

# Where the write is read back from. Not the screen. The read is filtered
# by a duplicate-key field only, so it holds every chart that could be a
# duplicate (see duplicate_key below).
verifier:
  kind: rest
  base_url: ${OPENEMR_API_URL:-https://127.0.0.1:9300}
  records_path: /apis/default/api/patient?lname={lname}
  path_params:
    lname: {param: lname}
  records_key: data
  session: benchmark.openemr_local.qualify_hooks:oracle_session

# The record the save must create, and which input fills each field.
# Shorthand for the effects list (see below).
record:
  name: patient
  match: [email, lname]     # how to find the new record
  duplicate_key: [lname, fname, DOB]  # another chart with these means a duplicate
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

The first block is the read-back contract from RFC-0002
(`openadapt.verify/v1`): `verifier`, `effects` (or the `record` shorthand) and
`minimum_effect_strength`. The same contract can judge a run by any executor.
`app`, `save_step`, `inputs` and `campaign` are what a qualification campaign
adds.

What each part does:

- **app**: where the application is, its version (recorded in the report),
  how to start a signed-in session, and how to reset it. `reset` takes exactly
  one of `command` (a shell command), `http` (`method`, `url`, `body`) or
  `python` (`module:function`). For a web app without sign-in you can leave
  out `session`; `qualify` opens a fresh browser at `url`.
- **verifier**: the read-back. `kind: rest` reads `records_path` under
  `base_url` and takes the list at `records_key`. Authentication is
  `auth: {bearer_env: NAME}`, `{header: X-Api-Key, value_env: NAME}` or
  `{basic_env: NAME}`, with the secret in the named environment variable.
  `kind: sql`, `fhir` and `graphql` take the same keys as the effects section
  of a deployment file ([EFFECT_KIT.md](EFFECT_KIT.md)). An on-screen check is
  refused: it cannot tell a real write from a screen that only looks right.
- **effects**: the save's checks as an effect contract list, the same
  entries an `effect_contract.yaml` holds:

  ```yaml
  effects:
    - kind: record_written
      match: {email: {param: email}, lname: {param: lname}}
      expected_count: 1
      idempotency_key: {param: email}
      key_field: email
    - kind: field_equals
      match: {email: {param: email}}
      field: DOB
      value: {param: DOB}
  ```

- **record**: a shorter way to write the same list. `match` finds the record,
  `fields` says which input each field must equal, and `duplicate_key` is the
  field that makes a second write a duplicate. A list, such as
  `duplicate_key: [lname, fname, DOB]`, names fields that are a duplicate only
  together; the check then fails when more than one record carries all of
  them, counting records that were already there. Those other records are
  compared ignoring case and extra spaces (`SMITH` and `Smith ` agree), and a
  date read back as a midnight datetime counts as its date; the saved record
  itself is compared exactly. A list of one field works the same way, unlike
  the plain string, which counts only new records when the check does. Because
  existing records count, the read must hold all of them: filter
  `records_path` or `sql_query` by key fields only, and do not cap it (a REST
  `_limit` needs `records_total_path` or `records_next_path`; SQL takes no
  `LIMIT`). `qualify` and `verify-run` refuse a read that breaks either rule.
  A list key needs `expected_count: 1`. If your database driver returns a
  DATE column as a datetime, the saved record's exact comparison on that
  field fails and the check stops the run rather than confirming it. It expands to one
  `record_written` plus a `field_equals` per other field. Give `effects` or
  `record`, not both; leave both out when the compiler already bound the
  save's checks from the demonstration.
- **minimum_readback**: `system_of_record` or `separate_session`
  ([`READBACK_LADDER.md`](READBACK_LADDER.md)). `qualify` refuses a verifier
  that reads weaker, and writes the value onto the workflow so runs enforce
  it too. A same-session reopen or a same-screen check is never enough.
  `minimum_effect_strength` is the older setting and still works:
  `independent_system_of_record` (the default when neither is given) equals
  `system_of_record`; `independent_session` equals `separate_session`. Give
  one, not both.
  Certification needs more than the minimum, though: see below.
- **save_step**: `auto` picks the single irreversible step. If there are none
  or several, `qualify` says so and asks you to name one.
- **inputs**: the test inputs. `recorded` reuses the demonstration's values;
  `file` loads a JSON object; `params` overrides single values; `unique`
  lists inputs to suffix per campaign, so reruns against a database that was
  not reset do not collide.
- **campaign**: the id written into the report, trials per case, the cases
  (all six by default) and the policy `--certify` uses when given no name.
- **business_rules** (optional): rules the application itself enforces, which
  the workflow must not click through. The six cases inject faults; a
  business-rule case injects nothing. After the reset, `seed` (one of
  `command`, `http` or `python`, like `reset`) creates the record the rule
  protects, and the run uses `params` that trigger the rule (they replace the
  matching inputs). The run must stop before the save (`expect:
  HALTED_BEFORE_EFFECT`, the only value), at the place the rule names
  (`halt_at`), and the read-back must show no new or changed record
  (`readback: no_mutation`, the only value). Each rule is case `rule-<id>`:

  ```yaml
  business_rules:
    - id: existing-duplicate
      description: An existing chart for the same person; the app's duplicate
        check lists it, and the workflow stops before saving.
      seed:
        python: my_app.hooks:seed_existing_patient
      params: {fname: Rosa, lname: Delgado, DOB: "1958-03-22"}
      expect: HALTED_BEFORE_EFFECT
      readback: no_mutation
      halt_at: empty_result_guard
      halt_reason: No matches were found.
  ```

  `halt_at` is `empty_result_guard` (the default: a step that checks the
  empty search result the compiler mined from a confirmation dialog, see
  `compile_notes.json`), `before_save` (any step up to the save, before its
  action; needs `halt_reason`), or a step id. `halt_reason`, when given, must
  be in the halt's reason. A halt anywhere else fails the trial: the run did
  not stop for the application's rule.

  Each trial reads the records before and after the seed and fails unless
  the seed created at least one; a verifier that cannot read the records
  cannot run a rule. Inputs a rule names that the workflow does not take,
  and a `halt_at` that names no step, are refused before any run.

  The rules run only with `--certify`, after the six cases pass, `trials`
  times each, on a certified copy of the bundle through the ordinary run gate.
  The bundle itself is saved certified only after every rule passes; a failed
  trial, or anything that raises (a seed or reset error, a run-gate refusal,
  a crash, Ctrl-C), withdraws the certification and fails `qualify`. With
  `--certify`, name all the `rule-*` cases or none. Their trials are in
  `result.json` `cases` (`fault_class: business_rule`), which the admission
  envelope binds. They are not signed case results in the qualification
  project: a certification made outside `qualify` does not run them.

`${NAME}` and `${NAME:-default}` read environment variables. A missing
variable without a default is an error.

## What you get

```
runs/qual-1/
  bundle/                     the bundle, with the qualification project
  cases/<case>/trial-NN/      run report, screenshots, oracle.json
  pixels-only/<case>/trial-NN/  the unsigned pixels-only pass (when the
                              signed cases used structure)
  qualification/lookup.json   the lookups the cases used, the surface, and
                              the pixels-only pass rate
  qualification/result.json   openadapt.qualification-result/v1
  qualification/admission.local.json   the result bound into an admission
                              envelope (with --certify, 3+ trials per case)
  qualification/report.json   the project's coverage report
  qualification/verdict.txt   the result, or why certification refused
  campaign-summary.json       one row per trial
  timing.json                 wall-clock time per trial and in total
  QUALIFICATION.md            a summary to read
```

`result.json` has one entry per trial: what the case expected the system of
record to show (`CONFIRMED` for the normal case, `REFUTED` for a fault), what
the read-back showed, how it was read, and digests of the trial's run report
and read-back. It counts silent incorrect successes (must be 0) and
over-halts, and reports model calls in two numbers: `executor_model_calls`
(calls the replay made; qualification requires 0) and `verifier_model_calls`
(calls made while judging; a read-back makes none). It holds no record
values, workflow names or reason text.

A fault case passes only when both hold: the read-back shows nothing landed
(`REFUTED`, outcome `halted_before_effect`), and the engine halted. A store
that shows nothing while the engine reported success counts as a silent
incorrect success.

Keep `result.json` inside the customer's systems when the campaign ran
against real records. For now, each case's `receipt_sha256` is a plain,
unsalted SHA-256 of that trial's `report.json`, and `evidence_digest` is the
same over its read-back result. Those files hold the inputs and record
details the run used, so someone with a guess at the values could confirm it
against the digest. Effect receipts (RFC-0002), which bind identity values in
a form that does not allow this, will replace these digests. A campaign on
made-up test data, like the OpenEMR fixture or MockMed, has nothing to
protect.

How each read is labelled (`verification_tier`):

- `independent_readback`: the system of record read without going through
  the UI: `rest`, `sql`, `fhir`, `graphql`, or a file export.
- `screen_readback`: anything that reads through the application's UI,
  including `ui_readback`, which signs in to a separate UI session.
- `unchecked`: no read happened.

To certify, the normal case must reach `verified` at `independent_readback`,
and every fault case must show `REFUTED` (nothing landed) at
`independent_readback`. A workflow whose only read-back is `ui_readback` can
run the cases but cannot be certified.

`verify_config_sha256` identifies the contract the cases were judged
against. It is SHA-256 over the verifier's identity and the effective effects
list bound to the save step for the campaign: the `effects` you wrote, the
expansion of `record`, or the checks the compiler mined from the
demonstration, after the duplicate key is applied and before input values are
filled in. It is not a hash of the file, so the same contract written two
ways (an `effects` list or the equivalent `record`) gets the same digest.

With `--certify`, its digest is bound into the existing
`openadapt.qualification-admission/v1` envelope (`campaign.outcomes_sha256`;
`campaign.oracle_contract_sha256` is the verify-config digest) and signed with
the local-dev qualification key. Production trust maps refuse that key; a
production admission is signed by the production authority from the same
result. The envelope needs at least three trials per case.

With `--run-params inputs.json` it also resets the application and runs the
certified bundle once, as the runner would, into `runs/qual-1/certified-run/`.

## Limits

- Web only today. The ambiguity, wrong-identity and stale-identity faults are
  staged in the browser, on the save button the bundle's own selector finds.
  A fault driver for pixel surfaces (Windows, RDP, Citrix) is the next piece;
  until it exists, `qualify` refuses `app.target` other than `web`. The two
  effect faults do not depend on the surface.
- A web workflow can't yet be qualified for a remote-display surface. With
  `app.surface: remote_display` the cases run with structure lookups off, and
  the browser delivers a click only through an action bound to the page
  element, so the first click that pixels found stops the trial. The
  informational pixels-only pass on a `local` surface fails the same way
  today. See
  [Lookup capabilities](SURFACES.md#lookup-capabilities-structure-or-pixels).
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
- A button with no label evidence gets a crop of itself as its identity
  check, but only when the bundle will run where that crop can be verified:
  pass `compile --pixel-verify-enabled` when the deployment sets
  `runtime.pixel_verify_enabled`. Without it, the pixel check can only reject
  a crop, never accept one, so a crop-only check would stop an irreversible
  step on every run. The button then compiles as it did before.

These used to be hand edits in a per-workflow script.
