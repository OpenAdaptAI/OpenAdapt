# CHANGELOG


## v0.1.1 (2026-03-04)

### Bug Fixes

- Replace deprecated macos-13 runner with macos-14
  ([#7](https://github.com/OpenAdaptAI/openadapt-desktop/pull/7),
  [`bac0856`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bac0856958dd2c6c472253f47d0a83be5453f8ba))

* fix: replace deprecated macos-13 runner with macos-14 in build workflow

macos-13 runners have been deprecated by GitHub Actions, causing the Build Python Sidecar job to
  fail on every PR. Both macOS targets now use macos-14 (Apple Silicon), which supports x86_64
  builds via Rosetta.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: flaky concurrent reads test and deprecated macos-13 runner

- test_concurrent_reads: use separate IndexDB connections per thread (WAL concurrent reads require
  separate connections, not a shared one) - build.yml: replace macos-13 with macos-14 (deprecated
  runner)

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Continuous Integration

- Add automated release workflow with python-semantic-release
  ([#6](https://github.com/OpenAdaptAI/openadapt-desktop/pull/6),
  [`1a3c4b8`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1a3c4b8c81c3a6ddad9e0f2ee1aca76c079a1c5e))

Add release.yml workflow triggered on push to main that: - Runs python-semantic-release v9.15.2 to
  determine version bumps from conventional commit messages (feat=minor, fix/perf=patch) - Builds
  with uv and publishes to PyPI (trusted publishing) - Creates GitHub releases with changelogs -
  Uses ADMIN_TOKEN to push through branch protection - Skips semantic-release's own commits to
  prevent infinite loops

Also adds semantic_release config to pyproject.toml with version_toml + version_variables for dual
  version tracking (pyproject.toml + engine/__init__.py).

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.1.0 (2026-03-03)

### Bug Fixes

- Add hatch build config, fix lint errors and workflow ordering
  ([#1](https://github.com/OpenAdaptAI/openadapt-desktop/pull/1),
  [`e899e41`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e899e412cdd0fa4f6999a10e077496b95bab6822))

- Add [tool.hatch.build.targets.wheel] packages = ["engine"] so hatchling can find the Python
  package - Fix ruff import sorting in test_backends.py and test_scrubber.py - Remove unused
  StorageBackend import from test_backends.py - Move Xvfb setup step before test run in CI workflow
  - Add uv.lock for reproducible builds

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Remove premature screenshot generation and Playwright UI tests
  ([#3](https://github.com/OpenAdaptAI/openadapt-desktop/pull/3),
  [`2e6c874`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2e6c874aea15ccbd96a42ba42cf547b06185b218))

Remove: - screenshots/ (fake screenshots of non-functional placeholder HTML) -
  scripts/generate_screenshots.py (generates screenshots of stubs) - tests/test_e2e/test_ui.py
  (Playwright tests against placeholder HTML) - tests/test_e2e/test_screenshots.py (tests for the
  screenshot generator) - pytest-playwright dependency - Xvfb CI step

Keep: - tests/test_e2e/test_ipc.py (tests real IPC protocol code) - All engine tests (test real
  business logic) - README (without screenshot section) - CLAUDE.md

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Add docs sync trigger ([#4](https://github.com/OpenAdaptAI/openadapt-desktop/pull/4),
  [`62d3da2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/62d3da207e7cc2c1b16c2cf0851add5eb3532547))

- Add README, screenshots, e2e tests, and automated screenshot generation
  ([#2](https://github.com/OpenAdaptAI/openadapt-desktop/pull/2),
  [`fa00a22`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/fa00a22d95391dcc1f7ddaf337316e000ab28d22))

* docs: add README and CLAUDE.md

- Comprehensive README with architecture, state machine, storage backends, project structure,
  configuration reference, and development guide - CLAUDE.md with project conventions and file map

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* feat: add screenshots, e2e tests, and automated screenshot generation

- Add Playwright-based UI tests for all HTML pages (dashboard, review, settings) - Add IPC protocol
  e2e tests (response format, error handling, event format) - Add automated screenshot generation
  script with mock data injection - Generate 4 documentation screenshots (idle, recording, review,
  settings) - Add screenshots to README with raw.githubusercontent.com URLs - Add pytest-playwright
  to dev dependencies - Update CI to install Playwright browsers - Update ruff config to allow long
  lines in scripts/ (inline HTML)

52 tests passing (26 engine + 7 IPC + 5 screenshot + 11 UI + 3 viewport)

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Implement end-to-end engine with CLI
  ([#5](https://github.com/OpenAdaptAI/openadapt-desktop/pull/5),
  [`400df35`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/400df3577008edc2b0fd21afe4ed98a8d8e31155))

* feat: implement end-to-end engine with CLI

Replace all NotImplementedError stubs with working implementations across the entire Python engine.
  The full pipeline now works: record -> scrub -> review -> upload.

New files: - engine/db.py: SQLite index database (WAL mode, captures + upload_jobs) - engine/cli.py:
  13-command argparse CLI entry point - engine/__main__.py: python -m engine support -
  tests/test_engine/test_db.py: 11 database tests - tests/test_engine/test_upload.py: 7 upload
  manager tests - tests/test_engine/test_monitor.py: 5 health monitor tests -
  tests/test_engine/test_cli.py: 8 CLI tests - tests/test_e2e/test_pipeline.py: 3 end-to-end
  pipeline tests

Implemented modules: - controller.py: recording lifecycle with openadapt-capture, crash recovery -
  scrubber.py: regex PII scrubbing (email/CC/SSN/phone/IP), Presidio fallback - review.py:
  DB-persisted egress gating with audit logging - storage_manager.py: hot/warm/cold tiers, tar.gz
  archival, cleanup - upload_manager.py: persistent queue, egress checks, multi-backend dispatch -
  monitor.py: memory + disk monitoring with daemon threads - backends/s3.py: boto3
  upload/delete/list/verify - backends/huggingface.py: huggingface_hub upload/delete/list/verify -
  backends/wormhole.py: subprocess-based wormhole send

Test results: 106 passed, 0 skipped (up from 33 passed, 17 skipped)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: use uv run prefix in README Quick Start

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Initial repo scaffold
  ([`b14c32e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b14c32e4fe53e8fd41fa52848236a3b646e2af74))

Tauri 2.x shell + Python sidecar architecture. See DESIGN.md for comprehensive design document.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
