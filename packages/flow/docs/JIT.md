# Compile a computer-use agent's run

`openadapt_flow.jit` wraps a computer-use agent, such as Claude or OpenAI
computer use. The first run of a task lets the agent do it while Flow records
every action. Flow compiles the recording into a DRAFT program. A named
person reviews it, watches it run once as a canary, and admits it. Later runs
execute the admitted program: no model calls, and each save is read back from
the system of record before the run reports `VERIFIED`.

```python
from openadapt_flow.jit import (
    TaskSignature, WebApp, compile_agent, load_customer_model_key,
)
from openadapt_flow.runtime.effects import RestRecordVerifier

jit = compile_agent(
    my_agent,  # your CUA loop; see "The agent" below
    task=TaskSignature(
        "clinic.add_note",
        "Open the first referral, start a Triage encounter, and save this note: {note}",
        {"note": str},
    ),
    app=WebApp(url="https://emr.example/tasks", record_reader=read_records),
    verifier=RestRecordVerifier("https://emr.example", records_path="/api/records"),
    policy="clinical-write",
    cache_dir="jit-cache",
    model_key=load_customer_model_key("anthropic"),  # your key, not OpenAdapt's
)

first = jit.run(note="Follow up in two weeks")   # status: needs_human_admit (DRAFT)
steps = [s["id"] for s in jit.status()["steps"]]
jit.review(principal_id="dana@clinic.example", reviewed_steps=steps,
           accept_params=["note"], confirm="ok")
jit.canary(principal_id="dana@clinic.example", note="Canary test note")
jit.admit(principal_id="dana@clinic.example", confirm="ok")
later = jit.run(note="Recheck knee in ten days")  # status: VERIFIED, model_calls: 0
```

[JIT_ADMISSION.md](JIT_ADMISSION.md) describes the DRAFT to ADMITTED steps,
the customer model key, and the warning shown before recording.

To try it with no model and no setup, run the example. It uses a recorded
Claude computer-use trajectory against the bundled MockMed app:

```bash
python examples/jit/run_mockmed.py
```

## What each run does

| State of the task | `run(**params)` does | `status` |
| --- | --- | --- |
| Nothing cached | Shows the recording warning, runs the agent once, records it, compiles a DRAFT | `needs_human_admit` |
| A DRAFT is waiting | Nothing. The agent is not run again, and the draft does not run | `needs_human_admit` |
| A program is admitted | Checks the signed admission, runs the program under the standard profile, and reads the save back | `VERIFIED`, or `HALTED` and others |
| The admitted bundle or its manifest changed | Nothing | `refused` |
| The replay called a model | Sends the run to the inbox | `needs_review` |

A run that does not end `VERIFIED` is written to the exception inbox
(`<cache_dir>/inbox/`), passed to `on_halt` if you gave one, and returned. The
agent is never called after a halt; a person decides what happens next. The
inbox entry carries the `openadapt-flow console ... --attend` command that
opens the run in the Needs Attention queue (see
[EXCEPTION_INBOX.md](EXCEPTION_INBOX.md)).

The cache key is the task id plus the parameter names and types. The
instruction text is not part of it, and nothing is matched fuzzily. Changing
the schema makes a new task, which starts with a new demonstration.

## The agent

The agent is any object with a `dialect` and a `run(request, computer)`
method. `run` is your existing CUA loop with one change: send each action to
`computer.execute(action)` instead of your own executor, and give the model the
observation it returns (`observation.png`, or `observation.png_base64()`).

```python
class ClaudeAgent:
    dialect = "anthropic"

    def run(self, request, computer):
        messages = [{"role": "user", "content": request.instruction}]
        while True:
            response = client.beta.messages.create(..., messages=messages)
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                return
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for call in calls:
                obs = computer.execute(call.input)
                results.append(screenshot_result(call.id, obs.png_base64()))
            messages.append({"role": "user", "content": results})
```

Create `client` with `request.model_key.secret()`, the customer key you
passed to `compile_agent`. An agent that calls a model is not recorded
without one. An agent that calls no model sets `requires_model_key = False`.

This sketch is not shipped or tested. The tests and the example use
`openadapt_flow.jit.recorded.RecordedTrajectoryAgent`, which replays a saved
trajectory and refuses a request with other inputs.

### Your own loop

To keep the loop in your own code, leave out the agent and use
`recording()` as a context manager:

```python
jit = compile_agent(task=..., app=..., verifier=..., policy="clinical-write",
                    dialect="anthropic", model_key=load_customer_model_key("anthropic"))
with jit.recording(note="Follow up in two weeks") as rec:
    my_cua_loop(rec.request.instruction, rec.computer.execute)
print(rec.result.status)  # needs_human_admit, unsupported, or capture_failed
```

An `UnsupportedAction` raised in the block ends the capture with no draft.

## Action formats

`computer.execute` performs each action through
`openadapt_flow.authoring.AuthoringSession`, so the agent's run is recorded the
same way a person's demonstration is: before and after frames, the clicked
element's locator, and the system-of-record snapshot after each step. When an
action has no faithful step in the recording format, `execute` raises
`UnsupportedAction`, performs none of it, and the run produces no candidate.

