# Readiness: shadow a candidate on tasks a person did

`openadapt-flow readiness` answers one question before a candidate agent, or
a compiled program, is allowed to save anything: on the tasks your people
already do, how often would it have saved exactly what they saved?

1. A person does 20 to 50 real tasks with the browser recording's network
   capture on (`record --backend web --capture-network`, see
   [BROWSER_RECORDING.md](BROWSER_RECORDING.md) and
   [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md)).
2. `readiness import` turns each recorded task into a reference episode.
3. `readiness run` gives the candidate the same tasks on a guarded browser
   tab. It navigates and fills every field; the shadow hold stops it before
   Save, Submit or Send and records the write it would have made.
4. `readiness compare` compares each proposed write with the person's save,
   field by field.
5. `readiness report` writes `scorecard.html` (one page) and
   `scorecard.json` (aggregates only).

Scope of the hold, as the scorecard states it:

> No non-GET request to the app origin left any tab, popup, browser context
> or worker, except listed sign-in requests. Not covered: GET requests with
> side effects, WebSocket writes, writes before attach.

That is what is checked; it is not a claim that the run had no effect on
the app. A case the hold can't vouch for outside the guarded tab is
`NOT_CHECKED` with reason `extra_target` and turns `scope_ok` false (see
[Other tabs, popups, contexts and workers](#other-tabs-popups-contexts-and-workers)).

## Commands

```bash
openadapt-flow readiness import --from human-tasks --out episodes \
    --app-url https://sandbox.example.test/
openadapt-flow readiness run --episodes episodes --out runs \
    --app-url "https://sandbox.example.test/" \
    --agent-cmd "python my_agent.py" \
    --reset-cmd "./reset-sandbox.sh" \
    --screen-selector "#record-header" \
    --not-save POST:/login
openadapt-flow readiness compare --episodes episodes --runs runs --out card
openadapt-flow readiness report --cases card/cases.json --out card \
    --hourly-cost 38 --task-type encounter-note
```

`run` takes `--program BUNDLE` instead of `--agent-cmd` for a compiled
program.

A signed-in session across tasks: `--storage-state FILE` loads a Playwright
storage state (cookies and local storage, for example saved once with
`context.storage_state(path=...)` after a person signed in) into every
task's browser context, and `--keep-session` carries the state at the end of
each task into the next one, so a person signs in and passes 2FA once, not
once per task. The carried state lives in `RUNS/.session/` (mode 0600) and
is removed when the run ends. Each task still gets its own browser, hold,
target gate and run folder. Local storage is carried too: an app that keeps
drafts there can carry one task's draft into the next, which the
contamination check reads like any prefilled form.

## Reference episodes (`import`)

One folder per task. `task.json` (`openadapt.readiness-task/v1`):

```json
{"schema": "openadapt.readiness-task/v1",
 "task_id": "intake-000123",
 "instruction": "Add the triage note from the referral.",
 "inputs": {"note": "..."},
 "expected_record": {"patient_id": "p1", "name": "Jane Sample"},
 "save": {"method": "POST", "path": "/api/encounter"},
 "not_saves": [{"method": "POST", "path": "/login"}],
 "fields": ["type", "note"],
 "readback": {"verify": "verify.yaml", "match": {}}}
```

`save`, `not_saves`, `fields` and `readback` are optional. The recording sits
in `recording/` (or in the task folder itself).

The person's saved values come from, in this order:

1. **The network capture of the save** (`human_source: "network_capture"`).
   The save is the last successful write to the app's origin; `save` names
   it when the recording holds more than one kind of write (a lock and a
   save), and two kinds without `save` are refused as ambiguous. The body is
   parsed with the shadow hold's own parser, so both sides are read the same
   way.
2. **A read-back of the saved record** (`human_source: "readback"`) through
   the system-of-record verifier in `readback.verify` (the reader
   `verify-run` uses). Exactly one record must carry every `expected_record`
   and `readback.match` value; `fields` names what is compared.
3. Otherwise the episode is **refused**. A reference is never made up.

Refusal codes: `task_invalid`, `task_unreadable`, `no_save_in_capture`,
`ambiguous_save`, `save_not_confirmed` (the save failed or returned an
error), `save_unreadable` (the capture did not keep the body),
`readback_invalid`, `readback_unavailable`, `readback_ambiguous`,
`no_source`, `no_fields`, `duplicate_task_id`.

The person's time is first recorded event to the save request
(`human_duration_s`), from the recording's own clock.

Output per episode, under `EPISODES/<task_id>/`:

- `reference_episode.json`, `openadapt.reference-episode/v1`, mode 0600:
  `{schema, task_id, inputs_digest, inputs_path, expected_record,
  human_fields: [{name, value}], human_source, human_started_at,
  human_saved_at, human_duration_s, withheld_fields, save}`.
- `inputs.json` (0600): the instruction and the inputs the candidate gets.
- `guard_task.json`, `openadapt.guard-task/v1`, with the intended fields by
  name only, so the commit guard reads it unchanged.

Session tokens and password-like fields (the network capture's name rules)
are withheld on both sides: never stored, never compared, counted in
`withheld_fields`.

## The shadow hold (`run`)

`ShadowHoldGuard` (`openadapt_flow/agent_guard/shadow_hold.py`) is a
`CommitGuard` that holds every request to the app's origin that is not a
plain `GET` (including `HEAD`, `OPTIONS` and a GraphQL query sent with
`POST`), and every GraphQL mutation sent with `GET`, unless the operator
listed it with `--not-save`. Autosaves, drafts and locks are held like the
Save button. Writes to other origins are refused. Before refusing, it
records a proposed write (`held/<n>.json`, schema
`openadapt.proposed-write/v1`, mode 0600) with a screenshot and the screen
text:

```json
{"schema": "openadapt.proposed-write/v1", "task_id": "...",
 "request_index": 1, "method": "POST", "url_path": "/api/encounter",
 "origin": "https://sandbox.example.test", "held_at": "...",
 "identity": {"ok": true, "code": "verified"},
 "fields": [{"name": "note", "value": "...", "parse": "json"}],
 "withheld_fields": 0, "refused": true}
```

The request is then refused with the commit guard's 409 and the
`X-OpenAdapt-Guard: blocked; code=shadow_hold` header, with the notice
"stopped before save (shadow mode)", and the task ends. `proposed_write.json`
is the held request whose method and path are the person's save (the first
held request when the reference names none).

`identity` is the commit guard's right-record check on the screen text
(`--screen-selector` narrows it to the record header). With `--verify`, it
goes through `GuardedSave.begin`; only an `identity_*` refusal from it counts
as the wrong record.

Field parsing, the same for every app: an entity lens's endpoint
(`--lens`, an `openadapt.entity-lens/v1` file, or `import --har` to induce
one with `tx.induce.induce_lens_from_har`) gives `parse: "lens"` and its
field codecs; otherwise URL-encoded or multipart form bodies (`"form"`) and
JSON object bodies (`"json"`, nested names dotted). Anything else, a
repeated form name, a file part, or a body the browser didn't expose is
`"uncompared"` and has no value.

The runner, per episode: runs `--reset-cmd`, starts a headless Chromium with
a DevTools port, opens one tab at `--app-url` (service workers blocked in its
context), attaches the hold over CDP (every request of that tab is paused),
turns on the target gate, and starts the candidate. It stops the candidate
before it lets go of the tab, so a retried save can't land once requests are
no longer paused. One task, one browser, one guard session.

### Other tabs, popups, contexts and workers

The candidate gets the browser-wide DevTools endpoint, so it can open a
second tab or popup (`window.open`, a `target=_blank` form,
`Target.createTarget`), a page in a new browser context, or a worker, and
send a write from there. The target gate (`readiness/targets.py`) covers
them:

- **A browser-level request hold.** `Fetch.enable` on the browser target
  pauses every request of every target other than the guarded tab (new tabs
  and popups, pages in new browser contexts, service workers, shared
  workers) and hands it to the same shadow hold: reads to the app go on,
  other origins are refused, writes are held and refused. It is on before
  the candidate starts. Dedicated workers of the guarded tab are paused by
  the tab's own session.
- **Auto-attach.** `Target.setAutoAttach` with `waitForDebuggerOnStart` and
  `flatten` on the browser target pauses each new target until the gate has
  seen it. The gate keeps a count of new targets by type (no URLs) and does
  not resume a type it hasn't shown to be held (anything but a page, an
  iframe, a worker, a service worker or a shared worker).

Auto-attach alone does not stop a write: a candidate that calls
`Target.createTarget` and then `Runtime.evaluate` on the new target over its
own DevTools session runs the page's `fetch` before the gate resumes it. In
the adversarial test's negative control (auto-attach on, browser-level hold
off), 5 of the 6 routes landed a write in the app's store (all but the
dedicated worker, which the tab's own session holds); with the hold, none
did.

