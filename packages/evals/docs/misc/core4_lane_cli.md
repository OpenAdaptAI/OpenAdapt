# Core4 Lane CLI

`scripts/core4_lane.py` wraps the recurring 4-task WAA eval lane with deterministic arguments.

It supports:

- `pack`: generate a reusable shell script ("resume pack") with fully resolved commands.
- `run`: execute trial loops directly without manually pasting long command chains.

## Defaults

- tasks: `04d9aeaf,0bf05a7d,0e763496,70745df8`
- demo dir: `annotated_demos_core4`
- output root: `benchmark_results`
- lane name: `repeat_core4`
- agent: `api-openai`
- max steps: `15`

## Examples

Generate a deterministic command pack:

```bash
uv run python scripts/core4_lane.py pack --trials 3 --run-stamp 20260305_1534
```

Dry-run commands that would execute:

```bash
uv run python scripts/core4_lane.py run --trials 3 --dry-run
```

Run three trials now:

```bash
uv run python scripts/core4_lane.py run --trials 3 --fail-fast
```

Run with clean desktop parity flags:

```bash
uv run python scripts/core4_lane.py run \
  --trials 2 \
  --clean-desktop \
  --force-tray-icons \
  --waa-image-version win11-24h2-2026-03-04
```

