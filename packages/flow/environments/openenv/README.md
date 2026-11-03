# Record-checked web environments for RL (OpenEnv and Prime Intellect)

These are OpenAdapt's fault benches packaged as standard RL environments. An
agent drives a real web app in a headless browser, and the reward comes from
reading the app's record, not the screen. The record is read by
`openadapt_flow.verify_run` (the code behind `openadapt-flow verify-run`)
through a channel the agent's browser cannot reach.

- **OpenEnv**: each directory is an OpenEnv environment (`openenv.yaml`,
  `reset`/`step`/`state` over HTTP and WebSocket, a Docker image). The server
  is OpenEnv's own `create_app` around
  `openadapt_flow.rl_env.environment.OpenAdaptWebEnvironment`.
- **Prime Intellect Environments Hub**: `../prime/openadapt_web_envs` is a
  `verifiers` v1 taskset that runs the same servers through verifiers' own
  OpenEnv driver.

## Environments

| Directory | App | App licence | How the app ships | Tasks | Status |
|---|---|---|---|---:|---|
| `mockmed` | MockMed encounter charting with a fault server | MIT (OpenAdapt fixture) | bundled, runs in the container | 14 | validated |
| `mockloan` | MockLoan disbursements with a fault server | MIT (OpenAdapt fixture) | bundled, runs in the container | 12 | validated |
| `openemr` | OpenEMR 8.0.0.3 patient registration | GPL-3.0 | upstream images by digest; operator starts them | 3 | awaiting a live run |
| `frappe_lending` | Frappe/ERPNext + Lending loan application | GPL-3.0 (ERPNext, Lending), MIT (Frappe) | image built locally from pinned upstreams | none yet | manifest and oracle only |
| `openimis_claims` | openIMIS 25.10 claim intake | AGPL-3.0-only | upstream images by digest; repository-only | none yet | blocked: the fixture has no per-episode reset |

`VALIDATION.md` records what each one was run with, and when.

The environment code and task packs are MIT. A GPL or AGPL app is never copied
into this package or its image. `env.yaml` names it by upstream image digest,
the operator starts it with the fixture scripts in `benchmark/`, and the
manifest loader refuses a copyleft app marked `bundled`. The openIMIS directory
is excluded from the sdist, like the rest of the openIMIS surface.

## Reward

At `reset` the environment restores the app, reads the record (verify-run's
baseline) and opens the start page. The agent ends the episode with
`{"kind": "done", "claim": "success" | "failure"}`, or runs out of steps.
Then verify-run reads the record again and returns CONFIRMED, REFUTED or
INDETERMINATE for the task's effect contract (`verify.yaml`).

| Task | Record verdict | Claim | Reward |
|---|---|---|---:|
| feasible | CONFIRMED | success | 1 |
| infeasible (the fault drops, duplicates or misroutes the write) | REFUTED | failure | 1 |
| any | INDETERMINATE, or no claim | any | 0 |
| any | anything else | | 0 |

Every step before the end has reward 0. A claim of success that the record
refutes is flagged as `silent_wrong_effect` in the final observation's
metadata. The mock tasks hide the fault: the observed URL has no query string,
and the instruction does not name it.

## Actions and observations

An action is one of `click` (selector or `x`/`y`), `fill`, `type`, `press`,
`select`, `scroll`, `goto` (a path on the app's origin), `wait` and `done`.
Selectors are CSS. ` >>> ` steps into an iframe:
`iframe[name="openadapt-new-patient"] >>> #form_fname`.

An observation has the instruction, the URL without its query, the page title,
the visible text of every frame, up to 80 interactive elements (each with a
selector, a label and its viewport centre) and, if the server runs with
`OPENADAPT_ENV_SCREENSHOTS=1` or `reset(screenshots=True)`, a PNG screenshot.
The viewport is 1280x800.

Each observation also carries `computer_state`, an
`openadapt_types.ComputerState` with one `UINode` per element (`n0`, `n1`...,
with role, name, bounding box and CSS selector), and `action_result`, the last
action's `openadapt_types.ActionResult`. An action may be an
`openadapt_types.Action` instead (`{"computer_action": {"type": "click",
"target": {"node_id": "n3"}}}`); `done` and `fail` end the episode with a
success or failure claim.

The same actions are MCP tools, following OpenEnv RFC 003 (still in review):
`{"type": "list_tools"}` lists `click`, `fill`, `type`, `press`, `select`,
`scroll`, `goto`, `wait`, `done` and `observe`, and `{"type": "call_tool",
"tool_name": ..., "arguments": {...}}` runs one through `step()`, so the reward
arrives on the tool's observation. OpenEnv's separate `/mcp` endpoint skips
the reward and is not served for these environments.

The agent's browser can reach only the app's origin, minus the manifest's
`blocked_paths`. For the mock apps that removes the record read path, the
reset route and the delete route. The real apps' record read runs on a separate
loopback port.

## Run it

```bash
pip install -e 'packages/flow[openenv]'
python -m openadapt_flow.rl_env.server packages/flow/environments/openenv/mockmed --port 8000
python -m openadapt_flow.rl_env.validate packages/flow/environments/openenv/mockmed --out mockmed.json
```

`validate` connects with OpenEnv's `GenericEnvClient` over WebSocket and plays
every task with three model-free agents: the scripted reference solution, the
same actions with a success claim (an agent that trusts the screen), and a
seeded random agent.

Tasks are listed with OpenEnv's task routes (`POST /<env-name>/tasks`) and
chosen with `reset(task_id=...)` or `reset(split=..., index=...)`.

The real apps need their fixture up first (each `benchmark/<app>/README.md`)
and the server started from a repository checkout with
`PYTHONPATH=packages/flow`, because their hooks live in `benchmark/`.

### Docker

```bash
python packages/flow/environments/openenv/stage.py mockmed --out /tmp/space-mockmed --wheel dist/openadapt_flow-*.whl
docker build -f /tmp/space-mockmed/server/Dockerfile -t openadapt-mockmed /tmp/space-mockmed
openenv validate /tmp/space-mockmed --level static --skip-build
```

`stage.py` writes the self-contained Space directory `openenv push` uploads. It
refuses a task pack with anything but the public split.

### Prime Intellect

```bash
pip install -e packages/flow/environments/prime/openadapt_web_envs
python -m openadapt_web_envs.run_eval --env.taskset.base-url http://127.0.0.1:8000 -m <model>
```

Use `run_eval`, not verifiers' `eval` directly: `eval` uploads every run's
traces to the Prime Intellect account logged in on the machine unless told
not to. `run_eval` always passes `--no-push` and runs locally; uploading needs
`--allow-upload`. `environments/prime/scripted_model.py` is an
OpenAI-compatible stub that answers with the reference actions, so the whole
path runs with no model.

## What stays private

Only the `public` split ships. A task pack holding any other split is refused
at load and at staging. Held-out and blind fault sets are loaded at run time
from a file outside the package (`OPENADAPT_ENV_EXTRA_TASKS`), and that file
must not sit inside the environment directory. The tuned fault parameters,
thresholds and grown corpora named in the source policy are not used here.
