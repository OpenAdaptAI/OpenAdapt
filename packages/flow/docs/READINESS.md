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

> No non-GET request to the app origin left the browser except listed
> sign-in requests. Not covered: GET requests with side effects, WebSocket
> writes, other tabs, writes before attach.

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
    --hourly-cost 38
```

`run` takes `--program BUNDLE` instead of `--agent-cmd` for a compiled
program.

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
a DevTools port, opens one tab at `--app-url`, attaches the hold over CDP
(every request of that tab is paused), and starts the candidate. It stops
the candidate before it lets go of the tab, so a retried save can't land
once requests are no longer paused. One tab, one task, one guard session.

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
output_tokens, cost_usd, model}`. `cost_usd` is used as given. Without it,
tokens are priced with `compile_agent.agents.PRICES_PER_MTOK` when `model`
is listed there. Otherwise the cost is unknown and shown as "unknown", never
as zero.

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

The scorecard header names the mode.

## Comparison (`compare`)

Per field: `matched`, `mismatched` (both values are kept), `missing`,
`extra` (a field outside the person's fields, the task's record fields and
`--ignore-field`), `wrong_record` (a record field names another record), or
`uncompared`.

Normalisation: null equals empty text; surrounding and repeated whitespace
is ignored; numbers compare by decimal value; an ISO date equals the same
date at midnight. A lens's field codecs apply on top. Case is not folded.

Case status, in order: `NOT_CHECKED` (not run, timed out, no write held),
`FAILED_SCOPE`, `WRONG_RECORD` (the identity check failed, or a record field
in the write names another record), `NOT_COMPARABLE`, `MISMATCHED` (any
mismatched, missing or extra field), `NOT_CHECKED` (an uncompared field),
`MATCHED`. A case is `MATCHED` only when identity passed, every field the
person saved matched, no undeclared field was sent, and it is comparable.

`cases.json` holds values and is written 0600.

## The scorecard (`report`)

`scorecard.json`, `openadapt.readiness-scorecard/v1`, aggregates only, no
values:

```
{schema, generated_at, agent_label, mode, replay_state, scope_statement,
 cases_total, matched, mismatched, not_comparable, not_checked,
 failed_scope, scope_ok,
 risks: {wrong_record, out_of_scope_request, double_save, undeclared_field},
 field_errors: [{task_id, field, kind}],
 agent_time_s: {median, mean}, human_time_s: {median, mean},
 model_cost_usd: {total, per_task, unknown_count},
 savings: {formula, minutes_saved_per_task, est_value_usd, estimate, inputs},
 landed_writes}
```

`mismatched` includes wrong-record cases; `not_checked` includes
`FAILED_SCOPE` cases. `double_save` counts cases where the same write (same
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
  effects, WebSocket writes, other tabs, writes sent before the hold
  attached.
- A save whose body is not form or JSON is `uncompared`, so its case is never
  `MATCHED`.
- The program path takes field names from the form's labels; it was
  exercised by unit tests only in this release.
- Contamination detection reads screen text, not form controls: values that
  only appear inside inputs are not seen.
