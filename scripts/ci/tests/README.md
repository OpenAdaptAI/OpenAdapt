# Shard observer tests

Run these tests on Linux from a repository checkout. They use Flow’s existing locked development dependencies, including pytest and pytest-xdist:

```sh
cd packages/flow
uv sync --locked --extra dev
uv run python -m pytest -c ../../pyproject.toml ../../scripts/ci/tests/test_flow_shard_measurement.py
```

The tests load the sibling observer directly from the checkout. They exercise real small pytest-xdist runs and a synthetic process tree, alongside malformed report, missing sample, process identity and exit-preservation controls. Linux `/proc` is required for the process-tree checks. No lab-specific paths, credentials, external services or Flow application runs are needed.

The observer reports measurements; it does not change test outcomes, subprocess limits or worker policy. Passing these tests does not establish resource contention or validate a whole-shard measurement. The stopped hosted comparison workflow has been retired. Any future comparison must return with hash-locked installs and a fresh review.

Timing validation binds ordinary phases and subtests to the controller session and observed process lifetime. Epoch ordering and duration bounds are checked separately; rounding slack is derived from clock resolution and floating-point precision. It is not a bound on clock skew. Material inconsistency makes the measurement incomplete while preserving pytest’s original exit code. Subtests can nest and workers can run concurrently, so neither is double-counted as serial work.

Pytest's unittest adapter emits `started_at=0`, `finished_at=0` and `seconds=0` for subtests whose timing is unavailable. The observer accepts only this numeric triple-zero subtest sentinel between a passed setup and its same-worker, same-node call report. The parent call must pass the ordinary timing checks. Raw zeros are retained, and `subtests_timing_unavailable` counts these reports; no interval or duration is inferred. Other subtest timings remain subject to containment checks.
