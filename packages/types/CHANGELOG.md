# CHANGELOG


## v0.10.1 (2026-08-19)

### Bug Fixes

- **ci**: Publish with an action that accepts Metadata-Version 2.5
  ([#26](https://github.com/OpenAdaptAI/openadapt-types/pull/26),
  [`bdaca39`](https://github.com/OpenAdaptAI/openadapt-types/commit/bdaca39dd8eb9ddec1fb326b26375b000a8e44ce))

pypa/gh-action-pypi-publish v1.14.0 bundles twine 6.1.0 and packaging 25.0, which reject
  Metadata-Version 2.5 -- the version current hatchling emits. The publish step fails with:

InvalidDistribution: Invalid distribution metadata:

'2.5' is not a valid metadata version

This repo builds with unpinned hatchling via the semantic-release build_command, so it emits 2.5 and
  is exposed to exactly this failure.

This is not hypothetical: the same pin broke the openadapt-evals 0.91.0 release. The tag, the
  version-bump commit and the GitHub release all landed, then "Publish to PyPI" failed, leaving PyPI
  stale while every other release artifact said the version had shipped.

Move to v1.14.2 (twine 7.0.0 + packaging 26.2), the pin openadapt-flow and openadapt-capture already
  use and the one applied to openadapt-evals in OpenAdaptAI/openadapt-evals#291.

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


## v0.10.0 (2026-08-08)

### Documentation

- Align entry commands and substrate maturity across repo READMEs
  ([#24](https://github.com/OpenAdaptAI/openadapt-types/pull/24),
  [`097d11d`](https://github.com/OpenAdaptAI/openadapt-types/commit/097d11d8a5bc4e66417c65f3147000efe61079e4))

Adopt the canonical first-run path (pip install 'openadapt[browser]' + openadapt quickstart), keep
  engine-direct commands as explicit variants of the same loop, use the shared substrate-maturity
  table verbatim, name the tutorial fixture MockMed (a synthetic practice-management fixture) on
  first mention, and show the cmd.exe double-quoted install form.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Features

- Add portable business decision contracts
  ([#25](https://github.com/OpenAdaptAI/openadapt-types/pull/25),
  [`826cbb3`](https://github.com/OpenAdaptAI/openadapt-types/commit/826cbb3ff80621e6391e9e5419f02d71943ab099))

* feat: add portable business decision contracts

* docs: clarify decision presentation trust

* fix: bind reviewed business decision presentation


## v0.9.0 (2026-07-30)

### Features

- Add Execute reference clients
  ([`f0c08c4`](https://github.com/OpenAdaptAI/openadapt-types/commit/f0c08c4cf3610fdf8216f2fc86735ef32de4ba00))

Add the public Execute OpenAPI client, secure reference clients, and integration examples.


## v0.8.0 (2026-07-29)

### Features

- Add qualified entity decision task v2
  ([#22](https://github.com/OpenAdaptAI/openadapt-types/pull/22),
  [`d6e6e11`](https://github.com/OpenAdaptAI/openadapt-types/commit/d6e6e1110ba4f830a37976e819bad0cf824f55de))

Add a versioned signed task contract for qualification-approved entity labels. Preserve V1 bytes,
  bind the qualification project and exact step, keep a neutral fallback, and use a strict
  cross-language timestamp and action contract.


## v0.7.0 (2026-07-29)

### Chores

- **release**: Enforce source policy on archives
  ([#17](https://github.com/OpenAdaptAI/openadapt-types/pull/17),
  [`08059cd`](https://github.com/OpenAdaptAI/openadapt-types/commit/08059cdb8fc1401188418f68dddc6678e0c73858))

### Features

- Add runner capability contract
  ([`955d3b2`](https://github.com/OpenAdaptAI/openadapt-types/commit/955d3b2eb3069593b86a630a35ab2a786f851e9a))

- Coordinate Types 0.7 contracts
  ([`5b71bb2`](https://github.com/OpenAdaptAI/openadapt-types/commit/5b71bb232039fbd2d155abb09831330395580b38))

Add the portable attended-reconciliation and Execute v1 contracts. Regenerate the public schemas and
  keep the source boundary intact.


## v0.6.4 (2026-07-28)

### Bug Fixes

- Report an unparseable action as FAIL, not as a completed task
  ([#16](https://github.com/OpenAdaptAI/openadapt-types/pull/16),
  [`e3bdcea`](https://github.com/OpenAdaptAI/openadapt-types/commit/e3bdcea95779159c10a9a1eaaf157787e67cb5d5))

`ActionType.DONE` is a successful terminal outcome: a runner that sees it ends the episode as
  complete. Every parse and conversion failure in this package produced exactly that value, so "the
  model said it finished" and "we could not read the model's output at all" were the same Action.

- `openadapt_types/parsing.py`: the `_done()` helper behind all 19 failure paths now returns
  `Action(type=ActionType.FAIL)` carrying the reason in `reasoning` and in `raw[PARSE_ERROR_KEY]`.
  This also repairs `parse_action`, which used `type != DONE` as its own success test and so
  re-parsed a legitimate `{"type": "done"}` as DSL before returning it.

- `openadapt_types/_compat.py`: `from_benchmark_action`, `from_ml_action` and
  `from_omnimcp_action_decision` defaulted a missing type to `"done"` and mapped every unrecognized
  type to `DONE` with no log at all. An unconvertible record became a successful terminal step in
  converted training data. They now return FAIL with `raw[UNCONVERTIBLE_ACTION_KEY]` and the source
  dict.

- `openadapt_types/computer_state.py`: `get_children` returned `[]` both for a node with no children
  and for a node_id that is not in the state. An unknown node now raises KeyError.

- `openadapt_types/__init__.py`: `__version__` was the literal "0.1.0" against a package on 0.6.3.
  It is now read from installed distribution metadata.

Existing tests that asserted the DONE-on-failure behaviour are updated to assert FAIL; they
  enshrined the defect. `tests/test_no_false_success.py` adds the direct regressions, and every
  production change was reverted individually to confirm the matching test fails against pre-fix
  code.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


## v0.6.3 (2026-07-28)

### Bug Fixes

- **human-decision**: Give operator disagreement a closed wire channel
  ([#15](https://github.com/OpenAdaptAI/openadapt-types/pull/15),
  [`995d4d0`](https://github.com/OpenAdaptAI/openadapt-types/commit/995d4d0de3eddf3825aca68733a28253204fe2aa))

The attended vocabulary was closed at continue/skip/teach/escalate. An operator who looked at the
  live application and concluded the engine was right to stop had no way to say so: escalate PARKS
  the run for a colleague and reads as "I don't know", and continue asserts a repair that did not
  happen. Disagreement therefore had no channel, so the recorded answer distribution could not
  report how often a halt was correct -- the exact measurement needed before anyone tunes a halt
  rate.

Add `reject` to `HumanDecisionAction` and the terminal `rejected` / `rejected_by_operator` pair to
  the receipt. Reject and escalate stay separate members because they do opposite things to the run:
  escalate leaves the durable pause resumable, reject ends it. Collapsing them is cheaper and
  destroys the signal.

`allowed_actions` widens from four to five entries, which is the size of the vocabulary rather than
  a policy: a pause that is skippable, re-verifiable, rejectable, teachable and escalatable is
  legitimate, and a stale bound would have refused to issue that task at all.

The cause of a rejection is a closed enum with one member. A reason taxonomy would be more
  informative, but there is no evidence yet for what its members should be -- the reject rate is the
  data that would design them -- and adding members later is additive, whereas a free-text reason
  could never be closed again.

### Chores

- Gitignore .private/ ([#14](https://github.com/OpenAdaptAI/openadapt-types/pull/14),
  [`79e4987`](https://github.com/OpenAdaptAI/openadapt-types/commit/79e498773ba1411ec1f30b8bf4b830b3d03c1336))

`.private/` is the workspace-wide convention for material that must never be published. It was not
  ignored here, so a directory created inside this checkout was one stray `git add` from being
  committed. Ignore it mechanically rather than relying on that never happening.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


## v0.6.2 (2026-07-27)

### Bug Fixes

- Close the attended terminal-outcome contract gap with HumanDecisionReceiptV1
  ([#13](https://github.com/OpenAdaptAI/openadapt-types/pull/13),
  [`4eb190f`](https://github.com/OpenAdaptAI/openadapt-types/commit/4eb190f7f49cacf0f689d4ef6fc2c1506d2da316))

The signed human-decision *task* contract shipped in 0.6.0/0.6.1, but the *receipt* that closes the
  round trip did not. `openadapt-flow` already returns a terminal receipt from its console decision
  route and had to define its own half locally, so no consumer outside Flow can validate the shape
  Flow actually produces. Cloud cannot check the one payload that reports whether an operator's
  decision resumed, halted, or may have been delivered.

`HumanDecisionReceiptV1` closes that gap. It is a closed type in which protected content is
  structurally unrepresentable rather than stripped on send: no free-text field, `reason_code` a
  closed enum, every string closed by a pattern/const/enum, unknown fields rejected, RFC 3339
  timestamps, and uncertainty a first-class terminal state.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


## v0.6.1 (2026-07-27)

### Bug Fixes

- Close free text in human decision timestamps and pin canonicalization
  ([#12](https://github.com/OpenAdaptAI/openadapt-types/pull/12),
  [`b6a6aa5`](https://github.com/OpenAdaptAI/openadapt-types/commit/b6a6aa5e7bd58664f72410f484d266cf8a83e4f1))

The signed human-decision task contract shipped in 0.6.0 is already strict, closed, and Cloud-safe
  in Python. Two narrow gaps remained against the mobile attended-decision design, both about
  consumers that are not Python.

`created_at` and `expires_at` were the only declared strings with no pattern. Python rejected
  non-timestamps through the model validator, but the packaged `human-decision-task-v1.json` -- the
  artifact a TypeScript or other-language consumer validates against -- constrained them only by
  length, so it accepted up to 40 characters of arbitrary text in two fields. Free text is how PHI
  escapes, so the RFC 3339 shape now lives in the field pattern and therefore in the exported
  schema.

Canonicalization was correct but undocumented and unpinned: a reimplementer had to infer the rules
  from `json.dumps` keyword arguments. The rules are now normative in the module docstring, and a
  frozen vector pins the exact canonical bytes, digest, and signature hex so a cross-language
  implementation can self-check and a future change fails loudly instead of silently re-signing.

Tests now enumerate each forbidden evidence category -- screenshots, OCR, expected/observed values,
  free text, identifiers, unknown fields -- against the task and both nested models, and a
  structural guard walks the exported schema to fail on any string not closed by a pattern, const,
  or enum. That guard is what stops a later field addition from quietly reopening the hole.

No signed byte changes: the pinned vector was computed before this change and still matches. Every
  timestamp format Flow's `_iso()` emits still validates.

Claude-Session: https://claude.ai/code/session_01NyCHrzA1psrKMFfroYbzaM

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


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
