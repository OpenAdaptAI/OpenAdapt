# CHANGELOG


## v0.1.1 (2026-01-29)

### Bug Fixes

- Update tests badge URL to point to correct workflow
  ([`5b4fbd9`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/5b4fbd9376b5e1a136fc239e41341b6bcbf6fb43))

The badge was pointing to a non-existent workflow path. Updated to use the correct workflow file
  path (test.yml).

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- **ci**: Remove build_command from semantic-release config
  ([`b287c95`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/b287c9508e00a33a6ee8ccbd3fb0eab2524533b2))

The python-semantic-release action runs in a Docker container where uv is not available. Let the
  workflow handle building instead.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Continuous Integration

- Switch to python-semantic-release for automated versioning
  ([`0ca38cb`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/0ca38cb0051b6e6b240df1ee1fcb997dcb971858))

Replaces manual tag-triggered publish with python-semantic-release: - Automatic version bumping
  based on conventional commits - feat: -> minor, fix:/perf: -> patch - Creates GitHub releases
  automatically - Publishes to PyPI on release

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Documentation

- Add CLAUDE.md with development guidelines
  ([#1](https://github.com/OpenAdaptAI/openadapt-privacy/pull/1),
  [`e10082e`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/e10082e30ee7bf52c89168c0fb91bcdd604b3ada))

- Add overview of package purpose (PII/PHI detection and redaction) - Add quick commands for
  installation, testing, and usage - Add supported entity types table - Add links to related
  projects


## v0.1.0 (2025-12-11)

### Continuous Integration

- Add GitHub Actions for tests and PyPI publishing
  ([`e65a504`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/e65a5046edd72f757a6f832db32332f9e9278c52))

### Features

- Initial release with text, image, and dict scrubbing
  ([`f64dab7`](https://github.com/OpenAdaptAI/openadapt-privacy/commit/f64dab756450d6f07c3b360716e3990b63450ff5))
