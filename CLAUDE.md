# Claude Code Guidelines for OpenAdapt

## Repository role

This public repository is the launcher/meta-package and stable community
entry point for OpenAdapt. It owns `pip install openadapt`, the unified
`openadapt` CLI, release compatibility, and launcher packaging.

The canonical compiler and governed runtime live in
[`OpenAdaptAI/openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).
Engine, replay, verification, repair, policy, and backend changes belong there;
do not implement a second engine in this repository.

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
