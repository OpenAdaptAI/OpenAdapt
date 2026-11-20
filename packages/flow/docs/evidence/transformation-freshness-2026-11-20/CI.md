# Transformation freshness validation

## Earlier source follow-up

Full CI at `418cace67dcdad32ecbee811788bc389d8220f44` found an unnecessary dynamic import in provider source inspection and stale artifact-read inventory entries after sanitization moved into helper functions.

The follow-up inspects already-loaded provider classes, retaining their source hashes without importing a module named by a string. The read inventory now covers both public wrappers and their implementation helpers. Generation directory and sibling archive existence checks have explicit reasons; artifact content still uses sealed-aware readers. The import-debt baseline and reader enforcement remain unchanged.

Validation through the shared CPU slot:

- 20 focused checks passed: the transformation state tests, both artifact-reader inventory checks, and the direct and transitive import-layer gates. The one warning is an existing deprecated verifier import.
- The actual Privacy 1.1.3 Presidio provider produced a stable identity, invalidated it when its effective scrub policy changed, and returned to the original identity after the policy was restored. This probe created no source artifact or approval.
- Ruff check and format checks passed for both changed Python files.
- Mypy passed for the four sanitizer/publication modules using Flow's checked-in configuration and `--follow-imports=skip`.
- Source-boundary and engine-generality checks passed.

The focused pytest invocation, from `packages/flow`, was:

```sh
python -m pytest -q tests/test_sanitization_policy_freshness.py \
  tests/test_artifact_reads.py::test_converted_commands_read_artifacts_through_the_reader \
  tests/test_artifact_reads.py::test_allowlist_has_no_stale_entries \
  tests/test_import_layers.py::test_core_imports_nothing_forbidden_directly \
  tests/test_import_layers.py::test_core_reaches_nothing_forbidden
```

The earlier full CI also found the reconciliation date-fixture collision, fixed on main by `91beef30848e9125860f375f0d2a0d5d863c89fe`. That fix was absent from the earlier stacked source. The results above describe that source only.

Generality: no per-app engine code; one transformation identity covers all publication entrypoints, with source/kind/config/provider changes and preserved old generations tested. Held-out cases are synthetic, with no real customer publication claimed. The durable result is that changed transformations cannot inherit old human approval.


## Accepted-main integration

The existing source at `e7606c89bc2605fa173a60112217a3cb04a00d6b` was merged normally with accepted main `2579839e13fb28f320ddb58d317dc0658b58dd84`, after #2299 was delivered. Both parents and all earlier source commits remain in the history. Main supplies the reconciliation date-fixture fix.

The merge conflicted only in `openadapt_flow/sanitized_artifact.py` and `tests/test_sanitized_artifact.py`. Resolution preserves main's conservative AuthRef publication profile, credential-family veto, whole-record refusal and property tests, together with the transformation identity and provider configuration fixtures. AST comparison confirms the credential-prefix expression, identifier predicate and complete `_DeclaredFields` class match accepted main. The obsolete entropy import was removed. No scanner policy or import/reader baseline changed.

Validation of this integrated source, sequentially through the shared CPU slot:

- 538 Flow checks passed, with one existing Windows-only open-descriptor replacement test skipped on macOS and one existing deprecated-verifier import warning. This covered transformation identity/generation/approval transitions, the sanitizer, hosted and wrap publication, runtime validation, both artifact-reader inventories, and both core import-layer gates.
- Nine owning-Privacy configuration and provider-identity checks passed. These check policy identity and operation locking; they do not claim a real-data scrub or hosted publication.
- Ruff check and format checks passed on the ten affected Flow Python files and two Privacy files. Mypy passed on the four publication modules with Flow's checked-in configuration and `--follow-imports=skip`.
- Engine-generality, public artifact/release consistency and repository source-boundary checks passed. Release metadata remains 3.3.0.

The first test invocation stopped during collection because the reused local environment lacked `jsonschema`; it ran no tests. The lockfile's `jsonschema` 4.26.0 was installed in an isolated test-only directory before the successful invocation. The shared Python environment was unchanged.

The Flow invocation was:

```sh
python -m pytest -q tests/test_sanitization_policy_freshness.py \
  tests/test_sanitized_artifact.py tests/test_hosted.py \
  tests/test_wrap_publish.py tests/test_runtime_validation.py \
  tests/test_artifact_reads.py::test_converted_commands_read_artifacts_through_the_reader \
  tests/test_artifact_reads.py::test_allowlist_has_no_stale_entries \
  tests/test_import_layers.py::test_core_imports_nothing_forbidden_directly \
  tests/test_import_layers.py::test_core_reaches_nothing_forbidden --durations=5
```

The separate earlier `e760` native sign-in CI failure remains an actual bounded failure with cause unproved. Its single local pass did not establish flakiness or clear CI. Open #2326 is separate and was not imported by this integration. Required source CI and actual different-vendor review of the new head remain necessary; local results supply neither. No new application, customer-data, hosted, Windows or release qualification is claimed.


## Review correction: fresh content at identity boundaries

The independent review at `90f5e927aa7f99cdee38f7fe18f602b0fd27e256` ([comment](https://github.com/OpenAdaptAI/OpenAdapt/pull/2312#issuecomment-7038054007)) found that an engine-source digest cached by filesystem metadata can remain stale after a same-size rewrite. The earlier passing aggregate did not establish this property.

The correction removes that cache and the confirmed sibling cache in the image-file digest helper. Both read current complete file bytes on each identity request. Metadata remains a concurrent-change check, not permission to reuse an earlier content hash. The image file-size, unavailable-file and change checks, process-start checks, host/file scope and callers remain unchanged. AuthRef, hosted/wrap publication and provider production files are byte-identical to the reviewed parent.

One shared deterministic test model covers the engine package file, a future nested engine helper and the pure image-file digest. It freezes stat observations, rewrites equal-length bytes, requires a different identity, restores the original bytes and requires the original identity, then verifies missing-source refusal. Direct, wrap and hosted generation/approval controls also exercise the equal-stat rewrite. The image adapter exports only the two production function ASTs and constants; it imports no Windows module and reads only an owned ordinary synthetic file. These controls do not claim an observed NTFS timestamp collision, a Windows bypass, loaded-memory/DLL identity or application closure.

The shared controls against exact parent production bytes gave **six expected failures and three positive passes**. The failures covered both engine positions, image content and the three approval paths. With both caches removed, the focused freshness module and the two artifact-reader inventory checks gave **24 passes, no skips and one existing deprecated-verifier warning**. The earlier 538/9 and intermediate engine-only results are historical overlapping cohorts, not rerun totals for this correction. No timing tolerance, sleep, new cache or identity-version flag was added.

```sh
python -m pytest -q tests/test_sanitization_policy_freshness.py \
  tests/test_artifact_reads.py::test_converted_commands_read_artifacts_through_the_reader \
  tests/test_artifact_reads.py::test_allowlist_has_no_stale_entries -rs --durations=5
```

Ruff check/format passed for the three changed Python files, mypy passed for `sanitization_policy.py`, and engine-generality, artifact/release consistency, source-boundary and diff checks passed. AST checks separately preserve the image caller/process-start/size/change/error boundaries. The original review remains unchanged; actual review of this descendant and its required CI remain necessary. No native, provider, hosted, real-data or release qualification follows from these source/synthetic checks.
