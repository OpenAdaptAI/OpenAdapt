# Code as action: the run-1 agent works through a terminal

Before OpenAdapt compiles a task, an agent does it once
([COMPILE_AGENT.md](COMPILE_AGENT.md)). With `vision="code"` that agent does
not look at a screenshot after every click. It gets one terminal-style tool,
`run`, and writes short scripts: read the app's structure, act on elements by
role and name, read the result back, keep notes in a working directory, and
take a screenshot only when it asks for one. One model call can carry several
reads and actions.

Every action is still recorded by the JIT capture, gated by the same save
check, and compiled into the same program. The difference is what the agent
sees and how much of it the model has to read.

```python
from openadapt_flow.compile_agent import ClaudeAgent, compile_first_run

result = compile_first_run(
    "Open the referral for Jane Sample, start a Triage encounter, and save this note: {note}",
    params={"note": "Follow up in two weeks"},
    agent=ClaudeAgent(vision="code", model="claude-sonnet-5-5"),
    url="http://localhost:8080/#tasks", verify="verify.yaml",
    expect={"patient_id": "p1"}, on_screen=["Jane Sample", "MRN P1"],
)
```

```bash
openadapt-flow compile-agent "..." --vision code --url ... --verify verify.yaml ...
```

`--agent scripts.json` with `{"scripts": ["tree", "click name=Open near=\"Jane Sample\""]}`
runs the same tool from fixed scripts with no model calls (tests, CI, demos).

## Why

Three 2026 harness results point the same way.

- **CUAWright** (Microsoft Research, arXiv:2610.04116) gives the agent a
  terminal as its only action interface, keeps state in files, and shows a
  screenshot only on request ("vision on demand, not by default"). It reports
  67.9% partial reward on OSWorld 2.0 at $4.40 a task, against 62.7% and
  $13.90 for the same model's official harness. Removing file-system access
  cost it about 12 points.
- **OpenGCUA** (Alibaba Tongyi, arXiv:2610.22200) has the model write one
  program a step that mixes GUI, accessibility, API and shell calls, with
  loops and branches, and requests screenshots itself. Against batched GUI
  actions it reports 53% fewer steps on average across eight models.
- **ANIS** (arXiv:2610.15765) builds commands for desktop apps from their own
  registered operations, keeps only commands whose effect it can observe after
  saving, and reports about 73% fewer online tokens than raw SDK use. Taking
  away the read-back commands cost 12 points: a command that returns success
  has not shown that the task succeeded.

All three report their own results on their own baselines.

## What OpenAdapt takes from them, and what it does not

| Idea | Here |
| --- | --- |
| One terminal tool, scripts, text output | `run` takes a script; each line prints what it did or read. |
| State on disk | A working directory per run. `save FILE` writes the last output there; `sh grep ... FILE` reads it back. |
| Vision on demand | `screenshot` is a command. Nothing else sends an image to the model. |
| Accessibility first | `tree`, `find` and `read` list the DOM (every frame) or a desktop accessibility tree. |
| Compose several actions | A script can hold up to 40 lines; refs change after each action, so later lines name targets by role and name. |
| Read back after a write | After a save the script stops and the agent is told to read the screen. The trace records whether it read its inputs back. |

What it does not take: the script is not Python and not a full shell. Every
line is either a read or one action the recorder can hold (click, fill,
select, press, scroll). That is what lets each action go through the save gate
before it is performed and become a step of the compiled program. A general
program could write to the app in ways the gate and the compiler never see.

## The commands

```
tree [grep=TEXT] [max=N]   the structure: headings, text, rows, controls with @refs
find [role=R] [name=TEXT] [near=TEXT]   controls that match
text [grep=TEXT]           visible text lines
read TARGET                an element's value and the row it is in
click TARGET
fill TARGET value=TEXT     click an empty field and type TEXT
select TARGET option=TEXT  choose an option in a list or drop-down
press KEY                  Enter, Tab, Escape, ArrowDown, ...
scroll down|up [px=N]      scroll where the pointer is
screenshot                 an image of the screen
sh COMMAND                 read-only shell in the working directory
save FILE                  the previous command's output, to FILE
wait ms=N                  up to 2000 ms
```

