# openadapt-web-envs

A `verifiers` v1 taskset over OpenAdapt's record-checked web environments.
Each task is one task of a running OpenAdapt OpenEnv server (see
`../../openenv/README.md`). The rollout is verifiers' own OpenEnv driver:
the model receives the observation and the action schema as JSON, replies with
one JSON action per turn, and the reward is the server's. verify-run reads
the app's record to decide it, and the model's report never does.

```bash
uv pip install -e .
python -m openadapt_flow.rl_env.server ../../openenv/mockmed --port 8000 &
eval openadapt-web-envs --env.taskset.base-url http://127.0.0.1:8000 \
  --env.player.runtime.type subprocess --no-push -m <model> -n 14 -c 4
```

`OPENADAPT_ENV_BASE_URL` can stand in for `--env.taskset.base-url`, and
`--env.taskset.split` picks a split (default `public`). Pass `--no-push` unless
you mean to upload the traces to the Prime Intellect account logged in on this
machine.
