# Transformation freshness CI follow-up

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

The earlier full CI also found the known reconciliation date-fixture collision, fixed on main by `91beef30848e9125860f375f0d2a0d5d863c89fe`. That main change is absent from this stacked source. PR #2312 remains on its explicit #2299 dependency and independent-review hold; these local results do not claim overall green CI, a human approval, a publication or a release.

Generality: no per-app engine code; one transformation identity covers all publication entrypoints, with source/kind/config/provider changes and preserved old generations tested. Held-out cases are synthetic, with no real customer publication claimed. The durable result is that changed transformations cannot inherit old human approval.
