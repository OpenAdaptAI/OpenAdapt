# Spike: compiling the OpenEMR referral save to the app's own HTTP requests

Status: engineering spike, 18 October 2026. Local OpenEMR 8.0.0.3 test copy,
synthetic patients only. Not a product feature and not a published claim.

## Question

Can the save step of the recorded OpenEMR referral be compiled into the HTTP
requests the browser made, replayed without a browser using a fresh sign-in
and the run's values, and still pass the same independent record check? How
much faster is it?

## What was built

1. **Capture** (`capture_har.py`). One replay of the published referral
   program (the DOM-identity bundle, `replay-dom` in
   `../openemr_referral_timing`) with Playwright HAR recording on.
   `results/har_summary.json` is a redacted list of the 29 non-static requests.
2. **Compile** (`formtier.py`, `compile_api.py`). From the HAR, the compiler
   finds the save, the duplicate check and the sign-in. It then explains each
   of the 105 fields in the save body by where its value came from, or it
   refuses to compile (`api_bundle/compile_report.json`):

   | Source | Fields | Example |
   | --- | ---: | --- |
   | Recorded parameter | 11 | `form_lname` = `{lname}` |
   | Select option chosen by a recorded label | 1 | `form_state`: label `{state_label}` ("Massachusetts") gives value `MA` |
   | Server-issued session value | 1 | `csrf_token_form` |
   | The form's own default | 92 | `form_care_team_status` = `active` |

   The output is a one-step bundle (`api_bundle/`). Its step carries the
   engine's existing `ApiBinding` with `on_unavailable="halt"` and the same
   system-of-record effect contract as the GUI save step, so it never falls
   back to the GUI. The 41 GUI steps are folded into one request.
3. **Run** (`SessionFormActuator` in `formtier.py`). It implements the
   engine's API actuator protocol and runs through the unchanged `Replayer`
   with a backend that raises on any screen or input call. For each run it:
   signs in with a new session, reads the New Patient form (for the CSRF
   token, the current defaults and options, and a drift check), checks the
   form's client-side required fields, runs the duplicate check, and then
   sends the save once. The engine's API tier then confirms the record with
   the same REST `EffectVerifier` the GUI run uses.

### What the capture showed

| Step | Request | Auth |
| --- | --- | --- |
| Sign in | `POST /interface/main/main_screen.php?auth=login`, form-encoded user and password | Returns the `OpenEMR` session cookie; no CSRF token on this request |
| Duplicate check | `GET /interface/new/new_search_popup.php?mf_fname=…&mf_lname=…&mf_DOB=…` | Session cookie |
| Save | `POST /interface/new/new_comprehensive_save.php`, 105 form fields | Session cookie plus `csrf_token_form`, one token per session |
| Success | 200 with a script that redirects to `demographics.php?set_pid=<n>` | |

The GUI program's check before Confirm (the text "No matches were found."
must be on screen) carries over directly. The server writes that string into
the duplicate-check response only when the search finds no one. The compiled
step requires it in the response, so the pre-save check still works without
a screen.

## Results: 20 browser runs and 20 API runs

Five blocks. Each block resets the database and seeds the existing Rosa
Delgado chart, then runs four referrals in rotation; r6 (possible duplicate)
came up twice per arm. The arms were interleaved, and the order alternated by
block. Both arms used the same engine commit (8d0758b9) in warm mode, with
imports, Chromium launch and the OCR engine set up once beforehand. The clock
runs from a new browser context or HTTP session to the end of the record
check. The record check is the test setup's own SQL read-back after a 1.0 s
settle, the same rule as the October study: exactly one new chart with 12 of
12 fields, or no new chart for r6. No screen recorder ran in either arm.

