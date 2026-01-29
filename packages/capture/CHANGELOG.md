# CHANGELOG


## v0.2.0 (2026-01-29)

### Bug Fixes

- Comment out PyPI badges until package is published
  ([#3](https://github.com/OpenAdaptAI/openadapt-capture/pull/3),
  [`5aedd99`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5aedd99f6329368514cfb6340741241c3f71813a))

The PyPI version and downloads badges show "package not found" since openadapt-capture is not yet
  published to PyPI. Commenting them out until the package is released.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- Move openai-whisper to optional [transcribe] extra
  ([`9dca9e5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9dca9e5e394b015664d982a3581c11801217d50b))

The openai-whisper package requires numba → llvmlite which only supports Python 3.6-3.9, causing
  installation failures on Python 3.12+.

Moving whisper to an optional dependency allows the meta-package (openadapt) to install successfully
  while users who need transcription can explicitly opt-in with `pip install
  openadapt-capture[transcribe]`.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Update author email to richard@openadapt.ai
  ([`1987bee`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1987beeb22eed52d98b67516217d8c486ab7c37d))

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Use filename-based GitHub Actions badge URL
  ([#4](https://github.com/OpenAdaptAI/openadapt-capture/pull/4),
  [`957ca48`](https://github.com/OpenAdaptAI/openadapt-capture/commit/957ca480baf06b1b328b9f9cf65b1a483d948ea2))

The workflow-name-based badge URL was showing "no status" because GitHub requires workflow runs on
  the specified branch. Using the filename-based URL format (actions/workflows/test.yml/badge.svg)
  is more reliable and works regardless of when the workflow last ran.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- **ci**: Remove build_command from semantic-release config
  ([`93cdbb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/93cdbb8ff6f87a6ad96dc74d8092bbad58a34d51))

The python-semantic-release action runs in a Docker container where uv is not available. Let the
  workflow handle building instead.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Chores

- Gitignore turn-off-nightshift test capture
  ([`62b25be`](https://github.com/OpenAdaptAI/openadapt-capture/commit/62b25be430ca2e1e5f69803c3c4db9568fbcf72f))

Test capture data (video, screenshots, database) should not be committed.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Continuous Integration

- Add auto-release workflow
  ([`c3b3eb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c3b3eb806ac060f3d8a98d8b5e048a0f2acfa2b2))

Automatically bumps version and creates tags on PR merge: - feat: minor version bump - fix/perf:
  patch version bump - docs/style/refactor/test/chore/ci/build: patch version bump

Triggers publish.yml which deploys to PyPI.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Switch to python-semantic-release for automated versioning
  ([`b9246a6`](https://github.com/OpenAdaptAI/openadapt-capture/commit/b9246a60de8f83fa7d5ff32749fd0df9d0e22163))

Replaces manual commit parsing with python-semantic-release: - Automatic version bumping based on
  conventional commits - feat: -> minor, fix:/perf: -> patch - Creates GitHub releases automatically
  - Publishes to PyPI on release

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Documentation

- Add CLAUDE.md with development guidelines
  ([#1](https://github.com/OpenAdaptAI/openadapt-capture/pull/1),
  [`5f8fafc`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5f8fafcd77c613b3c74f46546e605f75a7b1c675))

- Add overview of package purpose - Add quick commands for installation, testing, and usage - Add
  architecture overview and key components - Add links to related projects

- Add viewer screenshot to README
  ([`a22c789`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a22c78952cc0e12f2a6cd742a2218a93a55a146d))

Add screenshot of the Capture Viewer HTML interface to improve documentation and show users what the
  viewer looks like.

### Features

- Add browser event capture via Chrome extension
  ([`553bb0a`](https://github.com/OpenAdaptAI/openadapt-capture/commit/553bb0ac783e7e0535b772c3840aeccf74815b20))

- Add BrowserBridge WebSocket server for Chrome extension communication - Add browser_events.py with
  Pydantic models for click, key, scroll events - Add Chrome extension with manifest v3 for DOM
  event capture - Export browser bridge API from __init__.py - Add step navigation controls to HTML
  visualizer - Comprehensive test suite (800+ lines)

Also includes: - docs/whisper-integration-plan.md: Whisper strategy analysis - README improvements
  with ecosystem documentation

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Add faster-whisper backend for 4x faster transcription
  ([`6a8e30e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/6a8e30ec71ed4513bb643bfb58558911d2fe9584))

Add support for faster-whisper as an alternative transcription backend: - New transcribe-fast
  optional dependency in pyproject.toml - Backend auto-detection (tries faster-whisper first, falls
  back to openai-whisper) - New --backend CLI option: auto, faster-whisper, openai-whisper, api -
  Maintain backward compatibility with existing --api flag

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>


## v0.1.0 (2025-12-12)

### Bug Fixes

- Add contents read permission for publish workflow
  ([`eda29a4`](https://github.com/OpenAdaptAI/openadapt-capture/commit/eda29a49124d6db70369db518ae734ff0b994cec))

### Features

- Complete GUI capture with transcription, visualization, processing, and CI/CD
  ([`365dff8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/365dff8689378afce4013a50997b0fe02e650730))

- Initial repo with design doc
  ([`9e34077`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9e34077504e76e7449d185142caa4de2744059b9))
