"""Drive the claim -> evidence validator (`scripts/validate_claims.py`).

Three kinds of assertion:

* the REAL registry is honest right now (every claim's tier is backed by
  evidence of at least equal strength, and every evidence path exists) — what
  the CI gate enforces on every PR;
* the validator actually FAILS on a mislabeled claim (a `supported` claim whose
  only backing is an opt-in / infra-gated test, or a `field` result labeled
  `supported`, or a missing path) so the gate cannot rot into a no-op; and
* every real claim in `claims.yaml` points at a path that exists (registry-rot
  guard, independent of the tier contract).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "validate_claims.py"
_spec = importlib.util.spec_from_file_location("validate_claims", _SCRIPT)
assert _spec and _spec.loader
vc = importlib.util.module_from_spec(_spec)
# Register before exec so the module's @dataclass decorators can resolve their
# own __module__ (dataclasses looks the module up in sys.modules).
sys.modules[_spec.name] = vc
_spec.loader.exec_module(vc)

# A real opt-in / infra-gated test and a real CI test, used to build synthetic
# registries. These paths are asserted to exist by the rot guard below, so if
# they are ever moved this test fails loudly rather than testing a fiction.
OPTIN_TEST = "tests/e2e/test_citrix_pixel_e2e.py"
INLINE_OPTIN_TEST = "tests/e2e/test_citrix_workspace_standin_e2e.py"
PARALLELS_TEST = "tests/e2e/test_parallels_desktop_e2e.py"
CI_TEST = "tests/test_replayer.py"
DOC_ARTIFACT = "docs/desktop/CITRIX_PIXEL.md"


# --------------------------------------------------------------------------- #
# the real registry is honest right now
# --------------------------------------------------------------------------- #
def test_real_registry_passes() -> None:
    registry = vc.load_registry()
    results = vc.validate_all(registry)
    errors = [e for r in results for e in r.errors]
    assert errors == [], "claims gate found violations:\n" + "\n".join(errors)


def test_real_registry_has_the_seeded_claims() -> None:
    ids = {c["id"] for c in vc.load_registry()["claims"]}
    for expected in (
        "web-supported",
        "deterministic-zero-model-replay",
        "effect-verification-silent-writes",
        "identity-gate-halt-armed",
        "halt-teach-promote",
        "windows-desktop-validating",
        "citrix-pixel-validating",
        "openemr-field-benchmark",
    ):
        assert expected in ids, f"seeded claim missing from registry: {expected}"


def test_every_real_evidence_path_exists() -> None:
    """Registry-rot guard: every path in every claim must exist on disk."""
    missing: list[str] = []
    for claim in vc.load_registry()["claims"]:
        for ev in claim.get("evidence", []) or []:
            if not (REPO_ROOT / ev["path"]).exists():
                missing.append(f"{claim['id']} -> {ev['path']}")
    assert missing == [], "claims.yaml references missing paths:\n" + "\n".join(missing)


# --------------------------------------------------------------------------- #
# the opt-in detector works on the REAL opt-in tests
# --------------------------------------------------------------------------- #
def test_optin_detector_flags_the_real_optin_tests() -> None:
    citrix = (REPO_ROOT / OPTIN_TEST).read_text(encoding="utf-8")
    citrix_standin = (REPO_ROOT / INLINE_OPTIN_TEST).read_text(encoding="utf-8")
    parallels = (REPO_ROOT / PARALLELS_TEST).read_text(encoding="utf-8")
    assert vc.detect_optin_env(citrix) == "OAFLOW_CITRIX_PIXEL_E2E"
    assert vc.detect_optin_env(citrix_standin) == "OAFLOW_CITRIX_STANDIN_E2E"
    assert vc.detect_optin_env(parallels) == "OAFLOW_PARALLELS_E2E"


def test_optin_detector_ignores_per_function_skipif() -> None:
    """`test_effect_fhir.py` gates ONE function on an env var but the module
    still runs in CI, so it must NOT be classified as opt-in."""
    src = (REPO_ROOT / "tests/test_effect_fhir.py").read_text(encoding="utf-8")
    assert vc.detect_optin_env(src) is None


# --------------------------------------------------------------------------- #
# the validator FAILS on overclaims (the whole point of the gate)
# --------------------------------------------------------------------------- #
def _claim(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "synthetic",
        "claim": "a synthetic claim",
        "surfaces": ["README.md"],
        "tier": "supported",
        "evidence": [{"path": CI_TEST, "proves": "x"}],
        "caveats": [],
    }
    base.update(over)
    return base


def test_supported_with_only_optin_backing_fails() -> None:
    """The headline mislabel: a `supported` claim whose only proof is an opt-in
    (infra-gated) test must fail — opt-in never runs on default CI."""
    result = vc.validate_claim(
        _claim(tier="supported", evidence=[{"path": OPTIN_TEST, "proves": "x"}])
    )
    assert not result.ok
    assert any("OVERCLAIM" in e and "supported" in e for e in result.errors)


def test_same_claim_labeled_validating_passes() -> None:
    """The identical opt-in evidence is fine at the honest `validating` tier."""
    result = vc.validate_claim(
        _claim(tier="validating", evidence=[{"path": OPTIN_TEST, "proves": "x"}])
    )
    assert result.ok, result.errors


def test_supported_with_ci_backing_passes() -> None:
    assert vc.validate_claim(_claim()).ok


def test_browser_e2e_evidence_names_its_required_pr_gate() -> None:
    result = vc.validate_claim(
        _claim(
            evidence=[
                {
                    "path": "tests/e2e/test_record_compile_replay.py",
                    "proves": "x",
                }
            ]
        )
    )

    assert result.ok
    assert result.evidence[0].gating == "ci (required PR gate (e2e-browser))"


def test_field_result_cannot_be_supported() -> None:
    """A not-CI-reproducible field result may not be labeled `supported`, even
    with a real CI unit test attached."""
    result = vc.validate_claim(_claim(tier="supported", reproducibility="field"))
    assert not result.ok
    assert any("field" in e for e in result.errors)

    # the same field result IS allowed at `validating`.
    assert vc.validate_claim(_claim(tier="validating", reproducibility="field")).ok


def test_missing_evidence_path_fails() -> None:
    result = vc.validate_claim(
        _claim(evidence=[{"path": "tests/test_does_not_exist_xyz.py", "proves": "x"}])
    )
    assert not result.ok
    assert any("does not exist" in e for e in result.errors)


def test_supported_with_only_doc_evidence_fails() -> None:
    """A doc/benchmark artifact is design/field evidence, not a run proof; a
    claim backed only by a doc cannot exceed `roadmap`."""
    result = vc.validate_claim(
        _claim(tier="supported", evidence=[{"path": DOC_ARTIFACT, "proves": "x"}])
    )
    assert not result.ok
    roadmap_ok = vc.validate_claim(
        _claim(tier="roadmap", evidence=[{"path": DOC_ARTIFACT, "proves": "x"}])
    )
    assert roadmap_ok.ok, roadmap_ok.errors


def test_node_rot_is_caught() -> None:
    result = vc.validate_claim(
        _claim(
            evidence=[{"path": CI_TEST, "proves": "x", "node": "test_not_a_real_node"}]
        )
    )
    assert not result.ok
    assert any("node rot" in e for e in result.errors)


def test_unknown_tier_fails() -> None:
    result = vc.validate_claim(_claim(tier="totally-supported"))
    assert not result.ok
    assert any("unknown tier" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# required-job proof via a JUnit artifact
# --------------------------------------------------------------------------- #
def test_junit_green_check_flags_red_supported_test(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0"?>
        <testsuite>
          <testcase classname="tests.test_replayer" file="tests/test_replayer.py" name="t">
            <failure>boom</failure>
          </testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    parsed = vc.parse_junit(junit)
    assert parsed.get("tests/test_replayer.py") == "failed"
    result = vc.validate_claim(_claim(), junit=parsed, ci_job="test")
    assert not result.ok
    assert any("RED in test JUnit" in e for e in result.errors)


def test_pytest_xunit2_classname_is_mapped_without_file_attribute(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0"?>
        <testsuites><testsuite>
          <testcase classname="tests.test_replayer" name="test_happy_path_click_then_param_type" />
        </testsuite></testsuites>""",
        encoding="utf-8",
    )

    assert vc.parse_junit(junit) == {"tests/test_replayer.py": "passed"}


