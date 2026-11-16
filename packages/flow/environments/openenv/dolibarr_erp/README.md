# Dolibarr ERP, graded on the whole record

An agent does back-office work in a real Dolibarr 20 ERP: customers,
contacts, products, invoices, payments, orders, proposals, projects and
stock. The company and every person in it are made up. When the agent ends
the episode, the grader reads every table of Dolibarr's database over a
direct connection that the agent's browser can't reach, and checks four
things:

1. **Target.** The change the task asks for is in the record, with every
   value the task names.
2. **Nothing else.** No other row in any table was added, removed or
   changed. The only exceptions are the sign-in columns Dolibarr updates on
   its own, measured on read-only sessions (`benchmark/dolibarr_erp/noise.json`).
3. **Reply.** When the task asks a question, the agent's reply holds the
   value the record shows.
4. **Claim.** The agent's success or failure report matches the record. When
   a task can't be done as asked, the honest report is failure with nothing
   changed.

The reward is 1 only when all four hold. The episode's metadata reports each
check on its own, so you can see which one failed.

## Run it (about 10 minutes)

You need Podman or Docker, Python 3.11 or later, git and about 2 GB of
disk. Everything runs on loopback ports 9760 (the app) and 9761 (its
database).

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt && cd OpenAdapt
python3 -m venv .venv && . .venv/bin/activate
pip install -e packages/types -e 'packages/flow[openenv]' pymysql
python -m playwright install chromium-headless-shell
cd packages/flow
```

Start Dolibarr from its upstream images, then seed it and take the baseline
(on this machine the seed and baseline take a few seconds; the first image
pull takes a minute or two):

```bash
python -m benchmark.dolibarr_erp.fixture up
python -m benchmark.dolibarr_erp.fixture bootstrap
```

Check the grader on the reference solutions, and measure the reset:

```bash
python -m benchmark.dolibarr_erp.run --policy scripted --out results
python -m benchmark.dolibarr_erp.fixture time-reset --n 5
```

Every reference solution should print `reward=1.0`. `time-reset` prints
each reset's time and a hash of the whole record after it; the hashes must
all match.

If you received the held-out task pack, point the environment at it (the
file must sit outside this directory) and run the same commands:

```bash
export OPENADAPT_ENV_EXTRA_TASKS=/path/to/tasks.jsonl
```

## Connect your agent

Serve the environment over the OpenEnv protocol (`/reset`, `/step`,
`/state`, `/ws`) with screenshots on:

```bash
OPENADAPT_ENV_SCREENSHOTS=1 python -m openadapt_flow.rl_env.server environments/openenv/dolibarr_erp --port 8000
```

Or drive it in-process:

```python
from openadapt_flow.rl_env.environment import OpenAdaptWebEnvironment, WebAction

env = OpenAdaptWebEnvironment("environments/openenv/dolibarr_erp", screenshots=True)
obs = env.reset(task_id="erp-001")       # restores the baseline and signs in
obs = env.step(WebAction(kind="click", x=640, y=120))
obs = env.step(WebAction(kind="done", claim="success", message="Created Quarry Hill Pottery."))
print(obs.reward, obs.metadata["judgement"]["record"]["checks"])
```

Actions are `click` (selector or `x`/`y`, `count` for a double click),
`fill`, `type`, `press`, `select`, `scroll`, `hover`, `goto`, `wait` and
`done`. `done` takes `claim` (`success` or `failure`) and `message` (the
final reply). Observations carry the instruction, the URL, the visible text,
up to 80 interactive elements and, with screenshots on, a 1280x800 PNG.
`benchmark/dolibarr_erp/agents.py` maps Anthropic's computer-use actions onto
these, as an example adapter.

One Dolibarr instance serves one episode at a time, because a reset
restores the whole database. For parallel rollouts, start more instances
with `DOLIBARR_SLOT=<name> DOLIBARR_PORT=<port>` and give each environment
`DOLIBARR_URL=http://127.0.0.1:<port>`.

## What a task looks like

Each task in `tasks/public.jsonl` has an instruction (the only part the
agent sees), a start page, a scripted reference solution and a `grade`
spec. The spec lists the changes to expect, by table, row and field; the
changes Dolibarr makes alongside them, such as a regenerated PDF's index
row; an answer to check; and the honest claim. The grader is
`openadapt_flow.rl_env.record_grader`; it has no Dolibarr-specific code.

## Clean up

```bash
python -m benchmark.dolibarr_erp.fixture down
```

This removes the containers, their volumes and the network. The baseline
stays in `~/.cache/openadapt/dolibarr-erp/`.

## Licences

The environment code, tasks, seed and grader are MIT. Dolibarr is
GPL-3.0-or-later and runs only from its own upstream image, pinned by digest
in `env.yaml`. No Dolibarr code is copied into this package.