A TARGET is `@N` from the latest listing, or `role=button name="Open"
near="Jane Sample"` (`near` is text in the same table row). A description must
match exactly one element; if it matches several, the line fails and lists
them. On a screen with no structure, `text="Save"` names an OCR line.

Lines run in order and stop at the first error. An error never performs part
of an action. A few rules come from the recording format:

- `fill` types only into an empty field (typing appends to what is there).
- `select` on a native drop-down clicks it, types the option and presses Tab
  (the recorded form of a select), then checks the drop-down shows that option.
  On a custom list it clicks the list, then the option by name.
- `scroll` happens where the pointer is, because the recording has no pointer
  move. If nothing moved, the line fails and says to click inside the area
  first.
- Before a click, the tool checks the element is still at its point, in the
  same row, and not covered (a dialog backdrop, for example).

## Every action is a structured step

The trace (`code_action_trace.json` next to run 1) holds one step per action:

```json
{"action": "click",
 "element": {"role": "button", "name": "Open", "row": "Jane Sample | Knee pain referral | High | Open", "source": "dom"},
 "events": [0],
 "recorded": {"selector": "#open-p1", "role": "button", "name": "Open"},
 "unique": true, "agrees": true, "matches_precheck": true, "gated": false}
```

`element` is what the agent acted on. `recorded` is the structural locator the
recorder stored for that event, read from `events.jsonl`; it is also read just
before the click, and `matches_precheck` says the two agree. `unique` says the
locator finds one element on that screen, as the program will need at replay.
After compiling, `check_trace_against_ir` checks that the program carries each
of those locators on its steps, in order. The result is in
`run1["code_action"]["program"]`.

The trace also records what the agent read: for each `tree`, `find`, `text`
and `read`, which of the task's identity strings and input values appeared.
The draft's `compiled/agent_reads.json` keeps the identity the agent read
before the save and the inputs it read back after it, and
`FirstRunCompiler.proposed()` shows them to the person who confirms the
record checks, next to the checks proposed from the system of record.

## Surfaces

| Surface | Reads | Acts | Notes |
| --- | --- | --- | --- |
| Web (Playwright) | DOM of the page and every frame | element center, recorded with its DOM locator and frame path | `tree` says how many controls are above or below the visible area. |
| Windows, macOS, Linux | the authoring tree (`AuthoringSession.observe`), OCR text | role and name through the backend's `locate_structural` | No native backend implements `authoring_tree` yet, so listings fall back to OCR text; acting by exact role and name works now. |
| RDP, Citrix | OCR lines | the line's center | Pixels only. Steps compile with visual anchors, as for any pixel recording. |

