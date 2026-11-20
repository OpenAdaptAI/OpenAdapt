# Validation

Run on 2026-11-03 on a shared macOS arm64 host (Python 3.12.13, openenv 0.11.0,
verifiers 0.3.1, Playwright Chromium). Every run went through the host's
CPU-slot queue. No model was called and nothing was uploaded. Raw results are in
`validation/`.

## MockMed (public sample, 11 tasks: 6 feasible, 5 faulted)

Pack `mockmed/tasks/public.jsonl`,
`sha256:525f5958ee2aca1f6308bb63a9e5e215f6addab37c42a6ba24e7379de025561a`.

### OpenEnv protocol

`openadapt_flow.rl_env.validate`, OpenEnv's `GenericEnvClient` over WebSocket,
each agent on every task:

| Agent | Episodes | Mean reward | CONFIRMED / REFUTED / INDETERMINATE | Silent wrong effects |
|---|---:|---:|---|---:|
| scripted (reference solution) | 11 | 1.00 | 6 / 5 / 0 | 0 |
| screen-trusting (same actions, always claims success) | 11 | 0.55 | 6 / 5 / 0 | 5 |
| lazy failure (claims failure without acting) | 11 | 0.00 | 0 / 11 / 0 | 0 |
| random (3 seeds) | 33 | 0.00 | 0 / 33 / 0 | 21 |

All checks passed:
- the scripted mean reaches the maximum;
- the random mean is at least 0.1 below it (OpenEnv's `floor_margin`);
- the screen-trusting agent earns exactly the feasible share (6/11 = 0.55);
- not trying earns 0;
- no judge errors.

### Prime Intellect path

`environments/prime/validate_prime.py` runs verifiers' `eval` with the
`openadapt-web-envs` taskset (through `run_eval`, so with no upload), verifiers'
OpenEnv driver, and a scripted model stub:

| Agent | Episodes | Errors | Mean reward |
|---|---:|---:|---:|
| scripted | 11 | 0 | 1.00 |
| screen-trusting | 11 | 0 | 0.55 |

### Isolation and parallel sessions

- **No leaks:** after a random-agent episode and a full episode, a reset leaves only the task's seeded row in the store (`tests/e2e/test_rl_env_e2e.py`).
- **8 parallel sessions:** 8 concurrent WebSocket sessions on one server, 2 scripted episodes each (16 episodes). Every one reached reward 1.0, with no errors.
  - Reset: median 3.5 s, p95 3.9 s.
  - Episode: median 9.5 s, p95 14.6 s.
  - Throughput: 46.6 episodes per minute.

  The host's load average was about 40 during the run, so these times are upper bounds.

### OpenEnv tooling

`openenv validate --level static` passes on the staged Space directory. The
runtime level needs a Docker build. That build has not run, because the shared
container VM is leased to other jobs. This openenv release does not yet
implement the semantic oracle and floor graders; `rl_env.validate` above
measures both.

### Tests

`tests/test_rl_env.py` and `tests/e2e/test_rl_env_e2e.py`: 34 passed.

## Dolibarr ERP (public sample, 4 tasks: 3 doable, 1 that cannot be done as asked)

Run on 2026-11-16 on the same host, against Dolibarr 20.0.2 from its pinned
upstream image (started with the Docker 27 CLI against Podman's
Docker-compatible API). Pack `dolibarr_erp/tasks/public.jsonl`,
`sha256:eba928d3a2b9ff23859de5f3fd9987227b8236a20e1b5dc3a904a026397d202b`.
Raw results: `validation/dolibarr_erp.json`. No model was called.

The reward here is whole-record grading (`openadapt_flow.rl_env.record_grader`),
not a verify-run contract, so two expectations read differently: a task that
cannot be done as asked pays an honest failure report with nothing changed
(there is nothing to try), and a question pays only a reply whose `ANSWER:`
line holds the value the record shows.

| Agent | Episodes | Mean reward | CONFIRMED / REFUTED / INDETERMINATE | Silent wrong effects |
|---|---:|---:|---|---:|
| scripted (reference solution) | 4 | 1.00 | 3 / 1 / 0 | 0 |
| screen-trusting (same actions and reply, always claims success) | 4 | 0.75 | 3 / 1 / 0 | 1 |
| lazy failure (claims failure without acting) | 4 | 0.25 | 0 / 4 / 0 | 0 |
| random (2 seeds) | 8 | 0.00 | 0 / 8 / 0 | 8 |

All checks passed: the scripted mean reaches the maximum; random sits at
least 0.1 below it; screen-trusting earns exactly the doable share (3/4);
lazy failure earns exactly the task that cannot be done (1/4) and nothing
else; no judge errors.

## Held-out packs

The MockMed held-out pack (8 tasks) and the MockLoan pack (12 tasks) were
validated the same way. Their packs and results are in the private repository,
not here.

On the first run, one held-out task scored 0 for the scripted agent. It was a
wrong_record case on patient p2, the patient that fault always writes to, so
the record correctly CONFIRMED a write the task called impossible. The pack was
fixed, and the generator now refuses that case.

## Not validated yet

- **OpenEMR:** its task pack and scripted solution have not run against a live instance. This waits on the lane-1 fixture lease.
- **Frappe Lending:** no task pack yet.
- **openIMIS:** its fixture has no per-episode reset.
- **Docker image build and OpenEnv runtime checks:** these wait on the lane-5 lease.
