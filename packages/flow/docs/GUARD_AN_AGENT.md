# Guard an agent you already run

Preview. The command, the file formats and the tool names may change before a
release.

If your agent already drives a Chromium (a Playwright script, Playwright MCP,
browser-use, or a computer-use agent with a browser), you keep it. You start
its browser with a DevTools port and run `openadapt-flow guard` next to it.
The agent doesn't call OpenAdapt and doesn't know it is there.

- **Run 1, guarded.** The guard holds every save the agent's tab sends until
  the record on the screen is the one the task names. If it isn't, the guard
  blocks the save and the agent's page says why. After a save goes through,
  the guard reads the record back from the system of record.
- **Run 2, compiled.** When run 1 reads back correctly, it becomes a draft
  program with a record check built from the app's own reads. A person
  confirms the check and admits the draft. Then it is a tool your agent calls,
  and that tool makes no model calls.

Nothing leaves your computer, and the guard makes no model calls.

## Set it up

1. Add a DevTools port to the browser your agent launches. This is the only
   change to the agent:

   | Agent | The line |
   |---|---|
   | Playwright (Python) | `chromium.launch(args=["--remote-debugging-port=9222"])` |
   | Playwright MCP | `"args": ["@playwright/mcp", "--cdp-endpoint", "http://127.0.0.1:9222"]` with Chromium started on that port |
   | browser-use | pass `cdp_url="http://127.0.0.1:9222"` to its browser, with Chromium started on that port |
   | Your own Chromium | `--remote-debugging-port=9222` on its command line |

   The endpoint must be on this computer (`127.0.0.1` or `localhost`).

2. Write `guard.yaml` once per app:

   ```yaml
   schema_version: openadapt.agent-guard/v1
   app_url: http://127.0.0.1:8765/#tasks   # the guard holds saves to this origin
   verify: verify.yaml                      # how the save is read back
   screen_selector: "#patient-banner"       # optional: where the record is shown
   not_saves:                               # optional: writes that aren't the save
     - {method: POST, path: /chart/signin}
   approval: not_gated                      # each_item: a person approves each save
   teach:                                   # optional: make run 2 a program
     dir: .openadapt-teach
     name: triage_note
   ```

   `verify.yaml` is the file [`verify-run`](VERIFY_RUN.md) reads: the reader
   for the system of record and the record the save must produce. See
   [GUARDED_SAVE.md](GUARDED_SAVE.md) for an example.

3. For each task, whoever assigns the work writes the task file. The agent
   never writes it, so it can't change what it is checked against:

   ```json
   {
     "schema_version": "openadapt.guard-task/v1",
     "task_id": "triage-p1-0001",
     "expected_record": {"patient_id": "p1", "name": "Jane Sample"},
     "intended_fields": {"type": "Triage", "note": "Knee pain, refer"}
   }
   ```

4. Start the guard before the agent starts the task:

   ```bash
   openadapt-flow guard --cdp http://127.0.0.1:9222 --config guard.yaml --task task.json --out runs/triage-p1-0001
   ```

   The guard waits for a tab on `app_url`'s origin, attaches, and prints
   `guard: attached`. Then run your agent as usual. The guard ends after the
   save is read back.

## What the guard does with each request

The guard pauses the tab's requests with the CDP `Fetch` domain: documents
(form posts), `fetch`, XHR, beacons and others. Images, event streams,
scripts, styles, fonts and media aren't paused.

| Request | What happens |
|---|---|
| `GET`, `HEAD`, `OPTIONS`, a GraphQL `query` | Goes on at once. |
| A write listed in `not_saves` | Goes on. |
| `POST`, `PUT`, `PATCH`, `DELETE`, another method, or a GraphQL `mutation` to the app's origin | Held. This is a save. |
| A write to another origin | Blocked, unless it is in `not_saves`. |

A GraphQL body that Flow can't read, such as a persisted query with no text,
counts as a save.