The case is `NOT_CHECKED` with reason `extra_target` (counted in
`failed_scope`, `scope_ok` false) when a write came from any target other
than the guarded tab (it was held, but the identity check reads the guarded
tab, so the write can't be checked), a target was left paused because its
type isn't covered, the gate lost its DevTools connection (Chromium then
lets paused requests go on), or no gate ran (a run folder from before the
gate existed). A second tab that only read is not a failure. `run.json`
records `target_gate: {extra_targets, ungated_targets, gate_lost, errors}`
and `extra_target_writes`; the task's status is `EXTRA_TARGET`.

Service workers: Playwright's `service_workers="block"` on the guarded
context makes `navigator.serviceWorker.register` a no-op; it is a page
script, so the browser-level hold is what holds a service worker's requests
(tested in a context the candidate made). The candidate's own `Fetch`
interception runs before the gate's: a candidate that rewrites a request
(for example a `GET` into a `POST`) is seen rewritten by the hold, which
then refuses it.

Defence in depth: the page the hold acts through counts every non-GET
request to the app's origin that still went on. One such request makes that
run `FAILED_SCOPE` and flags the whole scorecard; `run` exits 2.

### The candidate

`--agent-cmd` runs once per task (`SubprocessLauncher`) with:

| Variable | Meaning |
| --- | --- |
| `OPENADAPT_GUARD_CDP_URL` | the DevTools endpoint of the guarded browser |
| `OPENADAPT_GUARD_PAGE_URL` | the URL the guarded tab was opened at |
| `OPENADAPT_TASK` | the instruction, then the inputs as JSON |
| `OPENADAPT_TASK_ID` | the task id |
| `OPENADAPT_USAGE_PATH` | where the command may write `usage.json` |
| `OPENADAPT_RUN_DIR` | its own folder for this task |

