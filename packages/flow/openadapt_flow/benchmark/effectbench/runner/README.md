# EffectBench runner adapter — one interface, every baseline

The **multi-baseline runner adapter** (downstream effort #2 on the
`openadapt_flow.benchmark.effectbench` contract). It drives every agent *arm*
through one common interface against the **identical task** and **identical
independent oracle**, records one `EpisodeRecord` per (task × arm × trial), and
summarizes per arm with `summarize`. A difference between arms is then the
AGENT, never the harness.

Run the CI-fast dry-run (real in-process HTTP, no Docker, no paid API, no spend):

```bash
python -m openadapt_flow.benchmark.effectbench.runner            # all live arms
python -m openadapt_flow.benchmark.effectbench.runner --list-arms
```

## The arm interface

An **arm** is one agent under test. It implements a single method:

```python
class AgentArm(Protocol):
    name: str          # "compiler" / "screen_only" / "claude_cu" / ...
    live: bool         # True = executes here; False = scaffolded external baseline
    def run(self, task: TaskSpec, session: SubstrateSession,
            *, params: Mapping[str, str]) -> ArmResult: ...
```

- The arm is handed **only** the task's natural-language `goal` (intent — *never*
  a step list; `TaskSpec` has no steps field), a `SubstrateSession` to drive, and
  the run `params` (the trial-unique payload it must write).
- It returns an `ArmResult` carrying the **untrusted** `AgentReport`
  (`reported_success` / `halted`) plus recorded `ModelCall`s so `cost_usd` is
  auditable `sum(model_calls)`.
- The harness (`run_episode`) adapts `run` into the
  `run_action: Callable[[], AgentReport]` that `score_episode` calls, and supplies
  the independent oracle — **which the arm can never reach**.

### Fairness / non-gameability (enforced by construction)

- **Same task, same oracle for every arm.** The oracle is built by the
  `env_factory` from `task.oracle` and passed only to `score_episode`, never to
  the arm.
- **No step leakage.** Learning arms get intent + params only.
- **Isolated read path.** The `SubstrateSession` exposes an action channel and a
  screen witness, but not the system-of-record read the oracle uses. The compiler
  arm's own effect check runs through a *different object and transport*
  (`product_effect_verifier`, the app's public API) than the benchmark oracle
  (the in-process record snapshot).
- **Unreadable ≠ scored.** An INDETERMINATE oracle makes `score_episode` raise;
  the harness drops the episode rather than scoring a guess.

## Arms

| arm | status | what it does |
|---|---|---|
| `compiler` (`CompilerArm`) | **live** | record→compile→replay: performs the deterministic compiled action, then GATES success on its own effect verifier. Fail-safe: REFUTED/INDETERMINATE → halt. Never *silently* wrong; even recovers a committed-but-timed-out write. |
| `screen_only` (`ScreenOnlyArm`) | **live** | the ablation: same action, success read from the SCREEN banner with **no** effect check. The arm that exhibits silent wrong-effects. |
| `mock` (`MockArm`) | **live** | deterministic, substrate-free; scripts an `AgentReport` for hermetic CI of the harness/metrics without HTTP. |
| `claude_cu`, `openai_cua`, `ui_tars`, `skyvern` | **scaffolded** | adapters in `baselines.py` — interface + docstring + TODOs. Every `run` raises `ScaffoldNotWired` (no paid call, no spend) until a funded run supplies credentials + a budget cap. |

## The MockMed dry-run (reference result)

`reference_tasks()` turns the bundled MockMed transactional-fault surface into
nine tasks; `MockMedEnvProvider` serves one fault server and resets it per
episode (isolation) while the arms write over real HTTP. The dry-run reproduces
the headline end-to-end:

```
screen_only SWER : 5/9 = 55.6%  (wrong_write 4, phantom 1)   gap 55.6%
compiler   SWER : 0/9 =  0.0%   over-halt 0/9                gap  0.0%
```

The ablation is silently wrong on partial-save, duplicate, optimistic-then-reject,
stale-overwrite, and double-delivered writes; the compiler arm's effect gate
turns each into a flagged wrong-action or a safe halt — and recovers the timeout
the screen mistook for a failure.

## Wiring a funded external baseline

Implement the arm's `_drive` in `baselines.py` (each class documents its loop),
flip `live` to `True` **behind** the `EFFECTBENCH_ALLOW_PAID_BASELINES` opt-in
flag and a per-run + per-suite USD cap, record one `ModelCall` per request, and
register it with the runner. A Dockerized system-of-record (OpenEMR / Frappe from
`benchmark/environments`) supplies its own `env_factory` in place of MockMed.
