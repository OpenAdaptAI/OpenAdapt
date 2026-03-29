# CHANGELOG


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
