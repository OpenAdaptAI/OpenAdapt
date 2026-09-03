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
- **Repository tree:** a test must never write into the checkout. The session
  hooks in `tests/conftest.py` snapshot the tracked-file status before the
  first test and after the last one, and fail the run when a new entry
  appears, because a regenerated golden can be committed by accident and
  `scripts/check_release_consistency.py` pins a reviewed SHA-256 inventory of
  the public files. Write to `tmp_path` (copy a fixture bundle there first)
  instead. The check reports any tracked file that changed during the run, so
  editing files yourself while a long suite runs also trips it — set
  `OPENADAPT_FLOW_ALLOW_DIRTY_TREE=1` for that case.

### CI execution lanes

Every pull request and push to `main` runs the required safety, unit, browser
E2E, native-platform contract, type, PHI, documentation, interoperability, and
package checks. The complete Python 3.10–3.12 Linux matrix plus macOS suite is
intentionally a second lane: it runs nightly and as an explicit release
qualification, avoiding four redundant full-suite jobs on every routine merge
without reducing the required merge or exact-main gates.

Prepare each release in a reviewed pull request. Update the version in
`pyproject.toml`, `openadapt_flow/__init__.py`, and the editable root entry in
`uv.lock`. Add the matching `CHANGELOG.md` section, then run the package and
claims checks:

```bash
python scripts/check_release_consistency.py
python scripts/validate_claims.py --check --structure-only
uv build --wheel --sdist
python scripts/check_release_consistency.py --require-dist
```

After that pull request merges, start the release from protected `main`:

```bash
gh workflow run release.yml --ref main -f version=<reviewed-version>
```

The release job starts the exact-SHA full CI matrix and the three-OS clean-wheel
lifecycle when either run is missing. It reuses an existing run for the same
commit, so retrying the release doesn't start duplicate matrices. Publication
still requires every job in both runs to pass. If an existing run failed or was
canceled, rerun that workflow on the same candidate before you retry the
release.

Keep `main` unchanged while those runs finish. The release job checks `main`
again immediately before it creates the tag and refuses a candidate that a
later merge replaced. The release App can push that annotated tag, but it can't
push a version commit to `main`.

The tag run rebuilds the package, repeats the source, license, and claims
checks, publishes through PyPI Trusted Publishing, and compares the public
artifact digests with the local build. If a publication step fails, rerun the
same tag run. Don't create a recovery tag.

## Pull request guidelines

- **Conventional Commits** for titles and commits: `feat:`, `fix:`, `perf:`,
  `docs:`, `ci:`, `chore:`, `refactor:`, `test:`. Releases are automated from
  these — `feat:` → minor, `fix:`/`perf:` → patch, `BREAKING CHANGE` → major.
- Keep PRs focused. Separate mechanical changes (formatting, renames) from
  behavior changes so review stays legible.
- Add or update tests for any behavior change. The suite mocks browsers/servers
  where it can, so most of it runs with no live VM.
- Update docs (`README.md`, `DESIGN.md`, `docs/`) when behavior or contracts
  change. Bind capability claims to exact evidence and qualification boundaries.
  Product state comes only from active release admissions. Do not add a static
  lifecycle label for a product target.

## Licensing of your contributions

This repository is MIT-licensed, and your contribution goes in under the MIT
License. Two things cover it.

**Developer Certificate of Origin (required now).** Sign off every commit:

```bash
git commit -s -m "fix: ..."
```

That adds a `Signed-off-by:` line certifying you wrote the change, or that you
have the right to submit it under the project license. The full text is at
[developercertificate.org](https://developercertificate.org/).

**Contributor License Agreement (published, not yet enforced).** The canonical
text is [`CLA.md`](CLA.md) for individuals and [`CCLA.md`](CCLA.md) for
companies whose employees contribute on company time. It gives MLDSAI Inc. an
explicit copyright and patent license, which the MIT License alone doesn't
provide, and it keeps the option of relicensing the combined work later.

You don't agree to the CLA by opening a pull request. Nothing is implied. You
agree when you sign, either through the automated CLA check once that check is
turned on for this repository, or by email. Until the check is on, the DCO
sign-off and the MIT License are what govern your contribution.

OpenAdapt is open-core. MLDSAI Inc. sells proprietary products built on this
code, including a hosted control plane, and your contribution may end up in
them. The MIT License already permits that. The CLA says it out loud so nobody
is surprised.

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