| | Browser program | API step |
| --- | --- | --- |
| Correct | 19 of 20 | 20 of 20 |
| New referrals saved, 12 of 12 | 17 of 18 | 18 of 18 |
| r6 possible duplicate: no new chart | 2 of 2 | 2 of 2 |
| Median time per run, correct new referrals | 23.9 s (p10–p90 23.0–27.1) | 2.14 s (2.04–2.37) |
| Sign-in and form, median | 1.48 s | 0.49 s (sign-in 0.32 s, form read 0.17 s) |
| Engine time after setup, median | 21.0 s | 0.34 s (duplicate check 0.035 s, save 0.036 s; the rest is the engine's record snapshot before the save and its check after) |
| Record check (settle + SQL), median | 1.34 s | 1.29 s |

- **Speedup:** the median run is 11.2 times faster (bootstrap 95% CI 10.8 to
  12.1), 21.8 s less per run. Without the shared 1.0 s settle and SQL check,
  it is 22.5 s against 0.85 s, about 27 times.
- **The one browser miss** (block 3, r2) was a fail-closed stop with nothing
  saved. At the final Confirm click, the engine's identity check found the
  button by position but saw "Emergency Contact" text around it, so it
  refused. The one OpenAdapt miss in the October data was also a stop with
  nothing saved, at the step before. The API step has no screen to misread.
- The browser arm's 23.9 s is faster than the published 29.7 s. That figure
  also counted the screen recorder and the audit-quiescence wait, and it ran
  on an older engine.

Raw data: `results/runs.csv` (one row per run), `results/runs/*.json` (per-run
results, including the API stage timings and every field compared), and
`results/figures.json` (from `compute_figures.py`).

## Where a recorded request is not enough

`results/probes.json`, run against the same test copy:

| Case | What the server does | What the API step does |
| --- | --- | --- |
| CSRF token from another session | Rejects ("Authentication Error"), nothing written | Never sends one: it reads a fresh token from the form in its own session |
| No CSRF token | Rejects, nothing written | As above |
| Token lifetime | Same token for the whole session, also after a save | One sign-in per run; a recorded token is never reused |
| Duplicate check skipped, r6 posted directly | **Saves a second Rosa Delgado chart.** The duplicate check runs only in the browser's script | Runs the check itself and stops before the save when the server finds a possible match |
| Same save sent twice | **Two charts**; the endpoint has no idempotency | Sends once; after the POST starts it never retries (engine HALT rule) |
| No signed-in session | "Site ID is missing from session data!", nothing written | Treated as a rejected write (HALT), confirmed by the record check |
| Form fields changed since the recording | n/a | Refuses before sending |
| Admin makes Middle Name required | **The server still saves a record with Middle Name empty**; only the browser's script enforces the rule | Refuses before sending because the form's rules changed |
| State label the form doesn't offer | n/a | Refuses before sending |

Every refusal before the save is reported to the engine as "not sent", and
the step is API-only, so the run halts with nothing written. A real feature
could fall back to the GUI program at that point instead.

## Where this can't apply

- **No web form underneath.** Citrix, RDP and native desktop apps have no
  page requests to compile. Pixels stay the only layer there.
- **Rules that live in the page's script.** OpenEMR's duplicate check and
  required fields run in browser JavaScript, and the server enforces neither.
  A direct POST that skips them writes records the GUI would have blocked. The
  spike handles the two rules it found by name. An app with logic in the page
  script that the compiler doesn't model is unsafe at this tier, and finding
  that logic is the hard part.
- **Sign-in the session can't repeat by itself.** This copy accepts a plain
  user and password. SSO, MFA, device-bound sessions, CAPTCHA, or a sign-in
  that needs its own CSRF token or a JavaScript challenge would need the
  browser, or the vendor's real API, for at least the sign-in.
- **Multi-step server state.** This save needs no state from earlier pages. A
  workflow whose server keeps wizard state, uploads, or locks between pages
  needs the whole request sequence and its order, not one POST.
- **Request bodies built by script.** Signed, encrypted or computed fields,
  GraphQL or JSON bodies assembled at submit time, or tokens rotated on every
  request need more than value matching.
- **Success that isn't in the response.** Here a `set_pid` redirect is the
  success signal. Some apps return 200 for errors with nothing to match; then
  only the record check can decide.

## What a real feature would need

1. **Recording that captures requests by default.** The recorder would store
   request/response pairs next to the GUI events, so a later compile doesn't
   need a second capture replay. Credentials and tokens must be redacted at
   capture time, not later.
2. **Request bindings in the IR.** `ApiBinding` today sends JSON. The spike's
   form encoding, session sign-in, CSRF source, read-only pre-checks and
   success signature live in a separate `session_form.json`. They should be
   first-class, hashed fields so qualification can pin them.
3. **Pre-checks as steps.** The duplicate check should be a read step with
   its own pass condition, carried over from the GUI postcondition, rather
   than hidden in the actuator.
4. **Page-logic discovery.** Find the client-side checks the server doesn't
   repeat (validation rules, duplicate dialogs, confirmation prompts) and
   either compile them or refuse the tier. Today this is done by hand for two
   rules.
5. **Drift detection and fallback.** Compare the live form (fields, defaults,
   options, rules) with the recording on every run, and fall back to the GUI
   program before sending anything when they differ. The probes show this
   works for field and rule changes; a real feature needs it qualified per
   app.
6. **Qualification per app and endpoint.** Run the same effect contract, fault
   injection and false-stop measurements as for the GUI tier before the API
   tier may run unattended. Write `ApiIdentityBinding` contracts for
   form-field names; today's identity pointers expect the field name to match
   the effect field (`lname` against `form_lname`).
7. **Secrets from the deployment.** Sign-in values come from the deployment's
   secret store at run time. The spike reads them from the test copy's
   runtime file.

## Reproduce

```bash
export PYTHONPATH=packages/flow DOCKER_HOST=<podman socket>
python -m benchmark.openemr_api_tier.spike_fixture create   # once: private fixture on 9811/9810
python -m benchmark.openemr_api_tier.capture_har --ref 1 --out <dir>
python -m benchmark.openemr_api_tier.compile_api --har <dir>/referral.har --gui-bundle <replay-dom bundle> --out <bundle>
python -m benchmark.openemr_api_tier.bench matrix --blocks 5 --per-block 4 --out <dir>
python -m benchmark.openemr_api_tier.probes --out <file>
python -m benchmark.openemr_api_tier.compute_figures
```

The GUI bundle and the raw HAR are not in the repository. The bundle stays in
the referral study's scratch area. The HAR holds the session cookie and the
sign-in password of the disposable test copy. The OpenEMR compose file is
fetched at run time by `../openemr_local/upstream.py` and is not stored here.
