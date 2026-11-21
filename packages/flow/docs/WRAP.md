# Wrap an agent task

`openadapt wrap` lets an agent do a task once, reads the save back from the
system of record, compiles the run into a program, and admits it. Later
calls with new inputs run the program instead of the agent: no model calls,
and every save is read back again.

## Try it with one command

```bash
export ANTHROPIC_API_KEY=...
uvx --python 3.12 'openadapt[wrap]@latest' wrap --example
```

`--example` runs the whole loop on a bundled sample task. Nothing leaves the
computer except the agent's model requests.

1. A synthetic clinic app (MockMed) starts on `127.0.0.1`. Its patients and
   notes are made up.
2. **Run 1.** OpenAdapt's built-in Claude agent opens a patient's referral,
   starts a Triage encounter and saves a note, in a headless browser. The
   save is read back from the app's records. The run is compiled, a canary
   runs with another note, and the draft is admitted.
3. **Run 2.** A new note runs the compiled program through its MCP tool. It
   makes no model calls, and the save is read back again.

It prints each run's model calls, cost and time, the record the app holds
after run 2, and the folder with every file it wrote:

```text
Run 1: claude-opus-5-5 does the task (note "Synthetic follow-up in two weeks").
  ...
Run 2: the compiled program, new input (note "Recheck knee in ten days").
  0 model calls, $0.0000, ...; status VERIFIED

Read back from the app's records (/api/db):
  {"id": ..., "note": "Recheck knee in ten days", "patient_id": "p1", "type": "Triage"}
```

The exit status is 0 only when run 1 was admitted and run 2 made no model
calls and was read back `VERIFIED`. `--json` prints one JSON result instead.

### What the example does differently from a real task

The example is safe to run unattended only because its app and data are
synthetic. A real task differs in three ways:

| | `--example` | A real task (`--task`) |
|---|---|---|
| Who confirms the proposed read-back checks | Confirmed automatically | A person at the terminal (`--person`) |
| Who admits the draft | A throwaway key made in the example's folder | A person, or a registered admitter key (`--admit-key`) |
| Screenshots sent to the model | Unscrubbed, under the recorded opt-out | Scrubbed by the `privacy` extra, unless you opt out |

The throwaway approver and admitter keys live in the example's own folder
(`~/.openadapt/wrap/example/<time>/keys`, or `--out`). They are never written
to your key directory and admit nothing else. Each unscrubbed send is
recorded in `~/.openadapt/unscrubbed-sends.jsonl`.

### Options

| Option | Default | |
|---|---|---|
| `--max-cost-usd` | `2.0` | Spend cap for run 1. The agent reserves each request's worst case before sending it. |
| `--provider` | the key that is set | `claude` or `openai`. With `openai`, pass `--model` and its reviewed prices (`--price`, `--price-model`, `--pricing-source-url`, `--pricing-verified-at`). |
| `--model` | `claude-opus-5-5` | The model for run 1. |
| `--out` | `~/.openadapt/wrap/example/<time>` | A new, empty folder for the example's files. |

Without a model key the example stops before anything starts and names the
variable to set (`[MISSING_MODEL_KEY]`).

## Wrap your own agent

`openadapt wrap --task task.json --param NAME=VALUE -- COMMAND ...` runs your
agent, any program, for the first run instead of OpenAdapt's built-in agent.
OpenAdapt opens the task's app in its own headless browser and starts
`COMMAND` with three environment variables:

| Variable | What it holds |
|---|---|
| `OPENADAPT_MCP_URL` | A Model Context Protocol endpoint (streamable HTTP) on `127.0.0.1` |
| `OPENADAPT_MCP_TOKEN` | A random token for this run. Send it as `Authorization: Bearer <token>`; other requests get `401` |
| `OPENADAPT_TASK` | The task with its inputs, as the agent should read it |

The endpoint has six tools: `observe` (the page as a listing, with refs such
as `e3` on its controls), `click`, `type_text`, `press_key`, `scroll` and
`finish`. They are the tools OpenAdapt's own agents use, with the same
checks:

- Before a save, the record the task names must be on the screen, or the
  save is refused and the run ends.
- Every save is read back from the system of record. A change to another
  record, a second save or an unreadable store ends the run.
- An action that leaves the task's origins ends the run.
- Every action is recorded for the compiled program.

The command gets no handle to the browser itself, so it can't act around
these checks. It also gets your environment without any `OPENADAPT_*`
variable, so OpenAdapt's own keys and settings never reach it; its own
variables, such as its model key, pass through.

When the command exits, the endpoint closes. Then the run goes through the
same record check, proposed checks, canary and admission as any
`openadapt wrap --task` run (see [JIT_ADMISSION.md](JIT_ADMISSION.md)). Run it
in a terminal with `--person "Your Name"`: you confirm the proposed read-back
checks, review the steps and attend the canary there. Without `--person`, run 1
stays a draft, and `wrap run` reports `[DRAFT_NOT_ADMITTED]`.

```bash
openadapt wrap --task task.json --param note="Synthetic follow-up" \
  --person "Your Name" -- python my_agent.py
```

A nonzero exit makes no program, and `openadapt wrap` exits with the
command's status. The command is stopped after `--command-timeout` seconds
(default 600).

When the privacy extra scrubs what the agent sees, the task's own values (its
record's identity and its inputs) appear as placeholders such as
`⟦task.identity_1⟧` in `OPENADAPT_TASK` and in every listing. Typing a
placeholder types its value. The endpoint's instructions say so too.

[`examples/wrap_command/agent.py`](../examples/wrap_command/agent.py) is a
small agent with no model that does the sample clinic task through the
endpoint, with the `mcp` client SDK:

```python
async with streamablehttp_client(url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.call_tool("observe", {})
```

OpenAdapt can't see the model calls your command makes, so `openadapt wrap`
reports run 1's cost as unknown.

The command runs as an ordinary process with your permissions. Only the
browser it drives belongs to OpenAdapt.

### Later runs

```bash
openadapt wrap run --task task.json --param note="Recheck knee in ten days"
```

`wrap run` runs the admitted program only: no command and no model calls,
and the save is read back. A task with no admitted program stops with
`[NO_ADMITTED_PROGRAM]` and runs nothing.

## Wrap your own task

Describe the task, its read-back and its record in a task file, then run
`openadapt wrap --task task.json`. See
[WRAP_PUBLISH.md](WRAP_PUBLISH.md#3-describe-the-task) for the file and the
admission options, and [JIT_ADMISSION.md](JIT_ADMISSION.md) for what
admission requires.
