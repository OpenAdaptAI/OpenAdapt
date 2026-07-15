# CHANGELOG


## v0.1.1 (2026-07-15)

### Bug Fixes

- Recover tray 0.1.1 PyPI release
  ([`53776db`](https://github.com/OpenAdaptAI/openadapt-tray/commit/53776dbcea6f225005cfb3d739906171fcaebdb1))

Replace the undefined release lock variable with an offline exact-root synchronizer and preserve
  v0.1.0 as the failed-attempt audit record.


## v0.1.0 (2026-07-15)

### Bug Fixes

- **ci**: Use v9 branch config for python-semantic-release
  ([#4](https://github.com/OpenAdaptAI/openadapt-tray/pull/4),
  [`265b7c9`](https://github.com/OpenAdaptAI/openadapt-tray/commit/265b7c992e8ed36d4d73c98d3f92d07a665c1f51))

Replace `branch = "main"` (v7/v8 key) with `[tool.semantic_release.branches.main]` table (v9 key).
  The old key is silently ignored by v9, causing releases to never trigger on the main branch.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Align tray with the hosted workflow lifecycle
  ([`3b5ea95`](https://github.com/OpenAdaptAI/openadapt-tray/commit/3b5ea9543d6438f5d4fdbe2627c1ec8b886dafb3))

Reposition the Experimental tray around record, compile, replay, and governed hosted repair, with
  protected-main and pre-1.0 release safeguards.


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
