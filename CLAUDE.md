# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is the `openadapt` **meta-package** — a thin coordination layer, not where the real work lives. The base install depends only on `click`; all functionality comes from sibling packages installed as extras (`openadapt-capture`, `openadapt-ml`, `openadapt-evals`, `openadapt-viewer`, `openadapt-grounding`, `openadapt-retrieval`, `openadapt-privacy`). The entire shipped package is four files under `openadapt/`: `cli.py`, `__init__.py`, `config.py`, `version.py`.

Because the code here is small, most changes are either (a) wiring a new CLI command to a sibling package's entry point, or (b) adjusting the lazy-import seams. Both are governed by the rules below.

## Commands

```bash
pip install -e ".[dev]"          # dev setup (pytest + ruff only)
ruff check openadapt/            # lint
ruff format --check openadapt/   # format check (use `ruff format openadapt/` to fix)
pytest tests/ -v                 # run all tests
pytest tests/test_cli_smoke.py::test_every_command_help -v   # single test
```

There are only two test files (`tests/test_import_integrity.py`, `tests/test_cli_smoke.py`) — see below for what they actually check, because it's unusual.

## The lazy-import architecture (read before touching cli.py or __init__.py)

Everything that pulls in a sibling package is imported **lazily, inside function bodies** (in `cli.py` commands) or via `__getattr__` (in `__init__.py`). This is deliberate: importing a sibling can be expensive or have side effects (e.g. `openadapt-capture` takes a screenshot at import time, which crashes in headless/CI environments). Consequences you must respect:

- **`version` and `doctor` must never import sibling packages.** They use `importlib.metadata.version()` and `importlib.util.find_spec()` to inspect installed packages *without executing their code*. Don't "simplify" these to plain imports.
- **Never mask internal import failures.** The `except ImportError` handlers in `cli.py` are scoped to mean "the sibling isn't installed." If a sibling *is* installed but has a broken/renamed symbol, the real error must surface — `test_import_error_messages_not_masked` enforces this. Don't widen a handler to swallow a genuine wiring bug as "not installed."

## The #999 bug class (why the tests look the way they do)

`openadapt serve` and `openadapt train start` were broken for months while CI stayed green, because lazy imports inside command bodies never execute during ordinary import tests, and broad `except ImportError` reported every failure as "not installed." The two test files exist specifically to catch this:

- **`test_import_integrity.py`** walks the AST of both this package *and* the installed sibling packages to detect "phantom" imports (`from openadapt_ml import X` where `X` doesn't exist) and phantom kwargs (calling a sibling function with an argument its signature doesn't accept) — failures that a plain `import` test misses.
- **`test_cli_smoke.py`** renders `--help` for every command in the Click tree, and asserts the **seam contracts** with `openadapt-ml` (e.g. `serve` builds an `argparse.Namespace` whose attributes exactly match what `cmd_serve` reads; `train start` calls `scripts.train.main` with kwargs that exist in its signature).

These cross-package checks **skip** when siblings aren't installed, so they pass trivially in a bare local checkout. **CI installs all siblings explicitly** (see `.github/workflows/main.yml`) so they actually run. When you wire a new command to a sibling, or change how an existing one calls across the seam, update the corresponding contract (e.g. `SERVE_NAMESPACE_ATTRS` in `test_cli_smoke.py`) — a green check that verifies nothing is the original sin here.

## Configuration

`openadapt/config.py` exposes a global `settings` (pydantic `BaseSettings`) read from env vars with the `OPENADAPT_` prefix and `.env`. API keys (`anthropic_api_key`, etc.) are read *without* the prefix for compatibility. Note: `config.py` imports `pydantic_settings`, which is **not** in the base dependencies — only code paths that actually use `settings` need it installed.

## Hardcoded version caveat

`__version__` is hardcoded as `"1.0.0"` in `__init__.py` and `cli.py`'s `version_option`, while the real release version lives in `pyproject.toml` (managed by python-semantic-release). If asked to bump or reconcile versions, be aware these are out of sync by design of the release tooling — `pyproject.toml` is the source of truth for releases.

## Important Rules

### Always Use Pull Requests
**NEVER push directly to `main`.** Create a feature branch and open a PR, even for small changes. Branch protection can be bypassed by admins — don't.

```bash
git checkout -b feature/my-change
git commit -m "Description of change"
git push -u origin feature/my-change
gh pr create --title "Title" --body "Description"
```

### Commit messages drive releases
Releases are automated by python-semantic-release from Conventional Commit prefixes: `feat` → minor bump, `fix`/`perf` → patch. Other allowed tags (`build`, `chore`, `ci`, `docs`, `refactor`, `style`, `test`) don't trigger a release. Tag format: `vX.Y.Z`.

## Key Directories
- `openadapt/` — the shipped package (4 files)
- `tests/` — the two seam/integrity test files
- `docs/` — architecture (`architecture-evolution.md`), CLI, permissions, roadmap
- `legacy/` — archived monolithic codebase (v0.46.0); install via `pip install openadapt==0.46.0`. Not part of the current build.
- `examples/` — runnable end-to-end scripts
- `miso-replay/` — **separate product** (Miso Digital's local record & replay app) built on the legacy engine, not the meta-package. Has its own toolchain (Python engine + Tauri/React UI) and its own `miso-replay/CLAUDE.md` — read that before working in this subtree.

## Related Repositories
- Website: https://github.com/OpenAdaptAI/openadapt-web
- All sub-packages: https://github.com/OpenAdaptAI/openadapt-*