def test_supported_evidence_must_be_present_and_not_all_skipped() -> None:
    missing = vc.validate_claim(_claim(), junit={}, ci_job="test")
    skipped = vc.validate_claim(_claim(), junit={CI_TEST: "skipped"}, ci_job="test")
    passed = vc.validate_claim(_claim(), junit={CI_TEST: "passed"}, ci_job="test")

    assert any("ABSENT" in error for error in missing.errors)
    assert any("SKIPPED" in error for error in skipped.errors)
    assert passed.ok, passed.errors


def test_scoped_validating_evidence_must_pass_on_its_exact_substrate() -> None:
    claim = _claim(
        tier="validating",
        evidence=[{"path": OPTIN_TEST, "proves": "x"}],
    )
    scope = {OPTIN_TEST}
    absent = vc.validate_claim(
        claim,
        junit={},
        ci_job="validating",
        evidence_scope=scope,
    )
    skipped = vc.validate_claim(
        claim,
        junit={OPTIN_TEST: "skipped"},
        ci_job="validating",
        evidence_scope=scope,
    )
    failed = vc.validate_claim(
        claim,
        junit={OPTIN_TEST: "failed"},
        ci_job="validating",
        evidence_scope=scope,
    )
    passed = vc.validate_claim(
        claim,
        junit={OPTIN_TEST: "passed"},
        ci_job="validating",
        evidence_scope=scope,
    )

    assert any("ABSENT" in error for error in absent.errors)
    assert any("SKIPPED" in error for error in skipped.errors)
    assert any("RED" in error for error in failed.errors)
    assert passed.ok, passed.errors