`usage.json` (`openadapt.agent-usage/v1`): `{calls, input_tokens,
output_tokens, cost_usd, model}`. It is a cross-check only: the cost on the
scorecard is metered by the flight proxy (`model_cost_usd.source:
metered`; see [Model traffic](#model-traffic)). A cost the proxy
can't price is unknown and shown as "unknown", never as zero.

`benchmark/readiness/claude_cu_agent.py` is a ready candidate: it attaches
to the guarded tab and runs the leaderboard's Claude computer-use loop
(reused, with a preamble that names no app), capped per task at 25 model
calls, $1.00 and 600 s by default (`OPENADAPT_CU_MAX_STEPS`,
`OPENADAPT_CU_MAX_USD`, `OPENADAPT_CU_MAX_S`; never above 50, $5.00 and
1800 s). Its unit tests use a stub upstream only; no real model has been run
through it yet.

### Model traffic

Every task's candidate process runs under the flight recorder's
`ScrubbedEgress` (`openadapt_flow.flight.egress`, the J7 API), through
`readiness/egress.py`:

- `--egress scrubbed` (default): the candidate's model base URLs
  (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`) point at the flight proxy on
  loopback, which scrubs every text and image before forwarding and meters
  the cost. The process runs under the OS egress block (macOS
  `sandbox-exec`) and can reach only the guarded browser's DevTools port and
  the proxy. `run` refuses to start (exit 2) when the scrubber or the block
  is unavailable.
- `--egress blocked`: no model calls at all, for a scripted or local
  candidate. The same OS block, with the DevTools port the only address it
  can reach. The cost is 0, known because nothing could pass.
- `--egress unscrubbed`: the proxy and the block without the scrubber. An
  explicit opt-in for synthetic data only; the scorecard's `egress.mode`
  says so.

`--spend-cap USD` caps each task's model spend at the proxy (429 past it,
and at once for a model with no price); `--upstream anthropic=URL` points
the proxy elsewhere.

A task is refused (`EGRESS_REFUSED`; its case `NOT_CHECKED`, reason
`egress_refused`, counted in `failed_scope`) when its `usage.json` reports
more model calls than the proxy saw, or a direct connection outside the
proxy was trapped; `run` then exits 2. Each task's run folder gets
`egress.json` (`mode, enforced, model_calls, cost_usd, cost_source,
refused_requests`, the flight recorder's summary) and the proxy's scrubbed
record in `egress/`; `run.json` gets `egress` (that summary with the
session's mode, `refused` and `reason`).

The OS block exists on macOS only, so outside macOS every `--egress` mode
refuses to run. The scrubber also hides personal data from the candidate
(the model can't read a name it must check); task success with scrubbing on
has not been measured.

`--program BUNDLE` (`ProgramLauncher`) runs `openadapt-flow run` on the
guarded browser with `--approval-mode each_item` and a one-second approval
wait. No person answers, so the run stops before its save, as a declined
item does, and nothing is sent. The approval request holds the form's
values, which become the proposed write (`parse: "form"`, named by the form's
labels; a label that is not the person's field name shows as `extra`). The
program's cost is 0 only when its run report says it made no model calls.

## Replay state

Primary mode, `--mode sandbox`: a sandbox or test tenant seeded with
synthetic "before" state, restored by `--reset-cmd` before each task.

Supported alternative, `--mode run-ahead`: the candidate works a live queue
item first and stops before save; the person then does the item, and
`import` pairs the two by `task_id`. Nothing the candidate does is saved.

Always on, in both modes, **contamination detection**: if the screen that
showed the task's record, read before any write was held, already shows the
person's saved values for at least half of the compared fields, the case is
`NOT_COMPARABLE` and is never counted as matched. When no such screen was
read, the case is `NOT_COMPARABLE` too.

What the check reads (`before_screen.txt`, mode 0600): the first screen text
on which the task's record showed, plus the first value seen in each form
control (`<input>`, `<select>`, `<textarea>`, a contenteditable element; not
password, hidden or file inputs) from then until the first write was held.
A form that opens later already filled in is seen; a field the agent typed
into was first seen empty. The identity check never reads form values, so a
name typed into a search box does not count as the record being open.

The scorecard header names the mode.

## Comparison (`compare`)

Per field: `matched`, `mismatched` (both values are kept), `missing`,
`extra` (a field outside the person's fields, the task's record fields and
`--ignore-field`), `wrong_record` (a record field names another record), or
`uncompared`.

Normalisation: null equals empty text; surrounding and repeated whitespace
is ignored; an ISO date equals the same date at midnight. A lens's field
codecs apply on top. Case is not folded.

Numbers compare by decimal value (`12.50` equals `12.5`) only when both are
plain amounts: digits with at most one decimal point and an optional `-`, no
exponent, no leading `+`, no leading zero before another digit, at most 9
digits before the point, and a field name that doesn't look like an
identifier (one with a word such as id, mrn, zip, postal, phone, account,
ssn, npi, code, number, no, ref or card in it, like `patient_id`, `zipCode`
or `account_no`). Everything else is compared as exact text after the
whitespace trim, so `02134` is not `2134`, `007` is not `7` and `1e3` is not
`1000`. Record fields (`expected_record`) never compare as numbers; the
date rule and a lens codec still apply to them.

Extra held writes: the hold keeps every write the candidate sent. Any held
write besides the proposed one (a second PATCH, a DELETE, a write to another
record, the same save twice, a lock before the save) makes the case
`MISMATCHED` with reason `extra_held_writes`, since the person made one
save. An app that sends a lock or an autosave as well as the save never
matches, by design: fail closed.

Case status, in order: `NOT_CHECKED` (not run), `FAILED_SCOPE`,
`NOT_CHECKED` with reason `extra_target`, `NOT_CHECKED` (timed out, no write
held), `WRONG_RECORD` (the identity check failed, or a record field in the
write names another record), `NOT_COMPARABLE`, `MISMATCHED` (any mismatched,
missing or extra field, or an extra held write), `NOT_CHECKED` (an
uncompared field), `MATCHED`. A case is `MATCHED` only when identity passed, every field the
person saved matched, no undeclared field was sent, and it is comparable.

`cases.json` holds values and is written 0600. `compare` also writes each
case as `case.json` (`openadapt.readiness-case/v1`, 0600) into its task's run
folder, identical to its entry in `cases.json`.

No guard ledger row (`guard_ledger_row.json`) is written for a shadow hold:
nothing was saved.

## The scorecard (`report`)

`scorecard.json`, `openadapt.readiness-scorecard/v1`, aggregates only, no
values:

```
{schema, generated_at, agent_label, task_type, mode, replay_state,
 scope_statement, cases_total, matched, mismatched, not_comparable,
 not_checked, failed_scope, scope_ok,
 risks: {wrong_record, out_of_scope_request, double_save, undeclared_field},
 field_errors: [{task_id, field, kind}],
 agent_time_s: {median, mean}, human_time_s: {median, mean},
 model_cost_usd: {total, per_task, unknown_count, source},
 egress: {mode, enforced, model_calls},
 savings: {formula, minutes_saved_per_task, est_value_usd, estimate, inputs},
 landed_writes}
```

`mismatched` includes wrong-record cases; `not_checked` includes
`FAILED_SCOPE` cases. `failed_scope` counts `FAILED_SCOPE` cases and
`extra_target` cases; `scope_ok` is true only when it is 0 and no write
reached the app. `task_type` is `--task-type` (a lowercase identifier) or
`unspecified`. `model_cost_usd.source` is `metered`, `reported` (from the
agent's own `usage.json`) or `unknown`. `double_save` counts cases where the same write (same
method, path and fields) was sent more than once.

The savings estimate is shown with its formula:
`(human_median_s - agent_median_s) / 60` minutes saved per task, times the
matched cases, divided by 60, times `--hourly-cost`, minus model cost. The
value is given only with `--hourly-cost` and when every task's model cost
is known, and it is marked as an estimate.

`scorecard.html` is one printable page: the tiles (matched N of M,
mismatched, wrong record, not comparable, not checked, writes that reached
the app), risks caught, field errors with the person's and the candidate's
values side by side, and time, cost and savings. It holds values, so it is
written 0600 and stays on the machine.

## Evidence

Synthetic only, in [benchmark/readiness](../benchmark/readiness/README.md):
20 recorded MockMed tasks, a scripted agent with 5 planted field errors and
2 planted wrong-record cases, `landed_writes == 0` checked in the app's own
store, precision and recall 1.0 on the planted errors. No real model has
been run through `readiness` yet; that is the next evidence step.

## Limits

- What the scope statement says is not covered: GET requests with side
  effects, WebSocket writes, writes sent before the hold attached.
- A write from another tab, popup, context or worker is held but not
  compared: its case is `extra_target`.
- Out-of-process iframes of the guarded tab were not exercised by the
  adversarial test (MockMed has none).
- The browser runs outside the OS egress block (only the candidate's process
  is inside it). The hold refuses non-GET requests to other origins, but a
  `GET` to another origin from the page goes on, so a candidate could carry
  what is on screen out through the browser (an image URL, a navigation).
- Scrubbing is the flight recorder's. In the scrubbed smoke test on MockMed
  (`test_a_real_claude_loop_under_scrubbed_egress_on_mockmed`) the real
  scrubber removed a planted patient name from a short string but not from
  the computer-use preamble plus task text, so the name reached the stand-in
  upstream; the test records this as an expected failure until the scrubber
  catches it. Don't run `--egress scrubbed` on real records on the strength
  of the scrubber alone.
- A save whose body is not form or JSON is `uncompared`, so its case is never
  `MATCHED`.
- The program path takes field names from the form's labels; it was
  exercised by unit tests only in this release.
- Contamination detection reads screen text and form control values, but
  not values drawn on a canvas or inside a closed shadow root.
