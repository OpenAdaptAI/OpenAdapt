# CHANGELOG


## v0.6.0 (2026-07-27)

### Features

- Add signed human decision task contract
  ([#11](https://github.com/OpenAdaptAI/openadapt-types/pull/11),
  [`b8b3275`](https://github.com/OpenAdaptAI/openadapt-types/commit/b8b32756117c8dcc0c6edd14274b14e0e15c6e62))


## v0.5.0 (2026-07-25)

### Bug Fixes

- Separate runtime geometry from renderer mapping
  ([`9b88122`](https://github.com/OpenAdaptAI/openadapt-types/commit/9b88122c3b9da9229e2b563f3f8494f417efa691))

### Documentation

- Clarify browser target geometry scope
  ([`4e2fa41`](https://github.com/OpenAdaptAI/openadapt-types/commit/4e2fa41fc80f36c4a04f10cf646a353376e49c74))

### Features

- Add exact overlay target tracking contract
  ([`22b9cbb`](https://github.com/OpenAdaptAI/openadapt-types/commit/22b9cbb42540ac63f236fb72d295cdcd375ede29))


## v0.4.0 (2026-07-25)

### Documentation

- Add lifecycle status banner ([#7](https://github.com/OpenAdaptAI/openadapt-types/pull/7),
  [`c8b9109`](https://github.com/OpenAdaptAI/openadapt-types/commit/c8b91092901b1948bcc5671780fd31f13aee830f))

Adds the standard lifecycle banner used across the org (matching the banner wave on
  openadapt-capture, openadapt-grounding, etc.), derived from this repo's classification in the
  repository lifecycle registry (OpenAdaptAI/.github repository-lifecycle.yml, reviewed 2026-07-15):
  Experimental.

Existing README content is unchanged below the banner.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Refresh README to shared OpenAdapt house style
  ([#8](https://github.com/OpenAdaptAI/openadapt-types/pull/8),
  [`5d16b85`](https://github.com/OpenAdaptAI/openadapt-types/commit/5d16b85ab6c8db14c58a6e17252f5e81374a31ca))

Refresh the README to the shared OpenAdapt house style: governed demonstration compiler positioning,
  a consistent "The OpenAdapt stack" section cross-linking the sibling packages, docs.openadapt.ai
  and the main OpenAdapt repo links, first-class substrate framing with honest maturity, and no em
  dashes. Claims verified against the package code.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Add shared control overlay contract
  ([`ca1c85c`](https://github.com/OpenAdaptAI/openadapt-types/commit/ca1c85c34ac7a62c4b9e59e940b15c104cf68420))


## v0.3.1 (2026-07-16)

### Bug Fixes

- Keep release lock metadata consistent
  ([`e3c78f4`](https://github.com/OpenAdaptAI/openadapt-types/commit/e3c78f458e52aec69377c48e4ece7b77e8c88968))

Pin release tooling, verify editable lock metadata, and build the reviewed lock state during
  semantic release.


## v0.3.0 (2026-07-13)

### Features

- Add canonical Benchmark* types (Task/Observation/Action/Agent)
  ([#5](https://github.com/OpenAdaptAI/openadapt-types/pull/5),
  [`82ad485`](https://github.com/OpenAdaptAI/openadapt-types/commit/82ad48590e89e23c44cdf25ce636035195d6ea14))

Move the Benchmark* vocabulary into the canonical schema package so both openadapt-ml and
  openadapt-evals can import it without depending on each other, breaking the historical ml<->evals
  import cycle.

Definitions are dependency-free (dataclasses + abc) and faithfully match the previous
  openadapt-evals definitions.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>


## v0.2.0 (2026-03-29)

### Continuous Integration

- Switch to python-semantic-release for automated versioning
  ([#3](https://github.com/OpenAdaptAI/openadapt-types/pull/3),
  [`d5b3f3f`](https://github.com/OpenAdaptAI/openadapt-types/commit/d5b3f3f7a4d0b6cec42f78523d7bdc046b873478))

Conventional commit PR titles (feat:, fix:, etc.) auto-bump version, tag, publish to PyPI, and
  create GitHub Releases on merge to main.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Add universal action parser for DSL, JSON, and BenchmarkAction formats
  ([#4](https://github.com/OpenAdaptAI/openadapt-types/pull/4),
  [`0c2ac58`](https://github.com/OpenAdaptAI/openadapt-types/commit/0c2ac58d2bf2f9395be27c0c67b4cb844d1db52f))

Adds openadapt_types.parsing module with five public functions: - parse_action(): auto-detect format
  (DSL or JSON) and parse - parse_action_dsl(): parse DSL strings like CLICK(x=0.5, y=0.3) -
  parse_action_json(): parse JSON with canonical, flat, and coordinate formats -
  from_benchmark_action(): convert BenchmarkAction-style dicts to Action -
  to_benchmark_action_dict(): convert Action back to BenchmarkAction dict

Handles Thought:/Action: prefixes, markdown fences, coordinate normalization detection, clamping,
  and legacy field mapping. All edge cases return Action(type=DONE) with a logged warning instead of
  raising.

Includes 66 tests covering DSL, JSON, auto-detect, and BenchmarkAction round-trip conversion.

Co-authored-by: Claude Opus 4.6 (1M context) <noreply@anthropic.com>


## v0.1.0 (2026-03-03)

### Continuous Integration

- Add PyPI publish and test workflows ([#2](https://github.com/OpenAdaptAI/openadapt-types/pull/2),
  [`41afded`](https://github.com/OpenAdaptAI/openadapt-types/commit/41afdedbe7d7e04c00a5b9aa61aa5bddc315c155))

Trusted publisher (OIDC) — no tokens needed. Tag with vX.Y.Z to publish.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Initial schemas — ComputerState, Action, UINode, Episode
  ([#1](https://github.com/OpenAdaptAI/openadapt-types/pull/1),
  [`f9d06a2`](https://github.com/OpenAdaptAI/openadapt-types/commit/f9d06a21480d1db66c4cece6afa6c3049cd1f366))

Canonical Pydantic v2 schemas for computer-use agents, converging three existing schema formats
  (openadapt-ml, openadapt-evals, omnimcp) into one shared package with zero ML dependencies.

Includes: - ComputerState: screen state with UI element graph - UINode: element with role, bbox,
  hierarchy, platform anchors - Action + ActionTarget: typed actions with node_id > description >
  coords - ActionResult: explicit execution outcomes with error taxonomy - Episode + Step: complete
  task trajectories - FailureRecord: classified failures for dataset pipelines - _compat: converters
  from all 3 existing formats - 43 tests passing

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>
