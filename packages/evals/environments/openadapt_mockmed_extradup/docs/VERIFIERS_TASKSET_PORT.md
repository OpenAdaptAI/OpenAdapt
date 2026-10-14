# Porting to the verifiers v1 Taskset API: options brief

Evidence read on 2026-09-01: verifiers 0.3.1 from PyPI (released 2026-08-24), the 0.3.2.dev37 wheel (uploaded 2026-09-01), verifiers `main` at 16deb7b, prime CLI 0.6.31 (released 2026-08-31), and verifiers issue #1982 (open, updated 2026-08-30). Recommendation: option C, with the pin from option A applied today.

## What the environment is pinned to today

`openadapt_mockmed_extradup.py` imports `verifiers as vf` and builds a `vf.SingleTurnEnv` with a `vf.Rubric` of five async functions and a `vf.Parser`. Its entry point is a module-level `load_environment(**kwargs)`; `vf-eval` imports the module named after the env id and calls it. The package pyproject says `verifiers>=0.3.1`; the `prime-env` CI job installs `verifiers==0.3.1`.

In 0.3.1 that surface lives under `verifiers.legacy`; an alias finder keeps `vf.SingleTurnEnv` resolving. On 2026-08-31, PR #2480 removed `verifiers/legacy/`, the alias finder, the `vf-*` console scripts, and `verifiers/cli` from `main`. The 0.3.2.dev37 wheel carries zero files under `verifiers/legacy/`. The next stable release after 0.3.1 will not import this environment.

The hub still expects v0. `prime env push` (0.6.31) reads only the pyproject: name, version, description, tags, license, dependencies, `requires-python`. The hub's quality scan then calls `vf.load_environment(pkg)` and checks `isinstance(env, vf.SingleTurnEnv)` (issue #1982, filed 2026-07-13; a maintainer replied on 2026-07-20 that v1 support was coming; still open). The `prime-environments` README (pushed 2026-08-04) still documents `load_environment` and `vf-eval`.

## What v1 changes that touches this environment

| Today (0.3.1, v0) | v1 (`verifiers.v1`) |
| --- | --- |
| `load_environment(**kwargs) -> vf.Environment` | One `vf.Taskset` subclass exported in `__all__`; no loader function |
| `**kwargs` (`envs`, `num_tasks`, `seed`, `include_hacking_cases`, `score_from_screen`) | Fields on a `vf.TasksetConfig` subclass, set with `--env.taskset.<field>` |
| `Dataset` rows: `prompt`, `answer`, `info` (JSON string), `task` | A frozen `vf.TaskData` subclass with typed fields (`spec`, `case`, `scripted_completion`) |
| `dataset` and `eval_dataset` | One `load()`; a config flag decides whether the six labeled rows load |
| `vf.Rubric(funcs, weights, parser)` | `@vf.reward(weight=1.0)` and `@vf.metric` methods on a `vf.Task` subclass |
| `parser.parse_answer(completion)` | `trace.last_reply` |
| `state["certification"]` | `trace.info["certification"]` |
| Single turn by class | `@vf.stop` returning `trace.num_turns >= 1` |
| Reward raises: rollout errors | Reward `None`, `episode.ok` false; the episode is dropped |
| `vf-eval <id> -m -b -k -n -r -a '{...}'` | `eval <id> --model --client.base-url --client.api-key-var --num-tasks --num-rollouts --env.taskset.<field>` |
| Local by default | Runtime defaults to a Prime VM; `--env.agent.runtime.type subprocess`, `--env.agent.harness.id null`, and `--no-push` keep the run local and offline |
| `metadata.json` with `avg_reward` | `traces.jsonl`; `check_fails_closed.py` must read `rewards["certified_reward"].score` per episode |

`certify()`, `self_test()`, `certify_corpus()`, and `scripted_completion()` import nothing from verifiers and do not change in any port.

## Options

**A. Stay on 0.3.x `SingleTurnEnv` and pin.** Change `verifiers>=0.3.1` to `verifiers>=0.3.1,<0.3.2` in the pyproject and keep `==0.3.1` in CI. One hour. No risk to the fail-closed properties: no code moves. The `prime-env` job and the `openadapt-evals>=0.95.1` pin are unchanged. The package passes the hub scan as it runs today. Cost: a trainer who installs a newer verifiers gets an import error instead of a reward.

**B. Port to Taskset now, before the first hub push.** Rewrite the glue (about 300 lines), the pytest file, `check_fails_closed.py`, the CI job, and the README quickstart. Six to ten hours. Moderate risk during the port, all of it in glue: `score_from_screen=True` must raise from a `TasksetConfig` validator so the refusal stays visible at the config surface; the labeled rows must keep their case labels in `TaskData`; the metrics must stay unweighted. `certify_corpus()` and its Clopper-Pearson bound are untouched. The CI job needs the four local flags above and the `null` harness. The `openadapt-evals` pin is unchanged. Blocker: a v1-only package fails today's hub scan (#1982), so the first push lists with a failed scan or not at all.

**C. Publish on the v0 API as is, port in a later minor after the listing exists.** Do A today and push. Do B when verifiers ships a stable release with no `verifiers.legacy` or #1982 closes with the scan accepting v1, whichever comes first; that release then pins `verifiers>=<that release>`. Work, risk, and CI effect are A's now and B's later. The `openadapt-evals` pin is unchanged in both steps.

## Recommendation

Option C. The fail-closed properties live in `certify()` and never depended on the verifiers API, so waiting costs nothing in safety, while the hub scan that runs today accepts only v0. Porting against a fixed release beats porting against a `main` that changed its package layout twice in three weeks (PR #2303 on 2026-08-10, PR #2480 on 2026-08-31).

Nothing in any port may change: the reward is 1.0 only on a tier-2 `VERIFIED` read and 0.0 otherwise; an unscored episode is dropped and never paid 0.0; no tier-0 or tier-1 path exists in the code; "certified" appears next to "synthetic" in every README sentence that uses it.

## Verification list for the porter

1. `python openadapt_mockmed_extradup.py` prints 1.0 for both `control` entries, 0.0 for the fourteen hacking entries, then 700 trials, 0 false accepts, 100 gold, 0 false rejects, `upper_bound_95` 0.0043.
2. `python -m pytest tests/test_prime_env_mockmed_extradup.py -q` passes, with `test_load_environment_refuses_to_score_from_the_screen` rewritten for the config surface.
3. `python check_fails_closed.py --num-examples 2` exits 0 and prints `ok` for all eight cases.
4. `eval openadapt-mockmed-extradup --model scripted/dup ...` against `scripted_policy.py serve` writes a `traces.jsonl` in which every episode has `certified_reward` 0.0 and `inadmissible_evidence_offered` 0.0; the `screen_only` run has `inadmissible_evidence_offered` 1.0.
5. `uv pip install --dry-run openadapt-mockmed-extradup` resolves against the pinned verifiers on Python 3.11 and 3.12.