The surface comes from the recording computer: a browser page is the DOM; a
screen-only session (RDP, Citrix) is pixels; any other session is a desktop
tree. compile-agent itself runs web apps only until `jit.SurfaceApp`
([#1283](https://github.com/OpenAdaptAI/OpenAdapt/pull/1283)) lands; the tool
and the save gate already work without a page (tests/test_code_action.py
drives them on a fake desktop surface).

## Safety

Nothing here loosens what run 1 may do.

- **The save gate.** Every action that may commit a write goes through the
  harness's right-record check and hold before it is performed
  ([COMPILE_AGENT.md](COMPILE_AGENT.md)). The tool passes the harness what it
  resolved (`CommitHint`: the control's name and the text and field values
  around it). On the web a hint can add a gate, never remove one. On a
  surface with no page, the tool's resolved element is the only reading: a
  click it did not resolve is gated and refused, and Enter and Space are
  always gated. A drop-down whose change submits its form (an `onchange`
  handler) is gated like a save.
- **The customer's key.** The agent calls the model on the key the operator
  supplies; OpenAdapt holds none. The spend cap (`max_cost_usd`) holds as for
  any `ClaudeAgent`.
- **The shell.** `sh` runs only text tools (`cat`, `grep`, `head`, `wc`,
  `sort`, ...) from `/usr/bin` and `/bin`, each with a short list of allowed
  flags and no long options, so no flag can name a file, write one or run a
  program (`sort --compress-program`, `grep -f`, `sort -o` are refused).
  Commands join with `|` only; no `;`, `&`, subshells or input redirection;
  output redirection only to a plain file name in the working directory; no
  argument may leave that directory; an empty environment; 5 seconds per
  command, in its own process group that is killed whole. On macOS each
  command also runs under `sandbox-exec`: no network, no program but the one
  named, reads only in the working directory and system directories, writes
  only in the working directory. On Linux it runs under `unshare --net` when
  available.
- **The app's origin.** On the web, an action that leaves the app's origin
  ends the run.
- **Privacy.** Everything the tool returns is scrubbed with the same scrubber
  as the structural listing, except the task's own values.

Four gate fixes came with this work and apply to every compile-agent agent.
Each one wrongly refused, or failed to gate, a step on OpenEMR:

- a click inside an iframe is read through the backend's hit-tested frame
  chain (before, the harness saw only the frame, so a Save inside a frame was
  not gated);
- Enter with focus inside an iframe reads the frame that has focus;
- a click on a text field is not a save even when its label holds a commit
  word ("Postal code" starts with "post");
- Enter on a native drop-down is not a save (it picks an option), and a table
  row with more than 8 controls is page layout, so the gate's scope is the
  form or section around it, not that row.

## Measured

claude-sonnet-5-5, effort medium, on the operator's key. Raw rows:
[benchmark/code_as_action/results/](../benchmark/code_as_action/results/).
The host's 1-minute load was far above its 12 cores for every run (other
sessions' work), so every timing below was taken under load and is labelled
that way in the data; tokens, calls, actions and cost do not depend on load.

**MockMed**, a five-step note task, 5 trials per arm (correct = the record
check passed):

| median per run | code | structure first | screenshot loop |
| --- | ---: | ---: | ---: |
| Correct (read back) | 5 / 5 | 5 / 5 | 5 / 5 |
| Model calls | 6 | 5 | 5 |
| Actions | 6 | 6 | 13 |
| Prompt tokens | 12,738 | 10,332 | 73,303 |
| Cost | $0.012 | $0.014 | $0.075 |
| Screenshots sent | 0 | 1 | 14 |

On a task this short, code as action and the structural listing are about
the same. The program compiled from a code run was admitted and replayed
twice: VERIFIED, 0 model calls.

**OpenEMR referral**, register a made-up referred patient from 12 fields in a
form inside an iframe, without creating a duplicate; 8 referrals per arm,
correct = SQL read-back (one new chart, 12 of 12 fields; for the
possible-duplicate referral, no new chart):

| median per run | code | screenshot loop |
| --- | ---: | ---: |
| Correct (read back) | 8 / 8 | 6 / 8 |
| Model calls | 12 | 14 |
| Actions | 36 | 56 |
| Prompt tokens | 90,777 | 619,498 |
| Cost | $0.055 | $0.332 |
| Model time | 18.5 s | 48.6 s |
| Run time (under load) | 45.4 s | 75.2 s |
| Screenshots sent | 2 | 56 |

The screenshot loop's two misses were stopped by the save gate before any
save (Enter on a form `<button>`, and a Ctrl+End shortcut). Neither arm wrote
a wrong or duplicate chart. Details, caveats and the runs set aside while
fixing gate bugs: [benchmark/code_as_action/README.md](../benchmark/code_as_action/README.md).

## Limits

- The script language is fixed. An agent can't loop or branch inside a
  script; it reads, decides, and sends the next script.
- `scroll` depends on where the last click left the pointer.
- Desktop listings need a backend `authoring_tree`; until then a desktop
  agent finds controls from OCR text and acts by exact role and name.
- The trace check compares locators, not behaviour: a program that carries
  the right locators can still fail at replay, which its canaries and the
  record check catch.