def test_scoped_validating_report_is_not_ok_when_selected_test_skips() -> None:
    scope = {OPTIN_TEST}
    results = [
        vc.validate_claim(
            _claim(
                tier="validating",
                evidence=[{"path": OPTIN_TEST, "proves": "x"}],
            ),
            junit={OPTIN_TEST: "skipped"},
            ci_job="validating",
            evidence_scope=scope,
        )
    ]

    blob = vc.render_json(
        results,
        now="2026-07-14T00:00:00Z",
        junit_used=True,
        junit_job="validating",
        junit_scope=scope,
    )
    assert blob["green_check_run"] is True
    assert blob["green_check_job"] == "validating"
    assert blob["green_check_scope"] == [OPTIN_TEST]
    assert blob["ok"] is False


def test_real_validating_scope_checks_only_its_two_declared_files() -> None:
    scope = {PARALLELS_TEST, OPTIN_TEST}
    results = vc.validate_all(
        vc.load_registry(),
        junit={PARALLELS_TEST: "passed", OPTIN_TEST: "passed"},
        ci_job="validating",
        evidence_scope=scope,
    )
    errors = [error for result in results for error in result.errors]

    assert errors == []
    assert vc.validate_evidence_scope(results, "validating", scope) == []
    unselected = {
        evidence.path: evidence.junit_status
        for result in results
        for evidence in result.evidence
        if evidence.ci_job == "validating" and evidence.path not in scope
    }
    assert unselected
    assert set(unselected.values()) == {"unknown"}


def test_scoped_validating_evidence_rejects_unregistered_path() -> None:
    results = vc.validate_all(vc.load_registry())

    errors = vc.validate_evidence_scope(
        results,
        "validating",
        {"tests/e2e/test_not_registered.py"},
    )

    assert errors == [
        "[validating] scoped evidence is not registered for this job: "
        "tests/e2e/test_not_registered.py"
    ]


def test_junit_file_is_passed_when_one_case_passes_and_an_optional_case_skips(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0"?>
        <testsuite>
          <testcase classname="tests.test_replayer" name="optional"><skipped /></testcase>
          <testcase classname="tests.test_replayer" name="required" />
        </testsuite>""",
        encoding="utf-8",
    )

    assert vc.parse_junit(junit)[CI_TEST] == "passed"


def test_missing_or_empty_junit_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xml"
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuite />", encoding="utf-8")

    for path in (missing, empty):
        try:
            vc.parse_junit(path)
        except vc.JunitEvidenceError:
            pass
        else:
            raise AssertionError(f"{path} did not fail closed")


def test_cli_requires_junit_for_non_structural_supported_check() -> None:
    assert vc.main(["--check"]) == 1
    assert vc.main(["--check", "--structure-only"]) == 0
    assert vc.main(["--check", "--junit", "missing.xml"]) == 1
    assert (
        vc.main(
            [
                "--check",
                "--ci-job",
                "validating",
                "--junit",
                "missing.xml",
            ]
        )
        == 1
    )


def test_report_renders_without_crashing() -> None:
    results = vc.validate_all(vc.load_registry())
    md = vc.render_markdown(results, now="2026-07-14T00:00:00Z", junit_used=False)
    assert "VERIFICATION" in md
    assert "web-supported" in md
    assert md.endswith("\n") and not md.endswith("\n\n")
    blob = vc.render_json(results, now="2026-07-14T00:00:00Z", junit_used=False)
    assert blob["ok"] is True
    assert blob["green_check_job"] is None
    assert blob["green_check_scope"] == []
    assert blob["validation_errors"] == []
    assert all(
        "ci_job" in evidence
        for claim in blob["claims"]
        for evidence in claim["evidence"]
    )
    assert {c["id"] for c in blob["claims"]}  # non-empty
