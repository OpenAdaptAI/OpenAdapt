# Wrap an agent task

`openadapt wrap` lets an agent do a task once, reads the save back from the
system of record, compiles the run into a program, and admits it. Later
calls with new inputs run the program instead of the agent: no model calls,
and every save is read back again.

## Try it with one command

```bash
export ANTHROPIC_API_KEY=...
uvx --python 3.12 openadapt@latest wrap --example
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

## Wrap your own task

Describe the task, its read-back and its record in a task file, then run
`openadapt wrap --task task.json`. See
[WRAP_PUBLISH.md](WRAP_PUBLISH.md#3-describe-the-task) for the file and the
admission options, and [JIT_ADMISSION.md](JIT_ADMISSION.md) for what
admission requires.