For a save, the guard runs the [guarded save](GUARDED_SAVE.md) contract on
the agent's behalf:

1. It reads the text of the record on the screen (`screen_selector`, or the
   whole page) and checks that every `expected_record` value is there as a
   whole word. It reads the system of record for a baseline.
2. It commits the task once. A second save of the same task is blocked.
   With `approval: each_item`, the save waits for a person
   (`openadapt-flow item-approval approve ...`, Desktop or the phone portal).
3. It lets the request go on. When the request finishes, it reads the record
   back through `verify-run`: `VERIFIED`, `REFUTED` or
   `RECONCILIATION_REQUIRED`, with a receipt.

A blocked save gets a `409` response with an `X-OpenAdapt-Guard` header. A
`fetch` or XHR gets a JSON body that names the reason. A form post gets a
page that says why. The guard also puts a notice at the top of the agent's
page (`role="alert"`), so an agent that reads the screen or the accessibility
tree sees it. The notice names the fields that didn't match, not their
values.

### Observe only

`--observe-only` (or `observe_only: true`) never blocks. The guard runs the
same checks, records what it would have blocked, and still reads the save
back. Use it to see what the guard would do before you turn it on.

### Fail-closed

The default blocks when the guard can't tell that a save is safe: the record
isn't on the screen, the system of record can't be read before the write, a
second save of the task, a write to another origin, or an error in the guard.
If the guard stops while a save is paused, it blocks that save before it lets
go of the tab, because Chromium sends paused requests on when the session
that paused them goes away.

## Run 2: a program from run 1

With a `teach:` section, a run that reads back `VERIFIED` is handed to the
teach dir, the same place the MCP teach tools
([packages/agent](../../agent/README.md#teach-a-new-workflow-from-your-agent))
keep their drafts:

1. The recording (with its network capture) is compiled. When the save is a
   form post that replays cleanly, `compile` proposes an API step
   (`api_proposal.json` says which step, or why not). The GUI steps stay as
   the fallback.
2. Run 1's record change is checked again: exactly one new record, nothing
   else changed, the record the task named, every input in it.
3. `scaffold-verifier` builds the record check from the app's own reads (the
   reads the page made after the save, `teach.db` for a database read path, or
   the `verify.yaml` read) and fault-tests it. If it misses a fault, it is
   kept for review and the check from the before and after reads is proposed
   instead.
4. The draft is recorded as made by the attached agent, with no agent
   principal. Only a person or a registered admitter key can admit it.

Then a person confirms the check and admits:

```bash
openadapt-flow compile-agent admit .openadapt-teach/<task key> --admit-as "Dana Reviewer" --canary-param note="Canary note"
openadapt-agent serve --teach verify.yaml --teach-dir .openadapt-teach
```

The admitted program is the `taught_<name>` tool. Each call runs it with no
model calls and reads the save back.

## Measured on MockMed

Synthetic MockMed in front of the `effect_e2e` record store, localhost only,
no model calls anywhere. The outside agent is a scripted Playwright agent in
its own Chromium, with a 1 s pause before each of its 6 actions where a
model-driven agent would think. 15 scenarios, 3 arms, 2 trials each (90 runs,
the same outcome in both trials), one browser at a time, each judged by the
`effect_e2e` ground truth, which reads the SQLite file directly. Full tables
and raw rows: [benchmark/agent_guard/RESULTS.md](../benchmark/agent_guard/RESULTS.md).

The host was shared with other work: the 1-minute load average was 16 to 249
during the runs (median 29). The times below compare run 1 with run 2 under
the same load; they aren't absolute speeds.

**Setup the developer adds:** 1 line in the agent (the DevTools port on the
browser it launches), 1 command, a 9-line `guard.yaml` and a 16-line
`verify.yaml` once per app, and a 12-line task file per task from whoever
assigns the work.

**The `effect_e2e` faults**, injected in the app's store. Eight of the nine
leave a wrong or missing record:

| Of the 8 | Blocked by the guard | Caught by the read-back | Got through |
|---|---|---|---|
| Agent alone | n/a | n/a | 8: 6 with the screen saying saved, 2 with an error on screen |
| Guard, run 1 | 0 | 7 | 1 (`collateral_unaudited`) |
| Program, run 2 | 0 | 8 | 0 |

The guard can't block a fault in the app's server; it reads the record back
and reports it. Run 1's read is the app's REST record read, which doesn't
cover the billing table, so the stray billing write gets through, as it does
for the same read in [EFFECT_E2E.md](../benchmark/effect_e2e/EFFECT_E2E.md).
Run 2's record check came from scaffold-verifier with the database read path
(`teach.db`), so it guards the billing table too.

`timeout` (the save commits, then the server answers after 1.5 s) is a right
save: the agent alone sees it saved, and the guard and run 2 read it back
`VERIFIED`. When MockMed's page gives up
after 1.2 s, the agent alone sees an error for a save that landed; the guard
reads it back `VERIFIED`.

**What the guard blocks** is the agent's side: a save sent while the wrong
patient was on the screen (blocked with `identity_mismatch`; nothing was
written; the agent's page named the fields that didn't match), and the second
save when the page sent it twice (blocked; one record). Observe-only lets
both through and reports them on the read-back.

**Run 1 against run 2**, 5 pairs back to back at a load of 19 to 23:

| | Median time | Model calls |
|---|---|---|
| Run 1: the agent, guarded | 6.26 s | 0 here (scripted); about 6 for a model-driven agent, one per action |
| Run 2: the `taught_triage_note` tool over MCP | 4.39 s | 0 |

The guard held each save for its checks for a median 7.4 ms and read it back
in 41 ms; the agent's time with and without the guard was the same (6.24 s).
Run 1's time here is mostly the scripted pauses, so with a real model run 1 is
longer and run 2 is unchanged. Compiling run 1 into the draft took about 8 s
after the read-back. A person's admission (`compile-agent admit`, one attended
canary) took 8 s.

