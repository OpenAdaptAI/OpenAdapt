# Inert: openadapt-desktop's own GitHub configuration

These files came over with the openadapt-desktop history. GitHub runs only the
workflows at the root of the OpenAdapt monorepo, so nothing here runs.

What runs instead, from the monorepo root `.github/workflows/`:

| Here | Replaced by |
|---|---|
| `test.yml`, `build.yml` | `desktop-test.yml`, `desktop-build.yml`, called from `packages.yml` |
| `release.yml` (semantic-release) | `desktop-release.yml`, which dispatches `release-package.yml` for a merged, unpublished version |
| `native-version-guard.yml`, `native-freshness.yml` | the `desktop release intent` job in `packages.yml`; a release pull request moves the engine and native versions together |
| `dependency-review.yml` | `desktop-dependency-audit.yml` |
| `codeql.yml`, `secret-scan.yml`, `notify-docs.yml` | the monorepo's `codeql.yml`, `secret-scan.yml`, `notify-docs.yml` |
| `release-health.yml`, `release-health.json` | the monorepo's `release-health.yml` and its `desktop-engine` lane |
| `dependabot.yml` | the monorepo's `.github/dependabot.yml` |

Not moved yet: `native-release.yml`, `production-channel.yml` and
`ffmpeg-runtime.yml`. They update published releases in place, which
OpenAdapt's immutable releases do not allow, so they need a redesign first.
Tests under `tests/` still read these files to pin those lanes' contracts.
