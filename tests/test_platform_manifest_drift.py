"""The platform-manifest drift guard must FAIL when the manifest is stale.

`platform-manifest.json` is served from raw.githubusercontent and is the only
public *cryptographic* claim OpenAdapt makes about the current release: a
consumer verifying a downloaded wheel against it must get the right digest.

It went wrong once. On 2026-07-26 the manifest pinned openadapt-flow 1.23.0
with that release's wheel/sdist digests; openadapt-flow 1.24.0 published at
03:58 UTC the next morning and the manifest kept advertising the superseded
digests. Three things let that survive:

1. The drift check only ran on pull requests, pushes to this repo's main, and
   a WEEKLY cron. Staleness here is caused by a release in *another*
   repository, so nothing in this repo changes and only the cron can catch it
   -- a window of up to seven days.
2. The manifest's only automatic regeneration path lives in THIS repository's
   release workflow, so a flow/capture/desktop release has no way to fix it.
3. Both runs triggered by the launcher's own 1.9.0 release went red for
   benign, guaranteed-at-every-release reasons: the semantic-release version
   commit puts pyproject.toml ahead of the not-yet-reconciled manifest, and
   PyPI's `info.version` still read the previous version minutes after upload.
   Main was therefore already red when the genuine drift arrived, so the real
   signal was indistinguishable from the noise.

These tests pin the behaviour that matters: the guard must fail loudly on a
simulated future release and on a tampered digest, and must NOT fail on the
transient conditions that produce false reds -- a guard that cries wolf every
release is the reason a real failure went unnoticed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
from copy import deepcopy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_platform_manifest import (  # noqa: E402
    DriftError,
    _compatibility_status,
    _dependency_edges,
    _parse_component_versions,
    _release_ref_candidates,
    _runtime_unit_requirements,
    _version_satisfies,
)
from validate_platform_manifest import (  # noqa: E402
    Report,
    _locked_openadapt_versions,
    check_against_pypi,
    check_against_status,
    check_compatibility_status,
    check_desktop_sidecar_lock,
    check_signature_honesty,
    check_structure,
    compare_component_to_pypi,
    compare_launcher_to_pyproject,
)

MANIFEST_PATH = REPO_ROOT / "platform-manifest.json"
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/release-and-publish.yml"


@pytest.fixture()
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture()
def flow_component(manifest: dict) -> dict:
    return deepcopy(manifest["components"]["flow"])


def pypi_doc(version: str, component: dict, latest: str | None = None) -> dict:
    """Build a PyPI JSON payload that publishes exactly `component`'s files.

    `latest` defaults to `version`; pass it explicitly to simulate PyPI's
    `info.version` disagreeing with the release being described.
    """
    files = [
        {
            "filename": artifact["filename"],
            "url": artifact["url"],
            "digests": {"sha256": artifact["sha256"]},
            "packagetype": artifact["type"],
        }
        for artifact in component["artifacts"]
    ]
    requires_dist = []
    for constraint in component.get("dependency_constraints", []):
        extras = f"[{constraint['extras']}]" if constraint.get("extras") else ""
        marker = f"; {constraint['marker']}" if constraint.get("marker") else ""
        requires_dist.append(
            f"{constraint['package']}{extras}{constraint['requires']}{marker}"
        )
    return {
        "info": {
            "version": latest or version,
            "requires_dist": requires_dist,
            "requires_python": component.get("requires_python"),
        },
        "urls": files,
        "releases": {version: files},
    }


def errors_for(component: dict, doc: dict) -> list[str]:
    report = Report()
    compare_component_to_pypi("flow", component, doc, report)
    return report.errors


def warnings_for(component: dict, doc: dict) -> list[str]:
    report = Report()
    compare_component_to_pypi("flow", component, doc, report)
    return report.warnings


def test_truthful_manifest_passes(flow_component: dict) -> None:
    """The committed manifest must validate against its own published state."""
    doc = pypi_doc(flow_component["version"], flow_component)
    assert errors_for(flow_component, doc) == []


def test_guard_fails_when_a_newer_release_supersedes_the_manifest(
    flow_component: dict,
) -> None:
    """A future release must make the guard fail -- the exact 1.23.0 bug.

    The manifest still describes the version it was generated for, with
    correct digests for THAT version, and PyPI has moved on. This must fail:
    the manifest's digests no longer describe the current release.
    """
    stale = deepcopy(flow_component)
    future = deepcopy(flow_component)
    future_version = "99.0.0"
    future["version"] = future_version
    for artifact in future["artifacts"]:
        artifact["filename"] = artifact["filename"].replace(
            stale["version"], future_version
        )
        artifact["url"] = artifact["url"].replace(stale["version"], future_version)
        artifact["sha256"] = "f" * 64

    doc = pypi_doc(future_version, future)
    doc["releases"][stale["version"]] = [
        {
            "filename": artifact["filename"],
            "url": artifact["url"],
            "digests": {"sha256": artifact["sha256"]},
            "packagetype": artifact["type"],
        }
        for artifact in stale["artifacts"]
    ]

    errors = errors_for(stale, doc)
    assert errors, "a superseded manifest version must be reported as drift"
    assert any("version drift" in error for error in errors)
    assert any(future_version in error for error in errors)


def test_explicit_published_selection_can_pin_an_older_exact_release(
    flow_component: dict,
) -> None:
    future_version = "99.0.0"
    doc = pypi_doc(flow_component["version"], flow_component, latest=future_version)
    report = Report()

    compare_component_to_pypi("flow", flow_component, doc, report, allow_pinned=True)

    assert report.errors == []
    assert any("explicit platform selection pins" in item for item in report.warnings)


def test_explicit_pin_uses_its_version_specific_metadata(
    flow_component: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_doc = pypi_doc(flow_component["version"], flow_component)
    latest_doc = pypi_doc(flow_component["version"], flow_component, latest="99.0.0")
    latest_doc["info"]["requires_dist"] = ["openadapt-types>=99"]

    monkeypatch.setattr(
        "validate_platform_manifest._fetch_json",
        lambda url: (
            selected_doc if f"/{flow_component['version']}/" in url else latest_doc
        ),
    )
    report = Report()
    check_against_pypi(
        {
            "release_selection": {
                "component_versions": {"flow": flow_component["version"]}
            },
            "components": {"flow": flow_component},
        },
        report,
        require_network=True,
    )

    assert report.errors == []
    assert report.warnings


def test_guard_fails_on_a_tampered_digest(flow_component: dict) -> None:
    """A correct version with a wrong digest must still fail.

    Version-only checking would pass this. The digest is the load-bearing
    claim, so it is checked independently.
    """
    doc = pypi_doc(flow_component["version"], flow_component)
    tampered = deepcopy(flow_component)
    tampered["artifacts"][0]["sha256"] = "0" * 64

    errors = errors_for(tampered, doc)
    assert any("sha256 drift" in error for error in errors)


def test_guard_fails_on_a_tampered_url(flow_component: dict) -> None:
    doc = pypi_doc(flow_component["version"], flow_component)
    tampered = deepcopy(flow_component)
    tampered["artifacts"][0]["url"] = "https://example.invalid/evil.whl"

    errors = errors_for(tampered, doc)
    assert any("URL drift" in error for error in errors)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda component: component["artifacts"].pop(), "filename-set drift"),
        (
            lambda component: component["artifacts"][0].update(type="invented"),
            "type drift",
        ),
        (
            lambda component: component.update(requires_python=">=99"),
            "requires_python drift",
        ),
    ],
)
def test_guard_checks_complete_artifact_metadata(
    flow_component: dict, mutation, message: str
) -> None:
    doc = pypi_doc(flow_component["version"], flow_component)
    mutation(flow_component)
    assert any(message in error for error in errors_for(flow_component, doc))


def test_guard_fails_on_a_tampered_dependency_range(flow_component: dict) -> None:
    doc = pypi_doc(flow_component["version"], flow_component)
    tampered = deepcopy(flow_component)
    tampered["requires"]["openadapt-types"] = [">=0.1.0"]

    errors = errors_for(tampered, doc)
    assert any("dependency-range drift" in error for error in errors)


def test_guard_fails_on_a_version_pypi_never_published(
    flow_component: dict,
) -> None:
    """A manifest may not name a release that does not exist."""
    doc = pypi_doc(flow_component["version"], flow_component)
    invented = deepcopy(flow_component)
    invented["version"] = "1.99.99"

    errors = errors_for(invented, doc)
    assert any("no files" in error for error in errors)


def test_index_propagation_lag_warns_but_does_not_fail(
    flow_component: dict,
) -> None:
    """The release-time false red must not recur.

    Minutes after an upload, PyPI's `info.version` can still report the
    previous release while the new one is already in `releases`. That is not
    drift. A false red at release time is what trained everyone to ignore this
    check, so it warns instead -- while still verifying digests.
    """
    doc = pypi_doc(flow_component["version"], flow_component, latest="1.0.0")

    assert errors_for(flow_component, doc) == []
    assert any("propagation lag" in w for w in warnings_for(flow_component, doc))


def test_propagation_lag_still_verifies_digests(flow_component: dict) -> None:
    """The lag allowance must not become a hole that hides a bad digest."""
    doc = pypi_doc(flow_component["version"], flow_component, latest="1.0.0")
    tampered = deepcopy(flow_component)
    tampered["artifacts"][0]["sha256"] = "0" * 64

    assert any("sha256 drift" in error for error in errors_for(tampered, doc))


def test_release_in_flight_warns_instead_of_failing() -> None:
    """A reviewed version candidate can be ahead of the published manifest.

    The version and changelog reach protected main through a pull request
    before the release App creates its tag. For that window pyproject.toml is
    ahead of the manifest, which is correct because the manifest records only
    published versions.
    """
    report = Report()
    compare_launcher_to_pyproject("1.8.0", "1.9.0", report)

    assert report.errors == []
    assert any("release is in flight" in w for w in report.warnings)


def test_manifest_ahead_of_pyproject_still_fails() -> None:
    """A manifest naming a launcher this repo never produced is an error.

    The lenient direction is one-way. This is the case the pyproject check
    still exists to catch.
    """
    report = Report()
    compare_launcher_to_pyproject("2.0.0", "1.9.0", report)

    assert any("version drift" in error for error in report.errors)


def test_unparseable_launcher_versions_fail_closed() -> None:
    """No ordering can be inferred, so do not guess it is benign."""
    report = Report()
    compare_launcher_to_pyproject("1.9.0", "1.10.0rc1", report)

    assert report.errors, "an unorderable version pair must not be waved through"


def test_matching_launcher_versions_are_silent() -> None:
    report = Report()
    compare_launcher_to_pyproject("1.9.0", "1.9.0", report)

    assert report.errors == []
    assert report.warnings == []


def test_release_retains_generated_bom_without_pushing_main() -> None:
    """Publication emits an exact reviewed-update input without bypassing main."""
    workflow = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "git push origin HEAD:main" not in workflow
    assert "published-platform-manifest.json" in workflow
    assert "published-platform-report.md" in workflow
    assert "platform-manifest.json needs the published launcher release" in workflow


def test_every_manifest_component_is_covered(manifest: dict) -> None:
    """All seven published components are guarded, not just flow."""
    assert set(manifest["components"]) == {
        "launcher",
        "flow",
        "capture",
        "privacy",
        "types",
        "desktop",
        "agent",
    }
    for role, component in manifest["components"].items():
        doc = pypi_doc(component["version"], component)
        report = Report()
        compare_component_to_pypi(role, component, doc, report)
        assert report.errors == [], f"{role} failed self-consistency"


def test_runtime_compatibility_status_is_recomputed(manifest: dict) -> None:
    truthful = Report()
    check_compatibility_status(manifest, truthful)
    assert truthful.errors == []

    tampered = deepcopy(manifest)
    tampered["compatibility_status"] = {
        "status": "dependency-incompatible",
        "basis": "published-metadata-and-desktop-lock",
        "failures": [{"source_role": "invented"}],
    }
    report = Report()
    check_compatibility_status(tampered, report)
    assert any("compatibility_status" in error for error in report.errors)


def test_runtime_requirements_come_from_selected_release_metadata(
    manifest: dict,
) -> None:
    expected = _runtime_unit_requirements(manifest["components"])
    actual = {
        name: unit["requirements"] for name, unit in manifest["runtime_units"].items()
    }
    assert actual == expected

    assert len(manifest["dependency_edges"]) == 11
    components = deepcopy(manifest["components"])
    components["capture"]["version"] = "0.0.1"
    edges = _dependency_edges(components, manifest["runtime_units"])
    status = _compatibility_status(edges)
    assert status["status"] == "dependency-incompatible"
    assert any(
        failure["source_role"] == "launcher"
        and failure["package"] == "openadapt-capture"
        for failure in status["failures"]
    )


def test_version_range_comparison_is_closed() -> None:
    assert _version_satisfies("1.2.0", ">=1.2.0,<2.0.0")
    assert _version_satisfies("1.9.9", ">=1.2.0,<2.0.0")
    assert not _version_satisfies("1.1.99", ">=1.2.0,<2.0.0")
    assert not _version_satisfies("2.0.0", ">=1.2.0,<2.0.0")


def test_release_train_uses_explicit_published_version_inputs() -> None:
    assert _parse_component_versions(["flow=9.8.7", "desktop=6.5.4"]) == {
        "flow": "9.8.7",
        "desktop": "6.5.4",
    }
    with pytest.raises(DriftError, match="ROLE=VERSION"):
        _parse_component_versions(["unknown=1.0.0"])


def test_desktop_release_ref_candidates_include_python_package_tag() -> None:
    """Desktop 0.16.0 published v0.16.0 and no desktop-v0.16.0 tag.

    The generator must bind a tag that exists. It must not invent
    desktop-vX.Y.Z when that native tag was never published.
    """
    assert _release_ref_candidates("desktop", "0.16.0") == (
        "desktop-v0.16.0",
        "v0.16.0",
    )
    assert _release_ref_candidates("privacy", "1.1.0") == ("v1.1.0",)


def test_schema_ranges_are_closed_and_exact(manifest: dict) -> None:
    schemas = manifest["schema_compatibility"]
    assert {
        "workflow_bundle",
        "capture_structural_observation",
        "control_overlay_frame",
        "control_overlay_timeline",
        "human_decision_task",
        "human_decision_receipt",
        "remote_decision_projection",
        "runtime_validation",
    } <= set(schemas)
    assert schemas["capture_structural_observation"]["schema_prefix"] == (
        "openadapt.capture.structural-observation/v"
    )
    for schema in schemas.values():
        accepted = schema["accepted_versions"]
        assert accepted == sorted(set(accepted))
        assert schema["minimum"] == accepted[0]
        assert schema["maximum"] == accepted[-1]
        assert (
            schema["source_commit"]
            == manifest["components"][schema["source_component"]]["provenance"][
                "commit"
            ]
        )

    report = Report()
    check_structure(manifest, report)
    assert report.errors == []

    tampered = deepcopy(manifest)
    tampered_schema = tampered["schema_compatibility"]["human_decision_receipt"]
    tampered_schema["maximum"] = tampered_schema["maximum"] + 1
    report = Report()
    check_structure(tampered, report)
    assert any("range does not match" in error for error in report.errors)


def test_status_contract_rejects_fabricated_substrates_and_strict_unreachable(
    manifest: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {
        "versions": {
            role: component["version"]
            for role, component in manifest["components"].items()
        },
        "substrates": deepcopy(manifest["substrate_drivers"]),
    }
    tampered = deepcopy(manifest)
    tampered["substrate_drivers"][0]["delivery"] = "Invented"
    monkeypatch.setattr("validate_platform_manifest._fetch_json", lambda _url: status)
    report = Report()
    check_against_status(tampered, report, strict=False)
    assert any("substrate_drivers drift" in error for error in report.errors)

    monkeypatch.setattr(
        "validate_platform_manifest._fetch_json",
        lambda _url: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    report = Report()
    check_against_status(manifest, report, strict=True)
    assert report.errors


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["qualification_evidence"][1].update(
            source_object_sha="0" * 39
        ),
        lambda value: value["qualification_evidence"].pop(),
        lambda value: value["schema_compatibility"]["runtime_validation"].update(
            source_commit="0" * 40
        ),
        lambda value: value["signature"].update(algorithm="invented"),
        lambda value: value.update(generated_at="not-a-time"),
        lambda value: value["substrate_drivers"][0].update(public_label="Invented"),
    ],
)
def test_structural_provenance_claims_fail_closed(manifest: dict, mutate) -> None:
    tampered = deepcopy(manifest)
    mutate(tampered)
    report = Report()
    check_structure(tampered, report)
    check_signature_honesty(tampered, report)
    assert report.errors


def test_native_lock_parser_returns_only_exact_platform_packages() -> None:
    lock = """
