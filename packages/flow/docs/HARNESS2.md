# Harness² with a read-back scorer

[Harness²](https://github.com/google-research/harness2) improves an agent by
editing its harness: the instructions and skills the agent works from. Model
proposers read full rollout trajectories and write new harness versions. A
model selector then picks the final one. Its benchmarks have no ground-truth
scorer, so the chosen run is only measured after selection, and the measure
is another model's judgement.

GUI tasks on a system of record do have a scorer. After the agent stops, the
store either holds the requested record on the requested patient, exactly
once, or it does not. `openadapt_flow.integrations.harness2` connects the two:
Harness² supplies the search, and OpenAdapt supplies the task environment,
the score, and the safety envelope.

## What it does

Each rollout:

1. starts a fresh copy of the app on a local port (never a production
   system);
2. registers the episode with the reward worker, which reads the baseline;
3. lets the agent drive the copy with the harness under test as its standing
   instructions;
4. reads the store again, judges it, and signs a reward receipt;
5. stops the copy.

The reward worker is the one `openadapt-flow serve-reward` runs
([REWARD_WORKER.md](REWARD_WORKER.md)). The contract for each copy is one
`exact_new_set` effect: the rollout adds exactly one record, for the
requested patient, with the requested type and note, and nothing else. The
oracle reads MockMed's `/api/db` through the `rest` recipe, so the receipt is
tier 2.

On top of the receipt, the adapter records which records were added and whose
they are, and turns the outcome into one number for ranking:

| Outcome | Search reward |
|---|---|
| `verified` | 1.0 |
| Safe stop: `halted_before_effect`, nothing written | 0.0 |
| The requested patient's one new record has the wrong values | -0.5 |
| `wrong_effect`: reported saved, but no record landed | -0.5 |
| A record landed on another patient | -1.0 |
| The patient got more than one record | -1.0 |
| `reconciliation_required` or `failed_platform` | none (a person settles it) |

The penalties are the caller's choice (`Penalties`). A write on another
patient, a second write, or a record with the wrong values is penalised even
when the agent reported "stopped". The worker leaves that case for a person
to settle; the search still ranks it below a safe stop.

## The MockMed task family

All data is synthetic. Every task asks for the same thing: a Triage
encounter for Jane Sample, born 1980-01-01, with a fixed note. The app
changes underneath.

| Task | What changed | Right outcome |
|---|---|---|
| `mockmed/plain` | nothing | `verified` |
| `mockmed/lookalike` | another patient with the same referral reason sits above her | `verified` |
| `mockmed/sort` | the queue is sorted by name | `verified` |
| `mockmed/typelabel` | the encounter-type buttons swapped order | `verified` |
| `mockmed/timeout` | the save lands, then the client times out and shows an error | `verified` or `reconciliation_required` |
| `mockmed/missing` | her referral is gone | `halted_before_effect` |

The instruction never says which variant is running or what the right
outcome is.

## Two evidence modes

`OpenAdaptGuiAdapter(evidence=...)` controls what the proposers and the
selector see.

- `screen`: the trajectory (each screen the agent saw, each action, its
  reasons) and the agent's own report. This is Harness² as published.
- `readback`: the same, plus what the system of record shows after the run,
  as a final observation: which records were added, whose they are, and the
  outcome.

`score()` is the read-back in both modes. In `screen` mode Harness² calls it
only after selection, to measure the chosen run.

The receipt and the read-back file never enter a proposer's context as files
(`leak_globs`), and Harness²'s grader-verdict tripwire still runs on every
context it builds.

## Choosing the final

`reward_selection` picks the candidate the read-back rewarded most and
returns Harness²'s own `Selection` record. `run_task` runs one task end to
end with Harness²'s recursion code: `k` base rollouts, one mode (`seq` or
`agg`), selection by reward or by Harness²'s model selector, and the
read-back of the chosen run.

## Trace export