Run 2 ran under the JIT standard profile: durable, with governed
authorization. No API step was proposed. The compiler's recorded reason is
"the recording has no parameters to map into a request" (the attached
recorder marks no parameters; inputs are lifted after compile). MockMed's
save is also a JSON request built by script, which isn't proposed either, and
an API step doesn't run in a durable or governed run. So the program ran its
GUI steps, and the run-2 times are GUI times.

## Limits

- One task per guard session, from when the guard attaches until the save is
  read back. A save sent before the guard attaches isn't seen. Start the
  guard before the agent starts the task.
- One tab: the tab on the app's origin. A save from another tab or a popup
  isn't guarded.
- Saves sent over a WebSocket aren't seen. CDP `Fetch` doesn't pause them.
- The record check is text on the screen. If the screen shows the expected
  values for two records (a list), the check can pass on the wrong row. Set
  `screen_selector` to the element that shows the open record.
- The guard can't stop a fault in the app's server. It reads the record back
  after the save, so it reports those faults; it doesn't prevent them.
- The agent must click a field before it types into it. An agent that fills a
  field without clicking it (Playwright's `fill`, which Playwright MCP's
  typing tool uses) leaves no focus step in the recording, and the compiled
  program refuses at the canary because it can't confirm what it typed. Run 1
  is still guarded and read back.
- Compiling run 1 needs the recording. The recorder refuses a run it can't
  keep exactly (an agent that acts faster than a frame can be kept). The
  guard keeps guarding; only run 2 is missing.
- API steps are proposed only for form posts
  (`application/x-www-form-urlencoded`). A save built by script as JSON, like
  MockMed's, stays on GUI steps. Programs from the teach dir run as durable
  JIT runs, and API steps don't run in durable runs yet, so a taught program
  runs its GUI steps.