version = 1

[[package]]
name = "openadapt-flow"
version = "1.2.3"

[[package]]
name = "openadapt-types"
version = "0.4.5"

[[package]]
name = "unrelated"
version = "9.9.9"
"""
    assert _locked_openadapt_versions(lock, {"openadapt-flow", "openadapt-types"}) == {
        "openadapt-flow": "1.2.3",
        "openadapt-types": "0.4.5",
    }


def test_native_lock_digest_is_verified(
    manifest: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = manifest["runtime_units"]["desktop_sidecar"]["resolved"]
    lock = "version = 1\n" + "".join(
        f'\n[[package]]\nname = "{package}"\nversion = "{version}"\n'
        for package, version in resolved.items()
    )
    expected_hash = hashlib.sha256(lock.encode()).hexdigest()
    manifest = deepcopy(manifest)
    manifest["runtime_units"]["desktop_sidecar"]["lock_sha256"] = expected_hash
    monkeypatch.setattr("validate_platform_manifest._fetch_text", lambda _url: lock)

    report = Report()
    check_desktop_sidecar_lock(manifest, report, require_network=True)
    assert report.errors == []

    manifest["runtime_units"]["desktop_sidecar"]["lock_sha256"] = "0" * 64
    report = Report()
    check_desktop_sidecar_lock(manifest, report, require_network=True)
    assert any("lock sha256 drift" in error for error in report.errors)
