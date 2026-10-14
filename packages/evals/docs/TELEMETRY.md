# Telemetry

OpenAdapt collects anonymous usage telemetry to understand how the tools are used and prioritize development. No screenshots, text content, API keys, or personally identifiable information is ever collected.

## Disabling telemetry

### Option 1: `pyproject.toml` (recommended for teams)

Add to your project's `pyproject.toml`:

```toml
[tool.openadapt]
telemetry = false
```

Commit this to your repo. Every developer who clones the repo has telemetry disabled automatically — no per-user setup, no environment variables, no `.env` files.

This is the recommended approach for enterprises and teams.

### Option 2: `.env` file (single user)

```env
DO_NOT_TRACK=1
```

### Option 3: Environment variable

```bash
export DO_NOT_TRACK=1
```

Or per-command:

```bash
DO_NOT_TRACK=1 python scripts/train_trl_grpo.py --task-dir tasks/ ...
```

### Option 4: `OPENADAPT_TELEMETRY_ENABLED`

```env
OPENADAPT_TELEMETRY_ENABLED=false
```

All four options have the same effect. `DO_NOT_TRACK` follows the [Console Do Not Track](https://consoledonottrack.com/) standard. The `pyproject.toml` approach follows the Python ecosystem convention used by ruff, black, pytest, and mypy.

### Priority order

The first match wins:

1. `DO_NOT_TRACK=1` — always checked first
2. `OPENADAPT_TELEMETRY_ENABLED=false` — explicit override
3. `[tool.openadapt] telemetry = false` in `pyproject.toml` — project-level
4. CI detection — telemetry off by default in CI

## What we collect

When telemetry is enabled, we collect:

- **Event counts**: how many training runs, agent episodes, demos recorded
- **Timing**: how long operations take (not wall-clock timestamps)
- **Package metadata**: package version, Python version, OS (no hostname or IP)
- **Training metrics**: reward mean, loss value, gradient norm (no model weights or training data)

We do **not** collect:

- Screenshots or images
- Text content, prompts, or model outputs
- API keys, passwords, or credentials
- File paths with usernames
- IP addresses or hostnames
- Any data that could identify a person or organization

## How it works

Telemetry events are sent to [PostHog](https://posthog.com/) via a background thread. Events are batched and sent asynchronously — telemetry never blocks your training or evaluation. If the network is unavailable, events are silently dropped.

The telemetry client generates a random anonymous ID stored locally at `~/.openadapt/telemetry_distinct_id`. This ID is not linked to any personal information.

## Automatic scrubbing

Even if telemetry is enabled, a privacy scrubber automatically removes:

- Email addresses and phone numbers (pattern-matched)
- API keys and tokens (key name detection)
- File paths containing usernames (sanitized to `~`)
- Any field named `password`, `token`, `secret`, `api_key`, etc.

## CI environments

Telemetry is automatically disabled in CI environments unless explicitly enabled:

```bash
OPENADAPT_TELEMETRY_IN_CI=true  # opt-in for CI
```

## Source code

- Privacy scrubber: [openadapt-telemetry/privacy.py](https://github.com/OpenAdaptAI/openadapt-telemetry)
- PostHog client: [openadapt-telemetry/posthog.py](https://github.com/OpenAdaptAI/openadapt-telemetry)
- Event definitions: [openadapt-evals/telemetry.py](https://github.com/OpenAdaptAI/openadapt-evals/blob/main/openadapt_evals/telemetry.py)
