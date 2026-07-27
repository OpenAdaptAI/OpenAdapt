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

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_platform_manifest import (  # noqa: E402
    Report,
    compare_component_to_pypi,
    compare_launcher_to_pyproject,
)

MANIFEST_PATH = REPO_ROOT / "platform-manifest.json"


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
    return {
        "info": {"version": latest or version},
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
    """The second guaranteed false red must not recur either.

    python-semantic-release pushes a version-bump commit to main before the
    reconcile job regenerates the manifest. For that window pyproject.toml is
    ahead of the manifest, which is correct -- the manifest records only
    PUBLISHED versions. Failing here made main red after every release.
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


def test_every_manifest_component_is_covered(manifest: dict) -> None:
    """All four published components are guarded, not just flow."""
    assert set(manifest["components"]) == {
        "launcher",
        "flow",
        "capture",
        "desktop",
    }
    for role, component in manifest["components"].items():
        doc = pypi_doc(component["version"], component)
        report = Report()
        compare_component_to_pypi(role, component, doc, report)
        assert report.errors == [], f"{role} failed self-consistency"
