# CHANGELOG


## v0.20.0 (2026-10-15)

### Chores

- Sync the published source policy ([#42](https://github.com/OpenAdaptAI/openadapt-types/pull/42),
  [`c9113b8`](https://github.com/OpenAdaptAI/openadapt-types/commit/c9113b82b75d73b96dce34b1a09cdf7865276ce1))

Replace source-policy.public.json with the render of the canonical policy on openadapt-internal main
  (policy_last_updated 2026-09-03).

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Documentation

- Lead the README with what the package does and a quick start
  ([#43](https://github.com/OpenAdaptAI/openadapt-types/pull/43),
  [`49a3b59`](https://github.com/OpenAdaptAI/openadapt-types/commit/49a3b59f51a4f34820e187cecf5f765d9913a0c8))

* docs: lead the README with what the package does and a quick start

The README now opens with what the models describe, who they are for, and a quick start that runs as
  written against the PyPI release (0.19.0). The contract table (admission registry, process
  receipts, Execute, authoring, clinic, reward) moves to an index at the top of docs/CONTRACTS.md,
  which the README links once. The README also stops claiming that the grounding and privacy
  packages use these types, and that the screen model covers RDP and Citrix, since the code doesn't
  support either claim.

Links in the README are absolute GitHub URLs, because PyPI shows the same page and doesn't resolve
  relative links. docs/CONTRACTS.md had a broken relative link to examples/execute; it now points
  one level up.

New checks in tests/test_readme_claims.py: - every Python block in the README runs, and prints what
  its comments say - the README passes the vocabulary lint at the business level - the README has no
  relative links - the docs/CONTRACTS.md index names every shipped schema file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* docs: correct README claims found by running it from PyPI

A first run of the README against openadapt-types 0.19.0 turned up claims the code doesn't support:

- The converter example imported from_benchmark_action from _compat. The package exports a
  different, public from_benchmark_action that detects normalized coordinates; the _compat one
  treats {"x": 0.5, "y": 0.3} as pixels. The example now uses the public function and prints
  is_normalized. - "Every model round-trips through JSON" fails for a ComputerState that holds
  screenshot_png bytes, and so does Episode.to_json(). Limits now says so and names screenshot_path
  and screenshot_base64. - The parse_action() section recommends node_id targets, but the
  NAME(arg=value) form drops node_id and description. Limits now says so. - plain_outcome() maps
  OpenAdapt run statuses, not any run's. The section and the PlainOutcomeV1 row now say OpenAdapt. -
  The forty files under schemas/ are not all JSON Schema: one is OpenAPI and two are test vectors.
  The README and docs/CONTRACTS.md now say so.

The package docstring's quick start used ActionTarget without importing it. It now imports it, and a
  test runs it. Two more tests fail when a fix removes a limit the README still lists.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **execute**: Optional Cloud issuer seal_signature on ExecuteEvidenceReceiptV1
  ([#44](https://github.com/OpenAdaptAI/openadapt-types/pull/44),
  [`0de097a`](https://github.com/OpenAdaptAI/openadapt-types/commit/0de097af8210227f96aaac5923880382ac244fca))

Adds openadapt_types.seal: SealSignatureV1 (openadapt.seal-signature/v1), key registry parsing, and
  fail-closed verify_seal_signature / verify_sealed_receipt, byte-compatible with Cloud's
  sealSignature.ts (a TypeScript-generated test vector verifies in Python). The receipt gains an
  optional seal_signature; existing receipts stay valid and are unsigned. Verification needs
  cryptography and refuses without it.

Signed-off-by: Richard Abrich <richard.abrich@mldsai.com>


## v0.19.0 (2026-10-14)

### Features

- Add plain run results and a shared vocabulary lint
  ([#41](https://github.com/OpenAdaptAI/openadapt-types/pull/41),
  [`0658f95`](https://github.com/OpenAdaptAI/openadapt-types/commit/0658f95d0a7c7f6673de64b17a1e433c743c3788))

* wip: snapshot of interrupted build track

Partial work saved after the session limit interrupted the agent. A follow-up commit on this branch
  completes and cleans it up before review.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(vocabulary): finish the registry files and the lint

Turns the partial vocabulary directory into a package and checks the two registry files against each
  other.

- outcomes.json lists every emitter it was checked against (Flow origin/main 6e4e4ed0, Agent
  88b1dbd, this repo 07b8b0c), the run states it deliberately leaves out, and the Flow program
  graph's end state as one more coarse source. - The REJECTED_POLICY result reads "Blocked before
  saving" in both files. - Every engine status code in capitals is an error on a business surface. -
  The lint takes Literal aliases and Pydantic fields for coverage, keeps one finding per stretch of
  text, and refuses a registry whose labels disagree or whose may-have-changed rows claim nothing
  changed. - python -m openadapt_types.vocabulary runs the lint.

* feat: map every run outcome to a plain result

plain_outcome() turns whatever outcome values a surface has into one result a person can act on:
  done, stopped_before_change, check_the_record, finished_not_checked, did_not_finish, or refused.
  It also says whether anything may have changed, whether a retry is safe, what backs the result
  (sealed, local, or none), the next step in plain words, and the exact code it came from.

The precise transaction_outcome or Execute terminal_outcome decides. A coarse HALTED alone always
  reads "Check the record", and so do unknown values, disagreeing inputs, missing outcomes, and
  uncertain delivery. PlainOutcomeV1 refuses any hand-built result that breaks that rule.

Helpers cover a Flow report.json, an Execute status, an Execute receipt, and a terminal Execute
  webhook (sealed only after its HMAC checks).

outcome-vectors.json holds 46 hand-written cases for ports to other languages. The tests check every
  enum this package owns, every emitter snapshot in outcomes.json, and about 35,000 input
  combinations against the safety rule. JSON Schemas ship as plain-outcome-v1.json and
  plain-progress-v1.json.

* feat(vocabulary): ship the lint as openadapt-vocab-lint and test it

- Console script openadapt-vocab-lint, next to python -m openadapt_types.vocabulary and running a
  copied lint.py directly. - The registry validator now accepts a "nothing changed" row only for the
  eleven engine values that prove no effect by their own contract. Any other row that says so is
  refused, so a copy edit can't weaken the safety rule. - Tests cover the brief's vocabulary term by
  term, every word the brief keeps off first screens, the registry's own sentences against the
  business check and the writing rules, the old "nothing changed" sentences from the Agent, Cloud,
  Desktop, and website, Literal and Pydantic coverage, .vocab.json configs, exit codes, and a copied
  registry running with no site-packages.

* fix(vocabulary): find claims inside joined Python strings

Python joins adjacent string literals into one constant that starts on the first line. The claim
  check looked only around that first line, so it missed the Agent's MCP server instructions, where
  "the record did not change" sits fifteen lines down. Fragments now carry their end line, and
  findings point at the line that holds the matched words.

Run against openadapt-agent origin/main 88b1dbd, the claims check now reports all eight places the
  concept-model review listed, plus two more (README.md:181 and the "stopped safely" message in
  runner.py:65).

coverage also takes --partial for an emitter that sends only some of a source's values, such as
  Flow's production receipt.

* docs: explain plain results and the vocabulary lint

docs/VOCABULARY.md covers the surface levels, the vocabulary, the six run results and the safety
  rule, how to call plain_outcome() from Python, how to port the table to another language, how to
  run the lint, and the CI commands Flow and the Agent need to keep their outcome values covered. It
  also records the 2026-10-09 checks against Flow and Agent origin/main.

The README gains a short example and a row for PlainOutcomeV1, and CONTRACTS.md says what a plain
  result may carry. A test keeps the doc's terms, labels, and counts in step with the registry.

* docs(vocabulary): keep the registry and docs clean under their own lint

The claims check flagged three places in this repository that quote the phrases it looks for. The
  docs now carry reasoned vocab-allow comments, and one test vector has a name that no longer reads
  as a claim. vocabulary.json also says outright that a plain term may appear at every level.

* docs(vocabulary): note that patterns match words, not senses

A hospital admission is plain English on a healthcare page, but the admission pattern can't tell it
  from workflow admission. The doc says to mark that use with a reasoned vocab-allow comment. The
  writing-rule test now spells the dash characters as escapes.

* fix(vocabulary): import the module entry point without running the lint

Walking the package with pkgutil imported vocabulary/__main__.py, which ran the CLI and exited with
  status 2. The entry point now runs only under python -m.

* feat(vocabulary): map control overlay phases to plain results

The overlay frame is what a person sees on screen while a run goes, and it carries only a phase.
  Flow sets its end phase straight from the coarse execution_outcome, so a halted overlay says
  nothing about whether a save went through. Without a row, a surface had to invent its own words
  for it, which is how "stopped safely" reached people.

overlay_phase is now a coarse source in the third tier. Its end phases read the same as the
  execution_outcome they mirror, so halted reads "Check the record". Mid-run phases read as the
  Execute progress states (observing through stopping as Running, paused as Waiting for a person),
  and idle and recording aren't runs. An in-progress entry for a source other than execute_state
  names the Execute state it reads as, so every surface shares three progress labels.

Tests check ControlOverlayPhase coverage, that each end phase reads like its execution_outcome, and
  the safety rule across coarse and overlay combinations. Five new vectors cover it for other
  languages.

* fix(vocabulary): don't read code that mentions the pragma as a pragma

The claims check flagged lint.py's own pragma pattern as a malformed vocab-allow. The docs tell
  other repositories to copy the vocabulary directory into their tree, so `claims src` over that
  tree always failed. Pragma names now hold only the characters a term or rule uses.

* test(vocabulary): fail when an outcome enum here is neither mapped nor set aside

The coverage tests only checked the enums someone remembered to list. A new enum in this package
  that holds run outcome values (verified, halted, and the rest of the transaction, Execute, and
  coarse outcome values) now fails until outcomes.json maps it as an emitter or lists it under
  not_run_outcomes with a reason. A second test checks that each emitter read from this package
  lists exactly its enum's values.

The scan found seven enums that weren't accounted for. None is a run result, so each is now set
  aside with its reason: the reward oracle's episode verdict, a person's answer on a paused run or a
  business decision, authoring command status, sign-in outcomes, and artifact verification.

* fix(vocabulary): stop suggesting "Stopped before saving" for a bare HALTED

HALTED was filed under the Stopped before saving term, so the terms check told writers to replace
  "Status: HALTED" with "Say: Stopped before saving". A coarse HALTED alone reads "Check the record"
  in the outcome table, so the lint was steering copy toward the overclaim the safety rule forbids.

HALTED now has its own entry. Its suggestion is "Check the record", with Stopped before saving only
  when transaction_outcome is HALTED_BEFORE_EFFECT. The registry pair check also refuses any
  technical term that is itself an outcome value but sits under a result that value doesn't read as
  on its own.

* docs(vocabulary): say how the coarse outcome can still add caution

The tier description said a lower tier never overrides the first one. Merge rule 4 lets
  execution_outcome turn a disagreeing transaction outcome into Check the record, so the doc now
  names that exception.

* docs(vocabulary): say exactly what the schema and coarse tiers promise

CONTRACTS.md said the JSON Schema refuses a result that breaks the safety rule. Only the
  PlainOutcomeV1 model does; the schema names the rule but doesn't enforce it, so ports check
  themselves against the vectors. The label and next step can't carry PHI, which is narrower than
  saying the whole object can't.

VOCABULARY.md now says which coarse values can read as nothing written with dispatched=False (Agent
  timeout or error, connector failure) instead of implying a coarse halt can. It also says that
  technical terms stay allowed at the engineering level, and what an idle overlay reads as.

* fix(vocabulary): refuse a plain result the table doesn't give for its inputs

PlainOutcomeV1 claimed that a hand-built or deserialized result can't claim more than the table
  allows, but it only checked the result against its own flags. A result built by hand could read a
  bare HALTED as "Stopped before saving" with safe_to_retry=true, or call a coarse connector success
  "Done and checked", and still validate.

The merge rules now live in a pure _decide() that both plain_outcome() and the model validator use.
  The validator re-derives the decision from the values in `exact` and refuses a technical_code,
  result, may_have_changed, or safe_to_retry that differs. A fallback code only adds caution, so it
  stays valid with any inputs. Every table output and every conformance vector still round-trips.

* docs(vocabulary): keep process receipt outcomes out of the plain table

ProcessEvidenceReceiptV1.outcome reuses ExecuteTerminalOutcomeV1, so the enum coverage test counted
  it as mapped. Flow's process runtime (runtime/process_v1.py) sets it to the outcome of the step
  that ended the process, even when an earlier step already saved. Read as an Execute terminal
  outcome, halted_before_effect there would tell a person nothing was written.

List it under not_run_outcomes with that reason, and tell callers in VOCABULARY.md not to pass it as
  execute_terminal_outcome.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.18.0 (2026-09-03)

### Bug Fixes

- **reward**: Refuse the certificate claims this version cannot check
  ([#40](https://github.com/OpenAdaptAI/openadapt-types/pull/40),
  [`b7b40f3`](https://github.com/OpenAdaptAI/openadapt-types/commit/b7b40f395125aae2c084f49c6a10933771c3e9d0))

A reward certificate was trusted on its shape. Four properties it asserted were never checked
  anywhere:

* `calibration_scope: production`. The validator refused only the pair `self_signed` + `production`,
  so `issuer="organization"` bought a receipt reading `certified: true, calibration_scope:
  production, production_certified: true` with no worker, no oracle, and no read.

* `issuer: organization`. There is no issuer key registry, so nothing can resolve `issuer_key_id` to
  a key anyone trusts. * The contract's own `certificate_policy`. `RewardCertificateV1.satisfies`
  had exactly one caller, a test. A certificate measured at epsilon 0.248885 against a contract
  demanding 0.05 still produced `certified`. * Revocation. `grep -i revok` over the reward code in
  types, evals, and flow returns nothing, and the certificate docstring described revocation as an
  existing mechanism checked by the issuer.

The narrowing:

* `RewardCalibrationScopeV1` keeps `SYNTHETIC` only, and `RewardCertificateIssuerV1` keeps
  `SELF_SIGNED` only. Both stay enums, so adding a member back once a registry exists is not a
  breaking change. * `RewardEvidenceReceiptV1.production_certified` is gone. With one scope it could
  only ever return False, and its name promised a distinction the type cannot draw. * `score()`
  takes the contract as a required keyword and drops `scoring`. It reads the scalar from
  `contract.scoring` and certifies only a certificate that names this contract by digest and clears
  `contract.certificate_policy`. `RewardScoreV1` gains `certification_refusals`, so a false
  `certified` says why. * `RewardCertificateV1.unmet(policy)` lists each shortfall; `satisfies` is
  now `not unmet(...)` and is live code on the certification path. *
  `RewardEvidenceReceiptV1.certification_refusals(contract, certificate)` lets a reader who holds
  both recheck a receipt's flag. The receipt carries digests, so it cannot check itself during
  validation. * The docstrings drop the revocation sentence and say plainly that `signature` is
  checked for encoding and length only.

Breaking for `score()` callers and for anything that reads `production_certified` or constructs a
  production-scope certificate. Nothing outside tests constructs one today.

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>


## v0.17.1 (2026-09-03)

### Bug Fixes

- **types**: Close production admission and authoring contracts
  ([#39](https://github.com/OpenAdaptAI/openadapt-types/pull/39),
  [`f8a1ca1`](https://github.com/OpenAdaptAI/openadapt-types/commit/f8a1ca12eac52c564b07d50339525297dd04a4d6))


## v0.17.0 (2026-09-01)

### Features

- **types**: Add calibration scope and issuer to the reward certificate
  ([#38](https://github.com/OpenAdaptAI/openadapt-types/pull/38),
  [`cbadccf`](https://github.com/OpenAdaptAI/openadapt-types/commit/cbadccf3c9a7a9285832919580e210091aff763d))

A self-signed reward certificate may carry only synthetic scope. certified requires oracle tier 2 or
  3, a current certificate, a calibration corpus digest, and a stated calibration scope. The only
  computable scope today is synthetic.

Merged by an agent session, not the founder.


## v0.16.0 (2026-09-01)

### Features

- **types**: Add reward contract, certificate, and evidence receipt
  ([#37](https://github.com/OpenAdaptAI/openadapt-types/pull/37),
  [`ef42cd7`](https://github.com/OpenAdaptAI/openadapt-types/commit/ef42cd738d31d5c35a766aca8d19760e84801541))

RewardContractV1, RewardCertificateV1, and RewardEvidenceReceiptV1 for training against a verified
  terminal effect with a certified false-accept bound. RECONCILIATION_REQUIRED and FAILED_PLATFORM
  are unscored and never 0.0. certified requires oracle tier 2 or 3 plus a current certificate. The
  reward receipt is not an Execute Seal.

Merged by an agent session, not the founder.


## v0.15.0 (2026-09-01)

### Features

- **types**: Add clinic inbox, outbox, and MCP result contracts
  ([#36](https://github.com/OpenAdaptAI/openadapt-types/pull/36),
  [`7af28a7`](https://github.com/OpenAdaptAI/openadapt-types/commit/7af28a748901656a03535f1d020011b86d5df5a0))

Public JSON for the workbench-to-OpenAdapt job handoff. Identity is patient_token only. HALTED and
  RECONCILIATION_REQUIRED do not map to success. needs_human true refuses actuation.


## v0.14.0 (2026-09-01)

### Features

- **types**: Authoring observe, command, and bind schemas
  ([#35](https://github.com/OpenAdaptAI/openadapt-types/pull/35),
  [`9a4e46f`](https://github.com/OpenAdaptAI/openadapt-types/commit/9a4e46ffa07734dd05374175da9122677496f131))

Merged by an agent session, not the founder.


## v0.13.0 (2026-08-31)

### Features

- Add portable process capability contracts
  ([#34](https://github.com/OpenAdaptAI/openadapt-types/pull/34),
  [`07e89a4`](https://github.com/OpenAdaptAI/openadapt-types/commit/07e89a4260e97ff2fbb7aaeee65a9f764dd87000))


## v0.12.0 (2026-08-29)

### Features

- Add a Tier-2 oracle adapter and refuse visual VERIFIED
  ([#33](https://github.com/OpenAdaptAI/openadapt-types/pull/33),
  [`a542c07`](https://github.com/OpenAdaptAI/openadapt-types/commit/a542c075deccc8977837fde0e3ed03ffee5205f6))

A partner implements channel plus read (API, DB, file, ack, or a second session). Production Execute
  receipts require oracle tier 2 or 3. Visual and OCR (tier 0) cannot mint VERIFIED.

Stacked on #32 so both gates stay fail-closed: observed strength cannot be visual, and oracle_tier
  must match it.

Merged by an agent session, not the founder.


## v0.11.0 (2026-08-29)

### Chores

- Ignore every .env variant, not one at a time
  ([#28](https://github.com/OpenAdaptAI/openadapt-types/pull/28),
  [`fa0ec1f`](https://github.com/OpenAdaptAI/openadapt-types/commit/fa0ec1f640fe7ed15351c0914cca29673eaa4ae8))

.gitignore did not cover .env.bak or .env.local. A .env.bak containing populated API keys sat in a
  local stash in another repo since 2026-07-13, untracked and one `git add -A` from being committed.

Use a glob and negate the committed .env.example template.

- **policy**: Sync generated source boundary
  ([#29](https://github.com/OpenAdaptAI/openadapt-types/pull/29),
  [`2b5f521`](https://github.com/OpenAdaptAI/openadapt-types/commit/2b5f521b31a11b178fb18a516c37f09d50057229))

### Documentation

- Rewrite the README around the schema you actually import
  ([#30](https://github.com/OpenAdaptAI/openadapt-types/pull/30),
  [`eef6eaa`](https://github.com/OpenAdaptAI/openadapt-types/commit/eef6eaacdc8e5a02790e6d39ac5ba7b1eec4d4ea))

Same structural rewrite as openadapt-flow. The old file spent its first 30 lines on a status
  admonition and a stack table copied verbatim into five other repositories, then buried the code
  sample that shows what this package is.

- Open with what the schemas are for and who else in the stack reads them, then the install line,
  then the code. - Move the overlay and business-decision wire-contract rules, about 45 lines of
  exact 'may not carry' language, into docs/CONTRACTS.md. Nothing deleted. - Turn the Design
  principles bullets into prose. Every bullet was a bolded phrase followed by a restatement of
  itself. - Verified against openadapt-types 0.10.1 from PyPI: to_text_tree() output, ActionTarget
  field names, the _compat converters, ExecuteClient, and the count of shipped JSON schema files.

265 lines to 154.

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>

- State the real number of shipped schema files
  ([#31](https://github.com/OpenAdaptAI/openadapt-types/pull/31),
  [`d06c42d`](https://github.com/OpenAdaptAI/openadapt-types/commit/d06c42d319d868264d6e3a65f04dd51dae807d7a))

The README said nineteen JSON files ship under openadapt_types/schemas/. Seventeen do. Nobody could
  have caught that by reading the diff, which is the problem worth fixing rather than the digit.

So the claim now has a test behind it, along with two neighbours in the same class: every relative
  link in the README resolves to a real path, and every schema filename it names in backticks is one
  the package ships.

Co-authored-by: Claude Opus 5 <noreply@anthropic.com>

### Features

- **execute**: Bind Seal fields on the evidence receipt
  ([#32](https://github.com/OpenAdaptAI/openadapt-types/pull/32),
  [`179c01f`](https://github.com/OpenAdaptAI/openadapt-types/commit/179c01fea8bff9d0c616ae6e50db2d69e21e25bb))

ExecuteEvidenceReceiptV1 is the Seal. It binds program version, admission, environment, runner,
  independent-effect tier, outcome, and a per-receipt nonce, plus the bundle hash it already had.

verified and rolled_back_verified require oracle_tier 2 or 3. oracle_tier must match
  observed_effect_strength, so a caller cannot claim SoR after a screenshot check.

Merged by an agent session, not the founder.


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
