# Flight recorder

`openadapt flight` records a run of a computer-use agent you already have,
without rewriting its action layer. It works with a Claude computer-use loop,
an OpenAI computer-use (CUA) loop, Browser-Use, or any other process that
calls a model API.

```bash
openadapt flight --task-id referral-0042 -- python my_agent.py
```

For that one run you get:

- **Model cost metering.** Each model call goes through a loopback proxy. The
  proxy counts input, output and cache tokens and prices them from the price
  table in `openadapt_flow.compile_agent.agents.PRICES_PER_MTOK`. A model with
  no price gets a cost of `null` with `cost_source: "unknown"`; the proxy
  never guesses a price.
- **Scrubbing before anything leaves the machine.** The proxy scrubs every
  string in the request body (message text, tool-call arguments, tool
  results, URLs) and redacts every base64 screenshot before it forwards the
  request. See [What the scrubber does](#what-the-scrubber-does).
- **An egress block.** On macOS your command runs under `sandbox-exec` with
  every outbound connection denied except to loopback, so a direct
  connection from your command or anything it starts fails. Elsewhere
  `flight` refuses to run unless you opt out. See [The egress block](#the-egress-block).
- **A redacted timeline.** `timeline.jsonl` lists each model call, each
  computer action the model asked for, each action your executor performed
  and each gate decision. Frames are written as scrubbed thumbnails.
  `timeline.html` is a static page you open locally.
- **A pre-commit gate.** If you swap one line in your loop, every click or
  Enter on a control that is not known to be safe is held for a person before
  it happens.

## How it works

1. `flight` starts a server on `127.0.0.1` on a random port, with a random
   token in the path.
2. It runs your command with `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` set to
   that server. The Anthropic and OpenAI SDKs read these variables. If they
   were already set (a company gateway, for example), that URL stays the
   provider the proxy forwards to.
3. For each request, the proxy:
   1. scrubs the request body;
   2. checks the spend cap;
   3. forwards the body over HTTPS with your agent's own headers, including
      its API key. The key is never logged or written.
   4. passes the response back, streamed or not, and reads the usage from it.
4. Your command also gets `OPENADAPT_FLIGHT_GATE_URL`. A `GatedComputer` or
   `GatedComputerCall` in your process uses it to log actions and to ask the
   gate. `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY` point at a trap on the
   same server (loopback is exempt through `NO_PROXY`), and
   `OPENADAPT_USAGE_PATH` says where your agent may write its own call count.
5. On macOS the command runs under the egress block.
6. When the command exits, `flight` writes `flight.json` and `egress.json`,
   prints the run directory, and exits with the command's exit status, or 3
   when the run is refused for egress outside the proxy.

The proxy sends nothing anywhere except to the provider your agent already
targets. Anything it sends is scrubbed first.

## The egress block

What is enforced, by system:

| System | What `flight` does |
| --- | --- |
| macOS | Runs your command under `/usr/bin/sandbox-exec` with a profile that denies every outbound network connection except to loopback, and denies DNS lookups. It checks the block first: a sandboxed connect to a TEST-NET address must fail with "operation not permitted". Everything your command starts inherits the block, so a browser it launches reaches loopback only. |
| Any other system | No OS block yet. `flight` refuses to start unless you give `--allow-unenforced-egress`. |

`--no-egress-block` runs without the block on macOS too, and without the
proxy-variable trap: the behaviour before the block. Both opt-outs are
recorded in `flight.json` under `details.egress.opt_out`.

`flight` marks the run refused, and exits 3, when model calls went around
the proxy:

- your agent wrote `usage.json` (`openadapt.agent-usage/v1`, at
  `OPENADAPT_USAGE_PATH`) with more calls than the proxy saw;
- your agent wrote no `usage.json` and the egress block was off; or
- a client that ignored the base URLs but honoured `HTTPS_PROXY` reached the
  trap. The trap refuses it and records the host. Anything else that honours
  the proxy variables is trapped too and refuses the run: an agent's
  telemetry, or on Linux a browser your command starts.

`details.egress` in `flight.json` holds `refused`, the reason and the
numbers. Without a `usage.json` and without the block, the run is refused
too: nothing could show a call that went around the proxy. Under the block,
`details.egress.checks.cross_check` is `"unavailable"`. A connection that
the kernel refuses without going through the trap is blocked but not
counted: macOS doesn't log these denials to an unprivileged process. Under
the block such a direct call still fails.

Other limits of the block:

- Anything listening on loopback can still be reached. A local forward proxy
  or SSH tunnel on this machine would carry traffic out.
- The block covers direct connections from the process tree `flight`
  starts. Work the agent hands to a process outside it is not covered: `open`
  (LaunchServices), Apple Events, XPC helpers, an already running browser or
  a daemon reached over a local socket.

The same API is available in Python as
`openadapt_flow.flight.egress.ScrubbedEgress`.

## What the scrubber does

Every string in the request body is scrubbed, wherever it is, with these
exceptions that must reach the provider unchanged: thinking and reasoning
blocks (their signatures must verify; they came from the provider), tool
definitions, request settings (`model`, `tool_choice`...; not `metadata`,
whose values are scrubbed) and
protocol fields (`type`, `role`, ids, the tool name of a tool call). Inside
tool-call arguments and a tool result's fields no key and no block type is
exempt. Arguments sent as a JSON object or array in a string are parsed,
scrubbed value by value and serialised again. A number of five or more
digits becomes `"<ID>"`.

Each string goes through these steps:

1. URLs are cut to their origin (`https://emr.example/<path removed>`).
2. Personal values are found: values already seen in this run, deterministic
   patterns (dates, phone numbers, e-mail addresses, street addresses,
   numbers of five or more digits, the value after a label such as `Name:`,
   `DOB:` or `MRN:`), and the openadapt-privacy text scrubber (Presidio with
   spaCy `en_core_web_sm`) on the whole string and again on each line,
   sentence and quoted string. Presidio can miss a name in a long prompt
   that it finds in the same sentence alone.
3. Each value is replaced with a run token: `[PERSON_1]`, `[ID_1]`,
   `[DATE_1]`, `[PHONE_1]`...

Presidio's ORGANIZATION and NRP hits are not among HIPAA's 18 identifiers
and are left alone; they mostly hit app names, headers and words like
"DONE". When Presidio, asked about the same words alone, names a person or
another identifier, they are redacted (it reads some names in a sentence as
organisations). A date with no digit ("today") is left alone too.

Each image is OCR'd with tesseract. Each OCR line, split into table cells at
wide gaps, goes through the same detection, and each value is painted over
with its token written in the box.

### Run tokens

The same value, ignoring case and spacing, gets the same token in text and
in screenshots, in every request of the run. The model can match
`[PERSON_1]` in its task to the row that shows `[PERSON_1]` on screen.

On the way back, `flight` replaces each token with its real value inside
tool-call inputs only: a computer `type` action's text, `function_call`
arguments, any other tool input. Your agent types the real value into the
app. Text the model writes for itself keeps the tokens. When your agent
sends that tool call back in its history, the real value becomes the same
token again before anything leaves the machine: every value already in the
map is matched, whether or not the detectors would find it again.

Streams are rewritten too. Anthropic tool-use input and OpenAI Responses
argument and input deltas are held until their block or item is complete,
then passed on as one delta. A stream event that can't be parsed ends the
stream with an error event. A streamed Chat Completions request is refused
once the run has a token, because its tool-call deltas can't be rewritten
safely; send `stream=false`.

The map is in memory and in `pseudonyms.json` in the run directory, mode
0600. It holds the real values in plaintext and is never forwarded. Don't
share the run directory as is.

### Measured

Recall on 50 synthetic records (five fields each;
[benchmark](../benchmark/flight/scrub_recall/README.md)): 98.0% of fields
in text sent as a tool result, 97.6% as plain message text, 97.6% as
message text after a long agent preamble, and 97.2% of readable fields in
rendered form screenshots. Every miss was a name. Presidio alone, as the
proxy used it first, caught 77 to 78% in text and 67.5% in images.

Over-redaction, on 20 synthetic work-list screenshots and task
instructions: 93.1% of the non-personal words tesseract reads on the
original screen (app name, navigation, headers, referral reasons) are still
readable after scrubbing, against 30.9% with Presidio's image redaction.
The app name ("MockMed") is lost on every screen: Presidio reads it as a
person. In the instructions 94.4% of non-personal words are kept, against
89.1%. On those screens 38 of 40 names and record numbers were hidden; in
the instructions, 37 of 40.

The corpus is synthetic and clean, and it was written by the same author as
the patterns; real screens will do worse.

The scrubber still hides personal data from your agent. With run tokens the
model can match a name in its task to the same name on screen and ask to
type it, but it can't read the name itself, so it can't notice a misspelled
or wrong one. Task success with scrubbing on has not been measured.

## Failing closed

| Situation | What happens |
| --- | --- |
| openadapt-privacy, its spaCy model, pytesseract or tesseract is missing, or a probe scrub fails | `flight` exits 2 before it starts your command |
| No OS egress block on this system, and no `--allow-unenforced-egress` | `flight` exits 2 before it starts your command |
| A model call went around the proxy (see the egress block) | The run is marked refused and `flight` exits 3 |
| An image can't be decoded, an image or file is sent by URL, the body is compressed or isn't JSON, or the scrubber raises | The request is answered with HTTP 502 and a clear message, and is not forwarded |
| The spend cap is reached, or a cap is set and the model has no price | HTTP 429, and the request is not forwarded |
| The gate can't read the target under the pointer | The action counts as commit-class and goes to the gate |
| The gate errors, or the gate URL can't be reached | The action is refused and not performed |
| No scrubber is available for a local frame or value | The frame is not written, and the value is written as `[withheld]` |

`--no-scrub` turns scrubbing off. It only works with `--i-accept-unscrubbed`,
and each use is recorded in `flight.json` and in the local
`~/.openadapt/unscrubbed-sends.jsonl` ledger.

Image redaction finds text in a screenshot and covers it. It does not detect
faces or handwriting, and it can't cover text tesseract can't read.

## The pre-commit gate

The gate checks clicks, double clicks, Enter and Space keys, Ctrl+S or Cmd+S,
and typed text that ends a line. For each one, the wrapper reads the label of
the target:

1. from your executor's accessibility hook (`label_at(x, y)`, or
   `focused_label()` for keys) when it has one. The hook may name the role
   first, as in `"button Save"`;
2. otherwise, by OCR of a fresh screenshot. Only a text line whose box holds
   the click point names the target, or every line inside the target's
   bounds when your executor has `target_bounds(x, y)`. A line next to the
   target, such as the field label beside an icon-only button, is not its
   label.

The gate fails closed. An action passes without a person only when its
target is known to be safe:

- the role only focuses or reveals: `textbox`, `searchbox`, `textarea`,
  `combobox` or `tab` (this needs the accessibility hook); or
- the whole label, ignoring case and surrounding arrows, is one of: Back, Go
  back, Cancel, Close, Search, Find, Open, View, View details, Details, Show
  more, Show less, More, Expand, Collapse, Menu, Previous page, Next page,
  Home, Help.

Everything else is commit-class: Save, OK, Done, Next, Apply, Delete,
"Cancel order", a label in another language, an icon with no text, a target
that can't be read, and any action the JIT translators can't classify (a
triple click, a separate mouse-down). Enter and Space on the focused control
use the same rule; Enter also passes in a multi-line text area or a search
box, and Space in a text field. Ctrl+S or Cmd+S is always commit-class.

`--safe-label LABEL` (repeatable) adds labels for one deployment, such as a
navigation button your app calls "New Encounter". A label with Save, Submit,
Send, Sign, Approve, Confirm, Post, Pay, File or Finalize in it is refused
there. The added labels are recorded in `flight.json`.

A bare Cancel or Close passes. If Cancel on one of your screens cancels
something (an appointment), have your hook name it in full
(`"button Cancel appointment"`) so that the click is held.

A commit-class action goes to the gate, which allows, holds or refuses it.
Unless the gate allows it, your executor is never called. The agent gets back
"held for a person: not performed".

| `--gate` | Who decides |
| --- | --- |
| `terminal` (default) | `y/N` on the terminal that runs `flight`. With no terminal, the action is refused. |
| `file` | An item-approval request (`openadapt.item-approval-request/v1`) in the run directory. Desktop's Run it or a paired phone answers it; only a person's signed answer counts. If no one answers within `--approval-timeout`, the action stays held. |
| `refuse` | Nobody. Every commit-class action is held. Use this for unattended runs. |

Because the gate fails closed, it holds most clicks when your executor has
no accessibility hook: with OCR alone it can't tell a button from a field, so
every click on text that isn't on the list, and every click on an empty
field or an icon, goes to a person. Add `label_at` to your executor (with the
role first) to let clicks into fields through.

The gate sees clicks and keys, not network traffic. An app that saves on
blur, on change, or when a dropdown changes saves without a commit click,
and the gate holds nothing there.

## The one-line swap

### Anthropic computer-use reference loop

In the reference implementation's `loop.py`, wrap the computer tool:

```python
from openadapt_flow.flight import GatedComputer

tool_collection = ToolCollection(
    GatedComputer(ComputerTool(), held=lambda msg: ToolResult(error=msg)),
    BashTool(),
    EditTool(),
)
```

`GatedComputer` is async when the tool is. It passes `name`, `to_params()`
and other attributes through. With the `computer_toolset_20260801` toolset,
the tool name is the action, so call it as `computer(action=name, **input)`.

### OpenAI CUA sample loop

```python
from openadapt_flow.flight import GatedComputerCall

with LocalPlaywrightBrowser() as computer:
    agent = Agent(computer=GatedComputerCall(computer))
```

Every method passes through. A held action is not performed. The next
`screenshot()` has a red banner at the top saying so, because a
`computer_call_output` returns only a screenshot.

### Browser-Use

Browser-Use drives Chromium through CDP. Start Chromium with a DevTools port,
point the LLM at the proxy, and give `flight` the existing guard's config:

```python
import os
from browser_use import Agent, Browser, ChatAnthropic

browser = Browser(cdp_url="http://127.0.0.1:9222")
llm = ChatAnthropic(model="claude-opus-5-5", base_url=os.environ["ANTHROPIC_BASE_URL"])
agent = Agent(task="...", llm=llm, browser=browser)
```

```bash
openadapt flight --cdp http://127.0.0.1:9222 \
    --guard-config guard.yaml --guard-task task.json -- python browser_agent.py
```

With `--cdp`, `flight` also runs `openadapt-flow guard`, which holds each save
until the right record is on screen and reads it back. See
[GUARD_AN_AGENT.md](GUARD_AN_AGENT.md). If the command starts Chromium itself
with `--remote-debugging-port=N`, `flight` uses that port. Without
`--guard-config` and `--guard-task`, it prints a reminder and runs without
the guard.

### Your own loop

```python
from openadapt_flow.flight import GatedComputer, TerminalGate, Timeline

timeline = Timeline("runs/flight-1", task_id="t1", executor_label="my-loop")
computer = GatedComputer(my_executor, TerminalGate(), timeline)
...
timeline.finish()
```

## `flight.json`

```json
{
  "schema": "openadapt.flight-run/v1",
  "task_id": "referral-0042",
  "executor_label": "python",
  "started_at": "2026-11-21T10:00:00.000Z",
  "ended_at": "2026-11-21T10:03:12.512Z",
  "model": {
    "calls": 7,
    "input_tokens": 8400,
    "output_tokens": 560,
    "cost_usd": 0.0476,
    "cost_source": "price_table"
  },
  "actions_count": 6,
  "gate_events": [
    {"at": "2026-11-21T10:03:10.004Z", "kind": "held", "reason": "the target reads 'button Save Encounter' (save)"}
  ],
  "timeline_path": "timeline.jsonl",
  "details": {"scrub": {"mode": "on"}, "exit_code": 0}
}
```

The top-level keys are exactly the ones the run ledger collects. `details` is
optional and a reader may ignore it. It holds the scrub mode, cache tokens,
the priced part of the cost when some model had no price, the command's exit
code, the gate used and the guard's result. `cost_source` is `price_table`,
`unknown` (some call had no price, so `cost_usd` is `null`) or `no_calls`.
`actions_count` is the larger of the actions the model asked for and the
actions the executor reported.

## Limits

- The proxy only sees calls that use `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`.
  On macOS a direct connection from a client with a hard-coded base URL is
  blocked; elsewhere, with `--allow-unenforced-egress`, it is not (unless it
  honours `HTTPS_PROXY`). A run whose agent writes no `usage.json` is then
  refused (see the egress block).
- Scrubbing blinds the agent to the personal data it scrubs (see above).
- OpenAI models are not in the price table, so their cost is recorded as
  unknown.
- `flight` does not run agents; it records the agent you run.
