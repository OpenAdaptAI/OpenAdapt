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
- **Scrubbing before anything leaves the machine.** The proxy redacts every
  base64 screenshot with openadapt-privacy's image redaction, and scrubs every
  content text block with its text scrubber, before it forwards the request.
- **A redacted timeline.** `timeline.jsonl` lists each model call, each
  computer action the model asked for, each action your executor performed
  and each gate decision. Frames are written as scrubbed thumbnails.
  `timeline.html` is a static page you open locally.
- **A pre-commit gate.** If you swap one line in your loop, every click or
  Enter on a Save, Submit or Send control is held for a person before it
  happens.

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
   gate.
5. When the command exits, `flight` writes `flight.json`, prints the run
   directory, and exits with the command's exit status.

The proxy sends nothing anywhere except to the provider your agent already
targets. Anything that leaves the machine is scrubbed first.

## Failing closed

| Situation | What happens |
| --- | --- |
| openadapt-privacy is not installed | `flight` exits 2 before it starts your command |
| An image can't be decoded, an image or file is sent by URL, the body is compressed or isn't JSON, or the scrubber raises | The request is answered with HTTP 502 and a clear message, and is not forwarded |
| The spend cap is reached, or a cap is set and the model has no price | HTTP 429, and the request is not forwarded |
| The gate can't read the target under the pointer | The action counts as commit-class and goes to the gate |
| The gate errors, or the gate URL can't be reached | The action is refused and not performed |
| No scrubber is available for a local frame or value | The frame is not written, and the value is written as `[withheld]` |

`--no-scrub` turns scrubbing off. It only works with `--i-accept-unscrubbed`,
and each use is recorded in `flight.json` and in the local
`~/.openadapt/unscrubbed-sends.jsonl` ledger.

Some fields are passed on byte for byte, because the provider must get them
unchanged: thinking and reasoning blocks (their signatures must verify), tool
definitions, ids and model names. A thinking block holds only what the model
wrote from input that was already scrubbed.

Image redaction finds text in a screenshot (OCR, then named-entity
recognition) and covers it. It does not detect faces.

## The pre-commit gate

The gate checks clicks, double clicks, Enter and Space keys, Ctrl+S or Cmd+S,
and typed text that ends a line. For each one, the wrapper reads the label of
the target:

1. from your executor's accessibility hook (`label_at(x, y)`, or
   `focused_label()` for keys) when it has one;
2. otherwise, by OCR of a fresh screenshot near the pointer.

The action is commit-class when the label holds one of these words: Save,
Submit, Send, Sign, Approve, Confirm, Post, Pay, File, Finalize. It is also
commit-class when no label can be read. An action the JIT translators can't
classify, such as a triple click or a separate mouse-down, is commit-class
too.

A commit-class action goes to the gate, which allows, holds or refuses it.
Unless the gate allows it, your executor is never called. The agent gets back
"held for a person: not performed".

| `--gate` | Who decides |
| --- | --- |
| `terminal` (default) | `y/N` on the terminal that runs `flight`. With no terminal, the action is refused. |
| `file` | An item-approval request (`openadapt.item-approval-request/v1`) in the run directory. Desktop's Run it or a paired phone answers it; only a person's signed answer counts. If no one answers within `--approval-timeout`, the action stays held. |
| `refuse` | Nobody. Every commit-class action is held. Use this for unattended runs. |

Because "unknown" counts as commit-class, a click on a control with no
readable text (an icon button, an empty field) is held when your executor has
no accessibility hook. Add `label_at` to your executor to avoid that.

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
  A client built with a hard-coded base URL bypasses it, and `flight` warns
  when no call came through.
- Function-call `arguments` strings are forwarded as they are. Content text,
  typed text in computer actions, tool results and images are scrubbed.
- OpenAI models are not in the price table, so their cost is recorded as
  unknown.
- `flight` does not run agents; it records the agent you run.
