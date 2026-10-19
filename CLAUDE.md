# Claude Code Guidelines for OpenAdapt

## Repository role

This public repository is the launcher/meta-package and stable community
entry point for OpenAdapt. It owns `pip install openadapt`, the unified
`openadapt` CLI, release compatibility, and launcher packaging.

The compiler and governed runtime (openadapt-flow) live in this monorepo
under `packages/flow`, next to `packages/agent`, `packages/types` and
`packages/capture`. The standalone openadapt-flow, openadapt-agent and
openadapt-types repositories are frozen; changes go here.
[CAPABILITIES.md](CAPABILITIES.md) is the map of what exists and how proven
it is; update it in the same pull request as any capability change, and list
any new `openadapt-flow` command under "Every command, by capability" (a test
enforces it). Refresh the review brief with `python scripts/build_llms_full.py` when you
change README.md, CAPABILITIES.md, SURFACES.md, LIMITS.md or the flow
CHANGELOG, and always before a release.

The current product compiles demonstrated GUI workflows into deterministic,
locally executable programs. Healthy runs make no model calls. Training,
retrieval, and general computer-use agents are separate research surfaces.

## Before changing the repository

1. Read the workspace `AGENTS.md` and current `STATUS.md` when available.
2. Fetch `origin` and base findings and changes on fresh `origin/main`.
3. Read [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
4. Keep public installation centered on `pip install openadapt` and
   `openadapt ...`; `openadapt-flow` remains the contributor/engine package.
5. Preserve local-first use, fail-closed execution, and the open-core boundary.

## Repository layout

- `openadapt/`: launcher package and unified CLI
- `packages/`: the product packages, each folded in with its history
  (`flow`, `agent`, `types`, `capture`, `privacy`, `desktop`); see
  `docs/monorepo/`
- `tests/`: launcher, integration-seam, and release-artifact tests
- `legacy/`: frozen pre-1.0 monolith
- `docs/`: noncanonical historical repository documentation;
  docs.openadapt.ai is maintained in `OpenAdaptAI/openadapt-ops`
- `.github/`: CI, security, dependency, and release workflows

## Development and delivery

```bash
python -m pip install -e '.[dev]'
pytest
ruff check openadapt tests
```

Use a focused branch and pull request for every change. Do not push directly to
`main`. Releases are performed only through the reviewed GitHub Actions release
workflow after explicit authorization; never publish from a development task.