| Dialect | Actions mapped | Actions refused |
| --- | --- | --- |
| `generic`: Flow's own steps, `{"kind": "click", "x": 10, "y": 20}` | `click`, `double_click`, `right_click` (at a point, a CSS `target`, or the pointer), `drag`, `type`, `press`, `hotkey`, `scroll`, `wait`, `observe` | Any other kind, and any field the kind does not take |
| `anthropic`: the `input` of a `computer` tool call | `left_click`, `right_click`, `double_click`, `left_click_drag`, `type`, `key` (xdotool names and chords), `scroll`, `wait`, `screenshot`, `zoom`, `cursor_position` | `triple_click`, `middle_click`, `left_mouse_down`, `left_mouse_up`, `hold_key`, a click with held keys |
| `openai`: a Responses `computer_call` (`actions` or `action`) | `click` (left, right), `double_click`, `drag` with two points, `keypress` (one key plus modifiers), `type`, `scroll`, `wait`, `screenshot` | `wheel`, `back` and `forward` buttons, held `keys`, a drag path with more than two points, a key sequence |
| `playwright_mcp`: `{"name": ..., "arguments": ...}` | `browser_click` and `browser_type` with a unique CSS selector, `browser_press_key`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, `browser_mouse_wheel`, read-only tools | Snapshot refs such as `e12`, `browser_select_option`, `browser_fill_form`, `browser_evaluate`, tabs, uploads, dialogs, navigating away, and every other tool |

Use `generic` when your loop already maps its model's actions itself. To add
a format, pass a translator to `register_dialect(name, translator)`. It maps
one native action to a list of `Op` and raises `UnsupportedAction` for
anything it can't map exactly. The built-in names can't be replaced.

Some mappings have conditions:

- A pointer move is not performed. The recording has no pointer-move step, so
  a hover the program would not repeat could change the app. The agent gets a
  fresh screenshot and a note.
- A scroll must happen at the pointer, where the last click or drag left it.
  The recording scrolls at the pointer, so a scroll anywhere else is refused.
- `browser_type` replaces a field's text, and recorded typing appends to it.
  It is mapped only when the field is empty.
- Snapshot refs belong to the Playwright MCP server's own page. Flow can't
  resolve them, so a Playwright MCP agent must target elements with CSS
  selectors or coordinates. `observation.text` carries the page's ARIA
  snapshot without refs.

## Pruning

Compile distills the recording first
([`compiler/distill.py`](../openadapt_flow/compiler/distill.py)). Besides
steps with no visible effect, it drops dead ends: a run of two or more steps
that ends on the screen it started from, such as clicking into a field and
clicking away, or opening a menu and pressing Escape. A dead end is dropped
only when the URL, title, page count and system-of-record snapshot match at
both ends; no step in it types, carries a parameter, submits, presses Enter or
Tab, or clicks a control whose label names a write; and its last step is not
the click that focuses the next step's typing. The rule is deterministic. The
candidate lists every dropped step and the rule that dropped it.

## Parameters

The agent was told the run's inputs, so a typed step whose text is exactly one
input's value is where that input goes. The candidate proposes those steps as
parameters in the bundle's `param_proposals.json`, next to the compiler's own
field-label proposals. An input typed in two steps, typed inside longer text,
or not typed at all is listed as not lifted.

Nothing is applied until admission. `admit(accept_params=[...])` runs the
existing confirm pass
([`compiler/param_confirm.py`](../openadapt_flow/compiler/param_confirm.py)),
which recompiles the recording with the confirmed parameters. The recompile
binds the mined read-back to the parameter, so a run with a new value checks
for the new value. `accept_params` must name exactly the task's parameters.

## Admission

A draft is admitted in three calls, each by a named person, and only in this
order: `review`, `canary`, `admit`. `run` never calls any of them, and a
draft never runs unattended. [JIT_ADMISSION.md](JIT_ADMISSION.md) lists what
each step checks. The admission manifest (`admission.json` in the bundle) is
signed with a local Ed25519 key and pins the bundle's content digest. A run
checks the signature and the digest, and the run gate pins the digest, so a
bundle or manifest edited after admission is refused.

`review`, `admit`, `reject` and `status` are also on the command line:

```bash
openadapt-flow jit status jit-cache/<task key>
openadapt-flow jit review jit-cache/<task key> --principal dana@clinic.example \
    --accept-param note --all-steps-shown
openadapt-flow jit admit jit-cache/<task key> --principal dana@clinic.example
```

The canary needs the app and the verifier, so it runs from Python only.

## Files

```
<cache_dir>/
  <task_id>-<schema digest>/
    program/
      recording/            the agent's run as recorded
      recording-distilled/  what was compiled
      bundle/               the program; admission.json after admission
      candidate.json        what the reviewer sees; state DRAFT
      review.json           who reviewed which steps, for which digest
      canary.json           the attended canary and its read-back coverage
      lifecycle.jsonl       one line per review, canary, admission, rejection
      capture.json          each native action and the steps it became
    runs/                   one directory per run
    refused-*/, rejected-*/ captures that produced no program
  inbox/                    one JSON file per run that did not end VERIFIED
```

`reject(principal_id=...)` sets a waiting draft aside, and the next run
explores again.

## Limits

- Web apps only. `AuthoringSession` can drive desktop, RDP and Citrix, but the
  wrapper launches a Playwright browser for capture and for runs.
- Without a `record_reader`, the candidate has no effect contract, admission
  refuses it, and no run can reach `VERIFIED`.
- Secret inputs are not lifted. A secret is typed by a person at a pause
  (see `AuthoringSession.pause_for_input`), not by the agent.
- The review recompiles the recording to apply the confirmed parameters, so
  the step ids can change. If `review` refuses with "every step", read the
  steps again with `status` and review those.
- OpenAI `pending_safety_checks` are your loop's to acknowledge;
  `computer.execute` does not read them.
- One admitted program per task signature. A task that needs branches still
  goes through the repair and teach paths
  ([REPAIR_LIFECYCLE.md](REPAIR_LIFECYCLE.md)), never back to the agent.
