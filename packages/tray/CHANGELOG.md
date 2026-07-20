# CHANGELOG


## v0.2.0 (2026-07-20)

### Chores

- Add security CI (CodeQL, gitleaks, dependency-review, Dependabot)
  ([#9](https://github.com/OpenAdaptAI/openadapt-tray/pull/9),
  [`43ef343`](https://github.com/OpenAdaptAI/openadapt-tray/commit/43ef343579311674451dfe81d5864dc55e3d5097))

Adds CodeQL SAST, gitleaks secret scanning, dependency review, Dependabot, and a
  vulnerability-disclosure policy as part of SOC 2 readiness. Additive and non-breaking.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Documentation

- Refresh README release boundary to the shipped 0.1.1 reality
  ([#6](https://github.com/OpenAdaptAI/openadapt-tray/pull/6),
  [`12af131`](https://github.com/OpenAdaptAI/openadapt-tray/commit/12af13199b75de049a792dd495ed5c7178ba3ec5))

Supersedes the original whole-file rewrite: main's README (from #5) already covers the loop
  status/launcher role, so this PR now only fixes the claims that went stale when the hosted
  lifecycle merged and 0.1.1 shipped to PyPI:

- hosted behavior is on main and released, not 'feat/hosted-rewire branch code' - version is 0.1.1
  (pre-alpha classifier), not 0.0.1 - add the PyPI badge and a real pip install quickstart - keep
  the honest boundary: no released openadapt-desktop build provides the companion IPC service yet

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Use canonical 'retained evidence' wording in README
  ([#8](https://github.com/OpenAdaptAI/openadapt-tray/pull/8),
  [`b7a81c5`](https://github.com/OpenAdaptAI/openadapt-tray/commit/b7a81c53086dff01c8b5ebf7c7e31e862101dce4))

Match the AGENTS.md / org-profile / LIMITS.md canonical one-liner ('retained evidence'); the tray
  README said 'recorded evidence'.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Features

- Use OpenAdapt mark, tinted per state, for the tray icon
  ([#16](https://github.com/OpenAdaptAI/openadapt-tray/pull/16),
  [`c2a8a48`](https://github.com/OpenAdaptAI/openadapt-tray/commit/c2a8a48bd34d1fd48bc721895bef9b2096be473d))

Replace the plain solid colored circle with the OpenAdapt brand mark (the chat/robot face from the
  openadapt.ai favicon), color-stylized per recording-lifecycle state so the state stays readable at
  a glance while the brand is finally shown in the menu bar / system tray.

- Add the vector mark (assets/mark.svg) and a high-res transparent raster master
  (assets/mark-master.png), plus a 512px copy packaged inside the wheel
  (src/openadapt_tray/assets/mark-master.png) so the installed app can render the mark. The wheel
  previously shipped no assets, so it always fell back to a generated circle. - IconManager now
  tints the mark's alpha silhouette per state (idle=brand blue, recording=red,
  starting/stopping=amber, compiling=purple, error=red). One tint implementation is shared by the
  runtime and the generator, so committed PNGs and runtime icons never diverge. The needs-attention
  badge is preserved. - Give every TrayState its own icon file. This fixes a latent bug where
  COMPILING referenced a nonexistent compiling.png (fell back to a circle) and starting/stopping
  showed red instead of amber. - Rewrite scripts/generate_icons.py to re-render the master from the
  SVG (rsvg-convert/cairosvg/inkscape) and tint per-state 1x + @2x PNGs and a multi-size logo.ico.
  Remove the stale training.png icons.

Icons verified valid at menu-bar sizes (22px + 44px, light and dark); 137 tests pass, ruff clean.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>


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
