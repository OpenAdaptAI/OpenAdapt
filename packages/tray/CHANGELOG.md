# CHANGELOG


## v0.0.1 (2026-01-29)

### Bug Fixes

- Add README badges for license and Python version
  ([#1](https://github.com/OpenAdaptAI/openadapt-tray/pull/1),
  [`d7e723f`](https://github.com/OpenAdaptAI/openadapt-tray/commit/d7e723f4df1ea6080629c40970560a330b2b3eec))

Add standard badges for license and Python version. PyPI badges are commented out until the package
  is published.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- **ci**: Remove build_command from semantic-release config
  ([`d2cc03f`](https://github.com/OpenAdaptAI/openadapt-tray/commit/d2cc03fb8e9fda710f04dfd30ee65416653ab3cb))

The python-semantic-release action runs in a Docker container where uv is not available. Let the
  workflow handle building instead.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Continuous Integration

- Add PyPI publish and auto-release workflows
  ([`992fa64`](https://github.com/OpenAdaptAI/openadapt-tray/commit/992fa640903fdee6dc9a2a035e9196a6a5be9d56))

- publish.yml: Triggered on tags, publishes to PyPI - release.yml: Auto-bumps version on PR merge,
  creates tags

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Switch to python-semantic-release for automated versioning
  ([`3975bfc`](https://github.com/OpenAdaptAI/openadapt-tray/commit/3975bfcb6fa6de6009d6ddf37c64e99e83e53d0e))

Replaces manual commit parsing with python-semantic-release: - Automatic version bumping based on
  conventional commits - feat: -> minor, fix:/perf: -> patch - Creates GitHub releases automatically
  - Publishes to PyPI on release

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