Meta-Harness ([arXiv:2603.28052](https://arxiv.org/abs/2603.28052)) gives
its proposer a filesystem with every earlier candidate's harness, full
execution traces and scores. In its ablation, full traces beat summaries,
56.7 to 38.7. `openadapt_flow.integrations.harness2.traces` writes that
filesystem for GUI work. `run_task` writes one for every task under
`<work_dir>/traces`.

```
traces/INDEX.md                        one line per trace, best reward first
traces/index.jsonl                     the same, one JSON object per trace
traces/candidates/<label>/harness/     the harness each candidate ran
traces/traces/<id>/trace.md            the full trace, readable
traces/traces/<id>/trace.json          the full trace, structured
```

Each trace keeps every step in full: the screen, the action, the reason the
agent gave, and any error. At each write it records whether the patient on
screen was the requested one (`verified`, `mismatch`, `unreadable`). It ends
with the read-back: the records added, whose they are, the outcome, the
reward and the receipt id.

`from_flow_run` reads a compiled program's `report.json` instead. Its
identity verdicts are the runtime's own gate results, and a read-back can be
attached. The repair search uses this to read a halted run beside the last
verified ones.

Screen text passes through `privacy.scrub_text` before it is written, like
`REPORT.md`. Identity verdicts are computed before scrubbing, so a scrubbed
export still says which write hit the wrong patient.

In `readback` mode the same identity verdict also appears in the trajectory
Harness² proposers read, as an observation after each write.

## Install

Harness² needs Python 3.12 and is not on PyPI. Install it from its
repository at the commit this integration is written against, beside the
extra:

```bash
pip install 'openadapt-flow[harness2]'
```

```bash
pip install --no-deps 'harness2 @ git+https://github.com/google-research/harness2@1cfe254e8d25b15f37048c3730134686abe60335'
```

```bash
playwright install chromium
```

The integration imports Harness² and copies none of its code. It registers
one prompt surface with Harness²'s prompt builder (its private table of
surface texts, built from its own INV and DOM templates), which is why the
commit is pinned.

## Use

```python
from pathlib import Path

from harness2.core.spec import ChannelSpec
from openadapt_flow.integrations.harness2.adapter import OpenAdaptGuiAdapter
from openadapt_flow.integrations.harness2.agent import ScriptedGuiAgent
from openadapt_flow.integrations.harness2.proposers import ScriptedProposer
from openadapt_flow.integrations.harness2.selection import run_task
from openadapt_flow.integrations.harness2.tasks import mockmed_tasks

adapter = OpenAdaptGuiAdapter(
    mockmed_tasks(("lookalike",)), ScriptedGuiAgent(), Path("runs"), evidence="readback"
)
prop = ScriptedProposer(ChannelSpec("claude_cli", "scripted"), audit_dir=Path("runs/audit"))
summary = run_task("mockmed/lookalike", 1, adapter=adapter, prop=prop, work_dir=Path("runs/work"))
print(summary["chosen_score"]["outcome"])  # verified
```

`ScriptedGuiAgent` and `ScriptedProposer` make no model calls; the tests use
them. For a real run, use `ModelGuiAgent` and `ModelProposer` with an
`HttpChatModel`, which charges every call to a `CostLedger` with a hard cap.
Harness²'s own `Proposer` (Claude Code or OpenCode on Vertex AI) also works
with this adapter unchanged.

## Surfaces: DOM, pixels, RDP

The agent reads the page through a driver. Three are supported, and the
scorer is the same for all of them, because it reads the system of record,
not the screen.

| Surface | Driver | What the agent sees |
|---|---|---|
| `dom` (default) | `agent.PageDriver` | the page text and its controls, from the DOM |
| `web_pixels` | `pixels.web_pixels` | OCR text segments from Flow's `PlaywrightBackend` screenshots; clicks at their centres |
| `rdp`, `citrix`, desktop | `pixels.PixelDriver(backend)` | the same, over any Flow backend, for example `RemoteDisplayBackend` on an RDP client window |

Pass the driver as `driver_factory` to `OpenAdaptGuiAdapter` or
`run_rollout`. `run.json` records the surface.

On pixels the agent has no DOM ids or field tags. OCR drops spaces
("JaneSample"), so matching compares text without them. A field is focused
by clicking its label, and typing does not clear the field first, because a
remote client cannot be trusted to honour a select-all chord.

## Limits

- One app target today: MockMed. A target is a small class
  (`apps.AppTarget`): start a copy, give the entry URL and the oracle recipe,
  stop it.
- The GUI agent reads the page as text and numbered controls, from the DOM
  or from OCR. It cannot run
  scripts, start sub-agents or use hooks, so the harness surface is
  `AGENTS.md` and `skills/<name>/SKILL.md` only.
- Model requests pass the outbound privacy gate like every other send
  (`openadapt_flow.privacy.prepare_outbound`). Scrubbing patient names out of
  a GUI task makes it unsolvable, because the agent has to match the patient,
  so the measured run on synthetic MockMed data sends under a recorded
  opt-out (`OPENADAPT_FLOW_SEND_UNSCRUBBED`). Real records need a local model.
- The contract is synthetic and uncertified. Receipts are tier 2 and say so.
