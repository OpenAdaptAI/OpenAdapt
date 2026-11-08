# Shard observer tests

Run these tests on Linux from a repository checkout. They use Flow’s existing locked development dependencies, including pytest and pytest-xdist:

```sh
cd packages/flow
uv sync --locked --extra dev
uv run python -m pytest -c ../../pyproject.toml ../../scripts/ci/tests/test_flow_shard_measurement.py
```

The tests load the sibling observer directly from the checkout. They exercise real small pytest-xdist runs and a synthetic process tree, alongside malformed report, missing sample, process identity and exit-preservation controls. Linux `/proc` is required for the process-tree checks. No lab-specific paths, credentials, external services or Flow application runs are needed.

The observer reports measurements; it does not change test outcomes, subprocess limits or worker policy. Passing these tests does not establish resource contention or validate a whole-shard measurement. The proposed hosted comparison requires separate approval.
