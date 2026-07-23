# Contributing to openadapt-flow

Thanks for your interest in improving openadapt-flow. This project compiles a
recorded GUI demonstration into a deterministic, self-healing, locally-run
script — so correctness, determinism, and honest measurement matter more here
than raw feature count.

## Development setup

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow && cd openadapt-flow
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
pytest -q
```

Python 3.10–3.12 are supported and exercised in CI.

## The checks CI runs (run them locally first)

```bash
ruff check openadapt_flow          # lint
ruff format --check openadapt_flow # format (drop --check to auto-apply)
mypy                               # type-check (config in pyproject.toml)
pytest -q                          # tests
```

- **Lint/format:** `ruff`. Config lives in `[tool.ruff]` in `pyproject.toml`.
- **Types:** `mypy` runs on the core package (not tests). It is deliberately
  lenient today; a set of modules with known type debt is listed under
  `[[tool.mypy.overrides]]`. Improving a module's annotations and removing it
  from that list is a very welcome PR.
- **Coverage:** CI reports coverage for visibility. There is no hard floor yet,
  but new code should come with tests.

### CI execution lanes

Every pull request and push to `main` runs the required safety, unit, browser
E2E, native-platform contract, type, PHI, documentation, interoperability, and
package checks. The complete Python 3.10–3.12 Linux matrix plus macOS suite is
intentionally a second lane: it runs nightly and as an explicit release
qualification, avoiding four redundant full-suite jobs on every routine merge
without reducing the required merge or exact-main gates.

Before a release, dispatch the full matrix on the exact candidate ref:

```bash
gh workflow run ci.yml --ref <candidate-ref>
```

Wait for all four `test-matrix` jobs and the unchanged required jobs to pass
before publishing. The release workflow independently resolves the exact
candidate SHA and refuses both semantic and recovery publication unless that
dispatched CI run completed successfully with all four matrix jobs present and
green.

## Pull request guidelines

- **Conventional Commits** for titles and commits: `feat:`, `fix:`, `perf:`,
  `docs:`, `ci:`, `chore:`, `refactor:`, `test:`. Releases are automated from
  these — `feat:` → minor, `fix:`/`perf:` → patch, `BREAKING CHANGE` → major.
- Keep PRs focused. Separate mechanical changes (formatting, renames) from
  behavior changes so review stays legible.
- Add or update tests for any behavior change. The suite mocks browsers/servers
  where it can, so most of it runs with no live VM.
- Update docs (`README.md`, `DESIGN.md`, `docs/`) when behavior or contracts
  change. We prefer honest, measured claims — if something is experimental, say
  so.

## Licensing and vendored files

`openadapt-flow` package artifacts are MIT-licensed. Do not copy, adapt, vendor,
embed, or redistribute GPL, AGPL, LGPL, SSPL, source-available, or
field-of-use-restricted material in the wheel or source distribution without
explicit reviewed approval from qualified licensing counsel.

OpenAdapt-specific non-negotiable: do not ship AGPL benchmark files in a PyPI
wheel or sdist. The openIMIS reference environment and any other copied or
adapted AGPL benchmark material must remain repository-only or be obtained
through a pinned, hash-verified, opt-in upstream fetch.

Running or automating an external copyleft application is not the same as
redistributing its source. For reference environments, prefer an opt-in fetch of
the exact pinned, hash-verified upstream project. If repository-only benchmark
material has a different file-local license, preserve its full license,
provenance, modification notice, and source hashes, and exclude the entire
surface from permissively licensed package artifacts.

The release-consistency gate inspects the actual wheel and sdist. A source-tree
notice alone is not sufficient.

## Safety-sensitive areas

The identity gate, the resolution ladder, and the postcondition/halt logic are
the safety core: the whole value proposition is that the tool halts instead of
acting on the wrong target. Changes there deserve extra tests (see the
`test_identity_*`, `test_resolver*`, and `*_fuzz` suites) and a clear
explanation of why the never-false-accept invariant still holds.

## Reporting security issues

See [SECURITY.md](SECURITY.md) — do not file security problems as public issues.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
