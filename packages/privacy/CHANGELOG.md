# CHANGELOG


## v1.1.3 (2026-10-18)

### Bug Fixes

- **scan**: Drop site-specific tokens from the forbidden-path list


## v1.1.2 (2026-10-14)

### Bug Fixes

- Correct the fill colour docs and give the scan CLI real help text (openadapt-privacy#23)

- Drop internal wording from the scan output and config docs (openadapt-privacy#23)

- Keep a surname that matches a label abbreviation redacted (openadapt-privacy#23)

- Keep field labels readable and find addresses after any address label (openadapt-privacy#23)

### Chores

- Sync the published source policy (openadapt-privacy#22)

### Documentation

- Lead the README with what the package does and a quick start (openadapt-privacy#23)

- Make the README's card, scan, and setup claims match the code (openadapt-privacy#23)


## v1.1.1 (2026-10-14)

### Bug Fixes

- Fail the PHI scan closed and drop the pip runtime dependency (openadapt-privacy#21)

- Fail the PHI scan when the root is missing, empty, or unreadable (openadapt-privacy#21)

- Keep the scan's fail-closed checks precise across Python versions (openadapt-privacy#21)

- Stop requiring pip at install time (openadapt-privacy#21)


## v1.1.0 (2026-09-01)

### Features

- Add stdlib git/CI PHI scanner (openadapt-privacy#20)


## v1.0.4 (2026-08-28)

### Bug Fixes

- Decide separated action text by shape, and label identifiers by recognizer (openadapt-privacy#19)

### Chores

- Ignore every .env variant, not one at a time (openadapt-privacy#16)

- **policy**: Sync generated source boundary (openadapt-privacy#17)

### Documentation

- Rewrite the README, and replace the examples with measured output (openadapt-privacy#18)


## v1.0.3 (2026-08-19)

### Bug Fixes

- **ci**: Publish with an action that accepts Metadata-Version 2.5 (openadapt-privacy#14)


## v1.0.2 (2026-07-29)

### Bug Fixes

- Fail closed when privacy dependencies fail

### Chores

- Scrub nested privacy structures recursively (openadapt-privacy#12)

- **release**: Enforce source policy on archives (openadapt-privacy#11)


## v1.0.1 (2026-07-28)

### Bug Fixes

- Stop reporting a failed scrub as a successful one (openadapt-privacy#10)

### Chores

- Add MIT LICENSE file (openadapt-privacy#7)

- Declare privacy release line

- Gitignore .private/ (openadapt-privacy#9)

### Documentation

- Refresh README to shared OpenAdapt house style (openadapt-privacy#8)


## v1.0.0 (2026-07-15)

### Bug Fixes

- Remove vulnerable transformer dependency (openadapt-privacy#4)

- Remove vulnerable transformer dependency from Presidio scrubber (openadapt-privacy#4)

- **ci**: Release through protected main (openadapt-privacy#5)

- **ci**: Use v9 branch config for python-semantic-release (openadapt-privacy#3)

### Continuous Integration

- Pin privacy test and release toolchain (openadapt-privacy#4)

### Testing

- Support Python 3.10 dependency checks (openadapt-privacy#4)



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
